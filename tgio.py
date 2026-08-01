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
import io
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

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
THUMB_REQUEST_SIZE = 256 * 1024

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
BLOCKS_CACHED = 8

# How much wsgidav pulls per read() while streaming a response body. This is the
# opposite knob to BLOCK_SIZE and must not be tied to it: the block is the
# rounding on a read (small = little waste), while this is the *width* of a read
# (large = many blocks batched into one parallel fetch). Setting wsgidav's
# block_size to BLOCK_SIZE meant every streamed read was a single block on a
# single connection, and 8 MiB reads got slower even though small ones improved.
# A Range request is still served with only the bytes it asked for, so a 64 KB
# probe does not pay this width.
STREAM_BLOCK_SIZE = REQUEST_SIZE * DOWNLOAD_CONNECTIONS

# Same split boundary as the browser uploader: MAX_PARTS (1000) x CHUNK_SIZE
# (512 KB) = 500 MiB, see frontend/src/lib/gramjs.ts:502 and frontend config.ts.
# Not 512 MiB — that would exceed the browser's 1000-part-per-message ceiling.
UPLOAD_PART_KB = 512
SEGMENT_SIZE = 1000 * UPLOAD_PART_KB * 1024

DOC_CACHE_TTL = 45 * 60  # file_reference lives a few hours; refresh well before
MAX_FLOOD_WAIT = 120


# --------------------------------------------------------------------------- #
# Pure split math
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Part:
    """One Telegram message holding a contiguous slice of a logical file."""

    message_id: int
    size: int
    start: int  # offset of this part's first byte within the logical file


