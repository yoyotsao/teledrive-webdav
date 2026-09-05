"""Deterministic Web limiter transition vectors and real persistence effects."""

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)


@pytest.fixture
def module():
    # Keep a missing implementation an explicit test failure, not collection error.
    assert importlib.util.find_spec("upload_limiter"), "Web limiter module is missing"
    return importlib.import_module("upload_limiter")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def limiter(module, clock):
    return module.AdaptiveUploadLimiter(clock=clock, sleeper=clock.sleep)


def test_web_constants(module):
    cfg = module.LimiterConfig.web_defaults()
    assert (cfg.initial, cfg.minimum, cfg.maximum, cfg.burst) == (4.0, 0.5, 12.0, 2)
    assert (cfg.increase_step, cfg.increase_interval) == (0.5, 10.0)
    assert (cfg.clean_window, cfg.first_backoff, cfg.backoff) == (20.0, 0.5, 0.95)
    assert (cfg.slow_zone, cfg.slow_step, cfg.slow_interval) == (0.8, 0.1, 30.0)
    assert (cfg.probe_cooldown, cfg.probe_cooldown_max) == (300.0, 1800.0)
    assert (cfg.probe_step, cfg.probe_confirm) == (0.2, 60.0)
    assert (cfg.escalation_count, cfg.escalation_window) == (3, 120.0)


def test_initial_ramp_has_interval_and_maximum(limiter, clock):
    assert limiter.snapshot().rate == 4
    limiter.success(1000)  # RTT does not determine the rate.
    assert limiter.snapshot().rate == 4.5
    clock.advance(9.99)
    limiter.success(0.001)
    assert limiter.snapshot().rate == 4.5
    clock.advance(0.01)
    limiter.success(0)
    assert limiter.snapshot().rate == 5
    for _ in range(30):
        clock.advance(10)
        limiter.success(1)
    assert limiter.snapshot().rate == 12


def test_first_flood_and_concurrent_distinct_event_guard(limiter, clock):
    for _ in range(12):
        limiter.flood(3)
    state = limiter.snapshot()
    assert (state.rate, state.ceiling, state.floods, state.window) == (2, 2, 12, 12)
    assert state.paused_until == 1004
    assert not state.escalated
    clock.advance(4)
    limiter.flood(3)
    state = limiter.snapshot()
    assert (state.rate, state.ceiling) == (1, 1.9)
    assert not state.escalated


def test_premium_wait_only_pauses(limiter, clock):
    before = limiter.snapshot()
    limiter.flood(17, premium=True)
    after = limiter.snapshot()
    assert (after.rate, after.ceiling, after.floods) == (before.rate, before.ceiling, 0)
    assert after.paused_until == clock.now + 18
    limiter.success(1)
    assert limiter.snapshot().rate == 4.5  # Premium does not reset clean-window timing.


def test_premium_wait_holds_pacing_through_the_web_guard_second(limiter, clock):
    limiter.flood(17, premium=True)

    asyncio.run(limiter.pace())

    assert clock.sleeps == [18]
    assert clock.now == 1018


def test_learned_ceiling_fast_zone_and_slow_zone(limiter, clock):
    limiter.flood(1)
    clock.advance(130)  # Distinct but outside the escalation window.
    limiter.flood(1)
    assert limiter.snapshot().rate == 1
    clock.advance(19.99)
    limiter.success(1)
    assert limiter.snapshot().rate == 1
    clock.advance(0.01)
    limiter.success(1)
    assert limiter.snapshot().rate == 1.5
    clock.advance(10)
    limiter.success(1)
    assert limiter.snapshot().rate == pytest.approx(1.52)
    clock.advance(29.99)
    limiter.success(1)
    assert limiter.snapshot().rate == pytest.approx(1.52)
    clock.advance(0.01)
    limiter.success(1)
    assert limiter.snapshot().rate == pytest.approx(1.62)


def test_probe_waits_then_confirms_new_ceiling(limiter, clock):
    limiter.flood(1)
    clock.advance(299.99)
    limiter.success(1)
    assert limiter.snapshot().rate == 2
    clock.advance(0.01)
    limiter.success(1)
    assert (limiter.snapshot().rate, limiter.snapshot().ceiling) == (2.2, 2)
    clock.advance(59.99)
    limiter.success(1)
    assert limiter.snapshot().ceiling == 2
    clock.advance(0.01)
    limiter.success(1)
    assert limiter.snapshot().ceiling == 2.2
    assert limiter.snapshot().probe_started_at is None
    clock.advance(299.99)
    limiter.success(1)
    assert limiter.snapshot().rate == 2.2
    clock.advance(0.01)
    limiter.success(1)
    assert limiter.snapshot().rate == pytest.approx(2.4)


