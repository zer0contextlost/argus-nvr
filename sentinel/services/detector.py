"""
sentinel/services/detector.py — Motion detection + CPU face detection.

Architecture
------------
One CameraDetector per enabled camera runs in its own daemon thread.
  - Polls the camera's substream JPEG via go2rtc's frame endpoint at DETECT_FPS.
  - Downscales + converts to grayscale (Pillow + numpy — no OpenCV).
  - Computes abs-diff vs previous frame; counts changed pixels inside
    configurable polygon zones (stored normalised in cameras.detect_config_json).
  - Applies a BLOB SIZE FILTER: the largest contiguous changed region must
    exceed min_blob_pct of the zone area.  This rejects insect/bug false
    positives (scattered tiny pixels) while passing people / vehicles (large
    connected blobs).
  - Applies debounce:
      * Sustained-motion requirement: motion must be present for MOTION_CONFIRM_FRAMES
        consecutive polling cycles before firing a motion-start event.
      * Cooldown / post-time: after the last motion frame the gate stays open for
        POST_MOTION_SECONDS before motion-end is declared and the gate is closed.
  - On motion confirmation, runs MediaPipe CPU face detection on the best frame.
      * If faces found  → event type='object', label='face', face-bbox thumbnail.
      * If no faces     → event type='motion', motion-diff bounding box thumbnail.
  - On motion START:
      * Creates an events row in the DB.
      * Saves a best-frame thumbnail with bounding box.
      * Calls CameraRecorder.open_gate() (drives motion-only recording).
      * Pushes an event payload over all connected WebSockets.
  - On motion END:
      * Sets ended_at on the event row.
      * Calls CameraRecorder.close_gate().

Zone format (detect_config_json)
---------------------------------
{
  "zones": [
    {
      "name": "zone1",
      "polygon": [[x1,y1], [x2,y2], ...],   // normalised 0..1 coords
      "enabled": true
    }
  ],
  "min_changed_pct": 0.5,       // % of zone pixels that must change — lower bound (default 0.5)
  "max_changed_pct": 25.0,      // % of zone pixels changed — upper bound; rejects bugs on lens (default 25)
  "min_blob_pct": 0.3,          // % of zone that must form one contiguous blob (default 0.3)
  "motion_confirm_frames": 2,   // consecutive frames before firing (default 2)
  "post_motion_seconds": 10,    // cooldown after last motion frame (default 10)
  "pixel_threshold": 25,        // per-pixel brightness diff to count as changed (default 25)
  "face_min_confidence": 0.75,  // MediaPipe min confidence for a face detection (default 0.75)
  "face_min_area_pct": 0.5,     // face must occupy >= this % of frame area (default 0.5)
  "arm_schedule": [               // time windows when detection is armed; empty = always
    {"days": [0,1,2,3,4], "start": "08:00", "end": "22:00"}
  ]
}

Face detection
--------------
Requires `mediapipe` installed in the venv.  If unavailable the detector falls
back to motion-only events.  Face detection runs only after motion is confirmed
(not every frame) so CPU impact is low.
"""

from __future__ import annotations

import concurrent.futures as _futures
import http.client
import io
import json
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw

from sentinel.config import settings
from sentinel.util.hikvision import build_go2rtc_stream_name
from sentinel.util.logging import get_logger

log = get_logger("sentinel.detector")

# Scipy is optional — imported once at module load, not per-frame.
try:
    from scipy.ndimage import label as _scipy_label
    _SCIPY_AVAILABLE = True
except ImportError:
    _scipy_label = None
    _SCIPY_AVAILABLE = False

# Thread pool for face detection — keeps detector thread unblocked.
_face_pool = _futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="face-det")

# ---------------------------------------------------------------------------
# Constants / tunables
# ---------------------------------------------------------------------------

DETECT_WIDTH = 320       # downscale width for diff
DETECT_HEIGHT = 240      # downscale height for diff
JPEG_TIMEOUT_S = 4.0    # HTTP timeout for each JPEG frame fetch (Wyze lazy-connect needs headroom)
MIN_CHANGED_PCT_DEFAULT = 0.5   # % of zone pixels that must change (lower bound)
MAX_CHANGED_PCT_DEFAULT = 25.0  # % of zone pixels changed — upper bound; rejects bugs/obstructions
MIN_BLOB_PCT_DEFAULT = 0.3      # % of zone that must form one contiguous blob
MOTION_CONFIRM_DEFAULT = 2      # consecutive frames before firing
POST_MOTION_DEFAULT = 10        # seconds gate stays open after last motion frame

# ---------------------------------------------------------------------------
# WebSocket event bus
# ---------------------------------------------------------------------------

_ws_subscribers: list = []
_ws_lock = threading.Lock()
# Event loop stored at subscribe time (runs on the loop thread); used by
# detector threads to schedule queue puts safely via call_soon_threadsafe.
_event_loop = None


def subscribe_events():
    """Return a new asyncio.Queue that will receive event dicts."""
    import asyncio
    global _event_loop
    _event_loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    with _ws_lock:
        _ws_subscribers.append(q)
    return q


