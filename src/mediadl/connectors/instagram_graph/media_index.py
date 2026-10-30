"""Permalink -> media id index (spec section 4.5.6).

The Graph API has no "look up a post by URL" endpoint, so a URL is resolved
by paging the authorized account's own media edge and matching the shortcode.
A miss means one of two things, and they are reported differently:

  * the account's index is complete and the post is not in it -> the post
    belongs to someone else (NOT_ACCESSIBLE_WITH_AUTHORIZED_ACCOUNT);
  * paging stopped at the documented 10K most-recent-media cap -> the post
    may exist but is unreachable (NOT_IN_INDEX_WINDOW).

There is never a fallback to scraping.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field

from mediadl.auth.credentials import AccessCredential
from mediadl.connectors.instagram_graph.graph_client import GraphClient
from mediadl.obs.logging import get_logger

log = get_logger(__name__)

#: Documented cap on the IG User media edge.
MEDIA_EDGE_CAP = 10_000

_PERMALINK_RE = re.compile(
    r'^/(?:[A-Za-z0-9._]+/)?(?P<type>p|reel|reels|tv)/(?P<shortcode>[A-Za-z0-9_-]+)/?$'
)
_MEDIA_REF_RE = re.compile(r'^ig-media:(?P<id>\d+)$')

INDEX_FIELDS = 'id,shortcode,permalink,timestamp,media_type,media_product_type'


def parse_media_ref(raw: str) -> str | None:
    match = _MEDIA_REF_RE.match(raw.strip())
    return match.group('id') if match else None


def parse_permalink(raw: str) -> str | None:
    """Return the shortcode for a post/reel/tv URL, or None.

    ``/share/`` links and ``/reels/audio/`` pages are deliberately excluded:
    they are not media permalinks.
    """
    try:
        parts = urllib.parse.urlsplit(raw.strip())
    except ValueError:
        return None
    host = (parts.hostname or '').lower().removeprefix('www.')
    if host not in ('instagram.com', 'instagr.am'):
        return None
    path = parts.path
    if '/share/' in path or '/reels/audio/' in path:
        return None
    match = _PERMALINK_RE.match(path if path.endswith('/') else path + '/')
    return match.group('shortcode') if match else None


def canonical_permalink(shortcode: str) -> str:
    return f'https://www.instagram.com/p/{shortcode}/'


@dataclass(slots=True)
class IndexEntry:
    media_id: str
    shortcode: str
    permalink: str | None
    timestamp: str | None


@dataclass(slots=True)
class MediaIndex:
    """Per-connection cache of shortcode -> media id."""

    client: GraphClient
    page_size: int = 50
    max_pages: int = 200
    _entries: dict[str, dict[str, IndexEntry]] = field(default_factory=dict)
    _complete: dict[str, bool] = field(default_factory=dict)
    _count: dict[str, int] = field(default_factory=dict)

    def lookup(self, connection_id: str, shortcode: str) -> IndexEntry | None:
        return self._entries.get(connection_id, {}).get(shortcode)

    def window_full(self, connection_id: str) -> bool:
        """True when paging stopped at the documented cap rather than the end."""
        return self._count.get(connection_id, 0) >= MEDIA_EDGE_CAP

    def is_complete(self, connection_id: str) -> bool:
        return self._complete.get(connection_id, False)

    async def refresh(
        self,
        connection_id: str,
        credential: AccessCredential,
        *,
        stop_when: str | None = None,
    ) -> None:
        """Page ``/me/media``, stopping early once the wanted shortcode appears."""
        entries = self._entries.setdefault(connection_id, {})
        payload = await self.client.get(
            'me/media',
            credential=credential,
            params={'fields': INDEX_FIELDS, 'limit': self.page_size},
        )
        pages = 0
        while True:
            for node in payload.get('data', []) or []:
                shortcode = node.get('shortcode')
                if not shortcode:
                    continue
                entries[str(shortcode)] = IndexEntry(
                    media_id=str(node['id']),
                    shortcode=str(shortcode),
                    permalink=node.get('permalink'),
                    timestamp=node.get('timestamp'),
                )
            self._count[connection_id] = len(entries)
            if stop_when and stop_when in entries:
                return
            pages += 1
            next_url = ((payload.get('paging') or {}).get('next'))
            if not next_url or pages >= self.max_pages or len(entries) >= MEDIA_EDGE_CAP:
                break
            payload = await self.client.get_url(next_url, credential=credential)

        self._complete[connection_id] = not (
            (payload.get('paging') or {}).get('next')
        ) and len(entries) < MEDIA_EDGE_CAP
        log.debug(
            'media_index_refreshed',
            connection_id=connection_id, entries=len(entries), complete=self._complete[connection_id]
        )
