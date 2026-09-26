"""Account-routed transfers and exact, independently settled registration."""

from __future__ import annotations

import logging
import mimetypes
import os
import re
import time
from collections import deque
from contextlib import contextmanager
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from threading import BoundedSemaphore, Lock
from typing import Callable, Dict, Mapping, Optional, Sequence
from uuid import uuid4

from config import ext_path
from tgio import SegmentReader
from tgupload import AttemptRevoked, SMALL_FILE_MAX, decide_protocol
from transfer_models import AttemptLease, QueueStage, TransferRequest, TransferResult, UploadedPart, UploadRpcToken
from upload_limiter import MessageTokenBucket
from segment_scheduler import SchedulerExecutionError, SegmentDescriptor, SegmentScheduler


log = logging.getLogger("upload_engine")


class CoverageError(RuntimeError):
    """Metadata parts do not describe exactly the logical file bytes."""


@dataclass
class TransferMetrics:
    """Where one logical file's wall clock went, in the web client's terms.

    The stage numbers are summed work, not disjoint slices of ``total_ms``:
    a split file's segments upload concurrently, so ``upload_ms`` can exceed
    the wall clock the whole transfer took. That is the useful reading -- it
    says how much of the account's budget the file spent -- but it does mean
    the stages are not expected to add up to the total.
    """

    protocol: str = ""
    bytes: int = 0
    parts: int = 0
    hash_ms: float = 0.0
    check_ms: float = 0.0
    thumb_ms: float = 0.0
    slot_ms: float = 0.0
    upload_ms: float = 0.0
    message_ms: float = 0.0
    register_ms: float = 0.0
    total_ms: float = 0.0
    account_ids: tuple = ()
    logical_uploaded_bytes: int = 0
    physical_transferred_bytes: int = 0
    migration_overhead_bytes: int = 0
    migration_count: int = 0
    rate: float = 0.0
    ceiling: Optional[float] = None
    started: float = field(default_factory=time.monotonic)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def add(self, stage: str, seconds: float) -> None:
        with self._lock:
            setattr(self, stage, getattr(self, stage) + seconds * 1000.0)

    def observe_limiter(self, runtime) -> None:
        stats = getattr(getattr(runtime, "chunk_limiter", None), "stats", None)
        if not callable(stats):
            return
        current = dict(stats())
        with self._lock:
            self.rate = float(current.get("rate") or 0.0)
            self.ceiling = current.get("ceiling")


@contextmanager
def _timed(metrics: Optional[TransferMetrics], stage: str):
    if metrics is None:
        yield
        return
    started = time.monotonic()
    try:
        yield
    finally:
        metrics.add(stage, time.monotonic() - started)


