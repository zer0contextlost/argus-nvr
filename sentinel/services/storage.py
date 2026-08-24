"""
sentinel/services/storage.py — Storage retention manager (Phase 3).

Background job that runs every STORAGE_CHECK_INTERVAL_S seconds and enforces:

  1. Max-age pruning: delete recordings whose ended_at is older than
     now - RETENTION_MAX_AGE_DAYS.  Rows with NULL ended_at are treated as
     old = now - started_at (conservative; avoids deleting open segments).

  2. Free-space-floor enforcement on F:: while free_pct(F:) is below
     RETENTION_FREE_SPACE_FLOOR_PCT, delete the single oldest segment
     (by ended_at ASC NULLS LAST, then started_at ASC) and repeat until
     the drive is back above the floor — or no more deletable rows remain.

Safety rules
------------
- NEVER delete the newest segment per camera (the one FFmpeg is most
  likely still writing).  We keep at least the N newest segments per
  camera untouched (SAFETY_KEEP_NEWEST_PER_CAMERA, default 2) to avoid
  evicting segments that were just opened.
- If a file is missing on disk (already deleted externally), the DB row
  is still removed for consistency.
- Tiny / zero-frame orphan files (<= TINY_FILE_MAX_BYTES, default 512)
  that are NOT the newest segment for their camera are treated as
  shutdown-time stubs and deleted (file + row).
- All deletions cascade via SQLite FK ON DELETE CASCADE to linked
  events.recording_id (SET NULL) and notifications rows.

Disk-dashboard helpers
----------------------
  get_storage_status() -> dict
    disk_{used,free,total}_bytes  — shutil.disk_usage on F:
    recordings_total_bytes        — SUM(size_bytes) from DB
    segment_count                 — COUNT(*) from recordings
    gb_per_day                    — rate estimate from last 24 h of rows
    projected_retention_days      — free_bytes / (gb_per_day × 1e9) if rate > 0
    retention_max_age_days        — from config
    free_space_floor_pct          — from config
    free_pct                      — current free / total × 100
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sentinel.config import settings
from sentinel.db import get_db, open_db
from sentinel.util.logging import get_logger

log = get_logger("sentinel.storage")

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# How often the maintenance loop runs (seconds). Configurable via .env as
# STORAGE_CHECK_INTERVAL_S; falls back to this default.
_DEFAULT_CHECK_INTERVAL_S: float = 300.0  # 5 minutes

# Number of newest segments per camera to never delete during SPACE pruning.
# Keeps the currently-open segment AND the most-recently-completed one so
# the space cleaner never evicts what FFmpeg is actively writing or a segment
# whose indexer row may not be fully committed yet.
SAFETY_KEEP_NEWEST_PER_CAMERA: int = 2

# Number of newest segments per camera to protect during AGE pruning.
# We keep only the single newest because age-expired segments genuinely
# need to be removed; we just avoid deleting the currently-open file.
SAFETY_KEEP_NEWEST_AGE_PRUNE: int = 1

# Files at or below this size are considered tiny "stub" files left by
# FFmpeg at an abnormal shutdown and are eligible for cleanup (unless they
# are the newest segment for their camera).
TINY_FILE_MAX_BYTES: int = 512


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_interval() -> float:
    """Return the configured or default check interval in seconds."""
    try:
        raw = getattr(settings, "storage_check_interval_s", None)
        if raw is not None:
            return float(raw)
    except (TypeError, ValueError):
        pass
    return _DEFAULT_CHECK_INTERVAL_S


def _recordings_drive() -> Path:
    """The root path whose disk usage we monitor (the F: drive root)."""
    root = settings.sentinel_recordings_dir
    # Walk up to the drive root on Windows (e.g. F:\), or use root on POSIX.
    return Path(root.anchor)


def _disk_usage() -> shutil.usage_result:
    """Return shutil.disk_usage for the recordings drive."""
    return shutil.disk_usage(str(_recordings_drive()))


def _free_pct(usage: shutil.usage_result) -> float:
    """Free space as a percentage of total."""
    if usage.total == 0:
        return 100.0
    return usage.free / usage.total * 100.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _safe_ids(
    camera_ids: list[int],
    db: sqlite3.Connection,
    keep: int = SAFETY_KEEP_NEWEST_PER_CAMERA,
) -> set[int]:
    """
    Return the set of recording IDs that must NOT be deleted — the
    `keep` most-recent segments for each camera.

    Args:
        camera_ids: list of camera IDs to consider.
        db: database connection.
        keep: number of newest segments per camera to protect.
              Defaults to SAFETY_KEEP_NEWEST_PER_CAMERA (used by space pruning).
              Pass SAFETY_KEEP_NEWEST_AGE_PRUNE for age pruning.
    """
    safe: set[int] = set()
    if not camera_ids:
        return safe
    for cam_id in camera_ids:
        rows = db.execute(
            """
            SELECT id FROM recordings
            WHERE camera_id = ?
            ORDER BY COALESCE(ended_at, started_at) DESC, id DESC
            LIMIT ?
            """,
            (cam_id, keep),
        ).fetchall()
        for r in rows:
            safe.add(r["id"])
    return safe


def _all_camera_ids(db: sqlite3.Connection) -> list[int]:
    rows = db.execute("SELECT DISTINCT camera_id FROM recordings").fetchall()
    return [r["camera_id"] for r in rows]


def _delete_recording(row_id: int, file_path: str, db: sqlite3.Connection) -> bool:
    """
    Delete one recording: remove the DB row first, then unlink the file.
    Returns True on success.

    Row is deleted before the file so a crash after commit leaves an orphaned
    file (reclaimed by space pruning) rather than a dangling DB reference
    pointing at a missing file.

    SQLite FKs handle cascades:
      events.recording_id  -> ON DELETE SET NULL   (row kept, link cleared)
      notifications        -> ON DELETE CASCADE via events (if events deleted)
    """
    try:
        db.execute("DELETE FROM recordings WHERE id = ?", (row_id,))
        db.commit()
    except sqlite3.Error as exc:
        log.error("storage: DB delete error for recording id=%s: %s", row_id, exc)
        return False

    p = Path(file_path)
    try:
        if p.exists():
            p.unlink()
            log.debug("storage: deleted file %s", p)
        else:
            log.debug("storage: file already gone %s", p)
    except OSError as exc:
        log.warning("storage: could not delete %s: %s", p, exc)
    return True


def _batch_delete_recordings(ids_and_paths: list[tuple[int, str]], db: sqlite3.Connection) -> int:
    """Remove DB rows first, then unlink files. Returns count deleted."""
    if not ids_and_paths:
        return 0
    # Commit the DB deletions before touching the filesystem so a crash leaves
    # orphaned files (harmless) rather than dangling DB rows (broken references).
    placeholders = ",".join("?" * len(ids_and_paths))
    ids = [r[0] for r in ids_and_paths]
    try:
        db.execute(f"DELETE FROM recordings WHERE id IN ({placeholders})", ids)
        db.commit()
    except sqlite3.Error as exc:
        log.error("storage: batch delete error: %s", exc)
        return 0
    for _row_id, file_path in ids_and_paths:
        p = Path(file_path)
        try:
            if p.exists():
                p.unlink()
        except OSError as exc:
            log.warning("storage: could not delete %s: %s", p, exc)
    return len(ids_and_paths)


# ---------------------------------------------------------------------------
# Tiny-file orphan cleanup
# ---------------------------------------------------------------------------

def _cleanup_tiny_orphans(db: sqlite3.Connection) -> int:
    """
    Find and remove tiny stub files (size_bytes <= TINY_FILE_MAX_BYTES or
    size_bytes IS NULL *and* the physical file is tiny) that are NOT the
    newest segment for their camera.

    These are left by FFmpeg when it is killed mid-open before writing any
    real frames.  Returns the number of rows deleted.

    Uses SAFETY_KEEP_NEWEST_AGE_PRUNE (1) so only the single newest segment per
    camera is protected — tiny-file cleanup should not shield the second-newest.
    """
    cam_ids = _all_camera_ids(db)
    safe = _safe_ids(cam_ids, db, keep=SAFETY_KEEP_NEWEST_AGE_PRUNE)

    # Candidates: rows with very small recorded size or NULL size.
    if safe:
        in_clause = "AND id NOT IN ({})".format(",".join("?" * len(safe)))
        params = [TINY_FILE_MAX_BYTES] + list(safe)
    else:
        in_clause = ""
        params = [TINY_FILE_MAX_BYTES]

    candidates = db.execute(
        f"""
        SELECT id, camera_id, path, size_bytes
        FROM recordings
        WHERE (size_bytes IS NULL OR size_bytes <= ?)
          {in_clause}
        """,
        params,
    ).fetchall()

    # Check physical file sizes and collect candidates for batch deletion.
    to_delete: list[tuple[int, str]] = []
    for row in candidates:
        p = Path(row["path"])
        if p.exists():
            try:
                physical_size: int = p.stat().st_size
            except OSError:
                physical_size = row["size_bytes"] or 0
        else:
            physical_size = 0  # file gone; still clean up the row

        effective_size = physical_size if row["size_bytes"] is None else (row["size_bytes"] or 0)
        if effective_size <= TINY_FILE_MAX_BYTES:
            log.info(
                "storage: removing tiny/orphan segment id=%s path=%s size=%s",
                row["id"], row["path"], effective_size,
            )
            to_delete.append((row["id"], row["path"]))

    deleted = _batch_delete_recordings(to_delete, db)
    if deleted:
        log.info("storage: cleaned up %d tiny/orphan segment(s)", deleted)
    return deleted


# ---------------------------------------------------------------------------
# Age-based pruning
# ---------------------------------------------------------------------------

def _prune_by_age(db: sqlite3.Connection, dry_run: bool = False) -> int:
    """
    Delete recordings older than per-camera or global retention threshold.
    cameras.retention_days overrides settings.retention_max_age_days when set.
    Event-linked recordings get extended protection up to retention_event_min_age_days.
    """
    now = _utcnow()
    event_min_age_days = settings.retention_event_min_age_days
    event_cutoff = now - timedelta(days=event_min_age_days)
    event_cutoff_str = event_cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    global_max_age = settings.retention_max_age_days

    cam_rows = db.execute("SELECT id, retention_days FROM cameras").fetchall()
    cam_retention: dict[int, int] = {
        r["id"]: int(r["retention_days"]) if r["retention_days"] is not None else global_max_age
        for r in cam_rows
    }

    cam_ids = _all_camera_ids(db)
    safe = _safe_ids(cam_ids, db, keep=SAFETY_KEEP_NEWEST_AGE_PRUNE)

    total_deleted = 0
    for cam_id in cam_ids:
        max_age_days = cam_retention.get(cam_id, global_max_age)
        cutoff = now - timedelta(days=max_age_days)
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

        if safe:
            in_clause = "AND id NOT IN ({})".format(",".join("?" * len(safe)))
            params: list = [cam_id, cutoff_str, event_cutoff_str] + list(safe)
        else:
            in_clause = ""
            params = [cam_id, cutoff_str, event_cutoff_str]

        query = f"""
            SELECT id, path
            FROM recordings
            WHERE camera_id = ?
              AND COALESCE(ended_at, started_at) < ?
              AND NOT (
                COALESCE(ended_at, started_at) >= ?
                AND EXISTS (SELECT 1 FROM events WHERE events.recording_id = recordings.id)
              )
              {in_clause}
            ORDER BY COALESCE(ended_at, started_at) ASC
        """
        rows = db.execute(query, params).fetchall()
        if not rows:
            continue

        log.info(
            "storage: age prune cam=%s — cutoff=%s max_age=%dd candidates=%d",
            cam_id, cutoff_str, max_age_days, len(rows),
        )
        if dry_run:
            total_deleted += len(rows)
            continue
        deleted = _batch_delete_recordings([(r["id"], r["path"]) for r in rows], db)
        total_deleted += deleted

    if total_deleted:
        log.info("storage: age prune deleted %d segment(s) total", total_deleted)
    return total_deleted


# ---------------------------------------------------------------------------
# Free-space-floor enforcement
# ---------------------------------------------------------------------------

def _prune_for_space(db: sqlite3.Connection, _disk_usage_fn=None) -> int:
    """
    While free space on the recordings drive is below
    RETENTION_FREE_SPACE_FLOOR_PCT%, delete the oldest segment (by
    COALESCE(ended_at, started_at) ASC) and repeat until back above the
    floor or no more deletable rows exist.

    _disk_usage_fn: injectable shutil.disk_usage replacement for tests.
    Returns the number of rows deleted.
    """
    floor_pct = settings.retention_free_space_floor_pct
    get_usage = _disk_usage_fn or _disk_usage

    usage = get_usage()
    current_free_pct = _free_pct(usage)

    if current_free_pct >= floor_pct:
        return 0

    log.warning(
        "storage: free space %.1f%% is below floor %.1f%% — starting cleanup",
        current_free_pct, floor_pct,
    )

    deleted = 0
    failed_ids: set[int] = set()  # skip rows whose deletion keeps failing

    # We already know we're below the floor from the check above.
    # Loop: delete one segment, refresh usage, repeat until satisfied.
    while True:
        # Recompute safe set each iteration — the newest segment shifts
        # after each deletion.
        cam_ids = _all_camera_ids(db)
        safe = _safe_ids(cam_ids, db)
        skip_ids = safe | failed_ids

        if skip_ids:
            in_clause = "WHERE id NOT IN ({})".format(",".join("?" * len(skip_ids)))
            q_params = list(skip_ids)
        else:
            in_clause = ""
            q_params = []

        row = db.execute(
            f"""
            SELECT id, path, size_bytes
            FROM recordings
            {in_clause}
            ORDER BY COALESCE(ended_at, started_at) ASC, id ASC
            LIMIT 1
            """,
            q_params,
        ).fetchone()

        if row is None:
            log.warning(
                "storage: no more deletable segments but free space is still "
                "%.1f%% (below floor %.1f%%) — cannot free more space",
                current_free_pct, floor_pct,
            )
            break

        log.info(
            "storage: space floor %.1f%% not met (free %.1f%%) — deleting "
            "oldest segment id=%s path=%s size=%s",
            floor_pct, current_free_pct,
            row["id"], row["path"], row["size_bytes"],
        )
        if _delete_recording(row["id"], row["path"], db):
            deleted += 1
        else:
            failed_ids.add(row["id"])

        # Re-check free space after each deletion.
        usage = get_usage()
        current_free_pct = _free_pct(usage)
        if current_free_pct >= floor_pct:
            log.info(
                "storage: space floor satisfied — free=%.1f%% floor=%.1f%%",
                current_free_pct, floor_pct,
            )
            break

    if deleted:
        log.info("storage: space-floor cleanup deleted %d segment(s)", deleted)
    return deleted


# ---------------------------------------------------------------------------
# Disk / storage dashboard
# ---------------------------------------------------------------------------

def get_storage_status() -> dict:
    """
    Return a dict with current disk and retention statistics for the API.

    Keys:
      disk_total_bytes, disk_used_bytes, disk_free_bytes, free_pct
      recordings_total_bytes, segment_count
      gb_per_day                — rolling 24h write rate (bytes converted to GB)
      projected_retention_days  — how many more days recordings can grow
      retention_max_age_days    — configured max age
      free_space_floor_pct      — configured floor
    """
    try:
        usage = _disk_usage()
        disk_total = usage.total
        disk_used = usage.used
        disk_free = usage.free
        free_pct = _free_pct(usage)
    except OSError as exc:
        log.warning("storage: disk_usage error: %s", exc)
        disk_total = disk_used = disk_free = 0
        free_pct = 0.0

    db = get_db()

    # Total size of all indexed recordings.
    row = db.execute("SELECT COUNT(*) AS cnt, SUM(size_bytes) AS total FROM recordings").fetchone()
    segment_count = row["cnt"] or 0
    recordings_total_bytes = row["total"] or 0

    # GB/day estimate: sum of size_bytes for recordings that ended in the
    # past 24 hours.  This gives a rolling write-rate proxy.
    cutoff_24h = (_utcnow() - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rate_row = db.execute(
        """
        SELECT SUM(size_bytes) AS recent_bytes
        FROM recordings
        WHERE COALESCE(ended_at, started_at) >= ?
        """,
        (cutoff_24h,),
    ).fetchone()
    recent_bytes = rate_row["recent_bytes"] or 0
    gb_per_day = recent_bytes / 1e9  # bytes in the last 24h ≈ GB/day

    # Projected retention: how many more days until floor is hit.
    bytes_per_day = recent_bytes  # already per 24h window
    usable_free = disk_free - (disk_total * settings.retention_free_space_floor_pct / 100.0)
    if bytes_per_day > 0 and usable_free > 0:
        projected_retention_days = usable_free / bytes_per_day
    elif bytes_per_day == 0:
        projected_retention_days = float("inf")
    else:
        projected_retention_days = 0.0

    return {
        "disk_total_bytes": disk_total,
        "disk_used_bytes": disk_used,
        "disk_free_bytes": disk_free,
        "free_pct": round(free_pct, 2),
        "recordings_total_bytes": recordings_total_bytes,
        "segment_count": segment_count,
        "gb_per_day": round(gb_per_day, 4),
        "projected_retention_days": round(projected_retention_days, 1)
        if projected_retention_days != float("inf")
        else None,
        "retention_max_age_days": settings.retention_max_age_days,
        "free_space_floor_pct": settings.retention_free_space_floor_pct,
    }


# ---------------------------------------------------------------------------
# Low-disk-space alerting
# ---------------------------------------------------------------------------

_low_space_notified = False  # True once an alert has been fired; reset on recovery
_LOW_SPACE_DB_KEY = "_storage_low_space_notified"


def _persist_low_space_flag(value: bool) -> None:
    """Write the low-space flag to the DB so it survives a crash/restart."""
    try:
        db = open_db()
        try:
            db.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                (_LOW_SPACE_DB_KEY, "1" if value else "0"),
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        log.debug("storage: could not persist low-space flag: %s", exc)


def _load_low_space_flag() -> None:
    """Read the persisted low-space flag from DB at startup."""
    global _low_space_notified
    try:
        db = open_db()
        try:
            row = db.execute(
                "SELECT value FROM settings WHERE key=?", (_LOW_SPACE_DB_KEY,)
            ).fetchone()
            if row:
                _low_space_notified = row[0] == "1"
        finally:
            db.close()
    except Exception:
        pass


def _check_and_alert_low_space() -> None:
    """
    Compare current free space against NOTIFY_STORAGE_WARN_PCT.
    Fires one alert when space drops below the threshold; resets when recovered.
    The notified state is persisted to the DB so a crash/restart doesn't
    trigger a duplicate alert when space is still low.
    """
    global _low_space_notified
    try:
        usage = _disk_usage()
        free = _free_pct(usage)
    except OSError as exc:
        log.warning("storage: disk_usage error in alert check: %s", exc)
        return

    warn_pct = settings.notify_storage_warn_pct

    if free < warn_pct:
        if not _low_space_notified:
            _low_space_notified = True
            _persist_low_space_flag(True)
            log.warning(
                "storage: low disk space — free=%.1f%% threshold=%.0f%%",
                free, warn_pct,
            )
            try:
                from sentinel.services.notifier import send_system_alert
                send_system_alert(
                    "Low Disk Space",
                    f"Recordings drive is {free:.1f}% free "
                    f"(alert threshold: {warn_pct}%). "
                    "Consider reducing retention or adding storage.",
                )
            except Exception as exc:
                log.error("storage: low-space alert send failed: %s", exc)
    else:
        if _low_space_notified:
            _low_space_notified = False
            _persist_low_space_flag(False)
            log.info("storage: disk space recovered — free=%.1f%%", free)


# ---------------------------------------------------------------------------
# Main maintenance cycle
# ---------------------------------------------------------------------------

def run_maintenance(db: Optional[sqlite3.Connection] = None) -> dict:
    """
    Run one full maintenance cycle synchronously:
      1. Clean up tiny/orphan stub files.
      2. Prune by max age (with event-priority extension).
      3. Prune for free-space floor.
      4. Check disk space and fire alert if below warn threshold.

    Returns a summary dict with counts.  Safe to call from tests with a
    supplied `db` connection.

    When called from a thread-pool executor (background task), pass db=None;
    this function opens its own dedicated connection to avoid sharing the
    event-loop connection across threads.
    """
    _owns_db = db is None
    _db = db if db is not None else open_db()
    try:
        return _run_maintenance_inner(_db)
    finally:
        if _owns_db:
            try:
                _db.close()
            except Exception:
                pass


def _run_maintenance_inner(_db: sqlite3.Connection) -> dict:

    tiny_deleted = _cleanup_tiny_orphans(_db)
    age_deleted = _prune_by_age(_db)
    space_deleted = _prune_for_space(_db)
    _check_and_alert_low_space()

    summary = {
        "tiny_orphans_deleted": tiny_deleted,
        "age_pruned": age_deleted,
        "space_pruned": space_deleted,
        "total_deleted": tiny_deleted + age_deleted + space_deleted,
    }
    log.info("storage: maintenance cycle complete: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Background asyncio task (wired into main.py lifespan)
# ---------------------------------------------------------------------------

_stop_event: Optional[asyncio.Event] = None
_task: Optional[asyncio.Task] = None


async def _maintenance_loop() -> None:
    """Asyncio task that runs maintenance on a periodic timer."""
    global _stop_event
    interval = _check_interval()
    log.info("storage: maintenance loop started, interval=%.0fs", interval)

    while _stop_event is not None and not _stop_event.is_set():
        try:
            # Run the blocking DB + filesystem work in the default executor so
            # it does not block the event loop.
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, run_maintenance)
        except Exception as exc:
            log.error("storage: maintenance cycle error: %s", exc, exc_info=True)

        # Sleep in small slices so we can respond to stop quickly.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + interval
        while (_stop_event is not None and not _stop_event.is_set()
               and loop.time() < deadline):
            await asyncio.sleep(min(5.0, interval))


async def start_storage_manager() -> None:
    """Start the background storage maintenance task.  Called from lifespan."""
    global _stop_event, _task
    _stop_event = asyncio.Event()
    # Restore the low-space alert flag from DB so we don't re-fire after a
    # crash when space is still below threshold.
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _load_low_space_flag)
    _task = asyncio.create_task(_maintenance_loop(), name="storage-manager")
    log.info("storage: manager started")


async def stop_storage_manager() -> None:
    """Signal the maintenance task to stop and wait for it.  Called from lifespan."""
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
    log.info("storage: manager stopped")
