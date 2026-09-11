from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from telegram_accounts import TelegramAccountPool
from transfer_models import AccountSpec, AttemptLease, TransferRequest
from tgupload import AttemptRevoked, ProtocolDecision
from upload_activity import AccountActivityRegistry, UploadSpeedTracker
from upload_engine import UploadEngine


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self.value

    def advance(self, seconds):
        with self._lock:
            self.value += seconds


class FakeRevokeHandle:
    def __init__(self):
        self.worker_event = threading.Event()

    def revoke(self):
        self.worker_event.set()


class FakeWorker:
    def __init__(self, _api_id, _api_hash, expected_user_id, _session_path, _connections, *, upload_parts=12, rig=None):
        self.user_id = expected_user_id
        self.rig = rig
        self.slow = expected_user_id == 1
        self.revoked = False
        self.started_parts = []
        self.handles = []
        self.messages = []
        self._calls = 0

    def start(self):
        return None

    def stop(self):
        return None

    def send_dm(self, *args, **kwargs):
        return None

    def create_revoke_handle(self):
        handle = FakeRevokeHandle()
        self.rig.handles[self.user_id] = handle
        return handle

    def prepare_segment(self, reader, size, name, *, force_big=False, observer=None, revoke_handle=None, rpc_timeout=120.0):
        assert force_big is True
        self._calls += 1
        self.started_parts.append(0)
        if self.slow and self._calls == 1:
            token = observer.request_started(0, 100)
            observer.request_succeeded(token, 100)
            observer.request_settled(token)
            observer.premium_flood(60, SimpleNamespace(mode="frozen", rate=3.0, penalty_until=60.0))
            self.rig.clock.advance(30)
            if not revoke_handle.worker_event.wait(timeout=2):
                raise AssertionError("slow attempt was not revoked")
            self.revoked = True
            raise AttemptRevoked()

        token = observer.request_started(0, size)
        observer.request_succeeded(token, size)
        observer.request_settled(token)
        handle = f"file-{self.user_id}-{self._calls}"
        self.handles.append(handle)
        return handle

    def prepare_thumbnail(self, preview):
        return None

    def send_uploaded_segment(self, handle, size, name, *, preview, mime_type, message_limiter):
        self.messages.append(handle)
        return {
            "message_id": 1000 + self.user_id,
            "file_id": f"document-{self.user_id}-{len(self.messages)}",
            "access_hash": None,
            "size": size,
        }


class Rig:
    def __init__(self, tmp_path: Path):
        self.clock = FakeClock()
        self.handles = {}
        self.workers = {}
        speed = UploadSpeedTracker(clock=self.clock)
        activity = AccountActivityRegistry(clock=self.clock, speed_tracker=speed)

        def factory(*args, **kwargs):
            worker = FakeWorker(*args, **kwargs, rig=self)
            self.workers[worker.user_id] = worker
            return worker

        specs = [
            AccountSpec(1, tmp_path / "1.session"),
            AccountSpec(2, tmp_path / "2.session"),
        ]
        for spec in specs:
            spec.session_path.write_bytes(b"sqlite")
        self.pool = TelegramAccountPool(
            specs,
            api_id=1,
            api_hash="hash",
            download_connections=1,
            upload_files=1,
            worker_factory=factory,
            speed_tracker=speed,
            activity_registry=activity,
        )
        for runtime in self.pool._runtimes:
            runtime.online = True
            runtime.linked = True

        # Give account 2 a valid idle effective-speed snapshot before the job.
        activity.begin_job(2, "seed")
        speed.record_effective(AttemptLease("seed", 1, 2), 0, 3000)
        activity.end_job(2, "seed")
        self.engine = UploadEngine(None, self.pool, segment_concurrency=2, scheduler_clock=self.clock)


def test_idle_account_restarts_premium_flooded_segment_from_part_zero(tmp_path):
    rig = Rig(tmp_path)
    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * 1000)
    request = TransferRequest(source, "big.bin", "application/octet-stream", None, 1000)
    decision = ProtocolDecision("big", ((0, 1000),), True)

    parts = rig.engine._upload_big_with_scheduler(request, decision, preview=None)

    assert len(parts) == 1
    assert parts[0].telegram_user_id == 2
    assert rig.workers[1].revoked
    assert rig.workers[1].started_parts == [0]
    assert rig.workers[1].messages == []
    assert rig.workers[2].started_parts == [0]
    assert rig.workers[2].messages == ["file-2-1"]
    assert rig.pool.activity.snapshot(1).active_byte_upload_jobs == 0
    assert rig.pool.activity.snapshot(2).active_byte_upload_jobs == 0


def test_executor_exception_cannot_leave_task_active_forever(tmp_path):
    rig = Rig(tmp_path)
    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * 1000)
    request = TransferRequest(source, "big.bin", "application/octet-stream", None, 1000)
    decision = ProtocolDecision("big", ((0, 1000),), True)
    rig.workers[1].slow = False

    def broken(*args, **kwargs):
        raise AssertionError("synthetic executor defect")

    rig.engine._execute_scheduler_action = broken
    try:
        rig.engine._upload_big_with_scheduler(request, decision, preview=None)
    except Exception as exc:
        assert type(exc).__name__ == "SchedulerExecutionError"
    else:
        raise AssertionError("scheduler failure was not surfaced")