# Anything that could carry an auth_key or a drive JWT onwards. Failures are
# written into durable queue state and into bridge.log, and both outlive the
# process, so the redaction has to happen before the text is stored -- not at
# the point somebody reads it.
_SECRETS = (
    (re.compile(r"(session\s*[=:]\s*)\S+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(authorization\s*:\s*)\S+(?:\s+\S+)?", re.IGNORECASE), r"\1***"),
    (re.compile(r"(bearer\s+)\S+", re.IGNORECASE), r"\1***"),
    (re.compile(r"\bey[A-Za-z0-9_\-]*\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), "***"),
)


def redact(value) -> str:
    """``Type: message`` with every credential-shaped run replaced."""
    text = value if isinstance(value, str) else f"{type(value).__name__}: {value}"
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


# mimetypes on Windows answers out of HKCR, so the same extension gets a
# different name on different machines: .zip is "application/x-zip-compressed"
# here and "application/zip" on the box that wrote the row the dedup check will
# match. That is not cosmetic -- album_eligible() below keys off the "image/"
# and "video/" prefixes and singles out image/webp, so a machine-local answer
# silently changes which protocol a file is uploaded with. The extensions that
# decide anything are pinned to their IANA names, which is also what the web
# client sends.
_MIME_OVERRIDES = {
    ".zip": "application/zip",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".webm": "video/webm",
    ".mov": "video/quicktime", ".avi": "video/x-msvideo", ".m4v": "video/x-m4v",
}


def guess_mime_type(name: str) -> str:
    """The mime type a row is registered with, independent of this machine."""
    suffix = os.path.splitext(name)[1].lower()
    if suffix in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[suffix]
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def album_eligible(mime_type: str, size: int) -> bool:
    return size <= SMALL_FILE_MAX and mime_type != "image/webp" and (
        mime_type.startswith("image/") or mime_type.startswith("video/")
    )


def assert_parts_cover_file(parts: Sequence[UploadedPart], size: int) -> None:
    """Reject metadata that would advertise too few or too many bytes."""
    if sorted(part.index for part in parts) != list(range(len(parts))):
        raise CoverageError("uploaded part indices must be contiguous from zero")
    if any(part.size < 0 for part in parts):
        raise CoverageError("uploaded parts cannot have negative sizes")
    total = sum(part.size for part in parts)
    if total != size:
        raise CoverageError(f"uploaded parts cover {total} bytes, expected {size}")


def _row_sort_key(row: Mapping[str, object]) -> tuple[str, str, str, str, str]:
    """A stable winner for aliases, independent of backend result order."""
    return (
        str(row.get("file_id") or ""),
        str(row.get("access_hash") or ""),
        str(row.get("telegram_user_id") or 0),
        str(row.get("telegram_message_id") or ""),
        str(row.get("mime_type") or ""),
    )


def _part_from_row(row: Mapping[str, object], index: int) -> UploadedPart | None:
    message_id = row.get("telegram_message_id")
    if message_id is None:
        return None
    try:
        return UploadedPart(
            index=index,
            message_id=int(message_id),
            file_id=str(row.get("file_id") or ""),
            access_hash=(str(row["access_hash"]) if row.get("access_hash") is not None else None),
            size=int(row.get("filesize") or 0),
            telegram_user_id=int(row.get("telegram_user_id") or 0),
            has_thumbnail=bool(row.get("has_thumbnail")),
        )
    except (TypeError, ValueError):
        return None


def _canonical_split_candidate(rows: Sequence[Mapping[str, object]], original_size: int) -> list[UploadedPart]:
    """Return one exact group, collapsing registration aliases along the way."""
    by_index: Dict[int, list[Mapping[str, object]]] = {}
    for row in rows:
        try:
            index = int(row.get("part_index"))
        except (TypeError, ValueError):
            return []
        if index < 0:
            return []
        by_index.setdefault(index, []).append(row)

    if not by_index or sorted(by_index) != list(range(len(by_index))):
        return []

    parts: list[UploadedPart] = []
    seen_messages: set[tuple[int, int]] = set()
    for index in range(len(by_index)):
        # Multiple DB rows can alias the same Telegram message.  Choosing the
        # lexically first full identity makes that collapse deterministic.
        candidates = sorted(by_index[index], key=_row_sort_key)
        part = _part_from_row(candidates[0], index)
        if part is None or part.size < 0:
            return []
        identity = (part.telegram_user_id, part.message_id)
        if identity in seen_messages:
            return []
        seen_messages.add(identity)
        parts.append(part)

    try:
        assert_parts_cover_file(parts, original_size)
    except CoverageError:
        return []
    return parts


def canonical_existing_parts(rows: Sequence[Mapping[str, object]], original_size: int) -> list[UploadedPart]:
    """Select one exact, deterministic prior upload from ``check-hash`` rows.

    ``check-hash`` returns aliases registered under other names as well as the
    original upload.  A split candidate is valid only when it owns every index
    from zero and its non-duplicated Telegram segments total exactly the source
    file length. Unsplit rows are considered only after valid split groups.
    """
    if original_size < 0:
        return []

    groups: Dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        group = row.get("split_group_id")
        if bool(row.get("is_split_file")) and group:
            groups.setdefault(str(group), []).append(row)

    for group in sorted(groups):
        parts = _canonical_split_candidate(groups[group], original_size)
        if parts:
            return parts

    # Each unsplit metadata row is a one-part candidate.  Multiple aliases of
    # the same message collapse before selection; unlike split groups there is
    # no part index to infer.
    singles: Dict[tuple[int, int], Mapping[str, object]] = {}
    for row in rows:
        if bool(row.get("is_split_file")):
            continue
        part = _part_from_row(row, 0)
        if part is None or part.size != original_size:
            continue
        identity = (part.telegram_user_id, part.message_id)
        previous = singles.get(identity)
        if previous is None or _row_sort_key(row) < _row_sort_key(previous):
            singles[identity] = row

    candidates = sorted(singles.values(), key=_row_sort_key)
    if not candidates:
        return []
    chosen = _part_from_row(candidates[0], 0)
    return [chosen] if chosen is not None else []


class FingerprintClaims:
    """Future-backed claims shared by the due batch that owns this instance."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._claims: Dict[str, Future[list[UploadedPart]]] = {}

    def _claim(self, key: str) -> tuple[Future[list[UploadedPart]], bool]:
        with self._lock:
            future = self._claims.get(key)
            if future is not None:
                return future, False
            future = Future()
            self._claims[key] = future
            future.add_done_callback(lambda completed: self._release_failed(key, completed))
            return future, True

    def _release_failed(self, key, future):
        if future.cancelled() or future.exception() is not None:
            with self._lock:
                if self._claims.get(key) is future:
                    self._claims.pop(key, None)

    def run(self, fingerprint: str, producer: Callable[[], list[UploadedPart]]) -> list[UploadedPart]:
        future, owner = self._claim(fingerprint)
        if owner:
            try:
                future.set_result(producer())
            except BaseException as exc:
                future.set_exception(exc)
        return future.result()


class AlbumQueue:
    """One account's prepared documents; flushes settle every item's future."""

    def __init__(self, runtime, fallback, *, batch=10, timeout=60):
        self.runtime = runtime
        self.fallback = fallback
        self.batch = max(1, int(batch))
        self.timeout = timeout
        self._pending = []
        self._lock = Lock()

    def add(self, item, future=None):
        if item.telegram_user_id != self.runtime.telegram_user_id:
            raise ValueError("prepared item belongs to another Telegram account")
        future = future if future is not None else Future()
        with self._lock:
            self._pending.append((item, future))
            batch = self._pending if len(self._pending) == self.batch else []
            if batch:
                self._pending = []
        if batch:
            self._send(batch)
        return future

    def flush(self):
        with self._lock:
            batch, self._pending = self._pending, []
        if batch:
            self._send(batch)

    def _send(self, batch):
        items = [item for item, _ in batch]
        try:
            parts = self.runtime.worker.send_album(
                items, timeout=self.timeout, message_limiter=self.runtime.message_limiter,
            )
            if len(parts) != len(items):
                raise CoverageError("album did not return every prepared item")
            for item, part in zip(items, parts):
                assert_parts_cover_file([part], item.size)
                if part.telegram_user_id != item.telegram_user_id or part.file_id != item.document_id:
                    raise CoverageError("album changed a prepared document identity")
        except Exception:
            # A malformed response invalidates the whole batch, just like an
            # RPC error. Each fallback reopens its own source and settles alone.
            for item, future in batch:
                try:
                    part = self.fallback(self.runtime, item)
                    assert_parts_cover_file([part], item.size)
                except Exception as exc:
                    future.set_exception(exc)
                else:
                    future.set_result([part])
        else:
            for (_, future), part in zip(batch, parts):
                future.set_result([part])


class AccountUploadObserver:
    """Account-wide telemetry for byte uploads that are not migration candidates."""

    def __init__(self, pool, account_id: int, task_id: str):
        self.pool = pool
        self.lease = AttemptLease(str(task_id), 1, int(account_id))
        self._sequence = 0
        self._lock = Lock()

    def request_started(self, part_index: int, nbytes: int) -> UploadRpcToken:
        with self._lock:
            self._sequence += 1
            token = UploadRpcToken(
                self.lease.task_id, self.lease.attempt_id, self.lease.account_id,
                int(part_index), self._sequence,
            )
        self.pool.activity.request_started(token)
        return token

    def request_succeeded(self, token: UploadRpcToken, nbytes: int) -> None:
        self.pool.speed_tracker.record_physical(token, nbytes)
        self.pool.speed_tracker.record_effective(self.lease, token.part_index, nbytes)

    def premium_flood(self, seconds: float, pacer_snapshot) -> None:
        self.pool.speed_tracker.close_premium_flood_cycle(self.lease, seconds)

    def request_settled(self, token: UploadRpcToken) -> None:
        self.pool.activity.request_settled(token)

    def late_request_succeeded(self, token: UploadRpcToken, nbytes: int) -> None:
        self.pool.speed_tracker.record_physical(token, nbytes)


class _LegacyUploadLease:
    """Adapt the pre-owned-lease pool API for non-production test doubles.

    Real ``AccountPool`` instances always expose owned leases plus activity
    telemetry.  A few integration rigs intentionally keep the older
    ``acquire_upload()`` context-manager contract; preserving that contract
    keeps those end-to-end tests focused on WebDAV behavior without weakening
    the production scheduler invariants.
    """

    def __init__(self, runtime, work_id: str, context_manager):
        self.runtime = runtime
        self.work_id = str(work_id)
        self._context_manager = context_manager
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._context_manager.__exit__(None, None, None)


class SchedulerUploadObserver:
    """Bind worker-level part telemetry to one immutable scheduler attempt."""

    def __init__(self, scheduler: SegmentScheduler, lease: AttemptLease):
        self.scheduler = scheduler
        self.lease = lease

    def request_started(self, part_index: int, nbytes: int) -> UploadRpcToken:
        return self.scheduler.begin_request(self.lease, part_index)

    def request_succeeded(self, token: UploadRpcToken, nbytes: int) -> None:
        self.scheduler.physical_success(token, nbytes)
        self.scheduler.part_succeeded(self.lease, token.part_index, nbytes)

    def premium_flood(self, seconds: float, pacer_snapshot) -> None:
        self.scheduler.notify_premium_flood(self.lease, seconds, pacer_snapshot)

    def request_settled(self, token: UploadRpcToken) -> None:
        self.scheduler.request_settled(token)

    def late_request_succeeded(self, token: UploadRpcToken, nbytes: int) -> None:
        self.scheduler.physical_success(token, nbytes)


class UploadEngine:
    """Transfer files; callers register results before deleting sources.

    Share one engine within a due batch to share fingerprint claims. Account
    admission belongs to the pool, and registration admission to this engine.
    """

    def __init__(
        self, api, pool, *, claims=None, register_concurrency=8,
        segment_concurrency=32, ffmpeg=None, message_rate=3.0, message_burst=6,
        hash_concurrency=2, hash_check_concurrency=8,
        album_batch=10, album_timeout=60.0, scheduler_clock=time.monotonic,
    ):
        self.api = api
        self.pool = pool
        self.claims = claims if claims is not None else FingerprintClaims()
        self.ffmpeg = ffmpeg
        self._segment_concurrency = max(1, int(segment_concurrency))
        self._register_concurrency = min(8, max(1, int(register_concurrency)))
        self._register_slots = BoundedSemaphore(self._register_concurrency)
        self._hash_concurrency = max(1, int(hash_concurrency))
        self._check_concurrency = max(1, int(hash_check_concurrency))
        self._album_batch = max(1, int(album_batch))
        self._album_timeout = float(album_timeout)
        self._scheduler_clock = scheduler_clock
        self._bucket_lock = Lock()
        self._message_rate = message_rate
        self._message_burst = message_burst
        # Keyed by id(request) and only alive between PLANNING and the result
        # it gets attached to, so a caller that never registers cannot grow it.
        self._metrics: Dict[int, TransferMetrics] = {}
        self._metrics_lock = Lock()
        self._active_schedulers: Dict[int, SegmentScheduler] = {}
        self._scheduler_state_lock = Lock()

    def _supports_failover_scheduler(self) -> bool:
        required = (
            "runtime", "acquire_upload_lease", "acquire_exact_upload_lease",
            "try_reserve_idle", "activate_reservation", "release_reservation",
        )
        return (
            hasattr(self.pool, "activity")
            and hasattr(self.pool, "speed_tracker")
            and all(callable(getattr(self.pool, name, None)) for name in required)
        )

    def _acquire_upload_lease(self, work_id: str, timeout=None):
        acquire = getattr(self.pool, "acquire_upload_lease", None)
        if callable(acquire):
            return acquire(work_id=work_id, timeout=timeout)
        legacy = self.pool.acquire_upload(timeout=timeout)
        runtime = legacy.__enter__()
        return _LegacyUploadLease(runtime, work_id, legacy)

    def _acquire_exact_upload_lease(self, account_id: int, work_id: str):
        acquire = getattr(self.pool, "acquire_exact_upload_lease", None)
        if callable(acquire):
            return acquire(account_id, work_id)
        lease = self._acquire_upload_lease(work_id)
        if int(lease.runtime.telegram_user_id) != int(account_id):
            lease.close()
            return None
        return lease

    def _account_observer(self, account_id: int, task_id: str):
        if not (hasattr(self.pool, "activity") and hasattr(self.pool, "speed_tracker")):
            return None
        return AccountUploadObserver(self.pool, account_id, task_id)

    def transfer(self, request: TransferRequest) -> TransferResult:
        return self.transfer_batch([request])[0]

    def _metrics_for(self, request) -> Optional[TransferMetrics]:
        with self._metrics_lock:
            return self._metrics.get(id(request))

    def _take_metrics(self, request) -> Optional[TransferMetrics]:
        with self._metrics_lock:
            return self._metrics.pop(id(request), None)

    def _discard_metrics(self, request) -> None:
        with self._metrics_lock:
            self._metrics.pop(id(request), None)

    def _fingerprint(self, request):
        # Imported lazily: gamestage also exposes the legacy upload wrapper.
        from gamestage import sample_hash

        if request.logical_size <= 0:
            raise ValueError(f"{request.upload_name} is empty (0 bytes) or has an invalid size")
        actual_size = request.source.stat().st_size
        if actual_size != request.logical_size:
            raise CoverageError(f"source has {actual_size} bytes, expected {request.logical_size}")
        return sample_hash(request.source)

    def _check_existing(self, request, fingerprint):
        """Reuse a prior upload only under the exact name it was registered as.

        The backend's ``files`` table has ``file_id`` as its PRIMARY KEY and
        registers with INSERT OR REPLACE, so a row is *addressed* by the
        Telegram document id. Registering a reused document under a second name
        therefore does not add a row -- it overwrites the first one, and that
        file disappears from the drive while the stager, having seen a
        successful registration, deletes the only local copy.

        So dedup is scoped to (filename, parent): re-uploading over the same
        name still costs nothing, and two names for one payload each get their
        own document, which is the only shape this schema can hold. Measured on
        the real backend before this rule existed: two identical 1 MiB files
        under different names left exactly one row.
        """
        response = self.api.check_hash(fingerprint) or {}
        rows = [
            row for row in (response.get("files") or [])
            if str(row.get("filename") or "") == request.upload_name
            and (row.get("parent_id") or None) == (request.parent_id or None)
        ]
        return canonical_existing_parts(rows, request.logical_size)

    @staticmethod
    def _claim_key(request, fingerprint: str) -> str:
        """Same rule for in-batch collapsing as for backend reuse.

        Sharing a claim by fingerprint alone would hand the second name the
        first one's document id, which is the same overwrite by another route.
        """
        return f"{fingerprint}|{request.parent_id or ''}|{request.upload_name}"

    def _submit_inspection(self, request, hash_pool, check_pool):
        """Chain hash -> check-hash so the two caps stay independent.

        Both stages are latency, not CPU: a 100 MiB read and a half-second
        round trip to a backend on the other side of Cloudflare. Running them
        in one pool would make the slower one set the other's cap.
        """
        inspected: Future = Future()

        def checked(fingerprint, future):
            try:
                inspected.set_result((fingerprint, future.result()))
            except BaseException as exc:  # noqa: BLE001 - forwarded to the caller
                inspected.set_exception(exc)

        def hashed(future):
            try:
                fingerprint = future.result()
            except BaseException as exc:  # noqa: BLE001 - forwarded to the caller
                inspected.set_exception(exc)
                return
            try:
                check_pool.submit(self._timed_check, request, fingerprint).add_done_callback(
                    lambda done: checked(fingerprint, done)
                )
            except BaseException as exc:  # noqa: BLE001 - pool already shutting down
                inspected.set_exception(exc)

        hash_pool.submit(self._timed_fingerprint, request).add_done_callback(hashed)
        return inspected

    def _timed_fingerprint(self, request):
        with _timed(self._metrics_for(request), "hash_ms"):
            return self._fingerprint(request)

    def _timed_check(self, request, fingerprint):
        with _timed(self._metrics_for(request), "check_ms"):
            return self._check_existing(request, fingerprint)

    def _inspection_stream(self, requests, hash_pool, check_pool, notify, lookahead):
        """Yield ``(request, inspection)`` in input order, ``lookahead`` ahead.

        The window is what makes hashing and checking overlap the upload stage
        without reading the whole input first: a caller streaming a directory
        walk still gets its first upload started after one file.
        """
        window = max(0, int(lookahead)) + 1
        source = iter(requests)
        inflight = deque()
        exhausted = False
        while True:
            while not exhausted and len(inflight) < window:
                try:
                    request = next(source)
                except StopIteration:
                    exhausted = True
                    break
                notify(request, QueueStage.PLANNING, "")
                with self._metrics_lock:
                    self._metrics[id(request)] = TransferMetrics(
                        protocol="?", bytes=request.logical_size,
                    )
                inflight.append((request, self._submit_inspection(request, hash_pool, check_pool)))
            if not inflight:
                return
            yield inflight.popleft()

    def transfer_batch(self, requests, status_sink=None, *, on_result=None, lookahead=0):
        """Stream a batch through hash, check-hash and upload; return in input order.

        No registration or source deletion happens here -- ``on_result`` is
        called with each :class:`TransferResult` the moment it exists, so the
        caller can start registering one file while the next is still moving
        bytes. Pending aliases share futures without blocking discovery, so an
        album tail can always reach its flush.

        The upload stage itself is deliberately serial in this thread: album
        batches are formed in arrival order, and ten prepared items must flush
        while discovery continues rather than in whatever order preparations
        happen to finish. Concurrency within a file (segments across accounts)
        and across accounts (per-account file slots) already lives lower down.
        """
        notify = status_sink if status_sink is not None else (lambda *_a, **_kw: None)
        queues = {}
        pending = []
        errors = []
        delivered: Dict[int, TransferMetrics] = {}

        def deliver(request, fingerprint, future):
            """Hand one finished file to the caller; never called twice."""
            try:
                parts = future.result()
                assert_parts_cover_file(parts, request.logical_size)
            except BaseException as exc:  # noqa: BLE001 - reported per request
                errors.append(exc)
                detail = redact(exc)
                self._discard_metrics(request)
                log.warning("transfer failed name=%s %s", request.upload_name, detail)
                notify(request, QueueStage.FAILED, detail)
                return
            metrics = self._take_metrics(request)
            if metrics is not None:
                metrics.parts = len(parts)
                metrics.account_ids = tuple(sorted({p.telegram_user_id for p in parts}))
                if metrics.protocol == "?":
                    metrics.protocol = "duplicate"
            result = TransferResult(
                request, fingerprint, tuple(sorted(parts, key=lambda p: p.index)), metrics,
            )
            if metrics is not None:
                delivered[id(request)] = metrics
            notify(request, QueueStage.SENDING, "")
            if on_result is not None:
                on_result(result)
            return result

        with ThreadPoolExecutor(max_workers=self._hash_concurrency, thread_name_prefix="tx-hash") as hash_pool, \
                ThreadPoolExecutor(max_workers=self._check_concurrency, thread_name_prefix="tx-check") as check_pool:
            try:
                stream = self._inspection_stream(requests, hash_pool, check_pool, notify, lookahead)
                for request, inspection in stream:
                    try:
                        fingerprint, existing = inspection.result()
                        if existing:
                            future = Future()
                            future.set_result(existing)
                            owner = False
                        else:
                            future, owner = self.claims._claim(
                                self._claim_key(request, fingerprint)
                            )
                        pending.append((request, fingerprint, future))
                        if owner:
                            notify(request, QueueStage.UPLOADING, "")
                            try:
                                if request.allow_album and album_eligible(request.mime_type, request.logical_size):
                                    runtime, item = self._prepare_album(request)
                                    queue = queues.setdefault(runtime.telegram_user_id, AlbumQueue(
                                        runtime, self._album_fallback,
                                        batch=self._album_batch, timeout=self._album_timeout,
                                    ))
                                    queue.add(item, future)
                                else:
                                    future.set_result(self._upload_fresh(request))
                            except BaseException as exc:  # noqa: BLE001 - reported per request
                                future.set_exception(exc)
                        # A callback, not a blocking read: an alias and an album
                        # item both settle later, and waiting here for an album
                        # future would stop the batch that has to flush it.
                        future.add_done_callback(
                            lambda done, r=request, f=fingerprint: deliver(r, f, done)
                        )
                    except BaseException as exc:  # noqa: BLE001 - reported per request
                        errors.append(exc)
                        detail = redact(exc)
                        self._discard_metrics(request)
                        log.warning("transfer failed name=%s %s", request.upload_name, detail)
                        notify(request, QueueStage.FAILED, detail)
            finally:
                for queue in queues.values():
                    queue.flush()
        results = []
        for request, fingerprint, future in pending:
            try:
                parts = future.result()
                results.append(TransferResult(
                    request, fingerprint, tuple(sorted(parts, key=lambda p: p.index)),
                    delivered.get(id(request)),
                ))
            except BaseException:  # noqa: BLE001 - already recorded by deliver
                pass
        if errors:
            raise errors[0]
        return results

    def _message_bucket(self, runtime):
        with self._bucket_lock:
            if runtime.message_limiter is None:
                runtime.message_limiter = MessageTokenBucket(self._message_rate, self._message_burst)
        return runtime.message_limiter

    def _prepare_album(self, request):
        from gamestage import _preview_file

        metrics = self._metrics_for(request)
        if metrics is not None:
            metrics.protocol = "album"
        with _timed(metrics, "slot_ms"):
            lease = self._acquire_upload_lease(work_id=f"album:{id(request)}")
            runtime = lease.runtime
        try:
            if metrics is not None:
                metrics.observe_limiter(runtime)
            bucket = self._message_bucket(runtime)
            with _timed(metrics, "thumb_ms"):
                preview_cm = _preview_file(request.source, request.mime_type, self.ffmpeg)
                preview = preview_cm.__enter__()
            try:
                with _timed(metrics, "upload_ms"):
                    album_kwargs = {"message_limiter": bucket}
                    observer = self._account_observer(
                        runtime.telegram_user_id, f"{lease.work_id}:main"
                    )
                    thumb_observer = (
                        self._account_observer(
                            runtime.telegram_user_id, f"{lease.work_id}:thumb"
                        ) if preview else None
                    )
                    if observer is not None:
                        album_kwargs["observer"] = observer
                    if thumb_observer is not None:
                        album_kwargs["thumbnail_observer"] = thumb_observer
                    item = runtime.worker.prepare_album_item(
                        request.source, request.logical_size, request.upload_name,
                        request.mime_type, preview, **album_kwargs,
                    )
            finally:
                preview_cm.__exit__(None, None, None)
        finally:
            lease.close()
        return runtime, item

    def _album_fallback(self, runtime, item):
        # Reacquire this exact account through the pool. A reservation must block
        # fallback just like it blocks ordinary work; direct semaphore access
        # would make a supposedly idle migration target busy behind the scheduler.
        lease = self._acquire_exact_upload_lease(
            runtime.telegram_user_id, f"album-fallback:{id(item)}"
        )
        if lease is None:
            raise RuntimeError(
                f"Telegram account {runtime.telegram_user_id} is reserved or busy"
            )
        try:
            with open(ext_path(item.source), "rb") as stream:
                observer = self._account_observer(
                    runtime.telegram_user_id, f"{lease.work_id}:main"
                )
                prepare_fallback = getattr(runtime.worker, "prepare_album_fallback", None)
                if callable(prepare_fallback):
                    kwargs = {}
                    if observer is not None:
                        kwargs["observer"] = observer
                    handle = prepare_fallback(
                        stream, item.size, item.upload_name, **kwargs,
                    )
                else:
                    kwargs = {"force_big": False}
                    if observer is not None:
                        kwargs["observer"] = observer
                    handle = runtime.worker.prepare_segment(
                        stream, item.size, item.upload_name, **kwargs,
                    )
        finally:
            lease.close()
        result = runtime.worker.send_uploaded_segment(
            handle, item.size, item.upload_name, preview=None,
            mime_type=item.mime_type, message_limiter=self._message_bucket(runtime),
        )
        return UploadedPart(
            index=0, message_id=int(result["message_id"]), file_id=str(result["file_id"]),
            access_hash=result.get("access_hash"), size=int(result["size"]),
            telegram_user_id=item.telegram_user_id, has_thumbnail=False,
        )

    def _upload_fresh(self, request):
        from gamestage import _preview_file

        metrics = self._metrics_for(request)
        decision = decide_protocol(request.logical_size, album_eligible=False)
        if metrics is not None:
            metrics.protocol = decision.name
        with _timed(metrics, "thumb_ms"):
            preview_cm = _preview_file(request.source, request.mime_type, self.ffmpeg)
            preview = preview_cm.__enter__()
        try:
            if decision.force_big and self._supports_failover_scheduler():
                parts = self._upload_big_with_scheduler(request, decision, preview)
            elif decision.force_big:
                parts = self._upload_big_legacy_compatible(request, decision, preview)
            else:
                offset, size = decision.segments[0]
                parts = [self._upload_segment_once(
                    request, 0, offset, size, False, False, preview,
                )]
        finally:
            preview_cm.__exit__(None, None, None)
        assert_parts_cover_file(parts, request.logical_size)
        return parts

    def _upload_big_legacy_compatible(self, request, decision, preview):
        """Keep old pool test doubles usable without bypassing production failover.

        Production ``AccountPool`` always advertises the full scheduler contract,
        so force-big transfers there still take the central scheduler path.
        """
        split = len(decision.segments) > 1
        with ThreadPoolExecutor(
            max_workers=min(self._segment_concurrency, len(decision.segments))
        ) as executor:
            futures = [
                executor.submit(
                    self._upload_segment_once, request, index, offset, size,
                    True, split, preview if index == 0 else None,
                )
                for index, (offset, size) in enumerate(decision.segments)
            ]
            return [future.result() for future in as_completed(futures)]

    @staticmethod
    def _log_scheduler_diagnostic(scheduler, kind: str, payload: dict) -> None:
        if kind == "premium_flood":
            cycle = payload["cycle"]
            pacer = payload["pacer"]
            task = scheduler.task(cycle.lease.task_id)
            remaining_ratio = (task.size - task.logical_uploaded_bytes) / task.size
            live_speed = scheduler.speed_tracker.live_speed(cycle.lease)
            mode = getattr(pacer, "mode", "unknown")
            penalty = getattr(pacer, "paused_until", getattr(pacer, "penalty_until", 0.0))
            log.info(
                "premium flood file_job_id=%s task_id=%s segment_index=%d attempt_id=%d "
                "account_id=%d wait_seconds=%g penalty_until=%.3f pacer_mode=%s rate=%.2f "
                "live_speed=%.2f logical_bytes=%d remaining_ratio=%.6f "
                "account_accepted_parts=%d account_accepted_bytes=%d "
                "task_accepted_parts=%d task_accepted_bytes=%d",
                scheduler.file_job_id, cycle.lease.task_id, task.index, cycle.lease.attempt_id,
                cycle.lease.account_id, cycle.wait_seconds, float(penalty or 0.0), mode,
                float(getattr(pacer, "rate", 0.0) or 0.0), live_speed,
                task.logical_uploaded_bytes, remaining_ratio,
                cycle.account_accepted_parts, cycle.account_accepted_bytes,
                cycle.task_accepted_parts, cycle.task_accepted_bytes,
            )
            return
        if kind == "migration":
            commit = payload["commit"]
            task = scheduler.task(commit.task_id)
            log.info(
                "segment migration file_job_id=%s task_id=%s segment_index=%d "
                "old_attempt_id=%d new_attempt_id=%d from_account_id=%d to_account_id=%d "
                "snapshot_speed=%.2f snapshot_age=%.2f current_speed=%.2f "
                "remaining_ratio=%.6f score=%.6f abandoned_bytes=%d migration_count=%d",
                scheduler.file_job_id, commit.task_id, task.index,
                commit.old_attempt_id, commit.new_attempt_id, commit.from_account_id,
                commit.to_account_id, commit.target_snapshot_speed, commit.target_snapshot_age,
                commit.current_speed, commit.remaining_ratio, commit.score,
                commit.abandoned_logical_bytes, task.migration_count,
            )

    def scheduler_status(self) -> list[dict]:
        with self._scheduler_state_lock:
            schedulers = list(self._active_schedulers.values())
        return [
            {
                "file_job_id": scheduler.file_job_id,
                "segments": scheduler.status(),
                **scheduler.metrics_snapshot(),
            }
            for scheduler in schedulers
        ]

    def _new_segment_scheduler(self, request, descriptors, preview):
        scheduler = SegmentScheduler(
            f"upload:{id(request)}",
            descriptors,
            pool=self.pool,
            activity=self.pool.activity,
            speed_tracker=self.pool.speed_tracker,
            clock=self._scheduler_clock,
        )
        # Execution-only immutable context.  Scheduler state remains authoritative
        # for ownership/generation; these values merely avoid widening every
        # SchedulerAction with file-system objects.
        scheduler.execution_request = request
        scheduler.execution_preview = preview
        scheduler._diagnostic_sink = (
            lambda kind, payload, current=scheduler:
                self._log_scheduler_diagnostic(current, kind, payload)
        )
        return scheduler

    def _upload_big_with_scheduler(self, request, decision, preview):
        descriptors = [
            SegmentDescriptor(index=index, offset=offset, size=size)
            for index, (offset, size) in enumerate(decision.segments)
        ]
        scheduler = self._new_segment_scheduler(request, descriptors, preview)
        with self._scheduler_state_lock:
            self._active_schedulers[id(request)] = scheduler
        try:
            with ThreadPoolExecutor(max_workers=min(self._segment_concurrency, len(descriptors))) as executor:
                parts = self._run_scheduler_loop(scheduler, executor)
            assert_parts_cover_file(parts, request.logical_size)
            snapshot = scheduler.metrics_snapshot()
            metrics = self._metrics_for(request)
            if metrics is not None:
                metrics.logical_uploaded_bytes = snapshot["logical_uploaded_bytes"]
                metrics.physical_transferred_bytes = snapshot["physical_transferred_bytes"]
                metrics.migration_overhead_bytes = snapshot["migration_overhead_bytes"]
                metrics.migration_count = snapshot["migration_count"]
            return parts
        finally:
            with self._scheduler_state_lock:
                self._active_schedulers.pop(id(request), None)

    def _run_scheduler_loop(self, scheduler: SegmentScheduler, executor) -> list[UploadedPart]:
        submitted: dict[Future, object] = {}

        def on_done(future, action):
            category = None
            try:
                future.result()
            except BaseException as exc:  # consume cancellation/programming failures
                category = type(exc).__name__
            scheduler.enqueue_executor_completion(future, action, category)

        observed_version = scheduler.version
        while True:
            for future, action, category in scheduler.take_executor_completions():
                submitted.pop(future, None)
                scheduler.executor_finished(action, category)
            if scheduler.all_terminal() and not submitted:
                break

            action = (
                scheduler.select_next_action()
                if len(submitted) < self._segment_concurrency
                else None
            )
            if action is not None:
                try:
                    future = executor.submit(self._execute_scheduler_action, scheduler, action)
                except Exception as exc:
                    scheduler.submission_failed(action, type(exc).__name__)
                    continue
                submitted[future] = action
                future.add_done_callback(
                    lambda done, claim=action: on_done(done, claim)
                )
                continue

            deadline = scheduler.next_deadline()
            observed_version = scheduler.wait_for_change(observed_version, deadline)

        return scheduler.completed_results_by_index()

    def _execute_scheduler_action(self, scheduler: SegmentScheduler, action) -> None:
        request = scheduler.execution_request
        preview = scheduler.execution_preview if action.descriptor.index == 0 else None
        lease = action.lease
        runtime = scheduler.borrow_runtime(lease)
        if runtime is None:
            return
        name = (
            f"{request.upload_name}.part{action.descriptor.index + 1}"
            if len(scheduler.status()) > 1 else request.upload_name
        )
        metrics = self._metrics_for(request)
        if metrics is not None:
            metrics.observe_limiter(runtime)
        self._message_bucket(runtime)
        revoke_handle = runtime.worker.create_revoke_handle()
        if not scheduler.bind_revoke_handle(lease, revoke_handle):
            revoke_handle.revoke()
            scheduler.attempt_quiesced(lease)
            return
        observer = SchedulerUploadObserver(scheduler, lease)
        reader = SegmentReader(
            ext_path(request.source),
            action.descriptor.offset,
            action.descriptor.size,
            force_big=True,
        )
        try:
            try:
                with _timed(metrics, "upload_ms"):
                    handle = runtime.worker.prepare_segment(
                        reader, action.descriptor.size, name, force_big=True,
                        observer=observer, revoke_handle=revoke_handle, rpc_timeout=120.0,
                    )
                    uploaded_preview = (
                        runtime.worker.prepare_thumbnail(
                            preview,
                            observer=AccountUploadObserver(
                                self.pool, runtime.telegram_user_id,
                                f"{lease.task_id}:{lease.attempt_id}:thumb",
                            ),
                        ) if preview else None
                    )
                if not scheduler.bytes_prepared(lease):
                    return
                if not scheduler.grant_finalize(lease):
                    return
                with _timed(metrics, "message_ms"):
                    result = runtime.worker.send_uploaded_segment(
                        handle, action.descriptor.size, name, preview=uploaded_preview,
                        mime_type=request.mime_type, message_limiter=runtime.message_limiter,
                    )
                uploaded = UploadedPart(
                    index=action.descriptor.index,
                    message_id=int(result["message_id"]),
                    file_id=str(result["file_id"]),
                    access_hash=result.get("access_hash"),
                    size=int(result["size"]),
                    telegram_user_id=int(runtime.worker.user_id),
                    has_thumbnail=uploaded_preview is not None,
                )
                if not scheduler.complete_attempt(lease, uploaded):
                    raise RuntimeError("scheduler rejected completed current attempt")
            except AttemptRevoked:
                return
            except (AssertionError, TypeError, AttributeError):
                raise
            except Exception as exc:
                scheduler.fail_attempt(lease, redact(exc))
        finally:
            reader.close()
            scheduler.attempt_quiesced(lease)

    def _upload_segment_once(self, request, index, offset, size, force_big, split, preview):
        name = f"{request.upload_name}.part{index + 1}" if split else request.upload_name
        metrics = self._metrics_for(request)
        with _timed(metrics, "slot_ms"):
            lease = self._acquire_upload_lease(work_id=f"single:{id(request)}:{index}")
            runtime = lease.runtime
        try:
            if metrics is not None:
                metrics.observe_limiter(runtime)
            self._message_bucket(runtime)
            reader = SegmentReader(ext_path(request.source), offset, size, force_big=force_big)
            try:
                with _timed(metrics, "upload_ms"):
                    observer = self._account_observer(
                        runtime.telegram_user_id, f"{lease.work_id}:main"
                    )
                    segment_kwargs = {"force_big": force_big}
                    if observer is not None:
                        segment_kwargs["observer"] = observer
                    handle = runtime.worker.prepare_segment(
                        reader, size, name, **segment_kwargs,
                    )
                    if preview:
                        thumb_observer = self._account_observer(
                            runtime.telegram_user_id, f"{lease.work_id}:thumb"
                        )
                        thumb_kwargs = {}
                        if thumb_observer is not None:
                            thumb_kwargs["observer"] = thumb_observer
                        uploaded_preview = runtime.worker.prepare_thumbnail(
                            preview, **thumb_kwargs,
                        )
                    else:
                        uploaded_preview = None
            finally:
                reader.close()
        finally:
            lease.close()
        with _timed(metrics, "message_ms"):
            result = runtime.worker.send_uploaded_segment(
                handle, size, name, preview=uploaded_preview,
                mime_type=request.mime_type, message_limiter=runtime.message_limiter,
            )
        return UploadedPart(
            index=index, message_id=int(result["message_id"]),
            file_id=str(result["file_id"]), access_hash=result.get("access_hash"),
            size=int(result["size"]), telegram_user_id=int(runtime.worker.user_id),
            has_thumbnail=uploaded_preview is not None,
        )

    # Kept for focused legacy tests and internal callers; fresh force-big paths
    # never use this compatibility spelling.
    _upload_segment = _upload_segment_once

    def register_result(self, result: TransferResult) -> None:
        """Settle every part registration, then say where the time went.

        The completion line lands here rather than at the end of the transfer
        because a logical file is not done until it is both on Telegram and in
        the drive; a failure before that point is logged as one instead.
        """
        request = result.request
        parts = sorted(result.parts, key=lambda p: p.index)
        assert_parts_cover_file(parts, request.logical_size)
        group = uuid4().hex
        total = len(parts)
        metrics = result.metrics if isinstance(result.metrics, TransferMetrics) else None
        try:
            with _timed(metrics, "register_ms"):
                self._register_parts(request, parts, result.fingerprint, group, total)
                # Only the reuse path can be swallowed: a fresh upload mints a
                # document id nobody else owns, so its row cannot replace one.
                if metrics is not None and metrics.protocol == "duplicate":
                    self._assert_registered(request, result.fingerprint)
        except BaseException as exc:  # noqa: BLE001 - logged, then re-raised
            log.warning("registration failed name=%s %s", request.upload_name, redact(exc))
            raise
        self.api.invalidate(request.parent_id)
        self._log_complete(metrics)

    @staticmethod
    def _log_complete(metrics: Optional[TransferMetrics]) -> None:
        if metrics is None:
            return
        metrics.total_ms = (time.monotonic() - metrics.started) * 1000.0
        log.info(
            "transfer complete protocol=%s bytes=%d parts=%d hash_ms=%.0f check_ms=%.0f "
            "thumb_ms=%.0f slot_ms=%.0f upload_ms=%.0f message_ms=%.0f register_ms=%.0f "
            "total_ms=%.0f accounts=%s logical_uploaded_bytes=%d physical_transferred_bytes=%d "
            "migration_overhead_bytes=%d migration_count=%d rate=%.2f ceiling=%s",
            metrics.protocol, metrics.bytes, metrics.parts, metrics.hash_ms, metrics.check_ms,
            metrics.thumb_ms, metrics.slot_ms, metrics.upload_ms, metrics.message_ms,
            metrics.register_ms, metrics.total_ms, metrics.account_ids,
            metrics.logical_uploaded_bytes, metrics.physical_transferred_bytes,
            metrics.migration_overhead_bytes, metrics.migration_count, metrics.rate,
            metrics.ceiling,
        )

    def _register_parts(self, request, parts, fingerprint, group, total) -> None:
        with ThreadPoolExecutor(max_workers=self._register_concurrency) as executor:
            futures = [
                executor.submit(
                    self._register_part,
                    filename=request.upload_name, filesize=part.size,
                    message_id=part.message_id, file_id=part.file_id,
                    access_hash=part.access_hash, telegram_user_id=part.telegram_user_id,
                    mime_type=request.mime_type, parent_id=request.parent_id,
                    is_split_file=total > 1, original_name=request.upload_name,
                    part_index=part.index, total_parts=total, split_group_id=group,
                    file_hash=fingerprint, has_thumbnail=part.has_thumbnail,
                )
                for part in parts
            ]
            for future in futures:
                future.result()

    def _assert_registered(self, request, fingerprint: str) -> None:
        """Confirm the row is really addressable under the name we asked for.

        The caller deletes its only copy of the bytes on the strength of this
        call returning, and a registration can be accepted and still leave no
        row under that name -- INSERT OR REPLACE keyed on the Telegram document
        id means one document holds one name. Better a retained staging file
        and a visible failure than a file that quietly is not there.
        """
        response = self.api.check_hash(fingerprint) or {}
        for row in response.get("files") or []:
            if (str(row.get("filename") or "") == request.upload_name
                    and (row.get("parent_id") or None) == (request.parent_id or None)):
                return
        raise CoverageError(
            f"{request.upload_name} registered but no row answers to that name "
            f"under its parent; the drive stores one name per Telegram document"
        )

    def _register_part(self, **payload):
        with self._register_slots:
            return self.api.register(**payload)
