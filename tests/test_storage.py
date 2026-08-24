"""
tests/test_storage.py — Unit + integration tests for Phase 3 storage manager.

Test coverage:
  - Age-based pruning: only rows older than the cutoff are deleted.
  - Free-space-floor: oldest segments deleted first until floor is met.
  - Newest-segment safety: SAFETY_KEEP_NEWEST_PER_CAMERA segments per camera
    are never deleted regardless of age or space pressure.
  - Tiny/orphan file cleanup: <=512-byte stubs removed, but newest kept.
  - DB sync: no rows pointing to deleted files; deleted files have no row.
  - get_storage_status returns expected keys and sane values.
  - run_maintenance integration: all three phases in one call.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure settings loads without a real .env.
os.environ.setdefault("SENTINEL_SECRET_KEY", "test-secret-for-storage-tests")


# ---------------------------------------------------------------------------
# DB / fixture helpers
# ---------------------------------------------------------------------------

def _make_db(fk: bool = True) -> sqlite3.Connection:
    """
    Create a minimal in-memory SQLite DB matching the Phase 2 schema.
    Foreign keys enabled by default (matching production).
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    fk_pragma = "ON" if fk else "OFF"
    conn.executescript(
        f"""
        PRAGMA foreign_keys={fk_pragma};

        CREATE TABLE cameras (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        );

        CREATE TABLE recordings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id   INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
            path        TEXT    NOT NULL,
            started_at  TEXT,
            ended_at    TEXT,
            duration_s  REAL,
            size_bytes  INTEGER,
            has_gap     INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id    INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
            type         TEXT    NOT NULL DEFAULT 'motion',
            label        TEXT,
            score        REAL,
            started_at   TEXT    NOT NULL,
            ended_at     TEXT,
            thumb_path   TEXT,
            clip_path    TEXT,
            recording_id INTEGER REFERENCES recordings(id) ON DELETE SET NULL,
            meta_json    TEXT    NOT NULL DEFAULT '{{}}'
        );

        CREATE TABLE notifications (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            channel  TEXT    NOT NULL DEFAULT 'telegram',
            status   TEXT    NOT NULL DEFAULT 'pending',
            sent_at  TEXT
        );

        INSERT INTO cameras(name) VALUES ('cam_a');
        INSERT INTO cameras(name) VALUES ('cam_b');
        """
    )
    conn.commit()
    return conn


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _dt(days_ago: float = 0, seconds_ago: float = 0) -> datetime:
    """Return a UTC datetime offset from now."""
    return datetime.now(timezone.utc) - timedelta(days=days_ago, seconds=seconds_ago)


def _insert_recording(
    conn: sqlite3.Connection,
    *,
    camera_id: int,
    path: str,
    days_ago: float,
    size_bytes: int = 100_000,
    duration_s: float = 120.0,
) -> int:
    """Insert a synthetic recording row and return its ID."""
    ended_at_dt = _dt(days_ago)
    started_at_dt = ended_at_dt - timedelta(seconds=duration_s)
    cur = conn.execute(
        """
        INSERT INTO recordings(camera_id, path, started_at, ended_at, duration_s, size_bytes, has_gap)
        VALUES (?, ?, ?, ?, ?, ?, 0)
        """,
        (camera_id, path, _iso(started_at_dt), _iso(ended_at_dt), duration_s, size_bytes),
    )
    conn.commit()
    return cur.lastrowid


def _make_file(directory: Path, name: str, size: int = 100_000) -> Path:
    """Create a dummy file of the given size in directory."""
    p = directory / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00" * size)
    return p


# ---------------------------------------------------------------------------
# Import helpers — patch get_db inside the module under test
# ---------------------------------------------------------------------------

def _storage_module():
    """Import and return the storage module (lazy to pick up env vars)."""
    from sentinel.services import storage
    return storage


# ---------------------------------------------------------------------------
# Tests: age-based pruning
# ---------------------------------------------------------------------------

