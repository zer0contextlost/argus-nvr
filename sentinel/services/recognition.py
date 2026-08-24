"""
sentinel/services/recognition.py — Named-face recognition using InsightFace.

Architecture
------------
- One InsightFace FaceAnalysis instance (buffalo_sc model) shared across all
  detector threads via a module-level lock.
- In-memory cache of known face encodings + person IDs, rebuilt from DB on
  startup and updated live as users enroll new faces.
- encode_face(): run InsightFace on a frame, return 512-d L2-normalised embedding.
- recognize(): cosine-similarity match against the in-memory cache.
- enroll(): store a new encoding for a person, refresh cache.

The buffalo_sc model (14 MB) downloads automatically on first use to
~/.insightface/models/buffalo_sc/ and is cached there permanently.
"""

from __future__ import annotations

import threading
from typing import Optional

import numpy as np

from sentinel.util.logging import get_logger

log = get_logger("sentinel.recognition")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COSINE_THRESHOLD = 0.35   # dot product of L2-normalised vectors >= this = match
                           # InsightFace MBF typical range: 0.2–0.5

# ---------------------------------------------------------------------------
# InsightFace singleton
# ---------------------------------------------------------------------------

_app_lock = threading.Lock()
_app = None
_app_failed = False       # if loading failed once, skip future attempts


def _get_app():
    """Lazy-load InsightFace FaceAnalysis (downloads buffalo_sc on first call)."""
    global _app, _app_failed
    if _app is not None:  # fast path — GIL makes single object read atomic
        return _app
    with _app_lock:
        if _app is not None:
            return _app
        if _app_failed:
            return None
        try:
            from insightface.app import FaceAnalysis
            a = FaceAnalysis(name="buffalo_sc", providers=["CPUExecutionProvider"])
            a.prepare(ctx_id=0, det_size=(320, 320))
            _app = a
            log.info("[recognition] InsightFace buffalo_sc loaded")
        except Exception as exc:
            _app_failed = True
            log.warning("[recognition] InsightFace unavailable: %s", exc)
        return _app


# ---------------------------------------------------------------------------
# In-memory encoding cache (rebuilt from DB on startup + updated on enroll)
# ---------------------------------------------------------------------------

_cache_lock = threading.RLock()
_cached_encodings: list[np.ndarray] = []
_cached_person_ids: list[int] = []
_cached_enc_ids: list[int] = []   # face_encodings.id — for targeted removal


def load_cache_from_db(db) -> None:
    """Load all face_encodings rows into memory. Call at startup."""
    global _cached_encodings, _cached_person_ids, _cached_enc_ids
    with _cache_lock:
        rows = db.execute(
            "SELECT id, person_id, encoding FROM face_encodings"
        ).fetchall()
        _cached_encodings = []
        _cached_person_ids = []
        _cached_enc_ids = []
        for row in rows:
            try:
                blob = bytes(row["encoding"])
                # New format: raw float32 bytes. Legacy format: pickle blob.
                # Try raw bytes first; fall back to pickle for old rows.
                if len(blob) % 4 == 0 and len(blob) >= 4:
                    enc = np.frombuffer(blob, dtype=np.float32).copy()
                    if enc.ndim == 1 and enc.shape[0] >= 64:
                        _cached_encodings.append(enc)
                        _cached_person_ids.append(row["person_id"])
                        _cached_enc_ids.append(row["id"])
                        continue
                # Legacy pickle path — safe here because it's read-once at startup
                # from our own DB; new enrollments always write raw bytes.
                import pickle
                enc = pickle.loads(blob)  # noqa: S301
                _cached_encodings.append(enc)
                _cached_person_ids.append(row["person_id"])
                _cached_enc_ids.append(row["id"])
            except Exception:
                pass
    log.info("[recognition] cache loaded: %d encoding(s)", len(_cached_encodings))


def _add_to_cache(enc_id: int, person_id: int, encoding: np.ndarray) -> None:
    with _cache_lock:
        _cached_encodings.append(encoding)
        _cached_person_ids.append(person_id)
        _cached_enc_ids.append(enc_id)


def _remove_from_cache(enc_id: int) -> None:
    with _cache_lock:
        if enc_id in _cached_enc_ids:
            idx = _cached_enc_ids.index(enc_id)
            _cached_encodings.pop(idx)
            _cached_person_ids.pop(idx)
            _cached_enc_ids.pop(idx)


def _remove_person_from_cache(person_id: int) -> None:
    with _cache_lock:
        indices = [i for i, p in enumerate(_cached_person_ids) if p == person_id]
        for i in reversed(indices):
            _cached_encodings.pop(i)
            _cached_person_ids.pop(i)
            _cached_enc_ids.pop(i)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def encode_face(frame_bytes: bytes) -> Optional[np.ndarray]:
    """
    Run InsightFace on a JPEG frame, return the 512-d L2-normalised embedding
    of the highest-confidence face found, or None if no face is detected.
    """
    app = _get_app()
    if app is None:
        return None
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(frame_bytes)).convert("RGB")
        arr = np.array(img)
        with _app_lock:
            faces = app.get(arr)
        if not faces:
            return None
        # Pick the largest face (most prominent in frame).
        # No det_score filter here — MediaPipe already validated the face at
        # 75%+ confidence before encode_face() is called.
        best = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        emb = best.embedding
        # InsightFace already L2-normalises; guard anyway
        norm = np.linalg.norm(emb)
        if norm > 0:
            emb = emb / norm
        return emb.astype(np.float32)
    except Exception as exc:
        log.debug("[recognition] encode_face error: %s", exc)
        return None


def recognize(embedding: np.ndarray) -> tuple[Optional[int], float]:
    """
    Compare embedding against cache.
    Returns (person_id, similarity) of best match if >= COSINE_THRESHOLD, else (None, 0.0).
    """
    with _cache_lock:
        if not _cached_encodings:
            return None, 0.0
        sims = [float(np.dot(embedding, e)) for e in _cached_encodings]
        best_idx = int(np.argmax(sims))
        best_sim = sims[best_idx]
        if best_sim >= COSINE_THRESHOLD:
            return _cached_person_ids[best_idx], best_sim
    return None, best_sim


def enroll(db, person_id: int, embedding: np.ndarray, event_id: Optional[int] = None) -> int:
    """
    Store a new face encoding for person_id and update the in-memory cache.
    Returns the face_encodings.id (existing row if already enrolled for this event).
    """
    # Idempotent: if this event was already enrolled for this person, return existing id.
    if event_id is not None:
        existing = db.execute(
            "SELECT id FROM face_encodings WHERE person_id=? AND event_id=?",
            (person_id, event_id),
        ).fetchone()
        if existing:
            return existing["id"]
    blob = embedding.astype(np.float32).tobytes()
    cur = db.execute(
        "INSERT INTO face_encodings (person_id, event_id, encoding) VALUES (?, ?, ?)",
        (person_id, event_id, blob),
    )
    db.commit()
    enc_id = cur.lastrowid
    _add_to_cache(enc_id, person_id, embedding)
    log.info("[recognition] enrolled encoding %d for person %d", enc_id, person_id)
    return enc_id


def remove_encoding(db, enc_id: int) -> None:
    """Delete a specific face encoding from DB and cache."""
    db.execute("DELETE FROM face_encodings WHERE id=?", (enc_id,))
    db.commit()
    _remove_from_cache(enc_id)


def remove_person_encodings(db, person_id: int) -> None:
    """Remove all encodings for a person (called when person is deleted)."""
    db.execute("DELETE FROM face_encodings WHERE person_id=?", (person_id,))
    db.commit()
    _remove_person_from_cache(person_id)
