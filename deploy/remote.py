#!/usr/bin/env python3
"""Restricted SSH receiver and detached, rollback-capable host deployment.

Installed root-owned outside the checkout. Python standard library only.
The SSH account has no Docker access and can request only upload/deploy/status.
"""
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
try:
    import fcntl
except ImportError:  # Archive/security tests also run on Windows.
    fcntl = None

ROOT = Path('/opt/news-watch-bot')
INCOMING = Path('/var/lib/newswatch-deploy/incoming')
STATE = ROOT / 'deploy-state'
RELEASES = ROOT / 'releases'
HOST_SCRIPT = '/usr/local/lib/newswatch/deploy_remote.py'
MAX_ARCHIVE = 20 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
REVISION = re.compile(r'[0-9a-f]{40}')
TERMINAL = {'succeeded', 'failed', 'rolled_back'}


class DeployError(Exception):
    pass


def revision(value):
    if not isinstance(value, str) or not REVISION.fullmatch(value):
        raise DeployError('invalid_revision')
    return value


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def load_state(sha):
    path = STATE / revision(sha) / 'status.json'
    return json.loads(path.read_text()) if path.exists() else {'revision': sha, 'status': 'unknown'}


def save_state(data, **changes):
    data.update(changes, updated_at=now())
    write_json(STATE / data['revision'] / 'status.json', data)
    write_json(STATE / 'last.json', data)


def validate_archive(path, sha):
    """No traversal, links, devices, local settings, or decompression bombs."""
    revision(sha)
    if path.stat().st_size > MAX_ARCHIVE:
        raise DeployError('archive_too_large')
    seen, total, manifest = set(), 0, None
    with tarfile.open(path, 'r:gz') as archive:
        for index, member in enumerate(archive):
            name = PurePosixPath(member.name)
            if (index > 5000 or name.is_absolute() or '..' in name.parts or '\\' in member.name
                    or not name.parts or any(ord(c) < 32 for c in member.name)):
                raise DeployError('unsafe_archive_path')
            if member.name in seen:
                raise DeployError('duplicate_archive_path')
            seen.add(member.name)
            if not (member.isfile() or member.isdir()):
                raise DeployError('archive_links_or_devices')
            for part in name.parts:
                if (part in {'.git', 'work', 'outputs', 'backups', 'deploy-state', 'releases', 'current'}
                        or part.startswith('private-access')
                        or (part.startswith('.env') and part not in {'.env.example', '.env.admin.example'})
                        or part.endswith(('.key', '.pem', '.p12', '.pfx'))):
                    raise DeployError('private_file_in_archive')
            if member.size < 0:
                raise DeployError('invalid_archive_size')
            total += member.size
            if total > MAX_EXPANDED:
                raise DeployError('expanded_archive_too_large')
            if member.name == 'REVISION':
                if member.size > 64:
                    raise DeployError('invalid_manifest')
                manifest = archive.extractfile(member).read().decode('ascii').strip()
    if manifest != sha or not {'Dockerfile', 'compose.yaml', 'app/main.py'} <= seen:
        raise DeployError('release_manifest_mismatch')
    return total


def extract_release(path, destination, sha):
    validate_archive(path, sha)
    destination.mkdir(mode=0o750)
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as stream:
                    shutil.copyfileobj(archive.extractfile(member), stream)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)


def command(args, timeout=180, capture=False):
    try:
        result = subprocess.run(args, check=True, timeout=timeout,
            stdout=subprocess.PIPE if capture else None, text=True)
        return result.stdout.strip() if capture else None
    except (subprocess.SubprocessError, OSError):
        raise DeployError('command_failed_' + Path(args[0]).name) from None


def compose(source, image, sha, *args, label_revision=None):
    override = STATE / sha / ('image-' + hashlib.sha256(image.encode()).hexdigest()[:12] + '.yaml')
    label = label_revision or sha
    override.write_text('services:\n  bot:\n    image: ' + json.dumps(image) +
        '\n    labels:\n      io.newsstream.revision: ' + json.dumps(label) +
        '\n  worker:\n    image: ' + json.dumps(image) +
        '\n    labels:\n      io.newsstream.revision: ' + json.dumps(label) + '\n')
    return ['docker', 'compose', '--project-directory', str(source), '--env-file', str(ROOT / '.env'),
        '-p', 'newswatch', '-f', str(source / 'compose.yaml'), '-f', str(override), *args]


def switch_pointer(source):
    temporary = ROOT / 'current.next'
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(source, target_is_directory=True)
    temporary.replace(ROOT / 'current')


