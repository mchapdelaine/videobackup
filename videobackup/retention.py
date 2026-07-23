"""Enforce storage limits on the Google Drive backup folder.

Deletes oldest files first when the folder exceeds a byte budget, and
(optionally) anything older than a maximum age. The selection logic is a pure
function (:func:`select_for_deletion`) so it can be unit-tested without rclone.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
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


def _run_rclone(args: list[str]) -> subprocess.CompletedProcess[str]:
    if shutil.which("rclone") is None:
        raise RuntimeError("rclone is not installed or not on PATH")
    return subprocess.run(["rclone", *args], capture_output=True, text=True)


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


def prune(config: Config, reserve_bytes: int = 0) -> int:
    """Delete remote files to honor the size/age caps. Returns count deleted.

    ``reserve_bytes`` lowers the effective size cap so headroom is freed for
    data about to be uploaded. Used as a pre-upload gate: prune to
    ``max_drive_bytes - reserve_bytes`` first, so the subsequent upload lands
    at or under the cap instead of overshooting it.
    """
    files = list_remote(config)
    now = datetime.now(timezone.utc)
    effective_cap = max(0, config.max_drive_bytes - max(0, reserve_bytes))
    victims = select_for_deletion(
        files, effective_cap, now, config.max_age_days
    )
    if not victims:
        total = sum(f.size for f in files)
        log.info(
            "Retention OK: %d file(s), %.2f GiB (cap %.2f GiB, reserve %.2f GiB)",
            len(files), total / 2**30,
            config.max_drive_bytes / 2**30, reserve_bytes / 2**30,
        )
        return 0

    deleted = 0
    for f in victims:
        # By default delete permanently: the Drive trash still counts against
        # the account quota. Set use_trash: true to keep deletions recoverable.
        result = _run_rclone(
            [
                "deletefile",
                f"--drive-use-trash={'true' if config.use_trash else 'false'}",
                f"{config.remote_path}/{f.name}",
            ]
        )
        if result.returncode == 0:
            deleted += 1
            log.info("Pruned %s", f.name)
        else:
            log.error("Failed to prune %s: %s", f.name, result.stderr.strip())
    return deleted
