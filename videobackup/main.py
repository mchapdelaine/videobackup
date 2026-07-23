"""Command-line entry point and orchestration."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from .config import Config, ConfigError, ensure_spool_dirs, load_config
from .encrypt import encrypt_pending
from .recorder import Recorder
from .retention import prune
from .uploader import upload_pending

log = logging.getLogger("videobackup")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def run_batch(config: Config) -> None:
    """One encrypt -> upload -> prune cycle."""
    ensure_spool_dirs(config)
    encrypted = encrypt_pending(config)
    uploaded = upload_pending(config)
    deleted = prune(config)
    log.info(
        "Batch done: encrypted=%d uploaded=%d pruned=%d",
        encrypted,
        uploaded,
        deleted,
    )


def run_batch_loop(config: Config, stop: threading.Event | None = None) -> None:
    """Repeat the batch cycle every ``batch_interval_seconds`` until signalled.

    If ``stop`` is None a fresh event is created and signal handlers installed
    (standalone use, main thread). Callers running this off-thread must pass
    their own ``stop`` event, since signal handlers only work in the main thread.
    """
    if stop is None:
        stop = _install_stop_handler()
    while not stop.is_set():
        try:
            run_batch(config)
        except Exception:  # keep the loop alive across transient failures
            log.exception("Batch cycle failed; will retry")
        stop.wait(config.batch_interval_seconds)


def run_encrypt_loop(config: Config, stop: threading.Event) -> None:
    """Encrypt closed segments on a tight interval to drain plaintext fast.

    Encryption is cheap and local, so it runs far more often than uploads.
    This keeps plaintext on disk only briefly (security) and bounds the raw
    spool regardless of upload bandwidth.
    """
    ensure_spool_dirs(config)
    while not stop.is_set():
        try:
            encrypt_pending(config)
        except Exception:
            log.exception("Encrypt cycle failed; will retry")
        stop.wait(config.encrypt_interval_seconds)


# When the upload queue is empty, poll this often for newly-encrypted files.
_UPLOAD_IDLE_POLL_SECONDS = 5


def _pending_upload_bytes(config: Config) -> int:
    total = 0
    for f in config.spool_encrypted.glob("*.gpg"):
        try:
            total += f.stat().st_size
        except FileNotFoundError:
            pass
    return total


def run_upload_loop(config: Config, stop: threading.Event) -> None:
    """Continuously upload encrypted segments, gating on the size cap.

    Runs independently of encryption so a slow Drive upload never blocks
    plaintext from being encrypted and removed. When files are queued it prunes
    the remote down to ``max_drive_bytes`` minus the bytes about to be uploaded
    (a pre-upload gate, so the upload lands at/under the cap instead of
    overshooting), then uploads back-to-back until drained. When idle it polls,
    and periodically prunes to enforce the age cap even without new uploads.
    """
    import time

    last_prune = 0.0
    while not stop.is_set():
        moved = 0
        try:
            pending = _pending_upload_bytes(config)
            if pending > 0:
                prune(config, reserve_bytes=pending)  # gate: free room first
                last_prune = time.monotonic()
                moved = upload_pending(config)
            elif time.monotonic() - last_prune >= config.batch_interval_seconds:
                prune(config)  # idle: still enforce age/size caps
                last_prune = time.monotonic()
        except Exception:
            log.exception("Upload/prune cycle failed; will retry")
        if moved > 0 and not stop.is_set():
            continue  # more may be ready — go again immediately
        stop.wait(_UPLOAD_IDLE_POLL_SECONDS)


def run_all(config: Config) -> None:
    """Run recorder + encrypt loop + upload/prune loop in one process.

    Signals are handled once, here in the main thread, via a shared stop event
    passed to every worker. Encryption and upload run in separate threads so
    the fast local stage is never blocked by the slow network stage.
    """
    stop = _install_stop_handler()
    workers = [
        threading.Thread(
            target=run_encrypt_loop, args=(config, stop), name="encrypt", daemon=True
        ),
        threading.Thread(
            target=run_upload_loop, args=(config, stop), name="upload", daemon=True
        ),
    ]
    for w in workers:
        w.start()
    Recorder(config).run_forever(stop)  # blocks until stop is set


def _install_stop_handler() -> threading.Event:
    stop = threading.Event()

    def _handle(signum, _frame):
        log.info("Received signal %s; stopping", signum)
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    return stop


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="videobackup",
        description="Encrypted Google Drive backup of UniFi Protect footage.",
    )
    p.add_argument("-c", "--config", default="config.yaml", help="Path to config.yaml")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("record", help="Record all cameras continuously (foreground)")
    sub.add_parser("batch", help="Run one encrypt->upload->prune cycle and exit")
    sub.add_parser("batch-loop", help="Run the batch cycle on a repeating timer")
    sub.add_parser("run", help="Record and run the batch loop together")
    sub.add_parser("prune", help="Enforce the storage cap once and exit")
    sub.add_parser("check", help="Validate config and report, then exit")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        log.error("Config error: %s", exc)
        return 2

    if args.command == "check":
        log.info(
            "Config OK: %d camera(s), cap %.1f GiB, remote %s",
            len(config.cameras),
            config.max_drive_bytes / 2**30,
            config.remote_path,
        )
        return 0
    if args.command == "record":
        Recorder(config).run_forever()
    elif args.command == "batch":
        run_batch(config)
    elif args.command == "batch-loop":
        run_batch_loop(config)
    elif args.command == "prune":
        prune(config)
    elif args.command == "run":
        run_all(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
