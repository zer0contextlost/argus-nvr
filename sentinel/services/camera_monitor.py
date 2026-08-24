"""
sentinel/services/camera_monitor.py — Camera offline detection and alerting.

Polls each camera's go2rtc stream every 5 minutes.  When a camera has been
unreachable for CAMERA_OFFLINE_ALERT_MINUTES (default 10) consecutive minutes,
fires a system alert via notifier.  Fires a recovery alert when it comes back.

One offline alert per offline event (no spam) and one recovery alert.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

import httpx

from sentinel.config import settings
from sentinel.util.logging import get_logger

log = get_logger("sentinel.camera_monitor")

_DEFAULT_CHECK_INTERVAL_S = 300.0  # 5 minutes

# Per-camera state dict keyed by camera_id.
# Each entry: {"name": str, "offline_since": float|None, "alerted": bool}
_states: dict[int, dict] = {}

_stop_event: Optional[asyncio.Event] = None
_task: Optional[asyncio.Task] = None


# ---------------------------------------------------------------------------
# Camera list loader
# ---------------------------------------------------------------------------

def _get_cameras() -> list[dict]:
    """Return enabled cameras with their computed go2rtc slugs."""
    try:
        from sentinel.db import get_db
        from sentinel.util.hikvision import build_go2rtc_stream_name
        rows = get_db().execute(
            "SELECT id, name FROM cameras WHERE enabled = 1"
        ).fetchall()
        result = []
        for r in rows:
            slug = build_go2rtc_stream_name(r["name"])
            if slug:
                result.append({"id": r["id"], "name": r["name"], "go2rtc_slug": slug})
        return result
    except Exception as exc:
        log.warning("camera_monitor: failed to load cameras: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Online check
# ---------------------------------------------------------------------------

def _is_camera_online(slug: str) -> bool:
    """
    Return True if go2rtc reports at least one active producer for this stream.
    Uses /api/streams on the local go2rtc API port.
    """
    url = f"http://127.0.0.1:{settings.go2rtc_api_port}/api/streams"
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url)
        if resp.status_code != 200:
            return False
        stream_info = resp.json().get(slug)
        if not stream_info:
            return False
        producers = stream_info.get("producers", [])
        return len(producers) > 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Single check pass
# ---------------------------------------------------------------------------

def _run_check() -> None:
    """Check all cameras once. Runs in a thread-pool executor."""
    cameras = _get_cameras()
    if not cameras:
        return

    alert_after_s = settings.camera_offline_alert_minutes * 60
    now = time.monotonic()

    for cam in cameras:
        cam_id = cam["id"]
        cam_name = cam.get("name") or f"Camera {cam_id}"
        slug = cam["go2rtc_slug"]

        state = _states.setdefault(cam_id, {
            "name": cam_name,
            "offline_since": None,
            "alerted": False,
        })
        state["name"] = cam_name  # keep name fresh from DB

        online = _is_camera_online(slug)

        if online:
            if state["offline_since"] is not None:
                was_alerted = state["alerted"]
                state["offline_since"] = None
                state["alerted"] = False
                if was_alerted:
                    log.info("camera_monitor: %s is back online", cam_name)
                    _send_alert(f"Camera Online: {cam_name}",
                                f"{cam_name} is back online.")
        else:
            if state["offline_since"] is None:
                state["offline_since"] = now
                log.debug("camera_monitor: %s offline — starting timer", cam_name)
            elif not state["alerted"]:
                offline_s = now - state["offline_since"]
                if offline_s >= alert_after_s:
                    state["alerted"] = True
                    mins = int(offline_s / 60)
                    log.warning(
                        "camera_monitor: %s has been offline for %d min — alerting",
                        cam_name, mins,
                    )
                    _send_alert(
                        f"Camera Offline: {cam_name}",
                        f"{cam_name} has been offline for {mins} minutes.",
                    )


def _send_alert(title: str, body: str) -> None:
    try:
        from sentinel.services.notifier import send_system_alert
        send_system_alert(title, body)
    except Exception as exc:
        log.error("camera_monitor: alert send failed: %s", exc)


# ---------------------------------------------------------------------------
# Background asyncio task
# ---------------------------------------------------------------------------

async def _monitor_loop() -> None:
    global _stop_event
    log.info("camera_monitor: loop started, interval=%.0fs", _DEFAULT_CHECK_INTERVAL_S)

    while _stop_event is not None and not _stop_event.is_set():
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _run_check)
        except Exception as exc:
            log.error("camera_monitor: check error: %s", exc, exc_info=True)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _DEFAULT_CHECK_INTERVAL_S
        while (_stop_event is not None and not _stop_event.is_set()
               and loop.time() < deadline):
            await asyncio.sleep(min(10.0, _DEFAULT_CHECK_INTERVAL_S))


async def start_camera_monitor() -> None:
    """Start the camera offline monitor task. Called from lifespan."""
    global _stop_event, _task
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(_monitor_loop(), name="camera-monitor")
    log.info("camera_monitor: started")


async def stop_camera_monitor() -> None:
    """Signal the monitor task to stop and wait for it. Called from lifespan."""
    global _stop_event, _task
    if _stop_event is not None:
        _stop_event.set()
    if _task is not None and not _task.done():
        try:
            await asyncio.wait_for(_task, timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _task.cancel()
    _stop_event = None
    _task = None
    log.info("camera_monitor: stopped")
