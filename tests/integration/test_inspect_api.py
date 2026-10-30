"""The dashboard's no-job source inspection contract."""
from __future__ import annotations

import contextlib

import httpx

from mediadl.api import create_app
from mediadl.api.runtime import DashboardRuntime
from mediadl.app.context import build
from mediadl.config import JobsConfig, OutputConfig, Settings, SourcesConfig
from mediadl.domain.models import (
    Asset,
    Availability,
    HttpLocator,
    MediaKind,
    Representation,
    RepresentationRole,
    SourceMetadata,
    SourceRef,
    TechnicalInfo,
)
from mediadl.net.guarded_client import UrlPolicy
from mediadl.sources.base import AccessContext, BaseConnector, ConnectorCapabilities, SourceRequest
from mediadl.sources.registry import SourceRegistry


class InspectConnector(BaseConnector):
    connector_id = 'inspect_test'
    capabilities = ConnectorCapabilities(
        media_kinds=frozenset({MediaKind.IMAGE}),
        auth_modes=frozenset({'none'}),
        allowed_media_hosts=('media.example',),
    )

    def match(self, request: SourceRequest) -> int | None:
        return 100 if request.raw == 'inspect-this' else None

    async def resolve(self, request: SourceRequest, ctx: AccessContext) -> Asset:
        return Asset(
            asset_id='inspect_test:one',
            ref=SourceRef(self.connector_id, 'one'),
            kind=MediaKind.IMAGE,
            position=1,
            availability=Availability.AVAILABLE,
            representations=(
                Representation(
                    rep_id='primary',
                    kind=MediaKind.IMAGE,
                    role=RepresentationRole.PRIMARY,
                    protocol='https',
                    locator=HttpLocator('https://media.example/full.jpg?signature=private'),
                    declared=TechnicalInfo(
                        mime_type='image/jpeg', container='jpg', width=1600, height=900
                    ),
                    label='full image',
                ),
                Representation(
                    rep_id='cover',
                    kind=MediaKind.IMAGE,
                    role=RepresentationRole.PREVIEW,
                    protocol='https',
                    locator=HttpLocator('https://media.example/cover.jpg?access_token=private'),
                    label='cover',
                ),
            ),
            metadata=SourceMetadata(
                fields={'caption': 'A safe title', 'access_token': 'must not be exposed'}
            ),
        )


class PreviewResponse:
    status_code = 200
    headers = {'content-type': 'image/jpeg', 'content-length': '3'}

    async def aiter_raw(self):
        yield b'jpg'


class PreviewHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    @contextlib.asynccontextmanager
    async def stream(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        yield PreviewResponse()


async def test_inspection_is_safe_and_does_not_create_a_job(tmp_path):
    settings = Settings(
        sources=SourcesConfig(enabled=['inspect_test']),
        output=OutputConfig(root=str(tmp_path / 'out')),
        jobs=JobsConfig(database=str(tmp_path / 'jobs.sqlite3')),
    )
    ctx = build(
        settings,
        registry=SourceRegistry([InspectConnector()]),
        url_policy=UrlPolicy(allow_private=True),
        output_root=tmp_path / 'out',
    )
    runtime = DashboardRuntime(settings, ctx=ctx)
    guarded_http = runtime.ctx.http
    preview_http = PreviewHttp()
    runtime.ctx.http = preview_http  # type: ignore[assignment]
    app = create_app(runtime=runtime)
    app.state.runtime = runtime

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://dashboard') as client:
            inspected = await client.post('/api/inspect', json={'source': 'inspect-this'})
            assert inspected.status_code == 200
            payload = inspected.json()

            assert payload['supported'] is True
            assert payload['connector_id'] == 'inspect_test'
            assert payload['summary'] == {
                'asset_count': 1,
                'available_count': 1,
                'downloadable_count': 1,
            }
            asset = payload['assets'][0]
            assert asset['title'] == 'A safe title'
            assert asset['availability'] == 'available'
            assert asset['selected_format_id'] == 'primary'
            assert asset['formats'][0]['width'] == 1600
            assert asset['formats'][0]['height'] == 900
            assert asset['preview_url'].startswith('/api/previews/')
            assert 'media.example' not in inspected.text
            assert 'signature=private' not in inspected.text
            assert 'access_token' not in inspected.text

            preview = await client.get(asset['preview_url'])
            assert preview.status_code == 200
            assert preview.content == b'jpg'
            assert preview.headers['content-type'] == 'image/jpeg'
            assert preview_http.calls == [
                ('GET', 'https://media.example/cover.jpg?access_token=private', {
                    'headers': {}, 'allowed_hosts': ('media.example',),
                })
            ]

        assert await runtime.ctx.jobs.list_jobs('local', 10) == []
    finally:
        runtime.ctx.http = guarded_http
        await runtime.aclose()


async def test_inspection_requires_an_instagram_connection_before_resolving(tmp_path):
    from mediadl.connectors.instagram_graph.connector import InstagramGraphConnector

    settings = Settings(
        sources=SourcesConfig(enabled=['instagram_graph']),
        output=OutputConfig(root=str(tmp_path / 'out')),
        jobs=JobsConfig(database=str(tmp_path / 'jobs.sqlite3')),
    )
    ctx = build(
        settings,
        registry=SourceRegistry([InstagramGraphConnector()]),
        output_root=tmp_path / 'out',
    )
    runtime = DashboardRuntime(settings, ctx=ctx)
    app = create_app(runtime=runtime)
    app.state.runtime = runtime
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://dashboard') as client:
            response = await client.post(
                '/api/inspect', json={'source': 'https://www.instagram.com/p/SomeShortcode/'}
            )
        assert response.status_code == 401
        assert response.json()['detail']['code'] == 'AUTH_REQUIRED'
        assert await runtime.ctx.jobs.list_jobs('local', 10) == []
    finally:
        await runtime.aclose()


async def test_inspection_refuses_facebook_pages_without_a_facebook_connector(tmp_path):
    from mediadl.connectors.direct_url.connector import DirectUrlConnector

    settings = Settings(
        sources=SourcesConfig(enabled=['direct_url']),
        output=OutputConfig(root=str(tmp_path / 'out')),
        jobs=JobsConfig(database=str(tmp_path / 'jobs.sqlite3')),
    )
    ctx = build(
        settings,
        registry=SourceRegistry([DirectUrlConnector()]),
        output_root=tmp_path / 'out',
    )
    runtime = DashboardRuntime(settings, ctx=ctx)
    app = create_app(runtime=runtime)
    app.state.runtime = runtime
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://dashboard') as client:
            response = await client.post(
                '/api/inspect', json={'source': 'https://www.facebook.com/reel/123456789'}
            )
        assert response.status_code == 400
        assert response.json()['detail']['code'] == 'UNSUPPORTED_SOURCE'
        assert 'Facebook connector' in response.json()['detail']['message']
        # The guard is intentionally narrow: ordinary direct-media URLs keep
        # their existing preflight behavior.
        async with httpx.AsyncClient(transport=transport, base_url='http://dashboard') as client:
            direct = await client.post('/api/inspect', json={'source': 'https://example.com/video.mp4'})
        assert direct.status_code == 200
        assert direct.json()['connector_id'] == 'direct_url'
        assert await runtime.ctx.jobs.list_jobs('local', 10) == []
    finally:
        await runtime.aclose()
