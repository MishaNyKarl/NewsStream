"""Wait for the server-owned deployment; losing this client never stops the bot."""
import json
import subprocess
import sys
import time


def main():
    host, user, key, known_hosts, revision = sys.argv[1:]
    deadline = time.monotonic() + 1700
    last = None
    while time.monotonic() < deadline:
        result = subprocess.run(['ssh', '-i', key, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UserKnownHostsFile=' + known_hosts, '-o', 'ConnectTimeout=15',
            user + '@' + host, 'status ' + revision], capture_output=True, text=True, timeout=30)
        if result.returncode:
            print('Waiting for the server connection...', flush=True)
            time.sleep(10)
            continue
        data = json.loads(result.stdout)
        marker = (data.get('status'), data.get('phase'))
        if marker != last:
            print('Deployment:', *marker, flush=True)
            last = marker
        if data.get('status') == 'succeeded':
            print('Healthy release:', revision)
            return 0
        if data.get('status') in {'failed', 'rolled_back'}:
            print('Deployment did not complete:', data.get('error', 'server_error'))
            return 1
        time.sleep(10)
    print('Client timeout; deployment continues on the server. Inspect server state before retrying.')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
