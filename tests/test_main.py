"""Tests for loop-level error handling.

The distinction under test: a transient failure should be retried on the normal
interval, while a dead OAuth grant should not -- only a human at a browser can
fix that one, so retrying fast just buries the log line that says so.
"""

import logging
from types import SimpleNamespace

import pytest

from videobackup import main
from videobackup.rclone import RcloneAuthError


class _FakeStop:
    """A stop event that records wait durations and ends the loop."""

    def __init__(self, stop_after=1):
        self.waits = []
        self._calls = 0
        self._stop_after = stop_after
        self._set = False

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self._calls += 1
        if self._calls >= self._stop_after:
            self._set = True
        return self._set


def _cfg(tmp_path):
    enc = tmp_path / "encrypted"
    enc.mkdir()
    (enc / "seg.gpg").write_bytes(b"x" * 10)  # non-empty: forces the prune path
    raw = tmp_path / "raw"
    raw.mkdir()
    return SimpleNamespace(
        spool_encrypted=enc,
        spool_raw=raw,
        upload_slice_bytes=1 << 30,
        batch_interval_seconds=30,
        max_spool_bytes=0,
        max_drive_bytes=10**9,
    )


def _boom(*_a, **_k):
    raise RcloneAuthError("grant is gone; reconnect with a trailing colon.")


@pytest.fixture
def quiet_spool(monkeypatch):
    monkeypatch.setattr(main, "prune_spool", lambda _c: 0)
    monkeypatch.setattr(main, "prune_raw_spool", lambda _c: 0)


def test_upload_loop_backs_off_on_auth_error(monkeypatch, tmp_path, quiet_spool):
    monkeypatch.setattr(main, "prune", _boom)
    stop = _FakeStop()
    main.run_upload_loop(_cfg(tmp_path), stop)
    assert stop.waits == [main._AUTH_RETRY_SECONDS]


def test_upload_loop_retries_fast_on_a_transient_error(
    monkeypatch, tmp_path, quiet_spool
):
    def _transient(*_a, **_k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(main, "prune", _transient)
    stop = _FakeStop()
    main.run_upload_loop(_cfg(tmp_path), stop)
    assert stop.waits == [main._UPLOAD_IDLE_POLL_SECONDS]


def test_auth_error_is_logged_without_a_traceback(
    monkeypatch, tmp_path, quiet_spool, caplog
):
    # Nothing here is a bug in this program, so a traceback is noise that
    # hides the one actionable sentence.
    monkeypatch.setattr(main, "prune", _boom)
    with caplog.at_level(logging.ERROR):
        main.run_upload_loop(_cfg(tmp_path), _FakeStop())
    records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(records) == 1
    assert records[0].exc_info is None
    assert "trailing colon" in records[0].getMessage()


def test_transient_error_keeps_its_traceback(
    monkeypatch, tmp_path, quiet_spool, caplog
):
    # The contrast: an unexpected failure might be our bug, so the traceback
    # is the useful part and must survive.
    def _transient(*_a, **_k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(main, "prune", _transient)
    with caplog.at_level(logging.ERROR):
        main.run_upload_loop(_cfg(tmp_path), _FakeStop())
    records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(records) == 1
    assert records[0].exc_info is not None


def test_batch_loop_backs_off_on_auth_error(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "run_batch", _boom)
    stop = _FakeStop()
    main.run_batch_loop(_cfg(tmp_path), stop)
    assert stop.waits == [main._AUTH_RETRY_SECONDS]


def test_cli_reports_auth_failure_as_a_message_not_a_crash(
    monkeypatch, tmp_path, caplog
):
    # `videobackup prune` on a dead grant used to dump a traceback, which reads
    # as a bug in this program rather than an expired token.
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("")
    monkeypatch.setattr(main, "load_config", lambda _p: _cfg(tmp_path))
    monkeypatch.setattr(main, "prune", _boom)
    with caplog.at_level(logging.ERROR):
        rc = main.main(["-c", str(cfg_path), "prune"])
    assert rc == main.EXIT_AUTH
    assert rc != main.EXIT_CONFIG  # distinguishable from a bad config file
    assert "trailing colon" in caplog.text


def test_batch_loop_retries_fast_on_a_transient_error(monkeypatch, tmp_path):
    def _transient(_c):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(main, "run_batch", _transient)
    cfg = _cfg(tmp_path)
    stop = _FakeStop()
    main.run_batch_loop(cfg, stop)
    assert stop.waits == [cfg.batch_interval_seconds]