def unsubscribe_events(q) -> None:
    with _ws_lock:
        try:
            _ws_subscribers.remove(q)
        except ValueError:
            pass


def _broadcast_event(event: dict) -> None:
    """Push an event dict to all WS subscribers from any thread."""
    loop = _event_loop
    with _ws_lock:
        dead = []
        for q in _ws_subscribers:
            try:
                if loop is not None and loop.is_running():
                    # Schedule the put on the event loop thread — asyncio.Queue
                    # is not safe to call from non-loop threads directly.
                    loop.call_soon_threadsafe(q.put_nowait, event)
                else:
                    q.put_nowait(event)
            except Exception:
                dead.append(q)
        for d in dead:
            try:
                _ws_subscribers.remove(d)
            except ValueError:
                pass


# ---------------------------------------------------------------------------
# Module-level registry
# ---------------------------------------------------------------------------

_detectors: dict[int, "CameraDetector"] = {}
_registry_lock = threading.Lock()


def get_recorder(camera_id: int):
    """Lazy import to avoid circular imports with recorder module."""
    from sentinel.services import recorder as recorder_svc
    with recorder_svc._registry_lock:
        return recorder_svc._recorders.get(camera_id)


# ---------------------------------------------------------------------------
# Zone masking helper
# ---------------------------------------------------------------------------

def _build_zone_mask(
    zones: list[dict],
    width: int,
    height: int,
) -> np.ndarray:
    """
    Build a boolean mask (H x W) that is True inside any enabled zone.
    Polygon coords are normalised [0..1]; we scale to pixel space.
    If no zones are given, the whole frame is the zone.
    """
    enabled = [z for z in zones if z.get("enabled", True)]
    if not enabled:
        return np.ones((height, width), dtype=bool)

    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    for z in enabled:
        poly = z.get("polygon", [])
        if len(poly) < 3:
            continue
        pixel_poly = [
            (int(x * width), int(y * height))
            for x, y in poly
        ]
        draw.polygon(pixel_poly, fill=255)

    arr = np.array(mask, dtype=bool)
    if not arr.any():
        return np.ones((height, width), dtype=bool)
    return arr


def _bbox_from_mask(changed_in_zone: np.ndarray) -> Optional[tuple[int, int, int, int]]:
    """Return (x1, y1, x2, y2) bounding box of the True region."""
    rows = np.any(changed_in_zone, axis=1)
    cols = np.any(changed_in_zone, axis=0)
    if not rows.any():
        return None
    y1, y2 = int(np.argmax(rows)), int(len(rows) - 1 - np.argmax(rows[::-1]))
    x1, x2 = int(np.argmax(cols)), int(len(cols) - 1 - np.argmax(cols[::-1]))
    return (x1, y1, x2, y2)


def _largest_blob_pct(mask: np.ndarray) -> float:
    """
    Return the size of the largest contiguous True region in mask as a
    percentage of mask.size.

    Uses scipy.ndimage.label if available.  Falls back to a 3x3
    neighbour-density filter (pixels need >= 4 of 8 neighbours also True)
    which approximates erosion and still rejects isolated insect pixels.
    """
    if not mask.any():
        return 0.0
    if _SCIPY_AVAILABLE:
        labeled, n = _scipy_label(mask)
        if n == 0:
            return 0.0
        largest = int(max(int(np.sum(labeled == i)) for i in range(1, n + 1)))
        return largest / mask.size * 100.0
    else:
        h, w = mask.shape
        m = mask.astype(np.uint8)
        p = np.pad(m, 1, mode='constant')
        density = (p[:-2, :-2] + p[:-2, 1:-1] + p[:-2, 2:] +
                   p[1:-1, :-2]                + p[1:-1, 2:] +
                   p[2:,  :-2] + p[2:,  1:-1] + p[2:,  2:])
        solid = (m == 1) & (density >= 4)
        return int(solid.sum()) / mask.size * 100.0


# ---------------------------------------------------------------------------
# Face detection (MediaPipe Tasks API, CPU-only)
# ---------------------------------------------------------------------------

# Path to the blaze_face_full_range TFLite model (downloaded to bin/ by install).
_FACE_MODEL_PATH = Path(__file__).resolve().parents[2] / "bin" / "blaze_face_full_range.tflite"


FACE_MIN_CONFIDENCE_DEFAULT = 0.75   # MediaPipe detection threshold (raise = fewer false positives)
FACE_MIN_AREA_PCT_DEFAULT = 0.5      # Minimum face area as % of frame; rejects tiny artifacts


