"""
tests/test_crypto.py — Crypto round-trip tests.
"""

import os
import pytest

# Set a test secret before importing sentinel modules.
os.environ.setdefault("SENTINEL_SECRET_KEY", "test-secret-key-for-unit-tests-only")


def test_encrypt_decrypt_roundtrip():
    from sentinel.util.crypto import encrypt_password, decrypt_password

    plaintext = "MyS3cur3P@ss!"
    token = encrypt_password(plaintext)
    assert token != plaintext, "Token should not equal plaintext"
    recovered = decrypt_password(token)
    assert recovered == plaintext


def test_encrypt_empty():
    from sentinel.util.crypto import encrypt_password, decrypt_password

    assert encrypt_password("") == ""
    assert decrypt_password("") == ""


def test_different_plaintexts_different_tokens():
    from sentinel.util.crypto import encrypt_password

    t1 = encrypt_password("password1")
    t2 = encrypt_password("password2")
    assert t1 != t2


def test_token_is_string():
    from sentinel.util.crypto import encrypt_password

    token = encrypt_password("hello")
    assert isinstance(token, str)


def test_decrypt_invalid_raises():
    from cryptography.fernet import InvalidToken
    from sentinel.util.crypto import decrypt_password

    with pytest.raises(Exception):
        decrypt_password("not-a-valid-token")
