"""
tests/test_notifier.py — Unit tests for Phase 6 notifications.

Tests:
  - Telegram: correct URL + payload; photo attached; text fallback when no thumb.
  - ntfy:     correct URL; image attached; text fallback.
  - SMTP:     correct host/port; attachment present; STARTTLS vs plain vs SSL.
  - Failing channel: exception caught, 'failed' row written, no re-raise.
  - Cooldown/dedup: repeated sends suppressed within window; allowed after.
  - Quiet-hours gate: in-window suppressed; out-of-window passes.
  - Camera-enable filter: excluded camera suppressed; included passes.
  - Pipeline: synthetic event written through Notifier._handle_event with mocked
    transports; assert 'notifications' DB rows created with correct status.
"""

from __future__ import annotations

import io
import os
import smtplib
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Ensure test secret before any sentinel import
os.environ.setdefault("SENTINEL_SECRET_KEY", "test-secret-key-for-unit-tests-only")


# ---------------------------------------------------------------------------
# In-memory DB helpers
# ---------------------------------------------------------------------------

def _make_db() -> sqlite3.Connection:
    """Minimal in-memory DB with events + notifications + settings tables."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA foreign_keys=OFF;

        CREATE TABLE cameras (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        );

        CREATE TABLE events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id    INTEGER NOT NULL,
            type         TEXT    NOT NULL DEFAULT 'motion',
            label        TEXT,
            score        REAL,
            started_at   TEXT    NOT NULL,
            ended_at     TEXT,
            thumb_path   TEXT,
            clip_path    TEXT,
            recording_id INTEGER,
            meta_json    TEXT    NOT NULL DEFAULT '{}'
        );

        CREATE TABLE notifications (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            channel  TEXT    NOT NULL,
            status   TEXT    NOT NULL DEFAULT 'pending',
            sent_at  TEXT
        );

        CREATE TABLE settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        INSERT INTO cameras(name) VALUES ('test_cam');
        INSERT INTO events(camera_id, type, started_at, meta_json)
            VALUES (1, 'motion', '2026-05-24T10:00:00Z', '{}');
        """
    )
    conn.commit()
    return conn


def _make_thumb(tmp_path: Path) -> Path:
    """Write a tiny JPEG file and return its path."""
    from PIL import Image
    img = Image.new("RGB", (10, 10), color=(200, 100, 50))
    p = tmp_path / "thumb.jpg"
    img.save(str(p), "JPEG")
    return p


def _settings_with(conn: sqlite3.Connection, **kwargs) -> None:
    """Insert or replace settings rows."""
    for k, v in kwargs.items():
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
    conn.commit()


def _notifier_with_conn(conn: sqlite3.Connection):
    """Create a Notifier instance whose _conn is the given in-memory DB."""
    from sentinel.services.notifier import Notifier
    n = Notifier()
    n._conn = conn
    n._running = True
    return n


# ===========================================================================
# 1. Telegram channel
# ===========================================================================

