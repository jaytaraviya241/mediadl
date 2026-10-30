"""Connector contracts (spec section 5.5).

A connector knows one platform and nothing about transfer, naming, or
storage. It resolves a request into typed domain objects using *authorized
mechanisms only*, and can re-resolve an expired locator through the same
documented call.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

from mediadl.domain.models import Asset, MediaKind, ResolvedItem, SourceRef

if TYPE_CHECKING:  # pragma: no cover
    from mediadl.auth.credentials import AccessCredential
    from mediadl.net.guarded_client import GuardedHttpClient


@dataclass(frozen=True, slots=True)
class ConnectorCapabilities:
    media_kinds: frozenset[MediaKind]
    auth_modes: frozenset[str]
    supports_refresh: bool = False
    supports_listing: bool = False
    #: Host suffixes the transfer layer may contact for this connector's media.
    #: Empty means "any public host", which only the direct-URL connector uses.
    allowed_media_hosts: tuple[str, ...] = ()
    #: What the outputs should be labelled as; platforms serve renditions.
    rendition_basis: str = 'direct_source'


@dataclass(frozen=True, slots=True)
class SourceRequest:
    raw: str
    connection_id: str | None = None
    options: Mapping[str, object] = field(default_factory=dict)

    def redacted(self) -> str:
        from mediadl.net.redact import redact_url

        return redact_url(self.raw)


@dataclass(slots=True)
class AccessContext:
    principal_id: str
    connection_id: str | None
    credential: AccessCredential | None
    http: GuardedHttpClient
    deadline: float = 0.0


@dataclass(frozen=True, slots=True)
class Page:
    items: tuple[Asset, ...]
    cursor: str | None = None


@runtime_checkable
class SourceConnector(Protocol):
    connector_id: ClassVar[str]
    capabilities: ClassVar[ConnectorCapabilities]

    def match(self, request: SourceRequest) -> int | None:
        """Pure and offline. Higher score wins; None means "not mine"."""

    def required_scopes(self, request: SourceRequest) -> frozenset[str]: ...

    async def resolve(self, request: SourceRequest, ctx: AccessContext) -> ResolvedItem: ...

    async def refresh(self, ref: SourceRef, ctx: AccessContext) -> Asset: ...

    async def list_media(self, ctx: AccessContext, cursor: str | None) -> Page: ...


class BaseConnector:
    """Convenience base with inert defaults for optional capabilities."""

    connector_id: ClassVar[str] = 'base'
    capabilities: ClassVar[ConnectorCapabilities] = ConnectorCapabilities(
        media_kinds=frozenset(MediaKind), auth_modes=frozenset({'none'})
    )

    def match(self, request: SourceRequest) -> int | None:
        return None

    def required_scopes(self, request: SourceRequest) -> frozenset[str]:
        return frozenset()

    async def resolve(self, request: SourceRequest, ctx: AccessContext) -> ResolvedItem:
        raise NotImplementedError

    async def refresh(self, ref: SourceRef, ctx: AccessContext) -> Asset:
        from mediadl.errors import ConnectorContractError

        raise ConnectorContractError(
            f'{self.connector_id} does not support refresh', connector=self.connector_id
        )

    async def list_media(self, ctx: AccessContext, cursor: str | None = None) -> Page:
        from mediadl.errors import ConnectorContractError

        raise ConnectorContractError(
            f'{self.connector_id} does not support listing', connector=self.connector_id
        )

    def allowed_media_hosts(self) -> Sequence[str] | None:
        hosts = self.capabilities.allowed_media_hosts
        return hosts or None
