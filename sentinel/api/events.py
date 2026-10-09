"""
sentinel/api/events.py — Events API router.

Endpoints:
  GET  /api/events                   — list events (filter by camera/type/time)
  GET  /api/events/{id}              — get a single event
  GET  /api/events/{id}/thumb        — serve the event thumbnail (JPEG)
  PUT  /api/cameras/{id}/detect-zone — save detection zone config for a camera
  WS   /api/events/ws                — WebSocket live event stream
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from sentinel.config import settings
from sentinel.db import get_db
from sentinel.util.logging import get_logger
from sentinel.util.roles import require_role

# Root directory that all event thumbnail files must reside within.
_EVENTS_ROOT = settings.sentinel_events_dir


def _safe_event_path(path_str: str) -> Path:
    """Resolve a thumb path and reject any path-traversal attempts.

    The resolved path must be inside sentinel_events_dir.
    Raises HTTPException 400 on traversal, 404 if not found.
    """
    try:
        p = Path(path_str).resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")

    root = _EVENTS_ROOT.resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal rejected")

    if not p.exists():
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return p

log = get_logger("sentinel.api.events")
router = APIRouter(prefix="/events", tags=["events"])

# The /ws route lives on its own router (see bottom of file) mounted WITHOUT
# the app-wide auth dependency — see events_websocket() for why.
ws_router = APIRouter(prefix="/events", tags=["events"])


# ---------------------------------------------------------------------------
# Serialiser
# ---------------------------------------------------------------------------

def _row_to_dict(row) -> dict:
    try:
        reviewed_at = row["reviewed_at"]
    except (IndexError, KeyError):
        reviewed_at = None
    try:
        person_id = row["person_id"]
    except (IndexError, KeyError):
        person_id = None
    return {
        "id": row["id"],
        "camera_id": row["camera_id"],
        "type": row["type"],
        "label": row["label"],
        "score": row["score"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "thumb_path": row["thumb_path"],
        "clip_path": row["clip_path"],
        "recording_id": row["recording_id"],
        "meta_json": row["meta_json"],
        "reviewed_at": reviewed_at,
        "person_id": person_id,
    }


# ---------------------------------------------------------------------------
# List / query
# ---------------------------------------------------------------------------

@router.get("")
async def list_events(
    camera_id: Optional[int] = Query(None, description="Filter by camera ID"),
    event_type: Optional[str] = Query(None, alias="type", description="motion or object"),
    label: Optional[str] = Query(None, description="Filter by label (e.g. face)"),
    person_id: Optional[int] = Query(None, description="Filter by person ID"),
    started_after: Optional[str] = Query(None, description="ISO-8601 lower bound (inclusive)"),
    started_before: Optional[str] = Query(None, description="ISO-8601 upper bound (exclusive)"),
    limit: int = Query(100, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> dict:
    """List events with optional filters."""
    db = get_db()

    conditions: list[str] = []
    params: list = []

    if camera_id is not None:
        conditions.append("camera_id = ?")
        params.append(camera_id)
    if event_type is not None:
        conditions.append("type = ?")
        params.append(event_type)
    if label is not None:
        conditions.append("label = ?")
        params.append(label)
    if person_id is not None:
        if person_id == -1:
            conditions.append("person_id IS NULL")
        else:
            conditions.append("person_id = ?")
            params.append(person_id)
    if started_after is not None:
        conditions.append("started_at >= ?")
        params.append(started_after)
    if started_before is not None:
        conditions.append("started_at < ?")
        params.append(started_before)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    total_row = db.execute(f"SELECT COUNT(*) FROM events {where}", params).fetchone()
    total = total_row[0] if total_row else 0

    rows = db.execute(
        f"SELECT * FROM events {where} ORDER BY started_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "events": [_row_to_dict(r) for r in rows],
    }


@router.get("/unreviewed-count")
async def unreviewed_count() -> dict:
    """Return count of events with reviewed_at IS NULL."""
    db = get_db()
    n = db.execute("SELECT COUNT(*) FROM events WHERE reviewed_at IS NULL").fetchone()[0]
    return {"count": n}


@router.get("/{event_id}")
async def get_event(event_id: int) -> dict:
    """Fetch a single event by ID."""
    db = get_db()
    row = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")
    return _row_to_dict(row)


class ReviewedUpdate(BaseModel):
    reviewed: bool

@router.patch("/{event_id}/reviewed")
async def set_event_reviewed(event_id: int, body: ReviewedUpdate) -> dict:
    """Mark an event as reviewed or unreviewed."""
    from datetime import datetime, timezone
    db = get_db()
    row = db.execute("SELECT id FROM events WHERE id=?", (event_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")
    reviewed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if body.reviewed else None
    db.execute("UPDATE events SET reviewed_at=? WHERE id=?", (reviewed_at, event_id))
    db.commit()
    return {"id": event_id, "reviewed_at": reviewed_at}


# ---------------------------------------------------------------------------
# Thumbnail serving
# ---------------------------------------------------------------------------

@router.get("/{event_id}/thumb")
async def get_event_thumb(event_id: int):
    """Serve the JPEG thumbnail for an event.

    Path-traversal protection: the resolved thumb path must reside within
    sentinel_events_dir.  Any attempt to serve a file outside that root is
    rejected with HTTP 400.
    """
    db = get_db()
    row = db.execute("SELECT thumb_path FROM events WHERE id=?", (event_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")
    thumb = row["thumb_path"]
    if not thumb:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    safe_path = _safe_event_path(thumb)
    return FileResponse(
        path=str(safe_path),
        media_type="image/jpeg",
        filename=f"event_{event_id}_thumb.jpg",
    )


@router.get("/{event_id}/clip")
async def get_event_clip(event_id: int):
    """Serve the MP4 clip for an event (if one was saved)."""
    db = get_db()
    row = db.execute("SELECT clip_path FROM events WHERE id=?", (event_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")
    clip = row["clip_path"]
    if not clip:
        raise HTTPException(status_code=404, detail="No clip saved for this event")
    safe_path = _safe_event_path(clip)
    return FileResponse(
        path=str(safe_path),
        media_type="video/mp4",
        filename=f"event_{event_id}.mp4",
    )


# ---------------------------------------------------------------------------
# Detection zone update (on camera)
# ---------------------------------------------------------------------------

class DetectZoneUpdate(BaseModel):
    detect_config_json: str


@router.put("/cameras/{camera_id}/detect-zone", tags=["cameras"],
            dependencies=[Depends(require_role("admin", "operator"))])
async def update_detect_zone(camera_id: int, body: DetectZoneUpdate) -> dict:
    """
    Save a camera's detection zone configuration.
    Expects detect_config_json with a 'zones' list of normalised polygons.
    """
    # Validate JSON
    try:
        cfg = json.loads(body.detect_config_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}")

    db = get_db()
    row = db.execute("SELECT id FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Camera not found")

    db.execute(
        "UPDATE cameras SET detect_config_json=? WHERE id=?",
        (body.detect_config_json, camera_id),
    )
    db.commit()
    log.info("detect zone updated for cam=%s zones=%s", camera_id, len(cfg.get("zones", [])))

    # Reload the detector so the new zones/thresholds take effect immediately
    # (without this, the DB had the change but the running detector kept its
    # original config until the next service restart). Zones are detector-only
    # config and don't appear in go2rtc.yaml, so skip the go2rtc sync.
    try:
        from sentinel.api.cameras import _apply_camera_changes
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: _apply_camera_changes(camera_id=camera_id, sync_streams=False)
        )
    except Exception as exc:
        log.warning("detector reload after detect-zone update failed cam=%s: %s", camera_id, exc)

    return {"camera_id": camera_id, "detect_config_json": body.detect_config_json}


# ---------------------------------------------------------------------------
# WebSocket live event stream
# ---------------------------------------------------------------------------

@ws_router.websocket("/ws")
async def events_websocket(websocket: WebSocket, token: str = ""):
    """
    WebSocket endpoint that streams live motion events to the browser.

    Auth: pass the JWT token as a query parameter:
      ws://host/api/events/ws?token=<bearer-token>
    Browser WebSocket API cannot send custom headers, so we use a query param.

    This route is intentionally NOT on `router` above: `router` is mounted in
    main.py with the app-wide `Depends(get_current_user_flexible)` dependency,
    which pulls in fastapi.security.HTTPBearer. HTTPBearer's __call__ requires
    an HTTP Request, which FastAPI cannot supply when solving dependencies for
    a WebSocket route — every connection would crash with
    "TypeError: HTTPBearer.__call__() missing 1 required positional
    argument: 'request'" before this function's own token check below ever
    ran. So this route lives on `ws_router`, mounted without that dependency,
    and does its own auth via the query-param token instead.

    The client receives JSON messages:
      {"type": "motion_start", "event_id": ..., "camera_id": ..., "started_at": ...}
      {"type": "motion_end",   "event_id": ..., "camera_id": ..., "ended_at": ...}
    """
    from sentinel.services.detector import subscribe_events, unsubscribe_events
    from sentinel.util.auth import decode_access_token
    import jwt as _jwt

    # Validate token before accepting the connection.
    if not token:
        await websocket.close(code=4401, reason="Unauthenticated")
        return
    try:
        decode_access_token(token)
    except (_jwt.ExpiredSignatureError, _jwt.InvalidTokenError):
        await websocket.close(code=4401, reason="Invalid or expired token")
        return

    await websocket.accept()
    log.info("[events/ws] client connected: %s", websocket.client)
    q = subscribe_events()

    try:
        # Run two concurrent tasks: pump event queue → WS, and keep-alive ping
        async def _pump():
            while True:
                event = await q.get()
                try:
                    await websocket.send_json(event)
                except Exception:
                    break

        async def _keepalive():
            while True:
                await asyncio.sleep(30)
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break

        pump_task = asyncio.create_task(_pump())
        ping_task = asyncio.create_task(_keepalive())

        # Wait for client disconnect
        try:
            while True:
                data = await websocket.receive_text()
                # Ignore anything client sends
        except WebSocketDisconnect:
            pass
        finally:
            pump_task.cancel()
            ping_task.cancel()
            # Await cancellation so the tasks are fully cleaned up and don't
            # emit "Task was destroyed but it is pending!" warnings.
            try:
                await asyncio.gather(pump_task, ping_task, return_exceptions=True)
            except Exception:
                pass

    finally:
        unsubscribe_events(q)
        log.info("[events/ws] client disconnected: %s", websocket.client)
