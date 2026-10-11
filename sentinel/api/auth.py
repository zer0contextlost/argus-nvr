"""
sentinel/api/auth.py — Authentication endpoints.

Endpoints:
  POST /api/auth/login        — issue JWT on valid credentials
  POST /api/auth/logout       — client-side token invalidation (stateless)
  GET  /api/auth/me           — return the current user info
  PUT  /api/auth/password     — change the current user's password
"""

from __future__ import annotations

import threading
import time as _time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from sentinel.db import get_db
from sentinel.util.auth import (
    hash_password,
    verify_password,
    create_access_token,
    get_current_user,
    get_current_user_pending_password_change,
)
from sentinel.util.logging import get_logger

log = get_logger("sentinel.api.auth")
router = APIRouter(prefix="/auth", tags=["auth"])

# ---------------------------------------------------------------------------
# Login rate limiter — 5 failures per IP in a 5-minute window → HTTP 429
# ---------------------------------------------------------------------------

_RATE_WINDOW_S = 300
_RATE_LIMIT = 5
_rate_lock = threading.Lock()
_failures: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _check_rate_limit(ip: str) -> None:
    now = _time.monotonic()
    with _rate_lock:
        ts = [t for t in _failures.get(ip, []) if now - t < _RATE_WINDOW_S]
        _failures[ip] = ts
        if len(ts) >= _RATE_LIMIT:
            raise HTTPException(
                status_code=429,
                detail="Too many failed login attempts. Try again later.",
                headers={"Retry-After": str(_RATE_WINDOW_S)},
            )


def _record_failure(ip: str) -> None:
    now = _time.monotonic()
    with _rate_lock:
        _failures.setdefault(ip, []).append(now)


def _clear_failures(ip: str) -> None:
    with _rate_lock:
        _failures.pop(ip, None)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str
    must_change_password: bool = False


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class FcmTokenRequest(BaseModel):
    token: str
    device_name: str = ""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/login", response_model=LoginResponse)
async def login(request: Request, body: LoginRequest) -> LoginResponse:
    """
    Validate credentials and return a signed JWT access token.
    Returns HTTP 401 on bad credentials, HTTP 429 after 5 failures in 5 minutes.
    """
    ip = _client_ip(request)
    _check_rate_limit(ip)

    db = get_db()
    row = db.execute(
        "SELECT id, username, pass_hash, role FROM users WHERE username = ?",
        (body.username,),
    ).fetchone()

    if row is None or not verify_password(body.password, row["pass_hash"]):
        _record_failure(ip)
        log.warning("login failed for username=%r ip=%s", body.username, ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    _clear_failures(ip)
    must_change = bool(db.execute(
        "SELECT must_change_password FROM users WHERE id = ?", (row["id"],)
    ).fetchone()["must_change_password"])
    claims = {"sub": str(row["id"]), "username": row["username"], "role": row["role"]}
    if must_change:
        # Only the /auth/me, /auth/password and /auth/logout routes accept this token.
        claims["pwc"] = True
    token = create_access_token(data=claims)
    log.info("login OK username=%r role=%s ip=%s", row["username"], row["role"], ip)
    from sentinel.util.audit import log_audit
    log_audit(
        action="auth.login",
        user_id=row["id"],
        username=row["username"],
        ip=ip,
    )
    return LoginResponse(
        access_token=token,
        username=row["username"],
        role=row["role"],
        must_change_password=must_change,
    )


@router.post("/logout")
async def logout(current_user: dict = Depends(get_current_user_pending_password_change)) -> dict:
    """
    Stateless logout — the client should discard the token.
    (Server-side token revocation is out of scope for v1.)
    """
    log.info("logout username=%r", current_user.get("username"))
    return {"message": "Logged out successfully"}


@router.get("/me")
async def me(current_user: dict = Depends(get_current_user_pending_password_change)) -> dict:
    """Return the currently authenticated user's info."""
    db = get_db()
    user_id = current_user.get("sub")
    row = db.execute(
        "SELECT must_change_password FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    must_change = bool(row["must_change_password"]) if row else False
    return {
        "id": user_id,
        "username": current_user.get("username"),
        "role": current_user.get("role"),
        "must_change_password": must_change,
    }


@router.put("/password")
async def change_password(
    body: ChangePasswordRequest,
    current_user: dict = Depends(get_current_user_pending_password_change),
) -> dict:
    """Change the authenticated user's password."""
    db = get_db()
    user_id = current_user.get("sub")
    row = db.execute(
        "SELECT pass_hash FROM users WHERE id = ?", (user_id,)
    ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="User not found")

    if not verify_password(body.current_password, row["pass_hash"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )

    if len(body.new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="New password must be at least 8 characters",
        )

    new_hash = hash_password(body.new_password)
    db.execute(
        "UPDATE users SET pass_hash = ?, must_change_password = 0 WHERE id = ?",
        (new_hash, user_id),
    )
    db.commit()
    log.info("password changed for user_id=%s", user_id)
    from sentinel.util.audit import log_audit
    log_audit(
        action="auth.password_change",
        user_id=int(user_id),
        username=current_user.get("username", ""),
    )
    # Fresh token without the "pwc" claim so the client can carry on.
    token = create_access_token(data={
        "sub": str(user_id),
        "username": current_user.get("username", ""),
        "role": current_user.get("role", ""),
    })
    return {"message": "Password changed successfully", "access_token": token}


@router.post("/fcm-token")
async def register_fcm_token(
    body: FcmTokenRequest,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Register (or refresh) an FCM push token for this user's device."""
    from datetime import datetime, timezone
    token = body.token.strip()
    if not token:
        raise HTTPException(status_code=422, detail="token is required")
    db = get_db()
    user_id = int(current_user["sub"])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute(
        """
        INSERT INTO fcm_tokens(user_id, token, device_name, created_at, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(token) DO UPDATE SET
            user_id     = excluded.user_id,
            device_name = excluded.device_name,
            last_seen   = excluded.last_seen
        """,
        (user_id, token, body.device_name.strip(), now, now),
    )
    db.commit()
    log.info("FCM token registered user_id=%s device=%r", user_id, body.device_name)
    return {"ok": True}


@router.delete("/fcm-token")
async def unregister_fcm_token(
    body: FcmTokenRequest,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Unregister an FCM token (on logout or token refresh)."""
    token = body.token.strip()
    if not token:
        raise HTTPException(status_code=422, detail="token is required")
    db = get_db()
    user_id = int(current_user["sub"])
    db.execute(
        "DELETE FROM fcm_tokens WHERE token=? AND user_id=?",
        (token, user_id),
    )
    db.commit()
    log.info("FCM token unregistered user_id=%s", user_id)
    return {"ok": True}