def _create_face_detector(min_confidence: float = FACE_MIN_CONFIDENCE_DEFAULT):
    """
    Create a MediaPipe Tasks FaceDetector instance.
    Returns None (with a warning) if mediapipe is not installed or the model
    file is missing.  The full-range model detects faces up to ~5 m away —
    better for security cameras than the short-range variant.
    """
    try:
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision

        if not _FACE_MODEL_PATH.exists():
            log.warning(
                "[detector] Face model not found at %s — face detection disabled. "
                "Download blaze_face_full_range.tflite to bin/",
                _FACE_MODEL_PATH,
            )
            return None

        base_opts = mp_python.BaseOptions(model_asset_path=str(_FACE_MODEL_PATH))
        opts = mp_vision.FaceDetectorOptions(
            base_options=base_opts,
            min_detection_confidence=min_confidence,
        )
        det = mp_vision.FaceDetector.create_from_options(opts)
        log.info(
            "[detector] MediaPipe face detection loaded (model=%s, min_confidence=%.2f)",
            _FACE_MODEL_PATH.name, min_confidence,
        )
        return det
    except ImportError:
        log.warning(
            "[detector] mediapipe not installed — face detection disabled. "
            "Run: pip install mediapipe"
        )
        return None
    except Exception as exc:
        log.warning("[detector] MediaPipe init failed: %s", exc)
        return None


def _detect_faces(
    face_detector,
    frame_bytes: bytes,
) -> list[tuple[float, float, float, float, float]]:
    """
    Run face detection on a JPEG frame using the MediaPipe Tasks API.
    Returns [(x1_norm, y1_norm, x2_norm, y2_norm, confidence), ...].
    Empty list if no faces or detector unavailable.
    """
    if face_detector is None:
        return []
    try:
        import mediapipe as mp
        img = Image.open(io.BytesIO(frame_bytes)).convert("RGB")
        arr = np.array(img)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=arr)
        result = face_detector.detect(mp_image)
        faces = []
        if result.detections:
            for det in result.detections:
                bbox = det.bounding_box
                ih, iw = arr.shape[:2]
                x1 = max(0.0, bbox.origin_x / iw)
                y1 = max(0.0, bbox.origin_y / ih)
                x2 = min(1.0, (bbox.origin_x + bbox.width) / iw)
                y2 = min(1.0, (bbox.origin_y + bbox.height) / ih)
                score = float(det.categories[0].score) if det.categories else 0.0
                faces.append((x1, y1, x2, y2, score))
        return faces
    except Exception as exc:
        log.debug("[detector] face detection error: %s", exc)
        return []


# ---------------------------------------------------------------------------
# DB helpers (thread-safe via dedicated per-detector connection)
# ---------------------------------------------------------------------------

def _open_db() -> sqlite3.Connection:
    """Open a per-thread SQLite connection (WAL, write-capable)."""
    db_path = settings.sentinel_db_path
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _find_covering_recording(conn: sqlite3.Connection, camera_id: int, ts: str) -> Optional[int]:
    """Find the recording segment whose time range covers ts. Returns id or None."""
    row = conn.execute(
        """
        SELECT id FROM recordings
        WHERE camera_id=? AND started_at <= ? AND (ended_at IS NULL OR ended_at >= ?)
        ORDER BY started_at DESC LIMIT 1
        """,
        (camera_id, ts, ts),
    ).fetchone()
    return row["id"] if row else None


