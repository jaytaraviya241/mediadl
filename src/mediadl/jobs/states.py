"""Job state machine (spec section 9.1)."""
from __future__ import annotations

import enum


class JobState(enum.StrEnum):
    QUEUED = 'QUEUED'
    VALIDATING = 'VALIDATING'
    RESOLVING = 'RESOLVING'
    DOWNLOADING = 'DOWNLOADING'
    PROCESSING = 'PROCESSING'
    COMPLETED = 'COMPLETED'
    PARTIAL_SUCCESS = 'PARTIAL_SUCCESS'
    FAILED = 'FAILED'
    CANCELLED = 'CANCELLED'
    PAUSED = 'PAUSED'
    AUTHORIZATION_REQUIRED = 'AUTHORIZATION_REQUIRED'
    RATE_LIMITED = 'RATE_LIMITED'


class ItemState(enum.StrEnum):
    PENDING = 'PENDING'
    RESOLVED = 'RESOLVED'
    TRANSFERRING = 'TRANSFERRING'
    PROCESSING = 'PROCESSING'
    DONE = 'DONE'
    FAILED = 'FAILED'
    UNAVAILABLE = 'UNAVAILABLE'
    SKIPPED = 'SKIPPED'


TERMINAL_STATES = frozenset({JobState.COMPLETED, JobState.CANCELLED})

#: Allowed transitions. Anything not listed is an InvalidTransitionError.
ALLOWED: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.VALIDATING, JobState.CANCELLED}),
    JobState.VALIDATING: frozenset({JobState.RESOLVING, JobState.FAILED, JobState.CANCELLED}),
    JobState.RESOLVING: frozenset({
        JobState.DOWNLOADING, JobState.AUTHORIZATION_REQUIRED, JobState.RATE_LIMITED,
        JobState.FAILED, JobState.CANCELLED,
    }),
    JobState.DOWNLOADING: frozenset({
        JobState.PROCESSING, JobState.RATE_LIMITED, JobState.AUTHORIZATION_REQUIRED,
        JobState.PAUSED, JobState.FAILED, JobState.CANCELLED,
    }),
    JobState.PROCESSING: frozenset({
        JobState.COMPLETED, JobState.PARTIAL_SUCCESS, JobState.FAILED, JobState.CANCELLED,
    }),
    JobState.AUTHORIZATION_REQUIRED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.RATE_LIMITED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.PAUSED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.PARTIAL_SUCCESS: frozenset({JobState.QUEUED}),
    JobState.FAILED: frozenset({JobState.QUEUED}),
    JobState.COMPLETED: frozenset(),
    JobState.CANCELLED: frozenset(),
}


def can_transition(source: JobState, target: JobState) -> bool:
    return target in ALLOWED.get(source, frozenset())


#: States a worker may lease a job from.
LEASABLE = frozenset({JobState.QUEUED})
