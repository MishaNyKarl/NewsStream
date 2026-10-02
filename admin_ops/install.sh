#!/bin/sh
# Run manually as root on the host after reviewing helper.py. Never restarts bot/worker.
set -eu
[ "$(id -u)" = 0 ] || { echo 'Run as root' >&2; exit 1; }
[ -x /usr/bin/docker ] && [ -x /usr/bin/python3 ]
# systemd resolves Group=10001 through NSS; a numeric chown alone is insufficient.
if ! getent group 10001 >/dev/null; then
    groupadd --gid 10001 newswatch-admin
fi
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install -d -m 0755 /opt/newswatch-admin-ops
install -o root -g root -m 0644 "$script_dir/helper.py" /opt/newswatch-admin-ops/helper.py
install -o root -g root -m 0644 "$script_dir/newswatch-admin-ops.service" /etc/systemd/system/newswatch-admin-ops.service
systemctl daemon-reload
systemctl enable newswatch-admin-ops.service
systemctl restart newswatch-admin-ops.service
systemctl status --no-pager newswatch-admin-ops.service