class TestAgeBasedPruning:
    def test_old_segments_deleted(self, tmp_path):
        """Recordings older than retention_max_age_days are deleted."""
        storage = _storage_module()
        conn = _make_db()

        max_age = 30  # days

        # Old recording (35 days ago) — should be pruned.
        old_file = _make_file(tmp_path, "old.mp4", size=50_000)
        old_id = _insert_recording(conn, camera_id=1, path=str(old_file), days_ago=35)

        # Recent recording (5 days ago) — should be kept (but also safe as newest).
        new_file = _make_file(tmp_path, "new.mp4", size=50_000)
        new_id = _insert_recording(conn, camera_id=1, path=str(new_file), days_ago=5)

        with patch("sentinel.services.storage.get_db", return_value=conn), \
             patch("sentinel.services.storage.settings") as mock_settings:
            mock_settings.retention_max_age_days = max_age
            mock_settings.retention_free_space_floor_pct = 10

            deleted = storage._prune_by_age(conn)

        assert deleted == 1
        # Old row gone.
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (old_id,)).fetchone() is None
        # New row still present.
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (new_id,)).fetchone() is not None
        # File on disk deleted.
        assert not old_file.exists()
        # New file still on disk.
        assert new_file.exists()

    def test_recent_segments_untouched(self, tmp_path):
        """Recordings within retention window are never deleted."""
        storage = _storage_module()
        conn = _make_db()

        # Two recordings both within the 30-day window.
        f1 = _make_file(tmp_path, "a.mp4")
        f2 = _make_file(tmp_path, "b.mp4")
        _insert_recording(conn, camera_id=1, path=str(f1), days_ago=10)
        _insert_recording(conn, camera_id=1, path=str(f2), days_ago=20)

        with patch("sentinel.services.storage.get_db", return_value=conn), \
             patch("sentinel.services.storage.settings") as mock_settings:
            mock_settings.retention_max_age_days = 30
            mock_settings.retention_free_space_floor_pct = 10

            deleted = storage._prune_by_age(conn)

        assert deleted == 0
        assert f1.exists()
        assert f2.exists()

    def test_cutoff_boundary_exclusive(self, tmp_path):
        """A recording exactly at the cutoff (not older) should NOT be deleted."""
        storage = _storage_module()
        conn = _make_db()

        # Exactly 30 days ago — borderline; our query uses < not <=.
        f = _make_file(tmp_path, "border.mp4")
        _insert_recording(conn, camera_id=1, path=str(f), days_ago=30, duration_s=1.0)

        with patch("sentinel.services.storage.get_db", return_value=conn), \
             patch("sentinel.services.storage.settings") as mock_settings:
            mock_settings.retention_max_age_days = 30
            mock_settings.retention_free_space_floor_pct = 10

            deleted = storage._prune_by_age(conn)

        # The file is at exactly 30 days ago; cutoff is now - 30 days, so
        # ended_at < cutoff is False — should NOT be deleted.
        assert deleted == 0

    def test_missing_file_row_still_removed(self, tmp_path):
        """If the file is already gone from disk, the DB row is still cleaned up."""
        storage = _storage_module()
        conn = _make_db()

        ghost_path = str(tmp_path / "ghost.mp4")  # file never created
        ghost_id = _insert_recording(conn, camera_id=1, path=ghost_path, days_ago=40)
        # Also add newest (safe) so the ghost is not the only row.
        safe_file = _make_file(tmp_path, "safe.mp4")
        _insert_recording(conn, camera_id=1, path=str(safe_file), days_ago=1)

        with patch("sentinel.services.storage.get_db", return_value=conn), \
             patch("sentinel.services.storage.settings") as mock_settings:
            mock_settings.retention_max_age_days = 30
            mock_settings.retention_free_space_floor_pct = 10

            deleted = storage._prune_by_age(conn)

        assert deleted == 1
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (ghost_id,)).fetchone() is None

    def test_multiple_cameras_age_prune(self, tmp_path):
        """Old segments for both cameras are pruned."""
        storage = _storage_module()
        conn = _make_db()

        # cam 1: one old, one new
        f1_old = _make_file(tmp_path, "cam1_old.mp4")
        f1_new = _make_file(tmp_path, "cam1_new.mp4")
        _insert_recording(conn, camera_id=1, path=str(f1_old), days_ago=40)
        _insert_recording(conn, camera_id=1, path=str(f1_new), days_ago=2)

        # cam 2: one old, one new
        f2_old = _make_file(tmp_path, "cam2_old.mp4")
        f2_new = _make_file(tmp_path, "cam2_new.mp4")
        _insert_recording(conn, camera_id=2, path=str(f2_old), days_ago=45)
        _insert_recording(conn, camera_id=2, path=str(f2_new), days_ago=3)

        with patch("sentinel.services.storage.get_db", return_value=conn), \
             patch("sentinel.services.storage.settings") as mock_settings:
            mock_settings.retention_max_age_days = 30
            mock_settings.retention_free_space_floor_pct = 10

            deleted = storage._prune_by_age(conn)

        assert deleted == 2
        assert not f1_old.exists()
        assert not f2_old.exists()
        assert f1_new.exists()
        assert f2_new.exists()


