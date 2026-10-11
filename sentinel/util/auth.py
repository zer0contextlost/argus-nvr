"""
sentinel/util/auth.py — Password hashing (argon2) and JWT token utilities.

Public API:
  hash_password(plaintext) -> str          — argon2id hash
  verify_password(plaintext, hash) -> bool — constant-time verify
  create_access_token(data, expires_delta) -> str  — signed JWT
  decode_access_token(token) -> dict               — verify + decode
  get_current_user(token) -> dict  — FastAPI dependency (Bearer token)
  require_auth  — alias for Depends(get_current_user)
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from sentinel.config import settings
from sentinel.util.logging import get_logger

log = get_logger("sentinel.util.auth")

# ---------------------------------------------------------------------------
# Argon2 password hashing
# ---------------------------------------------------------------------------

_ph = PasswordHasher(
    time_cost=2,
    memory_cost=65536,   # 64 MB
    parallelism=2,
    hash_len=32,
    salt_len=16,
)


def hash_password(plaintext: str) -> str:
    """Return an argon2id hash of the plaintext password."""
    return _ph.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    """
    Verify plaintext against the stored argon2 hash.
    Returns False (rather than raising) on any mismatch.
    """
    try:
        return _ph.verify(hashed, plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# ---------------------------------------------------------------------------
# JWT tokens
# ---------------------------------------------------------------------------

_ALGORITHM = "HS256"
_ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 8  # 8 hours


def _secret() -> str:
    key = settings.sentinel_secret_key
    if key == "change-me":
        log.warning(
            "ARGUS_SECRET_KEY is still the default 'change-me' — "
            "set a strong secret in .env before production use."
        )
    return key


def create_access_token(
    data: dict,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Create a signed JWT.  `data` must include a 'sub' (subject) claim."""
    payload = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=_ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload["exp"] = expire
    payload["iat"] = datetime.now(timezone.utc)
    return jwt.encode(payload, _secret(), algorithm=_ALGORITHM)


class PasswordChangeRequired(jwt.InvalidTokenError):
    """Token belongs to a user who must change their password first."""


def decode_access_token(token: str, allow_pending_password_change: bool = False) -> dict:
    """
    Decode and verify a JWT.
    Raises jwt.ExpiredSignatureError or jwt.InvalidTokenError on failure.

    Tokens issued while must_change_password=1 carry a "pwc" claim and are
    rejected (PasswordChangeRequired) unless allow_pending_password_change is
    set — only the /auth/me, /auth/password and /auth/logout routes allow it.
    """
    payload = jwt.decode(token, _secret(), algorithms=[_ALGORITHM])
    if payload.get("pwc") and not allow_pending_password_change:
        raise PasswordChangeRequired("Password change required")
    return payload


def _payload_or_http_error(token: str, allow_pending_password_change: bool = False) -> dict:
    try:
        return decode_access_token(token, allow_pending_password_change)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except PasswordChangeRequired:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password change required",
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# FastAPI dependency — Bearer token auth
# ---------------------------------------------------------------------------

_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> dict:
    """
    FastAPI dependency.  Validates the Authorization: Bearer <token> header.
    Returns the decoded token payload on success.
    Raises HTTP 401 on missing or invalid token, 403 if the user still has to
    change their password.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _payload_or_http_error(credentials.credentials)


async def get_current_user_pending_password_change(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> dict:
    """Like get_current_user, but also accepts tokens with a pending forced password change."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _payload_or_http_error(credentials.credentials, allow_pending_password_change=True)


# Convenient alias — use as `Depends(require_auth)` in route definitions.
require_auth = Depends(get_current_user)


# ---------------------------------------------------------------------------
# Flexible auth — accepts Bearer header OR ?token= query param
# ---------------------------------------------------------------------------

from fastapi import Request as _Request  # noqa: E402 — deferred import avoids circular


async def get_current_user_flexible(
    request: _Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> dict:
    """
    Same as get_current_user but also accepts a ``?token=<jwt>`` query param.

    Required for endpoints served to browser <video> and <img> elements, which
    cannot attach an Authorization header.  Bearer token takes precedence when
    both are supplied.
    """
    token: Optional[str] = None
    if credentials and credentials.scheme.lower() == "bearer":
        token = credentials.credentials
    if not token:
        token = request.query_params.get("token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _payload_or_http_error(token)


# ---------------------------------------------------------------------------
# First-run admin bootstrap
# ---------------------------------------------------------------------------

_FIRST_RUN_PASSWORD = "Screenshot123!"


def bootstrap_admin_if_needed() -> None:
    """
    If the users table is empty, create the admin user.

    Default password is Screenshot123! with must_change_password=1 so the
    frontend forces a change on first login.  Override via ARGUS_ADMIN_PASSWORD
    env var (skips the forced-change requirement when set explicitly).
    """
    from sentinel.db import get_db

    db = get_db()
    count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count > 0:
        return

    env_pass = os.environ.get("ARGUS_ADMIN_PASSWORD", "").strip()
    if env_pass:
        password = env_pass
        must_change = 0
    else:
        password = _FIRST_RUN_PASSWORD
        must_change = 1

    hashed = hash_password(password)
    db.execute(
        "INSERT INTO users (username, pass_hash, role, must_change_password) VALUES (?, ?, 'admin', ?)",
        ("admin", hashed, must_change),
    )
    db.commit()

    log.warning("=" * 60)
    log.warning("FIRST RUN — admin user created.")
    log.warning("  Username : admin")
    if env_pass:
        log.warning("  Password : (from ARGUS_ADMIN_PASSWORD env var)")
    else:
        log.warning("  Password : %s", password)
        log.warning("  Change this password on first login.")
    log.warning("=" * 60)
