"""Dependency wiring (spec section 5.2).

One place builds the object graph from ``Settings`` so the orchestrator
receives finished collaborators and the tests can swap any of them.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mediadl.config import Settings
from mediadl.domain.models import DashLocator, HlsLocator
from mediadl.jobs.store_sqlite import SqliteJobStore
from mediadl.net.guarded_client import GuardedHttpClient, UrlPolicy
from mediadl.obs.logging import get_logger
from mediadl.output.finalize import LocalOutputStore
from mediadl.policy.admission import AdmissionPolicy
from mediadl.policy.asset_policy import AssetPolicy
from mediadl.processing.ffmpeg_runner import FfmpegTools
from mediadl.processing.pipeline import MediaProcessor
from mediadl.processing.policies import ProcessingPolicy
from mediadl.selection.policies import SelectionPolicy
from mediadl.sources.registry import SourceRegistry
from mediadl.transfer.base import TransferLimits
from mediadl.transfer.dash.engine import DashTransfer
from mediadl.transfer.hls.engine import HlsTransfer
from mediadl.transfer.http_engine import HttpTransfer
from mediadl.transfer.local_engine import LocalFileTransfer
from mediadl.transfer.manager import TransferManager, default_engines
from mediadl.transfer.ratelimit import CircuitBreaker, RateLimiterRegistry
from mediadl.transfer.retry import RetryBudget

log = get_logger(__name__)


@dataclass(slots=True)
class AppContext:
    settings: Settings
    registry: SourceRegistry
    http: GuardedHttpClient
    transfers: TransferManager
    processor: MediaProcessor
    output: LocalOutputStore
    jobs: SqliteJobStore
    asset_policy: AssetPolicy
    admission: AdmissionPolicy
    selection_policy: SelectionPolicy
    processing_policy: ProcessingPolicy
    breaker: CircuitBreaker
    temp_root: Path
    enabled_protocols: frozenset[str] = frozenset({'https', 'file'})

    async def aclose(self) -> None:
        await self.http.aclose()
        self.jobs.close()

    async def __aenter__(self) -> AppContext:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


def selection_policy_for(settings: Settings) -> SelectionPolicy:
    name = settings.output.policy
    if name == 'broadly_compatible':
        return SelectionPolicy(
            name=name,
            max_height=None,  # a filter here would drop the only rendition
            max_bytes=settings.limits.max_asset_bytes,
        )
    if name == 'storage_efficient':
        return SelectionPolicy(
            name=name,
            min_short_side=settings.policies.storage_efficient.min_short_side,
            max_bytes=settings.limits.max_asset_bytes,
        )
    return SelectionPolicy(name='source_preserving', max_bytes=settings.limits.max_asset_bytes)


def build(
    settings: Settings | None = None,
    *,
    url_policy: UrlPolicy | None = None,
    registry: SourceRegistry | None = None,
    output_root: Path | None = None,
) -> AppContext:
    settings = settings or Settings()
    root = output_root or settings.output.resolved_root()
    root.mkdir(parents=True, exist_ok=True)

    http = GuardedHttpClient(
        settings.network,
        policy=url_policy or UrlPolicy.from_config(settings.network),
    )
    limits = TransferLimits(
        max_bytes=settings.limits.max_asset_bytes,
        chunk_size=settings.network.chunk_size,
        max_playlist_bytes=settings.limits.max_playlist_bytes,
        segment_concurrency=settings.concurrency.segments,
    )
    rate_limiter = RateLimiterRegistry(default_rate=float(settings.network.per_host_concurrency))
    rate_limiter.configure(
        'connector:instagram_graph',
        settings.instagram_graph.requests_per_second_per_connection,
    )
    tools = FfmpegTools.try_discover(settings.ffmpeg.path or None) if settings.ffmpeg.enabled else None
    engines = default_engines(
        HttpTransfer(http),
        LocalFileTransfer(tuple(Path(p).expanduser() for p in settings.sources.import_roots)),
    )
    if tools is not None:
        # Segmented formats need FFmpeg to assemble; without it they stay
        # unregistered and the selection engine excludes them by protocol.
        engines[HlsLocator] = HlsTransfer(http, tools, concurrency=settings.concurrency.segments)
        engines[DashLocator] = DashTransfer(http, tools, concurrency=settings.concurrency.segments)
    transfers = TransferManager(
        engines,
        limits=limits,
        retry_budget=RetryBudget(
            max_attempts=settings.retry.max_attempts,
            base_s=settings.retry.base_s,
            cap_s=settings.retry.cap_s,
            park_threshold_s=settings.retry.park_threshold_s,
        ),
        rate_limiter=rate_limiter,
        host_concurrency=settings.network.per_host_concurrency,
    )

    asset_policy = AssetPolicy(limits=settings.limits)
    if tools is None:
        log.warning(
            'ffmpeg_unavailable',
            impact='merge, remux, transcode and probing are disabled',
        )

    if registry is None:
        registry = SourceRegistry.from_entry_points(
            settings.sources.enabled,
            user_files={'import_roots': tuple(settings.sources.import_roots)},
        )

    return AppContext(
        settings=settings,
        registry=registry,
        http=http,
        transfers=transfers,
        processor=MediaProcessor(
            tools, asset_policy,
            timeout_factor=settings.limits.ffmpeg_timeout_factor,
            timeout_floor_s=settings.limits.ffmpeg_timeout_floor_s,
        ),
        output=LocalOutputStore(root, collision=settings.output.collision),
        jobs=SqliteJobStore(settings.jobs_database()),
        asset_policy=asset_policy,
        admission=AdmissionPolicy(
            enabled_connectors=frozenset(settings.sources.enabled),
            active_jobs_per_user=settings.concurrency.active_jobs_per_user,
        ),
        selection_policy=selection_policy_for(settings),
        processing_policy=ProcessingPolicy(
            name=settings.output.policy,
            keep_source=settings.output.keep_source,
            config=settings.policies,
            embed_metadata=settings.output.embed_metadata,
        ),
        breaker=CircuitBreaker(),
        temp_root=root / '.mediadl-tmp',
        enabled_protocols=frozenset(
            {'https', 'file'} | ({'hls', 'dash'} if tools is not None else set())
        ),
    )
