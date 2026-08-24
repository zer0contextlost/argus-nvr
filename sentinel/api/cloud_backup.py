"""
sentinel/api/cloud_backup.py — Cloud backup via rclone.

Endpoints:
  POST /api/cloud-backup/trigger    — start a backup now (async subprocess)
  GET  /api/cloud-backup/history    — list recent backup runs
  GET  /api/cloud-backup/status     — latest run status

Requires rclone to be installed and configured.
rclone config is stored at sentinel_data_root/rclone.conf.
The remote destination is read from settings key 'cloud_backup_remote'
(e.g. 'gdrive:argus-backups').
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sentinel.config import settings
from sentinel.db import get_db, open_db
from sentinel.util.auth import get_current_user
from sentinel.util.logging import get_logger

log = get_logger("sentinel.api.cloud_backup")
router = APIRouter(prefix="/cloud-backup", tags=["cloud-backup"])

_backup_lock = threading.Lock()
_backup_running = False


def _rclone_path() -> str:
    """Find rclone on PATH or common locations."""
    import shutil
    rc = shutil.which("rclone")
    if rc:
        return rc
    for candidate in [r"C:\rclone\rclone.exe", r"C:\Program Files\rclone\rclone.exe"]:
        if Path(candidate).exists():
            return candidate
    raise FileNotFoundError("rclone not found. Install from https://rclone.org")


def _rclone_config_path() -> Path:
    p = settings.sentinel_data_root / "rclone.conf"
    return p


def _run_backup_thread(run_id: int, remote: str, source_dir: str) -> None:
    """Background thread: runs rclone sync and updates cloud_backup_runs."""
    global _backup_running
    db = open_db()
    try:
        rclone = _rclone_path()
        config = str(_rclone_config_path())
        cmd = [
            rclone,
            "sync",
            source_dir,
            remote,
            "--config", config,
            "--stats-log-level", "NOTICE",
            "--stats", "30s",
        ]
        log.info("cloud backup starting: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        ended = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if result.returncode == 0:
            db.execute(
                "UPDATE cloud_backup_runs SET status='ok', ended_at=? WHERE id=?",
                (ended, run_id),
            )
            log.info("cloud backup OK run_id=%s", run_id)
        else:
            error_msg = (result.stderr or result.stdout or "")[-500:]
            db.execute(
                "UPDATE cloud_backup_runs SET status='error', ended_at=?, error_msg=? WHERE id=?",
                (ended, error_msg, run_id),
            )
            log.warning("cloud backup FAILED run_id=%s: %s", run_id, error_msg)
    except Exception as exc:
        ended = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.execute(
            "UPDATE cloud_backup_runs SET status='error', ended_at=?, error_msg=? WHERE id=?",
            (ended, str(exc)[:500], run_id),
        )
        log.warning("cloud backup exception run_id=%s: %s", run_id, exc)
    finally:
        db.commit()
        db.close()
        _backup_running = False


@router.post("/trigger")
async def trigger_backup(current_user: dict = Depends(get_current_user)) -> dict:
    """Start a cloud backup now. Returns immediately; backup runs in background."""
    global _backup_running
    if current_user.get("role") not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="Operator or admin role required")

    with _backup_lock:
        if _backup_running:
            return {"ok": False, "error": "A backup is already running"}

        db = get_db()
        remote_row = db.execute(
            "SELECT value FROM settings WHERE key='cloud_backup_remote'"
        ).fetchone()
        remote = remote_row["value"] if remote_row else ""
        if not remote:
            return {"ok": False, "error": "cloud_backup_remote not configured in Settings"}

        source_dir = str(settings.sentinel_recordings_dir)
        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur = db.execute(
            "INSERT INTO cloud_backup_runs(started_at, status) VALUES (?, 'running')",
            (started,),
        )
        db.commit()
        run_id = cur.lastrowid

        # Mark running while still holding the lock so a second request
        # racing the thread start can't slip through.
        _backup_running = True

    t = threading.Thread(target=_run_backup_thread, args=(run_id, remote, source_dir), daemon=True)
    t.start()

    return {"ok": True, "run_id": run_id, "started_at": started}


@router.get("/history")
async def backup_history(
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Return recent cloud backup run history."""
    db = get_db()
    rows = db.execute(
        """
        SELECT id, started_at, ended_at, status, bytes_transferred, files_transferred, error_msg
        FROM cloud_backup_runs
        ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return {
        "runs": [
            {
                "id": r["id"],
                "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "status": r["status"],
                "bytes_transferred": r["bytes_transferred"],
                "files_transferred": r["files_transferred"],
                "error_msg": r["error_msg"],
            }
            for r in rows
        ]
    }


@router.get("/status")
async def backup_status(current_user: dict = Depends(get_current_user)) -> dict:
    """Return the status of the latest backup run."""
    global _backup_running
    db = get_db()
    row = db.execute(
        "SELECT * FROM cloud_backup_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return {"has_run": False, "running": _backup_running}
    return {
        "has_run": True,
        "running": _backup_running,
        "id": row["id"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "status": row["status"],
        "bytes_transferred": row["bytes_transferred"],
        "error_msg": row["error_msg"],
    }
