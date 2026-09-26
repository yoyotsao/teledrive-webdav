"""Offline tests for tgupload's part planner, random-access reader, paced
sender, and parallel segment uploader.

No live Telegram connection: a scripted fake stands in for the MTProto
sender, but the request objects it receives are the real Telethon classes,
so a Telethon upgrade that changes their shape fails here.
"""

import asyncio
from contextlib import asynccontextmanager
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tgupload import (  # noqa: E402
    MAX_PARTS_PER_MESSAGE,
    PART_RETRIES,
    PART_SIZE,
    UploadGate,
    _PartReader,
    plan_parts,
    send_part,
    upload_big_file_parts,
    upload_file_parts,
)


def run(coro):
    return asyncio.run(coro)


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_sleeper(clock: FakeClock):
    async def sleeper(seconds: float) -> None:
        clock.advance(seconds)

    return sleeper


def fast_gate(max_window: int = 4) -> UploadGate:
    """A gate whose clock/sleeper never causes real wall-clock delay."""
    clock = FakeClock()
    return UploadGate(max_window=max_window, clock=clock, sleeper=make_sleeper(clock))


# --------------------------------------------------------------------------- #
# plan_parts
# --------------------------------------------------------------------------- #


def test_plan_parts_various_sizes():
    assert plan_parts(1) == [(0, 1)]
    assert plan_parts(PART_SIZE - 1) == [(0, PART_SIZE - 1)]
    assert plan_parts(PART_SIZE) == [(0, PART_SIZE)]
    assert plan_parts(PART_SIZE + 1) == [(0, PART_SIZE), (PART_SIZE, 1)]


def test_plan_parts_exact_500_mib_segment():
    size = MAX_PARTS_PER_MESSAGE * PART_SIZE
    parts = plan_parts(size)
    assert len(parts) == MAX_PARTS_PER_MESSAGE
    assert all(n == PART_SIZE for _, n in parts)
    assert parts[0] == (0, PART_SIZE)
    assert parts[-1] == ((MAX_PARTS_PER_MESSAGE - 1) * PART_SIZE, PART_SIZE)


def test_plan_parts_rejects_non_positive_size():
    with pytest.raises(ValueError):
        plan_parts(0)
    with pytest.raises(ValueError):
        plan_parts(-1)


def test_plan_parts_rejects_over_the_message_limit():
    with pytest.raises(ValueError):
        plan_parts(MAX_PARTS_PER_MESSAGE * PART_SIZE + 1)


# --------------------------------------------------------------------------- #
# _PartReader
# --------------------------------------------------------------------------- #


def test_part_reader_matches_direct_reads(tmp_path):
    path = tmp_path / "segment.bin"
    data = bytes((i % 256) for i in range(3000))
    path.write_bytes(data)

    async def scenario():
        stream = open(path, "rb")
        try:
            async with _PartReader(stream) as reader:
                for offset, n in [(0, 500), (500, 500), (2999, 1), (200, 300)]:
                    got = await reader.read_at(offset, n)
                    assert got == data[offset : offset + n]
        finally:
            stream.close()

    run(scenario())


def test_part_reader_raises_on_short_read():
    class ShortStream:
        def seek(self, offset):
            pass

        def read(self, n):
            return b"x" * (n - 1)

    async def scenario():
        async with _PartReader(ShortStream()) as reader:
            with pytest.raises(IOError):
                await reader.read_at(0, 10)

    run(scenario())


# --------------------------------------------------------------------------- #
# fakes shared by send_part / upload_file_parts tests
# --------------------------------------------------------------------------- #


