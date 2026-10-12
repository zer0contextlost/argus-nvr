"""
sentinel/api/cameras.py — Camera CRUD, test, and discovery.

Endpoints:
  GET    /api/cameras              — list cameras
  POST   /api/cameras              — add camera
  GET    /api/cameras/{id}         — get camera
  PUT    /api/cameras/{id}         — update camera
  DELETE /api/cameras/{id}         — delete camera
  POST   /api/cameras/{id}/test    — ffprobe connectivity test
  POST   /api/cameras/discover     — run LAN auto-discovery

The snapshot route (GET /api/cameras/{id}/snapshot) lives in cameras_media.py
because it needs to be reachable from browser <img> tags, which can only carry
auth via ?token= query param. cameras_media is registered with _auth_dep_flex.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from sentinel.config import settings
from sentinel.db import get_db, open_db
from sentinel.services.discovery import discover_all
from sentinel.services.go2rtc import reload_go2rtc
from sentinel.util.crypto import encrypt_password, decrypt_password
from sentinel.util.roles import require_role
from sentinel.util.hikvision import (
    build_main_rtsp_url,
    build_sub_rtsp_url,
    build_go2rtc_stream_name,
)
from sentinel.util.logging import get_logger

log = get_logger("sentinel.api.cameras")
router = APIRouter(prefix="/cameras", tags=["cameras"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class CameraCreate(BaseModel):
    name: str
    host: str
    rtsp_port: int = 554
    rtsp_user: str = ""
    rtsp_pass: str = Field(default="", description="Plaintext; stored encrypted")
    main_path: str = "/Streaming/Channels/101"
    sub_path: str = "/Streaming/Channels/102"
    channel: int = 1
    record_mode: str = "motion-only"
    enabled: bool = True
    detect_config_json: str = "{}"
    cam_type: str = "rtsp"
    cam_make: str = "generic"
    cam_group: str = ""
    retention_days: Optional[int] = None
    ptz_capable: bool = False
    ptz_config_json: str = "{}"
    privacy_masks_json: str = "[]"


class CameraUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    rtsp_port: Optional[int] = None
    rtsp_user: Optional[str] = None
    rtsp_pass: Optional[str] = None
    main_path: Optional[str] = None
    sub_path: Optional[str] = None
    channel: Optional[int] = None
    record_mode: Optional[str] = None
    enabled: Optional[bool] = None
    detect_config_json: Optional[str] = None
    cam_type: Optional[str] = None
    cam_make: Optional[str] = None
    cam_group: Optional[str] = None
    retention_days: Optional[int] = None
    ptz_capable: Optional[bool] = None
    ptz_config_json: Optional[str] = None
    privacy_masks_json: Optional[str] = None


class CameraOut(BaseModel):
    id: int
    name: str
    host: str
    rtsp_port: int
    rtsp_user: str
    main_path: str
    sub_path: str
    channel: int
    record_mode: str
    enabled: bool
    detect_config_json: str
    cam_type: str
    cam_make: str
    cam_group: str
    created_at: str
    retention_days: Optional[int] = None
    # Note: rtsp_pass_enc is NOT returned.


def _add_recorder(camera_id: int, camera_name: str, record_mode: str) -> None:
    """Thin wrapper so tests can patch at 'sentinel.api.cameras._add_recorder'."""
    from sentinel.services.recorder import add_recorder
    add_recorder(camera_id=camera_id, camera_name=camera_name, record_mode=record_mode)


def _remove_recorder(camera_id: int) -> None:
    from sentinel.services.recorder import remove_recorder
    remove_recorder(camera_id)


def _add_detector(camera_id: int, camera_name: str, detect_config_json: str, privacy_masks_json: str = "[]") -> None:
    from sentinel.services.detector import add_detector
    add_detector(camera_id=camera_id, camera_name=camera_name, detect_config_json=detect_config_json, privacy_masks_json=privacy_masks_json)


def _remove_detector(camera_id: int) -> None:
    from sentinel.services.detector import remove_detector
    remove_detector(camera_id)


def _apply_camera_changes(
    camera_id: int | None = None,
    deleted: bool = False,
    sync_streams: bool = True,
) -> None:
    """
    After any camera add/edit/delete:
    1. Sync go2rtc (write new config + live per-stream API sync; full restart
       only as a fallback). Skipped when sync_streams=False.
    2. Reconcile recorders and detectors to the current enabled-camera set
       — stop orphan workers for removed/disabled cameras, start workers for
       newly added/enabled cameras.

    Args:
        camera_id: The camera that was added/edited/deleted (None = reconcile all).
        deleted:   True when the camera was deleted (so we skip fetching it from DB).
        sync_streams: False for changes that don't affect go2rtc's config
                   (e.g. detect-zone edits: zones only feed the detector).

    Blocking (go2rtc HTTP sync, detector restart) — async callers must run it
    in an executor.
    """
    # 1. Sync go2rtc so it serves the current stream set immediately.
    if sync_streams:
        try:
            reload_go2rtc()
        except Exception as exc:
            log.warning("go2rtc reload failed: %s", exc)

    if camera_id is None:
        return

    # ISAPI alert subscriber: started, restarted on host/credential change,
    # or stopped on delete/disable. Reads the camera's current DB row.
    try:
        from sentinel.services.hikvision_alerts import sync_alert_subscriber
        sync_alert_subscriber(camera_id)
    except Exception as exc:
        log.warning("hik alert sync cam=%s failed: %s", camera_id, exc)

    if deleted:
        # Remove recorder and detector for the deleted camera.
        try:
            _remove_recorder(camera_id)
        except Exception as exc:
            log.warning("remove_recorder cam=%s failed: %s", camera_id, exc)
        try:
            _remove_detector(camera_id)
        except Exception as exc:
            log.warning("remove_detector cam=%s failed: %s", camera_id, exc)
        return

    # Camera was added or edited — fetch its current state from DB. Own
    # connection: this runs in an executor thread, and the shared get_db()
    # connection belongs to the event loop.
    conn = open_db()
    try:
        row = conn.execute(
            "SELECT id, name, record_mode, enabled, detect_config_json, privacy_masks_json FROM cameras WHERE id=?",
            (camera_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return

    enabled = bool(row["enabled"])
    if enabled:
        # Ensure recorder is running.
        try:
            _add_recorder(
                camera_id=row["id"],
                camera_name=row["name"],
                record_mode=row["record_mode"],
            )
        except Exception as exc:
            log.warning("add_recorder cam=%s failed: %s", camera_id, exc)
        # Ensure detector is running with the CURRENT config — add_detector is
        # idempotent (no-op if one already exists), so we must remove the old
        # one first to pick up edits to detect_config_json, privacy_masks, or
        # camera name. Without this, zone/sensitivity changes only take effect
        # on next service restart.
        try:
            _remove_detector(camera_id)
        except Exception as exc:
            log.warning("remove_detector before reload cam=%s failed: %s", camera_id, exc)
        try:
            _add_detector(
                camera_id=row["id"],
                camera_name=row["name"],
                detect_config_json=row["detect_config_json"],
                privacy_masks_json=row["privacy_masks_json"] or "[]",
            )
        except Exception as exc:
            log.warning("add_detector cam=%s failed: %s", camera_id, exc)
    else:
        # Camera was disabled — stop its recorder and detector.
        try:
            _remove_recorder(camera_id)
        except Exception as exc:
            log.warning("remove_recorder cam=%s failed: %s", camera_id, exc)
        try:
            _remove_detector(camera_id)
        except Exception as exc:
            log.warning("remove_detector cam=%s failed: %s", camera_id, exc)


def _row_to_out(row) -> dict:
    try:
        cam_type = row["cam_type"] or "rtsp"
    except (IndexError, KeyError):
        cam_type = "rtsp"
    # cam_make added in migration 9; fall back gracefully on un-migrated DBs.
    try:
        cam_make = row["cam_make"] or "generic"
    except (IndexError, KeyError):
        cam_make = "generic"
    # cam_group added in migration 5.
    try:
        cam_group = row["cam_group"] or ""
    except (IndexError, KeyError):
        cam_group = ""
    try:
        ptz_capable = bool(row["ptz_capable"])
    except (IndexError, KeyError):
        ptz_capable = False
    try:
        ptz_config_json = row["ptz_config_json"] or "{}"
    except (IndexError, KeyError):
        ptz_config_json = "{}"
    try:
        privacy_masks_json = row["privacy_masks_json"] or "[]"
    except (IndexError, KeyError):
        privacy_masks_json = "[]"
    return {
        "id": row["id"],
        "name": row["name"],
        "host": row["host"],
        "rtsp_port": row["rtsp_port"],
        "rtsp_user": row["rtsp_user"],
        "main_path": row["main_path"],
        "sub_path": row["sub_path"],
        "channel": row["channel"],
        "record_mode": row["record_mode"],
        "enabled": bool(row["enabled"]),
        "detect_config_json": row["detect_config_json"],
        "cam_type": cam_type,
        "cam_make": cam_make,
        "cam_group": cam_group,
        "created_at": row["created_at"],
        "retention_days": row["retention_days"] if "retention_days" in row.keys() else None,
        "ptz_capable": ptz_capable,
        "ptz_config_json": ptz_config_json,
        "privacy_masks_json": privacy_masks_json,
    }


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.get("")
async def list_cameras() -> list[dict]:
    db = get_db()
    rows = db.execute("SELECT * FROM cameras ORDER BY id").fetchall()
    return [_row_to_out(r) for r in rows]


@router.post("", status_code=201, dependencies=[Depends(require_role("admin", "operator"))])
async def add_camera(body: CameraCreate) -> dict:
    db = get_db()
    enc_pass = encrypt_password(body.rtsp_pass)
    cur = db.execute(
        """
        INSERT INTO cameras
            (name, host, rtsp_port, rtsp_user, rtsp_pass_enc,
             main_path, sub_path, channel, record_mode, enabled, detect_config_json,
             cam_type, cam_make, cam_group, retention_days,
             ptz_capable, ptz_config_json, privacy_masks_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            body.name, body.host, body.rtsp_port, body.rtsp_user, enc_pass,
            body.main_path, body.sub_path, body.channel, body.record_mode,
            int(body.enabled), body.detect_config_json,
            body.cam_type, body.cam_make, body.cam_group, body.retention_days,
            int(body.ptz_capable), body.ptz_config_json, body.privacy_masks_json,
        ),
    )
    db.commit()
    camera_id = cur.lastrowid
    log.info("camera added id=%s name=%s host=%s", camera_id, body.name, body.host)

    # Reload go2rtc and reconcile recorders/detectors.
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: _apply_camera_changes(camera_id=camera_id)
    )

    row = db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
    return _row_to_out(row)


