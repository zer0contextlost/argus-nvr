"""
sentinel/api/system.py — /api/system router: health + status + purge.
"""

from __future__ import annotations

import platform
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from sentinel.config import settings
from sentinel.db import get_db, list_tables
from sentinel.util.auth import get_current_user, verify_password
from sentinel.services import go2rtc as go2rtc_svc
from sentinel.services import recorder as recorder_svc
from sentinel.services import detector as detector_svc
from sentinel.services import storage as storage_svc
from sentinel.services.supervisor import all_supervisor_statuses
from sentinel.services.hikvision_alerts import alert_subscriber_statuses
from sentinel.util.logging import get_logger

log = get_logger("sentinel.api.system")
router = APIRouter(prefix="/system", tags=["system"])
# Routes loaded by browser <img> tags (mounted with ?token= auth in main.py).
media_router = APIRouter(prefix="/system", tags=["system"])

_start_time = time.time()


@router.get("/health")
async def health() -> dict:
    """
    Health check endpoint.  Returns 200 when the app is running.
    go2rtc status, recorder status, and storage stats are included for
    observability.  Also includes all supervisor restart counts.
    """
    tables = list_tables()
    g2r = go2rtc_svc.go2rtc_status()
    recorders = recorder_svc.recorder_statuses()
    detectors = detector_svc.detector_statuses()
    all_supervisors = all_supervisor_statuses()

    try:
        hik_alerts = alert_subscriber_statuses()
    except Exception as exc:
        hik_alerts = {"error": str(exc)}

    try:
        storage = storage_svc.get_storage_status()
    except Exception as exc:
        storage = {"error": str(exc)}

    # Last backup info
    backup_info: dict = {}
    try:
        backup_dir = settings.sentinel_data_root / "backups"
        backups = sorted(backup_dir.glob("argus-*.db"), reverse=True) if backup_dir.exists() else []
        if backups:
            b = backups[0]
            stat = b.stat()
            backup_info = {
                "last_file": b.name,
                "last_size_bytes": stat.st_size,
                "count": len(backups),
            }
        else:
            backup_info = {"last_file": None, "count": 0}
    except Exception as exc:
        backup_info = {"error": str(exc)}

    return {
        "status": "ok",
        "uptime_s": round(time.time() - _start_time, 1),
        "platform": platform.system(),
        "python": platform.python_version(),
        "db_tables": tables,
        "go2rtc": g2r,
        "recorders": recorders,
        "detectors": detectors,
        "supervisors": all_supervisors,
        "hik_alert_subscribers": hik_alerts,
        "storage": storage,
        "backup": backup_info,
    }


class PurgeRequest(BaseModel):
    password: str


