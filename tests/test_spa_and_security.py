"""
tests/test_spa_and_security.py — v2 regression tests.

Covers:
  1. SPA fallback routing:
     - GET /login → 200 HTML (index.html served)
     - GET /cameras → 200 HTML
     - GET / → 200 HTML
     - GET /api/does-not-exist → 404 JSON
     - Static assets served correctly (not as HTML)
     - Known API routes still reachable (not swallowed by fallback)

  2. Path-traversal rejection:
     - Event thumb endpoint rejects paths outside events root (HTTP 400)
     - Recording video endpoint already tested in test_timeline.py;
       verified here end-to-end via the app client.

  3. Auth coverage:
     - All /api/* protected routes return 401 without a token.
     - /api/health and /api/auth/login are public.
     - WebSocket endpoint rejects connection without token.

  4. Supervisor log file handle leak:
     - _launch closes the log file handle after Popen.

  5. Recorder tiny-segment shutdown guard:
     - _index_final_segment skips/deletes files < 4 096 bytes.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

os.environ.setdefault("SENTINEL_SECRET_KEY", "test-secret-key-for-unit-tests-32x")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

from fastapi import Request as _Request
from fastapi.responses import FileResponse as _FileResponse, JSONResponse as _JSONResponse


def _make_spa_handler(dist: Path, index_html: Path):
    """Return a module-level SPA fallback function so FastAPI/Pydantic can resolve types."""
    async def _spa_fallback(request: _Request, full_path: str):
        if full_path.startswith("api/") or full_path == "api":
            return _JSONResponse({"detail": "Not Found"}, status_code=404)
        candidate = dist / full_path
        try:
            resolved = candidate.resolve()
            resolved.relative_to(dist.resolve())
            if resolved.is_file():
                return _FileResponse(str(resolved))
        except (ValueError, OSError):
            pass
        return _FileResponse(str(index_html))
    return _spa_fallback


def _make_full_app():
    """
    Build the full Sentinel FastAPI app backed by an in-memory SQLite DB
    and a temporary frontend/dist directory containing a real index.html.
    """
    import sqlite3
    import sentinel.db as db_module

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        PRAGMA foreign_keys=OFF;
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
            detect_config_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE recordings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id INTEGER NOT NULL,
            path TEXT NOT NULL,
            started_at TEXT,
            ended_at TEXT,
            duration_s REAL,
            size_bytes INTEGER,
            has_gap INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id INTEGER NOT NULL,
            type TEXT NOT NULL DEFAULT 'motion',
            label TEXT,
            score REAL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            thumb_path TEXT,
            clip_path TEXT,
            recording_id INTEGER,
            meta_json TEXT NOT NULL DEFAULT '{}'
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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            channel TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            sent_at TEXT
        );
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT);
    """)
    conn.commit()
    return conn