class RecordingSender:
    """Records every part index it was asked to send; part indices in
    ``fail_counts`` raise a scripted number of times (``float('inf')`` for
    always) before succeeding. Yields once per call so concurrent callers
    actually overlap in the event loop."""

    def __init__(self, fail_counts=None) -> None:
        self.sent_indices = []
        self.concurrent = 0
        self.peak_concurrent = 0
        self._fail_counts = dict(fail_counts or {})

    async def send(self, request) -> None:
        self.concurrent += 1
        self.peak_concurrent = max(self.peak_concurrent, self.concurrent)
        try:
            await asyncio.sleep(0)
            idx = request.file_part
            self.sent_indices.append(idx)
            remaining = self._fail_counts.get(idx, 0)
            if remaining:
                if remaining != float("inf"):
                    self._fail_counts[idx] = remaining - 1
                raise RuntimeError(f"part {idx} scripted failure")
        finally:
            self.concurrent -= 1


class FakeClient:
    def __init__(self, sender) -> None:
        self._sender = sender


def make_request(index=0, total=1, payload=b"x" * 10):
    from telethon.tl.functions.upload import SaveBigFilePartRequest

    return SaveBigFilePartRequest(1, index, total, payload)


# --------------------------------------------------------------------------- #
# send_part
# --------------------------------------------------------------------------- #


def test_send_part_succeeds_first_try():
    gate = fast_gate()
    sender = RecordingSender()
    request = make_request()

    run(send_part(lambda: sender, request, gate, "part"))

    assert sender.sent_indices == [0]
    assert gate.stats()["floods"] == 0


