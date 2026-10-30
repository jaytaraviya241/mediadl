"""In-memory connector for pipeline tests (spec section 9.4)."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from mediadl.domain.models import (
    Asset,
    Availability,
    Collection,
    CollectionKind,
    HttpLocator,
    MediaKind,
    Representation,
    RepresentationRole,
    ResolvedItem,
    SourceMetadata,
    SourceRef,
    TechnicalInfo,
)
from mediadl.sources.base import AccessContext, BaseConnector, ConnectorCapabilities, SourceRequest


def asset(
    media_id: str,
    url: str,
    *,
    kind: MediaKind = MediaKind.IMAGE,
    position: int = 1,
    parent_id: str | None = None,
    availability: Availability = Availability.AVAILABLE,
    unavailable_reason: str | None = None,
    connector_id: str = 'mock',
    **declared,
) -> Asset:
    reps: tuple[Representation, ...] = ()
    if availability is Availability.AVAILABLE:
        reps = (
            Representation(
                rep_id='primary', kind=kind, role=RepresentationRole.PRIMARY,
                protocol='https', locator=HttpLocator(url),
                declared=TechnicalInfo(**declared), label='primary',
            ),
        )
    return Asset(
        asset_id=f'{connector_id}:{media_id}',
        ref=SourceRef(connector_id, media_id, parent_id=parent_id, position=position),
        kind=kind,
        position=position,
        availability=availability,
        representations=reps,
        metadata=SourceMetadata(
            fields={'username': 'tester', 'timestamp': '2026-07-04T08:01:22+00:00'},
            fetched_at=datetime.now(UTC),
        ),
        unavailable_reason=unavailable_reason,
    )


def carousel(collection_id: str, assets: tuple[Asset, ...], connector_id: str = 'mock') -> Collection:
    return Collection(
        collection_id=f'{connector_id}:{collection_id}',
        ref=SourceRef(connector_id, collection_id),
        kind=CollectionKind.CAROUSEL,
        assets=assets,
        position_basis='api_response_order',
        metadata=SourceMetadata(fields={'username': 'tester'}, fetched_at=datetime.now(UTC)),
    )


class MockConnector(BaseConnector):
    """Returns pre-built items keyed by request string."""

    connector_id: ClassVar[str] = 'mock'
    capabilities: ClassVar[ConnectorCapabilities] = ConnectorCapabilities(
        media_kinds=frozenset(MediaKind),
        auth_modes=frozenset({'none'}),
        supports_refresh=True,
        rendition_basis='platform_rendition',
    )

    def __init__(self, items: dict[str, ResolvedItem] | None = None) -> None:
        self.items = items or {}
        self.refresh_calls: list[str] = []
        self.refreshed_url: str | None = None

    def match(self, request: SourceRequest) -> int | None:
        return 100 if request.raw in self.items else None

    async def resolve(self, request: SourceRequest, ctx: AccessContext) -> ResolvedItem:
        return self.items[request.raw]

    async def refresh(self, ref: SourceRef, ctx: AccessContext) -> Asset:
        self.refresh_calls.append(ref.media_id)
        url = self.refreshed_url or ''
        return asset(ref.media_id, url, position=ref.position or 1, connector_id=self.connector_id)
