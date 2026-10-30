"""Graph API error mapping (spec sections 4.5.6, 9.2).

Meta reports errors as ``{"error": {"code", "error_subcode", "message", ...}}``
with HTTP 400 in most cases, so the HTTP status alone is not enough.

Codes marked [OPEN] in the spec are handled defensively: anything
unrecognized becomes a retryable UpstreamError only when the HTTP status says
so, otherwise it fails loudly rather than silently looking like success.
"""
from __future__ import annotations

from typing import Any

from mediadl.errors import (
    AuthExpiredError,
    ErrorCode,
    NotAccessibleError,
    NotFoundError,
    PermissionMissingError,
    RateLimited,
    UpstreamError,
)

#: Token problems: expired, revoked, or invalidated by a password change.
AUTH_CODES = frozenset({190, 102, 463, 467})
#: Missing permission or capability for the requested edge.
PERMISSION_CODES = frozenset({10, 200, 201, 202, 203, 204, 205, 206, 299})
#: Application/user/page throttling. 4 and 17 are app- and user-level limits.
RATE_LIMIT_CODES = frozenset({4, 17, 32, 613, 80001, 80002, 80003, 80004})
#: The object does not exist, or is not visible to this token.
NOT_FOUND_CODES = frozenset({803, 100})
#: Transient server-side failures.
TRANSIENT_CODES = frozenset({1, 2, 341, 368})


def parse_error(payload: Any, status: int) -> Exception:
    error = (payload or {}).get('error', {}) if isinstance(payload, dict) else {}
    code = error.get('code')
    subcode = error.get('error_subcode')
    message = error.get('message') or f'Graph API returned HTTP {status}'
    detail = {
        'graph_code': code,
        'graph_subcode': subcode,
        'status': status,
        'type': error.get('type'),
    }

    if code in AUTH_CODES:
        return AuthExpiredError(message, **detail)
    if code in PERMISSION_CODES:
        return PermissionMissingError(message, **detail)
    if code in RATE_LIMIT_CODES:
        return RateLimited(message, retry_after=None, **detail)
    if code == 100 and subcode in (33, 2207003):
        # "Unsupported get request" / object not visible to this token: the
        # media exists but is not reachable through the authorized account.
        return NotAccessibleError(
            message, code=ErrorCode.NOT_ACCESSIBLE_WITH_AUTHORIZED_ACCOUNT, **detail
        )
    if code in NOT_FOUND_CODES:
        return NotFoundError(message, **detail)
    if code in TRANSIENT_CODES or status >= 500:
        return UpstreamError(message, **detail)
    if status == 429:
        return RateLimited(message, retry_after=None, **detail)
    if status == 401 or status == 403:
        return AuthExpiredError(message, **detail)
    return UpstreamError(message, **detail)
