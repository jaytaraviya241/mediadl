"""Main workflow: resolve -> select -> transfer -> process -> finalize.

Spec sections 12.1 and 12.4. Two rules shape the control flow:

  * a per-asset failure is a *value*, so one bad item in a carousel cannot
    take down the rest and positions never get renumbered;
  * a job-level condition (authorization, rate limiting, cancellation) stops
    the whole job and is reported once.
"""
from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from mediadl.app.context import AppContext
from mediadl.domain.invariants import as_collection, validate
from mediadl.domain.models import Asset, Availability, Collection
from mediadl.domain.records import ItemResult, OutputRecord, SelectedRendition
from mediadl.errors import (
    AuthorizationNeeded,
    CancelledByUser,
    ContentUnavailableError,
    DrmError,
    InternalError,
    MediaDLError,
    RateLimited,
)
from mediadl.jobs.states import ItemState, JobState
from mediadl.jobs.store_sqlite import Job, JobItem
from mediadl.obs import metrics
from mediadl.obs.logging import get_logger
from mediadl.output import naming, sidecar
from mediadl.selection import choose
from mediadl.sources.base import AccessContext, SourceConnector, SourceRequest
from mediadl.transfer.base import CancelToken, ProgressReporter
from mediadl.transfer.manager import TransferredFiles
from mediadl.transfer.retry import connector_error_class, with_retries

log = get_logger(__name__)


@dataclass(slots=True)
class JobOutcome:
    job_id: str
    state: JobState
    results: list[ItemResult]
    manifest_path: Path | None = None
    counts: dict[str, int] | None = None

    @property
    def completed(self) -> list[ItemResult]:
        return [r for r in self.results if r.ok]


@dataclass(slots=True)
class JobRunContext:
    job: Job
    cancel: CancelToken
    progress: ProgressReporter
    temp_dir: Path
    deadline: float


