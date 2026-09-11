from __future__ import annotations

import json
import os
import traceback
from pathlib import Path

import pytest

from sessionctl import MigrationResult, SessionCtlError, migrate_legacy_sessions
from telegram_sessions import SessionDirectoryLock


class Policy:
    def prepare_directory(self, path):
        Path(path).mkdir(parents=True, exist_ok=True)

    def verify_staged_file(self, path):
        pass


class Rig:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.config = tmp_path / "config.ini"
        self.session_dir = tmp_path / "new-sessions"
        self.accounts_file = tmp_path / "accounts.json"
        self.mapping = {}
        self.copies = []
        self.client_paths = []
        monkeypatch.delenv("TELEGRAM_SESSION_STRING", raising=False)

    def write_config(self, *, session="", accounts_file=None, env_file=""):
        lines = ["[telegram]", "api_id = 123", "api_hash = hash"]
        if session:
            lines.append(f"session = {session}")
        if accounts_file is not None:
            lines.append(f"accounts_file = {accounts_file}")
        if env_file:
            lines.extend(["", "[env]", f"env_file = {env_file}"])
        self.config.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def write_accounts(self, rows):
        self.accounts_file.write_text(
            json.dumps({
                "accounts": [
                    {"telegram_user_id": user_id, "label": f"a{user_id}", "session": raw}
                    for user_id, raw in rows
                ]
            }),
            encoding="utf-8",
        )

    def copier(self, raw, destination):
        self.copies.append(raw)
        Path(destination).write_bytes(raw.encode("utf-8"))

    def client_factory(self, path, api_id, api_hash):
        self.client_paths.append(Path(path))
        raw = Path(path).read_text("utf-8")
        outcome = self.mapping[raw]

        class Client:
            async def connect(self_inner):
                if isinstance(outcome, BaseException):
                    raise outcome

            async def is_user_authorized(self_inner):
                return not isinstance(outcome, BaseException)

            async def get_me(self_inner):
                return type("Me", (), {"id": outcome})()

            async def disconnect(self_inner):
                pass

        return Client()

    def matcher(self, existing, raw):
        return Path(existing).read_bytes() == raw.encode("utf-8")

    def migrate(self, mapping):
        self.mapping = mapping
        return migrate_legacy_sessions(
            self.config,
            self.session_dir,
            client_factory=self.client_factory,
            session_copier=self.copier,
            existing_matcher=self.matcher,
            permission_policy=Policy(),
        )


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def test_accounts_file_is_authoritative_and_first_account_becomes_primary(rig):
    rig.write_accounts([(20, "s20"), (3, "s3")])
    rig.write_config(session="ignored", accounts_file=rig.accounts_file)
    result = rig.migrate({"s20": 20, "s3": 3, "ignored": 99})
    assert result == MigrationResult(
        20, rig.session_dir.resolve(), (20, 3), ("[telegram].session",)
    )
    assert sorted(path.name for path in rig.session_dir.glob("*.session")) == [
        "20.session", "3.session"
    ]
    text = rig.config.read_text("utf-8")
    assert "primary_user_id = 20" in text
    assert f"session_dir = {rig.session_dir.resolve()}" in text
    assert "\naccounts_file =" not in text
    assert "\nsession =" not in text
    assert "ignored" not in rig.copies


def test_any_account_failure_preserves_original_config_and_finalizes_nothing(rig):
    rig.write_accounts([(1, "good"), (2, "bad")])
    rig.write_config(accounts_file=rig.accounts_file)
    before = rig.config.read_bytes()
    with pytest.raises(SessionCtlError, match="account 2") as raised:
        rig.migrate({"good": 1, "bad": RuntimeError(f"unauthorized {rig.session_dir}")})
    assert rig.config.read_bytes() == before
    assert not list(rig.session_dir.glob("*.session"))
    rendered = "".join(traceback.format_exception(raised.value))
    assert str(rig.session_dir) not in rendered
    assert raised.value.__cause__ is None


def test_single_source_precedence_is_config_then_environment_then_env_file(rig):
    env_file = rig.tmp_path / ".env"
    env_file.write_text("TELEGRAM_SESSION_STRING=from-file\n", encoding="utf-8")
    rig.monkeypatch.setenv("TELEGRAM_SESSION_STRING", "from-env")
    rig.write_config(session="from-config", env_file=env_file)
    result = rig.migrate({"from-config": 7, "from-env": 8, "from-file": 9})
    assert result.primary_user_id == 7
    assert rig.copies == ["from-config"]
    assert "TELEGRAM_SESSION_STRING" in result.obsolete_sources


def test_environment_precedes_env_file_when_config_session_is_blank(rig):
    env_file = rig.tmp_path / ".env"
    env_file.write_text("TELEGRAM_SESSION_STRING=from-file\n", encoding="utf-8")
    rig.monkeypatch.setenv("TELEGRAM_SESSION_STRING", "from-env")
    rig.write_config(env_file=env_file)
    result = rig.migrate({"from-env": 8, "from-file": 9})
    assert result.primary_user_id == 8
    assert rig.copies == ["from-env"]


def test_configured_and_actual_id_mismatch_is_rejected_without_path_or_secret(rig):
    secret = "top-" + "secret-string"
    rig.write_accounts([(2, secret)])
    rig.write_config(accounts_file=rig.accounts_file)
    before = rig.config.read_bytes()
    with pytest.raises(SessionCtlError, match="account 2.*got 22") as raised:
        rig.migrate({secret: 22})
    rendered = "".join(traceback.format_exception(raised.value))
    assert secret not in rendered
    assert str(rig.config) not in rendered
    assert rig.config.read_bytes() == before


def test_duplicate_actual_ids_are_rejected(rig):
    rig.write_accounts([(1, "a"), (2, "b")])
    rig.write_config(accounts_file=rig.accounts_file)
    with pytest.raises(SessionCtlError, match="duplicate.*7"):
        rig.migrate({"a": 7, "b": 7})


def test_rerun_adopts_matching_existing_destination_but_refuses_conflict(rig):
    rig.write_config(session="same")
    rig.session_dir.mkdir()
    existing = rig.session_dir / "7.session"
    existing.write_bytes(b"same")
    result = rig.migrate({"same": 7})
    assert result.account_ids == (7,)
    assert existing.read_bytes() == b"same"

    rig.write_config(session="different")
    with pytest.raises(SessionCtlError, match="account 7.*conflict"):
        rig.migrate({"different": 7})
    assert existing.read_bytes() == b"same"


def test_lock_contention_fails_before_legacy_credentials_are_copied(rig):
    rig.write_config(session="secret")
    Policy().prepare_directory(rig.session_dir)
    held = SessionDirectoryLock(rig.session_dir).acquire()
    try:
        with pytest.raises(SessionCtlError, match="already in use"):
            rig.migrate({"secret": 1})
    finally:
        held.release()
    assert rig.copies == []


def test_migration_does_not_create_plaintext_backup(rig):
    rig.write_config(session="secret")
    rig.migrate({"secret": 11})
    assert not list(rig.tmp_path.glob("*.bak"))
    assert "secret" not in rig.config.read_text("utf-8")