# ---------------------------------------------------------------------------
# Tests: free-space-floor enforcement
# ---------------------------------------------------------------------------

class TestFreeSpaceFloor:
    """
    Inject a fake disk_usage function that returns configurable free space.
    After each deletion the "free" space is incremented by the file size that
    was deleted (simulating the OS reclaiming space).
    """

    def _make_usage_fn(self, total: int, initial_free: int, conn: sqlite3.Connection):
        """
        Return a callable that simulates shutil.disk_usage.

        It starts with initial_free and adds back size_bytes for each row that
        has been removed from the DB since the last call.  This mimics the OS
        reclaiming space as files are deleted.
        """
        state = {"free": initial_free, "seen_ids": set()}

        def _fake_usage(_path=None):
            # Look at which IDs have been deleted from the DB since last call.
            current_ids = {
                r["id"] for r in conn.execute("SELECT id FROM recordings").fetchall()
            }
            # If any ID has disappeared it means a deletion happened.
            # We don't track sizes here — simpler: just jump free above floor
            # by a fixed step per deletion.  Tests control initial_free explicitly.
            _Result = type("disk_usage", (), {
                "total": total,
                "used": total - state["free"],
                "free": state["free"],
            })
            return _Result()

        return _fake_usage, state

    def test_oldest_deleted_first(self, tmp_path):
        """When space is low, the OLDEST segment is deleted first."""
        storage = _storage_module()
        conn = _make_db()

        total = 1_000_000_000  # 1 GB total
        floor_pct = 10         # 10% = 100 MB floor
        # Start at 8% free — below floor.  Each file is 40 MB, so one deletion
        # should push us to ~12% free.
        file_size = 40_000_000

        # Insert 3 recordings per camera, ordered oldest to newest.
        old_file = _make_file(tmp_path, "cam1_oldest.mp4", size=file_size)
        mid_file = _make_file(tmp_path, "cam1_mid.mp4",    size=file_size)
        new_file = _make_file(tmp_path, "cam1_newest.mp4", size=file_size)

        old_id = _insert_recording(conn, camera_id=1, path=str(old_file), days_ago=20, size_bytes=file_size)
        mid_id = _insert_recording(conn, camera_id=1, path=str(mid_file), days_ago=10, size_bytes=file_size)
        new_id = _insert_recording(conn, camera_id=1, path=str(new_file), days_ago=1,  size_bytes=file_size)

        # Fake usage: start at 8% free; after one delete, jump to 12%.
        call_count = [0]
        def fake_usage(_path=None):
            call_count[0] += 1
            free = 80_000_000 if call_count[0] <= 1 else 120_000_000
            return type("r", (), {"total": total, "used": total - free, "free": free})()

        with patch("sentinel.services.storage.settings") as mock_settings:
            mock_settings.retention_free_space_floor_pct = floor_pct
            mock_settings.retention_max_age_days = 30

            deleted = storage._prune_for_space(conn, _disk_usage_fn=fake_usage)

        assert deleted == 1
        # Oldest deleted.
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (old_id,)).fetchone() is None
        assert not old_file.exists()
        # Mid and newest still present.
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (mid_id,)).fetchone() is not None
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (new_id,)).fetchone() is not None

    def test_loop_continues_until_floor_met(self, tmp_path):
        """Multiple oldest segments are deleted in sequence until floor is met."""
        storage = _storage_module()
        conn = _make_db()

        total = 1_000_000_000
        floor_pct = 10
        file_size = 30_000_000

        # 4 recordings; 2 oldest are deletable (newest 2 are protected).
        files = []
        ids = []
        for i, days in enumerate([40, 30, 5, 1]):
            f = _make_file(tmp_path, f"seg_{i}.mp4", size=file_size)
            rid = _insert_recording(conn, camera_id=1, path=str(f), days_ago=days, size_bytes=file_size)
            files.append(f)
            ids.append(rid)

        # Simulate: starts at 5%, after 1 delete = 8%, after 2 deletes = 12%.
        call_count = [0]
        def fake_usage(_path=None):
            call_count[0] += 1
            if call_count[0] == 1:
                free = 50_000_000   # 5% — need to delete
            elif call_count[0] == 2:
                free = 80_000_000   # 8% — still need to delete
            else:
                free = 120_000_000  # 12% — above floor, stop
            return type("r", (), {"total": total, "used": total - free, "free": free})()

        with patch("sentinel.services.storage.settings") as mock_settings:
            mock_settings.retention_free_space_floor_pct = floor_pct
            mock_settings.retention_max_age_days = 30

            deleted = storage._prune_for_space(conn, _disk_usage_fn=fake_usage)

        assert deleted == 2
        # Oldest two deleted.
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (ids[0],)).fetchone() is None
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (ids[1],)).fetchone() is None
        assert not files[0].exists()
        assert not files[1].exists()
        # Newest two kept.
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (ids[2],)).fetchone() is not None
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (ids[3],)).fetchone() is not None

    def test_above_floor_no_deletions(self, tmp_path):
        """When free space is already above floor, nothing is deleted."""
        storage = _storage_module()
        conn = _make_db()

        f = _make_file(tmp_path, "keep.mp4")
        _insert_recording(conn, camera_id=1, path=str(f), days_ago=5)

        def fake_usage(_path=None):
            return type("r", (), {"total": 1_000_000_000, "used": 800_000_000, "free": 200_000_000})()

        with patch("sentinel.services.storage.settings") as mock_settings:
            mock_settings.retention_free_space_floor_pct = 10
            mock_settings.retention_max_age_days = 30

            deleted = storage._prune_for_space(conn, _disk_usage_fn=fake_usage)

        assert deleted == 0
        assert f.exists()

    def test_stops_when_no_more_deletable(self, tmp_path):
        """
        If all remaining rows are safety-protected and space is still low,
        the loop exits without error (logs a warning).

        With SAFETY_KEEP_NEWEST_PER_CAMERA=1, a single recording per camera
        is protected.  Use one camera with exactly one recording so no row
        can be deleted.
        """
        storage = _storage_module()
        conn = _make_db()

        # Only 1 recording — it is the newest (only) segment, so it is protected.
        f1 = _make_file(tmp_path, "sole.mp4")
        id1 = _insert_recording(conn, camera_id=1, path=str(f1), days_ago=5)

        # Always below floor.
        def fake_usage(_path=None):
            return type("r", (), {"total": 1_000_000_000, "used": 960_000_000, "free": 40_000_000})()

        with patch("sentinel.services.storage.settings") as mock_settings:
            mock_settings.retention_free_space_floor_pct = 10
            mock_settings.retention_max_age_days = 30

            # Should not raise; should exit cleanly after finding nothing to delete.
            deleted = storage._prune_for_space(conn, _disk_usage_fn=fake_usage)

        assert deleted == 0
        assert f1.exists()
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (id1,)).fetchone() is not None


