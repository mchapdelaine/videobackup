"""GPG public-key encryption of recorded segments."""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)

# A segment is considered "closed" (ffmpeg done writing) once its mtime has
# been stable for at least this many seconds beyond the segment length.
_SETTLE_MARGIN = 15.0


def _is_closed(path: Path, now: float) -> bool:
    try:
        return (now - path.stat().st_mtime) >= _SETTLE_MARGIN
    except FileNotFoundError:
        return False


def encrypt_file(src: Path, dest: Path, recipient: str) -> None:
    """Encrypt ``src`` to ``dest`` for ``recipient`` using GPG public key.

    Writes to a ``.part`` file first and renames on success so a partial
    output is never picked up by the uploader.
    """
    tmp = dest.with_suffix(dest.suffix + ".part")
    cmd = [
        "gpg",
        "--batch",
        "--yes",
        "--no-tty",
        "--trust-model",
        "always",  # recipient key is explicitly chosen by us
        "--encrypt",
        "--recipient",
        recipient,
        "--output",
        str(tmp),
        str(src),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"gpg failed for {src.name} (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )
    tmp.replace(dest)


def encrypt_pending(config: Config) -> int:
    """Encrypt all closed raw segments; delete plaintext after success.

    Returns the number of files successfully encrypted.
    """
    if shutil.which("gpg") is None:
        raise RuntimeError("gpg is not installed or not on PATH")

    now = time.time()
    count = 0
    for src in sorted(config.spool_raw.glob("*.mp4")):
        if not _is_closed(src, now):
            continue
        dest = config.spool_encrypted / (src.name + ".gpg")
        if dest.exists():
            log.warning("Encrypted output already exists, skipping: %s", dest.name)
            src.unlink(missing_ok=True)
            continue
        try:
            encrypt_file(src, dest, config.gpg_recipient)
        except RuntimeError as exc:
            log.error("Encrypt failed: %s", exc)
            continue
        src.unlink(missing_ok=True)
        count += 1
        log.info("Encrypted %s", src.name)
    return count
