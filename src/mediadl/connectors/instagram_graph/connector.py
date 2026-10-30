"""Instagram connector on the official Graph API (spec section 4.5.6).

Scope: media owned by the professional account that authorized this app.
It does not and will not touch the private ``/api/v1/`` endpoints, the
GraphQL ``doc_id`` path, browser ``sessionid`` cookies, or TLS impersonation
that yt-dlp's extractor relies on; those are outside the authorized-access
model this program is built around, and they cannot fetch images anyway.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from mediadl.connectors.instagram_graph.graph_client import GraphClient
from mediadl.connectors.instagram_graph.media_index import (
    MediaIndex,
    canonical_permalink,
    parse_media_ref,
    parse_permalink,
)
from mediadl.connectors.instagram_graph.normalize import (
    CONNECTOR_ID,
    normalize_child,
    normalize_node,
)
from mediadl.domain.models import Asset, MediaKind, ResolvedItem, SourceRef
from mediadl.errors import AuthRequiredError, ErrorCode, InputError, NotAccessibleError
from mediadl.obs.logging import get_logger
from mediadl.sources.base import (
    AccessContext,
    BaseConnector,
    ConnectorCapabilities,
    Page,
    SourceRequest,
)

log = get_logger(__name__)

#: Everything needed to resolve a post in one call. Nested children fields
#: are [OPEN] in the spec, so a failure falls back to per-child GETs.
MEDIA_FIELDS = (
    'id,media_type,media_product_type,media_url,thumbnail_url,permalink,shortcode,'
    'timestamp,caption,username,alt_text,is_shared_to_feed,media_audio_type,'
    'children{id,media_type,media_url,thumbnail_url,alt_text}'
)
MEDIA_FIELDS_NO_CHILDREN = (
    'id,media_type,media_product_type,media_url,thumbnail_url,permalink,shortcode,'
    'timestamp,caption,username,alt_text,is_shared_to_feed,media_audio_type'
)
CHILD_FIELDS = 'id,media_type,media_url,thumbnail_url,alt_text'
REFRESH_FIELDS = 'id,media_type,media_url,thumbnail_url'

#: The CDN hosts Instagram serves media from. The transfer layer refuses
#: anything else, so a redirect off-platform cannot be followed.
MEDIA_HOSTS = (
    'cdninstagram.com',
    'fbcdn.net',
    'instagram.com',
)


class InstagramGraphConnector(BaseConnector):
    connector_id: ClassVar[str] = CONNECTOR_ID
    capabilities: ClassVar[ConnectorCapabilities] = ConnectorCapabilities(
        media_kinds=frozenset({MediaKind.IMAGE, MediaKind.VIDEO}),
        auth_modes=frozenset({'instagram_login', 'facebook_login'}),
        supports_refresh=True,
        supports_listing=True,
        allowed_media_hosts=MEDIA_HOSTS,
        # Instagram serves transcodes, never the original upload.
        rendition_basis='platform_rendition',
    )

    def __init__(
        self,
        *,
        api_version: str = 'v25.0',
        auth_mode: str = 'instagram_login',
        page_size: int = 50,
        max_pages: int = 200,
    ) -> None:
        self.api_version = api_version
        self.auth_mode = auth_mode
        self.page_size = page_size
        self.max_pages = max_pages
        self._indexes: dict[str, MediaIndex] = {}

    # -- routing -----------------------------------------------------------
    def match(self, request: SourceRequest) -> int | None:
        raw = request.raw.strip()
        if parse_media_ref(raw):
            return 100
        if parse_permalink(raw):
            return 100
        import urllib.parse

        host = (urllib.parse.urlsplit(raw).hostname or '').lower()
        if host.endswith('instagram.com'):
            # An Instagram URL we cannot resolve is still ours to refuse, so
            # no other connector tries to scrape it.
            return 80
        return None

    def required_scopes(self, request: SourceRequest) -> frozenset[str]:
        if self.auth_mode == 'instagram_login':
            return frozenset({'instagram_business_basic'})
        return frozenset({'instagram_basic', 'pages_read_engagement'})

    # -- resolution --------------------------------------------------------
    def _client(self, ctx: AccessContext) -> GraphClient:
        return GraphClient(ctx.http, api_version=self.api_version, auth_mode=self.auth_mode)

    def _index(self, ctx: AccessContext) -> MediaIndex:
        key = ctx.connection_id or 'default'
        if key not in self._indexes:
            self._indexes[key] = MediaIndex(
                self._client(ctx), page_size=self.page_size, max_pages=self.max_pages
            )
        return self._indexes[key]

    def _credential(self, ctx: AccessContext):
        if ctx.credential is None:
            raise AuthRequiredError(
                'Instagram requires a connected professional account',
                connector=self.connector_id,
            )
        return ctx.credential

    async def resolve(self, request: SourceRequest, ctx: AccessContext) -> ResolvedItem:
        credential = self._credential(ctx)
        client = self._client(ctx)
        media_id = await self._media_id_for(request, ctx)

        try:
            node = await client.get(
                media_id, credential=credential, params={'fields': MEDIA_FIELDS}
            )
        except Exception as exc:  # nested children expansion is [OPEN]
            if not _looks_like_field_error(exc):
                raise
            log.info('children_expansion_unsupported', fallback='per-child GET')
            node = await self._resolve_with_child_gets(media_id, client, credential)

        return normalize_node(
            node, fetched_at=datetime.now(UTC), api_version=self.api_version
        )

    async def _resolve_with_child_gets(self, media_id: str, client, credential) -> dict:
        node = dict(
            await client.get(
                media_id, credential=credential, params={'fields': MEDIA_FIELDS_NO_CHILDREN}
            )
        )
        if node.get('media_type') != 'CAROUSEL_ALBUM':
            return node
        listing = await client.get(f'{media_id}/children', credential=credential)
        children = []
        for child in listing.get('data', []) or []:
            children.append(
                await client.get(
                    str(child['id']), credential=credential, params={'fields': CHILD_FIELDS}
                )
            )
        node['children'] = {'data': children}
        return node

    async def _media_id_for(self, request: SourceRequest, ctx: AccessContext) -> str:
        credential = self._credential(ctx)
        raw = request.raw.strip()

        if (media_id := parse_media_ref(raw)) is not None:
            return media_id

        shortcode = parse_permalink(raw)
        if shortcode is None:
            raise InputError(
                'not an Instagram post, reel, or tv URL', url=request.redacted()
            )

        index = self._index(ctx)
        connection_id = ctx.connection_id or 'default'
        entry = index.lookup(connection_id, shortcode)
        if entry is None:
            await index.refresh(connection_id, credential, stop_when=shortcode)
            entry = index.lookup(connection_id, shortcode)
        if entry is None:
            # Never fall back to scraping: report which limit was hit.
            if index.window_full(connection_id):
                raise NotAccessibleError(
                    'this post is outside the 10,000 most recent media the API returns',
                    code=ErrorCode.NOT_IN_INDEX_WINDOW,
                    permalink=canonical_permalink(shortcode),
                )
            raise NotAccessibleError(
                'this post does not belong to the connected professional account',
                permalink=canonical_permalink(shortcode),
            )
        return entry.media_id

    async def refresh(self, ref: SourceRef, ctx: AccessContext) -> Asset:
        """Re-resolve an expired CDN URL through the documented media call."""
        credential = self._credential(ctx)
        client = self._client(ctx)
        node = await client.get(
            ref.media_id, credential=credential, params={'fields': REFRESH_FIELDS}
        )
        return normalize_child(
            node, position=ref.position or 1, parent_id=ref.parent_id,
            fetched_at=datetime.now(UTC), api_version=self.api_version,
        )

    async def list_media(self, ctx: AccessContext, cursor: str | None = None) -> Page:
        credential = self._credential(ctx)
        client = self._client(ctx)
        if cursor:
            payload = await client.get_url(cursor, credential=credential)
        else:
            payload = await client.get(
                'me/media', credential=credential,
                params={'fields': MEDIA_FIELDS_NO_CHILDREN, 'limit': self.page_size},
            )
        now = datetime.now(UTC)
        assets = tuple(
            normalize_child(
                node, position=index, parent_id=None,
                fetched_at=now, api_version=self.api_version,
            )
            for index, node in enumerate(payload.get('data', []) or [], start=1)
        )
        return Page(items=assets, cursor=(payload.get('paging') or {}).get('next'))


def _looks_like_field_error(exc: Exception) -> bool:
    detail = getattr(exc, 'detail', {}) or {}
    if detail.get('graph_code') == 100:
        return True
    return 'nonexisting field' in str(exc).lower() or 'unknown fields' in str(exc).lower()
