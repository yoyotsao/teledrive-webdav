from dataclasses import replace

import pytest

from tdapi import LocationMetadataError, parse_file_location
from transfer_models import FileLocation, LegacySavedMessagesLocation, physical_location_key


def canonical(**overrides):
    values = dict(
        telegram_chat_id=None,
        telegram_user_id=42,
        telegram_message_id=1001,
        media_kind="document",
        media_id="9001",
        media_size=123,
        photo_variant=None,
        location_version=7,
    )
    values.update(overrides)
    return FileLocation(**values)


def test_canonical_cache_key_changes_with_location_version():
    loc = canonical()
    assert physical_location_key(loc) != physical_location_key(replace(loc, location_version=8))


def test_saved_messages_cache_key_includes_exact_storage_account():
    assert physical_location_key(canonical(telegram_user_id=42)) != physical_location_key(
        canonical(telegram_user_id=9)
    )


def test_canonical_cache_key_covers_target_message_and_media_identity():
    base = canonical()
    variants = [
        replace(base, telegram_chat_id="-100123", telegram_user_id=None),
        replace(base, telegram_message_id=1002),
        replace(base, media_kind="photo"),
        replace(base, media_id="9002"),
        replace(base, media_size=124),
        replace(base, photo_variant="x"),
    ]
    base_key = physical_location_key(base)
    assert all(physical_location_key(item) != base_key for item in variants)


def test_legacy_saved_messages_location_has_explicit_identity():
    legacy = LegacySavedMessagesLocation(
        telegram_user_id=42,
        telegram_message_id=1001,
        file_id="legacy-file-id",
        media_size=123,
    )
    key = physical_location_key(legacy)
    assert key[0] == "legacy_saved_messages"
    assert key[1:] == (42, 1001, "legacy-file-id", 123)


def test_parse_canonical_saved_messages_requires_exact_account():
    row = {
        "file_id": "f1",
        "telegram_chat_id": None,
        "telegram_user_id": 42,
        "telegram_message_id": 7,
        "telegram_media_kind": "document",
        "telegram_media_id": "d1",
        "telegram_media_size": 99,
        "location_version": 3,
    }
    loc = parse_file_location(row)
    assert loc == FileLocation(None, 42, 7, "document", "d1", 99, None, 3)
    with pytest.raises(LocationMetadataError):
        parse_file_location({**row, "telegram_user_id": None})


def test_parse_incomplete_channel_fails_closed_instead_of_legacy_fallback():
    with pytest.raises(LocationMetadataError):
        parse_file_location({
            "file_id": "f1",
            "telegram_chat_id": "-100123",
            "telegram_message_id": 7,
            "telegram_media_kind": "document",
            # telegram_media_id deliberately missing
            "telegram_media_size": 99,
            "location_version": 3,
        })


def test_parse_pre_schema_row_uses_explicit_legacy_location():
    loc = parse_file_location({
        "file_id": "old",
        "telegram_user_id": 0,
        "telegram_message_id": 8,
        "filesize": 12,
    })
    assert loc == LegacySavedMessagesLocation(0, 8, "old", 12)


@pytest.mark.parametrize("version", [0, "0"])
def test_default_zero_version_does_not_require_canonical_media(version):
    loc = parse_file_location({
        "file_id": "old", "telegram_user_id": 42,
        "telegram_message_id": 8, "filesize": 12,
        "location_version": version, "telegram_chat_id": None,
        "telegram_media_kind": None, "telegram_media_id": None,
        "telegram_media_size": None,
    })
    assert loc == LegacySavedMessagesLocation(42, 8, "old", 12)


@pytest.mark.parametrize("chat", [None, "-100123"])
def test_complete_media_accepts_initial_zero_location_version(chat):
    row = {
        "file_id": "f", "telegram_user_id": 42, "telegram_chat_id": chat,
        "telegram_message_id": 8, "telegram_media_kind": "document",
        "telegram_media_id": "9001", "telegram_media_size": 12,
        "location_version": 0,
    }
    loc = parse_file_location(row)
    assert loc == FileLocation(chat, 42, 8, "document", "9001", 12, None, 0)
    assert physical_location_key(loc) != physical_location_key(replace(loc, location_version=1))
    with pytest.raises(LocationMetadataError):
        parse_file_location({**row, "location_version": -1})


@pytest.mark.parametrize("extra", [
    {"telegram_chat_id": "-100123"},
    {"telegram_media_kind": "document"},
    {"location_version": 1},
    {"location_version": -1},
    {"location_version": "invalid"},
])
def test_partial_or_invalid_identity_is_not_downgraded_to_legacy(extra):
    with pytest.raises(LocationMetadataError):
        parse_file_location({
            "file_id": "f", "telegram_user_id": 42,
            "telegram_message_id": 8, "filesize": 12,
            "location_version": 0, **extra,
        })
