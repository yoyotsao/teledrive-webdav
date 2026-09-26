from __future__ import annotations

from dataclasses import dataclass

import pytest

from segment_scheduler import SegmentDescriptor, SegmentScheduler, SegmentState
from transfer_models import AttemptLease, UploadedPart
from upload_activity import AccountActivityRegistry, UploadSpeedTracker


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


@dataclass
class Runtime:
    telegram_user_id: int
    online: bool = True
    linked: bool = True


class Lease:
    def __init__(self, pool, account_id, work_id):
        self.pool = pool
        self.runtime = pool.runtime(account_id)
        self.work_id = work_id
        self.fake_release_count = 0
        self.closed = False

    def close(self):
        if self.closed:
            from upload_activity import ActivityChange
            return ActivityChange(self.runtime.telegram_user_id, False, False, False)
        self.closed = True
        self.fake_release_count += 1
        return self.pool.activity.end_job(self.runtime.telegram_user_id, self.work_id)


class Pool:
    def __init__(self, activity, account_ids=(1, 2, 3)):
        self.activity = activity
        self._runtimes = {account_id: Runtime(account_id) for account_id in account_ids}
        self._rr = 0
        for account_id in account_ids:
            activity.add_account(account_id)

    @property
    def eligible_upload_ids(self):
        return tuple(k for k, v in self._runtimes.items() if v.online and v.linked)

    def runtime(self, account_id):
        return self._runtimes[account_id]

    def acquire_exact_upload_lease(self, account_id, work_id):
        snap = self.activity.snapshot(account_id)
        if not self.runtime(account_id).online or not self.runtime(account_id).linked:
            return None
        if snap.reserved_task_id is not None or snap.active_byte_upload_jobs:
            return None
        self.activity.begin_job(account_id, work_id)
        return Lease(self, account_id, work_id)

    def acquire_upload_lease(self, work_id, timeout=0):
        ids = self.eligible_upload_ids
        for _ in ids:
            account_id = ids[self._rr % len(ids)]
            self._rr += 1
            lease = self.acquire_exact_upload_lease(account_id, work_id)
            if lease is not None:
                return lease
        return None

    def try_reserve_idle(self, account_id, task_id):
        runtime = self.runtime(account_id)
        if not runtime.online or not runtime.linked:
            return False
        snap = self.activity.snapshot(account_id)
        if not snap.idle or snap.idle_snapshot is None:
            return False
        return self.activity.reserve_if_idle(account_id, task_id)

    def activate_reservation(self, account_id, task_id):
        change = self.activity.activate_reservation(account_id, task_id, task_id)
        if not change.changed:
            raise RuntimeError("reservation unavailable")
        return Lease(self, account_id, task_id)

    def release_reservation(self, account_id, task_id):
        return self.activity.release_reservation(account_id, task_id)


@pytest.fixture
def rig():
    clock = Clock()
    speed = UploadSpeedTracker(clock=clock, window=30, snapshot_ttl=300)
    activity = AccountActivityRegistry(clock=clock, speed_tracker=speed)
    pool = Pool(activity)
    scheduler = SegmentScheduler(
        "file", [], pool=pool, activity=activity, speed_tracker=speed, clock=clock,
    )
    scheduler.register_segment(SegmentDescriptor(0, 0, 1000), task_id="task")
    return clock, speed, activity, pool, scheduler


def make_idle_snapshot(speed_tracker, activity, account_id, bps):
    work_id = f"sample:{account_id}"
    activity.begin_job(account_id, work_id)
    speed_tracker.record_effective(AttemptLease(work_id, 1, account_id), 0, int(bps * 30))
    activity.end_job(account_id, work_id)
    assert activity.snapshot(account_id).idle_snapshot is not None


def test_candidate_requires_age_premium_flood_and_strict_score(rig):
    clock, speed, activity, pool, scheduler = rig
    old = scheduler.activate("task", 2)
    scheduler.notify_premium_flood(old, 30, {"mode": "frozen"})
    make_idle_snapshot(speed, activity, 1, 20)
    speed.record_effective(old, 99, 300)  # 10 B/s without changing logical progress.
    assert scheduler.score(1, "task") is None  # too young
    clock.advance(30)
    assert scheduler.score(1, "task") == 2
    assert scheduler.commit_migration(1, "task") is None
    # Refresh the idle snapshot at 20.1 B/s.
    activity.begin_job(1, "refresh")
    speed.record_effective(AttemptLease("refresh", 1, 1), 0, 3)
    activity.end_job(1, "refresh")
    assert scheduler.score(1, "task") > 2
    assert scheduler.commit_migration(1, "task") is not None