class TestTelegram:
    def test_sends_photo_when_thumb_exists(self, tmp_path):
        from sentinel.services.notifier import _send_telegram
        thumb = _make_thumb(tmp_path)

        with patch("sentinel.services.notifier.httpx.Client") as MockClient:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            ctx = MockClient.return_value.__enter__.return_value
            ctx.post.return_value = mock_resp

            _send_telegram(
                token="BOT_TOKEN",
                chat_id="CHAT_ID",
                caption="Test caption",
                thumb_path=str(thumb),
            )

        # Should call /sendPhoto with a files= arg
        assert MockClient.return_value.__enter__.return_value.post.called
        call_kwargs = ctx.post.call_args
        assert "files" in call_kwargs.kwargs or (len(call_kwargs.args) > 0)
        url_called = ctx.post.call_args.args[0] if ctx.post.call_args.args else ctx.post.call_args.kwargs.get("url", "")
        assert "sendPhoto" in url_called

    def test_falls_back_to_text_when_no_thumb(self):
        from sentinel.services.notifier import _send_telegram

        with patch("sentinel.services.notifier.httpx.Client") as MockClient:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            ctx = MockClient.return_value.__enter__.return_value
            ctx.post.return_value = mock_resp

            _send_telegram(
                token="BOT_TOKEN",
                chat_id="CHAT_ID",
                caption="Test caption",
                thumb_path=None,
            )

        url_called = ctx.post.call_args.args[0] if ctx.post.call_args.args else ""
        assert "sendMessage" in url_called

    def test_uses_correct_bot_token_in_url(self, tmp_path):
        from sentinel.services.notifier import _send_telegram

        with patch("sentinel.services.notifier.httpx.Client") as MockClient:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            ctx = MockClient.return_value.__enter__.return_value
            ctx.post.return_value = mock_resp

            _send_telegram(
                token="MY_SECRET_TOKEN",
                chat_id="123",
                caption="hi",
                thumb_path=None,
            )

        url_called = ctx.post.call_args.args[0] if ctx.post.call_args.args else ""
        assert "MY_SECRET_TOKEN" in url_called

    def test_raises_on_http_error(self):
        from sentinel.services.notifier import _send_telegram
        import httpx

        with patch("sentinel.services.notifier.httpx.Client") as MockClient:
            ctx = MockClient.return_value.__enter__.return_value
            mock_resp = MagicMock()
            mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "403 Forbidden", request=MagicMock(), response=MagicMock()
            )
            ctx.post.return_value = mock_resp

            with pytest.raises(httpx.HTTPStatusError):
                _send_telegram("BAD_TOKEN", "chat", "msg", None)


# ===========================================================================
# 2. ntfy channel
# ===========================================================================

class TestNtfy:
    def test_posts_image_when_thumb_exists(self, tmp_path):
        from sentinel.services.notifier import _send_ntfy
        thumb = _make_thumb(tmp_path)

        with patch("sentinel.services.notifier.httpx.Client") as MockClient:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            ctx = MockClient.return_value.__enter__.return_value
            ctx.post.return_value = mock_resp

            _send_ntfy(
                server="https://ntfy.sh",
                topic="sentinel-test",
                message="Motion detected",
                title="Sentinel",
                thumb_path=str(thumb),
            )

        # The call should include Content-Type: image/jpeg
        call_kwargs = ctx.post.call_args.kwargs
        headers = call_kwargs.get("headers", {})
        assert headers.get("Content-Type") == "image/jpeg"

    def test_correct_topic_url(self, tmp_path):
        from sentinel.services.notifier import _send_ntfy

        with patch("sentinel.services.notifier.httpx.Client") as MockClient:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            ctx = MockClient.return_value.__enter__.return_value
            ctx.post.return_value = mock_resp

            _send_ntfy(
                server="https://ntfy.sh",
                topic="my-topic",
                message="test",
                title="T",
                thumb_path=None,
            )

        url = ctx.post.call_args.args[0] if ctx.post.call_args.args else ctx.post.call_args.kwargs.get("url", "")
        assert "my-topic" in url
        assert "ntfy.sh" in url

    def test_raises_on_http_error(self):
        from sentinel.services.notifier import _send_ntfy
        import httpx

        with patch("sentinel.services.notifier.httpx.Client") as MockClient:
            ctx = MockClient.return_value.__enter__.return_value
            mock_resp = MagicMock()
            mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "500", request=MagicMock(), response=MagicMock()
            )
            ctx.post.return_value = mock_resp

            with pytest.raises(httpx.HTTPStatusError):
                _send_ntfy("https://ntfy.sh", "topic", "msg", "title", None)


# ===========================================================================
# 3. SMTP email channel
# ===========================================================================

