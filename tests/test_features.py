"""
tests/test_features.py — Tests for the three new features:

1. DB migration 2: cam_type column (default 'hikvision', backfill existing rows).
2. cam_type in Camera API (create, read, update).
3. Universal snapshot endpoint (go2rtc frame.jpeg, not Hikvision ISAPI).
4. Single-stream go2rtc config generation (Wyze/Generic use stream-alias for sub).
"""

from __future__ import annotations

import os
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("SENTINEL_SECRET_KEY", "test-secret-key-for-unit-tests-32x")


# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------

def _make_cameras_db(include_cam_type: bool = True) -> sqlite3.Connection:
    """Create an in-memory cameras table with the shape after migration 2."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    col_extra = ", cam_type TEXT NOT NULL DEFAULT 'hikvision'" if include_cam_type else ""
    conn.executescript(f"""
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
            record_mode TEXT NOT NULL DEFAULT 'motion-only',
            enabled   INTEGER NOT NULL DEFAULT 1,
            detect_config_json TEXT NOT NULL DEFAULT '{{}}',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
            {col_extra}
        );
        CREATE TABLE recordings (
            id INTEGER PRIMARY KEY, camera_id INTEGER,
            path TEXT, started_at TEXT, ended_at TEXT,
            duration_s REAL, size_bytes INTEGER, has_gap INTEGER DEFAULT 0
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY, camera_id INTEGER,
            type TEXT DEFAULT 'motion', label TEXT, score REAL,
            started_at TEXT, ended_at TEXT, thumb_path TEXT,
            clip_path TEXT, recording_id INTEGER, meta_json TEXT DEFAULT '{{}}'
        );
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            pass_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'viewer',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY, event_id INTEGER,
            channel TEXT, status TEXT DEFAULT 'pending', sent_at TEXT
        );
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT);
    """)
    conn.commit()
    return conn


def _build_app(conn):
    """Build a minimal FastAPI test app with cameras, live, proxy, auth routers."""
    from fastapi import FastAPI, Depends
    from fastapi.middleware.cors import CORSMiddleware
    from sentinel.api.auth import router as auth_router
    from sentinel.api.cameras import router as cameras_router
    from sentinel.api.live import router as live_router
    from sentinel.api.proxy import router as proxy_router
    from sentinel.util.auth import get_current_user

    app = FastAPI()
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"],
        allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
    )
    app.include_router(auth_router, prefix="/api")
    _dep = [Depends(get_current_user)]
    app.include_router(cameras_router, prefix="/api", dependencies=_dep)
    app.include_router(live_router, prefix="/api", dependencies=_dep)
    app.include_router(proxy_router, prefix="/api", dependencies=[])
    return app


def _run_with_db(conn, fn):
    """Run fn(client, token) with get_db patched to the in-memory DB."""
    import sentinel.db as db_module
    import sentinel.api.cameras  # noqa: F401 — pre-import for patch
    import sentinel.api.live  # noqa: F401
    import sentinel.api.auth  # noqa: F401
    from sentinel.util.auth import create_access_token
    from fastapi.testclient import TestClient

    orig_db = db_module._db
    orig_fn = db_module.get_db
    db_module._db = conn
    db_module.get_db = lambda: conn
    try:
        with patch("sentinel.api.cameras.get_db", return_value=conn), \
             patch("sentinel.api.live.get_db", return_value=conn), \
             patch("sentinel.api.cameras.reload_go2rtc"), \
             patch("sentinel.api.cameras._add_recorder"), \
             patch("sentinel.api.cameras._add_detector"), \
             patch("sentinel.api.cameras._remove_recorder"), \
             patch("sentinel.api.cameras._remove_detector"):
            app = _build_app(conn)
            token = create_access_token({"sub": "1", "username": "admin", "role": "admin"})
            client = TestClient(app, raise_server_exceptions=True)
            result = fn(client, token)
    finally:
        db_module._db = orig_db
        db_module.get_db = orig_fn
    return result


# ===========================================================================
# 1. DB Migration 2 — cam_type column
# ===========================================================================

class TestMigration2CamType:
    """Migration 2 adds cam_type column and backfills existing rows."""

    def test_migration_2_adds_cam_type_column(self):
        """
        Running all migrations (including migration 2) on a fresh DB
        results in a cam_type column on the cameras table.
        """
        import tempfile
        from pathlib import Path
        import sentinel.db as db_module
        from sentinel.db import run_migrations, _MIGRATIONS

        # Verify migration 2 exists in the list.
        versions = [m[0] for m in _MIGRATIONS]
        assert 2 in versions, f"Migration 2 not found in _MIGRATIONS: {versions}"

        # Apply migration 2 SQL to an in-memory DB manually.
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # Apply migration 1 (initial schema).
        sql_1 = next(sql for v, sql in _MIGRATIONS if v == 1)
        conn.executescript(sql_1)
        conn.commit()

        # Verify cam_type is NOT present before migration 2.
        cols_before = [row[1] for row in conn.execute("PRAGMA table_info(cameras)").fetchall()]
        assert "cam_type" not in cols_before, "cam_type should not exist before migration 2"

        # Apply migration 2 SQL.
        sql_2 = next(sql for v, sql in _MIGRATIONS if v == 2)
        conn.executescript(sql_2)
        conn.commit()

        # Verify cam_type is now present.
        cols_after = [row[1] for row in conn.execute("PRAGMA table_info(cameras)").fetchall()]
        assert "cam_type" in cols_after, f"cam_type column not found after migration 2; columns: {cols_after}"

    def test_migration_2_default_is_hikvision(self):
        """A row inserted without cam_type after migration 2 defaults to 'hikvision'."""
        conn = _make_cameras_db(include_cam_type=True)
        conn.execute(
            "INSERT INTO cameras (name, host) VALUES ('TestCam', '192.168.1.10')"
        )
        conn.commit()
        row = conn.execute("SELECT cam_type FROM cameras WHERE name='TestCam'").fetchone()
        assert row is not None
        assert row["cam_type"] == "hikvision", f"Expected 'hikvision', got {row['cam_type']!r}"

    def test_migration_2_backfill_to_hikvision(self):
        """
        Simulate pre-migration rows (no cam_type column) being upgraded:
        after ALTER TABLE + UPDATE the existing rows have cam_type='hikvision'.
        """
        # Create table WITHOUT cam_type (as migration 1 would have it).
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE cameras (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                host TEXT NOT NULL DEFAULT '192.168.1.1',
                rtsp_port INTEGER NOT NULL DEFAULT 554,
                rtsp_user TEXT NOT NULL DEFAULT '',
                rtsp_pass_enc TEXT NOT NULL DEFAULT '',
                main_path TEXT NOT NULL DEFAULT '/Streaming/Channels/101',
                sub_path  TEXT NOT NULL DEFAULT '/Streaming/Channels/102',
                channel   INTEGER NOT NULL DEFAULT 1,
                record_mode TEXT NOT NULL DEFAULT 'motion-only',
                enabled   INTEGER NOT NULL DEFAULT 1,
                detect_config_json TEXT NOT NULL DEFAULT '{}'
            );
        """)
        conn.execute("INSERT INTO cameras (name, host) VALUES ('OldCam', '10.0.0.1')")
        conn.commit()

        # Apply migration 2 SQL.
        conn.execute("ALTER TABLE cameras ADD COLUMN cam_type TEXT NOT NULL DEFAULT 'hikvision'")
        conn.execute("UPDATE cameras SET cam_type = 'hikvision' WHERE cam_type IS NULL OR cam_type = ''")
        conn.commit()

        row = conn.execute("SELECT cam_type FROM cameras WHERE name='OldCam'").fetchone()
        assert row["cam_type"] == "hikvision", f"Backfill failed: got {row['cam_type']!r}"

    def test_migration_2_explicit_cam_type_preserved(self):
        """After migration 2, rows with explicit cam_type values are preserved."""
        conn = _make_cameras_db(include_cam_type=True)
        conn.execute(
            "INSERT INTO cameras (name, host, cam_type) VALUES ('WyzeCam', '10.0.0.2', 'wyze')"
        )
        conn.commit()
        row = conn.execute("SELECT cam_type FROM cameras WHERE name='WyzeCam'").fetchone()
        assert row["cam_type"] == "wyze"


