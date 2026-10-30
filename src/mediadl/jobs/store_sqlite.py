"""SQLite job store (spec section 9.1).

Optimistic concurrency on a ``version`` column, leases with heartbeats, and
an append-only event log. Every transition names its allowed predecessors so
two workers cannot both advance the same job.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from mediadl.errors import ConcurrencyConflictError, InvalidTransitionError, JobNotFoundError
from mediadl.jobs.states import ALLOWED, ItemState, JobState

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    connector_id TEXT,
    connection_id TEXT,
    request_json TEXT NOT NULL,
    policy_name TEXT NOT NULL,
    policy_version INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 0,
    resume_at REAL,
    lease_owner TEXT,
    lease_expires_at REAL,
    output_root TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error_code TEXT,
    error_detail_json TEXT
);
CREATE TABLE IF NOT EXISTS job_items (
    job_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    rep_id TEXT,
    bytes_total INTEGER,
    bytes_done INTEGER NOT NULL DEFAULT 0,
    output_record_json TEXT,
    error_code TEXT,
    error_message TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (job_id, position),
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    at TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    actor TEXT,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, resume_at);
CREATE INDEX IF NOT EXISTS idx_jobs_principal ON jobs(principal_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_job ON job_events(job_id, id);
"""


@dataclass(slots=True)
class Job:
    id: str
    principal_id: str
    idempotency_key: str
    request_json: str
    policy_name: str
    state: JobState = JobState.QUEUED
    version: int = 0
    attempt: int = 0
    connector_id: str | None = None
    connection_id: str | None = None
    policy_version: int = 1
    resume_at: float | None = None
    lease_owner: str | None = None
    lease_expires_at: float | None = None
    output_root: str | None = None
    created_at: str = ''
    updated_at: str = ''
    error_code: str | None = None
    error_detail: Mapping[str, Any] | None = None

    @property
    def request(self) -> dict[str, Any]:
        return json.loads(self.request_json)


