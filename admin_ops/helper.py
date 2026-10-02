#!/usr/bin/env python3
"""Linux host-only helper. Fixed Docker argv; no shell, arbitrary paths or commands.

Protocol: one JSON object followed by LF, <= 1024 bytes, one response then close.
Socket peer must be root or numeric UID 10001 (the dedicated admin container).
"""
import json
import os
from pathlib import Path
import re
import shutil
import socket
import socketserver
import sqlite3
import struct
import subprocess
import threading
import time

CONTAINERS = {'bot': 'newswatch-bot-1', 'worker': 'newswatch-worker-1', 'db': 'newswatch-db-1'}
DOCKER = '/usr/bin/docker'
SOCKET = '/run/newswatch-admin-ops/control.sock'
DATABASE = '/var/lib/newswatch-admin-ops/operations.sqlite3'
DOCKER_SLOTS = threading.BoundedSemaphore(2)


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError('invalid_request')
    if payload == {'action': 'status'}:
        return payload
    if (set(payload) == {'action', 'target', 'id'} and payload['action'] == 'restart'
            and isinstance(payload['target'], str) and payload['target'] in {'bot', 'worker'}
            and isinstance(payload['id'], str)
            and re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}', payload['id'])):
        return payload
    raise ValueError('invalid_request')


def run(argv, timeout=5):
    if not DOCKER_SLOTS.acquire(timeout=2):
        raise TimeoutError('docker_busy')
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=timeout, check=True, text=True,
                              env={'PATH': '/usr/bin:/bin', 'GOMAXPROCS': '2'}, shell=False).stdout
    finally:
        DOCKER_SLOTS.release()


class Operations:
    def __init__(self, path=DATABASE, runner=run):
        self.path, self.runner = str(path), runner
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS operations '
                       '(id TEXT PRIMARY KEY, target TEXT NOT NULL, created REAL NOT NULL, result TEXT NOT NULL)')
        Path(path).chmod(0o600)

    def inspect(self, target):
        # Inspect returns no Env, command, mounts, logs or labels besides expected identity.
        template = ('{"id":{{json .Id}},"status":{{json .State.Status}},'
                    '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}"unknown"{{end}},'
                    '"restarts":{{.RestartCount}},"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
                    '"service":{{json (index .Config.Labels "com.docker.compose.service")}}}')
        result = json.loads(self.runner([DOCKER, 'inspect', '--format', template, CONTAINERS[target]]))
        if (not isinstance(result, dict) or result.get('project') != 'newswatch' or result.get('service') != target
                or not re.fullmatch(r'[0-9a-f]{64}', result.get('id', ''))):
            raise ValueError('container_identity_mismatch')
        return result

    def restart(self, payload):
        validate(payload)
        request_id, target = payload['id'], payload['target']
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT target,result FROM operations WHERE id=?', (request_id,)).fetchone()
            if row:
                return {'result': row[1] if row[0] == target else 'failed', 'id': request_id}
            # A started/unknown operation blocks automatic retries indefinitely until host inspection.
            prior = db.execute('SELECT created,result FROM operations WHERE target=? ORDER BY created DESC LIMIT 1',
                               (target,)).fetchone()
            if prior and (time.time()-prior[0] < 120 or prior[1] in {'started', 'unknown'}):
                return {'result': 'cooldown', 'id': request_id}
            db.execute('INSERT INTO operations VALUES (?,?,?,?)', (request_id, target, time.time(), 'started'))
        try:
            container = self.inspect(target)
            # Immutable container ID prevents a name replacement between inspect and restart.
            self.runner([DOCKER, 'restart', '--time', '20', container['id']], timeout=45)
            result = 'completed'
        except (subprocess.TimeoutExpired, TimeoutError):
            result = 'unknown'
        except (ValueError, KeyError, OSError, subprocess.CalledProcessError):
            # CLI error might arrive after daemon accepted the command. Conservatively block retries.
            result = 'unknown'
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute('UPDATE operations SET result=? WHERE id=?', (result, request_id))
        return {'result': result, 'id': request_id}

    def status(self):
        services = {}
        for target in CONTAINERS:
            try:
                value = self.inspect(target)
                services[target] = {k: value[k] for k in ('status', 'health', 'restarts')}
            except (ValueError, KeyError, OSError, subprocess.SubprocessError):
                services[target] = {'status': 'unknown', 'health': 'unknown', 'restarts': '—'}
        memory = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            if key in {'MemTotal', 'MemAvailable'}:
                memory[key] = int(value.strip().split()[0]) // 1024
        host = {'memory_total_mb': memory.get('MemTotal'), 'memory_available_mb': memory.get('MemAvailable'),
                'disk_free_gb': round(shutil.disk_usage('/').free/2**30, 1),
                'uptime_hours': round(float(Path('/proc/uptime').read_text().split()[0])/3600, 1),
                'load': ', '.join(f'{v:.2f}' for v in os.getloadavg())}
        with sqlite3.connect(self.path) as db:
            operations = [{'id': r[0], 'target': r[1], 'result': r[2]} for r in db.execute(
                'SELECT id,target,result FROM operations ORDER BY created DESC LIMIT 10')]
        return {'result': 'ok', 'services': services, 'host': host, 'operations': operations}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(2)
        _, uid, _ = struct.unpack('3i', self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid not in {0, 10001}:
            return
        try:
            raw = self.rfile.readline(1025)
            if len(raw) > 1024 or not raw.endswith(b'\n'):
                raise ValueError
            payload = validate(json.loads(raw))
            result = self.server.ops.status() if payload['action'] == 'status' else self.server.ops.restart(payload)
        except (ValueError, TypeError, OSError, sqlite3.Error):
            result = {'result': 'failed'}
        self.wfile.write(json.dumps(result).encode()+b'\n')


# BaseServer fallback permits protocol unit tests on Windows; main refuses non-Linux.
# It never exposes a TCP listener.
class Server(socketserver.ThreadingMixIn, getattr(socketserver, 'UnixStreamServer', socketserver.BaseServer)):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address, ops):
        self.ops = ops
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def main():
    if not hasattr(socket, 'SO_PEERCRED') or not hasattr(socketserver, 'UnixStreamServer'):
        raise SystemExit('Host helper requires Linux')
    if os.geteuid() != 0:
        raise SystemExit('Host helper must run as root via its systemd unit')
    path = Path(SOCKET)
    if path.exists():
        if not path.is_socket():
            raise SystemExit('Refusing to replace non-socket path')
        path.unlink()
    with Server(SOCKET, Operations()) as server:
        os.chown(SOCKET, 0, 10001)
        os.chmod(SOCKET, 0o660)
        server.serve_forever(poll_interval=1)


if __name__ == '__main__':
    main()