class Orchestrator:
    def __init__(self, ctx: AppContext, *, credentials=None) -> None:
        self.ctx = ctx
        self.credentials = credentials

    # -- entry point -------------------------------------------------------
    async def run_job(
        self,
        job_id: str,
        worker_id: str = 'local',
        *,
        cancel: CancelToken | None = None,
        progress: ProgressReporter | None = None,
    ) -> JobOutcome:
        store = self.ctx.jobs
        job = await store.get(job_id)
        run = JobRunContext(
            job=job,
            cancel=cancel or CancelToken(job_id=job_id),
            progress=progress or ProgressReporter(),
            temp_dir=self.ctx.temp_root / job_id,
            deadline=time.monotonic() + self.ctx.settings.limits.job_timeout_s,
        )
        run.temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        results: list[ItemResult] = []

        try:
            await store.transition(job_id, JobState.VALIDATING)
            request = SourceRequest(
                raw=job.request['raw'],
                connection_id=job.connection_id,
                options=job.request.get('options', {}),
            )
            connector = self.ctx.registry.select(request)
            active = await store.active_job_count(job.principal_id)
            self.ctx.admission.admit(
                job.principal_id, request, connector, active_jobs=max(0, active - 1)
            )

            await store.transition(job_id, JobState.RESOLVING)
            access = await self._access_context(job, connector, request)
            item = await with_retries(
                lambda: connector.resolve(request, access),
                classify=connector_error_class,
                budget=self.ctx.transfers.retry_budget,
                label=f'resolve:{connector.connector_id}',
            )
            collection = as_collection(validate(item))
            self.ctx.asset_policy.check_collection_size(len(collection.assets))
            await store.record_items(
                job_id,
                [
                    JobItem(job_id, a.position, a.asset_id, str(a.kind))
                    for a in collection.assets
                ],
            )

            await store.transition(job_id, JobState.DOWNLOADING)
            results = await self.process_collection(collection, connector, access, run)

            await store.transition(job_id, JobState.PROCESSING)
            manifest_path, counts, final = await self._finish(collection, results, run)
            metrics.increment(
                'mediadl_jobs_total',
                connector=connector.connector_id, final_state=str(final),
            )
            for status, count in counts.items():
                if count:
                    metrics.increment('mediadl_collection_items_total', count, status=status)
            await store.transition(
                job_id, final,
                detail={'manifest': str(manifest_path) if manifest_path else None, 'counts': counts},
            )
            return JobOutcome(job_id, final, results, manifest_path, counts)

        except AuthorizationNeeded as exc:
            await store.transition(job_id, JobState.AUTHORIZATION_REQUIRED, detail=exc.public())
            return JobOutcome(job_id, JobState.AUTHORIZATION_REQUIRED, results)
        except RateLimited as exc:
            await store.transition(
                job_id, JobState.RATE_LIMITED, detail=exc.public(), resume_at=exc.resume_at
            )
            return JobOutcome(job_id, JobState.RATE_LIMITED, results)
        except CancelledByUser:
            await self._cleanup_temp(run)
            await store.transition(job_id, JobState.CANCELLED)
            return JobOutcome(job_id, JobState.CANCELLED, results)
        except MediaDLError as exc:
            log.warning('job_failed', job_id=job_id, code=str(exc.code))
            await store.transition(job_id, JobState.FAILED, detail=exc.public())
            return JobOutcome(job_id, JobState.FAILED, results)
        except Exception as exc:
            log.exception('internal_error', job_id=job_id)
            await store.transition(
                job_id, JobState.FAILED, detail=InternalError.from_exc(exc).public()
            )
            return JobOutcome(job_id, JobState.FAILED, results)
        finally:
            await store.release_lease(job_id, worker_id)

    # -- per-collection ----------------------------------------------------
    async def process_collection(
        self,
        collection: Collection,
        connector: SourceConnector,
        access: AccessContext,
        run: JobRunContext,
    ) -> list[ItemResult]:
        semaphore = asyncio.Semaphore(self.ctx.settings.concurrency.assets_per_job)

        async def one(asset: Asset) -> ItemResult:
            async with semaphore:
                return await self._process_asset(asset, collection, connector, access, run)

        tasks: list[asyncio.Task[ItemResult]] = []
        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(one(asset)) for asset in collection.assets]
        except* (AuthorizationNeeded, RateLimited, CancelledByUser) as eg:
            # TaskGroup wraps escapes in an ExceptionGroup; unwrap so the
            # job-level handlers in run_job match on the real type.
            raise eg.exceptions[0] from None
        return sorted((task.result() for task in tasks), key=lambda r: r.position)

    async def _process_asset(
        self,
        asset: Asset,
        collection: Collection,
        connector: SourceConnector,
        access: AccessContext,
        run: JobRunContext,
    ) -> ItemResult:
        store = self.ctx.jobs
        job_id = run.job.id
        run.cancel.raise_if_cancelled()
        asset_dir = run.temp_dir / f'{asset.position:03d}'
        asset_dir.mkdir(parents=True, exist_ok=True)

        try:
            if asset.availability is not Availability.AVAILABLE:
                await store.item_state(
                    job_id, asset.position, ItemState.UNAVAILABLE,
                    error_code=str(asset.availability),
                    error_message=asset.unavailable_reason,
                )
                return ItemResult(
                    position=asset.position, asset_id=asset.asset_id, status='unavailable',
                    error_code=_availability_code(asset.availability),
                    error_message=asset.unavailable_reason or str(asset.availability),
                )

            self.ctx.asset_policy.check_asset(asset)
            request_options = run.job.request.get('options', {})
            preferred_rep_id = (
                request_options.get('format_id')
                if isinstance(request_options, dict)
                and isinstance(request_options.get('format_id'), str)
                else None
            )
            rendition = choose(
                asset, self.ctx.selection_policy,
                enabled_protocols=self.ctx.enabled_protocols,
                preferred_rep_id=preferred_rep_id,
            )

            await store.item_state(job_id, asset.position, ItemState.TRANSFERRING, bump_attempt=True)
            files = await self.ctx.transfers.fetch(
                rendition, asset, connector, access,
                temp_dir=asset_dir, progress=run.progress, cancel=run.cancel,
            )

            await store.item_state(job_id, asset.position, ItemState.PROCESSING)
            processed = await self.ctx.processor.apply(
                rendition, files, self.ctx.processing_policy, asset.kind, work_dir=asset_dir,
            )

            record = await self._finalize(asset, collection, rendition, processed, files, run)
            metrics.increment(
                'mediadl_transfer_bytes_total', float(files.primary.bytes_written),
                engine='transfer',
            )
            await store.item_state(
                job_id, asset.position, ItemState.DONE,
                record={
                    'relative_path': record.relative_path,
                    'sha256': record.sha256,
                    'selected_rep_id': rendition.primary.rep_id,
                },
            )
            return ItemResult(
                position=asset.position, asset_id=asset.asset_id,
                status='completed', record=record,
            )

        except (AuthorizationNeeded, RateLimited, CancelledByUser):
            raise  # job-level: stop everything
        except (ContentUnavailableError, DrmError) as exc:
            await store.item_state(
                job_id, asset.position, ItemState.UNAVAILABLE,
                error_code=str(exc.code), error_message=exc.user_message,
            )
            return ItemResult(
                position=asset.position, asset_id=asset.asset_id, status='unavailable',
                error_code=str(exc.code), error_message=exc.user_message,
                detail=dict(exc.public()),
            )
        except MediaDLError as exc:
            log.warning(
                'item_failed', job_id=job_id, position=asset.position, code=str(exc.code)
            )
            await store.item_state(
                job_id, asset.position, ItemState.FAILED,
                error_code=str(exc.code), error_message=exc.user_message,
            )
            return ItemResult(
                position=asset.position, asset_id=asset.asset_id, status='failed',
                error_code=str(exc.code), error_message=exc.user_message,
                detail=dict(exc.public()),
            )
        except Exception as exc:
            log.exception('item_internal_error', job_id=job_id, position=asset.position)
            wrapped = InternalError.from_exc(exc)
            await store.item_state(
                job_id, asset.position, ItemState.FAILED,
                error_code=str(wrapped.code), error_message=wrapped.user_message,
            )
            return ItemResult(
                position=asset.position, asset_id=asset.asset_id, status='failed',
                error_code=str(wrapped.code), error_message=wrapped.user_message,
            )

    # -- finalization ------------------------------------------------------
    async def _finalize(
        self,
        asset: Asset,
        collection: Collection,
        rendition: SelectedRendition,
        processed,
        files: TransferredFiles,
        run: JobRunContext,
    ) -> OutputRecord:
        from mediadl.transfer.base import sha256_file

        ext = processed.extension
        context = naming.NamingContext.build(asset, collection, rendition, ext=ext)
        relative = naming.render(self.ctx.settings.output.template, context, kind=asset.kind)
        planned = self.ctx.output.plan(relative)
        digest = await sha256_file(processed.path)

        connector = self.ctx.registry.get(asset.ref.connector_id)
        record = await self.ctx.output.finalize(
            processed.path, planned,
            asset_id=asset.asset_id,
            position=asset.position,
            sha256=digest,
            mime_type=processed.mime_type,
            selected_rep_id=rendition.primary.rep_id,
            rendition_basis=connector.capabilities.rendition_basis,  # type: ignore[arg-type]
            probe=processed.probe,
            processing=tuple(processed.steps),
            retained_source=processed.retained_source,
        )

        if self.ctx.settings.output.sidecar:
            payload = sidecar.build_asset_sidecar(
                asset, collection, rendition, record,
                transfer_host=files.primary.url_fingerprint.split('/')[0],
                resumed=files.primary.resumed,
                attempts=files.primary.attempts,
            )
            if processed.notes:
                payload['app']['processing_notes'] = processed.notes
            sidecar.write_asset_sidecar(
                self.ctx.output.root / record.relative_path, payload
            )
        return record

    async def _finish(
        self, collection: Collection, results: list[ItemResult], run: JobRunContext
    ) -> tuple[Path | None, dict[str, int], JobState]:
        done = sum(1 for r in results if r.ok)
        if not results:
            final = JobState.FAILED
        elif done == len(results):
            final = JobState.COMPLETED
        elif done == 0:
            final = JobState.FAILED
        else:
            final = JobState.PARTIAL_SUCCESS

        counts = {
            status: sum(1 for r in results if r.status == status)
            for status in ('completed', 'failed', 'unavailable', 'skipped')
        }

        manifest_path: Path | None = None
        if len(collection.assets) > 1 or self.ctx.settings.output.zip:
            directory = self._collection_dir(results)
            if directory is not None:
                payload = sidecar.build_collection_manifest(
                    collection, results,
                    job_id=run.job.id,
                    policy_name=self.ctx.settings.output.policy,
                    final_state=str(final),
                )
                manifest_path = sidecar.write_collection_manifest(directory, payload)

        if self.ctx.settings.output.zip and results:
            from mediadl.output.zip import package

            files = [
                self.ctx.output.root / r.record.relative_path
                for r in results if r.record is not None
            ]
            if manifest_path is not None:
                files.append(manifest_path)
            if files:
                await package(
                    self.ctx.output.root, files,
                    self.ctx.output.root / f'{collection.ref.media_id}.zip',
                )

        await self._cleanup_temp(run)
        return manifest_path, counts, final

    def _collection_dir(self, results: list[ItemResult]) -> Path | None:
        for result in results:
            if result.record is not None:
                return (self.ctx.output.root / result.record.relative_path).parent
        return None

    async def _cleanup_temp(self, run: JobRunContext) -> None:
        await asyncio.to_thread(shutil.rmtree, run.temp_dir, True)
        # Opportunistic sweep: a worker that died earlier leaves scratch behind.
        from mediadl.storage.retention import sweep_temp

        await sweep_temp(self.ctx.temp_root, active_job_ids=frozenset({run.job.id}))

    # -- access ------------------------------------------------------------
    async def _access_context(
        self, job: Job, connector: SourceConnector, request: SourceRequest
    ) -> AccessContext:
        credential = None
        if 'none' not in connector.capabilities.auth_modes:
            if self.credentials is None:
                from mediadl.errors import AuthRequiredError

                raise AuthRequiredError(
                    f'{connector.connector_id} requires a connected account',
                    connector=connector.connector_id,
                )
            credential = await self.credentials.acquire(
                job.principal_id, job.connection_id, connector.required_scopes(request)
            )
        return AccessContext(
            principal_id=job.principal_id,
            connection_id=job.connection_id,
            credential=credential,
            http=self.ctx.http,
            deadline=time.monotonic() + self.ctx.settings.limits.job_timeout_s,
        )


def _availability_code(availability: Availability) -> str:
    return {
        Availability.UNAVAILABLE_COPYRIGHT: 'UNAVAILABLE_COPYRIGHT',
        Availability.UNAVAILABLE_DRM: 'UNAVAILABLE_DRM',
        Availability.NOT_FOUND: 'NOT_FOUND',
        Availability.NOT_ACCESSIBLE: 'NOT_ACCESSIBLE_WITH_AUTHORIZED_ACCOUNT',
        Availability.UNSUPPORTED_TYPE: 'UNSUPPORTED_TYPE',
    }.get(availability, 'UNAVAILABLE')
