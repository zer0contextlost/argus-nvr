"""
sentinel/api/webpush.py — Web Push (VAPID) subscription management.

Endpoints:
  GET  /api/webpush/vapid-public-key  — return the VAPID public key for the frontend
  POST /api/webpush/subscribe          — register a push subscription
  DELETE /api/webpush/subscribe        — unregister a push subscription
  POST /api/webpush/test               — send a test push notification (requires pywebpush)

VAPID keys are auto-generated on first use and stored in the settings table.
Sending requires `pywebpush` to be installed: pip install pywebpush
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sentinel.db import get_db
from sentinel.util.auth import get_current_user
from sentinel.util.logging import get_logger

log = get_logger("sentinel.api.webpush")
router = APIRouter(prefix="/webpush", tags=["webpush"])

_VAPID_PRIVATE_KEY = "vapid_private_key"
_VAPID_PUBLIC_KEY = "vapid_public_key"


def _ensure_vapid_keys() -> tuple[str, str]:
    """Return (private_key_b64, public_key_b64), generating them on first call."""
    db = get_db()
    pub_row = db.execute("SELECT value FROM settings WHERE key=?", (_VAPID_PUBLIC_KEY,)).fetchone()
    prv_row = db.execute("SELECT value FROM settings WHERE key=?", (_VAPID_PRIVATE_KEY,)).fetchone()
    if pub_row and prv_row and pub_row["value"] and prv_row["value"]:
        return prv_row["value"], pub_row["value"]

    try:
        from py_vapid import Vapid
        v = Vapid()
        v.generate_keys()
        private_b64 = v.private_key.private_bytes(
            encoding=__import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.PEM,
            format=__import__("cryptography.hazmat.primitives.serialization", fromlist=["PrivateFormat"]).PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=__import__("cryptography.hazmat.primitives.serialization", fromlist=["NoEncryption"]).NoEncryption(),
        ).decode()
        public_b64 = v.public_key.public_bytes(
            encoding=__import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.X962,
            format=__import__("cryptography.hazmat.primitives.serialization", fromlist=["PublicFormat"]).PublicFormat.UncompressedPoint,
        )
        import base64
        public_urlsafe = base64.urlsafe_b64encode(public_b64).rstrip(b"=").decode()
    except Exception:
        # pywebpush/cryptography not available — return placeholder keys
        private_b64 = "NOT_CONFIGURED"
        public_urlsafe = "NOT_CONFIGURED"

    db.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", (_VAPID_PRIVATE_KEY, private_b64)
    )
    db.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", (_VAPID_PUBLIC_KEY, public_urlsafe)
    )
    db.commit()
    return private_b64, public_urlsafe


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class PushSubscription(BaseModel):
    endpoint: str
    p256dh: str
    auth: str
    user_agent: str = ""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/vapid-public-key")
async def get_vapid_public_key() -> dict:
    """Return the VAPID application server key for registering push subscriptions."""
    _, public_key = _ensure_vapid_keys()
    return {"public_key": public_key, "available": public_key != "NOT_CONFIGURED"}


@router.post("/subscribe")
async def subscribe(
    body: PushSubscription,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Register a Web Push subscription for the current user."""
    db = get_db()
    user_id = int(current_user["sub"])
    db.execute(
        """
        INSERT INTO web_push_subscriptions(user_id, endpoint, p256dh, auth_key, user_agent)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(endpoint) DO UPDATE SET
            user_id    = excluded.user_id,
            p256dh     = excluded.p256dh,
            auth_key   = excluded.auth_key,
            user_agent = excluded.user_agent
        """,
        (user_id, body.endpoint, body.p256dh, body.auth, body.user_agent),
    )
    db.commit()
    log.info("webpush subscribe user_id=%s", user_id)
    return {"ok": True}


@router.delete("/subscribe")
async def unsubscribe(
    body: PushSubscription,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Remove a Web Push subscription."""
    db = get_db()
    user_id = int(current_user["sub"])
    db.execute(
        "DELETE FROM web_push_subscriptions WHERE endpoint=? AND user_id=?",
        (body.endpoint, user_id),
    )
    db.commit()
    log.info("webpush unsubscribe user_id=%s", user_id)
    return {"ok": True}


@router.post("/test")
async def test_push(current_user: dict = Depends(get_current_user)) -> dict:
    """Send a test push notification to all subscriptions for the current user."""
    db = get_db()
    user_id = int(current_user["sub"])
    rows = db.execute(
        "SELECT endpoint, p256dh, auth_key FROM web_push_subscriptions WHERE user_id=?",
        (user_id,),
    ).fetchall()
    if not rows:
        return {"ok": False, "error": "No subscriptions found for this user"}

    private_key, _ = _ensure_vapid_keys()
    if private_key == "NOT_CONFIGURED":
        return {"ok": False, "error": "VAPID keys not configured (pywebpush not installed)"}

    sent = 0
    errors: list[str] = []
    for row in rows:
        try:
            from pywebpush import webpush, WebPushException
            import json as _json
            webpush(
                subscription_info={
                    "endpoint": row["endpoint"],
                    "keys": {"p256dh": row["p256dh"], "auth": row["auth_key"]},
                },
                data=_json.dumps({"title": "Argus NVR", "body": "Push notifications are working!"}),
                vapid_private_key=private_key,
                vapid_claims={"sub": "mailto:admin@argus.local"},
            )
            sent += 1
        except Exception as exc:
            errors.append(str(exc))

    return {"ok": sent > 0, "sent": sent, "errors": errors}
