"""Progressive HTTP transfer with validated resume (spec section 7.6).

Differences from yt-dlp's ``HttpFD`` that matter:
  * resume is gated on ``If-Range`` with a *strong* validator, not on
    ``Content-Range`` alone;
  * a 416 completes the file only on an exact length match (yt-dlp tolerates
    +/-100 bytes);
  * the body is sniffed and size-capped while streaming, not afterwards.
"""
from __future__ import annotations

import email.utils
import hashlib
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO, ClassVar

import httpx

from mediadl.domain.models import HttpLocator, Representation
from mediadl.errors import (
    ClientResponseError,
    ContentTypeMismatchError,
    IntegrityError,
    LocatorExpiredError,
    NetworkError,
    NotFoundError,
    RateLimited,
    SizeLimitError,
    UpstreamError,
)
from mediadl.net.guarded_client import GuardedHttpClient
from mediadl.net.redact import url_fingerprint
from mediadl.obs.logging import get_logger
from mediadl.processing import sniff
from mediadl.transfer.base import (
    CancelToken,
    ProgressReporter,
    TransferPlan,
    TransferResult,
    Validators,
)
from mediadl.transfer.resume_state import ResumeState

log = get_logger(__name__)

#: Statuses that mean "this locator is no longer valid; ask for a new one".
EXPIRED_STATUSES = frozenset({401, 403, 410})


def parse_validators(response: httpx.Response, *, offset: int = 0) -> Validators:
    etag = response.headers.get('etag')
    last_modified = _strong_last_modified(response)
    total = _total_length(response, offset=offset)
    return Validators(etag=etag, last_modified=last_modified, total_length=total)


def _strong_last_modified(response: httpx.Response) -> str | None:
    """A Last-Modified is a strong validator only if it predates Date by >= 1s.

    RFC 9110 section 8.8.2.2.
    """
    last_modified: str | None = response.headers.get('last-modified')
    date: str | None = response.headers.get('date')
    if not last_modified:
        return None
    if not date:
        return None
    try:
        lm = email.utils.parsedate_to_datetime(last_modified)
        dt = email.utils.parsedate_to_datetime(date)
    except (TypeError, ValueError):
        return None
    if dt - lm >= timedelta(seconds=1):
        return last_modified
    return None


def _total_length(response: httpx.Response, *, offset: int = 0) -> int | None:
    content_range = response.headers.get('content-range')
    if content_range and '/' in content_range:
        total = content_range.rsplit('/', 1)[1].strip()
        if total.isdigit():
            return int(total)
    length = response.headers.get('content-length')
    if length and length.isdigit():
        return int(length) + (offset if response.status_code == 206 else 0)
    return None


