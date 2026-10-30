"""Business Login for Instagram (spec section 6.2).

Verified behaviours this encodes:
  * the authorization code is single use, valid one hour, and arrives with a
    trailing ``#_`` that is not part of the code;
  * ``state`` is mandatory here (the docs call it optional) and single use;
  * the short-lived token is exchanged for a 60-day token immediately;
  * refreshing re-issues the access token itself. There is no refresh token,
    and Meta rejects a refresh for tokens younger than 24 hours.
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address

from mediadl.errors import AuthExpiredError, PermissionMissingError, UpstreamError
from mediadl.net.guarded_client import GuardedHttpClient
from mediadl.obs.logging import get_logger

log = get_logger(__name__)

AUTHORIZE_URL = 'https://www.instagram.com/oauth/authorize'
TOKEN_URL = 'https://api.instagram.com/oauth/access_token'
GRAPH_INSTAGRAM = 'https://graph.instagram.com'
GRAPH_FACEBOOK = 'https://graph.facebook.com'

INSTAGRAM_AUTH_HOSTS = ('instagram.com', 'graph.instagram.com', 'api.instagram.com')
FACEBOOK_AUTH_HOSTS = ('facebook.com', 'graph.facebook.com')

#: The minimum scope that can read the account's own media.
READ_SCOPE_INSTAGRAM_LOGIN = 'instagram_business_basic'
READ_SCOPES_FACEBOOK_LOGIN = ('instagram_basic', 'pages_read_engagement')


@dataclass(frozen=True, slots=True)
class TokenResponse:
    access_token: str
    expires_at: datetime | None
    provider_user_id: str | None = None
    permissions: tuple[str, ...] = ()

    def __repr__(self) -> str:  # never print a token
        return (
            f'TokenResponse(access_token=«redacted», expires_at={self.expires_at!r}, '
            f'provider_user_id={self.provider_user_id!r}, permissions={self.permissions!r})'
        )


@dataclass(frozen=True, slots=True)
class OAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...] = (READ_SCOPE_INSTAGRAM_LOGIN,)
    auth_mode: str = 'instagram_login'

    def __post_init__(self) -> None:
        """Keep OAuth configuration on the one implemented Meta flow.

        The Graph connector has a future-facing ``facebook_login`` capability,
        but its authorization and token exchange are materially different from
        Instagram Login.  Sending it through these endpoints would result in a
        misleading Meta error, so reject it until that flow is implemented.
        """
        if self.auth_mode != 'instagram_login':
            raise ValueError(
                'facebook_login is not implemented. Use Instagram Login with an '
                'Instagram App ID.'
            )
        client_id = self.client_id.strip()
        if not client_id:
            raise ValueError('Instagram App ID is required')
        object.__setattr__(self, 'client_id', client_id)
        object.__setattr__(self, 'redirect_uri', normalize_redirect_uri(self.redirect_uri))

    @property
    def graph_base(self) -> str:
        return GRAPH_INSTAGRAM if self.auth_mode == 'instagram_login' else GRAPH_FACEBOOK

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        return INSTAGRAM_AUTH_HOSTS if self.auth_mode == 'instagram_login' else FACEBOOK_AUTH_HOSTS


def authorize_url(config: OAuthConfig, state: str, *, force_reauth: bool = False) -> str:
    params = {
        'client_id': config.client_id,
        'redirect_uri': config.redirect_uri,
        'response_type': 'code',
        'scope': ','.join(config.scopes),
        'state': state,
    }
    if force_reauth:
        params['force_reauth'] = 'true'
    return f'{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}'


def clean_code(code: str) -> str:
    """Strip the trailing ``#_`` Meta appends to the redirect."""
    return code.split('#')[0].strip()


def normalize_redirect_uri(value: str) -> str:
    """Validate the stable, registered part of an Instagram Login callback.

    Meta compares the supplied URI exactly.  We deliberately retain the
    caller's spelling (including a trailing slash) after trimming accidental
    surrounding whitespace, while rejecting forms that cannot work in this
    local copy/paste flow.
    """
    redirect_uri = value.strip()
    if not redirect_uri:
        raise ValueError('Redirect URI is required')
    if any(char.isspace() for char in redirect_uri):
        raise ValueError('Redirect URI cannot contain whitespace')
    try:
        parts = urllib.parse.urlsplit(redirect_uri)
        # Reading ``port`` forces urllib to reject malformed values such as
        # ``https://example.com:not-a-port/callback``.
        _ = parts.port
    except ValueError as exc:
        raise ValueError('Redirect URI is not a valid HTTPS URL') from exc
    if parts.scheme.lower() != 'https' or not parts.hostname:
        raise ValueError('Redirect URI must be an HTTPS URL with a public host')
    if parts.username is not None or parts.password is not None:
        raise ValueError('Redirect URI cannot include a username or password')
    if parts.query or parts.fragment:
        raise ValueError('Redirect URI must not include a query string or fragment')

    hostname = parts.hostname.rstrip('.').lower()
    try:
        address = ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and address.is_loopback:
        raise ValueError(
            'Redirect URI cannot use localhost; register a public HTTPS callback URL'
        )
    if hostname == 'localhost':
        raise ValueError(
            'Redirect URI cannot use localhost; register a public HTTPS callback URL'
        )
    return redirect_uri


