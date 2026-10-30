"""Dashboard API over the real runtime (no mocks below the HTTP layer)."""
from __future__ import annotations

import asyncio
import json
import urllib.parse

import httpx
import pytest

from mediadl.api import create_app
from mediadl.api.runtime import DashboardRuntime
from mediadl.app.context import build
from mediadl.jobs.states import JobState


@pytest.fixture
def dashboard_settings(settings):
    """Every connector enabled, so the credentials panel has something to describe."""
    settings.sources.enabled = [
        'direct_url', 'user_files', 'stream_manifest', 'instagram_graph'
    ]
    return settings


@pytest.fixture
async def dashboard(dashboard_settings, tmp_path):
    """A real runtime, with the loopback-allowing policy so fixtures are reachable."""
    from tests.conftest import LOCAL_POLICY

    settings = dashboard_settings
    ctx = build(settings, url_policy=LOCAL_POLICY, output_root=tmp_path / 'out')
    runtime = DashboardRuntime(settings, ctx=ctx)
    app = create_app(runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url='http://dash', timeout=30
    ) as client:
        async with app.router.lifespan_context(app):
            yield client, runtime


async def wait_for(client, job_id, *, states, timeout=20.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        payload = (await client.get(f'/api/jobs/{job_id}')).json()
        if payload['state'] in states:
            return payload
        await asyncio.sleep(0.05)
    raise AssertionError(f'job stayed in {payload["state"]}, wanted one of {states}')


async def test_state_reports_the_real_environment(dashboard):
    client, runtime = dashboard
    payload = (await client.get('/api/state')).json()

    assert payload['output_root'] == str(runtime.ctx.output.root)
    assert payload['policy'] == 'source_preserving'
    assert {c['id'] for c in payload['connectors']} >= {'direct_url', 'user_files'}
    direct = next(c for c in payload['connectors'] if c['id'] == 'direct_url')
    assert direct['needs_auth'] is False
    assert 'https' in payload['protocols']


async def test_submitting_a_job_runs_it_and_writes_files(dashboard, server, jpeg_bytes):
    client, runtime = dashboard
    server.add('/dash.jpg', jpeg_bytes, content_type='image/jpeg')

    response = await client.post('/api/jobs', json={'source': server.url('/dash.jpg')})
    assert response.status_code == 202
    job_id = response.json()['id']

    detail = await wait_for(client, job_id, states={'COMPLETED'})
    assert detail['items'][0]['state'] == 'DONE'

    outputs = (await client.get(f'/api/jobs/{job_id}/outputs')).json()['outputs']
    assert len(outputs) == 1
    assert outputs[0]['mime_type'] == 'image/jpeg'
    assert outputs[0]['exists'] is True

    served = await client.get('/api/files', params={'path': outputs[0]['relative_path']})
    assert served.status_code == 200
    assert served.content == jpeg_bytes


async def test_selected_format_is_persisted_and_used_for_the_download(dashboard, server, jpeg_bytes):
    """The dashboard option reaches selection, not merely the job JSON."""
    from mediadl.domain.models import (
        Asset,
        Availability,
        HttpLocator,
        MediaKind,
        Representation,
        RepresentationRole,
        SourceRef,
        TechnicalInfo,
    )
    from mediadl.testing.mock_connector import MockConnector

    client, runtime = dashboard
    server.add('/format.jpg', jpeg_bytes, content_type='image/jpeg')
    connector = MockConnector()
    connector.items['two-image-options'] = Asset(
        asset_id='mock:two-image-options',
        ref=SourceRef('mock', 'two-image-options'),
        kind=MediaKind.IMAGE,
        position=1,
        availability=Availability.AVAILABLE,
        representations=(
            Representation(
                rep_id='large', kind=MediaKind.IMAGE, role=RepresentationRole.PRIMARY,
                protocol='https', locator=HttpLocator(server.url('/format.jpg')),
                declared=TechnicalInfo(mime_type='image/jpeg', width=1600, height=900),
                label='large image',
            ),
            Representation(
                rep_id='small', kind=MediaKind.IMAGE, role=RepresentationRole.PRIMARY,
                protocol='https', locator=HttpLocator(server.url('/format.jpg')),
                declared=TechnicalInfo(mime_type='image/jpeg', width=800, height=450),
                label='small image',
            ),
        ),
    )
    runtime.ctx.registry.register(connector)
    runtime.ctx.admission.enabled_connectors = frozenset({
        *runtime.ctx.admission.enabled_connectors, 'mock'
    })

    default_response = await client.post('/api/jobs', json={'source': 'two-image-options'})
    assert default_response.status_code == 202
    await wait_for(client, default_response.json()['id'], states={'COMPLETED'})
    selected_response = await client.post(
        '/api/jobs',
        json={'source': 'two-image-options', 'options': {'format_id': 'small'}},
    )
    assert selected_response.status_code == 202
    assert default_response.json()['id'] != selected_response.json()['id']

    job_id = selected_response.json()['id']
    await wait_for(client, job_id, states={'COMPLETED'})
    job = await runtime.ctx.jobs.get(job_id)
    assert job.request['options'] == {'format_id': 'small'}
    output = (await client.get(f'/api/jobs/{job_id}/outputs')).json()['outputs'][0]
    assert output['selected_rep_id'] == 'small'
    sidecar = json.loads(
        (runtime.ctx.output.root / f"{output['relative_path']}.meta.json").read_text()
    )
    assert sidecar['app']['selected_representation']['rep_id'] == 'small'


async def test_the_job_is_the_same_one_the_cli_sees(dashboard, server, jpeg_bytes):
    client, runtime = dashboard
    server.add('/shared.jpg', jpeg_bytes, content_type='image/jpeg')
    job_id = (await client.post('/api/jobs', json={'source': server.url('/shared.jpg')})).json()['id']
    await wait_for(client, job_id, states={'COMPLETED'})

    # Read it straight from the store the CLI uses.
    stored = await runtime.ctx.jobs.get(job_id)
    assert stored.state is JobState.COMPLETED
    assert stored.principal_id == 'local'


async def test_partial_success_is_reported_per_item(
    dashboard, server, jpeg_bytes, dashboard_settings
):
    client, runtime = dashboard
    from mediadl.testing.mock_connector import MockConnector, asset, carousel

    connector = MockConnector()
    runtime.ctx.registry.register(connector)
    runtime.ctx.admission.enabled_connectors = frozenset(
        {*dashboard_settings.sources.enabled, 'mock'}
    )
    server.add('/ok.jpg', jpeg_bytes, content_type='image/jpeg')
    connector.items['mixed'] = carousel('900', (
        asset('m1', server.url('/ok.jpg'), position=1, parent_id='900'),
        asset('m2', server.url('/missing.jpg'), position=2, parent_id='900'),
    ))

    job_id = (await client.post('/api/jobs', json={'source': 'mixed'})).json()['id']
    detail = await wait_for(client, job_id, states={'PARTIAL_SUCCESS'})

    assert [i['state'] for i in detail['items']] == ['DONE', 'FAILED']
    assert detail['items'][1]['error_code']
    outputs = (await client.get(f'/api/jobs/{job_id}/outputs')).json()['outputs']
    assert [o['position'] for o in outputs] == [1], 'only the successful item has a file'


async def test_progress_events_stream_while_a_job_runs(dashboard, server, jpeg_bytes):
    client, runtime = dashboard
    body = jpeg_bytes * 400
    resource = server.add('/slow.jpg', body, content_type='image/jpeg')
    resource.slow = 0.01

    seen: list[dict] = []

    async def collect() -> None:
        async with runtime.bus.subscribe() as queue:
            while True:
                seen.append(await queue.get())

    collector = asyncio.create_task(collect())
    try:
        job_id = (await client.post('/api/jobs', json={'source': server.url('/slow.jpg')})).json()['id']
        await wait_for(client, job_id, states={'COMPLETED'})
        await asyncio.sleep(0.1)
    finally:
        collector.cancel()

    kinds = {event['type'] for event in seen}
    assert 'job.started' in kinds
    assert 'progress' in kinds
    assert 'job.finished' in kinds
    progress = [e for e in seen if e['type'] == 'progress']
    assert progress[-1]['done'] == len(body)
    assert progress[-1]['percent'] == 100.0


async def test_runtime_stream_starts_with_a_snapshot(dashboard):
    """The generator behind /api/events. It never ends, so read one frame."""
    _, runtime = dashboard
    stream = runtime.stream()
    frame = await asyncio.wait_for(anext(stream), timeout=5)
    await stream.aclose()

    assert frame.startswith('data: ')
    payload = json.loads(frame[6:])
    assert payload['type'] == 'snapshot'
    assert isinstance(payload['jobs'], list)


async def test_sse_endpoint_streams_over_a_real_server(dashboard_settings, tmp_path):
    """End-to-end SSE.

    httpx's ASGITransport buffers a response until it completes, and this
    stream never completes, so this one runs against a real uvicorn server.
    """
    import uvicorn

    from tests.conftest import LOCAL_POLICY

    ctx = build(dashboard_settings, url_policy=LOCAL_POLICY, output_root=tmp_path / 'out')
    app = create_app(runtime=DashboardRuntime(dashboard_settings, ctx=ctx))
    config = uvicorn.Config(app, host='127.0.0.1', port=0, log_level='warning', access_log=False)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started, 'server did not start'
        port = server.servers[0].sockets[0].getsockname()[1]

        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=10) as client:
            async with client.stream('GET', '/api/events') as response:
                assert response.status_code == 200
                assert response.headers['content-type'].startswith('text/event-stream')
                chunk = await asyncio.wait_for(
                    anext(aiter(response.aiter_bytes())), timeout=5
                )
        payload = json.loads(chunk.decode().split('data: ', 1)[1].split('\n\n')[0])
        assert payload['type'] == 'snapshot'
        assert isinstance(payload['jobs'], list)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