def upload(sha, stream):
    revision(sha)
    INCOMING.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='upload-', dir=INCOMING)
    total = 0
    try:
        with os.fdopen(fd, 'wb') as output:
            while chunk := stream.read(65536):
                total += len(chunk)
                if total > MAX_ARCHIVE:
                    raise DeployError('archive_too_large')
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        validate_archive(Path(temporary), sha)
        os.replace(temporary, INCOMING / (sha + '.tar.gz'))
        return {'revision': sha, 'uploaded_bytes': total}
    finally:
        Path(temporary).unlink(missing_ok=True)


def enqueue(sha):
    revision(sha)
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'enqueue.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = load_state(sha)
        if data['status'] in {'queued', 'running', 'succeeded'}:
            return public_state(data)
        job = STATE / sha
        job.mkdir(mode=0o700, exist_ok=True)
        source = INCOMING / (sha + '.tar.gz')
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as incoming:
            if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                raise DeployError('invalid_upload')
            with (job / 'source.tar.gz').open('wb') as output:
                remaining = MAX_ARCHIVE + 1
                while remaining and (chunk := incoming.read(min(65536, remaining))):
                    output.write(chunk)
                    remaining -= len(chunk)
                if remaining == 0:
                    raise DeployError('archive_too_large')
        validate_archive(job / 'source.tar.gz', sha)
        data = {'revision': sha, 'status': 'queued', 'phase': 'queued', 'created_at': now()}
        save_state(data)
        try:
            command(['systemd-run', '--unit=newswatch-deploy-' + sha, '--collect', '--no-block',
                '--property=Type=oneshot', '--property=TimeoutStartSec=1800', '--property=TimeoutStopSec=180',
                '--property=ExecStopPost=/usr/bin/python3 ' + HOST_SCRIPT + ' rescue ' + sha,
                '/usr/bin/python3', HOST_SCRIPT, 'run', sha], timeout=30)
        except DeployError:
            current = load_state(sha)
            if current['status'] in {'running', 'succeeded', 'rolled_back'}:
                return public_state(current)
            save_state(data, status='failed', phase='enqueue', error='cannot_start_server_job')
            raise
        return public_state(data)


def public_state(data):
    allowed = {'revision', 'status', 'phase', 'created_at', 'updated_at', 'started_at',
        'finished_at', 'error', 'admin_deployed', 'backup'}
    return {key: value for key, value in data.items() if key in allowed}


def restore(data):
    previous = data.get('previous')
    if not previous:
        return
    source = Path(previous['source'])
    if source != ROOT and source.parent != RELEASES:
        raise DeployError('invalid_previous_release')
    command(compose(source, previous['image'], data['revision'], 'up', '-d', '--no-build',
        '--no-deps', '--wait', '--wait-timeout', '120', 'bot', 'worker',
        label_revision=previous.get('revision', 'legacy')), timeout=150)
    if source != ROOT:
        switch_pointer(source)
    elif (ROOT / 'current').is_symlink():
        (ROOT / 'current').unlink()


