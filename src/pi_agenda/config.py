from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _path_env(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser().resolve()


@dataclass(frozen=True)
class RuntimeConfig:
    data_dir: Path
    db_path: Path
    secret_path: Path
    host: str
    port: int
    cache_port: int
    testing: bool
    max_upload_bytes: int
    max_remote_bytes: int
    max_archive_bytes: int

    @classmethod
    def from_env(cls) -> RuntimeConfig:
        data_dir = _path_env("PI_AGENDA_DATA_DIR", "/var/lib/pi-agenda")
        return cls(
            data_dir=data_dir,
            db_path=_path_env("PI_AGENDA_DB", str(data_dir / "db.sqlite")),
            secret_path=_path_env(
                "PI_AGENDA_SECRET_FILE", str(data_dir / "secrets" / "flask-secret")
            ),
            # The authenticated management interface is intentionally exposed on the LAN.
            host=os.environ.get("PI_AGENDA_HOST", "0.0.0.0"),  # nosec B104
            port=int(os.environ.get("PI_AGENDA_PORT", "8000")),
            cache_port=int(os.environ.get("PI_AGENDA_CACHE_PORT", "8002")),
            testing=os.environ.get("PI_AGENDA_TESTING", "0") == "1",
            max_upload_bytes=int(
                os.environ.get("PI_AGENDA_MAX_UPLOAD_BYTES", str(512 * 1024 * 1024))
            ),
            max_remote_bytes=int(
                os.environ.get("PI_AGENDA_MAX_REMOTE_BYTES", str(128 * 1024 * 1024))
            ),
            max_archive_bytes=int(
                os.environ.get("PI_AGENDA_MAX_ARCHIVE_BYTES", str(64 * 1024 * 1024))
            ),
        )

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.data_dir / "secrets",
            self.data_dir / "uploads",
            self.data_dir / "generations",
            self.data_dir / "thumbs",
            self.data_dir / "staging",
            self.data_dir / "backups",
        ):
            path.mkdir(parents=True, exist_ok=True)


DEFAULT_SETTINGS = {
    "resolution": "720p",
    "timezone": "America/Chicago",
    "check_time": "06:00",
    "site_name": "Pi-Agenda",
    "default_duration_sec": "20",
    "default_slide_sec": "10",
    "default_volume": "80",
    "content_quota_bytes": str(20 * 1024 * 1024 * 1024),
    "minimum_free_bytes": str(1024 * 1024 * 1024),
    "intranet_allowlist": "",
    "remote_player_enabled": "0",
    "playlist_version": "1",
    "boot_splash_seconds": "5",
    "internet_online": "0",
    "internet_checked_at": "",
    "last_daily_refresh_date": "",
    "display_power_state": "unknown",
    "display_desired_state": "unknown",
    "display_power_pending_at": "",
    "player_heartbeat": "",
    "player_visual_state": "unknown",
    "current_item_id": "",
}