# ---------------------------------------------------------------------------
# Tests: newest-segment safety guard
# ---------------------------------------------------------------------------

class TestNewestSegmentSafety:
    """
    The SAFETY_KEEP_NEWEST_PER_CAMERA most-recent segments per camera must
    never be deleted regardless of age or space pressure.
    """

    def test_newest_not_deleted_by_age(self, tmp_path):
        """Even if a camera has only one (ancient) segment, it is kept."""
        storage = _storage_module()
        conn = _make_db()

        # Only one recording — very old.
        f = _make_file(tmp_path, "sole.mp4")
        rid = _insert_recording(conn, camera_id=1, path=str(f), days_ago=365)

        with patch("sentinel.services.storage.get_db", return_value=conn), \
             patch("sentinel.services.storage.settings") as mock_settings:
            mock_settings.retention_max_age_days = 30
            mock_settings.retention_free_space_floor_pct = 10

            deleted = storage._prune_by_age(conn)

        # The sole segment must survive (it's the newest — and only — one).
        assert deleted == 0
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (rid,)).fetchone() is not None
        assert f.exists()

    def test_newest_kept_per_camera(self, tmp_path):
        """
        With SAFETY_KEEP_NEWEST_PER_CAMERA=1, the 1 newest segment per
        camera is never deleted even under age pressure.
        All older segments beyond the safety window are pruned if past cutoff.
        """
        storage = _storage_module()
        conn = _make_db()

        # 4 recordings; oldest 3 are past cutoff (60, 50, 40 days), newest 1 kept.
        files = []
        ids = []
        for i, days in enumerate([60, 50, 40, 1]):
            f = _make_file(tmp_path, f"seg_{i}.mp4")
            rid = _insert_recording(conn, camera_id=1, path=str(f), days_ago=days)
            files.append(f)
            ids.append(rid)

        with patch("sentinel.services.storage.get_db", return_value=conn), \
             patch("sentinel.services.storage.settings") as mock_settings:
            mock_settings.retention_max_age_days = 30
            mock_settings.retention_free_space_floor_pct = 10

            deleted = storage._prune_by_age(conn)

        assert deleted == 3
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (ids[0],)).fetchone() is None
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (ids[1],)).fetchone() is None
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (ids[2],)).fetchone() is None
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (ids[3],)).fetchone() is not None

    def test_newest_not_deleted_by_space_prune(self, tmp_path):
        """The newest segment per camera is preserved during space-floor cleanup."""
        storage = _storage_module()
        conn = _make_db()

        # Only one recording per camera — both must be preserved.
        fa = _make_file(tmp_path, "cam_a.mp4", size=50_000_000)
        fb = _make_file(tmp_path, "cam_b.mp4", size=50_000_000)
        id_a = _insert_recording(conn, camera_id=1, path=str(fa), days_ago=10, size_bytes=50_000_000)
        id_b = _insert_recording(conn, camera_id=2, path=str(fb), days_ago=15, size_bytes=50_000_000)

        # Always below floor.
        def fake_usage(_path=None):
            return type("r", (), {"total": 1_000_000_000, "used": 960_000_000, "free": 40_000_000})()

        with patch("sentinel.services.storage.settings") as mock_settings:
            mock_settings.retention_free_space_floor_pct = 10
            mock_settings.retention_max_age_days = 30

            deleted = storage._prune_for_space(conn, _disk_usage_fn=fake_usage)

        assert deleted == 0
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (id_a,)).fetchone() is not None
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (id_b,)).fetchone() is not None

    def test_safe_ids_per_camera_independent(self, tmp_path):
        """
        Safety window is per-camera: cam A's newest does not protect cam B's
        oldest from being deleted.
        """
        storage = _storage_module()
        conn = _make_db()

        # cam 1: one old, one new
        fa_old = _make_file(tmp_path, "ca_old.mp4")
        fa_new = _make_file(tmp_path, "ca_new.mp4")
        _insert_recording(conn, camera_id=1, path=str(fa_old), days_ago=50)
        _insert_recording(conn, camera_id=1, path=str(fa_new), days_ago=2)

        # cam 2: one old, one new
        fb_old = _make_file(tmp_path, "cb_old.mp4")
        fb_new = _make_file(tmp_path, "cb_new.mp4")
        _insert_recording(conn, camera_id=2, path=str(fb_old), days_ago=45)
        _insert_recording(conn, camera_id=2, path=str(fb_new), days_ago=3)

        with patch("sentinel.services.storage.get_db", return_value=conn), \
             patch("sentinel.services.storage.settings") as mock_settings:
            mock_settings.retention_max_age_days = 30
            mock_settings.retention_free_space_floor_pct = 10

            deleted = storage._prune_by_age(conn)

        # Both old ones pruned; both new ones kept.
        assert deleted == 2
        assert not fa_old.exists()
        assert not fb_old.exists()
        assert fa_new.exists()
        assert fb_new.exists()


