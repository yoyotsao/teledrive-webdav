"""How a read is spread over the pool, and who answers it.

Offline. The fake clients here stand in for pooled Telethon clients; the parts
that matter are which connection each request lands on and what happens to the
exported sender that answered it.

Two things are being pinned down:

* **Depth per connection.** MTProto multiplexes, so a connection waiting for a
  reply can already carry the next request. One request each leaves every
  connection idle for a round trip between chunks, which is most of the link on
  a video.
* **Who answers.** Every file here lives in a DC that is not the session's, so
  every read is served by a borrowed exported sender. Telethon drops those 60s
  after the last one is returned and then rebuilds all eight at once, which
  Telegram answers by closing connections — and a closed connection mid-preview
  is a whole-file read, not a slow thumbnail (see tgio._pin_exported_sender).
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tgio  # noqa: E402


class _Doc:
    def __init__(self, dc_id=1, size=None):
        self.id = 4242
        self.access_hash = 7
        self.file_reference = b"ref"
        self.dc_id = dc_id
        self.size = size if size is not None else tgio.STREAM_BLOCK_SIZE * 4
        self.thumbs = [_Thumb()]


class _Thumb:
    def __init__(self, type_="m", size=17_000):
        self.type = type_
        self.size = size


class _Session:
    def __init__(self, dc_id):
        self.dc_id = dc_id


class _Download:
    """What ``iter_download`` hands back: an iterator with its own cleanup.

    Telethon's is a ``RequestIter`` whose ``close()`` is where a borrowed sender
    goes back. Breaking out of ``async for`` never reaches it, so a reader that
    takes one chunk and returns has to close it by hand.
    """

    def __init__(self, payload, live):
        self._payload = payload
        self._live = live
        self.closed = False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        self._live.enter()
        try:
            for _ in range(3):  # let every sibling request start
                await asyncio.sleep(0)
            yield self._payload
        finally:
            self._live.leave()

    async def close(self):
        self.closed = True


class _Live:
    def __init__(self):
        self.now = 0
        self.peak = 0

    def enter(self):
        self.now += 1
        self.peak = max(self.peak, self.now)

    def leave(self):
        self.now -= 1


class _FakeClient:
    """A pooled client whose own DC is not where the file lives."""

    def __init__(self, dc_id=4, live=None):
        self.session = _Session(dc_id)
        self.downloads = []
        self.borrowed = []
        self._live = live or _Live()

    async def __call__(self, request):
        raise AssertionError("a raw request cannot follow a FILE_MIGRATE")

    def iter_download(self, file, **kwargs):
        pull = _Download(b"\0" * tgio.REQUEST_SIZE, self._live)
        self.downloads.append((kwargs, pull))
        return pull

    async def _borrow_exported_sender(self, dc_id):
        self.borrowed.append(dc_id)
        return object()


def _worker(pool):
    worker = tgio.TelegramWorker(1, "hash", "session")
    worker._pool = pool
    return worker


# --------------------------------------------------------------------------- #
# depth per connection
# --------------------------------------------------------------------------- #


def test_a_streamed_read_puts_two_requests_on_every_connection():
    live = _Live()
    pool = [_FakeClient(live=live) for _ in range(tgio.DOWNLOAD_CONNECTIONS)]
    worker = _worker(pool)

    data = asyncio.run(worker._read(_Doc(), 0, tgio.STREAM_BLOCK_SIZE))

    assert len(data) == tgio.STREAM_BLOCK_SIZE
    per_client = [len(c.downloads) for c in pool]
    assert per_client == [tgio.READS_IN_FLIGHT] * tgio.DOWNLOAD_CONNECTIONS
    # ...and all of them at once. Issued back to back they would be no faster
    # than one connection, which is the whole reason the pool exists.
    assert live.peak == tgio.DOWNLOAD_CONNECTIONS * tgio.READS_IN_FLIGHT


def test_a_narrow_read_still_costs_one_request():
    """Width is what a caller asks for, never a rounding applied to it."""
    pool = [_FakeClient() for _ in range(tgio.DOWNLOAD_CONNECTIONS)]
    worker = _worker(pool)

    asyncio.run(worker._read(_Doc(), 0, 4096))

    assert sum(len(c.downloads) for c in pool) == 1


# --------------------------------------------------------------------------- #
# the exported sender
# --------------------------------------------------------------------------- #


def test_the_file_dc_sender_is_pinned_once_per_connection():
    pool = [_FakeClient() for _ in range(tgio.DOWNLOAD_CONNECTIONS)]
    worker = _worker(pool)
    doc = _Doc(dc_id=1)

    async def scenario():
        await worker._read(doc, 0, tgio.STREAM_BLOCK_SIZE)
        await worker._read(doc, tgio.STREAM_BLOCK_SIZE, tgio.STREAM_BLOCK_SIZE)
        await worker._thumbnail_bytes(doc)

    asyncio.run(scenario())

    # One borrow per client for the file's DC, however many reads follow. The
    # borrow is never returned on purpose: that is what keeps Telethon's 60s
    # timer from tearing the connection down between batches.
    for client in pool:
        assert client.borrowed == [1]


def test_home_dc_media_is_not_pinned():
    """A file in the session's own DC is served by the main sender."""
    pool = [_FakeClient(dc_id=1)]
    worker = _worker(pool)

    asyncio.run(worker._read(_Doc(dc_id=1), 0, 4096))

    assert pool[0].borrowed == []


def test_a_read_closes_its_download_iterator():
    """One chunk is the whole request, so the iterator is always left early.

    Telethon returns the borrowed sender in ``close()`` and nowhere else, so
    skipping it means every cross-DC read records a borrow and never a return —
    an accounting that only counts up cannot be told apart from a pin.
    """
    pool = [_FakeClient()]
    worker = _worker(pool)

    asyncio.run(worker._read(_Doc(), 0, 4096))

    _, pull = pool[0].downloads[0]
    assert pull.closed


def test_a_failing_read_also_closes_its_iterator():
    class _BrokenDownload(_Download):
        def __aiter__(self):
            async def boom():
                raise RuntimeError("connection went away")
                yield b""  # pragma: no cover - unreachable, keeps it a generator

            return boom()

    class _Broken(_FakeClient):
        def iter_download(self, file, **kwargs):
            pull = _BrokenDownload(b"", self._live)
            self.downloads.append((kwargs, pull))
            return pull

    pool = [_Broken()]
    worker = _worker(pool)

    with pytest.raises(RuntimeError):
        asyncio.run(worker._read(_Doc(), 0, 4096))

    assert pool[0].downloads[0][1].closed


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
