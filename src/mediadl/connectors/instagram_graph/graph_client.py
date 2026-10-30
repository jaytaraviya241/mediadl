"""Thin Graph API client (spec section 4.5.6).

Only documented endpoints are called. The access token goes to the Graph
host and nowhere else: media downloads from the CDN carry no credentials.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mediadl.auth.credentials import AccessCredential
from mediadl.connectors.instagram_graph.errors import parse_error
from mediadl.errors import UpstreamError
from mediadl.net.guarded_client import GuardedHttpClient
from mediadl.obs.logging import get_logger

log = get_logger(__name__)

GRAPH_INSTAGRAM = 'graph.instagram.com'
GRAPH_FACEBOOK = 'graph.facebook.com'


@dataclass(slots=True)
class GraphClient:
    http: GuardedHttpClient
    api_version: str = 'v25.0'
    auth_mode: str = 'instagram_login'

    @property
    def host(self) -> str:
        return GRAPH_INSTAGRAM if self.auth_mode == 'instagram_login' else GRAPH_FACEBOOK

    @property
    def base_url(self) -> str:
        return f'https://{self.host}/{self.api_version}'

    async def get(
        self,
        path: str,
        *,
        credential: AccessCredential,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f'{self.base_url}/{path.lstrip("/")}'
        query = dict(params or {})
        # The docs pass the token as a query parameter; the redaction filter
        # masks it everywhere it could otherwise be written down.
        query['access_token'] = credential.token
        response = await self.http.request(
            'GET', url, params=query, allowed_hosts=(self.host,)
        )
        return self._payload(response)

    async def get_url(
        self, absolute_url: str, *, credential: AccessCredential
    ) -> dict[str, Any]:
        """Follow a ``paging.next`` cursor, which already carries its params."""
        import urllib.parse

        host = (urllib.parse.urlsplit(absolute_url).hostname or '').lower()
        if host not in (GRAPH_INSTAGRAM, GRAPH_FACEBOOK):
            raise UpstreamError('refusing to follow paging cursor off the Graph host', host=host)
        response = await self.http.request('GET', absolute_url, allowed_hosts=(host,))
        return self._payload(response)

    @staticmethod
    def _payload(response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise UpstreamError(
                'Graph API returned a non-JSON body', status=response.status_code
            ) from exc
        if response.status_code >= 400 or (isinstance(data, dict) and 'error' in data):
            raise parse_error(data, response.status_code)
        if not isinstance(data, dict):
            raise UpstreamError('Graph API returned an unexpected shape')
        return data
