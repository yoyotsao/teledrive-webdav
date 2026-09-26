"""Task 8 contracts for deterministic, target-aware Telegram writes."""

import inspect

import pytest

import tgupload
from tgio import TelegramWorker


def test_random_id_contract_is_signed_int64():
    assert hasattr(tgupload, "normalise_random_id")
    normalise = tgupload.normalise_random_id
    assert normalise(-(1 << 63)) == -(1 << 63)
    assert normalise((1 << 63) - 1) == (1 << 63) - 1
    assert normalise(str(-7)) == -7
    with pytest.raises(ValueError):
        normalise(1 << 63)
    with pytest.raises(ValueError):
        normalise(-(1 << 63) - 1)


def test_message_producing_primitives_require_explicit_target_and_random_id():
    for name in ("send_uploaded_segment_to", "send_album_to", "forward_messages_to"):
        assert hasattr(TelegramWorker, name), name
        signature = inspect.signature(getattr(TelegramWorker, name))
        assert "target_peer" in signature.parameters
        assert "random_id" in signature.parameters or "random_ids" in signature.parameters


def test_durable_write_result_exposes_backend_mapping_and_media_identity():
    from transfer_models import DurableTelegramWrite

    result = DurableTelegramWrite(
        uploader_id=42,
        random_id=-9,
        target_peer_key="777",
        destination_message_id=12,
        media_kind="document",
        media_id="99",
        media_size=123,
        access_hash="456",
        photo_variant=None,
    )
    assert result.mapping == {
        "uploader_id": 42,
        "random_id": -9,
        "target_peer_key": "777",
        "destination_message_id": 12,
    }
    assert result.media_identity == {
        "destination_media_kind": "document",
        "destination_media_id": "99",
        "destination_size": 123,
        "destination_access_hash": "456",
    }
