"""Streaming batch stages and durable staged-upload state.

Two layers are covered here. ``UploadEngine.transfer_batch`` is the streaming
half: hashing and check-hash run ahead of the upload stage under their own
bounded pools, and a finished transfer is handed to the caller the moment it
exists rather than at the end of the batch. ``UploadStager`` is the durable
half: it dispatches every due file through one batch, registers each result
concurrently, and only then deletes the staged source.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import gamestage
import upload_engine
import uploadstage
from telegram_accounts import TelegramAccountPool
from transfer_models import (
    AccountSpec,
    QueueStage,
    TransferRequest,
    TransferResult,
    UploadedPart,
)


class Gate:
    """A named barrier: a stage blocks until the test lets that name through."""

    def __init__(self):
        self.lock = threading.Lock()
        self.started: dict[str, threading.Event] = {}
        self.open: dict[str, threading.Event] = {}
        self.active: dict[str, int] = {}
        self.peak: dict[str, int] = {}
        self.all_open = False

    def _event(self, table, key):
        with self.lock:
            return table.setdefault(key, threading.Event())

    def enter(self, stage: str, name: str) -> None:
        with self.lock:
            self.active[stage] = self.active.get(stage, 0) + 1
            self.peak[stage] = max(self.peak.get(stage, 0), self.active[stage])
        self._event(self.started, f"{stage}:{name}").set()
        if not self.all_open:
            self._event(self.open, f"{stage}:{name}").wait(5)
        with self.lock:
            self.active[stage] -= 1

    def release(self, stage: str, *names: str) -> None:
        for name in names:
            self._event(self.open, f"{stage}:{name}").set()

    def release_all(self) -> None:
        """Open every gate, including ones no stage has reached yet."""
        with self.lock:
            self.all_open = True
            events = list(self.open.values())
        for event in events:
            event.set()

    def started_at(self, stage: str, name: str, timeout: float = 5.0) -> bool:
        return self._event(self.started, f"{stage}:{name}").wait(timeout)

    def blocked(self, stage: str, name: str) -> bool:
        """Started but not yet allowed through."""
        return (self.started_at(stage, name)
                and not self._event(self.open, f"{stage}:{name}").is_set())


class PipelineEngine(upload_engine.UploadEngine):
    """A real streaming engine whose only stub is the byte-moving stage."""

    def __init__(self, api, gate, **kwargs):
        super().__init__(api, pool=None, **kwargs)
        self.gate = gate
        self.uploaded: list[str] = []

    def _upload_fresh(self, request):
        self.gate.enter("upload", request.upload_name)
        self.uploaded.append(request.upload_name)
        return [UploadedPart(0, 500, "doc", None, request.logical_size, 7)]


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    gate = Gate()

    def sample_hash(path):
        gate.enter("hash", path.name)
        return f"fp-{path.name}"

    monkeypatch.setattr(gamestage, "sample_hash", sample_hash)

    def check_hash(fingerprint):
        gate.enter("check", fingerprint.removeprefix("fp-"))
        return {"found": False, "files": []}

    api = SimpleNamespace(check_hash=check_hash, register=lambda **_: None,
                          invalidate=lambda _: None)

    def request(name, size=4):
        path = tmp_path / name
        path.write_bytes(b"x" * size)
        return TransferRequest(path, name, "application/octet-stream", "parent", size)

    return SimpleNamespace(
        gate=gate, api=api, request=request,
        engine=lambda **kw: PipelineEngine(api, gate, **kw),
    )


# --------------------------------------------------------------------------- #
# Streaming stages
# --------------------------------------------------------------------------- #


def test_hash_check_upload_and_register_overlap(pipeline):
    """The batch must not be three serial passes over the whole input."""
    engine = pipeline.engine()
    names = ["first.bin", "second.bin", "third.bin"]
    gate = pipeline.gate
    gate.release("hash", *names)
    gate.release("check", *names)
    gate.release("upload", "first.bin")
    registering = threading.Event()

    with ThreadPoolExecutor(4) as registrations, ThreadPoolExecutor(1) as driver:
        def on_result(result):
            # Exactly what the stager does: hand the result to a pool and
            # return, so a slow registration never stalls the upload stage.
            registering.set()
            registrations.submit(gate.enter, "register", result.request.upload_name)

        batch = driver.submit(
            engine.transfer_batch, [pipeline.request(n) for n in names],
            None, on_result=on_result, lookahead=2,
        )
        try:
            assert registering.wait(5), "the first result never reached the caller"
            # While the first registration is held open, later files must have
            # moved on: the third has been fingerprinted and the second is
            # already pushing bytes.
            assert gate.started_at("hash", "third.bin")
            assert gate.blocked("upload", "second.bin")
        finally:
            gate.release_all()
        results = batch.result(timeout=5)
    assert [r.request.upload_name for r in results] == names


def test_hashing_is_capped_at_two_and_checks_at_eight(pipeline):
    engine = pipeline.engine(hash_concurrency=2, hash_check_concurrency=8)
    requests = [pipeline.request(f"f{i}.bin") for i in range(12)]
    gate = pipeline.gate

    def unblock():
        # Let every stage through only once all twelve are in flight, so the
        # observed peak is the pool's cap and not the test's pacing.
        for i in range(12):
            gate.started_at("hash", f"f{i}.bin", timeout=0.2)
        gate.release_all()

    thread = threading.Thread(target=unblock, daemon=True)
    thread.start()
    engine.transfer_batch(requests, lookahead=12)
    thread.join(timeout=5)
    assert gate.peak["hash"] <= 2
    assert gate.peak["check"] <= 8


def test_status_sink_sees_every_stage_in_order(pipeline):
    engine = pipeline.engine()
    pipeline.gate.release_all()
    seen = []
    engine.transfer_batch(
        [pipeline.request("one.bin")],
        lambda request, stage, detail="": seen.append((request.upload_name, stage)),
    )
    assert seen == [
        ("one.bin", QueueStage.PLANNING),
        ("one.bin", QueueStage.UPLOADING),
        ("one.bin", QueueStage.SENDING),
    ]


def test_a_failing_file_is_reported_alone_and_does_not_stop_the_batch(pipeline):
    engine = pipeline.engine()
    pipeline.gate.release_all()
    good = pipeline.request("good.bin")
    bad = pipeline.request("bad.bin")
    bad.source.unlink()
    failures = []

    def sink(request, stage, detail=""):
        if stage is QueueStage.FAILED:
            failures.append((request.upload_name, detail))

    with pytest.raises(Exception):
        engine.transfer_batch([bad, good], sink)
    assert [name for name, _ in failures] == ["bad.bin"]
    assert engine.uploaded == ["good.bin"]


def test_status_details_never_carry_a_session_or_bearer_token():
    redacted = upload_engine.redact(
        RuntimeError("session=1AaBbCc failed; " + "Authorization" + ": Bearer synthetic-token")
    )
    assert "1AaBbCc" not in redacted and "ey.J0.eXA" not in redacted
    assert "RuntimeError" in redacted


def test_three_file_slots_per_account_bound_concurrent_preparations():
    pool = TelegramAccountPool(
        [AccountSpec(1, Path("/sessions/1.session"))], api_id=1, api_hash="h", upload_files=3,
        worker_factory=lambda *_a, **_kw: SimpleNamespace(user_id=1, stop=lambda: None),
    )
    pool.runtime(1).online = pool.runtime(1).linked = True
    lock = threading.Lock()
    active = peak = 0
    release = threading.Event()

    def hold():
        nonlocal active, peak
        with pool.acquire_upload():
            with lock:
                active += 1
                peak = max(peak, active)
            release.wait(2)
            with lock:
                active -= 1

    with ThreadPoolExecutor(6) as executor:
        tasks = [executor.submit(hold) for _ in range(6)]
        time.sleep(0.2)
        assert peak == 3
        release.set()
        for task in tasks:
            task.result(timeout=3)


# --------------------------------------------------------------------------- #
# Durable staged state
# --------------------------------------------------------------------------- #


class FakeEngine:
    """Engine seam for the stager: per-request outcomes, no Telegram."""

    def __init__(self):
        self.fail_transfer: set[str] = set()
        self.fail_register: set[str] = set()
        self.block_register = threading.Event()
        self.block_register.set()
        self.registered: list[str] = []
        self.transferred: list[str] = []
        self.lock = threading.Lock()
        self.active = self.peak = 0

    def transfer_batch(self, requests, status_sink=None, *, on_result=None, lookahead=0):
        results, errors = [], []
        for request in requests:
            name = request.upload_name
            if status_sink:
                status_sink(request, QueueStage.UPLOADING, "")
            if name in self.fail_transfer:
                errors.append(RuntimeError("upload failed"))
                if status_sink:
                    status_sink(request, QueueStage.FAILED, "RuntimeError: upload failed")
                continue
            with self.lock:
                self.transferred.append(name)
            result = TransferResult(request, f"fp-{name}", (
                UploadedPart(0, 1, "doc", None, request.logical_size, 7),
            ))
            results.append(result)
            if status_sink:
                status_sink(request, QueueStage.SENDING, "")
            if on_result:
                on_result(result)
        if errors:
            raise errors[0]
        return results

    def register_result(self, result):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            self.block_register.wait(5)
            if result.request.upload_name in self.fail_register:
                raise RuntimeError("registration failed")
            with self.lock:
                self.registered.append(result.request.upload_name)
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def stager(tmp_path):
    cfg = SimpleNamespace(
        upload_dir=tmp_path / "uploads", cache_dir=tmp_path / "meta",
        debounce_minutes=0.0, register_concurrency=8, hash_concurrency=2,
    )
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    api = SimpleNamespace(resolve=lambda segments: None)
    engine = FakeEngine()
    built = uploadstage.UploadStager(cfg, api, engine)

    def stage(name, data=b"payload"):
        path = built.create_file([name], "parent")
        path.write_bytes(data)
        return path

    built.stage_file = stage
    built.engine = engine
    built.cfg = cfg
    return built


def test_only_the_affected_source_remains_after_a_registration_failure(stager):
    stager.engine.fail_register.add("bad.bin")
    stager.stage_file("good.bin")
    stager.stage_file("bad.bin")
    stager.process_due([("good.bin",), ("bad.bin",)])
    assert not stager.path_for(("good.bin",)).exists()
    assert stager.path_for(("bad.bin",)).exists()
    assert stager.status_for(("bad.bin",))["stage"] == "failed"
    assert stager.engine.registered == ["good.bin"]


def test_an_upload_failure_leaves_its_neighbour_untouched(stager):
    stager.engine.fail_transfer.add("bad.bin")
    stager.stage_file("good.bin")
    stager.stage_file("bad.bin")
    stager.process_due([("bad.bin",), ("good.bin",)])
    assert not stager.path_for(("good.bin",)).exists()
    assert stager.status_for(("bad.bin",))["stage"] == "failed"
    assert stager.status_for(("good.bin",)) is None


def test_fifth_failure_is_abandoned_and_the_source_is_retained(stager):
    stager.engine.fail_register.add("x.bin")
    stager.stage_file("x.bin")
    for _ in range(5):
        stager.get(("x.bin",)).retry_after = 0.0
        stager.get(("x.bin",)).stage = QueueStage.STAGING
        stager.process_due([("x.bin",)])
    status = stager.status_for(("x.bin",))
    assert (status["stage"], status["attempts"]) == ("abandoned", 5)
    assert stager.path_for(("x.bin",)).exists()
    assert ("x.bin",) not in stager._due(0.0)


def test_registrations_run_concurrently_up_to_the_configured_cap(stager):
    stager.engine.block_register.clear()
    for i in range(12):
        stager.stage_file(f"f{i}.bin")
    keys = [(f"f{i}.bin",) for i in range(12)]

    def unblock():
        for _ in range(50):
            time.sleep(0.02)
            if stager.engine.peak >= 8:
                break
        stager.engine.block_register.set()

    thread = threading.Thread(target=unblock, daemon=True)
    thread.start()
    stager.process_due(keys)
    thread.join(timeout=5)
    assert stager.engine.peak == 8
    assert sorted(stager.engine.registered) == sorted(f"f{i}.bin" for i in range(12))


def test_durable_state_survives_a_restart_and_readopts_every_source(stager):
    stager.engine.fail_register.add("kept.bin")
    stager.stage_file("kept.bin")
    stager.stage_file("quiet.bin")
    stager.process_due([("kept.bin",)])
    assert stager.status_for(("kept.bin",))["attempts"] == 1

    revived = uploadstage.UploadStager(stager.cfg, stager.api, stager.engine)
    assert revived.status_for(("kept.bin",))["stage"] == "failed"
    assert revived.status_for(("kept.bin",))["attempts"] == 1
    # Everything still on disk is adopted, including a file that never ran.
    assert revived.status_for(("quiet.bin",))["stage"] == "staging"


def test_state_is_written_atomically_and_dropped_once_the_source_is_gone(stager):
    state = stager.cfg.cache_dir / "upload-queue.json"
    stager.engine.fail_register.add("kept.bin")
    stager.stage_file("kept.bin")
    stager.stage_file("gone.bin")
    stager.process_due([("kept.bin",), ("gone.bin",)])
    body = json.loads(state.read_text("utf-8"))
    assert list(body["pending"]) == ["kept.bin"]
    assert not list(stager.cfg.cache_dir.glob("*.part"))


def test_a_batch_duplicate_uploads_once_and_registers_each_alias(stager):
    # Two staged names, one payload: the engine's fingerprint claim collapses
    # the upload while both destinations still get their own registration.
    seen = []

    def transfer_batch(requests, status_sink=None, *, on_result=None, lookahead=0):
        parts = (UploadedPart(0, 1, "doc", None, 7, 7),)
        results = []
        for request in requests:
            seen.append(request.upload_name)
            result = TransferResult(request, "shared", parts)
            results.append(result)
            if status_sink:
                status_sink(request, QueueStage.SENDING, "")
            if on_result:
                on_result(result)
        return results

    stager.engine.transfer_batch = transfer_batch
    stager.stage_file("a.bin", b"payload")
    stager.stage_file("b.bin", b"payload")
    stager.process_due([("a.bin",), ("b.bin",)])
    assert sorted(stager.engine.registered) == ["a.bin", "b.bin"]
    assert not stager.path_for(("a.bin",)).exists()
    assert not stager.path_for(("b.bin",)).exists()


def test_status_reports_stage_attempts_and_a_redacted_error(stager):
    stager.engine.fail_register.add("x.bin")
    stager.stage_file("x.bin")
    stager.process_due([("x.bin",)])
    entry = next(p for p in stager.status()["pending"] if p["path"] == "x.bin")
    assert entry["stage"] == "failed"
    assert entry["attempts"] == 1
    assert "registration failed" in entry["detail"]
    assert "session" not in json.dumps(stager.status()).lower()
