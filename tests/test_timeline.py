"""
tests/test_timeline.py — Unit tests for Phase 5: timeline, range-request, export.

Tests:
  - Timeline assembly: segments + events within range, overlap, gap handling
  - Range-header parsing and 206 logic
  - Export offset math (wall-clock range → segment offsets)
  - Path-traversal rejection
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("SENTINEL_SECRET_KEY", "test-secret-key-for-unit-tests-only")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db() -> sqlite3.Connection:
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
        CREATE TABLE events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id    INTEGER NOT NULL,
            type         TEXT    NOT NULL DEFAULT 'motion',
            label        TEXT,
            score        REAL,
            started_at   TEXT    NOT NULL,
            ended_at     TEXT,
            thumb_path   TEXT,
            clip_path    TEXT,
            recording_id INTEGER,
            meta_json    TEXT    NOT NULL DEFAULT '{}'
        );
        INSERT INTO cameras(name) VALUES ('test_cam');
        """
    )
    conn.commit()
    return conn


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Range-header parsing
# ---------------------------------------------------------------------------

class TestParseRangeHeader:
    """Test _parse_range_header from sentinel.api.timeline."""

    def _parse(self, header, size):
        from sentinel.api.timeline import _parse_range_header
        return _parse_range_header(header, size)

    def test_simple_range(self):
        start, end = self._parse("bytes=0-99", 1000)
        assert start == 0
        assert end == 99

    def test_range_to_eof(self):
        start, end = self._parse("bytes=500-", 1000)
        assert start == 500
        assert end == 999

    def test_suffix_range(self):
        start, end = self._parse("bytes=-200", 1000)
        assert start == 800
        assert end == 999

    def test_full_file_range(self):
        start, end = self._parse("bytes=0-999", 1000)
        assert start == 0
        assert end == 999

    def test_clamp_end_to_filesize(self):
        # Request past end of file — should clamp.
        start, end = self._parse("bytes=0-9999", 1000)
        assert end == 999

    def test_single_byte(self):
        start, end = self._parse("bytes=42-42", 1000)
        assert start == 42
        assert end == 42

    def test_last_byte(self):
        start, end = self._parse("bytes=999-999", 1000)
        assert start == 999
        assert end == 999

    def test_invalid_format_raises_416(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            self._parse("bytes=abc", 1000)
        assert exc.value.status_code == 416

    def test_start_beyond_filesize_raises_416(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            self._parse("bytes=1000-1500", 1000)
        assert exc.value.status_code == 416

    def test_start_greater_than_end_raises_416(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            self._parse("bytes=500-100", 1000)
        assert exc.value.status_code == 416

    def test_empty_range_raises_416(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            self._parse("bytes=-", 1000)
        assert exc.value.status_code == 416


# ---------------------------------------------------------------------------
# Path-traversal rejection
# ---------------------------------------------------------------------------

class TestSafeRecordingPath:
    def test_valid_path_inside_root(self, tmp_path):
        """A path inside the recordings root is allowed."""
        rec_root = tmp_path / "recordings"
        rec_root.mkdir()
        seg = rec_root / "cam1" / "2026-05-24" / "120000.mp4"
        seg.parent.mkdir(parents=True)
        seg.write_bytes(b"\x00" * 100)

        from sentinel.api.timeline import _safe_recording_path
        with patch("sentinel.api.timeline._RECORDINGS_ROOT", rec_root):
            result = _safe_recording_path(str(seg))
        assert result == seg.resolve()

    def test_traversal_rejected(self, tmp_path):
        """A path escaping the recordings root raises 400."""
        from fastapi import HTTPException
        from sentinel.api.timeline import _safe_recording_path
        outside = tmp_path / "secret.txt"
        outside.write_text("secret")

        with patch("sentinel.api.timeline._RECORDINGS_ROOT", tmp_path / "recordings"):
            with pytest.raises(HTTPException) as exc:
                _safe_recording_path(str(outside))
        assert exc.value.status_code in (400, 404)

    def test_dotdot_traversal_rejected(self, tmp_path):
        """A path with .. that escapes the root raises 400."""
        from fastapi import HTTPException
        from sentinel.api.timeline import _safe_recording_path
        rec_root = tmp_path / "recordings"
        rec_root.mkdir()
        # Construct a path that looks inside recordings but actually escapes.
        evil = str(rec_root / ".." / "secret.txt")

        with patch("sentinel.api.timeline._RECORDINGS_ROOT", rec_root):
            with pytest.raises(HTTPException) as exc:
                _safe_recording_path(evil)
        assert exc.value.status_code in (400, 404)

    def test_missing_file_raises_404(self, tmp_path):
        """A path inside the root that doesn't exist raises 404."""
        from fastapi import HTTPException
        from sentinel.api.timeline import _safe_recording_path
        rec_root = tmp_path / "recordings"
        rec_root.mkdir()
        ghost = rec_root / "does_not_exist.mp4"

        with patch("sentinel.api.timeline._RECORDINGS_ROOT", rec_root):
            with pytest.raises(HTTPException) as exc:
                _safe_recording_path(str(ghost))
        assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Timeline assembly
# ---------------------------------------------------------------------------

class TestTimelineAssembly:
    """Exercise timeline query logic via the DB layer directly."""

    def _query_timeline(self, db, camera_id, start_str, end_str):
        """Mirror the SQL from the timeline endpoint."""
        seg_rows = db.execute(
            """
            SELECT id, path, started_at, ended_at, duration_s, has_gap
            FROM   recordings
            WHERE  camera_id = ?
              AND  started_at < ?
              AND  (ended_at IS NULL OR ended_at > ?)
            ORDER  BY started_at
            """,
            (camera_id, end_str, start_str),
        ).fetchall()

        evt_rows = db.execute(
            """
            SELECT id, type, label, score, started_at, ended_at, thumb_path
            FROM   events
            WHERE  camera_id = ?
              AND  started_at < ?
              AND  (ended_at IS NULL OR ended_at > ?)
            ORDER  BY started_at
            """,
            (camera_id, end_str, start_str),
        ).fetchall()

        return seg_rows, evt_rows

    def test_segment_within_range(self):
        db = _make_db()
        base = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
        db.execute(
            "INSERT INTO recordings(camera_id, path, started_at, ended_at, duration_s, size_bytes)"
            " VALUES(1, '/r/s.mp4', ?, ?, 120.0, 1000)",
            (_ts(base), _ts(base + timedelta(minutes=2))),
        )
        db.commit()

        segs, evts = self._query_timeline(
            db, 1,
            _ts(base - timedelta(hours=1)),
            _ts(base + timedelta(hours=1)),
        )
        assert len(segs) == 1
        assert evts == []

    def test_segment_before_range_excluded(self):
        db = _make_db()
        base = datetime(2026, 5, 24, 8, 0, 0, tzinfo=timezone.utc)
        db.execute(
            "INSERT INTO recordings(camera_id, path, started_at, ended_at, duration_s, size_bytes)"
            " VALUES(1, '/r/s.mp4', ?, ?, 120.0, 1000)",
            (_ts(base), _ts(base + timedelta(minutes=2))),
        )
        db.commit()

        # Query for 12:00–13:00; segment is at 08:00 → should be excluded
        segs, _ = self._query_timeline(
            db, 1,
            _ts(datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)),
            _ts(datetime(2026, 5, 24, 13, 0, 0, tzinfo=timezone.utc)),
        )
        assert len(segs) == 0

    def test_segment_spanning_range_start_included(self):
        """A segment that started before the query start but ends within it is included."""
        db = _make_db()
        base = datetime(2026, 5, 24, 11, 55, 0, tzinfo=timezone.utc)  # starts before 12:00
        db.execute(
            "INSERT INTO recordings(camera_id, path, started_at, ended_at, duration_s, size_bytes)"
            " VALUES(1, '/r/s.mp4', ?, ?, 600.0, 1000)",
            (_ts(base), _ts(base + timedelta(minutes=10))),  # ends at 12:05
        )
        db.commit()

        segs, _ = self._query_timeline(
            db, 1,
            _ts(datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)),
            _ts(datetime(2026, 5, 24, 13, 0, 0, tzinfo=timezone.utc)),
        )
        assert len(segs) == 1

    def test_gap_flag_preserved(self):
        db = _make_db()
        base = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
        db.execute(
            "INSERT INTO recordings(camera_id, path, started_at, ended_at, duration_s, size_bytes, has_gap)"
            " VALUES(1, '/r/s.mp4', ?, ?, 120.0, 1000, 1)",
            (_ts(base), _ts(base + timedelta(minutes=2))),
        )
        db.commit()

        segs, _ = self._query_timeline(
            db, 1,
            _ts(base - timedelta(hours=1)),
            _ts(base + timedelta(hours=1)),
        )
        assert len(segs) == 1
        assert segs[0]["has_gap"] == 1

    def test_multiple_segments_ordered(self):
        db = _make_db()
        base = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(3):
            t = base + timedelta(minutes=i * 5)
            db.execute(
                "INSERT INTO recordings(camera_id, path, started_at, ended_at, duration_s, size_bytes)"
                " VALUES(1, '/r/s{}.mp4', ?, ?, 120.0, 1000)".format(i),
                (_ts(t), _ts(t + timedelta(minutes=4))),
            )
        db.commit()

        segs, _ = self._query_timeline(
            db, 1,
            _ts(base - timedelta(hours=1)),
            _ts(base + timedelta(hours=1)),
        )
        assert len(segs) == 3
        # Ordered by started_at ascending
        for i in range(len(segs) - 1):
            assert segs[i]["started_at"] <= segs[i + 1]["started_at"]

    def test_event_within_range(self):
        db = _make_db()
        base = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
        db.execute(
            "INSERT INTO events(camera_id, type, started_at, ended_at)"
            " VALUES(1, 'motion', ?, ?)",
            (_ts(base), _ts(base + timedelta(seconds=30))),
        )
        db.commit()

        _, evts = self._query_timeline(
            db, 1,
            _ts(base - timedelta(hours=1)),
            _ts(base + timedelta(hours=1)),
        )
        assert len(evts) == 1

    def test_event_outside_range_excluded(self):
        db = _make_db()
        base = datetime(2026, 5, 24, 8, 0, 0, tzinfo=timezone.utc)
        db.execute(
            "INSERT INTO events(camera_id, type, started_at, ended_at)"
            " VALUES(1, 'motion', ?, ?)",
            (_ts(base), _ts(base + timedelta(seconds=30))),
        )
        db.commit()

        _, evts = self._query_timeline(
            db, 1,
            _ts(datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)),
            _ts(datetime(2026, 5, 24, 13, 0, 0, tzinfo=timezone.utc)),
        )
        assert len(evts) == 0

    def test_no_cross_camera_leakage(self):
        """Segments/events from a different camera are not included."""
        db = _make_db()
        db.execute("INSERT INTO cameras(name) VALUES ('other_cam')")
        db.commit()
        base = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)

        # Insert for camera_id=2
        db.execute(
            "INSERT INTO recordings(camera_id, path, started_at, ended_at, duration_s)"
            " VALUES(2, '/r/s.mp4', ?, ?, 60.0)",
            (_ts(base), _ts(base + timedelta(minutes=1))),
        )
        db.commit()

        segs, _ = self._query_timeline(
            db, 1,
            _ts(base - timedelta(hours=1)),
            _ts(base + timedelta(hours=1)),
        )
        assert len(segs) == 0

    def test_open_ended_segment_included(self):
        """A segment with NULL ended_at (still being written) is included."""
        db = _make_db()
        base = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
        db.execute(
            "INSERT INTO recordings(camera_id, path, started_at, ended_at, duration_s)"
            " VALUES(1, '/r/s.mp4', ?, NULL, NULL)",
            (_ts(base),),
        )
        db.commit()

        segs, _ = self._query_timeline(
            db, 1,
            _ts(base - timedelta(minutes=5)),
            _ts(base + timedelta(hours=1)),
        )
        assert len(segs) == 1


