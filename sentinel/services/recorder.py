"""
sentinel/services/recorder.py — Per-camera FFmpeg segment recorder (Phase 2).

Architecture
------------
One CameraRecorder per enabled camera:
  - Builds the FFmpeg command to pull rtsp://127.0.0.1:8554/<stream_name>
    (go2rtc restream) and write segmented MP4 to
    F:\\sentinel\\recordings\\<cam_slug>\\<YYYY-MM-DD>\\%H%M%S.mp4
  - Supervises the FFmpeg process via ProcessSupervisor (restart on crash).
  - A background SegmentIndexer thread watches the camera's recording directory,
    detects completed segments (new file = previous file is closed), probes them
    with ffprobe, and inserts rows into the `recordings` DB table.
  - On restart, the indexer compares the new segment's start time to the last
    `ended_at` for that camera; when the gap exceeds GAP_THRESHOLD_S it sets
    has_gap=1.

Record modes
------------
  continuous          — gate always OPEN; FFmpeg runs 24/7.
  continuous+events   — same as continuous in Phase 2 (events wired in Phase 4).
  motion-only         — gate defaults OPEN in Phase 2 so the recorder is
                        testable; Phase 4 will drive the gate closed/open based
                        on motion detection.

NOTE (Phase 4 integration point): Each CameraRecorder exposes `open_gate()` /
`close_gate()` methods.  When the gate is closed the supervisor is stopped;
when opened it is started again.  Phase 4's motion/detector service will call
these methods.  In Phase 2 the gate is unconditionally open for all modes.

Segment indexing
----------------
Completed-segment detection works by watching for new *.mp4 files to appear in
the output directory.  When a new file appears the *previous* file is considered
closed and is indexed.  On shutdown the recorder explicitly indexes the last
(possibly partial) segment.

Gap detection
-------------
On each recorder (re)start the indexer queries the last `ended_at` for the
camera.  If `now - ended_at > GAP_THRESHOLD_S` it marks the incoming segment
has_gap=1 when that row is eventually inserted.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sentinel.config import settings
from sentinel.db import get_db, open_db
from sentinel.services.supervisor import ProcessSupervisor, RestartPolicy
from sentinel.util.hikvision import build_go2rtc_stream_name
from sentinel.util.logging import get_logger

log = get_logger("sentinel.recorder")

# A gap larger than this many seconds between the end of the last recorded
# segment and the start of the new one is considered a recording discontinuity.
GAP_THRESHOLD_S: float = 5.0

# How often (seconds) the segment indexer polls the directory for new files.
POLL_INTERVAL_S: float = 2.0

# Seconds to keep the FFmpeg → go2rtc RTSP consumer alive after the motion gate
# closes. Frequent motion otherwise restarts FFmpeg on every event, adding and
# tearing down a go2rtc consumer each time; that churn races go2rtc 1.9.14's
# track teardown and triggers the nil-pointer panic in mp4/consumer.go. Holding
# the consumer open through brief lulls collapses the churn into one stable
# connection per camera.
RECORDER_LINGER_S: float = 90.0

# How often (seconds) to age-prune the pre-buffer directory while it runs
# unattended (no motion opening the gate). Kept short relative to typical
# motion_pre_buffer_seconds settings so the on-disk window stays tight even
# through a long motion-free stretch.
PREBUFFER_HOUSEKEEPING_INTERVAL_S: float = 30.0

# ---------------------------------------------------------------------------
# Module-level registry: camera_id -> CameraRecorder
# ---------------------------------------------------------------------------
_recorders: dict[int, "CameraRecorder"] = {}
_registry_lock = threading.Lock()


# ---------------------------------------------------------------------------
# ffprobe helper
# ---------------------------------------------------------------------------

def _probe_segment(path: Path) -> dict:
    """
    Run ffprobe on a completed segment file.

    Returns a dict with keys:
      duration_s  (float | None)
      started_at  (str ISO-8601 | None) — derived from filename
      ended_at    (str ISO-8601 | None) — started_at + duration
    """
    cmd = [
        str(settings.ffprobe_path),
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        if result.returncode != 0 or not result.stdout:
            log.warning("ffprobe failed on %s: %s", path, result.stderr[:200])
            return {}
        data = json.loads(result.stdout)
        fmt = data.get("format", {})
        duration_s = float(fmt["duration"]) if fmt.get("duration") else None
        return {"duration_s": duration_s, "format": fmt, "streams": data.get("streams", [])}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError, ValueError) as exc:
        log.warning("ffprobe error on %s: %s", path, exc)
        return {}


def parse_segment_filename(path: Path) -> Optional[datetime]:
    """
    Parse the local datetime encoded in an FFmpeg strftime segment filename
    and return it as a timezone-aware UTC datetime.

    FFmpeg strftime uses the system local clock, so the filename encodes
    LOCAL time, not UTC.  We parse it as a naive local datetime and then
    convert to UTC via astimezone() (which assumes the system local timezone).

    Expected filename pattern: HHMMSS.mp4 (e.g. 143052.mp4).
    The parent directory encodes the local date: YYYY-MM-DD.
    """
    try:
        date_str = path.parent.name          # e.g. "2026-05-24"
        time_str = path.stem                 # e.g. "143052"
        dt_naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H%M%S")
        # astimezone() on a naive datetime assumes system local tz, then converts to UTC.
        return dt_naive.astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """Convert a datetime to ISO-8601 string, or return None."""
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Segment indexer
# ---------------------------------------------------------------------------

class SegmentIndexer:
    """
    Background thread that watches a camera's recording directory tree for
    completed MP4 segments and inserts rows into the `recordings` DB table.

    Completed-segment detection:
      We track the set of .mp4 files seen.  When a new file appears the
      previously-newest file is considered complete (FFmpeg has moved on to the
      next segment).  On graceful shutdown we index the last file explicitly.
    """

    def __init__(
        self,
        camera_id: int,
        camera_dir: Path,
        ensure_dirs: Optional[list[Path]] = None,
    ):
        self.camera_id = camera_id
        self.camera_dir = camera_dir
        # Directory roots whose local-date subdirs must always exist (see
        # _ensure_date_dirs). Defaults to just the camera recording dir.
        self._ensure_dirs = list(ensure_dirs) if ensure_dirs else [camera_dir]

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._indexed: set[Path] = set()   # files already inserted into DB
        self._seen_order: list[Path] = []  # ordered list of files seen so far
        self._pending_gap: bool = False    # set True when a gap is detected at (re)start

        # Dedicated connection — this indexer runs its own thread, so it must
        # not share the main event-loop connection from get_db() (multiple
        # indexer threads interleaving BEGIN/COMMIT on one shared sqlite3
        # connection corrupts that connection's transaction state). Also
        # touched by the supervisor's restart-watchdog thread via
        # notify_restart(), so guard it with a lock rather than assuming
        # single-threaded access.
        self._db = open_db()
        self._db_lock = threading.Lock()

        # Pre-populate from what's already been indexed in the DB so we don't
        # double-insert on app restart.
        self._preload_indexed()

    def _preload_indexed(self) -> None:
        try:
            from datetime import timedelta
            # Load only paths from the last 30 days (matching the default retention
            # window) so the set stays bounded even on long-running deployments.
            # INSERT OR IGNORE in _insert_segment handles the rare case where an
            # older-than-30-day file is still on disk and somehow re-scanned.
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            with self._db_lock:
                rows = self._db.execute(
                    "SELECT path FROM recordings WHERE camera_id=? AND started_at >= ?",
                    (self.camera_id, cutoff),
                ).fetchall()
            for r in rows:
                self._indexed.add(Path(r["path"]))
        except Exception as exc:
            log.warning("[indexer cam=%s] preload error: %s", self.camera_id, exc)

    def _cleanup_stuck_from_previous_session(self) -> None:
        """
        On (re)start, look at all rows for this camera with ended_at IS NULL —
        those were left "open" by a previous process that died or was killed
        before finalizing the segment. For each one:

          - If the file is missing  → drop the DB row.
          - If the file has no moov → it's unplayable; delete file + DB row.
          - If the file has a moov  → close it: set ended_at to mtime and
            duration_s from (mtime - started_at).

        This stops crashed segments from accumulating across service restarts
        and polluting the timeline / Events / Playback UI with un-loadable
        recordings.
        """
        with self._db_lock:
            db = self._db
            try:
                rows = db.execute(
                    "SELECT id, path, started_at FROM recordings "
                    "WHERE camera_id=? AND ended_at IS NULL",
                    (self.camera_id,),
                ).fetchall()
            except Exception as exc:
                log.warning("[indexer cam=%s] stuck-cleanup query error: %s", self.camera_id, exc)
                return

            if not rows:
                return

            purged = 0
            closed = 0
            for r in rows:
                rid = r["id"]
                path = Path(r["path"])

                if not path.exists():
                    try:
                        db.execute("DELETE FROM recordings WHERE id=?", (rid,))
                        db.commit()
                        self._indexed.discard(path)
                        purged += 1
                    except Exception as exc:
                        log.warning("[indexer cam=%s] could not purge missing-file row %s: %s",
                                    self.camera_id, rid, exc)
                    continue

                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0

                playable = False
                if size >= 4096:
                    try:
                        with open(path, "rb") as fh:
                            head = fh.read(min(512 * 1024, size))
                        playable = b"moov" in head
                    except OSError:
                        playable = False

                if not playable:
                    # Unplayable — delete file + DB row.
                    try:
                        if path.exists():
                            path.unlink()
                    except OSError as exc:
                        log.warning("[indexer cam=%s] could not delete unplayable %s: %s",
                                    self.camera_id, path, exc)
                    try:
                        db.execute("DELETE FROM recordings WHERE id=?", (rid,))
                        db.commit()
                        self._indexed.discard(path)
                        purged += 1
                    except Exception as exc:
                        log.warning("[indexer cam=%s] could not delete DB row %s: %s",
                                    self.camera_id, rid, exc)
                    continue

                # Playable — close the row using mtime as ended_at.
                try:
                    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                except OSError:
                    mtime = datetime.now(timezone.utc)

                started_at_dt = None
                try:
                    started_at_dt = datetime.strptime(
                        r["started_at"], "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    pass

                if started_at_dt is not None:
                    duration_s = max(0.0, (mtime - started_at_dt).total_seconds())
                else:
                    duration_s = None

                try:
                    db.execute(
                        "UPDATE recordings SET ended_at=?, duration_s=?, size_bytes=? WHERE id=?",
                        (_iso(mtime), duration_s, size, rid),
                    )
                    db.commit()
                    closed += 1
                except Exception as exc:
                    log.warning("[indexer cam=%s] could not close row %s: %s",
                                self.camera_id, rid, exc)

        if purged or closed:
            log.info(
                "[indexer cam=%s] startup stuck-cleanup: purged=%d closed=%d",
                self.camera_id, purged, closed,
            )

    def start(self) -> None:
        self._stop_event.clear()
        # Recover from any segments left open by a previous (crashed) session
        # BEFORE detecting the gap, so the gap heuristic sees the correct last
        # ended_at.
        self._cleanup_stuck_from_previous_session()
        self._detect_gap()
        self._thread = threading.Thread(
            target=self._run,
            name=f"indexer-cam{self.camera_id}",
            daemon=True,
        )
        self._thread.start()
        log.info("[indexer cam=%s] started, watching %s", self.camera_id, self.camera_dir)

    def stop(self) -> None:
        """Signal stop and index the last (possibly partial) segment."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10.0)
        # Index the final segment (the one that was still open).
        self._index_final_segment()
        log.info("[indexer cam=%s] stopped", self.camera_id)

    def notify_restart(self) -> None:
        """Call this when the FFmpeg process is restarted to trigger gap detection."""
        self._detect_gap()

    # ------------------------------------------------------------------
    # Gap detection
    # ------------------------------------------------------------------

    def _detect_gap(self) -> None:
        """
        Query the last ended_at for this camera.  If the gap from now is large
        enough, set _pending_gap so the next indexed segment is flagged has_gap=1.
        """
        try:
            with self._db_lock:
                row = self._db.execute(
                    "SELECT ended_at FROM recordings WHERE camera_id=? "
                    "AND ended_at IS NOT NULL "
                    "ORDER BY ended_at DESC LIMIT 1",
                    (self.camera_id,),
                ).fetchone()
            if row and row["ended_at"]:
                last_end_str = row["ended_at"]
                # Parse ISO string
                last_end = datetime.strptime(last_end_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                gap_s = (now - last_end).total_seconds()
                if gap_s > GAP_THRESHOLD_S:
                    log.warning(
                        "[indexer cam=%s] gap detected: %.1fs since last segment ended",
                        self.camera_id, gap_s,
                    )
                    self._pending_gap = True
                else:
                    self._pending_gap = False
            else:
                # No prior recordings — no gap to report.
                self._pending_gap = False
        except Exception as exc:
            log.warning("[indexer cam=%s] gap check error: %s", self.camera_id, exc)
            self._pending_gap = False

    # ------------------------------------------------------------------
    # Main poll loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._scan()
            except Exception as exc:
                log.error("[indexer cam=%s] scan error: %s", self.camera_id, exc)
            self._stop_event.wait(POLL_INTERVAL_S)

    def _ensure_date_dirs(self) -> None:
        """Pre-create today's AND tomorrow's local-date directories.

        FFmpeg's segment muxer does NOT create directories. The date dir used
        to be created only once, when the FFmpeg command was built, so:
          - a recorder running across midnight rolled to a date dir that did
            not exist and crash-looped until its supervisor was rebuilt
            ("Failed to open segment ... No such file or directory"), losing
            whole nights of footage when motion kept the gate open; and
          - the pre-buffer harvest rmdir'd the date dir out from under the
            still-running pre-buffer FFmpeg on nearly every motion event.
        Running this on every 2s scan tick (tomorrow included, so the midnight
        rollover never races) guarantees the directories exist before FFmpeg
        needs them; a crashed FFmpeg then recovers on its next relaunch.
        """
        from datetime import timedelta
        now = datetime.now()  # local time — FFmpeg strftime uses the local clock
        for root in self._ensure_dirs:
            for day in (now, now + timedelta(days=1)):
                try:
                    (root / day.strftime("%Y-%m-%d")).mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    log.warning(
                        "[indexer cam=%s] could not ensure date dir under %s: %s",
                        self.camera_id, root, exc,
                    )

    def _scan(self) -> None:
        """Scan the camera directory tree for new MP4 files."""
        self._ensure_date_dirs()
        found: list[Path] = []
        if self.camera_dir.exists():
            for mp4 in sorted(self.camera_dir.rglob("*.mp4")):
                if mp4 not in self._indexed:
                    found.append(mp4)

        if not found:
            return

        # Sort by name (which encodes time) so ordering is stable.
        found.sort()

        # Merge into seen_order (avoiding duplicates).
        seen_set = set(self._seen_order)
        for f in found:
            if f not in seen_set:
                self._seen_order.append(f)
                seen_set.add(f)

        # All files except the last one are considered complete.
        # (The last one may still be open by FFmpeg.)
        completed = self._seen_order[:-1]
        for path in completed:
            if path not in self._indexed:
                self._insert_segment(path)

        # Prune _seen_order to the last element only. Every earlier entry is
        # now indexed (or was already); retaining the full list grows memory
        # unboundedly over days of continuous recording.
        if len(self._seen_order) > 1:
            self._seen_order = [self._seen_order[-1]]

    def _index_final_segment(self) -> None:
        """
        Index the last seen file. Called both at full indexer shutdown and by
        CameraRecorder._linger_stop() after each individual motion window's
        FFmpeg process exits — otherwise that last segment (often the most
        relevant few minutes of the whole event) sits un-indexed and invisible
        on the timeline/events UI until some later, unrelated file finally
        bumps it out of _seen_order[-1] on the next _scan().

        If the file is tiny (< TINY_SEGMENT_MIN_BYTES) it was almost certainly
        an empty stub created by FFmpeg just before the process was terminated.
        Skip indexing and delete the stub so storage.py's orphan cleaner does
        not have to deal with it later.
        """
        # Run a final scan so we pick up any segment that appeared between the
        # last poll tick and now (e.g. FFmpeg opened a new file just before stop).
        try:
            self._scan()
        except Exception:
            pass

        if not self._seen_order:
            return
        last = self._seen_order[-1]
        if last in self._indexed:
            return

        # Guard against empty/stub segments produced at shutdown.
        try:
            size = last.stat().st_size if last.exists() else 0
        except OSError:
            size = 0

        # 4096 bytes is still far too small to be a real MP4 segment.
        if size < 4096:
            if last.exists():
                try:
                    last.unlink()
                    log.info(
                        "[indexer cam=%s] removed tiny shutdown stub %s (%d bytes)",
                        self.camera_id, last.name, size,
                    )
                except OSError as exc:
                    log.warning(
                        "[indexer cam=%s] could not remove stub %s: %s",
                        self.camera_id, last.name, exc,
                    )
            return

        self._insert_segment(last, force=True)

    def _insert_segment(self, path: Path, force: bool = False) -> None:
        """Probe a completed segment and insert a row into recordings."""
        if path in self._indexed:
            return

        # Wait briefly to let FFmpeg finish writing if this is a freshly closed file.
        if not force:
            time.sleep(0.5)

        try:
            size_bytes = path.stat().st_size if path.exists() else None
        except OSError:
            size_bytes = None

        if size_bytes is not None and size_bytes < 4096:
            # Stub file — FFmpeg wrote the ftyp box but exited before any video frames.
            # Delete it and mark as indexed so it never accumulates or appears in timelines.
            if path.exists():
                try:
                    path.unlink()
                    log.info(
                        "[indexer cam=%s] removed tiny stub %s (%d bytes)",
                        self.camera_id, path.name, size_bytes,
                    )
                except OSError as exc:
                    log.warning(
                        "[indexer cam=%s] could not remove stub %s: %s",
                        self.camera_id, path.name, exc,
                    )
            self._indexed.add(path)
            return

        probe = _probe_segment(path)
        duration_s = probe.get("duration_s")

        started_at_dt = parse_segment_filename(path)
        started_at = _iso(started_at_dt)

        # Clamp the probed duration to the file's wall-clock write window.
        # Camera 5 (and occasionally the Wyze) emits broken video timestamps
        # that jump forward mid-segment; ffprobe then reports hours of
        # "duration" for a seconds-long file. A segment can never be longer
        # than (last write) - (segment open), so use that as the ceiling.
        if duration_s and started_at_dt:
            try:
                mtime_dt = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                wall_s = (mtime_dt - started_at_dt).total_seconds() + 2.0
                if wall_s > 0 and duration_s > wall_s:
                    log.warning(
                        "[indexer cam=%s] %s: probed duration %.1fs exceeds wall-clock "
                        "window %.1fs — clamping (broken camera timestamps)",
                        self.camera_id, path.name, duration_s, wall_s,
                    )
                    duration_s = wall_s
            except OSError:
                pass

        if started_at_dt and duration_s:
            from datetime import timedelta
            ended_at_dt = started_at_dt + timedelta(seconds=duration_s)
            ended_at = _iso(ended_at_dt)
        else:
            ended_at = None

        has_gap = 1 if self._pending_gap else 0
        self._pending_gap = False  # reset after first insert

        try:
            with self._db_lock:
                db = self._db
                cur = db.execute(
                    """
                    INSERT OR IGNORE INTO recordings
                        (camera_id, path, started_at, ended_at, duration_s, size_bytes, has_gap)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.camera_id,
                        str(path),
                        started_at,
                        ended_at,
                        duration_s,
                        size_bytes,
                        has_gap,
                    ),
                )
                db.commit()

                # Backfill events that fired before this segment was indexed.
                # The detector's _find_covering_recording() runs at event-creation
                # time, before the segment row exists, so recording_id is often
                # NULL. Correct it now that the recording row is committed.
                # INSERT OR IGNORE: lastrowid is 0/stale if the row already
                # existed, so only backfill on a genuinely new insert.
                new_rec_id = cur.lastrowid if cur.rowcount > 0 else 0
                if new_rec_id and started_at:
                    backfill_end = ended_at or _iso(datetime.now(timezone.utc))
                    db.execute(
                        """
                        UPDATE events
                           SET recording_id = ?
                         WHERE camera_id = ?
                           AND recording_id IS NULL
                           AND started_at >= ?
                           AND started_at < ?
                        """,
                        (new_rec_id, self.camera_id, started_at, backfill_end),
                    )
                    db.commit()
            self._indexed.add(path)
            log.info(
                "[indexer cam=%s] indexed %s duration=%.1fs gap=%s",
                self.camera_id,
                path.name,
                duration_s or 0,
                bool(has_gap),
            )
        except sqlite3.Error as exc:
            log.error("[indexer cam=%s] DB insert error: %s", self.camera_id, exc)


# ---------------------------------------------------------------------------
# Motion gate interface (Phase 4 integration point)
# ---------------------------------------------------------------------------

class MotionGate:
    """
    Simple gate that controls whether recording is active.

    Phase 4: gate is OPEN by default for continuous modes and starts CLOSED
    for motion-only mode.  The detector service calls open() / close() to
    gate recording on motion.

    For continuous and continuous+events modes the gate remains permanently
    open and the detector does not touch it.
    """

    def __init__(self, record_mode: str = "continuous") -> None:
        # motion-only: start closed; detector opens on first motion
        # continuous / continuous+events: always open
        if record_mode == "motion-only":
            self._open = False
        else:
            self._open = True
        self._record_mode = record_mode

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        """Open the gate (start/resume recording)."""
        self._open = True

    def close(self) -> None:
        """Close the gate (pause recording).  Only effective for motion-only mode."""
        if self._record_mode == "motion-only":
            self._open = False
        else:
            log.debug("MotionGate.close() ignored for mode=%s", self._record_mode)


# ---------------------------------------------------------------------------
# Per-camera recorder
# ---------------------------------------------------------------------------

class CameraRecorder:
    """
    Manages the FFmpeg segment recorder and the segment indexer for one camera.

    Lifecycle:
      start() — start FFmpeg supervisor + indexer
      stop()  — stop FFmpeg + index final segment
    """

    def __init__(
        self,
        camera_id: int,
        camera_name: str,
        record_mode: str,
    ):
        self.camera_id = camera_id
        self.camera_name = camera_name
        self.record_mode = record_mode

        # Derive the go2rtc RTSP restream URL using the same slug scheme.
        self._stream_name = build_go2rtc_stream_name(camera_name)
        self._rtsp_url = (
            f"rtsp://127.0.0.1:{settings.go2rtc_rtsp_port}/{self._stream_name}"
        )

        # Output directory root: <data_root>/recordings/<slug>/
        self._cam_dir = settings.sentinel_recordings_dir / self._stream_name
        self._cam_dir.mkdir(parents=True, exist_ok=True)

        # FFmpeg supervisor
        self._supervisor: Optional[ProcessSupervisor] = None

        # Pre-buffer supervisor (motion-only mode only): always-on short-segment
        # FFmpeg that fills a rolling window so open_gate() can harvest recent
        # footage before the main recorder starts.
        self._prebuffer_supervisor: Optional[ProcessSupervisor] = None
        self._prebuffer_dir = (
            settings.sentinel_recordings_dir / ".prebuffer" / self._stream_name
        )

        # Segment indexer. It also keeps the local-date subdirs alive for both
        # the main and pre-buffer output roots (FFmpeg cannot create them).
        self._indexer = SegmentIndexer(
            camera_id=camera_id,
            camera_dir=self._cam_dir,
            ensure_dirs=[self._cam_dir, self._prebuffer_dir],
        )

        # Motion gate — starts closed for motion-only, open for continuous modes
        self._gate = MotionGate(record_mode=record_mode)

        # Pending linger-stop timer (motion-only): scheduled on gate close,
        # cancelled if the gate reopens before it fires.
        self._linger_timer: Optional[threading.Timer] = None
        self._lifecycle_lock = threading.Lock()

        # Periodic pre-buffer housekeeping timer: age-prunes .prebuffer segments
        # while the pre-buffer runs unattended (no motion). _collect_prebuffer()
        # is otherwise only invoked from open_gate(), so a camera with no motion
        # for longer than the pre-buffer window would accumulate full-bitrate
        # segments forever — they carry no DB row, so storage.py's space-based
        # retention can't see them either. See PREBUFFER_HOUSEKEEPING_INTERVAL_S.
        self._prebuffer_housekeeping_timer: Optional[threading.Timer] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the FFmpeg recorder and segment indexer."""
        self._indexer.start()
        # For motion-only mode the gate starts closed; the pre-buffer recorder
        # runs continuously so that footage leading up to the first motion event
        # can be harvested when the gate opens.
        if self._gate.is_open:
            self._start_supervisor()
        else:
            self._start_prebuffer()
            log.info(
                "[recorder cam=%s name=%r mode=%s] gate closed — pre-buffer running",
                self.camera_id, self.camera_name, self.record_mode,
            )
        log.info(
            "[recorder cam=%s name=%r mode=%s] started (gate=%s)",
            self.camera_id, self.camera_name, self.record_mode, self._gate.is_open,
        )

    def stop(self) -> None:
        """Stop FFmpeg cleanly and index the final segment."""
        with self._lifecycle_lock:
            if self._linger_timer is not None:
                self._linger_timer.cancel()
                self._linger_timer = None
            if self._supervisor:
                self._supervisor.stop()
                self._supervisor = None
            self._stop_prebuffer()
            # Stop the indexer inside the lock so open_gate() cannot restart the
            # supervisor between our supervisor.stop() and indexer.stop() calls,
            # which would leave a running supervisor with a dead indexer.
            self._indexer.stop()
        log.info("[recorder cam=%s] stopped", self.camera_id)

    def open_gate(self) -> None:
        """Open the motion gate (start recording).  Phase 4 integration point."""
        self._gate.open()
        with self._lifecycle_lock:
            # Stop the pre-buffer recorder before starting the main one — we
            # only want one RTSP consumer per camera at a time.
            self._stop_prebuffer()
            # Harvest AFTER stopping the pre-buffer so its final segment — the
            # one holding the seconds immediately before the trigger — is closed
            # on disk and gets collected too. Previously harvest ran while the
            # pre-buffer was still writing, so the open chunk was skipped
            # (segments[:-1]) and then deleted on the next event, guaranteeing
            # the loss of the moment of the trigger. Fragmented MP4 stays
            # playable even if FFmpeg was cut mid-segment.
            self._collect_prebuffer(include_last=True)
            # A pending linger-stop means FFmpeg is still alive from the previous
            # motion window — cancel the stop and reuse the existing consumer.
            if self._linger_timer is not None:
                self._linger_timer.cancel()
                self._linger_timer = None
            if self._supervisor is None:
                self._start_supervisor()

    def close_gate(self) -> None:
        """Close the motion gate (pause recording for motion-only cameras)."""
        was_open = self._gate.is_open
        self._gate.close()
        if not (was_open and not self._gate.is_open):
            return
        with self._lifecycle_lock:
            if self._supervisor is None or self._linger_timer is not None:
                return
            log.info(
                "[recorder cam=%s] gate closed — keeping FFmpeg alive for %.0fs",
                self.camera_id, RECORDER_LINGER_S,
            )
            self._linger_timer = threading.Timer(
                RECORDER_LINGER_S, self._linger_stop
            )
            self._linger_timer.daemon = True
            self._linger_timer.start()

    def _linger_stop(self) -> None:
        """Fired by the linger timer: stop FFmpeg if the gate is still closed."""
        with self._lifecycle_lock:
            self._linger_timer = None
            if self._gate.is_open:
                return
            if self._supervisor is not None:
                log.info("[recorder cam=%s] linger expired — stopping FFmpeg", self.camera_id)
                self._supervisor.stop()
                self._supervisor = None
                # supervisor.stop() blocks until FFmpeg has fully exited, so the
                # segment it was writing is finalized on disk — index it now
                # instead of leaving it invisible until the next motion event.
                try:
                    self._indexer._index_final_segment()
                except Exception as exc:
                    log.warning("[recorder cam=%s] final-segment index after linger failed: %s",
                                self.camera_id, exc)
            # Restart the pre-buffer so footage is available for the next motion event.
            if self.record_mode == "motion-only":
                self._start_prebuffer()

    @property
    def is_recording(self) -> bool:
        """True when the main FFmpeg supervisor is actively recording."""
        return self._gate.is_open and self._supervisor is not None

    def status(self) -> dict:
        """Return status dict for the health endpoint."""
        sup_status = self._supervisor.status() if self._supervisor else {"status": "stopped", "pid": None, "restarts": 0}
        return {
            "camera_id": self.camera_id,
            "camera_name": self.camera_name,
            "record_mode": self.record_mode,
            "stream_url": self._rtsp_url,
            "output_dir": str(self._cam_dir),
            "gate_open": self._gate.is_open,
            **sup_status,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_prebuffer_cmd(self) -> list[str]:
        """FFmpeg command for the short-segment pre-buffer recorder."""
        # Keep segment chunks SHORT and FIXED (5s) so the pre-buffer window has
        # fine-grained rollover. The pre-buffer WINDOW length is set by
        # settings.motion_pre_buffer_seconds and enforced via the delete
        # threshold in _collect_prebuffer(). Coupling them (previous behaviour)
        # capped the window at 15s and made larger window settings ineffective.
        seg_secs = 5
        today_dir = self._prebuffer_dir / datetime.now().strftime("%Y-%m-%d")
        today_dir.mkdir(parents=True, exist_ok=True)
        pattern = str(self._prebuffer_dir / "%Y-%m-%d" / "%H%M%S.mp4")
        return [
            str(settings.ffmpeg_path),
            "-rtsp_transport", "tcp",
            "-i", self._rtsp_url,
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "32k",
            "-f", "segment",
            "-segment_time", str(seg_secs),
            "-segment_atclocktime", "1",
            "-reset_timestamps", "1",
            "-strftime", "1",
            "-segment_format", "mp4",
            "-segment_format_options", "movflags=frag_keyframe+default_base_moof",
            "-hide_banner", "-loglevel", "error",
            pattern,
        ]

    def _start_prebuffer(self) -> None:
        """Start the always-on pre-buffer recorder (motion-only mode only)."""
        if self._prebuffer_supervisor is not None:
            return
        self._prebuffer_dir.mkdir(parents=True, exist_ok=True)
        log_file = settings.sentinel_log_dir / f"prebuffer_cam{self.camera_id}.log"
        self._prebuffer_supervisor = ProcessSupervisor(
            name=f"prebuffer-cam{self.camera_id}",
            cmd=self._build_prebuffer_cmd(),
            restart_policy=RestartPolicy.ALWAYS,
            max_restarts=200,
            initial_backoff=2.0,
            max_backoff=30.0,
            log_file=log_file,
            register_global=False,
        )
        self._prebuffer_supervisor.start()
        log.info("[recorder cam=%s] pre-buffer recorder started", self.camera_id)
        self._schedule_prebuffer_housekeeping()

    def _schedule_prebuffer_housekeeping(self) -> None:
        """(Re)arm the periodic pre-buffer age-pruning timer."""
        self._prebuffer_housekeeping_timer = threading.Timer(
            PREBUFFER_HOUSEKEEPING_INTERVAL_S, self._prebuffer_housekeeping_tick
        )
        self._prebuffer_housekeeping_timer.daemon = True
        self._prebuffer_housekeeping_timer.start()

    def _prebuffer_housekeeping_tick(self) -> None:
        """
        Periodic no-motion cleanup: age-prune the pre-buffer directory the same
        way open_gate() does on a real trigger, so a camera with no motion for
        a long stretch doesn't accumulate full-bitrate segments indefinitely.
        """
        with self._lifecycle_lock:
            if self._prebuffer_supervisor is None:
                return  # gate opened / recorder stopped since this fired
            try:
                self._collect_prebuffer(include_last=False, harvest=False)
            except Exception as exc:
                log.warning("[recorder cam=%s] pre-buffer housekeeping failed: %s",
                            self.camera_id, exc)
            self._schedule_prebuffer_housekeeping()

    def _stop_prebuffer(self) -> None:
        """Stop the pre-buffer recorder (must be called under _lifecycle_lock or from stop())."""
        if self._prebuffer_housekeeping_timer is not None:
            self._prebuffer_housekeeping_timer.cancel()
            self._prebuffer_housekeeping_timer = None
        sup = self._prebuffer_supervisor
        self._prebuffer_supervisor = None
        if sup is not None:
            sup.stop()
            log.debug("[recorder cam=%s] pre-buffer recorder stopped", self.camera_id)

    def _collect_prebuffer(self, include_last: bool = False, harvest: bool = True) -> None:
        """
        Age-prune the pre-buffer directory, deleting segments older than the
        pre-buffer window.

        include_last: when True, consider every segment including the newest
        one. Only safe once the pre-buffer FFmpeg has been stopped (see
        open_gate) — that final chunk holds the footage right before the
        trigger, so dropping it loses the start of the event. When False
        (periodic housekeeping while the pre-buffer is still running), the
        newest file may still be open by FFmpeg, so it is skipped.

        harvest: when True (a real motion trigger via open_gate), segments
        still within the window are additionally moved into the main
        recordings directory so the SegmentIndexer picks up the pre-roll
        footage. When False (periodic no-motion housekeeping, called from
        _prebuffer_housekeeping_tick), in-window segments are left alone —
        promoting them into the permanent recording without any actual
        motion would silently turn motion-only mode into continuous
        recording.
        """
        if not self._prebuffer_dir.exists():
            return
        now = time.time()
        threshold = now - settings.motion_pre_buffer_seconds

        segments = sorted(self._prebuffer_dir.rglob("*.mp4"))
        # Skip the last file only when the pre-buffer FFmpeg may still hold it open.
        if segments and not include_last:
            segments = segments[:-1]

        moved = 0
        for seg in segments:
            try:
                mtime = seg.stat().st_mtime
            except OSError:
                continue
            if mtime < threshold:
                try:
                    seg.unlink(missing_ok=True)
                    # Remove empty PAST date dirs only. rmdir'ing today's dir
                    # yanks it out from under the still-running pre-buffer
                    # FFmpeg, which then dies ("Failed to open segment") on
                    # its next 5s rollover — this fired on nearly every
                    # motion event before 2026-07-02.
                    if seg.parent.name != datetime.now().strftime("%Y-%m-%d"):
                        try:
                            seg.parent.rmdir()
                        except OSError:
                            pass
                except OSError:
                    pass
                continue
            if not harvest:
                continue  # within window, no motion — leave it in the pre-buffer
            # Within the pre-buffer window — move to main recordings dir.
            # Pre-buffer uses the same YYYY-MM-DD/HHMMSS.mp4 layout so the
            # indexer's parse_segment_filename() works without any changes.
            dest_dir = self._cam_dir / seg.parent.name
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / seg.name
            try:
                seg.rename(dest)
                moved += 1
            except OSError as exc:
                log.debug("[recorder cam=%s] prebuffer rename error: %s", self.camera_id, exc)

        if moved:
            log.info(
                "[recorder cam=%s] harvested %d pre-buffer segment(s) before motion",
                self.camera_id, moved,
            )

    def _build_ffmpeg_cmd(self) -> list[str]:
        """
        Build the FFmpeg command for segmented MP4 recording.

        Output path uses strftime so FFmpeg names each segment with the
        wall-clock time it was started:
          F:\\sentinel\\recordings\\<slug>\\<YYYY-MM-DD>\\<HHMMSS>.mp4

        FFmpeg does NOT create directories for strftime output paths — every
        date directory must exist before FFmpeg opens a segment in it, or the
        segment muxer fails with "No such file or directory" and the process
        exits. We pre-create today's dir here for the initial launch; the
        SegmentIndexer keeps today's AND tomorrow's dirs alive on every scan
        tick so the midnight rollover (and supervisor relaunches with this
        same command) never hit a missing directory.
        """
        # Pre-create today's date directory so FFmpeg can write immediately.
        # Use local time to match FFmpeg's strftime which also uses local time.
        today_dir = self._cam_dir / datetime.now().strftime("%Y-%m-%d")
        today_dir.mkdir(parents=True, exist_ok=True)

        segment_pattern = str(self._cam_dir / "%Y-%m-%d" / "%H%M%S.mp4")

        cmd = [
            str(settings.ffmpeg_path),
            # Input options
            "-rtsp_transport", "tcp",
            "-i", self._rtsp_url,
            # Video: stream copy (no re-encode).
            # Audio: transcode to AAC — G.711 (pcm_mulaw/pcm_alaw) is not
            # valid in the MP4 container, so we must convert it.
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "32k",
            # Segmented MP4
            "-f", "segment",
            "-segment_time", str(settings.segment_seconds),
            "-segment_atclocktime", "1",
            "-reset_timestamps", "1",
            "-strftime", "1",
            # Fragmented MP4: each fragment is self-contained, so a segment
            # interrupted mid-write (process kill, crash, gate close) is still
            # playable from the start up to the last completed fragment.
            #
            # Previously we used `movflags=+faststart`, which only writes the
            # moov atom when the segment closes cleanly — any interruption
            # produced an unplayable file (ftyp+free+mdat with no moov),
            # creating a massive backlog of "ghost" segments on the timeline.
            #
            # Do NOT add `empty_moov` here — that variant emits a moov with
            # timescale=0 which Chrome rejects. The flags below produce a real
            # initialization moov plus moof/mdat fragment pairs that browsers
            # handle correctly via Range requests.
            "-segment_format", "mp4",
            "-segment_format_options", "movflags=frag_keyframe+default_base_moof",
            # Suppress banner / progress noise
            "-hide_banner",
            "-loglevel", "error",
            segment_pattern,
        ]
        return cmd

    def _start_supervisor(self) -> None:
        """Create and start the ProcessSupervisor for FFmpeg."""
        log_file = settings.sentinel_log_dir / f"recorder_cam{self.camera_id}.log"
        cmd = self._build_ffmpeg_cmd()

        # Wrap the standard ProcessSupervisor with a callback so the indexer
        # is notified on each restart (for gap detection).
        self._supervisor = _RestartNotifyingSupervisor(
            name=f"recorder-cam{self.camera_id}",
            cmd=cmd,
            restart_policy=RestartPolicy.ALWAYS,
            max_restarts=200,
            initial_backoff=2.0,
            max_backoff=30.0,
            log_file=log_file,
            on_restart=self._on_ffmpeg_restart,
            register_global=False,
        )
        self._supervisor.start()

    def _on_ffmpeg_restart(self) -> None:
        """Called by the supervisor watchdog each time FFmpeg is restarted."""
        log.warning("[recorder cam=%s] FFmpeg restarted — checking for gap", self.camera_id)
        self._indexer.notify_restart()


# ---------------------------------------------------------------------------
# Restart-notifying supervisor subclass
# ---------------------------------------------------------------------------

class _RestartNotifyingSupervisor(ProcessSupervisor):
    """
    ProcessSupervisor subclass that calls an on_restart callback each time
    the child process is restarted by the watchdog.
    """

    def __init__(self, *args, on_restart=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_restart = on_restart

    def _watch(self) -> None:
        """Override watchdog loop to fire on_restart callback after each restart."""
        backoff = self.initial_backoff

        while not self._stop_event.is_set():
            proc = None
            with self._lock:
                proc = self._proc
            if proc is None:
                time.sleep(0.5)
                continue

            rc = proc.wait()

            if self._stop_event.is_set():
                break

            log.warning("[%s] exited with code %s", self.name, rc)

            should_restart = (
                self.restart_policy == RestartPolicy.ALWAYS
                or (self.restart_policy == RestartPolicy.ON_FAILURE and rc != 0)
            )

            if not should_restart:
                with self._lock:
                    from sentinel.services.supervisor import ProcessStatus
                    self._status = ProcessStatus.STOPPED
                break

            if self._restarts >= self.max_restarts:
                with self._lock:
                    from sentinel.services.supervisor import ProcessStatus
                    self._status = ProcessStatus.ERROR
                log.error("[%s] max restarts (%s) reached", self.name, self.max_restarts)
                break

            with self._lock:
                from sentinel.services.supervisor import ProcessStatus
                self._status = ProcessStatus.RESTARTING
                self._restarts += 1

            log.info(
                "[%s] restarting in %.1fs (attempt %s/%s)",
                self.name, backoff, self._restarts, self.max_restarts,
            )

            # Fire the restart callback before sleeping.
            if self._on_restart:
                try:
                    self._on_restart()
                except Exception as exc:
                    log.warning("[%s] on_restart callback error: %s", self.name, exc)

            self._stop_event.wait(backoff)
            if self._stop_event.is_set():
                break

            backoff = min(backoff * 2, self.max_backoff)

            with self._lock:
                self._launch()


# ---------------------------------------------------------------------------
# Module-level lifecycle (called from main.py)
# ---------------------------------------------------------------------------

def start_recorders() -> None:
    """
    Load all enabled cameras from the DB and start a CameraRecorder for each.
    Called during FastAPI lifespan startup, after go2rtc is up.
    """
    global _recorders
    db = get_db()
    cameras = db.execute(
        "SELECT id, name, record_mode FROM cameras WHERE enabled=1"
    ).fetchall()

    with _registry_lock:
        for cam in cameras:
            cam_id = cam["id"]
            if cam_id in _recorders:
                log.info("[recorder] cam=%s already running, skipping", cam_id)
                continue
            rec = CameraRecorder(
                camera_id=cam_id,
                camera_name=cam["name"],
                record_mode=cam["record_mode"],
            )
            rec.start()
            _recorders[cam_id] = rec

    log.info("[recorder] started %d recorder(s)", len(cameras))


def stop_recorders() -> None:
    """Stop all running recorders.  Called during FastAPI lifespan shutdown."""
    global _recorders
    with _registry_lock:
        for cam_id, rec in list(_recorders.items()):
            try:
                rec.stop()
            except Exception as exc:
                log.error("[recorder] error stopping cam=%s: %s", cam_id, exc)
        _recorders.clear()
    log.info("[recorder] all recorders stopped")


def recorder_statuses() -> list[dict]:
    """Return status dicts for all recorders (for the health endpoint)."""
    with _registry_lock:
        return [rec.status() for rec in _recorders.values()]


def get_recorder(camera_id: int) -> "CameraRecorder | None":
    """Return the CameraRecorder for a camera, or None if not running."""
    with _registry_lock:
        return _recorders.get(camera_id)


def add_recorder(camera_id: int, camera_name: str, record_mode: str) -> None:
    """
    Start a recorder for a newly added/enabled camera.
    Idempotent: if already running, does nothing.
    """
    with _registry_lock:
        if camera_id in _recorders:
            return
        rec = CameraRecorder(camera_id=camera_id, camera_name=camera_name, record_mode=record_mode)
        rec.start()
        _recorders[camera_id] = rec
    log.info("[recorder] added recorder for cam=%s", camera_id)


def remove_recorder(camera_id: int) -> None:
    """Stop and remove the recorder for a camera."""
    with _registry_lock:
        rec = _recorders.pop(camera_id, None)
    if rec:
        rec.stop()
        log.info("[recorder] removed recorder for cam=%s", camera_id)
