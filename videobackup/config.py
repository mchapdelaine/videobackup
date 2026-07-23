"""Configuration loading and validation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when the configuration file is missing required or valid values."""


@dataclass(frozen=True)
class Camera:
    name: str
    rtsp_url: str


@dataclass(frozen=True)
class Config:
    udm_host: str
    cameras: list[Camera]
    gpg_recipient: str
    rclone_remote: str
    drive_folder: str
    max_drive_bytes: int
    local_spool: Path
    segment_seconds: int = 300
    max_age_days: int = 0
    batch_interval_seconds: int = 300
    encrypt_interval_seconds: int = 20
    upload_transfers: int = 4
    use_trash: bool = False

    @property
    def spool_raw(self) -> Path:
        """Where ffmpeg writes plaintext segments."""
        return self.local_spool / "raw"

    @property
    def spool_encrypted(self) -> Path:
        """Where encrypted .gpg files wait to be uploaded."""
        return self.local_spool / "encrypted"

    @property
    def remote_path(self) -> str:
        """rclone destination, e.g. 'gdrive:unifi-backup'."""
        return f"{self.rclone_remote}:{self.drive_folder}"


def _as_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "1", "on"):
            return True
        if low in ("false", "no", "0", "off"):
            return False
    raise ConfigError(f"{key!r} must be a boolean (true/false)")


def _require(data: dict[str, Any], key: str) -> Any:
    if key not in data or data[key] in (None, ""):
        raise ConfigError(f"Missing required config key: {key!r}")
    return data[key]


def _parse_cameras(raw: Any) -> list[Camera]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("'cameras' must be a non-empty list")
    cameras: list[Camera] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"cameras[{i}] must be a mapping")
        name = entry.get("name")
        url = entry.get("rtsp_url")
        if not name:
            raise ConfigError(f"cameras[{i}] missing 'name'")
        if not url:
            raise ConfigError(f"camera {name!r} missing 'rtsp_url'")
        if name in seen:
            raise ConfigError(f"duplicate camera name: {name!r}")
        seen.add(name)
        cameras.append(Camera(name=str(name), rtsp_url=str(url)))
    return cameras


def load_config(path: str | os.PathLike[str]) -> Config:
    """Load, validate, and return the configuration from a YAML file."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")

    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError("Config root must be a mapping")

    cameras = _parse_cameras(_require(data, "cameras"))

    try:
        max_drive_bytes = int(_require(data, "max_drive_bytes"))
        segment_seconds = int(data.get("segment_seconds", 300))
        max_age_days = int(data.get("max_age_days", 0))
        batch_interval_seconds = int(data.get("batch_interval_seconds", 300))
        encrypt_interval_seconds = int(data.get("encrypt_interval_seconds", 20))
        upload_transfers = int(data.get("upload_transfers", 4))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Numeric config value invalid: {exc}") from exc

    if max_drive_bytes <= 0:
        raise ConfigError("'max_drive_bytes' must be positive")
    if segment_seconds <= 0:
        raise ConfigError("'segment_seconds' must be positive")
    if max_age_days < 0:
        raise ConfigError("'max_age_days' must be >= 0")
    if encrypt_interval_seconds <= 0:
        raise ConfigError("'encrypt_interval_seconds' must be positive")
    if upload_transfers <= 0:
        raise ConfigError("'upload_transfers' must be positive")

    use_trash = _as_bool(data.get("use_trash", False), "use_trash")

    return Config(
        udm_host=str(data.get("udm_host", "")),
        cameras=cameras,
        gpg_recipient=str(_require(data, "gpg_recipient")),
        rclone_remote=str(_require(data, "rclone_remote")),
        drive_folder=str(_require(data, "drive_folder")),
        max_drive_bytes=max_drive_bytes,
        local_spool=Path(str(_require(data, "local_spool"))).expanduser(),
        segment_seconds=segment_seconds,
        max_age_days=max_age_days,
        batch_interval_seconds=batch_interval_seconds,
        encrypt_interval_seconds=encrypt_interval_seconds,
        upload_transfers=upload_transfers,
        use_trash=use_trash,
    )


def ensure_spool_dirs(config: Config) -> None:
    """Create the local spool subdirectories if they don't exist."""
    config.spool_raw.mkdir(parents=True, exist_ok=True)
    config.spool_encrypted.mkdir(parents=True, exist_ok=True)
