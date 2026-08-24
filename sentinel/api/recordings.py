"""
sentinel/api/recordings.py — Recordings API router.

Endpoints:
  GET /api/recordings                        — list segments (filter by camera + time)
  GET /api/recordings/{id}                   — get a single segment record
  GET /api/recordings/cameras/{camera_id}    — list segments for one camera
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from sentinel.db import get_db
from sentinel.util.logging import get_logger

log = get_logger("sentinel.api.recordings")
router = APIRouter(prefix="/recordings", tags=["recordings"])


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "camera_id": row["camera_id"],
        "path": row["path"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "duration_s": row["duration_s"],
        "size_bytes": row["size_bytes"],
        "has_gap": bool(row["has_gap"]),
    }


@router.get("")
async def list_recordings(
    camera_id: Optional[int] = Query(None, description="Filter by camera ID"),
    started_after: Optional[str] = Query(None, description="ISO-8601 datetime lower bound (inclusive)"),
    started_before: Optional[str] = Query(None, description="ISO-8601 datetime upper bound (exclusive)"),
    limit: int = Query(200, ge=1, le=2000, description="Max rows to return"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
) -> dict:
    """
    List recording segments with optional filters.

    Time range filtering uses the `started_at` column.
    All times are UTC ISO-8601 strings (YYYY-MM-DDTHH:MM:SSZ).
    """
    db = get_db()

    conditions: list[str] = []
    params: list = []

    if camera_id is not None:
        conditions.append("camera_id = ?")
        params.append(camera_id)
    if started_after is not None:
        conditions.append("started_at >= ?")
        params.append(started_after)
    if started_before is not None:
        conditions.append("started_at < ?")
        params.append(started_before)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    # Count total (for pagination metadata).
    total_row = db.execute(
        f"SELECT COUNT(*) FROM recordings {where}", params
    ).fetchone()
    total = total_row[0] if total_row else 0

    rows = db.execute(
        f"SELECT * FROM recordings {where} ORDER BY started_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "recordings": [_row_to_dict(r) for r in rows],
    }


@router.get("/cameras/{camera_id}")
async def list_camera_recordings(
    camera_id: int,
    started_after: Optional[str] = Query(None, description="ISO-8601 datetime lower bound"),
    started_before: Optional[str] = Query(None, description="ISO-8601 datetime upper bound"),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> dict:
    """List recording segments for a specific camera."""
    db = get_db()

    # Verify camera exists.
    cam = db.execute("SELECT id FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")

    conditions: list[str] = ["camera_id = ?"]
    params: list = [camera_id]

    if started_after is not None:
        conditions.append("started_at >= ?")
        params.append(started_after)
    if started_before is not None:
        conditions.append("started_at < ?")
        params.append(started_before)

    where = f"WHERE {' AND '.join(conditions)}"

    total_row = db.execute(f"SELECT COUNT(*) FROM recordings {where}", params).fetchone()
    total = total_row[0] if total_row else 0

    rows = db.execute(
        f"SELECT * FROM recordings {where} ORDER BY started_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()

    return {
        "camera_id": camera_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "recordings": [_row_to_dict(r) for r in rows],
    }


# ---------------------------------------------------------------------------
# Smart search — unified search across recordings + events
# ---------------------------------------------------------------------------

@router.get("/search")
async def smart_search(
    q: Optional[str] = Query(None, description="Text to match against event label"),
    camera_id: Optional[int] = Query(None),
    event_type: Optional[str] = Query(None, description="motion|object"),
    started_after: Optional[str] = Query(None, description="ISO-8601 lower bound"),
    started_before: Optional[str] = Query(None, description="ISO-8601 upper bound"),
    min_score: Optional[float] = Query(None, description="Minimum detection score 0-1"),
    has_recording: Optional[bool] = Query(None, description="Only events with a linked recording"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict:
    """
    Smart search: find events (and their associated recordings) matching the given filters.
    Results include event metadata, camera name, thumb URL, and the recording that covers it.
    """
    db = get_db()
    conditions: list[str] = []
    params: list = []

    if q:
        conditions.append("(e.label LIKE ? OR c.name LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    if camera_id is not None:
        conditions.append("e.camera_id = ?")
        params.append(camera_id)
    if event_type:
        conditions.append("e.type = ?")
        params.append(event_type)
    if started_after:
        conditions.append("e.started_at >= ?")
        params.append(started_after)
    if started_before:
        conditions.append("e.started_at < ?")
        params.append(started_before)
    if min_score is not None:
        conditions.append("e.score >= ?")
        params.append(min_score)
    if has_recording is True:
        conditions.append("e.recording_id IS NOT NULL")
    elif has_recording is False:
        conditions.append("e.recording_id IS NULL")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    total = db.execute(
        f"""
        SELECT COUNT(*) FROM events e
        LEFT JOIN cameras c ON c.id = e.camera_id
        {where}
        """,
        params,
    ).fetchone()[0]

    rows = db.execute(
        f"""
        SELECT e.id, e.camera_id, e.type, e.label, e.score,
               e.started_at, e.ended_at, e.thumb_path, e.recording_id,
               e.reviewed_at,
               c.name AS camera_name,
               r.path AS rec_path, r.started_at AS rec_started_at
        FROM events e
        LEFT JOIN cameras c ON c.id = e.camera_id
        LEFT JOIN recordings r ON r.id = e.recording_id
        {where}
        ORDER BY e.started_at DESC
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "results": [
            {
                "id": r["id"],
                "camera_id": r["camera_id"],
                "camera_name": r["camera_name"] or f"Camera {r['camera_id']}",
                "type": r["type"],
                "label": r["label"],
                "score": r["score"],
                "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "thumb_url": f"/api/events/{r['id']}/thumb" if r["thumb_path"] else None,
                "recording_id": r["recording_id"],
                "rec_started_at": r["rec_started_at"],
                "reviewed_at": r["reviewed_at"],
            }
            for r in rows
        ],
    }


# NOTE: this catch-all path parameter route MUST stay registered after every
# literal /recordings/* route in this router (e.g. /search above) — FastAPI
# matches top-to-bottom, and /{recording_id} swallows any single-segment path.
@router.get("/{recording_id}")
async def get_recording(recording_id: int) -> dict:
    """Fetch a single recording segment by ID."""
    db = get_db()
    row = db.execute("SELECT * FROM recordings WHERE id=?", (recording_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Recording not found")
    return _row_to_dict(row)