# ---------------------------------------------------------------------------
# Tests: tiny/orphan file cleanup
# ---------------------------------------------------------------------------

class TestTinyOrphanCleanup:
    def test_tiny_file_deleted(self, tmp_path):
        """A stub file (<= TINY_FILE_MAX_BYTES) that is not the newest is removed."""
        storage = _storage_module()
        conn = _make_db()

        # Tiny stub — 48 bytes, like FFmpeg leaves at shutdown.
        tiny = _make_file(tmp_path, "stub.mp4", size=48)
        tiny_id = _insert_recording(conn, camera_id=1, path=str(tiny),
                                    days_ago=1, size_bytes=48)

        # A "real" newer segment so tiny is not the newest.
        real = _make_file(tmp_path, "real.mp4", size=500_000)
        _insert_recording(conn, camera_id=1, path=str(real),
                          days_ago=0.01, size_bytes=500_000)

        with patch("sentinel.services.storage.get_db", return_value=conn), \
             patch("sentinel.services.storage.settings") as mock_settings:
            mock_settings.retention_max_age_days = 30
            mock_settings.retention_free_space_floor_pct = 10

            deleted = storage._cleanup_tiny_orphans(conn)

        assert deleted == 1
        assert not tiny.exists()
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (tiny_id,)).fetchone() is None

    def test_tiny_file_that_is_newest_kept(self, tmp_path):
        """
        If the tiny file is the newest segment for its camera (e.g. just opened),
        it must NOT be deleted.
        """
        storage = _storage_module()
        conn = _make_db()

        tiny = _make_file(tmp_path, "just_opened.mp4", size=48)
        tiny_id = _insert_recording(conn, camera_id=1, path=str(tiny),
                                    days_ago=0.001, size_bytes=48)

        with patch("sentinel.services.storage.get_db", return_value=conn), \
             patch("sentinel.services.storage.settings") as mock_settings:
            mock_settings.retention_max_age_days = 30
            mock_settings.retention_free_space_floor_pct = 10

            deleted = storage._cleanup_tiny_orphans(conn)

        assert deleted == 0
        assert tiny.exists()
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (tiny_id,)).fetchone() is not None

    def test_normal_sized_file_not_cleaned(self, tmp_path):
        """A properly-sized segment is never treated as a tiny orphan."""
        storage = _storage_module()
        conn = _make_db()

        normal = _make_file(tmp_path, "normal.mp4", size=1_000_000)
        nid = _insert_recording(conn, camera_id=1, path=str(normal),
                                days_ago=5, size_bytes=1_000_000)
        # Add a newer one so 'normal' is not the newest.
        newer = _make_file(tmp_path, "newer.mp4", size=1_000_000)
        _insert_recording(conn, camera_id=1, path=str(newer),
                          days_ago=1, size_bytes=1_000_000)

        with patch("sentinel.services.storage.get_db", return_value=conn), \
             patch("sentinel.services.storage.settings") as mock_settings:
            mock_settings.retention_max_age_days = 30
            mock_settings.retention_free_space_floor_pct = 10

            deleted = storage._cleanup_tiny_orphans(conn)

        assert deleted == 0
        assert normal.exists()

    def test_tiny_file_missing_from_disk_row_removed(self, tmp_path):
        """If the tiny file is already gone from disk, its DB row is removed."""
        storage = _storage_module()
        conn = _make_db()

        ghost_path = str(tmp_path / "ghost_stub.mp4")  # never created
        ghost_id = _insert_recording(conn, camera_id=1, path=ghost_path,
                                     days_ago=2, size_bytes=48)

        # Add a newer real segment so ghost is not the newest.
        real = _make_file(tmp_path, "real.mp4", size=500_000)
        _insert_recording(conn, camera_id=1, path=str(real),
                          days_ago=0.1, size_bytes=500_000)

        with patch("sentinel.services.storage.get_db", return_value=conn), \
             patch("sentinel.services.storage.settings") as mock_settings:
            mock_settings.retention_max_age_days = 30
            mock_settings.retention_free_space_floor_pct = 10

            deleted = storage._cleanup_tiny_orphans(conn)

        assert deleted == 1
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (ghost_id,)).fetchone() is None


