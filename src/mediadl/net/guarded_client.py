"""Guarded HTTP client (spec sections 6.6, 6.7, 7.6).

Every outbound request passes the URL policy, connects to a vetted IP
(pinning defeats DNS rebinding), and follows redirects manually so each hop
is re-checked. Credentials are attached by an explicit hook and only for the
hosts that hook declares; they are dropped the moment the origin changes.

Cookies are ephemeral: a jar lives for one logical operation (including its
redirect hops) and is then discarded. Nothing is ever persisted or shared.
"""
from __future__ import annotations

import contextlib
import ipaddress
import urllib.parse
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from mediadl.config import NetworkConfig
from mediadl.errors import NetworkError, UpstreamError
from mediadl.net.redact import redact_headers, redact_url
from mediadl.obs.logging import get_logger
from mediadl.policy.url_guard import UrlPolicyError, is_public_ip, vet_url

log = get_logger(__name__)

#: Methods a redirect may switch to, per RFC 9110 section 15.4.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

AuthHook = Callable[[str], Mapping[str, str] | None]
"""Given the absolute URL about to be fetched, return headers to attach (or None)."""


@dataclass(frozen=True, slots=True)
class UrlPolicy:
    allowed_schemes: frozenset[str] = frozenset({'https'})
    allowed_ports: frozenset[int] = frozenset({443})
    allowed_hosts: tuple[str, ...] | None = None
    max_redirects: int = 5
    #: Only ever True for local fixture servers in tests. Never settable from
    #: user configuration.
    allow_private: bool = False

    @classmethod
    def from_config(cls, cfg: NetworkConfig, allowed_hosts: Sequence[str] | None = None) -> UrlPolicy:
        return cls(
            allowed_schemes=frozenset(cfg.allowed_schemes),
            allowed_ports=frozenset(cfg.allowed_ports),
            allowed_hosts=tuple(allowed_hosts) if allowed_hosts else None,
            max_redirects=cfg.max_redirects,
        )

    def with_hosts(self, hosts: Sequence[str] | None) -> UrlPolicy:
        return UrlPolicy(
            self.allowed_schemes, self.allowed_ports,
            tuple(hosts) if hosts else None, self.max_redirects, self.allow_private,
        )


@dataclass(slots=True)
class VettedTarget:
    url: str
    host: str
    pinned_ip: str
    connect_url: str
    sni_hostname: str
    host_header: str


def _pin(url: str, ip: str) -> VettedTarget:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or '').rstrip('.')
    port = parts.port
    literal = f'[{ip}]' if ipaddress.ip_address(ip.split('%')[0]).version == 6 else ip
    netloc = f'{literal}:{port}' if port else literal
    host_header = f'{host}:{port}' if port else host
    return VettedTarget(
        url=url,
        host=host,
        pinned_ip=ip,
        connect_url=urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, '')),
        sni_hostname=host,
        host_header=host_header,
    )


def vet_and_pin(url: str, policy: UrlPolicy) -> VettedTarget:
    if policy.allow_private:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in policy.allowed_schemes:
            raise UrlPolicyError(f'scheme {parts.scheme!r} not allowed')
        if not parts.hostname:
            raise UrlPolicyError('missing host')
        return VettedTarget(url, parts.hostname, '', url, parts.hostname, parts.netloc)
    ips = vet_url(
        url,
        allowed_schemes=policy.allowed_schemes,
        allowed_ports=policy.allowed_ports,
        allowed_hosts=policy.allowed_hosts,
    )
    return _pin(url, ips[0])


@dataclass(slots=True)
class _Hop:
    host: str
    status: int


