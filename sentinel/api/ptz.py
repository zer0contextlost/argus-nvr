"""
sentinel/api/ptz.py — ONVIF PTZ control endpoints.

Endpoints:
  GET  /api/cameras/{id}/ptz/presets     — list ONVIF presets
  POST /api/cameras/{id}/ptz/move        — relative move (pan/tilt/zoom)
  POST /api/cameras/{id}/ptz/goto        — goto preset by token
  POST /api/cameras/{id}/ptz/stop        — stop movement

Camera must have ptz_capable=1 in the cameras table.
ONVIF credentials are taken from the camera's RTSP user/password.
The PTZ service URL defaults to http://<host>/onvif/ptz_service.
"""

from __future__ import annotations

import html as _html
import textwrap
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sentinel.db import get_db
from sentinel.util.crypto import decrypt_password
from sentinel.util.logging import get_logger

log = get_logger("sentinel.api.ptz")
router = APIRouter(tags=["ptz"])

_SOAP_NS = textwrap.dedent("""
    xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
    xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"
    xmlns:tt="http://www.onvif.org/ver10/schema"
""").strip()

_SOAP_WRAP = """\
<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope {ns}>
  <SOAP-ENV:Body>{body}</SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""


def _envelope(body: str) -> str:
    return _SOAP_WRAP.format(ns=_SOAP_NS, body=body)


async def _onvif_post(url: str, user: str, password: str, body: str) -> str:
    """Send an ONVIF SOAP request; return the raw response text."""
    payload = _envelope(body)
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.post(
            url,
            content=payload.encode(),
            headers={"Content-Type": "application/soap+xml"},
            auth=(user, password),
        )
    if resp.status_code not in (200, 400):
        raise HTTPException(status_code=502, detail=f"ONVIF returned HTTP {resp.status_code}")
    return resp.text


def _ptz_url(camera_row) -> str:
    cfg: dict = {}
    try:
        import json
        cfg = json.loads(camera_row["ptz_config_json"] or "{}")
    except Exception:
        pass
    return cfg.get("ptz_url") or f"http://{camera_row['host']}/onvif/ptz_service"


def _profile_token(camera_row) -> str:
    try:
        import json
        cfg = json.loads(camera_row["ptz_config_json"] or "{}")
        return cfg.get("profile_token") or "MainStreamProfileToken"
    except Exception:
        return "MainStreamProfileToken"


def _get_ptz_camera(camera_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Camera not found")
    if not row["ptz_capable"]:
        raise HTTPException(status_code=400, detail="Camera does not have PTZ enabled")
    return row


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class PTZMoveRequest(BaseModel):
    pan: float = 0.0    # -1.0 to 1.0
    tilt: float = 0.0   # -1.0 to 1.0
    zoom: float = 0.0   # -1.0 to 1.0
    speed_pan: float = 0.5
    speed_tilt: float = 0.5


class PTZGotoRequest(BaseModel):
    preset_token: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/cameras/{camera_id}/ptz/presets")
async def list_presets(camera_id: int) -> dict:
    """List ONVIF PTZ presets for the camera."""
    row = _get_ptz_camera(camera_id)
    password = decrypt_password(row["rtsp_pass_enc"])
    url = _ptz_url(row)
    token = _html.escape(str(_profile_token(row)))

    body = f"""
    <tptz:GetPresets>
      <tptz:ProfileToken>{token}</tptz:ProfileToken>
    </tptz:GetPresets>"""

    try:
        resp_text = await _onvif_post(url, row["rtsp_user"], password, body)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ONVIF error: {exc}")

    import re
    presets = []
    for m in re.finditer(
        r'<[^:]*Preset\b[^>]*token="([^"]+)"[^>]*>.*?<[^:]*Name>([^<]+)</[^:]*Name>',
        resp_text,
        re.DOTALL,
    ):
        presets.append({"token": m.group(1), "name": m.group(2)})

    return {"presets": presets}


@router.post("/cameras/{camera_id}/ptz/move")
async def relative_move(camera_id: int, body: PTZMoveRequest) -> dict:
    """Perform a relative PTZ move."""
    row = _get_ptz_camera(camera_id)
    password = decrypt_password(row["rtsp_pass_enc"])
    url = _ptz_url(row)
    token = _html.escape(str(_profile_token(row)))

    pan = max(-1.0, min(1.0, body.pan))
    tilt = max(-1.0, min(1.0, body.tilt))
    zoom = max(-1.0, min(1.0, body.zoom))
    sp = max(0.0, min(1.0, body.speed_pan))
    st = max(0.0, min(1.0, body.speed_tilt))

    soap_body = f"""
    <tptz:RelativeMove>
      <tptz:ProfileToken>{token}</tptz:ProfileToken>
      <tptz:Translation>
        <tt:PanTilt x="{pan:.4f}" y="{tilt:.4f}"/>
        <tt:Zoom x="{zoom:.4f}"/>
      </tptz:Translation>
      <tptz:Speed>
        <tt:PanTilt x="{sp:.4f}" y="{st:.4f}"/>
      </tptz:Speed>
    </tptz:RelativeMove>"""

    try:
        await _onvif_post(url, row["rtsp_user"], password, soap_body)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ONVIF error: {exc}")

    return {"ok": True}


@router.post("/cameras/{camera_id}/ptz/goto")
async def goto_preset(camera_id: int, body: PTZGotoRequest) -> dict:
    """Go to an ONVIF PTZ preset."""
    row = _get_ptz_camera(camera_id)
    password = decrypt_password(row["rtsp_pass_enc"])
    url = _ptz_url(row)
    token = _html.escape(str(_profile_token(row)))
    preset_token_safe = _html.escape(str(body.preset_token))

    soap_body = f"""
    <tptz:GotoPreset>
      <tptz:ProfileToken>{token}</tptz:ProfileToken>
      <tptz:PresetToken>{preset_token_safe}</tptz:PresetToken>
    </tptz:GotoPreset>"""

    try:
        await _onvif_post(url, row["rtsp_user"], password, soap_body)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ONVIF error: {exc}")

    return {"ok": True}


@router.post("/cameras/{camera_id}/ptz/stop")
async def stop_ptz(camera_id: int) -> dict:
    """Stop PTZ movement."""
    row = _get_ptz_camera(camera_id)
    password = decrypt_password(row["rtsp_pass_enc"])
    url = _ptz_url(row)
    token = _html.escape(str(_profile_token(row)))

    soap_body = f"""
    <tptz:Stop>
      <tptz:ProfileToken>{token}</tptz:ProfileToken>
      <tptz:PanTilt>true</tptz:PanTilt>
      <tptz:Zoom>true</tptz:Zoom>
    </tptz:Stop>"""

    try:
        await _onvif_post(url, row["rtsp_user"], password, soap_body)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ONVIF error: {exc}")

    return {"ok": True}