# ---------------------------------------------------------------------------
# Export offset math
# ---------------------------------------------------------------------------

class TestExportOffsetMath:
    """Test the wall-clock → segment-offset calculation used in export_clip."""

    def _calc_offsets(self, req_start, req_end, seg_start, seg_end):
        """Mirror the arithmetic from export_clip."""
        clip_start_in_seg = max(0.0, (req_start - seg_start).total_seconds())
        clip_end_in_seg = min(
            (seg_end - seg_start).total_seconds(),
            (req_end - seg_start).total_seconds(),
        )
        clip_dur = clip_end_in_seg - clip_start_in_seg
        return clip_start_in_seg, clip_dur

    def test_exact_segment_match(self):
        """Request exactly matches the segment bounds."""
        seg_start = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
        seg_end = seg_start + timedelta(seconds=120)
        offset, dur = self._calc_offsets(seg_start, seg_end, seg_start, seg_end)
        assert offset == pytest.approx(0.0)
        assert dur == pytest.approx(120.0)

    def test_request_starts_mid_segment(self):
        """Request starts 30s into a 120s segment."""
        seg_start = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
        seg_end = seg_start + timedelta(seconds=120)
        req_start = seg_start + timedelta(seconds=30)
        req_end = seg_end
        offset, dur = self._calc_offsets(req_start, req_end, seg_start, seg_end)
        assert offset == pytest.approx(30.0)
        assert dur == pytest.approx(90.0)

    def test_request_ends_mid_segment(self):
        """Request ends 60s into a 120s segment."""
        seg_start = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
        seg_end = seg_start + timedelta(seconds=120)
        req_end = seg_start + timedelta(seconds=60)
        offset, dur = self._calc_offsets(seg_start, req_end, seg_start, seg_end)
        assert offset == pytest.approx(0.0)
        assert dur == pytest.approx(60.0)

    def test_request_inside_segment(self):
        """Request is a 20s window inside a 120s segment."""
        seg_start = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
        seg_end = seg_start + timedelta(seconds=120)
        req_start = seg_start + timedelta(seconds=10)
        req_end = seg_start + timedelta(seconds=30)
        offset, dur = self._calc_offsets(req_start, req_end, seg_start, seg_end)
        assert offset == pytest.approx(10.0)
        assert dur == pytest.approx(20.0)

    def test_request_starts_before_segment(self):
        """Request start is before segment start → offset = 0."""
        seg_start = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
        seg_end = seg_start + timedelta(seconds=120)
        req_start = seg_start - timedelta(seconds=30)
        offset, dur = self._calc_offsets(req_start, seg_end, seg_start, seg_end)
        assert offset == pytest.approx(0.0)
        assert dur == pytest.approx(120.0)

    def test_zero_or_negative_dur_means_no_coverage(self):
        """A segment entirely before the request window has dur <= 0."""
        seg_start = datetime(2026, 5, 24, 10, 0, 0, tzinfo=timezone.utc)
        seg_end = seg_start + timedelta(seconds=120)
        req_start = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
        req_end = req_start + timedelta(seconds=60)
        offset, dur = self._calc_offsets(req_start, req_end, seg_start, seg_end)
        # dur should be 0 or negative — caller skips these
        assert dur <= 0

    def test_multi_segment_offsets(self):
        """Two adjacent 120s segments, request spans both."""
        seg1_start = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
        seg1_end = seg1_start + timedelta(seconds=120)
        seg2_start = seg1_end
        seg2_end = seg2_start + timedelta(seconds=120)

        req_start = seg1_start + timedelta(seconds=60)  # 60s into seg1
        req_end = seg2_start + timedelta(seconds=60)     # 60s into seg2

        off1, dur1 = self._calc_offsets(req_start, req_end, seg1_start, seg1_end)
        off2, dur2 = self._calc_offsets(req_start, req_end, seg2_start, seg2_end)

        assert off1 == pytest.approx(60.0)
        assert dur1 == pytest.approx(60.0)
        assert off2 == pytest.approx(0.0)
        assert dur2 == pytest.approx(60.0)
