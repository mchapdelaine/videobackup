"""Tests for the rclone move argv builder (pure)."""

from types import SimpleNamespace

from videobackup.uploader import _rclone_move_args


def _cfg(**over):
    base = dict(
        spool_encrypted="/spool/encrypted",
        remote_path="gdrive:unifi-backup",
        upload_transfers=4,
        upload_tpslimit=0,
        upload_slice_bytes=1 << 30,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _val(args, flag):
    return args[args.index(flag) + 1]


def test_move_args_core():
    args = _rclone_move_args(_cfg())
    assert args[:3] == ["move", "/spool/encrypted", "gdrive:unifi-backup"]
    assert _val(args, "--include") == "*.gpg"
    assert _val(args, "--transfers") == "4"
    assert _val(args, "--checkers") == "4"


def test_move_args_include_stall_hardening():
    args = _rclone_move_args(_cfg())
    # These are what stop a wedged connection from hanging the loop forever.
    assert _val(args, "--timeout") == "120s"
    assert _val(args, "--contimeout") == "30s"
    assert _val(args, "--low-level-retries") == "10"


def test_move_args_no_tpslimit_when_zero():
    args = _rclone_move_args(_cfg(upload_tpslimit=0))
    assert "--tpslimit" not in args


def test_move_args_tpslimit_when_set():
    args = _rclone_move_args(_cfg(upload_tpslimit=10))
    assert _val(args, "--tpslimit") == "10"


def test_move_args_respects_transfers():
    args = _rclone_move_args(_cfg(upload_transfers=2))
    assert _val(args, "--transfers") == "2"
    assert _val(args, "--checkers") == "2"


def test_move_args_bounds_the_cycle():
    # Unbounded, one rclone move over a large spool runs for hours and starves
    # the prune that shares its thread.
    args = _rclone_move_args(_cfg(upload_slice_bytes=5 * 2**30))
    assert _val(args, "--max-transfer") == str(5 * 2**30)
    assert _val(args, "--cutoff-mode") == "soft"


def test_move_args_uploads_newest_first():
    # Default listing order is by name, which starves whichever camera sorts
    # last; by modtime the cameras drain fairly and latest footage wins.
    args = _rclone_move_args(_cfg())
    assert _val(args, "--order-by") == "modtime,desc"


def test_move_args_request_a_final_summary():
    # Without this rclone is silent on success, hiding a cycle that retried
    # most of its bytes away.
    args = _rclone_move_args(_cfg())
    assert _val(args, "--stats") == "1000h"  # long enough to never fire early
    assert _val(args, "--stats-log-level") == "NOTICE"
