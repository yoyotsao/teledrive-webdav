from __future__ import annotations

from pathlib import Path

import pytest

from config import ConfigError, load_config
from transfer_models import UploadedPart


def write_config(tmp_path: Path, extra: str = "") -> Path:
    sessions = tmp_path / "sessions"
    sessions.mkdir(exist_ok=True)
    path = tmp_path / "config.ini"
    path.write_text(
        "[telegram]\n"
        "api_id = 123\n"
        "api_hash = hash\n"
        "primary_user_id = 123\n"
        "session_dir = sessions\n"
        f"{extra}",
        encoding="utf-8",
    )
    return path


def load_minimal_config(tmp_path: Path, monkeypatch) -> object:
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.delenv("TELEGRAM_SESSION_STRING", raising=False)
    return load_config(write_config(tmp_path))


def test_parity_defaults(tmp_path, monkeypatch):
    cfg = load_minimal_config(tmp_path, monkeypatch)
    assert (cfg.upload_files, cfg.upload_parts) == (3, 12)
    assert (cfg.hash_concurrency, cfg.hash_check_concurrency) == (2, 8)
    assert (cfg.register_concurrency, cfg.album_batch) == (8, 10)
    assert (cfg.album_timeout_seconds, cfg.message_rate, cfg.message_burst) == (60.0, 3.0, 6)


def test_non_positive_concurrency_is_rejected(tmp_path, monkeypatch):
    path = write_config(tmp_path, "[upload]\nhash_concurrency = 0\n")
    with pytest.raises(ConfigError, match="hash_concurrency"):
        load_config(path)


def test_non_finite_rate_is_rejected(tmp_path):
    path = write_config(tmp_path, "[upload]\nmessage_rate = inf\n")
    with pytest.raises(ConfigError, match="message_rate"):
        load_config(path)


def test_uploaded_part_requires_storage_identity():
    part = UploadedPart(0, 12, "991", None, 7, 44)
    assert (part.index, part.telegram_user_id, part.file_id) == (0, 44, "991")


def test_config_relative_session_path_and_blank_ffmpeg(tmp_path):
    path = write_config(tmp_path, "\n[upload]\nffmpeg =\n")
    cfg = load_config(path)
    assert cfg.session_dir == tmp_path / "sessions"
    assert cfg.ffmpeg == ""


def test_primary_user_id_must_be_positive(tmp_path):
    path = write_config(tmp_path)
    text = path.read_text("utf-8").replace("primary_user_id = 123", "primary_user_id = 0")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="primary_user_id"):
        load_config(path)
