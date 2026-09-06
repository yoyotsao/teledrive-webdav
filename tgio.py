"""MTProto byte transport for the bridge.

Everything that touches Telegram bytes lives here. Metadata still comes from the
TeleDrive REST API (tdapi.py); this module never asks the Python backend for a
single byte, which is TeleDrive's core invariant.

Three pieces:

* pure split math (``build_part_table`` / ``map_range`` / ``plan_segments``) —
  unit-tested offline in tests/test_split_math.py
* ``TelegramWorker`` — one Telethon client on one background asyncio loop,
  callable from wsgidav's worker threads
* ``SeekableRemoteFile`` — a seekable, range-reading file object over one or more
  Telegram messages; feeds both wsgidav's Range handling and ``zipfile``
"""

from __future__ import annotations

import asyncio
import inspect
import io
import logging
import mimetypes
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import tgupload
from media_thumbnail import PREVIEW_BOX, PREVIEW_MAX_BYTES, capture_thumbnail
from transfer_models import PreparedAlbumItem, RemotePart, UploadedPart

log = logging.getLogger("tgio")

# MTProto upload.GetFile demands a 4096-aligned offset and a limit that is both
# 4096-aligned and a divisor of 1 MiB — and the limit must NOT be shortened for
# the final chunk (that returns LIMIT_INVALID). 512 KiB satisfies all of it and is
# also Telethon's MAX_CHUNK_SIZE, so anything larger would just be clamped.
#
# Feeding iter_download 4096-aligned offsets also keeps it on its
# _DirectDownloadIter path instead of the re-fetching generic one.
ALIGN = 4096
REQUEST_SIZE = 512 * 1024

# Independent connections beat a single one, the way the browser client reads.
#
# Measuring this is harder than it looks: Telegram throttles a session the longer
# it keeps pulling, so a straight sweep just crowns whichever setting ran first
# (1 -> 8 connections made 1 look best, 8 -> 1 made 8 look best, from the same
# machine minutes apart). Interleaving the two and comparing medians gives the
# honest answer: 8 connections beat 1 in every round, by 1.72x.
#
# Do not read that as "more is better" — 16+ connections started drawing
# "Server closed the connection" from Telegram. And this multiplies throughput
# rather than setting it: without cryptg (see requirements.txt) the pure-Python
# AES ceiling dominates and no number here helps much.
DOWNLOAD_CONNECTIONS = 8

# One GetFile is enough for any preview: Telegram's stored thumbnails top out
# well under this. Like every limit it must be 4096-aligned and divide 1 MiB.
# Cap on a photo preview. A document's `thumbs` are all small, but a photo's
# `sizes` include near-full-resolution entries, and 2,000 of those per warm-up
# pass is the whole-file read this module exists to avoid. 64 KB keeps the
# 240-320px entry Telegram stores beside every photo, which is already at the
# 256px the shell asks for.
THUMB_PREVIEW_MAX = 64 * 1024
THUMB_REQUEST_SIZE = 256 * 1024

# Our own preview, for what this bridge uploads. Telegram keeps a
# client-supplied document thumbnail only inside tight limits -- Telethon's
# guidance, matching what the API actually accepts, is a .jpg under 20 kB and
# 320x320 -- and 320 also covers the cx=256 Explorer asks for.

# How many preview GetFiles may be in flight at once, across every batch.
#
# A folder prefetch slice is THUMB_PREFETCH_SLICE (100) ids and _thumbnails used
# to gather all of them, so 100 GetFiles left at once over 8 connections. Telegram
# answers that with FLOOD_WAIT, and these calls go through Telethon's ``_call``,
# which both sleeps on the flood itself and then arms its per-request-type
# ``_flood_waited_requests`` gate — so every *other* preview in the batch is
# failed or delayed by the burst too (same gate described in CLAUDE.md for
# uploads). Two per connection keeps the pool busy without the burst.
THUMB_CONCURRENCY = DOWNLOAD_CONNECTIONS * 2

# Read granularity of the block cache inside SeekableRemoteFile. wsgidav asks for
# small blocks (config block_size) and zipfile asks for tiny ones; both get
# absorbed here so each network round trip carries a useful payload.
#
# Kept at one request: the block is the *rounding* applied to every read, so a
# large block quietly turns a small read into a large transfer. Measured, a 4 MiB
# block made a 64 KB read cost 3.95s. Width comes from fetching all the blocks a
# read spans in one parallel batch (see _blocks_for), not from making each block
# bigger, so this stays small without leaving the connection pool idle.
BLOCK_SIZE = REQUEST_SIZE

# How many GetFiles one pooled connection may have in flight at a time.
#
# The other half of DOWNLOAD_CONNECTIONS, and the cheap half: MTProto multiplexes
# requests on a connection, so one that is waiting for a reply can already carry
# the next request. With a single request each, every connection sits idle for a
# whole round trip between chunks — which is why a streamed read that fills the
# pool exactly once still leaves most of the link unused. Two per connection is
# what the preview path has been running at all along (THUMB_CONCURRENCY) without
# drawing FLOOD_WAIT, and unlike raising the connection count past 8 it does not
# earn "Server closed the connection".
READS_IN_FLIGHT = 2

# Enough for one full-width streamed read (see STREAM_BLOCK_SIZE), so the blocks
# a read just fetched are all still there when the caller comes back for the
# second half of them. Sized off the read width rather than picked: with fewer,
# _blocks_for trims the batch it has only just filled.
BLOCKS_CACHED = DOWNLOAD_CONNECTIONS * READS_IN_FLIGHT

# How much wsgidav pulls per read() while streaming a response body. This is the
# opposite knob to BLOCK_SIZE and must not be tied to it: the block is the
# rounding on a read (small = little waste), while this is the *width* of a read
# (large = many blocks batched into one parallel fetch). Setting wsgidav's
# block_size to BLOCK_SIZE meant every streamed read was a single block on a
# single connection, and 8 MiB reads got slower even though small ones improved.
# A Range request is still served with only the bytes it asked for, so a 64 KB
# probe does not pay this width.
#
# Wide enough for READS_IN_FLIGHT requests on every connection, not just one:
# _read hands the whole width to the pool in a single gather, so the width is
# also what decides how many requests are outstanding at once. One per
# connection left each of them idle for a round trip between chunks — the
# per-connection depth is the point, and this is where it comes from.
STREAM_BLOCK_SIZE = REQUEST_SIZE * DOWNLOAD_CONNECTIONS * READS_IN_FLIGHT

# Same split boundary as the browser uploader: MAX_PARTS (1000) x CHUNK_SIZE
# (512 KB) = 500 MiB, see frontend/src/lib/gramjs.ts:502 and frontend config.ts.
# Not 512 MiB — that would exceed the browser's 1000-part-per-message ceiling.
# The sender's message maximum is authoritative, so split planning and wire
# limits cannot drift apart.
SEGMENT_SIZE = tgupload.MESSAGE_MAX
assert SEGMENT_SIZE == tgupload.MAX_PARTS_PER_MESSAGE * tgupload.PART_SIZE

DOC_CACHE_TTL = 45 * 60  # file_reference lives a few hours; refresh well before
MAX_FLOOD_WAIT = 120


