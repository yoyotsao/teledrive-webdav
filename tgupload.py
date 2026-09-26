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
import math
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Callable, List, Literal, Optional, Protocol, Tuple

from upload_limiter import AdaptiveUploadLimiter, LimiterConfig, LimiterSnapshot
from transfer_models import UploadRpcToken

log = logging.getLogger("tgupload")

SMALL_PART_SIZE = 128 * 1024
BIG_PART_SIZE = 512 * 1024
PART_SIZE = BIG_PART_SIZE  # compatibility spelling used by split math
MAX_PARTS_PER_MESSAGE = 1000
SMALL_FILE_MAX = 10 * 1024 * 1024
BIG_FILE_THRESHOLD = SMALL_FILE_MAX
MESSAGE_MAX = MAX_PARTS_PER_MESSAGE * BIG_PART_SIZE

PART_RETRIES = 3

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
    """Return Telegram's requested upload wait, without imposing a local cap."""
    try:
        from telethon.errors import FloodPremiumWaitError, FloodWaitError

        premium = isinstance(exc, FloodPremiumWaitError)
        if isinstance(exc, (FloodWaitError, FloodPremiumWaitError)):
            seconds = float(exc.seconds)
            if math.isfinite(seconds) and seconds >= 0:
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


class AttemptRevoked(RuntimeError):
    """The scheduler revoked this upload attempt before the next RPC committed."""


class UploadObserver(Protocol):
    def request_started(self, part_index: int, nbytes: int) -> UploadRpcToken:
        ...

    def request_succeeded(self, token: UploadRpcToken, nbytes: int) -> None:
        ...

    def premium_flood(self, seconds: float, pacer_snapshot: LimiterSnapshot) -> None:
        ...

    def request_settled(self, token: UploadRpcToken) -> None:
        ...

    def late_request_succeeded(self, token: UploadRpcToken, nbytes: int) -> None:
        ...


class RevokeHandle:
    def __init__(self, loop: asyncio.AbstractEventLoop, event: asyncio.Event):
        self._loop = loop
        self.worker_event = event

    @classmethod
    def create_on_worker_loop(cls) -> "RevokeHandle":
        return cls(asyncio.get_running_loop(), asyncio.Event())

    def revoke(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self.worker_event.set)
        except RuntimeError:
            # Test/during-shutdown loops may already be closed. At that point
            # there is no loop-owned callback left to wake, but setting the
            # flag keeps cleanup idempotent and must not replace the real error.
            self.worker_event.set()


async def _await_cleanup(awaitable):
    cleanup = asyncio.ensure_future(awaitable)
    interrupted = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            interrupted = True
    return cleanup.result(), interrupted


async def _wait_or_revoke(awaitable, revoked: Optional[asyncio.Event]):
    if revoked is None:
        return await awaitable
    work = asyncio.ensure_future(awaitable)
    cancellation = asyncio.create_task(revoked.wait())
    interrupted = False
    try:
        await asyncio.wait({work, cancellation}, return_when=asyncio.FIRST_COMPLETED)
        if work.done():
            return work.result()
        work.cancel()
        _, interrupted = await _await_cleanup(asyncio.gather(work, return_exceptions=True))
        if not work.cancelled() and work.exception() is None:
            return work.result()
        raise AttemptRevoked()
    finally:
        cancellation.cancel()
        if not work.done():
            work.cancel()
        _, cleanup_interrupted = await _await_cleanup(
            asyncio.gather(work, cancellation, return_exceptions=True)
        )
        if interrupted or cleanup_interrupted:
            raise asyncio.CancelledError()


@asynccontextmanager
async def _cancelable_context(cm, revoked: Optional[asyncio.Event]):
    enter = asyncio.ensure_future(cm.__aenter__())
    entered = False
    interrupted = False
    try:
        value = await _wait_or_revoke(enter, revoked)
        entered = True
        if revoked is not None and revoked.is_set():
            raise AttemptRevoked()
        yield value
    finally:
        exit_args = sys.exc_info()
        if not enter.done():
            enter.cancel()
        _, interrupted = await _await_cleanup(asyncio.gather(enter, return_exceptions=True))
        if not entered and not enter.cancelled() and enter.exception() is None:
            entered = True
        if entered:
            _, exit_interrupted = await _await_cleanup(cm.__aexit__(*exit_args))
            interrupted = interrupted or exit_interrupted
        if interrupted:
            raise asyncio.CancelledError()


