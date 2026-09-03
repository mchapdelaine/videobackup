"""Tests for the scheme-aware ffmpeg command builder."""

from pathlib import Path

from videobackup.config import Camera
from videobackup.recorder import _ffmpeg_cmd


def _cmd(url: str) -> list[str]:
    return _ffmpeg_cmd(Camera(name="cam", url=url), Path("/spool/raw"), 300)


def test_rtsp_uses_tcp_transport_and_mp4():
    cmd = _cmd("rtsp://10.0.0.1:7447/xyz")
    assert "-rtsp_transport" in cmd and "tcp" in cmd
    assert "-reconnect" not in cmd
    # segment_format is the token right after -segment_format
    assert cmd[cmd.index("-segment_format") + 1] == "mp4"
    assert cmd[-1].endswith(".mp4")
    assert "rtsp://10.0.0.1:7447/xyz" in cmd


def test_http_uses_reconnect_and_mpegts():
    cmd = _cmd("http://192.168.1.50:5004/auto/v5.1")
    # HDHomeRun: no RTSP-only flags (they'd make ffmpeg error out).
    assert "-rtsp_transport" not in cmd
    assert "-reconnect" in cmd and "-reconnect_streamed" in cmd
    assert cmd[cmd.index("-segment_format") + 1] == "mpegts"
    assert cmd[-1].endswith(".ts")
    assert "http://192.168.1.50:5004/auto/v5.1" in cmd


def test_https_treated_as_http():
    cmd = _cmd("https://host/stream")
    assert "-reconnect" in cmd
    assert cmd[-1].endswith(".ts")


def test_unknown_scheme_falls_back_to_rtsp_path():
    # Anything not http(s) keeps the original RTSP-style flags + mp4.
    cmd = _cmd("rtsps://host/stream")
    assert "-rtsp_transport" in cmd
    assert cmd[-1].endswith(".mp4")