class GuardedHttpClient:
    """Async HTTP client that enforces the URL policy on every hop."""

    def __init__(
        self,
        config: NetworkConfig | None = None,
        *,
        policy: UrlPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str = 'mediadl/0.1 (+https://example.invalid/mediadl)',
    ) -> None:
        self.config = config or NetworkConfig()
        self.policy = policy or UrlPolicy.from_config(self.config)
        self.user_agent = user_agent
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(
                connect=self.config.connect_timeout_s,
                read=self.config.read_timeout_s,
                write=self.config.write_timeout_s,
                pool=self.config.connect_timeout_s,
            ),
            limits=httpx.Limits(
                max_connections=self.config.per_host_concurrency * 4,
                max_keepalive_connections=self.config.per_host_concurrency,
            ),
            transport=transport,
            verify=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GuardedHttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- request building --------------------------------------------------
    def _build(
        self,
        method: str,
        target: VettedTarget,
        *,
        headers: Mapping[str, str] | None,
        auth_hook: AuthHook | None,
        cookies: httpx.Cookies,
        origin_of_first: str,
    ) -> httpx.Request:
        merged: dict[str, str] = {
            'User-Agent': self.user_agent,
            'Accept-Encoding': 'identity',
        }
        if headers:
            merged.update(headers)
        if auth_hook is not None:
            # Same origin as the first request only: a redirect off-origin
            # must never carry the API token.
            if _origin(target.url) == origin_of_first:
                extra = auth_hook(target.url)
                if extra:
                    merged.update(extra)
        if target.pinned_ip:
            merged['Host'] = target.host_header
        request = self._client.build_request(
            method,
            target.connect_url,
            headers=merged,
            cookies=cookies,
            extensions={'sni_hostname': target.sni_hostname} if target.pinned_ip else {},
        )
        return request

    async def _send_once(
        self, request: httpx.Request, *, stream: bool
    ) -> httpx.Response:
        try:
            return await self._client.send(request, stream=stream)
        except httpx.TimeoutException as exc:
            raise NetworkError(f'timeout fetching {redact_url(str(request.url))}', kind='timeout') from exc
        except httpx.TransportError as exc:
            raise NetworkError(
                f'transport error fetching {redact_url(str(request.url))}: {type(exc).__name__}',
                kind='transport',
            ) from exc

    async def _follow(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None,
        auth_hook: AuthHook | None,
        policy: UrlPolicy,
        content: bytes | None,
        stream: bool,
    ) -> tuple[httpx.Response, list[_Hop]]:
        cookies = httpx.Cookies()  # ephemeral, per operation
        origin_of_first = _origin(url)
        hops: list[_Hop] = []
        current_method, current_url = method.upper(), url
        current_headers = dict(headers or {})

        for hop in range(policy.max_redirects + 1):
            target = vet_and_pin(current_url, policy)
            request = self._build(
                current_method, target,
                headers=current_headers, auth_hook=auth_hook,
                cookies=cookies, origin_of_first=origin_of_first,
            )
            if content is not None and current_method in ('POST', 'PUT', 'PATCH'):
                request = self._client.build_request(
                    current_method, target.connect_url,
                    headers=request.headers, content=content, cookies=cookies,
                    extensions={'sni_hostname': target.sni_hostname} if target.pinned_ip else {},
                )
            response = await self._send_once(request, stream=stream)
            hops.append(_Hop(target.host, response.status_code))

            if response.status_code not in _REDIRECT_STATUSES:
                response.request.url = httpx.URL(current_url)  # report the logical URL
                return response, hops

            location = response.headers.get('location')
            if stream:
                await response.aclose()
            else:
                await response.aread()
            if not location:
                raise UpstreamError('redirect without a Location header', status=response.status_code)
            if hop >= policy.max_redirects:
                break

            next_url = urllib.parse.urljoin(current_url, location)
            _reject_downgrade(current_url, next_url)
            if _origin(next_url) != _origin(current_url):
                # Cross-origin: drop anything bearer-like that the caller set.
                current_headers = {
                    k: v for k, v in current_headers.items()
                    if k.lower() not in ('authorization', 'cookie', 'proxy-authorization')
                }
            if response.status_code == 303 or (
                response.status_code in (301, 302) and current_method == 'POST'
            ):
                current_method, content = 'GET', None
            current_url = next_url
            log.debug('redirect', hop=hop + 1, to_host=urllib.parse.urlsplit(next_url).hostname)

        raise UpstreamError(
            f'too many redirects (> {policy.max_redirects})',
            hops=[h.host for h in hops],
        )

    # -- public API --------------------------------------------------------
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        auth_hook: AuthHook | None = None,
        allowed_hosts: Sequence[str] | None = None,
    ) -> httpx.Response:
        """Buffered request. Use ``stream`` for media bytes."""
        if params:
            url = _with_params(url, params)
        policy = self.policy.with_hosts(allowed_hosts) if allowed_hosts else self.policy
        response, hops = await self._follow(
            method, url, headers=headers, auth_hook=auth_hook,
            policy=policy, content=content, stream=False,
        )
        await response.aread()
        log.debug(
            'http_response', url=redact_url(url), status=response.status_code,
            hops=[h.host for h in hops],
        )
        return response

    @contextlib.asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        auth_hook: AuthHook | None = None,
        allowed_hosts: Sequence[str] | None = None,
    ) -> AsyncIterator[httpx.Response]:
        """Streaming request; the body is never buffered in memory."""
        policy = self.policy.with_hosts(allowed_hosts) if allowed_hosts else self.policy
        response, hops = await self._follow(
            method, url, headers=headers, auth_hook=auth_hook,
            policy=policy, content=None, stream=True,
        )
        log.debug(
            'http_stream', url=redact_url(url), status=response.status_code,
            headers=redact_headers(dict(response.headers)), hops=[h.host for h in hops],
        )
        try:
            yield response
        finally:
            await response.aclose()


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme, (parts.hostname or '').lower(), parts.port


def _reject_downgrade(current: str, nxt: str) -> None:
    cur_scheme = urllib.parse.urlsplit(current).scheme
    next_scheme = urllib.parse.urlsplit(nxt).scheme
    if cur_scheme == 'https' and next_scheme != 'https':
        raise UrlPolicyError(f'refusing HTTPS -> {next_scheme} downgrade')


def _with_params(url: str, params: Mapping[str, Any]) -> str:
    parts = urllib.parse.urlsplit(url)
    existing = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    existing.extend((k, str(v)) for k, v in params.items() if v is not None)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(existing), '')
    )


__all__ = ['GuardedHttpClient', 'UrlPolicy', 'VettedTarget', 'is_public_ip', 'vet_and_pin']
