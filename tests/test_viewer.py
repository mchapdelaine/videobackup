"""Tests for the GUI-free bits of the decrypt/view tool.

The Tk UI needs a display, but the decrypt helper is a plain function with the
subprocess call mocked out, so these run headless in CI.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from videobackup import viewer


class _FakeCompleted:
    def __init__(self, returncode: int, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


def _fake_run(returncode: int, stderr: str = ""):
    """Return a subprocess.run stand-in yielding a fixed result."""

    def run(_cmd, **_kwargs):
        return _FakeCompleted(returncode, stderr)

    return run


def test_decrypt_success_returns_temp_with_real_suffix(monkeypatch):
    monkeypatch.setattr(viewer.subprocess, "run", _fake_run(0))
    out = viewer.decrypt_to_temp(Path("front_door_20260716.mp4.gpg"))
    try:
        assert out.exists()
        assert out.suffix == ".mp4"  # .gpg stripped, real ext preserved
        assert out.name.startswith("videobackup-view-")
    finally:
        out.unlink(missing_ok=True)


def test_decrypt_defaults_suffix_when_none(monkeypatch):
    monkeypatch.setattr(viewer.subprocess, "run", _fake_run(0))
    out = viewer.decrypt_to_temp(Path("clip.gpg"))  # stem "clip" has no suffix
    try:
        assert out.suffix == ".mp4"
    finally:
        out.unlink(missing_ok=True)


def test_decrypt_failure_raises_and_cleans_up(monkeypatch, tmp_path):
    # Pin the temp file into tmp_path so we can assert it was removed.
    made = tmp_path / "videobackup-view-x.mp4"

    def fake_mkstemp(*_a, **_k):
        fd = os.open(made, os.O_CREAT | os.O_WRONLY, 0o600)
        return (fd, str(made))

    monkeypatch.setattr(viewer.tempfile, "mkstemp", fake_mkstemp)
    monkeypatch.setattr(viewer.subprocess, "run", _fake_run(2, "no secret key"))

    with pytest.raises(RuntimeError, match="no secret key"):
        viewer.decrypt_to_temp(Path("clip.mp4.gpg"))
    assert not made.exists()  # temp plaintext cleaned up on failure


def test_decrypt_failure_uses_fallback_message(monkeypatch):
    monkeypatch.setattr(viewer.subprocess, "run", _fake_run(1, ""))
    with pytest.raises(RuntimeError, match="gpg decrypt failed"):
        viewer.decrypt_to_temp(Path("clip.mp4.gpg"))
