"""
sentinel/api/proxy.py — Reverse-proxy go2rtc through the FastAPI app.

Adds routes under /go2rtc/{path:path} that forward HTTP and WebSocket
requests to go2rtc's internal API (http://127.0.0.1:<go2rtc_api_port>).

Why:
  go2rtc listens only on localhost.  When the browser talks to go2rtc
  directly it must connect to 127.0.0.1:1984 — which only works on the
  server machine.  Proxying through FastAPI means the browser sends all
  traffic to the single app origin (e.g. http://192.168.1.150:8080) and
  works from other LAN machines, phones, and over Tailscale.

Routes:
  GET/POST/… /go2rtc/{path}   — HTTP proxy (streams, frame.jpeg, webrtc SDP …)
  WS          /go2rtc/api/ws  — WebSocket proxy (WebRTC signalling)
  GET         /go2rtc/api/stream.mp4  — MSE fragmented-MP4 (streaming passthrough)

Auth:
  HTTP routes  — require Bearer token (same as every other protected route).
  WebSocket    — accepts token as ?token=<jwt> query parameter (same pattern
                 used by the events WebSocket so the frontend can connect).
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import StreamingResponse

from sentinel.config import settings
from sentinel.services.go2rtc import go2rtc_api_base
from sentinel.util.auth import decode_access_token, get_current_user
from sentinel.util.logging import get_logger

log = get_logger("sentinel.api.proxy")

router = APIRouter(prefix="/go2rtc", tags=["proxy"])

# go2rtc treats a `src` query value as EITHER a known stream name OR an inline
# source expression it will spawn/fetch — `exec:<cmd>` (runs a shell command),
# `ffmpeg:<…>`, `http://…`, `rtsp://…`, etc. If we forward an attacker-supplied
# src verbatim, this proxy becomes remote command execution + SSRF on the NVR
# host, reachable by anyone holding any valid token (including a low-privilege
# view-only ?token= used in an <img>). Legitimate sources are go2rtc stream
# slugs produced by build_go2rtc_stream_name(): lowercase alphanumerics plus
# `_`/`-` (and the `_sub`/`_mjpeg` suffixes). Anything with a scheme separator,
# slash, or other character is rejected.
_SRC_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _src_values_ok(params) -> bool:
    """True if every `src` query value is a plain stream name (no source expr)."""
    return all(_SRC_RE.match(v or "") for k, v in params.multi_items() if k == "src")

# Shared async HTTP client (re-used across requests for connection pooling).
# Timeouts: connect quickly; read/write can be long for streaming content.
_HTTP_CLIENT: Optional[httpx.AsyncClient] = None


def _client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        _HTTP_CLIENT = httpx.AsyncClient(
            # read=None: live streams (MSE/MJPEG) run indefinitely — never time out
            # waiting for the next chunk. connect/write stay bounded so hung
            # cameras don't hold a connection forever during setup.
            # pool=None: never raise PoolTimeout while a long-lived live stream
            # holds a connection — a new viewer must be able to open another
            # connection without the pool acquisition timing out.
            timeout=httpx.Timeout(connect=5.0, read=None, write=30.0, pool=None),
            # Generous pool: each live <video>/<img> holds an upstream
            # connection open for the life of the stream. With a handful of
            # cameras x reconnects the default (max 100) is plenty, but we make
            # the ceiling explicit and keep connections fresh.
            limits=httpx.Limits(
                max_connections=200,
                max_keepalive_connections=50,
                keepalive_expiry=30.0,
            ),
            follow_redirects=True,
        )
    return _HTTP_CLIENT


async def _ws_token_auth(websocket: WebSocket) -> dict:
    """
    Authenticate a WebSocket connection by reading the `token` query parameter.
    Raises HTTP 403 (which closes the WS with a 403 handshake) on failure.
    """
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing token")
    try:
        payload = decode_access_token(token)
        return payload
    except Exception:
        await websocket.close(code=4001)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid token")


# ---------------------------------------------------------------------------
# WebSocket proxy — /go2rtc/api/ws  (WebRTC signalling)
# ---------------------------------------------------------------------------

@router.websocket("/api/ws")
async def proxy_ws(websocket: WebSocket):
    """
    Proxy the WebRTC signalling WebSocket to go2rtc.
    Auth: ?token=<jwt> query param.
    """
    await _ws_token_auth(websocket)
    # Reject inline source expressions before accepting — the WS signalling URL
    # carries `src`, so `?src=exec:<cmd>` here is the same RCE/SSRF vector as the
    # HTTP proxy below.
    if not _src_values_ok(websocket.query_params):
        await websocket.close(code=4003)
        return
    await websocket.accept()

    # Reconstruct go2rtc WS URL, forwarding all query params except `token`.
    qs = "&".join(
        f"{k}={v}"
        for k, v in websocket.query_params.items()
        if k != "token"
    )
    target = f"ws://127.0.0.1:{settings.go2rtc_api_port}/api/ws"
    if qs:
        target = f"{target}?{qs}"

    from websockets.asyncio.client import connect as ws_connect
    try:
        async with ws_connect(target) as go2rtc_ws:
            async def client_to_go2rtc():
                try:
                    async for msg in websocket.iter_text():
                        await go2rtc_ws.send(msg)
                except WebSocketDisconnect:
                    pass
                except Exception as exc:
                    log.debug("client→go2rtc error: %s", exc)

            async def go2rtc_to_client():
                try:
                    while True:
                        msg = await go2rtc_ws.recv()
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except Exception as exc:
                    log.debug("go2rtc→client error: %s", exc)

            await asyncio.gather(client_to_go2rtc(), go2rtc_to_client())
    except Exception as exc:
        log.warning("WS proxy error: %s", exc)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# HTTP proxy — /go2rtc/{path}
# ---------------------------------------------------------------------------

async def _http_auth(request: Request) -> dict:
    """
    Authenticate via EITHER an ``Authorization: Bearer`` header OR a
    ``?token=<jwt>`` query parameter. The query-param form is essential because
    browser <video> (MSE stream.mp4) and <img> (frame.jpeg) elements cannot
    attach an Authorization header.
    """
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    if not token:
        token = request.query_params.get("token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        return decode_access_token(token)
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


# Only read methods are proxied. The browser needs GET (frame.jpeg, stream.mp4,
# HLS) and CORS preflight (OPTIONS/HEAD); it never mutates go2rtc through this
# proxy. All go2rtc mutations (creating/removing streams via PUT/DELETE
# /api/streams) happen server-side against 127.0.0.1 directly. Dropping the
# write verbs removes the `PUT /api/streams?src=exec:<cmd>` stream-creation
# vector; the `src` validation below covers the GET lazy-source vector.
@router.api_route(
    "/{path:path}",
    methods=["GET", "OPTIONS", "HEAD"],
    dependencies=[Depends(_http_auth)],
)
async def proxy_http(request: Request, path: str):
    """
    Forward a read-only HTTP request to go2rtc's internal API.
    Streaming responses (MSE, MJPEG) are forwarded as StreamingResponse.
    """
    if not _src_values_ok(request.query_params):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid stream source")
    base = go2rtc_api_base()
    target_url = f"{base}/{path}"
    # Forward query params, but strip our auth `token` (go2rtc doesn't need it).
    fwd = [(k, v) for k, v in request.query_params.multi_items() if k != "token"]
    if fwd:
        from urllib.parse import urlencode
        target_url = f"{target_url}?{urlencode(fwd)}"

    # Forward the request body and selected headers.
    body = await request.body()
    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "authorization", "content-length")
    }
    if body:
        forward_headers["content-length"] = str(len(body))

    # Open the upstream request in STREAMING mode (stream=True) so the body is
    # NOT buffered.  This is essential for live endpoints (stream.mp4 / MJPEG
    # never end — buffering them hangs forever); it is also correct for finite
    # responses (frame.jpeg, JSON), which simply stream through and close.
    req = _client().build_request(
        method=request.method,
        url=target_url,
        headers=forward_headers,
        content=body,
    )
    try:
        go2rtc_resp = await _client().send(req, stream=True)
    except httpx.ConnectError as exc:
        raise HTTPException(status_code=502, detail=f"go2rtc unreachable: {exc}")
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail=f"go2rtc timeout: {exc}")

    content_type = go2rtc_resp.headers.get("content-type", "")
    # Drop hop-by-hop and length/encoding headers — the body is re-chunked.
    exclude_resp_headers = {"transfer-encoding", "connection", "content-encoding", "content-length"}
    resp_headers = {
        k: v for k, v in go2rtc_resp.headers.items()
        if k.lower() not in exclude_resp_headers
    }

    async def _iter():
        try:
            async for chunk in go2rtc_resp.aiter_bytes(chunk_size=65536):
                yield chunk
        except (httpx.StreamClosed, httpx.ReadError, httpx.RemoteProtocolError) as exc:
            # Upstream (go2rtc) closed the live stream — e.g. it restarted or
            # the RTSP source reconnected. The browser-side player detects the
            # EOF and reconnects on its own, so just end the generator quietly.
            log.debug("upstream stream closed for %s: %s", path, exc)
        except asyncio.CancelledError:
            # The browser disconnected. Give go2rtc a brief window to finish
            # its current write before we signal connection close — go2rtc
            # 1.9.14 has a nil-pointer race in consumer.go:82 that fires when
            # the HTTP response is torn down while a frame write is in flight.
            try:
                await asyncio.shield(asyncio.sleep(0.15))
            except asyncio.CancelledError:
                pass
            raise
        finally:
            await go2rtc_resp.aclose()

    return StreamingResponse(
        _iter(),
        status_code=go2rtc_resp.status_code,
        headers=resp_headers,
        media_type=content_type or None,
    )
