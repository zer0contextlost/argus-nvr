"""
sentinel/api/audit.py — Audit log endpoint (admin-only).

Endpoints:
  GET /api/audit             — paginated audit log
  DELETE /api/audit          — purge audit log (admin)
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status

from sentinel.db import get_db
from sentinel.util.auth import get_current_user
from sentinel.util.logging import get_logger

log = get_logger("sentinel.api.audit")
router = APIRouter(prefix="/audit", tags=["audit"])


def _require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return current_user


@router.get("")
async def list_audit_log(
    limit: int = 200,
    offset: int = 0,
    action: Optional[str] = None,
    username: Optional[str] = None,
    admin: dict = Depends(_require_admin),
) -> dict:
    """Return paginated audit log rows, newest first."""
    db = get_db()
    conditions: list[str] = []
    params: list = []
    if action:
        conditions.append("action LIKE ?")
        params.append(f"%{action}%")
    if username:
        conditions.append("username = ?")
        params.append(username)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    total = db.execute(f"SELECT COUNT(*) FROM audit_log {where}", params).fetchone()[0]
    rows = db.execute(
        f"""
        SELECT id, user_id, username, action, target_type, target_id, detail, ip, created_at
        FROM audit_log {where}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "entries": [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "username": r["username"],
                "action": r["action"],
                "target_type": r["target_type"],
                "target_id": r["target_id"],
                "detail": r["detail"],
                "ip": r["ip"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


@router.delete("")
async def purge_audit_log(admin: dict = Depends(_require_admin)) -> dict:
    """Purge all audit log entries."""
    db = get_db()
    db.execute("DELETE FROM audit_log")
    db.commit()
    log.info("audit log purged by %r", admin.get("username"))
    return {"ok": True}