def _create_event_row(
    conn: sqlite3.Connection,
    camera_id: int,
    started_at: str,
    recording_id: Optional[int],
    thumb_path: Optional[str],
    event_type: str = "motion",
    event_label: Optional[str] = None,
    score: Optional[float] = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO events
            (camera_id, type, label, score, started_at, recording_id, thumb_path, meta_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, '{}')
        """,
        (camera_id, event_type, event_label, score, started_at, recording_id, thumb_path),
    )
    conn.commit()
    return cur.lastrowid


def _close_event_row(conn: sqlite3.Connection, event_id: int, ended_at: str) -> None:
    conn.execute(
        "UPDATE events SET ended_at=? WHERE id=?",
        (ended_at, event_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Thumbnail writer
# ---------------------------------------------------------------------------

def _save_thumbnail(
    frame_bytes: bytes,
    camera_slug: str,
    event_id: int,
    bbox_norm: Optional[tuple[float, float, float, float]],
    box_color: tuple[int, int, int] = (255, 80, 0),
) -> Optional[str]:
    """
    Decode the JPEG frame, optionally draw a bounding box, save to:
      F:\\sentinel\\events\\<slug>\\<event_id>\\thumb.jpg
    Returns the path as a string, or None on failure.
    box_color: RGB tuple — orange for motion, green for face.
    """
    try:
        img = Image.open(io.BytesIO(frame_bytes)).convert("RGB")
        w, h = img.size

        if bbox_norm:
            x1n, y1n, x2n, y2n = bbox_norm
            x1, y1 = int(x1n * w), int(y1n * h)
            x2, y2 = int(x2n * w), int(y2n * h)
            draw = ImageDraw.Draw(img)
            draw.rectangle([x1, y1, x2, y2], outline=box_color, width=3)

        out_dir = settings.sentinel_events_dir / camera_slug / str(event_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        thumb_path = out_dir / "thumb.jpg"
        img.save(str(thumb_path), "JPEG", quality=80)
        return str(thumb_path)
    except Exception as exc:
        log.warning("[detector] thumbnail save failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# JPEG frame fetcher
# ---------------------------------------------------------------------------

class _MJPEGStreamReader:
    """
    Maintain ONE persistent multipart-MJPEG connection to go2rtc per detector
    thread and expose the latest decoded JPEG frame.

    go2rtc 1.9.x creates and destroys an internal MJPEG consumer on every
    /api/frame.jpeg request; under DETECT_FPS polling across several cameras
    that teardown churn trips a nil-pointer race in its consumer write path and
    crashes the whole process, dropping every live MSE/WebRTC viewer. A single
    long-lived /api/stream.mjpeg connection creates exactly one consumer that is
    destroyed only when the connection closes, eliminating the churn entirely.
    A daemon background thread reads the multipart stream and publishes frames;
    the detector thread reads the most recent frame each tick.
    """

    _MAX_BUF = 2 * 1024 * 1024
    _RETRY_BASE_S = 3.0
    _RETRY_MAX_S = 60.0

    def __init__(self, url_path: str, host: str = "127.0.0.1", port: int = 1984) -> None:
        self._host = host
        self._port = port
        self._path = url_path
        self._frame: Optional[bytes] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def get_frame(self) -> Optional[bytes]:
        with self._lock:
            return self._frame

    def close(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        # Exponential backoff while the stream can't be opened (dead camera):
        # each attempt makes go2rtc spawn an ffmpeg exec, so don't retry at a
        # fixed few seconds forever. Reset on the first frame received.
        failures = 0
        while not self._stop.is_set():
            ok = False
            try:
                ok = self._stream()
            except Exception:
                pass
            if self._stop.is_set():
                break
            if ok:
                if failures:
                    log.info("[detector %s] stream recovered after %d failed attempts",
                             self._path, failures)
                failures = 0
                delay = 1.0  # was streaming; brief pause before reconnect (detection is blind meanwhile)
            else:
                failures += 1
                with self._lock:
                    self._frame = None  # don't serve a stale frame from a dead stream
                delay = min(self._RETRY_BASE_S * 2 ** (failures - 1), self._RETRY_MAX_S)
                if failures == 1:
                    log.warning("[detector %s] stream unavailable, retrying with backoff (max %ds)",
                                self._path, self._RETRY_MAX_S)
            self._stop.wait(delay)

    def _stream(self) -> bool:
        """Read one connection's worth of frames. True if at least one frame arrived."""
        got_frame = False
        conn = http.client.HTTPConnection(self._host, self._port, timeout=10)
        try:
            conn.request("GET", self._path)
            resp = conn.getresponse()
            if resp.status != 200:
                return False
            buf = b""
            while not self._stop.is_set():
                chunk = resp.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    start = buf.find(b"\xff\xd8")
                    end = buf.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                    if start >= 0 and end >= 0:
                        jpeg = buf[start:end + 2]
                        with self._lock:
                            self._frame = jpeg
                        got_frame = True
                        buf = buf[end + 2:]
                    else:
                        break
                if len(buf) > self._MAX_BUF:
                    buf = b""
            return got_frame
        finally:
            try:
                conn.close()
            except Exception:
                pass


def _fetch_jpeg(stream_name: str) -> Optional[bytes]:
    """One-shot JPEG fetch (no connection reuse). Retained for callers outside
    the per-camera detector loop."""
    url = (
        f"http://127.0.0.1:{settings.go2rtc_api_port}"
        f"/api/frame.jpeg?src={stream_name}"
    )
    try:
        with urllib.request.urlopen(url, timeout=JPEG_TIMEOUT_S) as resp:
            if resp.status == 200:
                data = resp.read()
                return data if data else None
    except Exception as exc:
        log.debug("[detector] frame fetch error for %s: %s", stream_name, exc)
    return None


# ---------------------------------------------------------------------------
# Per-camera detector
# ---------------------------------------------------------------------------

class CameraDetector:
    """
    Polls the substream JPEG endpoint, performs frame-diff motion detection
    with blob-size filtering, and (when MediaPipe is available) CPU face
    detection on confirmed motion frames.
    """

    def __init__(
        self,
        camera_id: int,
        camera_name: str,
        camera_slug: str,
        detect_config: dict,
        privacy_masks: list[dict] = [],
    ):
        self.camera_id = camera_id
        self.camera_name = camera_name
        self.camera_slug = camera_slug

        # Config knobs (from detect_config_json, all overridable per-camera)
        self._zones: list[dict] = detect_config.get("zones", [])
        self._min_changed_pct: float = float(detect_config.get("min_changed_pct", MIN_CHANGED_PCT_DEFAULT))
        self._max_changed_pct: float = float(detect_config.get("max_changed_pct", MAX_CHANGED_PCT_DEFAULT))
        self._min_blob_pct: float = float(detect_config.get("min_blob_pct", MIN_BLOB_PCT_DEFAULT))
        self._confirm_frames: int = int(detect_config.get("motion_confirm_frames", MOTION_CONFIRM_DEFAULT))
        self._post_motion_s: float = float(detect_config.get("post_motion_seconds", POST_MOTION_DEFAULT))
        self._pixel_threshold: int = int(detect_config.get("pixel_threshold", 25))
        self._arm_schedule: list[dict] = detect_config.get("arm_schedule", [])

        # Substream name in go2rtc (e.g. "cam1_sub")
        self._sub_stream = f"{camera_slug}_sub"
        self._mjpeg_stream = f"{camera_slug}_mjpeg"

        # Persistent MJPEG stream reader (one go2rtc consumer per camera,
        # avoids the per-request consumer churn that crashes go2rtc).
        self._frame_fetcher = _MJPEGStreamReader(
            f"/api/stream.mjpeg?src={self._mjpeg_stream}",
            host="127.0.0.1",
            port=settings.go2rtc_api_port,
        )

        # MediaPipe face detector (one instance per thread).
        # Respects the per-camera face_detection flag (also accepts legacy detect_faces key).
        _face_det_on = detect_config.get("face_detection", detect_config.get("detect_faces", True))
        _face_min_conf = float(detect_config.get("face_min_confidence", FACE_MIN_CONFIDENCE_DEFAULT))
        self._face_min_area_pct: float = float(detect_config.get("face_min_area_pct", FACE_MIN_AREA_PCT_DEFAULT))
        self._face_detector = _create_face_detector(_face_min_conf) if _face_det_on else None

        # Thread state
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Debounce state
        self._consecutive_motion = 0
        self._last_motion_time: float = 0.0
        self._in_motion = False
        self._current_event_id: Optional[int] = None
        self._best_frame: Optional[bytes] = None
        self._best_bbox_norm: Optional[tuple] = None

        self._privacy_masks: list[dict] = privacy_masks
        # Zone mask (built lazily on first frame)
        self._zone_mask: Optional[np.ndarray] = None
        self._privacy_allow_mask: Optional[np.ndarray] = None

        # Previous frame (greyscale numpy array)
        self._prev_gray: Optional[np.ndarray] = None

        # DB connection (dedicated to this thread)
        self._conn: Optional[sqlite3.Connection] = None

        # Status
        self._status = "stopped"
        self._error: Optional[str] = None
        self._consecutive_fetch_failures = 0

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"detector-cam{self.camera_id}",
            daemon=True,
        )
        self._thread.start()
        self._status = "running"
        log.info(
            "[detector cam=%s name=%r sub=%s] started at %s fps, face_detection=%s",
            self.camera_id, self.camera_name, self._sub_stream,
            settings.detect_fps,
            "yes" if self._face_detector else "no",
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10.0)
        self._status = "stopped"
        self._frame_fetcher.close()
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        if self._face_detector:
            try:
                self._face_detector.close()
            except Exception:
                pass
        log.info("[detector cam=%s] stopped", self.camera_id)

    def status(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "camera_name": self.camera_name,
            "status": self._status,
            "in_motion": self._in_motion,
            "sub_stream": self._sub_stream,
            "face_detection": self._face_detector is not None,
            "error": self._error,
        }

    # ------------------------------------------------------------------
    # Main poll loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        interval = 1.0 / max(1, settings.detect_fps)
        try:
            self._conn = _open_db()
        except Exception as exc:
            log.error("[detector cam=%s] DB open failed: %s", self.camera_id, exc)
            self._status = "error"
            self._error = str(exc)
            return

        while not self._stop_event.is_set():
            t0 = time.monotonic()
            try:
                self._tick()
            except Exception as exc:
                log.error("[detector cam=%s] tick error: %s", self.camera_id, exc)
                self._error = str(exc)
            elapsed = time.monotonic() - t0
            sleep_s = max(0.0, interval - elapsed)
            self._stop_event.wait(sleep_s)

        if self._in_motion and self._current_event_id is not None:
            self._end_motion()

    def _is_armed(self) -> bool:
        """True if detection should run now per arm_schedule. Empty = always armed."""
        if not self._arm_schedule:
            return True
        import datetime as _dt
        now_local = _dt.datetime.now()
        weekday = now_local.weekday()  # 0=Monday, 6=Sunday
        now_min = now_local.hour * 60 + now_local.minute
        for window in self._arm_schedule:
            days = window.get("days", list(range(7)))
            if weekday not in days:
                continue
            try:
                sh, sm = map(int, window.get("start", "00:00").split(":"))
                eh, em = map(int, window.get("end", "23:59").split(":"))
            except (ValueError, AttributeError):
                continue
            if sh * 60 + sm <= now_min <= eh * 60 + em:
                return True
        return False

    def _update_heatmap(self, motion_mask: np.ndarray) -> None:
        import json as _json
        heatmap_path = settings.sentinel_data_root / "heatmaps" / f"{self.camera_id}.json"
        heatmap_path.parent.mkdir(parents=True, exist_ok=True)
        acc = np.zeros((DETECT_HEIGHT, DETECT_WIDTH), dtype=np.float32)
        if heatmap_path.exists():
            try:
                data = _json.loads(heatmap_path.read_text())
                for cell in data.get("cells", []):
                    r, c, v = cell.get("r", 0), cell.get("c", 0), cell.get("v", 1.0)
                    if 0 <= r < DETECT_HEIGHT and 0 <= c < DETECT_WIDTH:
                        acc[r, c] = float(v)
            except Exception:
                pass
        acc[motion_mask] += 1.0
        ys, xs = np.where(acc > 0)
        cells = [{"r": int(r), "c": int(c), "v": float(acc[r, c])} for r, c in zip(ys, xs)]
        payload = _json.dumps({"width": DETECT_WIDTH, "height": DETECT_HEIGHT, "cells": cells})
        tmp = heatmap_path.with_suffix(".tmp")
        tmp.write_text(payload)
        tmp.replace(heatmap_path)

    def _tick(self) -> None:
        """One detection cycle: fetch frame, diff, blob-filter, update debounce."""
        frame_bytes = self._frame_fetcher.get_frame()
        if frame_bytes is None:
            self._consecutive_fetch_failures += 1
            self._prev_gray = None  # discard stale prev so next good frame doesn't false-trigger
            return
        self._consecutive_fetch_failures = 0

        if not self._is_armed():
            self._prev_gray = None
            self._consecutive_motion = 0
            return

        try:
            img = Image.open(io.BytesIO(frame_bytes))
            img_small = img.resize((DETECT_WIDTH, DETECT_HEIGHT), Image.BILINEAR)
            gray = np.array(img_small.convert("L"), dtype=np.uint8)
        except Exception as exc:
            log.debug("[detector cam=%s] frame decode error: %s", self.camera_id, exc)
            return

        if self._prev_gray is None:
            self._prev_gray = gray
            return

        if self._zone_mask is None:
            self._zone_mask = _build_zone_mask(self._zones, DETECT_WIDTH, DETECT_HEIGHT)

        if self._privacy_allow_mask is None and self._privacy_masks:
            pmask = np.ones((DETECT_HEIGHT, DETECT_WIDTH), dtype=bool)
            for rect in self._privacy_masks:
                x1 = max(0, int(rect.get('x', 0) * DETECT_WIDTH))
                y1 = max(0, int(rect.get('y', 0) * DETECT_HEIGHT))
                x2 = min(DETECT_WIDTH, int((rect.get('x', 0) + rect.get('w', 0)) * DETECT_WIDTH))
                y2 = min(DETECT_HEIGHT, int((rect.get('y', 0) + rect.get('h', 0)) * DETECT_HEIGHT))
                pmask[y1:y2, x1:x2] = False
            self._privacy_allow_mask = pmask

        diff = np.abs(gray.astype(np.int16) - self._prev_gray.astype(np.int16)).astype(np.uint8)
        self._prev_gray = gray

        changed = diff > self._pixel_threshold
        if self._privacy_allow_mask is not None:
            changed_in_zone = changed & self._zone_mask & self._privacy_allow_mask
        else:
            changed_in_zone = changed & self._zone_mask

        zone_pixel_count = int(self._zone_mask.sum())
        changed_count = int(changed_in_zone.sum())
        changed_pct = (changed_count / zone_pixel_count * 100.0) if zone_pixel_count > 0 else 0.0

        # Three-stage filter:
        # 1. Lower bound: enough zone pixels changed to be worth checking
        # 2. Upper bound: too many pixels changed = bug on lens or obstruction, not a person
        # 3. Largest contiguous blob: rejects scattered insect pixels
        has_motion = False
        if self._min_changed_pct <= changed_pct <= self._max_changed_pct:
            if self._min_blob_pct > 0:
                blob_pct = _largest_blob_pct(changed_in_zone)
                has_motion = blob_pct >= self._min_blob_pct
            else:
                has_motion = True

        now_mono = time.monotonic()

        if has_motion:
            self._consecutive_motion += 1
            self._last_motion_time = now_mono
            # Track best frame = frame with most changed pixels
            prev_count = (self._best_bbox_norm[4] if self._best_bbox_norm and len(self._best_bbox_norm) > 4 else 0)
            if self._best_frame is None or changed_count > prev_count:
                self._best_frame = frame_bytes
                bbox = _bbox_from_mask(changed_in_zone)
                if bbox:
                    x1, y1, x2, y2 = bbox
                    self._best_bbox_norm = (
                        x1 / DETECT_WIDTH, y1 / DETECT_HEIGHT,
                        x2 / DETECT_WIDTH, y2 / DETECT_HEIGHT,
                        changed_count,
                    )
                else:
                    self._best_bbox_norm = None
            try:
                self._update_heatmap(changed_in_zone)
            except Exception as _hm_exc:
                log.debug("[detector cam=%s] heatmap update error: %s", self.camera_id, _hm_exc)
        else:
            self._consecutive_motion = 0
            # If a pre-confirm motion burst dies out before it was ever confirmed,
            # drop its best-frame so a stale burst can't leak into the next event's
            # thumbnail. While _in_motion, keep the frame — it belongs to the live event.
            if not self._in_motion:
                self._best_frame = None
                self._best_bbox_norm = None

        if not self._in_motion:
            if self._consecutive_motion >= self._confirm_frames:
                self._start_motion(frame_bytes, changed_in_zone)
        else:
            time_since_motion = now_mono - self._last_motion_time
            if time_since_motion > self._post_motion_s:
                self._end_motion()

    # ------------------------------------------------------------------
    # Motion start / end
    # ------------------------------------------------------------------

    def _start_motion(self, frame_bytes: bytes, changed_in_zone: np.ndarray) -> None:
        """Confirm motion, run face detection, create event, save thumbnail, open gate."""
        now_utc = datetime.now(timezone.utc)
        started_at = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Use the frame with the most motion seen during the confirm period
        best = self._best_frame or frame_bytes

        # --- Face detection — offloaded to pool so detector thread stays live ---
        faces: list = []
        if self._face_detector:
            try:
                future = _face_pool.submit(_detect_faces, self._face_detector, best)
                raw_faces = future.result(timeout=2.0) or []
                # Drop faces smaller than face_min_area_pct of the frame (rejects artifacts).
                min_area = self._face_min_area_pct / 100.0
                faces = [f for f in raw_faces if (f[2] - f[0]) * (f[3] - f[1]) >= min_area]
                if len(raw_faces) != len(faces):
                    log.debug(
                        "[detector cam=%s] dropped %d small face(s) (min_area=%.2f%%)",
                        self.camera_id, len(raw_faces) - len(faces), self._face_min_area_pct,
                    )
            except Exception:
                faces = []

        event_score: Optional[float] = None
        face_embedding: Optional[bytes] = None
        person_id: Optional[int] = None
        person_name: Optional[str] = None

        if faces:
            # Use the highest-confidence face for the thumbnail bbox
            best_face = max(faces, key=lambda f: f[4])
            event_type = "object"
            event_label = "face"
            event_score = round(float(best_face[4]), 4)
            bbox4: Optional[tuple] = best_face[:4]
            box_color = (0, 220, 80)  # green for face
            log.info(
                "[detector cam=%s] FACE detected (conf=%.2f) at %s",
                self.camera_id, best_face[4], started_at,
            )

            # --- Face recognition: generate embedding + compare against known persons ---
            try:
                from sentinel.services.recognition import encode_face, recognize
                embedding = encode_face(best)
                if embedding is not None:
                    face_embedding = embedding.astype(np.float32).tobytes()
                    matched_id, sim = recognize(embedding)
                    if matched_id is not None:
                        person_id = matched_id
                        p_row = self._conn.execute(
                            "SELECT name FROM persons WHERE id=?", (matched_id,)
                        ).fetchone()
                        person_name = p_row["name"] if p_row else None
                        log.info(
                            "[detector cam=%s] RECOGNIZED %r (person_id=%d sim=%.2f)",
                            self.camera_id, person_name, matched_id, sim,
                        )
            except Exception as exc:
                log.debug("[detector cam=%s] recognition error: %s", self.camera_id, exc)
        else:
            event_type = "motion"
            event_label = None
            bbox4 = None
            if self._best_bbox_norm and len(self._best_bbox_norm) >= 4:
                bbox4 = self._best_bbox_norm[:4]
            box_color = (255, 80, 0)  # orange for motion
            log.info("[detector cam=%s] motion START at %s", self.camera_id, started_at)

        # Link to covering recording if available
        recording_id = None
        try:
            recording_id = _find_covering_recording(self._conn, self.camera_id, started_at)
        except Exception as exc:
            log.warning("[detector cam=%s] recording lookup failed: %s", self.camera_id, exc)

        # Create event row (thumb_path updated after save)
        try:
            event_id = _create_event_row(
                self._conn, self.camera_id, started_at, recording_id, None,
                event_type=event_type, event_label=event_label, score=event_score,
            )
        except Exception as exc:
            log.error("[detector cam=%s] event insert failed: %s", self.camera_id, exc)
            return

        self._current_event_id = event_id
        self._in_motion = True

        # Store face embedding + person_id on the event row
        if face_embedding is not None or person_id is not None:
            try:
                self._conn.execute(
                    "UPDATE events SET face_embedding=?, person_id=? WHERE id=?",
                    (face_embedding, person_id, event_id),
                )
                self._conn.commit()
            except Exception as exc:
                log.warning("[detector cam=%s] face_embedding update failed: %s", self.camera_id, exc)

        thumb_path = _save_thumbnail(best, self.camera_slug, event_id, bbox4, box_color)

        if thumb_path and self._conn:
            try:
                self._conn.execute(
                    "UPDATE events SET thumb_path=? WHERE id=?",
                    (thumb_path, event_id),
                )
                self._conn.commit()
            except Exception as exc:
                log.warning("[detector cam=%s] thumb_path update failed: %s", self.camera_id, exc)

        # Open recording gate (motion-only mode)
        try:
            rec = get_recorder(self.camera_id)
            if rec and rec.record_mode == "motion-only":
                rec.open_gate()
                log.debug("[detector cam=%s] gate opened", self.camera_id)
        except Exception as exc:
            log.warning("[detector cam=%s] gate open failed: %s", self.camera_id, exc)

        _broadcast_event({
            "type": "motion_start",
            "event_id": event_id,
            "event_type": event_type,
            "event_label": event_label,
            "person_id": person_id,
            "person_name": person_name,
            "camera_id": self.camera_id,
            "camera_name": self.camera_name,
            "started_at": started_at,
            "thumb_path": thumb_path,
            "recording_id": recording_id,
        })

        self._best_frame = None
        self._best_bbox_norm = None
        self._consecutive_motion = 0

    def _end_motion(self) -> None:
        """Set ended_at on the event row, close recording gate."""
        now_utc = datetime.now(timezone.utc)
        ended_at = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        log.info(
            "[detector cam=%s] motion END at %s (event=%s)",
            self.camera_id, ended_at, self._current_event_id,
        )

        if self._current_event_id and self._conn:
            try:
                _close_event_row(self._conn, self._current_event_id, ended_at)
            except Exception as exc:
                log.warning("[detector cam=%s] event close failed: %s", self.camera_id, exc)

        try:
            rec = get_recorder(self.camera_id)
            if rec and rec.record_mode == "motion-only":
                rec.close_gate()
                log.debug("[detector cam=%s] gate closed", self.camera_id)
        except Exception as exc:
            log.warning("[detector cam=%s] gate close failed: %s", self.camera_id, exc)

        _broadcast_event({
            "type": "motion_end",
            "event_id": self._current_event_id,
            "camera_id": self.camera_id,
            "camera_name": self.camera_name,
            "ended_at": ended_at,
        })

        self._in_motion = False
        self._current_event_id = None
        self._consecutive_motion = 0
        # Clear the best-frame tracker at event end. It is only reset on event
        # START otherwise, so without this the peak frame from this event survives
        # and becomes the NEXT event's thumbnail unless the next event happens to
        # have more changed pixels (see thumbnail-mismatch bug).
        self._best_frame = None
        self._best_bbox_norm = None