def _observe_late_rpc(rpc, observer, token, nbytes: int) -> None:
    if rpc is None:
        return

    def settled(future) -> None:
        if (
            not future.cancelled()
            and future.exception() is None
            and observer is not None
            and token is not None
        ):
            observer.late_request_succeeded(token, nbytes)

    rpc.add_done_callback(settled)


def _sender_of(sender_or_client):
    return sender_or_client if hasattr(sender_or_client, "send") else getattr(sender_or_client, "_sender", None)


async def _sleep(limiter, seconds: float) -> None:
    sleeper = getattr(limiter, "sleep", None)
    if sleeper is None:
        await asyncio.sleep(seconds)
    else:
        await sleeper(seconds)


async def send_part(
    sender_of: Callable[[], object],
    request,
    gate,
    label: str,
    *,
    part_index: int = 0,
    nbytes: int = 0,
    observer: Optional[UploadObserver] = None,
    revoked: Optional[asyncio.Event] = None,
    rpc_timeout: float = 120.0,
) -> None:
    """Paced, flood-aware single part send with a revoke-safe commit boundary."""
    while True:
        # New account limiters split admission into a capacity slot plus pacing so
        # revocation can abort between those phases.  Older injected/test limiters
        # expose one acquire() context that already owns both responsibilities.
        # Keep both contracts valid while preserving the same commit boundary:
        # once request_started() succeeds, the RPC is sent even if revoke races in.
        slot = getattr(gate, "slot", None)
        pace = getattr(gate, "pace", None)
        if callable(slot) and callable(pace):
            admission = slot()
            legacy_admission = False
        else:
            acquire = getattr(gate, "acquire", None)
            if not callable(acquire):
                raise AttributeError("upload limiter has no admission context")
            admission = acquire()
            legacy_admission = True
            start = gate.now()

        async with _cancelable_context(admission, revoked):
            if not legacy_admission:
                start = gate.now()
                await _wait_or_revoke(pace(), revoked)
            if revoked is not None and revoked.is_set():
                raise AttemptRevoked()
            sender = sender_of()
            if sender is None:
                raise RuntimeError(f"{label}: upload client has no MTProto sender")
            token = observer.request_started(part_index, nbytes) if observer else None
            rpc = None
            try:
                # Successful request_started() is the point of no return. Do not
                # re-check revoke between this token and invoking sender.send().
                rpc = asyncio.ensure_future(sender.send(request))
                marker = getattr(gate, "mark_send_started", None)
                if marker is not None:
                    marker()
                await asyncio.wait_for(asyncio.shield(rpc), timeout=rpc_timeout)
                if observer is not None:
                    observer.request_succeeded(token, nbytes)
            except asyncio.TimeoutError:
                _observe_late_rpc(rpc, observer, token, nbytes)
                raise
            except asyncio.CancelledError:
                _observe_late_rpc(rpc, observer, token, nbytes)
                raise
            except Exception as exc:
                flood = _flood_wait(exc)
                if flood is None:
                    raise
                seconds, premium = flood
                log.warning("%s hit FLOOD_WAIT; retrying after %.1fs", label, seconds)
                gate.flood(seconds, premium=premium)
                if premium and observer is not None:
                    snapshot = getattr(gate, "snapshot", None)
                    if callable(snapshot):
                        observer.premium_flood(seconds, snapshot())
                continue
            finally:
                if observer is not None and token is not None:
                    observer.request_settled(token)
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
    observer: Optional[UploadObserver] = None,
    revoked: Optional[asyncio.Event] = None,
    rpc_timeout: float = 120.0,
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
        async with _cancelable_context(worker_slots, revoked):
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
                        part_index=index,
                        nbytes=nbytes,
                        observer=observer,
                        revoked=revoked,
                        rpc_timeout=rpc_timeout,
                    )
                    break
                except Exception as exc:
                    if _is_flood_error(exc):
                        raise
                    if attempt + 1 >= PART_RETRIES:
                        raise
                    await _wait_or_revoke(_sleep(limiter, 2 ** attempt), revoked)
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
        if revoked is not None:
            revoked.set()
            await asyncio.gather(*tasks, return_exceptions=True)
        else:
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
    observer: Optional[UploadObserver] = None,
    revoked: Optional[asyncio.Event] = None,
    rpc_timeout: float = 120.0,
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
        observer=observer,
        revoked=revoked,
        rpc_timeout=rpc_timeout,
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
    observer: Optional[UploadObserver] = None,
    revoked: Optional[asyncio.Event] = None,
    rpc_timeout: float = 120.0,
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
        observer=observer,
        revoked=revoked,
        rpc_timeout=rpc_timeout,
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
