# Videobackup — Setup Guide

End-to-end setup for encrypted UniFi Protect → Google Drive backup. Follow in
order. Steps 1–3 are one-time prep on other machines/devices; steps 4–8 are on
the Linux backup host; steps 9–10 are optional service deployment.

---

## 1. Check system dependencies

On the Linux backup host:

```bash
ffmpeg -version | head -1
gpg --version | head -1
rclone version | head -1
python3 --version
```

All four must be present. Install any missing:

```bash
sudo apt update && sudo apt install -y ffmpeg gnupg rclone python3-venv   # Debian/Ubuntu
```

> `rclone` from distro repos can be old. If Google Drive auth misbehaves,
> install the latest: `curl https://rclone.org/install.sh | sudo bash`.

---

## 2. Enable RTSP on each camera (UniFi Protect)

For every camera you want to back up:

1. Open UniFi Protect (web or app).
2. **Camera → Settings → Advanced → RTSP**.
3. Toggle on a stream (High/Medium/Low — higher = more disk/bandwidth).
4. Copy the URL, e.g. `rtsp://192.168.1.1:7447/aBcDeFgHiJkLmNoP`.

Record the URLs; you'll paste them into the config in step 7.

> The path id in the URL is effectively a stream credential. Keep it private.

---

## 3. Create a GPG keypair (on a TRUSTED, ideally OFFLINE machine)

Do **not** generate the keypair on the backup host — it should never hold the
private key.

```bash
gpg --full-generate-key
# Choose: RSA 4096 (or ECC/ed25519), no expiry or your preference,
# real name + email — the email becomes your `gpg_recipient`.
```

Export both halves:

```bash
# Public key -> copy this to the backup host
gpg --armor --export you@example.com > public.asc

# Private key -> BACK UP SECURELY (offline: encrypted USB, paper, password mgr).
# You need it ONLY to restore/decrypt footage. Never put it on the backup host.
gpg --armor --export-secret-keys you@example.com > private.asc
```

Store `private.asc` safely offline. Losing it = footage unrecoverable.

---

## 4. Copy the public key to the backup host

Transfer `public.asc` to the backup host (scp/USB), then import it:

```bash
gpg --import public.asc
gpg --list-keys           # confirm your key/email is listed
```

Only the public key lives here — nothing secret to steal.

---

## 5. Install the application

