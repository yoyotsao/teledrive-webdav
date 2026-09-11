from __future__ import annotations

import logging
from types import SimpleNamespace

from segment_scheduler import MigrationCommit
from transfer_models import AttemptLease
from upload_activity import FloodCycleSnapshot
from upload_engine import TransferMetrics, UploadEngine


def test_transfer_metrics_include_logical_physical_and_migration_counters():
    metrics = TransferMetrics()
    assert metrics.logical_uploaded_bytes == 0
    assert metrics.physical_transferred_bytes == 0
    assert metrics.migration_overhead_bytes == 0
    assert metrics.migration_count == 0


def test_premium_flood_log_uses_closed_physical_cycle(caplog):
    caplog.set_level(logging.INFO, logger="upload_engine")
    lease = AttemptLease("segment:0", 1, 2)
    cycle = FloodCycleSnapshot(lease, 30, 2, 1024, 2, 1024)
    task = SimpleNamespace(index=0, logical_uploaded_bytes=512, size=2048)
    scheduler = SimpleNamespace(
        file_job_id="file-job",
        task=lambda _task_id: task,
        speed_tracker=SimpleNamespace(live_speed=lambda _lease: 17.5),
    )
    pacer = SimpleNamespace(mode="frozen", rate=3.0, paused_until=60.0)

    UploadEngine._log_scheduler_diagnostic(
        scheduler, "premium_flood", {"cycle": cycle, "pacer": pacer}
    )

    text = caplog.text
    for expected in (
        "premium flood", "account_id=2", "task_id=segment:0", "segment_index=0",
        "attempt_id=1", "wait_seconds=30", "pacer_mode=frozen", "rate=3.00",
        "live_speed=17.50", "logical_bytes=512", "remaining_ratio=0.750000",
        "account_accepted_parts=2", "account_accepted_bytes=1024",
        "task_accepted_parts=2", "task_accepted_bytes=1024",
    ):
        assert expected in text


def test_migration_log_contains_decision_inputs_without_secrets(caplog):
    caplog.set_level(logging.INFO, logger="upload_engine")
    commit = MigrationCommit(
        task_id="segment:0", old_attempt_id=1, new_attempt_id=2,
        from_account_id=1, to_account_id=2, score=4.25,
        abandoned_logical_bytes=700, target_snapshot_speed=120.0,
        target_snapshot_age=5.0, current_speed=20.0, remaining_ratio=0.5,
    )
    scheduler = SimpleNamespace(file_job_id="file-job", task=lambda _task_id: SimpleNamespace(index=0, migration_count=1))

    UploadEngine._log_scheduler_diagnostic(scheduler, "migration", {"commit": commit})

    text = caplog.text
    for expected in (
        "segment migration", "old_attempt_id=1", "new_attempt_id=2",
        "from_account_id=1", "to_account_id=2", "snapshot_speed=120.00",
        "snapshot_age=5.00", "current_speed=20.00", "remaining_ratio=0.500000",
        "score=4.250000", "abandoned_bytes=700", "migration_count=1",
    ):
        assert expected in text
    for secret in ("session=", "authorization:", "bearer ", "auth_key"):
        assert secret not in text.lower()


def test_generic_account_observer_contributes_to_idle_speed_snapshot():
    from upload_activity import AccountActivityRegistry, UploadSpeedTracker
    from upload_engine import AccountUploadObserver

    now = [0.0]
    clock = lambda: now[0]
    speed = UploadSpeedTracker(clock=clock)
    activity = AccountActivityRegistry(clock=clock, speed_tracker=speed)
    activity.add_account(7)
    pool = SimpleNamespace(activity=activity, speed_tracker=speed)
    activity.begin_job(7, "small:1")
    observer = AccountUploadObserver(pool, 7, "small:1:bytes")
    token = observer.request_started(0, 300)
    observer.request_succeeded(token, 300)
    observer.request_settled(token)
    activity.end_job(7, "small:1")

    snapshot = activity.snapshot(7).idle_snapshot
    assert snapshot is not None
    assert snapshot.bytes_per_second == 10
