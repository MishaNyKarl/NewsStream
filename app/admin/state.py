"""Private SQLite state, separate from the read-only application database.

All admission/nonce transitions use BEGIN IMMEDIATE, including across processes.
Only hashes of opaque browser session tokens are stored. Audit is durable, not
tamper-proof against an administrator who controls the host or this volume.
"""
import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class State:
    def __init__(self, path, credential):
        self.path = path
        self.credential = digest(credential)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, csrf TEXT NOT NULL, authenticated INTEGER NOT NULL,
                    created REAL NOT NULL, touched REAL NOT NULL, credential TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (ip TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS attempts_created ON attempts(created);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, created REAL NOT NULL, actor TEXT NOT NULL,
                    action TEXT NOT NULL, target TEXT NOT NULL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS nonces (
                    id TEXT PRIMARY KEY, session TEXT NOT NULL, target TEXT NOT NULL,
                    expires REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS web_accounts (
                    username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, role TEXT NOT NULL, enabled INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS session_principals (
                    session TEXT PRIMARY KEY, username TEXT NOT NULL, revision TEXT NOT NULL);
            ''')
        path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA busy_timeout=5000')
            db.execute('BEGIN IMMEDIATE')
            yield db
            if db.in_transaction:
                db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            db.close()

    def session(self, token, absolute, idle):
        if not token or len(token) > 128:
            return None
        now = time.time()
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE created < ? OR touched < ? OR credential != ?',
                       (now - absolute, now - idle, self.credential))
            row = db.execute('SELECT * FROM sessions WHERE id=?', (digest(token),)).fetchone()
            if row:
                principal = db.execute('SELECT * FROM session_principals WHERE session=?', (row['id'],)).fetchone()
                extra = {}
                if principal:
                    account = db.execute('SELECT * FROM web_accounts WHERE username=?', (principal['username'],)).fetchone()
                    if not account or not account['enabled'] or digest(account['password_hash']) != principal['revision']:
                        db.execute('DELETE FROM sessions WHERE id=?', (row['id'],))
                        return None
                    extra = {'principal': account['username'], 'role': account['role']}
                db.execute('UPDATE sessions SET touched=? WHERE id=?', (now, row['id']))
                return dict(row) | extra

    def web_account(self, username):
        with self.db() as db:
            row = db.execute('SELECT * FROM web_accounts WHERE username=? AND enabled=1', (username,)).fetchone()
            return dict(row) if row else None

    def web_accounts(self):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT username,role,enabled FROM web_accounts ORDER BY username')]

    def set_web_account(self, username, password_hash, role, enabled, actor):
        with self.db() as db:
            existing = db.execute('SELECT * FROM web_accounts WHERE username=?', (username,)).fetchone()
            if existing and not password_hash:
                password_hash = existing['password_hash']
            if not password_hash:
                raise ValueError('Для нового аккаунта задайте пароль.')
            db.execute('INSERT INTO web_accounts VALUES(?,?,?,?) ON CONFLICT(username) DO UPDATE SET '
                       'password_hash=excluded.password_hash,role=excluded.role,enabled=excluded.enabled',
                       (username, password_hash, role, int(enabled)))
            db.execute('DELETE FROM sessions WHERE id IN (SELECT session FROM session_principals WHERE username=?)', (username,))
            self._audit(db, actor, 'web_account_changed', username, role if enabled else 'disabled')

    def bind_principal(self, token, username, password_hash):
        with self.db() as db:
            db.execute('INSERT INTO session_principals VALUES(?,?,?)', (digest(token), username, digest(password_hash)))

    def new_session(self, authenticated=False, old_token=None):
        token, csrf, now = secrets.token_urlsafe(32), secrets.token_urlsafe(32), time.time()
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE created < ? OR credential != ?', (now-86400, self.credential))
            db.execute('DELETE FROM session_principals WHERE session NOT IN (SELECT id FROM sessions)')
            if old_token:
                db.execute('DELETE FROM sessions WHERE id=?', (digest(old_token),))
            # Bound anonymous state even under repeated GET /login traffic.
            db.execute('DELETE FROM sessions WHERE authenticated=0 AND id IN '
                       '(SELECT id FROM sessions WHERE authenticated=0 ORDER BY created DESC LIMIT -1 OFFSET 500)')
            db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?)',
                       (digest(token), csrf, int(authenticated), now, now, self.credential))
        return token, csrf

    def logout(self, token):
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE id=?', (digest(token),))

    def admit_login(self, ip):
        now = time.time()
        with self.db() as db:
            db.execute('DELETE FROM attempts WHERE created < ?', (now - 900,))
            total = db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]
            per_ip = db.execute('SELECT COUNT(*) FROM attempts WHERE ip=?', (digest(ip),)).fetchone()[0]
            if total >= 25 or per_ip >= 5:
                return False
            db.execute('INSERT INTO attempts VALUES (?,?)', (digest(ip), now))
            return True

    def audit(self, actor, action, target='', result='ok'):
        with self.db() as db:
            self._audit(db, actor, action, target, result)

    @staticmethod
    def _audit(db, actor, action, target, result):
        db.execute('INSERT INTO audit(created,actor,action,target,result) VALUES (?,?,?,?,?)',
                   (time.time(), actor, action, target, result))

    def audit_rows(self, page):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 51 OFFSET ?',
                                              ((page-1)*50,))]

    def nonce(self, session, target):
        value = str(uuid.uuid4())
        with self.db() as db:
            db.execute('DELETE FROM nonces WHERE expires < ?', (time.time(),))
            db.execute('INSERT INTO nonces(id,session,target,expires) VALUES (?,?,?,?)',
                       (value, session, target, time.time()+180))
        return value

    def consume(self, nonce, session, target, actor, audit_action='restart_requested'):
        with self.db() as db:
            updated = db.execute('UPDATE nonces SET consumed=1 WHERE id=? AND session=? AND target=? '
                                 'AND expires>=? AND consumed=0', (nonce, session, target, time.time()))
            if updated.rowcount != 1:
                return False
            self._audit(db, actor, audit_action, target, nonce)
            return True

    def calculator(self):
        with self.db() as db:
            row = db.execute('SELECT data FROM settings WHERE id=1').fetchone()
            return json.loads(row[0]) if row else {}

    def save_calculator(self, values, actor):
        with self.db() as db:
            db.execute('INSERT INTO settings VALUES (1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                       (json.dumps(values),))
            self._audit(db, actor, 'calculator_saved', '', 'ok')
