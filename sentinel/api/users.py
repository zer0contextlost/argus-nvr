"""
sentinel/api/users.py — User management endpoints (admin-only).

Endpoints:
  GET    /api/users              — list all users
  POST   /api/users              — create user
  GET    /api/users/{id}         — get user
  PUT    /api/users/{id}         — update user (role / force-reset)
  DELETE /api/users/{id}         — delete user (cannot delete self)
  POST   /api/users/{id}/reset-password — admin resets password for a user
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from sentinel.db import get_db
from sentinel.util.auth import hash_password, get_current_user
from sentinel.util.audit import log_audit
from sentinel.util.logging import get_logger

log = get_logger("sentinel.api.users")
router = APIRouter(prefix="/users", tags=["users"])

VALID_ROLES = {"admin", "operator", "viewer"}


def _require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return current_user


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "viewer"
    must_change_password: bool = True


class UserUpdate(BaseModel):
    role: Optional[str] = None
    must_change_password: Optional[bool] = None


class ResetPasswordRequest(BaseModel):
    new_password: str
    must_change_password: bool = True


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("")
async def list_users(admin: dict = Depends(_require_admin)) -> list[dict]:
    db = get_db()
    rows = db.execute(
        "SELECT id, username, role, created_at, must_change_password FROM users ORDER BY id"
    ).fetchall()
    return [
        {
            "id": r["id"],
            "username": r["username"],
            "role": r["role"],
            "created_at": r["created_at"],
            "must_change_password": bool(r["must_change_password"]),
        }
        for r in rows
    ]


@router.post("", status_code=201)
async def create_user(
    request: Request,
    body: UserCreate,
    admin: dict = Depends(_require_admin),
) -> dict:
    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=422, detail=f"Invalid role. Valid: {sorted(VALID_ROLES)}")
    if len(body.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE username=?", (body.username,)).fetchone()
    if existing:
        raise HTTPException(status_code=409, detail="Username already exists")

    pw_hash = hash_password(body.password)
    cur = db.execute(
        "INSERT INTO users(username, pass_hash, role, must_change_password) VALUES (?,?,?,?)",
        (body.username, pw_hash, body.role, int(body.must_change_password)),
    )
    db.commit()
    new_id = cur.lastrowid
    log.info("user created id=%s username=%r role=%s by admin=%r", new_id, body.username, body.role, admin.get("username"))
    log_audit(
        action="user.create",
        user_id=int(admin["sub"]),
        username=admin.get("username", ""),
        ip=_client_ip(request),
        target_type="user",
        target_id=new_id,
        detail=f"role={body.role}",
    )
    row = db.execute(
        "SELECT id, username, role, created_at, must_change_password FROM users WHERE id=?", (new_id,)
    ).fetchone()
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "created_at": row["created_at"],
        "must_change_password": bool(row["must_change_password"]),
    }


@router.get("/{user_id}")
async def get_user(user_id: int, admin: dict = Depends(_require_admin)) -> dict:
    db = get_db()
    row = db.execute(
        "SELECT id, username, role, created_at, must_change_password FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "created_at": row["created_at"],
        "must_change_password": bool(row["must_change_password"]),
    }


@router.put("/{user_id}")
async def update_user(
    request: Request,
    user_id: int,
    body: UserUpdate,
    admin: dict = Depends(_require_admin),
) -> dict:
    db = get_db()
    row = db.execute("SELECT id, username FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    updates: dict = {}
    if body.role is not None:
        if body.role not in VALID_ROLES:
            raise HTTPException(status_code=422, detail=f"Invalid role. Valid: {sorted(VALID_ROLES)}")
        updates["role"] = body.role
    if body.must_change_password is not None:
        updates["must_change_password"] = int(body.must_change_password)

    if updates:
        set_clause = ", ".join(f"{k}=?" for k in updates)
        db.execute(f"UPDATE users SET {set_clause} WHERE id=?", list(updates.values()) + [user_id])
        db.commit()
        log.info("user updated id=%s fields=%s by admin=%r", user_id, list(updates.keys()), admin.get("username"))
        log_audit(
            action="user.update",
            user_id=int(admin["sub"]),
            username=admin.get("username", ""),
            ip=_client_ip(request),
            target_type="user",
            target_id=user_id,
            detail=str(updates),
        )

    row = db.execute(
        "SELECT id, username, role, created_at, must_change_password FROM users WHERE id=?", (user_id,)
    ).fetchone()
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "created_at": row["created_at"],
        "must_change_password": bool(row["must_change_password"]),
    }


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    request: Request,
    user_id: int,
    admin: dict = Depends(_require_admin),
):
    if str(user_id) == str(admin.get("sub")):
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    db = get_db()
    row = db.execute("SELECT id, username FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()
    log.info("user deleted id=%s username=%r by admin=%r", user_id, row["username"], admin.get("username"))
    log_audit(
        action="user.delete",
        user_id=int(admin["sub"]),
        username=admin.get("username", ""),
        ip=_client_ip(request),
        target_type="user",
        target_id=user_id,
        detail=f"deleted username={row['username']}",
    )


@router.post("/{user_id}/reset-password")
async def reset_password(
    request: Request,
    user_id: int,
    body: ResetPasswordRequest,
    admin: dict = Depends(_require_admin),
) -> dict:
    if len(body.new_password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    db = get_db()
    row = db.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    pw_hash = hash_password(body.new_password)
    db.execute(
        "UPDATE users SET pass_hash=?, must_change_password=? WHERE id=?",
        (pw_hash, int(body.must_change_password), user_id),
    )
    db.commit()
    log.info("password reset user_id=%s by admin=%r", user_id, admin.get("username"))
    log_audit(
        action="user.password_reset",
        user_id=int(admin["sub"]),
        username=admin.get("username", ""),
        ip=_client_ip(request),
        target_type="user",
        target_id=user_id,
    )
    return {"ok": True}
