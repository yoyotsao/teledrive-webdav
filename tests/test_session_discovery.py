from __future__ import annotations

import logging
import os
import traceback
from pathlib import Path

import pytest

from config import ConfigError, load_config
from telegram_sessions import discover_account_specs
from transfer_models import AccountSpec


def _root_and_sessions(tmp_path: Path):
    root = tmp_path / "app"
    sessions = tmp_path / "sessions"
    root.mkdir()
    sessions.mkdir()
    return root, sessions


def test_one_file_is_one_account_and_primary_is_first(tmp_path):
    root, sessions = _root_and_sessions(tmp_path)
    (sessions / "42.session").write_bytes(b"sqlite")
    specs = discover_account_specs(sessions, 42, root)
    assert specs == [AccountSpec(42, (sessions / "42.session").resolve())]


def test_secondaries_are_sorted_numerically_after_primary(tmp_path):
    root, sessions = _root_and_sessions(tmp_path)
    for name in ("20.session", "3.session", "10.session"):
        (sessions / name).write_bytes(b"sqlite")
    assert [s.telegram_user_id for s in discover_account_specs(sessions, 20, root)] == [20, 3, 10]


def test_empty_directory_is_rejected(tmp_path):
    root, sessions = _root_and_sessions(tmp_path)
    with pytest.raises(ConfigError, match="no Telegram session files"):
        discover_account_specs(sessions, 1, root)


def test_missing_primary_is_rejected_before_client_creation(tmp_path):
    root, sessions = _root_and_sessions(tmp_path)
    (sessions / "2.session").write_bytes(b"sqlite")
    with pytest.raises(ConfigError, match="primary Telegram account 1"):
        discover_account_specs(sessions, 1, root)


@pytest.mark.parametrize("name", ["0.session", "-1.session", "abc.session", "01.session"])
def test_invalid_candidate_is_identified_without_printing_its_name(tmp_path, name):
    root, sessions = _root_and_sessions(tmp_path)
    (sessions / "1.session").write_bytes(b"sqlite")
    (sessions / name).write_bytes(b"sqlite")
    with pytest.raises(ConfigError) as raised:
        discover_account_specs(sessions, 1, root)
    assert name not in str(raised.value)
    assert "sha256=" in str(raised.value) and "length=" in str(raised.value)


def test_sidecars_and_unrelated_files_are_ignored(tmp_path):
    root, sessions = _root_and_sessions(tmp_path)
    (sessions / "1.session").write_bytes(b"sqlite")
    for name in ("1.session-wal", "1.session-shm", "1.session-journal", ".pending", "notes.txt"):
        (sessions / name).write_bytes(b"ignored")
    assert discover_account_specs(sessions, 1, root) == [AccountSpec(1, (sessions / "1.session").resolve())]


def test_session_directory_must_be_outside_application_root(tmp_path):
    root = tmp_path / "app"
    sessions = root / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "1.session").write_bytes(b"sqlite")
    with pytest.raises(ConfigError, match="outside the application root"):
        discover_account_specs(sessions, 1, root)


def test_symlink_candidate_cannot_escape_session_directory(tmp_path):
    root, sessions = _root_and_sessions(tmp_path)
    outside = tmp_path / "1.session"
    outside.write_bytes(b"sqlite")
    try:
        (sessions / "1.session").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(ConfigError, match="direct regular file"):
        discover_account_specs(sessions, 1, root)


def test_missing_session_path_has_no_path_in_exception_chain_or_traceback(tmp_path, caplog):
    root = tmp_path / "app"
    root.mkdir()
    secret = tmp_path / "private-name" / "credential-dir-xyzz"
    with caplog.at_level(logging.DEBUG), pytest.raises(ConfigError) as raised:
        discover_account_specs(secret, 1, root)
    rendered = "".join(traceback.format_exception(raised.value)) + caplog.text
    assert str(secret) not in rendered
    assert secret.name not in rendered
    assert raised.value.__cause__ is None


def _write_new_config(tmp_path: Path, extra: str = "") -> Path:
    sessions = tmp_path / "sessions"
    sessions.mkdir(exist_ok=True)
    path = tmp_path / "config.ini"
    path.write_text(
        "[telegram]\n"
        "api_id = 123\n"
        "api_hash = hash\n"
        "primary_user_id = 42\n"
        "session_dir = sessions\n"
        f"{extra}",
        encoding="utf-8",
    )
    return path


def test_config_resolves_relative_session_dir_against_config(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_SESSION_STRING", raising=False)
    cfg = load_config(_write_new_config(tmp_path))
    assert cfg.primary_user_id == 42
    assert cfg.session_dir == tmp_path / "sessions"
    assert not hasattr(cfg, "session")
    assert not hasattr(cfg, "accounts_file")


def test_non_empty_legacy_config_conflicts_with_new_settings(tmp_path):
    secret = "legacy-secret-never-render"
    path = _write_new_config(tmp_path, f"session = {secret}\n")
    with pytest.raises(ConfigError) as raised:
        load_config(path)
    assert "legacy telegram setting 'session'" in str(raised.value).lower()
    assert secret not in str(raised.value)


def test_complete_new_settings_ignore_but_warn_legacy_environment(tmp_path, monkeypatch, caplog):
    secret = "legacy-env-secret-never-render"
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", secret)
    with caplog.at_level(logging.WARNING):
        cfg = load_config(_write_new_config(tmp_path))
    assert cfg.primary_user_id == 42
    assert "TELEGRAM_SESSION_STRING" in caplog.text
    assert secret not in caplog.text


def test_legacy_only_config_is_rejected_without_rendering_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_SESSION_STRING", raising=False)
    secret = "legacy-secret-never-render"
    path = tmp_path / "config.ini"
    path.write_text(
        "[telegram]\napi_id = 123\napi_hash = hash\n" f"session = {secret}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="sessionctl") as raised:
        load_config(path)
    assert secret not in str(raised.value)


def test_runtime_has_no_plaintext_session_configuration():
    # Task 6 will keep this as a repository-wide guard; adding it now ensures
    # Task 1 does not accidentally retain the old runtime fallback.
    assert "TELEGRAM_SESSION_STRING" not in Path("config.py").read_text("utf-8")
    assert 'get("telegram", "session")' not in Path("config.py").read_text("utf-8")
