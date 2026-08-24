"""
sentinel/api/live.py — Live stream info for the frontend WebRTC player.

Endpoints:
  GET /api/live                — list all active stream info
  GET /api/live/{camera_id}    — get stream URLs for one camera

go2rtc URLs are now RELATIVE (e.g. /api/go2rtc) so they resolve against
window.location.origin in the browser.  This makes live view work from any
LAN machine or over Tailscale — the browser never needs to talk directly to
go2rtc's localhost port.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from sentinel.config import settings
from sentinel.db import get_db
from sentinel.services.recorder import get_recorder
from sentinel.util.hikvision import build_go2rtc_stream_name
from sentinel.util.logging import get_logger

log = get_logger("sentinel.api.live")
router = APIRouter(prefix="/live", tags=["live"])

# Relative base for go2rtc proxy — the frontend resolves this against its own origin.
GO2RTC_PROXY_BASE = "/api/go2rtc"


def _stream_urls(camera_name: str) -> dict:
    """
    Build the go2rtc stream URL set for a camera.

    All URLs are relative to the app origin so they work from any machine
    that can reach the app (LAN, Tailscale, etc.).

    go2rtc endpoints (served via /api/go2rtc proxy):
      WebRTC:  /api/go2rtc/api/ws?src=<stream>          (signalling WebSocket)
      MSE:     /api/go2rtc/api/stream.mp4?src=<stream>  (fragmented MP4 / MSE)
      HLS:     /api/go2rtc/api/stream.m3u8?src=<stream>
      RTSP:    rtsp://<server-ip>:<rtsp_port>/<stream>   (direct; for VLC etc.)
    """
    base = GO2RTC_PROXY_BASE
    slug = build_go2rtc_stream_name(camera_name)
    sub_slug = build_go2rtc_stream_name(camera_name, sub=True)

    return {
        "stream_name": slug,
        "sub_stream_name": sub_slug,
        "webrtc_url": f"{base}/api/ws?src={slug}",
        "mse_url": f"{base}/api/stream.mp4?src={slug}",
        "hls_url": f"{base}/api/stream.m3u8?src={slug}",
        "rtsp_url": f"rtsp://127.0.0.1:{settings.go2rtc_rtsp_port}/{slug}",
        "go2rtc_api": base,
    }


@router.get("")
async def list_streams() -> list[dict]:
    """Return stream URLs for all enabled cameras."""
    db = get_db()
    cameras = db.execute(
        "SELECT id, name FROM cameras WHERE enabled=1 ORDER BY id"
    ).fetchall()
    result = []
    for cam in cameras:
        info = {"camera_id": cam["id"], "camera_name": cam["name"]}
        info.update(_stream_urls(cam["name"]))
        rec = get_recorder(cam["id"])
        info["is_recording"] = rec.is_recording if rec is not None else False
        # is_armed: True whenever the recorder service is running and healthy,
        # regardless of whether the motion gate is currently open.  For
        # motion-only cameras this stays True between motion events, making it
        # clear the camera is monitoring even though FFmpeg is not actively writing.
        info["is_armed"] = rec is not None
        result.append(info)
    return result


@router.get("/{camera_id}")
async def get_stream(camera_id: int) -> dict:
    """Return stream URLs for a single camera."""
    db = get_db()
    cam = db.execute(
        "SELECT id, name, enabled FROM cameras WHERE id=?", (camera_id,)
    ).fetchone()
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")
    if not cam["enabled"]:
        raise HTTPException(status_code=409, detail="Camera is disabled")

    info = {"camera_id": cam["id"], "camera_name": cam["name"]}
    info.update(_stream_urls(cam["name"]))
    return info
