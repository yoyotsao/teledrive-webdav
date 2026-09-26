from __future__ import annotations

import threading
from pathlib import Path

import pytest

from telegram_accounts import TelegramAccountPool
from transfer_models import AccountSpec, AttemptLease, UploadRpcToken


class Worker:
    def __init__(self, user_id):
        self.user_id = user_id

    def start(self):
        pass

    def stop(self):
        pass

    def send_dm(self, *_args):
        pass


class Factory:
    def __init__(self, ids):
        self.workers = {user_id: Worker(user_id) for user_id in ids}

    def __call__(self, api_id, api_hash, expected_user_id, session_path, connections, *, upload_parts):
        return self.workers[expected_user_id]


class Api:
    def __init__(self, linked):
        self.linked = set(linked)

    def set_dm_sender(self, _sender):
        pass

    def login(self):
        pass

    def linked_account_ids(self):
        return self.linked


def make_pool(ids=(1, 2), *, linked=None, upload_files=2):
    linked = set(ids if linked is None else linked)
    specs = [AccountSpec(user_id, Path(f"/outside/{user_id}.session")) for user_id in ids]
    pool = TelegramAccountPool(
        specs, api_id=1, api_hash="hash", upload_files=upload_files,
        worker_factory=Factory(ids),
    )
    pool.start(Api(linked))
    return pool


def seed_idle_snapshot(pool, account_id, bps=10):
    work = f"sample:{account_id}"
    pool.activity.begin_job(account_id, work)
    pool.speed_tracker.record_effective(AttemptLease(work, 1, account_id), 0, int(bps * 30))
    pool.activity.end_job(account_id, work)
    assert pool.activity.snapshot(account_id).idle_snapshot is not None


def test_busy_account_with_free_file_slots_cannot_be_failover_target():
    pool = make_pool(upload_files=2)
    try:
        seed_idle_snapshot(pool, 1)
        with pool.acquire_upload(work_id="small:1") as runtime:
            assert runtime.telegram_user_id == 1
            assert not pool.try_reserve_idle(1, "segment:2")
    finally:
        pool.stop()


def test_reservation_blocks_normal_work_without_consuming_slot():
    pool = make_pool(upload_files=1)
    try:
        seed_idle_snapshot(pool, 1)
        assert pool.try_reserve_idle(1, "segment:2")
        assert pool.activity.snapshot(1).idle_snapshot is None
        with pool.acquire_upload(timeout=0, work_id="normal") as other:
            assert other.telegram_user_id == 2
        lease = pool.activate_reservation(1, "segment:2")
        assert lease.runtime.telegram_user_id == 1
        assert pool.activity.snapshot(1).active_byte_upload_jobs == 1
        assert pool.activity.snapshot(1).reserved_task_id is None
        first = lease.close()
        second = lease.close()
        assert first.changed
        assert not second.changed
        assert pool.activity.snapshot(1).active_byte_upload_jobs == 0
    finally:
        pool.stop()


def test_inflight_rpc_blocks_idle_reservation_even_with_snapshot():
    pool = make_pool()
    try:
        seed_idle_snapshot(pool, 1)
        token = UploadRpcToken("other", 1, 1, 0, 1)
        pool.activity.request_started(token)
        assert not pool.try_reserve_idle(1, "segment")
        pool.activity.request_settled(token)
    finally:
        pool.stop()


def test_missing_snapshot_cannot_be_failover_target():
    pool = make_pool()
    try:
        assert pool.activity.snapshot(1).idle
        assert pool.activity.snapshot(1).idle_snapshot is None
        assert not pool.try_reserve_idle(1, "segment")
    finally:
        pool.stop()


def test_offline_or_unlinked_account_cannot_be_reserved():
    pool = make_pool(linked={1})
    try:
        seed_idle_snapshot(pool, 2)
        assert not pool.try_reserve_idle(2, "segment")
        pool.runtime(1).online = False
        seed_idle_snapshot(pool, 1)
        assert not pool.try_reserve_idle(1, "segment")
    finally:
        pool.stop()


def test_two_threads_cannot_reserve_same_idle_account():
    pool = make_pool()
    try:
        seed_idle_snapshot(pool, 1)
        results = []

        def reserve(task_id):
            results.append(pool.try_reserve_idle(1, task_id))

        threads = [threading.Thread(target=reserve, args=(f"task:{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(results) == [False, True]
    finally:
        pool.stop()


def test_release_unused_reservation_does_not_consume_or_release_file_slot():
    pool = make_pool(upload_files=1)
    try:
        seed_idle_snapshot(pool, 1)
        assert pool.try_reserve_idle(1, "segment")
        change = pool.release_reservation(1, "segment")
        assert change.changed
        lease = pool.acquire_exact_upload_lease(1, "normal")
        assert lease is not None
        lease.close()
    finally:
        pool.stop()


def test_normal_work_invalidates_idle_snapshot_before_returning_runtime():
    pool = make_pool()
    try:
        seed_idle_snapshot(pool, 1)
        lease = pool.acquire_exact_upload_lease(1, "normal")
        assert lease is not None
        assert pool.activity.snapshot(1).idle_snapshot is None
        lease.close()
    finally:
        pool.stop()


def test_status_exposes_activity_without_session_paths():
    pool = make_pool()
    try:
        seed_idle_snapshot(pool, 1, bps=12)
        status = pool.status()
        account = status["accounts"][0]
        assert account["idle"] is True
        assert account["active_byte_upload_jobs"] == 0
        assert account["in_flight_upload_rpcs"] == 0
        assert account["reserved_task_id"] is None
        assert account["idle_speed_bytes_per_second"] == 12
        rendered = repr(status)
        assert "/outside/" not in rendered
        assert ".session" not in rendered
    finally:
        pool.stop()
