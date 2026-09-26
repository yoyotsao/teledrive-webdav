import pytest

from telegram_accounts import (
    AccountUnavailableError,
    ChannelAccess,
    TelegramAccountPool,
)
from tgio import RemoteIdentityError, validate_canonical_media
from transfer_models import FileLocation


class FakeWorker:
    def __init__(self, access=None, error=None):
        self.access = access
        self.error = error

    def resolve_channel_access(self, channel_id):
        if self.error:
            raise self.error
        return self.access


class FakeRuntime:
    def __init__(self, user_id, worker, *, online=True, linked=True):
        self.telegram_user_id = user_id
        self.worker = worker
        self.online = online
        self.linked = linked
        self.error = None


def pool_with(*runtimes):
    pool = object.__new__(TelegramAccountPool)
    pool._runtimes = list(runtimes)
    pool._by_id = {r.telegram_user_id: r for r in runtimes}
    return pool


def location(**overrides):
    values = dict(
        telegram_chat_id="-100123",
        telegram_user_id=1,
        telegram_message_id=77,
        media_kind="document",
        media_id="9001",
        media_size=12,
        photo_variant=None,
        location_version=4,
    )
    values.update(overrides)
    return FileLocation(**values)


def test_saved_messages_read_uses_exact_storage_account_and_me_peer():
    first = FakeRuntime(1, FakeWorker())
    second = FakeRuntime(2, FakeWorker())
    pool = pool_with(first, second)

    routes = pool.read_routes(location(telegram_chat_id=None, telegram_user_id=2))

    assert routes == ((second, "me"),)


def test_channel_read_routes_resolve_independently_and_fail_over():
    bad = FakeRuntime(1, FakeWorker(error=RuntimeError("no peer")))
    access = ChannelAccess("-100123", object(), can_read=True, can_write=False, session_generation=3)
    good = FakeRuntime(2, FakeWorker(access=access))
    pool = pool_with(bad, good)

    routes = pool.read_routes(location())

    assert len(routes) == 1
    assert routes[0][0] is good
    assert routes[0][1] is access.peer


def test_channel_read_raises_routing_error_when_no_route_yields():
    pool = pool_with(FakeRuntime(1, FakeWorker(error=RuntimeError("gone"))))
    with pytest.raises(AccountUnavailableError):
        pool.read_routes(location())


def test_channel_writers_require_backend_link_and_current_write_access():
    read_only = FakeRuntime(
        1, FakeWorker(access=ChannelAccess("-100123", object(), can_read=True, can_write=False))
    )
    writer_access = ChannelAccess("-100123", object(), can_read=True, can_write=True)
    writer = FakeRuntime(2, FakeWorker(access=writer_access))
    unlinked = FakeRuntime(
        3, FakeWorker(access=ChannelAccess("-100123", object(), can_read=True, can_write=True)), linked=False
    )
    pool = pool_with(read_only, writer, unlinked)

    writers = pool.channel_writers("-100123", {1, 2, 3})

    assert [(runtime.telegram_user_id, access.can_write) for runtime, access in writers] == [(2, True)]


def test_canonical_media_mismatch_is_global_identity_failure():
    media = type("Document", (), {"id": 9002, "size": 12})()
    with pytest.raises(RemoteIdentityError):
        validate_canonical_media(media, location())