class TestSMTP:
    def test_starttls_used_by_default(self, tmp_path):
        from sentinel.services.notifier import _send_email

        with patch("sentinel.services.notifier.smtplib.SMTP") as MockSMTP:
            mock_smtp = MockSMTP.return_value.__enter__.return_value
            mock_smtp.sendmail = MagicMock()

            _send_email(
                host="smtp.example.com",
                port=587,
                user="user@example.com",
                password="secret",
                from_addr="user@example.com",
                to_addrs=["dest@example.com"],
                subject="Test",
                body="Hello",
                thumb_path=None,
                tls_mode="1",
            )

        mock_smtp.starttls.assert_called_once()
        mock_smtp.login.assert_called_once_with("user@example.com", "secret")
        mock_smtp.sendmail.assert_called_once()

    def test_ssl_uses_smtp_ssl(self, tmp_path):
        from sentinel.services.notifier import _send_email

        with patch("sentinel.services.notifier.smtplib.SMTP_SSL") as MockSMTPSSL:
            mock_smtp = MockSMTPSSL.return_value.__enter__.return_value
            mock_smtp.sendmail = MagicMock()

            _send_email(
                host="smtp.example.com",
                port=465,
                user="u",
                password="p",
                from_addr="u@example.com",
                to_addrs=["r@example.com"],
                subject="s",
                body="b",
                thumb_path=None,
                tls_mode="ssl",
            )

        MockSMTPSSL.assert_called_once()
        mock_smtp.sendmail.assert_called_once()

    def test_plain_no_starttls(self):
        from sentinel.services.notifier import _send_email

        with patch("sentinel.services.notifier.smtplib.SMTP") as MockSMTP:
            mock_smtp = MockSMTP.return_value.__enter__.return_value
            mock_smtp.sendmail = MagicMock()

            _send_email(
                host="smtp.example.com",
                port=25,
                user="",
                password="",
                from_addr="a@b.com",
                to_addrs=["c@d.com"],
                subject="s",
                body="b",
                thumb_path=None,
                tls_mode="0",
            )

        mock_smtp.starttls.assert_not_called()

    def test_attachment_included_when_thumb_exists(self, tmp_path):
        from sentinel.services.notifier import _send_email
        thumb = _make_thumb(tmp_path)
        captured = {}

        with patch("sentinel.services.notifier.smtplib.SMTP") as MockSMTP:
            mock_smtp = MockSMTP.return_value.__enter__.return_value

            def capture_sendmail(from_addr, to_addrs, raw_msg):
                captured["raw"] = raw_msg

            mock_smtp.sendmail = capture_sendmail

            _send_email(
                host="smtp.example.com",
                port=587,
                user="u",
                password="p",
                from_addr="u@e.com",
                to_addrs=["r@e.com"],
                subject="Motion",
                body="Motion detected",
                thumb_path=str(thumb),
                tls_mode="0",
            )

        # The raw message should contain the JPEG content-type header
        assert b"image/jpeg" in captured["raw"]
        assert b"thumb.jpg" in captured["raw"]


# ===========================================================================
# 4. Failing channel: exception caught, status='failed' row written
# ===========================================================================