# ---------------------------------------------------------------------------
# Tests: DB synchronisation
# ---------------------------------------------------------------------------

class TestDBSync:
    def test_no_rows_for_deleted_files(self, tmp_path):
        """After age pruning, no DB rows should point to deleted files."""
        storage = _storage_module()
        conn = _make_db()

        old_file = _make_file(tmp_path, "old.mp4")
        new_file = _make_file(tmp_path, "new.mp4")
        _insert_recording(conn, camera_id=1, path=str(old_file), days_ago=50)
        _insert_recording(conn, camera_id=1, path=str(new_file), days_ago=2)

        with patch("sentinel.services.storage.get_db", return_value=conn), \
             patch("sentinel.services.storage.settings") as mock_settings:
            mock_settings.retention_max_age_days = 30
            mock_settings.retention_free_space_floor_pct = 10
            storage._prune_by_age(conn)

        rows = conn.execute("SELECT path FROM recordings").fetchall()
        for r in rows:
            assert Path(r["path"]).exists(), f"DB row points to missing file: {r['path']}"

    def test_events_recording_id_set_null_on_delete(self, tmp_path):
        """
        When a recording is deleted, linked events.recording_id is SET NULL
        (FK ON DELETE SET NULL) and the event row is preserved.
        """
        storage = _storage_module()
        conn = _make_db()

        old_file = _make_file(tmp_path, "old_with_event.mp4")
        old_id = _insert_recording(conn, camera_id=1, path=str(old_file), days_ago=50)

        # Link an event to this recording.
        conn.execute(
            """
            INSERT INTO events(camera_id, type, started_at, ended_at, recording_id, meta_json)
            VALUES (1, 'motion', '2026-01-01T00:00:00Z', '2026-01-01T00:00:10Z', ?, '{}')
            """,
            (old_id,),
        )
        conn.commit()
        event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Add newest so old is not the sole segment.
        new_file = _make_file(tmp_path, "new.mp4")
        _insert_recording(conn, camera_id=1, path=str(new_file), days_ago=1)

        with patch("sentinel.services.storage.get_db", return_value=conn), \
             patch("sentinel.services.storage.settings") as mock_settings:
            mock_settings.retention_max_age_days = 30
            mock_settings.retention_free_space_floor_pct = 10
            storage._prune_by_age(conn)

        # Recording row deleted.
        assert conn.execute("SELECT id FROM recordings WHERE id=?", (old_id,)).fetchone() is None

        # Event row still present but recording_id is NULL.
        ev = conn.execute("SELECT recording_id FROM events WHERE id=?", (event_id,)).fetchone()
        assert ev is not None
        assert ev["recording_id"] is None

    def test_notifications_cascade_deleted_with_event(self, tmp_path):
        """
        When an event is deleted (via its cascade from recordings), its
        notifications rows cascade-delete too.

        Note: in the production schema events.recording_id is ON DELETE SET NULL
        (events survive recording deletion).  This test verifies notifications
        cascade if an event is explicitly deleted.
        """
        conn = _make_db()

        # Insert an event then a notification for it.
        conn.execute(
            """
            INSERT INTO events(camera_id, type, started_at, meta_json)
            VALUES (1, 'motion', '2026-01-01T00:00:00Z', '{}')
            """
        )
        conn.commit()
        ev_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        conn.execute(
            "INSERT INTO notifications(event_id, channel) VALUES (?, 'telegram')",
            (ev_id,),
        )
        conn.commit()

        # Verify notification exists.
        assert conn.execute(
            "SELECT id FROM notifications WHERE event_id=?", (ev_id,)
        ).fetchone() is not None

        # Delete the event — notification should cascade-delete.
        conn.execute("DELETE FROM events WHERE id=?", (ev_id,))
        conn.commit()

        assert conn.execute(
            "SELECT id FROM notifications WHERE event_id=?", (ev_id,)
        ).fetchone() is None


