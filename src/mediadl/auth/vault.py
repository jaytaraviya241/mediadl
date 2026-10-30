"""Token encryption at rest (spec section 6.4).

AES-256-GCM with a random 96-bit nonce per write. The AAD binds a ciphertext
to its connection, principal, and key version, so a row cannot be replayed
into another connection's slot.

The data key lives in the OS keychain. When no keychain is available (a
headless container, say) it falls back to a 0600 file under the app data
directory, which is weaker and says so out loud.
"""
from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mediadl.errors import AuthExpiredError, MediaDLError
from mediadl.obs.logging import get_logger

log = get_logger(__name__)

KEYRING_SERVICE = 'mediadl'
KEYRING_USER = 'vault-data-key'
CURRENT_KEY_VERSION = 1
NONCE_BYTES = 12
KEY_BYTES = 32


class VaultError(MediaDLError):
    user_message = 'Stored credentials could not be read.'


@dataclass(frozen=True, slots=True)
class SealedToken:
    ciphertext: bytes
    nonce: bytes
    key_version: int


def _aad(connection_id: str, principal_id: str, key_version: int) -> bytes:
    return f'{connection_id}‖{principal_id}‖{key_version}'.encode()


class KeyProvider:
    """Supplies the data-encryption key, preferring the OS keychain."""

    def __init__(self, fallback_dir: Path | None = None) -> None:
        self.fallback_dir = fallback_dir
        self._cached: bytes | None = None

    def _from_keyring(self) -> bytes | None:
        try:
            import keyring

            stored = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
            if stored:
                return base64.b64decode(stored)
            key = AESGCM.generate_key(bit_length=256)
            keyring.set_password(KEYRING_SERVICE, KEYRING_USER, base64.b64encode(key).decode())
            return key
        except Exception as exc:
            log.warning('keyring_unavailable', error=type(exc).__name__)
            return None

    def _from_file(self) -> bytes:
        if self.fallback_dir is None:
            raise VaultError('no key storage is available')
        self.fallback_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.fallback_dir / 'vault.key'
        if path.exists():
            return base64.b64decode(path.read_bytes())
        key = AESGCM.generate_key(bit_length=256)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as fh:
            fh.write(base64.b64encode(key))
        log.warning(
            'vault_key_on_disk',
            path=str(path),
            note='OS keychain unavailable; key stored with 0600 permissions',
        )
        return key

    def key(self, version: int = CURRENT_KEY_VERSION) -> bytes:
        if self._cached is None:
            self._cached = self._from_keyring() or self._from_file()
        if len(self._cached) != KEY_BYTES:
            raise VaultError('data key has the wrong length')
        return self._cached


class TokenVault:
    def __init__(self, keys: KeyProvider) -> None:
        self.keys = keys

    def seal(self, plaintext: str, *, connection_id: str, principal_id: str) -> SealedToken:
        key = self.keys.key()
        nonce = secrets.token_bytes(NONCE_BYTES)
        ciphertext = AESGCM(key).encrypt(
            nonce, plaintext.encode('utf-8'),
            _aad(connection_id, principal_id, CURRENT_KEY_VERSION),
        )
        return SealedToken(ciphertext, nonce, CURRENT_KEY_VERSION)

    def open(self, sealed: SealedToken, *, connection_id: str, principal_id: str) -> str:
        key = self.keys.key(sealed.key_version)
        try:
            plaintext = AESGCM(key).decrypt(
                sealed.nonce, sealed.ciphertext,
                _aad(connection_id, principal_id, sealed.key_version),
            )
        except InvalidTag as exc:
            # Wrong key, tampering, or a row moved between connections.
            raise AuthExpiredError(
                'stored credential could not be decrypted', connection_id=connection_id
            ) from exc
        return plaintext.decode('utf-8')

    def rotate(
        self, sealed: SealedToken, *, connection_id: str, principal_id: str
    ) -> SealedToken:
        plaintext = self.open(sealed, connection_id=connection_id, principal_id=principal_id)
        return self.seal(plaintext, connection_id=connection_id, principal_id=principal_id)
