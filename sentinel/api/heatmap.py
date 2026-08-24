"""
sentinel/api/heatmap.py — Motion heatmap endpoints.

Heatmaps are per-camera PNG overlays that visualise cumulative motion density.
The accumulator is a simple JSON file updated by the detector service
(sentinel/services/detector.py writes to heatmap/<camera_id>.json).
The PNG is rendered on-the-fly using Pillow if available, otherwise a 1-pixel
placeholder is returned.

Endpoints:
  GET /api/cameras/{id}/heatmap   — current heatmap PNG (or placeholder)
  DELETE /api/cameras/{id}/heatmap — reset the heatmap accumulator
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from sentinel.config import settings
from sentinel.db import get_db
from sentinel.util.logging import get_logger

log = get_logger("sentinel.api.heatmap")
router = APIRouter(tags=["heatmap"])


def _heatmap_path(camera_id: int) -> Path:
    return settings.sentinel_data_root / "heatmaps" / f"{camera_id}.json"


@router.get("/cameras/{camera_id}/heatmap")
async def get_heatmap(camera_id: int, width: int = 640, height: int = 360):
    """
    Return the motion heatmap for this camera as a PNG.
    Width/height should match the camera's resolution for accurate overlay.
    Returns a transparent PNG placeholder if no heatmap data exists yet.
    """
    db = get_db()
    cam = db.execute("SELECT id FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")

    hmap_path = _heatmap_path(camera_id)
    width = max(16, min(3840, width))
    height = max(16, min(2160, height))

    try:
        from PIL import Image, ImageFilter
        import numpy as np

        if hmap_path.exists():
            data = json.loads(hmap_path.read_text())
            grid_w = data.get("width", width)
            grid_h = data.get("height", height)
            cells = data.get("cells", [])
            arr = np.zeros((grid_h, grid_w), dtype=np.float32)
            for cell in cells:
                r, c, v = cell["r"], cell["c"], cell["v"]
                if 0 <= r < grid_h and 0 <= c < grid_w:
                    arr[r, c] = v
            # Normalize
            mx = arr.max()
            if mx > 0:
                arr = arr / mx
            # Convert to RGBA heat overlay (blue→red gradient)
            rgba = np.zeros((grid_h, grid_w, 4), dtype=np.uint8)
            rgba[..., 0] = (arr * 255).astype(np.uint8)         # R
            rgba[..., 2] = ((1 - arr) * 180).astype(np.uint8)  # B
            rgba[..., 3] = (arr * 200).astype(np.uint8)         # A
            img = Image.fromarray(rgba, "RGBA")
        else:
            img = Image.new("RGBA", (width, height), (0, 0, 0, 0))

        # Scale to requested dimensions
        img = img.resize((width, height), Image.LANCZOS)
        img = img.filter(ImageFilter.GaussianBlur(radius=max(2, width // 80)))

        import io
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png")

    except ImportError:
        # Pillow/numpy not available — return a 1×1 transparent PNG
        _TRANSPARENT_PNG = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0bIDATx\x9cc"
            b"\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        return Response(content=_TRANSPARENT_PNG, media_type="image/png")
    except Exception as exc:
        log.warning("heatmap render failed cam=%s: %s", camera_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete("/cameras/{camera_id}/heatmap", status_code=204)
async def reset_heatmap(camera_id: int):
    """Clear the motion heatmap accumulator for this camera."""
    db = get_db()
    cam = db.execute("SELECT id FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")
    hmap_path = _heatmap_path(camera_id)
    try:
        hmap_path.unlink(missing_ok=True)
    except Exception as exc:
        log.warning("heatmap delete failed cam=%s: %s", camera_id, exc)
