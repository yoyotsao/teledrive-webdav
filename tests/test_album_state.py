from types import SimpleNamespace

from storage_parity import StorageParityCoordinator, prepared_media_fingerprint
from transfer_models import DurableTelegramWrite, FrozenStorageTarget, PreparedAlbumItem


class Api:
    def __init__(self):
        self.events = []
        self.records = {}

    def create_telegram_operation(self, payload):
        self.events.append(("create", payload["operation_id"], payload["random_id"]))
        record = {**payload, "state": "planned", "version": 0}
        self.records[payload["operation_id"]] = record
        return record

    def patch_telegram_operation(self, operation_id, payload):
        self.events.append(("patch", operation_id, payload["state"]))
        record = dict(self.records[operation_id])
        record.update(payload)
        record["version"] = int(record.get("version", 0)) + 1
        self.records[operation_id] = record
        return record

    def reconcile_telegram_operation_result(self, operation_id, payload):
        self.events.append(("reconcile", operation_id))
        record = dict(self.records[operation_id])
        record.update(state="sent", version=int(record["version"]) + 1,
                      result_version=1, mapping=payload["mapping"], media_identity=payload["media_identity"])
        self.records[operation_id] = record
        return record

    def register_telegram_operation_group(self, group_id):
        self.events.append(("register_group", group_id))
        return {"group_id": group_id}


class Worker:
    def __init__(self, api):
        self.api = api
        self.random_ids = None
        self.peer = None

    def send_album_to(self, items, *, target_peer, target_peer_key, random_ids, **_kwargs):
        # Every child must already exist durably and be marked sending before RPC.
        assert len([e for e in self.api.events if e[0] == "create"]) == len(items)
        assert len([e for e in self.api.events if e[0] == "patch" and e[2] == "sending"]) == len(items)
        self.random_ids = tuple(random_ids)
        self.peer = target_peer
        return tuple(
            DurableTelegramWrite(
                uploader_id=item.telegram_user_id,
                random_id=random_id,
                target_peer_key=target_peer_key,
                destination_message_id=100 + index,
                media_kind="document",
                media_id=item.document_id,
                media_size=item.size,
                access_hash=item.access_hash,
            )
            for index, (item, random_id) in enumerate(zip(items, random_ids))
        )


class Pool:
    def __init__(self, runtime):
        self.runtime = runtime

    def channel_writers(self, channel_id, linked_ids):
        assert channel_id == "777"
        assert set(linked_ids) == {1}
        return ((self.runtime, SimpleNamespace(peer="channel-peer")),)


TARGET = FrozenStorageTarget("channel", "777", "777", 4, 6, 1, (1,))


def item(tmp_path, index):
    path = tmp_path / f"{index}.jpg"
    path.write_bytes(b"x")
    return PreparedAlbumItem(path, path.name, "image/jpeg", index + 1, 1,
                             str(900 + index), str(1000 + index), True, b"ref")


def test_album_children_are_durable_before_one_send_and_use_journal_random_ids(tmp_path):
    api = Api()
    worker = Worker(api)
    coordinator = StorageParityCoordinator(api, Pool(SimpleNamespace(telegram_user_id=1, worker=worker)))
    items = (item(tmp_path, 0), item(tmp_path, 1))

    result = coordinator.send_prepared_album(items, target=TARGET, group_id="g")

    assert len(result.operations) == 2
    assert len(set(worker.random_ids)) == 2
    assert all(-(1 << 63) <= value <= (1 << 63) - 1 for value in worker.random_ids)
    assert [event[0] for event in api.events[:4]] == ["create", "create", "patch", "patch"]
    assert [record["request_metadata"]["prepared_media_fingerprint"] for record in result.operations] == [
        prepared_media_fingerprint(items[0]), prepared_media_fingerprint(items[1])
    ]


def test_album_registration_is_one_group_barrier(tmp_path):
    api = Api()
    worker = Worker(api)
    coordinator = StorageParityCoordinator(api, Pool(SimpleNamespace(telegram_user_id=1, worker=worker)))
    result = coordinator.send_prepared_album((item(tmp_path, 0), item(tmp_path, 1)), target=TARGET, group_id="g")

    coordinator.register_album(result)

    assert api.events[-1] == ("register_group", "g")
