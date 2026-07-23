# videobackup

![CI](https://github.com/mchapdelaine/videobackup/actions/workflows/ci.yml/badge.svg)

Unattended Linux tool that records RTSP camera footage, **GPG-encrypts** every clip, 
uploads it to a remote location via `rclone`, and enforces a **storage cap** by 
pruning the oldest files first.

```
[cameras] --RTSP--> ffmpeg segment --> gpg encrypt --> rclone upload --> Drive
                                                                 |
                                                    retention prune (size/age cap)
```

Only your GPG **public** key lives on this machine — footage is encrypted
before it leaves the box, and the private key (needed to decrypt) stays
offline.

## Requirements

System tools (install with your package manager):

- `ffmpeg` — records RTSP streams
- `gpg` — encryption
- `rclone` — Google Drive transfer
- Python 3.9+

```bash
sudo apt install ffmpeg gnupg rclone python3-venv    # Debian/Ubuntu
```

## Install

With [uv](https://docs.astral.sh/uv/) (recommended — fast, uses the pinned
`uv.lock`):

```bash
uv sync            # creates .venv from the lockfile
```

Or with plain pip:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

> Full step-by-step walkthrough: see [SETUP.md](SETUP.md).

## One-time setup

### 1. Enable RTSP on each camera
In UniFi Protect: **Camera → Settings → Advanced → RTSP**, enable a stream,
and copy the `rtsp://<UDM-IP>:7447/<id>` URL. Repeat for all 3 cameras.

### 2. Import your GPG public key
Generate a keypair on a **trusted, offline** machine, export the public key,
and import only that here:

```bash
gpg --import public.asc
```

Keep the private key offline. You only need it to restore footage:

```bash
gpg --decrypt front_door_20260716_120000.mp4.gpg > clip.mp4
```

### 3. Configure rclone for Google Drive

```bash
rclone config          # create a remote named e.g. "gdrive" (type: drive)
rclone lsd gdrive:     # verify access
```

### 4. Write your config

```bash
cp config.yaml.example config.yaml
$EDITOR config.yaml     # fill in RTSP URLs, gpg_recipient, remote, size cap
videobackup -c config.yaml check
```

### 5. Lock down credential files

Both `config.yaml` (RTSP stream tokens) and rclone's config (unencrypted
Google Drive OAuth token by default) grant access — restrict them to your user:

```bash
chmod 600 config.yaml ~/.config/rclone/rclone.conf
```

Optionally, `rclone config` can password-encrypt `rclone.conf`.

Note: the RTSP URL is passed to `ffmpeg` as an argument, so it is visible in
`ps` to other local users. On a shared box, run under a dedicated user (the
systemd units already do this).

## Usage

```bash
videobackup -c config.yaml record       # record cameras (foreground)
videobackup -c config.yaml batch        # one encrypt->upload->prune cycle
videobackup -c config.yaml batch-loop   # batch on a repeating timer
videobackup -c config.yaml prune        # enforce storage cap once
videobackup -c config.yaml run          # record + batch loop together
```

For a quick end-to-end test, `run` does everything in one process. For
production, use the systemd units below (recorder as a long service, batch on
a timer).

## Run as a service (systemd)

Copy the units in `systemd/` and adjust `User`, paths, and config location:

```bash
sudo cp systemd/videobackup-record.service /etc/systemd/system/
sudo cp systemd/videobackup-batch.service  /etc/systemd/system/
sudo cp systemd/videobackup-batch.timer    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now videobackup-record.service
sudo systemctl enable --now videobackup-batch.timer
```

Note: rclone stores its Drive token in the running user's config
(`~/.config/rclone/rclone.conf`); make sure the service `User=` is the account
that ran `rclone config`.

## Configuration reference

See `config.yaml.example`. Key fields:

| Field | Meaning |
|---|---|
| `cameras[]` | Name + RTSP URL per camera |
| `segment_seconds` | Length of each recorded file (default 300) |
| `gpg_recipient` | Public key id/email footage is encrypted to |
| `rclone_remote` / `drive_folder` | Google Drive destination |
| `max_drive_bytes` | Storage cap; oldest files pruned when exceeded |
| `max_age_days` | Optional: also delete files older than this (0 = off) |
| `use_trash` | `false` (default) deletes pruned files permanently; `true` sends them to Drive trash (still counts against quota) |
| `local_spool` | Working dir for in-flight segments — use a real disk path, **not** `/tmp` (tmpfs/RAM) |
| `encrypt_interval_seconds` | How often the encrypt loop drains plaintext (default 20; keep small) |
| `upload_transfers` | Parallel rclone transfers per upload (default 4; try 8 on a fast uplink) |
| `batch_interval_seconds` | Prune cadence when idle (default 300); uploads are continuous, not gated by this |

## How the pipeline runs

`run` (and the service setup) runs three independent stages so a slow upload
never stalls the fast local ones:

- **record** — one ffmpeg per camera writing segments to `local_spool/raw`.
- **encrypt** — every `encrypt_interval_seconds`, GPG-encrypts closed segments
  to `local_spool/encrypted` and deletes the plaintext (short plaintext window).
- **upload + prune** — continuously `rclone move`s encrypted files to Drive
  with `upload_transfers` parallel transfers; files ship as soon as they're ready.

## How retention works

Before each upload the remote folder is listed (`rclone lsjson`) and the oldest
files are deleted first until total size is at or under `max_drive_bytes`
**minus the bytes about to be uploaded** — a pre-upload gate, so the folder
lands at/under the cap instead of overshooting it. Files older than
`max_age_days` are also dropped (enforced even when idle). Deletion is
permanent by default (`use_trash: false`) so it actually frees quota — the
Drive trash otherwise still counts against your storage. The selection logic is
a pure function, unit-tested in `tests/test_retention.py`.

## Development

Dependencies are managed with [uv](https://docs.astral.sh/uv/); exact versions
are pinned in `uv.lock` (committed). Set up the dev environment and run tests:

```bash
uv sync --all-extras    # installs ruff, pytest, pre-commit into .venv
uv run pytest
```

`uv run <cmd>` executes inside the project env without activating it. Prefix the
ruff/pytest/pre-commit commands below with `uv run` (or activate `.venv` first).
To refresh the lockfile after changing dependencies in `pyproject.toml`, run
`uv lock`. Plain `pip install -e '.[dev]'` still works if you prefer pip.

### Code style & linting

[Ruff](https://docs.astral.sh/ruff/) handles both linting and formatting,
configured under `[tool.ruff]` in `pyproject.toml` (rule sets: `E`, `F`, `I`,
`UP`, `B`). Run manually:

```bash
ruff check .            # lint (add --fix to auto-fix)
ruff format .           # format in place
ruff format --check .   # verify formatting without writing
```

### Pre-commit hook

A [pre-commit](https://pre-commit.com/) hook runs ruff lint + format on staged
files at commit time. Enable it once per clone:

```bash
pre-commit install
```

Now every `git commit` runs the checks; if ruff reformats a file the commit
aborts, so re-stage (`git add`) and commit again. Run against the whole repo
manually:

```bash
pre-commit run --all-files
```

### Continuous integration

`.github/workflows/ci.yml` runs on every push to `main` and every pull request,
across Python 3.9 and 3.13. It installs deps with uv (`uv sync`, cached) and
runs `ruff check`, `ruff format --check`, then `pytest`. The same checks passing
locally means CI passes — status shows in the badge at the top of this README.

## Notes & limitations

- Recording is continuous (24/7) via RTSP stream-copy — low CPU, but steady
  data volume. To back up motion events only, the ingest stage would swap
  ffmpeg for the UniFi Protect API (`uiprotect`).
- `-c copy` keeps original codec/quality; playback needs a player that
  supports the camera's codec (H.264/H.265).
- Encrypted files are opaque to Google — filenames reveal camera + timestamp
  only.
