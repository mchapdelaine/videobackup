"""Graphical decrypt-and-view tool for encrypted footage.

Pick a ``*.gpg`` clip, decrypt it with GPG (your **private** key must be
present — so this runs on your trusted restore machine, not the recording box,
which only holds the public key), and play it in an embedded mpv window.

Playback needs a seekable file, so the clip is decrypted to a temporary file
with ``0600`` permissions and unlinked when you close the window or open the
next clip. That is a brief plaintext-on-disk window; run this only on a machine
you trust.

Requires the optional ``[gui]`` extra (``python-mpv``) plus a system install of
``mpv``/libmpv and ``gpg``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

log = logging.getLogger("videobackup")


def _require_mpv():
    """Import python-mpv lazily with an actionable error if it's missing."""
    try:
        import mpv  # noqa: PLC0415  (optional dependency, imported on demand)
    except ImportError as exc:  # pragma: no cover - depends on env
        raise RuntimeError(
            "python-mpv is not installed. Install the GUI extra:\n"
            "    uv sync --extra gui        (or: pip install -e '.[gui]')\n"
            "and the libmpv system library (e.g. 'sudo apt install mpv libmpv2')."
        ) from exc
    return mpv


def decrypt_to_temp(src: Path) -> Path:
    """Run gpg, writing plaintext to a fresh 0600 temp file. Returns its path.

    On gpg failure the temp file is removed and a RuntimeError is raised. GUI-
    free so it can be unit-tested with a mocked subprocess.
    """
    # Preserve the real extension (strip .gpg) so mpv detects the format.
    suffix = Path(src.stem).suffix or ".mp4"
    fd, tmp_name = tempfile.mkstemp(prefix="videobackup-view-", suffix=suffix)
    os.close(fd)  # gpg writes the file; mkstemp already made it 0600
    tmp = Path(tmp_name)
    result = subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--quiet",
            "--decrypt",
            "--output",
            str(tmp),
            str(src),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(result.stderr.strip() or "gpg decrypt failed")
    return tmp


class ViewerApp:
    """Minimal Tk window: open a .gpg clip, decrypt, play embedded in mpv."""

    def __init__(self, root: tk.Tk, initial: Path | None = None) -> None:
        self.root = root
        self._tmp: Path | None = None
        self.player = None

        root.title("videobackup — decrypt & view")
        root.geometry("960x600")
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        controls = tk.Frame(root)
        controls.pack(side=tk.TOP, fill=tk.X)
        tk.Button(controls, text="Open .gpg…", command=self.open_dialog).pack(
            side=tk.LEFT, padx=4, pady=4
        )
        tk.Button(controls, text="Play / Pause", command=self.toggle_pause).pack(
            side=tk.LEFT, padx=4, pady=4
        )
        tk.Button(controls, text="Stop", command=self.stop).pack(
            side=tk.LEFT, padx=4, pady=4
        )
        self.status = tk.StringVar(value="Open an encrypted clip to begin.")
        tk.Label(controls, textvariable=self.status, anchor="w").pack(
            side=tk.LEFT, padx=8, fill=tk.X, expand=True
        )

        # Black canvas that libmpv renders into (reparented via its window id).
        self.video = tk.Frame(root, bg="black")
        self.video.pack(side=tk.BOTTOM, fill=tk.BOTH, expand=True)

        if initial is not None:
            # Defer until the window is mapped so the frame has a valid id.
            root.after(200, lambda: self.load(initial))

    # -- mpv lifecycle ----------------------------------------------------

    def _ensure_player(self):
        """Create the embedded mpv instance once the frame has a window id."""
        if self.player is not None:
            return self.player
        mpv = _require_mpv()
        self.root.update_idletasks()  # realize the frame -> valid winfo_id()
        self.player = mpv.MPV(
            wid=str(self.video.winfo_id()),
            osc=True,  # on-screen seek/volume controls
            input_default_bindings=True,
            input_vo_keyboard=True,
            keep_open="yes",  # don't tear down the window at end of file
        )
        return self.player

    # -- decrypt + play ---------------------------------------------------

    def open_dialog(self) -> None:
        path = filedialog.askopenfilename(
            title="Select an encrypted clip",
            filetypes=[("Encrypted footage", "*.gpg"), ("All files", "*")],
        )
        if path:
            self.load(Path(path))

    def load(self, src: Path) -> None:
        """Decrypt ``src`` off-thread, then hand the plaintext to mpv."""
        if not src.exists():
            messagebox.showerror("Not found", f"No such file:\n{src}")
            return
        self.status.set(f"Decrypting {src.name}…")
        threading.Thread(target=self._decrypt_worker, args=(src,), daemon=True).start()

    def _decrypt_worker(self, src: Path) -> None:
        try:
            plain = self._decrypt(src)
        except Exception as exc:  # surface gpg failures on the UI thread
            # Bind via default arg: `exc` is unbound once this block exits.
            self.root.after(0, lambda e=exc: self._decrypt_failed(e))
            return
        self.root.after(0, lambda: self._play(plain))

    def _decrypt(self, src: Path) -> Path:
        return decrypt_to_temp(src)

    def _play(self, plain: Path) -> None:
        self._discard_tmp()  # drop any previously-decrypted clip
        self._tmp = plain
        try:
            player = self._ensure_player()
        except RuntimeError as exc:
            self.status.set("mpv unavailable")
            messagebox.showerror("mpv unavailable", str(exc))
            self._discard_tmp()
            return
        player.play(str(plain))
        self.status.set(f"Playing (decrypted to a temp file): {plain.name}")

    def _decrypt_failed(self, exc: Exception) -> None:
        self.status.set("Decryption failed")
        messagebox.showerror("Decryption failed", str(exc))

    # -- controls ---------------------------------------------------------

    def toggle_pause(self) -> None:
        if self.player is not None:
            self.player.pause = not self.player.pause

    def stop(self) -> None:
        if self.player is not None:
            self.player.stop()
        self.status.set("Stopped.")
        self._discard_tmp()

    # -- cleanup ----------------------------------------------------------

    def _discard_tmp(self) -> None:
        if self._tmp is not None:
            try:
                self._tmp.unlink(missing_ok=True)
            except OSError:
                log.warning("Could not remove temp file %s", self._tmp)
            self._tmp = None

    def _on_close(self) -> None:
        if self.player is not None:
            self.player.terminate()
            self.player = None
        self._discard_tmp()
        self.root.destroy()


def run_viewer(initial: Path | None = None) -> None:
    """Launch the Tk viewer, optionally preselecting a file."""
    root = tk.Tk()
    ViewerApp(root, initial)
    root.mainloop()


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point (``videobackup-view``)."""
    import argparse

    p = argparse.ArgumentParser(
        prog="videobackup-view",
        description="Decrypt and watch an encrypted clip in a GUI window.",
    )
    p.add_argument("file", nargs="?", help="Optional .gpg clip to preselect on launch")
    args = p.parse_args(argv)
    run_viewer(Path(args.file) if args.file else None)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
