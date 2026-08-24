"""
sentinel/api/timeline.py — Timeline, video streaming, and clip export endpoints.

Endpoints:
  GET  /api/timeline              — segment map + event markers for a time range
  GET  /api/recordings/{id}/video — serve segment with HTTP range requests (206)
  POST /api/export                — clip export via FFmpeg stream-copy → MP4 download
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse, FileResponse
from pydantic import BaseModel

from sentinel.config import settings
from sentinel.db import get_db
from sentinel.util.logging import get_logger

log = get_logger("sentinel.api.timeline")
router = APIRouter(tags=["timeline"])

# Cap concurrent FFmpeg export jobs so they don't starve the recorder processes.
_export_sem = asyncio.Semaphore(2)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RECORDINGS_ROOT = settings.sentinel_recordings_dir


def _safe_recording_path(path_str: str) -> Path:
    """Resolve path and reject any path-traversal attempts.

    The resolved path must be inside sentinel_recordings_dir.
    Raises HTTPException 400 on traversal, 404 if not found.
    """
    try:
        p = Path(path_str).resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")

    root = _RECORDINGS_ROOT.resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal rejected")

    if not p.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")
    return p


def _parse_range_header(range_header: str, file_size: int) -> tuple[int, int]:
    """Parse a 'Range: bytes=start-end' header.

    Returns (start, end) inclusive.  Raises HTTPException 416 on bad range.
    """
    m = re.match(r"bytes=(\d*)-(\d*)", range_header)
    if not m:
        raise HTTPException(
            status_code=416,
            detail="Unsupported Range format",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    start_str, end_str = m.group(1), m.group(2)

    if not start_str and not end_str:
        raise HTTPException(
            status_code=416,
            detail="Empty Range",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    if not start_str:
        # Suffix range: bytes=-N  → last N bytes
        suffix = int(end_str)
        start = max(0, file_size - suffix)
        end = file_size - 1
    else:
        start = int(start_str)
        end = int(end_str) if end_str else file_size - 1

    if start > end or start >= file_size:
        raise HTTPException(
            status_code=416,
            detail="Range Not Satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    end = min(end, file_size - 1)
    return start, end


# ---------------------------------------------------------------------------
# Timeline endpoint
# ---------------------------------------------------------------------------

@router.get("/timeline")
async def get_timeline(
    camera_id: int = Query(..., description="Camera ID"),
    start: str = Query(..., description="ISO-8601 range start (inclusive)"),
    end: str = Query(..., description="ISO-8601 range end (exclusive)"),
) -> dict:
    """
    Return the segment map + event markers for a camera over a time range.

    Segments are ordered by started_at.  has_gap=True on a segment means
    there was a recording gap *before* this segment started.

    Response shape:
      {
        camera_id: int,
        start: str,
        end: str,
        segments: [
          { id, path, url, started_at, ended_at, duration_s, has_gap }
        ],
        events: [
          { id, type, label, score, started_at, ended_at, thumb_url }
        ],
      }
    """
    db = get_db()

    # Verify camera exists.
    cam = db.execute("SELECT id FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")

    # Segments that overlap [start, end).
    # A segment overlaps if started_at < end AND (ended_at > start OR it's still
    # recording AND started within this window).
    #
    # The `ended_at IS NULL` branch is further restricted to segments that
    # started in the last 5 minutes. Any recording that has been "open" longer
    # than that is almost certainly a crashed/killed FFmpeg whose file was never
    # finalized (no moov atom → unplayable). Since segments are ~2 minutes,
    # the currently-recording segment is at most a couple minutes old; older
    # NULL-ended_at rows are ghosts and should not appear on the timeline.
    seg_rows = db.execute(
        """
        SELECT id, path, started_at, ended_at, duration_s, has_gap
        FROM   recordings
        WHERE  camera_id = ?
          AND  started_at < ?
          AND  (
                  ended_at > ?
               OR (ended_at IS NULL
                   AND started_at >= ?
                   AND started_at >= datetime('now', '-5 minutes'))
              )
        ORDER  BY started_at
        """,
        (camera_id, end, start, start),
    ).fetchall()

    segments = []
    for r in seg_rows:
        # Skip DB entries whose file is missing or is a stub (< 4 KB = no real video frames).
        try:
            p = Path(r["path"]).resolve()
            if not p.exists() or p.stat().st_size < 4096:
                continue
        except Exception:
            continue
        segments.append(
            {
                "id": r["id"],
                "path": r["path"],
                "url": f"/api/recordings/{r['id']}/video",
                "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "duration_s": r["duration_s"],
                "has_gap": bool(r["has_gap"]),
            }
        )

    # Events whose START falls within [start, end).
    #
    # An event is a point-in-time marker on the timeline: the UI pins it and
    # seeks to its started_at.  We therefore window events by started_at alone,
    # NOT by interval overlap.  The previous overlap clause
    # `(ended_at IS NULL OR ended_at > start)` caused stale, never-closed events
    # (ended_at IS NULL) to be treated as "ongoing forever" and leak onto the
    # timeline of every subsequent day, where clicking them found no covering
    # recording segment ("No recording covers this time" / failed to load).
    evt_rows = db.execute(
        """
        SELECT id, type, label, score, started_at, ended_at, thumb_path
        FROM   events
        WHERE  camera_id = ?
          AND  started_at >= ?
          AND  started_at < ?
        ORDER  BY started_at
        """,
        (camera_id, start, end),
    ).fetchall()

    events = []
    for r in evt_rows:
        events.append(
            {
                "id": r["id"],
                "type": r["type"],
                "label": r["label"],
                "score": r["score"],
                "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "thumb_url": f"/api/events/{r['id']}/thumb" if r["thumb_path"] else None,
            }
        )

    return {
        "camera_id": camera_id,
        "start": start,
        "end": end,
        "segments": segments,
        "events": events,
    }


# ---------------------------------------------------------------------------
# Timeline thumbnail — single JPEG frame from a recording at a given time
# ---------------------------------------------------------------------------

@router.get("/recordings/thumbnail")
async def get_recording_thumbnail(
    camera_id: int = Query(..., description="Camera ID"),
    t: str = Query(..., description="ISO-8601 timestamp"),
) -> Response:
    """
    Extract a single JPEG frame from the recording segment covering timestamp t.
    Returns 404 if no recording covers that time.
    Supports ?token= query param so <img> elements can use it directly.
    """
    from datetime import datetime as _dt, timezone as _tz

    db = get_db()
    # Selection must mirror ContinuousVideoPlayer.findSegIdx(): the player scans
    # segments in ascending started_at order and plays the FIRST one covering t.
    # Ordering DESC here made the hover thumbnail come from a different segment
    # than playback whenever two rows overlapped. Stub files (< 4 KB) and stale
    # never-closed rows are excluded the same way the /timeline endpoint does.
    row = db.execute(
        """
        SELECT path, started_at FROM recordings
        WHERE camera_id = ?
          AND started_at <= ?
          AND (size_bytes IS NULL OR size_bytes >= 4096)
          AND (
                ended_at >= ?
             OR (ended_at IS NULL AND started_at >= datetime('now', '-5 minutes'))
              )
        ORDER BY started_at ASC LIMIT 1
        """,
        (camera_id, t, t),
    ).fetchone()

    if not row or not row["path"] or not Path(row["path"]).exists():
        raise HTTPException(status_code=404, detail="No recording at this time")

    try:
        seg_start = _dt.fromisoformat(row["started_at"].replace("Z", "+00:00"))
        t_dt = _dt.fromisoformat(t.replace("Z", "+00:00"))
        offset_s = max(0.0, (t_dt - seg_start).total_seconds())
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid timestamp format")

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                [
                    str(settings.ffmpeg_path), "-y",
                    "-ss", f"{offset_s:.3f}",
                    "-i", row["path"],
                    "-vframes", "1",
                    "-q:v", "4",
                    "-vf", "scale=320:-2",
                    tmp_path,
                ],
                capture_output=True,
                timeout=10,
            ),
        )
        if result.returncode != 0 or not Path(tmp_path).exists():
            raise HTTPException(status_code=500, detail="Frame extraction failed")
        data = Path(tmp_path).read_bytes()
        return Response(
            content=data,
            media_type="image/jpeg",
            headers={"Cache-Control": "max-age=300, immutable"},
        )
    except Exception as exc:
        log.debug("thumbnail extraction error: %s", exc)
        raise HTTPException(status_code=500, detail="Frame extraction error")
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Video streaming with HTTP range requests
# ---------------------------------------------------------------------------

_CHUNK = 1 << 20  # 1 MB streaming chunks


@router.get("/recordings/{recording_id}/video")
async def stream_recording_video(recording_id: int, request: Request):
    """
    Serve a recording segment as a video/mp4 byte stream.

    Supports HTTP Range requests (Accept-Ranges: bytes).
    Returns 206 Partial Content for range requests, 200 for full-file requests.
    Validates that the resolved file path stays within the recordings root
    (path-traversal protection).
    """
    db = get_db()
    row = db.execute("SELECT path FROM recordings WHERE id=?", (recording_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Recording not found")

    file_path = _safe_recording_path(row["path"])
    file_size = file_path.stat().st_size

    range_header = request.headers.get("range")

    if range_header:
        start, end = _parse_range_header(range_header, file_size)
        length = end - start + 1

        def _iter_range():
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(_CHUNK, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Content-Type": "video/mp4",
        }
        return StreamingResponse(
            _iter_range(),
            status_code=206,
            headers=headers,
            media_type="video/mp4",
        )

    # Full file — still send Accept-Ranges so the client can range-request later.
    def _iter_full():
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        _iter_full(),
        status_code=200,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
            "Content-Type": "video/mp4",
        },
        media_type="video/mp4",
    )


# ---------------------------------------------------------------------------
# Clip export
# ---------------------------------------------------------------------------

class ExportRequest(BaseModel):
    camera_id: int
    start: str                 # ISO-8601 UTC
    end: str                   # ISO-8601 UTC
    watermark: bool = False    # burn timestamp + "Argus NVR" into the video
    watermark_text: str = ""   # optional custom watermark text (uses camera name if empty)


@router.post("/export")
async def export_clip(body: ExportRequest):
    """
    Export an MP4 clip covering [start, end) for a camera.

    Finds all recording segments that overlap the requested time range,
    and uses FFmpeg stream-copy (concat demuxer for multi-segment, or
    direct trim for single-segment) to produce a downloadable MP4.

    Wall-clock → segment-offset math:
      For each segment, the clip's offset into the segment is
        seg_offset = max(0, request_start - segment_start)
      and the duration is
        clip_end = min(request_end, segment_end)  →  clip_dur = clip_end - seg_offset_start

    Uses -movflags +faststart so the moov atom is at the front of the file
    (allows streaming/progressive playback).
    """
    from datetime import datetime, timezone

    def _parse_iso(s: str) -> datetime:
        # Accept either "Z" suffix or "+00:00"
        s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)

    try:
        req_start = _parse_iso(body.start)
        req_end = _parse_iso(body.end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid datetime: {exc}")

    if req_end <= req_start:
        raise HTTPException(status_code=422, detail="end must be after start")

    db = get_db()
    cam = db.execute("SELECT id FROM cameras WHERE id=?", (body.camera_id,)).fetchone()
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")

    # Find overlapping segments (ordered).
    rows = db.execute(
        """
        SELECT id, path, started_at, ended_at, duration_s
        FROM   recordings
        WHERE  camera_id = ?
          AND  started_at < ?
          AND  (ended_at IS NULL OR ended_at > ?)
        ORDER  BY started_at
        """,
        (body.camera_id, body.end, body.start),
    ).fetchall()

    if not rows:
        raise HTTPException(status_code=404, detail="No recordings found for requested range")

    # Validate all paths up-front.
    segments: list[dict] = []
    for r in rows:
        try:
            p = _safe_recording_path(r["path"])
        except HTTPException:
            continue  # skip missing / invalid paths

        seg_start_str = r["started_at"]  # e.g. "2026-05-24T15:37:15Z"
        seg_start = _parse_iso(seg_start_str)
        if r["ended_at"]:
            seg_end = _parse_iso(r["ended_at"])
        elif r["duration_s"]:
            from datetime import timedelta
            seg_end = seg_start + timedelta(seconds=float(r["duration_s"]))
        else:
            # Unknown duration — assume it ends at req_end
            seg_end = req_end

        # Clamp to requested range.
        clip_start_in_seg = max(0.0, (req_start - seg_start).total_seconds())
        clip_end_in_seg = min(
            (seg_end - seg_start).total_seconds(),
            (req_end - seg_start).total_seconds(),
        )
        clip_dur = clip_end_in_seg - clip_start_in_seg
        if clip_dur <= 0:
            continue

        segments.append(
            {
                "path": p,
                "seg_start": seg_start,
                "seg_end": seg_end,
                "clip_offset": clip_start_in_seg,
                "clip_dur": clip_dur,
            }
        )

    if not segments:
        raise HTTPException(status_code=404, detail="No valid segment files found")

    ffmpeg = str(settings.ffmpeg_path)

    # Resolve camera name for watermark
    cam_row = db.execute("SELECT name FROM cameras WHERE id=?", (body.camera_id,)).fetchone()
    cam_name = cam_row["name"] if cam_row else f"Camera {body.camera_id}"
    # Strip characters that break FFmpeg drawtext filter syntax
    cam_name = re.sub(r"[':,\\=\[\]]", "", cam_name)
    wm_text = re.sub(r"[':,\\=\[\]]", "", body.watermark_text or cam_name)

    # Temp directory for output on the same drive as recordings to avoid
    # filling the system (C:) drive with large MP4 clips.
    tmp_dir = tempfile.mkdtemp(
        prefix="sentinel_export_",
        dir=str(settings.sentinel_recordings_dir),
    )
    out_path = Path(tmp_dir) / "clip.mp4"

    # Cap concurrent FFmpeg exports at 2 so recording processes keep CPU priority.
    if _export_sem._value == 0:  # type: ignore[attr-defined]
        raise HTTPException(status_code=429, detail="Too many concurrent exports — try again shortly")
    await _export_sem.acquire()

    # When watermark is on we must re-encode (drawtext filter requires it).
    # Use fast libx264 so export isn't painfully slow.
    watermark_filter = (
        f"drawtext=text='{wm_text} %{{pts\\:localtime\\:{int(req_start.timestamp())}}}'"
        f":fontsize=24:fontcolor=white:x=10:y=10:box=1:boxcolor=black@0.5"
        if body.watermark else ""
    )

    try:
        if len(segments) == 1:
            seg = segments[0]
            if body.watermark:
                cmd = [
                    ffmpeg, "-y",
                    "-ss", str(seg["clip_offset"]),
                    "-t",  str(seg["clip_dur"]),
                    "-i",  str(seg["path"]),
                    "-vf", watermark_filter,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "copy",
                    "-movflags", "+faststart",
                    str(out_path),
                ]
            else:
                cmd = [
                    ffmpeg, "-y",
                    "-ss", str(seg["clip_offset"]),
                    "-t",  str(seg["clip_dur"]),
                    "-i",  str(seg["path"]),
                    "-c",  "copy",
                    "-movflags", "+faststart",
                    str(out_path),
                ]
            log.info("export single-seg watermark=%s: %s", body.watermark, " ".join(cmd))
            result = subprocess.run(cmd, capture_output=True, timeout=120)
        else:
            # Multi-segment: write a concat list then use concat demuxer.
            concat_list = Path(tmp_dir) / "list.txt"
            with open(concat_list, "w") as f:
                for seg in segments:
                    f.write(f"file '{seg['path']}'\n")
                    f.write(f"inpoint {seg['clip_offset']}\n")
                    f.write(f"outpoint {seg['clip_offset'] + seg['clip_dur']}\n")

            if body.watermark:
                cmd = [
                    ffmpeg, "-y",
                    "-f", "concat", "-safe", "0", "-i", str(concat_list),
                    "-vf", watermark_filter,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "copy",
                    "-movflags", "+faststart",
                    str(out_path),
                ]
            else:
                cmd = [
                    ffmpeg, "-y",
                    "-f", "concat", "-safe", "0", "-i", str(concat_list),
                    "-c", "copy",
                    "-movflags", "+faststart",
                    str(out_path),
                ]
            log.info("export multi-seg watermark=%s: %s segments", body.watermark, len(segments))
            result = subprocess.run(cmd, capture_output=True, timeout=300)
    finally:
        _export_sem.release()

    try:
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")
            log.error("ffmpeg export failed: %s", stderr[-2000:])
            raise HTTPException(
                status_code=500,
                detail=f"FFmpeg export failed: {stderr[-500:]}",
            )

        if not out_path.exists() or out_path.stat().st_size == 0:
            raise HTTPException(status_code=500, detail="Export produced empty file")

        # Stream the file back as a download, then clean up.
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        download_name = f"clip_cam{body.camera_id}_{ts}.mp4"

        file_size = out_path.stat().st_size

        def _iter_and_cleanup():
            try:
                with open(out_path, "rb") as f:
                    while True:
                        chunk = f.read(_CHUNK)
                        if not chunk:
                            break
                        yield chunk
            finally:
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)

        return StreamingResponse(
            _iter_and_cleanup(),
            media_type="video/mp4",
            headers={
                "Content-Disposition": f'attachment; filename="{download_name}"',
                "Content-Length": str(file_size),
            },
        )

    except HTTPException:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except Exception as exc:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
        log.error("export error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Time-lapse generation
# ---------------------------------------------------------------------------

class TimelapseRequest(BaseModel):
    camera_id: int
    start: str   # ISO-8601 UTC
    end: str     # ISO-8601 UTC
    speed: int = 60  # 1 source second = 1/speed output seconds (60 = 1 min → 1 sec)


@router.post("/timelapse")
async def generate_timelapse(body: TimelapseRequest):
    """
    Generate a time-lapse MP4 from recorded segments.
    speed=60 means 1 minute of footage → 1 second of time-lapse.
    Uses FFmpeg setpts + fps filter: no re-encode of audio (dropped).
    """
    from datetime import datetime, timezone, timedelta
    import shutil, tempfile, subprocess

    def _parse_iso(s):
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    try:
        req_start = _parse_iso(body.start)
        req_end = _parse_iso(body.end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid datetime: {exc}")

    if body.speed < 1 or body.speed > 3600:
        raise HTTPException(status_code=422, detail="speed must be 1–3600")

    db = get_db()
    cam = db.execute("SELECT id FROM cameras WHERE id=?", (body.camera_id,)).fetchone()
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")

    rows = db.execute(
        """
        SELECT path, started_at, ended_at FROM recordings
        WHERE camera_id=? AND started_at < ? AND (ended_at IS NULL OR ended_at > ?)
        ORDER BY started_at
        """,
        (body.camera_id, body.end, body.start),
    ).fetchall()

    valid_paths = []
    for r in rows:
        try:
            p = _safe_recording_path(r["path"])
            valid_paths.append(str(p))
        except HTTPException:
            continue

    if not valid_paths:
        raise HTTPException(status_code=404, detail="No recordings found for range")

    tmp_dir = tempfile.mkdtemp(prefix="sentinel_tl_", dir=str(settings.sentinel_recordings_dir))
    out_path = Path(tmp_dir) / "timelapse.mp4"
    ffmpeg = str(settings.ffmpeg_path)

    try:
        concat_file = Path(tmp_dir) / "list.txt"
        with open(concat_file, "w") as f:
            for p in valid_paths:
                f.write(f"file '{p}'\n")

        # setpts speeds up video; fps=30 ensures smooth output; drop audio
        pts_expr = f"PTS/{body.speed}"
        cmd = [
            ffmpeg, "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_file),
            "-vf", f"setpts={pts_expr},fps=30",
            "-an",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-movflags", "+faststart",
            str(out_path),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")
            raise HTTPException(status_code=500, detail=f"FFmpeg failed: {stderr[-400:]}")

        if not out_path.exists() or out_path.stat().st_size == 0:
            raise HTTPException(status_code=500, detail="Timelapse produced empty file")

        from datetime import datetime as _dt
        ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        filename = f"timelapse_cam{body.camera_id}_{ts}.mp4"
        file_size = out_path.stat().st_size

        def _iter_cleanup():
            try:
                with open(out_path, "rb") as f:
                    while True:
                        chunk = f.read(_CHUNK)
                        if not chunk:
                            break
                        yield chunk
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        return StreamingResponse(
            _iter_cleanup(),
            media_type="video/mp4",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(file_size),
            },
        )
    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(exc))