def test_qualification_never_changes_ownership_and_only_commit_migrates(rig):
    clock, speed, activity, pool, scheduler = rig
    old = scheduler.activate("task", 2)
    scheduler.notify_premium_flood(old, 30, {"mode": "frozen"})
    clock.advance(30)
    assert scheduler.task("task").state is SegmentState.ACTIVE
    assert scheduler.task("task").attempt_id == old.attempt_id
    make_idle_snapshot(speed, activity, 1, 30)
    speed.record_effective(old, 99, 30)
    commit = scheduler.commit_migration(1, "task")
    assert commit is not None
    task = scheduler.task("task")
    assert task.state is SegmentState.MIGRATING
    assert task.attempt_id == old.attempt_id + 1
    assert task.current_account_id is None
    assert not scheduler.grant_finalize(old)


def test_stale_settlement_drains_tokens_without_changing_logical_state(rig):
    clock, speed, activity, pool, scheduler = rig
    old = scheduler.activate("task", 2)
    token = scheduler.begin_request(old, part_index=0)
    scheduler.notify_premium_flood(old, 30, {"mode": "frozen"})
    clock.advance(30)
    make_idle_snapshot(speed, activity, 1, 30)
    speed.record_effective(old, 99, 30)
    scheduler.commit_migration(1, "task")
    assert not scheduler.part_succeeded(old, 0, 512)
    assert scheduler.physical_success(token, 512)
    assert scheduler.request_settled(token)
    assert not scheduler.request_settled(token)
    assert scheduler.drained("task", old.attempt_id)
    assert scheduler.task("task").logical_uploaded_bytes == 0


def test_generation_validated_progress_is_unique_and_migration_resets_logical(rig):
    clock, speed, activity, pool, scheduler = rig
    old = scheduler.activate("task", 2)
    assert scheduler.part_succeeded(old, 0, 200)
    assert not scheduler.part_succeeded(old, 0, 200)
    assert scheduler.task("task").logical_uploaded_bytes == 200
    scheduler.notify_premium_flood(old, 30, {"mode": "frozen"})
    clock.advance(30)
    make_idle_snapshot(speed, activity, 1, 100)
    scheduler.commit_migration(1, "task")
    assert scheduler.task("task").logical_uploaded_bytes == 0
    assert not scheduler.part_succeeded(old, 1, 200)


def test_finalize_and_migration_are_mutually_exclusive(rig):
    clock, speed, activity, pool, scheduler = rig
    old = scheduler.activate("task", 2)
    scheduler.notify_premium_flood(old, 30, {"mode": "frozen"})
    clock.advance(30)
    make_idle_snapshot(speed, activity, 1, 100)
    assert scheduler.grant_finalize(old)
    assert scheduler.commit_migration(1, "task") is None
    assert scheduler.task("task").state is SegmentState.FINALIZING


def test_one_migration_max_and_attempted_account_exclusion(rig):
    clock, speed, activity, pool, scheduler = rig
    old = scheduler.activate("task", 2)
    scheduler.notify_premium_flood(old, 30, {"mode": "frozen"})
    clock.advance(30)
    make_idle_snapshot(speed, activity, 1, 100)
    commit = scheduler.commit_migration(1, "task")
    assert commit is not None
    scheduler.attempt_quiesced(old)
    replacement_action = scheduler.select_next_action()
    assert replacement_action is not None and replacement_action.account_id == 1
    replacement = replacement_action.lease
    scheduler.notify_premium_flood(replacement, 30, {"mode": "frozen"})
    clock.advance(30)
    make_idle_snapshot(speed, activity, 3, 1000)
    assert scheduler.commit_migration(3, "task") is None
    assert scheduler.commit_migration(2, "task") is None


def test_select_next_action_cannot_issue_same_attempt_twice():
    clock = Clock()
    speed = UploadSpeedTracker(clock=clock)
    activity = AccountActivityRegistry(clock=clock, speed_tracker=speed)
    pool = Pool(activity, account_ids=(1,))
    scheduler = SegmentScheduler(
        "file", [SegmentDescriptor(0, 0, 100)], pool=pool,
        activity=activity, speed_tracker=speed, clock=clock,
    )
    first = scheduler.select_next_action()
    assert first is not None
    assert scheduler.select_next_action() is None
    assert scheduler.task(first.task_id).active_upload_lease is not None


