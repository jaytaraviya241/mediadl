"""Transfer manager (spec sections 4.3, 12.6).

Picks the engine by *locator type*, never by platform. On expiry it asks the
connector to re-resolve through its documented API and retries exactly once;
resume across a re-signed URL survives only if the validators still match.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from mediadl.domain.models import (
    Asset,
    DashLocator,
    HlsLocator,
    HttpLocator,
    LocalFileLocator,
    Representation,
    SourceRef,
)
from mediadl.domain.records import SelectedRendition
from mediadl.errors import ConnectorContractError, LocatorExpiredError, UnsupportedSourceError
from mediadl.obs.logging import get_logger
from mediadl.sources.base import AccessContext, SourceConnector
from mediadl.transfer.base import (
    CancelToken,
    ProgressReporter,
    TransferEngine,
    TransferLimits,
    TransferPlan,
    TransferResult,
)
from mediadl.transfer.ratelimit import RateLimiterRegistry
from mediadl.transfer.retry import RetryBudget, transfer_error_class, with_retries

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TransferredFiles:
    primary: TransferResult
    audio: TransferResult | None = None

    @property
    def all(self) -> tuple[TransferResult, ...]:
        return (self.primary, self.audio) if self.audio else (self.primary,)


class TransferManager:
    def __init__(
        self,
        engines: dict[type, TransferEngine],
        *,
        limits: TransferLimits,
        retry_budget: RetryBudget | None = None,
        rate_limiter: RateLimiterRegistry | None = None,
        host_concurrency: int = 4,
    ) -> None:
        self.engines = engines
        self.limits = limits
        self.retry_budget = retry_budget or RetryBudget()
        self.rate_limiter = rate_limiter or RateLimiterRegistry()
        self._host_semaphores: dict[str, asyncio.Semaphore] = {}
        self.host_concurrency = host_concurrency

    def engine_for(self, rep: Representation) -> TransferEngine:
        engine = self.engines.get(type(rep.locator))
        if engine is None:
            raise UnsupportedSourceError(
                f'no transfer engine for {type(rep.locator).__name__}',
                locator=type(rep.locator).__name__,
            )
        return engine

    def _semaphore(self, host: str) -> asyncio.Semaphore:
        if host not in self._host_semaphores:
            self._host_semaphores[host] = asyncio.Semaphore(self.host_concurrency)
        return self._host_semaphores[host]

    async def fetch(
        self,
        rendition: SelectedRendition,
        asset: Asset,
        connector: SourceConnector,
        ctx: AccessContext,
        *,
        temp_dir: Path,
        progress: ProgressReporter,
        cancel: CancelToken,
    ) -> TransferredFiles:
        primary = await self._fetch_one(
            rendition.primary, asset, connector, ctx,
            temp_dir=temp_dir, progress=progress, cancel=cancel, suffix='primary',
        )
        audio = None
        if rendition.paired_audio is not None:
            audio = await self._fetch_one(
                rendition.paired_audio, asset, connector, ctx,
                temp_dir=temp_dir, progress=progress, cancel=cancel, suffix='audio',
            )
        return TransferredFiles(primary=primary, audio=audio)

    async def _fetch_one(
        self,
        rep: Representation,
        asset: Asset,
        connector: SourceConnector,
        ctx: AccessContext,
        *,
        temp_dir: Path,
        progress: ProgressReporter,
        cancel: CancelToken,
        suffix: str,
    ) -> TransferResult:
        allowed_hosts = connector.capabilities.allowed_media_hosts or None
        plan = TransferPlan(
            rep=rep,
            dest=temp_dir / f'{_safe(asset.asset_id)}.{suffix}.{_ext_hint(rep)}',
            limits=self.limits,
            allowed_hosts=allowed_hosts,
            expected_kind=rep.kind,
        )
        host = _host_of(rep)
        attempts = 0

        for refresh_attempt in range(2):
            engine = self.engine_for(plan.rep)

            # Bound as defaults: `plan` is replaced on a refresh, and the
            # closure must use the values from this iteration.
            async def run(
                engine: TransferEngine = engine, plan: TransferPlan = plan
            ) -> TransferResult:
                nonlocal attempts
                attempts += 1
                cancel.raise_if_cancelled()
                if host:
                    await self.rate_limiter.acquire(f'host:{host}')
                async with self._semaphore(host or 'local'):
                    return await engine.transfer(plan, progress, cancel)

            try:
                result = await with_retries(
                    run, classify=transfer_error_class,
                    budget=self.retry_budget, label=f'transfer:{asset.asset_id}:{suffix}',
                )
                return TransferResult(
                    path=result.path, bytes_written=result.bytes_written, sha256=result.sha256,
                    validators=result.validators, resumed=result.resumed, attempts=attempts,
                    url_fingerprint=result.url_fingerprint, content_type=result.content_type,
                    rep_id=result.rep_id,
                )
            except LocatorExpiredError:
                if refresh_attempt or not connector.capabilities.supports_refresh:
                    raise
                log.info('locator_expired_refreshing', asset_id=asset.asset_id, rep_id=rep.rep_id)
                fresh = await connector.refresh(_ref_for(asset, rep), ctx)
                replacement = _matching_rep(fresh, rep)
                if replacement is None:
                    raise ConnectorContractError(
                        'refresh did not return the requested representation',
                        asset_id=asset.asset_id, rep_id=rep.rep_id,
                    ) from None
                plan = plan.with_locator(replacement.locator)
        raise LocatorExpiredError('locator still expired after refresh', asset_id=asset.asset_id)


def _ref_for(asset: Asset, rep: Representation) -> SourceRef:
    return asset.ref


def _matching_rep(fresh: Asset, rep: Representation) -> Representation | None:
    for candidate in fresh.representations:
        if candidate.rep_id == rep.rep_id:
            return candidate
    for candidate in fresh.representations:
        if candidate.role is rep.role and candidate.kind is rep.kind:
            return candidate
    return None


def _host_of(rep: Representation) -> str:
    import urllib.parse

    locator = rep.locator
    url = None
    if isinstance(locator, HttpLocator):
        url = locator.url
    elif isinstance(locator, HlsLocator):
        url = locator.playlist_url
    elif isinstance(locator, DashLocator):
        url = locator.mpd_url
    if not url:
        return ''
    return (urllib.parse.urlsplit(url).hostname or '').lower()


def _ext_hint(rep: Representation) -> str:
    declared = rep.declared.container
    if declared and declared.isalnum():
        return declared.lower()
    return {'image': 'img', 'video': 'vid', 'audio': 'aud'}[str(rep.kind)]


def _safe(value: str) -> str:
    return ''.join(c if c.isalnum() or c in '-_' else '_' for c in value)[:80]


def default_engines(
    http_engine: TransferEngine, local_engine: TransferEngine
) -> dict[type, TransferEngine]:
    return {HttpLocator: http_engine, LocalFileLocator: local_engine}
