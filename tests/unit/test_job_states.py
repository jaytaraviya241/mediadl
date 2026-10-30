"""Job state machine and store semantics (spec section 9.1, Phase 2 acceptance)."""
from __future__ import annotations

import json

import pytest

from mediadl.errors import ConcurrencyConflictError, InvalidTransitionError, JobNotFoundError
from mediadl.jobs.states import ALLOWED, JobState, can_transition
from mediadl.jobs.store_sqlite import (
    ItemState,
    Job,
    JobItem,
    SqliteJobStore,
    idempotency_key,
    new_job_id,
)


@pytest.fixture
def store(tmp_path):
    store = SqliteJobStore(tmp_path / 'jobs.sqlite3')
    try:
        yield store
    finally:
        store.close()


def make_job(**kwargs):
    defaults = dict(
        id=new_job_id(), principal_id='u1',
        idempotency_key=idempotency_key('u1', 'direct_url', 'https://x/a.jpg', 'source_preserving'),
        request_json=json.dumps({'raw': 'https://x/a.jpg'}),
        policy_name='source_preserving', connector_id='direct_url',
    )
    defaults.update(kwargs)
    return Job(**defaults)


ALL_STATES = list(JobState)


@pytest.mark.parametrize('source', ALL_STATES)
@pytest.mark.parametrize('target', ALL_STATES)
def test_transition_table_is_total_and_explicit(source, target):
    # Every pair is either explicitly allowed or explicitly not; no gaps.
    assert isinstance(can_transition(source, target), bool)
    if can_transition(source, target):
        assert target in ALLOWED[source]


def test_terminal_states_have_no_successors():
    assert ALLOWED[JobState.COMPLETED] == frozenset()
    assert ALLOWED[JobState.CANCELLED] == frozenset()


async def test_happy_path_transitions(store):
    job = await store.create(make_job())
    for state in (JobState.VALIDATING, JobState.RESOLVING, JobState.DOWNLOADING,
                  JobState.PROCESSING, JobState.COMPLETED):
        job = await store.transition(job.id, state)
    assert job.state is JobState.COMPLETED
    assert job.version == 5


async def test_illegal_transition_is_rejected(store):
    job = await store.create(make_job())
    with pytest.raises(InvalidTransitionError):
        await store.transition(job.id, JobState.COMPLETED)


async def test_completed_job_cannot_be_restarted(store):
    job = await store.create(make_job())
    for state in (JobState.VALIDATING, JobState.RESOLVING, JobState.DOWNLOADING,
                  JobState.PROCESSING, JobState.COMPLETED):
        await store.transition(job.id, state)
    with pytest.raises(InvalidTransitionError):
        await store.transition(job.id, JobState.QUEUED)


async def test_partial_success_can_be_requeued(store):
    job = await store.create(make_job())
    for state in (JobState.VALIDATING, JobState.RESOLVING, JobState.DOWNLOADING,
                  JobState.PROCESSING, JobState.PARTIAL_SUCCESS):
        await store.transition(job.id, state)
    requeued = await store.transition(job.id, JobState.QUEUED, actor='user')
    assert requeued.state is JobState.QUEUED


async def test_stale_version_loses_the_race(store):
    job = await store.create(make_job())
    await store.transition(job.id, JobState.VALIDATING)
    with pytest.raises(ConcurrencyConflictError):
        await store.transition(job.id, JobState.RESOLVING, expected_version=0)


async def test_idempotency_key_returns_the_same_job(store):
    first = await store.create(make_job())
    second = await store.create(make_job(id=new_job_id()))
    assert first.id == second.id


def test_idempotency_key_keeps_different_format_choices_distinct():
    common = ('u1', 'direct_url', 'https://x/a.jpg', 'source_preserving')
    assert idempotency_key(*common, options={'format_id': 'small'}) != idempotency_key(
        *common, options={'format_id': 'large'}
    )


async def test_lease_is_exclusive(store):
    await store.create(make_job())
    first = await store.lease_next('worker-a')
    second = await store.lease_next('worker-b')
    assert first is not None
    assert second is None, 'a leased job must not be handed to a second worker'


async def test_expired_lease_is_reclaimed(store):
    job = await store.create(make_job())
    leased = await store.lease_next('worker-a', lease_s=60)
    await store.transition(leased.id, JobState.VALIDATING)

    # Simulate the worker dying: expire its lease.
    store._conn.execute(
        'UPDATE jobs SET lease_expires_at = 1 WHERE id = ?', (job.id,)
    )
    reclaimed = await store.lease_next('worker-b', lease_s=60)
    assert reclaimed is not None
    assert reclaimed.id == job.id
    assert reclaimed.attempt == 1, 'reclaiming must count an attempt'


async def test_item_states_and_events_are_recorded(store):
    job = await store.create(make_job())
    await store.record_items(job.id, [
        JobItem(job.id, 1, 'a:1', 'image'), JobItem(job.id, 2, 'a:2', 'video')
    ])
    await store.item_state(job.id, 1, ItemState.DONE, record={'sha256': 'abc'})
    await store.item_state(
        job.id, 2, ItemState.FAILED, error_code='NETWORK_ERROR',
        error_message='boom', bump_attempt=True,
    )
    items = await store.items(job.id)
    assert [str(i.state) for i in items] == ['DONE', 'FAILED']
    assert items[0].output_record == {'sha256': 'abc'}
    assert items[1].attempts == 1
    events = await store.events(job.id)
    assert events[0]['to_state'] == 'QUEUED'


async def test_missing_job_raises(store):
    with pytest.raises(JobNotFoundError):
        await store.get('does-not-exist')


async def test_active_job_count_is_scoped_by_principal(store):
    await store.create(make_job(principal_id='u1'))
    await store.create(make_job(
        principal_id='u2',
        idempotency_key=idempotency_key('u2', 'direct_url', 'https://x/a.jpg', 'source_preserving'),
    ))
    assert await store.active_job_count('u1') == 1
    assert await store.active_job_count('u2') == 1
