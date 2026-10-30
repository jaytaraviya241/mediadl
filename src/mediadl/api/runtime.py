"""Dashboard runtime: one AppContext, background jobs, live events.

The dashboard is not a separate implementation of anything. It submits jobs
through ``MediaDLService`` and reads the same SQLite job store the CLI uses,
so a job started here is visible to ``mediadl jobs list`` and vice versa.

Live updates come from two sources that together are self-healing:
  * explicit lifecycle and progress events pushed as they happen, and
  * a periodic snapshot tick, so a client that missed an event still
    converges on the real state.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
import urllib.parse
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mediadl.app.context import AppContext, build
from mediadl.app.service import MediaDLService
from mediadl.auth.connections import ConnectionStore
from mediadl.auth.credentials import CredentialProvider
from mediadl.auth.vault import KeyProvider, TokenVault
from mediadl.config import Settings
from mediadl.domain.invariants import as_collection, validate
from mediadl.domain.models import (
    Asset,
    Collection,
    HttpLocator,
    Representation,
    RepresentationRole,
    SourceMetadata,
)
from mediadl.errors import (
    AuthRequiredError,
    ClientResponseError,
    ContentTypeMismatchError,
    InternalError,
    LocatorExpiredError,
    MediaDLError,
    NotFoundError,
    PolicyDeniedError,
    SizeLimitError,
    UnsupportedSourceError,
    UpstreamError,
)
from mediadl.jobs.states import JobState
from mediadl.jobs.store_sqlite import Job
from mediadl.net.redact import redact_text
from mediadl.obs.logging import get_logger
from mediadl.policy.url_guard import UrlPolicyError
from mediadl.selection import choose, enumerate_options, hard_filter
from mediadl.sources.base import AccessContext, SourceRequest
from mediadl.transfer.base import CancelToken, ProgressReporter

log = get_logger(__name__)

#: How often the SSE stream emits a full snapshot, in seconds.
SNAPSHOT_INTERVAL_S = 2.0

#: Inspection previews are fetched only through a short-lived, opaque local
#: URL.  This keeps CDN locators and their (often signed) query strings out of
#: the browser-facing inspection JSON.
PREVIEW_TTL_S = 5 * 60
MAX_PREVIEW_BYTES = 10 * 1024 * 1024
MAX_LIVE_PREVIEWS = 256


#: The dashboard intentionally returns a narrow, display-safe view of a
#: connector result.  In particular, source metadata and representation
#: locators must not accidentally turn into an API for signed download URLs.
_DISPLAY_METADATA_FIELDS: dict[str, tuple[str, ...]] = {
    'title': ('title',),
    'caption': ('caption',),
    'author': ('username', 'author', 'owner', 'creator'),
    'timestamp': ('timestamp', 'published_at', 'created_at', 'modified_at'),
    'alt_text': ('alt_text',),
    'filename': ('filename', 'name', 'directory'),
    'media_product_type': ('media_product_type',),
}


class _FacebookConnectorUnavailable(UnsupportedSourceError):
    """A truthful preflight result until an authorized Facebook connector exists."""

    user_message = 'Facebook links need a Facebook connector, which is not installed.'


@dataclass(frozen=True, slots=True)
class _PreviewTarget:
    locator: HttpLocator
    allowed_hosts: tuple[str, ...] | None
    expires_at: float


class EventBus:
    """Fan-out of dashboard events to every connected browser."""

    def __init__(self, max_queue: int = 256) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._max_queue = max_queue

    def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A stalled client must not slow the downloader down.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(self._max_queue)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


@dataclass(slots=True)
class LiveProgress:
    job_id: str
    asset_id: str
    done: int
    total: int | None
    updated_at: float = field(default_factory=time.monotonic)

    @property
    def percent(self) -> float | None:
        if not self.total:
            return None
        return min(100.0, round(self.done * 100 / self.total, 1))


class DashboardRuntime:
    """Owns the application context and the set of running jobs."""

    def __init__(self, settings: Settings | None = None, ctx: AppContext | None = None) -> None:
        self.settings = settings or Settings()
        self.ctx = ctx or build(self.settings)
        self.bus = EventBus()
        self.connections = ConnectionStore(
            self.settings.data_dir() / 'connections.sqlite3',
            TokenVault(KeyProvider(self.settings.data_dir())),
        )
        self.credentials = CredentialProvider(self.connections)
        self.service = MediaDLService(
            self.ctx, principal_id='local', credentials=self.credentials
        )
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._cancels: dict[str, CancelToken] = {}
        self._progress: dict[str, dict[str, LiveProgress]] = {}
        self._previews: dict[str, _PreviewTarget] = {}
        self._semaphore = asyncio.Semaphore(self.settings.concurrency.active_jobs_per_user)
        self.started_at = time.time()

    async def aclose(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._previews.clear()
        self.connections.close()
        await self.ctx.aclose()

    # -- jobs --------------------------------------------------------------
    async def submit(
        self,
        raw: str,
        connection_id: str | None = None,
        *,
        options: Mapping[str, object] | None = None,
    ) -> Job:
        source = raw.strip()
        requested_connection = connection_id.strip() if connection_id else None
        request = SourceRequest(raw=source, connection_id=requested_connection)
        connector = self.ctx.registry.select(request)
        _reject_direct_facebook_page_fallback(source, connector.connector_id, request.redacted())
        job = await self.service.submit(
            source,
            connection_id=requested_connection,
            options=dict(options or {}),
        )
        self.bus.publish({'type': 'job.submitted', 'job_id': job.id, 'state': str(job.state)})
        if job.state in (JobState.QUEUED,):
            self.start(job)
        return job

    async def inspect(self, raw: str, connection_id: str | None = None) -> dict[str, Any]:
        """Resolve a source for the dashboard without creating a job.

        This is deliberately the same authorized connector path used by a job:
        connector selection happens first, credential acquisition is delegated
        to ``CredentialProvider``, and the resolved result is validated before
        it is made visible.  The returned dictionary is a display projection,
        not a serialized domain object, so credentials, headers, media
        locators, and arbitrary provider fields cannot escape to the browser.
        """
        from mediadl.errors import InputError

        source = raw.strip()
        if not source:
            raise InputError('source is blank')
        requested_connection = connection_id.strip() if connection_id else None
        request = SourceRequest(raw=source, connection_id=requested_connection)
        connector_id: str | None = None

        try:
            connector = self.ctx.registry.select(request)
            connector_id = connector.connector_id
            _reject_direct_facebook_page_fallback(
                source, connector.connector_id, request.redacted()
            )
            # This checks the configured connector allowlist and any durable
            # quota policy.  There is no active job here, because inspection
            # itself never creates one.
            self.ctx.admission.admit('local', request, connector)

            credential = None
            if 'none' not in connector.capabilities.auth_modes:
                if self.credentials is None:
                    raise AuthRequiredError(
                        f'{connector.connector_id} requires a connected account',
                        connector=connector.connector_id,
                    )
                credential = await self.credentials.acquire(
                    'local',
                    requested_connection,
                    connector.required_scopes(request),
                    connector_id=connector.connector_id,
                )

            access = AccessContext(
                principal_id='local',
                connection_id=requested_connection,
                credential=credential,
                http=self.ctx.http,
                deadline=time.monotonic() + self.ctx.settings.limits.job_timeout_s,
            )
            collection = as_collection(validate(await connector.resolve(request, access)))
            self.ctx.asset_policy.check_collection_size(len(collection.assets))
        except MediaDLError:
            raise
        except Exception as exc:
            # A connector bug must remain a typed, redacted dashboard error,
            # just as it does when a job runs through the orchestrator.
            log.warning(
                'dashboard_inspect_error', connector=connector_id, error_type=type(exc).__name__
            )
            raise InternalError.from_exc(exc) from exc

        return _inspection_payload(
            source=request.redacted(),
            collection=collection,
            connector_id=connector.connector_id,
            requires_auth='none' not in connector.capabilities.auth_modes,
            connection_id=credential.connection_id if credential is not None else None,
            enabled_protocols=self.ctx.enabled_protocols,
            selection_policy=self.ctx.selection_policy,
            preview_url_for=lambda asset: self._register_preview(
                asset,
                allowed_hosts=connector.capabilities.allowed_media_hosts or None,
            ),
        )

    async def preview(self, preview_id: str) -> tuple[bytes, str]:
        """Fetch one previously inspected image through the guarded client.

        The endpoint serving this result receives opaque bytes and never the
        provider URL, signed query string, or connector headers.  Targets live
        only for a short time in this process; they are not persisted.
        """
        self._purge_previews()
        target = self._previews.get(preview_id)
        if target is None:
            raise NotFoundError('preview is missing or expired')
        if target.locator.expires_at is not None and target.locator.expires_at <= datetime.now(UTC):
            raise LocatorExpiredError('preview locator expired')

        try:
            async with self.ctx.http.stream(
                'GET',
                target.locator.url,
                headers=target.locator.headers,
                allowed_hosts=target.allowed_hosts,
            ) as response:
                _check_preview_response(response.status_code)
                content_type = (
                    (response.headers.get('content-type') or '').split(';', 1)[0].strip().lower()
                )
                if (
                    not content_type.startswith('image/')
                    or content_type in ('image/svg+xml', 'image/svg')
                ):
                    raise ContentTypeMismatchError(
                        'preview response was not an image', content_type=content_type or None
                    )
                content_length = response.headers.get('content-length')
                if content_length and content_length.isdigit() and int(content_length) > MAX_PREVIEW_BYTES:
                    raise SizeLimitError(
                        'preview is too large', declared=int(content_length), limit=MAX_PREVIEW_BYTES
                    )

                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_raw():
                    size += len(chunk)
                    if size > MAX_PREVIEW_BYTES:
                        raise SizeLimitError('preview is too large', limit=MAX_PREVIEW_BYTES)
                    chunks.append(chunk)
            return b''.join(chunks), content_type
        except UrlPolicyError as exc:
            raise PolicyDeniedError('preview URL was rejected by the network policy') from exc
        except MediaDLError:
            raise
        except Exception as exc:
            log.warning('dashboard_preview_error', error_type=type(exc).__name__)
            raise InternalError.from_exc(exc) from exc

    def _register_preview(
        self,
        asset: Asset,
        *,
        allowed_hosts: tuple[str, ...] | None,
    ) -> str | None:
        locator = _preview_locator(asset)
        if locator is None:
            return None
        self._purge_previews()
        while len(self._previews) >= MAX_LIVE_PREVIEWS:
            oldest = next(iter(self._previews), None)
            if oldest is None:
                break
            self._previews.pop(oldest, None)
        preview_id = secrets.token_urlsafe(24)
        self._previews[preview_id] = _PreviewTarget(
            locator=locator,
            allowed_hosts=allowed_hosts,
            expires_at=time.monotonic() + PREVIEW_TTL_S,
        )
        return f'/api/previews/{preview_id}'

    def _purge_previews(self) -> None:
        now = time.monotonic()
        for preview_id, target in tuple(self._previews.items()):
            if target.expires_at <= now:
                self._previews.pop(preview_id, None)

    def start(self, job: Job) -> None:
        if job.id in self._tasks and not self._tasks[job.id].done():
            return
        cancel = CancelToken(job_id=job.id)
        self._cancels[job.id] = cancel
        task = asyncio.create_task(self._run(job, cancel), name=f'job:{job.id}')
        self._tasks[job.id] = task
        task.add_done_callback(lambda _t, jid=job.id: self._tasks.pop(jid, None))

    async def _run(self, job: Job, cancel: CancelToken) -> None:
        def on_progress(asset_id: str, done: int, total: int | None) -> None:
            live = LiveProgress(job.id, asset_id, done, total)
            self._progress.setdefault(job.id, {})[asset_id] = live
            self.bus.publish({
                'type': 'progress', 'job_id': job.id, 'asset_id': asset_id,
                'done': done, 'total': total, 'percent': live.percent,
            })

        async with self._semaphore:
            self.bus.publish({'type': 'job.started', 'job_id': job.id})
            try:
                outcome = await self.service.run(
                    job,
                    progress=ProgressReporter(callback=on_progress, min_interval_s=0.2),
                    cancel=cancel,
                    worker_id='dashboard',
                )
                self.bus.publish({
                    'type': 'job.finished', 'job_id': job.id,
                    'state': str(outcome.state), 'counts': outcome.counts or {},
                })
            except MediaDLError as exc:
                log.warning('dashboard_job_failed', job_id=job.id, code=str(exc.code))
                self.bus.publish({
                    'type': 'job.finished', 'job_id': job.id,
                    'state': 'FAILED', 'error': exc.user_message,
                })
            except asyncio.CancelledError:
                self.bus.publish({'type': 'job.finished', 'job_id': job.id, 'state': 'CANCELLED'})
                raise
            except Exception as exc:
                log.exception('dashboard_job_error', job_id=job.id)
                self.bus.publish({
                    'type': 'job.finished', 'job_id': job.id,
                    'state': 'FAILED', 'error': str(exc),
                })
            finally:
                self._progress.pop(job.id, None)
                self._cancels.pop(job.id, None)

    async def cancel(self, job_id: str) -> bool:
        token = self._cancels.get(job_id)
        if token is None:
            return False
        token.cancel()
        self.bus.publish({'type': 'job.cancelling', 'job_id': job_id})
        return True

    def is_running(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        return task is not None and not task.done()

    def progress_for(self, job_id: str) -> list[dict[str, Any]]:
        return [
            {
                'asset_id': live.asset_id, 'done': live.done,
                'total': live.total, 'percent': live.percent,
            }
            for live in self._progress.get(job_id, {}).values()
        ]

    # -- snapshots ---------------------------------------------------------
    async def job_summaries(self, limit: int = 30) -> list[dict[str, Any]]:
        jobs = await self.ctx.jobs.list_jobs('local', limit)
        summaries = []
        for job in jobs:
            items = await self.ctx.jobs.items(job.id)
            summaries.append(self._summarize(job, items))
        return summaries

    def _summarize(self, job: Job, items: list[Any]) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for item in items:
            counts[str(item.state)] = counts.get(str(item.state), 0) + 1
        return {
            'id': job.id,
            'state': str(job.state),
            'running': self.is_running(job.id),
            'connector': job.connector_id,
            'policy': job.policy_name,
            'source': job.request.get('raw', ''),
            'created_at': job.created_at,
            'updated_at': job.updated_at,
            'attempt': job.attempt,
            'error_code': job.error_code,
            'item_counts': counts,
            'items_total': len(items),
            'progress': self.progress_for(job.id),
        }

    async def job_detail(self, job_id: str) -> dict[str, Any]:
        job = await self.ctx.jobs.get(job_id)
        items = await self.ctx.jobs.items(job_id)
        events = await self.ctx.jobs.events(job_id)
        summary = self._summarize(job, items)
        summary['items'] = [
            {
                'position': item.position,
                'asset_id': item.asset_id,
                'kind': item.kind,
                'state': str(item.state),
                'attempts': item.attempts,
                'error_code': item.error_code,
                'error_message': item.error_message,
                'output': item.output_record,
            }
            for item in items
        ]
        summary['events'] = [
            {
                'at': event['at'],
                'from_state': event['from_state'],
                'to_state': event['to_state'],
                'actor': event['actor'],
                'detail': json.loads(event['detail_json'] or '{}'),
            }
            for event in events
        ]
        summary['error_detail'] = job.error_detail
        return summary

    async def stream(self) -> AsyncIterator[str]:
        """SSE body: explicit events, plus a periodic reconciling snapshot."""
        async with self.bus.subscribe() as queue:
            snapshot = {'type': 'snapshot', 'jobs': await self.job_summaries()}
            yield _sse(snapshot)
            last_snapshot = time.monotonic()
            while True:
                timeout = max(0.1, SNAPSHOT_INTERVAL_S - (time.monotonic() - last_snapshot))
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=timeout)
                    yield _sse(event)
                except TimeoutError:
                    pass
                if time.monotonic() - last_snapshot >= SNAPSHOT_INTERVAL_S:
                    last_snapshot = time.monotonic()
                    yield _sse({'type': 'snapshot', 'jobs': await self.job_summaries()})


def _inspection_payload(
    *,
    source: str,
    collection: Collection,
    connector_id: str,
    requires_auth: bool,
    connection_id: str | None,
    enabled_protocols: frozenset[str],
    selection_policy: Any,
    preview_url_for: Callable[[Asset], str | None],
) -> dict[str, Any]:
    """Build the browser-facing projection of a resolved collection.

    Domain objects intentionally contain locators, headers, canonical URLs,
    and verbatim provider metadata.  Those are useful to the downloader but
    inappropriate for a dashboard response.  This projection only includes
    explicit display fields.  ``preview_url`` is the sole locator-derived
    value: it is restricted to an ordinary HTTP(S) image URL so the browser
    can render a preview without receiving a provider URL or request headers.
    """
    collection_metadata = _display_metadata(collection.metadata)
    collection_title = _display_title(collection_metadata)
    assets = [
        _inspection_asset(
            asset,
            enabled_protocols=enabled_protocols,
            selection_policy=selection_policy,
            preview_url=preview_url_for(asset),
        )
        for asset in collection.assets
    ]
    selectable = sum(1 for asset in assets if asset['downloadable'])

    return {
        'supported': True,
        'source': source,
        'connector_id': connector_id,
        'requires_auth': requires_auth,
        # The credential provider may choose the most recently active
        # connection when none was supplied.  Returning only its opaque ID is
        # useful to a client that wants to submit the exact same account later.
        'connection_id': connection_id,
        'collection': {
            'id': collection.collection_id,
            'kind': str(collection.kind),
            'title': collection_title,
            'asset_count': len(assets),
            'position_basis': collection.position_basis,
            'metadata': collection_metadata,
        },
        'assets': assets,
        'summary': {
            'asset_count': len(assets),
            'available_count': sum(1 for asset in assets if asset['available']),
            'downloadable_count': selectable,
        },
    }


def _inspection_asset(
    asset: Asset,
    *,
    enabled_protocols: frozenset[str],
    selection_policy: Any,
    preview_url: str | None,
) -> dict[str, Any]:
    metadata = _display_metadata(asset.metadata, surface=asset.surface)
    title = _display_title(metadata)
    selected_ids: set[str] = set()
    selection: dict[str, Any]
    candidates = [
        rep
        for rep in asset.representations
        if rep.role is not RepresentationRole.PREVIEW
        and hard_filter(
            rep, selection_policy, enabled_protocols=enabled_protocols
        ) is None
    ]
    selectable_format_ids = {
        option.primary.rep_id for option in enumerate_options(asset, candidates)
    }

    try:
        selected = choose(asset, selection_policy, enabled_protocols=enabled_protocols)
    except MediaDLError as exc:
        selection = {
            'available': False,
            'error': {
                'code': str(exc.code),
                'message': exc.user_message,
                'retryable': exc.retryable,
            },
        }
    else:
        selected_ids.add(selected.primary.rep_id)
        if selected.paired_audio is not None:
            selected_ids.add(selected.paired_audio.rep_id)
        selection = {
            'available': True,
            'selected_format_id': selected.primary.rep_id,
            'selected_audio_format_id': (
                selected.paired_audio.rep_id if selected.paired_audio is not None else None
            ),
            'container': selected.container,
            'reasons': list(selected.reasons),
        }

    formats = [
        _format_payload(
            rep,
            selected=rep.rep_id in selected_ids,
            enabled_protocols=enabled_protocols,
            selectable=rep.rep_id in selectable_format_ids,
        )
        for rep in asset.representations
    ]
    return {
        'id': asset.asset_id,
        'position': asset.position,
        'kind': str(asset.kind),
        # ``media_type`` is retained as a UI-friendly alias for clients that
        # should not need to know the internal domain field name.
        'media_type': str(asset.kind),
        'title': title,
        'availability': str(asset.availability),
        'available': str(asset.availability) == 'available',
        'downloadable': bool(selection['available']),
        'unavailable_reason': _safe_text(asset.unavailable_reason),
        'preview_url': preview_url,
        'metadata': metadata,
        'formats': formats,
        'selection': selection,
        'selected_format_id': selection.get('selected_format_id'),
    }


def _format_payload(
    rep: Representation,
    *,
    selected: bool,
    enabled_protocols: frozenset[str],
    selectable: bool,
) -> dict[str, Any]:
    declared = rep.declared
    return {
        'id': rep.rep_id,
        'label': _safe_text(rep.label) or rep.rep_id,
        'kind': str(rep.kind),
        'role': str(rep.role),
        'protocol': rep.protocol,
        'drm': rep.drm,
        'selected': selected,
        # A format is selectable only when it is a complete source rendition,
        # or the video half of a compatible V+A pair.  This deliberately
        # excludes preview images and stand-alone audio components.
        'downloadable': selectable,
        'selectable': selectable,
        # These are provider-declared values.  They are intentionally kept
        # separate from the locator and can be absent when the provider does
        # not disclose a quality dimension.
        'mime_type': declared.mime_type,
        'container': declared.container,
        'width': declared.width,
        'height': declared.height,
        'duration_s': declared.duration_s,
        'fps': declared.fps,
        'video_codec': declared.video_codec,
        'audio_codec': declared.audio_codec,
        'bitrate_kbps': declared.bitrate_kbps,
        'audio_channels': declared.audio_channels,
        'sample_rate_hz': declared.sample_rate_hz,
        'dynamic_range': declared.dynamic_range,
        'bit_depth': declared.bit_depth,
        'filesize_bytes': declared.filesize_bytes,
        'filesize_is_estimate': declared.filesize_is_estimate,
        'has_video': declared.has_video,
        'has_audio': declared.has_audio,
        'is_live': declared.is_live,
        'quality_label': _quality_label(rep),
    }


def _display_metadata(
    metadata: SourceMetadata,
    *,
    surface: str | None = None,
) -> dict[str, str | int | float | bool]:
    """Keep a small, stable allowlist of source fields for the dashboard."""
    result: dict[str, str | int | float | bool] = {}
    fields = metadata.fields
    for output_name, candidates in _DISPLAY_METADATA_FIELDS.items():
        for field_name in candidates:
            value = _safe_metadata_value(fields.get(field_name))
            if value is not None:
                result[output_name] = value
                break
    if surface and (safe_surface := _safe_text(surface)) is not None:
        result['surface'] = safe_surface
    return result


def _display_title(metadata: dict[str, str | int | float | bool]) -> str | None:
    for key in ('title', 'caption', 'filename'):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _safe_metadata_value(value: object) -> str | int | float | bool | None:
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, bool | int | float):
        return value
    return None


def _safe_text(value: object | None, *, limit: int = 500) -> str | None:
    if not isinstance(value, str):
        return None
    text = redact_text(value).strip()
    if not text:
        return None
    return text[:limit]


def _is_facebook_page_url(source: str) -> bool:
    """Whether a URL is a Facebook page/reel, not a direct CDN media URL."""
    try:
        host = (urllib.parse.urlsplit(source).hostname or '').rstrip('.').lower()
    except ValueError:
        return False
    return host in {'facebook.com', 'fb.com', 'fb.watch'} or host.endswith(
        ('.facebook.com', '.fb.com', '.fb.watch')
    )


def _reject_direct_facebook_page_fallback(
    source: str,
    connector_id: str,
    redacted_source: str,
) -> None:
    """Keep page URLs out of the generic direct-media path.

    A Facebook CDN object is still a legitimate direct URL.  A facebook.com
    page, Reel, or watch URL is not: treating the page response as media would
    make both the dashboard and its API report a false capability.  A future
    authorized Facebook connector naturally bypasses this because it wins
    connector selection before this narrow fallback guard applies.
    """
    if connector_id == 'direct_url' and _is_facebook_page_url(source):
        raise _FacebookConnectorUnavailable(
            'Facebook URL matched only the direct-media fallback',
            source=redacted_source,
            connector=connector_id,
        )


def _preview_locator(asset: Asset) -> HttpLocator | None:
    """Return one image locator that is safe for the local proxy to fetch.

    Preview representations take precedence.  For image assets, the primary
    image is a reasonable preview when the provider did not supply a separate
    thumbnail.  No locator is returned to the browser.
    """
    candidates = [
        rep for rep in asset.representations
        if rep.role is RepresentationRole.PREVIEW and isinstance(rep.locator, HttpLocator)
    ]
    if str(asset.kind) == 'image':
        candidates.extend(
            rep for rep in asset.representations
            if rep.role is RepresentationRole.PRIMARY
            and isinstance(rep.locator, HttpLocator)
            and rep.kind == asset.kind
        )
    for rep in candidates:
        locator = rep.locator
        if not isinstance(locator, HttpLocator):  # narrows for static checkers and future models
            continue
        if locator.send_credentials:
            # The preview proxy intentionally never has an access-token hook.
            # A connector that requires credentialed media needs an explicit,
            # separately designed preview capability rather than piggybacking
            # on this display endpoint.
            continue
        try:
            parts = urllib.parse.urlsplit(locator.url)
        except ValueError:
            continue
        if (
            parts.scheme not in ('http', 'https')
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
        ):
            continue
        # Fragments are not request data.  Preserve the query and connector
        # headers only inside the runtime because CDN preview locators are
        # commonly short-lived signed URLs.
        return HttpLocator(
            url=urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, '')),
            headers=locator.headers,
            expires_at=locator.expires_at,
            send_credentials=False,
        )
    return None


def _check_preview_response(status: int) -> None:
    if status in (401, 403, 410):
        raise LocatorExpiredError('preview locator was rejected', status=status)
    if status == 404:
        raise NotFoundError('preview media was not found', status=status)
    if status >= 500:
        raise UpstreamError('preview server error', status=status)
    if status >= 400:
        raise ClientResponseError('preview request was rejected', status=status)
    if status != 200:
        raise UpstreamError('unexpected preview response', status=status)


def _quality_label(rep: Representation) -> str | None:
    declared = rep.declared
    parts: list[str] = []
    if declared.width and declared.height:
        parts.append(f'{declared.width}x{declared.height}')
    elif declared.height:
        parts.append(f'{declared.height}p')
    if declared.fps:
        parts.append(f'{declared.fps:g} fps')
    if declared.bitrate_kbps:
        parts.append(f'{declared.bitrate_kbps:g} kbps')
    if declared.container:
        parts.append(declared.container.upper())
    return ' · '.join(parts) or None


def _sse(payload: dict[str, Any]) -> str:
    return f'data: {json.dumps(payload, default=str)}\n\n'


def output_file(runtime: DashboardRuntime, relative: str) -> Path:
    """Resolve a relative output path, refusing anything outside the root."""
    from mediadl.policy.paths import enforce_under_root

    return enforce_under_root(relative, runtime.ctx.output.root)