class RemoteIdentityError(RuntimeError):
    """Telegram returned media other than the immutable file we expected."""


def _assert_media_id(media, expected_file_id: str) -> None:
    """Refuse media that is not the file the metadata named.

    Only a Telegram document/photo id can be checked, and only some rows carry
    one. /game split parts used to be registered as
    ``f"{split_group_id}-{index}"`` whenever the upload did not hand back a
    document id, so their file_id is a timestamp and a hex tag; comparing that
    to what Telegram returns can only ever fail, and a failure here means the
    archive does not open at all. Measured on the live drive after this check
    was introduced: 127 of 143 rows under /game carried such an id and every
    one of them stopped reading, taking PROPFIND / down with them.

    A value that is not a document id carries no identity to verify, so those
    reads fall back to trusting the message id -- which is exactly what every
    read did before this check existed.
    """
    expected = str(expected_file_id or "")
    if not expected.isdigit():
        return
    actual = str(getattr(media, "id", ""))
    if actual != expected:
        raise RemoteIdentityError(
            f"Telegram file mismatch: expected {expected_file_id}, got {actual}"
        )


def read_part(pool, part: RemotePart, offset: int, length: int) -> bytes:
    """Read one routed part without weakening a nonzero account identity."""
    return pool.for_read(part.telegram_user_id).worker.read(
        part.message_id, part.file_id, offset, length
    )


# --------------------------------------------------------------------------- #
# Pure split math
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Part:
    """One Telegram message holding a contiguous slice of a logical file."""

    remote: RemotePart
    start: int  # offset of this part's first byte within the logical file

    @property
    def message_id(self) -> int:
        return self.remote.message_id

    @property
    def size(self) -> int:
        return self.remote.size


def build_part_table(parts: Sequence[RemotePart]) -> Tuple[List[Part], int]:
    """Add logical offsets to routed remote parts ordered by part index.

    Returns the table plus the logical total size. Zero-sized parts are dropped:
    they carry no bytes and would only create ambiguous offset boundaries.
    """
    table: List[Part] = []
    offset = 0
    for remote in parts:
        if remote.size <= 0:
            continue
        table.append(Part(remote=remote, start=offset))
        offset += remote.size
    return table, offset


def map_range(table: Sequence[Part], total: int, offset: int, length: int) -> List[Tuple[int, int, int]]:
    """Map a logical byte range onto parts.

    Returns ``[(table_index, offset_within_part, nbytes), ...]`` in order,
    clipped to the logical file. A range that spans a part boundary yields one
    tuple per part it touches.
    """
    if length <= 0 or offset >= total or offset < 0:
        return []
    end = min(offset + length, total)
    out: List[Tuple[int, int, int]] = []
    for i, part in enumerate(table):
        part_end = part.start + part.size
        if part_end <= offset:
            continue
        if part.start >= end:
            break
        inner = max(0, offset - part.start)
        take = min(part_end, end) - (part.start + inner)
        if take > 0:
            out.append((i, inner, take))
    return out


def plan_segments(total_size: int, segment_size: int = SEGMENT_SIZE) -> List[Tuple[int, int]]:
    """Split an upload into ``[(offset, size), ...]`` segments.

    A file that fits in one segment yields a single entry, which the caller
    registers as a non-split file (is_split_file=False).
    """
    if total_size < 0:
        raise ValueError("total_size must be >= 0")
    if segment_size <= 0:
        raise ValueError("segment_size must be > 0")
    if total_size == 0:
        return [(0, 0)]
    out = []
    offset = 0
    while offset < total_size:
        size = min(segment_size, total_size - offset)
        out.append((offset, size))
        offset += size
    return out


# --------------------------------------------------------------------------- #
# Telethon worker
# --------------------------------------------------------------------------- #


