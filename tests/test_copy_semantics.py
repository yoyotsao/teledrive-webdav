from types import SimpleNamespace

from tdapi import TeleDriveClient


def test_copy_aliases_current_canonical_location_without_storage_target_lookup():
    calls = []
    client = object.__new__(TeleDriveClient)
    client.current_file_row = lambda file_id: {
        "file_id": file_id,
        "telegram_user_id": 2,
        "telegram_message_id": 81,
        "telegram_chat_id": "777",
        "telegram_media_kind": "document",
        "telegram_media_id": "9001",
        "telegram_media_size": 12,
        "telegram_photo_variant": None,
        "location_version": 4,
        "access_hash": "55",
        "has_thumbnail": True,
    }
    client._call = lambda method, path, **kwargs: calls.append((method, path, kwargs)) or {}
    client.invalidate = lambda parent: calls.append(("invalidate", parent))
    client.get_storage_target = lambda: (_ for _ in ()).throw(AssertionError("COPY consulted storage target"))
    entry = SimpleNamespace(
        is_split=False, split_group_id=None, file_id="logical", mime="application/octet-stream",
        file_hash="hash", has_thumbnail=True,
    )

    client.duplicate(entry, filename="copy.bin", parent_id="parent")

    payload = calls[0][2]["payload"]
    assert payload["telegram_chat_id"] == "777"
    assert payload["telegram_media_id"] == "9001"
    assert payload["message_id"] == 81
    assert all(call[1] != "/storage-target" for call in calls if len(call) > 1)
