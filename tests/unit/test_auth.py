"""Credential storage, OAuth flow, and Meta callbacks (spec sections 6.2, 6.4)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from mediadl.auth.connections import (
    Connection,
    ConnectionStatus,
    ConnectionStore,
    new_connection_id,
)
from mediadl.auth.credentials import CredentialProvider
from mediadl.auth.meta_callbacks import (
    MetaCallbackHandler,
    SignedRequestError,
    parse_signed_request,
)
from mediadl.auth.oauth_instagram import (
    InstagramOAuth,
    OAuthConfig,
    authorize_url,
    clean_code,
    normalize_redirect_uri,
    validate_redirect_uri,
)
from mediadl.auth.vault import KeyProvider, TokenVault
from mediadl.config import NetworkConfig
from mediadl.errors import AuthExpiredError, AuthRequiredError, PermissionMissingError
from mediadl.net.guarded_client import GuardedHttpClient, UrlPolicy

APP_SECRET = 'test-app-secret'
TOKEN = 'IGAA-long-lived-token-value'


@pytest.fixture
def vault(tmp_path):
    # Force the file-backed key so tests never touch the real OS keychain.
    provider = KeyProvider(tmp_path / 'keys')
    provider._from_keyring = lambda: None
    return TokenVault(provider)


@pytest.fixture
def store(tmp_path, vault):
    store = ConnectionStore(tmp_path / 'connections.sqlite3', vault)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
async def oauth_client():
    client = GuardedHttpClient(NetworkConfig(), policy=UrlPolicy(allow_private=True))
    try:
        yield client
    finally:
        await client.aclose()


def make_connection(connection_id=None, **kwargs):
    defaults = dict(
        id=connection_id or new_connection_id(),
        principal_id='u1',
        connector_id='instagram_graph',
        auth_mode='instagram_login',
        status=ConnectionStatus.ACTIVE,
        provider_user_id='17841400000000000',
        scopes_granted=('instagram_business_basic',),
        expires_at=datetime.now(UTC) + timedelta(days=60),
        last_refreshed_at=datetime.now(UTC),
    )
    defaults.update(kwargs)
    return Connection(**defaults)


# --- vault ----------------------------------------------------------------
def test_token_round_trips(vault):
    sealed = vault.seal(TOKEN, connection_id='c1', principal_id='u1')
    assert TOKEN.encode() not in sealed.ciphertext
    assert vault.open(sealed, connection_id='c1', principal_id='u1') == TOKEN


def test_ciphertext_is_bound_to_its_connection(vault):
    sealed = vault.seal(TOKEN, connection_id='c1', principal_id='u1')
    with pytest.raises(AuthExpiredError):
        vault.open(sealed, connection_id='c2', principal_id='u1')
    with pytest.raises(AuthExpiredError):
        vault.open(sealed, connection_id='c1', principal_id='someone-else')


def test_tampered_ciphertext_is_rejected(vault):
    sealed = vault.seal(TOKEN, connection_id='c1', principal_id='u1')
    tampered = type(sealed)(sealed.ciphertext[:-1] + bytes([sealed.ciphertext[-1] ^ 1]),
                            sealed.nonce, sealed.key_version)
    with pytest.raises(AuthExpiredError):
        vault.open(tampered, connection_id='c1', principal_id='u1')


def test_each_write_uses_a_fresh_nonce(vault):
    first = vault.seal(TOKEN, connection_id='c1', principal_id='u1')
    second = vault.seal(TOKEN, connection_id='c1', principal_id='u1')
    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext


# --- connection store -----------------------------------------------------
async def test_connection_lookup_is_scoped_by_principal(store):
    connection = await store.save(make_connection(), TOKEN)
    assert await store.get_owned('u1', connection.id) is not None
    # Another principal must not reach this row, even with the exact id.
    assert await store.get_owned('attacker', connection.id) is None


async def test_disconnect_hard_deletes_the_ciphertext(store, vault):
    connection = await store.save(make_connection(), TOKEN)
    await CredentialProvider(store).disconnect('u1', connection.id)
    rows = store._conn.execute(
        'SELECT COUNT(*) AS n FROM connections WHERE id = ?', (connection.id,)
    ).fetchone()
    assert rows['n'] == 0


async def test_expired_connection_raises_auth_expired(store):
    connection = await store.save(
        make_connection(expires_at=datetime.now(UTC) - timedelta(days=1)), TOKEN
    )
    with pytest.raises(AuthExpiredError):
        await CredentialProvider(store).acquire('u1', connection.id)


async def test_missing_scope_is_reported(store):
    connection = await store.save(make_connection(scopes_granted=('instagram_business_basic',)), TOKEN)
    with pytest.raises(PermissionMissingError):
        await CredentialProvider(store).acquire(
            'u1', connection.id, frozenset({'instagram_business_content_publish'})
        )


async def test_no_connection_raises_auth_required(store):
    with pytest.raises(AuthRequiredError):
        await CredentialProvider(store).acquire('u1', None)


async def test_acquired_credential_never_prints_its_token(store):
    connection = await store.save(make_connection(), TOKEN)
    credential = await CredentialProvider(store).acquire('u1', connection.id)
    assert credential.token == TOKEN
    assert TOKEN not in repr(credential)
    assert TOKEN not in str(credential)


def test_refresh_is_not_due_for_a_fresh_token():
    assert not make_connection().needs_refresh()


def test_refresh_is_due_near_expiry():
    connection = make_connection(
        expires_at=datetime.now(UTC) + timedelta(days=10),
        last_refreshed_at=datetime.now(UTC),
    )
    assert connection.needs_refresh()


# --- OAuth state ----------------------------------------------------------
async def test_state_is_single_use(store):
    state = await store.create_state('u1', 'instagram_graph', 'https://cb.example/x')
    assert await store.consume_state(state) == ('u1', 'https://cb.example/x')
    with pytest.raises(AuthRequiredError, match='already used'):
        await store.consume_state(state)


async def test_unknown_state_is_rejected(store):
    with pytest.raises(AuthRequiredError, match='unknown'):
        await store.consume_state('never-issued')


async def test_expired_state_is_rejected(store):
    state = await store.create_state('u1', 'instagram_graph', 'https://cb.example/x')
    with pytest.raises(AuthRequiredError, match='expired'):
        await store.consume_state(state, ttl_s=-1)


# --- OAuth flow -----------------------------------------------------------
def test_authorize_url_requests_the_minimum_scope():
    config = OAuthConfig('APPID', 'SECRET', 'https://cb.example/x')
    url = authorize_url(config, 'STATE')
    assert 'scope=instagram_business_basic' in url
    assert 'state=STATE' in url
    assert 'response_type=code' in url
    # No publishing, messaging, or comment scopes are ever requested.
    for scope in ('content_publish', 'manage_messages', 'manage_comments'):
        assert scope not in url


def test_authorize_url_requires_https_redirect():
    with pytest.raises(ValueError, match='HTTPS'):
        authorize_url(OAuthConfig('A', 'S', 'http://cb.example/x'), 'STATE')


@pytest.mark.parametrize(
    'redirect_uri',
    (
        '',
        'https://localhost/callback',
        'https://127.0.0.1/callback',
        'https://user:password@cb.example/callback',
        'https://cb.example/callback?fixed=value',
        'https://cb.example/callback#fragment',
        'https://cb.example:not-a-port/callback',
    ),
)
def test_redirect_uri_rejects_values_that_cannot_be_registered(redirect_uri):
    with pytest.raises(ValueError):
        normalize_redirect_uri(redirect_uri)


def test_redirect_uri_trims_outer_whitespace_but_preserves_the_registered_path():
    assert normalize_redirect_uri('  https://cb.example/callback/  ') == 'https://cb.example/callback/'


def test_facebook_login_is_rejected_until_its_distinct_oauth_flow_exists():
    with pytest.raises(ValueError, match='facebook_login is not implemented'):
        OAuthConfig('APPID', 'SECRET', 'https://cb.example/x', auth_mode='facebook_login')


def test_cli_rejects_unimplemented_facebook_login_before_opening_a_connection_store():
    from typer.testing import CliRunner

    from mediadl.cli import app

    result = CliRunner().invoke(app, [
        'connect', 'instagram',
        '--client-id', 'APPID',
        '--client-secret', 'SECRET',
        '--redirect-uri', 'https://cb.example/x',
        '--auth-mode', 'facebook_login',
    ])

    assert result.exit_code == 2
    assert 'facebook_login is not implemented' in result.output


def test_code_suffix_is_stripped():
    assert clean_code('AQBxSomeCode#_') == 'AQBxSomeCode'


def test_redirect_uri_must_match_exactly():
    validate_redirect_uri('https://cb.example/x', 'https://cb.example/x')
    with pytest.raises(AuthExpiredError):
        validate_redirect_uri('https://cb.example/x/', 'https://cb.example/x')


@respx.mock
async def test_code_exchange_then_long_lived(oauth_client):
    respx.post('https://api.instagram.com/oauth/access_token').mock(
        return_value=httpx.Response(200, json={
            'access_token': 'SHORT', 'user_id': '17841400000000000',
            'permissions': 'instagram_business_basic',
        })
    )
    respx.get('https://graph.instagram.com/access_token').mock(
        return_value=httpx.Response(200, json={
            'access_token': 'LONG', 'token_type': 'bearer', 'expires_in': 5184000,
        })
    )
    oauth = InstagramOAuth(oauth_client, OAuthConfig('APPID', 'SECRET', 'https://cb.example/x'))

    short = await oauth.exchange_code('CODE#_')
    long_lived = await oauth.exchange_long_lived(short.access_token)

    assert short.provider_user_id == '17841400000000000'
    assert long_lived.access_token == 'LONG'
    assert (long_lived.expires_at - datetime.now(UTC)).days >= 59
    assert 'SECRET' not in repr(short)


@respx.mock
async def test_code_exchange_without_read_scope_fails(oauth_client):
    respx.post('https://api.instagram.com/oauth/access_token').mock(
        return_value=httpx.Response(200, json={
            'access_token': 'SHORT', 'user_id': '1', 'permissions': 'instagram_business_manage_comments',
        })
    )
    oauth = InstagramOAuth(oauth_client, OAuthConfig('APPID', 'SECRET', 'https://cb.example/x'))
    with pytest.raises(PermissionMissingError):
        await oauth.exchange_code('CODE')


@respx.mock
async def test_refresh_failure_marks_the_connection_expired(oauth_client, store):
    respx.get('https://graph.instagram.com/refresh_access_token').mock(
        return_value=httpx.Response(400, json={
            'error': {'message': 'token too new', 'code': 190, 'type': 'OAuthException'}
        })
    )
    connection = await store.save(
        make_connection(
            expires_at=datetime.now(UTC) + timedelta(days=5),
            last_refreshed_at=datetime.now(UTC) - timedelta(days=30),
        ),
        TOKEN,
    )
    oauth = InstagramOAuth(oauth_client, OAuthConfig('APPID', 'SECRET', 'https://cb.example/x'))
    provider = CredentialProvider(store, oauth)

    with pytest.raises(AuthExpiredError):
        await provider.acquire('u1', connection.id)

    assert (await store.get(connection.id)).status is ConnectionStatus.EXPIRED


@respx.mock
async def test_successful_refresh_updates_expiry(oauth_client, store):
    respx.get('https://graph.instagram.com/refresh_access_token').mock(
        return_value=httpx.Response(200, json={'access_token': 'REFRESHED', 'expires_in': 5184000})
    )
    connection = await store.save(
        make_connection(
            expires_at=datetime.now(UTC) + timedelta(days=5),
            last_refreshed_at=datetime.now(UTC) - timedelta(days=30),
        ),
        TOKEN,
    )
    oauth = InstagramOAuth(oauth_client, OAuthConfig('APPID', 'SECRET', 'https://cb.example/x'))
    credential = await CredentialProvider(store, oauth).acquire('u1', connection.id)

    assert credential.token == 'REFRESHED'
    assert (await store.get(connection.id)).expires_at > datetime.now(UTC) + timedelta(days=55)


# --- Meta callbacks -------------------------------------------------------
def signed_request(payload: dict, secret: str = APP_SECRET) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature).decode().rstrip('=') + '.' + encoded


def test_valid_signed_request_parses():
    payload = parse_signed_request(
        signed_request({'user_id': '123', 'algorithm': 'HMAC-SHA256'}), APP_SECRET
    )
    assert payload['user_id'] == '123'


@pytest.mark.parametrize(
    'mutate',
    [
        lambda s: signed_request({'user_id': '123'}, 'wrong-secret'),
        lambda s: s[:-3] + 'AAA',
        lambda s: s.replace('.', '.X', 1),
        lambda s: 'not-a-signed-request',
        lambda s: '',
    ],
)
def test_invalid_signed_requests_are_rejected(mutate):
    original = signed_request({'user_id': '123', 'algorithm': 'HMAC-SHA256'})
    with pytest.raises(SignedRequestError):
        parse_signed_request(mutate(original), APP_SECRET)


async def test_deauthorize_revokes_the_connection(store):
    connection = await store.save(make_connection(provider_user_id='555'), TOKEN)
    handler = MetaCallbackHandler(store, APP_SECRET, 'https://app.example/deletion/{code}')

    await handler.deauthorize(signed_request({'user_id': '555', 'algorithm': 'HMAC-SHA256'}))

    assert (await store.get(connection.id)).status is ConnectionStatus.REVOKED


async def test_data_deletion_returns_url_and_confirmation_code(store):
    await store.save(make_connection(provider_user_id='777'), TOKEN)
    handler = MetaCallbackHandler(store, APP_SECRET, 'https://app.example/deletion/{code}')

    response = await handler.delete_data(
        signed_request({'user_id': '777', 'algorithm': 'HMAC-SHA256'})
    )

    assert set(response.as_dict()) == {'url', 'confirmation_code'}
    assert response.confirmation_code in response.url
