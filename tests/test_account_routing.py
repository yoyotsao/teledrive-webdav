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
from transfer_models import RemotePart


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


def test_document_cache_key_includes_expected_file_id():
    worker = _worker({5: _Message(5, "700")})

    assert worker.get_document(5, "700").id == 700
    with pytest.raises(RuntimeError, match="expected 701.*got 700"):
        worker.get_document(5, "701")

    assert (5, "700") in worker._docs
    assert (5, "701") not in worker._docs


def test_thumbnail_identity_is_checked_before_getfile():
    worker = _worker({5: _Message(5, "700")})
    worker._thumb_gate = None
    part = RemotePart(5, 1, 0, "701")

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

    def put(self, message_id, file_id, data):
        self.files[(message_id, str(file_id))] = data

    def read(self, message_id, expected_file_id, offset, length):
        self.calls.append((message_id, str(expected_file_id), offset, length))
        return self.files[(message_id, str(expected_file_id))][offset : offset + length]

    def media_info(self, parts):
        return {
            (part.message_id, str(part.file_id)): self.info[(part.message_id, str(part.file_id))]
            for part in parts
        }


class _Pool:
    def __init__(self):
        self.workers = {1: _MemoryWorker(), 2: _MemoryWorker()}

    def runtime(self, account_id):
        return SimpleNamespace(worker=self.workers[account_id])

    def for_read(self, account_id):
        return self.runtime(account_id)


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
    resolver = bridge.Resolver(cfg, SimpleNamespace(), _Pool())
    first = _entry(1)
    second = _entry(2)

    bridge._write_atomic(resolver._thumb_path(first), b"first-thumb")
    bridge._write_atomic(resolver._head_path(first), b"first-head")

    assert resolver.cached_thumb(first) == b"first-thumb"
    assert resolver.cached_thumb(second) is None
    assert resolver.cached_head(first) == b"first-head"
    assert resolver.cached_head(second) == b""


def test_property_cache_does_not_cross_accounts_with_same_file_id(tmp_path):
    cfg = SimpleNamespace(cache_dir=tmp_path)
    pool = _Pool()
    pool.runtime(1).worker.info[(9, "same")] = {"width": 1}
    pool.runtime(2).worker.info[(9, "same")] = {"width": 2}
    resolver = bridge.Resolver(cfg, SimpleNamespace(), pool)
    first = _entry(1)
    second = _entry(2)

    assert resolver.props_for([first]) == {"same": {"width": 1}}
    assert resolver.props_for([second]) == {"same": {"width": 2}}
    assert resolver._prop_cache.get("1-same") == {"width": 1}
    assert resolver._prop_cache.get("2-same") == {"width": 2}


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
