from __future__ import annotations

import json
import traceback
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import ConfigError
from telegram_accounts import AccountUnavailableError, TelegramAccountPool
from tgio import TelegramWorker
from transfer_models import AccountSpec


class FakeWorker:
    def __init__(self, user_id, *, failure=None):
        self.user_id = user_id
        self.failure = failure
        self.started = 0
        self.stopped = 0
        self.dms = []
        self.bound_limiter = None

    def start(self):
        self.started += 1
        if self.failure is not None:
            raise self.failure

    def stop(self):
        self.stopped += 1

    def send_dm(self, username, text):
        self.dms.append((username, text))

    def set_upload_limiter(self, limiter):
        self.bound_limiter = limiter


class WorkerFactory:
    def __init__(self, workers):
        self.workers = workers
        self.calls = []

    def __call__(
        self, api_id, api_hash, expected_user_id, session_path, connections, *, upload_parts
    ):
        path = Path(session_path)
        self.calls.append(
            (api_id, api_hash, expected_user_id, path, connections, upload_parts)
        )
        return self.workers[expected_user_id]


class FakeApi:
    def __init__(self, linked):
        self.linked = set(linked)
        self.dm_sender = None
        self.logins = 0
        self.account_lookups = 0

    def set_dm_sender(self, sender):
        self.dm_sender = sender

    def login(self):
        self.logins += 1
        self.dm_sender("verify_bot", "nonce")
        return "jwt"

    def linked_account_ids(self):
        self.account_lookups += 1
        return set(self.linked)


def spec(user_id: int) -> AccountSpec:
    return AccountSpec(user_id, Path(f"/outside/sessions/{user_id}.session"))


def make_pool(specs, workers, *, linked, upload_files=3):
    pool = TelegramAccountPool(
        specs,
        api_id=123,
        api_hash="hash",
        download_connections=4,
        upload_files=upload_files,
        upload_parts=12,
        worker_factory=WorkerFactory(workers),
    )
    api = FakeApi(linked)
    pool.start(api)
    return pool, api


@pytest.fixture
def pool():
    pool, _ = make_pool([spec(1)], {1: FakeWorker(1)}, linked={1})
    try:
        yield pool
    finally:
        pool.stop()


def test_zero_routes_to_primary(pool):
    assert pool.for_read(0) is pool.primary


def test_nonzero_never_falls_back(pool):
    with pytest.raises(AccountUnavailableError, match="99"):
        pool.for_read(99)


def test_duplicate_configured_ids_are_rejected():
    with pytest.raises(ConfigError, match="duplicate.*7"):
        TelegramAccountPool(
            [spec(7), AccountSpec(7, Path("/other/7.session"))],
            api_id=1,
            api_hash="hash",
            worker_factory=WorkerFactory({7: FakeWorker(7)}),
        )


def test_round_robin_skips_a_busy_account():
    pool, _ = make_pool(
        [spec(1), spec(2)], {1: FakeWorker(1), 2: FakeWorker(2)}, linked={1, 2}
    )
    try:
        for _ in range(3):
            assert pool.runtime(1).file_slots.acquire(blocking=False)
        with pool.acquire_upload(timeout=0.1) as runtime:
            assert runtime.telegram_user_id == 2
    finally:
        pool.stop()


def test_upload_lease_returns_its_file_slot_on_context_exit():
    pool, _ = make_pool([spec(1)], {1: FakeWorker(1)}, linked={1}, upload_files=1)
    try:
        with pool.acquire_upload(timeout=0.1) as runtime:
            assert runtime.telegram_user_id == 1
        with pool.acquire_upload(timeout=0.1) as runtime:
            assert runtime.telegram_user_id == 1
    finally:
        pool.stop()


def test_session_user_id_mismatch_disables_only_that_account():
    bad = FakeWorker(22)
    pool, api = make_pool(
        [spec(1), spec(2)], {1: FakeWorker(1), 2: bad}, linked={1, 2}
    )
    try:
        assert pool.eligible_upload_ids == (1,)
        with pytest.raises(AccountUnavailableError, match="account 2"):
            pool.for_read(2)
        rendered = json.dumps(pool.status())
        assert "/outside/sessions" not in rendered
        assert "expected 2" in rendered and "got 22" in rendered
        assert bad.stopped == 1
        assert api.logins == 1
    finally:
        pool.stop()


def test_primary_auth_failure_stops_workers_and_never_renders_session_path():
    primary = FakeWorker(1)
    secondary = FakeWorker(2)
    pool = TelegramAccountPool(
        [spec(1), spec(2)],
        api_id=123,
        api_hash="hash",
        worker_factory=WorkerFactory({1: primary, 2: secondary}),
    )
    api = FakeApi({1, 2})
    api.login = lambda: (_ for _ in ()).throw(
        RuntimeError("backend rejected /outside/sessions/1.session")
    )

    with pytest.raises(AccountUnavailableError) as raised:
        pool.start(api)

    message = str(raised.value)
    rendered_traceback = "".join(traceback.format_exception(raised.value))
    assert "account 1" in message
    assert "/outside/sessions" not in message + rendered_traceback
    assert primary.stopped == secondary.stopped == 1


