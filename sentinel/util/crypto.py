"""
sentinel/util/crypto.py — Fernet-based encryption/decryption for camera
passwords stored in SQLite.

The Fernet key is derived from ARGUS_SECRET_KEY using HKDF-SHA256 so
any arbitrary string (including base64) is accepted as the secret.
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from sentinel.config import settings

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        # Use ARGUS_FERNET_KEY if set (decouples camera passwords from JWT key),
        # otherwise derive from ARGUS_SECRET_KEY for backward compatibility.
        key_material = settings.sentinel_fernet_key or settings.sentinel_secret_key
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"sentinel-nvr-v1",
            info=b"camera-password-encryption",
        )
        derived = hkdf.derive(key_material.encode())
        key = base64.urlsafe_b64encode(derived)
        _fernet = Fernet(key)
    return _fernet


def encrypt_password(plaintext: str) -> str:
    """Encrypt a plaintext password; returns a base64-url-encoded token."""
    if not plaintext:
        return ""
    token = _get_fernet().encrypt(plaintext.encode())
    return token.decode()


def decrypt_password(token: str) -> str:
    """Decrypt a Fernet token back to plaintext."""
    if not token:
        return ""
    plaintext = _get_fernet().decrypt(token.encode())
    return plaintext.decode()
