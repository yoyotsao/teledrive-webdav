"""Offline album preparation, account routing, mapping and fallback contracts."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

import gamestage
import upload_engine
from media_thumbnail import ThumbnailResult
from telegram_accounts import TelegramAccountPool
from tgio import TelegramWorker
from transfer_models import AccountSpec, PreparedAlbumItem, TransferRequest

MiB = 1024 * 1024


class Limiter:
    def __init__(self):
        self.active = 0
        self.acquires = 0

    @asynccontextmanager
    async def acquire(self):
        self.active += 1
        self.acquires += 1
        try:
            yield
        finally:
            self.active -= 1

    def now(self):
        return 0

    def success(self, duration):
        pass


class Bucket:
    def __init__(self):
        self.admissions = 0
        self.floods = []

    async def acquire(self):
        self.admissions += 1

    def flood(self, seconds):
        self.floods.append(seconds)


class Client:
    def __init__(self, worker):
        self.worker = worker
        self.chunks = []
        self.media = []
        self.albums = []
        self.singles = []
        self.active = self.peak = 0
        self.fail_album = None
        self.on_album = lambda: None
        self.fail_single = False
        self.docs = {}
        self.runtime = None

    async def send(self, request):
        assert self.worker._upload_gate().active > 0
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0)
            self.chunks.append(request)
        finally:
            self.active -= 1

    async def __call__(self, request):
        kind = type(request).__name__
        if kind == "UploadMediaRequest":
            self.media.append(request)
            assert not self.runtime.file_slots.acquire(blocking=False), "preparation lost its lease"
            identity = self.worker.user_id * 1000 + len(self.media)
            doc = SimpleNamespace(id=identity, access_hash=identity + 100,
                                  file_reference=b"reference", size=999)
            self.docs[identity] = doc
            return SimpleNamespace(document=doc)
        assert kind == "SendMultiMediaRequest"
        assert self.runtime.file_slots.acquire(blocking=False), "album retained a file lease"
        self.runtime.file_slots.release()
        self.albums.append(request)
        self.on_album()
        if self.fail_album:
            raise self.fail_album
        return SimpleNamespace(updates=[SimpleNamespace(message=SimpleNamespace(
            id=item.media.id.id + 10000, media=SimpleNamespace(document=self.docs[item.media.id.id]),
        )) for item in reversed(request.multi_media)])

    async def send_file(self, peer, handle, **kwargs):
        self.singles.append((handle, kwargs))
        if self.fail_single:
            raise RuntimeError("single failed")
        return SimpleNamespace(id=8000 + len(self.singles),
                               document=SimpleNamespace(id=9000 + len(self.singles), access_hash=55))


class Worker(TelegramWorker):
    def __init__(self, identity):
        super().__init__(1, "hash", "offline")
        self._me = SimpleNamespace(id=identity)
        self.set_upload_limiter(Limiter())
        self.client = Client(self)

    def run(self, coro, timeout=None):
        return asyncio.run(coro)

    async def _upload_client(self):
        return self.client


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr("tgio.capture_thumbnail", lambda *_: ThumbnailResult("ready", b"jpeg", 640, 360))
    monkeypatch.setattr(gamestage, "HASH_SAMPLE", 32)

    def build(accounts=1):
        workers = {i: Worker(i) for i in range(1, accounts + 1)}
        pool = TelegramAccountPool(
            [AccountSpec(i, str(i), str(i)) for i in workers], api_id=1, api_hash="hash", upload_files=1,
            worker_factory=lambda _a, _b, session, *_args, **_kwargs: workers[int(session)],
            message_limiter_factory=Bucket,
        )
        for identity, worker in workers.items():
            runtime = pool.runtime(identity)
            runtime.online = runtime.linked = True
            worker.client.runtime = runtime
        api = SimpleNamespace(check_hash=lambda _: {}, register=lambda **_: pytest.fail("transfer registered"))

        def request(index, size=1):
            path = tmp_path / f"{index}.jpg"
            path.write_bytes(bytes([index + 1]) * size)
            return TransferRequest(path, f"alias-{index}.jpg", "image/jpeg", "parent", size)

        return SimpleNamespace(engine=upload_engine.UploadEngine(api, pool), pool=pool,
                               workers=workers, request=request, api=api)

    return build


@pytest.mark.parametrize("mime,size,eligible", [
    ("image/jpeg", 10 * MiB, True), ("video/mp4", 10 * MiB, True),
    ("image/webp", 1, False), ("image/jpeg", 10 * MiB + 1, False),
    ("application/pdf", 1, False),
])
def test_album_eligibility(mime, size, eligible):
    assert upload_engine.album_eligible(mime, size) is eligible


def test_ten_flush_before_discovery_continues_and_tail_flushes(rig):
    r = rig()
    client = r.workers[1].client

    def discovery():
        for i in range(11):
            if i == 10:
                assert [len(batch.multi_media) for batch in client.albums] == [10]
            yield r.request(i)

    results = r.engine.transfer_batch(discovery())
    assert [len(batch.multi_media) for batch in client.albums] == [10, 1]
    assert [result.request.upload_name for result in results] == [f"alias-{i}.jpg" for i in range(11)]
    assert all(result.request.source.exists() for result in results)


def test_shuffled_updates_map_by_document_id_with_original_size_and_identity(rig):
    r = rig()
    results = r.engine.transfer_batch([r.request(0, 2), r.request(1, 3)])
    assert [(p.message_id, p.file_id, p.access_hash, p.size, p.telegram_user_id, p.has_thumbnail)
            for result in results for p in result.parts] == [
        (11001, "1001", "1101", 2, 1, True), (11002, "1002", "1102", 3, 1, True),
    ]
    sent = r.workers[1].client.albums[0].multi_media
    assert [item.media.id.file_reference for item in sent] == [b"reference", b"reference"]


def test_album_original_and_thumbnail_use_512k_shared_gate_and_message_bucket(rig, monkeypatch):
    r = rig()
    monkeypatch.setattr("tgio.capture_thumbnail", lambda *_: ThumbnailResult("ready", b"t" * (512 * 1024 + 1), 640, 360))
    r.engine.transfer_batch([r.request(0, 512 * 1024 + 1)])
    worker = r.workers[1]
    assert sorted(len(req.bytes) for req in worker.client.chunks) == [1, 1, 512 * 1024, 512 * 1024]
    assert {type(req).__name__ for req in worker.client.chunks} == {"SaveFilePartRequest"}
    assert worker._upload_gate().acquires == 4
    assert r.pool.runtime(1).message_limiter.admissions == 2
    media = worker.client.media[0].media
    assert media.mime_type == "image/jpeg"
    assert media.thumb is not None
    assert media.attributes[0].file_name == "alias-0.jpg"
    assert (media.attributes[1].w, media.attributes[1].h) == (640, 360)


def test_accounts_never_share_album_and_one_batch_failure_does_not_cancel_other(rig):
    r = rig(accounts=2)
    r.workers[1].client.fail_album = RuntimeError("album failed")
    results = r.engine.transfer_batch([r.request(i) for i in range(4)])
    assert [result.parts[0].telegram_user_id for result in results] == [1, 2, 1, 2]
    assert [result.parts[0].has_thumbnail for result in results] == [False, True, False, True]
    for identity, worker in r.workers.items():
        assert len(worker.client.albums) == 1
        assert all(item.media.id.id // 1000 == identity for item in worker.client.albums[0].multi_media)


@pytest.mark.parametrize("failure", [TimeoutError("timeout"), RuntimeError("bad album")])
def test_batch_failure_rereads_sources_one_worker_forced_document_without_thumb(rig, failure):
    r = rig()
    request = r.request(0, 1024 * 1024)
    client = r.workers[1].client
    client.fail_album = failure

    def replace_source():
        request.source.write_bytes(b"z" * request.logical_size)
        client.chunks.clear()
        client.peak = 0

    client.on_album = replace_source
    result = r.engine.transfer_batch([request])[0]
    assert b"".join(req.bytes for req in sorted(client.chunks, key=lambda req: req.file_part)) == b"z" * request.logical_size
    assert client.peak == 1
    assert len(client.chunks) == 8  # ordinary small fallback uses 128 KiB
    assert client.singles[0][1]["force_document"] is True
    assert client.singles[0][1]["thumb"] is None
    assert client.singles[0][0].name == "alias-0.jpg"
    assert result.parts[0].has_thumbnail is False
    assert result.parts[0].telegram_user_id == 1


def test_fallback_failure_still_sends_other_account_tail(rig):
    r = rig(accounts=2)
    r.workers[1].client.fail_album = RuntimeError("album failed")
    r.workers[1].client.fail_single = True
    with pytest.raises(RuntimeError, match="single failed"):
        r.engine.transfer_batch([r.request(0), r.request(1)])
    assert len(r.workers[2].client.albums) == 1


def test_a_repeat_of_one_name_in_a_batch_claims_a_single_preparation(rig):
    """The same destination twice in one batch is one document, not two."""
    r = rig()
    request = r.request(0)
    results = r.engine.transfer_batch([request, request])
    assert results[0].parts == results[1].parts
    assert len(r.workers[1].client.media) == 1


def test_two_names_for_one_payload_each_get_their_own_document(rig):
    """A shared claim would hand the second name the first one's document id,
    which the backend stores by primary key -- so the first name would vanish."""
    r = rig()
    request = r.request(0)
    alias = replace(request, upload_name="second.jpg", parent_id="other")
    results = r.engine.transfer_batch([request, alias])
    assert results[0].parts != results[1].parts
    assert results[1].request == alias
    assert len(r.workers[1].client.media) == 2


def test_disallowed_album_uses_existing_non_album_sender(rig):
    r = rig()
    result = r.engine.transfer_batch([replace(r.request(0), allow_album=False)])[0]
    assert result.parts[0].message_id == 8001
    assert r.workers[1].client.albums == []


def test_send_album_timeout_cancels_rpc(rig):
    r = rig()
    worker = r.workers[1]
    cancelled = []

    async def blocked(_request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    async def client():
        return blocked

    worker._upload_client = client
    item = PreparedAlbumItem(r.request(0).source, "alias.jpg", "image/jpeg", 1, 1, "1001", "1101", True)
    with pytest.raises(asyncio.TimeoutError):
        worker.send_album([item], timeout=0.01, message_limiter=Bucket())
    assert cancelled == [True]


@pytest.mark.parametrize("kind", ["missing", "duplicate"])
def test_malformed_album_mapping_falls_back_for_whole_batch(rig, kind):
    r = rig()
    client = r.workers[1].client

    async def transport(request):
        response = await client(request)
        if type(request).__name__ == "SendMultiMediaRequest":
            response.updates = response.updates[:1]
            if kind == "duplicate":
                response.updates *= 2
        return response

    class Transport:
        send = client.send
        send_file = client.send_file

        async def __call__(self, request):
            return await transport(request)

    async def wrapped():
        return Transport()

    r.workers[1]._upload_client = wrapped
    results = r.engine.transfer_batch([r.request(0), r.request(1)])
    assert [result.parts[0].message_id for result in results] == [8001, 8002]
    assert all(not result.parts[0].has_thumbnail for result in results)


@pytest.mark.parametrize("rpc", ["UploadMediaRequest", "SendMultiMediaRequest"])
def test_album_flood_reenters_message_bucket_and_reuses_request(rig, rpc):
    from telethon.errors import FloodWaitError

    r = rig()
    client = r.workers[1].client
    flooded = []

    class Transport:
        send = client.send
        send_file = client.send_file

        async def __call__(self, request):
            if type(request).__name__ == rpc:
                flooded.append(request)
                if len(flooded) == 1:
                    raise FloodWaitError(request=request, capture=2)
            return await client(request)

    async def wrapped():
        return Transport()

    r.workers[1]._upload_client = wrapped
    result = r.engine.transfer_batch([r.request(0)])[0]
    bucket = r.pool.runtime(1).message_limiter
    assert result.parts[0].has_thumbnail is True
    assert bucket.admissions == 3
    assert bucket.floods == [2]
    assert flooded[0] is flooded[1]


def test_album_default_timeout_is_sixty_seconds(rig, monkeypatch):
    r = rig()
    original = asyncio.wait_for
    deadlines = []

    async def wait_for(awaitable, timeout):
        deadlines.append(timeout)
        return await original(awaitable, timeout)

    monkeypatch.setattr("tgio.asyncio.wait_for", wait_for)
    r.engine.transfer_batch([r.request(0)])
    assert deadlines == [60]


def test_worker_rejects_mixed_accounts_before_sending(rig):
    r = rig()
    item = PreparedAlbumItem(r.request(0).source, "alias.jpg", "image/jpeg", 1, 2, "2001", "2101", True)
    with pytest.raises(ValueError, match="mix Telegram accounts"):
        r.workers[1].send_album([item])
    assert r.workers[1].client.albums == []


def test_failed_album_claim_can_retry_after_fallback_failure(rig):
    r = rig()
    client = r.workers[1].client
    client.fail_album = RuntimeError("album failed")
    client.fail_single = True
    request = r.request(0)
    with pytest.raises(RuntimeError, match="single failed"):
        r.engine.transfer_batch([request])
    client.fail_album = None
    client.fail_single = False
    result = r.engine.transfer_batch([request])[0]
    assert result.parts[0].has_thumbnail is True
    assert len(client.media) == 2
