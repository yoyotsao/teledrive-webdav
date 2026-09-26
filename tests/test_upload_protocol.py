"""Offline contract tests for Telegram's small and big upload protocols."""

import asyncio
import hashlib
import io
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tgupload import (  # noqa: E402
    BIG_PART_SIZE,
    SMALL_PART_SIZE,
    _PartReader,
    _upload_parts,
    decide_protocol,
    upload_big_file_parts,
    upload_small_file_parts,
)
from tgio import TelegramWorker  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    "size,protocol,segments",
    [
        (10 * 1024 * 1024, "small", 1),
        (10 * 1024 * 1024 + 1, "big", 1),
        (500 * 1024 * 1024, "big", 1),
        (500 * 1024 * 1024 + 1, "split", 2),
    ],
)
def test_protocol_boundaries(size, protocol, segments):
    decision = decide_protocol(size, album_eligible=False)

    assert (decision.name, len(decision.segments)) == (protocol, segments)
    assert sum(segment_size for _, segment_size in decision.segments) == size
    assert decision.segments[0][0] == 0
    assert decision.force_big is (size > 10 * 1024 * 1024)


def test_album_protocol_is_small_only():
    assert decide_protocol(10 * 1024 * 1024, album_eligible=True).name == "album"
    assert decide_protocol(10 * 1024 * 1024 + 1, album_eligible=True).name == "big"


class RecordingLimiter:
    def __init__(self):
        self.acquires = 0
        self.in_flight = 0
        self.peak = 0
        self.successes = []
        self.floods = []

    @asynccontextmanager
    async def acquire(self):
        self.acquires += 1
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            yield
        finally:
            self.in_flight -= 1

    def now(self):
        return 0.0

    def success(self, duration):
        self.successes.append(duration)

    def flood(self, seconds, *, premium=False):
        self.floods.append((seconds, premium))


class RecordingSender:
    def __init__(self):
        self.requests = []
        self.in_flight = []
        self._active = 0

    async def send(self, request):
        self._active += 1
        self.in_flight.append(self._active)
        try:
            await asyncio.sleep(0)
            self.requests.append(request)
        finally:
            self._active -= 1


class BlockingSender:
    def __init__(self, target):
        self.target = target
        self.active = 0
        self.peak = 0
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, request):
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.active >= self.target:
            self.reached.set()
        try:
            await self.release.wait()
        finally:
            self.active -= 1


def test_small_file_clamps_even_an_explicit_twelve_worker_request():
    data = b"x" * (20 * SMALL_PART_SIZE)
    class ProbeSender:
        def __init__(self):
            self.requests = []
            self.active = 0
            self.peak = 0
            self.reached_cap = asyncio.Event()
            self.release = asyncio.Event()

        async def send(self, request):
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.active >= 4:
                self.reached_cap.set()
            self.requests.append(request)
            try:
                await self.release.wait()
            finally:
                self.active -= 1

    sender = ProbeSender()
    limiter = RecordingLimiter()

    async def scenario():
        async with _PartReader(io.BytesIO(data)) as reader:
            task = asyncio.create_task(upload_small_file_parts(
                sender,
                limiter,
                reader,
                len(data),
                "x.bin",
                workers=12,
            ))
            await asyncio.wait_for(sender.reached_cap.wait(), timeout=1)
            sender.release.set()
            return await task

    result = run(scenario())

    assert sender.peak <= 4
    assert {request.file_part for request in sender.requests} == set(range(20))
    assert {len(request.bytes) for request in sender.requests} == {SMALL_PART_SIZE}
    assert result.md5_checksum == hashlib.md5(data).hexdigest()
    assert limiter.acquires == 20


