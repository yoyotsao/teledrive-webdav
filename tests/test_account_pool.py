from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from config import ConfigError
from tgio import TelegramWorker
from transfer_models import AccountSpec


def write_accounts(tmp_path, rows):
    path = tmp_path / "accounts.json"
    path.write_text(
        json.dumps(
            {
                "accounts": [
                    {"telegram_user_id": user_id, "label": label, "session": session}
                    for user_id, label, session in rows
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


class FakeWorker:
    def __init__(self, user_id, *, failure=None):
        self.user_id = user_id
        self.failure = failure
        self.started = 0
        self.stopped = 0
        self.dms = []

    def start(self):
        self.started += 1
        if self.failure is not None:
            raise self.failure

    def stop(self):
        self.stopped += 1

    def send_dm(self, username, text):
        self.dms.append((username, text))


class WorkerFactory:
    def __init__(self, workers):
        self.workers = workers
        self.calls = []

    def __call__(self, api_id, api_hash, session, connections, *, upload_parts):
        self.calls.append((api_id, api_hash, session, connections, upload_parts))
        return self.workers[session]


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


def make_pool(specs, workers, *, linked, upload_files=3):
    from telegram_accounts import TelegramAccountPool

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
    pool, _ = make_pool(
        [AccountSpec(1, "primary", "primary-secret")],
        {"primary-secret": FakeWorker(1)},
        linked={1},
    )
    try:
        yield pool
    finally:
        pool.stop()


def test_zero_routes_to_primary(pool):
    assert pool.for_read(0) is pool.primary


def test_nonzero_never_falls_back(pool):
    from telegram_accounts import AccountUnavailableError

    with pytest.raises(AccountUnavailableError, match="99"):
        pool.for_read(99)


def test_duplicate_configured_ids_are_rejected(tmp_path):
    from telegram_accounts import load_account_specs

    path = write_accounts(tmp_path, [(7, "a", "s1"), (7, "b", "s2")])
    with pytest.raises(ConfigError, match="duplicate.*7"):
        load_account_specs(path)


def test_round_robin_skips_a_busy_account():
    specs = [AccountSpec(1, "one", "s1"), AccountSpec(2, "two", "s2")]
    pool, _ = make_pool(specs, {"s1": FakeWorker(1), "s2": FakeWorker(2)}, linked={1, 2})
    try:
        for _ in range(3):
            assert pool.runtime(1).file_slots.acquire(blocking=False)
        with pool.acquire_upload(timeout=0.1) as runtime:
            assert runtime.telegram_user_id == 2
    finally:
        pool.stop()


def test_upload_lease_returns_its_file_slot_on_context_exit():
    specs = [AccountSpec(1, "primary", "s1")]
    pool, _ = make_pool(
        specs,
        {"s1": FakeWorker(1)},
        linked={1},
        upload_files=1,
    )
    try:
        with pool.acquire_upload(timeout=0.1) as runtime:
            assert runtime.telegram_user_id == 1
        with pool.acquire_upload(timeout=0.1) as runtime:
            assert runtime.telegram_user_id == 1
    finally:
        pool.stop()


def test_session_user_id_mismatch_disables_only_that_account():
    specs = [AccountSpec(1, "primary", "secret-1"), AccountSpec(2, "wrong-id", "secret-2")]
    bad = FakeWorker(22)
    pool, api = make_pool(specs, {"secret-1": FakeWorker(1), "secret-2": bad}, linked={1, 2})
    try:
        assert pool.eligible_upload_ids == (1,)
        with pytest.raises(Exception, match="2.*wrong-id"):
            pool.for_read(2)
        rendered = json.dumps(pool.status())
        assert "secret-1" not in rendered and "secret-2" not in rendered
        assert "expected 2" in rendered and "got 22" in rendered
        assert bad.stopped == 1
        assert api.logins == 1
    finally:
        pool.stop()


def test_primary_auth_failure_stops_workers_and_redacts_primary_session():
    from telegram_accounts import AccountUnavailableError, TelegramAccountPool

    specs = [
        AccountSpec(1, "primary", "primary-secret"),
        AccountSpec(2, "secondary", "secondary-secret"),
    ]
    primary = FakeWorker(1)
    secondary = FakeWorker(2)
    pool = TelegramAccountPool(
        specs,
        api_id=123,
        api_hash="hash",
        worker_factory=WorkerFactory({
            "primary-secret": primary,
            "secondary-secret": secondary,
        }),
    )
    api = FakeApi({1, 2})
    api.login = lambda: (_ for _ in ()).throw(RuntimeError("backend rejected primary-secret"))

    with pytest.raises(AccountUnavailableError) as raised:
        pool.start(api)

    message = str(raised.value)
    assert "account 1 (primary)" in message
    assert "primary-secret" not in message
    assert primary.stopped == secondary.stopped == 1


def test_one_account_startup_failure_is_isolated():
    specs = [
        AccountSpec(1, "primary", "s1"),
        AccountSpec(2, "broken", "do-not-print"),
        AccountSpec(3, "healthy", "s3"),
    ]
    broken = FakeWorker(2, failure=RuntimeError("failed with do-not-print"))
    pool, _ = make_pool(
        specs,
        {"s1": FakeWorker(1), "do-not-print": broken, "s3": FakeWorker(3)},
        linked={1, 2, 3},
    )
    try:
        assert pool.eligible_upload_ids == (1, 3)
        assert pool.for_read(3).online
        rendered = json.dumps(pool.status())
        assert "broken" in rendered and "do-not-print" not in rendered
        assert broken.stopped == 1
    finally:
        pool.stop()


def test_unlinked_account_is_readable_but_not_upload_eligible():
    specs = [AccountSpec(1, "primary", "s1"), AccountSpec(2, "historical", "s2")]
    pool, api = make_pool(specs, {"s1": FakeWorker(1), "s2": FakeWorker(2)}, linked={1})
    try:
        assert pool.eligible_upload_ids == (1,)
        assert pool.for_read(2).telegram_user_id == 2
        assert api.logins == 1 and api.account_lookups == 1
        assert api.dm_sender.__self__ is pool.primary.worker
        assert pool.primary.worker.dms == [("verify_bot", "nonce")]
        assert pool.runtime(2).worker.dms == []
    finally:
        pool.stop()


def test_from_config_keeps_legacy_single_session_mode(tmp_path):
    from telegram_accounts import TelegramAccountPool

    cfg = SimpleNamespace(
        accounts_file=None,
        session="legacy-secret",
        api_id=123,
        api_hash="hash",
        download_connections=8,
        upload_files=3,
        upload_parts=12,
    )
    factory = WorkerFactory({"legacy-secret": FakeWorker(42)})
    pool = TelegramAccountPool.from_config(cfg, worker_factory=factory)
    api = FakeApi({42})
    pool.start(api)
    try:
        assert pool.primary.telegram_user_id == 42
        assert pool.for_read(42) is pool.primary
        assert pool.eligible_upload_ids == (42,)
    finally:
        pool.stop()


def test_runtime_limiters_are_independent_and_injectable():
    from telegram_accounts import TelegramAccountPool

    specs = [AccountSpec(1, "one", "s1"), AccountSpec(2, "two", "s2")]
    workers = {"s1": FakeWorker(1), "s2": FakeWorker(2)}
    pool = TelegramAccountPool(
        specs,
        api_id=1,
        api_hash="hash",
        worker_factory=WorkerFactory(workers),
        chunk_limiter_factory=object,
        message_limiter_factory=object,
    )

    assert pool.runtime(1).chunk_limiter is not pool.runtime(2).chunk_limiter
    assert pool.runtime(1).message_limiter is not pool.runtime(2).message_limiter


def test_worker_can_start_again_after_connection_failure():
    class RetryWorker(TelegramWorker):
        def __init__(self):
            super().__init__(1, "hash", "secret")
            self.connect_attempts = 0

        async def _connect(self):
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