async def test_unsupported_source_is_a_typed_400(dashboard):
    client, _ = dashboard
    response = await client.post('/api/jobs', json={'source': 'gopher://nope/x'})
    assert response.status_code == 400
    assert response.json()['detail']['code'] == 'UNSUPPORTED_SOURCE'


async def test_instagram_without_a_connection_is_a_401(dashboard):
    client, _ = dashboard
    response = await client.post(
        '/api/jobs', json={'source': 'https://www.instagram.com/p/SomeShortcode/'}
    )
    job_id = response.json()['id']
    detail = await wait_for(client, job_id, states={'AUTHORIZATION_REQUIRED', 'FAILED'})
    assert detail['state'] == 'AUTHORIZATION_REQUIRED', 'missing auth must not look like a crash'


async def test_facebook_page_is_not_queued_through_the_direct_url_fallback(dashboard):
    client, runtime = dashboard
    response = await client.post(
        '/api/jobs', json={'source': 'https://www.facebook.com/reel/123456789'}
    )
    assert response.status_code == 400
    assert response.json()['detail']['code'] == 'UNSUPPORTED_SOURCE'
    assert 'Facebook connector' in response.json()['detail']['message']
    assert await runtime.ctx.jobs.list_jobs('local', 10) == []


@pytest.mark.parametrize(
    'path', ['../../../etc/passwd', '/etc/passwd', 'a/../../../../etc/hosts']
)
async def test_file_endpoint_refuses_paths_outside_the_output_root(dashboard, path):
    client, _ = dashboard
    response = await client.get('/api/files', params={'path': path})
    assert response.status_code == 404
    assert response.json()['detail']['code'] in ('PATH_REJECTED', 'NOT_FOUND')


