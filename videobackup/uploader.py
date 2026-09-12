"""Upload encrypted segments to Google Drive via rclone.

Uses a single ``rclone move`` over the whole encrypted spool with parallel
transfers. One rclone process amortizes Google Drive's per-file/session
overhead and pipelines uploads, which is dramatically faster than spawning a
process per file. ``move`` deletes each source file as soon as its upload
succeeds, so files leave the local disk as early as possible.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import NamedTuple

from .config import Config

log = logging.getLogger(__name__)


def _ready_files(spool: Path) -> list[Path]:
    # Only fully-written encrypted files. In-progress ones are "*.gpg.part".
    return sorted(spool.glob("*.gpg"))


def _rclone_move_args(config: Config) -> list[str]:
    """Build the ``rclone move`` argv (pure/testable).

    Includes stall-resistance flags so a wedged connection (flaky USB NIC,
    Drive API back-off) aborts and retries instead of hanging the whole upload
    loop forever, plus an optional API rate cap.

    ``--max-transfer`` bounds the cycle so the call always returns and prune
    gets to run; ``--order-by`` uploads freshest footage first, which also
    drains cameras fairly (the default listing order is by name, which starves
    whichever camera sorts last).
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
        "--order-by",
        "modtime,desc",  # newest first; goal is the *latest* footage
        "--max-transfer",
        str(config.upload_slice_bytes),
        "--cutoff-mode",
        "soft",  # finish in-flight transfers, start no new ones
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
        # One final summary on stderr and nothing periodic (the interval never
        # elapses). Without it rclone is silent on success, which hides a
        # transfer that is retrying most of its bytes away.
        "--stats",
        "1000h",
        "--stats-log-level",
        "NOTICE",
    ]
    if config.upload_tpslimit > 0:
        # Cap API calls/sec to stay under Drive's per-minute Queries quota.
        args += ["--tpslimit", str(config.upload_tpslimit)]
    return args


# rclone's exit code for "--max-transfer limit reached". Expected, not a failure:
# the cycle did its bounded share and the rest goes on the next pass.
_RC_MAX_TRANSFER = 8

# "Errors:  3 (retrying may help)" in rclone's final stats block.
_ERRORS_RE = re.compile(r"^Errors:\s+(\d+)", re.MULTILINE)

# Drive 403s that mean "slow down", as opposed to "out of space". These are
# survivable -- rclone backs off and retries -- so they never reach the exit
# code, yet they can silently discard most of a cycle's transferred bytes.
_RATE_LIMIT_MARKERS = (
    "ratelimitexceeded",
    "userratelimitexceeded",
    "quota metric",
    "quota exceeded",
)

# Drive's *other* 403: the account is full. Same status code as throttling but
# the opposite remedy -- retrying never helps, something has to be deleted.
_OUT_OF_SPACE_MARKERS = (
    "storagequotaexceeded",
    "storage quota has been exceeded",
)

_CLIENT_ID_HINT = (
    "If this persists, the rclone remote is probably using rclone's shared "
    "default client_id, whose API quota is consumed by all rclone users at "
    "once. Create your own Drive API client_id: "
    "https://rclone.org/drive/#making-your-own-client-id"
)


class RcloneOutcome(NamedTuple):
    errors: int
    rate_limited: bool
    out_of_space: bool


def _summarize_rclone(stderr: str) -> RcloneOutcome:
    """Parse rclone's stderr into the signals worth acting on (pure).

    ``errors`` comes from the final stats block. ``rate_limited`` is true when
    any 403-style throttling appeared, whether or not it was fatal.
    ``out_of_space`` is the distinct 403 that means the Drive account is full;
    it is deliberately not folded into ``rate_limited`` because the two call for
    opposite responses -- back off versus free space.
    """
    match = _ERRORS_RE.search(stderr)
    errors = int(match.group(1)) if match else 0
    low = stderr.lower()
    return RcloneOutcome(
        errors=errors,
        rate_limited=any(marker in low for marker in _RATE_LIMIT_MARKERS),
        out_of_space=any(marker in low for marker in _OUT_OF_SPACE_MARKERS),
    )


def upload_pending(config: Config) -> int:
    """Move ready ``.gpg`` files to Drive, newest first. Returns count uploaded.

    Transfers at most ``config.upload_slice_bytes`` per call so the loop can
    prune between cycles. rclone handles retries internally; files it fails to
    transfer stay in the spool and are retried on the next call.
    """
    if shutil.which("rclone") is None:
        raise RuntimeError("rclone is not installed or not on PATH")

    before = _ready_files(config.spool_encrypted)
    if not before:
        return 0

    # Size them up front: measuring the delta against a later listing would
    # count segments encrypted *during* the run as if they had failed.
    sizes: dict[str, int] = {}
    for path in before:
        try:
            sizes[path.name] = path.stat().st_size
        except OSError:  # vanished between listing and stat
            pass

    # Log up front: an rclone move is silent until it returns, so without this
    # a slow/large upload looks like a hang.
    log.info(
        "Uploading up to %.2f GiB of %d queued file(s) (%.2f GiB) to %s",
        config.upload_slice_bytes / 2**30,
        len(before),
        sum(sizes.values()) / 2**30,
        config.remote_path,
    )
    started = time.monotonic()
    result = subprocess.run(
        ["rclone", *_rclone_move_args(config)], capture_output=True, text=True
    )
    elapsed = max(time.monotonic() - started, 1e-6)

    if result.returncode == _RC_MAX_TRANSFER:
        log.info("Upload slice full; remainder queued for the next cycle")
    elif result.returncode != 0:
        log.error(
            "rclone move failed (rc=%s): %s", result.returncode, result.stderr.strip()
        )

    # ``move`` unlinks each source only once its upload is confirmed, so the
    # names that disappeared are exactly the ones that landed.
    remaining = {p.name for p in _ready_files(config.spool_encrypted)}
    moved_names = [name for name in sizes if name not in remaining]
    moved = len(moved_names)
    moved_bytes = sum(sizes[name] for name in moved_names)

    outcome = _summarize_rclone(result.stderr)
    log.info(
        "Upload cycle: %d/%d file(s), %.2f GiB in %.0fs (%.0f MB/min), "
        "%d rclone error(s)",
        moved,
        len(sizes),
        moved_bytes / 2**30,
        elapsed,
        moved_bytes / elapsed * 60 / 1e6,
        outcome.errors,
    )
    if outcome.rate_limited:
        log.warning(
            "Google Drive is throttling uploads (403). Transfers still succeed "
            "on retry, so this is invisible in the exit code, but most "
            "transferred bytes are being discarded and re-sent. %s",
            _CLIENT_ID_HINT,
        )
    if outcome.out_of_space:
        largest = max(sizes.values(), default=0)
        log.error(
            "Google Drive is out of space (403 storageQuotaExceeded); retrying "
            "will not help. The pre-upload prune should have freed room, so "
            "either the account is full outside this folder (check `rclone "
            "about %s:`) or a queued segment is too big to fit — the largest "
            "here is %.2f GiB against a %.2f GiB cap.",
            config.rclone_remote,
            largest / 2**30,
            config.max_drive_bytes / 2**30,
        )
    return moved
