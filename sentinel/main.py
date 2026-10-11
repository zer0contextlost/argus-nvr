"""
sentinel/main.py — FastAPI application entrypoint.

Lifespan:
  startup  → init dirs, run DB migrations, bootstrap admin, start go2rtc, start recorders
  shutdown → stop recorders (indexes final segments), stop go2rtc, close DB

Routes (public):
  GET /api/health              — liveness ping (no auth required)
  POST /api/auth/login         — issue JWT token
  GET  /api/auth/me            — current user (requires token)
  POST /api/auth/logout        — logout (requires token)

Routes (protected — require Bearer token):
  /api/cameras, /api/live, /api/recordings, /api/events, /api/timeline,
  /api/settings, /api/system, /api/export
  /                    — serve built React frontend (static)

SPA fallback:
  Any GET that is not under /api/ and is not an existing static asset is
  served index.html so that client-side routes (e.g. /login, /cameras) work
  after a hard refresh or deep-link.  /api/* unknown paths still return a
  JSON 404, real static assets are served directly.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from sentinel.config import settings
from sentinel.db import run_migrations, close_db
from sentinel.services.go2rtc import start_go2rtc, stop_go2rtc
from sentinel.services.recorder import start_recorders, stop_recorders
from sentinel.services.detector import start_detectors, stop_detectors
from sentinel.services.storage import start_storage_manager, stop_storage_manager
from sentinel.services.backup import start_backup_service, stop_backup_service
from sentinel.services.camera_monitor import start_camera_monitor, stop_camera_monitor
from sentinel.services.notifier import start_notifier, stop_notifier, attach_notifier_to_pipeline
from sentinel.services.supervisor import start_global_watchdog, stop_global_watchdog
from sentinel.services.hikvision_alerts import start_alert_subscribers, stop_alert_subscribers
from sentinel.util.logging import get_logger
from sentinel.util.auth import get_current_user, get_current_user_flexible, bootstrap_admin_if_needed

log = get_logger("sentinel.main")

FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- Startup -----------------------------------------------------------
    log.info("Argus NVR starting up")

    # 1. Create all required directories on F:.
    settings.create_dirs()
    log.info("runtime directories verified")

    # 2. Run DB migrations (creates tables on first run).
    run_migrations()
    log.info("database migrations complete")

    # 2a. Load face recognition cache from DB.
    try:
        from sentinel.services.recognition import load_cache_from_db
        from sentinel.db import get_db
        load_cache_from_db(get_db())
    except Exception as exc:
        log.warning("face recognition cache load failed: %s", exc)

    # 2b. First-run admin bootstrap — create admin user if users table is empty.
    bootstrap_admin_if_needed()

    # 3. Start go2rtc supervisor.
    try:
        start_go2rtc()
    except Exception as exc:
        log.error("go2rtc failed to start: %s", exc)

    # 4. Give go2rtc a moment to open its RTSP port, then start recorders.
    import asyncio
    await asyncio.sleep(2.0)
    try:
        start_recorders()
    except Exception as exc:
        log.error("recorder startup failed: %s", exc)

    # 5. Start motion detectors (after recorders so gate is available).
    try:
        start_detectors()
    except Exception as exc:
        log.error("detector startup failed: %s", exc)

    # 6. Start the storage retention manager background task.
    try:
        await start_storage_manager()
    except Exception as exc:
        log.error("storage manager startup failed: %s", exc)

    # 6b. Start daily DB backup service.
    try:
        await start_backup_service()
    except Exception as exc:
        log.error("backup service startup failed: %s", exc)

    # 7. Start notifier + hook into detector event pipeline.
    try:
        start_notifier()
        await attach_notifier_to_pipeline()
    except Exception as exc:
        log.error("notifier startup failed: %s", exc)

    # 8. Start Hikvision ISAPI alert stream subscribers.
    try:
        start_alert_subscribers()
    except Exception as exc:
        log.error("alert subscriber startup failed: %s", exc)

    # 9. Start global watchdog — periodically checks all managed children.
    start_global_watchdog()

    # 10. Start camera offline monitor (after recorders and go2rtc are up).
    try:
        await start_camera_monitor()
    except Exception as exc:
        log.error("camera monitor startup failed: %s", exc)

    log.info(
        "Argus NVR ready — http://%s:%s",
        settings.sentinel_host,
        settings.sentinel_port,
    )
    yield

    # ---- Shutdown ----------------------------------------------------------
    log.info("Argus NVR shutting down")
    try:
        stop_global_watchdog()
    except Exception as exc:
        log.error("error stopping global watchdog: %s", exc)
    try:
        await stop_camera_monitor()
    except Exception as exc:
        log.error("error stopping camera monitor: %s", exc)
    try:
        stop_alert_subscribers()
    except Exception as exc:
        log.error("error stopping alert subscribers: %s", exc)
    try:
        stop_notifier()
    except Exception as exc:
        log.error("error stopping notifier: %s", exc)
    try:
        await stop_storage_manager()
    except Exception as exc:
        log.error("error stopping storage manager: %s", exc)
    try:
        await stop_backup_service()
    except Exception as exc:
        log.error("error stopping backup service: %s", exc)
    try:
        stop_detectors()
    except Exception as exc:
        log.error("error stopping detectors: %s", exc)
    try:
        stop_recorders()
    except Exception as exc:
        log.error("error stopping recorders: %s", exc)
    try:
        stop_go2rtc()
    except Exception as exc:
        log.error("error stopping go2rtc: %s", exc)
    close_db()
    log.info("shutdown complete")


app = FastAPI(
    title="Argus NVR",
    version="0.1.0",
    description="Self-hosted NVR for Hikvision cameras",
    lifespan=lifespan,
)

# CORS.
#
# Argus is a self-hosted NVR reached from arbitrary origins: the LAN IP
# (http://192.168.1.x:8080), localhost, the Vite dev server (:5173), and over
# Tailscale (http://100.x.y.z:8080 or an https MagicDNS name).  The previous
# allow-list was built from settings.sentinel_host, which resolves to
# 0.0.0.0/127.0.0.1 and therefore NEVER matched the real browser origin a
# remote machine uses — so the server returned no Access-Control-Allow-Origin
# header and cross-checked requests (media fetches, WebRTC SDP POSTs, etc.)
# from remote machines failed even though the API itself was reachable.
#
# Because every protected route still requires a JWT (Bearer header or
# ?token= query param), reflecting the request Origin is safe here: an
# attacker's page cannot read responses without a valid token, and tokens are
# never sent automatically (we use Authorization / explicit query params, not
# cookies).  We therefore reflect any origin via allow_origin_regex.
#
# NOTE: with a reflected/duplicated origin we must NOT also send
# allow_credentials=True together with a wildcard; we use a regex (which echoes
# the concrete origin, not "*"), so credentialed requests remain valid.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- API routers ------------------------------------------------------------
from sentinel.api.auth import router as auth_router
from sentinel.api.system import router as system_router
from sentinel.api.system import media_router as system_media_router
from sentinel.api.cameras import router as cameras_router
from sentinel.api.live import router as live_router
from sentinel.api.recordings import router as recordings_router
from sentinel.api.events import router as events_router, ws_router as events_ws_router
from sentinel.api.timeline import router as timeline_router
from sentinel.api.settings import router as settings_router
from sentinel.api.proxy import router as proxy_router
from sentinel.api.persons import router as persons_router
from sentinel.api.users import router as users_router
from sentinel.api.audit import router as audit_router
from sentinel.api.ptz import router as ptz_router
from sentinel.api.heatmap import router as heatmap_router
from sentinel.api.cameras_media import router as cameras_media_router
from sentinel.api.webpush import router as webpush_router
from sentinel.api.cloud_backup import router as cloud_backup_router

# Auth router — login is public; me/logout are individually protected.
app.include_router(auth_router, prefix="/api")

# Protected routers — every route requires a valid Bearer token.
_auth_dep = [Depends(get_current_user)]
# Flexible auth — Bearer OR ?token= query param; needed for routers that serve
# media to browser <video>/<img> elements which can't send Authorization headers.
_auth_dep_flex = [Depends(get_current_user_flexible)]

app.include_router(system_router, prefix="/api", dependencies=_auth_dep)
# QR code PNG — needs ?token= query auth so the Settings page <img> can load it.
app.include_router(system_media_router, prefix="/api", dependencies=_auth_dep_flex)
app.include_router(cameras_router, prefix="/api", dependencies=_auth_dep)
# Snapshot route — needs ?token= query auth so browser <img> tags can use it.
app.include_router(cameras_media_router, prefix="/api", dependencies=_auth_dep_flex)
app.include_router(live_router, prefix="/api", dependencies=_auth_dep)
# timeline_router MUST be mounted before recordings_router: it owns the
# literal routes /recordings/thumbnail and /recordings/{id}/video (flex auth
# for browser <img>/<video> elements), while recordings_router has a catch-all
# /recordings/{recording_id} (Bearer-only). Mounted the other way round, the
# catch-all swallows /recordings/thumbnail and rejects its ?token= auth.
app.include_router(timeline_router, prefix="/api", dependencies=_auth_dep_flex)
app.include_router(recordings_router, prefix="/api", dependencies=_auth_dep)
app.include_router(events_router, prefix="/api", dependencies=_auth_dep_flex)
# events_ws_router carries only /events/ws. Mounted WITHOUT the auth
# dependency above: HTTPBearer (behind get_current_user_flexible) requires an
# HTTP Request, which doesn't exist when FastAPI solves dependencies for a
# WebSocket route, so applying it here would crash every connection attempt.
# The route authenticates itself via a ?token= query param instead.
app.include_router(events_ws_router, prefix="/api")
app.include_router(settings_router, prefix="/api", dependencies=_auth_dep)
app.include_router(persons_router, prefix="/api")
app.include_router(users_router, prefix="/api", dependencies=_auth_dep)
app.include_router(audit_router, prefix="/api", dependencies=_auth_dep)
app.include_router(ptz_router, prefix="/api", dependencies=_auth_dep)
app.include_router(heatmap_router, prefix="/api", dependencies=_auth_dep_flex)
app.include_router(webpush_router, prefix="/api", dependencies=_auth_dep)
app.include_router(cloud_backup_router, prefix="/api", dependencies=_auth_dep)
# go2rtc reverse proxy — auth is enforced inside the router (Bearer for HTTP,
# ?token= for WebSocket).  Mount at /api/go2rtc so the browser never needs to
# know about the internal go2rtc port.
app.include_router(proxy_router, prefix="/api")

# Convenience alias: /api/health → public liveness ping (no auth required).
from sentinel.api.system import health
app.add_api_route("/api/health", health, methods=["GET"], tags=["system"])

# --- Static frontend --------------------------------------------------------
# Mount real static assets (js/css/icons/manifest/sw.js) under /assets etc.
# These are served directly by StaticFiles; missing paths in those directories
# return a real 404 from StaticFiles (not the SPA fallback).
#
# We do NOT use html=True on the root mount because that causes FastAPI's
# StaticFiles to return index.html for *every* unknown path — including
# unknown /api/* paths — making API 404s invisible.
#
# Instead we register a catch-all GET route that:
#   1. Returns index.html for any non-/api path (SPA fallback).
#   2. Returns a JSON 404 for any /api/* path that wasn't matched earlier.

_INDEX_HTML = FRONTEND_DIST / "index.html"

if FRONTEND_DIST.exists():
    # Serve static asset sub-trees directly.
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")
    if (FRONTEND_DIST / "icons").exists():
        app.mount("/icons", StaticFiles(directory=str(FRONTEND_DIST / "icons")), name="icons")

    log.info("serving frontend from %s", FRONTEND_DIST)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_fallback(request: Request, full_path: str):
        """
        SPA fallback:
        - /api/* paths that reached here are unknown API routes → JSON 404.
        - Known static files (manifest.json, sw.js, robots.txt …) are served
          directly from FRONTEND_DIST if they exist on disk.
        - Everything else → index.html so React Router can handle the URL.
        """
        # Reject unknown API paths with a JSON 404 so clients get proper errors.
        if full_path.startswith("api/") or full_path == "api":
            return JSONResponse(
                {"detail": "Not Found"},
                status_code=404,
            )

        # Serve real static files that exist in the dist root directly.
        candidate = FRONTEND_DIST / full_path
        try:
            resolved = candidate.resolve()
            # Must stay inside FRONTEND_DIST (path-traversal guard).
            resolved.relative_to(FRONTEND_DIST.resolve())
            if resolved.is_file():
                return FileResponse(str(resolved))
        except (ValueError, OSError):
            pass

        # Fall back to index.html for all other GET requests (client-side routes).
        # no-cache so browsers always revalidate index.html and pick up new JS hashes.
        if _INDEX_HTML.exists():
            return FileResponse(str(_INDEX_HTML), headers={"Cache-Control": "no-cache"})
        return JSONResponse(
            {
                "message": "Argus NVR API is running",
                "docs": "/docs",
                "health": "/api/health",
                "note": "Frontend not built yet — run 'npm run build' in frontend/",
            }
        )

else:
    @app.get("/{full_path:path}", include_in_schema=False)
    async def _no_frontend(request: Request, full_path: str):
        if full_path.startswith("api/") or full_path == "api":
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return JSONResponse(
            {
                "message": "Argus NVR API is running",
                "docs": "/docs",
                "health": "/api/health",
                "note": "Frontend not built yet — run 'npm run build' in frontend/",
            }
        )


def run() -> None:
    """Console-script entrypoint for pyproject.toml packaging."""
    import uvicorn
    from pathlib import Path as _Path

    uvicorn_kwargs: dict = {
        "host": settings.sentinel_host,
        "port": settings.sentinel_port,
        "reload": False,
    }

    # Check HTTPS toggle in settings DB.
    try:
        from sentinel.db import get_db as _get_db
        _db = _get_db()
        _row = _db.execute("SELECT value FROM settings WHERE key='https_enabled'").fetchone()
        https_on = bool(_row and _row["value"] == "1")
    except Exception:
        https_on = False

    if https_on:
        try:
            from sentinel.util.ssl_cert import ensure_self_signed_cert
            cert_dir = settings.sentinel_data_root / "ssl"
            cert_path = cert_dir / "sentinel.crt"
            key_path  = cert_dir / "sentinel.key"
            try:
                _db = _get_db()
                _cp = _db.execute("SELECT value FROM settings WHERE key='ssl_cert_path'").fetchone()
                _kp = _db.execute("SELECT value FROM settings WHERE key='ssl_key_path'").fetchone()
                if _cp and _cp["value"]:
                    cert_path = _Path(_cp["value"])
                if _kp and _kp["value"]:
                    key_path = _Path(_kp["value"])
            except Exception:
                pass
            if not cert_path.exists():
                ensure_self_signed_cert(cert_path, key_path)
            uvicorn_kwargs["ssl_certfile"] = str(cert_path)
            uvicorn_kwargs["ssl_keyfile"]  = str(key_path)
            log.info("HTTPS enabled — cert=%s", cert_path)
        except Exception as exc:
            log.error("HTTPS setup failed, falling back to HTTP: %s", exc)

    uvicorn.run("sentinel.main:app", **uvicorn_kwargs)
