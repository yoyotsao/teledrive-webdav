"""Parallel MTProto part upload.

``TelegramWorker._upload_segment`` hands each segment to explicit MTProto part
primitives. Small files use Telegram's MD5-bearing ``SaveFilePart`` protocol;
large files and split tails use ``SaveBigFilePart``. All part sends go through
the account-owned upload limiter instead of Telethon's per-client flood gate.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, List, Literal, Optional, Tuple

from upload_limiter import AdaptiveUploadLimiter, LimiterConfig

log = logging.getLogger("tgupload")

SMALL_PART_SIZE = 128 * 1024
BIG_PART_SIZE = 512 * 1024
PART_SIZE = BIG_PART_SIZE  # compatibility spelling used by split math
MAX_PARTS_PER_MESSAGE = 1000
SMALL_FILE_MAX = 10 * 1024 * 1024
BIG_FILE_THRESHOLD = SMALL_FILE_MAX
MESSAGE_MAX = MAX_PARTS_PER_MESSAGE * BIG_PART_SIZE

PART_RETRIES = 3
MAX_FLOOD_RETRIES = 10

# An upload is a multi-hour batch job: waiting out a long FLOOD_WAIT beats
# orphaning hundreds of already-accepted parts. Deliberately not tgio's
# MAX_FLOOD_WAIT (120s); that one guards interactive reads.
UPLOAD_MAX_FLOOD_WAIT = 600

_WEB_LIMITER = LimiterConfig.web_defaults()
DECREASE_FACTOR = _WEB_LIMITER.decrease_factor
INCREASE_STEP = _WEB_LIMITER.increase_step
INCREASE_INTERVAL = _WEB_LIMITER.increase_interval
CLEAN_WINDOW = _WEB_LIMITER.clean_window
BURST = _WEB_LIMITER.burst
MIN_RATE = _WEB_LIMITER.minimum


def _plan_parts(size: int, part_size: int) -> List[Tuple[int, int]]:
    """Split one segment into ``[(offset_within_segment, nbytes), ...]``."""
    if size <= 0:
        raise ValueError("segment size must be > 0")
    if part_size <= 0:
        raise ValueError("part size must be > 0")
    out: List[Tuple[int, int]] = []
    offset = 0
    while offset < size:
        n = min(part_size, size - offset)
        out.append((offset, n))
        offset += n
    if len(out) > MAX_PARTS_PER_MESSAGE:
        raise ValueError(
            f"segment of {size} bytes needs {len(out)} parts, over Telegram's "
            f"{MAX_PARTS_PER_MESSAGE}-part-per-message limit"
        )
    return out


def plan_small_parts(size: int, part_size: int = SMALL_PART_SIZE) -> List[Tuple[int, int]]:
    return _plan_parts(size, part_size)


def plan_big_parts(size: int, part_size: int = BIG_PART_SIZE) -> List[Tuple[int, int]]:
    return _plan_parts(size, part_size)


def plan_parts(size: int) -> List[Tuple[int, int]]:
    """Compatibility alias for the historic big-file planner."""
    return plan_big_parts(size)


@dataclass(frozen=True)
class ProtocolDecision:
    name: Literal["small", "album", "big", "split"]
    segments: tuple[tuple[int, int], ...]
    force_big: bool


def decide_protocol(size: int, album_eligible: bool) -> ProtocolDecision:
    """Select Telegram's wire protocol and exact message segments."""
    if size < 0:
        raise ValueError("size must be >= 0")
    if album_eligible and size <= SMALL_FILE_MAX:
        return ProtocolDecision("album", ((0, size),), False)
    if size <= SMALL_FILE_MAX:
        return ProtocolDecision("small", ((0, size),), False)
    segments = tuple(
        (offset, min(MESSAGE_MAX, size - offset))
        for offset in range(0, size, MESSAGE_MAX)
    )
    return ProtocolDecision("big" if len(segments) == 1 else "split", segments, True)


class UploadGate(AdaptiveUploadLimiter):
    """Compatibility facade for callers not yet injected with an account limiter."""

    def window_slot(self):
        return self.slot()

    def report_success(self, duration: float) -> None:
        self.success(duration)

    def report_flood(self, seconds: Optional[float]) -> None:
        self.flood(seconds)


def _flood_wait(exc: BaseException) -> Optional[tuple[float, bool]]:
    """Classify premium waits before Telethon reduces them to generic errors."""
    try:
        from telethon.errors import FloodPremiumWaitError, FloodWaitError

        premium = isinstance(exc, FloodPremiumWaitError)
        if isinstance(exc, (FloodWaitError, FloodPremiumWaitError)):
            seconds = float(exc.seconds)
            if seconds <= UPLOAD_MAX_FLOOD_WAIT:
                return seconds, premium
    except Exception:  # pragma: no cover
        pass
    return None


def _is_flood_error(exc: BaseException) -> bool:
    try:
        from telethon.errors import FloodPremiumWaitError, FloodWaitError

        return isinstance(exc, (FloodWaitError, FloodPremiumWaitError))
    except Exception:  # pragma: no cover
        return False


def _flood_seconds(exc: BaseException) -> Optional[float]:
    """Compatibility helper retained for existing callers and tests."""
    flood = _flood_wait(exc)
    return None if flood is None else flood[0]


def _sender_of(sender_or_client):
    return sender_or_client if hasattr(sender_or_client, "send") else getattr(sender_or_client, "_sender", None)


async def _sleep(limiter, seconds: float) -> None:
    sleeper = getattr(limiter, "sleep", None)
    if sleeper is None:
        await asyncio.sleep(seconds)
    else:
        await sleeper(seconds)


