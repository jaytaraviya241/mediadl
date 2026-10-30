"""Instagram connector contract tests (spec section 4.5.6, Phase 4 acceptance)."""
from __future__ import annotations

import json

import httpx
import pytest
import respx
from graph_fixtures import TOKEN, fixture

from mediadl.connectors.instagram_graph.normalize import normalize_node
from mediadl.domain.invariants import validate
from mediadl.domain.models import Asset, Availability, Collection, MediaKind, RepresentationRole
from mediadl.errors import (
    AuthExpiredError,
    ErrorCode,
    NotAccessibleError,
    RateLimited,
)
from mediadl.selection import choose
from mediadl.sources.base import SourceRequest

GRAPH = 'https://graph.instagram.com/v25.0'


def normalized(name: str):
    from datetime import UTC, datetime

    return validate(normalize_node(fixture(name), fetched_at=datetime.now(UTC), api_version='v25.0'))


def test_single_image_normalizes_to_one_image_asset():
    asset = normalized('single_image')
    assert isinstance(asset, Asset)
    assert asset.kind is MediaKind.IMAGE
    assert asset.availability is Availability.AVAILABLE
    assert asset.surface == 'FEED'
    assert asset.position == 1
    assert [r.rep_id for r in asset.representations] == ['primary']
    assert asset.metadata.fields['shortcode'] == 'SanitizedCode1'


def test_single_video_exposes_cover_as_preview_only():
    asset = normalized('single_video')
    assert asset.kind is MediaKind.VIDEO
    roles = {r.rep_id: r.role for r in asset.representations}
    assert roles == {'primary': RepresentationRole.PRIMARY, 'thumbnail': RepresentationRole.PREVIEW}
    # The cover must never be selected as a substitute for the video.
    selected = choose(asset)
    assert selected.primary.rep_id == 'primary'


def test_mixed_carousel_preserves_api_response_order():
    collection = normalized('carousel_mixed')
    assert isinstance(collection, Collection)
    assert collection.position_basis == 'api_response_order'
    assert [a.position for a in collection.assets] == [1, 2, 3]
    assert [a.kind for a in collection.assets] == [MediaKind.IMAGE, MediaKind.VIDEO, MediaKind.IMAGE]
    assert [a.ref.media_id for a in collection.assets] == [
        '17889455560051451', '17889455560051452', '17889455560051453'
    ]
    assert all(a.ref.parent_id == '17896450804038745' for a in collection.assets)


def test_missing_media_url_is_per_asset_copyright_unavailability():
    asset = normalized('reel_copyright_withheld')
    assert asset.availability is Availability.UNAVAILABLE_COPYRIGHT
    assert 'copyright' in asset.unavailable_reason
    # The cover image exists but is not offered as the media.
    assert [r.role for r in asset.representations] == [RepresentationRole.PREVIEW]
    assert asset.metadata.fields['media_audio_type'] == 'LICENSED_AUDIO'


def test_carousel_with_one_withheld_child_keeps_the_others(tmp_path):
    from datetime import UTC, datetime

    node = fixture('carousel_mixed')
    del node['children']['data'][1]['media_url']  # provider withholds child 2
    collection = validate(normalize_node(node, fetched_at=datetime.now(UTC), api_version='v25.0'))
    states = [a.availability for a in collection.assets]
    assert states == [
        Availability.AVAILABLE, Availability.UNAVAILABLE_COPYRIGHT, Availability.AVAILABLE
    ]
    assert [a.position for a in collection.assets] == [1, 2, 3]


@respx.mock
async def test_resolve_by_permalink_uses_only_graph_hosts(connector, ctx):
    respx.get(f'{GRAPH}/me/media').mock(
        return_value=httpx.Response(200, json=fixture('media_index_page1'))
    )
    respx.get(f'{GRAPH}/17889455560051445').mock(
        return_value=httpx.Response(200, json=fixture('single_video'))
    )

    item = await connector.resolve(
        SourceRequest('https://www.instagram.com/reel/SanitizedCode2/'), ctx
    )

    assert item.ref.media_id == '17889455560051445'
    hosts = {call.request.url.host for call in respx.calls}
    assert hosts == {'graph.instagram.com'}, 'no request may leave the Graph host'