@router.get("/{camera_id}")
async def get_camera(camera_id: int) -> dict:
    db = get_db()
    row = db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Camera not found")
    return _row_to_out(row)


@router.put("/{camera_id}", dependencies=[Depends(require_role("admin", "operator"))])
async def update_camera(camera_id: int, body: CameraUpdate) -> dict:
    db = get_db()
    row = db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Camera not found")

    updates: dict = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.host is not None:
        updates["host"] = body.host
    if body.rtsp_port is not None:
        updates["rtsp_port"] = body.rtsp_port
    if body.rtsp_user is not None:
        updates["rtsp_user"] = body.rtsp_user
    if body.rtsp_pass:  # only update if non-empty; empty = keep existing
        updates["rtsp_pass_enc"] = encrypt_password(body.rtsp_pass)
    if body.main_path is not None:
        updates["main_path"] = body.main_path
    if body.sub_path is not None:
        updates["sub_path"] = body.sub_path
    if body.channel is not None:
        updates["channel"] = body.channel
    if body.record_mode is not None:
        updates["record_mode"] = body.record_mode
    if body.enabled is not None:
        updates["enabled"] = int(body.enabled)
    if body.detect_config_json is not None:
        updates["detect_config_json"] = body.detect_config_json
    if body.cam_type is not None:
        updates["cam_type"] = body.cam_type
    if body.cam_make is not None:
        updates["cam_make"] = body.cam_make
    if body.cam_group is not None:
        updates["cam_group"] = body.cam_group
    if "retention_days" in body.model_fields_set:
        updates["retention_days"] = body.retention_days
    if body.ptz_capable is not None:
        updates["ptz_capable"] = int(body.ptz_capable)
    if body.ptz_config_json is not None:
        updates["ptz_config_json"] = body.ptz_config_json
    if body.privacy_masks_json is not None:
        updates["privacy_masks_json"] = body.privacy_masks_json

    if updates:
        set_clause = ", ".join(f"{k}=?" for k in updates)
        db.execute(
            f"UPDATE cameras SET {set_clause} WHERE id=?",
            list(updates.values()) + [camera_id],
        )
        db.commit()
        log.info("camera updated id=%s fields=%s", camera_id, list(updates.keys()))

        # Reload go2rtc and reconcile recorders/detectors.
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: _apply_camera_changes(camera_id=camera_id)
        )

    row = db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
    return _row_to_out(row)