# ---------------------------------------------------------------------------
# Tests: get_storage_status
# ---------------------------------------------------------------------------

class TestStorageStatus:
    def test_returns_expected_keys(self, tmp_path):
        """get_storage_status returns all required dashboard keys."""
        storage = _storage_module()
        conn = _make_db()

        with patch("sentinel.services.storage.get_db", return_value=conn), \
             patch("sentinel.services.storage.settings") as mock_settings, \
             patch("sentinel.services.storage._disk_usage") as mock_usage:
            mock_settings.retention_max_age_days = 30
            mock_settings.retention_free_space_floor_pct = 10
            mock_usage.return_value = type(
                "r", (), {"total": 1_000_000_000, "used": 800_000_000, "free": 200_000_000}
            )()

            status = storage.get_storage_status()

        required_keys = {
            "disk_total_bytes", "disk_used_bytes", "disk_free_bytes", "free_pct",
            "recordings_total_bytes", "segment_count",
            "gb_per_day", "projected_retention_days",
            "retention_max_age_days", "free_space_floor_pct",
        }
        assert required_keys.issubset(set(status.keys()))

    def test_disk_values_sane(self, tmp_path):
        """Disk byte values match the injected usage."""
        storage = _storage_module()
        conn = _make_db()

        with patch("sentinel.services.storage.get_db", return_value=conn), \
             patch("sentinel.services.storage.settings") as mock_settings, \
             patch("sentinel.services.storage._disk_usage") as mock_usage:
            mock_settings.retention_max_age_days = 30
            mock_settings.retention_free_space_floor_pct = 10
            mock_usage.return_value = type(
                "r", (), {"total": 500_000_000, "used": 400_000_000, "free": 100_000_000}
            )()

            status = storage.get_storage_status()

        assert status["disk_total_bytes"] == 500_000_000
        assert status["disk_free_bytes"]  == 100_000_000
        assert status["free_pct"] == pytest.approx(20.0, rel=0.01)

    def test_empty_db_segment_count_zero(self):
        """With no recordings, segment_count is 0 and gb_per_day is 0."""
        storage = _storage_module()
        conn = _make_db()

        with patch("sentinel.services.storage.get_db", return_value=conn), \
             patch("sentinel.services.storage.settings") as mock_settings, \
             patch("sentinel.services.storage._disk_usage") as mock_usage:
            mock_settings.retention_max_age_days = 30
            mock_settings.retention_free_space_floor_pct = 10
            mock_usage.return_value = type(
                "r", (), {"total": 1_000_000_000, "used": 500_000_000, "free": 500_000_000}
            )()

            status = storage.get_storage_status()

        assert status["segment_count"] == 0
        assert status["recordings_total_bytes"] == 0
        assert status["gb_per_day"] == 0.0
        assert status["projected_retention_days"] is None  # inf → None

    def test_segment_count_and_total_size(self, tmp_path):
        """segment_count and recordings_total_bytes reflect inserted rows."""
        storage = _storage_module()
        conn = _make_db()

        for i in range(3):
            _insert_recording(conn, camera_id=1, path=f"/fake/p{i}.mp4",
                              days_ago=i, size_bytes=1_000_000)

        with patch("sentinel.services.storage.get_db", return_value=conn), \
             patch("sentinel.services.storage.settings") as mock_settings, \
             patch("sentinel.services.storage._disk_usage") as mock_usage:
            mock_settings.retention_max_age_days = 30
            mock_settings.retention_free_space_floor_pct = 10
            mock_usage.return_value = type(
                "r", (), {"total": 1_000_000_000, "used": 500_000_000, "free": 500_000_000}
            )()

            status = storage.get_storage_status()

        assert status["segment_count"] == 3
        assert status["recordings_total_bytes"] == 3_000_000


