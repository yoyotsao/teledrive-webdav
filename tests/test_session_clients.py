from __future__ import annotations

import asyncio
import traceback
from pathlib import Path
from types import SimpleNamespace

import pytest

from tgio import (
    SessionAuthorizationError,
    SessionClientError,
    SessionIdentityError,
    TelegramWorker,
)


class FakeSession:
    def __init__(self, name: str):
        self.name = name
        self.save_entities = True


class FakeMemorySession:
    def __init__(self, serialized: str):
        self.serialized = serialized


class FakeClient:
    def __init__(
        self,
        rig: "ClientRig",
        kind: str,
        session,
        *,
        authorized: bool = True,
        actual_user_id: int = 42,
        block_connect: bool = False,
    ):
        self.rig = rig
        self.kind = kind
        self.session = session if kind == "control" else FakeSession(f"{kind}-session")
        self.authorized = authorized
        self.actual_user_id = actual_user_id
        self.block_connect = block_connect

    async def connect(self):
        self.rig.connect_calls.append(self.kind)
        if self.block_connect:
            self.rig.connect_entered.set()
            await self.rig.release_connect.wait()

    async def disconnect(self):
        self.rig.disconnect_order.append(self.kind)

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        self.rig.get_me_calls += 1
        return SimpleNamespace(id=self.actual_user_id, username="fixture")


class ClientRig:
    def __init__(self, *, actual_user_id=42, authorized=True, block_connect=False):
        self.actual_user_id = actual_user_id
        self.authorized = authorized
        self.block_connect = block_connect
        self.calls = []
        self.sqlite_paths = []
        self.memory_sessions = []
        self.serializations = []
        self.disconnect_order = []
        self.connect_calls = []
        self.get_me_calls = 0
        self.connect_entered = asyncio.Event()
        self.release_connect = asyncio.Event()

    def serialize(self, session):
        value = f"serialized:{len(self.serializations)}:{session.name}"
        self.serializations.append(value)
        return value

    def memory(self, serialized):
        value = FakeMemorySession(serialized)
        self.memory_sessions.append(value)
        return value

    def client(self, session, api_id, api_hash, **kwargs):
        if isinstance(session, str):
            kind = "control"
            self.sqlite_paths.append(session)
            actual_session = FakeSession("control")
        else:
            kind = "upload" if kwargs.get("flood_sleep_threshold") == 0 else "download"
            actual_session = session
        self.calls.append((kind, session, api_id, api_hash, dict(kwargs)))
        return FakeClient(
            self,
            kind,
            actual_session,
            authorized=self.authorized,
            actual_user_id=self.actual_user_id,
            block_connect=self.block_connect and kind == "control",
        )


def make_worker(session_file: Path, rig: ClientRig, *, expected_user_id=42, connections=3):
    return TelegramWorker(
        1,
        "hash",
        expected_user_id,
        session_file,
        connections=connections,
        client_factory=rig.client,
        memory_session_factory=rig.memory,
        session_serializer=rig.serialize,
    )


def test_only_control_client_receives_sqlite_path_and_auxiliaries_are_distinct(tmp_path):
    session_file = tmp_path / "42.session"
    session_file.write_bytes(b"sqlite")
    rig = ClientRig()
    worker = make_worker(session_file, rig, connections=3)
    worker.start()
    try:
        worker.run(worker._download_pool())
        worker.run(worker._upload_client())
        assert rig.sqlite_paths == [str(session_file.resolve())]
        assert len(rig.memory_sessions) == 3
        assert len({id(item) for item in rig.memory_sessions}) == 3
        assert [kind for kind, *_ in rig.calls] == ["control", "download", "download", "upload"]
        assert all(call[-1]["receive_updates"] is False for call in rig.calls)
        assert worker._client.session.save_entities is False
    finally:
        worker.stop()


def test_auxiliary_clients_disconnect_before_sqlite_owner(tmp_path):
    session_file = tmp_path / "42.session"
    session_file.write_bytes(b"sqlite")
    rig = ClientRig()
    worker = make_worker(session_file, rig, connections=2)
    worker.start()
    worker.run(worker._download_pool())
    worker.run(worker._upload_client())
    worker.stop()
    assert rig.disconnect_order[-1] == "control"
    assert sorted(rig.disconnect_order[:-1]) == ["download", "upload"]


def test_control_session_identity_must_match_filename_without_path_leak(tmp_path, caplog):
    session_file = tmp_path / "private-credential-name.session"
    session_file.write_bytes(b"sqlite")
    rig = ClientRig(actual_user_id=456)
    worker = make_worker(session_file, rig, expected_user_id=123)
    with pytest.raises(SessionIdentityError, match="expected 123, got 456") as raised:
        worker.start()
    rendered = "".join(traceback.format_exception(raised.value)) + caplog.text
    assert str(session_file) not in rendered
    assert session_file.name not in rendered
    assert raised.value.__cause__ is None
    assert worker._client is None


def test_unauthorized_control_session_is_rejected_before_ready_log(tmp_path, caplog):
    session_file = tmp_path / "42.session"
    session_file.write_bytes(b"sqlite")
    rig = ClientRig(authorized=False)
    worker = make_worker(session_file, rig)
    with pytest.raises(SessionAuthorizationError, match="account 42"):
        worker.start()
    assert rig.get_me_calls == 0
    assert "Telegram connected as" not in caplog.text


def test_control_start_cancellation_disconnects_and_propagates_cancelled_error(tmp_path):
    async def exercise():
        session_file = tmp_path / "42.session"
        session_file.write_bytes(b"sqlite")
        rig = ClientRig(block_connect=True)
        worker = make_worker(session_file, rig)
        startup = asyncio.create_task(worker._connect_and_validate_control())
        await rig.connect_entered.wait()
        startup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await startup
        assert rig.disconnect_order == ["control"]
        assert worker._client is None

    asyncio.run(exercise())


def test_missing_sqlite_file_is_rechecked_and_sanitized(tmp_path, caplog):
    session_file = tmp_path / "very-secret-session-file.session"
    session_file.write_bytes(b"sqlite")
    rig = ClientRig()
    worker = make_worker(session_file, rig)
    session_file.unlink()
    with pytest.raises(SessionClientError, match="account 42") as raised:
        asyncio.run(worker._connect_and_validate_control())
    rendered = "".join(traceback.format_exception(raised.value)) + caplog.text
    assert str(session_file) not in rendered
    assert session_file.name not in rendered
    assert raised.value.__cause__ is None
    assert rig.calls == []


def test_recreated_auxiliary_derives_fresh_session_from_current_control_state(tmp_path):
    async def exercise():
        session_file = tmp_path / "42.session"
        session_file.write_bytes(b"sqlite")
        rig = ClientRig()
        worker = make_worker(session_file, rig, connections=1)
        await worker._connect_and_validate_control()
        first = worker._new_auxiliary_client(upload=True)
        worker._client.session.name = "control-updated"
        second = worker._new_auxiliary_client(upload=True)
        assert first is not second
        assert rig.serializations == [
            "serialized:0:control",
            "serialized:1:control-updated",
        ]
        assert [item.serialized for item in rig.memory_sessions] == rig.serializations
        await worker._disconnect_all()

    asyncio.run(exercise())