class TelegramWorker:
    """A Telethon client living on its own asyncio loop in a background thread.

    wsgidav serves requests from a thread pool while Telethon is asyncio-only, so
    every call is funnelled through ``run()`` onto the single client loop.
    """

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session: str,
        connections: int = DOWNLOAD_CONNECTIONS,
        *,
        upload_parts: int = 12,
        upload_limiter=None,
    ):
        self._api_id = api_id
        self._api_hash = api_hash
        self._session = session
        self._connections = max(1, int(connections))
        self._upload_parts = max(1, int(upload_parts))
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._client = None
        self._me = None
        self._docs = {}  # (message_id, expected_file_id) -> (media, fetched_at)
        self._docs_lock = threading.Lock()
        self._pool: Optional[list] = None
        self._pool_lock: Optional[asyncio.Lock] = None
        self._rr = 0  # round-robin cursor over the pool, see _next_client
        self._upload = None  # dedicated client for part sends, see _upload_client
        self._upload_limiter = upload_limiter
        self._gate = upload_limiter
        self._thumb_gate: Optional[asyncio.Semaphore] = None  # see THUMB_CONCURRENCY

    # -- lifecycle -------------------------------------------------------- #

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run_loop, name="tg-loop", daemon=True)
        self._thread.start()
        self._ready.wait()
        try:
            self.run(self._connect())
        except Exception:
            # A bad or expired session must not leave a live loop behind, and
            # the worker must remain retryable after configuration is fixed.
            self.stop()
            raise
        log.info("Telegram connected as %s (id=%s)", getattr(self._me, "username", None), getattr(self._me, "id", None))

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    async def _connect(self) -> None:
        # Constructed on the loop thread so Telethon binds to the right loop.
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        self._client = TelegramClient(StringSession(self._session), self._api_id, self._api_hash)
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized — regenerate it with generate_session.py")
        self._me = await self._client.get_me()
        self._pool_lock = asyncio.Lock()

    async def _download_pool(self) -> list:
        """Extra clients used only for reading bytes, built on first read.

        Same session string, so no extra login: each one just opens its own
        MTProto connection, which is the only thing that lifts the per-connection
        throughput ceiling (see DOWNLOAD_CONNECTIONS). The control client is the
        first member so a pool of one behaves exactly like the old code path.
        """
        if self._pool is not None:
            return self._pool
        async with self._pool_lock:
            if self._pool is not None:
                return self._pool
            from telethon import TelegramClient
            from telethon.sessions import StringSession

            pool = [self._client]
            for _ in range(self._connections - 1):
                extra = TelegramClient(StringSession(self._session), self._api_id, self._api_hash)
                try:
                    await extra.connect()
                except Exception as exc:  # a short pool still works, just slower
                    log.warning("download connection failed, continuing with %s: %s", len(pool), exc)
                    break
                pool.append(extra)
            log.info("download pool: %s connections", len(pool))
            self._pool = pool
            return pool

    async def _upload_client(self):
        """Extra client used only for sending upload parts, built on first upload.

        Kept out of ``_pool``, so ``_next_client``'s round robin never hands it
        download traffic, and so upload payloads never queue in front of a
        thumbnail's ``GetFile`` or a ``get_messages`` batch on ``pool[0]``
        (``pool[0] is self._client``, see ``_download_pool``).
        """
        if self._upload is not None:
            return self._upload
        async with self._pool_lock:
            if self._upload is not None:
                return self._upload
            from telethon import TelegramClient
            from telethon.sessions import StringSession

            # Surface every upload/message flood to the account limiters.
            # Telethon's default short-wait retry would bypass their feedback
            # and admission when send_file creates a message.
            client = TelegramClient(
                StringSession(self._session), self._api_id, self._api_hash,
                flood_sleep_threshold=0,
            )
            await client.connect()
            log.info("upload connection ready")
            self._upload = client
            return self._upload

    def _upload_gate(self) -> tgupload.UploadGate:
        if self._gate is None:
            self._gate = tgupload.UploadGate(self._upload_parts)
        return self._gate

    def set_upload_limiter(self, limiter) -> None:
        """Bind the AccountRuntime-owned limiter before this worker starts."""
        if self._gate is not None and self._gate is not limiter:
            raise RuntimeError("upload limiter cannot change after upload admission starts")
        self._upload_limiter = limiter
        self._gate = limiter

    def stop(self) -> None:
        loop = self._loop
        thread = self._thread
        if loop is None:
            return
        try:
            self.run(self._disconnect_all(), timeout=15)
        except Exception:  # pragma: no cover - best effort on shutdown
            pass
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=15)
        self._loop = None
        self._thread = None
        self._ready.clear()
        self._client = None
        self._me = None
        self._pool = None
        self._pool_lock = None
        self._upload = None
        self._gate = self._upload_limiter
        self._thumb_gate = None

    async def _disconnect_all(self) -> None:
        clients = list(self._pool or [self._client])
        if self._upload is not None:
            clients.append(self._upload)
        for client in clients:
            try:
                await client.disconnect()
            except Exception:  # pragma: no cover - best effort on shutdown
                pass

    def run(self, coro, timeout: Optional[float] = None):
        """Run a coroutine on the client loop and block until it finishes."""
        if self._loop is None:
            raise RuntimeError("TelegramWorker not started")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    @property
    def user_id(self) -> Optional[int]:
        return getattr(self._me, "id", None)

    def send_dm(self, username: str, text: str) -> None:
        """DM `text` to a bot as the logged-in user. Used for the backend's
        login challenge (tdapi.TeleDriveClient.login) -- the nonce has to arrive
        at the bot *from this account*, because the update's sender is the only
        proof of identity the backend gets."""
        self.run(self._send_dm(username, text), timeout=60)

    async def _send_dm(self, username: str, text: str) -> None:
        # Control connection, not the pool: this is one small message, and the
        # pool clients share the session only for file transfers.
        await self._client.send_message(username, text)

    # -- documents -------------------------------------------------------- #

    def get_document(self, message_id: int, expected_file_id: str, refresh: bool = False):
        """Resolve a message id to its Document, with a TTL cache.

        ``file_reference`` inside the Document expires after a few hours, so both
        the TTL and the explicit ``refresh`` path exist to re-fetch it.
        """
        return self.run(self._document(message_id, expected_file_id, refresh))

    async def _document(
        self, message_id: int, expected_file_id: str, refresh: bool = False
    ):
        """Async half of get_document, so batch paths can await it directly.

        Calling get_document from a coroutine already on the client loop would
        deadlock on its own run(); everything that needs a document while on the
        loop comes through here instead.
        """
        now = time.monotonic()
        key = (int(message_id), str(expected_file_id))
        if not refresh:
            with self._docs_lock:
                hit = self._docs.get(key)
            if hit and now - hit[1] < DOC_CACHE_TTL:
                return hit[0]
        doc = await self._fetch_document(message_id)
        _assert_media_id(doc, expected_file_id)
        with self._docs_lock:
            self._docs[key] = (doc, now)
        return doc

    async def _fetch_document(self, message_id: int):
        """The message's media, which is a Document *or* a Photo.

        The browser uploader sends documents, but the backend's chat-media
        import registers messages lifted straight out of chats and those are
        MessageMediaPhoto. Rejecting them here used to fail the read, the
        preview and the properties all at once — see _media_size for why a photo
        needs its own size arithmetic.
        """
        # Files live in Saved Messages ("me"), same as the browser uploader.
        messages = await self._client.get_messages("me", ids=[message_id])
        msg = messages[0] if messages else None
        media = _message_media(msg)
        if media is None:
            raise FileNotFoundError(f"Telegram message {message_id} has no document or photo")
        return media

    # -- thumbnails ------------------------------------------------------- #

    def thumbnails(self, parts: Sequence[RemotePart]) -> dict:
        """``{(message_id, file_id): jpeg}`` for routed media with a thumbnail.

        Telegram already stores a small preview beside every photo and video, so
        listing a folder of previews costs a few KB per file instead of the whole
        image — measured on one pixiv folder, 120.9 MB of originals against 52 KB
        of thumbnails. Ids without a usable thumbnail are simply absent.
        """
        unique = list({(p.message_id, str(p.file_id)): p for p in parts}.values())
        if not unique:
            return {}
        return self.run(self._thumbnails(unique))

    async def _thumbnails(self, parts: List[RemotePart]) -> dict:
        # One get_messages covers the whole batch; asking per file turned a
        # 30-file listing into 30 round trips.
        await self._prefetch_documents(parts)

        # Created here rather than in __init__ so it binds to the client loop,
        # and shared across batches: a folder prefetch and a foreground request
        # overlapping must not add up to twice the burst.
        if self._thumb_gate is None:
            self._thumb_gate = asyncio.Semaphore(THUMB_CONCURRENCY)
        gate = self._thumb_gate

        async def one(part: RemotePart):
            key = (part.message_id, str(part.file_id))
            try:
                doc = await self._document(part.message_id, part.file_id)
                async with gate:
                    return key, await self._thumbnail_bytes(doc)
            except Exception as exc:
                log.warning("thumbnail for message %s failed: %s", part.message_id, exc)
                return key, None

        pairs = await asyncio.gather(*[one(part) for part in parts])
        return {key: data for key, data in pairs if data}

    def media_info(self, parts: Sequence[RemotePart]) -> dict:
        """``{(message_id, file_id): {...}}`` describing routed media.

        Pixel dimensions and duration ride along in the document's attributes, so
        this costs one ``get_messages`` per hundred files and downloads nothing.
        That is the whole point: Explorer reads the head of every image to work
        out its size (measured, 258 KB of a 2 MB JPEG), and those reads are what
        make a folder crawl.
        """
        unique = list({(p.message_id, str(p.file_id)): p for p in parts}.values())
        if not unique:
            return {}
        return self.run(self._media_info(unique))

    async def _media_info(self, parts: List[RemotePart]) -> dict:
        await self._prefetch_documents(parts)
        out = {}
        for part in parts:
            key = (part.message_id, str(part.file_id))
            try:
                doc = await self._document(part.message_id, part.file_id)
            except Exception as exc:
                log.warning("media info for message %s failed: %s", part.message_id, exc)
                continue
            # Recorded even when empty. An entry here means "the document was
            # read and it has nothing to report", which is a cacheable answer;
            # dropping it would make a file with no dimensions look uncached
            # forever, and the whole-tree warm-up would ask about it on every
            # pass. A lookup that actually failed raises above and stays absent.
            out[key] = _media_attributes(doc)
        return out

    async def _prefetch_documents(self, parts: List[RemotePart]) -> None:
        now = time.monotonic()
        with self._docs_lock:
            wanted = [
                part for part in parts
                if not (
                    self._docs.get((part.message_id, str(part.file_id)))
                    and now - self._docs[(part.message_id, str(part.file_id))][1]
                    < DOC_CACHE_TTL
                )
            ]
        for batch in (wanted[i : i + 100] for i in range(0, len(wanted), 100)):
            try:
                messages = await self._client.get_messages(
                    "me", ids=list(dict.fromkeys(part.message_id for part in batch))
                )
            except Exception as exc:
                log.warning("batch document fetch failed (%s ids): %s", len(batch), exc)
                return  # per-file lookups below still work, just slower
            by_message = {
                msg.id: _message_media(msg) for msg in messages or [] if msg is not None
            }
            with self._docs_lock:
                for part in batch:
                    media = by_message.get(part.message_id)
                    if media is None:
                        continue
                    try:
                        _assert_media_id(media, part.file_id)
                    except RemoteIdentityError:
                        continue
                    self._docs[(part.message_id, str(part.file_id))] = (media, now)

    async def _thumbnail_bytes(self, doc) -> Optional[bytes]:
        """One preview, fetched over the pool rather than the control client.

        ``client.download_media`` would run every preview down the single control
        connection, which is the whole reason warming a folder used to crawl: the
        downloads are tiny but there are hundreds of them, and one connection
        serialises them all. Issued on a pooled client they spread across the pool
        like ordinary reads do.

        It has to be ``iter_download`` and not a bare ``GetFileRequest``, and
        ``dc_id`` has to be passed. A document whose ``dc_id`` is not the
        session's is answered with FILE_MIGRATE, and ``client._call`` only follows
        Phone/Network/User migrations (``telethon/client/users.py``); the file case
        is handled inside ``iter_download``, which borrows an exported sender for
        the file's DC up front and retries on FILE_MIGRATE
        (``telethon/client/downloads.py``). A raw call therefore failed *every*
        preview for documents stored elsewhere — "the file to be accessed is
        currently stored in DC 1", a whole folder at a time — and the shell
        handler then fell back to reading whole files, which is the slow path this
        module exists to avoid. Ordinary reads never showed it because ``_chunk``
        was already going through ``iter_download``.
        """
        thumb = _best_thumb(doc)
        if thumb is None:
            return None
        # Photos and documents are different constructors on the wire: a photo
        # addressed with InputDocumentFileLocation comes back LOCATION_INVALID.
        from telethon.tl.types import InputDocumentFileLocation, InputPhotoFileLocation

        ctor = InputPhotoFileLocation if _is_photo(doc) else InputDocumentFileLocation
        location = ctor(
            id=doc.id,
            access_hash=doc.access_hash,
            file_reference=doc.file_reference,
            thumb_size=thumb.type,
        )
        pool = await self._download_pool()
        attempt = 0
        while True:
            client = self._next_client(pool)
            try:
                await self._pin_exported_sender(client, getattr(doc, "dc_id", None))
                # file_size makes it one request; iterating to exhaustion (rather
                # than breaking out) lets Telethon return the exported sender.
                data = b""
                async for chunk in client.iter_download(
                    location,
                    dc_id=getattr(doc, "dc_id", None),
                    file_size=getattr(thumb, "size", None),
                    request_size=THUMB_REQUEST_SIZE,
                ):
                    data += bytes(chunk)
                return data or None
            except Exception as exc:
                wait = _flood_seconds(exc)
                if wait is None and _is_export_race(exc):
                    wait = 1
                if wait is None or attempt >= 2:
                    raise
                attempt += 1
                log.warning("preview retry %s after %ss: %s", attempt, wait, exc)
                await asyncio.sleep(wait + 1)

    def invalidate_document(self, message_id: int, expected_file_id: str) -> None:
        with self._docs_lock:
            self._docs.pop((int(message_id), str(expected_file_id)), None)

    # -- reading ---------------------------------------------------------- #

    def read(
        self, message_id: int, expected_file_id: str, offset: int, length: int
    ) -> bytes:
        """Read ``length`` bytes at ``offset`` from one message's document."""
        if length <= 0:
            return b""
        doc = self.get_document(message_id, expected_file_id)
        try:
            return self.run(self._read(doc, offset, length))
        except Exception as exc:
            if not _is_file_reference_error(exc):
                raise
            log.warning("file_reference expired for message %s — refetching", message_id)
            doc = self.get_document(message_id, expected_file_id, refresh=True)
            return self.run(self._read(doc, offset, length))

    async def _read(self, doc, offset: int, length: int) -> bytes:
        """Fetch a range as REQUEST_SIZE chunks, one per pooled connection.

        Splitting the range and spreading the pieces across independent
        connections is the whole point: the same requests issued back-to-back on
        one connection run at a fraction of the speed.
        """
        aligned = offset - (offset % ALIGN)
        skip = offset - aligned
        need = skip + length
        pool = await self._download_pool()
        starts = range(aligned, aligned + need, REQUEST_SIZE)
        chunks = await asyncio.gather(*[
            self._chunk(self._next_client(pool), doc, start) for start in starts
        ])
        return b"".join(chunks)[skip:need]

    def _next_client(self, pool):
        """Round-robin across the pool, continuing where the last read left off.

        Indexing by position *within* a read sends every one-chunk read to the
        same connection, so browsing a folder of small files — thumbnails, the
        case the pool exists for — crowded onto pool[0] and ran barely faster
        than serial. The cursor is per-worker and only ever touched from the
        client loop thread, so it needs no lock.
        """
        self._rr = (self._rr + 1) % len(pool)
        return pool[self._rr]

    async def _pin_exported_sender(self, client, dc_id) -> None:
        """Borrow the file's DC sender once per client and never give it back.

        Every byte here comes from a DC that is not the session's — the account
        lives in one, the stored files in another — so every read and every
        preview is answered by an *exported* sender. Telethon borrows one per
        download and returns it when the download ends, then disconnects it 60s
        after the last return (``_DISCONNECT_EXPORTED_AFTER`` in
        ``telethon/client/telegrambaseclient.py``). Sixty seconds of quiet is
        nothing here — it is one gap between warm-up batches, or a folder nobody
        clicked for a minute — so the next batch reconnects all eight pool
        connections to that DC at the same moment. One bridge.log has 248
        "Disconnecting borrowed sender for DC 1", 387 reconnects, and 138 "Server
        closed the connection": Telegram's answer to eight simultaneous
        handshakes from one address.

        Each of those closures fails whatever was in flight, and a failed preview
        is not a slower thumbnail — the DLL cannot tell it from "this file has no
        preview", so it delegates to the built-in handler, which reads the whole
        original image (see CLAUDE.md). One dropped connection therefore costs a
        multi-megabyte download, and it lands exactly when a folder is being
        browsed.

        Holding one borrow forever keeps the reference count off zero, so
        ``should_disconnect()`` never fires and the sender stays up for the life
        of the bridge. Nothing else changes: the per-download borrows still
        happen on top of this one, and MTProtoSender still reconnects itself if
        Telegram drops the connection anyway — it just no longer tears the
        connection down on a timer and rebuild eight at once.

        Same reasoning as TeleDrive's own ``senderDcFor`` (frontend commit
        4d397f8): who answers a GetFile is worth being deliberate about, because
        getting it wrong shows up as "this folder is cold", never as an error.
        """
        if not dc_id:
            return
        session = getattr(client, "session", None)
        if getattr(session, "dc_id", None) == dc_id:
            return  # home-DC media is served by the main sender, nothing to pin
        borrow = getattr(client, "_borrow_exported_sender", None)
        if borrow is None:  # pragma: no cover - a real client always has it
            return
        pinned = getattr(client, "_td_pinned_dcs", None)
        if pinned is None:
            pinned = set()
            setattr(client, "_td_pinned_dcs", pinned)
        if dc_id in pinned:
            return
        pinned.add(dc_id)
        try:
            await borrow(dc_id)
        except Exception as exc:  # the download below can still borrow its own
            pinned.discard(dc_id)
            log.warning("could not pin a sender for DC %s: %s", dc_id, exc)

    async def _chunk(self, client, doc, offset: int) -> bytes:
        """One REQUEST_SIZE read. Short only at end of file."""
        attempt = 0
        while True:
            try:
                await self._pin_exported_sender(client, getattr(doc, "dc_id", None))
                pull = client.iter_download(
                    doc,
                    offset=offset,
                    request_size=REQUEST_SIZE,
                    file_size=_media_size(doc),
                )
                try:
                    async for chunk in pull:
                        return bytes(chunk)
                    return b""
                finally:
                    await _close_download(pull)
            except Exception as exc:
                wait = _flood_seconds(exc)
                if wait is None or attempt >= 2:
                    raise
                attempt += 1
                log.warning("FLOOD_WAIT %ss while reading (attempt %s)", wait, attempt)
                await asyncio.sleep(wait + 1)

    # -- uploading -------------------------------------------------------- #

    def upload_segment(self, stream, size: int, file_name: str, progress=None, preview=None, *, force_big=None) -> dict:
        """Upload one segment as a single Telegram document message.

        ``stream`` is a binary file object positioned at the segment start and
        limited to ``size`` bytes (see ``SegmentReader``, which also supports
        the random-access ``seek()``+``read()`` the parallel path below uses).

        ``preview`` is an optional ``(jpeg_path, width, height)`` from
        ``make_preview`` — the thumbnail and dimensions that let /rpc/thumb and
        /rpc/props answer for our own uploads instead of sending the shell off
        to read the whole file. Callers pass it only for a single-segment still
        image; a zip or one part of a split file has no preview to give.
        """
        return self.run(
            self._upload_segment(stream, size, file_name, progress, preview, force_big), timeout=None
        )

    async def _upload_segment(self, stream, size: int, file_name: str, progress, preview=None, force_big=None) -> dict:
        handle = await self._prepare_segment(stream, size, file_name, progress, force_big)
        return await self._send_uploaded_segment(handle, size, file_name, preview)

    def prepare_segment(self, stream, size: int, file_name: str, progress=None, *, force_big=None):
        """Upload bytes without sending a message or retaining a file lease."""
        return self.run(self._prepare_segment(stream, size, file_name, progress, force_big), timeout=None)

    async def _prepare_segment(self, stream, size, file_name, progress=None, force_big=None):
        decision = tgupload.decide_protocol(size, album_eligible=False)
        if force_big is None:
            force_big = bool(getattr(stream, "force_big", False))
        client = await self._upload_client()
        async with tgupload._PartReader(stream) as reader:
            if force_big or decision.force_big:
                handle = await tgupload.upload_big_file_parts(
                    client, self._upload_gate(), reader, size, file_name,
                    force_big=True, progress=progress,
                )
            else:
                handle = await tgupload.upload_small_file_parts(
                    client, self._upload_gate(), reader, size, file_name, progress=progress,
                )
        return handle

    def prepare_thumbnail(self, preview):
        """Upload a JPEG through this account's chunk limiter before message send."""
        return self.run(self._prepare_thumbnail(preview), timeout=None)

    async def _prepare_thumbnail(self, preview):
        import io
        from pathlib import Path

        source, width, height = preview
        data = source if isinstance(source, bytes) else Path(source).read_bytes()
        client = await self._upload_client()
        async with tgupload._PartReader(io.BytesIO(data)) as reader:
            handle = await tgupload.upload_small_file_parts(
                client, self._upload_gate(), reader, len(data), "thumbnail.jpg",
            )
        return handle, width, height

    def prepare_album_item(self, source, size, file_name, mime_type, preview=None, *, message_limiter=None):
        """Prepare a document on this account while the caller owns a file lease."""
        return self.run(self._prepare_album_item(
            source, size, file_name, mime_type, preview, message_limiter=message_limiter,
        ), timeout=None)

    async def _prepare_album_file(self, stream, size, file_name):
        """Albums use 512 KiB SaveFilePart, including optional thumbnail bytes."""
        import hashlib
        from telethon.tl.functions.upload import SaveFilePartRequest
        from telethon.tl.types import InputFile

        if not 0 < size <= tgupload.SMALL_FILE_MAX:
            raise ValueError("album upload size must be between 1 byte and 10 MiB")
        client = await self._upload_client()
        async with tgupload._PartReader(stream) as reader:
            file_id, total, payloads = await tgupload._upload_parts(
                client, self._upload_gate(), reader, size,
                parts=[(offset, min(REQUEST_SIZE, size - offset))
                       for offset in range(0, size, REQUEST_SIZE)],
                request_factory=lambda file_id, index, _total, data: SaveFilePartRequest(file_id, index, data),
                workers=4, progress=None, collect_payloads=True,
            )
        return InputFile(file_id, total, file_name, hashlib.md5(b"".join(payloads)).hexdigest())

    async def _prepare_album_item(self, source, size, file_name, mime_type, preview=None, *, message_limiter=None):
        from pathlib import Path
        from config import ext_path
        from telethon.tl.functions.messages import UploadMediaRequest
        from telethon.tl.types import (
            DocumentAttributeFilename, DocumentAttributeImageSize,
            InputMediaUploadedDocument, InputPeerSelf,
        )

        with open(ext_path(source), "rb") as stream:
            handle = await self._prepare_album_file(stream, size, file_name)
        attributes = [DocumentAttributeFilename(file_name)]
        thumb = None
        if preview is not None:
            thumbnail, width, height = preview
            data = thumbnail if isinstance(thumbnail, bytes) else Path(thumbnail).read_bytes()
            thumb = await self._prepare_album_file(io.BytesIO(data), len(data), "thumbnail.jpg")
            attributes.append(DocumentAttributeImageSize(width, height))
        request = UploadMediaRequest(InputPeerSelf(), InputMediaUploadedDocument(
            file=handle, mime_type=mime_type, attributes=attributes, thumb=thumb,
        ))
        media = await self._album_rpc(request, message_limiter)
        document = getattr(media, "document", None)
        if document is None:
            raise RemoteIdentityError("album preparation returned no document")
        return PreparedAlbumItem(
            source=Path(source), upload_name=file_name, mime_type=mime_type, size=size,
            telegram_user_id=int(self.user_id), document_id=str(document.id),
            access_hash=str(document.access_hash), has_thumbnail=thumb is not None,
            file_reference=document.file_reference,
        )

    async def _album_rpc(self, request, message_limiter):
        client = await self._upload_client()
        for attempt in range(3):
            if message_limiter is not None:
                await message_limiter.acquire()
            try:
                return await client(request)
            except Exception as exc:
                wait = _flood_seconds(exc)
                if wait is None or message_limiter is None:
                    raise
                message_limiter.flood(wait)
                if attempt == 2:
                    raise

    def send_album(self, items, timeout=60, *, message_limiter=None):
        """Send one account's prepared items and match exact document identities."""
        return self.run(self._send_album(items, timeout, message_limiter=message_limiter), timeout=None)

    async def _send_album(self, items, timeout=60, *, message_limiter=None):
        from telethon import helpers
        from telethon.tl.functions.messages import SendMultiMediaRequest
        from telethon.tl.types import InputDocument, InputMediaDocument, InputPeerSelf, InputSingleMedia

        if not 1 <= len(items) <= 10:
            raise ValueError("an album must contain between one and ten items")
        if any(item.telegram_user_id != int(self.user_id) for item in items):
            raise ValueError("an album cannot mix Telegram accounts")
        if len({item.document_id for item in items}) != len(items):
            raise RemoteIdentityError("album preparation returned duplicate document IDs")
        request = SendMultiMediaRequest(InputPeerSelf(), [InputSingleMedia(
            media=InputMediaDocument(InputDocument(int(item.document_id), int(item.access_hash), item.file_reference)),
            random_id=helpers.generate_random_long(), message="",
        ) for item in items])
        response = await asyncio.wait_for(self._album_rpc(request, message_limiter), timeout=timeout)
        by_document = {}
        for update in response.updates:
            message = getattr(update, "message", None)
            document = getattr(getattr(message, "media", None), "document", None)
            if document is not None:
                key = str(document.id)
                if key in by_document:
                    raise RemoteIdentityError("album returned duplicate document IDs")
                by_document[key] = message
        parts = []
        for item in items:
            message = by_document.get(str(item.document_id))
            if message is None:
                raise RemoteIdentityError(f"album returned no message for document {item.document_id}")
            document = message.media.document
            parts.append(UploadedPart(
                index=0, message_id=int(message.id), file_id=str(document.id),
                access_hash=str(document.access_hash), size=item.size,
                telegram_user_id=item.telegram_user_id, has_thumbnail=item.has_thumbnail,
            ))
        return parts

    def prepare_album_fallback(self, stream, size, file_name):
        """Reread fallback bytes using exactly one ordinary small-upload worker."""
        return self.run(self._prepare_album_fallback(stream, size, file_name), timeout=None)

    async def _prepare_album_fallback(self, stream, size, file_name):
        client = await self._upload_client()
        async with tgupload._PartReader(stream) as reader:
            return await tgupload.upload_small_file_parts(
                client, self._upload_gate(), reader, size, file_name, workers=1,
            )

    def send_uploaded_segment(self, handle, size, file_name, preview=None, *, mime_type=None, message_limiter=None):
        """Send an already uploaded document on the account's event loop."""
        return self.run(self._send_uploaded_segment(
            handle, size, file_name, preview, mime_type=mime_type, message_limiter=message_limiter,
        ), timeout=None)

    async def _send_uploaded_segment(self, handle, size, file_name, preview=None, *, mime_type=None, message_limiter=None):
        from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeImageSize

        client = await self._upload_client()
        attributes = [DocumentAttributeFilename(file_name)]
        thumb = None
        if preview is not None:
            thumb, width, height = preview
            # Telegram needs image dimensions alongside the thumbnail. The
            # engine supplies a pre-uploaded JPEG handle; legacy callers may
            # still supply a .jpg path for Telethon to upload.
            attributes.append(DocumentAttributeImageSize(width, height))
        options = {"mime_type": mime_type} if mime_type else {}
        for attempt in range(3):
            if message_limiter is not None:
                await message_limiter.acquire()
            try:
                msg = await client.send_file(
                    "me", handle, force_document=True, attributes=attributes,
                    thumb=thumb, **options,
                )
                break
            except Exception as exc:
                wait = _flood_seconds(exc)
                if wait is None or message_limiter is None:
                    raise
                message_limiter.flood(wait)
                if attempt == 2:
                    raise
        doc = msg.document
        if doc is None:
            raise RemoteIdentityError("Telegram accepted the upload but returned no document")
        return {
            "message_id": msg.id,
            "file_id": str(doc.id),
            "access_hash": str(doc.access_hash),
            "size": getattr(doc, "size", size),
        }


