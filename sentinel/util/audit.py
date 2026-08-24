"""
sentinel/util/audit.py — Audit logging helper.

Call log_audit() from any API endpoint to record an admin or user action.
Failures are swallowed so they never break the calling request.
"""
from __future__ import annotations

from typing import Optional

from sentinel.util.logging import get_logger

log = get_logger("sentinel.audit")


def log_audit(
    action: str,
    user_id: Optional[int] = None,
    username: str = "",
    ip: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[int] = None,
    detail: Optional[str] = None,
) -> None:
    """Insert one row into audit_log. Silently no-ops on any error."""
    try:
        from sentinel.db import get_db
        db = get_db()
        db.execute(
            """
            INSERT INTO audit_log(user_id, username, action, target_type, target_id, detail, ip)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, username, action, target_type, target_id, detail, ip),
        )
        db.commit()
    except Exception as exc:
        log.warning("audit log write failed: %s", exc)
