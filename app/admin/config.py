import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from argon2 import extract_parameters, Type


@dataclass(frozen=True)
class Config:
    database_url: str = field(repr=False)
    password_hash: str = field(repr=False)
    origin: str
    state_path: Path = Path('/data/admin.sqlite3')
    docs_path: Path = Path('docs')
    ops_socket: str = ''
    username: str = 'admin'
    session_seconds: int = 28800
    idle_seconds: int = 1800
    provider: str = ''
    model: str = ''
    key_fingerprint: str = ''
    bot_username: str = ''
    revision: str = 'не указан'

    def __post_init__(self):
        parsed = urlsplit(self.origin)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError('ADMIN_ORIGIN must be an HTTPS origin without a trailing slash')
        params = extract_parameters(self.password_hash)
        if (params.type != Type.ID or not 19456 <= params.memory_cost <= 65536
                or not 2 <= params.time_cost <= 5 or not 1 <= params.parallelism <= 4
                or params.salt_len < 16 or params.hash_len < 32):
            raise ValueError('Use python -m app.admin.password to generate the hash')
        if not self.database_url.startswith(('postgresql+asyncpg://', 'sqlite+aiosqlite://')):
            raise ValueError('Unsupported DATABASE_URL')
        if self.key_fingerprint and not re.fullmatch(r'sha256:[0-9a-f]{12}', self.key_fingerprint):
            raise ValueError('ADMIN_LLM_KEY_FINGERPRINT must contain a SHA256 fingerprint, never a key')
        if not 300 <= self.idle_seconds <= self.session_seconds <= 86400:
            raise ValueError('Invalid session lifetime')

    @classmethod
    def from_env(cls):
        return cls(
            database_url=os.environ['DATABASE_URL'], password_hash=os.environ['ADMIN_PASSWORD_HASH'],
            origin=os.environ['ADMIN_ORIGIN'], state_path=Path(os.getenv('ADMIN_STATE_PATH', '/data/admin.sqlite3')),
            docs_path=Path(os.getenv('ADMIN_DOCS_PATH', 'docs')), ops_socket=os.getenv('ADMIN_OPS_SOCKET', ''),
            username=os.getenv('ADMIN_USERNAME', 'admin'),
            session_seconds=int(os.getenv('ADMIN_SESSION_SECONDS', '28800')),
            idle_seconds=int(os.getenv('ADMIN_IDLE_SECONDS', '1800')),
            provider=os.getenv('LLM_PROVIDER', ''), model=os.getenv('LLM_MODEL', ''),
            key_fingerprint=os.getenv('ADMIN_LLM_KEY_FINGERPRINT', ''),
            bot_username=os.getenv('ADMIN_BOT_USERNAME', ''), revision=os.getenv('ADMIN_REVISION', 'не указан'),
        )