def make_preview(
    path, mime_type: str = "", ffmpeg: Optional[str] = None
) -> Optional[Tuple[bytes, int, int]]:
    """Adapt classified media thumbnails to Telethon's document-thumb tuple.

    ``not_media`` and ``undecodable`` files deliberately upload without a
    preview. A ``ThumbnailError`` is allowed to propagate: it means a decoder
    accepted media but failed to produce a usable frame, which must not be
    silently registered as an ordinary no-thumbnail upload.
    """
    mime = mime_type or mimetypes.guess_type(str(path))[0] or ""
    result = capture_thumbnail(path, mime, ffmpeg)
    if result.kind != "ready":
        if result.error:
            log.info("no preview for %s: %s", getattr(path, "name", path), result.error)
        return None
    return result.jpeg, result.width, result.height


def _media_attributes(doc) -> dict:
    """Pixel size and duration off a Document's attributes, or ``{}``.

    Duck-typed rather than isinstance-checked against Telethon's TL classes: the
    attribute list is a union of types that differ only in which of these fields
    they carry, and a photo's DocumentAttributeImageSize has w/h exactly like a
    video's DocumentAttributeVideo does.
    """
    if _is_photo(doc):
        full = doc.sizes[-1] if getattr(doc, "sizes", None) else None
        out = {}
        if getattr(full, "w", None) and getattr(full, "h", None):
            out["width"] = int(full.w)
            out["height"] = int(full.h)
        # Photos are always JPEG on Telegram's side; there is no mime_type field
        # to read, and the shell wants one to decide it need not open the file.
        out["mime"] = "image/jpeg"
        return out

    out = {}
    for attr in getattr(doc, "attributes", None) or []:
        width = getattr(attr, "w", None)
        height = getattr(attr, "h", None)
        if width and height:
            out["width"] = int(width)
            out["height"] = int(height)
        duration = getattr(attr, "duration", None)
        if duration:
            out["duration"] = float(duration)
    if getattr(doc, "mime_type", None):
        out["mime"] = str(doc.mime_type)
    return out


