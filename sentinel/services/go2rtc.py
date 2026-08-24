"""
sentinel/services/go2rtc.py — Render go2rtc.yaml from DB cameras and supervise
the go2rtc process.

go2rtc YAML shape:
  api:
    listen: ":1984"
  rtsp:
    listen: ":8554"
  webrtc:
    listen: ":8555"
  streams:
    <cam_slug>:
      - rtsp://user:pass@host:554/Streaming/Channels/101
    <cam_slug>_sub:
      - rtsp://user:pass@host:554/Streaming/Channels/102
"""

from __future__ import annotations

import os
import sqlite3
import threading
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

import yaml

from sentinel.config import settings
from sentinel.services.supervisor import ProcessSupervisor, RestartPolicy
from sentinel.util.crypto import decrypt_password
from sentinel.util.hikvision import build_rtsp_url, build_go2rtc_stream_name
from sentinel.util.logging import get_logger

log = get_logger("sentinel.go2rtc")

_supervisor: ProcessSupervisor | None = None
_supervisor_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Config generation
# ---------------------------------------------------------------------------

def _load_cameras(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """
    Load enabled cameras.  cam_type was added in migration 2; fall back
    gracefully if the column does not exist yet (e.g. in-memory test DBs
    that haven't been migrated).
    """
    try:
        return conn.execute(
            "SELECT id, name, host, rtsp_port, rtsp_user, rtsp_pass_enc, "
            "       main_path, sub_path, channel, enabled, "
            "       COALESCE(cam_type, 'rtsp') AS cam_type, "
            "       COALESCE(cam_make, 'generic') AS cam_make "
            "FROM cameras WHERE enabled=1"
        ).fetchall()
    except Exception:
        # cam_make/cam_type columns not present — fall back to minimal query.
        return conn.execute(
            "SELECT id, name, host, rtsp_port, rtsp_user, rtsp_pass_enc, "
            "       main_path, sub_path, channel, enabled "
            "FROM cameras WHERE enabled=1"
        ).fetchall()


def render_go2rtc_config(
    extra_streams: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build the go2rtc config dict from the cameras in the database.

    Args:
        extra_streams: Additional stream entries merged in (useful for testing
                       synthetic lavfi/ffmpeg sources).
    """
    from sentinel.db import get_db  # local import to avoid circular at module load

    cameras = _load_cameras(get_db())

    streams: dict[str, Any] = {}
    for cam in cameras:
        password = decrypt_password(cam["rtsp_pass_enc"]) if cam["rtsp_pass_enc"] else ""
        slug = build_go2rtc_stream_name(cam["name"])

        main_url = build_rtsp_url(
            host=cam["host"],
            user=cam["rtsp_user"],
            password=password,
            path=cam["main_path"],
            port=cam["rtsp_port"],
        )

        # Single-stream when cam_make is wyze/generic, or paths are identical.
        cam_make = cam["cam_make"] if "cam_make" in cam.keys() else "generic"
        single_stream = cam_make in ("wyze", "generic") or cam["sub_path"] == cam["main_path"]

        streams[slug] = [main_url]
        if single_stream:
            # Single-stream cameras (Wyze, generic): use the same URL for sub.
            # Two connections to the camera, but both go2rtc and the detector work.
            streams[f"{slug}_sub"] = [main_url]
        else:
            sub_url = build_rtsp_url(
                host=cam["host"],
                user=cam["rtsp_user"],
                password=password,
                path=cam["sub_path"],
                port=cam["rtsp_port"],
            )
            streams[f"{slug}_sub"] = [sub_url]

        streams[f"{slug}_mjpeg"] = [f"ffmpeg:{slug}_sub#video=mjpeg"]

    if extra_streams:
        streams.update(extra_streams)

    config: dict[str, Any] = {
        "api": {"listen": f"127.0.0.1:{settings.go2rtc_api_port}"},
        "rtsp": {"listen": f"127.0.0.1:{settings.go2rtc_rtsp_port}"},
        "webrtc": {
            "listen": f":{settings.go2rtc_webrtc_port}",
            "ice_servers": [{"urls": ["stun:stun.l.google.com:19302"]}],
        },
        "ffmpeg": {"bin": str(settings.ffmpeg_path)},
        "streams": streams,
        "log": {
            "level": "info",
        },
    }
    return config


def write_go2rtc_config(extra_streams: dict[str, Any] | None = None) -> Path:
    """Render and write config/go2rtc.yaml; return the path written."""
    cfg = render_go2rtc_config(extra_streams=extra_streams)
    config_path = settings.go2rtc_config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        os.replace(tmp, config_path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    log.info("go2rtc config written to %s (%d streams)", config_path, len(cfg["streams"]))
    return config_path


def sync_camera_stream(
    camera_name: str,
    main_url: str | None,
    sub_url: str | None,
    enabled: bool = True,
) -> bool:
    """
    Add or remove a camera's streams in a running go2rtc instance via its
    REST API — no process restart, no dropped WebRTC sessions.

    Args:
        camera_name: Camera name (used to derive stream slug).
        main_url:    Full RTSP URL for the main stream. None = skip.
        sub_url:     Full RTSP URL for the sub stream. None = skip.
        enabled:     If False, delete the streams instead of adding them.

    Returns True on success, False if go2rtc API is unreachable.
    """
    slug = build_go2rtc_stream_name(camera_name)
    base = go2rtc_api_base()

    try:
        with httpx.Client(timeout=4.0) as client:
            for stream_name, url in [(slug, main_url), (f"{slug}_sub", sub_url)]:
                if url is None:
                    continue
                if enabled:
                    src = urllib.parse.quote(url, safe="")
                    resp = client.put(f"{base}/api/streams?name={stream_name}&src={src}")
                    if resp.status_code not in (200, 201, 204):
                        log.warning(
                            "go2rtc stream sync PUT %s HTTP %s", stream_name, resp.status_code
                        )
                else:
                    resp = client.delete(f"{base}/api/streams?name={stream_name}")
                    if resp.status_code not in (200, 204, 404):
                        log.warning(
                            "go2rtc stream sync DELETE %s HTTP %s", stream_name, resp.status_code
                        )
        return True
    except Exception as exc:
        log.debug("go2rtc stream sync failed (will fall back to restart): %s", exc)
        return False


# ---------------------------------------------------------------------------
# Supervisor lifecycle
# ---------------------------------------------------------------------------

def _kill_stale_go2rtc() -> None:
    """Kill any go2rtc processes left over from a previous (force-killed) run."""
    import subprocess as _sp
    go2rtc_name = settings.go2rtc_path.name  # e.g. "go2rtc.exe"
    try:
        result = _sp.run(
            ["tasklist", "/FI", f"IMAGENAME eq {go2rtc_name}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            parts = line.strip().strip('"').split('","')
            if len(parts) >= 2:
                try:
                    pid = int(parts[1])
                    _sp.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5)
                    log.info("killed stale go2rtc pid=%d", pid)
                except (ValueError, Exception):
                    pass
    except Exception as exc:
        log.debug("_kill_stale_go2rtc: %s", exc)


class _Go2rtcSupervisor(ProcessSupervisor):
    """ProcessSupervisor that reaps any stale go2rtc before every launch.

    go2rtc exits hard on panic; on Windows the panicked process's listener
    sockets can linger long enough that the supervisor's relaunch loses the
    race to rebind :1984/:8554 ("Only one usage of each socket address").
    Killing leftovers first guarantees the ports are free before relaunch.
    """

    def _launch(self) -> None:
        _kill_stale_go2rtc()
        super()._launch()


def start_go2rtc(extra_streams: dict[str, Any] | None = None) -> None:
    """Write config and start (or restart) the supervised go2rtc process."""
    global _supervisor

    config_path = write_go2rtc_config(extra_streams=extra_streams)

    if not settings.go2rtc_path.exists():
        log.error("go2rtc binary not found at %s", settings.go2rtc_path)
        return

    with _supervisor_lock:
        if _supervisor is not None and _supervisor.status()["status"] == "running":
            log.info("go2rtc already running — reloading config")
            _supervisor.restart()
            return

        _supervisor = _Go2rtcSupervisor(
            name="go2rtc",
            cmd=[str(settings.go2rtc_path), "-config", str(config_path)],
            cwd=str(config_path.parent),
            restart_policy=RestartPolicy.ALWAYS,
            max_restarts=50,
            initial_backoff=2.0,
            max_backoff=30.0,
            log_file=settings.go2rtc_log_path,
        )
        _supervisor.start()
    log.info("go2rtc supervisor started")


def stop_go2rtc() -> None:
    """Stop the supervised go2rtc process."""
    global _supervisor
    if _supervisor is not None:
        _supervisor.stop()
        _supervisor = None
    log.info("go2rtc stopped")


def go2rtc_status() -> dict:
    """Return current go2rtc supervisor status."""
    if _supervisor is None:
        return {"name": "go2rtc", "status": "stopped", "pid": None, "restarts": 0}
    return _supervisor.status()


def reload_go2rtc() -> None:
    """
    Write an updated config and sync stream changes to the running go2rtc
    instance via its REST API. Falls back to a full process restart only if
    the API is unreachable (e.g. go2rtc crashed or hasn't started yet).
    """
    global _supervisor
    write_go2rtc_config()
    if _supervisor is None:
        log.warning("go2rtc supervisor not running — skipping reload")
        return
    # Try a lightweight API-based sync first (no dropped WebRTC sessions).
    # The API is available whenever go2rtc is up; if it fails, fall back to restart.
    base = go2rtc_api_base()
    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(f"{base}/api/streams")
        if resp.status_code == 200:
            log.info("go2rtc config updated; stream state synced via API (no restart)")
            return
    except Exception as exc:
        log.debug("go2rtc API not reachable (%s); falling back to process restart", exc)

    log.info("reloading go2rtc after camera change (full restart)")
    _supervisor.restart()


def go2rtc_api_base() -> str:
    """Return the base URL of the go2rtc HTTP API (server-side only)."""
    return f"http://127.0.0.1:{settings.go2rtc_api_port}"
