"""Deauthorize and data-deletion callbacks (spec section 6.2).

Meta POSTs a ``signed_request``: base64url(HMAC-SHA256 signature) + '.' +
base64url(payload JSON), signed with the app secret. The comparison is
constant time, and an unverified payload is never acted on.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any

from mediadl.errors import MediaDLError


class SignedRequestError(MediaDLError):
    user_message = 'The callback could not be verified.'


def _b64url_decode(segment: str) -> bytes:
    padding = '=' * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def parse_signed_request(signed_request: str, app_secret: str) -> dict[str, Any]:
    """Verify and decode. Raises unless the signature matches exactly."""
    if not signed_request or '.' not in signed_request:
        raise SignedRequestError('malformed signed_request')
    encoded_sig, _, encoded_payload = signed_request.partition('.')
    try:
        signature = _b64url_decode(encoded_sig)
        payload_bytes = _b64url_decode(encoded_payload)
        payload = json.loads(payload_bytes)
    except (ValueError, json.JSONDecodeError) as exc:
        raise SignedRequestError('signed_request could not be decoded') from exc

    algorithm = str(payload.get('algorithm', '')).upper()
    if algorithm not in ('HMAC-SHA256', ''):
        raise SignedRequestError(f'unsupported signature algorithm {algorithm!r}')

    expected = hmac.new(
        app_secret.encode('utf-8'), encoded_payload.encode('utf-8'), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected, signature):
        raise SignedRequestError('signed_request signature did not verify')
    return payload


@dataclass(frozen=True, slots=True)
class DeletionResponse:
    url: str
    confirmation_code: str

    def as_dict(self) -> dict[str, str]:
        return {'url': self.url, 'confirmation_code': self.confirmation_code}


def new_confirmation_code() -> str:
    return secrets.token_hex(16)


class MetaCallbackHandler:
    """Implements both callbacks against the connection store."""

    def __init__(self, store, app_secret: str, status_url_template: str) -> None:
        self.store = store
        self.app_secret = app_secret
        self.status_url_template = status_url_template

    async def deauthorize(self, signed_request: str) -> None:
        payload = parse_signed_request(signed_request, self.app_secret)
        provider_user_id = str(payload.get('user_id', ''))
        await self._revoke_for_user(provider_user_id, action='deauthorized')

    async def delete_data(self, signed_request: str) -> DeletionResponse:
        payload = parse_signed_request(signed_request, self.app_secret)
        provider_user_id = str(payload.get('user_id', ''))
        await self._revoke_for_user(provider_user_id, action='data_deletion_requested')
        code = new_confirmation_code()
        await self.store.audit(
            None, None, 'data_deletion', {'provider_user_id': provider_user_id, 'code': code}
        )
        return DeletionResponse(
            url=self.status_url_template.format(code=code), confirmation_code=code
        )

    async def _revoke_for_user(self, provider_user_id: str, *, action: str) -> None:
        from mediadl.auth.connections import ConnectionStatus

        def _find() -> list[str]:
            rows = self.store._conn.execute(
                'SELECT id, principal_id FROM connections WHERE provider_user_id = ?',
                (provider_user_id,),
            ).fetchall()
            return [(r['id'], r['principal_id']) for r in rows]

        import asyncio

        matches = await asyncio.to_thread(_find)
        for connection_id, principal_id in matches:
            await self.store.set_status(connection_id, ConnectionStatus.REVOKED)
            await self.store.audit(principal_id, connection_id, action)
