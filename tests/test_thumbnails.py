"""Thumbnail fetching: cross-DC documents and the in-flight cap.

Offline. The fake clients here stand in for pooled Telethon clients; nothing
touches the network.
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tgio  # noqa: E402


class _Thumb:
    def __init__(self, type_="m", size=17_000):
        self.type = type_
        self.size = size


class _Doc:
    def __init__(self, dc_id=1):
        self.id = 12345
        self.access_hash = 999
        self.file_reference = b"ref"
        self.dc_id = dc_id
        self.size = 4_000_000
        self.thumbs = [_Thumb()]


class _Session:
    def __init__(self, dc_id):
        self.dc_id = dc_id


class _FakeClient:
    """A pooled client whose own DC is *not* where the file lives.

    A bare ``client(GetFileRequest(...))`` in that situation is answered with
    FILE_MIGRATE by Telegram, so calling this object directly is a test failure:
    the thumbnail path has to go through Telethon's DC-aware download instead.
    """

    def __init__(self, dc_id=2, payload=b"\xff\xd8jpeg"):
        self.session = _Session(dc_id)
        self.payload = payload
        self.downloads = []

    async def __call__(self, request):
        raise AssertionError("raw GetFileRequest cannot follow a FILE_MIGRATE")

    def iter_download(self, file, **kwargs):
        self.downloads.append((file, kwargs))
        payload = self.payload

        async def gen():
            yield payload

        return gen()


def _worker():
    worker = tgio.TelegramWorker(1, "hash", "session")
    return worker


def test_thumbnail_uses_dc_aware_download():
    worker = _worker()
    client = _FakeClient(dc_id=2)
    worker._pool = [client]
    doc = _Doc(dc_id=1)

    data = asyncio.run(worker._thumbnail_bytes(doc))

    assert data == client.payload
    assert len(client.downloads) == 1
    location, kwargs = client.downloads[0]
    # The document's own DC must be handed over, or Telethon downloads from the
    # session's DC and gets FILE_MIGRATE back.
    assert kwargs["dc_id"] == 1
    assert location.thumb_size == "m"
    assert location.id == doc.id


def test_thumbnail_batch_caps_requests_in_flight():
    """A folder prefetch is 100 ids; firing 100 GetFiles at once earns FLOOD_WAIT."""
    worker = _worker()
    ids = list(range(1, 101))
    now = time.monotonic()
    worker._docs = {i: (_Doc(), now) for i in ids}

    state = {"live": 0, "peak": 0}

    async def fake_bytes(doc):
        state["live"] += 1
        state["peak"] = max(state["peak"], state["live"])
        for _ in range(3):
            await asyncio.sleep(0)
        state["live"] -= 1
        return b"jpeg"

    worker._thumbnail_bytes = fake_bytes

    out = asyncio.run(worker._thumbnails(ids))

    assert len(out) == len(ids)
    assert state["peak"] <= tgio.THUMB_CONCURRENCY
    assert state["peak"] > 1  # still concurrent, just bounded


def _failing_once(exc):
    """A client whose first iter_download raises ``exc``, then succeeds."""

    class _Failing(_FakeClient):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def iter_download(self, file, **kwargs):
            self.attempts += 1
            attempts = self.attempts
            payload = self.payload

            async def gen():
                if attempts == 1:
                    raise exc
                yield payload

            return gen()

    return _Failing()


def test_thumbnail_retries_a_short_flood_wait():
    from telethon.errors import FloodWaitError

    worker = _worker()
    client = _failing_once(FloodWaitError(request=None, capture=2))
    worker._pool = [client]

    async def scenario():
        sleeps = []

        async def no_sleep(seconds):
            sleeps.append(seconds)

        real_sleep = asyncio.sleep
        asyncio.sleep = no_sleep
        try:
            return await worker._thumbnail_bytes(_Doc()), sleeps
        finally:
            asyncio.sleep = real_sleep

    data, sleeps = asyncio.run(scenario())
    assert data == client.payload
    assert client.attempts == 2
    assert sleeps  # it waited before retrying


def test_thumbnail_retries_a_rejected_exported_auth():
    """AUTH_BYTES_INVALID: eight clients exporting for the same DC at once."""
    from telethon.errors import AuthBytesInvalidError

    worker = _worker()
    client = _failing_once(AuthBytesInvalidError(request=None))
    worker._pool = [client]

    async def scenario():
        real_sleep = asyncio.sleep
        asyncio.sleep = lambda seconds: real_sleep(0)
        try:
            return await worker._thumbnail_bytes(_Doc())
        finally:
            asyncio.sleep = real_sleep

    assert asyncio.run(scenario()) == client.payload
    assert client.attempts == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