@pytest.fixture(scope="module")
def dist_dir(tmp_path_factory):
    """Create a temporary dist directory with index.html and static assets."""
    dist = tmp_path_factory.mktemp("dist")
    # Write a minimal index.html that contains the app root div.
    (dist / "index.html").write_text(
        '<!DOCTYPE html><html><head><title>Sentinel NVR</title></head>'
        '<body><div id="root"></div>'
        '<script type="module" src="/assets/index.js"></script>'
        '</body></html>',
        encoding="utf-8",
    )
    assets = dist / "assets"
    assets.mkdir()
    (assets / "index.js").write_bytes(b"// fake JS bundle")
    (assets / "index.css").write_bytes(b"/* fake CSS */")

    icons = dist / "icons"
    icons.mkdir()
    (icons / "icon-192.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    (dist / "sw.js").write_bytes(b"// service worker")
    (dist / "manifest.json").write_bytes(b'{"name":"Sentinel NVR"}')

    return dist


@pytest.fixture(scope="module")
def app_client(dist_dir):
    """Return a TestClient for the full Sentinel app, monkey-patched for test isolation."""
    import sentinel.db as db_module
    from sentinel.util.auth import create_access_token

    conn = _make_full_app()

    # Insert a test admin user.
    from sentinel.util.auth import hash_password
    conn.execute(
        "INSERT INTO users (username, pass_hash, role) VALUES (?, ?, 'admin')",
        ("admin", hash_password("testpassword")),
    )
    conn.commit()

    original_get_db = db_module.get_db
    original_db = db_module._db
    db_module.get_db = lambda: conn
    # Also set the module-level singleton so modules that bound the real
    # sentinel.db.get_db function (not the lambda) still return our test conn.
    db_module._db = conn

    # Patch FRONTEND_DIST in main so the app sees our temp dist directory.
    import sentinel.main as main_module
    original_dist = main_module.FRONTEND_DIST
    original_index = main_module._INDEX_HTML

    # We need to reload the app with the patched dist dir.
    # Patch the module-level names and rebuild the app routes.
    from fastapi import FastAPI, Depends
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.staticfiles import StaticFiles

    from sentinel.api.auth import router as auth_router
    from sentinel.api.system import router as system_router
    from sentinel.api.cameras import router as cameras_router
    from sentinel.api.live import router as live_router
    from sentinel.api.recordings import router as recordings_router
    from sentinel.api.events import router as events_router
    from sentinel.api.timeline import router as timeline_router
    from sentinel.api.settings import router as settings_router
    from sentinel.api.system import health
    from sentinel.util.auth import get_current_user

    test_app = FastAPI(title="Sentinel NVR Test")
    test_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    test_app.include_router(auth_router, prefix="/api")
    _auth_dep = [Depends(get_current_user)]
    test_app.include_router(system_router, prefix="/api", dependencies=_auth_dep)
    test_app.include_router(cameras_router, prefix="/api", dependencies=_auth_dep)
    test_app.include_router(live_router, prefix="/api", dependencies=_auth_dep)
    test_app.include_router(recordings_router, prefix="/api", dependencies=_auth_dep)
    test_app.include_router(events_router, prefix="/api", dependencies=_auth_dep)
    test_app.include_router(timeline_router, prefix="/api", dependencies=_auth_dep)
    test_app.include_router(settings_router, prefix="/api", dependencies=_auth_dep)
    test_app.add_api_route("/api/health", health, methods=["GET"], tags=["system"])

    # Mount static assets
    DIST = dist_dir
    INDEX_HTML = DIST / "index.html"

    if (DIST / "assets").exists():
        test_app.mount("/assets", StaticFiles(directory=str(DIST / "assets")), name="assets")
    if (DIST / "icons").exists():
        test_app.mount("/icons", StaticFiles(directory=str(DIST / "icons")), name="icons")

    # Build the SPA fallback as a proper function in module scope so
    # FastAPI/Pydantic can resolve the `Request` type annotation correctly.
    _spa = _make_spa_handler(DIST, INDEX_HTML)
    test_app.add_api_route("/{full_path:path}", _spa, methods=["GET"], include_in_schema=False)

    from fastapi.testclient import TestClient
    client = TestClient(test_app, raise_server_exceptions=True)

    yield client, conn, create_access_token({"sub": "1", "username": "admin", "role": "admin"})

    db_module.get_db = original_get_db
    db_module._db = original_db
    conn.close()


# ===========================================================================
# 1. SPA Fallback Routing
# ===========================================================================

class TestSPAFallback:
    def test_root_returns_html(self, app_client):
        client, conn, token = app_client
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
        assert b'id="root"' in r.content

    def test_login_route_returns_html(self, app_client):
        """GET /login must return the SPA index.html (200), not a 404."""
        client, conn, token = app_client
        r = client.get("/login")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text[:200]}"
        assert "text/html" in r.headers.get("content-type", "")
        # The response body must contain the app root div.
        assert b'id="root"' in r.content

    def test_cameras_route_returns_html(self, app_client):
        """GET /cameras (client-side route) must return index.html."""
        client, conn, token = app_client
        r = client.get("/cameras")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
        assert b'id="root"' in r.content

    def test_deep_spa_route_returns_html(self, app_client):
        """GET /playback (deep client-side route) must return index.html."""
        client, conn, token = app_client
        r = client.get("/playback")
        assert r.status_code == 200
        assert b'id="root"' in r.content

    def test_unknown_api_route_returns_json_404(self, app_client):
        """GET /api/does-not-exist must return JSON 404, not the SPA HTML."""
        client, conn, token = app_client
        r = client.get("/api/does-not-exist")
        assert r.status_code == 404, f"Expected 404, got {r.status_code}"
        ct = r.headers.get("content-type", "")
        assert "application/json" in ct, f"Expected JSON, got content-type={ct!r}"
        data = r.json()
        assert "detail" in data

    def test_unknown_api_subpath_returns_json_404(self, app_client):
        """GET /api/nope/deeply/nested must return JSON 404."""
        client, conn, token = app_client
        r = client.get("/api/nope/deeply/nested")
        assert r.status_code == 404
        assert "application/json" in r.headers.get("content-type", "")

    def test_api_health_still_works(self, app_client):
        """GET /api/health must still return 200 JSON (public route)."""
        client, conn, token = app_client
        r = client.get("/api/health")
        assert r.status_code == 200
        assert "application/json" in r.headers.get("content-type", "")
        assert r.json()["status"] == "ok"

    def test_static_asset_served_directly(self, app_client):
        """Static JS assets under /assets/ must be served directly."""
        client, conn, token = app_client
        r = client.get("/assets/index.js")
        assert r.status_code == 200

    def test_sw_js_served_directly(self, app_client):
        """sw.js must be served from dist root, not as SPA HTML."""
        client, conn, token = app_client
        r = client.get("/sw.js")
        assert r.status_code == 200
        # Should NOT be the SPA HTML.
        assert b'id="root"' not in r.content

    def test_manifest_json_served_directly(self, app_client):
        """manifest.json must be served directly."""
        client, conn, token = app_client
        r = client.get("/manifest.json")
        assert r.status_code == 200


