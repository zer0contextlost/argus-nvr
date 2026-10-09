"""
sentinel/services/notifier.py — Multi-channel notifier (Phase 6).

Architecture
------------
- Three delivery channels: Telegram Bot API, ntfy (HTTP POST), SMTP email.
- Rules engine: per-camera enable, time-of-day windows, per-channel selection,
  and cooldown + dedup via the `notifications` table.
- All network calls use httpx with timeouts; failures are caught and logged —
  a dead channel NEVER crashes detection/recording.
- The notifier subscribes to detector events via the in-process event bus
  (_ws_subscribers in detector.py) and runs an asyncio task in the background.

Settings keys (in the `settings` DB table, seeded from config.py env values):
  notify_enabled              — global on/off ("1"/"0")
  notify_cooldown_seconds     — per-camera burst suppression window (int)
  notify_quiet_start          — HH:MM (local time), e.g. "22:00"
  notify_quiet_end            — HH:MM (local time), e.g. "07:00"
  notify_cameras              — comma-separated camera IDs to notify; empty = all
  notify_channels             — comma-separated: telegram,ntfy,email

  notify_telegram_bot_token   — Telegram bot token
  notify_telegram_chat_id     — Telegram chat ID

  notify_ntfy_server          — ntfy server base URL
  notify_ntfy_topic           — ntfy topic

  notify_smtp_host            — SMTP host
  notify_smtp_port            — SMTP port (int)
  notify_smtp_user            — SMTP username
  notify_smtp_pass            — SMTP password (stored encrypted via Fernet; decrypted at use time)
  notify_smtp_from            — From address (defaults to smtp_user)
  notify_smtp_to              — Recipient address(es), comma-separated
  notify_smtp_tls             — "1" = STARTTLS (default), "0" = plain, "ssl" = SSL

  notify_webhook_url          — HTTP/HTTPS URL to POST event JSON to
  notify_webhook_secret       — optional HMAC-SHA256 signing secret (X-Argus-Signature header)
"""

from __future__ import annotations

import asyncio
import email as email_lib
import email.mime.base
import email.mime.image
import email.mime.multipart
import email.mime.text
import smtplib
import sqlite3
import threading
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Optional

import httpx

from sentinel.config import settings
from sentinel.util.logging import get_logger

log = get_logger("sentinel.notifier")

# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def _get_settings_conn() -> sqlite3.Connection:
    """Open a fresh SQLite connection dedicated to the notifier thread/task."""
    db_path = settings.sentinel_db_path
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _read_settings(conn: sqlite3.Connection) -> dict[str, str]:
    """
    Read all rows from the settings table into a plain dict.
    Encrypted keys (notify_smtp_pass) are transparently decrypted.
    """
    from sentinel.util.crypto import decrypt_password

    _ENCRYPTED_KEYS = {"notify_smtp_pass"}
    result: dict[str, str] = {}
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    for r in rows:
        k, v = r["key"], r["value"]
        if k in _ENCRYPTED_KEYS and v:
            try:
                v = decrypt_password(v)
            except Exception:
                # If decryption fails (e.g. key not yet migrated) keep raw value.
                pass
        result[k] = v
    return result


def _write_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# DB seeding — populate settings from config.py env values on first run
# ---------------------------------------------------------------------------

_SETTING_DEFAULTS: list[tuple[str, str]] = [
    ("notify_enabled", "1"),
    ("notify_cooldown_seconds", "60"),
    ("notify_quiet_start", ""),
    ("notify_quiet_end", ""),
    ("notify_cameras", ""),           # empty = all cameras
    ("notify_channels", "telegram,ntfy,email"),
    ("notify_telegram_bot_token", ""),
    ("notify_telegram_chat_id", ""),
    ("notify_ntfy_server", settings.notify_ntfy_server),
    ("notify_ntfy_topic", ""),
    ("notify_smtp_host", ""),
    ("notify_smtp_port", str(settings.notify_smtp_port)),
    ("notify_smtp_user", ""),
    ("notify_smtp_pass", ""),
    ("notify_smtp_from", ""),
    ("notify_smtp_to", ""),
    ("notify_smtp_tls", "1"),
    ("notify_webhook_url", ""),
    ("notify_webhook_secret", ""),
    ("notify_fcm_service_account_json", ""),  # path or JSON blob for firebase-admin
    ("https_enabled", "0"),
    ("ssl_cert_path", ""),
    ("ssl_key_path", ""),
]

