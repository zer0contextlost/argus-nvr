"""
sentinel/api/cameras_media.py — Camera media endpoints that need to be reachable
from browser <img>/<video> tags.

These routes are registered with ``_auth_dep_flex`` in main.py so they accept
either a Bearer header OR a ``?token=<jwt>`` query param — matching the pattern
already used by heatmap, recordings video, and event thumbnails. The rest of
the cameras router (CRUD, test, discover, …) stays Bearer-only.

Endpoints:
  GET /api/cameras/{id}/snapshot   — fresh JPEG via go2rtc frame.jpeg
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from sentinel.db import get_db
from sentinel.services.go2rtc import go2rtc_api_base
from sentinel.util.hikvision import build_go2rtc_stream_name
from sentinel.util.logging import get_logger

log = get_logger("sentinel.api.cameras_media")
router = APIRouter(tags=["cameras-media"])


@router.get("/cameras/{camera_id}/snapshot")
async def get_snapshot(camera_id: int):
    """
    Fetch a JPEG snapshot for any camera by pulling a frame from go2rtc.

    go2rtc exposes GET /api/frame.jpeg?src=<slug> which works for any camera
    type (Hikvision, Wyze, Generic RTSP) as long as the stream is up. Returns
    image/jpeg on success, 502 if go2rtc is unhappy, 504 on timeout.
    """
    db = get_db()
    row = db.execute("SELECT name FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Camera not found")

    slug = build_go2rtc_stream_name(row["name"])
    frame_url = f"{go2rtc_api_base()}/api/frame.jpeg?src={slug}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(frame_url)
        # go2rtc answers 200 with an empty body when the camera is offline.
        if resp.status_code == 200 and resp.content.startswith(b"\xff\xd8"):
            return Response(content=resp.content, media_type="image/jpeg")
        if resp.status_code == 200:
            raise HTTPException(status_code=503, detail="Camera offline: no frame available")
        raise HTTPException(
            status_code=502,
            detail=f"go2rtc returned HTTP {resp.status_code} — stream may not be up yet",
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="go2rtc snapshot request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"go2rtc unreachable: {exc}")