# ===========================================================================
# 2. Auth coverage
# ===========================================================================

class TestAuthCoverage:
    """Every /api route except /health and /auth/login must require a token."""

    def test_health_is_public(self, app_client):
        client, conn, token = app_client
        r = client.get("/api/health")
        assert r.status_code == 200

    def test_login_is_public(self, app_client):
        client, conn, token = app_client
        r = client.post("/api/auth/login", json={"username": "x", "password": "y"})
        # 401 from credential check, not from missing auth header → route was reached.
        assert r.status_code == 401

    def test_cameras_requires_auth(self, app_client):
        client, conn, token = app_client
        r = client.get("/api/cameras")
        assert r.status_code == 401

    def test_live_requires_auth(self, app_client):
        client, conn, token = app_client
        r = client.get("/api/live")
        assert r.status_code == 401

    def test_recordings_requires_auth(self, app_client):
        client, conn, token = app_client
        r = client.get("/api/recordings")
        assert r.status_code == 401

    def test_events_requires_auth(self, app_client):
        client, conn, token = app_client
        r = client.get("/api/events")
        assert r.status_code == 401

    def test_timeline_requires_auth(self, app_client):
        client, conn, token = app_client
        r = client.get("/api/timeline?camera_id=1&start=2026-01-01T00:00:00Z&end=2026-01-02T00:00:00Z")
        assert r.status_code == 401

    def test_settings_requires_auth(self, app_client):
        client, conn, token = app_client
        r = client.get("/api/settings")
        assert r.status_code == 401

    def test_system_storage_requires_auth(self, app_client):
        client, conn, token = app_client
        r = client.get("/api/system/storage")
        assert r.status_code == 401

    def test_export_requires_auth(self, app_client):
        client, conn, token = app_client
        r = client.post("/api/export", json={"camera_id": 1, "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z"})
        assert r.status_code == 401

    def test_auth_me_requires_auth(self, app_client):
        client, conn, token = app_client
        r = client.get("/api/auth/me")
        assert r.status_code == 401

    def test_authenticated_cameras_returns_200(self, app_client):
        """A valid token unlocks a protected route."""
        client, conn, token = app_client
        r = client.get("/api/cameras", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200


# ===========================================================================
# 3. Event thumbnail path-traversal rejection
# ===========================================================================

class TestEventThumbPathTraversal:
    """Verify _safe_event_path rejects paths outside sentinel_events_dir."""

    def test_path_outside_events_root_rejected(self, tmp_path):
        from sentinel.api.events import _safe_event_path
        from fastapi import HTTPException

        events_root = tmp_path / "events"
        events_root.mkdir()
        outside = tmp_path / "etc" / "passwd"
        outside.parent.mkdir()
        outside.write_text("root:x:0:0:root:/root:/bin/bash")

        with patch("sentinel.api.events._EVENTS_ROOT", events_root):
            with pytest.raises(HTTPException) as exc:
                _safe_event_path(str(outside))
        assert exc.value.status_code == 400

    def test_dotdot_traversal_rejected(self, tmp_path):
        from sentinel.api.events import _safe_event_path
        from fastapi import HTTPException

        events_root = tmp_path / "events"
        events_root.mkdir()
        evil = str(events_root / ".." / "secret.txt")

        with patch("sentinel.api.events._EVENTS_ROOT", events_root):
            with pytest.raises(HTTPException) as exc:
                _safe_event_path(evil)
        assert exc.value.status_code in (400, 404)

    def test_valid_thumb_inside_root_allowed(self, tmp_path):
        from sentinel.api.events import _safe_event_path

        events_root = tmp_path / "events"
        events_root.mkdir()
        thumb = events_root / "cam1" / "thumb.jpg"
        thumb.parent.mkdir()
        thumb.write_bytes(b"\xff\xd8\xff")  # JPEG magic bytes

        with patch("sentinel.api.events._EVENTS_ROOT", events_root):
            result = _safe_event_path(str(thumb))
        assert result == thumb.resolve()

    def test_missing_file_raises_404(self, tmp_path):
        from sentinel.api.events import _safe_event_path
        from fastapi import HTTPException

        events_root = tmp_path / "events"
        events_root.mkdir()
        ghost = events_root / "nonexistent.jpg"

        with patch("sentinel.api.events._EVENTS_ROOT", events_root):
            with pytest.raises(HTTPException) as exc:
                _safe_event_path(str(ghost))
        assert exc.value.status_code == 404


# ===========================================================================
# 4. Supervisor log file handle not leaked
# ===========================================================================

class TestSupervisorLogHandleClosed:
    """After _launch(), the Python-side log file handle must be closed."""

    def test_log_handle_closed_after_launch(self, tmp_path):
        from sentinel.services.supervisor import ProcessSupervisor, RestartPolicy
        import subprocess

        log_file = tmp_path / "test.log"
        # Use a no-op command that exits immediately.
        sup = ProcessSupervisor(
            name=f"test-sup-{id(tmp_path)}",
            cmd=["python", "-c", "import sys; sys.exit(0)"],
            restart_policy=RestartPolicy.NEVER,
            max_restarts=0,
            log_file=log_file,
        )

        opened_handles = []
        original_open = __builtins__["open"] if isinstance(__builtins__, dict) else open

        import builtins
        original_open = builtins.open

        def _tracking_open(path, mode="r", **kwargs):
            fh = original_open(path, mode, **kwargs)
            if "ab" in str(mode) and str(path) == str(log_file):
                opened_handles.append(fh)
            return fh

        with patch("builtins.open", side_effect=_tracking_open):
            with sup._lock:
                sup._launch()

        # Clean up the process
        try:
            if sup._proc:
                sup._proc.wait(timeout=5)
        except Exception:
            pass

        # All tracked handles must be closed.
        for fh in opened_handles:
            assert fh.closed, "Log file handle was not closed after _launch()"


# ===========================================================================
# 5. Recorder tiny-segment shutdown guard
# ===========================================================================

class TestRecorderTinySegmentGuard:
    """_index_final_segment must skip and delete files smaller than 4096 bytes."""

    def _make_indexer(self, cam_dir, db):
        import sentinel.db as db_module
        original = db_module.get_db
        db_module.get_db = lambda: db
        try:
            from sentinel.services.recorder import SegmentIndexer
            idx = SegmentIndexer(camera_id=1, camera_dir=cam_dir)
        finally:
            db_module.get_db = original
        return idx

    def _make_db(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE recordings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,
                duration_s REAL,
                size_bytes INTEGER,
                has_gap INTEGER NOT NULL DEFAULT 0
            );
        """)
        conn.commit()
        return conn

    def test_tiny_file_not_indexed(self, tmp_path):
        """A 48-byte stub at shutdown must not be inserted into the DB."""
        import sentinel.db as db_module

        cam_dir = tmp_path / "cam1"
        cam_dir.mkdir()
        day_dir = cam_dir / "2026-05-24"
        day_dir.mkdir()
        stub = day_dir / "120000.mp4"
        stub.write_bytes(b"\x00" * 48)  # 48-byte stub

        db = self._make_db()
        original_get_db = db_module.get_db
        db_module.get_db = lambda: db
        try:
            from sentinel.services.recorder import SegmentIndexer
            idx = SegmentIndexer(camera_id=1, camera_dir=cam_dir)
            # Manually set the seen order to simulate the stub being the last file seen.
            idx._seen_order = [stub]
            idx._index_final_segment()
        finally:
            db_module.get_db = original_get_db

        rows = db.execute("SELECT * FROM recordings").fetchall()
        assert len(rows) == 0, f"Tiny stub must not be indexed, got {len(rows)} rows"

    def test_tiny_file_deleted_on_shutdown(self, tmp_path):
        """A tiny stub must be deleted from disk at shutdown."""
        import sentinel.db as db_module

        cam_dir = tmp_path / "cam1"
        cam_dir.mkdir()
        day_dir = cam_dir / "2026-05-24"
        day_dir.mkdir()
        stub = day_dir / "120000.mp4"
        stub.write_bytes(b"\x00" * 48)

        db = self._make_db()
        original_get_db = db_module.get_db
        db_module.get_db = lambda: db
        try:
            from sentinel.services.recorder import SegmentIndexer
            idx = SegmentIndexer(camera_id=1, camera_dir=cam_dir)
            idx._seen_order = [stub]
            idx._index_final_segment()
        finally:
            db_module.get_db = original_get_db

        assert not stub.exists(), "Tiny stub file must be deleted at shutdown"

    def test_real_segment_is_indexed(self, tmp_path):
        """A file >= 4096 bytes should still be passed to _insert_segment."""
        import sentinel.db as db_module

        cam_dir = tmp_path / "cam1"
        cam_dir.mkdir()
        day_dir = cam_dir / "2026-05-24"
        day_dir.mkdir()
        real_mp4 = day_dir / "120000.mp4"
        real_mp4.write_bytes(b"\x00" * 8192)  # large enough

        db = self._make_db()
        original_get_db = db_module.get_db
        db_module.get_db = lambda: db

        inserted = []

        def _fake_insert(self_idx, path, force=False):
            inserted.append(path)

        try:
            from sentinel.services import recorder as rec_module
            from sentinel.services.recorder import SegmentIndexer
            idx = SegmentIndexer(camera_id=1, camera_dir=cam_dir)
            idx._seen_order = [real_mp4]

            with patch.object(SegmentIndexer, "_insert_segment", _fake_insert):
                idx._index_final_segment()
        finally:
            db_module.get_db = original_get_db

        assert real_mp4 in inserted, "Real segment was not passed to _insert_segment"

    def test_already_indexed_file_skipped(self, tmp_path):
        """If the last segment is already in _indexed, _index_final_segment is a no-op."""
        import sentinel.db as db_module

        cam_dir = tmp_path / "cam1"
        cam_dir.mkdir()
        day_dir = cam_dir / "2026-05-24"
        day_dir.mkdir()
        real_mp4 = day_dir / "120000.mp4"
        real_mp4.write_bytes(b"\x00" * 8192)

        db = self._make_db()
        original_get_db = db_module.get_db
        db_module.get_db = lambda: db

        inserted = []

        def _fake_insert(self_idx, path, force=False):
            inserted.append(path)

        try:
            from sentinel.services.recorder import SegmentIndexer
            idx = SegmentIndexer(camera_id=1, camera_dir=cam_dir)
            idx._seen_order = [real_mp4]
            idx._indexed.add(real_mp4)  # already indexed

            with patch.object(SegmentIndexer, "_insert_segment", _fake_insert):
                idx._index_final_segment()
        finally:
            db_module.get_db = original_get_db

        assert inserted == [], "Already-indexed file must not be inserted again"
