"""The downloader dashboard rendered in a real DOM.

The smoke harness covers the user-facing states that are easy to regress in a
static client: the downloader must be first, a URL must be checked before a
job starts, account-required feedback must be actionable, and an inspected
media card must offer a real download action. It also catches the old hidden
drawer regression that obscured the first screen.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from mediadl.api import create_app
from mediadl.api.runtime import DashboardRuntime
from mediadl.app.context import build

UI_SMOKE = Path(__file__).resolve().parents[2] / 'src' / 'mediadl' / 'testing' / 'ui_smoke.js'


def _node_env() -> dict[str, str] | None:
    """Node plus a resolvable jsdom, or None."""
    node = shutil.which('node')
    if node is None:
        return None
    local = Path.cwd() / 'node_modules'
    if (local / 'jsdom').is_dir():
        return {**os.environ, 'NODE_PATH': str(local)}
    probe = subprocess.run(
        [node, '-e', "require('jsdom')"], capture_output=True, timeout=30, check=False
    )
    return dict(os.environ) if probe.returncode == 0 else None


NODE_ENV = _node_env()
pytestmark = pytest.mark.skipif(
    NODE_ENV is None, reason='needs node and jsdom (npm install jsdom)'
)


@pytest.fixture
async def live_dashboard(settings, tmp_path):
    """A real uvicorn server, because the page is loaded over HTTP."""
    import uvicorn

    settings.sources.enabled = ['direct_url', 'user_files', 'stream_manifest', 'instagram_graph']
    ctx = build(settings, output_root=tmp_path / 'out')
    app = create_app(runtime=DashboardRuntime(settings, ctx=ctx))
    config = uvicorn.Config(app, host='127.0.0.1', port=0, log_level='warning', access_log=False)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started, 'server did not start'
        yield f'http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}'
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


@pytest.fixture
async def rendered(live_dashboard):
    result = await asyncio.to_thread(
        subprocess.run,
        ['node', str(UI_SMOKE), live_dashboard],
        capture_output=True, text=True, timeout=180, env=NODE_ENV, check=False,
    )
    if result.returncode != 0:
        pytest.fail(f'UI harness failed:\n{result.stderr[-2000:]}')
    return json.loads(result.stdout)


def test_page_boots_without_javascript_errors(rendered):
    assert rendered['errors'] == [], 'the dashboard must load with no console errors'


def test_live_updates_connect(rendered):
    assert rendered['live'].lower() == 'live', 'the SSE stream must connect'


def test_downloader_is_the_first_visible_product_surface(rendered):
    initial = rendered['initial']
    assert initial['downloader_visible'] is True
    assert initial['drawer_hidden'] is True, 'a hidden drawer must not cover the first screen'
    assert 'Download media' in initial['title']
    assert 'Media details' in initial['empty_review']


def test_link_feedback_and_account_required_state_are_actionable(rendered):
    assert 'complete URL' in rendered['invalid_hint']
    assert 'Instagram link detected' in rendered['instagram_hint']
    assert 'Connect your Instagram professional account' in rendered['auth_required']


def test_inspected_media_has_a_review_and_download_action(rendered):
    review = rendered['review']
    assert review['title'] == 'A safe media preview'
    assert '1080' in review['option']
    assert review['download_enabled'] is True
    assert 'video' in review['facts']


def test_setup_is_secondary_but_still_exposes_source_status(rendered):
    assert rendered['setup']['visible'] is True
    assert rendered['setup']['sources'] >= 3


def test_connection_wizard_validates_and_locks_the_oauth_sequence(rendered):
    wizard = rendered['wizard']
    assert wizard['step_two_initially_locked'] is True
    assert wizard['complete_initially_disabled'] is True
    assert 'Instagram App ID: is required' in wizard['required_error']
    assert 'Redirect URI: is required' in wizard['required_error']
    assert wizard['requests_after_required_error'] == 0
    assert 'must start with https://' in wizard['non_https_error']
    assert wizard['requests_after_non_https_error'] == 0
    assert 'public HTTPS domain' in wizard['https_error']
    assert wizard['requests_after_https_error'] == 0
    assert wizard['unlocked_after_start'] is True
    assert wizard['step_one_locked_after_start'] is True
    assert wizard['registered_redirect'] == 'https://app.example/auth/instagram/callback'
    assert wizard['relocked_after_step_one_change'] is True
    assert 'ready to edit' in wizard['stale_status']


def test_connection_wizard_explains_fastapi_422_field_errors(rendered):
    wizard = rendered['wizard']
    assert 'Redirect URI: is required' in wizard['fastapi_422']
    assert wizard['fastapi_422_marks_redirect'] is True


def test_self_test_remains_available_from_setup(rendered):
    assert 'System check' in rendered['selftest']
