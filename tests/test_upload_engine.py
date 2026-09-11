"""Offline integration of account leases, Telegram upload phases and registration."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import gamestage
import tgupload
import upload_engine
from media_thumbnail import ThumbnailResult
from telegram_accounts import TelegramAccountPool
from tgio import TelegramWorker
from transfer_models import AccountSpec, TransferRequest, TransferResult, UploadedPart

MiB = 1024 * 1024


class Api:
    def __init__(self):
        self.rows = []
        self.payloads = []
        self.invalidated = []

    def check_hash(self, fingerprint):
        return {"found": bool(self.rows), "files": self.rows}

    def register(self, **payload):
        self.payloads.append(payload)

    def invalidate(self, parent_id):
        self.invalidated.append(parent_id)


class Client:
    def __init__(self):
        self.requests = []
        self.messages = []
        self.active = self.peak = 0
        self.target = 1
        self.reached = None
        self.on_message = lambda: None

    async def send(self, request):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.reached is None:
                self.reached = asyncio.Event()
            if self.active >= self.target:
                self.reached.set()
            await asyncio.wait_for(self.reached.wait(), timeout=2)
            self.requests.append(request)
        finally:
            self.active -= 1

    async def send_file(self, peer, handle, **kwargs):
        self.on_message()
        self.messages.append((peer, handle, kwargs))
        return SimpleNamespace(id=10, document=SimpleNamespace(id=20, access_hash=30))


class Worker(TelegramWorker):
    def __init__(self, user_id):
        super().__init__(1, "hash", user_id, Path("unused.session"))
        self._me = SimpleNamespace(id=user_id)
        self.client = Client()
        self.set_upload_limiter(UnpacedLimiter())

    def run(self, coro, timeout=None):
        return asyncio.run(coro)

    async def _upload_client(self):
        return self.client


class UnpacedLimiter:
    @asynccontextmanager
    async def acquire(self):
        yield

    def now(self):
        return 0

    def success(self, duration):
        pass


def _complete_scheduler_observer(kwargs, size):
    observer = kwargs.get("observer")
    if observer is None:
        return
    token = observer.request_started(0, size)
    observer.request_succeeded(token, size)
    observer.request_settled(token)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    workers = {i: Worker(i) for i in (1, 2)}
    pool = TelegramAccountPool(
        [AccountSpec(i, Path(f"/sessions/{i}.session")) for i in (1, 2)],
        api_id=1, api_hash="hash", upload_files=1,
        worker_factory=lambda _a, _b, user_id, _path, *_args, **_kwargs: workers[user_id],
    )
    for i in (1, 2):
        pool.runtime(i).online = pool.runtime(i).linked = True
    api = Api()
    monkeypatch.setattr(gamestage, "HASH_SAMPLE", 32)
    calls = []

    async def big(client, limiter, reader, size, name, *, force_big, progress=None, **kwargs):
        calls.append((client, size, name, force_big))
        _complete_scheduler_observer(kwargs, size)
        return SimpleNamespace(name=name)

    monkeypatch.setattr(tgupload, "upload_big_file_parts", big)

    def request(size, mime="application/octet-stream"):
        path = tmp_path / "source.bin"
        with path.open("wb") as stream:
            stream.truncate(size)
        return TransferRequest(path, "file.bin", mime, "parent", size)

    return SimpleNamespace(
        engine=lambda **kw: upload_engine.UploadEngine(api, pool, **kw),
        api=api, pool=pool, workers=workers, calls=calls, request=request,
    )


def test_small_upload_uses_four_workers_and_does_not_register(rig):
    rig.workers[1].client.target = 4
    result = rig.engine().transfer(rig.request(10 * tgupload.SMALL_PART_SIZE))
    client = rig.workers[1].client
    assert client.peak == 4
    assert len(client.requests) == 10
    assert {type(r).__name__ for r in client.requests} == {"SaveFilePartRequest"}
    assert isinstance(result, TransferResult)
    assert isinstance(result.parts[0], UploadedPart)
    assert result.parts[0].telegram_user_id == 1
    assert rig.api.payloads == []


def test_one_big_segment_uses_big_protocol(rig):
    result = rig.engine().transfer(rig.request(10 * MiB + 1))
    assert len(result.parts) == 1
    assert [(size, force) for _, size, _, force in rig.calls] == [(10 * MiB + 1, True)]


def test_500m_plus_one_dispatches_two_big_segments_and_registers_accounts(rig):
    engine = rig.engine()
    result = engine.transfer(rig.request(500 * MiB + 1))
    assert [p.index for p in result.parts] == [0, 1]
    assert [p.telegram_user_id for p in result.parts] == [1, 2]
    assert sorted((size, force) for _, size, _, force in rig.calls) == [(1, True), (500 * MiB, True)]
    engine.register_result(result)
    rows = sorted(rig.api.payloads, key=lambda p: p["part_index"])
    assert [p["telegram_user_id"] for p in rows] == [1, 2]
    assert [p["filesize"] for p in rows] == [500 * MiB, 1]
    assert len({p["split_group_id"] for p in rows}) == 1
    assert all(p["total_parts"] == 2 and p["is_split_file"] for p in rows)
    assert rig.api.invalidated == ["parent"]


def test_segments_overlap_and_results_return_in_plan_order(rig, monkeypatch):
    second_finished = threading.Event()
    first_started = threading.Event()
    finished = []

    async def big(client, limiter, reader, size, name, **kwargs):
        if size > 1:
            first_started.set()
            assert second_finished.wait(2), "split segments did not overlap"
        else:
            assert first_started.wait(2)
            second_finished.set()
        finished.append(size)
        _complete_scheduler_observer(kwargs, size)
        return object()

    monkeypatch.setattr(tgupload, "upload_big_file_parts", big)
    result = rig.engine().transfer(rig.request(500 * MiB + 1))
    assert finished == [1, 500 * MiB]
    assert [p.index for p in result.parts] == [0, 1]


def test_only_segment_zero_has_thumbnail_and_upload_uses_chunk_limiter(rig, monkeypatch):
    monkeypatch.setattr("tgio.capture_thumbnail", lambda *_: ThumbnailResult("ready", b"jpeg", 640, 360))
    result = rig.engine().transfer(rig.request(500 * MiB + 1, "video/mp4"))
    assert [p.has_thumbnail for p in result.parts] == [True, False]
    first = rig.workers[1].client
    second = rig.workers[2].client
    assert len(first.requests) == 1  # thumbnail uses explicit small-part upload
    assert first.requests[0].bytes == b"jpeg"
    assert first.messages[0][2]["thumb"] is not None
    assert second.messages[0][2]["thumb"] is None


def test_file_slot_is_free_before_message_bucket_send_and_registration(rig):
    runtime = rig.pool.runtime(1)
    events = []

    def assert_free(event):
        assert runtime.file_slots.acquire(blocking=False), "file lease still held"
        runtime.file_slots.release()
        events.append(event)

    class Bucket:
        async def acquire(self):
            assert_free("bucket")

    runtime.message_limiter = Bucket()
    runtime.worker.client.on_message = lambda: assert_free("message")
    rig.api.register = lambda **_: assert_free("register")
    engine = rig.engine()
    engine.register_result(engine.transfer(rig.request(1)))
    assert events == ["bucket", "message", "register"]


def test_exact_coverage_is_checked_before_any_registration(rig):
    request = rig.request(10)
    result = TransferResult(request, "hash", (UploadedPart(0, 10, "20", None, 9, 1),))
    with pytest.raises(upload_engine.CoverageError):
        rig.engine().register_result(result)
    assert rig.api.payloads == []
    assert issubclass(upload_engine.CoverageError, RuntimeError)


def test_duplicate_reuse_preserves_storage_identity_without_upload(rig):
    request = rig.request(10)
    # filename and parent_id are part of the match now: reuse is only safe
    # under the name the row already answers to.
    rig.api.rows = [{"telegram_message_id": 78, "file_id": "98", "filesize": 10,
                     "telegram_user_id": 42, "has_thumbnail": True,
                     "filename": "file.bin", "parent_id": "parent"}]
    result = rig.engine().transfer(request)
    assert [(p.message_id, p.telegram_user_id, p.has_thumbnail) for p in result.parts] == [(78, 42, True)]
    assert all(not w.client.messages for w in rig.workers.values())


def test_registration_cap_is_shared_between_concurrent_results_and_waits(rig):
    engine = rig.engine(register_concurrency=99)
    request = rig.request(12)
    result = TransferResult(request, "hash", tuple(UploadedPart(i, 10+i, str(20+i), None, 1, 1) for i in range(12)))
    lock = threading.Lock()
    reached = threading.Event()
    release = threading.Event()
    active = peak = completed = 0

    def register(**payload):
        nonlocal active, peak, completed
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 8:
                reached.set()
        assert release.wait(3)
        with lock:
            active -= 1
            completed += 1

    rig.api.register = register
    with ThreadPoolExecutor(2) as executor:
        tasks = [executor.submit(engine.register_result, result) for _ in range(2)]
        try:
            assert reached.wait(2)
            assert not any(task.done() for task in tasks)
        finally:
            release.set()
        for task in tasks:
            task.result(timeout=3)
    assert peak == 8
    assert completed == 24


def test_registration_failure_still_settles_other_futures(rig):
    engine = rig.engine()
    request = rig.request(2)
    result = TransferResult(request, "hash", (UploadedPart(0, 10, "20", None, 1, 1), UploadedPart(1, 11, "21", None, 1, 2)))
    settled = threading.Event()

    def register(**payload):
        if payload["part_index"] == 0:
            raise RuntimeError("registration failed")
        settled.set()

    rig.api.register = register
    with pytest.raises(RuntimeError, match="registration failed"):
        engine.register_result(result)
    assert settled.is_set()
    assert rig.api.invalidated == []


@pytest.mark.parametrize("parts", [
    (UploadedPart(0, 10, "20", None, 5, 1), UploadedPart(2, 11, "21", None, 5, 1)),
    (UploadedPart(0, 10, "20", None, -1, 1), UploadedPart(1, 11, "21", None, 11, 1)),
])
def test_registration_rejects_invalid_segments_even_if_total_matches(rig, parts):
    result = TransferResult(rig.request(10), "hash", parts)
    with pytest.raises(upload_engine.CoverageError):
        rig.engine().register_result(result)
    assert rig.api.payloads == []


def test_upload_failure_releases_slot_and_a_retry_can_upload(rig, monkeypatch):
    engine = rig.engine()
    request = rig.request(10 * MiB + 1)
    attempts = []

    async def big(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("upload failed")
        _complete_scheduler_observer(kwargs, args[3])
        return object()

    monkeypatch.setattr(tgupload, "upload_big_file_parts", big)
    with pytest.raises(RuntimeError, match="upload failed"):
        engine.transfer(request)
    runtime = rig.pool.runtime(1)
    assert runtime.file_slots.acquire(blocking=False)
    runtime.file_slots.release()
    assert engine.transfer(request).parts[0].size == 10 * MiB + 1
    assert len(attempts) == 2
    assert request.source.exists()
    assert rig.api.payloads == []


def test_concurrent_writes_to_one_name_share_a_single_upload(rig, monkeypatch):
    """Two writers racing on the same destination pay for one upload."""
    engine = rig.engine()
    request = rig.request(10 * MiB + 1)
    barrier = threading.Barrier(2)
    uploaded = []

    def check_hash(_):
        barrier.wait(timeout=2)
        return {"found": False, "files": []}

    async def big(*args, **kwargs):
        uploaded.append(1)
        _complete_scheduler_observer(kwargs, args[3])
        return object()

    rig.api.check_hash = check_hash
    monkeypatch.setattr(tgupload, "upload_big_file_parts", big)
    with ThreadPoolExecutor(2) as executor:
        futures = [executor.submit(engine.transfer, item) for item in (request, request)]
        results = [f.result(timeout=3) for f in futures]
    for result in results:
        engine.register_result(result)
    assert len(uploaded) == 1
    assert results[0].parts == results[1].parts


def test_identical_bytes_under_two_names_are_uploaded_twice(rig, monkeypatch):
    """One Telegram document can only carry one name, so two names need two.

    The backend's files table keys on file_id and registers with INSERT OR
    REPLACE, so handing the second name the first one's document id does not
    add a row -- it overwrites the first, and the stager then deletes the only
    local copy of a file that is no longer in the drive. Measured against the
    real backend: two identical 1 MiB files under different names left one row.
    """
    engine = rig.engine()
    request = rig.request(10 * MiB + 1)
    alias = replace(request, upload_name="alias.bin", parent_id="other")
    uploaded = []

    async def big(*args, **kwargs):
        uploaded.append(1)
        _complete_scheduler_observer(kwargs, args[3])
        return object()

    monkeypatch.setattr(tgupload, "upload_big_file_parts", big)
    results = [engine.transfer(item) for item in (request, alias)]
    for result in results:
        engine.register_result(result)
    assert len(uploaded) == 2
    assert results[0].parts != results[1].parts
    assert {(p["filename"], p["parent_id"]) for p in rig.api.payloads} == {
        ("file.bin", "parent"), ("alias.bin", "other"),
    }


def test_message_flood_updates_bucket_and_retry_reuses_uploaded_handle(rig):
    from telethon.errors import FloodWaitError

    runtime = rig.pool.runtime(1)
    calls = []
    admissions = []

    class Bucket:
        async def acquire(self):
            admissions.append("acquire")

        def flood(self, seconds):
            admissions.append(seconds)

    send_file = runtime.worker.client.send_file

    async def send(peer, handle, **options):
        calls.append(handle)
        if len(calls) == 1:
            raise FloodWaitError(request=None, capture=2)
        return await send_file(peer, handle, **options)

    runtime.message_limiter = Bucket()
    runtime.worker.client.send_file = send
    rig.engine().transfer(rig.request(1))
    assert admissions == ["acquire", 2, "acquire"]
    assert calls[0] is calls[1]
    assert len(runtime.worker.client.requests) == 1


def test_telegram_reported_short_size_is_rejected_without_registering(rig):
    async def send(*args, **kwargs):
        return SimpleNamespace(id=10, document=SimpleNamespace(id=20, access_hash=30, size=1))

    rig.workers[1].client.send_file = send
    with pytest.raises(upload_engine.CoverageError):
        rig.engine().transfer(rig.request(2))
    assert rig.api.payloads == []


def test_upload_client_short_flood_reaches_message_bucket_before_sender_retry(monkeypatch):
    """Telethon's own retry loop must not swallow short message FloodWaits."""
    from datetime import datetime, timezone

    from telethon import TelegramClient
    from telethon.errors import FloodWaitError
    from telethon.tl.types import InputFile, InputPeerSelf, Updates

    events = []
    requests = []

    class Sender:
        def send(self, request, ordered=False):
            requests.append(request)
            events.append("send")
            future = asyncio.get_running_loop().create_future()
            if len(requests) == 1:
                future.set_exception(FloodWaitError(request=request, capture=2))
            else:
                future.set_result(Updates([], [], [], datetime.now(timezone.utc), 0))
            return future

    class Bucket:
        async def acquire(self):
            events.append("acquire")

        def flood(self, seconds):
            events.append(("flood", seconds))

    async def connect(client):
        client._loop = asyncio.get_running_loop()
        client._sender = Sender()

    async def get_input_entity(client, peer):
        return InputPeerSelf()

    async def unexpected_automatic_sleep(seconds):
        events.append(("automatic_sleep", seconds))

    monkeypatch.setattr(TelegramClient, "connect", connect)
    monkeypatch.setattr(TelegramClient, "get_input_entity", get_input_entity)
    monkeypatch.setattr(TelegramClient, "_get_response_message", lambda *_: SimpleNamespace(
        id=10, document=SimpleNamespace(id=20, access_hash=30, size=1),
    ))
    monkeypatch.setattr("telethon.client.users.asyncio.sleep", unexpected_automatic_sleep)

    from telethon.sessions import MemorySession

    worker = TelegramWorker(1, "hash", 1, Path("unused.session"))
    # Auxiliary upload clients now clone the validated control session.  This
    # transport-level test does not exercise SQLite startup, so provide the
    # minimal connected control-session state required by that lifecycle.
    worker._client = SimpleNamespace(session=MemorySession())
    handle = InputFile(99, 1, "file.bin", "checksum")

    async def scenario():
        worker._pool_lock = asyncio.Lock()
        return await worker._send_uploaded_segment(
            handle, 1, "file.bin", message_limiter=Bucket(),
        )

    result = asyncio.run(scenario())
    assert result["message_id"] == 10
    assert events == ["acquire", "send", ("flood", 2), "acquire", "send"]
    inner_requests = [getattr(request, "query", request) for request in requests]
    assert [type(request).__name__ for request in inner_requests] == ["SendMediaRequest", "SendMediaRequest"]
    assert all(request.media.file is handle for request in inner_requests)