@router.post("/purge")
async def purge_all(
    body: PurgeRequest,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Purge ALL recordings, events, and notifications.

    Steps:
      1. Stop all active recorders (kills FFmpeg writers).
      2. Delete every file under recordings_dir and events_dir.
      3. Truncate recordings, events, and notifications tables.
      4. Restart recorders so cameras start writing fresh segments.
    """
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    # Verify the caller's password before doing anything destructive.
    # JWT "sub" is the user ID (str), not the username.
    user_id = current_user.get("sub", "")
    db = get_db()
    user_row = db.execute(
        "SELECT pass_hash FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if not user_row or not verify_password(body.password, user_row["pass_hash"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Incorrect password",
        )

    # 1. Stop recorders so FFmpeg releases file handles before we delete.
    try:
        recorder_svc.stop_recorders()
    except Exception as exc:
        log.warning("purge: error stopping recorders: %s", exc)

    # 2. Delete all files on disk.
    deleted_bytes = 0
    deleted_files = 0
    for dir_path in (settings.sentinel_recordings_dir, settings.sentinel_events_dir):
        p = Path(dir_path)
        if p.exists():
            for child in p.iterdir():
                try:
                    if child.is_dir():
                        size = sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
                        shutil.rmtree(child, ignore_errors=True)
                        deleted_bytes += size
                        deleted_files += 1
                    else:
                        deleted_bytes += child.stat().st_size
                        child.unlink(missing_ok=True)
                        deleted_files += 1
                except Exception as exc:
                    log.warning("purge: could not delete %s: %s", child, exc)

    # 3. Truncate DB tables (cascade handles notifications via events FK).
    db = get_db()
    db.execute("DELETE FROM notifications")
    db.execute("DELETE FROM events")
    db.execute("DELETE FROM recordings")
    db.commit()

    log.info(
        "purge complete: deleted %d items (%.1f MB)",
        deleted_files,
        deleted_bytes / 1_048_576,
    )

    # 4. Restart recorders.
    try:
        recorder_svc.start_recorders()
    except Exception as exc:
        log.warning("purge: error restarting recorders: %s", exc)

    return {
        "ok": True,
        "deleted_files": deleted_files,
        "deleted_bytes": deleted_bytes,
    }


@router.post("/backup")
async def trigger_backup(current_user: dict = Depends(get_current_user)) -> dict:
    """
    Trigger an immediate DB backup.
    Returns the path and size of the created file.
    """
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    import asyncio as _asyncio
    from sentinel.services.backup import run_backup
    loop = _asyncio.get_running_loop()
    dest = await loop.run_in_executor(None, run_backup)
    return {"ok": True, "path": str(dest), "size_bytes": dest.stat().st_size}


@router.get("/dashboard")
async def dashboard() -> dict:
    """Lightweight dashboard stats for the home page."""
    from datetime import datetime, timedelta, timezone
    db = get_db()

    cam_rows = db.execute("SELECT enabled FROM cameras").fetchall()
    total_cameras = len(cam_rows)
    enabled_cameras = sum(1 for r in cam_rows if r["enabled"])

    now = datetime.now(timezone.utc)
    since_24h = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    since_7d  = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    count_24h = db.execute("SELECT COUNT(*) FROM events WHERE started_at >= ?", (since_24h,)).fetchone()[0]
    count_7d  = db.execute("SELECT COUNT(*) FROM events WHERE started_at >= ?", (since_7d,)).fetchone()[0]
    unreviewed = db.execute("SELECT COUNT(*) FROM events WHERE reviewed_at IS NULL").fetchone()[0]

    recent_rows = db.execute(
        """SELECT e.id, e.camera_id, e.type, e.label, e.started_at, e.ended_at, e.thumb_path,
                  c.name AS camera_name
           FROM events e LEFT JOIN cameras c ON c.id = e.camera_id
           ORDER BY e.started_at DESC LIMIT 10"""
    ).fetchall()
    recent_events = [
        {
            "id": r["id"],
            "camera_id": r["camera_id"],
            "camera_name": r["camera_name"] or f"Camera {r['camera_id']}",
            "type": r["type"],
            "label": r["label"],
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "has_thumb": bool(r["thumb_path"]),
        }
        for r in recent_rows
    ]

    try:
        stor = storage_svc.get_storage_status()
    except Exception:
        stor = {}

    recorder_statuses = recorder_svc.recorder_statuses()
    online = sum(1 for s in recorder_statuses if s.get("status") == "running")

    return {
        "cameras": {"total": total_cameras, "enabled": enabled_cameras, "recording": online},
        "events": {"last_24h": count_24h, "last_7d": count_7d, "unreviewed": unreviewed},
        "recent_events": recent_events,
        "storage": stor,
    }


@router.get("/camera-health")
async def camera_health() -> dict:
    """
    Per-camera health summary: recorder status, detector status, go2rtc presence,
    last recording time, and event count in the last hour.
    """
    from datetime import datetime, timedelta, timezone
    db = get_db()
    cameras = db.execute("SELECT id, name, enabled, record_mode FROM cameras ORDER BY id").fetchall()

    rec_statuses = {s["camera_name"]: s for s in recorder_svc.recorder_statuses()}
    det_statuses = {s["camera_name"]: s for s in detector_svc.detector_statuses()}

    now = datetime.now(timezone.utc)
    since_1h = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    result = []
    for cam in cameras:
        cam_id = cam["id"]
        cam_name = cam["name"]

        # Last recording
        last_rec = db.execute(
            "SELECT started_at, ended_at FROM recordings WHERE camera_id=? ORDER BY id DESC LIMIT 1",
            (cam_id,),
        ).fetchone()

        # Event count in last hour
        evt_1h = db.execute(
            "SELECT COUNT(*) FROM events WHERE camera_id=? AND started_at >= ?",
            (cam_id, since_1h),
        ).fetchone()[0]

        # Recorder/detector status
        rec_status = rec_statuses.get(cam_name, {}).get("status", "unknown")
        det_status = det_statuses.get(cam_name, {}).get("status", "unknown")

        result.append({
            "id": cam_id,
            "name": cam_name,
            "enabled": bool(cam["enabled"]),
            "record_mode": cam["record_mode"],
            "recorder_status": rec_status,
            "detector_status": det_status,
            "last_recording_at": last_rec["started_at"] if last_rec else None,
            "last_recording_ended": last_rec["ended_at"] if last_rec else None,
            "events_last_hour": evt_1h,
        })

    return {"cameras": result}


@media_router.get("/qr")
async def generate_qr(url: str = "") -> object:
    """
    Generate a QR code PNG for the given URL (defaults to the app's base URL).
    Requires the 'qrcode' Python package (pip install qrcode[pil]).
    """
    from fastapi.responses import Response as _Response
    if not url:
        url = f"http://{settings.sentinel_host}:{settings.sentinel_port}"

    try:
        import qrcode  # type: ignore
        from io import BytesIO
        img = qrcode.make(url)
        buf = BytesIO()
        img.save(buf, format="PNG")
        return _Response(content=buf.getvalue(), media_type="image/png")
    except ImportError:
        # qrcode not installed — return plain text with the URL
        return _Response(content=url.encode(), media_type="text/plain")
    except Exception as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/storage")
async def storage_status() -> dict:
    """
    Dedicated storage-status endpoint.

    Returns disk metrics for the recordings drive and retention projections:
      - disk_total_bytes / disk_used_bytes / disk_free_bytes / free_pct
      - recordings_total_bytes — sum of all indexed segment sizes
      - segment_count          — total number of indexed segments
      - gb_per_day             — rolling 24-hour write rate in GB
      - projected_retention_days — estimated days until space floor is hit
      - retention_max_age_days  — configured max retention age
      - free_space_floor_pct   — configured free-space floor
    """
    return storage_svc.get_storage_status()