def _message_media(msg):
    """The Document or Photo a message carries, or None.

    Two kinds because two upload paths: the browser sends documents, the
    backend's chat-media import registers photos lifted out of chats.
    """
    if msg is None:
        return None
    return getattr(msg, "document", None) or getattr(msg, "photo", None)


def _is_photo(media) -> bool:
    """A Photo carries ``sizes``; a Document carries ``thumbs`` and ``size``.

    Structural rather than isinstance so the fakes in the tests stay small and
    a Telethon type rename cannot silently turn every photo back into a 500.
    """
    return getattr(media, "sizes", None) is not None and not hasattr(media, "size")


def _photo_size_bytes(size) -> Optional[int]:
    """Byte count of one PhotoSize-ish entry, or None if it has no concrete one.

    ``PhotoSizeProgressive`` has no ``size``: it lists the cumulative length of
    each progressive scan, so the whole image is the *last* element — summing
    them over-reports by a factor of three and makes the client read past the
    end.
    """
    concrete = getattr(size, "size", None)
    if concrete is not None:
        return int(concrete)
    progressive = getattr(size, "sizes", None)
    if progressive:
        return int(progressive[-1])
    return None


def _media_size(media) -> Optional[int]:
    """Length in bytes of the file this media *is*.

    For a document that is ``size``. For a photo it is the byte count of
    ``sizes[-1]`` — which is both what Telethon's own ``get_input_location``
    downloads and, checked against four live messages, exactly what the backend
    recorded as the file's size. Getting this wrong is not a rounding error: the
    reader would either stop short or wait out a timeout on bytes that are not
    there.
    """
    if _is_photo(media):
        return _photo_size_bytes(media.sizes[-1]) if media.sizes else None
    return getattr(media, "size", None)