class TestFailingChannel:
    def test_failed_channel_writes_failed_row(self, tmp_path):
        """
        If a channel raises an exception, the notifications row has status='failed'
        and the exception does NOT propagate.
        """
        conn = _make_db()
        _settings_with(
            conn,
            notify_enabled="1",
            notify_cooldown_seconds="0",
            notify_channels="telegram",
            notify_cameras="",
            notify_quiet_start="",
            notify_quiet_end="",
            notify_telegram_bot_token="FAKE_TOKEN",
            notify_telegram_chat_id="FAKE_CHAT",
        )

        n = _notifier_with_conn(conn)

        import httpx
        with patch(
            "sentinel.services.notifier._send_telegram",
            side_effect=Exception("network error"),
        ):
            # Should NOT raise
            n._handle_event({
                "type":        "motion_start",
                "event_id":    1,
                "camera_id":   1,
                "camera_name": "cam1",
                "started_at":  "2026-05-24T10:00:00Z",
                "thumb_path":  None,
            })

        row = conn.execute(
            "SELECT status FROM notifications WHERE event_id=1 AND channel='telegram'"
        ).fetchone()
        assert row is not None, "notifications row should be written even on failure"
        assert row["status"] == "failed"

    def test_one_channel_failure_does_not_block_others(self, tmp_path):
        """If telegram fails, ntfy should still be attempted."""
        conn = _make_db()
        _settings_with(
            conn,
            notify_enabled="1",
            notify_cooldown_seconds="0",
            notify_channels="telegram,ntfy",
            notify_cameras="",
            notify_quiet_start="",
            notify_quiet_end="",
            notify_telegram_bot_token="T",
            notify_telegram_chat_id="C",
            notify_ntfy_server="https://ntfy.sh",
            notify_ntfy_topic="test-topic",
        )

        n = _notifier_with_conn(conn)

        ntfy_called = []

        with patch(
            "sentinel.services.notifier._send_telegram",
            side_effect=Exception("telegram down"),
        ):
            with patch(
                "sentinel.services.notifier._send_ntfy",
                side_effect=lambda *a, **kw: ntfy_called.append(True),
            ):
                n._handle_event({
                    "type":        "motion_start",
                    "event_id":    1,
                    "camera_id":   1,
                    "camera_name": "cam1",
                    "started_at":  "2026-05-24T10:00:00Z",
                    "thumb_path":  None,
                })

        assert ntfy_called, "ntfy should still be called after telegram failure"

        rows = conn.execute(
            "SELECT channel, status FROM notifications WHERE event_id=1 ORDER BY channel"
        ).fetchall()
        statuses = {r["channel"]: r["status"] for r in rows}
        assert statuses.get("telegram") == "failed"
        assert statuses.get("ntfy") == "sent"


# ===========================================================================
# 5. Cooldown / dedup
# ===========================================================================

class TestCooldown:
    def test_suppresses_within_window(self, tmp_path):
        conn = _make_db()
        _settings_with(
            conn,
            notify_enabled="1",
            notify_cooldown_seconds="60",
            notify_channels="telegram",
            notify_cameras="",
            notify_quiet_start="",
            notify_quiet_end="",
            notify_telegram_bot_token="T",
            notify_telegram_chat_id="C",
        )

        n = _notifier_with_conn(conn)
        calls = []

        with patch(
            "sentinel.services.notifier._send_telegram",
            side_effect=lambda *a, **kw: calls.append(True),
        ):
            # First event
            n._handle_event({
                "type": "motion_start", "event_id": 1, "camera_id": 1,
                "camera_name": "cam1", "started_at": "2026-05-24T10:00:00Z",
                "thumb_path": None,
            })
            # Insert a second event row so the DB reference is valid
            conn.execute(
                "INSERT INTO events(camera_id,type,started_at,meta_json) VALUES(1,'motion','2026-05-24T10:00:05Z','{}')"
            )
            conn.commit()
            event_id2 = conn.execute("SELECT MAX(id) FROM events").fetchone()[0]
            # Second event within cooldown window
            n._handle_event({
                "type": "motion_start", "event_id": event_id2, "camera_id": 1,
                "camera_name": "cam1", "started_at": "2026-05-24T10:00:05Z",
                "thumb_path": None,
            })

        # Only first send should have gone through
        assert len(calls) == 1, f"Expected 1 send, got {len(calls)}"

    def test_allows_after_cooldown_expires(self, tmp_path):
        conn = _make_db()
        _settings_with(
            conn,
            notify_enabled="1",
            notify_cooldown_seconds="1",
            notify_channels="telegram",
            notify_cameras="",
            notify_quiet_start="",
            notify_quiet_end="",
            notify_telegram_bot_token="T",
            notify_telegram_chat_id="C",
        )

        n = _notifier_with_conn(conn)
        calls = []

        with patch(
            "sentinel.services.notifier._send_telegram",
            side_effect=lambda *a, **kw: calls.append(True),
        ):
            # First event
            n._handle_event({
                "type": "motion_start", "event_id": 1, "camera_id": 1,
                "camera_name": "cam1", "started_at": "2026-05-24T10:00:00Z",
                "thumb_path": None,
            })
            # Wait for cooldown to expire
            time.sleep(1.1)
            conn.execute(
                "INSERT INTO events(camera_id,type,started_at,meta_json) VALUES(1,'motion','2026-05-24T10:00:02Z','{}')"
            )
            conn.commit()
            event_id2 = conn.execute("SELECT MAX(id) FROM events").fetchone()[0]
            # Second event — should be allowed
            n._handle_event({
                "type": "motion_start", "event_id": event_id2, "camera_id": 1,
                "camera_name": "cam1", "started_at": "2026-05-24T10:00:02Z",
                "thumb_path": None,
            })

        assert len(calls) == 2, f"Expected 2 sends after cooldown, got {len(calls)}"


