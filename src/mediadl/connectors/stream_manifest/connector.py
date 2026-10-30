"""Direct HLS/DASH manifest connector (spec section 7.7, Phase 7).

Accepts a ``.m3u8`` or ``.mpd`` URL the operator is authorized to fetch. It
resolves nothing itself: the manifest is parsed by the transfer engine,
which refuses live streams and every DRM key format.
"""
from __future__ import annotations

import hashlib
import urllib.parse
from datetime import UTC, datetime
from typing import ClassVar

from mediadl.domain.models import (
    Asset,
    Availability,
    DashLocator,
    HlsLocator,
    MediaKind,
    Representation,
    RepresentationRole,
    ResolvedItem,
    SourceMetadata,
    SourceRef,
    TechnicalInfo,
)
from mediadl.errors import InputError
from mediadl.sources.base import AccessContext, BaseConnector, ConnectorCapabilities, SourceRequest

HLS_EXTENSIONS = ('.m3u8', '.m3u')
DASH_EXTENSIONS = ('.mpd',)


class StreamManifestConnector(BaseConnector):
    connector_id: ClassVar[str] = 'stream_manifest'
    capabilities: ClassVar[ConnectorCapabilities] = ConnectorCapabilities(
        media_kinds=frozenset({MediaKind.VIDEO, MediaKind.AUDIO}),
        auth_modes=frozenset({'none'}),
        rendition_basis='direct_source',
    )

    def match(self, request: SourceRequest) -> int | None:
        parts = urllib.parse.urlsplit(request.raw.strip())
        if parts.scheme not in ('http', 'https'):
            return None
        path = parts.path.lower()
        if path.endswith(HLS_EXTENSIONS) or path.endswith(DASH_EXTENSIONS):
            return 60
        return None

    async def resolve(self, request: SourceRequest, ctx: AccessContext) -> ResolvedItem:
        url = request.raw.strip()
        parts = urllib.parse.urlsplit(url)
        path = parts.path.lower()

        if path.endswith(HLS_EXTENSIONS):
            protocol, locator = 'hls', HlsLocator(playlist_url=url)
        elif path.endswith(DASH_EXTENSIONS):
            protocol, locator = 'dash', DashLocator(mpd_url=url)
        else:
            raise InputError('not an .m3u8 or .mpd URL', url=request.redacted())

        media_id = hashlib.sha256(f'{parts.hostname}{parts.path}'.encode()).hexdigest()[:16]
        rep = Representation(
            rep_id=protocol,
            kind=MediaKind.VIDEO,
            role=RepresentationRole.PRIMARY,
            protocol=protocol,  # type: ignore[arg-type]
            locator=locator,
            declared=TechnicalInfo(container='mp4'),
            label=protocol,
        )
        return Asset(
            asset_id=f'{self.connector_id}:{media_id}',
            ref=SourceRef(self.connector_id, media_id, canonical_url=url),
            kind=MediaKind.VIDEO,
            position=1,
            availability=Availability.AVAILABLE,
            representations=(rep,),
            metadata=SourceMetadata(
                fields={'url_host': parts.hostname, 'manifest': protocol},
                fetched_at=datetime.now(UTC),
            ),
        )