def build_part_table(parts: Sequence[Tuple[int, int]]) -> Tuple[List[Part], int]:
    """Turn ``[(message_id, size), ...]`` (ordered by part_index) into a table.

    Returns the table plus the logical total size. Zero-sized parts are dropped:
    they carry no bytes and would only create ambiguous offset boundaries.
    """
    table: List[Part] = []
    offset = 0
    for message_id, size in parts:
        if size <= 0:
            continue
        table.append(Part(message_id=message_id, size=size, start=offset))
        offset += size
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

    def __init__(self, api_id: int, api_hash: str, session: str, connections: int = DOWNLOAD_CONNECTIONS):
        self._api_id = api_id
        self._api_hash = api_hash
        self._session = session
        self._connections = max(1, int(connections))
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._client = None
        self._me = None
        self._docs = {}  # message_id -> (document, fetched_at)
        self._docs_lock = threading.Lock()
        self._pool: Optional[list] = None
        self._pool_lock: Optional[asyncio.Lock] = None
        self._rr = 0  # round-robin cursor over the pool, see _next_client

    # -- lifecycle -------------------------------------------------------- #

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run_loop, name="tg-loop", daemon=True)
        self._thread.start()
        self._ready.wait()
        self.run(self._connect())
        log.info("Telegram connected as %s (id=%s)", getattr(self._me, "username", None), getattr(self._me, "id", None))

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

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

    def stop(self) -> None:
        if self._loop is None:
            return
        try:
            self.run(self._disconnect_all(), timeout=15)
        except Exception:  # pragma: no cover - best effort on shutdown
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)

    async def _disconnect_all(self) -> None:
        for client in self._pool or [self._client]:
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

    # -- documents -------------------------------------------------------- #

    def get_document(self, message_id: int, refresh: bool = False):
        """Resolve a message id to its Document, with a TTL cache.

        ``file_reference`` inside the Document expires after a few hours, so both
        the TTL and the explicit ``refresh`` path exist to re-fetch it.
        """
        return self.run(self._document(message_id, refresh))

    async def _document(self, message_id: int, refresh: bool = False):
        """Async half of get_document, so batch paths can await it directly.

        Calling get_document from a coroutine already on the client loop would
        deadlock on its own run(); everything that needs a document while on the
        loop comes through here instead.
        """
        now = time.monotonic()
        if not refresh:
            with self._docs_lock:
                hit = self._docs.get(message_id)
            if hit and now - hit[1] < DOC_CACHE_TTL:
                return hit[0]
        doc = await self._fetch_document(message_id)
        with self._docs_lock:
            self._docs[message_id] = (doc, now)
        return doc

    async def _fetch_document(self, message_id: int):
        # Files live in Saved Messages ("me"), same as the browser uploader.
        messages = await self._client.get_messages("me", ids=[message_id])
        msg = messages[0] if messages else None
        if msg is None or msg.document is None:
            raise FileNotFoundError(f"Telegram message {message_id} has no document")
        return msg.document

    # -- thumbnails ------------------------------------------------------- #

    def thumbnails(self, message_ids: Sequence[int]) -> dict:
        """``{message_id: jpeg_bytes}`` for whichever messages have a thumbnail.

        Telegram already stores a small preview beside every photo and video, so
        listing a folder of previews costs a few KB per file instead of the whole
        image — measured on one pixiv folder, 120.9 MB of originals against 52 KB
        of thumbnails. Ids without a usable thumbnail are simply absent.
        """
        ids = list(dict.fromkeys(int(m) for m in message_ids))
        if not ids:
            return {}
        return self.run(self._thumbnails(ids))

    async def _thumbnails(self, message_ids: List[int]) -> dict:
        # One get_messages covers the whole batch; asking per file turned a
        # 30-file listing into 30 round trips.
        await self._prefetch_documents(message_ids)

        async def one(message_id):
            try:
                doc = await self._document(message_id)
                return message_id, await self._thumbnail_bytes(doc)
            except Exception as exc:
                log.warning("thumbnail for message %s failed: %s", message_id, exc)
                return message_id, None

        pairs = await asyncio.gather(*[one(m) for m in message_ids])
        return {message_id: data for message_id, data in pairs if data}

    def media_info(self, message_ids: Sequence[int]) -> dict:
        """``{message_id: {...}}`` describing each message's media.

        Pixel dimensions and duration ride along in the document's attributes, so
        this costs one ``get_messages`` per hundred files and downloads nothing.
        That is the whole point: Explorer reads the head of every image to work
        out its size (measured, 258 KB of a 2 MB JPEG), and those reads are what
        make a folder crawl.
        """
        ids = list(dict.fromkeys(int(m) for m in message_ids))
        if not ids:
            return {}
        return self.run(self._media_info(ids))

    async def _media_info(self, message_ids: List[int]) -> dict:
        await self._prefetch_documents(message_ids)
        out = {}
        for message_id in message_ids:
            try:
                doc = await self._document(message_id)
            except Exception as exc:
                log.warning("media info for message %s failed: %s", message_id, exc)
                continue
            info = _media_attributes(doc)
            if info:
                out[message_id] = info
        return out

    async def _prefetch_documents(self, message_ids: List[int]) -> None:
        now = time.monotonic()
        with self._docs_lock:
            wanted = [
                m for m in message_ids
                if not (self._docs.get(m) and now - self._docs[m][1] < DOC_CACHE_TTL)
            ]
        for batch in (wanted[i : i + 100] for i in range(0, len(wanted), 100)):
            try:
                messages = await self._client.get_messages("me", ids=batch)
            except Exception as exc:
                log.warning("batch document fetch failed (%s ids): %s", len(batch), exc)
                return  # per-file lookups below still work, just slower
            with self._docs_lock:
                for msg in messages or []:
                    if msg is not None and msg.document is not None:
                        self._docs[msg.id] = (msg.document, now)

    async def _thumbnail_bytes(self, doc) -> Optional[bytes]:
        """One preview, fetched over the pool rather than the control client.

        ``download_media`` would run every preview down the single control
        connection, which is the whole reason warming a folder used to crawl: the
        downloads are tiny but there are hundreds of them, and one connection
        serialises them all. Issued as plain GetFile calls they spread across the
        pool like ordinary reads do.
        """
        thumb = _best_thumb(doc)
        if thumb is None:
            return None
        from telethon.tl.functions.upload import GetFileRequest
        from telethon.tl.types import InputDocumentFileLocation

        location = InputDocumentFileLocation(
            id=doc.id,
            access_hash=doc.access_hash,
            file_reference=doc.file_reference,
            thumb_size=thumb.type,
        )
        pool = await self._download_pool()
        client = self._next_client(pool)
        result = await client(GetFileRequest(location, offset=0, limit=THUMB_REQUEST_SIZE, precise=True))
        return bytes(result.bytes)

    def invalidate_document(self, message_id: int) -> None:
        with self._docs_lock:
            self._docs.pop(message_id, None)

    # -- reading ---------------------------------------------------------- #

    def read(self, message_id: int, offset: int, length: int) -> bytes:
        """Read ``length`` bytes at ``offset`` from one message's document."""
        if length <= 0:
            return b""
        doc = self.get_document(message_id)
        try:
            return self.run(self._read(doc, offset, length))
        except Exception as exc:
            if not _is_file_reference_error(exc):
                raise
            log.warning("file_reference expired for message %s — refetching", message_id)
            doc = self.get_document(message_id, refresh=True)
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

    async def _chunk(self, client, doc, offset: int) -> bytes:
        """One REQUEST_SIZE read. Short only at end of file."""
        attempt = 0
        while True:
            try:
                async for chunk in client.iter_download(
                    doc,
                    offset=offset,
                    request_size=REQUEST_SIZE,
                    file_size=doc.size,
                ):
                    return bytes(chunk)
                return b""
            except Exception as exc:
                wait = _flood_seconds(exc)
                if wait is None or attempt >= 2:
                    raise
                attempt += 1
                log.warning("FLOOD_WAIT %ss while reading (attempt %s)", wait, attempt)
                await asyncio.sleep(wait + 1)

    # -- uploading -------------------------------------------------------- #

    def upload_segment(self, stream, size: int, file_name: str, progress=None) -> dict:
        """Upload one segment as a single Telegram document message.

        ``stream`` is a binary file object positioned at the segment start and
        limited to ``size`` bytes (see ``SegmentReader``).
        """
        return self.run(self._upload_segment(stream, size, file_name, progress), timeout=None)

    async def _upload_segment(self, stream, size: int, file_name: str, progress) -> dict:
        from telethon.tl.types import DocumentAttributeFilename

        handle = await self._client.upload_file(
            stream,
            file_size=size,
            file_name=file_name,
            part_size_kb=UPLOAD_PART_KB,
            progress_callback=progress,
        )
        msg = await self._client.send_file(
            "me",
            handle,
            force_document=True,
            attributes=[DocumentAttributeFilename(file_name)],
        )
        doc = msg.document
        if doc is None:
            raise RuntimeError("Telegram accepted the upload but returned no document")
        return {
            "message_id": msg.id,
            "file_id": str(doc.id),
            "access_hash": str(doc.access_hash),
            "size": size,
        }