# Keys that have env-variable overrides (from config.py)
_ENV_OVERRIDES: dict[str, Optional[str]] = {
    "notify_telegram_bot_token": settings.notify_telegram_bot_token,
    "notify_telegram_chat_id":   settings.notify_telegram_chat_id,
    "notify_ntfy_server":        settings.notify_ntfy_server,
    "notify_ntfy_topic":         settings.notify_ntfy_topic,
    "notify_smtp_host":          settings.notify_smtp_host,
    "notify_smtp_port":          str(settings.notify_smtp_port),
    "notify_smtp_user":          settings.notify_smtp_user,
    "notify_smtp_pass":          settings.notify_smtp_pass,
    "notify_smtp_to":            settings.notify_smtp_to,
}


def seed_settings(conn: sqlite3.Connection) -> None:
    """
    Insert default settings rows if they do not already exist, then apply
    any non-None .env overrides so the DB reflects the user's environment.
    Called once from notifier startup.
    """
    existing = _read_settings(conn)

    # Insert defaults for missing keys
    for key, default_val in _SETTING_DEFAULTS:
        if key not in existing:
            _write_setting(conn, key, default_val)

    # Apply env overrides (only if the env var is set to a non-empty value)
    for key, env_val in _ENV_OVERRIDES.items():
        if env_val:
            _write_setting(conn, key, str(env_val))


# ---------------------------------------------------------------------------
# Rules engine
# ---------------------------------------------------------------------------

class NotifyRules:
    """Parsed notification rules from the settings table."""

    def __init__(self, s: dict[str, str]):
        self.enabled: bool = s.get("notify_enabled", "1") == "1"
        self.cooldown_s: int = int(s.get("notify_cooldown_seconds", "60"))
        self.quiet_start: Optional[dtime] = self._parse_time(s.get("notify_quiet_start", ""))
        self.quiet_end:   Optional[dtime] = self._parse_time(s.get("notify_quiet_end", ""))
        cam_raw = s.get("notify_cameras", "").strip()
        self.camera_ids: Optional[set[int]] = (
            {int(x) for x in cam_raw.split(",") if x.strip().isdigit()}
            if cam_raw else None   # None = all cameras
        )
        ch_raw = s.get("notify_channels", "telegram,ntfy,email")
        self.channels: set[str] = {
            c.strip() for c in ch_raw.split(",") if c.strip()
        }

    @staticmethod
    def _parse_time(s: str) -> Optional[dtime]:
        if not s:
            return None
        try:
            h, m = s.split(":")
            return dtime(int(h), int(m))
        except Exception:
            return None

    def camera_allowed(self, camera_id: int) -> bool:
        """Return True if this camera should be notified."""
        if self.camera_ids is None:
            return True
        return camera_id in self.camera_ids

    def in_quiet_hours(self) -> bool:
        """Return True if current local time falls inside quiet hours."""
        if self.quiet_start is None or self.quiet_end is None:
            return False
        now = datetime.now().time().replace(second=0, microsecond=0)
        start = self.quiet_start
        end   = self.quiet_end
        if start <= end:
            # Simple window: e.g. 09:00 – 17:00
            return start <= now < end
        else:
            # Overnight: e.g. 22:00 – 07:00
            return now >= start or now < end


# ---------------------------------------------------------------------------
# Cooldown / dedup tracker
# ---------------------------------------------------------------------------

class CooldownTracker:
    """
    In-memory cooldown state: tracks the last time we sent a notification
    for each (camera_id, channel).  The `notifications` DB table is the
    persistent record; this object is the fast in-process gate.
    """

    def __init__(self):
        self._last_sent: dict[tuple[int, str], float] = {}
        self._lock = threading.Lock()

    def is_suppressed(self, camera_id: int, channel: str, cooldown_s: int) -> bool:
        """Return True if a notification was already sent within cooldown_s."""
        import time
        key = (camera_id, channel)
        with self._lock:
            last = self._last_sent.get(key)
            if last is None:
                return False
            return (time.monotonic() - last) < cooldown_s

    def mark_sent(self, camera_id: int, channel: str) -> None:
        import time
        key = (camera_id, channel)
        with self._lock:
            self._last_sent[key] = time.monotonic()

    def load_from_db(self, conn: sqlite3.Connection, cooldown_s: int) -> None:
        """
        Initialise in-memory state from recent notifications rows so the
        cooldown survives an app restart within the window.
        """
        import time
        rows = conn.execute(
            """
            SELECT n.channel,
                   e.camera_id,
                   n.sent_at
            FROM notifications n
            JOIN events e ON e.id = n.event_id
            WHERE n.status = 'sent'
              -- sent_at is stored as ISO-8601 with 'T'/'Z'; compare in the same
              -- format (datetime('now') is space-separated and sorts wrong).
              AND n.sent_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ? || ' seconds')
            """,
            (f"-{cooldown_s}",),
        ).fetchall()
        now_mono = time.monotonic()
        now_wall = time.time()
        for row in rows:
            try:
                sent_wall = datetime.fromisoformat(
                    row["sent_at"].replace("Z", "+00:00")
                ).timestamp()
                age_s = now_wall - sent_wall
                if age_s < cooldown_s:
                    key = (row["camera_id"], row["channel"])
                    with self._lock:
                        self._last_sent[key] = now_mono - age_s
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Delivery: Telegram
# ---------------------------------------------------------------------------

