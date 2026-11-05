"""Authenticated symmetric encryption for secrets at rest (webhook secrets, connections).

Fernet = AES-128-CBC + HMAC-SHA256 with a timestamp: tampering is detected, not just
hidden. ``MultiFernet`` encrypts with the first key and decrypts with any, which gives
zero-downtime key rotation:

1. Prepend a new key to ``NUROFLOW_ENCRYPTION_KEYS`` and deploy.
2. Re-encrypt stored values with :meth:`SecretBox.rotate` (a background job).
3. Remove the old key.
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from nuroflow.core.config import Settings


class DecryptionError(Exception):
    pass


class SecretBox:
    def __init__(self, keys: list[bytes]) -> None:
        if not keys:
            raise ValueError("at least one encryption key is required")
        self._fernet = MultiFernet([Fernet(k) for k in keys])

    @classmethod
    def from_settings(cls, settings: Settings) -> SecretBox:
        keys = [k.get_secret_value().encode() for k in settings.encryption_keys]
        if not keys:
            keys = [base64.urlsafe_b64encode(settings.derive_key("fernet"))]
        return cls(keys)

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, token: bytes) -> str:
        try:
            return self._fernet.decrypt(token).decode()
        except InvalidToken as exc:
            raise DecryptionError(
                "ciphertext is invalid or was encrypted with an unknown key"
            ) from exc

    def rotate(self, token: bytes) -> bytes:
        return self._fernet.rotate(token)
