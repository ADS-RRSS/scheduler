"""Encryption of OAuth tokens at rest.

Tokens never touch the database in plaintext. The Fernet key lives either in
APP_ENCRYPTION_KEY or in .secrets/encryption.key (created with mode 0600).
"""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings

_KEY_FILENAME = "encryption.key"
_fernet: Fernet | None = None


def _load_or_create_key() -> bytes:
    settings = get_settings()
    if settings.app_encryption_key:
        return settings.app_encryption_key.encode()

    key_path = Path(settings.secrets_dir) / _KEY_FILENAME
    if key_path.exists():
        return key_path.read_bytes().strip()

    key = Fernet.generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    # Create with restrictive permissions from the start (no race window).
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def encrypt(plaintext: str | None) -> str | None:
    if plaintext is None:
        return None
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        # Key rotated or DB copied between machines: treat as "no token".
        return None


def reset_cache() -> None:
    """Testing hook."""
    global _fernet
    _fernet = None