# ---------------------------------------------------------------------------
# Tests: run_maintenance integration
# ---------------------------------------------------------------------------

class TestRunMaintenance:
    def test_all_phases_called(self, tmp_path):
        """run_maintenance deletes old files, frees space, and cleans orphans."""
        storage = _storage_module()
        conn = _make_db()

        total = 1_000_000_000
        file_size = 50_000_000

        # Tiny orphan (not newest).
        tiny = _make_file(tmp_path, "tiny_stub.mp4", size=48)
        _insert_recording(conn, camera_id=1, path=str(tiny), days_ago=5, size_bytes=48)

        # Old segment (35 days ago) — age pruned.
        old_f = _make_file(tmp_path, "old_segment.mp4", size=file_size)
        _insert_recording(conn, camera_id=1, path=str(old_f), days_ago=35, size_bytes=file_size)

        # Newest segment per camera 1 — must survive everything.
        newest = _make_file(tmp_path, "newest.mp4", size=file_size)
        _insert_recording(conn, camera_id=1, path=str(newest), days_ago=0.5, size_bytes=file_size)

        # Space prune: start below floor; one extra old-ish for cam 2.
        space_target = _make_file(tmp_path, "cam2_old.mp4", size=file_size)
        space_id = _insert_recording(conn, camera_id=2, path=str(space_target),
                                     days_ago=10, size_bytes=file_size)
        # Newest for cam 2.
        cam2_newest = _make_file(tmp_path, "cam2_newest.mp4", size=file_size)
        _insert_recording(conn, camera_id=2, path=str(cam2_newest),
                          days_ago=0.1, size_bytes=file_size)

        call_count = [0]
        def fake_usage(_path=None):
            call_count[0] += 1
            # First call: below floor; thereafter above.
            free = 50_000_000 if call_count[0] == 1 else 200_000_000
            return type("r", (), {"total": total, "used": total - free, "free": free})()

        with patch("sentinel.services.storage.get_db", return_value=conn), \
             patch("sentinel.services.storage._disk_usage", side_effect=fake_usage), \
             patch("sentinel.services.storage.settings") as mock_settings:
            mock_settings.retention_max_age_days = 30
            mock_settings.retention_free_space_floor_pct = 10

            result = storage.run_maintenance(db=conn)

        assert result["total_deleted"] >= 2   # at minimum: tiny + old_f
        # Newest segments must still exist.
        assert newest.exists()
        assert cam2_newest.exists()
        # Tiny orphan deleted.
        assert not tiny.exists()
        # Old segment deleted.
        assert not old_f.exists()

    def test_returns_counts_dict(self, tmp_path):
        """run_maintenance returns a dict with expected count keys."""
        storage = _storage_module()
        conn = _make_db()

        with patch("sentinel.services.storage.get_db", return_value=conn), \
             patch("sentinel.services.storage._disk_usage") as mock_usage, \
             patch("sentinel.services.storage.settings") as mock_settings:
            mock_settings.retention_max_age_days = 30
            mock_settings.retention_free_space_floor_pct = 10
            mock_usage.return_value = type(
                "r", (), {"total": 1_000_000_000, "used": 500_000_000, "free": 500_000_000}
            )()

            result = storage.run_maintenance(db=conn)

        assert "tiny_orphans_deleted" in result
        assert "age_pruned" in result
        assert "space_pruned" in result
        assert "total_deleted" in result
        assert result["total_deleted"] == (
            result["tiny_orphans_deleted"] + result["age_pruned"] + result["space_pruned"]
        )
