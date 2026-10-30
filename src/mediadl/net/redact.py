"""Redaction helpers (spec sections 6.4, 9.3).

Secrets must never reach logs, traces, sidecars, or error payloads. The
filters here are deliberately broad: a false positive costs a masked log
line, a false negative costs a credential.
"""
from __future__ import annotations

import hashlib
import re
import urllib.parse
from collections.abc import Mapping
from typing import Any

MASK = '«redacted»'

#: Query-string / form / JSON keys whose values are always secrets.
SECRET_KEYS = frozenset({
    'access_token', 'client_secret', 'app_secret', 'code', 'refresh_token', 'id_token',
    'password', 'passwd', 'secret', 'token', 'api_key', 'apikey', 'authorization',
    'cookie', 'set-cookie', 'session', 'sessionid', 'signed_request', 'state',
    'sig', 'signature', 'oh', 'oe', 'hmac', 'x-amz-signature', 'x-amz-credential',
    'x-amz-security-token', 'key', 'private_key',
})

_SECRET_KEY_RE = '|'.join(sorted((re.escape(k) for k in SECRET_KEYS), key=len, reverse=True))

#: ``access_token=abc`` in a URL, form body, or free-text message.
_QUERY_PARAM_RE = re.compile(rf'(?i)\b({_SECRET_KEY_RE})=([^\s&"\'<>\\]+)')
#: ``"access_token": "abc"`` in JSON.
_JSON_FIELD_RE = re.compile(rf'(?i)"({_SECRET_KEY_RE})"\s*:\s*"[^"]*"')
#: ``Authorization: Bearer abc`` in a header dump.
_HEADER_RE = re.compile(rf'(?im)^({_SECRET_KEY_RE})\s*:\s*.+$')
#: Long opaque Meta-style tokens that appear without a key name.
_BEARER_RE = re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}')


def redact_text(text: str) -> str:
    """Mask secrets in arbitrary text (log messages, exception strings)."""
    if not text:
        return text
    out = _JSON_FIELD_RE.sub(lambda m: f'"{m.group(1)}": "{MASK}"', text)
    out = _QUERY_PARAM_RE.sub(lambda m: f'{m.group(1)}={MASK}', out)
    out = _HEADER_RE.sub(lambda m: f'{m.group(1)}: {MASK}', out)
    out = _BEARER_RE.sub(f'Bearer {MASK}', out)
    return out


def redact_url(url: str) -> str:
    """Drop the query string entirely; keep scheme, host, and path."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return MASK
    if not parts.scheme and not parts.netloc:
        return redact_text(url)
    host = parts.hostname or ''
    port = f':{parts.port}' if parts.port and parts.port not in (80, 443) else ''
    suffix = '?' + MASK if parts.query else ''
    return f'{parts.scheme}://{host}{port}{parts.path}{suffix}'


def url_fingerprint(url: str) -> str:
    """Stable, non-reversible identity for a URL: host + hashed path.

    Used in logs and sidecars where the full URL (often signed) must not appear.
    """
    parts = urllib.parse.urlsplit(url)
    digest = hashlib.sha256(parts.path.encode('utf-8')).hexdigest()[:12]
    return f'{parts.hostname or "?"}/{digest}'


def redact_headers(headers: Mapping[str, Any]) -> dict[str, Any]:
    return {
        k: (MASK if k.lower() in SECRET_KEYS else v)
        for k, v in headers.items()
    }


def redact_mapping(data: Mapping[str, Any], *, _depth: int = 0) -> dict[str, Any]:
    """Recursively mask secret-valued keys and scrub secrets from strings."""
    if _depth > 6:
        return {'...': 'truncated'}
    out: dict[str, Any] = {}
    for key, value in data.items():
        if str(key).lower() in SECRET_KEYS:
            out[key] = MASK
        elif isinstance(value, Mapping):
            out[key] = redact_mapping(value, _depth=_depth + 1)
        elif isinstance(value, (list, tuple)):
            out[key] = [
                redact_mapping(v, _depth=_depth + 1) if isinstance(v, Mapping)
                else redact_text(v) if isinstance(v, str) else v
                for v in value
            ]
        elif isinstance(value, str):
            out[key] = redact_text(value)
        else:
            out[key] = value
    return out


def redact_argv(argv: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Mask secrets in a subprocess argument list before recording it."""
    return tuple(redact_text(redact_url(a) if '://' in a else a) for a in argv)
