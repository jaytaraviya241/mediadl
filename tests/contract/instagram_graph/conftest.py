from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from graph_fixtures import TOKEN

from mediadl.auth.credentials import AccessCredential
from mediadl.config import NetworkConfig
from mediadl.connectors.instagram_graph import InstagramGraphConnector
from mediadl.net.guarded_client import GuardedHttpClient, UrlPolicy
from mediadl.sources.base import AccessContext


@pytest.fixture
def graph_client():
    # IP pinning rewrites the URL host, which respx cannot match on; the
    # guard itself is covered by its own tests.
    policy = UrlPolicy(allow_private=True)
    return GuardedHttpClient(NetworkConfig(), policy=policy)


@pytest.fixture
def credential():
    return AccessCredential(
        connection_id='conn-1',
        connector_id='instagram_graph',
        auth_mode='instagram_login',
        token=TOKEN,
        scopes=frozenset({'instagram_business_basic'}),
        provider_user_id='17841400000000000',
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )


@pytest.fixture
async def ctx(graph_client, credential):
    context = AccessContext(
        principal_id='tester',
        connection_id='conn-1',
        credential=credential,
        http=graph_client,
    )
    try:
        yield context
    finally:
        await graph_client.aclose()


@pytest.fixture
def connector():
    return InstagramGraphConnector(api_version='v25.0')
