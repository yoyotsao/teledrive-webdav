from bridge import physical_set_cache_key
from transfer_models import FileLocation, ResolvedRemotePart


def part(version=1, *, chat=None, account=42, message=7, media_id="d1"):
    return ResolvedRemotePart(
        "logical-file",
        0,
        FileLocation(
            telegram_chat_id=chat,
            telegram_user_id=account,
            telegram_message_id=message,
            media_kind="document",
            media_id=media_id,
            media_size=12,
            photo_variant=None,
            location_version=version,
        ),
    )


def test_physical_set_cache_key_changes_when_location_version_changes():
    assert physical_set_cache_key([part(1)]) != physical_set_cache_key([part(2)])


def test_physical_set_cache_key_is_ordered_for_split_parts():
    first = part(message=7, media_id="d1")
    second = part(message=8, media_id="d2")
    assert physical_set_cache_key([first, second]) != physical_set_cache_key([second, first])


def test_physical_set_cache_key_changes_when_saved_messages_moves_to_channel():
    saved = part(chat=None, account=42)
    channel = part(chat="-100123", account=None, version=2)
    assert physical_set_cache_key([saved]) != physical_set_cache_key([channel])
