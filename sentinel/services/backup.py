"""
sentinel/services/backup.py — Daily SQLite backup service.

Creates a timestamped copy of the production DB using sqlite3.Connection.backup()
(online backup API — safe while the DB is in use under WAL mode).  Keeps the
most recent BACKUP_KEEP_COUNT files and removes older ones automatically.

Backup files land at: <data_root>/backups/argus-YYYY-MM-DD.db
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sentinel.config import settings
from sentinel.util.logging import get_logger

log = get_logger("sentinel.backup")

_DEFAULT_INTERVAL_S = 86400.0  # 24 hours

_stop_event: Optional[asyncio.Event] = None
_task: Optional[asyncio.Task] = None


def _backup_dir() -> Path:
    return settings.sentinel_data_root / "backups"


def run_backup() -> Path:
    """
    Run one backup synchronously.  Returns the path of the created file.
    Safe to call at any time — sqlite3.Connection.backup() works while
    the DB is open in WAL mode.
    """
    bdir = _backup_dir()
    bdir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest = bdir / f"argus-{ts}.db"

    src_path = str(settings.sentinel_db_path)

    # Checkpoint WAL before backup. PASSIVE mode does not block concurrent
    # writers (unlike TRUNCATE which acquires an exclusive lock).
    with sqlite3.connect(src_path, check_same_thread=False) as pre:
        pre.execute("PRAGMA wal_checkpoint(PASSIVE)")

    with sqlite3.connect(src_path, check_same_thread=False) as src:
        with sqlite3.connect(str(dest)) as dst:
            src.backup(dst)

    size_mb = dest.stat().st_size / 1_000_000
    log.info("backup: wrote %s (%.1f MB)", dest.name, size_mb)

    # Integrity check — catch silent corruption immediately.
    try:
        with sqlite3.connect(str(dest), check_same_thread=False) as chk:
            result = chk.execute("PRAGMA quick_check").fetchone()
        if result and result[0] == "ok":
            log.info("backup: integrity check OK for %s", dest.name)
        else:
            log.error("backup: integrity check FAILED for %s: %s", dest.name, result)
    except Exception as exc:
        log.error("backup: integrity check error for %s: %s", dest.name, exc)

    # Prune: keep only the newest BACKUP_KEEP_COUNT files.
    keep = settings.backup_keep_count
    backups = sorted(bdir.glob("argus-*.db"))
    for old in backups[:-keep]:
        try:
            old.unlink()
            log.info("backup: removed old backup %s", old.name)
        except OSError as exc:
            log.warning("backup: could not remove %s: %s", old.name, exc)

    return dest


async def _backup_loop() -> None:
    global _stop_event
    log.info("backup: loop started, interval=%.0fs", _DEFAULT_INTERVAL_S)

    while _stop_event is not None and not _stop_event.is_set():
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, run_backup)
        except Exception as exc:
            log.error("backup: error: %s", exc, exc_info=True)

        # Sleep in small slices so stop responds quickly.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _DEFAULT_INTERVAL_S
        while (_stop_event is not None and not _stop_event.is_set()
               and loop.time() < deadline):
            await asyncio.sleep(min(60.0, _DEFAULT_INTERVAL_S))


async def start_backup_service() -> None:
    """Start the daily backup background task. Called from lifespan."""
    global _stop_event, _task
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(_backup_loop(), name="backup-service")
    log.info("backup: service started")


async def stop_backup_service() -> None:
    """Signal the backup task to stop and wait for it. Called from lifespan."""
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
    log.info("backup: service stopped")