@router.delete("/{camera_id}", status_code=204, dependencies=[Depends(require_role("admin"))])
async def delete_camera(camera_id: int):
    db = get_db()
    row = db.execute("SELECT id, name FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Camera not found")
    db.execute("DELETE FROM cameras WHERE id=?", (camera_id,))
    db.commit()
    log.info("camera deleted id=%s", camera_id)

    # Clean up files on disk for deleted camera
    import shutil as _shutil
    slug = build_go2rtc_stream_name(row["name"])
    for base in (settings.sentinel_recordings_dir, settings.sentinel_events_dir):
        cam_dir = base / slug
        if cam_dir.exists():
            try:
                _shutil.rmtree(cam_dir, ignore_errors=True)
                log.info("delete_camera: removed %s", cam_dir)
            except Exception as exc:
                log.warning("delete_camera: could not remove %s: %s", cam_dir, exc)
    heatmap = settings.sentinel_data_root / "heatmaps" / f"{camera_id}.json"
    if heatmap.exists():
        try:
            heatmap.unlink(missing_ok=True)
        except Exception as exc:
            log.warning("delete_camera: could not remove heatmap: %s", exc)

    # Stop orphan recorder/detector, reload go2rtc without the deleted camera.
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: _apply_camera_changes(camera_id=camera_id, deleted=True)
    )