def _best_thumb(doc):
    """The largest ready-to-use thumbnail on ``doc``, or None.

    Documents also carry a PhotoStrippedSize: a ~100 byte blur that only becomes
    a JPEG after re-attaching a standard header. The PhotoSize entries are
    complete JPEGs already, and they identify themselves by having a ``size``,
    so picking the largest of those skips the stripped one without special-casing
    its type letter.
    """
    if _is_photo(doc):
        # A photo's own `sizes` are its previews, except the last, which is the
        # full image. Unlike a document's `thumbs` — which Telegram keeps small,
        # ~17 KB on average — these run up to near the original: on one measured
        # message "m" was 32 KB and "x" was 150 KB of a 283 KB file. Taking the
        # largest would make the preview cost as much as the read it exists to
        # avoid, so cap it and take the biggest that fits.
        best = _under_cap(doc.sizes[:-1])
        # A photo small enough to have no intermediate size still needs an
        # answer, and at that point the full image *is* the cheap one.
        return best if best is not None else _under_cap(doc.sizes)

    candidates = getattr(doc, "thumbs", None) or []
    # A photo's own `sizes` are its previews, except the last, which is the full
    # image — fetching that per file is the whole-file read the preview exists
    # to avoid. Trimming it can leave nothing but the stripped blur, and for a
    # photo small enough to have no intermediate size the full one *is* cheap,
    # so fall back to the untrimmed list rather than answering 404.
    return _largest_concrete(candidates)


