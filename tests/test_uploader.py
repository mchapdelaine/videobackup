"""Tests for the rclone move argv builder and stderr summary (pure)."""

from types import SimpleNamespace

from videobackup.uploader import _rclone_move_args, _summarize_rclone


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


# -- stderr summary parsing ----------------------------------------------

_CLEAN = """
Transferred:   	    1.234 GiB / 1.234 GiB, 100%, 12.000 MiB/s, ETA -
Transferred:           40 / 40, 100%
Elapsed time:      1m30.0s
"""

_THROTTLED = """
ERROR : seg.gpg: Failed to copy: googleapi: Error 403: Quota exceeded for
 quota metric 'Queries' and limit 'Requests per minute', rateLimitExceeded

Transferred:   	  512.000 MiB / 1.234 GiB, 40%, 3.000 MiB/s, ETA 4m
Errors:                 7 (retrying may help)
Transferred:           12 / 40, 30%
Elapsed time:      2m0.0s
"""


# Verbatim from a live failure: Drive's "account is full" 403.
_FULL = """
ERROR : cbc_20260912_080716.ts.gpg: Failed to copy: googleapi: Error 403: The \
user's Drive storage quota has been exceeded., storageQuotaExceeded
ERROR : Attempt 1/3 failed with 1 errors and: googleapi: Error 403: The user's \
Drive storage quota has been exceeded., storageQuotaExceeded
Transferred:              0 B / 0 B, -, 0 B/s, ETA -
Errors:                 1 (retrying may help)
Elapsed time:         2.5s
"""


def test_summarize_clean_run():
    assert _summarize_rclone(_CLEAN) == (0, False, False)


def test_summarize_counts_errors_and_flags_throttling():
    outcome = _summarize_rclone(_THROTTLED)
    assert outcome.errors == 7
    assert outcome.rate_limited is True
    assert outcome.out_of_space is False


def test_summarize_detects_throttling_without_error_line():
    # The dangerous case: rclone retries past the 403s, so the stats block has
    # no Errors line and the exit code is 0 -- but bytes were still discarded.
    outcome = _summarize_rclone(
        "NOTICE: low level retry 1/10: rateLimitExceeded\n" + _CLEAN
    )
    assert outcome.errors == 0
    assert outcome.rate_limited is True


def test_summarize_flags_out_of_space():
    outcome = _summarize_rclone(_FULL)
    assert outcome.errors == 1
    assert outcome.out_of_space is True


def test_summarize_does_not_mistake_a_full_account_for_throttling():
    # Both are 403s. Reporting "out of space" as throttling would print the
    # client_id advice, which is irrelevant, and imply retrying fixes it.
    assert _summarize_rclone(_FULL).rate_limited is False


def test_summarize_does_not_mistake_throttling_for_a_full_account():
    assert _summarize_rclone(_THROTTLED).out_of_space is False


def test_summarize_empty_stderr():
    assert _summarize_rclone("") == (0, False, False)


def test_summarize_ignores_unrelated_403_free_text():
    assert _summarize_rclone("Transferred: 1 / 1, 100%\n") == (0, False, False)