def _media_attributes(doc) -> dict:
    """Pixel size and duration off a Document's attributes, or ``{}``.

    Duck-typed rather than isinstance-checked against Telethon's TL classes: the
    attribute list is a union of types that differ only in which of these fields
    they carry, and a photo's DocumentAttributeImageSize has w/h exactly like a
    video's DocumentAttributeVideo does.
    """
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


def _best_thumb(doc):
    """The largest ready-to-use thumbnail on ``doc``, or None.

    Documents also carry a PhotoStrippedSize: a ~100 byte blur that only becomes
    a JPEG after re-attaching a standard header. The PhotoSize entries are
    complete JPEGs already, and they identify themselves by having a ``size``,
    so picking the largest of those skips the stripped one without special-casing
    its type letter.
    """
    best = None
    for thumb in getattr(doc, "thumbs", None) or []:
        size = getattr(thumb, "size", None)
        if size is None:
            continue
        if best is None or size > best[1]:
            best = (thumb, size)
    return best[0] if best else None


def _is_file_reference_error(exc: BaseException) -> bool:
    try:
        from telethon.errors import FileReferenceExpiredError

        if isinstance(exc, FileReferenceExpiredError):
            return True
    except Exception:  # pragma: no cover
        pass
    return "FILE_REFERENCE" in str(exc).upper()


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
        reader,
        parts: Sequence[Tuple[int, int]],
        *,
        name: str = "",
        block_size: int = BLOCK_SIZE,
        blocks_cached: int = BLOCKS_CACHED,
    ):
        super().__init__()
        self._reader = reader
        self._table, self._total = build_part_table(parts)
        self._name = name
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
            out += self._reader.read(part.message_id, inner, nbytes)
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

    Telethon's ``upload_file`` reads sequentially from this and never sees the
    rest of the archive, so a >512 MB zip becomes N independent messages.
    """

    def __init__(self, path, start: int, size: int):
        super().__init__()
        self._fh = open(path, "rb")
        self._fh.seek(start)
        self._start = start
        self._size = size
        self._pos = 0

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
