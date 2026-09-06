"""Offline tests for tgupload.UploadGate.

No Telegram, no network, no real waiting: a fake clock and a fake sleeper
that advances it stand in for time, so flood/backoff sequences that span
minutes run instantly.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tgupload import (  # noqa: E402
    CLEAN_WINDOW,
    INCREASE_INTERVAL,
    MIN_RATE,
    UploadGate,
)


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_sleeper(clock: FakeClock):
    calls = []

    async def sleeper(seconds: float) -> None:
        calls.append(seconds)
        clock.advance(seconds)

    sleeper.calls = calls
    return sleeper


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# pace() / window_slot() basics
# --------------------------------------------------------------------------- #


def test_pace_applies_web_initial_rate_before_a_flood():
    clock = FakeClock()
    sleeper = make_sleeper(clock)
    gate = UploadGate(max_window=12, clock=clock, sleeper=sleeper)

    async def scenario():
        for _ in range(5):
            await gate.pace()

    run(scenario())
    assert sleeper.calls == [0.25, 0.25]


def test_window_slot_bounds_concurrency():
    gate = UploadGate(max_window=3)

    async def scenario():
        current = 0
        peak = 0
        release = asyncio.Event()

        async def worker():
            nonlocal current, peak
            async with gate.window_slot():
                current += 1
                peak = max(peak, current)
                await release.wait()
                current -= 1

        tasks = [asyncio.ensure_future(worker()) for _ in range(6)]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert peak == 3
        assert current == 3
        release.set()
        await asyncio.gather(*tasks)
        assert current == 0

    run(scenario())


def test_window_slot_releases_on_failure():
    gate = UploadGate(max_window=1)

    async def scenario():
        with pytest.raises(RuntimeError):
            async with gate.window_slot():
                raise RuntimeError("boom")
        # the slot must be free again, or a second acquire would hang
        async with gate.window_slot():
            pass

    run(scenario())


# --------------------------------------------------------------------------- #
# report_flood / report_success
# --------------------------------------------------------------------------- #


def test_distinct_event_guard_on_simultaneous_floods():
    clock = FakeClock()
    gate = UploadGate(max_window=8, clock=clock)

    before = gate.stats()
    for _ in range(12):
        gate.report_flood(3)

    after = gate.stats()
    assert after["floods"] == 12
    assert after["window"] == before["window"]
    assert after["rate"] == pytest.approx(before["rate"] * 0.5)
    assert after["ceiling"] == 2


def test_flood_backoff_is_independent_of_measured_rtt():
    clock = FakeClock()
    gate = UploadGate(max_window=3, clock=clock)
    gate.report_success(2.0)

    gate.report_flood(5)

    assert gate.stats()["rate"] == pytest.approx(2.25)


def test_concurrency_window_is_unchanged_by_floods():
    clock = FakeClock()
    gate = UploadGate(max_window=8, clock=clock)

    windows = []
    for _ in range(5):
        gate.report_flood(1)
        windows.append(gate.stats()["window"])
        clock.advance(100.0)  # clear the penalty window before the next flood

    assert windows == [8, 8, 8, 8, 8]


def test_min_rate_floor():
    clock = FakeClock()
    gate = UploadGate(max_window=4, clock=clock)
    gate.report_success(1000.0)  # huge rtt -> tiny achieved throughput

    for _ in range(10):
        gate.report_flood(1)
        clock.advance(100.0)

    assert gate.stats()["rate"] == pytest.approx(MIN_RATE)


def test_learned_ceiling_is_held_until_probe_cooldown():
    clock = FakeClock()
    gate = UploadGate(max_window=4, clock=clock)

    gate.report_flood(2)
    stats = gate.stats()
    assert stats["window"] == 4
    assert stats["rate"] == pytest.approx(2.0)

    # Climb the rate: each successful report needs a clean window since the
    # flood and a minimum gap since the last increase.
    clock.advance(CLEAN_WINDOW)
    for _ in range(4):
        clock.advance(INCREASE_INTERVAL)
        gate.report_success(1.0)

    stats = gate.stats()
    assert stats["rate"] == 2.0
    assert stats["ceiling"] == 2.0
    assert stats["window"] == 4

    clock.advance(300)
    gate.report_success(1.0)
    assert gate.stats()["rate"] == 2.2
    assert gate.stats()["ceiling"] == 2.0
    assert gate.stats()["window"] == 4


def test_success_clean_window_gate():
    clock = FakeClock()
    gate = UploadGate(max_window=4, clock=clock)
    gate.report_success(1.0)
    gate.report_flood(2)
    before = gate.stats()

    clock.advance(CLEAN_WINDOW - 1)  # not clean long enough yet
    gate.report_success(1.0)
    assert gate.stats() == before


# --------------------------------------------------------------------------- #
# virtual-time slot scheduling
# --------------------------------------------------------------------------- #


def test_pace_spaces_out_calls_once_a_rate_is_set():
    clock = FakeClock()
    sleeper = make_sleeper(clock)
    gate = UploadGate(max_window=4, clock=clock, sleeper=sleeper)
    gate.report_success(1.0)
    gate.report_flood(1)  # rate -> 4/1.0*0.5 = 2.0 parts/s, interval 0.5s

    clock.advance(100.0)  # clear the flood penalty

    async def scenario():
        for _ in range(4):
            await gate.pace()

    run(scenario())
    # first call may not sleep (burst absorbs it); later calls are paced.
    assert sum(sleeper.calls) > 0
    assert all(c >= 0 for c in sleeper.calls)