# ---------------------------------------------------------------------------
# Module-level lifecycle
# ---------------------------------------------------------------------------

def start_detectors() -> None:
    """Load all enabled cameras from the DB and start a CameraDetector for each."""
    global _detectors
    from sentinel.db import get_db
    db = get_db()
    cameras = db.execute(
        "SELECT id, name, detect_config_json, privacy_masks_json FROM cameras WHERE enabled=1"
    ).fetchall()

    with _registry_lock:
        for cam in cameras:
            cam_id = cam["id"]
            if cam_id in _detectors:
                continue
            try:
                detect_cfg = json.loads(cam["detect_config_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                detect_cfg = {}

            try:
                privacy_masks = json.loads(cam["privacy_masks_json"] or "[]")
                if not isinstance(privacy_masks, list):
                    privacy_masks = []
            except (json.JSONDecodeError, TypeError):
                privacy_masks = []

            slug = build_go2rtc_stream_name(cam["name"])
            det = CameraDetector(
                camera_id=cam_id,
                camera_name=cam["name"],
                camera_slug=slug,
                detect_config=detect_cfg,
                privacy_masks=privacy_masks,
            )
            det.start()
            _detectors[cam_id] = det

    log.info("[detector] started %d detector(s)", len(_detectors))


def stop_detectors() -> None:
    """Stop all detectors. Called from main.py lifespan shutdown."""
    global _detectors
    with _registry_lock:
        for cam_id, det in list(_detectors.items()):
            try:
                det.stop()
            except Exception as exc:
                log.error("[detector] error stopping cam=%s: %s", cam_id, exc)
        _detectors.clear()
    log.info("[detector] all detectors stopped")


def detector_statuses() -> list[dict]:
    """Return status dicts for all detectors (health endpoint)."""
    with _registry_lock:
        return [det.status() for det in _detectors.values()]


def add_detector(camera_id: int, camera_name: str, detect_config_json: str, privacy_masks_json: str = "[]") -> None:
    """Start a detector for a newly added camera (idempotent)."""
    with _registry_lock:
        if camera_id in _detectors:
            return
        try:
            detect_cfg = json.loads(detect_config_json or "{}")
        except (json.JSONDecodeError, TypeError):
            detect_cfg = {}
        try:
            privacy_masks = json.loads(privacy_masks_json or "[]")
            if not isinstance(privacy_masks, list):
                privacy_masks = []
        except (json.JSONDecodeError, TypeError):
            privacy_masks = []
        slug = build_go2rtc_stream_name(camera_name)
        det = CameraDetector(
            camera_id=camera_id,
            camera_name=camera_name,
            camera_slug=slug,
            detect_config=detect_cfg,
            privacy_masks=privacy_masks,
        )
        det.start()
        _detectors[camera_id] = det
    log.info("[detector] added detector for cam=%s", camera_id)


def remove_detector(camera_id: int) -> None:
    """Stop and remove the detector for a camera."""
    with _registry_lock:
        det = _detectors.pop(camera_id, None)
    if det:
        det.stop()
        log.info("[detector] removed detector for cam=%s", camera_id)