@respx.mock
async def test_unknown_permalink_never_falls_back_to_scraping(connector, ctx):
    respx.get(f'{GRAPH}/me/media').mock(
        return_value=httpx.Response(200, json=fixture('media_index_page1'))
    )
    respx.get(f'{GRAPH}/v25.0/me/media').mock(
        return_value=httpx.Response(200, json=fixture('media_index_page2'))
    )
    respx.get(url__startswith='https://graph.instagram.com/v25.0/me/media?after=BBB').mock(
        return_value=httpx.Response(200, json=fixture('media_index_page2'))
    )

    with pytest.raises(NotAccessibleError) as excinfo:
        await connector.resolve(
            SourceRequest('https://www.instagram.com/p/SomeoneElsesPost/'), ctx
        )

    assert excinfo.value.code is ErrorCode.NOT_ACCESSIBLE_WITH_AUTHORIZED_ACCOUNT
    hosts = {call.request.url.host for call in respx.calls}
    assert hosts == {'graph.instagram.com'}
    assert not any('instagram.com/p/' in str(c.request.url) for c in respx.calls)


@respx.mock
async def test_carousel_falls_back_to_per_child_gets(connector, ctx):
    node = fixture('carousel_mixed')
    children = node['children']['data']
    respx.get(f'{GRAPH}/17896450804038745', params__contains={'fields': None}).mock(
        side_effect=[
            httpx.Response(400, json=fixture('error_not_visible')),
        ]
    )
    # First call (with children{...}) fails; the no-children call succeeds.
    route = respx.get(f'{GRAPH}/17896450804038745')
    stripped = {k: v for k, v in node.items() if k != 'children'}
    route.mock(side_effect=[
        httpx.Response(400, json={'error': {'code': 100, 'message': 'nonexisting field'}}),
        httpx.Response(200, json=stripped),
    ])
    respx.get(f'{GRAPH}/17896450804038745/children').mock(
        return_value=httpx.Response(200, json={'data': [{'id': c['id']} for c in children]})
    )
    for child in children:
        respx.get(f'{GRAPH}/{child["id"]}').mock(return_value=httpx.Response(200, json=child))

    item = await connector.resolve(SourceRequest('ig-media:17896450804038745'), ctx)

    assert isinstance(item, Collection)
    assert [a.position for a in item.assets] == [1, 2, 3]


@respx.mock
async def test_refresh_reresolves_the_media_url(connector, ctx):
    from mediadl.domain.models import SourceRef

    refreshed = dict(fixture('single_video'))
    refreshed['media_url'] = 'https://scontent.cdninstagram.com/v/t50/RESIGNED.mp4?oh=NEW'
    respx.get(f'{GRAPH}/17889455560051445').mock(
        return_value=httpx.Response(200, json=refreshed)
    )

    asset = await connector.refresh(
        SourceRef('instagram_graph', '17889455560051445', position=2, parent_id='p1'), ctx
    )

    assert asset.position == 2
    assert asset.ref.parent_id == 'p1'
    assert 'RESIGNED' in asset.representations[0].locator.url


@respx.mock
@pytest.mark.parametrize(
    ('fixture_name', 'status', 'expected'),
    [
        ('error_oauth_expired', 400, AuthExpiredError),
        ('error_rate_limited', 400, RateLimited),
        ('error_not_visible', 400, NotAccessibleError),
    ],
)
async def test_graph_errors_map_to_typed_errors(connector, ctx, fixture_name, status, expected):
    respx.get(f'{GRAPH}/999').mock(return_value=httpx.Response(status, json=fixture(fixture_name)))
    with pytest.raises(expected):
        await connector.resolve(SourceRequest('ig-media:999'), ctx)


@respx.mock
async def test_token_never_appears_in_logs_or_errors(connector, ctx, caplog):
    import logging

    from mediadl.obs.logging import RedactionFilter

    caplog.set_level(logging.DEBUG)
    caplog.handler.addFilter(RedactionFilter())
    respx.get(f'{GRAPH}/999').mock(
        return_value=httpx.Response(400, json=fixture('error_oauth_expired'))
    )

    with pytest.raises(AuthExpiredError) as excinfo:
        await connector.resolve(SourceRequest('ig-media:999'), ctx)

    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in json.dumps(dict(excinfo.value.public()))
    assert TOKEN not in caplog.text
    # The token was sent to the Graph host, and only there.
    assert any('access_token' in str(call.request.url) for call in respx.calls)
    assert {c.request.url.host for c in respx.calls} == {'graph.instagram.com'}


@respx.mock
async def test_list_media_pages_through_cursors(connector, ctx):
    respx.get(f'{GRAPH}/me/media').mock(
        return_value=httpx.Response(200, json=fixture('media_index_page1'))
    )
    page = await connector.list_media(ctx)
    assert [a.ref.media_id for a in page.items] == ['17889455560051444', '17889455560051445']
    assert page.cursor and page.cursor.startswith('https://graph.instagram.com/')
