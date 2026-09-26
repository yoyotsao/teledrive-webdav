"""Configuration for the TeleDrive WebDAV bridge.

Secret application credentials may fall back from config.ini to the process
environment and the optional env_file. Telegram account authorization itself is
never loaded from plaintext runtime configuration: accounts are discovered from
SQLite session files in ``session_dir``.
"""

from __future__ import annotations

import configparser
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
log = logging.getLogger("config")

_ENV_KEYS = {
    "api_id": "TELEGRAM_API_ID",
    "api_hash": "TELEGRAM_API_HASH",
    "base_url": "TELEDRIVE_BASE_URL",
}
_LEGACY_SESSION_ENV = "TELEGRAM_" + "SESSION_STRING"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    primary_user_id: int
    session_dir: Path = field(repr=False)
    base_url: str = "http://127.0.0.1:8000"
    game_folder: str = "game"
    dir_cache_seconds: float = 3600.0
    host: str = "127.0.0.1"
    port: int = 8081
    mount_drive: str = "E:"
    log_level: str = "INFO"
    cache_dir: Path = Path("data/meta")
    local_dir: Path = Path("data/local")
    staging_dir: Path = Path("data/staging")
    debounce_minutes: float = 5.0
    download_connections: int = 8
    rclone_dir: Path = Path("data/rclone")
    warmup_auto: bool = True
    warmup_interval_minutes: float = 360.0
    upload_files: int = 3
    upload_parts: int = 12
    hash_concurrency: int = 2
    hash_check_concurrency: int = 8
    register_concurrency: int = 8
    album_batch: int = 10
    album_timeout_seconds: float = 60.0
    message_rate: float = 3.0
    message_burst: int = 6
    ffmpeg: str = ""
    upload_dir: Path = Path("data/uploads")

    @property
    def api_base(self) -> str:
        return self.base_url.rstrip("/") + "/api/v1"

    @property
    def pack_dir(self) -> Path:
        return self.staging_dir / ".pack"


def ext_path(path) -> str:
    r"""Windows extended path (\\?\...) so paths longer than MAX_PATH still open."""
    text = str(path)
    if os.name == "nt" and not text.startswith("\\\\?\\"):
        absolute = os.path.abspath(text)
        if absolute.startswith("\\\\"):
            return "\\\\?\\UNC" + absolute[1:]
        return "\\\\?\\" + absolute
    return text


def load_endpoint(path: Optional[Path] = None) -> tuple:
    """Return endpoint settings without requiring Telegram credentials."""
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


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
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


def positive_int(name: str, raw: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be a positive integer, got {raw!r}") from None
    if value <= 0:
        raise ConfigError(f"{name} must be a positive integer, got {raw!r}")
    return value


def positive_float(name: str, raw: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be a positive number, got {raw!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be a positive number, got {raw!r}")
    return value


def load_config(path: Optional[Path] = None) -> Config:
    if path is None:
        env_path = os.environ.get("TELEDRIVE_WEBDAV_CONFIG")
        path = Path(env_path) if env_path else HERE / "config.ini"
    path = Path(path)
    config_dir = path.resolve().parent

    parser = configparser.ConfigParser()
    parser.read([HERE / "config.example.ini", path], encoding="utf-8")

    env_file = parser.get("env", "env_file", fallback="").strip()
    env_file_path = Path(env_file)
    if env_file and not env_file_path.is_absolute():
        env_file_path = config_dir / env_file_path
    file_env = _read_env_file(env_file_path) if env_file else {}

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
        if not p.is_absolute():
            p = config_dir / p
        return p.resolve()

    data_root = resolve_dir(get("paths", "cache_dir", "data"))

    api_id = get("telegram", "api_id")
    api_hash = get("telegram", "api_hash")
    primary_raw = parser.get("telegram", "primary_user_id", fallback="").strip()
    session_dir_raw = parser.get("telegram", "session_dir", fallback="").strip()

    legacy_config_keys = tuple(
        key for key in ("session", "accounts_file")
        if parser.get("telegram", key, fallback="").strip()
    )
    new_complete = bool(primary_raw and session_dir_raw)
    legacy_env_present = bool(
        os.environ.get(_LEGACY_SESSION_ENV) or file_env.get(_LEGACY_SESSION_ENV)
    )

    if legacy_config_keys:
        key = legacy_config_keys[0]
        if new_complete:
            raise ConfigError(
                f"legacy Telegram setting '{key}' conflicts with session_dir configuration; "
                "remove it after migration"
            ) from None
        raise ConfigError(
            f"legacy Telegram setting '{key}' is no longer supported at runtime; "
            "run sessionctl.py migrate"
        ) from None
    if not new_complete and legacy_env_present:
        raise ConfigError(
            f"legacy Telegram setting '{_LEGACY_SESSION_ENV}' is no longer supported at runtime; "
            "run sessionctl.py migrate"
        ) from None
    if new_complete and legacy_env_present:
        log.warning("obsolete Telegram environment setting %s is ignored", _LEGACY_SESSION_ENV)

    missing = [
        name for name, value in (
            ("api_id", api_id),
            ("api_hash", api_hash),
            ("primary_user_id", primary_raw),
            ("session_dir", session_dir_raw),
        ) if not value
    ]
    if missing:
        raise ConfigError(f"Missing configuration value(s): {', '.join(missing)}")
    if not api_id.isdigit():
        raise ConfigError(f"api_id must be numeric, got {api_id!r}")

    return Config(
        api_id=int(api_id),
        api_hash=api_hash,
        primary_user_id=positive_int("primary_user_id", primary_raw),
        session_dir=resolve_dir(session_dir_raw),
        download_connections=positive_int(
            "download_connections", get("telegram", "download_connections", "8")
        ),
        upload_files=positive_int("upload_files", get("telegram", "upload_files", "3")),
        upload_parts=positive_int("upload_parts", get("telegram", "upload_parts", "12")),
        hash_concurrency=positive_int("hash_concurrency", get("upload", "hash_concurrency", "2")),
        hash_check_concurrency=positive_int(
            "hash_check_concurrency", get("upload", "hash_check_concurrency", "8")
        ),
        register_concurrency=positive_int(
            "register_concurrency", get("upload", "register_concurrency", "8")
        ),
        album_batch=positive_int("album_batch", get("upload", "album_batch", "10")),
        album_timeout_seconds=positive_float(
            "album_timeout_seconds", get("upload", "album_timeout_seconds", "60")
        ),
        message_rate=positive_float("message_rate", get("upload", "message_rate", "3")),
        message_burst=positive_int("message_burst", get("upload", "message_burst", "6")),
        ffmpeg=get("upload", "ffmpeg"),
        base_url=get("teledrive", "base_url", "http://127.0.0.1:8000"),
        game_folder=get("teledrive", "game_folder", "game"),
        dir_cache_seconds=float(get("teledrive", "dir_cache_seconds", "3600")),
        host=get("bridge", "host", "127.0.0.1"),
        port=int(get("bridge", "port", "8081")),
        mount_drive=get("bridge", "mount_drive", "E:").rstrip("\\/"),
        log_level=get("bridge", "log_level", "INFO").upper(),
        cache_dir=data_root / "meta",
        local_dir=data_root / "local",
        staging_dir=data_root / "staging",
        rclone_dir=data_root / "rclone",
        upload_dir=data_root / "uploads",
        debounce_minutes=float(get("game", "debounce_minutes", "5")),
        warmup_auto=get("warmup", "auto", "true").lower() not in ("0", "false", "no", "off"),
        warmup_interval_minutes=float(get("warmup", "interval_minutes", "360")),
    )