def _parse_content_range(value: str) -> tuple[int, int, int] | None:
    try:
        units, _, spec = value.partition(' ')
        if units.strip().lower() != 'bytes':
            return None
        range_part, _, total_part = spec.partition('/')
        start_s, _, end_s = range_part.partition('-')
        return int(start_s), int(end_s), int(total_part)
    except (ValueError, AttributeError):
        return None


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    from datetime import UTC

    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class HttpTransfer:
    """Streams one HTTP(S) representation to ``plan.part_path``."""

    engine_id: ClassVar[str] = 'http'

    def __init__(self, client: GuardedHttpClient) -> None:
        self.client = client

    def supports(self, rep: Representation) -> bool:
        return isinstance(rep.locator, HttpLocator)

    async def transfer(
        self, plan: TransferPlan, progress: ProgressReporter, cancel: CancelToken
    ) -> TransferResult:
        locator = plan.rep.locator
        assert isinstance(locator, HttpLocator)
        cancel.raise_if_cancelled()

        if locator.expires_at is not None:
            from datetime import UTC

            if locator.expires_at <= datetime.now(UTC):
                raise LocatorExpiredError(
                    'locator expired before transfer started', rep_id=plan.rep.rep_id
                )

        part = plan.part_path
        part.parent.mkdir(parents=True, exist_ok=True)
        fingerprint = url_fingerprint(locator.url)

        resume_from, prior = self._plan_resume(part, fingerprint, plan)
        headers = dict(plan.headers)
        headers.update(locator.headers)
        if resume_from and prior is not None:
            if_range = prior.validators.if_range_value()
            if if_range:
                headers['Range'] = f'bytes={resume_from}-'
                headers['If-Range'] = if_range
            elif plan.immutable:
                headers['Range'] = f'bytes={resume_from}-'
            else:
                resume_from = 0

        async with self.client.stream(
            'GET', locator.url,
            headers=headers,
            auth_hook=plan.auth_hook if locator.send_credentials else None,
            allowed_hosts=plan.allowed_hosts,
        ) as response:
            status = response.status_code

            if status in EXPIRED_STATUSES:
                raise LocatorExpiredError(
                    f'locator rejected with HTTP {status}', status=status, rep_id=plan.rep.rep_id
                )
            if status == 429:
                retry_after = parse_retry_after(response.headers.get('retry-after'))
                raise RateLimited('provider throttled the transfer', retry_after=retry_after)
            if status == 416:
                return await self._handle_416(plan, part, prior, fingerprint, progress)
            if status >= 500:
                raise UpstreamError(f'server error HTTP {status}', status=status)
            if status == 408:
                raise NetworkError('request timeout reported by the server', status=status)
            if status == 404:
                raise NotFoundError('media not found at this locator', status=status)
            if status >= 400:
                # Other 4xx are our request's fault; retrying repeats the mistake.
                raise ClientResponseError(f'rejected with HTTP {status}', status=status)
            if status not in (200, 206):
                raise UpstreamError(f'unexpected status {status}', status=status)

            append = False
            if resume_from and status == 206:
                content_range = _parse_content_range(response.headers.get('content-range', ''))
                if content_range and content_range[0] == resume_from:
                    append = True
                else:
                    log.info('resume_rejected', reason='content_range_mismatch', asset=plan.rep.rep_id)
            elif resume_from and status == 200:
                log.info('resume_restarted', reason='server_ignored_range', asset=plan.rep.rep_id)

            validators = parse_validators(response, offset=resume_from if append else 0)
            total = validators.total_length
            if total is not None and total > plan.limits.max_bytes:
                raise SizeLimitError(
                    'declared size exceeds the limit', declared=total, limit=plan.limits.max_bytes
                )

            self._check_content_type(response, plan)
            written, digest = await self._stream_to_disk(
                response, plan, part, append=append, resume_from=resume_from,
                progress=progress, cancel=cancel, validators=validators,
            )

        expected = validators.total_length
        if expected is not None and written != expected:
            raise IntegrityError(
                'short read: body did not match the declared length',
                expected=expected, written=written,
            )

        ResumeState.clear(part)
        return TransferResult(
            path=part,
            bytes_written=written,
            sha256=digest,
            validators=Validators(validators.etag, validators.last_modified, written),
            resumed=append,
            attempts=1,
            url_fingerprint=fingerprint,
            content_type=response.headers.get('content-type'),
            rep_id=plan.rep.rep_id,
        )

    # -- internals ---------------------------------------------------------
    def _plan_resume(
        self, part: Path, fingerprint: str, plan: TransferPlan
    ) -> tuple[int, ResumeState | None]:
        if not part.exists():
            ResumeState.clear(part)
            return 0, None
        size = part.stat().st_size
        state = ResumeState.read(part)
        if state is None or state.url_fingerprint != fingerprint or size == 0:
            part.unlink(missing_ok=True)
            ResumeState.clear(part)
            return 0, None
        if state.bytes_written != size:
            # The process died between write and state flush; trust the file.
            state = ResumeState.new(fingerprint, state.validators, size)
        if state.validators.if_range_value() is None and not plan.immutable:
            part.unlink(missing_ok=True)
            ResumeState.clear(part)
            return 0, None
        return size, state

    def _check_content_type(self, response: httpx.Response, plan: TransferPlan) -> None:
        kind = plan.expected_kind
        if kind is None:
            return
        content_type = (response.headers.get('content-type') or '').split(';')[0].strip().lower()
        if not content_type:
            return
        if not content_type.startswith(sniff.expected_families(kind)):
            raise ContentTypeMismatchError(
                f'server declared {content_type!r} for a {kind} asset',
                content_type=content_type, kind=str(kind),
            )

    async def _stream_to_disk(
        self,
        response: httpx.Response,
        plan: TransferPlan,
        part: Path,
        *,
        append: bool,
        resume_from: int,
        progress: ProgressReporter,
        cancel: CancelToken,
        validators: Validators,
    ) -> tuple[int, str]:
        digest = hashlib.sha256()
        written = 0
        if append:
            written = resume_from
            with open(part, 'rb') as fh:  # re-hash the prefix; state is not serializable
                while chunk := fh.read(1024 * 1024):
                    digest.update(chunk)
        mode = 'ab' if append else 'wb'
        checked_magic = False
        head = b''
        fingerprint = url_fingerprint(str(response.request.url))
        state_validators = Validators(
            validators.etag, validators.last_modified, validators.total_length
        )
        # Flush resume state often enough that an interrupted transfer of any
        # size stays resumable, but not so often that it dominates the I/O.
        flush_every = max(plan.limits.chunk_size * 4, 2 * 1024 * 1024)
        state_flush_at = written + flush_every

        def save_state(count: int) -> None:
            if count > 0:
                ResumeState.new(fingerprint, state_validators, count).write(part)

        try:
            out: IO[bytes]
            with open(part, mode) as out:
                if not append:
                    out.truncate(0)
                async for chunk in response.aiter_raw(plan.limits.chunk_size):
                    cancel.raise_if_cancelled()
                    if not chunk:
                        continue
                    if not checked_magic and not append:
                        head += chunk[: sniff.SNIFF_BYTES * 4]
                        if len(head) >= sniff.SNIFF_BYTES or len(chunk) < plan.limits.chunk_size:
                            self._check_magic(head, plan)
                            checked_magic = True
                    written += len(chunk)
                    if written > plan.limits.max_bytes:
                        out.flush()
                        raise SizeLimitError(
                            'transfer exceeded the size limit',
                            limit=plan.limits.max_bytes, written=written,
                        )
                    digest.update(chunk)
                    out.write(chunk)
                    if written >= state_flush_at:
                        out.flush()
                        os.fsync(out.fileno())
                        save_state(written)
                        state_flush_at = written + flush_every
                    progress(plan.rep.rep_id, written, validators.total_length)
                out.flush()
                os.fsync(out.fileno())
        except BaseException as exc:
            # A dropped connection or cancellation must leave a resumable
            # prefix behind, so persist what actually reached the disk.
            try:
                save_state(part.stat().st_size if part.exists() else 0)
            except OSError:
                pass
            # Errors raised while iterating the body bypass the send-time
            # mapping, so translate them here too.
            if isinstance(exc, httpx.RemoteProtocolError):
                raise IntegrityError(
                    'connection closed before the body was complete',
                    written=written, expected=validators.total_length,
                ) from exc
            if isinstance(exc, httpx.TimeoutException):
                raise NetworkError('timeout while reading the body', written=written) from exc
            if isinstance(exc, httpx.TransportError):
                raise NetworkError(
                    f'transport error while reading the body: {type(exc).__name__}',
                    written=written,
                ) from exc
            raise

        if not checked_magic and head:
            self._check_magic(head, plan)
        progress(plan.rep.rep_id, written, validators.total_length, force=True)
        save_state(written)
        return written, digest.hexdigest()

    def _check_magic(self, head: bytes, plan: TransferPlan) -> None:
        if plan.expected_kind is None or not head:
            return
        detected = sniff.detect_bytes(head)
        if not detected.known:
            raise ContentTypeMismatchError(
                'response bytes are not a recognized media container',
                kind=str(plan.expected_kind),
            )
        if not sniff.compatible(detected, plan.expected_kind):
            raise ContentTypeMismatchError(
                f'expected {plan.expected_kind}, got {detected.kind}',
                expected=str(plan.expected_kind), detected=str(detected.kind),
            )

    async def _handle_416(
        self,
        plan: TransferPlan,
        part: Path,
        prior: ResumeState | None,
        fingerprint: str,
        progress: ProgressReporter,
    ) -> TransferResult:
        """416 means our offset is past the end. Complete only on an exact match."""
        locator = plan.rep.locator
        assert isinstance(locator, HttpLocator)
        size = part.stat().st_size if part.exists() else 0
        response = await self.client.request(
            'HEAD', locator.url,
            headers=dict(locator.headers),
            auth_hook=plan.auth_hook if locator.send_credentials else None,
            allowed_hosts=plan.allowed_hosts,
        )
        validators = parse_validators(response)
        exact_length = validators.total_length is not None and validators.total_length == size
        validators_match = prior is not None and validators.matches(prior.validators)
        if exact_length and (validators_match or plan.immutable):
            digest = hashlib.sha256()
            with open(part, 'rb') as fh:
                while chunk := fh.read(1024 * 1024):
                    digest.update(chunk)
            progress(plan.rep.rep_id, size, size, force=True)
            ResumeState.clear(part)
            log.info('resume_completed_on_416', asset=plan.rep.rep_id, size=size)
            return TransferResult(
                path=part, bytes_written=size, sha256=digest.hexdigest(),
                validators=Validators(validators.etag, validators.last_modified, size),
                resumed=True, attempts=1, url_fingerprint=fingerprint,
                content_type=response.headers.get('content-type'), rep_id=plan.rep.rep_id,
            )
        part.unlink(missing_ok=True)
        ResumeState.clear(part)
        raise IntegrityError(
            'range not satisfiable and the partial file does not match; restarting',
            size=size, declared=validators.total_length,
        )


__all__ = ['EXPIRED_STATUSES', 'HttpTransfer', 'parse_retry_after', 'parse_validators']
