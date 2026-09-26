import pytest

from storage_parity import AmbiguousTelegramWrite, StorageParityCoordinator
from transfer_models import DurableTelegramWrite


class Api:
    def __init__(self):
        self.calls = []

    def patch_telegram_operation(self, operation_id, payload):
        self.calls.append(("patch", operation_id, dict(payload)))
        return {
            "operation_id": operation_id,
            "uploader_id": 5,
            "random_id": "-44",
            "target_peer_key": "777",
            "state": payload["state"],
            "version": payload["expected_operation_version"] + 1,
        }

    def reconcile_telegram_operation_result(self, operation_id, payload):
        self.calls.append(("reconcile", operation_id, dict(payload)))
        return {"operation_id": operation_id, "state": "sent", "version": payload["expected_operation_version"] + 1}


class Pool:
    pass


def operation(state="sending", version=3):
    return {
        "operation_id": "op",
        "uploader_id": 5,
        "random_id": "-44",
        "target_peer_key": "777",
        "state": state,
        "version": version,
    }


def evidence():
    return DurableTelegramWrite(5, -44, "777", 91, "document", "1001", 12, "77")


def test_response_lost_reconciles_by_uploader_and_random_id_without_resend():
    api = Api()
    coordinator = StorageParityCoordinator(api, Pool())
    looked_up = []

    result = coordinator.recover_operation(
        operation(),
        mapping_lookup=lambda uploader, random_id: looked_up.append((uploader, random_id)) or evidence(),
    )

    assert looked_up == [(5, -44)]
    assert result["state"] == "sent"
    assert [call[0] for call in api.calls] == ["patch", "reconcile"]
    assert api.calls[0][2]["state"] == "recovering"


def test_incomplete_readback_becomes_uncertain_and_blocks_retry():
    api = Api()
    coordinator = StorageParityCoordinator(api, Pool())

    with pytest.raises(AmbiguousTelegramWrite, match="retry blocked"):
        coordinator.recover_operation(operation(), mapping_lookup=lambda *_: None)

    assert api.calls[-1][2]["state"] == "uncertain"
    with pytest.raises(AmbiguousTelegramWrite):
        coordinator.recover_operation(operation(state="uncertain"))


def test_cancellation_during_possible_send_preserves_recovery_state():
    api = Api()
    coordinator = StorageParityCoordinator(api, Pool())

    result = coordinator.cancel_operation(operation(state="sending"))

    assert result["state"] == "uncertain"
    assert api.calls[-1][2]["error_code"] == "cancelled_during_possible_send"
