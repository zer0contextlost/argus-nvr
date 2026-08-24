"""
sentinel/api/settings.py — Settings + Notification API (Phase 6/7).

Endpoints:
  GET  /api/settings                     — all settings as key/value dict (secrets masked)
  PUT  /api/settings                     — bulk-update settings (key/value dict)
  POST /api/settings/notify/test/{chan}  — test-send on telegram|ntfy|email
  GET  /api/settings/notify/history      — recent notification rows

Phase 7 security:
  - notify_smtp_pass is stored encrypted (Fernet) and masked in GET responses.
  - All endpoints require authentication (applied via router dependency in main.py).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sentinel.db import get_db
from sentinel.util.crypto import encrypt_password, decrypt_password
from sentinel.util.logging import get_logger
from sentinel.util.roles import require_role

log = get_logger("sentinel.api.settings")
router = APIRouter(prefix="/settings", tags=["settings"])

# Keys whose stored values are encrypted at rest and must be masked in GET.
_ENCRYPTED_KEYS = {"notify_smtp_pass"}
_MASKED_VALUE = "**masked**"

# Explicit whitelist of settings keys that the API is allowed to write.
# Any key not in this set is silently ignored on PUT to prevent arbitrary
# key injection into the settings table.
_ALLOWED_KEYS: frozenset[str] = frozenset({
    "notify_enabled",
    "notify_cooldown_seconds",
    "notify_quiet_start",
    "notify_quiet_end",
    "notify_cameras",
    "notify_channels",
    "notify_telegram_bot_token",
    "notify_telegram_chat_id",
    "notify_ntfy_server",
    "notify_ntfy_topic",
    "notify_smtp_host",
    "notify_smtp_port",
    "notify_smtp_user",
    "notify_smtp_pass",
    "notify_smtp_from",
    "notify_smtp_to",
    "notify_smtp_tls",
    "notify_webhook_url",
    "notify_webhook_secret",
    "notify_fcm_service_account_json",
    "notify_storage_warn_pct",
    "cycle_interval_seconds",
    # Cloud backup
    "cloud_backup_enabled",
    "cloud_backup_remote",
    "cloud_backup_schedule",
    # Web Push / PWA
    "webpush_enabled",
    # ONVIF output
    "onvif_output_enabled",
    "onvif_output_port",
    # SSL
    "https_enabled",
    "ssl_cert_path",
    "ssl_key_path",
})


def _maybe_encrypt(key: str, value: str) -> str:
    """Return the encrypted form if this key stores a secret, else the value as-is."""
    if key in _ENCRYPTED_KEYS and value:
        return encrypt_password(value)
    return value


def _maybe_mask(key: str, value: str) -> str:
    """Return a masked placeholder for secret keys so plaintext never leaves the API."""
    if key in _ENCRYPTED_KEYS and value:
        return _MASKED_VALUE
    return value


# ---------------------------------------------------------------------------
# GET all settings
# ---------------------------------------------------------------------------

@router.get("")
async def get_settings() -> dict:
    """
    Return all settings key/value pairs.
    Encrypted secret keys (e.g. notify_smtp_pass) are returned as '**masked**'.
    """
    db = get_db()
    rows = db.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    return {r["key"]: _maybe_mask(r["key"], r["value"]) for r in rows}


# ---------------------------------------------------------------------------
# PUT (bulk update) settings
# ---------------------------------------------------------------------------

class SettingsUpdate(BaseModel):
    settings: dict[str, str]


@router.put("", dependencies=[Depends(require_role("admin"))])
async def update_settings(body: SettingsUpdate) -> dict:
    """
    Bulk-update settings.  Only keys that exist in the request are written.
    Secret keys (e.g. notify_smtp_pass) are transparently encrypted before storage.
    Passing '**masked**' for a secret key is a no-op (value not overwritten).
    """
    db = get_db()
    written: list[str] = []
    for key, value in body.settings.items():
        if key not in _ALLOWED_KEYS:
            log.warning("settings: rejected unknown key %r", key)
            continue
        # Skip masked sentinel — frontend re-sends '**masked**' when not changed.
        if key in _ENCRYPTED_KEYS and value == _MASKED_VALUE:
            continue
        stored_value = _maybe_encrypt(key, value)
        db.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            (key, stored_value),
        )
        written.append(key)
    db.commit()
    log.info("settings updated: %s keys", len(written))
    return {"updated": len(written), "keys": written}


# ---------------------------------------------------------------------------
# Test-send
# ---------------------------------------------------------------------------

VALID_CHANNELS = {"telegram", "ntfy", "email", "webhook"}


@router.post("/notify/test/{channel}", dependencies=[Depends(require_role("admin", "operator"))])
async def test_notify(channel: str) -> dict:
    """
    Attempt a test notification on the given channel.
    Returns {"ok": true} or {"ok": false, "error": "..."}.
    """
    if channel not in VALID_CHANNELS:
        raise HTTPException(status_code=400, detail=f"Unknown channel '{channel}'. Valid: {sorted(VALID_CHANNELS)}")

    from sentinel.services.notifier import get_notifier
    notifier = get_notifier()
    if notifier is None:
        raise HTTPException(status_code=503, detail="Notifier not running")

    result = await _run_in_executor(notifier.test_send, channel)
    return result


async def _run_in_executor(fn, *args):
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn, *args)


# ---------------------------------------------------------------------------
# Notification history
# ---------------------------------------------------------------------------

@router.get("/notify/history")
async def notify_history(
    limit: int = 100,
    offset: int = 0,
    channel: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    """Return recent notification rows with event + camera info."""
    db = get_db()
    conditions = []
    params: list = []
    if channel:
        conditions.append("n.channel = ?")
        params.append(channel)
    if status:
        conditions.append("n.status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    total_row = db.execute(
        f"SELECT COUNT(*) FROM notifications n {where}", params
    ).fetchone()
    total = total_row[0] if total_row else 0

    rows = db.execute(
        f"""
        SELECT n.id, n.event_id, n.channel, n.status, n.sent_at,
               e.camera_id, e.started_at, e.type
        FROM notifications n
        LEFT JOIN events e ON e.id = n.event_id
        {where}
        ORDER BY n.id DESC
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "notifications": [
            {
                "id":         r["id"],
                "event_id":   r["event_id"],
                "channel":    r["channel"],
                "status":     r["status"],
                "sent_at":    r["sent_at"],
                "camera_id":  r["camera_id"],
                "started_at": r["started_at"],
                "event_type": r["type"],
            }
            for r in rows
        ],
    }
