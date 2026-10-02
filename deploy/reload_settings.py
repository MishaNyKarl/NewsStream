"""Root-only maintenance: reapply environment to the currently deployed images."""
import importlib.util
import os
import sys


def main():
    if os.geteuid() != 0 or sys.argv[1:] not in [['bot'], ['admin']]:
        raise SystemExit('Usage as root: python3 deploy/reload_settings.py bot|admin')
    spec = importlib.util.spec_from_file_location('newsstream_host', '/usr/local/lib/newswatch/deploy_remote.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    with (host.STATE / 'deploy.lock').open('a') as lock:
        host.fcntl.flock(lock, host.fcntl.LOCK_EX)
        source = (host.ROOT / 'current').resolve()
        sha = host.revision((source / 'REVISION').read_text().strip())
        if sys.argv[1] == 'bot':
            args = host.compose(source, 'newswatch:' + sha, sha, 'up', '-d', '--no-build', '--no-deps',
                '--force-recreate', '--wait', '--wait-timeout', '120', 'bot', 'worker')
        else:
            args = host.admin_compose(source, 'newswatch-admin:' + sha, sha, 'up', '-d', '--no-build',
                '--force-recreate', '--wait', '--wait-timeout', '120', 'admin')
        host.command(args, timeout=150)


if __name__ == '__main__':
    main()
