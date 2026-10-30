from __future__ import annotations

import pytest

from mediadl.config import NetworkConfig
from mediadl.net.guarded_client import GuardedHttpClient, UrlPolicy
from mediadl.testing.fixture_server import FixtureServer

#: Loopback is denied by the production URL policy, so fixture-server tests
#: opt in explicitly. This flag is not reachable from user configuration.
LOCAL_POLICY = UrlPolicy(
    allowed_schemes=frozenset({'http', 'https'}),
    allowed_ports=frozenset({80, 443}),
    allow_private=True,
    max_redirects=5,
)


@pytest.fixture
def server():
    with FixtureServer() as srv:
        yield srv


@pytest.fixture
async def client():
    c = GuardedHttpClient(NetworkConfig(read_timeout_s=10), policy=LOCAL_POLICY)
    try:
        yield c
    finally:
        await c.aclose()


@pytest.fixture
def jpeg_bytes():
    return b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + bytes(range(256)) * 40 + b'\xff\xd9'


@pytest.fixture
def settings(tmp_path):
    from mediadl.config import JobsConfig, NetworkConfig, OutputConfig, Settings, SourcesConfig

    return Settings(
        sources=SourcesConfig(enabled=['direct_url', 'user_files'], import_roots=[str(tmp_path)]),
        output=OutputConfig(root=str(tmp_path / 'out')),
        jobs=JobsConfig(database=str(tmp_path / 'jobs.sqlite3')),
        network=NetworkConfig(read_timeout_s=10),
    )


@pytest.fixture
async def app(settings, tmp_path):
    from mediadl.app.context import build

    ctx = build(settings, url_policy=LOCAL_POLICY, output_root=tmp_path / 'out')
    try:
        yield ctx
    finally:
        await ctx.aclose()


@pytest.fixture
def service(app):
    from mediadl.app.service import MediaDLService

    return MediaDLService(app, principal_id='tester')