def _under_cap(sizes):
    """Largest complete entry within THUMB_PREVIEW_MAX, else the smallest one.

    The fallback matters: a photo whose every preview is over the cap still
    wants an answer, and the smallest of them is cheaper than the original the
    shell would otherwise read in full.
    """
    concrete = [(z, _photo_size_bytes(z)) for z in sizes or []]
    concrete = [(z, n) for z, n in concrete if n is not None and getattr(z, "size", None) is not None]
    if not concrete:
        return None
    fits = [(z, n) for z, n in concrete if n <= THUMB_PREVIEW_MAX]
    if fits:
        return max(fits, key=lambda pair: pair[1])[0]
    return min(concrete, key=lambda pair: pair[1])[0]


def _largest_concrete(sizes):
    """The biggest entry that is a complete JPEG on its own.

    A PhotoStrippedSize is ~100 bytes of blur that only becomes an image after a
    standard header is re-attached; it identifies itself by having no byte count
    of its own, so asking for one skips it without special-casing its type
    letter. Progressive entries are skipped too — they are the full image.
    """
    best = None
    for size in sizes or []:
        concrete = getattr(size, "size", None)
        if concrete is None:
            continue
        if best is None or concrete > best[1]:
            best = (size, concrete)
    return best[0] if best else None


def _is_file_reference_error(exc: BaseException) -> bool:
    try:
        from telethon.errors import FileReferenceExpiredError

        if isinstance(exc, FileReferenceExpiredError):
            return True
    except Exception:  # pragma: no cover
        pass
    return "FILE_REFERENCE" in str(exc).upper()


def _is_export_race(exc: BaseException) -> bool:
    """AUTH_BYTES_INVALID while importing an exported authorization.

    The pool is eight clients built from one session string, so the first
    cross-DC file in a batch has all of them exporting an authorization for the
    same DC at the same moment, and Telegram rejects some of the imports.
    Measured on one sweep: 19 previews lost inside a two-minute window at
    startup, then 1,679 fetches with none. Telethon neither retries it nor
    disconnects the sender it already connected before the import failed — that
    dropped sender is where the "Task was destroyed but it is pending" pairs in
    the log come from (76 of them, four per failure). Retrying lands on another
    connection, by which time the burst is over.
    """
    try:
        from telethon.errors import AuthBytesInvalidError

        if isinstance(exc, AuthBytesInvalidError):
            return True
    except Exception:  # pragma: no cover
        pass
    return "AUTH_BYTES_INVALID" in str(exc).upper()


def _flood_seconds(exc: BaseException) -> Optional[int]:
    """Return the wait in seconds if ``exc`` is a short FLOOD_WAIT, else None."""
    try:
        from telethon.errors import FloodWaitError

        if isinstance(exc, FloodWaitError) and exc.seconds <= MAX_FLOOD_WAIT:
            return int(exc.seconds)
    except Exception:  # pragma: no cover
        pass
    return None


# --------------------------------------------------------------------------- #
# File objects
# --------------------------------------------------------------------------- #


async def _close_download(pull) -> None:
    """Let a download iterator run its cleanup, which returns a borrowed sender.

    ``async for`` that breaks out on the first chunk — which is exactly what
    ``_chunk`` does, one chunk being the whole request — never reaches the
    iterator's own end, and Telethon's ``RequestIter`` only returns the exported
    sender in ``close()`` (``telethon/client/downloads.py``). Every cross-DC read
    therefore used to record a borrow and never a return, which CLAUDE.md noted
    and left alone. It is harmless only for as long as nothing depends on the
    count being right, and _pin_exported_sender does depend on it: an accounting
    that only ever counts up cannot be told apart from a deliberate pin.
    """
    close = getattr(pull, "close", None) or getattr(pull, "aclose", None)
    if close is None:
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # pragma: no cover - cleanup must not mask a read
        log.debug("closing a download iterator failed: %s", exc)


def _contiguous(indices: Sequence[int]):
    """Yield ``(start, end)`` for each run of consecutive ints in ``indices``."""
    start = prev = None
    for index in indices:
        if start is None:
            start = prev = index
        elif index == prev + 1:
            prev = index
        else:
            yield start, prev
            start = prev = index
    if start is not None:
        yield start, prev


