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

import logging
import os
import signal
import sqlite3
import sys
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

# httpx logs every request URL at INFO. Our stream-sync PUTs carry the camera
# RTSP URL (with credentials) in the query string, so keep that out of the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

_supervisor: ProcessSupervisor | None = None
_supervisor_lock = threading.Lock()

# Stream set (name -> normalized source list) that the running go2rtc is known
# to be serving, i.e. what the last config write / runtime sync left it with.
# None until the first write; reload_go2rtc() then seeds it from the file on disk.
_applied_streams: dict[str, list[str]] | None = None
_reload_lock = threading.Lock()


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


class _IndentedListDumper(yaml.Dumper):
    """
    Indent list items under their key ("  name:\n    - src") instead of PyYAML's
    default flush style ("  name:\n  - src"). go2rtc patches this same file on
    every PUT /api/streams, and its patcher fails with "did not find expected
    key" (HTTP 400) when replacing an existing stream written in the flush
    style, which forced reload_go2rtc() back to a full restart every time.
    """

    def increase_indent(self, flow: bool = False, indentless: bool = False):
        return super().increase_indent(flow, False)


def _write_config_file(cfg: dict[str, Any]) -> Path:
    """Atomically write an already-rendered config dict to go2rtc.yaml."""
    config_path = settings.go2rtc_config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, Dumper=_IndentedListDumper,
                      default_flow_style=False, allow_unicode=True)
        os.replace(tmp, config_path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    log.info("go2rtc config written to %s (%d streams)", config_path, len(cfg["streams"]))
    return config_path


def write_go2rtc_config(extra_streams: dict[str, Any] | None = None) -> Path:
    """Render and write config/go2rtc.yaml; return the path written."""
    global _applied_streams
    cfg = render_go2rtc_config(extra_streams=extra_streams)
    path = _write_config_file(cfg)
    # The caller is about to (re)start go2rtc from this file, so the runtime
    # stream set will match it.
    _applied_streams = _normalize_streams(cfg["streams"])
    return path


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
    if sys.platform.startswith("win"):
        _kill_stale_go2rtc_windows()
    else:
        _kill_stale_go2rtc_posix()


def _kill_stale_go2rtc_posix() -> None:
    """
    Linux: scan /proc for processes whose executable is OUR configured go2rtc
    binary (by resolved path, not just name, so an unrelated go2rtc elsewhere
    on the box is never touched) and SIGKILL them.
    """
    try:
        target = os.path.realpath(str(settings.go2rtc_path))
        me = os.getpid()
        for entry in os.listdir("/proc"):
            if not entry.isdigit() or int(entry) == me:
                continue
            pid = int(entry)
            try:
                exe = os.readlink(f"/proc/{pid}/exe")
            except OSError:
                continue  # exited, kernel thread, or not ours to inspect
            # A binary replaced on disk while running shows as "<path> (deleted)".
            if exe.endswith(" (deleted)"):
                exe = exe[: -len(" (deleted)")]
            if os.path.realpath(exe) != target:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
                log.info("killed stale go2rtc pid=%d", pid)
            except OSError as exc:
                log.debug("_kill_stale_go2rtc: kill pid=%d: %s", pid, exc)
    except Exception as exc:
        log.debug("_kill_stale_go2rtc: %s", exc)


def _kill_stale_go2rtc_windows() -> None:
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


def _normalize_streams(streams: dict[str, Any] | None) -> dict[str, list[str]]:
    """Coerce a config `streams:` mapping to {name: [source, ...]} for comparison."""
    out: dict[str, list[str]] = {}
    for name, srcs in (streams or {}).items():
        if srcs is None:
            srcs = []
        elif isinstance(srcs, (str, bytes)):
            srcs = [srcs]
        out[str(name)] = [str(x) for x in srcs]
    return out


def diff_streams(
    old: dict[str, list[str]], new: dict[str, list[str]]
) -> tuple[list[str], list[str]]:
    """
    Return (upserts, removals): stream names that are new or whose source list
    changed, and names present in `old` but gone from `new`. Unchanged streams
    appear in neither list so their live sessions are never touched.
    """
    upserts = [n for n, srcs in new.items() if old.get(n) != srcs]
    removals = [n for n in old if n not in new]
    return upserts, removals


def _read_config_streams() -> dict[str, list[str]]:
    """Streams from the go2rtc.yaml currently on disk ({} if missing/unreadable)."""
    try:
        with open(settings.go2rtc_config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return _normalize_streams(data.get("streams"))
    except Exception as exc:
        log.debug("could not read existing go2rtc config: %s", exc)
        return {}


def _apply_stream_diff(upserts: dict[str, list[str]], removals: list[str]) -> None:
    """
    Push stream changes into the RUNNING go2rtc via its HTTP API (go2rtc 1.9.x,
    internal/streams/api.go). Raises on any failure so the caller can fall back
    to a full restart.

      PUT    /api/streams?name=<stream>&src=<url>[&src=<url>...]
             Creates the stream, or replaces it if the name exists (the whole
             source list is replaced). Repeat `src` for multiple sources.
      DELETE /api/streams?src=<stream>
             NB: the stream NAME goes in `src` here; `name` is ignored.

    Both also patch go2rtc's own config file (a no-op in effect, since we have
    already written the same content to it).
    """
    base = go2rtc_api_base()
    with httpx.Client(timeout=4.0) as client:
        for name in removals:
            resp = client.delete(f"{base}/api/streams", params={"src": name})
            if resp.status_code not in (200, 204):
                raise RuntimeError(f"DELETE {name} HTTP {resp.status_code}")
        for name, srcs in upserts.items():
            if not srcs:
                # An empty source list can't be expressed via PUT (no `src`
                # would return the stream listing); drop the stream instead.
                resp = client.delete(f"{base}/api/streams", params={"src": name})
            else:
                resp = client.put(
                    f"{base}/api/streams",
                    params=[("name", name)] + [("src", s) for s in srcs],
                )
            if resp.status_code not in (200, 204):
                raise RuntimeError(f"PUT {name} HTTP {resp.status_code}")


def reload_go2rtc() -> None:
    """
    Write an updated config and bring the running go2rtc in line with it,
    without restarting the process when possible.

    go2rtc.yaml is rewritten in full (so any later restart is consistent),
    then the old and new stream sets are diffed and ONLY added/changed/removed
    streams (including derived <slug>_sub / <slug>_mjpeg ones) are pushed to
    the live process through PUT/DELETE /api/streams. Editing camera A
    therefore never disturbs viewers of camera B. If any API call fails we fall
    back to the old behavior, a full supervisor restart.

    History: an earlier "lightweight sync" here only did a GET /api/streams
    health check and never pushed anything into go2rtc's runtime, so added or
    edited cameras silently never appeared. The sync below really mutates the
    runtime stream table (PUT/DELETE), so don't reduce it to a GET again.

    Blocking (HTTP calls; the fallback restart waits up to ~5s). Async callers
    must run this in an executor.
    """
    global _applied_streams
    with _reload_lock:
        cfg = render_go2rtc_config()
        new = _normalize_streams(cfg["streams"])
        old = _applied_streams if _applied_streams is not None else _read_config_streams()
        _write_config_file(cfg)
        _applied_streams = new

        if _supervisor is None:
            log.warning("go2rtc supervisor not running — skipping reload")
            return

        upserts, removals = diff_streams(old, new)
        if not upserts and not removals:
            log.info("go2rtc streams unchanged — nothing to sync")
            return

        try:
            _apply_stream_diff({n: new[n] for n in upserts}, removals)
            log.info(
                "go2rtc streams synced live (updated=%s removed=%s)", upserts, removals
            )
            return
        except Exception as exc:
            log.warning("go2rtc live stream sync failed (%s) — falling back to full restart", exc)

        log.info("reloading go2rtc after camera change (full restart)")
        _supervisor.restart()


def go2rtc_api_base() -> str:
    """Return the base URL of the go2rtc HTTP API (server-side only)."""
    return f"http://127.0.0.1:{settings.go2rtc_api_port}"
