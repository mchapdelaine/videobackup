"""Enforce storage limits on the Google Drive backup folder.

Deletes oldest files first when the folder exceeds a byte budget, and
(optionally) anything older than a maximum age. The selection logic is a pure
function (:func:`select_for_deletion`) so it can be unit-tested without rclone.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RemoteFile:
    name: str
    size: int
    mod_time: datetime


def select_for_deletion(
    files: list[RemoteFile],
    max_bytes: int,
    now: datetime,
    max_age_days: int = 0,
) -> list[RemoteFile]:
    """Return the files to delete, oldest first.

    A file is selected if it is older than ``max_age_days`` (when > 0), or if
    it must go to bring total size at or under ``max_bytes``. Oldest files are
    removed first in both cases.
    """
    ordered = sorted(files, key=lambda f: f.mod_time)
    to_delete: list[RemoteFile] = []
    remaining: list[RemoteFile] = []

    # Age-based pass first.
    if max_age_days > 0:
        cutoff = now.timestamp() - max_age_days * 86400
        for f in ordered:
            if f.mod_time.timestamp() < cutoff:
                to_delete.append(f)
            else:
                remaining.append(f)
    else:
        remaining = ordered

    # Size-based pass on what survives the age cut.
    total = sum(f.size for f in remaining)
    while total > max_bytes and remaining:
        victim = remaining.pop(0)  # oldest
        to_delete.append(victim)
        total -= victim.size

    return to_delete


def effective_prune_cap(
    max_drive_bytes: int,
    reserve_bytes: int,
    folder_total: int,
    account_free: int | None,
    min_free_bytes: int,
) -> int:
    """Byte budget the backup folder must be pruned to (pre-upload target).

    Starts from the folder cap (``max_drive_bytes`` minus the bytes about to be
    uploaded). If ``min_free_bytes`` is set and the account's free space is
    known, tightens the budget so that after uploading ``reserve_bytes`` the
    account still has at least ``min_free_bytes`` free — accounting for quota
    shared with Gmail, Photos, other Drive files, and trash.

    Pure (no rclone) so it can be unit-tested.
    """
    reserve = max(0, reserve_bytes)
    cap = max(0, max_drive_bytes - reserve)
    if min_free_bytes > 0 and account_free is not None:
        # Deleting D bytes from the folder raises free by D; uploading reserve
        # lowers it by reserve. Require account_free + D - reserve >= min_free,
        # i.e. keep the folder at/under folder_total - deficit.
        deficit = min_free_bytes - (account_free - reserve)
        if deficit > 0:
            cap = min(cap, max(0, folder_total - deficit))
    return cap


def _run_rclone(args: list[str]) -> subprocess.CompletedProcess[str]:
    if shutil.which("rclone") is None:
        raise RuntimeError("rclone is not installed or not on PATH")
    return subprocess.run(["rclone", *args], capture_output=True, text=True)


def _rclone_delete_args(
    remote_path: str, files_from: str, use_trash: bool
) -> list[str]:
    """Build the ``rclone delete`` argv for a batch of files (pure/testable).

    ``--files-from`` lists names relative to ``remote_path`` (segments are flat
    in the folder), so a single invocation deletes them all in parallel instead
    of one subprocess per file. Trash is off by default: the Drive trash still
    counts against the account quota.
    """
    return [
        "delete",
        remote_path,
        "--files-from",
        files_from,
        f"--drive-use-trash={'true' if use_trash else 'false'}",
    ]


def _delete_remote(config: Config, names: list[str]) -> int:
    """Delete ``names`` under the remote folder in one rclone call.

    Returns the number deleted (all of them on success; 0 if the batch failed).
    """
    if not names:
        return 0
    fd, list_path = tempfile.mkstemp(prefix="videobackup-prune-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(names) + "\n")
        result = _run_rclone(
            _rclone_delete_args(config.remote_path, list_path, config.use_trash)
        )
    finally:
        os.unlink(list_path)
    if result.returncode != 0:
        log.error(
            "Batch prune of %d file(s) failed (rc=%s): %s",
            len(names),
            result.returncode,
            result.stderr.strip(),
        )
        return 0
    return len(names)


def _parse_mod_time(value: str) -> datetime:
    # rclone emits RFC3339, e.g. "2026-07-16T12:00:00.000000000Z".
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        # Trim sub-second precision Python can't parse, then retry.
        if "." in text:
            head, _, tail = text.partition(".")
            tz = tail[-6:] if tail.endswith(("+00:00",)) else "+00:00"
            return datetime.fromisoformat(head + tz)
        raise


def list_remote(config: Config) -> list[RemoteFile]:
    result = _run_rclone(["lsjson", "--files-only", config.remote_path])
    if result.returncode != 0:
        raise RuntimeError(f"rclone lsjson failed: {result.stderr.strip()}")
    entries = json.loads(result.stdout or "[]")
    files: list[RemoteFile] = []
    for e in entries:
        files.append(
            RemoteFile(
                name=e["Name"],
                size=int(e.get("Size", 0)),
                mod_time=_parse_mod_time(e["ModTime"]),
            )
        )
    return files


def remote_about(config: Config) -> int | None:
    """Return the account's free bytes via ``rclone about``, or None.

    Returns None (and logs a warning) if the command fails or the backend does
    not report free space, so the quota check degrades to folder-cap-only
    rather than breaking prune.
    """
    result = _run_rclone(["about", f"{config.rclone_remote}:", "--json"])
    if result.returncode != 0:
        log.warning(
            "rclone about failed; skipping quota check: %s", result.stderr.strip()
        )
        return None
    try:
        return int(json.loads(result.stdout or "{}")["free"])
    except (ValueError, KeyError) as exc:
        log.warning("Could not read free space from rclone about: %s", exc)
        return None


def prune(config: Config, reserve_bytes: int = 0) -> int:
    """Delete remote files to honor the size/age caps. Returns count deleted.

    ``reserve_bytes`` lowers the effective size cap so headroom is freed for
    data about to be uploaded. Used as a pre-upload gate: prune to
    ``max_drive_bytes - reserve_bytes`` first, so the subsequent upload lands
    at or under the cap instead of overshooting it.
    """
    files = list_remote(config)
    now = datetime.now(timezone.utc)
    folder_total = sum(f.size for f in files)
    # Only consult the account when the quota guard is enabled (saves an rclone
    # call otherwise). None => fall back to folder-cap-only.
    account_free = remote_about(config) if config.min_free_bytes > 0 else None
    effective_cap = effective_prune_cap(
        config.max_drive_bytes,
        reserve_bytes,
        folder_total,
        account_free,
        config.min_free_bytes,
    )
    victims = select_for_deletion(files, effective_cap, now, config.max_age_days)
    if not victims:
        free_note = (
            f", account free {account_free / 2**30:.2f} GiB"
            if account_free is not None
            else ""
        )
        log.info(
            "Retention OK: %d file(s), %.2f GiB (cap %.2f GiB, reserve %.2f GiB%s)",
            len(files),
            folder_total / 2**30,
            config.max_drive_bytes / 2**30,
            reserve_bytes / 2**30,
            free_note,
        )
        return 0

    deleted = _delete_remote(config, [f.name for f in victims])
    if deleted:
        log.info("Pruned %d file(s) from %s", deleted, config.remote_path)
    return deleted
