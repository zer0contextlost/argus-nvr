"""
sentinel/services/hikvision_alerts.py — Hikvision ISAPI alert stream subscriber.

Hikvision cameras expose a multipart HTTP stream at:
  GET http://<host>/ISAPI/Event/notification/alertStream

Each part is an XML EventNotificationAlert. We subscribe per-camera and
call open_gate() on VMD (Video Motion Detection) active events, which
triggers motion-only recording without CPU frame-diff overhead.

This service runs ALONGSIDE the frame-diff detector — it does NOT replace it.
The recorder gate is shared; whichever source opens it first wins.
"""

from __future__ import annotations

import threading
import time
import xml.etree.ElementTree as ET
from typing import Optional

import urllib.request
import urllib.error

from sentinel.util.logging import get_logger

log = get_logger("sentinel.hikvision_alerts")

_subscribers: dict[int, "HikvisionAlertSubscriber"] = {}
_registry_lock = threading.Lock()

POST_MOTION_HOLD_S = 15  # seconds to hold gate open after last VMD active event
RETRY_S = 10  # normal retry delay for transient errors
UNSUPPORTED_AFTER_FAILURES = 3  # consecutive 404s before assuming the endpoint doesn't exist
UNSUPPORTED_RETRY_S = 1800  # retry delay once alertStream looks unsupported (30 min)


