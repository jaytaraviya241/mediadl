"""What credentials each connector needs, and their live status.

The dashboard renders this verbatim, so the answer to "what do I need, where
do I put it, where do I get it" lives in one place instead of being split
between docs and UI copy.

Only requirements that follow from the *documented* APIs appear here. There
is deliberately no entry for cookies, session ids, or app-impersonation
values: this program has no code that could consume them.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

Status = Literal['ready', 'action_required', 'not_configured', 'optional']


@dataclass(frozen=True, slots=True)
class CredentialField:
    name: str
    label: str
    where_to_add: str
    where_to_obtain: str
    secret: bool = False
    example: str = ''
    notes: str = ''


@dataclass(frozen=True, slots=True)
class CredentialRequirement:
    connector_id: str
    title: str
    summary: str
    #: 'none' means the connector needs no credential at all.
    kind: Literal['none', 'oauth', 'tool']
    fields: tuple[CredentialField, ...] = ()
    prerequisites: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    docs: tuple[dict[str, str], ...] = ()
    limits: tuple[str, ...] = ()
    status: Status = 'not_configured'
    detail: str = ''

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


NO_CREDENTIAL_CONNECTORS: dict[str, tuple[str, str]] = {
    'direct_url': (
        'Direct media URL',
        'Fetches a URL that returns media bytes. No credential exists to add; '
        'you are responsible for having the right to fetch what you point it at.',
    ),
    'user_files': (
        'Your own files',
        'Imports files and folders you already have, including an official '
        '"Download your information" export. Providing the file is the authorization.',
    ),
    'stream_manifest': (
        'HLS / DASH manifest',
        'Downloads an unprotected on-demand .m3u8 or .mpd stream. DRM-protected '
        'streams are refused before any segment is requested.',
    ),
}

INSTAGRAM_FIELDS: tuple[CredentialField, ...] = (
    CredentialField(
        name='client_id',
        label='Instagram App ID',
        where_to_add=(
            'Dashboard → Connections → Connect Instagram, or '
            '[instagram_graph] client_id in your TOML config, or '
            'MEDIADL_INSTAGRAM_GRAPH__CLIENT_ID'
        ),
        where_to_obtain=(
            'developers.facebook.com → your app → Instagram → API setup with '
            'Instagram login → step 3, "Instagram app ID"'
        ),
        example='1234567890123456',
        notes='Not a secret; it appears in the authorization URL.',
    ),
    CredentialField(
        name='client_secret',
        label='Instagram App Secret',
        secret=True,
        where_to_add=(
            'Dashboard → Connections → paste at the final step only. It is used '
            'once and never written to disk.'
        ),
        where_to_obtain=(
            'Same screen as the App ID: "Instagram app secret" → Show. Treat it '
            'like a password.'
        ),
        notes=(
            'Needed only to exchange the authorization code and to obtain the '
            '60-day token. Refreshing that token later uses the token itself, '
            'so mediadl never stores the secret.'
        ),
    ),
    CredentialField(
        name='redirect_uri',
        label='Redirect URI',
        where_to_add=(
            'Dashboard → Connections → Connect Instagram, or '
            '[instagram_graph] redirect_uri in your TOML config'
        ),
        where_to_obtain=(
            'You choose it, then register the exact string under Instagram → '
            'API setup with Instagram login → "Set up Instagram business login" '
            '→ OAuth redirect URIs'
        ),
        example='https://your-domain.example/auth/instagram/callback',
        notes=(
            'Must be HTTPS and must match byte-for-byte. Meta does not document '
            'loopback redirects for this flow, so a plain http://localhost URI '
            'will not work.'
        ),
    ),
)

INSTAGRAM_PREREQUISITES: tuple[str, ...] = (
    'An Instagram **professional** account (Business or Creator). Personal '
    'accounts lost API access when the Basic Display API shut down on 2024-12-04.',
    'A Meta app with the Instagram product added, in Development mode to start.',
    'Your Instagram account added to the app with a role (Standard Access), or '
    'App Review plus Business Verification for Advanced Access if you intend to '
    'serve accounts you do not own or manage.',
)

INSTAGRAM_STEPS: tuple[str, ...] = (
    'Convert your Instagram account to Professional in the Instagram app: '
    'Settings → Account type and tools → Switch to professional account.',
    'Create a Meta app at developers.facebook.com/apps with the "Business" type, '
    'then add the Instagram product.',
    'Open Instagram → API setup with Instagram login, and copy the Instagram app '
    'ID and app secret from step 3.',
    'In the same section, register your redirect URI under "Set up Instagram '
    'business login" → Business login settings → OAuth redirect URIs.',
    'Back in this dashboard, enter the App ID and redirect URI and press Start. '
    'Approve access in the browser window that opens.',
    'Copy the full URL you land on after approving, including ?code=..., paste it '
    'back here with the app secret, and press Finish.',
)

INSTAGRAM_DOCS: tuple[dict[str, str], ...] = (
    {'label': 'Instagram Platform overview',
     'url': 'https://developers.facebook.com/docs/instagram-platform/overview/'},
    {'label': 'Business Login for Instagram (the flow used here)',
     'url': 'https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login'},
    {'label': 'IG Media reference (the fields mediadl reads)',
     'url': 'https://developers.facebook.com/docs/instagram-platform/reference/instagram-media'},
    {'label': 'App dashboard (create the app, read the ID and secret)',
     'url': 'https://developers.facebook.com/apps'},
)

INSTAGRAM_LIMITS: tuple[str, ...] = (
    'Only media owned by the connected professional account can be downloaded. '
    'Someone else\'s post returns NOT_ACCESSIBLE_WITH_AUTHORIZED_ACCOUNT.',
    'The user media edge returns at most the 10,000 most recent items; older '
    'posts report NOT_IN_INDEX_WINDOW.',
    'Meta omits media_url for content flagged for copyright. That asset reports '
    'UNAVAILABLE_COPYRIGHT while its siblings in a carousel still download.',
    'The stored token lasts 60 days and is refreshed automatically once it is at '
    'least 24 hours old.',
)


def instagram_requirement(
    *, connections: list[dict[str, Any]], configured: dict[str, Any]
) -> CredentialRequirement:
    active = [c for c in connections if c['status'] == 'active']
    expiring = [c for c in active if c.get('expires_in_days') is not None
                and c['expires_in_days'] <= 7]
    needs_attention = [c for c in connections if c['status'] in ('expired', 'revoked')]

    if active and not expiring:
        status: Status = 'ready'
        detail = f'{len(active)} account connected.'
    elif active and expiring:
        status = 'action_required'
        detail = 'A token expires within 7 days; it refreshes automatically on next use.'
    elif needs_attention:
        status = 'action_required'
        detail = 'A connection expired or was revoked. Reconnect to continue.'
    else:
        status = 'not_configured'
        detail = 'No Instagram account connected yet.'

    return CredentialRequirement(
        connector_id='instagram_graph',
        title='Instagram (official Graph API)',
        summary=(
            'Downloads media owned by the professional account that authorizes '
            'this app, using Instagram Business Login. Requests only the '
            'instagram_business_basic scope: read access to your own media, and '
            'nothing else.'
        ),
        kind='oauth',
        fields=INSTAGRAM_FIELDS,
        prerequisites=INSTAGRAM_PREREQUISITES,
        steps=INSTAGRAM_STEPS,
        docs=INSTAGRAM_DOCS,
        limits=INSTAGRAM_LIMITS,
        status=status,
        detail=detail,
    )


def ffmpeg_requirement(*, available: bool, path: str, license_summary: str) -> CredentialRequirement:
    return CredentialRequirement(
        connector_id='ffmpeg',
        title='FFmpeg and FFprobe (a tool, not a credential)',
        summary=(
            'Needed to merge separate audio and video, remux, transcode, probe '
            'downloaded files, and assemble HLS/DASH segments. Progressive '
            'single-file downloads work without it.'
        ),
        kind='tool',
        fields=(
            CredentialField(
                name='ffmpeg_path',
                label='FFmpeg location',
                where_to_add='[ffmpeg] path in your TOML config, or leave empty to use PATH',
                where_to_obtain='ffmpeg.org/download.html, or "brew install ffmpeg" on macOS',
                example='/opt/homebrew/bin',
                notes=(
                    'mediadl reports the build\'s licence. A GPL build is fine to '
                    'use; redistributing one inside a product carries GPL '
                    'obligations, so ship an LGPL build if you bundle FFmpeg.'
                ),
            ),
        ),
        status='ready' if available else 'action_required',
        detail=(
            f'{path} — {license_summary}' if available
            else 'Not found. Merging, remuxing, transcoding, probing and '
                 'segmented streams are unavailable.'
        ),
    )


def no_credential_requirement(connector_id: str) -> CredentialRequirement:
    title, summary = NO_CREDENTIAL_CONNECTORS.get(
        connector_id, (connector_id, 'No credential required.')
    )
    return CredentialRequirement(
        connector_id=connector_id,
        title=title,
        summary=summary,
        kind='none',
        status='ready',
        detail='Ready to use; nothing to configure.',
    )


#: Things people often expect to supply, and why this program has no field for them.
NOT_REQUIRED: tuple[dict[str, str], ...] = (
    {
        'item': 'Your Instagram password',
        'why': 'mediadl never sees it. Authorization happens on Meta\'s own login '
               'page and returns a scoped token.',
    },
    {
        'item': 'A browser session cookie (sessionid)',
        'why': 'Not supported, by design. A session cookie is a bearer credential '
               'for your entire account, and automating a personal session is not '
               'an authorized integration. There is no code path that accepts one.',
    },
    {
        'item': 'A proxy, VPN, or user-agent override',
        'why': 'Geo-restriction evasion and identity rotation are not implemented.',
    },
    {
        'item': 'DRM keys or a Widevine CDM',
        'why': 'Protected streams are refused before any segment is fetched.',
    },
)
