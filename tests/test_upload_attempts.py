from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from tgupload import (
    AttemptRevoked,
    _cancelable_context,
    _wait_or_revoke,
    send_part,
)
from transfer_models import UploadRpcToken


class Gate:
    def __init__(self):
        self._now = 0.0
        self.events = []
        self.block_pace = None

    def now(self):
        return self._now

    @asynccontextmanager
    async def slot(self):
        self.events.append("slot+")
        try:
            yield
        finally:
            self.events.append("slot-")

    async def pace(self):
        self.events.append("pace")
        if self.block_pace is not None:
            await self.block_pace.wait()

    def mark_send_started(self):
        self.events.append("send-started")

    def success(self, _duration):
        self.events.append("success")

    def flood(self, seconds, *, premium=False):
        self.events.append(("flood", seconds, premium))

    def snapshot(self):
        return {"mode": "frozen"}


class Sender:
    def __init__(self, future=None):
        self.future = future
        self.calls = []

    def send(self, request):
        self.calls.append(request)
        if self.future is None:
            fut = asyncio.get_running_loop().create_future()
            fut.set_result(True)
            return fut
        return self.future


class Observer:
    def __init__(self, revoked=None):
        self.events = []
        self.sequence = 0
        self.revoked = revoked
        self.late = []

    def request_started(self, part_index, nbytes):
        self.sequence += 1
        token = UploadRpcToken("task", 1, 1, part_index, self.sequence)
        self.events.append(("start", token))
        if self.revoked is not None:
            self.revoked.set()
        return token

    def request_succeeded(self, token, nbytes):
        self.events.append(("success", token, nbytes))

    def request_settled(self, token):
        self.events.append(("settle", token))

    def late_request_succeeded(self, token, nbytes):
        self.late.append((token, nbytes))

    def premium_flood(self, seconds, pacer_snapshot):
        self.events.append(("premium", seconds, pacer_snapshot))


def test_revoke_and_slot_acquire_same_tick_releases_slot():
    async def exercise():
        revoked = asyncio.Event()
        slot = asyncio.BoundedSemaphore(1)

        @asynccontextmanager
        async def acquire_and_revoke():
            await slot.acquire()
            revoked.set()
            try:
                yield
            finally:
                slot.release()

        with pytest.raises(AttemptRevoked):
            async with _cancelable_context(acquire_and_revoke(), revoked):
                pytest.fail("revoked context body must not run")
        await asyncio.wait_for(slot.acquire(), timeout=0.1)
        slot.release()

    asyncio.run(exercise())


def test_revocation_during_pace_prevents_token_and_send():
    async def exercise():
        revoked = asyncio.Event()
        gate = Gate()
        gate.block_pace = asyncio.Event()
        sender = Sender()
        observer = Observer()
        task = asyncio.create_task(send_part(
            lambda: sender, object(), gate, "part", part_index=0, nbytes=10,
            observer=observer, revoked=revoked,
        ))
        await asyncio.sleep(0)
        revoked.set()
        with pytest.raises(AttemptRevoked):
            await task
        assert sender.calls == []
        assert observer.events == []
        assert gate.events[-1] == "slot-"

    asyncio.run(exercise())


def test_committed_token_still_sends_if_revoke_happens_after_begin_request():
    async def exercise():
        revoked = asyncio.Event()
        gate = Gate()
        sender = Sender()
        observer = Observer(revoked=revoked)
        await send_part(
            lambda: sender, "request", gate, "part", part_index=3, nbytes=99,
            observer=observer, revoked=revoked,
        )
        token = observer.events[0][1]
        assert sender.calls == ["request"]
        assert observer.events == [
            ("start", token), ("success", token, 99), ("settle", token),
        ]
        assert "send-started" in gate.events

    asyncio.run(exercise())


def test_timeout_settles_wrapper_and_late_success_is_physical_only():
    async def exercise():
        loop = asyncio.get_running_loop()
        rpc = loop.create_future()
        gate = Gate()
        sender = Sender(rpc)
        observer = Observer()
        with pytest.raises(asyncio.TimeoutError):
            await send_part(
                lambda: sender, "request", gate, "part", part_index=0, nbytes=512,
                observer=observer, rpc_timeout=0.001,
            )
        token = observer.events[0][1]
        assert observer.events[-1] == ("settle", token)
        assert not observer.late
        rpc.set_result(True)
        await asyncio.sleep(0)
        assert observer.late == [(token, 512)]
        assert not any(event[0] == "success" for event in observer.events if isinstance(event, tuple))

    asyncio.run(exercise())


def test_wait_or_revoke_completion_wins_same_tick():
    async def exercise():
        revoked = asyncio.Event()

        async def work():
            revoked.set()
            return 42

        assert await _wait_or_revoke(work(), revoked) == 42

    asyncio.run(exercise())


def test_normal_send_with_no_observer_preserves_old_behavior():
    async def exercise():
        gate = Gate()
        sender = Sender()
        await send_part(lambda: sender, "request", gate, "part")
        assert sender.calls == ["request"]
        assert gate.events[-1] == "success"

    asyncio.run(exercise())


def test_premium_flood_callback_receives_post_flood_snapshot(monkeypatch):
    import tgupload

    class PremiumError(RuntimeError):
        pass

    monkeypatch.setattr(tgupload, "_flood_wait", lambda exc: (7.0, True) if isinstance(exc, PremiumError) else None)

    class FloodThenSuccess(Sender):
        def __init__(self):
            super().__init__()
            self.count = 0

        def send(self, request):
            self.count += 1
            fut = asyncio.get_running_loop().create_future()
            if self.count == 1:
                fut.set_exception(PremiumError("premium"))
            else:
                fut.set_result(True)
            self.calls.append(request)
            return fut

    async def exercise():
        gate = Gate()
        sender = FloodThenSuccess()
        observer = Observer()
        await send_part(
            lambda: sender, "request", gate, "part", part_index=1, nbytes=50,
            observer=observer,
        )
        premium = [event for event in observer.events if event[0] == "premium"]
        assert premium == [("premium", 7.0, {"mode": "frozen"})]
        starts = [event for event in observer.events if event[0] == "start"]
        settles = [event for event in observer.events if event[0] == "settle"]
        successes = [event for event in observer.events if event[0] == "success"]
        assert len(starts) == len(settles) == 2
        assert len(successes) == 1
        assert starts[0][1] != starts[1][1]

    asyncio.run(exercise())
