"""Account-routed transfers and exact, independently settled registration."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from threading import BoundedSemaphore, Lock
from typing import Callable, Dict, Mapping, Sequence
from uuid import uuid4

from config import ext_path
from tgio import SegmentReader
from tgupload import SMALL_FILE_MAX, decide_protocol
from transfer_models import TransferRequest, TransferResult, UploadedPart
from upload_limiter import MessageTokenBucket


class CoverageError(RuntimeError):
    """Metadata parts do not describe exactly the logical file bytes."""


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

    def __init__(self, runtime, fallback, *, timeout=60):
        self.runtime = runtime
        self.fallback = fallback
        self.timeout = timeout
        self._pending = []
        self._lock = Lock()

    def add(self, item, future=None):
        if item.telegram_user_id != self.runtime.telegram_user_id:
            raise ValueError("prepared item belongs to another Telegram account")
        future = future if future is not None else Future()
        with self._lock:
            self._pending.append((item, future))
            batch = self._pending if len(self._pending) == 10 else []
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


class UploadEngine:
    """Transfer files; callers register results before deleting sources.

    Share one engine within a due batch to share fingerprint claims. Account
    admission belongs to the pool, and registration admission to this engine.
    """

    def __init__(
        self, api, pool, *, claims=None, register_concurrency=8,
        segment_concurrency=32, ffmpeg=None, message_rate=3.0, message_burst=6,
    ):
        self.api = api
        self.pool = pool
        self.claims = claims if claims is not None else FingerprintClaims()
        self.ffmpeg = ffmpeg
        self._segment_concurrency = max(1, int(segment_concurrency))
        self._register_concurrency = min(8, max(1, int(register_concurrency)))
        self._register_slots = BoundedSemaphore(self._register_concurrency)
        self._bucket_lock = Lock()
        self._message_rate = message_rate
        self._message_burst = message_burst

    def transfer(self, request: TransferRequest) -> TransferResult:
        return self.transfer_batch([request])[0]

    def _inspect_request(self, request):
        # Imported lazily: gamestage also exposes the legacy upload wrapper.
        from gamestage import sample_hash

        if request.logical_size <= 0:
            raise ValueError(f"{request.upload_name} is empty (0 bytes) or has an invalid size")
        actual_size = request.source.stat().st_size
        if actual_size != request.logical_size:
            raise CoverageError(f"source has {actual_size} bytes, expected {request.logical_size}")
        fingerprint = sample_hash(request.source)
        response = self.api.check_hash(fingerprint) or {}
        existing = canonical_existing_parts(response.get("files") or [], request.logical_size)
        return fingerprint, existing

    def transfer_batch(self, requests):
        """Prepare during discovery, flush account tails, then return in input order.

        No registration or source deletion occurs here. Pending aliases share
        futures without blocking discovery, so a tail can always reach flush.
        The streaming scheduler owns concurrent discovery in the next layer.
        """
        queues = {}
        pending = []
        errors = []
        try:
            for request in requests:
                try:
                    fingerprint, existing = self._inspect_request(request)
                    if existing:
                        future = Future()
                        future.set_result(existing)
                        owner = False
                    else:
                        future, owner = self.claims._claim(fingerprint)
                    pending.append((request, fingerprint, future))
                    if not owner:
                        continue
                    try:
                        if request.allow_album and album_eligible(request.mime_type, request.logical_size):
                            runtime, item = self._prepare_album(request)
                            queue = queues.setdefault(runtime.telegram_user_id, AlbumQueue(runtime, self._album_fallback))
                            queue.add(item, future)
                        else:
                            future.set_result(self._upload_fresh(request))
                    except Exception as exc:
                        future.set_exception(exc)
                except Exception as exc:
                    errors.append(exc)
        finally:
            for queue in queues.values():
                queue.flush()
        results = []
        for request, fingerprint, future in pending:
            try:
                parts = future.result()
                assert_parts_cover_file(parts, request.logical_size)
                results.append(TransferResult(request, fingerprint, tuple(sorted(parts, key=lambda p: p.index))))
            except Exception as exc:
                errors.append(exc)
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

        with self.pool.acquire_upload() as runtime:
            bucket = self._message_bucket(runtime)
            with _preview_file(request.source, request.mime_type, self.ffmpeg) as preview:
                item = runtime.worker.prepare_album_item(
                    request.source, request.logical_size, request.upload_name,
                    request.mime_type, preview, message_limiter=bucket,
                )
        return runtime, item

    def _album_fallback(self, runtime, item):
        # Reacquire this exact account; a different account cannot preserve the
        # prepared item's routing. File admission ends before message admission.
        with runtime.file_slots:
            with open(ext_path(item.source), "rb") as stream:
                handle = runtime.worker.prepare_album_fallback(stream, item.size, item.upload_name)
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

        decision = decide_protocol(request.logical_size, album_eligible=False)
        with _preview_file(request.source, request.mime_type, self.ffmpeg) as preview:
            with ThreadPoolExecutor(max_workers=min(self._segment_concurrency, len(decision.segments))) as executor:
                futures = [
                    executor.submit(
                        self._upload_segment, request, index, offset, size,
                        decision.force_big, len(decision.segments) > 1,
                        preview if index == 0 else None,
                    )
                    for index, (offset, size) in enumerate(decision.segments)
                ]
                parts = [future.result() for future in as_completed(futures)]
        # Check inside the claim so a corrupt result is never cached for aliases.
        assert_parts_cover_file(parts, request.logical_size)
        return parts

    def _upload_segment(self, request, index, offset, size, force_big, split, preview):
        name = f"{request.upload_name}.part{index + 1}" if split else request.upload_name
        with self.pool.acquire_upload() as runtime:
            self._message_bucket(runtime)
            reader = SegmentReader(ext_path(request.source), offset, size, force_big=force_big)
            try:
                handle = runtime.worker.prepare_segment(reader, size, name, force_big=force_big)
                uploaded_preview = runtime.worker.prepare_thumbnail(preview) if preview else None
            finally:
                reader.close()
        # A Telegram message and backend registration do not occupy file slots.
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

    def register_result(self, result: TransferResult) -> None:
        """Settle every part registration, propagating failure before success."""
        request = result.request
        parts = sorted(result.parts, key=lambda p: p.index)
        assert_parts_cover_file(parts, request.logical_size)
        group = uuid4().hex
        total = len(parts)
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
                    file_hash=result.fingerprint, has_thumbnail=part.has_thumbnail,
                )
                for part in parts
            ]
            for future in futures:
                future.result()
        self.api.invalidate(request.parent_id)

    def _register_part(self, **payload):
        with self._register_slots:
            return self.api.register(**payload)
