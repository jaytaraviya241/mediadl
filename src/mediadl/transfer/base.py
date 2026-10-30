"""Transfer-layer contracts (spec section 5.5)."""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Protocol, runtime_checkable

from mediadl.domain.models import MediaKind, Representation
from mediadl.errors import CancelledByUser


@dataclass(frozen=True, slots=True)
class TransferLimits:
    max_bytes: int = 4 * 1024**3
    chunk_size: int = 1024 * 1024
    max_playlist_bytes: int = 5 * 1024 * 1024
    segment_concurrency: int = 4


@dataclass(frozen=True, slots=True)
class Validators:
    """Cache validators that decide whether a partial file may be resumed."""

    etag: str | None = None
    last_modified: str | None = None
    total_length: int | None = None

    @property
    def strong_etag(self) -> str | None:
        if self.etag and not self.etag.startswith('W/'):
            return self.etag
        return None

    def if_range_value(self) -> str | None:
        """RFC 9110 section 13.1.5 forbids weak validators in If-Range."""
        return self.strong_etag or self.last_modified

    def matches(self, other: Validators) -> bool:
        if self.strong_etag and other.strong_etag:
            return self.strong_etag == other.strong_etag
        if self.last_modified and other.last_modified:
            return self.last_modified == other.last_modified
        return False


@dataclass(slots=True)
class TransferPlan:
    rep: Representation
    dest: Path
    limits: TransferLimits
    headers: Mapping[str, str] = field(default_factory=dict)
    auth_hook: Callable[[str], Mapping[str, str] | None] | None = None
    allowed_hosts: tuple[str, ...] | None = None
    expected_kind: MediaKind | None = None
    #: Content-addressed sources may resume without validators.
    immutable: bool = False

    @property
    def part_path(self) -> Path:
        return self.dest.with_suffix(self.dest.suffix + '.part')

    def with_locator(self, locator: object) -> TransferPlan:
        from dataclasses import replace as _replace

        return TransferPlan(
            rep=_replace(self.rep, locator=locator),  # type: ignore[arg-type]
            dest=self.dest, limits=self.limits, headers=self.headers,
            auth_hook=self.auth_hook, allowed_hosts=self.allowed_hosts,
            expected_kind=self.expected_kind, immutable=self.immutable,
        )


@dataclass(frozen=True, slots=True)
class TransferResult:
    path: Path
    bytes_written: int
    sha256: str
    validators: Validators
    resumed: bool
    attempts: int
    url_fingerprint: str
    content_type: str | None = None
    rep_id: str = ''


@dataclass(slots=True)
class ProgressReporter:
    """Throttled progress callback (at most ~4 events/second per asset)."""

    callback: Callable[[str, int, int | None], None] | None = None
    min_interval_s: float = 0.25
    _last: float = 0.0

    def __call__(self, asset_id: str, done: int, total: int | None, *, force: bool = False) -> None:
        if self.callback is None:
            return
        now = time.monotonic()
        if force or now - self._last >= self.min_interval_s:
            self._last = now
            self.callback(asset_id, done, total)


@dataclass(slots=True)
class CancelToken:
    """Cooperative cancellation checked between chunks and segments."""

    job_id: str = ''
    _event: asyncio.Event = field(default_factory=asyncio.Event)

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise CancelledByUser('job cancelled', job_id=self.job_id)

    async def wait(self) -> None:
        await self._event.wait()


@runtime_checkable
class TransferEngine(Protocol):
    engine_id: ClassVar[str]

    def supports(self, rep: Representation) -> bool: ...

    async def transfer(
        self, plan: TransferPlan, progress: ProgressReporter, cancel: CancelToken
    ) -> TransferResult: ...


async def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash a file off the event loop."""

    def _hash() -> str:
        digest = hashlib.sha256()
        with open(path, 'rb') as fh:
            while chunk := fh.read(chunk_size):
                digest.update(chunk)
        return digest.hexdigest()

    return await asyncio.to_thread(_hash)
