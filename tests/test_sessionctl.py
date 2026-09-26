from __future__ import annotations

import os
import stat
import traceback
from pathlib import Path

import pytest

from sessionctl import (
    PosixSessionPermissionPolicy,
    SessionCtlError,
    SessionExistsError,
    login_session,
    resolve_session_dir_for_cli,
)
from telegram_sessions import SessionDirectoryLock


class FakePolicy:
    def __init__(self):
        self.prepared = []
        self.verified = []

    def prepare_directory(self, path: Path) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)
        self.prepared.append(Path(path))

    def verify_staged_file(self, path: Path) -> None:
        self.verified.append(Path(path))


class FakeClient:
    def __init__(self, path: str, user_id=42):
        self.path = Path(path)
        self.user_id = user_id
        self.start_error = None
        self.disconnect_error = None
        self.disconnect_calls = 0
        self.path.write_bytes(b"sqlite-session")

    async def start(self):
        if self.start_error:
            raise self.start_error

    async def get_me(self):
        return type("Me", (), {"id": self.user_id})()

    async def disconnect(self):
        self.disconnect_calls += 1
        if self.disconnect_error:
            raise self.disconnect_error


class Rig:
    def __init__(self, tmp_path: Path):
        self.config = tmp_path / "config.ini"
        self.config.write_text("[telegram]\napi_id = 123\napi_hash = hash\n", encoding="utf-8")
        self.original_config = self.config.read_bytes()
        self.session_dir = tmp_path / "private-sessions"
        self.policy = FakePolicy()
        self.clients = []

    def login(self, user_id=42, configure=None):
        def factory(path, api_id, api_hash):
            assert (api_id, api_hash) == (123, "hash")
            client = FakeClient(path, user_id=user_id)
            if configure:
                configure(client)
            self.clients.append(client)
            return client

        return login_session(
            self.config,
            self.session_dir,
            client_factory=factory,
            permission_policy=self.policy,
        )


def test_login_names_session_from_get_me_and_does_not_edit_config(tmp_path):
    rig = Rig(tmp_path)
    result = rig.login(user_id=42)
    assert result == rig.session_dir / "42.session"
    assert result.read_bytes() == b"sqlite-session"
    assert rig.config.read_bytes() == rig.original_config
    assert rig.policy.prepared == [rig.session_dir]
    assert len(rig.policy.verified) == 1
    assert rig.policy.verified[0].parent.parent == rig.session_dir


def test_login_refuses_existing_destination_byte_for_byte(tmp_path):
    rig = Rig(tmp_path)
    rig.session_dir.mkdir()
    destination = rig.session_dir / "42.session"
    destination.write_bytes(b"existing")
    with pytest.raises(SessionExistsError, match="account 42") as raised:
        rig.login(user_id=42)
    assert destination.read_bytes() == b"existing"
    rendered = "".join(traceback.format_exception(raised.value))
    assert str(destination) not in rendered
    assert raised.value.__cause__ is None


def test_start_and_disconnect_failure_preserves_sanitized_start_error(tmp_path):
    rig = Rig(tmp_path)
    secret = rig.session_dir / ".private-staging-name"

    def configure(client):
        client.start_error = RuntimeError(f"login failed at {secret}")
        client.disconnect_error = ValueError("disconnect also failed")

    with pytest.raises(SessionCtlError, match="RuntimeError") as raised:
        rig.login(configure=configure)
    rendered = "".join(traceback.format_exception(raised.value))
    assert str(secret) not in rendered
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert not list(rig.session_dir.glob("*.session"))


def test_disconnect_failure_after_success_is_sanitized_and_not_promoted(tmp_path):
    rig = Rig(tmp_path)

    def configure(client):
        client.disconnect_error = RuntimeError(f"disconnect failed {client.path}")

    with pytest.raises(SessionCtlError, match="RuntimeError") as raised:
        rig.login(configure=configure)
    assert str(rig.session_dir) not in "".join(traceback.format_exception(raised.value))
    assert not list(rig.session_dir.glob("*.session"))


def test_lock_contention_happens_before_client_construction(tmp_path):
    rig = Rig(tmp_path)
    rig.policy.prepare_directory(rig.session_dir)
    held = SessionDirectoryLock(rig.session_dir).acquire()
    calls = []
    try:
        with pytest.raises(SessionCtlError, match="already in use"):
            login_session(
                rig.config,
                rig.session_dir,
                client_factory=lambda *_a: calls.append(True),
                permission_policy=rig.policy,
            )
    finally:
        held.release()
    assert calls == []


def test_relative_session_dir_is_resolved_against_config_directory(tmp_path):
    config = tmp_path / "cfg" / "config.ini"
    config.parent.mkdir()
    config.write_text("[telegram]\napi_id=1\napi_hash=h\n", encoding="utf-8")
    assert resolve_session_dir_for_cli(config, Path("../secrets")) == (
        tmp_path / "secrets"
    ).resolve()


def test_posix_policy_creates_private_directory_and_file(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX permissions")
    directory = tmp_path / "sessions"
    policy = PosixSessionPermissionPolicy()
    policy.prepare_directory(directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    staged = directory / "pending.session"
    staged.write_bytes(b"sqlite")
    policy.verify_staged_file(staged)
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600


def test_posix_policy_rejects_existing_broad_directory(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX permissions")
    directory = tmp_path / "sessions"
    directory.mkdir(mode=0o755)
    os.chmod(directory, 0o755)
    with pytest.raises(SessionCtlError, match="permissions") as raised:
        PosixSessionPermissionPolicy().prepare_directory(directory)
    assert str(directory) not in str(raised.value)
