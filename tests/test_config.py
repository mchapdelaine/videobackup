import textwrap

import pytest

from videobackup.config import ConfigError, load_config

VALID = """
udm_host: 192.168.1.1
cameras:
  - name: front
    rtsp_url: rtsp://192.168.1.1:7447/aaa
  - name: back
    rtsp_url: rtsp://192.168.1.1:7447/bbb
gpg_recipient: you@example.com
rclone_remote: gdrive
drive_folder: unifi-backup
max_drive_bytes: 107374182400
local_spool: /tmp/videobackup
"""


def _write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(text))
    return p


def test_valid_config(tmp_path):
    cfg = load_config(_write(tmp_path, VALID))
    assert len(cfg.cameras) == 2
    assert cfg.cameras[0].name == "front"
    assert cfg.max_drive_bytes == 107374182400
    assert cfg.segment_seconds == 300  # default
    assert cfg.remote_path == "gdrive:unifi-backup"
    assert cfg.spool_raw.name == "raw"


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_missing_required_key(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, VALID.replace("gpg_recipient: you@example.com", "")))


def test_empty_cameras(tmp_path):
    bad = VALID.replace(
        "cameras:\n  - name: front\n    rtsp_url: rtsp://192.168.1.1:7447/aaa\n"
        "  - name: back\n    rtsp_url: rtsp://192.168.1.1:7447/bbb",
        "cameras: []",
    )
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, bad))


def test_duplicate_camera_name(tmp_path):
    bad = VALID.replace("name: back", "name: front")
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, bad))


def test_negative_cap_rejected(tmp_path):
    bad = VALID.replace("max_drive_bytes: 107374182400", "max_drive_bytes: -1")
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, bad))


def test_use_trash_defaults_false(tmp_path):
    assert load_config(_write(tmp_path, VALID)).use_trash is False


def test_use_trash_parsed(tmp_path):
    cfg = load_config(_write(tmp_path, VALID + "use_trash: true\n"))
    assert cfg.use_trash is True


def test_use_trash_invalid_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, VALID + "use_trash: maybe\n"))