def test_failed_probe_uses_cheap_exit_and_doubles_capped_cooldown(limiter, clock):
    limiter.flood(1)
    for cooldown in [600, 1200, 1800, 1800]:
        clock.advance(limiter.snapshot().probe_cooldown)
        while limiter.snapshot().rate < limiter.snapshot().ceiling:
            limiter.success(1)
            clock.advance(30)
        limiter.success(1)
        assert limiter.snapshot().probe_started_at is not None
        limiter.flood(1)
        state = limiter.snapshot()
        assert state.rate == pytest.approx(state.ceiling * 0.8)
        assert state.probe_started_at is None
        assert state.probe_cooldown == cooldown
    # The Web formula refines the first failed probe ceiling from 2 to 2.09.


def test_failed_probe_ceiling_vector(limiter, clock):
    limiter.flood(1)
    clock.advance(300)
    limiter.success(1)
    limiter.flood(1)
    assert limiter.snapshot().ceiling == pytest.approx(2.09)
    assert limiter.snapshot().rate == pytest.approx(1.672)


def test_three_flood_escalation_clean_window_and_ten_minute_reset(limiter, clock):
    for _ in range(3):
        limiter.flood(1)
        clock.advance(3)
    state = limiter.snapshot()
    assert (state.rate, state.ceiling, state.escalated) == (0.5, 1, True)
    clock.advance(56.99)
    limiter.success(1)
    assert limiter.snapshot().rate == 0.5
    clock.advance(0.01)
    limiter.success(1)
    assert limiter.snapshot().rate == 0.8
    clock.advance(539.99)
    assert limiter.snapshot().escalated
    clock.advance(0.01)
    assert not limiter.snapshot().escalated
    limiter.flood(1)
    clock.advance(20)
    limiter.success(1)
    assert limiter.snapshot().rate == 0.8  # Normal 20-second clean window restored.


def test_sliding_escalation_window_excludes_boundary(limiter, clock):
    limiter.flood(1)
    clock.advance(60)
    limiter.flood(1)
    clock.advance(60)
    limiter.flood(1)
    assert not limiter.snapshot().escalated


def test_acquire_combines_twelve_slots_and_initial_pacing(limiter, clock):
    async def scenario():
        release = asyncio.Event()
        entered = []

        async def worker():
            async with limiter.acquire():
                entered.append(clock.now)
                await release.wait()

        tasks = [asyncio.create_task(worker()) for _ in range(13)]
        await asyncio.sleep(0)
        assert len(entered) == 12
        assert entered[:5] == [1000, 1000, 1000, 1000.25, 1000.5]
        release.set()
        await asyncio.gather(*tasks)
        assert len(entered) == 13

    asyncio.run(scenario())


def test_acquire_releases_slot_if_cancelled_during_pacing(module, clock):
    async def scenario():
        sleeping = asyncio.Event()
        release = asyncio.Event()

        async def sleep(seconds):
            sleeping.set()
            await release.wait()
            clock.advance(seconds)

        limiter = module.AdaptiveUploadLimiter(max_window=1, clock=clock, sleeper=sleep)
        limiter.flood(5)

        async def worker():
            async with limiter.acquire():
                return True

        task = asyncio.create_task(worker())
        await sleeping.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await asyncio.wait_for(worker(), 1)

    asyncio.run(scenario())


def test_pending_pace_rechecks_new_flood_and_preserves_spacing(module, clock):
    async def scenario():
        pending = []

        async def sleep(seconds):
            future = asyncio.get_running_loop().create_future()
            pending.append((clock.now + seconds, future))
            await future

        limiter = module.AdaptiveUploadLimiter(clock=clock, sleeper=sleep)
        for _ in range(3):
            await limiter.pace()
        admitted = []

        async def worker():
            await limiter.pace()
            admitted.append(clock.now)

        tasks = [asyncio.create_task(worker()) for _ in range(2)]
        await asyncio.sleep(0)
        limiter.flood(5)
        clock.advance(0.5)
        for deadline, future in list(pending):
            if deadline <= clock.now:
                future.set_result(None)
                pending.remove((deadline, future))
        await asyncio.sleep(0)
        assert admitted == []
        assert [deadline for deadline, _ in pending] == [1006, 1006.5]
        for deadline, future in list(pending):
            clock.now = deadline
            future.set_result(None)
            await asyncio.sleep(0)
        await asyncio.gather(*tasks)
        assert admitted == [1006, 1006.5]

    asyncio.run(scenario())


