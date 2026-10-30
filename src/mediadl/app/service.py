"""Use-case facade over the orchestrator (spec section 11)."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from mediadl.app.context import AppContext
from mediadl.app.orchestrator import JobOutcome, Orchestrator
from mediadl.errors import ConcurrencyConflictError
from mediadl.jobs.states import JobState
from mediadl.jobs.store_sqlite import Job, idempotency_key, new_job_id
from mediadl.sources.base import SourceRequest
from mediadl.transfer.base import CancelToken, ProgressReporter


@dataclass(slots=True)
class MediaDLService:
    ctx: AppContext
    principal_id: str = 'local'
    credentials: object | None = None

    async def submit(
        self,
        raw: str,
        *,
        connection_id: str | None = None,
        options: Mapping[str, object] | None = None,
    ) -> Job:
        normalized_options = dict(options or {})
        request = SourceRequest(raw=raw, connection_id=connection_id, options=normalized_options)
        connector = self.ctx.registry.select(request)
        policy = self.ctx.settings.output.policy
        job = Job(
            id=new_job_id(),
            principal_id=self.principal_id,
            idempotency_key=idempotency_key(
                self.principal_id, connector.connector_id, raw, policy, options=normalized_options
            ),
            request_json=json.dumps({'raw': raw, 'options': normalized_options}, sort_keys=True),
            policy_name=policy,
            connector_id=connector.connector_id,
            connection_id=connection_id,
            output_root=str(self.ctx.output.root),
        )
        return await self.ctx.jobs.create(job)

    async def run(
        self,
        job: Job,
        *,
        progress: ProgressReporter | None = None,
        cancel: CancelToken | None = None,
        worker_id: str = 'cli',
    ) -> JobOutcome:
        leased = await self.ctx.jobs.lease(job.id, worker_id, self.ctx.settings.jobs.lease_seconds)
        if leased is None:
            raise ConcurrencyConflictError(
                'this job is already being worked on', job_id=job.id
            )
        orchestrator = Orchestrator(self.ctx, credentials=self.credentials)
        return await orchestrator.run_job(
            job.id, worker_id, cancel=cancel, progress=progress
        )

    async def get(self, raw: str, **kwargs) -> JobOutcome:
        """Submit and run in one step (the CLI path)."""
        progress = kwargs.pop('progress', None)
        job = await self.submit(raw, **kwargs)
        if job.state in (JobState.COMPLETED, JobState.PARTIAL_SUCCESS):
            # Idempotent resubmission of a finished job: report it as-is.
            items = await self.ctx.jobs.items(job.id)
            from mediadl.domain.records import ItemResult

            results = [
                ItemResult(
                    position=i.position, asset_id=i.asset_id,
                    status='completed' if str(i.state) == 'DONE' else 'failed',
                    error_code=i.error_code,
                )
                for i in items
            ]
            return JobOutcome(job.id, job.state, results)
        return await self.run(job, progress=progress)

    async def retry_failed_items(self, job_id: str) -> JobOutcome:
        await self.ctx.jobs.transition(job_id, JobState.QUEUED, actor='user')
        job = await self.ctx.jobs.get(job_id)
        return await self.run(job)

    def output_root(self) -> Path:
        return self.ctx.output.root
