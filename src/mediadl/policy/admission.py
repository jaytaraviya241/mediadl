"""Request admission (spec section 5.3).

The first chokepoint: is this principal allowed to use this connector, and
are they within quota? Denials are never retryable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from mediadl.errors import PolicyDeniedError, QuotaExceededError
from mediadl.sources.base import SourceConnector, SourceRequest


@dataclass(slots=True)
class Quota:
    max_jobs_per_day: int | None = None
    max_bytes_per_day: int | None = None
    jobs_used: int = 0
    bytes_used: int = 0


@dataclass(slots=True)
class AdmissionPolicy:
    enabled_connectors: frozenset[str]
    quotas: dict[str, Quota] = field(default_factory=dict)
    active_jobs_per_user: int = 3

    def admit(
        self,
        principal_id: str,
        request: SourceRequest,
        connector: SourceConnector,
        *,
        active_jobs: int = 0,
    ) -> None:
        if connector.connector_id not in self.enabled_connectors:
            raise PolicyDeniedError(
                f'connector {connector.connector_id!r} is not enabled',
                connector=connector.connector_id,
            )
        if active_jobs >= self.active_jobs_per_user:
            raise QuotaExceededError(
                'too many jobs running for this user',
                active=active_jobs, limit=self.active_jobs_per_user,
            )
        quota = self.quotas.get(principal_id)
        if quota is None:
            return
        if quota.max_jobs_per_day is not None and quota.jobs_used >= quota.max_jobs_per_day:
            raise QuotaExceededError('daily job quota reached', limit=quota.max_jobs_per_day)
        if quota.max_bytes_per_day is not None and quota.bytes_used >= quota.max_bytes_per_day:
            raise QuotaExceededError('daily byte quota reached', limit=quota.max_bytes_per_day)