# ---------------------------------------------------------------------------
# Test endpoint — ffprobe connectivity check
# ---------------------------------------------------------------------------

@router.post("/{camera_id}/test")
async def test_camera(camera_id: int) -> dict:
    """
    Run ffprobe against the camera's main RTSP stream.
    Returns stream info on success, error message on failure.
    """
    db = get_db()
    row = db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Camera not found")

    password = decrypt_password(row["rtsp_pass_enc"])
    rtsp_url = build_main_rtsp_url(
        host=row["host"],
        user=row["rtsp_user"],
        password=password,
        channel=row["channel"],
        port=row["rtsp_port"],
        main_path=row["main_path"],
    )

    cmd = [
        str(settings.ffprobe_path),
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-rtsp_transport", "tcp",
        "-timeout", "5000000",  # 5 seconds in microseconds
        rtsp_url,
    ]

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=10),
        )
        if result.returncode == 0 and result.stdout:
            import json as _json
            info = _json.loads(result.stdout)
            streams = info.get("streams", [])
            return {
                "ok": True,
                "stream_count": len(streams),
                "streams": [
                    {
                        "index": s.get("index"),
                        "codec_name": s.get("codec_name"),
                        "codec_type": s.get("codec_type"),
                        "width": s.get("width"),
                        "height": s.get("height"),
                        "r_frame_rate": s.get("r_frame_rate"),
                    }
                    for s in streams
                ],
            }
        return {"ok": False, "error": result.stderr[:500] or "ffprobe returned no output"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ffprobe timed out (10s)"}
    except FileNotFoundError:
        return {"ok": False, "error": f"ffprobe not found at {settings.ffprobe_path}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Days endpoint — calendar activity for a month
# ---------------------------------------------------------------------------

@router.get("/{camera_id}/days")
async def camera_days(
    camera_id: int,
    month: str = Query(..., description="YYYY-MM"),
) -> list[dict]:
    """
    Return dates within the month that have recordings or events, with counts.
    Powers the calendar date-picker so users can see which days have footage.
    """
    import re as _re
    import calendar as _cal

    if not _re.match(r"^\d{4}-\d{2}$", month):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")

    db = get_db()
    cam = db.execute("SELECT id FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")

    year_s, mon_s = month.split("-")
    year, mon = int(year_s), int(mon_s)
    start_iso = f"{month}-01T00:00:00Z"
    # First day of next month = exclusive upper bound
    if mon == 12:
        next_month = f"{year + 1}-01-01T00:00:00Z"
    else:
        next_month = f"{year}-{mon + 1:02d}-01T00:00:00Z"

    rec_rows = db.execute(
        """
        SELECT substr(started_at, 1, 10) AS day, COUNT(*) AS cnt
        FROM   recordings
        WHERE  camera_id = ?
          AND  started_at >= ?
          AND  started_at <  ?
        GROUP  BY day
        ORDER  BY day
        """,
        (camera_id, start_iso, next_month),
    ).fetchall()

    evt_rows = db.execute(
        """
        SELECT substr(started_at, 1, 10) AS day, COUNT(*) AS cnt
        FROM   events
        WHERE  camera_id = ?
          AND  started_at >= ?
          AND  started_at <  ?
        GROUP  BY day
        ORDER  BY day
        """,
        (camera_id, start_iso, next_month),
    ).fetchall()

    rec_by_day: dict[str, int] = {r["day"]: r["cnt"] for r in rec_rows}
    evt_by_day: dict[str, int] = {r["day"]: r["cnt"] for r in evt_rows}
    all_days = sorted(set(rec_by_day) | set(evt_by_day))

    return [
        {
            "date": day,
            "recordings": rec_by_day.get(day, 0),
            "events": evt_by_day.get(day, 0),
        }
        for day in all_days
    ]


# ---------------------------------------------------------------------------
# Discovery endpoint
# ---------------------------------------------------------------------------

@router.post("/discover")
async def discover_cameras() -> dict:
    """
    Run LAN auto-discovery (ONVIF + SADP + subnet probe) and return found devices.
    Short timeouts — typically completes in 3-5 seconds.
    """
    log.info("starting camera discovery")
    devices = await discover_all(
        onvif_timeout=3.0,
        sadp_timeout=3.0,
        probe_timeout=0.5,
    )
    return {
        "count": len(devices),
        "devices": [
            {
                "ip": d.ip,
                "mac": d.mac,
                "model": d.model,
                "name": d.name,
                "onvif_url": d.onvif_url,
                "open_ports": d.open_ports,
                "source": d.source,
            }
            for d in devices
        ],
    }


# ---------------------------------------------------------------------------
# Camera time sync
# ---------------------------------------------------------------------------

async def _sync_time_onvif(host: str, user: str, password: str, now) -> tuple[bool, str]:
    """Send ONVIF SetSystemDateAndTime (SOAP 1.2, Basic auth) to camera device service."""
    device_url = f"http://{host}/onvif/device_service"
    body = (
        '<tds:SetSystemDateAndTime'
        ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
        ' xmlns:tt="http://www.onvif.org/ver10/schema">'
        '<tds:DateTimeType>Manual</tds:DateTimeType>'
        '<tds:DaylightSavings>false</tds:DaylightSavings>'
        # UTC0 is valid POSIX TZ; bare "UTC" is rejected by some cameras
        '<tds:TimeZone><tt:TZ>UTC0</tt:TZ></tds:TimeZone>'
        '<tds:UTCDateTime>'
        f'<tt:Time><tt:Hour>{now.hour}</tt:Hour><tt:Minute>{now.minute}</tt:Minute><tt:Second>{now.second}</tt:Second></tt:Time>'
        f'<tt:Date><tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month><tt:Day>{now.day}</tt:Day></tt:Date>'
        '</tds:UTCDateTime>'
        '</tds:SetSystemDateAndTime>'
    )
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        f'<s:Body>{body}</s:Body>'
        '</s:Envelope>'
    )
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                device_url,
                content=envelope.encode(),
                headers={"Content-Type": "application/soap+xml; charset=utf-8"},
                auth=(user, password),  # Basic — same as PTZ which works
            )
        if resp.status_code in (200, 204):
            return True, f"HTTP {resp.status_code}"
        return False, f"ONVIF HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return False, f"ONVIF error: {exc}"