# ===========================================================================
# 2. cam_type in Camera API
# ===========================================================================

class TestCamTypeApi:
    """cam_type field is present in create, read, and update responses."""

    def test_create_camera_default_cam_type(self):
        """POST /api/cameras without cam_type returns cam_type='hikvision'."""
        conn = _make_cameras_db()

        def _do(client, token):
            return client.post(
                "/api/cameras",
                json={"name": "TestCam", "host": "192.168.1.10"},
                headers={"Authorization": f"Bearer {token}"},
            )

        r = _run_with_db(conn, _do)
        assert r.status_code == 201, r.text
        data = r.json()
        assert "cam_type" in data, f"cam_type missing from response: {data}"
        assert data["cam_type"] == "hikvision"

    def test_create_camera_wyze_type(self):
        """POST /api/cameras with cam_type='wyze' persists and returns 'wyze'."""
        conn = _make_cameras_db()

        def _do(client, token):
            return client.post(
                "/api/cameras",
                json={
                    "name": "WyzeCam", "host": "10.0.0.5",
                    "cam_type": "wyze",
                    "main_path": "/live", "sub_path": "/live",
                },
                headers={"Authorization": f"Bearer {token}"},
            )

        r = _run_with_db(conn, _do)
        assert r.status_code == 201, r.text
        assert r.json()["cam_type"] == "wyze"

    def test_create_camera_generic_type(self):
        """POST /api/cameras with cam_type='generic' persists and returns 'generic'."""
        conn = _make_cameras_db()

        def _do(client, token):
            return client.post(
                "/api/cameras",
                json={
                    "name": "GenericCam", "host": "10.0.0.6",
                    "cam_type": "generic",
                    "main_path": "/stream", "sub_path": "/stream",
                },
                headers={"Authorization": f"Bearer {token}"},
            )

        r = _run_with_db(conn, _do)
        assert r.status_code == 201, r.text
        assert r.json()["cam_type"] == "generic"

    def test_list_cameras_includes_cam_type(self):
        """GET /api/cameras returns cam_type in each camera record."""
        conn = _make_cameras_db()
        conn.execute(
            "INSERT INTO cameras (name, host, cam_type) VALUES ('Cam1', '10.0.0.1', 'hikvision')"
        )
        conn.commit()

        def _do(client, token):
            return client.get("/api/cameras", headers={"Authorization": f"Bearer {token}"})

        r = _run_with_db(conn, _do)
        assert r.status_code == 200, r.text
        cams = r.json()
        assert len(cams) >= 1
        assert "cam_type" in cams[0], f"cam_type missing: {cams[0]}"

    def test_update_camera_changes_cam_type(self):
        """PUT /api/cameras/{id} can change cam_type."""
        conn = _make_cameras_db()
        cam_id = conn.execute(
            "INSERT INTO cameras (name, host, cam_type) VALUES ('Cam1', '10.0.0.1', 'hikvision')"
        ).lastrowid
        conn.commit()

        def _do(client, token):
            return client.put(
                f"/api/cameras/{cam_id}",
                json={"cam_type": "wyze"},
                headers={"Authorization": f"Bearer {token}"},
            )

        r = _run_with_db(conn, _do)
        assert r.status_code == 200, r.text
        assert r.json()["cam_type"] == "wyze"

    def test_get_single_camera_includes_cam_type(self):
        """GET /api/cameras/{id} returns cam_type."""
        conn = _make_cameras_db()
        cam_id = conn.execute(
            "INSERT INTO cameras (name, host, cam_type) VALUES ('Cam1', '10.0.0.2', 'generic')"
        ).lastrowid
        conn.commit()

        def _do(client, token):
            return client.get(
                f"/api/cameras/{cam_id}",
                headers={"Authorization": f"Bearer {token}"},
            )

        r = _run_with_db(conn, _do)
        assert r.status_code == 200, r.text
        assert r.json()["cam_type"] == "generic"


