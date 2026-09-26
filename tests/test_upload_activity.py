from __future__ import annotations

import threading

import pytest

from transfer_models import AttemptLease, UploadRpcToken
from upload_activity import AccountActivityRegistry, UploadSpeedTracker


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds: float):
        self.value += seconds


@pytest.fixture
def clock():
    return Clock()


def test_idle_requires_no_jobs_rpcs_or_reservation(clock):
    registry = AccountActivityRegistry(clock=clock)
    registry.add_account(1)
    assert registry.snapshot(1).idle
    registry.begin_job(1, "small:1")
    assert not registry.snapshot(1).idle
    registry.end_job(1, "small:1")
    token = UploadRpcToken("small:1", 1, 1, 0, 1)
    registry.request_started(token)
    assert not registry.snapshot(1).idle
    registry.request_settled(token)
    assert registry.reserve_if_idle(1, "task:1")
    assert not registry.snapshot(1).idle


def test_idle_snapshot_uses_unique_effective_bytes_not_physical_retries(clock):
    tracker = UploadSpeedTracker(clock=clock, window=30, snapshot_ttl=300)
    lease = AttemptLease("task", 1, 1)
    token1 = UploadRpcToken("task", 1, 1, 0, 1)
    token2 = UploadRpcToken("task", 1, 1, 0, 2)
    assert tracker.record_effective(lease, 0, 300)
    assert not tracker.record_effective(lease, 0, 300)
    assert tracker.record_physical(token1, 300)
    assert tracker.record_physical(token2, 300)
    snapshot = tracker.freeze_idle_snapshot(1)
    assert snapshot is not None
    assert snapshot.bytes_per_second == 10


def test_effective_identity_includes_attempt_generation(clock):
    tracker = UploadSpeedTracker(clock=clock, window=30, snapshot_ttl=300)
    old = AttemptLease("task", 1, 1)
    replacement = AttemptLease("task", 2, 2)
    assert tracker.record_effective(old, part_index=0, nbytes=300)
    assert tracker.record_effective(replacement, part_index=0, nbytes=600)
    assert tracker.live_speed(old) == 10
    assert tracker.live_speed(replacement) == 20


def test_premium_flood_cycle_reports_and_resets_physical_totals(clock):
    tracker = UploadSpeedTracker(clock=clock, window=30, snapshot_ttl=300)
    lease = AttemptLease("task", 1, 1)
    tracker.record_physical(UploadRpcToken("task", 1, 1, 0, 1), 512)
    first = tracker.close_premium_flood_cycle(lease, wait_seconds=17)
    second = tracker.close_premium_flood_cycle(lease, wait_seconds=19)
    assert (first.task_accepted_parts, first.task_accepted_bytes) == (1, 512)
    assert (first.account_accepted_parts, first.account_accepted_bytes) == (1, 512)
    assert (second.task_accepted_parts, second.task_accepted_bytes) == (0, 0)
    assert (second.account_accepted_parts, second.account_accepted_bytes) == (0, 0)


def test_account_cycle_includes_other_attempts_and_resets_independently(clock):
    tracker = UploadSpeedTracker(clock=clock, window=30, snapshot_ttl=300)
    a, b = AttemptLease("a", 1, 1), AttemptLease("b", 1, 1)
    tracker.record_physical(UploadRpcToken("a", 1, 1, 0, 1), 512)
    tracker.record_physical(UploadRpcToken("b", 1, 1, 0, 1), 1024)
    first = tracker.close_premium_flood_cycle(a, 17)
    assert (first.account_accepted_parts, first.account_accepted_bytes) == (2, 1536)
    assert (first.task_accepted_parts, first.task_accepted_bytes) == (1, 512)
    second = tracker.close_premium_flood_cycle(b, 19)
    assert (second.account_accepted_parts, second.account_accepted_bytes) == (0, 0)
    assert (second.task_accepted_parts, second.task_accepted_bytes) == (1, 1024)


def test_speed_window_has_fixed_denominator_and_expires_bytes(clock):
    tracker = UploadSpeedTracker(clock=clock, window=30, snapshot_ttl=300)
    lease = AttemptLease("task", 1, 1)
    tracker.record_effective(lease, 0, 300)
    assert tracker.live_speed(lease) == 10
    clock.advance(29.9)
    assert tracker.live_speed(lease) == 10
    clock.advance(0.2)
    assert tracker.live_speed(lease) == 0
    assert tracker.freeze_idle_snapshot(1) is None


def test_idle_snapshot_expires_after_five_minutes(clock):
    tracker = UploadSpeedTracker(clock=clock, window=30, snapshot_ttl=300)
    tracker.record_effective(AttemptLease("task", 1, 1), 0, 300)
    snapshot = tracker.freeze_idle_snapshot(1)
    assert snapshot is not None
    clock.advance(299.9)
    assert tracker.snapshot_is_fresh(snapshot)
    clock.advance(0.2)
    assert not tracker.snapshot_is_fresh(snapshot)


def test_new_work_invalidates_idle_snapshot_and_next_idle_refreezes(clock):
    tracker = UploadSpeedTracker(clock=clock, window=30, snapshot_ttl=300)
    tracker.record_effective(AttemptLease("old", 1, 1), 0, 300)
    registry = AccountActivityRegistry(clock=clock, speed_tracker=tracker)
    registry.add_account(1)
    registry.begin_job(1, "old")
    registry.end_job(1, "old")
    assert registry.snapshot(1).idle_snapshot is not None
    changed = registry.begin_job(1, "new")
    assert changed.snapshot_changed
    assert registry.snapshot(1).idle_snapshot is None
    registry.end_job(1, "new")
    assert registry.snapshot(1).idle_snapshot is not None


def test_physical_success_is_deduped_by_rpc_token_not_part(clock):
    tracker = UploadSpeedTracker(clock=clock)
    a = UploadRpcToken("task", 1, 1, 0, 1)
    retry = UploadRpcToken("task", 1, 1, 0, 2)
    assert tracker.record_physical(a, 512)
    assert not tracker.record_physical(a, 512)
    assert tracker.record_physical(retry, 512)
    assert tracker.physical_bytes(1) == 1024


def test_revoked_physical_success_never_becomes_effective_implicitly(clock):
    tracker = UploadSpeedTracker(clock=clock)
    token = UploadRpcToken("task", 1, 1, 0, 1)
    assert tracker.record_physical(token, 512)
    assert tracker.live_speed(token.lease) == 0
    assert tracker.freeze_idle_snapshot(1) is None


def test_request_cleanup_is_idempotent_and_never_goes_negative(clock):
    registry = AccountActivityRegistry(clock=clock)
    registry.add_account(1)
    token = UploadRpcToken("task", 1, 1, 0, 1)
    first = registry.request_started(token)
    duplicate = registry.request_started(token)
    assert first.changed and not duplicate.changed
    first_settle = registry.request_settled(token)
    duplicate_settle = registry.request_settled(token)
    assert first_settle.changed and not duplicate_settle.changed
    assert registry.snapshot(1).in_flight_upload_rpcs == 0
    registry.end_job(1, "missing")
    assert registry.snapshot(1).active_byte_upload_jobs == 0


def test_reservation_is_atomic_under_contention(clock):
    registry = AccountActivityRegistry(clock=clock)
    registry.add_account(1)
    results = []

    def reserve(task):
        results.append(registry.reserve_if_idle(1, task))

    threads = [threading.Thread(target=reserve, args=(f"task:{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True]