class HikvisionAlertSubscriber:
    """
    Long-polls the ISAPI alertStream for one camera.
    Calls recorder open_gate() on motion active, close_gate() after hold time.
    """

    def __init__(self, camera_id: int, host: str, port: int, user: str, password: str):
        self.camera_id = camera_id
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_active: float = 0.0
        self._gate_open = False
        self._close_timer: Optional[threading.Timer] = None
        self._timer_lock = threading.Lock()  # guards _close_timer and _gate_open
        self._status = "stopped"
        self._error: Optional[str] = None
        self._consecutive_404 = 0

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"hik-alerts-cam{self.camera_id}",
            daemon=True,
        )
        self._thread.start()
        self._status = "running"
        log.info("[hik-alerts cam=%s] subscriber started for %s:%s", self.camera_id, self._host, self._port)

    def stop(self) -> None:
        self._stop.set()
        with self._timer_lock:
            if self._close_timer is not None:
                self._close_timer.cancel()
                self._close_timer = None
        if self._thread:
            self._thread.join(timeout=8.0)
        self._status = "stopped"
        log.info("[hik-alerts cam=%s] subscriber stopped", self.camera_id)

    def status(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "status": self._status,
            "gate_open": self._gate_open,
            "error": self._error,
        }

    def _open_url(self, url: str):
        """
        Open the ISAPI alertStream URL, trying Basic auth first then Digest.
        Returns the response object on success, raises on failure.
        """
        password_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        password_mgr.add_password(None, url, self._user, self._password)
        digest_handler = urllib.request.HTTPDigestAuthHandler(password_mgr)
        basic_handler = urllib.request.HTTPBasicAuthHandler(password_mgr)
        opener = urllib.request.build_opener(digest_handler, basic_handler)
        req = urllib.request.Request(url, headers={"Accept": "multipart/x-mixed-replace"})
        return opener.open(req, timeout=60)

    def _run(self) -> None:
        url = f"http://{self._host}:{self._port}/ISAPI/Event/notification/alertStream"

        while not self._stop.is_set():
            try:
                with self._open_url(url) as resp:
                    self._status = "connected"
                    self._error = None
                    self._consecutive_404 = 0
                    self._read_stream(resp)
            except Exception as exc:
                if self._stop.is_set():
                    break

                is_404 = isinstance(exc, urllib.error.HTTPError) and exc.code == 404
                if is_404:
                    self._consecutive_404 += 1
                else:
                    self._consecutive_404 = 0

                if self._consecutive_404 >= UNSUPPORTED_AFTER_FAILURES:
                    # This camera's firmware doesn't expose the ISAPI alertStream endpoint at
                    # all (confirmed on some Hikvision models/firmwares) — retrying every 10s
                    # forever just floods the log and burns a thread wakeup for nothing. Back
                    # off hard; only log once per transition into this state.
                    if self._status != "unsupported":
                        log.warning(
                            "[hik-alerts cam=%s] alertStream returned 404 %d times in a row — "
                            "this camera's firmware likely doesn't support ISAPI event streaming. "
                            "Backing off to a %ds retry interval (VMD-triggered fast recording won't "
                            "work for this camera; frame-diff motion detection still runs normally).",
                            self.camera_id, self._consecutive_404, UNSUPPORTED_RETRY_S,
                        )
                    self._status = "unsupported"
                    self._error = str(exc)
                    self._stop.wait(UNSUPPORTED_RETRY_S)
                else:
                    self._status = "error"
                    self._error = str(exc)
                    log.debug("[hik-alerts cam=%s] stream error: %s — retrying in %ds", self.camera_id, exc, RETRY_S)
                    self._stop.wait(RETRY_S)

    def _read_stream(self, resp) -> None:
        """Read multipart MIME stream, parse XML parts, dispatch events."""
        buf = b""
        boundary = None

        # Extract boundary from Content-Type header
        ct = resp.headers.get("Content-Type", "")
        for part in ct.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[9:].strip('"').encode()
                break

        while not self._stop.is_set():
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            # Try to extract XML payloads from the buffer
            while True:
                # Find XML start
                start = buf.find(b"<?xml")
                if start == -1:
                    # Keep last 200 bytes to handle split boundaries
                    buf = buf[-200:] if len(buf) > 200 else buf
                    break
                # Find end of XML (closing tag)
                end = buf.find(b"</EventNotificationAlert>", start)
                if end == -1:
                    buf = buf[start:]
                    break
                xml_bytes = buf[start:end + len(b"</EventNotificationAlert>")]
                buf = buf[end + len(b"</EventNotificationAlert>"):]
                self._handle_xml(xml_bytes)

    def _handle_xml(self, xml_bytes: bytes) -> None:
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError:
            return

        ns = {"hik": "http://www.hikvision.com/ver20/XMLSchema"}

        def _find(tag: str) -> Optional[str]:
            # Try with and without namespace
            el = root.find(f"hik:{tag}", ns) or root.find(tag)
            return el.text.strip() if el is not None and el.text else None

        event_type = _find("eventType") or ""
        event_state = _find("eventState") or ""

        # VMD = Video Motion Detection
        if event_type.upper() not in ("VMD", "MOTION", "VIDEOMOTION"):
            return

        now = time.monotonic()

        if event_state.lower() == "active":
            self._last_active = now
            with self._timer_lock:
                open_now = not self._gate_open
                if open_now:
                    self._gate_open = True
            if open_now:
                self._open_gate()
        elif event_state.lower() in ("inactive", "stopped"):
            with self._timer_lock:
                if self._close_timer is not None:
                    self._close_timer.cancel()
                self._close_timer = threading.Timer(POST_MOTION_HOLD_S, self._do_close_gate)
                self._close_timer.daemon = True
                self._close_timer.start()

    def _do_close_gate(self) -> None:
        with self._timer_lock:
            self._close_timer = None
            close_now = self._gate_open
            if close_now:
                self._gate_open = False
        if close_now:
            self._close_gate()

    def _open_gate(self) -> None:
        try:
            from sentinel.services.recorder import _recorders, _registry_lock as _rl
            with _rl:
                rec = _recorders.get(self.camera_id)
            if rec:
                rec.open_gate()
                log.info("[hik-alerts cam=%s] VMD active → gate opened", self.camera_id)
        except Exception as exc:
            log.warning("[hik-alerts cam=%s] gate open failed: %s", self.camera_id, exc)

    def _close_gate(self) -> None:
        try:
            from sentinel.services.recorder import _recorders, _registry_lock as _rl
            with _rl:
                rec = _recorders.get(self.camera_id)
            if rec:
                rec.close_gate()
                log.info("[hik-alerts cam=%s] VMD inactive → gate closed", self.camera_id)
        except Exception as exc:
            log.warning("[hik-alerts cam=%s] gate close failed: %s", self.camera_id, exc)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def start_alert_subscribers() -> None:
    """Start ISAPI alert subscribers for all enabled Hikvision cameras."""
    from sentinel.db import get_db
    from sentinel.util.crypto import decrypt_password

    db = get_db()
    cameras = db.execute(
        "SELECT id, name, host, rtsp_port, rtsp_user, rtsp_pass_enc, cam_make "
        "FROM cameras WHERE enabled=1"
    ).fetchall()

    started = 0
    with _registry_lock:
        for cam in cameras:
            if cam["cam_make"] != "hikvision":
                continue
            cam_id = cam["id"]
            if cam_id in _subscribers:
                continue
            try:
                password = decrypt_password(cam["rtsp_pass_enc"]) if cam["rtsp_pass_enc"] else ""
            except Exception:
                password = ""

            sub = HikvisionAlertSubscriber(
                camera_id=cam_id,
                host=cam["host"],
                port=80,  # ISAPI uses HTTP port 80, not RTSP port
                user=cam["rtsp_user"],
                password=password,
            )
            sub.start()
            _subscribers[cam_id] = sub
            started += 1

    log.info("[hik-alerts] started %d alert subscriber(s)", started)


def stop_alert_subscribers() -> None:
    with _registry_lock:
        for cam_id, sub in list(_subscribers.items()):
            try:
                sub.stop()
            except Exception as exc:
                log.error("[hik-alerts] error stopping cam=%s: %s", cam_id, exc)
        _subscribers.clear()
    log.info("[hik-alerts] all subscribers stopped")


def alert_subscriber_statuses() -> list[dict]:
    with _registry_lock:
        return [sub.status() for sub in _subscribers.values()]
