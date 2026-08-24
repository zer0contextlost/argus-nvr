"""
tests/test_fixes.py — Tests for the four post-deploy bug fixes.

Covers:
  1. go2rtc ffmpeg-bin config — render_go2rtc_config() includes ffmpeg.bin
  2. go2rtc reload on camera change — _apply_camera_changes() calls reload_go2rtc
     and reconciles recorders/detectors (add/remove).
  3. go2rtc proxy — /api/go2rtc/{path} proxies HTTP to go2rtc;
     /api/live returns a relative go2rtc_api base ("/api/go2rtc").
  4. SENTINEL_HOST default — config default is "0.0.0.0".
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

os.environ.setdefault("SENTINEL_SECRET_KEY", "test-secret-key-for-unit-tests-32x")


# ===========================================================================
# 1. go2rtc ffmpeg-bin in rendered config
# ===========================================================================

class TestGo2rtcFfmpegBin:
    """render_go2rtc_config() must include a top-level 'ffmpeg.bin' entry."""

    def _make_db(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE cameras (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                host TEXT NOT NULL DEFAULT '192.168.1.1',
                rtsp_port INTEGER NOT NULL DEFAULT 554,
                rtsp_user TEXT NOT NULL DEFAULT 'admin',
                rtsp_pass_enc TEXT NOT NULL DEFAULT '',
                main_path TEXT NOT NULL DEFAULT '/Streaming/Channels/101',
                sub_path  TEXT NOT NULL DEFAULT '/Streaming/Channels/102',
                channel   INTEGER NOT NULL DEFAULT 1,
                record_mode TEXT NOT NULL DEFAULT 'continuous',
                enabled   INTEGER NOT NULL DEFAULT 1,
                detect_config_json TEXT NOT NULL DEFAULT '{}'
            );
        """)
        conn.commit()
        return conn

    def test_ffmpeg_bin_present_in_config(self):
        """render_go2rtc_config must include ffmpeg.bin pointing at ffmpeg_path."""
        import sentinel.db as db_module
        from sentinel.services.go2rtc import render_go2rtc_config
        from sentinel.config import settings

        db = self._make_db()
        orig = db_module.get_db
        db_module.get_db = lambda: db
        try:
            cfg = render_go2rtc_config()
        finally:
            db_module.get_db = orig

        assert "ffmpeg" in cfg, "top-level 'ffmpeg' key missing from go2rtc config"
        assert "bin" in cfg["ffmpeg"], "'bin' key missing from ffmpeg section"
        assert str(settings.ffmpeg_path) in cfg["ffmpeg"]["bin"] or \
               cfg["ffmpeg"]["bin"] == str(settings.ffmpeg_path), \
               f"ffmpeg.bin={cfg['ffmpeg']['bin']!r} does not match ffmpeg_path={settings.ffmpeg_path}"

    def test_ffmpeg_bin_written_to_yaml(self, tmp_path):
        """write_go2rtc_config() must write ffmpeg.bin to the YAML file."""
        import yaml
        import sentinel.db as db_module
        from sentinel.services.go2rtc import write_go2rtc_config
        from sentinel.config import settings

        db = self._make_db()
        orig_get_db = db_module.get_db
        db_module.get_db = lambda: db
        orig_path = settings.go2rtc_config_path
        fake_path = tmp_path / "go2rtc_test.yaml"
        try:
            with patch.object(type(settings), "go2rtc_config_path",
                              new_callable=lambda: property(lambda self: fake_path)):
                write_go2rtc_config()
        finally:
            db_module.get_db = orig_get_db

        assert fake_path.exists(), "YAML file was not written"
        with open(fake_path) as f:
            data = yaml.safe_load(f)
        assert "ffmpeg" in data, "ffmpeg section missing from written YAML"
        assert "bin" in data["ffmpeg"], "bin key missing from ffmpeg section in YAML"

    def test_streams_still_present(self):
        """render_go2rtc_config still generates the streams section correctly."""
        import sentinel.db as db_module
        from sentinel.services.go2rtc import render_go2rtc_config
        from sentinel.util.crypto import encrypt_password

        db = self._make_db()
        db.execute(
            "INSERT INTO cameras (name, host, rtsp_user, rtsp_pass_enc) "
            "VALUES ('TestCam', '192.168.1.10', 'admin', ?)",
            (encrypt_password("pass123"),),
        )
        db.commit()

        orig = db_module.get_db
        db_module.get_db = lambda: db
        try:
            cfg = render_go2rtc_config()
        finally:
            db_module.get_db = orig

        assert "streams" in cfg
        assert len(cfg["streams"]) >= 2  # main + sub


