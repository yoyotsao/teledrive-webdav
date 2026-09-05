"""Routed storage identities survive backend rows and metadata calls."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tdapi  # noqa: E402
from tdapi import TeleDriveClient  # noqa: E402
from transfer_models import RemotePart  # noqa: E402


class Cfg:
    def __init__(self, tmp_path):
        self.base_url = "https://backend.example"
        self.api_base = "https://backend.example/api/v1"
        self.cache_dir = tmp_path
        self.dir_cache_seconds = 60


@pytest.fixture
def api(tmp_path):
    client = TeleDriveClient(Cfg(tmp_path))
    client._token = "JWT"
    client.responses = {}
    client.last_payload = None

    def scripted_call(method, path, *, payload=None, **kwargs):
        if method == "POST":
            client.last_payload = payload
        return client.responses[path]

    client._call = scripted_call
    return client


def test_entry_and_parts_keep_account_and_file_identity(api):
    entry = tdapi._to_entry({"id": "row", "file_id": "9001", "filename": "x.bin",
                             "filesize": 8, "telegram_message_id": 77,
                             "telegram_user_id": 42})

    assert entry.telegram_user_id == 42
    assert api.parts_for(entry) == [RemotePart(77, 8, 42, "9001")]


def test_missing_storage_account_normalizes_to_zero(api):
    entry = tdapi._to_entry({"id": "row", "file_id": "9001", "filename": "x.bin",
                             "filesize": 8, "telegram_message_id": 77})

    assert entry.telegram_user_id == 0
    assert api.parts_for(entry) == [RemotePart(77, 8, 0, "9001")]


def test_split_parts_keep_each_part_storage_identity_and_cache_it(api):
    entry = tdapi._to_entry({"file_id": "9001", "filename": "x.bin", "filesize": 8,
                             "telegram_message_id": 77, "is_split_file": True,
                             "split_group_id": "group", "telegram_user_id": 42})
    api.responses["/files/by-split-group/group"] = {"files": [
        {"file_id": "9001", "filesize": 8, "telegram_message_id": 77,
         "telegram_user_id": 42, "part_index": 0},
        {"file_id": "9002", "filesize": 3, "telegram_message_id": 78,
         "telegram_user_id": 9, "part_index": 1},
    ]}

    assert api.parts_for(entry) == [RemotePart(77, 8, 42, "9001"),
                                    RemotePart(78, 3, 9, "9002")]
    assert api._split_cache.get("42:9001") == [[77, 8, 42, "9001"], [78, 3, 9, "9002"]]
    assert api.parts_for(entry) == [RemotePart(77, 8, 42, "9001"),
                                    RemotePart(78, 3, 9, "9002")]


def test_registration_sends_storage_account(api):
    api.responses["/files/register"] = {"file_id": "9001"}

    api.register("x", 8, "application/octet-stream", 77, "9001", None,
                 telegram_user_id=42)

    assert api.last_payload["telegram_user_id"] == 42


def test_registration_defaults_legacy_storage_account_to_zero(api):
    api.responses["/files/register"] = {"file_id": "9001"}

    api.register(filename="x", filesize=8, mime_type="application/octet-stream",
                 message_id=77, file_id="9001", access_hash=None)

    assert api.last_payload["telegram_user_id"] == 0


def test_linked_accounts_are_returned_as_ids(api):
    api.responses["/accounts"] = {"accounts": [{"telegram_user_id": 1},
                                                   {"telegram_user_id": 42}]}

    assert api.linked_account_ids() == {1, 42}
