"""Tests for the ffmpeg argv builder and the segment-stall watchdog (pure)."""

import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from videobackup.recorder import (
    _ffmpeg_cmd,
    _segment_opened_at,
    _stalled_segment,
)


def _cam(name="cbc", scheme="http", url="http://10.0.0.1:5004/auto/v9.1"):
    return SimpleNamespace(name=name, scheme=scheme, url=url)


def _val(args, flag):
    return args[args.index(flag) + 1]


# -- argv ----------------------------------------------------------------


def test_http_source_segments_to_mpegts():
    args = _ffmpeg_cmd(_cam(), out_dir=Path("/raw"), segment_seconds=60)
    assert _val(args, "-segment_format") == "mpegts"
    assert _val(args, "-segment_time") == "60"
    assert args[-1].endswith("cbc_%Y%m%d_%H%M%S.ts")


def test_http_source_hardens_timestamps():
    # Without these, a run of corrupt packets (dts = NOPTS) stops the segment
    # muxer from ever seeing the next boundary and one file grows unbounded.
    args = _ffmpeg_cmd(_cam(), out_dir=Path("/raw"), segment_seconds=60)
    assert _val(args, "-fflags") == "+genpts+discardcorrupt"
    # Input flags only take effect before -i.
    assert args.index("-fflags") < args.index("-i")


def test_rtsp_source_unchanged_by_the_http_hardening():
    args = _ffmpeg_cmd(
        _cam(name="front", scheme="rtsps", url="rtsps://h/x"),
        out_dir=Path("/raw"),
        segment_seconds=60,
    )
    assert "-fflags" not in args
    assert _val(args, "-rtsp_transport") == "tcp"
    assert _val(args, "-segment_format") == "mp4"


# -- segment timestamp parsing -------------------------------------------


def test_segment_opened_at_reads_the_strftime_stamp(tmp_path):
    p = tmp_path / "cbc_20260912_080716.ts"
    assert _segment_opened_at(p) == datetime(2026, 9, 12, 8, 7, 16).timestamp()


def test_segment_opened_at_handles_a_gpg_suffix(tmp_path):
    # Only the raw spool is watched, but the regex must not match past the
    # first extension and mis-parse.
    assert _segment_opened_at(tmp_path / "cbc_20260912_080716.ts.gpg") is None


def test_segment_opened_at_rejects_unstamped_names(tmp_path):
    assert _segment_opened_at(tmp_path / "cbc.ts") is None


def test_segment_opened_at_rejects_an_impossible_stamp(tmp_path):
    assert _segment_opened_at(tmp_path / "cbc_20261340_996716.ts") is None


# -- stall watchdog ------------------------------------------------------


def _segment(dir_path, name, opened="20260912_080716", span=0.0, size=10):
    p = dir_path / f"{name}_{opened}.ts"
    p.write_bytes(b"x" * size)
    opened_at = datetime.strptime(opened, "%Y%m%d_%H%M%S").timestamp()
    os.utime(p, (opened_at + span, opened_at + span))
    return p


def test_no_stall_while_segments_roll_over(tmp_path):
    _segment(tmp_path, "cbc", span=59.0)
    assert _stalled_segment(tmp_path, "cbc", limit_seconds=300) is None


def test_stall_detected_when_one_file_spans_too_long(tmp_path):
    # The live failure: 77 minutes written into a single 60s segment.
    p = _segment(tmp_path, "cbc", span=77 * 60)
    assert _stalled_segment(tmp_path, "cbc", limit_seconds=300) == p


def test_only_the_newest_segment_is_judged(tmp_path):
    # A previously-stalled segment keeps its long span forever once closed.
    # Judging it again would kill ffmpeg on every poll, so only the file still
    # being written -- the newest mtime -- counts. Here the old one was opened
    # at 08:00 and last written at 09:00 (a 1h span, already killed), while the
    # live one opened at 09:00:05 and is 10s in.
    old = _segment(tmp_path, "cbc", opened="20260912_080000", span=3600.0)
    fresh = _segment(tmp_path, "cbc", opened="20260912_090005", span=10.0)
    assert fresh.stat().st_mtime > old.stat().st_mtime  # fresh is the live one
    assert _stalled_segment(tmp_path, "cbc", limit_seconds=300) is None


def test_other_cameras_are_not_considered(tmp_path):
    _segment(tmp_path, "front", span=77 * 60)
    assert _stalled_segment(tmp_path, "cbc", limit_seconds=300) is None


def test_empty_dir_is_not_a_stall(tmp_path):
    assert _stalled_segment(tmp_path, "cbc", limit_seconds=300) is None