# ===========================================================================
# 6. Quiet hours
# ===========================================================================

class TestQuietHours:
    def test_suppresses_during_quiet_hours(self, tmp_path):
        from sentinel.services.notifier import NotifyRules
        # Build rules with quiet 00:00 – 23:59 (basically always quiet)
        s = {
            "notify_enabled":        "1",
            "notify_cooldown_seconds": "0",
            "notify_channels":       "telegram",
            "notify_cameras":        "",
            "notify_quiet_start":    "00:00",
            "notify_quiet_end":      "23:59",
        }
        rules = NotifyRules(s)
        assert rules.in_quiet_hours(), "Should be in quiet hours with 00:00-23:59 window"

    def test_allows_outside_quiet_hours(self):
        from sentinel.services.notifier import NotifyRules
        # No quiet hours
        s = {
            "notify_enabled":        "1",
            "notify_cooldown_seconds": "0",
            "notify_channels":       "telegram",
            "notify_cameras":        "",
            "notify_quiet_start":    "",
            "notify_quiet_end":      "",
        }
        rules = NotifyRules(s)
        assert not rules.in_quiet_hours()

    def test_handle_event_suppressed_in_quiet_hours(self, tmp_path):
        conn = _make_db()
        _settings_with(
            conn,
            notify_enabled="1",
            notify_cooldown_seconds="0",
            notify_channels="telegram",
            notify_cameras="",
            notify_quiet_start="00:00",
            notify_quiet_end="23:59",
            notify_telegram_bot_token="T",
            notify_telegram_chat_id="C",
        )

        n = _notifier_with_conn(conn)
        calls = []

        with patch(
            "sentinel.services.notifier._send_telegram",
            side_effect=lambda *a, **kw: calls.append(True),
        ):
            n._handle_event({
                "type": "motion_start", "event_id": 1, "camera_id": 1,
                "camera_name": "cam1", "started_at": "2026-05-24T10:00:00Z",
                "thumb_path": None,
            })

        assert len(calls) == 0, "Should be suppressed by quiet hours"


# ===========================================================================
# 7. Camera-enable filter
# ===========================================================================

