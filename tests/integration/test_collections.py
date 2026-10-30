"""Ordered collections and partial success (spec Phase 2 acceptance)."""
from __future__ import annotations

import json

import pytest

from mediadl.domain.models import Availability, MediaKind
from mediadl.jobs.states import JobState
from mediadl.testing.mock_connector import MockConnector, asset, carousel


@pytest.fixture
def mock_app(app, settings):
    connector = MockConnector()
    app.registry.register(connector)
    app.admission.enabled_connectors = frozenset({*settings.sources.enabled, 'mock'})
    return app, connector


async def test_mixed_carousel_middle_failure_yields_partial_success(
    mock_app, server, service, jpeg_bytes
):
    app, connector = mock_app
    server.add('/1.jpg', jpeg_bytes, content_type='image/jpeg')
    server.add('/3.jpg', jpeg_bytes + b'\x00', content_type='image/jpeg')
    # Position 2 points at a path the server does not serve.
    connector.items['carousel'] = carousel('17896', (
        asset('a1', server.url('/1.jpg'), kind=MediaKind.IMAGE, position=1, parent_id='17896'),
        asset('a2', server.url('/missing.mp4'), kind=MediaKind.VIDEO, position=2, parent_id='17896'),
        asset('a3', server.url('/3.jpg'), kind=MediaKind.IMAGE, position=3, parent_id='17896'),
    ))

    outcome = await service.get('carousel')

    assert outcome.state is JobState.PARTIAL_SUCCESS
    assert [r.status for r in outcome.results] == ['completed', 'failed', 'completed']
    # The surviving items keep their original position numbers: no renumbering.
    assert outcome.results[0].record.relative_path.count('01-of-03') == 1
    assert outcome.results[2].record.relative_path.count('03-of-03') == 1

    manifest = json.loads(outcome.manifest_path.read_text())
    assert manifest['counts'] == {'completed': 2, 'failed': 1, 'unavailable': 0, 'skipped': 0}
    failed = next(i for i in manifest['items'] if i['position'] == 2)
    assert failed['status'] == 'failed'
    assert failed['error_code']
    assert 'relative_path' not in failed


async def test_unavailable_asset_is_reported_not_failed(mock_app, server, service, jpeg_bytes):
    app, connector = mock_app
    server.add('/ok.jpg', jpeg_bytes, content_type='image/jpeg')
    connector.items['copyright'] = carousel('900', (
        asset('b1', server.url('/ok.jpg'), position=1, parent_id='900'),
        asset(
            'b2', '', kind=MediaKind.VIDEO, position=2, parent_id='900',
            availability=Availability.UNAVAILABLE_COPYRIGHT,
            unavailable_reason='media_url omitted by provider',
        ),
    ))

    outcome = await service.get('copyright')

    assert outcome.state is JobState.PARTIAL_SUCCESS
    assert outcome.results[1].status == 'unavailable'
    assert outcome.results[1].error_code == 'UNAVAILABLE_COPYRIGHT'
    manifest = json.loads(outcome.manifest_path.read_text())
    assert manifest['counts']['unavailable'] == 1


async def test_all_items_failing_is_a_failed_job(mock_app, server, service):
    app, connector = mock_app
    connector.items['allbad'] = carousel('901', (
        asset('c1', server.url('/nope1.jpg'), position=1, parent_id='901'),
        asset('c2', server.url('/nope2.jpg'), position=2, parent_id='901'),
    ))

    outcome = await service.get('allbad')

    assert outcome.state is JobState.FAILED
    assert all(r.status == 'failed' for r in outcome.results)


async def test_item_states_are_persisted(mock_app, server, service, app, jpeg_bytes):
    _, connector = mock_app
    server.add('/p.jpg', jpeg_bytes, content_type='image/jpeg')
    connector.items['persist'] = carousel('902', (
        asset('d1', server.url('/p.jpg'), position=1, parent_id='902'),
        asset('d2', server.url('/gone.jpg'), position=2, parent_id='902'),
    ))

    outcome = await service.get('persist')
    items = await app.jobs.items(outcome.job_id)

    assert [str(i.state) for i in items] == ['DONE', 'FAILED']
    assert items[1].error_code
    assert items[0].attempts >= 1


async def test_collection_larger_than_limit_is_refused(mock_app, service, server, jpeg_bytes):
    app, connector = mock_app
    app.asset_policy.limits.max_assets_per_collection = 2
    server.add('/x.jpg', jpeg_bytes, content_type='image/jpeg')
    connector.items['toobig'] = carousel('903', tuple(
        asset(f'e{i}', server.url('/x.jpg'), position=i, parent_id='903') for i in range(1, 4)
    ))

    outcome = await service.get('toobig')

    assert outcome.state is JobState.FAILED
