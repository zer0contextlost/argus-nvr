"""
tests/test_hikvision.py — Hikvision URL builder tests.
"""

import os
os.environ.setdefault("SENTINEL_SECRET_KEY", "test-secret-key-for-unit-tests-only")

from sentinel.util.hikvision import (
    build_rtsp_url,
    build_main_rtsp_url,
    build_sub_rtsp_url,
    build_snapshot_url,
    build_go2rtc_stream_name,
)


def test_build_rtsp_url_basic():
    url = build_rtsp_url("192.168.1.64", "admin", "pass123", "/Streaming/Channels/101")
    assert url == "rtsp://admin:pass123@192.168.1.64:554/Streaming/Channels/101"


def test_build_rtsp_url_custom_port():
    url = build_rtsp_url("10.0.0.5", "user", "pw", "/stream/1", port=8554)
    assert url == "rtsp://user:pw@10.0.0.5:8554/stream/1"


def test_build_rtsp_url_path_no_leading_slash():
    url = build_rtsp_url("192.168.1.1", "u", "p", "Streaming/Channels/101")
    assert "/Streaming/Channels/101" in url


def test_build_rtsp_url_special_chars_in_password():
    url = build_rtsp_url("192.168.1.1", "admin", "p@$$w0rd!", "/stream")
    # Password must be percent-encoded.
    assert "p%40%24%24w0rd%21" in url
    assert "@" not in url.split("@")[0].split("://")[1]  # only one unencoded @


def test_build_main_rtsp_url_default_channel():
    url = build_main_rtsp_url("192.168.1.64", "admin", "pass", channel=1)
    assert "/Streaming/Channels/11" in url


def test_build_main_rtsp_url_channel_2():
    url = build_main_rtsp_url("192.168.1.64", "admin", "pass", channel=2)
    assert "/Streaming/Channels/21" in url


def test_build_sub_rtsp_url_default_channel():
    url = build_sub_rtsp_url("192.168.1.64", "admin", "pass", channel=1)
    assert "/Streaming/Channels/12" in url


def test_build_sub_rtsp_url_custom_path():
    url = build_sub_rtsp_url(
        "192.168.1.64", "admin", "pass", sub_path="/custom/sub"
    )
    assert "/custom/sub" in url


def test_build_snapshot_url():
    url = build_snapshot_url("192.168.1.64", channel=1)
    assert url == "http://192.168.1.64/ISAPI/Streaming/channels/11/picture"


def test_build_snapshot_url_channel_2():
    url = build_snapshot_url("192.168.1.64", channel=2)
    assert "/channels/21/picture" in url


def test_build_go2rtc_stream_name_simple():
    assert build_go2rtc_stream_name("Front Door") == "front_door"


def test_build_go2rtc_stream_name_sub():
    assert build_go2rtc_stream_name("Front Door", sub=True) == "front_door_sub"


def test_build_go2rtc_stream_name_special_chars():
    name = build_go2rtc_stream_name("Cam #1 / Entry!")
    # Should only contain alphanumeric and underscore.
    assert all(c.isalnum() or c == "_" for c in name)