def test_persists_per_account_and_reloads_discounted_ceiling(module, clock, tmp_path):
    first = module.AdaptiveUploadLimiter(account_id=101, cache_dir=tmp_path, clock=clock)
    second = module.AdaptiveUploadLimiter(account_id=202, cache_dir=tmp_path, clock=clock)
    first.flood(1)
    second.success(1)
    first_path = tmp_path / "meta/upload-rate-101.json"
    second_path = tmp_path / "meta/upload-rate-202.json"
    assert json.loads(first_path.read_text())["version"] == 1
    assert json.loads(first_path.read_text())["rate"] == 2
    assert json.loads(second_path.read_text())["rate"] == 4.5
    restored = module.AdaptiveUploadLimiter(account_id=101, cache_dir=tmp_path, clock=clock)
    assert (restored.snapshot().rate, restored.snapshot().ceiling) == (1.6, 2)
    assert restored.snapshot().floods == 0
    assert restored.snapshot().paused_until == 0
    assert not list((tmp_path / "meta").glob("*.part"))


@pytest.mark.parametrize("payload", ["not json", "[]", '{"version":99}', '{"version":1,"rate":"bad"}', '{"version":1,"rate":NaN,"updated_at":1000}'])
def test_corrupt_state_falls_back(module, clock, tmp_path, payload):
    meta = tmp_path / "meta"
    meta.mkdir()
    (meta / "upload-rate-101.json").write_text(payload)
    limiter = module.AdaptiveUploadLimiter(account_id=101, cache_dir=tmp_path, clock=clock)
    assert (limiter.snapshot().rate, limiter.snapshot().ceiling) == (4, None)


def test_stored_learning_expires_after_twenty_four_hours(module, clock, tmp_path):
    limiter = module.AdaptiveUploadLimiter(account_id=101, cache_dir=tmp_path, clock=clock, wall_clock=clock)
    limiter.flood(1)
    clock.advance(86400)
    restored = module.AdaptiveUploadLimiter(account_id=101, cache_dir=tmp_path, clock=clock, wall_clock=clock)
    assert (restored.snapshot().rate, restored.snapshot().ceiling) == (4, None)


def test_atomic_replace_failure_preserves_previous_state(module, clock, tmp_path, monkeypatch):
    limiter = module.AdaptiveUploadLimiter(account_id=101, cache_dir=tmp_path, clock=clock)
    limiter.flood(1)
    path = tmp_path / "meta/upload-rate-101.json"
    original = path.read_bytes()

    def fail_replace(source, target):
        assert Path(source).parent == path.parent
        assert Path(source).suffix == ".part"
        assert json.loads(Path(source).read_text())["rate"] == 1
        raise OSError("disk unavailable")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    clock.advance(3)
    limiter.flood(1)
    assert limiter.snapshot().rate == 1
    assert path.read_bytes() == original
    assert not list(path.parent.glob("*.part"))


def test_message_bucket_burst_six_then_three_per_second(module, clock):
    bucket = module.MessageTokenBucket(clock=clock, sleeper=clock.sleep)

    async def scenario():
        times = []
        for _ in range(9):
            await bucket.acquire()
            times.append(clock.now)
        return times

    times = asyncio.run(scenario())
    assert times[:6] == [1000] * 6
    assert times[6:] == pytest.approx([1000 + 1 / 3, 1000 + 2 / 3, 1001])


def test_account_and_bucket_floods_are_independent(module, clock):
    chunk_a = module.AdaptiveUploadLimiter(account_id=101, clock=clock, sleeper=clock.sleep)
    chunk_b = module.AdaptiveUploadLimiter(account_id=202, clock=clock, sleeper=clock.sleep)
    message_a = module.MessageTokenBucket(clock=clock, sleeper=clock.sleep)
    message_b = module.MessageTokenBucket(clock=clock, sleeper=clock.sleep)
    chunk_a.flood(100)
    asyncio.run(message_a.acquire())
    asyncio.run(chunk_b.pace())
    assert clock.now == 1000
    message_a.flood(10)
    asyncio.run(message_b.acquire())
    asyncio.run(chunk_b.pace())
    assert clock.now == 1000
    asyncio.run(message_a.acquire())
    assert clock.now == pytest.approx(1010 + 1 / 3)
    assert chunk_a.snapshot().rate == 2
    assert chunk_b.snapshot().rate == 4