class TestCameraFilter:
    def test_excluded_camera_suppressed(self, tmp_path):
        conn = _make_db()
        _settings_with(
            conn,
            notify_enabled="1",
            notify_cooldown_seconds="0",
            notify_channels="telegram",
            notify_cameras="2,3",          # cam 1 NOT included
            notify_quiet_start="",
            notify_quiet_end="",
            notify_telegram_bot_token="T",
            notify_telegram_chat_id="C",
        )

        n = _notifier_with_conn(conn)
        calls = []

        with patch(
            "sentinel.services.notifier._send_telegram",
            side_effect=lambda *a, **kw: calls.append(True),
        ):
            n._handle_event({
                "type": "motion_start", "event_id": 1, "camera_id": 1,
                "camera_name": "cam1", "started_at": "2026-05-24T10:00:00Z",
                "thumb_path": None,
            })

        assert len(calls) == 0

    def test_included_camera_passes(self, tmp_path):
        conn = _make_db()
        _settings_with(
            conn,
            notify_enabled="1",
            notify_cooldown_seconds="0",
            notify_channels="telegram",
            notify_cameras="1,2",          # cam 1 IS included
            notify_quiet_start="",
            notify_quiet_end="",
            notify_telegram_bot_token="T",
            notify_telegram_chat_id="C",
        )

        n = _notifier_with_conn(conn)
        calls = []

        with patch(
            "sentinel.services.notifier._send_telegram",
            side_effect=lambda *a, **kw: calls.append(True),
        ):
            n._handle_event({
                "type": "motion_start", "event_id": 1, "camera_id": 1,
                "camera_name": "cam1", "started_at": "2026-05-24T10:00:00Z",
                "thumb_path": None,
            })

        assert len(calls) == 1

    def test_empty_camera_list_allows_all(self, tmp_path):
        conn = _make_db()
        _settings_with(
            conn,
            notify_enabled="1",
            notify_cooldown_seconds="0",
            notify_channels="telegram",
            notify_cameras="",             # all cameras
            notify_quiet_start="",
            notify_quiet_end="",
            notify_telegram_bot_token="T",
            notify_telegram_chat_id="C",
        )

        n = _notifier_with_conn(conn)
        calls = []

        with patch(
            "sentinel.services.notifier._send_telegram",
            side_effect=lambda *a, **kw: calls.append(True),
        ):
            n._handle_event({
                "type": "motion_start", "event_id": 1, "camera_id": 99,
                "camera_name": "any_cam", "started_at": "2026-05-24T10:00:00Z",
                "thumb_path": None,
            })

        assert len(calls) == 1


# ===========================================================================
# 8. Notifications DB rows: correct status written
# ===========================================================================

class TestNotificationsRows:
    def test_sent_row_written_on_success(self, tmp_path):
        conn = _make_db()
        _settings_with(
            conn,
            notify_enabled="1",
            notify_cooldown_seconds="0",
            notify_channels="ntfy",
            notify_cameras="",
            notify_quiet_start="",
            notify_quiet_end="",
            notify_ntfy_server="https://ntfy.sh",
            notify_ntfy_topic="test",
        )

        n = _notifier_with_conn(conn)

        with patch("sentinel.services.notifier._send_ntfy"):
            n._handle_event({
                "type": "motion_start", "event_id": 1, "camera_id": 1,
                "camera_name": "cam1", "started_at": "2026-05-24T10:00:00Z",
                "thumb_path": None,
            })

        row = conn.execute(
            "SELECT status, sent_at FROM notifications WHERE event_id=1 AND channel='ntfy'"
        ).fetchone()
        assert row is not None
        assert row["status"] == "sent"
        assert row["sent_at"] is not None

    def test_failed_row_written_on_exception(self, tmp_path):
        conn = _make_db()
        _settings_with(
            conn,
            notify_enabled="1",
            notify_cooldown_seconds="0",
            notify_channels="ntfy",
            notify_cameras="",
            notify_quiet_start="",
            notify_quiet_end="",
            notify_ntfy_server="https://ntfy.sh",
            notify_ntfy_topic="test",
        )

        n = _notifier_with_conn(conn)

        with patch(
            "sentinel.services.notifier._send_ntfy",
            side_effect=RuntimeError("connection refused"),
        ):
            n._handle_event({
                "type": "motion_start", "event_id": 1, "camera_id": 1,
                "camera_name": "cam1", "started_at": "2026-05-24T10:00:00Z",
                "thumb_path": None,
            })

        row = conn.execute(
            "SELECT status FROM notifications WHERE event_id=1 AND channel='ntfy'"
        ).fetchone()
        assert row is not None
        assert row["status"] == "failed"

    def test_global_disabled_writes_no_rows(self, tmp_path):
        conn = _make_db()
        _settings_with(
            conn,
            notify_enabled="0",
            notify_cooldown_seconds="0",
            notify_channels="telegram",
            notify_cameras="",
            notify_quiet_start="",
            notify_quiet_end="",
            notify_telegram_bot_token="T",
            notify_telegram_chat_id="C",
        )

        n = _notifier_with_conn(conn)

        with patch("sentinel.services.notifier._send_telegram") as m:
            n._handle_event({
                "type": "motion_start", "event_id": 1, "camera_id": 1,
                "camera_name": "cam1", "started_at": "2026-05-24T10:00:00Z",
                "thumb_path": None,
            })

        m.assert_not_called()
        row = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()
        assert row[0] == 0