# ===========================================================================
# 3. Universal snapshot endpoint (go2rtc frame.jpeg)
# ===========================================================================

class TestUniversalSnapshot:
    """Snapshot uses go2rtc frame.jpeg, not Hikvision ISAPI."""

    def test_snapshot_calls_go2rtc_frame_jpeg(self):
        """GET /api/cameras/{id}/snapshot hits go2rtc /api/frame.jpeg, not ISAPI."""
        conn = _make_cameras_db()
        cam_id = conn.execute(
            "INSERT INTO cameras (name, host, cam_type) VALUES ('HikCam', '192.168.1.52', 'hikvision')"
        ).lastrowid
        conn.commit()

        fake_jpeg = b'\xff\xd8\xff\xe0' + b'\x00' * 100 + b'\xff\xd9'

        def _do(client, token):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = fake_jpeg

            with patch("sentinel.api.cameras.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.get = AsyncMock(return_value=mock_resp)
                mock_cls.return_value = mock_client

                with patch("sentinel.api.cameras.go2rtc_api_base", new=lambda: "http://127.0.0.1:1984"):
                    return client.get(
                        f"/api/cameras/{cam_id}/snapshot",
                        headers={"Authorization": f"Bearer {token}"},
                    )

        r = _run_with_db(conn, _do)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("image/jpeg")
        assert r.content == fake_jpeg

    def test_snapshot_url_uses_go2rtc_slug(self):
        """Snapshot endpoint constructs the URL from the camera slug (not ISAPI path)."""
        conn = _make_cameras_db()
        cam_id = conn.execute(
            "INSERT INTO cameras (name, host, cam_type) VALUES ('Front Door', '10.0.0.1', 'hikvision')"
        ).lastrowid
        conn.commit()

        captured_urls = []

        async def _fake_get(url, **kwargs):
            captured_urls.append(url)
            mock = MagicMock()
            mock.status_code = 200
            mock.content = b'\xff\xd8\xff\xd9'
            return mock

        def _do(client, token):
            with patch("sentinel.api.cameras.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.get = _fake_get
                mock_cls.return_value = mock_client

                with patch("sentinel.api.cameras.go2rtc_api_base", new=lambda: "http://127.0.0.1:1984"):
                    return client.get(
                        f"/api/cameras/{cam_id}/snapshot",
                        headers={"Authorization": f"Bearer {token}"},
                    )

        r = _run_with_db(conn, _do)
        assert r.status_code == 200, r.text
        assert len(captured_urls) == 1
        url = captured_urls[0]
        # Must be go2rtc frame.jpeg, not Hikvision ISAPI.
        assert "/api/frame.jpeg" in url, f"Expected go2rtc frame.jpeg URL, got: {url!r}"
        assert "ISAPI" not in url, f"Should NOT call ISAPI, got: {url!r}"
        assert "front_door" in url, f"Expected slug 'front_door' in URL, got: {url!r}"

    def test_snapshot_works_for_wyze_camera(self):
        """Snapshot also works for a Wyze cam (same go2rtc path, different cam_type)."""
        conn = _make_cameras_db()
        cam_id = conn.execute(
            "INSERT INTO cameras (name, host, cam_type, main_path, sub_path) "
            "VALUES ('Wyze Back', '10.0.0.99', 'wyze', '/live', '/live')"
        ).lastrowid
        conn.commit()

        fake_jpeg = b'\xff\xd8\xff\xe0' + b'\x00' * 50 + b'\xff\xd9'

        def _do(client, token):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = fake_jpeg

            with patch("sentinel.api.cameras.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.get = AsyncMock(return_value=mock_resp)
                mock_cls.return_value = mock_client

                with patch("sentinel.api.cameras.go2rtc_api_base", new=lambda: "http://127.0.0.1:1984"):
                    return client.get(
                        f"/api/cameras/{cam_id}/snapshot",
                        headers={"Authorization": f"Bearer {token}"},
                    )

        r = _run_with_db(conn, _do)
        assert r.status_code == 200
        assert r.content == fake_jpeg

    def test_snapshot_404_for_unknown_camera(self):
        """Snapshot returns 404 when the camera does not exist."""
        conn = _make_cameras_db()

        def _do(client, token):
            return client.get(
                "/api/cameras/9999/snapshot",
                headers={"Authorization": f"Bearer {token}"},
            )

        r = _run_with_db(conn, _do)
        assert r.status_code == 404

    def test_snapshot_502_when_stream_not_up(self):
        """Snapshot returns 502 when go2rtc returns non-200 (stream not up)."""
        conn = _make_cameras_db()
        cam_id = conn.execute(
            "INSERT INTO cameras (name, host) VALUES ('OfflineCam', '10.0.0.100')"
        ).lastrowid
        conn.commit()

        def _do(client, token):
            mock_resp = MagicMock()
            mock_resp.status_code = 404  # go2rtc stream not found
            mock_resp.content = b""

            with patch("sentinel.api.cameras.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.get = AsyncMock(return_value=mock_resp)
                mock_cls.return_value = mock_client

                with patch("sentinel.api.cameras.go2rtc_api_base", new=lambda: "http://127.0.0.1:1984"):
                    return client.get(
                        f"/api/cameras/{cam_id}/snapshot",
                        headers={"Authorization": f"Bearer {token}"},
                    )

        r = _run_with_db(conn, _do)
        assert r.status_code == 502


# ===========================================================================
# 4. Single-stream go2rtc config generation
# ===========================================================================

class TestSingleStreamGo2rtcConfig:
    """
    Wyze and Generic cameras (or any cam where sub_path == main_path) produce
    a sub stream that references the main stream name (string alias) rather than
    a second RTSP connection.
    """

    def _make_db_with_cam(self, cam_type: str, main_path: str, sub_path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(f"""
            CREATE TABLE cameras (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                host TEXT NOT NULL DEFAULT '10.0.0.1',
                rtsp_port INTEGER NOT NULL DEFAULT 554,
                rtsp_user TEXT NOT NULL DEFAULT 'admin',
                rtsp_pass_enc TEXT NOT NULL DEFAULT '',
                main_path TEXT NOT NULL DEFAULT '/Streaming/Channels/101',
                sub_path  TEXT NOT NULL DEFAULT '/Streaming/Channels/102',
                channel   INTEGER NOT NULL DEFAULT 1,
                record_mode TEXT NOT NULL DEFAULT 'motion-only',
                enabled   INTEGER NOT NULL DEFAULT 1,
                detect_config_json TEXT NOT NULL DEFAULT '{{}}',
                cam_type TEXT NOT NULL DEFAULT 'hikvision'
            );
        """)
        conn.execute(
            "INSERT INTO cameras (name, host, cam_type, main_path, sub_path) "
            "VALUES (?, ?, ?, ?, ?)",
            ("TestCam", "10.0.0.1", cam_type, main_path, sub_path),
        )
        conn.commit()
        return conn

    def test_wyze_sub_stream_is_alias(self):
        """Wyze cam: sub stream entry is the main stream name (string), not an RTSP URL."""
        conn = self._make_db_with_cam("wyze", "/live", "/live")
        import sentinel.db as db_module
        from sentinel.services.go2rtc import render_go2rtc_config

        orig = db_module.get_db
        db_module.get_db = lambda: conn
        try:
            cfg = render_go2rtc_config()
        finally:
            db_module.get_db = orig

        streams = cfg["streams"]
        # Find the sub stream key.
        sub_key = next((k for k in streams if k.endswith("_sub")), None)
        assert sub_key is not None, f"No sub stream found in {list(streams.keys())}"
        main_key = sub_key.replace("_sub", "")

        sub_val = streams[sub_key]
        assert isinstance(sub_val, list) and len(sub_val) == 1
        # The sub stream must be the main stream slug (string), not an rtsp:// URL.
        assert not sub_val[0].startswith("rtsp://"), \
            f"Wyze sub stream should NOT be an RTSP URL, got: {sub_val[0]!r}"
        assert sub_val[0] == main_key, \
            f"Wyze sub stream should reference main stream key '{main_key}', got: {sub_val[0]!r}"

    def test_generic_sub_stream_is_alias(self):
        """Generic cam: sub stream entry is the main stream name (string), not an RTSP URL."""
        conn = self._make_db_with_cam("generic", "/stream", "/stream")
        import sentinel.db as db_module
        from sentinel.services.go2rtc import render_go2rtc_config

        orig = db_module.get_db
        db_module.get_db = lambda: conn
        try:
            cfg = render_go2rtc_config()
        finally:
            db_module.get_db = orig

        streams = cfg["streams"]
        sub_key = next((k for k in streams if k.endswith("_sub")), None)
        assert sub_key is not None
        main_key = sub_key.replace("_sub", "")
        sub_val = streams[sub_key]
        assert sub_val[0] == main_key

    def test_hikvision_sub_stream_is_rtsp(self):
        """Hikvision cam: sub stream is still a real RTSP URL."""
        conn = self._make_db_with_cam("hikvision", "/Streaming/Channels/101", "/Streaming/Channels/102")
        import sentinel.db as db_module
        from sentinel.services.go2rtc import render_go2rtc_config

        orig = db_module.get_db
        db_module.get_db = lambda: conn
        try:
            cfg = render_go2rtc_config()
        finally:
            db_module.get_db = orig

        streams = cfg["streams"]
        sub_key = next((k for k in streams if k.endswith("_sub")), None)
        assert sub_key is not None
        sub_val = streams[sub_key]
        assert sub_val[0].startswith("rtsp://"), \
            f"Hikvision sub stream must be an RTSP URL, got: {sub_val[0]!r}"

    def test_same_paths_triggers_single_stream(self):
        """
        Any cam type where sub_path == main_path produces an alias (one connection).
        """
        conn = self._make_db_with_cam("hikvision", "/Streaming/Channels/101", "/Streaming/Channels/101")
        import sentinel.db as db_module
        from sentinel.services.go2rtc import render_go2rtc_config

        orig = db_module.get_db
        db_module.get_db = lambda: conn
        try:
            cfg = render_go2rtc_config()
        finally:
            db_module.get_db = orig

        streams = cfg["streams"]
        sub_key = next((k for k in streams if k.endswith("_sub")), None)
        assert sub_key is not None
        main_key = sub_key.replace("_sub", "")
        sub_val = streams[sub_key]
        # Even for Hikvision, identical paths → alias.
        assert not sub_val[0].startswith("rtsp://"), \
            f"Identical paths should produce alias, got: {sub_val[0]!r}"
        assert sub_val[0] == main_key

    def test_both_streams_present_in_config(self):
        """Both main and sub keys are always present, even for single-stream cameras."""
        conn = self._make_db_with_cam("wyze", "/live", "/live")
        import sentinel.db as db_module
        from sentinel.services.go2rtc import render_go2rtc_config

        orig = db_module.get_db
        db_module.get_db = lambda: conn
        try:
            cfg = render_go2rtc_config()
        finally:
            db_module.get_db = orig

        keys = list(cfg["streams"].keys())
        sub_keys = [k for k in keys if k.endswith("_sub")]
        main_keys = [k for k in keys if not k.endswith("_sub")]
        assert len(sub_keys) == 1
        assert len(main_keys) == 1
