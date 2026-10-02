#!/bin/sh
# One-time bootstrap as root; source directory and public SSH key are explicit.
set -eu
umask 077
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
public_key=$1
test "$(id -u)" = 0
test -f /opt/news-watch-bot/.env
if ! id newsdeploy >/dev/null 2>&1; then
    useradd --system --user-group --create-home --home-dir /var/lib/newswatch-deploy --shell /bin/sh newsdeploy
fi
test "$(getent passwd newsdeploy | cut -d: -f6)" = /var/lib/newswatch-deploy
install -d -m 755 /usr/local/lib/newswatch
install -m 755 "$source_dir/remote.py" /usr/local/lib/newswatch/deploy_remote.py
install -d -m 700 /opt/news-watch-bot/deploy-state
install -d -m 750 /opt/news-watch-bot/releases
install -d -o newsdeploy -g newsdeploy -m 700 /var/lib/newswatch-deploy/.ssh /var/lib/newswatch-deploy/incoming
python3 - "$public_key" <<'PY'
from pathlib import Path
import re, sys
key = Path(sys.argv[1]).read_text().strip()
if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]+)?', key):
    raise SystemExit('Invalid deploy public key')
target = Path('/var/lib/newswatch-deploy/.ssh/authorized_keys')
target.write_text('restrict,command="/usr/bin/python3 /usr/local/lib/newswatch/deploy_remote.py ssh" ' + key + '\n')
PY
chown newsdeploy:newsdeploy /var/lib/newswatch-deploy/.ssh/authorized_keys
chmod 600 /var/lib/newswatch-deploy/.ssh/authorized_keys
cat > /usr/local/sbin/newswatch-deploy <<'SH'
#!/bin/sh
exec /usr/bin/python3 /usr/local/lib/newswatch/deploy_remote.py "$@"
SH
chmod 755 /usr/local/sbin/newswatch-deploy
cat > /etc/sudoers.d/newswatch-deploy <<'SUDO'
newsdeploy ALL=(root) NOPASSWD: /usr/local/sbin/newswatch-deploy enqueue *, /usr/local/sbin/newswatch-deploy status *
SUDO
chmod 440 /etc/sudoers.d/newswatch-deploy
visudo -cf /etc/sudoers.d/newswatch-deploy
python3 -m py_compile /usr/local/lib/newswatch/deploy_remote.py
echo 'RESTRICTED_DEPLOY_ACCOUNT_READY'