class SeekableRemoteFile(io.RawIOBase):
    """Seekable read-only view over a logical file stored in N Telegram messages.

    Split files are concatenated virtually: the caller sees one continuous byte
    range and never learns where the part boundaries are. Reads go through an
    LRU block cache so ``zipfile``'s many small seeks cost few round trips.
    """

    def __init__(
        self,
        pool,
        parts: Sequence[RemotePart],
        *,
        name: str = "",
        head: bytes = b"",
        block_size: int = BLOCK_SIZE,
        blocks_cached: int = BLOCKS_CACHED,
    ):
        super().__init__()
        self._pool = pool
        self._table, self._total = build_part_table(parts)
        self._name = name
        # Bytes from the start of the file that the caller already has on disk.
        # Overlaid on the block cache rather than seeded into it, because a head
        # is whatever length the caller chose and a partial block would make
        # _read_at stop short in the middle of a legitimate read.
        self._head = head[: self._total] if head else b""
        self._block_size = block_size
        self._blocks_cached = blocks_cached
        self._blocks: "OrderedDict[int, bytes]" = OrderedDict()
        self._pos = 0

    # -- io plumbing ------------------------------------------------------ #

    @property
    def name(self) -> str:  # noqa: A003 - matches the file-object protocol
        return self._name

    @property
    def size(self) -> int:
        return self._total

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            pos = offset
        elif whence == io.SEEK_CUR:
            pos = self._pos + offset
        elif whence == io.SEEK_END:
            pos = self._total + offset
        else:
            raise ValueError(f"invalid whence {whence}")
        if pos < 0:
            raise OSError("negative seek position")
        self._pos = pos
        return pos

    def readinto(self, buf) -> int:
        want = len(buf)
        if want == 0 or self._pos >= self._total:
            return 0
        want = min(want, self._total - self._pos)
        data = self._read_at(self._pos, want)
        buf[: len(data)] = data
        self._pos += len(data)
        return len(data)

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        if size is None or size < 0:
            size = self._total - self._pos
        size = max(0, min(size, self._total - self._pos))
        if size == 0:
            return b""
        data = self._read_at(self._pos, size)
        self._pos += len(data)
        return data

    # -- block cache ------------------------------------------------------ #

    def _read_at(self, offset: int, length: int) -> bytes:
        end = min(offset + length, self._total)
        if length <= 0 or offset >= end or offset < 0:
            return b""
        if offset < len(self._head):
            # Explorer's thumbnail pipeline opens every JPEG and reads its first
            # tens of KB — after the thumbnail provider has already answered, so
            # no shell extension can head it off. Cold, that read is a Telegram
            # round trip and costs 2-5 seconds per file; it is what makes a
            # folder nobody has opened slow while one opened before is instant.
            # Serving it from disk is the only place left to make it cheap.
            take = min(end, len(self._head)) - offset
            out = self._head[offset : offset + take]
            if offset + take >= end:
                return out
            return out + self._read_at(offset + take, end - offset - take)
        first = offset // self._block_size
        last = (end - 1) // self._block_size
        blocks = self._blocks_for(first, last)
        out = bytearray()
        pos = offset
        while pos < end:
            index = pos // self._block_size
            block = blocks.get(index)
            if not block:
                break  # short read: the file ends earlier than the table claims
            inner = pos - index * self._block_size
            take = min(end - pos, len(block) - inner)
            if take <= 0:
                break
            out += block[inner : inner + take]
            pos += take
        return bytes(out)

    def _blocks_for(self, first: int, last: int) -> dict:
        """Blocks ``first..last``, fetching the missing ones in one batch.

        Each contiguous run of missing blocks becomes a single ``_fetch``, which
        splits into REQUEST_SIZE pieces across the connection pool. That is what
        lets the block itself stay small: batching supplies the width, so a wide
        read still fills every connection while a narrow one only pays for a
        single block instead of rounding up to a large one.
        """
        found = {}
        missing = []
        for index in range(first, last + 1):
            block = self._blocks.get(index)
            if block is None:
                missing.append(index)
            else:
                found[index] = block
        for run_start, run_end in _contiguous(missing):
            start = run_start * self._block_size
            stop = min((run_end + 1) * self._block_size, self._total)
            data = self._fetch(start, stop - start)
            for index in range(run_start, run_end + 1):
                lo = index * self._block_size - start
                if lo >= len(data):
                    break
                found[index] = data[lo : lo + self._block_size]
        # Cache only after the batch is complete. Inserting as we go would let a
        # read wider than the cache evict blocks this same call still needs.
        for index in range(first, last + 1):
            block = found.get(index)
            if block:
                self._blocks[index] = block
                self._blocks.move_to_end(index)
        while len(self._blocks) > self._blocks_cached:
            self._blocks.popitem(last=False)
        return found

    def _fetch(self, offset: int, length: int) -> bytes:
        out = bytearray()
        for index, inner, nbytes in map_range(self._table, self._total, offset, length):
            part = self._table[index]
            out += read_part(self._pool, part.remote, inner, nbytes)
        return bytes(out)


class SlicedReader(io.RawIOBase):
    """Seekable window onto another seekable file object.

    Used to expose one stored zip entry as a standalone file without copying:
    the window maps straight onto the entry's bytes inside the archive.
    """

    def __init__(self, base, start: int, size: int, *, name: str = ""):
        super().__init__()
        self._base = base
        self._start = start
        self._size = size
        self._name = name
        self._pos = 0

    @property
    def name(self) -> str:  # noqa: A003
        return self._name

    @property
    def size(self) -> int:
        return self._size

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            pos = offset
        elif whence == io.SEEK_CUR:
            pos = self._pos + offset
        elif whence == io.SEEK_END:
            pos = self._size + offset
        else:
            raise ValueError(f"invalid whence {whence}")
        if pos < 0:
            raise OSError("negative seek position")
        self._pos = pos
        return pos

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        if size is None or size < 0:
            size = self._size - self._pos
        size = max(0, min(size, self._size - self._pos))
        if size == 0:
            return b""
        self._base.seek(self._start + self._pos)
        data = self._base.read(size)
        self._pos += len(data)
        return data

    def readinto(self, buf) -> int:
        data = self.read(len(buf))
        buf[: len(data)] = data
        return len(data)

    def close(self) -> None:
        try:
            self._base.close()
        finally:
            super().close()


class SegmentReader(io.RawIOBase):
    """Read-only view of one upload segment of a local file.

    Never sees the rest of the archive, so a >512 MB zip becomes N independent
    messages. Read two ways depending on segment size (see
    ``TelegramWorker._upload_segment``): Telethon's own ``upload_file`` reads
    it sequentially for small segments; ``tgupload._PartReader`` wraps it for
    parallel random-access ``seek()``+``read()`` on bigger ones, off the event
    loop.
    """

    def __init__(self, path, start: int, size: int, *, force_big: bool = False):
        super().__init__()
        self._fh = open(path, "rb")
        self._fh.seek(start)
        self._start = start
        self._size = size
        self._pos = 0
        self.force_big = bool(force_big)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            pos = offset
        elif whence == io.SEEK_CUR:
            pos = self._pos + offset
        elif whence == io.SEEK_END:
            pos = self._size + offset
        else:
            raise ValueError(f"invalid whence {whence}")
        if pos < 0:
            raise OSError("negative seek position")
        self._pos = pos
        self._fh.seek(self._start + pos)
        return pos

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        if size is None or size < 0:
            size = self._size - self._pos
        size = max(0, min(size, self._size - self._pos))
        if size == 0:
            return b""
        data = self._fh.read(size)
        self._pos += len(data)
        return data

    def readinto(self, buf) -> int:
        data = self.read(len(buf))
        buf[: len(data)] = data
        return len(data)

    def close(self) -> None:
        try:
            self._fh.close()
        finally:
            super().close()
