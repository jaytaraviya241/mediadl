"""Local HTTP fixture server for integration tests (spec section 9.4).

Implements enough of RFC 9110 range semantics to exercise the resume logic,
plus deliberate fault modes. No test ever reaches the public internet.
"""
from __future__ import annotations

import email.utils
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Resource:
    body: bytes
    content_type: str = 'application/octet-stream'
    etag: str | None = '"v1"'
    last_modified: str | None = None
    accept_ranges: bool = True
    #: Close the connection after this many bytes of the body.
    drop_after: int | None = None
    #: Ignore a Range header and answer 200 with the whole body.
    ignore_range: bool = False
    #: Serve this ETag from the second request onward (validator flip).
    etag_after_first: str | None = None
    #: Status codes to return before serving normally, consumed in order.
    fail_sequence: list[int] = field(default_factory=list)
    retry_after: str | None = None
    #: Seconds to sleep before each chunk.
    slow: float = 0.0
    #: Lie about Content-Length (short body -> IntegrityError).
    content_length_override: int | None = None
    hits: int = 0
    range_requests: int = 0


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'mediadl-fixture/1.0'

    @property
    def resources(self) -> dict[str, Resource]:
        return self.server.resources  # type: ignore[attr-defined]

    def log_message(self, *args: Any) -> None:
        pass

    def do_HEAD(self) -> None:
        self._serve(head_only=True)

    def do_GET(self) -> None:
        self._serve(head_only=False)

    # -- helpers -----------------------------------------------------------
    def _resource(self) -> Resource | None:
        path = self.path.split('?')[0]
        if path.startswith('/redirect/'):
            return None
        return self.resources.get(path)

    def _send_simple(self, status: int, body: bytes = b'', headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _serve(self, *, head_only: bool) -> None:
        path = self.path.split('?')[0]

        # Redirect chains: /redirect/<n>/<target-path>
        if path.startswith('/redirect/'):
            count, _, rest = path[len('/redirect/'):].partition('/')
            try:
                hops = int(count)
            except ValueError:
                self._send_simple(400, b'bad redirect spec')
                return
            location = f'/redirect/{hops - 1}/{rest}' if hops > 1 else f'/{rest}'
            self._send_simple(302, headers={'Location': location})
            return
        if path.startswith('/absolute-redirect/'):
            self._send_simple(302, headers={'Location': self.path[len('/absolute-redirect/'):]})
            return

        resource = self.resources.get(path)
        if resource is None:
            self._send_simple(404, b'not found')
            return

        resource.hits += 1
        if resource.fail_sequence:
            status = resource.fail_sequence.pop(0)
            headers = {'Content-Type': 'text/plain'}
            if resource.retry_after and status in (429, 503):
                headers['Retry-After'] = resource.retry_after
            self._send_simple(status, b'transient', headers)
            return

        etag = resource.etag
        if resource.etag_after_first and resource.hits > 1:
            etag = resource.etag_after_first

        body = resource.body
        total = len(body)
        start, end = 0, total - 1
        status = 200

        range_header = self.headers.get('Range')
        if_range = self.headers.get('If-Range')
        if range_header:
            resource.range_requests += 1

        range_ok = bool(range_header) and resource.accept_ranges and not resource.ignore_range
        if range_ok and if_range is not None and etag is not None and if_range.strip() != etag:
            # Validator changed: RFC 9110 says serve the whole representation.
            range_ok = False
            range_header = None

        if range_ok and range_header:
            try:
                spec = range_header.split('=', 1)[1]
                first, _, last = spec.partition('-')
                start = int(first) if first else 0
                end = int(last) if last else total - 1
            except (ValueError, IndexError):
                self._send_simple(400, b'bad range')
                return
            if start >= total:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{total}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            end = min(end, total - 1)
            status = 206
            body = resource.body[start:end + 1]

        self.send_response(status)
        self.send_header('Content-Type', resource.content_type)
        if etag:
            self.send_header('ETag', etag)
        self.send_header(
            'Last-Modified',
            resource.last_modified or email.utils.formatdate(time.time() - 86400, usegmt=True),
        )
        if resource.accept_ranges:
            self.send_header('Accept-Ranges', 'bytes')
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{total}')
        declared = resource.content_length_override
        self.send_header('Content-Length', str(declared if declared is not None else len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()

        if head_only:
            return
        limit = resource.drop_after
        written = 0
        for offset in range(0, len(body), 8192):
            chunk = body[offset:offset + 8192]
            if limit is not None and written + len(chunk) > limit:
                chunk = chunk[: max(0, limit - written)]
                if chunk:
                    self.wfile.write(chunk)
                self.wfile.flush()
                self.close_connection = True
                return
            if resource.slow:
                time.sleep(resource.slow)
            self.wfile.write(chunk)
            written += len(chunk)
        self.wfile.flush()


class _Server(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        # Deliberate fault modes close connections mid-body; that is the
        # point of the fixture, not an error worth printing.
        pass


class FixtureServer:
    """Context-managed threaded HTTP server serving in-memory resources."""

    def __init__(self) -> None:
        self._server = _Server(('127.0.0.1', 0), _Handler)
        self._server.resources = {}  # type: ignore[attr-defined]
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def resources(self) -> dict[str, Resource]:
        return self._server.resources  # type: ignore[attr-defined]

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f'http://{host}:{port}'

    def url(self, path: str) -> str:
        return f'{self.base_url}{path}'

    def add(self, path: str, body: bytes, **kwargs: Any) -> Resource:
        resource = Resource(body=body, **kwargs)
        self.resources[path] = resource
        return resource

    def add_directory(self, prefix: str, directory: Any, **kwargs: Any) -> None:
        """Serve every file in a directory under ``prefix`` (for HLS/DASH)."""
        from pathlib import Path

        directory = Path(directory)
        types = {
            '.m3u8': 'application/vnd.apple.mpegurl',
            '.mpd': 'application/dash+xml',
            '.ts': 'video/mp2t',
            '.m4s': 'video/iso.segment',
            '.mp4': 'video/mp4',
            '.key': 'application/octet-stream',
        }
        for file in sorted(directory.rglob('*')):
            if not file.is_file():
                continue
            relative = file.relative_to(directory).as_posix()
            self.add(
                f'{prefix.rstrip("/")}/{relative}',
                file.read_bytes(),
                content_type=types.get(file.suffix, 'application/octet-stream'),
                **kwargs,
            )

    def start(self) -> FixtureServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> FixtureServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
