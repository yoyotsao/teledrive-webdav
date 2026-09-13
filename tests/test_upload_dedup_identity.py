import pytest

from storage_parity import DedupDisposition, classify_dedup_row
from tdapi import LocationMetadataError
from transfer_models import FrozenStorageTarget


SAVED = FrozenStorageTarget("saved_messages", None, "me:1", 3, 5, 1, (1, 2))
CHANNEL = FrozenStorageTarget("channel", "777", "777", 3, 5, 1, (1, 2))


def canonical(*, chat_id=None, user_id=1):
    return {
        "file_id": "logical",
        "telegram_user_id": user_id,
        "telegram_message_id": 10,
        "telegram_chat_id": chat_id,
        "telegram_media_kind": "document",
        "telegram_media_id": "900",
        "telegram_media_size": 12,
        "telegram_photo_variant": None,
        "location_version": 2,
    }


def test_channel_dedup_only_aliases_same_canonical_channel():
    assert classify_dedup_row(canonical(chat_id="777", user_id=2), CHANNEL) is DedupDisposition.SAME_TARGET
    assert classify_dedup_row(canonical(chat_id="778", user_id=2), CHANNEL) is DedupDisposition.RELOCATE_REQUIRED
    assert classify_dedup_row(canonical(chat_id=None, user_id=1), CHANNEL) is DedupDisposition.RELOCATE_REQUIRED


def test_saved_messages_dedup_requires_exact_frozen_primary_account():
    assert classify_dedup_row(canonical(chat_id=None, user_id=1), SAVED) is DedupDisposition.SAME_TARGET
    assert classify_dedup_row(canonical(chat_id=None, user_id=2), SAVED) is DedupDisposition.RELOCATE_REQUIRED
    assert classify_dedup_row(canonical(chat_id="777", user_id=1), SAVED) is DedupDisposition.RELOCATE_REQUIRED


def test_malformed_canonical_location_is_not_silently_aliased():
    row = canonical(chat_id="777")
    row["telegram_media_id"] = None
    with pytest.raises(LocationMetadataError):
        classify_dedup_row(row, CHANNEL)
