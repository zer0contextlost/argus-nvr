"""
tests/test_recorder.py — Unit tests for Phase 2 recording engine.

Tests:
  - Segment filename -> timestamp parsing
  - Gap detection logic
  - DB index-insert path (using an in-memory SQLite DB)
  - MotionGate behaviour
  - FFmpeg command builder structure
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Set a test secret before importing sentinel modules.
os.environ.setdefault("SENTINEL_SECRET_KEY", "test-secret-key-for-unit-tests-only")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_in_memory_db() -> sqlite3.Connection:
    """Create a minimal in-memory SQLite DB with the recordings table."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA foreign_keys=OFF;
        CREATE TABLE cameras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        );
        CREATE TABLE recordings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id   INTEGER NOT NULL,
            path        TEXT    NOT NULL,
            started_at  TEXT,
            ended_at    TEXT,
            duration_s  REAL,
            size_bytes  INTEGER,
            has_gap     INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_recordings_camera_started
            ON recordings(camera_id, started_at);
        INSERT INTO cameras(name) VALUES ('test_cam');
        """
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# parse_segment_filename
# ---------------------------------------------------------------------------

class TestParseSegmentFilename:
    def test_valid_filename(self, tmp_path):
        from sentinel.services.recorder import parse_segment_filename
        date_dir = tmp_path / "2026-05-24"
        date_dir.mkdir()
        seg = date_dir / "143052.mp4"
        seg.touch()
        dt = parse_segment_filename(seg)
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 5
        assert dt.day == 24
        assert dt.hour == 14
        assert dt.minute == 30
        assert dt.second == 52
        assert dt.tzinfo == timezone.utc

    def test_midnight_boundary(self, tmp_path):
        from sentinel.services.recorder import parse_segment_filename
        date_dir = tmp_path / "2026-05-24"
        date_dir.mkdir()
        seg = date_dir / "000000.mp4"
        seg.touch()
        dt = parse_segment_filename(seg)
        assert dt is not None
        assert dt.hour == 0
        assert dt.minute == 0
        assert dt.second == 0

    def test_invalid_date_dir(self, tmp_path):
        from sentinel.services.recorder import parse_segment_filename
        bad_dir = tmp_path / "not-a-date"
        bad_dir.mkdir()
        seg = bad_dir / "143052.mp4"
        seg.touch()
        dt = parse_segment_filename(seg)
        assert dt is None

    def test_invalid_time_stem(self, tmp_path):
        from sentinel.services.recorder import parse_segment_filename
        date_dir = tmp_path / "2026-05-24"
        date_dir.mkdir()
        seg = date_dir / "bad_name.mp4"
        seg.touch()
        dt = parse_segment_filename(seg)
        assert dt is None

    def test_returns_utc(self, tmp_path):
        from sentinel.services.recorder import parse_segment_filename
        date_dir = tmp_path / "2026-01-15"
        date_dir.mkdir()
        seg = date_dir / "120000.mp4"
        seg.touch()
        dt = parse_segment_filename(seg)
        assert dt.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# Gap detection logic
# ---------------------------------------------------------------------------

class TestGapDetection:
    """Test the _detect_gap logic by manipulating a test DB."""

    def _make_indexer(self, camera_id: int, camera_dir: Path, conn: sqlite3.Connection):
        """Create a SegmentIndexer with a patched get_db that returns our test conn."""
        from sentinel.services.recorder import SegmentIndexer
        indexer = SegmentIndexer.__new__(SegmentIndexer)
        indexer.camera_id = camera_id
        indexer.camera_dir = camera_dir
        indexer._stop_event = __import__("threading").Event()
        indexer._thread = None
        indexer._indexed = set()
        indexer._seen_order = []
        indexer._pending_gap = False
        indexer._conn = conn  # store for use in patched methods
        return indexer

    def test_no_prior_recordings_no_gap(self, tmp_path):
        conn = _make_in_memory_db()
        from sentinel.services.recorder import SegmentIndexer, GAP_THRESHOLD_S

        with patch("sentinel.services.recorder.get_db", return_value=conn):
            indexer = SegmentIndexer(camera_id=1, camera_dir=tmp_path)
            indexer._detect_gap()
        assert indexer._pending_gap is False

    def test_recent_recording_no_gap(self, tmp_path):
        conn = _make_in_memory_db()
        # Insert a recent recording ending 2 seconds ago.
        ended_at = (datetime.now(timezone.utc) - timedelta(seconds=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        conn.execute(
            "INSERT INTO recordings(camera_id, path, started_at, ended_at, duration_s, size_bytes, has_gap)"
            " VALUES(1, '/fake/path.mp4', ?, ?, 60.0, 1000, 0)",
            (ended_at, ended_at),
        )
        conn.commit()

        from sentinel.services.recorder import SegmentIndexer, GAP_THRESHOLD_S

        with patch("sentinel.services.recorder.get_db", return_value=conn):
            indexer = SegmentIndexer(camera_id=1, camera_dir=tmp_path)
            indexer._detect_gap()
        # Gap is only 2s, below threshold (5s)
        assert indexer._pending_gap is False

    def test_old_recording_gap_detected(self, tmp_path):
        conn = _make_in_memory_db()
        # Insert a recording ending 5 minutes ago.
        ended_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        conn.execute(
            "INSERT INTO recordings(camera_id, path, started_at, ended_at, duration_s, size_bytes, has_gap)"
            " VALUES(1, '/fake/path.mp4', ?, ?, 60.0, 1000, 0)",
            (ended_at, ended_at),
        )
        conn.commit()

        from sentinel.services.recorder import SegmentIndexer

        with patch("sentinel.services.recorder.get_db", return_value=conn):
            indexer = SegmentIndexer(camera_id=1, camera_dir=tmp_path)
            indexer._detect_gap()
        assert indexer._pending_gap is True

    def test_gap_flag_resets_after_insert(self, tmp_path):
        """After _pending_gap is consumed by an insert, it resets to False."""
        conn = _make_in_memory_db()

        from sentinel.services.recorder import SegmentIndexer

        with patch("sentinel.services.recorder.get_db", return_value=conn):
            indexer = SegmentIndexer(camera_id=1, camera_dir=tmp_path)
            indexer._pending_gap = True  # simulate gap detected

            # Create a real segment file to probe
            date_dir = tmp_path / "2026-05-24"
            date_dir.mkdir()
            seg = date_dir / "100000.mp4"
            seg.write_bytes(b"\x00" * 2048)  # non-empty placeholder

            # Patch ffprobe to return a fake result without actually running ffmpeg.
            with patch(
                "sentinel.services.recorder._probe_segment",
                return_value={"duration_s": 60.0},
            ):
                indexer._insert_segment(seg, force=True)

        # Gap flag should be consumed.
        assert indexer._pending_gap is False

        # DB row should have has_gap=1.
        row = conn.execute("SELECT has_gap FROM recordings WHERE camera_id=1").fetchone()
        assert row is not None
        assert row["has_gap"] == 1


# ---------------------------------------------------------------------------
# DB index-insert path
# ---------------------------------------------------------------------------

class TestIndexInsert:
    def test_insert_segment_row(self, tmp_path):
        """_insert_segment creates a row in the recordings table."""
        conn = _make_in_memory_db()

        from sentinel.services.recorder import SegmentIndexer

        date_dir = tmp_path / "2026-05-24"
        date_dir.mkdir()
        seg = date_dir / "120000.mp4"
        seg.write_bytes(b"\x00" * 4096)

        with patch("sentinel.services.recorder.get_db", return_value=conn):
            indexer = SegmentIndexer(camera_id=1, camera_dir=tmp_path)
            with patch(
                "sentinel.services.recorder._probe_segment",
                return_value={"duration_s": 120.0},
            ):
                indexer._insert_segment(seg, force=True)

        rows = conn.execute("SELECT * FROM recordings WHERE camera_id=1").fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["path"] == str(seg)
        assert row["started_at"] == "2026-05-24T12:00:00Z"
        assert row["ended_at"] == "2026-05-24T12:02:00Z"
        assert row["duration_s"] == pytest.approx(120.0)
        assert row["size_bytes"] == 4096
        assert row["has_gap"] == 0

    def test_no_duplicate_insert(self, tmp_path):
        """_insert_segment is idempotent — calling twice inserts only one row."""
        conn = _make_in_memory_db()

        from sentinel.services.recorder import SegmentIndexer

        date_dir = tmp_path / "2026-05-24"
        date_dir.mkdir()
        seg = date_dir / "130000.mp4"
        seg.write_bytes(b"\x00" * 4096)

        with patch("sentinel.services.recorder.get_db", return_value=conn):
            indexer = SegmentIndexer(camera_id=1, camera_dir=tmp_path)
            with patch(
                "sentinel.services.recorder._probe_segment",
                return_value={"duration_s": 60.0},
            ):
                indexer._insert_segment(seg, force=True)
                indexer._insert_segment(seg, force=True)  # second call

        rows = conn.execute("SELECT * FROM recordings WHERE camera_id=1").fetchall()
        assert len(rows) == 1

    def test_probe_failure_still_inserts(self, tmp_path):
        """If ffprobe fails, the row is still inserted (with NULL duration/times)."""
        conn = _make_in_memory_db()

        from sentinel.services.recorder import SegmentIndexer

        date_dir = tmp_path / "2026-05-24"
        date_dir.mkdir()
        seg = date_dir / "140000.mp4"
        seg.write_bytes(b"\x00" * 4096)

        with patch("sentinel.services.recorder.get_db", return_value=conn):
            indexer = SegmentIndexer(camera_id=1, camera_dir=tmp_path)
            with patch(
                "sentinel.services.recorder._probe_segment",
                return_value={},  # ffprobe returned nothing useful
            ):
                indexer._insert_segment(seg, force=True)

        rows = conn.execute("SELECT * FROM recordings WHERE camera_id=1").fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["duration_s"] is None

    def test_size_bytes_stored(self, tmp_path):
        """size_bytes from stat() is stored correctly."""
        conn = _make_in_memory_db()

        from sentinel.services.recorder import SegmentIndexer

        date_dir = tmp_path / "2026-05-24"
        date_dir.mkdir()
        seg = date_dir / "150000.mp4"
        content = b"\x00" * 8192
        seg.write_bytes(content)

        with patch("sentinel.services.recorder.get_db", return_value=conn):
            indexer = SegmentIndexer(camera_id=1, camera_dir=tmp_path)
            with patch(
                "sentinel.services.recorder._probe_segment",
                return_value={"duration_s": 30.0},
            ):
                indexer._insert_segment(seg, force=True)

        row = conn.execute("SELECT size_bytes FROM recordings WHERE camera_id=1").fetchone()
        assert row["size_bytes"] == 8192


# ---------------------------------------------------------------------------
# MotionGate
# ---------------------------------------------------------------------------

class TestMotionGate:
    def test_default_open(self):
        from sentinel.services.recorder import MotionGate
        gate = MotionGate()
        assert gate.is_open is True

    def test_open_idempotent(self):
        from sentinel.services.recorder import MotionGate
        gate = MotionGate()
        gate.open()
        assert gate.is_open is True

    def test_close_is_noop_in_phase2(self):
        """In Phase 2 the gate always remains open."""
        from sentinel.services.recorder import MotionGate
        gate = MotionGate()
        gate.close()
        # Phase 2: close is a no-op; gate stays open.
        assert gate.is_open is True


# ---------------------------------------------------------------------------
# FFmpeg command builder
# ---------------------------------------------------------------------------

class TestFFmpegCommandBuilder:
    def test_cmd_contains_stream_copy(self, tmp_path):
        from sentinel.services.recorder import CameraRecorder
        with patch("sentinel.services.recorder.settings") as mock_settings:
            mock_settings.go2rtc_rtsp_port = 8554
            mock_settings.sentinel_recordings_dir = tmp_path
            mock_settings.sentinel_log_dir = tmp_path
            mock_settings.segment_seconds = 120
            mock_settings.ffmpeg_path = Path("ffmpeg")
            mock_settings.ffprobe_path = Path("ffprobe")

            rec = CameraRecorder.__new__(CameraRecorder)
            rec.camera_id = 1
            rec.camera_name = "test_cam"
            rec.record_mode = "continuous"
            rec._stream_name = "test_cam"
            rec._rtsp_url = "rtsp://127.0.0.1:8554/test_cam"
            rec._cam_dir = tmp_path / "test_cam"
            rec._cam_dir.mkdir(exist_ok=True)

            cmd = rec._build_ffmpeg_cmd()

        # Video stream is copied; audio is transcoded to AAC (G.711 not supported in MP4)
        assert "-c:v" in cmd
        assert cmd[cmd.index("-c:v") + 1] == "copy"
        assert "-c:a" in cmd
        assert cmd[cmd.index("-c:a") + 1] == "aac"
        assert "-segment_format_options" in cmd

    def test_cmd_uses_segment_format(self, tmp_path):
        from sentinel.services.recorder import CameraRecorder
        with patch("sentinel.services.recorder.settings") as mock_settings:
            mock_settings.go2rtc_rtsp_port = 8554
            mock_settings.sentinel_recordings_dir = tmp_path
            mock_settings.sentinel_log_dir = tmp_path
            mock_settings.segment_seconds = 60
            mock_settings.ffmpeg_path = Path("ffmpeg")
            mock_settings.ffprobe_path = Path("ffprobe")

            rec = CameraRecorder.__new__(CameraRecorder)
            rec.camera_id = 1
            rec.camera_name = "test_cam"
            rec.record_mode = "continuous"
            rec._stream_name = "test_cam"
            rec._rtsp_url = "rtsp://127.0.0.1:8554/test_cam"
            rec._cam_dir = tmp_path / "test_cam"
            rec._cam_dir.mkdir(exist_ok=True)

            cmd = rec._build_ffmpeg_cmd()

        assert "-f" in cmd
        fmt_idx = cmd.index("-f")
        assert cmd[fmt_idx + 1] == "segment"

    def test_cmd_includes_rtsp_url(self, tmp_path):
        from sentinel.services.recorder import CameraRecorder
        with patch("sentinel.services.recorder.settings") as mock_settings:
            mock_settings.go2rtc_rtsp_port = 8554
            mock_settings.sentinel_recordings_dir = tmp_path
            mock_settings.sentinel_log_dir = tmp_path
            mock_settings.segment_seconds = 120
            mock_settings.ffmpeg_path = Path("ffmpeg")
            mock_settings.ffprobe_path = Path("ffprobe")

            rec = CameraRecorder.__new__(CameraRecorder)
            rec.camera_id = 1
            rec.camera_name = "test_cam"
            rec.record_mode = "continuous"
            rec._stream_name = "test_cam"
            rec._rtsp_url = "rtsp://127.0.0.1:8554/test_cam"
            rec._cam_dir = tmp_path / "test_cam"
            rec._cam_dir.mkdir(exist_ok=True)

            cmd = rec._build_ffmpeg_cmd()

        assert "rtsp://127.0.0.1:8554/test_cam" in cmd

    def test_cmd_strftime_pattern(self, tmp_path):
        from sentinel.services.recorder import CameraRecorder
        with patch("sentinel.services.recorder.settings") as mock_settings:
            mock_settings.go2rtc_rtsp_port = 8554
            mock_settings.sentinel_recordings_dir = tmp_path
            mock_settings.sentinel_log_dir = tmp_path
            mock_settings.segment_seconds = 120
            mock_settings.ffmpeg_path = Path("ffmpeg")
            mock_settings.ffprobe_path = Path("ffprobe")

            rec = CameraRecorder.__new__(CameraRecorder)
            rec.camera_id = 1
            rec.camera_name = "test_cam"
            rec.record_mode = "continuous"
            rec._stream_name = "test_cam"
            rec._rtsp_url = "rtsp://127.0.0.1:8554/test_cam"
            rec._cam_dir = tmp_path / "test_cam"
            rec._cam_dir.mkdir(exist_ok=True)

            cmd = rec._build_ffmpeg_cmd()

        # The output path should end with the strftime pattern.
        output = cmd[-1]
        assert "%H%M%S.mp4" in output
        assert "%Y-%m-%d" in output
