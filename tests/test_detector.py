"""
tests/test_detector.py — Unit tests for Phase 4 motion detection.

Tests:
  - Frame-diff produces motion when pixels change in-zone; none when static
  - Zone masking excludes out-of-zone motion
  - Debounce: requires sustained motion to fire; applies post-motion cooldown
  - Event creation: DB row + thumbnail file produced; gate open/close invoked
"""

from __future__ import annotations

import io
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import numpy as np
import pytest
from PIL import Image

# Ensure test secret before any sentinel import
os.environ.setdefault("SENTINEL_SECRET_KEY", "test-secret-key-for-unit-tests-only")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_in_memory_db() -> sqlite3.Connection:
    """Minimal in-memory DB with cameras + events + recordings tables."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA foreign_keys=OFF;
        CREATE TABLE cameras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            detect_config_json TEXT NOT NULL DEFAULT '{}'
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


def _make_gray_frame(width: int, height: int, value: int) -> np.ndarray:
    """Create a uniform grayscale array of the given value (0-255)."""
    return np.full((height, width), value, dtype=np.uint8)


def _gray_to_jpeg(gray: np.ndarray) -> bytes:
    """Encode a grayscale numpy array as JPEG bytes."""
    img = Image.fromarray(gray, mode="L").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _make_rgb_jpeg(width: int, height: int, r: int, g: int, b: int) -> bytes:
    """Create a solid-colour RGB JPEG."""
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    arr[:, :, 0] = r
    arr[:, :, 1] = g
    arr[:, :, 2] = b
    img = Image.fromarray(arr, mode="RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Zone mask tests
# ---------------------------------------------------------------------------

class TestZoneMask:
    def test_no_zones_full_frame(self):
        from sentinel.services.detector import _build_zone_mask
        mask = _build_zone_mask([], 320, 240)
        assert mask.shape == (240, 320)
        assert mask.all()

    def test_full_frame_zone(self):
        from sentinel.services.detector import _build_zone_mask
        zones = [{"polygon": [[0,0],[1,0],[1,1],[0,1]], "enabled": True}]
        mask = _build_zone_mask(zones, 320, 240)
        assert mask.any()

    def test_disabled_zone_falls_back_to_full(self):
        from sentinel.services.detector import _build_zone_mask
        zones = [{"polygon": [[0,0],[1,0],[1,1],[0,1]], "enabled": False}]
        mask = _build_zone_mask(zones, 320, 240)
        assert mask.all()

    def test_right_half_zone_excludes_left(self):
        """A zone covering only the right half should have zeros on the left."""
        from sentinel.services.detector import _build_zone_mask
        zones = [{"polygon": [[0.5,0],[1,0],[1,1],[0.5,1]], "enabled": True}]
        mask = _build_zone_mask(zones, 100, 100)
        # Left half should be all False
        left_half = mask[:, :50]
        right_half = mask[:, 50:]
        assert not left_half.any(), "Left half should be outside the zone"
        assert right_half.any(), "Right half should be inside the zone"


# ---------------------------------------------------------------------------
# Frame-diff motion detection
# ---------------------------------------------------------------------------

class TestFrameDiff:
    """
    Test the core pixel-diff logic used in CameraDetector._tick().
    We test the primitives directly rather than the full async tick.
    """

    def test_static_scene_no_motion(self):
        """Identical frames → abs-diff = 0 → no changed pixels."""
        from sentinel.services.detector import DETECT_WIDTH, DETECT_HEIGHT
        gray = _make_gray_frame(DETECT_WIDTH, DETECT_HEIGHT, 128)
        diff = np.abs(gray.astype(np.int16) - gray.astype(np.int16)).astype(np.uint8)
        changed = diff > 25
        assert not changed.any()

    def test_changed_frame_produces_motion(self):
        """Frame that shifts 100 counts → all pixels change → motion detected."""
        from sentinel.services.detector import DETECT_WIDTH, DETECT_HEIGHT
        gray1 = _make_gray_frame(DETECT_WIDTH, DETECT_HEIGHT, 50)
        gray2 = _make_gray_frame(DETECT_WIDTH, DETECT_HEIGHT, 200)
        diff = np.abs(gray2.astype(np.int16) - gray1.astype(np.int16)).astype(np.uint8)
        changed = diff > 25
        assert changed.all()

    def test_zone_excludes_out_of_zone_motion(self):
        """Motion in the left half only — right-half zone should report no motion."""
        from sentinel.services.detector import DETECT_WIDTH, DETECT_HEIGHT, _build_zone_mask
        W, H = DETECT_WIDTH, DETECT_HEIGHT
        gray1 = _make_gray_frame(W, H, 50)
        gray2 = _make_gray_frame(W, H, 50)
        # Change only the left half in frame2
        gray2[:, : W // 2] = 200

        diff = np.abs(gray2.astype(np.int16) - gray1.astype(np.int16)).astype(np.uint8)
        changed = diff > 25

        # Zone = right half only
        right_half_zone = _build_zone_mask(
            [{"polygon": [[0.5, 0], [1, 0], [1, 1], [0.5, 1]], "enabled": True}],
            W, H,
        )
        changed_in_zone = changed & right_half_zone
        zone_pixel_count = int(right_half_zone.sum())
        changed_pct = changed_in_zone.sum() / zone_pixel_count * 100.0
        # No motion in the right-half zone
        assert changed_pct < 0.01

    def test_in_zone_motion_detected(self):
        """Motion in the right half — right-half zone should detect it."""
        from sentinel.services.detector import DETECT_WIDTH, DETECT_HEIGHT, _build_zone_mask
        W, H = DETECT_WIDTH, DETECT_HEIGHT
        gray1 = _make_gray_frame(W, H, 50)
        gray2 = _make_gray_frame(W, H, 50)
        # Change only the right half in frame2
        gray2[:, W // 2 :] = 200

        diff = np.abs(gray2.astype(np.int16) - gray1.astype(np.int16)).astype(np.uint8)
        changed = diff > 25

        right_half_zone = _build_zone_mask(
            [{"polygon": [[0.5, 0], [1, 0], [1, 1], [0.5, 1]], "enabled": True}],
            W, H,
        )
        changed_in_zone = changed & right_half_zone
        zone_pixel_count = int(right_half_zone.sum())
        changed_pct = changed_in_zone.sum() / zone_pixel_count * 100.0
        assert changed_pct > 90.0


# ---------------------------------------------------------------------------
# Debounce logic
# ---------------------------------------------------------------------------

class TestDebounce:
    """
    Test debounce by directly exercising CameraDetector state transitions
    with a patched _fetch_jpeg.
    """

    def _make_detector(self, tmp_path, detect_config=None):
        from sentinel.services.detector import CameraDetector
        cfg = detect_config or {
            "motion_confirm_frames": 2,
            "post_motion_seconds": 0.2,  # Short for tests
            "min_changed_pct": 0.5,
        }
        det = CameraDetector(
            camera_id=99,
            camera_name="test",
            camera_slug="test",
            detect_config=cfg,
        )
        return det

    def test_single_motion_frame_does_not_fire(self, tmp_path):
        """confirm_frames=2 means 1 frame is not enough to start an event."""
        from sentinel.services.detector import CameraDetector, DETECT_WIDTH, DETECT_HEIGHT

        det = self._make_detector(tmp_path)
        det._conn = _make_in_memory_db()

        # Feed two frames: first sets prev_gray; second gives diff
        gray1 = _gray_to_jpeg(_make_gray_frame(DETECT_WIDTH, DETECT_HEIGHT, 50))
        gray2 = _gray_to_jpeg(_make_gray_frame(DETECT_WIDTH, DETECT_HEIGHT, 200))

        started_calls = []
        with patch.object(det, "_start_motion", side_effect=lambda *a, **kw: started_calls.append(1)):
            with patch("sentinel.services.detector._fetch_jpeg", side_effect=[gray1, gray2]):
                det._tick()   # sets prev_gray
                det._tick()   # 1st motion frame (consecutive_motion=1 < confirm=2)

        assert not started_calls
        assert not det._in_motion

    def test_sustained_motion_fires_event(self, tmp_path):
        """confirm_frames=2 means 2+ consecutive motion frames fires start."""
        from sentinel.services.detector import CameraDetector, DETECT_WIDTH, DETECT_HEIGHT

        det = self._make_detector(tmp_path)
        det._conn = _make_in_memory_db()

        # Alternate dark/bright so every diff tick shows motion
        gray_dark   = _gray_to_jpeg(_make_gray_frame(DETECT_WIDTH, DETECT_HEIGHT, 50))
        gray_bright = _gray_to_jpeg(_make_gray_frame(DETECT_WIDTH, DETECT_HEIGHT, 200))
        gray_dark2  = _gray_to_jpeg(_make_gray_frame(DETECT_WIDTH, DETECT_HEIGHT, 52))  # different value so not same bytes

        started_calls = []
        with patch.object(det, "_start_motion", side_effect=lambda *a, **kw: started_calls.append(1)):
            with patch("sentinel.services.detector._fetch_jpeg", side_effect=[
                gray_dark,    # tick 1: establishes prev_gray
                gray_bright,  # tick 2: big diff → consecutive_motion = 1
                gray_dark2,   # tick 3: big diff → consecutive_motion = 2 >= confirm → fire
            ]):
                det._tick()  # prev_gray set
                det._tick()  # cm=1
                det._tick()  # cm=2, should fire

        assert len(started_calls) == 1

    def test_motion_end_fires_after_cooldown(self, tmp_path):
        """After motion ends, _end_motion is called after post_motion_seconds."""
        from sentinel.services.detector import CameraDetector, DETECT_WIDTH, DETECT_HEIGHT

        det = self._make_detector(tmp_path, detect_config={
            "motion_confirm_frames": 1,
            "post_motion_seconds": 0.1,
            "min_changed_pct": 0.5,
        })
        det._conn = _make_in_memory_db()

        # Put detector into in_motion state manually
        det._in_motion = True
        det._current_event_id = 1
        det._last_motion_time = time.monotonic() - 0.5  # 0.5s ago > 0.1s cooldown

        gray_static = _gray_to_jpeg(_make_gray_frame(DETECT_WIDTH, DETECT_HEIGHT, 100))

        end_calls = []
        with patch.object(det, "_end_motion", side_effect=lambda: end_calls.append(1)):
            with patch("sentinel.services.detector._fetch_jpeg", return_value=gray_static):
                # First tick: sets prev_gray
                det._tick()
                # Second tick: static frame (no motion), post_motion elapsed → end
                det._tick()

        assert len(end_calls) == 1

    def test_no_false_end_while_motion_continues(self, tmp_path):
        """While motion keeps arriving, _end_motion is NOT called."""
        from sentinel.services.detector import CameraDetector, DETECT_WIDTH, DETECT_HEIGHT

        det = self._make_detector(tmp_path, detect_config={
            "motion_confirm_frames": 1,
            "post_motion_seconds": 10.0,  # Long cooldown
            "min_changed_pct": 0.5,
        })
        det._conn = _make_in_memory_db()
        det._in_motion = True
        det._current_event_id = 1
        det._last_motion_time = time.monotonic()  # just now

        gray_dark = _gray_to_jpeg(_make_gray_frame(DETECT_WIDTH, DETECT_HEIGHT, 50))
        gray_bright = _gray_to_jpeg(_make_gray_frame(DETECT_WIDTH, DETECT_HEIGHT, 200))

        end_calls = []
        with patch.object(det, "_end_motion", side_effect=lambda: end_calls.append(1)):
            with patch("sentinel.services.detector._fetch_jpeg", side_effect=[gray_dark, gray_bright]):
                det._tick()  # prev_gray
                det._tick()  # motion → updates last_motion_time

        assert len(end_calls) == 0


# ---------------------------------------------------------------------------
# Event creation: DB row + thumbnail + gate
# ---------------------------------------------------------------------------

class TestEventCreation:
    def test_motion_start_creates_db_row_and_thumbnail(self, tmp_path):
        """
        When _start_motion is called, it should:
          1. Insert an events row into the DB
          2. Save thumb.jpg to the events directory
          3. Update thumb_path in the DB
        """
        from sentinel.services.detector import CameraDetector, DETECT_WIDTH, DETECT_HEIGHT

        conn = _make_in_memory_db()

        with patch("sentinel.services.detector.settings") as mock_settings:
            mock_settings.sentinel_events_dir = tmp_path / "events"
            mock_settings.sentinel_events_dir.mkdir(parents=True, exist_ok=True)
            mock_settings.go2rtc_api_port = 1984
            mock_settings.detect_fps = 5

            det = CameraDetector(
                camera_id=1,
                camera_name="test_cam",
                camera_slug="test_cam",
                detect_config={"motion_confirm_frames": 1, "post_motion_seconds": 10},
            )
            det._conn = conn

            # Provide a valid JPEG frame
            frame = _make_rgb_jpeg(320, 240, 100, 50, 30)

            with patch("sentinel.services.detector.get_recorder", return_value=None):
                with patch("sentinel.services.detector._broadcast_event"):
                    det._start_motion(frame, np.ones((240, 320), dtype=bool))

        # Check DB row
        row = conn.execute("SELECT * FROM events WHERE camera_id=1").fetchone()
        assert row is not None
        assert row["type"] == "motion"
        assert row["started_at"] is not None

        # Check thumbnail file
        thumb = row["thumb_path"]
        assert thumb is not None
        assert Path(thumb).exists()
        assert thumb.endswith("thumb.jpg")

    def test_motion_end_sets_ended_at(self, tmp_path):
        """_end_motion sets ended_at in the DB."""
        from sentinel.services.detector import CameraDetector

        conn = _make_in_memory_db()
        # Insert a live event row
        cur = conn.execute(
            "INSERT INTO events(camera_id, type, started_at, meta_json) VALUES(1,'motion','2026-05-24T10:00:00Z','{}')"
        )
        conn.commit()
        event_id = cur.lastrowid

        det = CameraDetector(
            camera_id=1,
            camera_name="test_cam",
            camera_slug="test_cam",
            detect_config={},
        )
        det._conn = conn
        det._in_motion = True
        det._current_event_id = event_id

        with patch("sentinel.services.detector.get_recorder", return_value=None):
            with patch("sentinel.services.detector._broadcast_event"):
                det._end_motion()

        row = conn.execute("SELECT ended_at FROM events WHERE id=?", (event_id,)).fetchone()
        assert row["ended_at"] is not None
        assert det._in_motion is False
        assert det._current_event_id is None

    def test_gate_opened_on_motion_start(self, tmp_path):
        """open_gate() is called on motion-only recorder."""
        from sentinel.services.detector import CameraDetector

        conn = _make_in_memory_db()
        mock_recorder = MagicMock()
        mock_recorder.record_mode = "motion-only"

        with patch("sentinel.services.detector.settings") as mock_settings:
            mock_settings.sentinel_events_dir = tmp_path / "events"
            mock_settings.sentinel_events_dir.mkdir(parents=True, exist_ok=True)
            mock_settings.go2rtc_api_port = 1984
            mock_settings.detect_fps = 5

            det = CameraDetector(
                camera_id=1,
                camera_name="test_cam",
                camera_slug="test_cam",
                detect_config={},
            )
            det._conn = conn
            frame = _make_rgb_jpeg(320, 240, 100, 50, 30)

            with patch("sentinel.services.detector.get_recorder", return_value=mock_recorder):
                with patch("sentinel.services.detector._broadcast_event"):
                    det._start_motion(frame, np.ones((240, 320), dtype=bool))

        mock_recorder.open_gate.assert_called_once()

    def test_gate_closed_on_motion_end(self, tmp_path):
        """close_gate() is called when motion ends on a motion-only recorder."""
        from sentinel.services.detector import CameraDetector

        conn = _make_in_memory_db()
        cur = conn.execute(
            "INSERT INTO events(camera_id, type, started_at, meta_json) VALUES(1,'motion','2026-05-24T10:00:00Z','{}')"
        )
        conn.commit()
        event_id = cur.lastrowid

        mock_recorder = MagicMock()
        mock_recorder.record_mode = "motion-only"

        det = CameraDetector(
            camera_id=1,
            camera_name="test_cam",
            camera_slug="test_cam",
            detect_config={},
        )
        det._conn = conn
        det._in_motion = True
        det._current_event_id = event_id

        with patch("sentinel.services.detector.get_recorder", return_value=mock_recorder):
            with patch("sentinel.services.detector._broadcast_event"):
                det._end_motion()

        mock_recorder.close_gate.assert_called_once()

    def test_gate_not_touched_for_continuous_mode(self, tmp_path):
        """Detector does not call gate methods for continuous-mode cameras."""
        from sentinel.services.detector import CameraDetector

        conn = _make_in_memory_db()
        mock_recorder = MagicMock()
        mock_recorder.record_mode = "continuous"

        with patch("sentinel.services.detector.settings") as mock_settings:
            mock_settings.sentinel_events_dir = tmp_path / "events"
            mock_settings.sentinel_events_dir.mkdir(parents=True, exist_ok=True)
            mock_settings.go2rtc_api_port = 1984
            mock_settings.detect_fps = 5

            det = CameraDetector(
                camera_id=1,
                camera_name="test_cam",
                camera_slug="test_cam",
                detect_config={},
            )
            det._conn = conn
            frame = _make_rgb_jpeg(320, 240, 100, 50, 30)

            with patch("sentinel.services.detector.get_recorder", return_value=mock_recorder):
                with patch("sentinel.services.detector._broadcast_event"):
                    det._start_motion(frame, np.ones((240, 320), dtype=bool))

        mock_recorder.open_gate.assert_not_called()

    def test_ws_broadcast_called_on_start(self, tmp_path):
        """_broadcast_event is called when motion starts."""
        from sentinel.services.detector import CameraDetector

        conn = _make_in_memory_db()
        broadcast_calls = []

        with patch("sentinel.services.detector.settings") as mock_settings:
            mock_settings.sentinel_events_dir = tmp_path / "events"
            mock_settings.sentinel_events_dir.mkdir(parents=True, exist_ok=True)
            mock_settings.go2rtc_api_port = 1984
            mock_settings.detect_fps = 5

            det = CameraDetector(
                camera_id=1,
                camera_name="test_cam",
                camera_slug="test_cam",
                detect_config={},
            )
            det._conn = conn
            frame = _make_rgb_jpeg(320, 240, 100, 50, 30)

            with patch("sentinel.services.detector.get_recorder", return_value=None):
                with patch(
                    "sentinel.services.detector._broadcast_event",
                    side_effect=lambda e: broadcast_calls.append(e),
                ):
                    det._start_motion(frame, np.ones((240, 320), dtype=bool))

        assert len(broadcast_calls) == 1
        assert broadcast_calls[0]["type"] == "motion_start"
        assert broadcast_calls[0]["camera_id"] == 1


# ---------------------------------------------------------------------------
# MotionGate Phase 4 behaviour
# ---------------------------------------------------------------------------

class TestMotionGatePhase4:
    def test_motion_only_starts_closed(self):
        from sentinel.services.recorder import MotionGate
        gate = MotionGate(record_mode="motion-only")
        assert gate.is_open is False

    def test_continuous_starts_open(self):
        from sentinel.services.recorder import MotionGate
        gate = MotionGate(record_mode="continuous")
        assert gate.is_open is True

    def test_continuous_plus_events_starts_open(self):
        from sentinel.services.recorder import MotionGate
        gate = MotionGate(record_mode="continuous+events")
        assert gate.is_open is True

    def test_motion_only_can_close(self):
        from sentinel.services.recorder import MotionGate
        gate = MotionGate(record_mode="motion-only")
        gate.open()
        assert gate.is_open is True
        gate.close()
        assert gate.is_open is False

    def test_continuous_close_is_noop(self):
        from sentinel.services.recorder import MotionGate
        gate = MotionGate(record_mode="continuous")
        gate.close()
        assert gate.is_open is True

    def test_continuous_plus_events_close_is_noop(self):
        from sentinel.services.recorder import MotionGate
        gate = MotionGate(record_mode="continuous+events")
        gate.close()
        assert gate.is_open is True


# ---------------------------------------------------------------------------
# bbox helper
# ---------------------------------------------------------------------------

class TestBboxHelper:
    def test_bbox_full_frame(self):
        from sentinel.services.detector import _bbox_from_mask
        mask = np.ones((100, 200), dtype=bool)
        bbox = _bbox_from_mask(mask)
        assert bbox is not None
        x1, y1, x2, y2 = bbox
        assert x1 == 0
        assert y1 == 0
        assert x2 == 199
        assert y2 == 99

    def test_bbox_empty_returns_none(self):
        from sentinel.services.detector import _bbox_from_mask
        mask = np.zeros((100, 200), dtype=bool)
        assert _bbox_from_mask(mask) is None

    def test_bbox_small_region(self):
        from sentinel.services.detector import _bbox_from_mask
        mask = np.zeros((100, 200), dtype=bool)
        mask[10:20, 50:80] = True
        bbox = _bbox_from_mask(mask)
        assert bbox is not None
        x1, y1, x2, y2 = bbox
        assert x1 == 50
        assert y1 == 10
        assert x2 == 79
        assert y2 == 19