def validate_redirect_uri(received: str, expected: str) -> None:
    """Exact string match, as the documentation requires."""
    try:
        normalized_received = normalize_redirect_uri(received)
        normalized_expected = normalize_redirect_uri(expected)
    except ValueError as exc:
        raise AuthExpiredError('redirect_uri is not a valid registered HTTPS callback URL') from exc
    if normalized_received != normalized_expected:
        raise AuthExpiredError('redirect_uri does not match the configured value')


class InstagramOAuth:
    def __init__(self, client: GuardedHttpClient, config: OAuthConfig) -> None:
        self.client = client
        self.config = config

    async def exchange_code(self, code: str) -> TokenResponse:
        body = urllib.parse.urlencode({
            'client_id': self.config.client_id,
            'client_secret': self.config.client_secret,
            'grant_type': 'authorization_code',
            'redirect_uri': self.config.redirect_uri,
            'code': clean_code(code),
        }).encode()
        response = await self.client.request(
            'POST', TOKEN_URL,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            content=body,
            allowed_hosts=self.config.allowed_hosts,
        )
        data = _json_or_raise(response, 'code exchange')
        permissions = _permissions(data)
        self._require_read_scope(permissions)
        return TokenResponse(
            access_token=str(data['access_token']),
            expires_at=None,  # short-lived; exchanged immediately
            provider_user_id=str(data.get('user_id')) if data.get('user_id') else None,
            permissions=permissions,
        )

    async def exchange_long_lived(self, short_lived: str) -> TokenResponse:
        response = await self.client.request(
            'GET', f'{self.config.graph_base}/access_token',
            params={
                'grant_type': 'ig_exchange_token',
                'client_secret': self.config.client_secret,
                'access_token': short_lived,
            },
            allowed_hosts=self.config.allowed_hosts,
        )
        data = _json_or_raise(response, 'long-lived exchange')
        return TokenResponse(
            access_token=str(data['access_token']),
            expires_at=_expiry(data.get('expires_in')),
        )

    async def refresh(self, long_lived: str) -> TokenResponse:
        response = await self.client.request(
            'GET', f'{self.config.graph_base}/refresh_access_token',
            params={'grant_type': 'ig_refresh_token', 'access_token': long_lived},
            allowed_hosts=self.config.allowed_hosts,
        )
        data = _json_or_raise(response, 'token refresh')
        return TokenResponse(
            access_token=str(data['access_token']),
            expires_at=_expiry(data.get('expires_in')),
        )

    def _require_read_scope(self, permissions: tuple[str, ...]) -> None:
        if not permissions:
            return  # nothing reported; the first Graph call will tell us
        if self.config.auth_mode == 'instagram_login':
            if READ_SCOPE_INSTAGRAM_LOGIN not in permissions:
                raise PermissionMissingError(
                    'the connection is missing instagram_business_basic',
                    granted=list(permissions), required=[READ_SCOPE_INSTAGRAM_LOGIN],
                )
        else:
            missing = [s for s in READ_SCOPES_FACEBOOK_LOGIN if s not in permissions]
            if missing:
                raise PermissionMissingError(
                    'the connection is missing required permissions',
                    granted=list(permissions), required=missing,
                )


def _permissions(data: dict) -> tuple[str, ...]:
    raw = data.get('permissions')
    if isinstance(raw, str):
        return tuple(p.strip() for p in raw.split(',') if p.strip())
    if isinstance(raw, list):
        return tuple(str(p) for p in raw)
    return ()


def _expiry(expires_in: object) -> datetime | None:
    if isinstance(expires_in, (int, float)):
        return datetime.now(UTC) + timedelta(seconds=float(expires_in))
    if isinstance(expires_in, str) and expires_in.isdigit():
        return datetime.now(UTC) + timedelta(seconds=int(expires_in))
    return None


def _json_or_raise(response, operation: str) -> dict:
    try:
        data = response.json()
    except ValueError as exc:
        raise UpstreamError(f'{operation} returned a non-JSON body',
                            status=response.status_code) from exc
    if response.status_code >= 400 or 'error' in data or 'error_message' in data:
        message = (
            data.get('error_message')
            or (data.get('error') or {}).get('message')
            if isinstance(data.get('error'), dict) else data.get('error')
        )
        raise AuthExpiredError(
            f'{operation} failed: {message or response.status_code}',
            status=response.status_code,
        )
    if 'access_token' not in data:
        raise UpstreamError(f'{operation} response had no access_token')
    return data