def _send_telegram(
    token: str,
    chat_id: str,
    caption: str,
    thumb_path: Optional[str],
    timeout: float = 15.0,
) -> None:
    """
    Send a photo (if thumb available) or text message via Telegram Bot API.
    Raises on failure; caller catches.
    """
    base = f"https://api.telegram.org/bot{token}"
    if thumb_path and Path(thumb_path).exists():
        url = f"{base}/sendPhoto"
        with open(thumb_path, "rb") as fh:
            files = {"photo": ("thumb.jpg", fh, "image/jpeg")}
            data  = {"chat_id": chat_id, "caption": caption[:1024]}
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, data=data, files=files)
        resp.raise_for_status()
    else:
        url = f"{base}/sendMessage"
        payload = {"chat_id": chat_id, "text": caption[:4096]}
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload)
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Delivery: ntfy
# ---------------------------------------------------------------------------

def _send_ntfy(
    server: str,
    topic: str,
    message: str,
    title: str,
    thumb_path: Optional[str],
    timeout: float = 15.0,
) -> None:
    """
    POST a notification to an ntfy server.
    If a thumbnail exists, it is attached as the message's image via PUT.
    The click URL opens the Sentinel events page (localhost — for home use).
    Raises on failure; caller catches.
    """
    server = server.rstrip("/")
    topic_url = f"{server}/{topic}"
    headers = {
        "Title":   title[:250],
        "Tags":    "video_camera",
        "Click":   "http://localhost:8080/events",
    }

    if thumb_path and Path(thumb_path).exists():
        with open(thumb_path, "rb") as fh:
            img_bytes = fh.read()
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                topic_url,
                content=img_bytes,
                headers={
                    **headers,
                    "Content-Type": "image/jpeg",
                    "Filename":     "thumb.jpg",
                    "Message":      message[:500],
                },
            )
        resp.raise_for_status()
    else:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                topic_url,
                data=message[:500].encode(),
                headers=headers,
            )
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Delivery: SMTP email
# ---------------------------------------------------------------------------

def _send_email(
    host: str,
    port: int,
    user: str,
    password: str,
    from_addr: str,
    to_addrs: list[str],
    subject: str,
    body: str,
    thumb_path: Optional[str],
    tls_mode: str = "1",   # "1"=STARTTLS, "0"=plain, "ssl"=SSL
    timeout: float = 20.0,
) -> None:
    """
    Send an email (with optional JPEG attachment) via SMTP.
    tls_mode: "1" = STARTTLS, "ssl" = SSL wrapper, "0" = plain.
    Raises on failure; caller catches.
    """
    msg = email.mime.multipart.MIMEMultipart()
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = ", ".join(to_addrs)
    msg.attach(email.mime.text.MIMEText(body, "plain"))

    if thumb_path and Path(thumb_path).exists():
        with open(thumb_path, "rb") as fh:
            img_data = fh.read()
        img_part = email.mime.image.MIMEImage(img_data, name="thumb.jpg")
        img_part.add_header("Content-Disposition", "attachment", filename="thumb.jpg")
        msg.attach(img_part)

    raw = msg.as_bytes()

    if tls_mode == "ssl":
        with smtplib.SMTP_SSL(host, port, timeout=timeout) as smtp:
            if user:
                smtp.login(user, password)
            smtp.sendmail(from_addr, to_addrs, raw)
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as smtp:
            if tls_mode == "1":
                smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.sendmail(from_addr, to_addrs, raw)


# ---------------------------------------------------------------------------
# Delivery: Webhook
# ---------------------------------------------------------------------------