@dataclass(slots=True)
class JobItem:
    job_id: str
    position: int
    asset_id: str
    kind: str
    state: ItemState = ItemState.PENDING
    rep_id: str | None = None
    bytes_total: int | None = None
    bytes_done: int = 0
    output_record: Mapping[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    attempts: int = 0
    updated_at: str = ''


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SqliteJobStore:
    """Async-friendly wrapper; SQLite calls run in a worker thread."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._conn.execute('PRAGMA foreign_keys=ON')
        self._conn.execute('PRAGMA busy_timeout=5000')
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # -- sync core (runs in a thread) -------------------------------------
    def _create(self, job: Job) -> Job:
        existing = self._conn.execute(
            'SELECT * FROM jobs WHERE idempotency_key = ?', (job.idempotency_key,)
        ).fetchone()
        if existing is not None:
            return _row_to_job(existing)
        job.created_at = job.updated_at = _now()
        self._conn.execute(
            """INSERT INTO jobs (id, principal_id, idempotency_key, connector_id, connection_id,
                                 request_json, policy_name, policy_version, state, version, attempt,
                                 output_root, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job.id, job.principal_id, job.idempotency_key, job.connector_id, job.connection_id,
             job.request_json, job.policy_name, job.policy_version, str(job.state), 0, 0,
             job.output_root, job.created_at, job.updated_at),
        )
        self._append_event(job.id, None, job.state, 'system', {'event': 'created'})
        return job

    def _append_event(
        self, job_id: str, from_state: JobState | None, to_state: JobState | None,
        actor: str, detail: Mapping[str, Any] | None,
    ) -> None:
        self._conn.execute(
            'INSERT INTO job_events (job_id, at, from_state, to_state, actor, detail_json) '
            'VALUES (?,?,?,?,?,?)',
            (job_id, _now(), str(from_state) if from_state else None,
             str(to_state) if to_state else None, actor, json.dumps(detail or {})),
        )

    def _transition(
        self, job_id: str, target: JobState, *, expected_version: int | None,
        detail: Mapping[str, Any] | None, actor: str, resume_at: float | None,
    ) -> Job:
        row = self._conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError('job not found', job_id=job_id)
        current = _row_to_job(row)
        if target not in ALLOWED.get(current.state, frozenset()):
            raise InvalidTransitionError(
                f'{current.state} -> {target} is not allowed',
                job_id=job_id, from_state=str(current.state), to_state=str(target),
            )
        version = current.version if expected_version is None else expected_version
        error_code = detail.get('code') if detail else None
        cursor = self._conn.execute(
            """UPDATE jobs SET state=?, version=version+1, updated_at=?, resume_at=?,
                               error_code=?, error_detail_json=?
               WHERE id=? AND version=? AND state=?""",
            (str(target), _now(), resume_at, error_code,
             json.dumps(detail or {}), job_id, version, str(current.state)),
        )
        if cursor.rowcount != 1:
            raise ConcurrencyConflictError(
                'job was modified concurrently', job_id=job_id, expected_version=version
            )
        self._append_event(job_id, current.state, target, actor, detail)
        return self._get(job_id)

    def _get(self, job_id: str) -> Job:
        row = self._conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError('job not found', job_id=job_id)
        return _row_to_job(row)

    def _lease_next(self, worker_id: str, lease_s: int) -> Job | None:
        now = time.time()
        # Reclaim expired leases first: a crashed worker must not strand a job.
        self._conn.execute(
            """UPDATE jobs SET state=?, lease_owner=NULL, lease_expires_at=NULL,
                               attempt=attempt+1, version=version+1, updated_at=?
               WHERE lease_expires_at IS NOT NULL AND lease_expires_at < ?
                 AND state IN (?,?,?,?)""",
            (str(JobState.QUEUED), _now(), now, str(JobState.VALIDATING),
             str(JobState.RESOLVING), str(JobState.DOWNLOADING), str(JobState.PROCESSING)),
        )
        row = self._conn.execute(
            # A job stays QUEUED until its worker advances it, so an existing
            # live lease must exclude it or two workers would both take it.
            """SELECT * FROM jobs WHERE state = ?
               AND (resume_at IS NULL OR resume_at <= ?)
               AND (lease_expires_at IS NULL OR lease_expires_at < ?)
               ORDER BY created_at LIMIT 1""",
            (str(JobState.QUEUED), now, now),
        ).fetchone()
        if row is None:
            return None
        job = _row_to_job(row)
        cursor = self._conn.execute(
            """UPDATE jobs SET lease_owner=?, lease_expires_at=?, version=version+1, updated_at=?
               WHERE id=? AND version=? AND state=?""",
            (worker_id, now + lease_s, _now(), job.id, job.version, str(JobState.QUEUED)),
        )
        if cursor.rowcount != 1:
            return None  # another worker won the race
        return self._get(job.id)

    def _record_items(self, job_id: str, items: Sequence[JobItem]) -> None:
        for item in items:
            self._conn.execute(
                """INSERT INTO job_items (job_id, position, asset_id, kind, state, updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(job_id, position) DO UPDATE SET
                       asset_id=excluded.asset_id, kind=excluded.kind, updated_at=excluded.updated_at""",
                (job_id, item.position, item.asset_id, item.kind, str(item.state), _now()),
            )

    def _update_item(
        self, job_id: str, position: int, *, state: ItemState,
        record: Mapping[str, Any] | None, error_code: str | None,
        error_message: str | None, bump_attempt: bool,
    ) -> None:
        self._conn.execute(
            """UPDATE job_items
               SET state=?, output_record_json=COALESCE(?, output_record_json),
                   error_code=?, error_message=?,
                   attempts=attempts + ?, updated_at=?
               WHERE job_id=? AND position=?""",
            (str(state), json.dumps(record) if record else None, error_code, error_message,
             1 if bump_attempt else 0, _now(), job_id, position),
        )

    def _items(self, job_id: str) -> list[JobItem]:
        rows = self._conn.execute(
            'SELECT * FROM job_items WHERE job_id = ? ORDER BY position', (job_id,)
        ).fetchall()
        return [_row_to_item(r) for r in rows]

    def _list(self, principal_id: str | None, limit: int) -> list[Job]:
        if principal_id:
            rows = self._conn.execute(
                'SELECT * FROM jobs WHERE principal_id = ? ORDER BY created_at DESC LIMIT ?',
                (principal_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                'SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?', (limit,)
            ).fetchall()
        return [_row_to_job(r) for r in rows]

    # -- async API ---------------------------------------------------------
    async def _run(self, fn, *args):  # type: ignore[no-untyped-def]
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    async def create(self, job: Job) -> Job:
        return await self._run(self._create, job)

    async def get(self, job_id: str) -> Job:
        return await self._run(self._get, job_id)

    async def transition(
        self, job_id: str, target: JobState, *, expected_version: int | None = None,
        detail: Mapping[str, Any] | None = None, actor: str = 'worker',
        resume_at: float | None = None,
    ) -> Job:
        return await self._run(
            partial(
                self._transition, job_id, target, expected_version=expected_version,
                detail=detail, actor=actor, resume_at=resume_at,
            )
        )

    async def lease_next(self, worker_id: str, lease_s: int = 60) -> Job | None:
        return await self._run(self._lease_next, worker_id, lease_s)

    async def lease(self, job_id: str, worker_id: str, lease_s: int = 60) -> Job | None:
        """Lease one specific job, or None when someone else holds it."""

        def _lease() -> Job | None:
            now = time.time()
            cursor = self._conn.execute(
                """UPDATE jobs SET lease_owner=?, lease_expires_at=?, version=version+1, updated_at=?
                   WHERE id=? AND state=?
                     AND (lease_expires_at IS NULL OR lease_expires_at < ?)""",
                (worker_id, now + lease_s, _now(), job_id, str(JobState.QUEUED), now),
            )
            return self._get(job_id) if cursor.rowcount == 1 else None

        return await self._run(_lease)

    async def heartbeat(self, job_id: str, worker_id: str, lease_s: int = 60) -> None:
        def _beat() -> None:
            self._conn.execute(
                'UPDATE jobs SET lease_expires_at=? WHERE id=? AND lease_owner=?',
                (time.time() + lease_s, job_id, worker_id),
            )

        await self._run(_beat)

    async def release_lease(self, job_id: str, worker_id: str) -> None:
        def _release() -> None:
            self._conn.execute(
                'UPDATE jobs SET lease_owner=NULL, lease_expires_at=NULL '
                'WHERE id=? AND lease_owner=?',
                (job_id, worker_id),
            )

        await self._run(_release)

    async def record_items(self, job_id: str, items: Sequence[JobItem]) -> None:
        await self._run(self._record_items, job_id, items)

    async def item_state(
        self, job_id: str, position: int, state: ItemState, *,
        record: Mapping[str, Any] | None = None, error_code: str | None = None,
        error_message: str | None = None, bump_attempt: bool = False,
    ) -> None:
        await self._run(
            partial(
                self._update_item, job_id, position, state=state, record=record,
                error_code=error_code, error_message=error_message, bump_attempt=bump_attempt,
            )
        )

    async def items(self, job_id: str) -> list[JobItem]:
        return await self._run(self._items, job_id)

    async def list_jobs(self, principal_id: str | None = None, limit: int = 20) -> list[Job]:
        return await self._run(self._list, principal_id, limit)

    async def events(self, job_id: str) -> list[dict[str, Any]]:
        def _events() -> list[dict[str, Any]]:
            rows = self._conn.execute(
                'SELECT * FROM job_events WHERE job_id = ? ORDER BY id', (job_id,)
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._run(_events)

    async def active_job_count(self, principal_id: str) -> int:
        def _count() -> int:
            row = self._conn.execute(
                """SELECT COUNT(*) AS n FROM jobs WHERE principal_id = ?
                   AND state IN (?,?,?,?,?)""",
                (principal_id, str(JobState.QUEUED), str(JobState.VALIDATING),
                 str(JobState.RESOLVING), str(JobState.DOWNLOADING), str(JobState.PROCESSING)),
            ).fetchone()
            return int(row['n'])

        return await self._run(_count)


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row['id'], principal_id=row['principal_id'], idempotency_key=row['idempotency_key'],
        request_json=row['request_json'], policy_name=row['policy_name'],
        state=JobState(row['state']), version=row['version'], attempt=row['attempt'],
        connector_id=row['connector_id'], connection_id=row['connection_id'],
        policy_version=row['policy_version'], resume_at=row['resume_at'],
        lease_owner=row['lease_owner'], lease_expires_at=row['lease_expires_at'],
        output_root=row['output_root'], created_at=row['created_at'], updated_at=row['updated_at'],
        error_code=row['error_code'],
        error_detail=json.loads(row['error_detail_json']) if row['error_detail_json'] else None,
    )


def _row_to_item(row: sqlite3.Row) -> JobItem:
    return JobItem(
        job_id=row['job_id'], position=row['position'], asset_id=row['asset_id'],
        kind=row['kind'], state=ItemState(row['state']), rep_id=row['rep_id'],
        bytes_total=row['bytes_total'], bytes_done=row['bytes_done'],
        output_record=json.loads(row['output_record_json']) if row['output_record_json'] else None,
        error_code=row['error_code'], error_message=row['error_message'],
        attempts=row['attempts'], updated_at=row['updated_at'],
    )


def new_job_id() -> str:
    return uuid.uuid4().hex


def idempotency_key(
    principal_id: str, connector_id: str, canonical_request: str,
    policy_name: str, policy_version: int = 1,
    *,
    options: Mapping[str, object] | None = None,
) -> str:
    import hashlib

    payload = '|'.join([principal_id, connector_id, canonical_request, policy_name, str(policy_version)])
    if options:
        # Existing jobs without an explicit selection retain their historical
        # idempotency key.  A different selected rendition must be a distinct
        # job, however, so a user can download the same post at two qualities.
        payload += '|' + json.dumps(dict(options), sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()