On the backup host, in the project directory:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
videobackup --help        # confirm the CLI is available
```

---

## 6. Configure rclone for Google Drive

Note: Any other rclone remote could work (MinIO, AWS S3, ...)

```bash
rclone config
```

- `n` (new remote) → name it `gdrive` → storage type `drive` (Google Drive).
- Leave client_id/secret blank (or supply your own for higher quotas).
- Scope: `1` (full) or `drive.file` for app-created files only.
- Complete the browser OAuth. On a headless host, use `rclone authorize` from a
  desktop machine and paste the token.

Verify:

```bash
rclone lsd gdrive:        # should list your Drive folders
```

---

## 7. Write and validate the config

```bash
cp config.yaml.example config.yaml
$EDITOR config.yaml
```

Fill in:
- `cameras[]` — name + RTSP URL for each of the 3 cameras (from step 1)
- `gpg_recipient` — the email/key id from step 2
- `rclone_remote: gdrive`, `drive_folder: unifi-backup`
- `max_drive_bytes` — storage cap (e.g. `107374182400` = 100 GiB)
- `max_age_days` — optional age cap (`0` = off)
- `use_trash` — `false` (default) deletes pruned files permanently so they free
  quota; `true` keeps them recoverable in Drive trash (still counts against quota)
- `local_spool` — working dir on a **real disk** (not `/tmp`, which is often
  tmpfs/RAM); needs room for a few minutes of footage plus retry buffer

Tuning (optional, sensible defaults):
- `encrypt_interval_seconds` — how often plaintext is encrypted+removed (default 20)
- `upload_transfers` — parallel rclone transfers (default 4; raise on a fast uplink)
- `batch_interval_seconds` — idle prune cadence (default 300); uploads are
  continuous and not gated by this

Create the spool dir and lock it down:

```bash
mkdir -p /var/lib/videobackup/spool && chmod 700 /var/lib/videobackup/spool
```

Validate:

```bash
videobackup -c config.yaml check
```

---

## 8. Lock down credential files

```bash
chmod 600 config.yaml ~/.config/rclone/rclone.conf
```

Both grant access (RTSP tokens; unencrypted Drive OAuth token). Optionally
password-encrypt rclone's config via `rclone config` → "Set configuration
password".

---

## 9. Test end-to-end

Quick single-process run (records + encrypts + uploads + prunes):

```bash
videobackup -c config.yaml -v run
```

Let it run ~10 minutes, then verify each stage:

```bash
ls <local_spool>/raw          # mp4 segments appear then get consumed
ls <local_spool>/encrypted    # transient .gpg files before upload
rclone ls gdrive:unifi-backup # encrypted files on Drive
```

Confirm you can decrypt (needs the private key from step 2, on your offline
machine):

```bash
rclone copy gdrive:unifi-backup/<file>.mp4.gpg .
gpg --decrypt <file>.mp4.gpg > clip.mp4   # plays in any H.264/H.265 player
```

Test retention: temporarily set a tiny `max_drive_bytes`, run
`videobackup -c config.yaml prune`, confirm oldest files are removed and total
stays under the cap. Restore your real cap afterward.

With `use_trash: false` (default) prunes are permanent and free quota
immediately. If you ran earlier versions (or `use_trash: true`), empty any
accumulated Drive trash once: `rclone cleanup gdrive:`.

Stop the test run with `Ctrl-C`.

---

## 10. Run as a service (systemd) — recommended for production

Recorder runs continuously; encrypt/upload/prune runs on a timer.

```bash
# Adjust User=, WorkingDirectory, and config path inside the unit files first.
sudo cp systemd/videobackup-record.service /etc/systemd/system/
sudo cp systemd/videobackup-batch.service  /etc/systemd/system/
sudo cp systemd/videobackup-batch.timer    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now videobackup-record.service
sudo systemctl enable --now videobackup-batch.timer
```

> The systemd `User=` must be the account that ran `rclone config` — the Drive
> token lives in that user's `~/.config/rclone/rclone.conf`.

Check status / logs:

```bash
systemctl status videobackup-record.service
systemctl list-timers videobackup-batch.timer
journalctl -u videobackup-record.service -f
```

---

## 11. Restore footage (when needed)

On your offline machine that holds the private key:

```bash
rclone copy gdrive:unifi-backup/front_door_20260716_120000.mp4.gpg .
gpg --decrypt front_door_20260716_120000.mp4.gpg > front_door_120000.mp4
```

Filenames encode `<camera>_<YYYYMMDD>_<HHMMSS>.mp4.gpg`, so you can pull a
specific camera/time window with `rclone ls`/`rclone copy` filters.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `ffmpeg not found` | Install ffmpeg; ensure it's on the service user's PATH. |
| No segments in `raw/` | Wrong RTSP URL, or RTSP not enabled on the camera. Test: `ffmpeg -rtsp_transport tcp -i <url> -t 10 test.mp4`. |
| `gpg: no such recipient` | Public key not imported, or `gpg_recipient` mismatch. `gpg --list-keys`. |
| Uploads fail | `rclone lsd gdrive:` to test auth; re-run `rclone config`. |
| Drive grows past cap | Check batch timer is active; `videobackup -c config.yaml prune -v`. |
| `encrypted/` keeps growing | Upload can't keep up: upstream bandwidth < camera bitrate. Lower camera RTSP to Medium/Low substream, or raise `upload_transfers`. |
| Quota full despite pruning | Old trash from `use_trash: true` (or older versions). Empty it: `rclone cleanup gdrive:`. |
| `raw/` fills RAM / lost on reboot | `local_spool` is on `/tmp` (tmpfs). Point it at a real disk path. |
| Service can't reach Drive | `User=` differs from who ran `rclone config`. |
