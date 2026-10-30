"""Connection records: one authorized account per row (spec section 6.4)."""
from __future__ import annotations

import asyncio
import enum
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mediadl.auth.vault import SealedToken, TokenVault
from mediadl.errors import AuthRequiredError
from mediadl.obs.logging import get_logger

log = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    connector_id TEXT NOT NULL,
    auth_mode TEXT NOT NULL,
    provider_user_id TEXT,
    provider_username TEXT,
    scopes_granted TEXT NOT NULL DEFAULT '[]',
    token_ciphertext BLOB,
    token_nonce BLOB,
    key_version INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT,
    last_refreshed_at TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_connections_principal
    ON connections(principal_id, connector_id);
CREATE TABLE IF NOT EXISTS auth_states (
    state_hash TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    connector_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    created_at REAL NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    principal_id TEXT,
    connection_id TEXT,
    action TEXT NOT NULL,
    detail_json TEXT
);
"""


class ConnectionStatus(enum.StrEnum):
    ACTIVE = 'active'
    EXPIRED = 'expired'
    REVOKED = 'revoked'
    PENDING = 'pending'


@dataclass(slots=True)
class Connection:
    id: str
    principal_id: str
    connector_id: str
    auth_mode: str
    status: ConnectionStatus
    provider_user_id: str | None = None
    provider_username: str | None = None
    scopes_granted: tuple[str, ...] = ()
    expires_at: datetime | None = None
    last_refreshed_at: datetime | None = None
    key_version: int = 1
    created_at: str = ''
    updated_at: str = ''

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= datetime.now(UTC)

    def needs_refresh(self, *, min_age: timedelta = timedelta(days=7),
                      window: timedelta = timedelta(days=15)) -> bool:
        """Refresh when the token is old enough *and* expiry is approaching.

        Meta rejects a refresh for tokens younger than 24 hours, so the lower
        bound matters as much as the upper one.
        """
        if self.expires_at is None or self.status is not ConnectionStatus.ACTIVE:
            return False
        now = datetime.now(UTC)
        age_ok = self.last_refreshed_at is None or (now - self.last_refreshed_at) >= min_age
        expiring = (self.expires_at - now) <= window
        return age_ok or expiring


class ConnectionStore:
    def __init__(self, path: Path | str, vault: TokenVault) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.vault = vault
        self._lock = asyncio.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._conn.executescript(SCHEMA)
        try:
            self.path.chmod(0o600)
        except OSError:  # pragma: no cover - platform dependent
            pass

    def close(self) -> None:
        self._conn.close()

    async def _run(self, fn, *args):  # type: ignore[no-untyped-def]
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    # -- writes ------------------------------------------------------------
    def _save(
        self, connection: Connection, token: str | None, principal_id: str
    ) -> Connection:
        now = datetime.now(UTC).isoformat()
        sealed: SealedToken | None = None
        if token is not None:
            sealed = self.vault.seal(
                token, connection_id=connection.id, principal_id=principal_id
            )
        existing = self._conn.execute(
            'SELECT id FROM connections WHERE id = ?', (connection.id,)
        ).fetchone()
        if existing is None:
            self._conn.execute(
                """INSERT INTO connections (id, principal_id, connector_id, auth_mode,
                       provider_user_id, provider_username, scopes_granted, token_ciphertext,
                       token_nonce, key_version, expires_at, last_refreshed_at, status,
                       created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (connection.id, connection.principal_id, connection.connector_id,
                 connection.auth_mode, connection.provider_user_id, connection.provider_username,
                 json.dumps(list(connection.scopes_granted)),
                 sealed.ciphertext if sealed else None, sealed.nonce if sealed else None,
                 sealed.key_version if sealed else 1,
                 connection.expires_at.isoformat() if connection.expires_at else None,
                 connection.last_refreshed_at.isoformat() if connection.last_refreshed_at else None,
                 str(connection.status), now, now),
            )
        else:
            self._conn.execute(
                """UPDATE connections SET provider_user_id=?, provider_username=?,
                       scopes_granted=?, token_ciphertext=COALESCE(?, token_ciphertext),
                       token_nonce=COALESCE(?, token_nonce), key_version=?,
                       expires_at=?, last_refreshed_at=?, status=?, updated_at=?
                   WHERE id=?""",
                (connection.provider_user_id, connection.provider_username,
                 json.dumps(list(connection.scopes_granted)),
                 sealed.ciphertext if sealed else None, sealed.nonce if sealed else None,
                 sealed.key_version if sealed else connection.key_version,
                 connection.expires_at.isoformat() if connection.expires_at else None,
                 connection.last_refreshed_at.isoformat() if connection.last_refreshed_at else None,
                 str(connection.status), now, connection.id),
            )
        self._audit(connection.principal_id, connection.id, 'connection_saved',
                    {'status': str(connection.status), 'connector': connection.connector_id})
        return self._get(connection.id)

    def _audit(self, principal_id: str | None, connection_id: str | None,
               action: str, detail: dict | None = None) -> None:
        self._conn.execute(
            'INSERT INTO audit_log (at, principal_id, connection_id, action, detail_json) '
            'VALUES (?,?,?,?,?)',
            (datetime.now(UTC).isoformat(), principal_id, connection_id, action,
             json.dumps(detail or {})),
        )

    def _get(self, connection_id: str) -> Connection:
        row = self._conn.execute(
            'SELECT * FROM connections WHERE id = ?', (connection_id,)
        ).fetchone()
        if row is None:
            raise AuthRequiredError('connection not found', connection_id=connection_id)
        return _row_to_connection(row)

    def _get_owned(self, principal_id: str, connection_id: str | None,
                   connector_id: str | None) -> Connection | None:
        """Always scoped by principal: no cross-user lookup is possible."""
        if connection_id:
            row = self._conn.execute(
                'SELECT * FROM connections WHERE id = ? AND principal_id = ?',
                (connection_id, principal_id),
            ).fetchone()
        else:
            row = self._conn.execute(
                """SELECT * FROM connections WHERE principal_id = ? AND connector_id = ?
                   AND status = ? ORDER BY updated_at DESC LIMIT 1""",
                (principal_id, connector_id, str(ConnectionStatus.ACTIVE)),
            ).fetchone()
        return _row_to_connection(row) if row else None

    def _token(self, connection: Connection) -> str:
        row = self._conn.execute(
            'SELECT token_ciphertext, token_nonce, key_version FROM connections WHERE id = ?',
            (connection.id,),
        ).fetchone()
        if row is None or row['token_ciphertext'] is None:
            raise AuthRequiredError('no stored token', connection_id=connection.id)
        return self.vault.open(
            SealedToken(row['token_ciphertext'], row['token_nonce'], row['key_version']),
            connection_id=connection.id, principal_id=connection.principal_id,
        )

    def _delete(self, principal_id: str, connection_id: str) -> None:
        self._conn.execute(
            'DELETE FROM connections WHERE id = ? AND principal_id = ?',
            (connection_id, principal_id),
        )
        self._audit(principal_id, connection_id, 'connection_deleted')

    def _list(self, principal_id: str) -> list[Connection]:
        rows = self._conn.execute(
            'SELECT * FROM connections WHERE principal_id = ? ORDER BY created_at',
            (principal_id,),
        ).fetchall()
        return [_row_to_connection(r) for r in rows]

    # -- async API ---------------------------------------------------------
    async def save(self, connection: Connection, token: str | None = None) -> Connection:
        return await self._run(self._save, connection, token, connection.principal_id)

    async def get(self, connection_id: str) -> Connection:
        return await self._run(self._get, connection_id)

    async def get_owned(
        self, principal_id: str, connection_id: str | None = None,
        connector_id: str | None = None,
    ) -> Connection | None:
        return await self._run(self._get_owned, principal_id, connection_id, connector_id)

    async def token(self, connection: Connection) -> str:
        return await self._run(self._token, connection)

    async def delete(self, principal_id: str, connection_id: str) -> None:
        await self._run(self._delete, principal_id, connection_id)

    async def list(self, principal_id: str) -> list[Connection]:
        return await self._run(self._list, principal_id)

    async def set_status(self, connection_id: str, status: ConnectionStatus) -> None:
        def _set() -> None:
            self._conn.execute(
                'UPDATE connections SET status=?, updated_at=? WHERE id=?',
                (str(status), datetime.now(UTC).isoformat(), connection_id),
            )
            self._audit(None, connection_id, 'status_changed', {'status': str(status)})

        await self._run(_set)

    async def audit(self, principal_id: str | None, connection_id: str | None,
                    action: str, detail: dict | None = None) -> None:
        await self._run(self._audit, principal_id, connection_id, action, detail)

    # -- OAuth state -------------------------------------------------------
    async def create_state(
        self, principal_id: str, connector_id: str, redirect_uri: str
    ) -> str:
        import hashlib
        import secrets
        import time

        state = secrets.token_urlsafe(32)  # 256 bits
        digest = hashlib.sha256(state.encode()).hexdigest()

        def _store() -> None:
            self._conn.execute(
                'INSERT INTO auth_states (state_hash, principal_id, connector_id, '
                'redirect_uri, created_at) VALUES (?,?,?,?,?)',
                (digest, principal_id, connector_id, redirect_uri, time.time()),
            )

        await self._run(_store)
        return state

    async def consume_state(self, state: str, *, ttl_s: float = 600) -> tuple[str, str]:
        """Single use and time limited; returns (principal_id, redirect_uri)."""
        import hashlib
        import time

        digest = hashlib.sha256(state.encode()).hexdigest()

        def _consume() -> tuple[str, str]:
            row = self._conn.execute(
                'SELECT * FROM auth_states WHERE state_hash = ?', (digest,)
            ).fetchone()
            if row is None:
                raise AuthRequiredError('unknown or already used authorization state')
            if row['used']:
                raise AuthRequiredError('authorization state was already used')
            if time.time() - row['created_at'] > ttl_s:
                raise AuthRequiredError('authorization state expired')
            self._conn.execute(
                'UPDATE auth_states SET used = 1 WHERE state_hash = ?', (digest,)
            )
            return row['principal_id'], row['redirect_uri']

        return await self._run(_consume)


def _row_to_connection(row: sqlite3.Row) -> Connection:
    return Connection(
        id=row['id'], principal_id=row['principal_id'], connector_id=row['connector_id'],
        auth_mode=row['auth_mode'], status=ConnectionStatus(row['status']),
        provider_user_id=row['provider_user_id'], provider_username=row['provider_username'],
        scopes_granted=tuple(json.loads(row['scopes_granted'])),
        expires_at=datetime.fromisoformat(row['expires_at']) if row['expires_at'] else None,
        last_refreshed_at=(
            datetime.fromisoformat(row['last_refreshed_at']) if row['last_refreshed_at'] else None
        ),
        key_version=row['key_version'], created_at=row['created_at'], updated_at=row['updated_at'],
    )


def new_connection_id() -> str:
    return uuid.uuid4().hex
