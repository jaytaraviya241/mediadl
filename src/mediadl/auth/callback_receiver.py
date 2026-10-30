"""Narrow loopback receiver for a browser OAuth callback.

This service intentionally has no access to the dashboard, job database, or
credential vault.  It is useful when an operator tunnels only this listener
to obtain a temporary HTTPS redirect URI during local development.  The
browser keeps the returned URL in its address bar so it can be pasted into the
normal, state-checked completion flow; this receiver never records it.
"""
from __future__ import annotations

import ipaddress
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final

CALLBACK_PATH: Final = '/oauth/instagram/callback'
_CONTENT_SECURITY_POLICY: Final = (
    "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class LoopbackOnlyError(ValueError):
    """Raised when a callback receiver would bind somewhere non-local."""


_SUCCESS_PAGE: Final = b"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <title>Authorization received &mdash; mediadl</title>
</head>
<body>
  <main>
    <h1>Authorization received</h1>
    <p>Return to mediadl and paste the complete URL from this browser address bar into Redirect URL.</p>
    <p>This temporary receiver does not store your authorization code.</p>
  </main>
</body>
</html>
"""

_NOT_FOUND_PAGE: Final = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Not found</title></head>
<body><p>Not found.</p></body></html>
"""

_METHOD_NOT_ALLOWED_PAGE: Final = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Method not allowed</title></head>
<body><p>Method not allowed.</p></body></html>
"""


def _loopback_address(host: str, port: int) -> tuple[int, tuple]:
    """Resolve *host* and refuse it unless every target is a loopback address."""
    value = host.strip().strip('[]')
    if not value:
        raise LoopbackOnlyError('callback receiver host must be a loopback address')

    try:
        addresses = socket.getaddrinfo(value, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise LoopbackOnlyError(f'could not resolve callback receiver host: {value}') from exc
    if not addresses:
        raise LoopbackOnlyError('callback receiver host must be a loopback address')

    for family, _, _, _, sockaddr in addresses:
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise LoopbackOnlyError('callback receiver host must use IPv4 or IPv6 loopback')
        if not ipaddress.ip_address(sockaddr[0]).is_loopback:
            raise LoopbackOnlyError(
                'refusing to bind the OAuth callback receiver outside loopback; '
                'tunnel a loopback listener instead'
            )

    family, _, _, _, sockaddr = addresses[0]
    return family, sockaddr


class OAuthCallbackReceiver(ThreadingHTTPServer):
    """A threaded, loopback-only HTTP server which exposes a single callback."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, host: str = '127.0.0.1', port: int = 8766) -> None:
        family, sockaddr = _loopback_address(host, port)
        self.address_family = family
        super().__init__(sockaddr, _OAuthCallbackHandler)


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Do not parse, log, reflect, or retain callback query parameters."""

    server_version = 'mediadl-oauth-callback'
    sys_version = ''

    def log_message(self, _format: str, *args: object) -> None:
        """Suppress the base server's request-line logging (which includes query data)."""

    def do_GET(self) -> None:
        # Query strings contain the authorization code and must never be used
        # outside this routing check.  Fragments do not reach an HTTP server.
        path = self.path.partition('?')[0]
        if path != CALLBACK_PATH:
            self._send(404, _NOT_FOUND_PAGE)
            return
        self._send(200, _SUCCESS_PAGE)

    def do_HEAD(self) -> None:
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:
        self._method_not_allowed()

    def do_POST(self) -> None:
        self._method_not_allowed()

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def do_CONNECT(self) -> None:
        self._method_not_allowed()

    def do_TRACE(self) -> None:
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        self._send(405, _METHOD_NOT_ALLOWED_PAGE, allow='GET')

    def _send(self, status: int, body: bytes, *, allow: str | None = None) -> None:
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', _CONTENT_SECURITY_POLICY)
        self.send_header('Permissions-Policy', 'clipboard-read=(), clipboard-write=()')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        if allow is not None:
            self.send_header('Allow', allow)
        self.end_headers()
        self.wfile.write(body)


def serve_callback(host: str = '127.0.0.1', port: int = 8766) -> None:
    """Run the isolated receiver until interrupted by the local operator."""
    server = OAuthCallbackReceiver(host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


__all__ = ['CALLBACK_PATH', 'LoopbackOnlyError', 'OAuthCallbackReceiver', 'serve_callback']