def test_prepared_and_terminal_cleanup_close_the_same_lease_once(rig):
    clock, speed, activity, pool, scheduler = rig
    lease = scheduler.activate("task", 1)
    owned = scheduler.task("task").active_upload_lease
    scheduler.part_succeeded(lease, 0, 1000)
    assert scheduler.bytes_prepared(lease)
    assert owned.fake_release_count == 1
    scheduler.fail_attempt(lease, "message failed")
    scheduler.attempt_quiesced(lease)
    assert owned.fake_release_count == 1
    assert scheduler.task("task").active_upload_lease is None


def test_late_physical_success_survives_terminal_state(rig):
    clock, speed, activity, pool, scheduler = rig
    lease = scheduler.activate("task", 2)
    token = scheduler.begin_request(lease, 0)
    assert scheduler.fail_attempt(lease, "failed")
    assert scheduler.physical_success(token, 512)
    assert scheduler.request_settled(token)
    assert speed.physical_bytes(2) == 512
    assert scheduler.task("task").state is SegmentState.FAILED


def test_next_deadline_tracks_candidate_age_and_snapshot_expiry(rig):
    clock, speed, activity, pool, scheduler = rig
    lease = scheduler.activate("task", 2)
    scheduler.notify_premium_flood(lease, 30, {"mode": "frozen"})
    make_idle_snapshot(speed, activity, 1, 10)
    assert scheduler.next_deadline() == 30
    clock.advance(30)
    # Candidate is still valid at the exact 30-second boundary, so schedule one
    # representable instant later to observe premium-recency expiry without a spin.
    expiry_wake = scheduler.next_deadline()
    assert expiry_wake > 30
    clock.advance(0.001)
    assert scheduler.next_deadline() == pytest.approx(300)


def test_completed_results_are_sorted_by_segment_index():
    clock = Clock()
    speed = UploadSpeedTracker(clock=clock)
    activity = AccountActivityRegistry(clock=clock, speed_tracker=speed)
    pool = Pool(activity, account_ids=(1,))
    scheduler = SegmentScheduler(
        "file", [SegmentDescriptor(1, 100, 100), SegmentDescriptor(0, 0, 100)],
        pool=pool, activity=activity, speed_tracker=speed, clock=clock,
    )
    for _ in range(2):
        action = scheduler.select_next_action()
        assert action is not None
        lease = action.lease
        scheduler.part_succeeded(lease, 0, 100)
        scheduler.bytes_prepared(lease)
        assert scheduler.grant_finalize(lease)
        result = UploadedPart(action.descriptor.index, 10 + action.descriptor.index, str(action.descriptor.index), None, 100, 1)
        assert scheduler.complete_attempt(lease, result)
        scheduler.attempt_quiesced(lease)
    assert [item.index for item in scheduler.completed_results_by_index()] == [0, 1]


def test_zero_current_speed_is_infinite_only_after_candidate_gates(rig):
    clock, speed, activity, pool, scheduler = rig
    lease = scheduler.activate("task", 2)
    make_idle_snapshot(speed, activity, 1, 10)
    assert scheduler.score(1, "task") is None
    scheduler.notify_premium_flood(lease, 5, {"mode": "frozen"})
    assert scheduler.score(1, "task") is None
    clock.advance(30)
    assert scheduler.score(1, "task") == float("inf")


def test_migration_selection_precedes_pending_assignment():
    clock = Clock()
    speed = UploadSpeedTracker(clock=clock)
    activity = AccountActivityRegistry(clock=clock, speed_tracker=speed)
    pool = Pool(activity, account_ids=(1, 2, 3))
    scheduler = SegmentScheduler(
        "file", [SegmentDescriptor(0, 0, 1000), SegmentDescriptor(1, 1000, 1000)],
        pool=pool, activity=activity, speed_tracker=speed, clock=clock,
    )
    first = scheduler.activate("file:0", 2)
    scheduler.notify_premium_flood(first, 30, {"mode": "frozen"})
    clock.advance(30)
    make_idle_snapshot(speed, activity, 1, 100)
    action = scheduler.select_next_action()
    assert action is None
    assert scheduler.task("file:0").state is SegmentState.MIGRATING
    assert scheduler.task("file:1").state is SegmentState.PENDING


def test_terminal_transition_is_idempotent(rig):
    clock, speed, activity, pool, scheduler = rig
    lease = scheduler.activate("task", 2)
    assert scheduler.fail_attempt(lease, "first")
    assert not scheduler.fail_attempt(lease, "second")
    assert scheduler.task("task").error_category == "first"
