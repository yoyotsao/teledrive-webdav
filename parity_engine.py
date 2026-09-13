"""UploadEngine integration for current-backend storage parity.

Raw SaveFilePart/SaveBigFilePart work stays in the proven worker.  This subclass
moves the *message* boundary behind durable backend intent, exact local result
persistence, frozen target routing, and operation-backed registration.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from uuid import uuid4

import upload_engine as _engine
from config import ext_path
from gamestage import _preview_file
from operation_cursor import OperationCursorStore
from operation_state import GroupBarrierStore
from storage_parity import (
    AmbiguousTelegramWrite,
    DedupDisposition,
    StorageParityCoordinator,
    classify_dedup_row,
    prepared_media_fingerprint,
)
from tgio import SegmentReader
from tgupload import decide_protocol
from transfer_models import QueueStage, TransferResult, UploadedPart


_ALBUM_CLASS_LOCK = threading.RLock()


class ParityAlbumQueue:
    """Same batching shape as legacy AlbumQueue, without unsafe fallback replay."""

    def __init__(self, runtime, fallback, *, batch=10, timeout=60):
        self.runtime = runtime
        self.fallback = fallback  # deliberately unused after possible bulk send
        self.batch = max(1, int(batch))
        self.timeout = timeout
        self._pending = []
        self._lock = threading.Lock()

    def add(self, item, future=None):
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
        engine = getattr(self.runtime, "_storage_parity_engine", None)
        if engine is None:
            exc = RuntimeError("parity album runtime is not bound to its engine")
            for _item, future in batch:
                future.set_exception(exc)
            return
        items = [item for item, _future in batch]
        try:
            parts = engine._send_album_batch(self.runtime, items, timeout=self.timeout)
            if len(parts) != len(items):
                raise _engine.CoverageError("durable album did not settle every child")
        except BaseException as exc:
            # Never fall back to individual sends: SendMultiMedia may already
            # have committed some/all children.
            for _item, future in batch:
                future.set_exception(exc)
        else:
            for (_item, future), part in zip(batch, parts):
                future.set_result([part])


class ParityUploadEngine(_engine.UploadEngine):
    """Target-aware durable message orchestration over legacy byte upload."""

    def __init__(self, api, pool, **kwargs):
        super().__init__(api, pool, **kwargs)
        self.coordinator = StorageParityCoordinator(api, pool)
        root = Path(getattr(getattr(api, "cfg", None), "cache_dir", "."))
        self.operation_cursors = OperationCursorStore(root)
        self.group_barriers = GroupBarrierStore(root)
        self._parity_lock = threading.RLock()
        self._targets = {}
        self._fingerprints = {}
        self._album_context = {}
        self._durable_parts = {}

    # -- target/fingerprint scope ---------------------------------------- #

    def transfer_batch(self, requests, status_sink=None, *, on_result=None, lookahead=0):
        target = self.coordinator.freeze_target()
        seen = []

        def scoped():
            for request in requests:
                with self._parity_lock:
                    self._targets[id(request)] = target
                seen.append(id(request))
                yield request

        # Inherited transfer_batch resolves AlbumQueue through its defining
        # module globals. Swap only while this parity engine owns the call.
        with _ALBUM_CLASS_LOCK:
            original = _engine.AlbumQueue
            _engine.AlbumQueue = ParityAlbumQueue
            try:
                return super().transfer_batch(
                    scoped(), status_sink, on_result=on_result, lookahead=lookahead,
                )
            finally:
                _engine.AlbumQueue = original
                with self._parity_lock:
                    for key in seen:
                        self._targets.pop(key, None)
                        self._fingerprints.pop(key, None)

    def _target(self, request):
        with self._parity_lock:
            target = self._targets.get(id(request))
        if target is None:
            # Direct unit callers of _upload_fresh still get one frozen target.
            target = self.coordinator.freeze_target()
            with self._parity_lock:
                self._targets[id(request)] = target
        return target

    def _check_existing(self, request, fingerprint):
        with self._parity_lock:
            self._fingerprints[id(request)] = fingerprint
        response = self.api.check_hash(fingerprint) or {}
        target = self._target(request)
        rows = []
        for row in response.get("files") or []:
            if str(row.get("filename") or "") != request.upload_name:
                continue
            if (row.get("parent_id") or None) != (request.parent_id or None):
                continue
            try:
                disposition = classify_dedup_row(row, target)
            except Exception:
                # Canonical-looking malformed rows are not safe aliases.
                continue
            if disposition is DedupDisposition.SAME_TARGET:
                rows.append(row)
        return _engine.canonical_existing_parts(rows, request.logical_size)

    # -- writer selection ------------------------------------------------ #

    def _writer_for(self, target, ordinal=0):
        if target.storage_mode == "saved_messages":
            runtime = self.pool.primary_for_write(target.primary_account_id)
            try:
                from telethon.tl.types import InputPeerSelf
                peer = InputPeerSelf()
            except Exception:  # pragma: no cover
                peer = "me"
            return runtime, peer
        writers = self.pool.channel_writers(target.channel_id, target.linked_account_ids)
        runtime, access = writers[int(ordinal) % len(writers)]
        return runtime, access.peer

    @staticmethod
    def _registration_metadata(request, *, size, total_parts, fingerprint, has_thumbnail):
        return {
            "filename": request.upload_name,
            "filesize": int(size),
            "mime_type": request.mime_type,
            "parent_id": request.parent_id,
            "has_thumbnail": bool(has_thumbnail),
            "original_name": request.upload_name,
            "total_parts": int(total_parts),
            "file_hash": fingerprint,
        }

    # -- ordinary/split upload ------------------------------------------ #

    def _upload_fresh(self, request):
        target = self._target(request)
        fingerprint = self._fingerprints.get(id(request))
        decision = decide_protocol(request.logical_size, album_eligible=False)
        metrics = self._metrics_for(request)
        if metrics is not None:
            metrics.protocol = decision.name

        with _engine._timed(metrics, "thumb_ms"):
            preview_cm = _preview_file(request.source, request.mime_type, self.ffmpeg)
            preview = preview_cm.__enter__()
        prepared = []
        try:
            # Byte preparation is safe before durable intent: SaveFilePart does
            # not create a Telegram message. Each handle stays on its writer.
            for index, (offset, size) in enumerate(decision.segments):
                runtime, _peer = self._writer_for(target, index)
                name = f"{request.upload_name}.part{index + 1}" if len(decision.segments) > 1 else request.upload_name
                bucket = self._message_bucket(runtime)
                reader = SegmentReader(ext_path(request.source), offset, size, force_big=decision.force_big)
                try:
                    with runtime.file_slots:
                        with _engine._timed(metrics, "upload_ms"):
                            handle = runtime.worker.prepare_segment(
                                reader, size, name, force_big=decision.force_big,
                            )
                            uploaded_preview = (
                                runtime.worker.prepare_thumbnail(preview)
                                if preview is not None and index == 0 else None
                            )
                finally:
                    reader.close()
                prepared.append((index, size, name, runtime, handle, uploaded_preview, bucket))
        finally:
            preview_cm.__exit__(None, None, None)

        split = len(prepared) > 1
        group_id = uuid4().hex if split else None
        plans = []
        logical_ids = []
        try:
            # Full preflight: all intents must exist before the first send.
            for index, size, name, runtime, handle, uploaded_preview, bucket in prepared:
                logical_id = uuid4().hex
                logical_ids.append(logical_id)
                plan = self.coordinator.plan(
                    target=target,
                    logical_file_id=logical_id,
                    rpc_kind="messages.SendMedia",
                    group_id=group_id,
                    part_index=index if split else None,
                    uploader_id=int(runtime.telegram_user_id),
                    request_metadata=self._registration_metadata(
                        request,
                        size=size,
                        total_parts=len(prepared),
                        fingerprint=fingerprint,
                        has_thumbnail=uploaded_preview is not None,
                    ),
                )
                plans.append(plan)
                self.operation_cursors.planned(plan.record)
        except BaseException:
            # Zero message-producing RPCs have happened. Tombstone whatever
            # preflight managed to create; caller may safely replan later.
            for plan in plans:
                try:
                    self.coordinator.cancel_operation(plan.record)
                except Exception:
                    pass
            raise

        if split:
            self.group_barriers.create(group_id, [plan.operation_id for plan in plans])

        sending = []
        for plan in plans:
            record = self.coordinator._sending(plan)
            sending.append(record)
            self.operation_cursors.transition(record)
            if split:
                self.group_barriers.mark_durable(group_id, plan.operation_id)
        if split:
            if not self.group_barriers.ready(group_id):
                raise RuntimeError("split group was not durably armed")
            if not self.group_barriers.mark_send_started(group_id):
                raise RuntimeError("split group send already started")

        def send_one(payload):
            (prepared_item, plan, record, logical_id) = payload
            index, size, name, runtime, handle, uploaded_preview, bucket = prepared_item
            try:
                with _engine._timed(metrics, "message_ms"):
                    write = runtime.worker.send_uploaded_segment_to(
                        handle,
                        size,
                        name,
                        target_peer=plan.target_peer,
                        target_peer_key=target.target_peer_key,
                        random_id=plan.random_id,
                        preview=uploaded_preview,
                        mime_type=request.mime_type,
                        message_limiter=bucket,
                    )
            except BaseException:
                try:
                    recovering = self.api.patch_telegram_operation(plan.operation_id, {
                        "expected_operation_version": int(record.get("version") or 0),
                        "state": "recovering",
                    })
                    self.operation_cursors.transition(recovering)
                except Exception:
                    pass
                raise

            # Critical ordering: exact local evidence reaches disk before the
            # backend reconcile CAS can fail or the process can disappear.
            self.operation_cursors.destination(plan.operation_id, write)
            try:
                persisted = self.coordinator._persist_write(plan.operation_id, record, write)
            except BaseException as exc:
                raise AmbiguousTelegramWrite(
                    f"Telegram send {plan.operation_id} succeeded before reconcile failed"
                ) from exc
            result_version = int(persisted.get("result_version") or 0)
            self.operation_cursors.transition(persisted)
            if result_version:
                self.operation_cursors.result_version(plan.operation_id, result_version)
            part = UploadedPart(
                index=index,
                message_id=int(write.destination_message_id),
                file_id=logical_id,
                access_hash=write.access_hash,
                size=int(write.media_size),
                telegram_user_id=int(write.uploader_id),
                has_thumbnail=uploaded_preview is not None,
            )
            with self._parity_lock:
                self._durable_parts[logical_id] = {
                    "operation_id": plan.operation_id,
                    "group_id": group_id,
                    "result_version": result_version,
                }
            return part

        payloads = list(zip(prepared, plans, sending, logical_ids))
        with ThreadPoolExecutor(max_workers=min(self._segment_concurrency, len(payloads))) as executor:
            futures = [executor.submit(send_one, payload) for payload in payloads]
            parts = [future.result() for future in as_completed(futures)]
        _engine.assert_parts_cover_file(parts, request.logical_size)
        return parts

    # -- album path ------------------------------------------------------- #

    def _prepare_album(self, request):
        target = self._target(request)
        runtime, _peer = self._writer_for(target, 0)
        metrics = self._metrics_for(request)
        if metrics is not None:
            metrics.protocol = "album"
            metrics.observe_limiter(runtime)
        bucket = self._message_bucket(runtime)
        with _engine._timed(metrics, "thumb_ms"):
            preview_cm = _preview_file(request.source, request.mime_type, self.ffmpeg)
            preview = preview_cm.__enter__()
        try:
            with runtime.file_slots:
                with _engine._timed(metrics, "upload_ms"):
                    item = runtime.worker.prepare_album_item(
                        request.source,
                        request.logical_size,
                        request.upload_name,
                        request.mime_type,
                        preview,
                        message_limiter=bucket,
                    )
        finally:
            preview_cm.__exit__(None, None, None)
        runtime._storage_parity_engine = self
        with self._parity_lock:
            self._album_context[id(item)] = {
                "request": request,
                "target": target,
                "fingerprint": self._fingerprints.get(id(request)),
            }
        return runtime, item

    def _send_album_batch(self, runtime, items, *, timeout):
        contexts = [self._album_context[id(item)] for item in items]
        targets = [ctx["target"] for ctx in contexts]
        if any(target != targets[0] for target in targets[1:]):
            raise RuntimeError("album batch crossed frozen storage targets")
        target = targets[0]
        group_id = "album-" + uuid4().hex
        plans = []
        logical_ids = []
        try:
            for index, (item, ctx) in enumerate(zip(items, contexts)):
                request = ctx["request"]
                logical_id = uuid4().hex
                logical_ids.append(logical_id)
                metadata = self._registration_metadata(
                    request,
                    size=item.size,
                    total_parts=1,
                    fingerprint=ctx["fingerprint"],
                    has_thumbnail=item.has_thumbnail,
                )
                metadata.update({
                    "durable_group_id": group_id,
                    "prepared_media_fingerprint": prepared_media_fingerprint(item),
                })
                # Backend operation groups currently model split-file atomic
                # registration. Album children are independent logical files,
                # so their shared barrier is local/durable while each backend
                # operation remains independently registrable.
                plan = self.coordinator.plan(
                    target=target,
                    logical_file_id=logical_id,
                    rpc_kind="messages.SendMultiMedia",
                    group_id=None,
                    part_index=None,
                    uploader_id=int(runtime.telegram_user_id),
                    request_metadata=metadata,
                )
                plans.append(plan)
                self.operation_cursors.planned(plan.record)
        except BaseException:
            for plan in plans:
                try:
                    self.coordinator.cancel_operation(plan.record)
                except Exception:
                    pass
            raise

        self.group_barriers.create(group_id, [plan.operation_id for plan in plans])
        sending = []
        for plan in plans:
            record = self.coordinator._sending(plan)
            sending.append(record)
            self.operation_cursors.transition(record)
            self.group_barriers.mark_durable(group_id, plan.operation_id)
        if not self.group_barriers.ready(group_id):
            raise RuntimeError("album group was not durably armed")
        if not self.group_barriers.mark_send_started(group_id):
            raise RuntimeError("album group send already started")

        try:
            writes = runtime.worker.send_album_to(
                items,
                target_peer=plans[0].target_peer,
                target_peer_key=target.target_peer_key,
                random_ids=[plan.random_id for plan in plans],
                timeout=timeout,
                message_limiter=self._message_bucket(runtime),
            )
        except BaseException:
            for plan, record in zip(plans, sending):
                try:
                    recovering = self.api.patch_telegram_operation(plan.operation_id, {
                        "expected_operation_version": int(record.get("version") or 0),
                        "state": "recovering",
                    })
                    self.operation_cursors.transition(recovering)
                except Exception:
                    pass
            raise

        if len(writes) != len(items):
            raise AmbiguousTelegramWrite("album returned incomplete child mapping")
        parts = []
        for item, plan, record, logical_id, write in zip(items, plans, sending, logical_ids, writes):
            self.operation_cursors.destination(plan.operation_id, write)
            try:
                persisted = self.coordinator._persist_write(plan.operation_id, record, write)
            except BaseException as exc:
                raise AmbiguousTelegramWrite(
                    f"album child {plan.operation_id} succeeded before reconcile failed"
                ) from exc
            result_version = int(persisted.get("result_version") or 0)
            self.operation_cursors.transition(persisted)
            if result_version:
                self.operation_cursors.result_version(plan.operation_id, result_version)
            with self._parity_lock:
                self._durable_parts[logical_id] = {
                    "operation_id": plan.operation_id,
                    "group_id": None,
                    "album_group_id": group_id,
                    "result_version": result_version,
                }
            parts.append(UploadedPart(
                index=0,
                message_id=int(write.destination_message_id),
                file_id=logical_id,
                access_hash=write.access_hash,
                size=int(write.media_size),
                telegram_user_id=int(write.uploader_id),
                has_thumbnail=item.has_thumbnail,
            ))
        return parts

    # -- operation-backed registration ---------------------------------- #

    def register_result(self, result: TransferResult) -> None:
        request = result.request
        parts = sorted(result.parts, key=lambda part: part.index)
        _engine.assert_parts_cover_file(parts, request.logical_size)
        durable = []
        with self._parity_lock:
            for part in parts:
                info = self._durable_parts.get(part.file_id)
                if info is not None:
                    durable.append((part, dict(info)))
        if not durable:
            # Compatible dedup means the exact logical row already exists under
            # this name/parent/target. Re-registering would only rewrite it.
            self.api.invalidate(request.parent_id)
            self._log_complete(result.metrics if isinstance(result.metrics, _engine.TransferMetrics) else None)
            return
        if len(durable) != len(parts):
            raise _engine.CoverageError("mixed durable/legacy registration result")

        group_ids = {info.get("group_id") for _part, info in durable}
        non_null_groups = {value for value in group_ids if value}
        with _engine._timed(
            result.metrics if isinstance(result.metrics, _engine.TransferMetrics) else None,
            "register_ms",
        ):
            if len(parts) > 1:
                if len(non_null_groups) != 1:
                    raise _engine.CoverageError("split durable result lost its operation group")
                self.api.register_telegram_operation_group(next(iter(non_null_groups)))
            else:
                self.api.register_telegram_operation(durable[0][1]["operation_id"])

        for _part, info in durable:
            self.operation_cursors.remove(info["operation_id"])
            with self._parity_lock:
                self._durable_parts.pop(_part.file_id, None)
        for group_id in non_null_groups:
            self.group_barriers.remove(group_id)
        album_groups = {info.get("album_group_id") for _part, info in durable if info.get("album_group_id")}
        for group_id in album_groups:
            self.group_barriers.remove(group_id)
        self.api.invalidate(request.parent_id)
        self._log_complete(result.metrics if isinstance(result.metrics, _engine.TransferMetrics) else None)

    # -- restart recovery ------------------------------------------------ #

    def recover_pending(self):
        """Resume metadata from exact local/backend evidence; never sends."""
        recovered = []
        operations = list(self.api.list_telegram_operations(include_terminal=False) or [])
        for operation in operations:
            operation_id = str(operation["operation_id"])
            state = str(operation.get("state") or "")
            if state == "sent" and int(operation.get("result_version") or 0) > 0:
                if operation.get("group_id"):
                    # Group registration is attempted once all siblings are sent.
                    siblings = [row for row in operations if row.get("group_id") == operation.get("group_id")]
                    if siblings and all(
                        str(row.get("state")) == "sent" and int(row.get("result_version") or 0) > 0
                        for row in siblings
                    ):
                        self.api.register_telegram_operation_group(str(operation["group_id"]))
                else:
                    self.api.register_telegram_operation(operation_id)
                self.operation_cursors.remove(operation_id)
                recovered.append(operation_id)
                continue

            local_write = self.operation_cursors.write_from(operation_id)
            if local_write is None:
                try:
                    self.coordinator.recover_operation(operation, mapping_lookup=lambda *_: None)
                except AmbiguousTelegramWrite:
                    pass
                continue

            # Validate the exact destination currently contains the same media.
            location = local_write.location(location_version=1)
            valid = False
            try:
                for runtime, peer in self.pool.read_routes(location):
                    try:
                        runtime.worker.get_location_media(location, peer, refresh=True)
                    except Exception:
                        continue
                    valid = True
                    break
            except Exception:
                valid = False
            if not valid:
                try:
                    self.coordinator.recover_operation(operation, mapping_lookup=lambda *_: None)
                except AmbiguousTelegramWrite:
                    pass
                continue
            reconciled = self.coordinator.recover_operation(
                operation,
                mapping_lookup=lambda uploader, random_id, write=local_write: (
                    write if int(uploader) == int(write.uploader_id)
                    and int(random_id) == int(write.random_id) else None
                ),
            )
            self.operation_cursors.transition(reconciled)
            if int(reconciled.get("result_version") or 0):
                self.operation_cursors.result_version(operation_id, int(reconciled["result_version"]))
            recovered.append(operation_id)
        return tuple(recovered)
