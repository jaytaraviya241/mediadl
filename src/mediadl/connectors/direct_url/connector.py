"""Direct media URL connector (spec section 4.1).

Accepts a URL that returns media bytes. It does not scrape pages, follow
embeds, or infer permission from public visibility: the operator is
responsible for having the right to fetch what they point it at.
"""
from __future__ import annotations

import mimetypes
import urllib.parse
from datetime import UTC, datetime
from typing import ClassVar

from mediadl.domain.models import (
    Asset,
    Availability,
    HttpLocator,
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

#: Extension -> kind, used only as a hint for the expected kind. The actual
#: kind is confirmed by magic bytes during transfer.
_EXT_KIND = {
    'jpg': MediaKind.IMAGE, 'jpeg': MediaKind.IMAGE, 'png': MediaKind.IMAGE,
    'gif': MediaKind.IMAGE, 'webp': MediaKind.IMAGE, 'heic': MediaKind.IMAGE,
    'avif': MediaKind.IMAGE, 'bmp': MediaKind.IMAGE, 'tiff': MediaKind.IMAGE,
    'mp4': MediaKind.VIDEO, 'm4v': MediaKind.VIDEO, 'mov': MediaKind.VIDEO,
    'webm': MediaKind.VIDEO, 'mkv': MediaKind.VIDEO, 'ts': MediaKind.VIDEO,
    'm4a': MediaKind.AUDIO, 'mp3': MediaKind.AUDIO, 'opus': MediaKind.AUDIO,
    'flac': MediaKind.AUDIO, 'wav': MediaKind.AUDIO, 'ogg': MediaKind.AUDIO,
    'aac': MediaKind.AUDIO,
}

#: Manifests are a different retrieval mechanism and belong to the stream
#: connector, not here.
_MANIFEST_EXT = frozenset({'m3u8', 'mpd'})


class DirectUrlConnector(BaseConnector):
    connector_id: ClassVar[str] = 'direct_url'
    capabilities: ClassVar[ConnectorCapabilities] = ConnectorCapabilities(
        media_kinds=frozenset(MediaKind),
        auth_modes=frozenset({'none'}),
        supports_refresh=False,
        supports_listing=False,
        allowed_media_hosts=(),  # any public host; the SSRF guard still applies
        rendition_basis='direct_source',
    )

    def match(self, request: SourceRequest) -> int | None:
        parts = urllib.parse.urlsplit(request.raw.strip())
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            return None
        host = parts.hostname.lower()
        if host.endswith('instagram.com') or host.endswith('cdninstagram.com'):
            # Instagram URLs belong to the authorized Graph connector only.
            return None
        ext = _extension(parts.path)
        if ext in _MANIFEST_EXT:
            return None
        # A known media extension is a strong signal; anything else is a weak
        # fallback so a more specific connector always wins.
        return 40 if ext in _EXT_KIND else 5

    async def resolve(self, request: SourceRequest, ctx: AccessContext) -> ResolvedItem:
        url = request.raw.strip()
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            raise InputError('not an http(s) URL', url=request.redacted())

        ext = _extension(parts.path)
        kind = _EXT_KIND.get(ext, MediaKind.VIDEO)
        mime = mimetypes.guess_type(parts.path)[0]
        media_id = _stable_id(url)

        rep = Representation(
            rep_id='primary',
            kind=kind,
            role=RepresentationRole.PRIMARY,
            # The HTTP engine handles both schemes; the URL policy decides
            # whether plain http is admissible at all.
            protocol='https',
            locator=HttpLocator(url=url),
            declared=TechnicalInfo(mime_type=mime, container=ext or None),
            label=ext or 'source',
        )
        return Asset(
            asset_id=f'{self.connector_id}:{media_id}',
            ref=SourceRef(self.connector_id, media_id, canonical_url=url),
            kind=kind,
            position=1,
            availability=Availability.AVAILABLE,
            representations=(rep,),
            metadata=SourceMetadata(
                fields={'url_host': parts.hostname, 'url_path': parts.path},
                fetched_at=datetime.now(UTC),
            ),
        )


def _extension(path: str) -> str:
    name = path.rsplit('/', 1)[-1]
    return name.rsplit('.', 1)[-1].lower() if '.' in name else ''


def _stable_id(url: str) -> str:
    import hashlib

    parts = urllib.parse.urlsplit(url)
    # The query string of a signed URL changes on every re-sign, so the
    # identity is host + path only.
    return hashlib.sha256(f'{parts.hostname}{parts.path}'.encode()).hexdigest()[:16]