async def send_part(sender_of: Callable[[], object], request, gate, label: str) -> None:
    """Paced, flood-aware single part send.

    Bypasses Telethon's ``client.__call__``/``client._call`` so part RPCs use
    the account limiter and classify ``FLOOD_PREMIUM_WAIT`` before Telethon's
    generic request handling can erase the wire name.
    """
    flood_retries = 0
    while True:
        sender = sender_of()
        if sender is None:
            raise RuntimeError(f"{label}: upload client has no MTProto sender")
        start = gate.now()
        try:
            async with gate.acquire():
                await sender.send(request)
        except ConnectionError:
            raise
        except Exception as exc:
            flood = _flood_wait(exc)
            if flood is None:
                raise
            seconds, premium = flood
            flood_retries += 1
            if flood_retries > MAX_FLOOD_RETRIES:
                raise
            log.warning("%s hit FLOOD_WAIT", label)
            gate.flood(seconds, premium=premium)
            continue
        gate.success(gate.now() - start)
        return


class _PartReader:
    """Random-access part reads over an existing ``seek()``/``read()`` stream."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self._executor = ThreadPoolExecutor(max_workers=1)

    def _read_sync(self, offset: int, nbytes: int) -> bytes:
        self._stream.seek(offset)
        return self._stream.read(nbytes)

    async def read_at(self, offset: int, nbytes: int) -> bytes:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(self._executor, self._read_sync, offset, nbytes)
        if len(data) != nbytes:
            raise IOError(f"short read at offset {offset}: expected {nbytes}, got {len(data)}")
        return data

    def close(self) -> None:
        self._executor.shutdown(wait=False)

    async def __aenter__(self) -> "_PartReader":
        return self

    async def __aexit__(self, *exc) -> None:
        self.close()


async def _upload_parts(
    sender_or_client,
    limiter,
    reader: "_PartReader",
    size: int,
    *,
    parts,
    request_factory,
    workers: int,
    progress,
    collect_payloads: bool = False,
):
    """Upload one complete MTProto message under worker and account limits."""
    from telethon import helpers

    total = len(parts)
    file_id = helpers.generate_random_long()
    sent = 0
    sent_lock = asyncio.Lock()
    worker_slots = asyncio.Semaphore(max(1, int(workers)))
    payloads = [None] * total if collect_payloads else None

    async def send_one(index: int, offset: int, nbytes: int) -> None:
        nonlocal sent
        async with worker_slots:
            data = await reader.read_at(offset, nbytes)
            if payloads is not None:
                payloads[index] = data
            for attempt in range(PART_RETRIES):
                try:
                    await send_part(
                        lambda: _sender_of(sender_or_client),
                        request_factory(file_id, index, total, data),
                        limiter,
                        f"part {index}/{total}",
                    )
                    break
                except Exception as exc:
                    if _is_flood_error(exc):
                        raise
                    if attempt + 1 >= PART_RETRIES:
                        raise
                    await _sleep(limiter, 2 ** attempt)
        async with sent_lock:
            sent += len(data)
            current = min(sent, size)
        if progress:
            progress(current, size)

    tasks = [
        asyncio.ensure_future(send_one(i, offset, nbytes))
        for i, (offset, nbytes) in enumerate(parts)
    ]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return file_id, total, payloads


async def upload_small_file_parts(
    sender_or_client,
    limiter,
    reader: "_PartReader",
    size: int,
    file_name: str,
    *,
    workers: int = 4,
    progress: Optional[Callable[[int, int], None]] = None,
):
    """Use 128 KiB SaveFilePart requests with an MD5 in the InputFile handle."""
    if not 0 < size <= SMALL_FILE_MAX:
        raise ValueError("small upload size must be between 1 byte and 10 MiB")
    workers = min(4, max(1, int(workers)))
    from telethon.tl.functions.upload import SaveFilePartRequest
    from telethon.tl.types import InputFile

    file_id, total, payloads = await _upload_parts(
        sender_or_client,
        limiter,
        reader,
        size,
        parts=plan_small_parts(size),
        request_factory=lambda file_id, index, _total, data: SaveFilePartRequest(
            file_id, index, data
        ),
        workers=workers,
        progress=progress,
        collect_payloads=True,
    )
    return InputFile(
        file_id, total, file_name, hashlib.md5(b"".join(payloads)).hexdigest()
    )


async def upload_big_file_parts(
    sender_or_client,
    limiter,
    reader: "_PartReader",
    size: int,
    file_name: str,
    *,
    force_big: bool = True,
    workers: int = 12,
    progress: Optional[Callable[[int, int], None]] = None,
):
    """Use 512 KiB SaveBigFilePart requests, including a small split tail."""
    if not 0 < size <= MESSAGE_MAX:
        raise ValueError("big upload size must be between 1 byte and 500 MiB")
    if not force_big:
        raise ValueError("big upload requires force_big=True")
    from telethon.tl.functions.upload import SaveBigFilePartRequest
    from telethon.tl.types import InputFileBig

    file_id, total, _ = await _upload_parts(
        sender_or_client,
        limiter,
        reader,
        size,
        parts=plan_big_parts(size),
        request_factory=SaveBigFilePartRequest,
        workers=workers,
        progress=progress,
        collect_payloads=False,
    )
    return InputFileBig(file_id, total, file_name)


async def upload_file_parts(
    *,
    client,
    gate,
    reader: "_PartReader",
    size: int,
    file_name: str,
    progress: Optional[Callable[[int, int], None]] = None,
):
    """Compatibility wrapper for the explicit big-file primitive."""
    return await upload_big_file_parts(
        client, gate, reader, size, file_name, force_big=True, progress=progress
    )
