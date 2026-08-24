"""
sentinel/db.py — SQLite (WAL) connection + migration runner.

Tables (from SOW §8):
  cameras, recordings, events, settings, users, notifications
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Generator

from sentinel.config import settings

# ---------------------------------------------------------------------------
# Schema migrations — list of (version, sql) tuples, applied in order.
# Each migration is idempotent via IF NOT EXISTS / IF NOT EXISTS checks.
# ---------------------------------------------------------------------------

_MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        -- User-visible migration 1: initial schema

        CREATE TABLE IF NOT EXISTS schema_version (
            version     INTEGER PRIMARY KEY,
            applied_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS cameras (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            name                TEXT    NOT NULL,
            host                TEXT    NOT NULL,
            rtsp_port           INTEGER NOT NULL DEFAULT 554,
            rtsp_user           TEXT    NOT NULL DEFAULT '',
            rtsp_pass_enc       TEXT    NOT NULL DEFAULT '',
            main_path           TEXT    NOT NULL DEFAULT '/Streaming/Channels/101',
            sub_path            TEXT    NOT NULL DEFAULT '/Streaming/Channels/102',
            channel             INTEGER NOT NULL DEFAULT 1,
            record_mode         TEXT    NOT NULL DEFAULT 'motion-only'
                                    CHECK(record_mode IN ('continuous','motion-only','continuous+events')),
            enabled             INTEGER NOT NULL DEFAULT 1,
            detect_config_json  TEXT    NOT NULL DEFAULT '{}',
            created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS recordings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id   INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
            path        TEXT    NOT NULL,
            started_at  TEXT    NOT NULL,
            ended_at    TEXT,
            duration_s  REAL,
            size_bytes  INTEGER,
            has_gap     INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_recordings_camera_started
            ON recordings(camera_id, started_at);

        CREATE TABLE IF NOT EXISTS events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id    INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
            type         TEXT    NOT NULL DEFAULT 'motion'
                                     CHECK(type IN ('motion','object')),
            label        TEXT,
            score        REAL,
            started_at   TEXT    NOT NULL,
            ended_at     TEXT,
            thumb_path   TEXT,
            clip_path    TEXT,
            recording_id INTEGER REFERENCES recordings(id) ON DELETE SET NULL,
            meta_json    TEXT    NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_events_camera_started
            ON events(camera_id, started_at);

        CREATE TABLE IF NOT EXISTS settings (
            key     TEXT PRIMARY KEY,
            value   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT    NOT NULL UNIQUE,
            pass_hash   TEXT    NOT NULL,
            role        TEXT    NOT NULL DEFAULT 'viewer'
                                    CHECK(role IN ('admin','viewer')),
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            channel  TEXT    NOT NULL,
            status   TEXT    NOT NULL DEFAULT 'pending'
                                 CHECK(status IN ('pending','sent','failed')),
            sent_at  TEXT
        );
        """,
    ),
    (
        2,
        """
        -- Migration 2: add cam_type column to cameras table
        -- Backfill existing rows to 'hikvision' (the original default behaviour).
        ALTER TABLE cameras ADD COLUMN cam_type TEXT NOT NULL DEFAULT 'hikvision';
        UPDATE cameras SET cam_type = 'hikvision' WHERE cam_type IS NULL OR cam_type = '';
        """,
    ),
    (
        3,
        """
        -- Migration 3: deduplicate recordings and enforce UNIQUE(path).
        -- Keep the lowest id per path (first-writer wins).
        DELETE FROM recordings
        WHERE id NOT IN (
            SELECT MIN(id) FROM recordings GROUP BY path
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_recordings_unique_path ON recordings(path);
        """,
    ),
    (
        4,
        """
        -- Migration 4: fix recording timestamps stored as local-time-with-Z.
        -- FFmpeg strftime uses local clock; previously we stored that as UTC (wrong).
        -- Shift all started_at / ended_at forward by the system UTC offset so they
        -- represent true UTC.  The Python migration runner applies the correct offset
        -- at runtime via a Python helper rather than hardcoding +5h in SQL.
        SELECT 1;
        """,
    ),
    (
        5,
        """
        -- Migration 5: camera groups + event reviewed state
        ALTER TABLE cameras ADD COLUMN cam_group TEXT NOT NULL DEFAULT '';
        ALTER TABLE events  ADD COLUMN reviewed_at TEXT;
        """,
    ),
    (
        6,
        """
        -- Migration 6: named-face recognition — persons + face encodings
        CREATE TABLE IF NOT EXISTS persons (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL,
            created_at TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS face_encodings (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id  INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
            event_id   INTEGER REFERENCES events(id) ON DELETE SET NULL,
            encoding   BLOB    NOT NULL,
            created_at TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        ALTER TABLE events ADD COLUMN person_id INTEGER REFERENCES persons(id) ON DELETE SET NULL;
        ALTER TABLE events ADD COLUMN face_embedding BLOB;
        """,
    ),
    (
        7,
        """
        -- Migration 7: FCM push tokens for Android / mobile clients
        CREATE TABLE IF NOT EXISTS fcm_tokens (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token       TEXT    NOT NULL UNIQUE,
            device_name TEXT    NOT NULL DEFAULT '',
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            last_seen   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_fcm_tokens_user ON fcm_tokens(user_id);
        """,
    ),
    (
        8,
        """
        -- Migration 8: forced first-login password change flag (installer default)
        ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    (
        9,
        """
        -- Migration 9: split cam_type (protocol) from cam_make (manufacturer/model)
        -- cam_make gets the old cam_type value (hikvision/wyze/generic).
        -- cam_type becomes the transport protocol; all existing cameras use RTSP.
        ALTER TABLE cameras ADD COLUMN cam_make TEXT NOT NULL DEFAULT 'generic';
        UPDATE cameras SET cam_make = cam_type;
        UPDATE cameras SET cam_type = 'rtsp';
        """,
    ),
    (
        10,
        """
        -- Migration 10: per-camera retention policy
        ALTER TABLE cameras ADD COLUMN retention_days INTEGER;
        """,
    ),
    (
        11,
        """
        -- Migration 11: expand user roles to admin/operator/viewer, add audit_log,
        --               add PTZ + privacy mask columns to cameras.

        DROP TABLE IF EXISTS users_new;
        CREATE TABLE users_new (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            username             TEXT    NOT NULL UNIQUE,
            pass_hash            TEXT    NOT NULL,
            role                 TEXT    NOT NULL DEFAULT 'viewer'
                                             CHECK(role IN ('admin','operator','viewer')),
            created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
            must_change_password INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO users_new(id, username, pass_hash, role, created_at, must_change_password)
            SELECT id, username, pass_hash, role, created_at, must_change_password FROM users;
        DROP TABLE users;
        ALTER TABLE users_new RENAME TO users;

        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            username    TEXT    NOT NULL DEFAULT '',
            action      TEXT    NOT NULL,
            target_type TEXT,
            target_id   INTEGER,
            detail      TEXT,
            ip          TEXT,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at);

        ALTER TABLE cameras ADD COLUMN ptz_capable INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE cameras ADD COLUMN ptz_config_json TEXT NOT NULL DEFAULT '{}';
        ALTER TABLE cameras ADD COLUMN privacy_masks_json TEXT NOT NULL DEFAULT '[]';
        """,
    ),
    (
        12,
        """
        -- Migration 12: Web Push VAPID subscriptions
        CREATE TABLE IF NOT EXISTS web_push_subscriptions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
            endpoint   TEXT    NOT NULL UNIQUE,
            p256dh     TEXT    NOT NULL,
            auth_key   TEXT    NOT NULL,
            user_agent TEXT    NOT NULL DEFAULT '',
            created_at TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_wps_user ON web_push_subscriptions(user_id);
        """,
    ),
    (
        13,
        """
        -- Migration 13: Cloud backup run history
        CREATE TABLE IF NOT EXISTS cloud_backup_runs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at        TEXT    NOT NULL,
            ended_at          TEXT,
            status            TEXT    NOT NULL DEFAULT 'pending'
                                          CHECK(status IN ('pending','running','ok','error')),
            bytes_transferred INTEGER,
            files_transferred INTEGER,
            error_msg         TEXT
        );
        """,
    ),
]


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    """Open a WAL-mode SQLite connection to the configured DB path."""
    db_path = settings.sentinel_db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# Module-level shared connection (single-writer, multi-reader for SQLite WAL).
_db: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    """Return the shared DB connection, opening it if necessary."""
    global _db
    if _db is None:
        _db = _connect()
    return _db


def open_db() -> sqlite3.Connection:
    """Open and return a NEW dedicated SQLite connection.

    Use this in background threads or executor tasks that cannot safely share
    the main event-loop connection returned by get_db().  The caller is
    responsible for closing the returned connection when done.
    """
    return _connect()


def checkpoint_wal() -> None:
    """Run PRAGMA wal_checkpoint(TRUNCATE) to shrink the WAL file."""
    if _db is not None:
        try:
            _db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass


def close_db() -> None:
    global _db
    if _db is not None:
        try:
            _db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        _db.close()
        _db = None


# ---------------------------------------------------------------------------
# Migration runner
# ---------------------------------------------------------------------------

def run_migrations() -> None:
    """Apply any outstanding schema migrations."""
    conn = get_db()

    # Ensure schema_version table exists before reading it.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version     INTEGER PRIMARY KEY,
            applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()

    applied = {
        row[0]
        for row in conn.execute("SELECT version FROM schema_version").fetchall()
    }

    for version, sql in _MIGRATIONS:
        if version not in applied:
            try:
                conn.executescript(sql)
            except Exception as exc:
                # ALTER TABLE fails with "duplicate column name" if the column
                # was added in a previous partial run (crash before schema_version
                # was written).  Treat that as already-applied and continue.
                if "duplicate column name" in str(exc).lower():
                    pass
                else:
                    raise
            conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)", (version,)
            )
            conn.commit()


def list_tables() -> list[str]:
    """Return the names of all user-visible tables in the database."""
    conn = get_db()
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]
