"""Exact Telegram storage identities are preserved through every read path."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

import bridge
import tdapi
import tgio
from tdapi import Entry, TeleDriveClient
from transfer_models import FileLocation, RemotePart, ResolvedRemotePart


class _Message:
    def __init__(self, message_id: int, file_id: str):
        self.id = message_id
        self.document = SimpleNamespace(
            id=int(file_id),
            access_hash=1,
            file_reference=b"ref",
            dc_id=1,
            size=1,
        )
        self.photo = None


class _TelegramClient:
    def __init__(self, messages):
        self.messages = messages
        self.getfile_calls = []

    async def get_messages(self, entity, ids=None):
        return [self.messages.get(message_id) for message_id in ids]

    def iter_download(self, file, **kwargs):
        self.getfile_calls.append((file, kwargs))

        async def chunks():
            yield b"x"

        return chunks()


def _worker(messages):
    worker = tgio.TelegramWorker.__new__(tgio.TelegramWorker)
    worker._client = _TelegramClient(messages)
    worker._docs = {}
    worker._docs_lock = threading.Lock()
    worker._pool = [worker._client]
    worker._rr = 0
    worker.run = asyncio.run

    async def pool():
        return worker._pool

    worker._download_pool = pool
    worker._pin_exported_sender = lambda client, dc_id: asyncio.sleep(0)
    return worker


def test_returned_file_id_is_checked_before_getfile():
    worker = _worker({5: _Message(5, "700")})

    with pytest.raises(RuntimeError, match="expected 701.*got 700") as raised:
        worker.read(5, "701", 0, 1)

    assert type(raised.value).__name__ == "RemoteIdentityError"
    assert worker._client.getfile_calls == []


def test_a_legacy_synthetic_file_id_still_reads():
    """Rows registered before file_id meant anything must stay readable.

    /game split parts used to be registered with ``f"{split_group_id}-{index}"``
    whenever the upload did not return a document id, so their file_id is a
    timestamp and a hex tag, not a Telegram identity. Checking those against
    what Telegram returns can only ever fail, and failing means the archive
    cannot be opened at all: measured on the live drive, 127 of 143 rows under
    /game carried one of these and every one of them stopped reading.

    An id that is not a document id carries no identity to verify, so the read
    falls back to trusting the message id, which is what it did before.
    """
    worker = _worker({5: _Message(5, "700")})

    assert worker.get_document(5, "1788435722109-52da4qq-3").id == 700
    assert worker.read(5, "1788435722109-52da4qq-3", 0, 1) == b"x"


def test_an_empty_file_id_is_not_an_identity_either():
    worker = _worker({5: _Message(5, "700")})

    assert worker.get_document(5, "").id == 700


def test_document_cache_key_includes_expected_file_id():
    worker = _worker({5: _Message(5, "700")})

    assert worker.get_document(5, "700").id == 700
    with pytest.raises(RuntimeError, match="expected 701.*got 700"):
        worker.get_document(5, "701")

    assert (5, "700") in worker._docs
    assert (5, "701") not in worker._docs


def _sized(message_id: int, file_id: str, size: int) -> _Message:
    message = _Message(message_id, file_id)
    message.document.size = size
    return message


def test_web_big_upload_id_is_verified_by_size_instead():
    """The browser registers big uploads under the upload's random file id.

    ``SaveBigFilePart`` uploads (>= 10 MiB, and every split segment) are
    registered with the client-generated InputFileBig id, not the document id
    Telegram assigns (frontend ``gramjs.ts``). Checked on the live drive: 16
    consecutive messages matched the backend's name and size byte for byte and
    none matched its file_id; 3,672 files had stopped reading. When the id
    cannot confirm the file, the recorded size of that part still can.
    """
    worker = _worker({5: _sized(5, "700", 12_066_260)})
    worker._thumb_gate = None

    assert worker.get_document(5, "9001", expected_size=12_066_260).id == 700
    assert worker.read(5, "9001", 0, 1, expected_size=12_066_260) == b"x"
    part = RemotePart(5, 12_066_260, 0, "9001")
    asyncio.run(worker._prefetch_documents([part]))
    assert (5, "9001") in worker._docs


def test_size_fallback_tolerates_the_backends_512k_padding():
    real = 3 * 512 * 1024 + 17
    padded = 4 * 512 * 1024
    worker = _worker({5: _sized(5, "700", real)})

    assert worker.get_document(5, "9001", expected_size=padded).id == 700


def test_a_different_file_is_still_refused_when_the_size_disagrees():
    worker = _worker({5: _sized(5, "700", 1_000_000)})

    for recorded in (999_999, 1_000_000 + 512 * 1024):
        with pytest.raises(RuntimeError, match="expected 9001.*got 700"):
            worker.get_document(5, "9001", expected_size=recorded)
    with pytest.raises(RuntimeError, match="expected 9001.*got 700"):
        worker.read(5, "9001", 0, 1, expected_size=1_000_000 + 512 * 1024)
    assert worker._client.getfile_calls == []


def test_thumbnail_identity_is_checked_before_getfile():
    worker = _worker({5: _Message(5, "700")})
    worker._thumb_gate = None
    part = RemotePart(5, 2 * 512 * 1024, 0, "701")

    assert asyncio.run(worker._thumbnails([part])) == {}
    assert worker._client.getfile_calls == []


def test_missing_prefetched_message_does_not_break_thumbnail_batch():
    worker = _worker({})
    worker._thumb_gate = None

    assert asyncio.run(worker._thumbnails([RemotePart(5, 1, 0, "700")])) == {}
    assert worker._client.getfile_calls == []


class _MemoryWorker:
    def __init__(self):
        self.files = {}
        self.calls = []
        self.info = {}
        self.thumbs = {}

    def put(self, message_id, file_id, data):
        self.files[(message_id, str(file_id))] = data

    def read(self, message_id, expected_file_id, offset, length, expected_size=None):
        self.calls.append((message_id, str(expected_file_id), offset, length))
        return self.files[(message_id, str(expected_file_id))][offset : offset + length]

    def media_info(self, parts):
        return {
            (part.message_id, str(part.file_id)): self.info[(part.message_id, str(part.file_id))]
            for part in parts
        }

    def thumbnails(self, parts):
        return {
            (part.message_id, str(part.file_id)): self.thumbs[(part.message_id, str(part.file_id))]
            for part in parts
        }

    def thumbnail_location(self, location, peer):
        assert peer == "me"
        return self.thumbs[(location.telegram_message_id, str(location.media_id))]

    def media_info_location(self, location, peer):
        assert peer == "me"
        return self.info[(location.telegram_message_id, str(location.media_id))]


class _Pool:
    def __init__(self):
        self.workers = {1: _MemoryWorker(), 2: _MemoryWorker()}

    def runtime(self, account_id):
        return SimpleNamespace(worker=self.workers[account_id])

    def for_read(self, account_id):
        return self.runtime(account_id)

    def read_routes(self, location):
        return ((self.runtime(int(location.telegram_user_id)), "me"),)


def test_duplicate_message_ids_do_not_cross_accounts():
    pool = _Pool()
    pool.runtime(1).worker.put(9, "101", b"A")
    pool.runtime(2).worker.put(9, "202", b"B")
    part = RemotePart(9, 1, 2, "202")

    assert tgio.read_part(pool, part, 0, 1) == b"B"
    assert pool.runtime(1).worker.calls == []


def test_range_crosses_storage_accounts():
    pool = _Pool()
    pool.runtime(1).worker.put(10, "110", b"abc")
    pool.runtime(2).worker.put(10, "210", b"DEF")
    parts = [RemotePart(10, 3, 1, "110"), RemotePart(10, 3, 2, "210")]

    remote = tgio.SeekableRemoteFile(pool, parts, block_size=1)
    remote.seek(2)

    assert remote.read(3) == b"cDE"
    assert pool.runtime(1).worker.calls == [(10, "110", 2, 1)]
    assert pool.runtime(2).worker.calls == [(10, "210", 0, 2)]


def _current_parts(entry: Entry):
    location = FileLocation(
        telegram_chat_id=None,
        telegram_user_id=entry.telegram_user_id,
        telegram_message_id=entry.message_id,
        media_kind="document",
        media_id=entry.file_id,
        media_size=entry.size,
        photo_variant=None,
        location_version=1,
    )
    return [ResolvedRemotePart(entry.file_id, 0, location)]


class _CurrentApi:
    def current_parts(self, entry):
        return _current_parts(entry)


def _entry(account_id: int, file_id: str = "same") -> Entry:
    return Entry(
        file_id=file_id,
        name="image.jpg",
        is_dir=False,
        size=1,
        mtime=0,
        message_id=9,
        has_thumbnail=True,
        telegram_user_id=account_id,
    )


def test_thumbnail_and_head_disk_caches_do_not_collide_across_accounts(tmp_path):
    cfg = SimpleNamespace(cache_dir=tmp_path)
    resolver = bridge.Resolver(cfg, _CurrentApi(), _Pool())
    first = _entry(1)
    second = _entry(2)

    bridge._write_atomic(resolver._thumb_path(first), b"first-thumb")
    bridge._write_atomic(resolver._head_path(first), b"first-head")

    assert resolver.cached_thumb(first) == b"first-thumb"
    assert resolver.cached_thumb(second) is None
    assert resolver.cached_head(first) == b"first-head"
    assert resolver.cached_head(second) == b""


def test_thumbnail_and_property_results_keep_account_identity_in_one_batch(tmp_path):
    cfg = SimpleNamespace(cache_dir=tmp_path)
    pool = _Pool()
    pool.runtime(1).worker.thumbs[(9, "same")] = b"thumb-1"
    pool.runtime(2).worker.thumbs[(9, "same")] = b"thumb-2"
    pool.runtime(1).worker.info[(9, "same")] = {"width": 1}
    pool.runtime(2).worker.info[(9, "same")] = {"width": 2}
    resolver = bridge.Resolver(cfg, _CurrentApi(), pool)
    first = _entry(1)
    second = _entry(2)

    assert resolver.thumbs_for([first, second]) == {
        (1, "same"): b"thumb-1",
        (2, "same"): b"thumb-2",
    }
    assert resolver.props_for([first, second]) == {
        (1, "same"): {"width": 1},
        (2, "same"): {"width": 2},
    }
    assert resolver._prop_cache.get(bridge.physical_set_cache_key(_current_parts(first))) == {"width": 1}
    assert resolver._prop_cache.get(bridge.physical_set_cache_key(_current_parts(second))) == {"width": 2}


class _SplitApi(TeleDriveClient):
    def __init__(self, tmp_path, responses):
        cfg = SimpleNamespace(cache_dir=tmp_path)
        super().__init__(cfg)
        self.responses = iter(responses)
        self.calls = 0

    def _call(self, method, path, **kwargs):
        self.calls += 1
        return next(self.responses)


def test_split_cache_does_not_cross_accounts_with_same_file_id(tmp_path):
    api = _SplitApi(tmp_path, [
        {"files": [{"file_id": "part-1", "filesize": 1,
                    "telegram_message_id": 9, "telegram_user_id": 1}]},
        {"files": [{"file_id": "part-2", "filesize": 1,
                    "telegram_message_id": 9, "telegram_user_id": 2}]},
    ])
    first = Entry("same", "x", False, 1, 0, message_id=9, is_split=True,
                  split_group_id="group", telegram_user_id=1)
    second = Entry("same", "x", False, 1, 0, message_id=9, is_split=True,
                   split_group_id="group", telegram_user_id=2)

    assert api.parts_for(first) == [RemotePart(9, 1, 1, "part-1")]
    assert api.parts_for(second) == [RemotePart(9, 1, 2, "part-2")]
    assert api.calls == 2


def test_parts_for_preserves_same_message_id_on_different_accounts_in_a_range(tmp_path):
    api = _SplitApi(tmp_path, [{"files": [
        {"file_id": "110", "filesize": 3, "telegram_message_id": 10,
         "telegram_user_id": 1, "part_index": 0},
        {"file_id": "210", "filesize": 3, "telegram_message_id": 10,
         "telegram_user_id": 2, "part_index": 1},
    ]}])
    entry = Entry("logical", "x", False, 3, 0, message_id=10, is_split=True,
                  split_group_id="group", telegram_user_id=1)
    pool = _Pool()
    pool.runtime(1).worker.put(10, "110", b"abc")
    pool.runtime(2).worker.put(10, "210", b"DEF")

    remote = tgio.SeekableRemoteFile(pool, api.parts_for(entry), block_size=1)
    remote.seek(2)

    assert remote.read(3) == b"cDE"


def test_legacy_split_cache_without_file_ids_is_refetched(tmp_path):
    api = _SplitApi(tmp_path, [{"files": [
        {"file_id": "110", "filesize": 3, "telegram_message_id": 10,
         "telegram_user_id": 1, "part_index": 0},
        {"file_id": "211", "filesize": 2, "telegram_message_id": 11,
         "telegram_user_id": 2, "part_index": 1},
    ]}])
    entry = Entry("logical", "x", False, 3, 0, message_id=10, is_split=True,
                  split_group_id="group", telegram_user_id=0)
    api._split_cache.put("group", [[10, 3], [11, 2]])

    assert api.parts_for(entry) == [
        RemotePart(10, 3, 1, "110"),
        RemotePart(11, 2, 2, "211"),
    ]
    assert api.calls == 1
    assert api._split_cache.get("0:logical") == [
        [10, 3, 1, "110"],
        [11, 2, 2, "211"],
    ]


def test_legacy_location_passes_its_media_size_to_the_identity_check():
    from transfer_models import LegacySavedMessagesLocation

    worker = _worker({5: _sized(5, "700", 12_066_260)})
    location = LegacySavedMessagesLocation(0, 5, "9001", 12_066_260)

    assert worker.read_location(location, None, 0, 1) == b"x"
    worker.media_info_location(location, None)

    fresh = _worker({5: _sized(5, "700", 12_066_260)})
    wrong = LegacySavedMessagesLocation(0, 5, "9001", 1_000)
    with pytest.raises(RuntimeError, match="expected 9001.*got 700"):
        fresh.read_location(wrong, None, 0, 1)
    assert fresh._client.getfile_calls == []
