"""Credential provider (spec section 5.5).

Workers receive a short-lived in-memory handle, never the stored ciphertext
and never a long-lived secret they can persist.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from mediadl.auth.connections import Connection, ConnectionStatus, ConnectionStore
from mediadl.auth.oauth_instagram import InstagramOAuth
from mediadl.errors import AuthExpiredError, AuthRequiredError, PermissionMissingError
from mediadl.obs.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AccessCredential:
    connection_id: str
    connector_id: str
    auth_mode: str
    token: str = field(repr=False)
    scopes: frozenset[str] = frozenset()
    provider_user_id: str | None = None
    expires_at: datetime | None = None

    def __repr__(self) -> str:
        return (
            f'AccessCredential(connection_id={self.connection_id!r}, '
            f'connector_id={self.connector_id!r}, token=«redacted»)'
        )

    def __str__(self) -> str:
        return repr(self)


class CredentialProvider:
    def __init__(self, store: ConnectionStore, oauth: InstagramOAuth | None = None) -> None:
        self.store = store
        self.oauth = oauth

    async def acquire(
        self,
        principal_id: str,
        connection_id: str | None,
        scopes: frozenset[str] = frozenset(),
        *,
        connector_id: str = 'instagram_graph',
    ) -> AccessCredential:
        connection = await self.store.get_owned(principal_id, connection_id, connector_id)
        if connection is None:
            raise AuthRequiredError(
                'no connected account for this connector', connector=connector_id
            )
        if connection.status is ConnectionStatus.REVOKED:
            raise AuthRequiredError('this connection was revoked', connection_id=connection.id)
        if connection.expired:
            await self.store.set_status(connection.id, ConnectionStatus.EXPIRED)
            raise AuthExpiredError('the stored token expired', connection_id=connection.id)
        missing = scopes - set(connection.scopes_granted)
        if connection.scopes_granted and missing:
            raise PermissionMissingError(
                'the connection is missing a required permission',
                missing=sorted(missing), granted=sorted(connection.scopes_granted),
            )

        if connection.needs_refresh() and self.oauth is not None:
            connection = await self.refresh(connection)

        token = await self.store.token(connection)
        return AccessCredential(
            connection_id=connection.id,
            connector_id=connection.connector_id,
            auth_mode=connection.auth_mode,
            token=token,
            scopes=frozenset(connection.scopes_granted),
            provider_user_id=connection.provider_user_id,
            expires_at=connection.expires_at,
        )

    async def refresh(self, connection: Connection) -> Connection:
        assert self.oauth is not None
        try:
            current = await self.store.token(connection)
            refreshed = await self.oauth.refresh(current)
        except AuthExpiredError:
            await self.store.set_status(connection.id, ConnectionStatus.EXPIRED)
            await self.store.audit(
                connection.principal_id, connection.id, 'token_refresh_failed'
            )
            raise
        connection.expires_at = refreshed.expires_at
        connection.last_refreshed_at = datetime.now(UTC)
        saved = await self.store.save(connection, refreshed.access_token)
        await self.store.audit(connection.principal_id, connection.id, 'token_refreshed')
        log.info('token_refreshed', connection_id=connection.id)
        return saved

    async def invalidate(self, connection_id: str, reason: str) -> None:
        await self.store.set_status(connection_id, ConnectionStatus.EXPIRED)
        await self.store.audit(None, connection_id, 'connection_invalidated', {'reason': reason})

    async def disconnect(self, principal_id: str, connection_id: str) -> None:
        """Hard delete: the ciphertext row goes away, not just a status flag."""
        await self.store.delete(principal_id, connection_id)
        await self.store.audit(principal_id, connection_id, 'disconnected')
        log.info('connection_disconnected', connection_id=connection_id)
