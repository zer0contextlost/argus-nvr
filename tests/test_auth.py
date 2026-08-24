"""
tests/test_auth.py — Phase 7 auth unit tests.

Covers:
  - Password hashing: round-trip, wrong password fails
  - JWT: issue/verify, tampered token rejected, expired token rejected
  - Auth dependency: no-token → 401, valid token → 200
  - SMTP password: encrypted at rest, stored value != plaintext, GET masks it
  - First-run admin bootstrap: creates exactly one admin user
"""

from __future__ import annotations

import os
import time

import pytest

# Set test secret before importing any sentinel modules.
os.environ.setdefault("SENTINEL_SECRET_KEY", "test-secret-key-for-unit-tests-32x")
# Prevent accidental writes to the real DB.
os.environ.setdefault("SENTINEL_DB_PATH", ":memory:")


# ===========================================================================
# Password hashing tests
# ===========================================================================

class TestPasswordHashing:
    def test_hash_is_not_plaintext(self):
        from sentinel.util.auth import hash_password
        h = hash_password("supersecret")
        assert h != "supersecret"
        assert len(h) > 20

    def test_roundtrip_correct_password(self):
        from sentinel.util.auth import hash_password, verify_password
        h = hash_password("CorrectPassword123!")
        assert verify_password("CorrectPassword123!", h) is True

    def test_wrong_password_fails(self):
        from sentinel.util.auth import hash_password, verify_password
        h = hash_password("RealPassword")
        assert verify_password("WrongPassword", h) is False

    def test_empty_password_wrong(self):
        from sentinel.util.auth import hash_password, verify_password
        h = hash_password("nonempty")
        assert verify_password("", h) is False

    def test_two_hashes_of_same_password_differ(self):
        """argon2 uses a random salt — same plaintext yields different hashes."""
        from sentinel.util.auth import hash_password
        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2


# ===========================================================================
# JWT token tests
# ===========================================================================