def run(sha):
    job = STATE / revision(sha)
    with (STATE / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        active_file = STATE / 'active.json'
        if active_file.exists():
            older_sha = json.loads(active_file.read_text())['revision']
            older = load_state(older_sha)
            if older['status'] not in {'succeeded', 'rolled_back'} and older.get('previous'):
                restore(older)
                save_state(older, status='rolled_back', phase='rescued', error='deployment_interrupted', finished_at=now())
        data = load_state(sha)
        if data['status'] == 'succeeded':
            return
        save_state(data, status='running', phase='preparing', started_at=now(), error=None)
        write_json(active_file, {'revision': sha})
        try:
            previous_source = (ROOT / 'current').resolve() if (ROOT / 'current').is_symlink() else ROOT
            previous_id = command(['docker', 'inspect', '--format', '{{.Image}}', 'newswatch-bot-1'], capture=True)
            previous_revision = command(['docker', 'inspect', '--format',
                '{{index .Config.Labels "io.newsstream.revision"}}', 'newswatch-bot-1'], capture=True)
            if not REVISION.fullmatch(previous_revision):
                previous_revision = 'legacy'
            previous_tag = 'newswatch:rollback-' + sha
            command(['docker', 'image', 'tag', previous_id, previous_tag])
            data['previous'] = {'source': str(previous_source), 'image': previous_tag, 'revision': previous_revision}
            save_state(data)
            RELEASES.mkdir(mode=0o750, exist_ok=True)
            release = RELEASES / sha
            if release.exists():
                if release.is_symlink() or release.resolve() == previous_source:
                    raise DeployError('release_already_active')
                shutil.rmtree(release)
            extract_release(job / 'source.tar.gz', release, sha)
            (release / '.env').symlink_to(ROOT / '.env')
            env_digest = hashlib.sha256((ROOT / '.env').read_bytes()).hexdigest()
            image = 'newswatch:' + sha
            save_state(data, phase='building')
            command(compose(release, image, sha, 'build', 'bot'), timeout=900)
            save_state(data, phase='backup')
            backup = ROOT / 'backups' / ('deploy-' + sha)
            backup.mkdir(mode=0o700, exist_ok=True)
            with (backup / 'database.dump').open('wb') as output:
                subprocess.run(['docker', 'exec', 'newswatch-db-1', 'pg_dump', '-U', 'newswatch',
                    '-d', 'newswatch', '-Fc'], stdout=output, check=True, timeout=120)
            if (backup / 'database.dump').stat().st_size < 100:
                raise DeployError('empty_backup')
            data['backup'] = backup.name
            save_state(data, phase='migrating')
            for action in [('upgrade', 'head'), ('check',)]:
                command(['docker', 'run', '--rm', '--memory', '256m', '--cpus', '0.7',
                    '--network', 'newswatch_default', '--env-file', str(ROOT / '.env'), image,
                    'alembic', *action], timeout=180)
            save_state(data, phase='waiting_for_checks')
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                count = command(['docker', 'exec', 'newswatch-db-1', 'psql', '-U', 'newswatch',
                    '-d', 'newswatch', '-Atc', 'SELECT count(*) FROM stories WHERE lock_until>now();'],
                    capture=True)
                if count == '0':
                    break
                time.sleep(5)
            save_state(data, phase='switching')
            command(compose(release, image, sha, 'up', '-d', '--no-build', '--no-deps',
                '--wait', '--wait-timeout', '120', 'bot', 'worker'), timeout=150)
            if hashlib.sha256((ROOT / '.env').read_bytes()).hexdigest() != env_digest:
                raise DeployError('settings_changed_during_deploy')
            save_state(data, phase='verifying')
            for container in ('newswatch-bot-1', 'newswatch-worker-1'):
                health = command(['docker', 'inspect', '--format', '{{.State.Health.Status}}', container], capture=True)
                if health != 'healthy':
                    raise DeployError('application_unhealthy')
            switch_pointer(release)
            save_state(data, status='succeeded', phase='complete', finished_at=now(), admin_deployed=False)
        except BaseException as exc:
            error = str(exc) if isinstance(exc, DeployError) else type(exc).__name__
            save_state(data, status='running', phase='restoring', error=error)
            try:
                restore(data)
                save_state(data, status='rolled_back', phase='restored', finished_at=now())
            except BaseException:
                save_state(data, status='failed', phase='recovery_failed', error=error, finished_at=now())
            raise DeployError('deployment_failed') from None


def rescue(sha):
    """ExecStopPost also runs if SSH disappears or the worker process is killed."""
    with (STATE / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = load_state(sha)
        if data['status'] in {'succeeded', 'rolled_back'}:
            return
        try:
            restore(data)
            save_state(data, status='rolled_back', phase='rescued', error='deployment_interrupted', finished_at=now())
        except BaseException:
            save_state(data, status='failed', phase='recovery_failed', error='deployment_interrupted', finished_at=now())


def parse_ssh(text):
    parts = shlex.split(text or '')
    if len(parts) != 2 or parts[0] not in {'upload', 'deploy', 'status'}:
        raise DeployError('command_not_allowed')
    return parts[0], revision(parts[1])


def main():
    os.umask(0o077)
    try:
        if len(sys.argv) == 2 and sys.argv[1] == 'ssh':
            action, sha = parse_ssh(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
            if action == 'upload':
                result = upload(sha, sys.stdin.buffer)
            else:
                result = subprocess.run(['sudo', '-n', '/usr/local/sbin/newswatch-deploy',
                    'enqueue' if action == 'deploy' else 'status', sha], check=False)
                return result.returncode
        else:
            if os.geteuid() != 0 or len(sys.argv) != 3:
                raise DeployError('root_control_required')
            action, sha = sys.argv[1], revision(sys.argv[2])
            if action == 'enqueue':
                result = enqueue(sha)
            elif action == 'status':
                result = public_state(load_state(sha))
            elif action in {'run', 'rescue'}:
                log = STATE / sha / 'deploy.log'
                with log.open('a', buffering=1) as output:
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                        os.dup2(output.fileno(), 1)
                        os.dup2(output.fileno(), 2)
                        (run if action == 'run' else rescue)(sha)
                return 0
            else:
                raise DeployError('command_not_allowed')
        print(json.dumps(result))
        return 0
    except BaseException as exc:
        code = str(exc) if isinstance(exc, DeployError) else type(exc).__name__
        print(json.dumps({'status': 'failed', 'error': code}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