async def test_credentials_describe_every_connector(dashboard):
    client, _ = dashboard
    payload = (await client.get('/api/credentials')).json()

    by_id = {r['connector_id']: r for r in payload['requirements']}
    assert by_id['direct_url']['kind'] == 'none'
    assert by_id['user_files']['kind'] == 'none'
    assert by_id['ffmpeg']['kind'] == 'tool'

    instagram = by_id['instagram_graph']
    assert instagram['kind'] == 'oauth'
    assert instagram['status'] == 'not_configured'
    names = {f['name'] for f in instagram['fields']}
    assert names == {'client_id', 'client_secret', 'redirect_uri'}
    for field in instagram['fields']:
        assert field['where_to_add'] and field['where_to_obtain']
    secret = next(f for f in instagram['fields'] if f['name'] == 'client_secret')
    assert secret['secret'] is True
    assert 'never' in secret['notes'].lower() or 'not' in secret['notes'].lower()
    assert any('developers.facebook.com' in d['url'] for d in instagram['docs'])
    assert {n['item'] for n in payload['not_required']} >= {'A browser session cookie (sessionid)'}


async def test_connect_start_requires_https_redirect(dashboard):
    client, _ = dashboard
    response = await client.post('/api/connections/instagram/start', json={
        'client_id': '123', 'redirect_uri': 'http://localhost/callback',
    })
    assert response.status_code == 400
    assert 'HTTPS' in response.json()['detail']['message']


