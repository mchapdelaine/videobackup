"""Per-source recorder: supervises an ffmpeg segment process per source.

Handles RTSP cameras and HTTP/MPEG-TS sources (e.g. an HDHomeRun tuner);
input flags and the segment container are chosen by the URL scheme.
"""

from __future__ import annotations

import logging
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from .config import Camera, Config, ensure_spool_dirs

log = logging.getLogger(__name__)

# Restart backoff bounds (seconds).
_BACKOFF_MIN = 2.0
_BACKOFF_MAX = 60.0


_HTTP_SCHEMES = ("http", "https")


def _ffmpeg_cmd(camera: Camera, out_dir: Path, segment_seconds: int) -> list[str]:
    """Build the ffmpeg command that segments a source stream into files.

    Uses stream copy (-c copy) so there is no re-encode: low CPU, original
    quality. Input flags and container are chosen by URL scheme:

    - ``rtsp://`` — TCP transport + connection timeout, muxed to ``.mp4``.
    - ``http(s)://`` (e.g. HDHomeRun) — auto-reconnect on drop, muxed to
      ``.ts`` (mpegts), which losslessly stream-copies broadcast codecs
      (MPEG-2/H.264 video, AC-3 audio) that MP4 handles poorly.

    Segments are written directly to their final extension; the encrypt stage
    waits for the mtime to settle before touching a file, so a segment still
    being written is never picked up.
    """
    if camera.scheme in _HTTP_SCHEMES:
        input_opts = [
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
        ]
        segment_format, ext = "mpegts", "ts"
    else:  # rtsp (and any other scheme) keeps the original RTSP path
        input_opts = [
            "-rtsp_transport",
            "tcp",
            "-timeout",
            "10000000",  # microseconds; drop dead connections
        ]
        segment_format, ext = "mp4", "mp4"

    pattern = str(out_dir / f"{camera.name}_%Y%m%d_%H%M%S.{ext}")
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "warning",
        *input_opts,
        "-i",
        camera.url,
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-segment_format",
        segment_format,
        "-reset_timestamps",
        "1",
        "-strftime",
        "1",
        pattern,
    ]


class CameraRecorder:
    """Runs and restarts a single camera's ffmpeg process until stopped."""

    def __init__(self, camera: Camera, out_dir: Path, segment_seconds: int):
        self.camera = camera
        self.out_dir = out_dir
        self.segment_seconds = segment_seconds
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._thread = threading.Thread(
            target=self._run, name=f"rec-{camera.name}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
        self._thread.join(timeout=15)

    def _run(self) -> None:
        backoff = _BACKOFF_MIN
        cmd = _ffmpeg_cmd(self.camera, self.out_dir, self.segment_seconds)
        while not self._stop.is_set():
            started = time.monotonic()
            log.info("Recording %s -> %s", self.camera.name, self.out_dir)
            try:
                self._proc = subprocess.Popen(cmd)
            except FileNotFoundError:
                log.error("ffmpeg not found on PATH; cannot record")
                return
            rc = self._proc.wait()
            if self._stop.is_set():
                break
            ran_for = time.monotonic() - started
            log.warning(
                "ffmpeg for %s exited (rc=%s) after %.0fs; restarting",
                self.camera.name,
                rc,
                ran_for,
            )
            # Reset backoff if the process ran healthily for a while.
            if ran_for > _BACKOFF_MAX:
                backoff = _BACKOFF_MIN
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, _BACKOFF_MAX)


class Recorder:
    """Owns one CameraRecorder per configured camera."""

    def __init__(self, config: Config):
        self.config = config
        self._recorders: list[CameraRecorder] = []

    def run_forever(self, stop: threading.Event | None = None) -> None:
        """Run all camera recorders until ``stop`` is set.

        If ``stop`` is None a fresh event is created and SIGINT/SIGTERM
        handlers are installed. Signal handlers can only be registered from the
        main thread, so callers running this off-thread must pass their own
        ``stop`` event and handle signals themselves.
        """
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg is not installed or not on PATH")
        ensure_spool_dirs(self.config)

        for cam in self.config.cameras:
            rec = CameraRecorder(
                cam, self.config.spool_raw, self.config.segment_seconds
            )
            rec.start()
            self._recorders.append(rec)
        log.info("Started %d camera recorder(s)", len(self._recorders))

        if stop is None:
            stop = threading.Event()

            def _handle(signum, _frame):
                log.info("Received signal %s; stopping recorders", signum)
                stop.set()

            signal.signal(signal.SIGINT, _handle)
            signal.signal(signal.SIGTERM, _handle)

        try:
            while not stop.wait(1.0):
                pass
        finally:
            for rec in self._recorders:
                rec.stop()
            log.info("All recorders stopped")
