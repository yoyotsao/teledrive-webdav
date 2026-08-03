"""Configuration for the TeleDrive WebDAV bridge.

Resolution order for every value: config.ini -> process environment -> the
optional env_file (defaults to the TeleDrive repo's .env). Credentials therefore
never need to be copied into this repo.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent

# Env-var name each secret falls back to, matching TeleDrive's .env keys.
_ENV_KEYS = {
    "api_id": "TELEGRAM_API_ID",
    "api_hash": "TELEGRAM_API_HASH",
    "session": "TELEGRAM_SESSION_STRING",
    "base_url": "TELEDRIVE_BASE_URL",
}


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    session: str
    base_url: str
    game_folder: str
    dir_cache_seconds: float
    host: str
    port: int
    mount_drive: str
    log_level: str
    cache_dir: Path
    local_dir: Path
    staging_dir: Path
    debounce_minutes: float
    # Last, with defaults: callers that build a Config by hand (the e2e tests)
    # should not have to care about a tuning knob or the rclone side.
    download_connections: int = 8
    rclone_dir: Path = Path("rclone")
    warmup_auto: bool = True
    warmup_interval_minutes: float = 360.0
    upload_parts: int = 12

    @property
    def api_base(self) -> str:
        return self.base_url.rstrip("/") + "/api/v1"

    @property
    def pack_dir(self) -> Path:
        """Where staged trees are zipped.

        Deliberately a sibling of the watched staging tree: writing the zip
        *inside* staging_dir/<top> would look like fresh activity and reset the
        debounce timer forever.
        """
        return self.staging_dir / ".pack"


def ext_path(path) -> str:
    r"""Windows extended path (\\?\...) so paths longer than MAX_PATH still open.

    Lives here because both the packer and the fetch-local client need it, and
    config.py is the one module every entry point already imports.
    """
    text = str(path)
    if os.name == "nt" and not text.startswith("\\\\?\\"):
        absolute = os.path.abspath(text)
        if absolute.startswith("\\\\"):
            return "\\\\?\\UNC" + absolute[1:]
        return "\\\\?\\" + absolute
    return text


def load_endpoint(path: Optional[Path] = None) -> tuple:
    """(host, port, mount_drive) without requiring credentials.

    The Explorer verb runs fetchlocal.py as a thin client; it only needs to know
    where the bridge listens, and must not fail because credentials are absent.
    """
    if path is None:
        env_path = os.environ.get("TELEDRIVE_WEBDAV_CONFIG")
        path = Path(env_path) if env_path else HERE / "config.ini"
    parser = configparser.ConfigParser()
    parser.read([HERE / "config.example.ini", Path(path)], encoding="utf-8")
    return (
        parser.get("bridge", "host", fallback="127.0.0.1").strip() or "127.0.0.1",
        int(parser.get("bridge", "port", fallback="8081").strip() or 8081),
        (parser.get("bridge", "mount_drive", fallback="E:").strip() or "E:").rstrip("\\/"),
    )


def _read_env_file(path: Path) -> dict:
    values = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip("'\"")
    return values


def load_config(path: Optional[Path] = None) -> Config:
    if path is None:
        env_path = os.environ.get("TELEDRIVE_WEBDAV_CONFIG")
        path = Path(env_path) if env_path else HERE / "config.ini"
    path = Path(path)

    parser = configparser.ConfigParser()
    # A missing config.ini is fine as long as the environment carries the
    # credentials; every non-secret setting has a default below.
    parser.read([HERE / "config.example.ini", path], encoding="utf-8")

    env_file = parser.get("env", "env_file", fallback="").strip()
    file_env = _read_env_file(Path(env_file)) if env_file else {}

    def get(section: str, key: str, default: str = "") -> str:
        val = parser.get(section, key, fallback="").strip()
        if val:
            return val
        env_key = _ENV_KEYS.get(key)
        if env_key:
            return (os.environ.get(env_key) or file_env.get(env_key) or default).strip()
        return default

    def resolve_dir(raw: str) -> Path:
        p = Path(raw.strip())
        return p if p.is_absolute() else (HERE / p)

    # One setting names the root; everything under it is this module's business.
    # Splitting it into four settings only invited them to drift apart, and three
    # of the four were never anything a user would want to place individually.
    data_root = resolve_dir(get("paths", "cache_dir", "data"))

    api_id = get("telegram", "api_id")
    api_hash = get("telegram", "api_hash")
    session = get("telegram", "session")
    missing = [n for n, v in (("api_id", api_id), ("api_hash", api_hash), ("session", session)) if not v]
    if missing:
        raise ConfigError(
            f"Missing credential(s): {', '.join(missing)}. Set them in {path.name}, "
            f"in the environment, or in the env_file ({env_file or 'unset'})."
        )
    if not api_id.isdigit():
        raise ConfigError(f"api_id must be numeric, got {api_id!r}")

    return Config(
        api_id=int(api_id),
        api_hash=api_hash,
        session=session,
        download_connections=int(get("telegram", "download_connections", "8")),
        upload_parts=int(get("telegram", "upload_parts", "12")),
        base_url=get("teledrive", "base_url", "http://127.0.0.1:8000"),
        game_folder=get("teledrive", "game_folder", "game"),
        dir_cache_seconds=float(get("teledrive", "dir_cache_seconds", "60")),
        host=get("bridge", "host", "127.0.0.1"),
        port=int(get("bridge", "port", "8081")),
        mount_drive=get("bridge", "mount_drive", "E:").rstrip("\\/"),
        log_level=get("bridge", "log_level", "INFO").upper(),
        # meta/  previews and metadata, safe to delete (a re-warm, not a loss)
        # rclone/ rclone's VFS cache, safe to delete
        # local/  files fetched on purpose — NOT a cache, deleting loses them
        # staging/ /game trees waiting to be packed and uploaded
        cache_dir=data_root / "meta",
        local_dir=data_root / "local",
        staging_dir=data_root / "staging",
        rclone_dir=data_root / "rclone",
        debounce_minutes=float(get("game", "debounce_minutes", "5")),
        warmup_auto=get("warmup", "auto", "true").lower() not in ("0", "false", "no", "off"),
        warmup_interval_minutes=float(get("warmup", "interval_minutes", "360")),
    )
