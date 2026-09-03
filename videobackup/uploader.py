"""Upload encrypted segments to Google Drive via rclone.

Uses a single ``rclone move`` over the whole encrypted spool with parallel
transfers. One rclone process amortizes Google Drive's per-file/session
overhead and pipelines uploads, which is dramatically faster than spawning a
process per file. ``move`` deletes each source file as soon as its upload
succeeds, so files leave the local disk as early as possible.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)


def _ready_files(spool: Path) -> list[Path]:
    # Only fully-written encrypted files. In-progress ones are "*.gpg.part".
    return sorted(spool.glob("*.gpg"))


def _total_bytes(files: list[Path]) -> int:
    total = 0
    for f in files:
        try:
            total += f.stat().st_size
        except OSError:  # file vanished between listing and stat
            pass
    return total


def _rclone_move_args(config: Config) -> list[str]:
    """Build the ``rclone move`` argv (pure/testable).

    Includes stall-resistance flags so a wedged connection (flaky USB NIC,
    Drive API back-off) aborts and retries instead of hanging the whole upload
    loop forever, plus an optional API rate cap.
    """
    transfers = str(config.upload_transfers)
    args = [
        "move",
        str(config.spool_encrypted),
        config.remote_path,
        "--include",
        "*.gpg",  # never touch in-progress "*.gpg.part"
        "--transfers",
        transfers,
        "--checkers",
        transfers,
        "--no-traverse",  # skip full remote listing
        "--retries",
        "3",
        "--low-level-retries",
        "10",  # retry a stalled/reset connection on a fresh one
        "--timeout",
        "120s",  # abort a transfer idle this long instead of hanging
        "--contimeout",
        "30s",
        "--drive-chunk-size",
        "64M",  # ignored by non-drive backends
    ]
    if config.upload_tpslimit > 0:
        # Cap API calls/sec to stay under Drive's per-minute Queries quota.
        args += ["--tpslimit", str(config.upload_tpslimit)]
    return args


def upload_pending(config: Config) -> int:
    """Move all ready ``.gpg`` files to Drive. Returns the count uploaded.

    rclone handles retries internally; files it fails to transfer stay in the
    spool and are retried on the next call.
    """
    if shutil.which("rclone") is None:
        raise RuntimeError("rclone is not installed or not on PATH")

    before = _ready_files(config.spool_encrypted)
    if not before:
        return 0

    # Log up front: an rclone move is silent until it returns, so without this
    # a slow/large upload looks like a hang.
    log.info(
        "Uploading %d file(s) (%.2f GiB) to %s",
        len(before),
        _total_bytes(before) / 2**30,
        config.remote_path,
    )
    result = subprocess.run(
        ["rclone", *_rclone_move_args(config)], capture_output=True, text=True
    )
    if result.returncode != 0:
        log.error(
            "rclone move failed (rc=%s): %s", result.returncode, result.stderr.strip()
        )

    moved = len(before) - len(_ready_files(config.spool_encrypted))
    if moved > 0:
        log.info("Uploaded %d file(s) to %s", moved, config.remote_path)
    return moved
