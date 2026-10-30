"""Redaction filter (spec sections 6.4, 9.3, Phase 0 acceptance)."""
from __future__ import annotations

import io
import json
import logging

import pytest

from mediadl.net.redact import (
    MASK,
    redact_argv,
    redact_headers,
    redact_mapping,
    redact_text,
    redact_url,
    url_fingerprint,
)
from mediadl.obs.logging import configure, get_logger

SECRETS = [
    'access_token=IGAAxyz123',
    'client_secret=abcdef0123456789',
    'code=AQBx-authorization-code',
    '?sig=deadbeef&oh=00_AT-abc&oe=66AABBCC',
    'Authorization: Bearer ya29.a0AfH6SM',
    'Cookie: sessionid=9876543210%3Aabc',
    'Set-Cookie: csrftoken=abc; Path=/',
    '{"access_token": "IGAAxyz", "user_id": "123"}',
    'X-Amz-Signature=abc123&X-Amz-Credential=AKIA',
    'refresh_token=1//0gzzz',
    'signed_request=abc.def',
    'api_key=sk-live-1234',
    'password=hunter2',
    'token=abcd1234',
    'private_key=-----BEGIN',
    'https://cdn.example.com/v/t51.jpg?oh=00_AT&oe=66AA',
    'state=csrf-state-value',
    'id_token=eyJhbGciOi',
    'hmac=abcdef',
    'secret=top-secret-value',
]


@pytest.mark.parametrize('sample', SECRETS)
def test_secret_values_are_masked(sample):
    masked = redact_text(sample)
    # The value must be gone; the key may stay so logs remain readable.
    for fragment in ('IGAAxyz123', 'abcdef0123456789', 'AQBx-authorization-code', 'deadbeef',
                     'ya29.a0AfH6SM', '9876543210', 'AKIA', '1//0gzzz', 'sk-live-1234',
                     'hunter2', 'abcd1234', 'csrf-state-value', 'eyJhbGciOi', 'top-secret-value'):
        assert fragment not in masked, f'{fragment!r} survived redaction of {sample!r}'


def test_redact_url_drops_the_query_string():
    url = 'https://scontent.cdninstagram.com/v/t51/photo.jpg?oh=00_SECRET&oe=66AA'
    redacted = redact_url(url)
    assert 'SECRET' not in redacted
    assert redacted.startswith('https://scontent.cdninstagram.com/v/t51/photo.jpg')


def test_url_fingerprint_is_stable_and_not_reversible():
    url = 'https://cdn.example.com/a/b/c.jpg?sig=abc'
    assert url_fingerprint(url) == url_fingerprint('https://cdn.example.com/a/b/c.jpg?sig=zzz')
    assert 'a/b/c.jpg' not in url_fingerprint(url)


def test_redact_mapping_is_recursive():
    payload = {
        'ok': 'visible',
        'access_token': 'SECRET',
        'nested': {'client_secret': 'SECRET2', 'list': [{'code': 'SECRET3'}]},
        'message': 'failed with access_token=SECRET4',
    }
    cleaned = redact_mapping(payload)
    dumped = json.dumps(cleaned)
    assert 'SECRET' not in dumped
    assert cleaned['ok'] == 'visible'


def test_redact_headers_masks_authorization_and_cookies():
    headers = redact_headers({'Authorization': 'Bearer x', 'Cookie': 'a=b', 'Accept': '*/*'})
    assert headers['Authorization'] == MASK
    assert headers['Cookie'] == MASK
    assert headers['Accept'] == '*/*'


def test_redact_argv_masks_urls_and_secrets():
    argv = redact_argv(['ffmpeg', '-i', 'https://cdn/x.mp4?sig=SECRET', '-metadata', 'token=SECRET2'])
    assert 'SECRET' not in ' '.join(argv)


def test_logging_filter_masks_message_and_extras():
    buffer = io.StringIO()
    configure('DEBUG', json_output=True, stream=buffer)
    log = get_logger('test.redaction')
    log.info(
        'request_failed',
        url='https://graph.instagram.com/me/media?access_token=IGAA_SECRET',
        detail={'client_secret': 'SECRET2'},
    )
    output = buffer.getvalue()
    assert 'IGAA_SECRET' not in output
    assert 'SECRET2' not in output
    assert 'request_failed' in output
    logging.getLogger().handlers.clear()


def test_error_public_payload_is_redacted():
    from mediadl.errors import UpstreamError

    exc = UpstreamError('boom', url='https://x/y?access_token=SECRET', client_secret='SECRET2')
    payload = json.dumps(dict(exc.public()))
    assert 'SECRET' not in payload