async def test_connect_start_rejects_loopback_and_normalizes_the_registered_uri(dashboard):
    client, runtime = dashboard
    loopback = await client.post('/api/connections/instagram/start', json={
        'client_id': '123', 'redirect_uri': 'https://127.0.0.1/callback',
    })
    assert loopback.status_code == 400
    assert 'localhost' in loopback.json()['detail']['message']

    started = await client.post('/api/connections/instagram/start', json={
        'client_id': '1234567890', 'redirect_uri': '  https://app.example/callback/  ',
    })
    assert started.status_code == 200
    payload = started.json()
    assert payload['redirect_uri'] == 'https://app.example/callback/'
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(payload['authorize_url']).query)['state'][0]
    assert await runtime.connections.consume_state(state) == ('local', payload['redirect_uri'])


async def test_connect_start_issues_a_single_use_state(dashboard):
    client, runtime = dashboard
    response = await client.post('/api/connections/instagram/start', json={
        'client_id': '1234567890', 'redirect_uri': 'https://app.example/callback',
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload['authorize_url'].startswith('https://www.instagram.com/oauth/authorize')
    assert 'instagram_business_basic' in payload['authorize_url']
    assert payload['scopes'] == ['instagram_business_basic']

    import urllib.parse

    state = urllib.parse.parse_qs(
        urllib.parse.urlsplit(payload['authorize_url']).query
    )['state'][0]
    assert await runtime.connections.consume_state(state) == ('local', 'https://app.example/callback')


async def test_connect_complete_keeps_state_usable_when_the_redirect_uri_was_edited(dashboard):
    client, runtime = dashboard
    started = await client.post('/api/connections/instagram/start', json={
        'client_id': '1234567890', 'redirect_uri': 'https://app.example/callback',
    })
    assert started.status_code == 200
    payload = started.json()
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(payload['authorize_url']).query)['state'][0]

    # The browser returned to the URI from Step 1, but the mutable form value
    # was changed.  Reject it locally before a token exchange or consuming the
    # one-time state, so the user can put the original value back.
    response = await client.post('/api/connections/instagram/complete', json={
        'redirect_url': f'https://app.example/callback?code=one-time-code&state={state}',
        'client_id': '1234567890',
        'client_secret': 'secret',
        'redirect_uri': 'https://app.example/callback/',
    })
    assert response.status_code == 400
    assert response.json()['detail']['code'] == 'INVALID_INPUT'
    assert await runtime.connections.consume_state(state) == ('local', 'https://app.example/callback')


async def test_connect_complete_rejects_a_url_without_a_code(dashboard):
    client, _ = dashboard
    response = await client.post('/api/connections/instagram/complete', json={
        'redirect_url': 'https://app.example/callback',
        'client_id': '123', 'client_secret': 'secret', 'redirect_uri': 'https://app.example/callback',
    })
    assert response.status_code == 400
    assert response.json()['detail']['code'] == 'INVALID_INPUT'


async def test_retry_requeues_a_failed_job(dashboard, server):
    client, _ = dashboard
    job_id = (await client.post(
        '/api/jobs', json={'source': server.url('/never-existed.jpg')}
    )).json()['id']
    await wait_for(client, job_id, states={'FAILED'})

    response = await client.post(f'/api/jobs/{job_id}/retry')
    assert response.status_code == 202
    detail = await wait_for(client, job_id, states={'FAILED', 'COMPLETED'})
    assert detail['attempt'] >= 0


async def test_retrying_a_completed_job_is_a_conflict(dashboard, server, jpeg_bytes):
    client, _ = dashboard
    server.add('/done.jpg', jpeg_bytes, content_type='image/jpeg')
    job_id = (await client.post('/api/jobs', json={'source': server.url('/done.jpg')})).json()['id']
    await wait_for(client, job_id, states={'COMPLETED'})

    response = await client.post(f'/api/jobs/{job_id}/retry')
    assert response.status_code == 409


async def test_static_dashboard_is_served(dashboard):
    client, _ = dashboard
    index = await client.get('/')
    assert index.status_code == 200
    assert '<title>mediadl — Media downloader</title>' in index.text
    for asset in ('/assets/app.js', '/assets/styles.css'):
        assert (await client.get(asset)).status_code == 200