def test_big_upload_releases_completed_payloads_instead_of_retaining_all_parts():
    import gc
    import weakref

    class Payload:
        def __init__(self, value):
            self.value = value

        def __len__(self):
            return len(self.value)

    class Stream:
        def __init__(self):
            self.payloads = [Payload(b"a"), Payload(b"b")]

        def seek(self, offset):
            self.offset = offset

        def read(self, _size):
            return self.payloads[self.offset]

    class Sender:
        def __init__(self):
            self.release_second = asyncio.Event()

        async def send(self, request):
            if request.index == 0:
                return
            await self.release_second.wait()

    sender = Sender()
    limiter = RecordingLimiter()
    stream = Stream()
    first_ref = weakref.ref(stream.payloads[0])
    first_completed = asyncio.Event()

    def progress(current, _total):
        if current >= 1:
            first_completed.set()

    async def scenario():
        async with _PartReader(stream) as reader:
            task = asyncio.create_task(
                _upload_parts(
                    sender,
                    limiter,
                    reader,
                    2,
                    parts=[(0, 1), (1, 1)],
                    request_factory=lambda _fid, index, total, data: SimpleNamespace(
                        index=index, total=total, data=data
                    ),
                    workers=2,
                    progress=progress,
                    collect_payloads=False,
                )
            )
            # The part is only complete after the shielded RPC has settled and
            # send_one has advanced progress. At that boundary the payload must
            # no longer be retained by the finished part task.
            await first_completed.wait()
            stream.payloads[0] = None
            gc.collect()
            released = first_ref() is None
            sender.release_second.set()
            await task
            return released

    assert run(scenario())


def test_one_byte_forced_big_upload_uses_big_request_and_limiter():
    sender = RecordingSender()
    limiter = RecordingLimiter()

    async def scenario():
        async with _PartReader(io.BytesIO(b"x")) as reader:
            return await upload_big_file_parts(sender, limiter, reader, 1, "tail", force_big=True)

    result = run(scenario())

    assert sender.requests[0].__class__.__name__ == "SaveBigFilePartRequest"
    assert len(sender.requests[0].bytes) == BIG_PART_SIZE - BIG_PART_SIZE + 1
    assert result.parts == 1
    assert limiter.acquires == 1


def test_big_upload_uses_the_account_limiter_as_its_default_worker_cap():
    data = b"x" * (12 * BIG_PART_SIZE)
    sender = BlockingSender(target=12)
    limiter = RecordingLimiter()

    async def scenario():
        async with _PartReader(io.BytesIO(data)) as reader:
            upload = asyncio.create_task(
                upload_big_file_parts(sender, limiter, reader, len(data), "big.bin", force_big=True)
            )
            failure = None
            try:
                await asyncio.wait_for(sender.reached.wait(), timeout=1.0)
            except asyncio.TimeoutError as exc:
                failure = exc
            finally:
                sender.release.set()
            await upload
            if failure is not None:
                raise AssertionError("big upload did not admit twelve concurrent part sends") from failure

    run(scenario())

    assert sender.peak == 12
    assert limiter.peak == 12


def test_worker_uses_explicit_small_primitive_instead_of_telethon_shortcut(monkeypatch):
    calls = []
    limiter = RecordingLimiter()

    async def upload_small(client, received_limiter, reader, size, name, **kwargs):
        calls.append(("small", client, received_limiter, size, name, kwargs))
        return object()

    async def upload_big(*args, **kwargs):
        calls.append(("big", args, kwargs))
        return object()

    class Client:
        async def send_file(self, *_args, **_kwargs):
            return SimpleNamespace(
                id=81, document=SimpleNamespace(id=91, access_hash=101)
            )

    worker = TelegramWorker(1, "hash", 1, Path("unused.session"))

    client = Client()

    async def upload_client():
        return client

    worker._upload_client = upload_client
    worker._upload_gate = lambda: limiter
    monkeypatch.setattr("tgupload.upload_small_file_parts", upload_small)
    monkeypatch.setattr("tgupload.upload_big_file_parts", upload_big)

    result = run(worker._upload_segment(io.BytesIO(b"x"), 1, "x.bin", None))

    assert result == {"message_id": 81, "file_id": "91", "access_hash": "101", "size": 1}
    assert calls == [("small", client, limiter, 1, "x.bin", {
        "progress": None, "observer": None, "revoked": None, "rpc_timeout": 120.0,
    })]
