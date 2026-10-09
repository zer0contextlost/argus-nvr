"""
sentinel/config.py — Load .env, define path roots, tool paths, and create
required directories on startup.

Paths are derived from ARGUS_DATA_ROOT (or inferred from this file's location
as 3 dirs up — works for both the repo layout and the installer layout).
Individual paths can always be overridden by explicit env vars.

.env file location: ARGUS_ENV_FILE env var > .env next to the app dir.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Computed once at import time — the directory containing this file.
_THIS_FILE = Path(__file__).resolve()

# Default data root: 3 levels up from sentinel/config.py.
# Layout: <root>/app/sentinel/config.py  → <root> is the data root.
# Windows install (C:\Sentinel\app\sentinel\config.py) → C:\Sentinel
# Linux install (/opt/sentinel/app/sentinel/config.py) → /opt/sentinel
_DEFAULT_DATA_ROOT = _THIS_FILE.parent.parent.parent


def _resolve_env_file() -> str:
    """Return the .env path to load: ARGUS_ENV_FILE > .env next to app dir."""
    explicit = os.environ.get("ARGUS_ENV_FILE", "").strip()
    if explicit:
        return explicit
    default = _THIS_FILE.parent.parent / ".env"
    return str(default)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_resolve_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # --- Root dirs -----------------------------------------------------------
    # Set ARGUS_DATA_ROOT to override the inferred default.
    sentinel_data_root: Path = Field(_DEFAULT_DATA_ROOT, validation_alias="argus_data_root")

    # Optional: if set, tool path defaults derive from here.
    # Defaults to <data_root>/tools if not overridden.
    sentinel_tools_root: Optional[Path] = Field(None, validation_alias="argus_tools_root")

    # --- Data paths (all optional; derived from argus_data_root) ------------
    sentinel_recordings_dir: Optional[Path] = Field(None, validation_alias="argus_recordings_dir")
    sentinel_events_dir: Optional[Path] = Field(None, validation_alias="argus_events_dir")
    sentinel_db_path: Optional[Path] = Field(None, validation_alias="argus_db_path")
    sentinel_log_dir: Optional[Path] = Field(None, validation_alias="argus_log_dir")

    # --- Tool paths (optional; derived from argus_tools_root) ----------------
    ffmpeg_path: Optional[Path] = None
    ffprobe_path: Optional[Path] = None
    go2rtc_path: Optional[Path] = None

    # --- App server ----------------------------------------------------------
    sentinel_host: str = Field("0.0.0.0", validation_alias="argus_host")
    sentinel_port: int = Field(8080, validation_alias="argus_port")
    sentinel_secret_key: str = Field("change-me", validation_alias="argus_secret_key")
    # Separate key for Fernet camera-password encryption. If set, rotating
    # ARGUS_SECRET_KEY (for JWT invalidation) won't affect stored passwords.
    # Falls back to deriving from sentinel_secret_key if not set (backward compat).
    sentinel_fernet_key: Optional[str] = Field(None, validation_alias="argus_fernet_key")

    # --- go2rtc ports --------------------------------------------------------
    go2rtc_api_port: int = 1984
    go2rtc_rtsp_port: int = 8554
    go2rtc_webrtc_port: int = 8555

    # --- Recording -----------------------------------------------------------
    segment_seconds: int = 120
    default_record_mode: str = "motion-only"
    motion_pre_buffer_seconds: int = 5
    motion_post_buffer_seconds: int = 10

    # --- Storage retention ---------------------------------------------------
    retention_max_age_days: int = 30
    retention_free_space_floor_pct: int = 10
    # Hard cap on total recording size (SUM of recordings.size_bytes), oldest
    # deleted first regardless of event links. 0 = no cap. Set in .env as
    # RETENTION_MAX_TOTAL_GB.
    retention_max_total_gb: float = 0
    retention_event_min_age_days: int = 90  # event-linked recordings kept at least this long

    # --- Backup --------------------------------------------------------------
    backup_keep_count: int = 7             # number of daily backups to keep

    # --- Alerting ------------------------------------------------------------
    notify_storage_warn_pct: int = 20      # alert when free disk drops below this %
    camera_offline_alert_minutes: int = 10  # alert when camera unreachable this long

    # --- Detection -----------------------------------------------------------
    detect_enabled: bool = False
    detect_fps: int = 5
    detect_min_confidence: float = 0.45
    detect_classes: str = "person,car,truck,bicycle,dog,cat"
    detect_provider: str = "auto"
    yolo_model_path: Path = Path("bin/yolov8n.onnx")

    # --- Notifications -------------------------------------------------------
    notify_telegram_bot_token: Optional[str] = None
    notify_telegram_chat_id: Optional[str] = None
    notify_ntfy_topic: Optional[str] = None
    notify_ntfy_server: str = "https://ntfy.sh"
    notify_smtp_host: Optional[str] = None
    notify_smtp_port: int = 587
    notify_smtp_user: Optional[str] = None
    notify_smtp_pass: Optional[str] = None
    notify_smtp_to: Optional[str] = None

    # -------------------------------------------------------------------------
    # Derived path validator — runs after all env/file values are loaded.
    # Only fills in None fields; explicit env-var overrides are preserved.
    # -------------------------------------------------------------------------

    @model_validator(mode="after")
    def _check_secret_key(self) -> "Settings":
        if self.sentinel_secret_key == "change-me":
            import warnings
            warnings.warn(
                "sentinel_secret_key is set to the default 'change-me'. "
                "Set ARGUS_SECRET_KEY in your environment before going to production.",
                stacklevel=2,
            )
        return self

    @model_validator(mode="after")
    def _derive_paths(self) -> "Settings":
        root = self.sentinel_data_root

        # Data paths
        if self.sentinel_recordings_dir is None:
            self.sentinel_recordings_dir = root / "recordings"
        if self.sentinel_events_dir is None:
            self.sentinel_events_dir = root / "events"
        if self.sentinel_db_path is None:
            self.sentinel_db_path = root / "data" / "sentinel.db"
        if self.sentinel_log_dir is None:
            self.sentinel_log_dir = root / "logs"

        # Tools root
        if self.sentinel_tools_root is None:
            self.sentinel_tools_root = root / "tools"
        tools = self.sentinel_tools_root

        # Tool binaries — platform-aware defaults
        if self.ffmpeg_path is None:
            if sys.platform == "win32":
                self.ffmpeg_path = tools / "ffmpeg" / "bin" / "ffmpeg.exe"
            else:
                candidate = tools / "ffmpeg"
                self.ffmpeg_path = candidate if candidate.exists() else Path("ffmpeg")

        if self.ffprobe_path is None:
            if sys.platform == "win32":
                self.ffprobe_path = tools / "ffmpeg" / "bin" / "ffprobe.exe"
            else:
                candidate = tools / "ffprobe"
                self.ffprobe_path = candidate if candidate.exists() else Path("ffprobe")

        if self.go2rtc_path is None:
            if sys.platform == "win32":
                self.go2rtc_path = tools / "go2rtc.exe"
            else:
                self.go2rtc_path = tools / "go2rtc"

        return self

    # -------------------------------------------------------------------------
    # Derived properties (not configurable via env)
    # -------------------------------------------------------------------------

    @property
    def go2rtc_config_path(self) -> Path:
        # Always lives next to the app source, not in the data root.
        # <install>/app/sentinel/config.py → <install>/app/config/go2rtc.yaml
        return _THIS_FILE.parent.parent / "config" / "go2rtc.yaml"

    @property
    def go2rtc_log_path(self) -> Path:
        return self.sentinel_log_dir / "go2rtc.log"

    @property
    def app_log_path(self) -> Path:
        return self.sentinel_log_dir / "sentinel.log"

    @property
    def detect_class_list(self) -> list[str]:
        return [c.strip() for c in self.detect_classes.split(",") if c.strip()]

    def create_dirs(self) -> None:
        """Create all required runtime directories (idempotent)."""
        dirs = [
            self.sentinel_data_root,
            self.sentinel_recordings_dir,
            self.sentinel_events_dir,
            self.sentinel_log_dir,
            self.sentinel_db_path.parent,
            self.go2rtc_config_path.parent,
            self.sentinel_data_root / "backups",
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)


# Singleton — imported everywhere as `from sentinel.config import settings`
settings = Settings()