def test_send_part_retries_through_flood_then_succeeds():
    from telethon.errors import FloodWaitError

    gate = fast_gate()
    request = make_request()
    sender = RecordingSender()
    original_send = sender.send
    calls = {"n": 0}

    async def send(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise FloodWaitError(req, capture=2)
        return await original_send(req)

    sender.send = send

    run(send_part(lambda: sender, request, gate, "part"))

    assert calls["n"] == 2
    assert gate.stats()["floods"] == 1


def test_send_part_classifies_premium_flood_before_retrying():
    from telethon.errors import FloodPremiumWaitError

    class Limiter:
        def __init__(self):
            self.floods = []

        @asynccontextmanager
        async def acquire(self):
            yield

        def now(self):
            return 0.0

        def success(self, _duration):
            pass

        def flood(self, seconds, *, premium=False):
            self.floods.append((seconds, premium))

    request = make_request()
    limiter = Limiter()
    attempts = {"n": 0}

    class PremiumThenSuccess:
        async def send(self, req):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise FloodPremiumWaitError(req, capture=17)

    run(send_part(lambda: PremiumThenSuccess(), request, limiter, "part"))

    assert attempts["n"] == 2
    assert limiter.floods == [(17.0, True)]


def test_send_part_honors_premium_flood_wait_longer_than_old_cap():
    from telethon.errors import FloodPremiumWaitError

    class Limiter:
        def __init__(self):
            self.floods = []

        @asynccontextmanager
        async def acquire(self):
            yield

        def now(self):
            return 0.0

        def success(self, _duration):
            pass

        def flood(self, seconds, *, premium=False):
            self.floods.append((seconds, premium))

    request = make_request()
    limiter = Limiter()
    attempts = {"n": 0}

    class PremiumThenSuccess:
        async def send(self, req):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise FloodPremiumWaitError(req, capture=601)

    run(send_part(lambda: PremiumThenSuccess(), request, limiter, "part"))

    assert attempts["n"] == 2
    assert limiter.floods == [(601.0, True)]


def test_send_part_keeps_waiting_through_more_than_the_old_flood_retry_limit():
    from telethon.errors import FloodWaitError

    gate = fast_gate()
    request = make_request()
    attempts = {"n": 0}

    class FloodsThenSucceeds:
        async def send(self, req):
            attempts["n"] += 1
            if attempts["n"] <= 12:
                raise FloodWaitError(req, capture=1)

    sender = FloodsThenSucceeds()

    run(send_part(lambda: sender, request, gate, "part"))

    assert attempts["n"] == 13
    assert gate.stats()["floods"] == 12


def test_send_part_leaves_non_flood_failures_for_the_three_attempt_primitive_retry():
    gate = fast_gate()
    request = make_request()
    attempts = {"n": 0}

    class DropsOnce:
        async def send(self, req):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionError("dropped")

    sender = DropsOnce()

    with pytest.raises(ConnectionError):
        run(send_part(lambda: sender, request, gate, "part"))

    assert attempts["n"] == 1


def test_big_primitive_retries_a_non_flood_failure_three_times_with_web_backoff():
    class Limiter:
        def __init__(self):
            self.sleeps = []

        @asynccontextmanager
        async def acquire(self):
            yield

        def now(self):
            return 0.0

        def success(self, _duration):
            pass

        def flood(self, _seconds, *, premium=False):
            pass

        async def sleep(self, seconds):
            self.sleeps.append(seconds)

    class FailsTwice:
        def __init__(self):
            self.attempts = 0

        async def send(self, _request):
            self.attempts += 1
            if self.attempts < 3:
                raise RuntimeError("temporary")

    async def scenario():
        sender, limiter = FailsTwice(), Limiter()
        async with _PartReader(io.BytesIO(b"x")) as reader:
            await upload_big_file_parts(sender, limiter, reader, 1, "tail", force_big=True)
        return sender, limiter

    sender, limiter = run(scenario())
    assert sender.attempts == 3
    assert limiter.sleeps == [1, 2]


def test_big_primitive_keeps_waiting_through_repeated_floods():
    from telethon.errors import FloodWaitError

    class Limiter:
        def __init__(self):
            self.sleeps = []
            self.floods = []

        @asynccontextmanager
        async def acquire(self):
            yield

        def now(self):
            return 0.0

        def success(self, _duration):
            pass

        def flood(self, seconds, *, premium=False):
            self.floods.append((seconds, premium))

        async def sleep(self, seconds):
            self.sleeps.append(seconds)

    class FloodsThenSucceeds:
        def __init__(self):
            self.attempts = 0

        async def send(self, request):
            self.attempts += 1
            if self.attempts <= 12:
                raise FloodWaitError(request, capture=1)

    async def scenario():
        sender, limiter = FloodsThenSucceeds(), Limiter()
        async with _PartReader(io.BytesIO(b"x")) as reader:
            await upload_big_file_parts(sender, limiter, reader, 1, "tail", force_big=True)
        return sender, limiter

    sender, limiter = run(scenario())
    assert sender.attempts == 13
    assert len(limiter.floods) == 12
    assert limiter.sleeps == []


def test_send_part_raises_on_missing_sender():
    gate = fast_gate()
    request = make_request()

    with pytest.raises(RuntimeError):
        run(send_part(lambda: None, request, gate, "part"))


def test_send_part_does_not_retry_on_a_plain_error():
    gate = fast_gate()
    request = make_request()

    class AlwaysFails:
        async def send(self, req):
            raise RuntimeError("not a flood")

    with pytest.raises(RuntimeError):
        run(send_part(lambda: AlwaysFails(), request, gate, "part"))


def test_send_part_does_not_swallow_cancellation():
    gate = fast_gate()
    request = make_request()

    class HangsForever:
        async def send(self, req):
            await asyncio.sleep(3600)

    async def scenario():
        task = asyncio.ensure_future(send_part(lambda: HangsForever(), request, gate, "part"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())


# --------------------------------------------------------------------------- #
# upload_file_parts
# --------------------------------------------------------------------------- #


def _segment_bytes(size: int) -> bytes:
    return bytes(i % 256 for i in range(size))


def test_upload_file_parts_segment_relative_indices_and_bytes(tmp_path):
    size = 2 * PART_SIZE + 100
    data = _segment_bytes(size)
    path = tmp_path / "seg.bin"
    path.write_bytes(data)

    sender = RecordingSender()
    client = FakeClient(sender)
    progress_calls = []

    async def scenario():
        gate = fast_gate(max_window=4)
        stream = open(path, "rb")
        try:
            async with _PartReader(stream) as reader:
                handle = await upload_file_parts(
                    client=client,
                    gate=gate,
                    reader=reader,
                    size=size,
                    file_name="seg.bin",
                    progress=lambda sent, total: progress_calls.append((sent, total)),
                )
        finally:
            stream.close()
        return handle

    handle = run(scenario())

    assert sorted(sender.sent_indices) == [0, 1, 2]
    assert handle.parts == 3
    assert handle.name == "seg.bin"
    # progress is monotonic and ends exactly at size, even though completion
    # order is not guaranteed.
    assert [c[1] for c in progress_calls] == [size] * len(progress_calls)
    sent_values = [c[0] for c in progress_calls]
    assert sent_values == sorted(sent_values)
    assert sent_values[-1] == size


def test_upload_file_parts_uses_a_fresh_file_id_per_call(tmp_path):
    size = PART_SIZE + 1
    data = _segment_bytes(size)
    path = tmp_path / "seg.bin"
    path.write_bytes(data)

    async def upload_once():
        sender = RecordingSender()
        client = FakeClient(sender)
        gate = fast_gate()
        stream = open(path, "rb")
        try:
            async with _PartReader(stream) as reader:
                return await upload_file_parts(
                    client=client, gate=gate, reader=reader, size=size, file_name="seg.bin"
                )
        finally:
            stream.close()

    handle_a = run(upload_once())
    handle_b = run(upload_once())
    assert handle_a.id != handle_b.id


def test_upload_file_parts_bytes_match_offsets(tmp_path):
    size = 3 * PART_SIZE - 7
    data = _segment_bytes(size)
    path = tmp_path / "seg.bin"
    path.write_bytes(data)

    class ByteCheckingSender:
        def __init__(self):
            self.seen = {}

        async def send(self, request):
            await asyncio.sleep(0)
            self.seen[request.file_part] = bytes(request.bytes)

    sender = ByteCheckingSender()
    client = FakeClient(sender)

    async def scenario():
        gate = fast_gate(max_window=4)
        stream = open(path, "rb")
        try:
            async with _PartReader(stream) as reader:
                await upload_file_parts(
                    client=client, gate=gate, reader=reader, size=size, file_name="seg.bin"
                )
        finally:
            stream.close()

    run(scenario())

    parts = plan_parts(size)
    assert len(sender.seen) == len(parts)
    for index, (offset, n) in enumerate(parts):
        assert sender.seen[index] == data[offset : offset + n]


def test_upload_file_parts_permanent_failure_cancels_siblings(tmp_path):
    size = 8 * PART_SIZE
    path = tmp_path / "seg.bin"
    path.write_bytes(_segment_bytes(size))

    sender = RecordingSender(fail_counts={3: float("inf")})
    client = FakeClient(sender)

    async def scenario():
        gate = fast_gate(max_window=4)
        stream = open(path, "rb")
        try:
            async with _PartReader(stream) as reader:
                with pytest.raises(RuntimeError):
                    await upload_file_parts(
                        client=client, gate=gate, reader=reader, size=size, file_name="seg.bin"
                    )
        finally:
            stream.close()
        # no task should be left running after upload_file_parts raises
        pending = [t for t in asyncio.all_tasks() if not t.done() and t is not asyncio.current_task()]
        assert pending == []

    run(scenario())
    # part 3 always fails, so it (and likely some siblings) never got to
    # complete PART_RETRIES successful attempts for every one of the 8 parts.
    assert sender.sent_indices.count(3) >= 1
    assert sender.peak_concurrent <= 4


def test_upload_file_parts_respects_the_window(tmp_path):
    size = 10 * PART_SIZE
    path = tmp_path / "seg.bin"
    path.write_bytes(_segment_bytes(size))

    sender = RecordingSender()
    client = FakeClient(sender)

    async def scenario():
        gate = fast_gate(max_window=3)
        stream = open(path, "rb")
        try:
            async with _PartReader(stream) as reader:
                await upload_file_parts(
                    client=client, gate=gate, reader=reader, size=size, file_name="seg.bin"
                )
        finally:
            stream.close()

    run(scenario())
    assert sender.peak_concurrent <= 3
    assert sorted(sender.sent_indices) == list(range(10))
