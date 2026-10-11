"""
sentinel/util/logging.py -- Rotating file logging to <data_root>\\logs.

Usage:
    from sentinel.util.logging import get_logger
    log = get_logger("sentinel.api.cameras")
    log.info("camera added", extra={"camera_id": 1})
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from sentinel.config import settings

_FORMATTER = logging.Formatter(
    fmt="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

_root_configured = False


def _configure_root() -> None:
    global _root_configured
    if _root_configured:
        return

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Rotating file handler — 10 MB per file, keep 5 backups.
    log_path: Path = settings.app_log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fh = logging.handlers.RotatingFileHandler(
        filename=str(log_path),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_FORMATTER)

    # Console handler — INFO and above.
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(_FORMATTER)

    root.addHandler(fh)
    root.addHandler(ch)

    _root_configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a named logger; configure the root logger on first call."""
    _configure_root()
    return logging.getLogger(name)