class TestJWT:
    def test_create_and_decode(self):
        from sentinel.util.auth import create_access_token, decode_access_token
        token = create_access_token({"sub": "1", "username": "alice", "role": "admin"})
        payload = decode_access_token(token)
        assert payload["sub"] == "1"
        assert payload["username"] == "alice"
        assert payload["role"] == "admin"

    def test_tampered_token_rejected(self):
        import jwt as _jwt
        from sentinel.util.auth import create_access_token, decode_access_token
        token = create_access_token({"sub": "1", "username": "alice"})
        # Flip one character in the signature.
        parts = token.split(".")
        assert len(parts) == 3
        sig = parts[2]
        # Alter the signature slightly.
        flipped = sig[:-1] + ("A" if sig[-1] != "A" else "B")
        bad_token = ".".join(parts[:2] + [flipped])
        with pytest.raises(_jwt.InvalidTokenError):
            decode_access_token(bad_token)

    def test_expired_token_rejected(self):
        from datetime import timedelta
        import jwt as _jwt
        from sentinel.util.auth import create_access_token, decode_access_token
        token = create_access_token(
            {"sub": "1"},
            expires_delta=timedelta(seconds=-1),  # already expired
        )
        with pytest.raises(_jwt.ExpiredSignatureError):
            decode_access_token(token)

    def test_wrong_secret_rejected(self):
        import jwt as _jwt
        from sentinel.config import settings
        from datetime import datetime, timezone, timedelta
        token = _jwt.encode(
            {"sub": "1", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
            "WRONG_SECRET",
            algorithm="HS256",
        )
        from sentinel.util.auth import decode_access_token
        with pytest.raises(_jwt.InvalidTokenError):
            decode_access_token(token)


# ===========================================================================
# FastAPI auth dependency tests (via TestClient)
# ===========================================================================

@pytest.fixture(scope="module")
def client():
    """
    Build a minimal FastAPI test app with one public and one protected endpoint.
    """
    from fastapi import FastAPI, Depends
    from fastapi.testclient import TestClient
    from sentinel.util.auth import get_current_user, create_access_token

    app = FastAPI()

    @app.get("/public")
    async def public_route():
        return {"public": True}

    @app.get("/protected")
    async def protected_route(user=Depends(get_current_user)):
        return {"user": user.get("username")}

    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture(scope="module")
def valid_token():
    from sentinel.util.auth import create_access_token
    return create_access_token({"sub": "42", "username": "testuser", "role": "admin"})


class TestAuthDependency:
    def test_public_route_no_auth_needed(self, client):
        r = client.get("/public")
        assert r.status_code == 200

    def test_protected_no_token_returns_401(self, client):
        r = client.get("/protected")
        assert r.status_code == 401

    def test_protected_invalid_token_returns_401(self, client):
        r = client.get("/protected", headers={"Authorization": "Bearer not-a-jwt"})
        assert r.status_code == 401

    def test_protected_valid_token_returns_200(self, client, valid_token):
        r = client.get("/protected", headers={"Authorization": f"Bearer {valid_token}"})
        assert r.status_code == 200
        assert r.json()["user"] == "testuser"

    def test_protected_wrong_scheme_returns_401(self, client, valid_token):
        r = client.get("/protected", headers={"Authorization": f"Basic {valid_token}"})
        assert r.status_code == 401


# ===========================================================================
# SMTP password encryption tests
# ===========================================================================

class TestSmtpPasswordEncryption:
    def test_stored_value_is_not_plaintext(self):
        from sentinel.util.crypto import encrypt_password
        plaintext = "MySmtpP@ss!"
        stored = encrypt_password(plaintext)
        assert stored != plaintext
        assert len(stored) > 20

    def test_decrypts_correctly(self):
        from sentinel.util.crypto import encrypt_password, decrypt_password
        plaintext = "MySmtpP@ss!"
        stored = encrypt_password(plaintext)
        recovered = decrypt_password(stored)
        assert recovered == plaintext

    def test_get_settings_masks_smtp_pass(self):
        """Verify that the settings GET endpoint masks the SMTP password."""
        import sqlite3
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from sentinel.util.crypto import encrypt_password

        # Build a minimal app that uses the real settings router
        # backed by an in-memory DB containing an encrypted password.
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        encrypted = encrypt_password("secret123")
        conn.execute("INSERT INTO settings VALUES (?, ?)", ("notify_smtp_pass", encrypted))
        conn.execute("INSERT INTO settings VALUES (?, ?)", ("notify_smtp_host", "smtp.example.com"))
        conn.commit()

        import sentinel.db as db_module
        original_get_db = db_module.get_db
        db_module.get_db = lambda: conn

        try:
            from sentinel.api.settings import router as settings_router
            app = FastAPI()
            app.include_router(settings_router, prefix="/api")
            tc = TestClient(app, raise_server_exceptions=True)

            r = tc.get("/api/settings")
            assert r.status_code == 200
            data = r.json()
            assert data.get("notify_smtp_pass") == "**masked**", \
                f"Expected '**masked**', got {data.get('notify_smtp_pass')!r}"
            # Non-secret keys are returned as-is.
            assert data.get("notify_smtp_host") == "smtp.example.com"
        finally:
            db_module.get_db = original_get_db
            conn.close()


# ===========================================================================
# First-run admin bootstrap
# ===========================================================================

class TestAdminBootstrap:
    def test_creates_admin_on_empty_users_table(self, monkeypatch):
        """bootstrap_admin_if_needed() creates exactly one admin user."""
        import sqlite3
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                pass_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()

        import sentinel.db as db_module
        original_get_db = db_module.get_db
        monkeypatch.setattr(db_module, "get_db", lambda: conn)

        # Run bootstrap
        from sentinel.util.auth import bootstrap_admin_if_needed
        bootstrap_admin_if_needed()

        rows = conn.execute("SELECT * FROM users").fetchall()
        assert len(rows) == 1, f"Expected 1 user, got {len(rows)}"
        assert rows[0]["username"] == "admin"
        assert rows[0]["role"] == "admin"
        assert rows[0]["pass_hash"] != ""

        # Password hash must verify correctly.
        from sentinel.util.auth import verify_password
        # We cannot know the generated password, but the hash must be a valid argon2 hash.
        assert rows[0]["pass_hash"].startswith("$argon2")

        conn.close()

    def test_does_not_create_duplicate(self, monkeypatch):
        """bootstrap_admin_if_needed() is idempotent when admin already exists."""
        import sqlite3
        from sentinel.util.auth import hash_password
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                pass_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "INSERT INTO users (username, pass_hash, role) VALUES (?, ?, 'admin')",
            ("admin", hash_password("existing")),
        )
        conn.commit()

        import sentinel.db as db_module
        monkeypatch.setattr(db_module, "get_db", lambda: conn)

        from sentinel.util.auth import bootstrap_admin_if_needed
        bootstrap_admin_if_needed()  # Should be a no-op.

        rows = conn.execute("SELECT * FROM users").fetchall()
        assert len(rows) == 1, f"Expected 1 user, got {len(rows)}"
        conn.close()

    def test_env_var_password_used(self, monkeypatch):
        """SENTINEL_ADMIN_PASSWORD env var is used instead of a random password."""
        import sqlite3
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                pass_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()

        import sentinel.db as db_module
        monkeypatch.setattr(db_module, "get_db", lambda: conn)
        monkeypatch.setenv("SENTINEL_ADMIN_PASSWORD", "MyTestPassword123!")

        from sentinel.util.auth import bootstrap_admin_if_needed, verify_password
        bootstrap_admin_if_needed()

        row = conn.execute("SELECT pass_hash FROM users WHERE username='admin'").fetchone()
        assert row is not None
        assert verify_password("MyTestPassword123!", row["pass_hash"])
        conn.close()
