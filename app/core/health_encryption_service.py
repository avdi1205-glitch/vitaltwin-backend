"""Health Encryption Service — authenticated encryption for external OAuth
tokens at rest (Google Health access/refresh tokens).

Uses Fernet (AES-128-CBC + HMAC-SHA256, authenticated encryption) from the
`cryptography` package — already a project dependency, no new package added,
and deliberately NOT a custom/home-grown cipher.

Key versioning is prepared (`HEALTH_TOKEN_ENCRYPTION_KEY_VERSION`) so a
future key rotation can decrypt old rows with the old key while new rows use
a new one — today only version 1 exists.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

CURRENT_KEY_VERSION = int(os.getenv("HEALTH_TOKEN_ENCRYPTION_KEY_VERSION", "1").strip() or "1")


class EncryptionNotConfiguredError(Exception):
    pass


class EncryptionFailedError(Exception):
    pass


def _fernet_for_version(version: int) -> Fernet:
    # Only one key today (HEALTH_TOKEN_ENCRYPTION_KEY). When rotation is
    # introduced, additional versioned env vars (e.g.
    # HEALTH_TOKEN_ENCRYPTION_KEY_V2) would be looked up here by `version`.
    if version != CURRENT_KEY_VERSION:
        raise EncryptionNotConfiguredError(
            f"Kein Schlüssel für Verschlüsselungsversion {version} hinterlegt — Key-Rotation nicht vorbereitet für diese Version."
        )
    key = os.getenv("HEALTH_TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        raise EncryptionNotConfiguredError(
            "HEALTH_TOKEN_ENCRYPTION_KEY ist nicht gesetzt — Tokens können nicht sicher gespeichert werden."
        )
    try:
        return Fernet(key.encode("utf-8"))
    except Exception as exc:
        raise EncryptionNotConfiguredError(
            "HEALTH_TOKEN_ENCRYPTION_KEY ist ungültig (kein gültiger Fernet-Key, mit "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` erzeugen)."
        ) from exc


def encrypt_secret(value: str, *, key_version: int = CURRENT_KEY_VERSION) -> str:
    """Never logs, never echoes the plaintext value in any exception."""
    return _fernet_for_version(key_version).encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: str, *, key_version: int = CURRENT_KEY_VERSION) -> str:
    try:
        return _fernet_for_version(key_version).decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise EncryptionFailedError("Gespeicherter Token konnte nicht entschlüsselt werden.") from exc


def rotate_encrypted_secret(value: str, *, from_version: int, to_version: int) -> str:
    """Decrypts with the old key version and re-encrypts with the new one.
    Not exercised in production yet (only one key version exists today) —
    provided so a future rotation doesn't require re-deriving this logic."""
    plaintext = decrypt_secret(value, key_version=from_version)
    return encrypt_secret(plaintext, key_version=to_version)
