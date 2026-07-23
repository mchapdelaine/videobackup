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
        "--drive-chunk-size",
        "64M",  # ignored by non-drive backends
    ]
    result = subprocess.run(["rclone", *args], capture_output=True, text=True)
    if result.returncode != 0:
        log.error(
            "rclone move failed (rc=%s): %s", result.returncode, result.stderr.strip()
        )

    moved = len(before) - len(_ready_files(config.spool_encrypted))
    if moved > 0:
        log.info("Uploaded %d file(s) to %s", moved, config.remote_path)
    return moved
