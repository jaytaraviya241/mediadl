"""SSRF address policy (spec sections 6.7, 12.10b).

Two rules make this work:
  1. *Every* address a hostname resolves to must be public. One private
     answer poisons the name, so we reject the whole host.
  2. The caller must connect to one of the returned IPs (pinning). Without
     pinning, a second DNS lookup can return a private address after the
     check passed (rebinding).
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from collections.abc import Iterable, Sequence

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

#: Ranges that ``is_global`` does not reject, or that it rejects for reasons
#: we want to state explicitly.
_EXTRA_DENY: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(n)
    for n in (
        '100.64.0.0/10',      # CGNAT
        '192.0.0.0/24',       # IETF protocol assignments
        '198.18.0.0/15',      # benchmarking
        '64:ff9b::/96',       # NAT64 well-known prefix
        '64:ff9b:1::/48',     # local-use NAT64
        '2002::/16',          # 6to4 (can embed a private IPv4)
        '2001::/32',          # Teredo
        'fd00:ec2::254/128',  # AWS IMDS over IPv6
    )
)

#: Single addresses that are globally routable in theory but are metadata
#: endpoints in practice.
_DENY_HOSTS = frozenset({'169.254.169.254', 'fd00:ec2::254', 'metadata.google.internal'})


class UrlPolicyError(ValueError):
    """Raised for any URL the transfer layer must not fetch."""


def is_public_ip(ip: IpAddress | str) -> bool:
    if isinstance(ip, str):
        try:
            ip = ipaddress.ip_address(ip.split('%')[0])
        except ValueError:
            return False
    if isinstance(ip, ipaddress.IPv6Address):
        # ::ffff:127.0.0.1 and 6to4/Teredo can smuggle a private IPv4.
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
        elif ip.teredo is not None:
            ip = ip.teredo[1]
    if not ip.is_global or ip.is_multicast or ip.is_loopback or ip.is_link_local:
        return False
    if str(ip) in _DENY_HOSTS:
        return False
    return not any(ip in net for net in _EXTRA_DENY if net.version == ip.version)


def parse_ip_literal(host: str) -> IpAddress | None:
    """Parse a host that is already an address, including legacy forms.

    ``socket.inet_aton`` accepts ``2130706433`` and ``0x7f.1`` as 127.0.0.1,
    and so does the C resolver, so these must be vetted as literals rather
    than handed to DNS.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if host and (host[0].isdigit() or host.lower().startswith('0x')):
        try:
            packed = socket.inet_aton(host)
        except OSError:
            return None
        return ipaddress.IPv4Address(packed)
    return None


def _resolve(host: str, port: int) -> set[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UrlPolicyError(f'DNS resolution failed for {host!r}: {exc}') from exc
    return {info[4][0] for info in infos}


def vet_url(
    url: str,
    *,
    allowed_schemes: Iterable[str] = ('https',),
    allowed_ports: Iterable[int] = (443,),
    allowed_hosts: Sequence[str] | None = None,
    resolver: object = None,
) -> list[str]:
    """Return the vetted IPs to pin the connection to, or raise ``UrlPolicyError``.

    ``allowed_hosts`` entries are matched as host suffixes, so
    ``"cdninstagram.com"`` admits ``scontent-lhr8-1.cdninstagram.com`` but not
    ``evilcdninstagram.com`` (the boundary dot is required).
    """
    allowed_schemes = frozenset(allowed_schemes)
    allowed_ports = frozenset(allowed_ports)
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise UrlPolicyError(f'malformed URL: {exc}') from exc

    if parts.scheme not in allowed_schemes:
        raise UrlPolicyError(f'scheme {parts.scheme!r} not allowed')
    if parts.username or parts.password:
        raise UrlPolicyError('userinfo in URL not allowed')
    if parts.fragment:
        # Harmless to fetch, but it means the caller did not canonicalize.
        url = urllib.parse.urlunsplit(parts._replace(fragment=''))
    host = parts.hostname
    if not host:
        raise UrlPolicyError('missing host')
    host = host.rstrip('.').lower()
    if host in _DENY_HOSTS:
        raise UrlPolicyError(f'host {host!r} is denied')

    try:
        port = parts.port or {'https': 443, 'http': 80}[parts.scheme]
    except ValueError as exc:
        raise UrlPolicyError(f'invalid port: {exc}') from exc
    if port not in allowed_ports:
        raise UrlPolicyError(f'port {port} not allowed')

    if allowed_hosts is not None and not host_allowed(host, allowed_hosts):
        raise UrlPolicyError(f'host {host!r} is not in the connector allowlist')

    literal = parse_ip_literal(host)
    if literal is not None:
        # Never send a literal through DNS: resolve the policy question here.
        if not is_public_ip(literal):
            raise UrlPolicyError(f'non-public address literal: {literal}')
        return [str(literal)]

    resolve = resolver if callable(resolver) else _resolve
    ips = resolve(host, port)
    if not ips:
        raise UrlPolicyError(f'no addresses for {host!r}')
    bad = sorted(ip for ip in ips if not is_public_ip(ip))
    if bad:
        raise UrlPolicyError(f'non-public address(es) for {host!r}: {bad}')
    return sorted(ips)


def host_allowed(host: str, allowed: Sequence[str]) -> bool:
    host = host.rstrip('.').lower()
    for suffix in allowed:
        suffix = suffix.lstrip('.').lower()
        if host == suffix or host.endswith('.' + suffix):
            return True
    return False


def same_origin(a: str, b: str) -> bool:
    pa, pb = urllib.parse.urlsplit(a), urllib.parse.urlsplit(b)
    return (pa.scheme, (pa.hostname or '').lower(), pa.port) == (
        pb.scheme, (pb.hostname or '').lower(), pb.port,
    )
