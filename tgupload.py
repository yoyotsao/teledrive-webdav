"""Parallel MTProto part upload.

Today's upload (``TelegramWorker._upload_segment`` in tgio.py, for big
segments) hands each segment to Telethon's ``client.upload_file``, which sends
one 512 KiB ``SaveBigFilePart`` at a time and waits for the round trip before
starting the next. The sibling web app (``D:\\python\\teledrive``,
``frontend/src/lib/gramjs.ts``) computes part indices itself and fans requests
out in parallel instead — this module mirrors that mechanic, but not its
tuning.

The web app's rate pacer (``frontend/src/lib/adaptiveRateLimiter.ts``) is a
pure rate controller decoupled from RTT: its throughput is ``rate`` parts/s
regardless of how many connections or how fast the network is, and it is
observed live latched at its floor (0.5 parts/s, ceiling 1.0, escalated,
flood #3010+) with no way back up except a probe that needs 60s of no floods
— unreachable while floods keep recurring. Porting that machinery verbatim
would import a state machine that is currently stuck in its worst state.

``UploadGate`` below never introduces a rate cap until a flood actually
happens, and then sizes it from the throughput measured right before the
flood, not a fixed guess. With no flood, upload throughput is bounded only by
``window`` (how many parts may be in flight), which starts at its maximum and
only ever shrinks — so the worst case (a flood on every batch) degrades to
one part in flight at a time, i.e. today's sequential behaviour, never below
it. Ceiling memory, probing, escalation, and persistence are deliberately not
ported: those solve a browser's problem (many files across a tab's lifetime,
page reloads, no memory of yesterday), and persisting a learned rate here
would let two independent controllers on the same account-level flood bucket
(browser tab + bridge) ratchet each other's rate down forever.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Callable, List, Optional, Tuple

log = logging.getLogger("tgupload")

PART_SIZE = 512 * 1024
MAX_PARTS_PER_MESSAGE = 1000
BIG_FILE_THRESHOLD = 10 * 1024 * 1024  # telethon client/uploads.py:701

PART_RETRIES = 3
MAX_FLOOD_RETRIES = 10
MAX_DISCONNECT_RETRIES = 30

# An upload is a multi-hour batch job: waiting out a long FLOOD_WAIT beats
# orphaning hundreds of already-accepted parts. Deliberately not tgio's
# MAX_FLOOD_WAIT (120s) — that one guards an interactive read, which should
# fail fast instead of stalling Explorer.
UPLOAD_MAX_FLOOD_WAIT = 600

DECREASE_FACTOR = 0.5
INCREASE_STEP = 0.5
INCREASE_INTERVAL = 10.0
CLEAN_WINDOW = 20.0
BURST = 2.0
MIN_RATE = 0.25


def plan_parts(size: int) -> List[Tuple[int, int]]:
    """Split one segment into ``[(offset_within_segment, nbytes), ...]``."""
    if size <= 0:
        raise ValueError("segment size must be > 0")
    out: List[Tuple[int, int]] = []
    offset = 0
    while offset < size:
        n = min(PART_SIZE, size - offset)
        out.append((offset, n))
        offset += n
    if len(out) > MAX_PARTS_PER_MESSAGE:
        raise ValueError(
            f"segment of {size} bytes needs {len(out)} parts, over Telegram's "
            f"{MAX_PARTS_PER_MESSAGE}-part-per-message limit"
        )
    return out


class UploadGate:
    """Admission control shared by every part of one upload.

    Two knobs:

    * ``window`` — how many parts may be in flight at once, acquired via
      :meth:`window_slot`. Starts at ``max_window`` and only shrinks, floor 1.
    * ``rate`` — a parts/s ceiling applied by :meth:`pace`. ``None`` until the
      first flood — there is nothing to cap when nothing has gone wrong. Once
      set, its value is derived from the throughput actually measured right
      before the flood (``window / rtt_ewma`` — Little's law), not a fixed
      guess. If it later ramps back up to where ``window`` alone was already
      the binding constraint even at full window, it is torn down to
      ``None`` instead of kept as dead weight.
    """

    def __init__(
        self,
        max_window: int = 12,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Optional[Callable[[float], "asyncio.Future"]] = None,
    ) -> None:
        self._max_window = max(1, int(max_window))
        self._window = self._max_window
        self._in_flight = 0
        self._rate: Optional[float] = None
        self._rtt_ewma: Optional[float] = None
        self._penalty_until = 0.0
        self._next_slot_at = 0.0
        self._last_flood_at: Optional[float] = None
        self._last_increase_at = 0.0
        self.floods = 0
        self._clock = clock
        self._sleep = sleeper or asyncio.sleep
        self._cond: Optional[asyncio.Condition] = None

    def now(self) -> float:
        return self._clock()

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await self._sleep(seconds)

    def _condition(self) -> asyncio.Condition:
        # Built lazily so a Gate can be constructed off-loop; the Condition
        # binds to whichever loop first calls window_slot().
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    @asynccontextmanager
    async def window_slot(self):
        """Held for one part's entire lifetime (read + all its retries), so
        the number of buffered-but-unsent part payloads is bounded by
        ``window``, not by how many tasks happen to have been created."""
        cond = self._condition()
        async with cond:
            while self._in_flight >= self._window:
                await cond.wait()
            self._in_flight += 1
        try:
            yield
        finally:
            async with cond:
                self._in_flight -= 1
                cond.notify()

    async def pace(self) -> None:
        """Blocks until the rate cap (if any) allows one more send attempt.

        Virtual-time slot reservation: every caller reserves the next slot
        synchronously before sleeping, so concurrent callers are serialised
        ~1/rate apart and can never all pass through at once.
        """
        if self._rate is None:
            return
        now = self._clock()
        interval = 1.0 / self._rate
        earliest = max(now, self._penalty_until)
        scheduled = max(self._next_slot_at, earliest - BURST * interval)
        self._next_slot_at = scheduled + interval
        delay = scheduled - now
        if delay > 0:
            await self.sleep(delay)

    def report_success(self, duration: float) -> None:
        if duration > 0:
            self._rtt_ewma = duration if self._rtt_ewma is None else 0.8 * self._rtt_ewma + 0.2 * duration
        now = self._clock()
        if self._last_flood_at is not None and now - self._last_flood_at < CLEAN_WINDOW:
            return
        if now - self._last_increase_at < INCREASE_INTERVAL:
            return
        self._last_increase_at = now
        if self._rate is not None:
            self._rate += INCREASE_STEP
            if self._rtt_ewma and self._rate >= self._max_window / self._rtt_ewma:
                log.info("upload rate cap lifted at %.2f parts/s (window is now the limit)", self._rate)
                self._rate = None
        elif self._window < self._max_window:
            self._window += 1
            log.info("upload window -> %d", self._window)

    def report_flood(self, seconds: Optional[float]) -> None:
        self.floods += 1
        now = self._clock()
        self._last_flood_at = now
        wait = seconds if seconds and seconds > 0 else 10.0
        # Distinct-event guard: N parts failing on the very same server flood
        # all land here, but only the first (before penalty_until is pushed
        # forward) actually cuts window/rate — otherwise one flood would be
        # cut by however many parts happened to be in flight.
        if now >= self._penalty_until:
            if self._rtt_ewma:
                achieved = self._window / self._rtt_ewma
            else:
                achieved = self._rate if self._rate is not None else float(self._window)
            base = self._rate if self._rate is not None else achieved
            self._rate = max(MIN_RATE, base * DECREASE_FACTOR)
            self._window = max(1, self._window // 2)
            log.warning(
                "FLOOD_WAIT #%d: wait=%.0fs window->%d rate->%.2f parts/s",
                self.floods, wait, self._window, self._rate,
            )
        self._penalty_until = max(self._penalty_until, now + wait + 1.0)
        self._next_slot_at = max(self._next_slot_at, self._penalty_until)

    def stats(self) -> dict:
        return {"window": self._window, "rate": self._rate, "floods": self.floods}


def _flood_seconds(exc: BaseException) -> Optional[float]:
    """Return the wait in seconds if ``exc`` is a tolerable FLOOD_WAIT, else None."""
    try:
        from telethon.errors import FloodWaitError

        if isinstance(exc, FloodWaitError) and exc.seconds <= UPLOAD_MAX_FLOOD_WAIT:
            return float(exc.seconds)
    except Exception:  # pragma: no cover
        pass
    return None


async def send_part(sender_of: Callable[[], object], request, gate: UploadGate, label: str) -> None:
    """Paced, flood-aware single part send.

    Bypasses ``client.__call__``/``client._call`` on purpose: Telethon 1.44's
    ``__call__`` accepts a per-call ``flood_sleep_threshold`` override and
    silently drops it — it forwards to ``_call`` without it
    (``telethon/client/users.py:29-30``, verified against the installed
    package) — so there is no supported way to get "raise instead of
    silently sleep" behaviour on a shared client without going around
    ``_call`` entirely. Going around it also skips ``_call``'s own
    request-type-keyed flood gate (``_flood_waited_requests``, which would
    make every remaining part of a flooded segment fail without touching the
    network) and its blind ``sleep(2)``-and-retry on ``ServerError``, both of
    which would multiply badly across several concurrent parts.

    Does not catch ``asyncio.CancelledError``: Telethon cancels pending
    futures outright on a hard disconnect
    (``telethon/network/mtprotosender.py:MTProtoSender._disconnect``,
    ``state.future.cancel()``), which is indistinguishable in type from this
    task's own cancellation. Swallowing it here would also swallow the
    sibling-cancellation ``upload_file_parts`` relies on to stop a segment
    that can no longer be committed. Letting it propagate means a disconnect
    fails this part immediately instead of retrying in place; the
    segment-level retry in gamestage picks it up once the client has had
    time to reconnect.
    """
    flood_retries = 0
    disconnect_retries = 0
    while True:
        await gate.pace()
        sender = sender_of()
        if sender is None:
            raise RuntimeError(f"{label}: upload client has no MTProto sender")
        start = gate.now()
        try:
            await sender.send(request)
        except ConnectionError:
            disconnect_retries += 1
            if disconnect_retries > MAX_DISCONNECT_RETRIES:
                raise
            await gate.sleep(1.0)
            continue
        except Exception as exc:
            seconds = _flood_seconds(exc)
            if seconds is None:
                raise
            flood_retries += 1
            if flood_retries > MAX_FLOOD_RETRIES:
                raise
            log.warning("%s hit FLOOD_WAIT", label)
            gate.report_flood(seconds)
            continue
        gate.report_success(gate.now() - start)
        return


class _PartReader:
    """Random-access 512 KiB reads over an existing ``seek()``+``read()``
    stream (a :class:`tgio.SegmentReader`), off the event loop.

    A single-worker executor doubles as the read lock — ``seek()``+``read()``
    is two syscalls and not atomic — and it keeps every disk read off
    ``tg-loop``, which also serves every rclone range read and ``/rpc/*``
    call. Does not open or close the underlying stream; that stays the
    caller's responsibility, matching how ``SegmentReader`` is used today.
    """

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


async def upload_file_parts(
    *,
    client,
    gate: UploadGate,
    reader: "_PartReader",
    size: int,
    file_name: str,
    progress: Optional[Callable[[int, int], None]] = None,
):
    """Upload one segment's bytes as parallel ``SaveBigFilePart`` requests.

    Only for segments over ``BIG_FILE_THRESHOLD`` — the caller keeps
    Telethon's own sequential ``client.upload_file`` for anything smaller,
    where a single round trip per part means parallelism buys nothing and
    Telethon's MD5 verification for small files is worth keeping untouched.

    Returns an ``InputFileBig`` handle; does not send a message — the caller
    commits it with ``client.send_file``.
    """
    from telethon import helpers
    from telethon.tl.functions.upload import SaveBigFilePartRequest
    from telethon.tl.types import InputFileBig

    parts = plan_parts(size)
    total = len(parts)
    file_id = helpers.generate_random_long()

    def sender_of():
        return getattr(client, "_sender", None)

    sent = 0

    async def send_one(index: int, part_offset: int, nbytes: int) -> None:
        nonlocal sent
        async with gate.window_slot():
            data = await reader.read_at(part_offset, nbytes)
            for attempt in range(PART_RETRIES):
                try:
                    request = SaveBigFilePartRequest(file_id, index, total, data)
                    await send_part(sender_of, request, gate, f"part {index}/{total}")
                    break
                except Exception:
                    if attempt + 1 >= PART_RETRIES:
                        raise
                    await gate.sleep(2 ** attempt)
        sent += len(data)
        if progress:
            progress(min(sent, size), size)

    tasks = [asyncio.ensure_future(send_one(i, off, n)) for i, (off, n) in enumerate(parts)]
    try:
        await asyncio.gather(*tasks)
    except Exception:
        # gather() propagates the first failure but does not cancel the
        # others — left alone they would keep consuming gate slots and
        # uplink for a segment that can no longer be committed.
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    return InputFileBig(file_id, total, file_name)