# ===========================================================================
# 2. Camera change reconciliation
# ===========================================================================

class TestCameraChangeReconcile:
    """_apply_camera_changes() must reload go2rtc and reconcile workers."""

    def _make_db(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE cameras (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                host TEXT NOT NULL DEFAULT '127.0.0.1',
                rtsp_port INTEGER NOT NULL DEFAULT 554,
                rtsp_user TEXT NOT NULL DEFAULT '',
                rtsp_pass_enc TEXT NOT NULL DEFAULT '',
                main_path TEXT NOT NULL DEFAULT '/Streaming/Channels/101',
                sub_path  TEXT NOT NULL DEFAULT '/Streaming/Channels/102',
                channel   INTEGER NOT NULL DEFAULT 1,
                record_mode TEXT NOT NULL DEFAULT 'continuous',
                enabled   INTEGER NOT NULL DEFAULT 1,
                detect_config_json TEXT NOT NULL DEFAULT '{}'
            );
        """)
        conn.commit()
        return conn

    def test_reload_go2rtc_called_on_add(self):
        """_apply_camera_changes with enabled camera calls reload_go2rtc."""
        import sentinel.db as db_module
        from sentinel.api.cameras import _apply_camera_changes

        db = self._make_db()
        cam_id = db.execute(
            "INSERT INTO cameras (name, host, enabled) VALUES ('NewCam', '127.0.0.1', 1)"
        ).lastrowid
        db.commit()

        reload_called = []
        add_recorder_called = []
        add_detector_called = []

        # Temporarily replace sentinel.db._db so the module-level get_db()
        # in sentinel.api.cameras returns our test DB.
        import sentinel.db as _db_mod
        orig_inner_db = _db_mod._db
        _db_mod._db = db
        try:
            with patch("sentinel.api.cameras.reload_go2rtc", side_effect=lambda: reload_called.append(1)):
                with patch("sentinel.api.cameras._add_recorder",
                           side_effect=lambda **kw: add_recorder_called.append(kw)):
                    with patch("sentinel.api.cameras._add_detector",
                               side_effect=lambda **kw: add_detector_called.append(kw)):
                        _apply_camera_changes(camera_id=cam_id)
        finally:
            _db_mod._db = orig_inner_db

        assert len(reload_called) == 1, "reload_go2rtc not called"
        assert len(add_recorder_called) == 1, "add_recorder not called for new camera"
        assert len(add_detector_called) == 1, "add_detector not called for new camera"

    def test_remove_recorder_detector_on_delete(self):
        """_apply_camera_changes(deleted=True) removes recorder and detector."""
        from sentinel.api.cameras import _apply_camera_changes

        remove_recorder_called = []
        remove_detector_called = []

        with patch("sentinel.api.cameras.reload_go2rtc"):
            with patch("sentinel.api.cameras._remove_recorder",
                       side_effect=lambda cam_id: remove_recorder_called.append(cam_id)):
                with patch("sentinel.api.cameras._remove_detector",
                           side_effect=lambda cam_id: remove_detector_called.append(cam_id)):
                    _apply_camera_changes(camera_id=42, deleted=True)

        assert 42 in remove_recorder_called, "remove_recorder not called with deleted camera_id"
        assert 42 in remove_detector_called, "remove_detector not called with deleted camera_id"

    def test_disabled_camera_removes_workers(self):
        """When a camera is disabled, _apply_camera_changes stops its workers."""
        from sentinel.api.cameras import _apply_camera_changes

        db = self._make_db()
        cam_id = db.execute(
            "INSERT INTO cameras (name, host, enabled) VALUES ('DisabledCam', '127.0.0.1', 0)"
        ).lastrowid
        db.commit()

        remove_recorder_called = []
        remove_detector_called = []

        # Temporarily set sentinel.db._db so the original get_db() returns our test DB.
        import sentinel.db as _db_mod
        orig_inner_db = _db_mod._db
        _db_mod._db = db
        try:
            with patch("sentinel.api.cameras.reload_go2rtc"):
                with patch("sentinel.api.cameras._remove_recorder",
                           side_effect=lambda cid: remove_recorder_called.append(cid)):
                    with patch("sentinel.api.cameras._remove_detector",
                               side_effect=lambda cid: remove_detector_called.append(cid)):
                        _apply_camera_changes(camera_id=cam_id)
        finally:
            _db_mod._db = orig_inner_db

        assert cam_id in remove_recorder_called, "remove_recorder not called for disabled camera"
        assert cam_id in remove_detector_called, "remove_detector not called for disabled camera"

    def test_reload_go2rtc_called_on_delete(self):
        """reload_go2rtc is called even when camera_id is None (config-only change)."""
        from sentinel.api.cameras import _apply_camera_changes

        reload_called = []
        with patch("sentinel.api.cameras.reload_go2rtc",
                   side_effect=lambda: reload_called.append(1)):
            _apply_camera_changes(camera_id=None)

        assert len(reload_called) == 1, "reload_go2rtc must be called regardless of camera_id"


# ===========================================================================
# 3. go2rtc proxy routes + live.py relative base
# ===========================================================================

class TestGo2rtcProxy:
    """The /api/go2rtc proxy routes forward requests to go2rtc's internal API."""

    def _build_app(self, conn):
        """Build a minimal test FastAPI app with the proxy router mounted."""
        from fastapi import FastAPI, Depends
        from fastapi.middleware.cors import CORSMiddleware
        from sentinel.api.auth import router as auth_router
        from sentinel.api.live import router as live_router
        from sentinel.api.proxy import router as proxy_router
        from sentinel.util.auth import get_current_user, create_access_token
        from sentinel.api.system import health

        app = FastAPI()
        app.add_middleware(CORSMiddleware, allow_origins=["*"],
                           allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
        app.include_router(auth_router, prefix="/api")
        _dep = [Depends(get_current_user)]
        app.include_router(live_router, prefix="/api", dependencies=_dep)
        app.include_router(proxy_router, prefix="/api", dependencies=[])
        app.add_api_route("/api/health", health, methods=["GET"], tags=["system"])
        return app

    def _make_conn(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE cameras (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL DEFAULT 'TestCam',
                host TEXT NOT NULL DEFAULT '127.0.0.1',
                rtsp_port INTEGER NOT NULL DEFAULT 554,
                rtsp_user TEXT NOT NULL DEFAULT '',
                rtsp_pass_enc TEXT NOT NULL DEFAULT '',
                main_path TEXT NOT NULL DEFAULT '/Streaming/Channels/101',
                sub_path TEXT NOT NULL DEFAULT '/Streaming/Channels/102',
                channel INTEGER NOT NULL DEFAULT 1,
                record_mode TEXT NOT NULL DEFAULT 'continuous',
                enabled INTEGER NOT NULL DEFAULT 1,
                detect_config_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE recordings (id INTEGER PRIMARY KEY, camera_id INTEGER,
                path TEXT, started_at TEXT, ended_at TEXT, duration_s REAL,
                size_bytes INTEGER, has_gap INTEGER DEFAULT 0);
            CREATE TABLE events (id INTEGER PRIMARY KEY, camera_id INTEGER,
                type TEXT DEFAULT 'motion', label TEXT, score REAL,
                started_at TEXT, ended_at TEXT, thumb_path TEXT,
                clip_path TEXT, recording_id INTEGER, meta_json TEXT DEFAULT '{}');
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                pass_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE notifications (id INTEGER PRIMARY KEY, event_id INTEGER,
                channel TEXT, status TEXT DEFAULT 'pending', sent_at TEXT);
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT);
        """)
        conn.commit()
        return conn

    def _run_with_db(self, conn, fn):
        """Run fn(client, token) with get_db patched at every relevant module.

        IMPORTANT: sentinel.api.live (and similar modules) bind `get_db` at
        import time via `from sentinel.db import get_db`. The patch must be done
        AFTER the module is already imported so `patch.stop()` restores the real
        function. We therefore pre-import the target modules, then patch them.
        """
        import sentinel.db as db_module
        # Pre-import so their module-level get_db binding is the original function.
        import sentinel.api.live  # noqa: F401
        import sentinel.api.auth  # noqa: F401
        from sentinel.util.auth import create_access_token
        from fastapi.testclient import TestClient
        from unittest.mock import patch as _patch

        # Patch the module-level binding in sentinel.api.live with our test conn.
        with _patch("sentinel.api.live.get_db", return_value=conn):
            # Override the sentinel.db singleton AND function so any late-binding
            # or direct module-level access also returns our test conn.
            orig_inner_db = db_module._db
            orig_get_db_fn = db_module.get_db
            db_module._db = conn
            db_module.get_db = lambda: conn
            try:
                app = self._build_app(conn)
                token = create_access_token({"sub": "1", "username": "admin", "role": "admin"})
                client = TestClient(app)
                result = fn(client, token)
            finally:
                # Restore original DB singleton and function reference.
                db_module._db = orig_inner_db
                db_module.get_db = orig_get_db_fn
        return result

    def test_live_returns_relative_go2rtc_base(self):
        """GET /api/live must return go2rtc_api as a relative path."""
        from sentinel.util.auth import hash_password

        conn = self._make_conn()
        conn.execute("INSERT INTO cameras (name, host, enabled) VALUES ('Cam1', '127.0.0.1', 1)")
        conn.execute(
            "INSERT INTO users (username, pass_hash, role) VALUES (?, ?, 'admin')",
            ("admin", hash_password("pw")),
        )
        conn.commit()

        def _do(client, token):
            return client.get("/api/live", headers={"Authorization": f"Bearer {token}"})

        r = self._run_with_db(conn, _do)
        conn.close()

        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        streams = r.json()
        assert len(streams) >= 1
        for s in streams:
            base = s["go2rtc_api"]
            assert base.startswith("/"), \
                f"go2rtc_api must be a relative path starting with '/', got {base!r}"
            assert "go2rtc" in base, \
                f"go2rtc_api must reference the proxy path, got {base!r}"
            assert s["webrtc_url"].startswith("/"), \
                f"webrtc_url must be relative, got {s['webrtc_url']!r}"
            assert s["mse_url"].startswith("/"), \
                f"mse_url must be relative, got {s['mse_url']!r}"

    def test_proxy_route_forwards_to_go2rtc(self):
        """GET /api/go2rtc/api/streams must proxy to go2rtc and return its response."""
        from sentinel.util.auth import hash_password

        conn = self._make_conn()
        conn.execute(
            "INSERT INTO users (username, pass_hash, role) VALUES (?, ?, 'admin')",
            ("admin", hash_password("pw")),
        )
        conn.commit()

        async def _aiter_bytes(chunk_size=65536):
            yield b'{"streams":{"cam1":{}}}'

        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.headers = {"content-type": "application/json"}
        fake_response.aiter_bytes = _aiter_bytes
        fake_response.aclose = AsyncMock()

        def _do(client, token):
            with patch("sentinel.api.proxy._client") as mock_client:
                mock_instance = MagicMock()
                mock_instance.build_request = MagicMock(return_value=MagicMock())
                mock_instance.send = AsyncMock(return_value=fake_response)
                mock_client.return_value = mock_instance
                return client.get(
                    "/api/go2rtc/api/streams",
                    headers={"Authorization": f"Bearer {token}"},
                )

        r = self._run_with_db(conn, _do)
        conn.close()

        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"

    def test_proxy_route_requires_auth(self):
        """GET /api/go2rtc/api/streams without token must return 401."""
        conn = self._make_conn()

        def _do(client, token):
            return client.get("/api/go2rtc/api/streams")  # no auth header

        r = self._run_with_db(conn, _do)
        conn.close()

        assert r.status_code == 401, \
            f"go2rtc proxy must require auth, got {r.status_code}"

    def test_proxy_webrtc_url_uses_relative_base(self):
        """webrtc_url and mse_url in /api/live response must be relative paths."""
        from sentinel.util.auth import hash_password
        from sentinel.api.live import GO2RTC_PROXY_BASE

        conn = self._make_conn()
        conn.execute("INSERT INTO cameras (name, host, enabled) VALUES ('FrontDoor', '10.0.0.1', 1)")
        conn.execute(
            "INSERT INTO users (username, pass_hash, role) VALUES (?, ?, 'admin')",
            ("admin", hash_password("pw")),
        )
        conn.commit()

        def _do(client, token):
            return client.get("/api/live", headers={"Authorization": f"Bearer {token}"})

        r = self._run_with_db(conn, _do)
        conn.close()

        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        streams = r.json()
        assert streams, "Expected at least one stream entry"
        s = streams[0]
        assert s["go2rtc_api"] == GO2RTC_PROXY_BASE
        assert s["webrtc_url"].startswith(GO2RTC_PROXY_BASE)
        assert s["mse_url"].startswith(GO2RTC_PROXY_BASE)


# ===========================================================================
# 4. Config defaults
# ===========================================================================

class TestConfigDefaults:
    """sentinel_host default must be 0.0.0.0 for LAN access."""

    def test_default_host_is_all_interfaces(self):
        """SENTINEL_HOST default must be 0.0.0.0, not 127.0.0.1."""
        from sentinel.config import Settings
        # Instantiate without reading any .env to test the bare default.
        with patch.dict(os.environ, {}, clear=False):
            s = Settings(_env_file=None)  # type: ignore[call-arg]
        # The class-level default (before any env file) must be 0.0.0.0.
        assert s.sentinel_host == "0.0.0.0", \
            f"Default SENTINEL_HOST must be '0.0.0.0', got {s.sentinel_host!r}"

    def test_sentinel_host_overrideable(self):
        """SENTINEL_HOST env var must override the default."""
        from sentinel.config import Settings
        with patch.dict(os.environ, {"SENTINEL_HOST": "127.0.0.1"}):
            s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.sentinel_host == "127.0.0.1"


# ===========================================================================
# 5. reload_go2rtc helper
# ===========================================================================

class TestReloadGo2rtc:
    """reload_go2rtc() must write config and restart the supervisor."""

    def test_reload_writes_config_and_restarts(self):
        """reload_go2rtc calls write_go2rtc_config then supervisor.restart."""
        import sentinel.services.go2rtc as g2r

        mock_supervisor = MagicMock()
        written = []

        with patch.object(g2r, "_supervisor", mock_supervisor):
            with patch("sentinel.services.go2rtc.write_go2rtc_config",
                       side_effect=lambda **kw: written.append(1)):
                g2r.reload_go2rtc()

        assert len(written) == 1, "write_go2rtc_config must be called by reload_go2rtc"
        mock_supervisor.restart.assert_called_once()

    def test_reload_no_supervisor_is_safe(self):
        """reload_go2rtc logs a warning when no supervisor is running (no crash)."""
        import sentinel.services.go2rtc as g2r

        with patch.object(g2r, "_supervisor", None):
            with patch("sentinel.services.go2rtc.write_go2rtc_config"):
                # Should not raise
                g2r.reload_go2rtc()