# ===========================================================================
# 9. Thumbnail attached through full dispatch path
# ===========================================================================

class TestThumbnailAttached:
    def test_telegram_receives_thumb_path(self, tmp_path):
        conn = _make_db()
        thumb = _make_thumb(tmp_path)
        _settings_with(
            conn,
            notify_enabled="1",
            notify_cooldown_seconds="0",
            notify_channels="telegram",
            notify_cameras="",
            notify_quiet_start="",
            notify_quiet_end="",
            notify_telegram_bot_token="T",
            notify_telegram_chat_id="C",
        )

        n = _notifier_with_conn(conn)
        received = {}

        def fake_telegram(token, chat_id, caption, thumb_path, **kw):
            received["thumb_path"] = thumb_path

        with patch("sentinel.services.notifier._send_telegram", side_effect=fake_telegram):
            n._handle_event({
                "type": "motion_start", "event_id": 1, "camera_id": 1,
                "camera_name": "cam1", "started_at": "2026-05-24T10:00:00Z",
                "thumb_path": str(thumb),
            })

        assert received.get("thumb_path") == str(thumb)

    def test_ntfy_receives_thumb_path(self, tmp_path):
        conn = _make_db()
        thumb = _make_thumb(tmp_path)
        _settings_with(
            conn,
            notify_enabled="1",
            notify_cooldown_seconds="0",
            notify_channels="ntfy",
            notify_cameras="",
            notify_quiet_start="",
            notify_quiet_end="",
            notify_ntfy_server="https://ntfy.sh",
            notify_ntfy_topic="test",
        )

        n = _notifier_with_conn(conn)
        received = {}

        def fake_ntfy(server, topic, message, title, thumb_path, **kw):
            received["thumb_path"] = thumb_path

        with patch("sentinel.services.notifier._send_ntfy", side_effect=fake_ntfy):
            n._handle_event({
                "type": "motion_start", "event_id": 1, "camera_id": 1,
                "camera_name": "cam1", "started_at": "2026-05-24T10:00:00Z",
                "thumb_path": str(thumb),
            })

        assert received.get("thumb_path") == str(thumb)


# ===========================================================================
# 10. CooldownTracker unit tests
# ===========================================================================

class TestCooldownTracker:
    def test_not_suppressed_initially(self):
        from sentinel.services.notifier import CooldownTracker
        ct = CooldownTracker()
        assert not ct.is_suppressed(1, "telegram", 60)

    def test_suppressed_after_mark(self):
        from sentinel.services.notifier import CooldownTracker
        ct = CooldownTracker()
        ct.mark_sent(1, "telegram")
        assert ct.is_suppressed(1, "telegram", 60)

    def test_not_suppressed_after_expiry(self):
        from sentinel.services.notifier import CooldownTracker
        ct = CooldownTracker()
        ct.mark_sent(1, "telegram")
        # Manually backdate the timestamp
        ct._last_sent[(1, "telegram")] = time.monotonic() - 61
        assert not ct.is_suppressed(1, "telegram", 60)

    def test_different_channels_independent(self):
        from sentinel.services.notifier import CooldownTracker
        ct = CooldownTracker()
        ct.mark_sent(1, "telegram")
        assert not ct.is_suppressed(1, "ntfy", 60)

    def test_different_cameras_independent(self):
        from sentinel.services.notifier import CooldownTracker
        ct = CooldownTracker()
        ct.mark_sent(1, "telegram")
        assert not ct.is_suppressed(2, "telegram", 60)


# ===========================================================================
# 11. NotifyRules
# ===========================================================================

