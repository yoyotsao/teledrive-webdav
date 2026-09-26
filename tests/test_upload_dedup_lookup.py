import pytest

from storage_parity import StorageParityCoordinator


class Api:
    def __init__(self):
        self.single = []
        self.groups = []
        self.fail = False

    def switch_file_location(self, file_id, payload):
        self.single.append((file_id, dict(payload)))
        if self.fail:
            raise RuntimeError("link lost")
        return {"file_id": file_id, "location_version": payload["expected_location_version"] + 1}

    def switch_file_location_group(self, parts):
        self.groups.append(tuple(parts))
        return {"bindings": list(parts)}


class Pool:
    pass


def test_relocation_switch_uses_location_cas_and_operation_result_version():
    api = Api()
    coordinator = StorageParityCoordinator(api, Pool())

    coordinator.switch_relocated_file("f", expected_location_version=7, operation_id="op", result_version=3)

    assert api.single == [("f", {
        "expected_location_version": 7,
        "operation_id": "op",
        "result_version": 3,
    })]


def test_link_loss_before_switch_does_not_publish_a_new_location():
    api = Api()
    api.fail = True
    coordinator = StorageParityCoordinator(api, Pool())
    source = {"file_id": "f", "location_version": 7, "telegram_chat_id": None}

    with pytest.raises(RuntimeError, match="link lost"):
        coordinator.switch_relocated_file("f", expected_location_version=7, operation_id="op", result_version=3)

    assert source["location_version"] == 7
    assert source["telegram_chat_id"] is None


def test_split_group_relocation_has_one_atomic_visibility_boundary():
    api = Api()
    coordinator = StorageParityCoordinator(api, Pool())
    parts = (
        {"file_id": "a", "expected_location_version": 1, "operation_id": "oa", "result_version": 1},
        {"file_id": "b", "expected_location_version": 2, "operation_id": "ob", "result_version": 1},
    )

    coordinator.switch_relocated_group(parts)

    assert api.groups == [parts]
