"""
sentinel/util/hikvision.py — Hikvision RTSP URL builder and ISAPI helpers.

URL scheme:
  main: rtsp://<user>:<pass>@<host>:<port>/Streaming/Channels/<channel*10+1>
  sub:  rtsp://<user>:<pass>@<host>:<port>/Streaming/Channels/<channel*10+2>

The ISAPI snapshot endpoint:
  http://<host>/ISAPI/Streaming/channels/<channel*10+1>/picture
"""

from __future__ import annotations

from urllib.parse import quote


def build_rtsp_url(
    host: str,
    user: str,
    password: str,
    path: str,
    port: int = 554,
) -> str:
    """
    Build a full RTSP URL from components.

    Args:
        host:     Camera IP or hostname.
        user:     RTSP username.
        password: RTSP password (plaintext — do NOT log this).
        path:     Stream path, e.g. '/Streaming/Channels/101'.
        port:     RTSP port (default 554).
    """
    user_enc = quote(user, safe="")
    pass_enc = quote(password, safe="")
    path = path if path.startswith("/") else f"/{path}"
    return f"rtsp://{user_enc}:{pass_enc}@{host}:{port}{path}"


def build_main_rtsp_url(
    host: str,
    user: str,
    password: str,
    channel: int = 1,
    port: int = 554,
    main_path: str | None = None,
) -> str:
    """Return the main-stream RTSP URL for a Hikvision camera."""
    path = main_path or f"/Streaming/Channels/{channel * 10 + 1}"
    return build_rtsp_url(host, user, password, path, port)


def build_sub_rtsp_url(
    host: str,
    user: str,
    password: str,
    channel: int = 1,
    port: int = 554,
    sub_path: str | None = None,
) -> str:
    """Return the sub-stream RTSP URL for a Hikvision camera."""
    path = sub_path or f"/Streaming/Channels/{channel * 10 + 2}"
    return build_rtsp_url(host, user, password, path, port)


def build_snapshot_url(host: str, channel: int = 1) -> str:
    """Return the ISAPI snapshot URL (HTTP, auth handled separately)."""
    return f"http://{host}/ISAPI/Streaming/channels/{channel * 10 + 1}/picture"


def build_go2rtc_stream_name(camera_name: str, sub: bool = False) -> str:
    """
    Return a safe go2rtc stream key for a camera.
    e.g. 'front_door' or 'front_door_sub'.
    """
    slug = camera_name.lower().replace(" ", "_").replace("-", "_")
    # Strip non-alphanumeric/underscore characters.
    slug = "".join(c for c in slug if c.isalnum() or c == "_")
    return f"{slug}_sub" if sub else slug
