"""Encryption of secrets at rest."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class TokenCipher:
    """Fernet encryption for OAuth tokens. The key comes from ``FERNET_KEY``."""

    def __init__(self, key: str) -> None:
        if not key:
            raise ValueError("FERNET_KEY is not set; generate one with Fernet.generate_key()")
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("token could not be decrypted; was FERNET_KEY changed?") from exc