class TestNotifyRules:
    def test_camera_ids_parsed(self):
        from sentinel.services.notifier import NotifyRules
        rules = NotifyRules({
            "notify_enabled": "1",
            "notify_cameras": "1,2,5",
            "notify_channels": "telegram",
            "notify_cooldown_seconds": "60",
            "notify_quiet_start": "",
            "notify_quiet_end": "",
        })
        assert rules.camera_ids == {1, 2, 5}
        assert rules.camera_allowed(1)
        assert rules.camera_allowed(5)
        assert not rules.camera_allowed(3)

    def test_empty_cameras_allows_all(self):
        from sentinel.services.notifier import NotifyRules
        rules = NotifyRules({
            "notify_enabled": "1",
            "notify_cameras": "",
            "notify_channels": "telegram",
            "notify_cooldown_seconds": "60",
            "notify_quiet_start": "",
            "notify_quiet_end": "",
        })
        assert rules.camera_ids is None
        assert rules.camera_allowed(999)

    def test_channels_parsed(self):
        from sentinel.services.notifier import NotifyRules
        rules = NotifyRules({
            "notify_enabled": "1",
            "notify_cameras": "",
            "notify_channels": "telegram,email",
            "notify_cooldown_seconds": "60",
            "notify_quiet_start": "",
            "notify_quiet_end": "",
        })
        assert "telegram" in rules.channels
        assert "email" in rules.channels
        assert "ntfy" not in rules.channels

    def test_overnight_quiet_hours(self):
        """Verify the overnight quiet-hour arithmetic in NotifyRules.in_quiet_hours()."""
        from sentinel.services.notifier import NotifyRules
        from datetime import time as dtime

        # Build rules for overnight window 22:00 – 07:00
        base_settings = {
            "notify_enabled":          "1",
            "notify_cameras":          "",
            "notify_channels":         "telegram",
            "notify_cooldown_seconds": "60",
            "notify_quiet_start":      "22:00",
            "notify_quiet_end":        "07:00",
        }
        rules = NotifyRules(base_settings)

        # Verify parsing
        assert rules.quiet_start == dtime(22, 0)
        assert rules.quiet_end   == dtime(7, 0)

        # Test the overnight logic by driving it with patched datetime.now()
        # The method does: datetime.now().time().replace(second=0, microsecond=0)
        # We patch at the module level so NotifyRules picks it up.
        def _fake_now(hour: int):
            """Return a fake datetime whose .time().replace(...) yields dtime(hour,0)."""
            from unittest.mock import MagicMock
            fake = MagicMock()
            fake.time.return_value.replace.return_value = dtime(hour, 0)
            return fake

        with patch("sentinel.services.notifier.datetime") as mock_dt:
            # 23:00 — should be IN quiet hours (>= 22:00)
            mock_dt.now.return_value = _fake_now(23)
            assert rules.in_quiet_hours(), "23:00 should be in overnight quiet window"

            # 06:00 — should be IN quiet hours (< 07:00)
            mock_dt.now.return_value = _fake_now(6)
            assert rules.in_quiet_hours(), "06:00 should be in overnight quiet window"

            # 12:00 — should NOT be in quiet hours
            mock_dt.now.return_value = _fake_now(12)
            assert not rules.in_quiet_hours(), "12:00 should NOT be in overnight quiet window"


# ===========================================================================
# 12. seed_settings: defaults written; env values applied
# ===========================================================================

class TestSeedSettings:
    def test_defaults_written_for_new_db(self):
        from sentinel.services.notifier import seed_settings
        conn = _make_db()
        seed_settings(conn)
        rows = {r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM settings").fetchall()}
        assert "notify_enabled" in rows
        assert "notify_cooldown_seconds" in rows
        assert rows["notify_enabled"] == "1"

    def test_existing_values_preserved(self):
        from sentinel.services.notifier import seed_settings
        conn = _make_db()
        conn.execute("INSERT INTO settings(key,value) VALUES('notify_enabled','0')")
        conn.commit()
        seed_settings(conn)
        row = conn.execute("SELECT value FROM settings WHERE key='notify_enabled'").fetchone()
        # seed should NOT overwrite existing value
        assert row["value"] == "0"