async def _sync_time_isapi(host: str, user: str, password: str, now) -> tuple[bool, str]:
    """Set camera time via Hikvision ISAPI PUT /ISAPI/System/time."""
    url = f"http://{host}/ISAPI/System/time"
    # localTime without offset — camera interprets in its own timezone.
    # We set timeZone=UTC so the camera treats this as UTC.
    ts = now.strftime("%Y-%m-%dT%H:%M:%S")
    xml_body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Time version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
        # timeMode must be lowercase — Hikvision ISAPI enum is case-sensitive
        '<timeMode>manual</timeMode>'
        f'<localTime>{ts}</localTime>'
        '<timeZone>UTC+0:00:00</timeZone>'
        '</Time>'
    )
    headers = {"Content-Type": "application/xml"}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            # Digest first (Hikvision default), Basic as fallback
            resp = await client.put(url, content=xml_body.encode(), headers=headers,
                                    auth=httpx.DigestAuth(user, password))
            if resp.status_code == 401:
                resp = await client.put(url, content=xml_body.encode(), headers=headers,
                                        auth=(user, password))
        if resp.status_code in (200, 204):
            return True, f"HTTP {resp.status_code}"
        return False, f"ISAPI HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return False, f"ISAPI error: {exc}"


@router.post("/{camera_id}/sync-time", dependencies=[Depends(require_role("admin", "operator"))])
async def sync_camera_time(camera_id: int) -> dict:
    """
    Set the camera's clock to the server's current UTC time.
    Tries ONVIF SetSystemDateAndTime (SOAP 1.1 + Digest) first;
    falls back to Hikvision ISAPI for hikvision cam_type or cam_make.
    Returns {"ok": true, "method": "onvif"|"isapi", "utc_time": "..."}.
    """
    from datetime import datetime, timezone as _tz
    db = get_db()
    row = db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Camera not found")

    password = decrypt_password(row["rtsp_pass_enc"]) if row["rtsp_pass_enc"] else ""
    user = row["rtsp_user"] or ""
    host = row["host"]
    cam_type = (row["cam_type"] or "rtsp").lower()
    try:
        cam_make = (row["cam_make"] or "generic").lower()
    except (IndexError, KeyError):
        cam_make = "generic"

    now = datetime.now(_tz.utc)
    reasons: list[str] = []

    # Try ONVIF first (SOAP 1.1, Digest auth)
    ok, detail = await _sync_time_onvif(host, user, password, now)
    if ok:
        log.info("sync-time cam=%s via ONVIF ok", camera_id)
        return {"ok": True, "method": "onvif", "utc_time": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    reasons.append(detail)
    log.debug("sync-time cam=%s ONVIF: %s", camera_id, detail)

    # Fall back to Hikvision ISAPI (Digest auth)
    is_hikvision = "hikvision" in cam_type or "hikvision" in cam_make
    if is_hikvision:
        ok, detail = await _sync_time_isapi(host, user, password, now)
        if ok:
            log.info("sync-time cam=%s via ISAPI ok", camera_id)
            return {"ok": True, "method": "isapi", "utc_time": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
        reasons.append(detail)
        log.debug("sync-time cam=%s ISAPI: %s", camera_id, detail)

    raise HTTPException(
        status_code=502,
        detail=f"Time sync failed: {'; '.join(reasons)}",
    )