def test_one_account_startup_failure_is_isolated():
    broken = FakeWorker(2, failure=RuntimeError("failed at /outside/sessions/2.session"))
    pool, _ = make_pool(
        [spec(1), spec(2), spec(3)],
        {1: FakeWorker(1), 2: broken, 3: FakeWorker(3)},
        linked={1, 2, 3},
    )
    try:
        assert pool.eligible_upload_ids == (1, 3)
        assert pool.for_read(3).online
        rendered = json.dumps(pool.status())
        assert "/outside/sessions" not in rendered
        assert broken.stopped == 1
    finally:
        pool.stop()


def test_unlinked_account_is_readable_but_not_upload_eligible():
    pool, api = make_pool(
        [spec(1), spec(2)], {1: FakeWorker(1), 2: FakeWorker(2)}, linked={1}
    )
    try:
        assert pool.eligible_upload_ids == (1,)
        assert pool.for_read(2).telegram_user_id == 2
        assert api.logins == 1 and api.account_lookups == 1
        assert api.dm_sender.__self__ is pool.primary.worker
        assert pool.primary.worker.dms == [("verify_bot", "nonce")]
        assert pool.runtime(2).worker.dms == []
    finally:
        pool.stop()


def test_pool_uses_configured_primary_and_discovered_numeric_order(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    for user_id in (20, 3, 10):
        (session_dir / f"{user_id}.session").write_bytes(b"sqlite")
    cfg = SimpleNamespace(
        primary_user_id=20,
        session_dir=session_dir,
        api_id=123,
        api_hash="hash",
        download_connections=8,
        upload_files=3,
        upload_parts=12,
        cache_dir=tmp_path / "meta",
    )
    workers = {20: FakeWorker(20), 3: FakeWorker(3), 10: FakeWorker(10)}
    factory = WorkerFactory(workers)
    pool = TelegramAccountPool.from_config(cfg, worker_factory=factory)
    pool.start(FakeApi({20, 3, 10}))
    assert pool.primary.spec.telegram_user_id == 20
    assert pool.for_read(0) is pool.primary
    assert [runtime.spec.telegram_user_id for runtime in pool._runtimes] == [20, 3, 10]
    assert [call[2] for call in factory.calls] == [20, 3, 10]
    assert [call[3] for call in factory.calls] == [
        (session_dir / "20.session").resolve(),
        (session_dir / "3.session").resolve(),
        (session_dir / "10.session").resolve(),
    ]


def test_runtime_limiters_are_independent_and_injectable():
    workers = {1: FakeWorker(1), 2: FakeWorker(2)}
    pool = TelegramAccountPool(
        [spec(1), spec(2)],
        api_id=1,
        api_hash="hash",
        worker_factory=WorkerFactory(workers),
        chunk_limiter_factory=object,
        message_limiter_factory=object,
    )
    assert pool.runtime(1).chunk_limiter is not pool.runtime(2).chunk_limiter
    assert pool.runtime(1).message_limiter is not pool.runtime(2).message_limiter


def test_runtime_binds_its_injected_chunk_limiter_to_its_worker():
    workers = {1: FakeWorker(1), 2: FakeWorker(2)}
    pool = TelegramAccountPool(
        [spec(1), spec(2)],
        api_id=1,
        api_hash="hash",
        worker_factory=WorkerFactory(workers),
        chunk_limiter_factory=object,
    )
    assert workers[1].bound_limiter is pool.runtime(1).chunk_limiter
    assert workers[2].bound_limiter is pool.runtime(2).chunk_limiter


def test_from_config_builds_and_binds_one_persisted_limiter_per_account(tmp_path):
    from upload_limiter import AdaptiveUploadLimiter

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / "42.session").write_bytes(b"sqlite")
    cfg = SimpleNamespace(
        primary_user_id=42,
        session_dir=session_dir,
        api_id=123,
        api_hash="hash",
        download_connections=8,
        upload_files=3,
        upload_parts=99,
        cache_dir=tmp_path / "meta",
    )
    worker = FakeWorker(42)
    pool = TelegramAccountPool.from_config(
        cfg, worker_factory=WorkerFactory({42: worker})
    )
    limiter = pool.runtime(42).chunk_limiter
    assert isinstance(limiter, AdaptiveUploadLimiter)
    assert limiter.account_id == 42
    assert limiter.snapshot().window == 12
    assert worker.bound_limiter is limiter


def test_status_has_primary_flag_but_no_label_or_path():
    pool, _ = make_pool(
        [spec(1), spec(2)], {1: FakeWorker(1), 2: FakeWorker(2)}, linked={1, 2}
    )
    try:
        status = pool.status()
        assert status["accounts"][0]["primary"] is True
        assert status["accounts"][1]["primary"] is False
        rendered = json.dumps(status)
        assert "label" not in rendered
        assert ".session" not in rendered
        assert "/outside/sessions" not in rendered
    finally:
        pool.stop()


def test_worker_can_start_again_after_connection_failure():
    class RetryWorker(TelegramWorker):
        def __init__(self):
            super().__init__(1, "hash", 7, Path("unused.session"))
            self.connect_attempts = 0

        async def _connect_and_validate_control(self):
            self.connect_attempts += 1
            if self.connect_attempts == 1:
                raise RuntimeError("connection failed")
            self._me = SimpleNamespace(id=7)

        async def _disconnect_all(self):
            pass

    worker = RetryWorker()
    try:
        with pytest.raises(RuntimeError, match="connection failed"):
            worker.start()
        worker.start()
        assert worker.user_id == 7
    finally:
        worker.stop()