async def _send_webhook(event: dict, s: dict, client: httpx.AsyncClient) -> bool:
    url = s.get("notify_webhook_url", "").strip()
    if not url:
        return False
    secret = s.get("notify_webhook_secret", "").strip()
    payload = {
        "event_id": event.get("event_id"),
        "event_type": event.get("event_type", "motion"),
        "event_label": event.get("event_label"),
        "camera_id": event.get("camera_id"),
        "camera_name": event.get("camera_name"),
        "started_at": event.get("started_at"),
    }
    headers = {"Content-Type": "application/json", "User-Agent": "Argus-NVR/1.0"}
    if secret:
        import hashlib, hmac, json as _json
        body_bytes = _json.dumps(payload).encode()
        sig = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
        headers["X-Argus-Signature"] = f"sha256={sig}"
    try:
        r = await client.post(url, json=payload, headers=headers, timeout=8.0)
        log.info("[notifier] webhook → %s status=%s", url, r.status_code)
        return r.status_code < 400
    except Exception as exc:
        log.warning("[notifier] webhook failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Delivery: FCM (Firebase Cloud Messaging)
# ---------------------------------------------------------------------------

_fcm_app = None
_fcm_app_lock = threading.Lock()


def _get_fcm_app(service_account_json: str):
    """Initialise firebase-admin app once, return it (or None on failure)."""
    global _fcm_app
    if _fcm_app is not None:
        return _fcm_app
    with _fcm_app_lock:
        if _fcm_app is not None:
            return _fcm_app
        try:
            import firebase_admin
            from firebase_admin import credentials as fb_creds
            import json as _json
            import os

            if not service_account_json:
                return None

            # Accept either a file path or a raw JSON string.
            if os.path.isfile(service_account_json):
                cred = fb_creds.Certificate(service_account_json)
            else:
                sa_dict = _json.loads(service_account_json)
                cred = fb_creds.Certificate(sa_dict)

            # Use a named app so multiple calls don't conflict.
            app_name = "sentinel"
            try:
                _fcm_app = firebase_admin.get_app(app_name)
            except ValueError:
                _fcm_app = firebase_admin.initialize_app(cred, name=app_name)
            return _fcm_app
        except Exception as exc:
            log.warning("[notifier] FCM init failed: %s", exc)
            return None


def _send_fcm(
    service_account_json: str,
    tokens: list[str],
    title: str,
    body: str,
    thumb_url: Optional[str] = None,
) -> int:
    """
    Send a FCM push notification to all registered tokens.
    Returns number of messages successfully sent.
    Raises on configuration error; per-token failures are logged but not raised.
    """
    from firebase_admin import messaging

    app = _get_fcm_app(service_account_json)
    if app is None:
        raise RuntimeError("FCM app not initialised — check service account JSON")
    if not tokens:
        return 0

    notification = messaging.Notification(title=title, body=body)
    if thumb_url:
        notification = messaging.Notification(title=title, body=body, image=thumb_url)

    messages = [
        messaging.Message(notification=notification, token=t) for t in tokens
    ]
    response = messaging.send_each(messages, app=app)
    success = sum(1 for r in response.responses if r.success)
    if response.failure_count:
        log.warning("[notifier] FCM: %d sent, %d failed", success, response.failure_count)
    return success


# ---------------------------------------------------------------------------
# Notification record helpers
# ---------------------------------------------------------------------------

def _record_notification(
    conn: sqlite3.Connection,
    event_id: int,
    channel: str,
    status: str,
    sent_at: Optional[str],
) -> int:
    """Insert a row into the notifications table. Returns the new row id."""
    cur = conn.execute(
        """
        INSERT INTO notifications(event_id, channel, status, sent_at)
        VALUES (?, ?, ?, ?)
        """,
        (event_id, channel, status, sent_at),
    )
    conn.commit()
    return cur.lastrowid


def _get_vapid_keys_from_db(conn: sqlite3.Connection) -> tuple[str, str]:
    """Return (private_key_pem, public_key_b64) from the settings table."""
    prv = conn.execute("SELECT value FROM settings WHERE key='vapid_private_key'").fetchone()
    pub = conn.execute("SELECT value FROM settings WHERE key='vapid_public_key'").fetchone()
    return (prv["value"] if prv else "", pub["value"] if pub else "")


# ---------------------------------------------------------------------------
# Core send dispatcher
# ---------------------------------------------------------------------------

class Notifier:
    """
    Subscribes to the detector event bus and dispatches notifications.
    One instance is created at startup and runs an asyncio task.
    """

    def __init__(self):
        self._cooldown = CooldownTracker()
        self._conn: Optional[sqlite3.Connection] = None
        self._task: Optional[asyncio.Task] = None
        self._queue: Optional[asyncio.Queue] = None
        self._running = False

    # ------------------------------------------------------------------
    # Startup / shutdown
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Called from main.py lifespan (async context). Opens DB, seeds settings,
        and starts the background asyncio task.
        """
        self._conn = _get_settings_conn()
        seed_settings(self._conn)

        # Bootstrap cooldown state from recent DB rows
        s = _read_settings(self._conn)
        cooldown_s = int(s.get("notify_cooldown_seconds", "60"))
        try:
            self._cooldown.load_from_db(self._conn, cooldown_s)
        except Exception as exc:
            log.warning("[notifier] cooldown bootstrap failed: %s", exc)

        self._running = True
        log.info("[notifier] started")

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        if self._queue is not None:
            try:
                from sentinel.services.detector import unsubscribe_events
                unsubscribe_events(self._queue)
            except Exception:
                pass
            self._queue = None
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        log.info("[notifier] stopped")

    def attach_to_event_loop(self, queue: asyncio.Queue) -> asyncio.Task:
        """
        Subscribe to the detector event bus by passing in a pre-registered queue.
        Call this from the async lifespan after start().
        Returns the background task.
        """
        self._queue = queue
        self._task = asyncio.create_task(self._consume_loop())
        return self._task

    # ------------------------------------------------------------------
    # Async consumer
    # ------------------------------------------------------------------

    async def _consume_loop(self) -> None:
        """Drain events from the detector queue and process each one."""
        assert self._queue is not None
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("[notifier] queue error: %s", exc)
                continue

            # Only act on motion_start events (not motion_end or ping)
            if event.get("type") != "motion_start":
                continue

            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._handle_event, event
                )
            except Exception as exc:
                log.error("[notifier] handle_event error: %s", exc)

    # ------------------------------------------------------------------
    # Synchronous event handler (runs in thread-pool executor)
    # ------------------------------------------------------------------

    def _handle_event(self, event: dict) -> None:
        """
        Apply rules, check cooldown, send to each enabled channel, record results.
        All failures are caught individually — one bad channel never blocks others.
        """
        if self._conn is None:
            return

        try:
            s = _read_settings(self._conn)
        except Exception as exc:
            log.error("[notifier] failed to read settings: %s", exc)
            return

        rules = NotifyRules(s)

        if not rules.enabled:
            log.debug("[notifier] notifications disabled globally")
            return

        camera_id: int = event.get("camera_id", 0)
        event_id:  int = event.get("event_id", 0)
        thumb_path: Optional[str] = event.get("thumb_path")
        camera_name: str = event.get("camera_name", f"Camera {camera_id}")
        started_at: str = event.get("started_at", "")

        if not rules.camera_allowed(camera_id):
            log.debug("[notifier] camera %s not in notify list", camera_id)
            return

        if rules.in_quiet_hours():
            log.debug("[notifier] quiet hours — suppressing notification")
            return

        # Format message
        person_name: Optional[str] = event.get("person_name")
        ts_human = started_at.replace("T", " ").replace("Z", " UTC")
        if person_name:
            caption = f"{person_name} detected on {camera_name} at {ts_human}"
            subject = f"[Argus] {person_name}: {camera_name}"
        else:
            caption = f"Motion detected on {camera_name} at {ts_human}"
            subject = f"[Argus] Motion: {camera_name}"

        sent_at_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for channel in rules.channels:
            if self._cooldown.is_suppressed(camera_id, channel, rules.cooldown_s):
                log.debug(
                    "[notifier] cam=%s channel=%s suppressed by cooldown",
                    camera_id, channel,
                )
                continue

            status = "failed"
            try:
                if channel == "telegram":
                    token    = s.get("notify_telegram_bot_token", "")
                    chat_id  = s.get("notify_telegram_chat_id", "")
                    if not token or not chat_id:
                        log.debug("[notifier] telegram not configured — skip")
                        continue
                    _send_telegram(token, chat_id, caption, thumb_path)
                    status = "sent"

                elif channel == "ntfy":
                    ntfy_server = s.get("notify_ntfy_server", "https://ntfy.sh")
                    topic       = s.get("notify_ntfy_topic", "")
                    if not topic:
                        log.debug("[notifier] ntfy topic not configured — skip")
                        continue
                    _send_ntfy(ntfy_server, topic, caption, subject, thumb_path)
                    status = "sent"

                elif channel == "email":
                    smtp_host = s.get("notify_smtp_host", "")
                    smtp_port = int(s.get("notify_smtp_port", "587"))
                    smtp_user = s.get("notify_smtp_user", "")
                    smtp_pass = s.get("notify_smtp_pass", "")
                    smtp_from = s.get("notify_smtp_from", "") or smtp_user
                    smtp_to   = s.get("notify_smtp_to", "")
                    smtp_tls  = s.get("notify_smtp_tls", "1")
                    if not smtp_host or not smtp_to:
                        log.debug("[notifier] smtp not configured — skip")
                        continue
                    to_list = [a.strip() for a in smtp_to.split(",") if a.strip()]
                    body = (
                        f"{caption}\n\n"
                        f"Camera: {camera_name} (id={camera_id})\n"
                        f"Event ID: {event_id}\n"
                        f"Time: {started_at}\n"
                    )
                    _send_email(
                        host=smtp_host,
                        port=smtp_port,
                        user=smtp_user,
                        password=smtp_pass,
                        from_addr=smtp_from,
                        to_addrs=to_list,
                        subject=subject,
                        body=body,
                        thumb_path=thumb_path,
                        tls_mode=smtp_tls,
                    )
                    status = "sent"

                elif channel == "webhook":
                    webhook_url = s.get("notify_webhook_url", "").strip()
                    if not webhook_url:
                        log.debug("[notifier] webhook url not configured — skip")
                        continue
                    webhook_secret = s.get("notify_webhook_secret", "").strip()
                    webhook_payload = {
                        "event_id": event_id,
                        "event_type": event.get("event_type", "motion"),
                        "event_label": event.get("event_label"),
                        "camera_id": camera_id,
                        "camera_name": camera_name,
                        "started_at": started_at,
                        "person_id": event.get("person_id"),
                        "person_name": event.get("person_name"),
                    }
                    import json as _json
                    webhook_headers = {
                        "Content-Type": "application/json",
                        "User-Agent": "Argus-NVR/1.0",
                    }
                    body_bytes = _json.dumps(webhook_payload, separators=(",", ":")).encode()
                    if webhook_secret:
                        import hashlib, hmac as _hmac
                        sig = _hmac.new(webhook_secret.encode(), body_bytes, hashlib.sha256).hexdigest()
                        webhook_headers["X-Argus-Signature"] = f"sha256={sig}"
                    with httpx.Client(timeout=8.0) as _wh_client:
                        wh_resp = _wh_client.post(webhook_url, content=body_bytes, headers=webhook_headers)
                    if wh_resp.status_code < 400:
                        status = "sent"
                        log.info("[notifier] webhook → %s status=%s", webhook_url, wh_resp.status_code)
                    else:
                        log.warning("[notifier] webhook returned %s", wh_resp.status_code)

                elif channel == "fcm":
                    fcm_sa = s.get("notify_fcm_service_account_json", "").strip()
                    if not fcm_sa:
                        log.debug("[notifier] FCM service account not configured — skip")
                        continue
                    # Load all registered FCM tokens from DB.
                    from sentinel.db import get_db as _get_db
                    _fcm_db = _get_db()
                    token_rows = _fcm_db.execute(
                        "SELECT token FROM fcm_tokens"
                    ).fetchall()
                    fcm_tokens = [r["token"] for r in token_rows]
                    if not fcm_tokens:
                        log.debug("[notifier] FCM: no registered tokens — skip")
                        continue
                    sent_count = _send_fcm(fcm_sa, fcm_tokens, subject, caption)
                    if sent_count > 0:
                        status = "sent"

                elif channel == "webpush":
                    private_key_pem, _ = _get_vapid_keys_from_db(self._conn)
                    if not private_key_pem or private_key_pem == "NOT_CONFIGURED":
                        log.debug("[notifier] webpush VAPID key not configured — skip")
                        continue
                    sub_rows = self._conn.execute(
                        "SELECT endpoint, p256dh, auth_key FROM web_push_subscriptions"
                    ).fetchall()
                    if not sub_rows:
                        log.debug("[notifier] webpush: no subscriptions — skip")
                        continue
                    import json as _json
                    wp_payload = _json.dumps({"title": subject, "body": caption})
                    sent_wp = 0
                    for sub in sub_rows:
                        try:
                            from pywebpush import webpush as _webpush
                            _webpush(
                                subscription_info={
                                    "endpoint": sub["endpoint"],
                                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth_key"]},
                                },
                                data=wp_payload,
                                vapid_private_key=private_key_pem,
                                vapid_claims={"sub": "mailto:admin@argus.local"},
                            )
                            sent_wp += 1
                        except Exception as _wp_exc:
                            log.warning("[notifier] webpush failed for %s: %s",
                                        sub["endpoint"][:40], _wp_exc)
                    if sent_wp > 0:
                        status = "sent"

                else:
                    log.warning("[notifier] unknown channel: %s", channel)
                    continue

            except Exception as exc:
                log.error(
                    "[notifier] channel=%s cam=%s event=%s error: %s",
                    channel, camera_id, event_id, exc,
                )
                status = "failed"

            # Record attempt regardless of outcome
            try:
                _record_notification(
                    self._conn, event_id, channel, status, sent_at_str
                )
            except Exception as exc:
                log.error("[notifier] record_notification failed: %s", exc)

            if status == "sent":
                self._cooldown.mark_sent(camera_id, channel)
                log.info(
                    "[notifier] sent via %s for cam=%s event=%s",
                    channel, camera_id, event_id,
                )

    # ------------------------------------------------------------------
    # Test-send (synchronous, called from API endpoint)
    # ------------------------------------------------------------------

    def test_send(self, channel: str, thumb_path: Optional[str] = None) -> dict:
        """
        Attempt a test notification on the given channel.
        Returns {"ok": True} or {"ok": False, "error": "..."}.
        """
        if self._conn is None:
            return {"ok": False, "error": "notifier not started"}

        try:
            s = _read_settings(self._conn)
        except Exception as exc:
            return {"ok": False, "error": f"settings read failed: {exc}"}

        caption = "Argus NVR — test notification"
        subject = "[Argus] Test notification"

        try:
            if channel == "telegram":
                token   = s.get("notify_telegram_bot_token", "")
                chat_id = s.get("notify_telegram_chat_id", "")
                if not token or not chat_id:
                    return {"ok": False, "error": "Telegram bot token / chat ID not configured"}
                _send_telegram(token, chat_id, caption, thumb_path)

            elif channel == "ntfy":
                ntfy_server = s.get("notify_ntfy_server", "https://ntfy.sh")
                topic       = s.get("notify_ntfy_topic", "")
                if not topic:
                    return {"ok": False, "error": "ntfy topic not configured"}
                _send_ntfy(ntfy_server, topic, caption, subject, thumb_path)

            elif channel == "email":
                smtp_host = s.get("notify_smtp_host", "")
                smtp_port = int(s.get("notify_smtp_port", "587"))
                smtp_user = s.get("notify_smtp_user", "")
                smtp_pass = s.get("notify_smtp_pass", "")
                smtp_from = s.get("notify_smtp_from", "") or smtp_user
                smtp_to   = s.get("notify_smtp_to", "")
                smtp_tls  = s.get("notify_smtp_tls", "1")
                if not smtp_host or not smtp_to:
                    return {"ok": False, "error": "SMTP host / to-address not configured"}
                to_list = [a.strip() for a in smtp_to.split(",") if a.strip()]
                _send_email(
                    host=smtp_host,
                    port=smtp_port,
                    user=smtp_user,
                    password=smtp_pass,
                    from_addr=smtp_from,
                    to_addrs=to_list,
                    subject=subject,
                    body=caption,
                    thumb_path=thumb_path,
                    tls_mode=smtp_tls,
                )
            elif channel == "webhook":
                webhook_url = s.get("notify_webhook_url", "").strip()
                if not webhook_url:
                    return {"ok": False, "error": "Webhook URL not configured"}
                webhook_secret = s.get("notify_webhook_secret", "").strip()
                import json as _json
                test_payload = {
                    "event_id": 0,
                    "event_type": "motion",
                    "event_label": None,
                    "camera_id": 0,
                    "camera_name": "Test Camera",
                    "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                webhook_headers = {
                    "Content-Type": "application/json",
                    "User-Agent": "Argus-NVR/1.0",
                }
                body_bytes = _json.dumps(test_payload, separators=(",", ":")).encode()
                if webhook_secret:
                    import hashlib, hmac as _hmac
                    sig = _hmac.new(webhook_secret.encode(), body_bytes, hashlib.sha256).hexdigest()
                    webhook_headers["X-Argus-Signature"] = f"sha256={sig}"
                with httpx.Client(timeout=8.0) as _wh_client:
                    wh_resp = _wh_client.post(webhook_url, content=body_bytes, headers=webhook_headers)
                if wh_resp.status_code >= 400:
                    return {"ok": False, "error": f"Webhook returned HTTP {wh_resp.status_code}"}

            elif channel == "fcm":
                fcm_sa = s.get("notify_fcm_service_account_json", "").strip()
                if not fcm_sa:
                    return {"ok": False, "error": "FCM service account JSON not configured"}
                from sentinel.db import get_db as _get_db
                _fcm_db = _get_db()
                token_rows = _fcm_db.execute("SELECT token FROM fcm_tokens").fetchall()
                fcm_tokens = [r["token"] for r in token_rows]
                if not fcm_tokens:
                    return {"ok": False, "error": "No FCM tokens registered"}
                sent = _send_fcm(fcm_sa, fcm_tokens, "[Argus] Test", "Argus NVR — test notification")
                if sent == 0:
                    return {"ok": False, "error": "FCM: all tokens failed"}

            else:
                return {"ok": False, "error": f"Unknown channel: {channel}"}

            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# System alert — not tied to a camera event, bypasses cooldown/quiet hours
# ---------------------------------------------------------------------------

def send_system_alert(title: str, body: str) -> None:
    """
    Send a system-level alert (disk space, camera offline, etc.) to all
    configured channels.  Runs synchronously; safe to call from a thread-pool
    executor.  Skips channels that are not configured.
    """
    notifier = get_notifier()
    if notifier is None or notifier._conn is None:
        log.debug("[notifier] send_system_alert: notifier not running")
        return
    try:
        s = _read_settings(notifier._conn)
    except Exception as exc:
        log.error("[notifier] send_system_alert settings read failed: %s", exc)
        return

    rules = NotifyRules(s)
    if not rules.enabled:
        return

    sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for channel in rules.channels:
        try:
            if channel == "telegram":
                token   = s.get("notify_telegram_bot_token", "")
                chat_id = s.get("notify_telegram_chat_id", "")
                if not token or not chat_id:
                    continue
                _send_telegram(token, chat_id, f"{title}\n{body}", None)

            elif channel == "ntfy":
                ntfy_server = s.get("notify_ntfy_server", "https://ntfy.sh")
                topic       = s.get("notify_ntfy_topic", "")
                if not topic:
                    continue
                _send_ntfy(ntfy_server, topic, body, title, None)

            elif channel == "email":
                smtp_host = s.get("notify_smtp_host", "")
                smtp_port = int(s.get("notify_smtp_port", "587"))
                smtp_user = s.get("notify_smtp_user", "")
                smtp_pass = s.get("notify_smtp_pass", "")
                smtp_from = s.get("notify_smtp_from", "") or smtp_user
                smtp_to   = s.get("notify_smtp_to", "")
                smtp_tls  = s.get("notify_smtp_tls", "1")
                if not smtp_host or not smtp_to:
                    continue
                to_list = [a.strip() for a in smtp_to.split(",") if a.strip()]
                _send_email(
                    host=smtp_host, port=smtp_port, user=smtp_user,
                    password=smtp_pass, from_addr=smtp_from, to_addrs=to_list,
                    subject=f"[Argus] {title}", body=body, thumb_path=None,
                    tls_mode=smtp_tls,
                )

            elif channel == "webhook":
                webhook_url = s.get("notify_webhook_url", "").strip()
                if not webhook_url:
                    continue
                webhook_secret = s.get("notify_webhook_secret", "").strip()
                import json as _json
                payload = {"event_type": "system_alert", "title": title,
                           "body": body, "sent_at": sent_at}
                headers = {"Content-Type": "application/json",
                           "User-Agent": "Argus-NVR/1.0"}
                b = _json.dumps(payload, separators=(",", ":")).encode()
                if webhook_secret:
                    import hashlib, hmac as _hmac
                    sig = _hmac.new(webhook_secret.encode(), b, hashlib.sha256).hexdigest()
                    headers["X-Argus-Signature"] = f"sha256={sig}"
                with httpx.Client(timeout=8.0) as _c:
                    _c.post(webhook_url, content=b, headers=headers)

            elif channel == "fcm":
                fcm_sa = s.get("notify_fcm_service_account_json", "").strip()
                if not fcm_sa:
                    continue
                from sentinel.db import get_db as _get_db
                token_rows = _get_db().execute("SELECT token FROM fcm_tokens").fetchall()
                fcm_tokens = [r["token"] for r in token_rows]
                if not fcm_tokens:
                    continue
                _send_fcm(fcm_sa, fcm_tokens, title, body)

            else:
                continue

            log.info("[notifier] system alert sent via %s: %s", channel, title)
        except Exception as exc:
            log.warning("[notifier] system alert channel=%s failed: %s", channel, exc)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_notifier: Optional[Notifier] = None


def get_notifier() -> Optional[Notifier]:
    return _notifier


def start_notifier() -> None:
    """
    Called from main.py lifespan (sync portion before yield).
    Creates the singleton and calls start().
    """
    global _notifier
    _notifier = Notifier()
    _notifier.start()


async def attach_notifier_to_pipeline() -> None:
    """
    Called from main.py lifespan (async, after start_notifier).
    Registers a queue with the detector event bus and starts the consumer task.
    """
    global _notifier
    if _notifier is None:
        return
    from sentinel.services.detector import subscribe_events
    q = subscribe_events()
    _notifier.attach_to_event_loop(q)
    log.info("[notifier] attached to detector event pipeline")


def stop_notifier() -> None:
    global _notifier
    if _notifier is not None:
        # Unsubscribe our queue so detector doesn't try to push into a dead queue
        if _notifier._queue is not None:
            from sentinel.services.detector import unsubscribe_events
            try:
                unsubscribe_events(_notifier._queue)
            except Exception:
                pass
        _notifier.stop()
        _notifier = None
