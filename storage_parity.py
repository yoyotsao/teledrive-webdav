"""Durable storage-target orchestration above the legacy upload engine.

This layer owns *message* side effects.  Byte preparation remains in the proven
legacy engine/worker, but no message-producing Telegram RPC may happen until an
owner-scoped backend operation has frozen its target, uploader and signed int64
random id.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Mapping, Optional, Sequence

from tdapi import LocationMetadataError, parse_file_location
from tgio import generate_random_id
from transfer_models import (
    DurableTelegramWrite,
    FileLocation,
    FrozenStorageTarget,
    LegacySavedMessagesLocation,
)


class AmbiguousTelegramWrite(RuntimeError):
    """A send may have succeeded, so retrying the RPC is forbidden."""


class DedupDisposition(str, Enum):
    SAME_TARGET = "same_target"
    RELOCATE_REQUIRED = "relocate_required"


@dataclass(frozen=True)
class PlannedOperation:
    operation_id: str
    uploader_id: int
    random_id: int
    target: FrozenStorageTarget
    target_peer: object
    record: Mapping[str, object]


@dataclass(frozen=True)
class DurableAlbumResult:
    group_id: str
    operations: tuple[Mapping[str, object], ...]
    writes: tuple[DurableTelegramWrite, ...]


def prepared_media_fingerprint(item) -> str:
    """Fingerprint the exact prepared Telegram document, not just source bytes."""
    body = {
        "telegram_user_id": int(item.telegram_user_id),
        "document_id": str(item.document_id),
        "access_hash": None if item.access_hash is None else str(item.access_hash),
        "file_reference": bytes(item.file_reference).hex(),
        "size": int(item.size),
        "mime_type": str(item.mime_type),
        "upload_name": str(item.upload_name),
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _operation_version(record: Mapping[str, object]) -> int:
    return int(record.get("version") or 0)


def _result_version(record: Mapping[str, object]) -> int:
    return int(record.get("result_version") or 0)


def _complete_mapping(record: Mapping[str, object]):
    mapping = record.get("mapping") or record.get("mapping_json")
    media = record.get("media_identity") or record.get("media_identity_json")
    if isinstance(mapping, str):
        try:
            mapping = json.loads(mapping)
        except ValueError:
            mapping = None
    if isinstance(media, str):
        try:
            media = json.loads(media)
        except ValueError:
            media = None
    if not isinstance(mapping, dict) or not isinstance(media, dict):
        return None
    required_mapping = {"uploader_id", "random_id", "target_peer_key", "destination_message_id"}
    required_media = {"destination_media_kind", "destination_media_id", "destination_size"}
    if not required_mapping.issubset(mapping) or not required_media.issubset(media):
        return None
    return mapping, media


def write_from_backend_record(record: Mapping[str, object]) -> Optional[DurableTelegramWrite]:
    complete = _complete_mapping(record)
    if complete is None:
        return None
    mapping, media = complete
    return DurableTelegramWrite(
        uploader_id=int(mapping["uploader_id"]),
        random_id=int(mapping["random_id"]),
        target_peer_key=str(mapping["target_peer_key"]),
        destination_message_id=int(mapping["destination_message_id"]),
        media_kind=str(media["destination_media_kind"]),
        media_id=str(media["destination_media_id"]),
        media_size=int(media["destination_size"]),
        access_hash=(str(media["destination_access_hash"])
                     if media.get("destination_access_hash") is not None else None),
        photo_variant=(str(media["destination_photo_variant"])
                       if media.get("destination_photo_variant") is not None else None),
    )


def classify_location_for_target(location, target: FrozenStorageTarget) -> DedupDisposition:
    if target.storage_mode == "channel":
        return (
            DedupDisposition.SAME_TARGET
            if isinstance(location, FileLocation)
            and location.telegram_chat_id == target.channel_id
            else DedupDisposition.RELOCATE_REQUIRED
        )
    exact_account = (
        location.telegram_user_id
        if isinstance(location, (FileLocation, LegacySavedMessagesLocation))
        else None
    )
    chat_id = location.telegram_chat_id if isinstance(location, FileLocation) else None
    return (
        DedupDisposition.SAME_TARGET
        if chat_id is None and int(exact_account or 0) == int(target.primary_account_id)
        else DedupDisposition.RELOCATE_REQUIRED
    )


def classify_dedup_row(row: Mapping[str, object], target: FrozenStorageTarget) -> DedupDisposition:
    """Fail closed on malformed canonical metadata instead of aliasing it."""
    location = parse_file_location(dict(row))
    return classify_location_for_target(location, target)


class StorageParityCoordinator:
    """Journal-first Telegram message coordinator.

    The class intentionally accepts a small API/pool surface so fake-backend
    acceptance tests exercise the same ordering as the real bridge.
    """

    def __init__(self, api, pool):
        self.api = api
        self.pool = pool

    def freeze_target(self) -> FrozenStorageTarget:
        return self.api.freeze_storage_target()

    def _writer(self, target: FrozenStorageTarget, uploader_id: Optional[int] = None):
        if target.storage_mode == "saved_messages":
            runtime = self.pool.primary_for_write(target.primary_account_id)
            if uploader_id is not None and int(runtime.telegram_user_id) != int(uploader_id):
                raise RuntimeError("durable uploader no longer matches frozen primary")
            try:
                from telethon.tl.types import InputPeerSelf
                peer = InputPeerSelf()
            except Exception:  # pragma: no cover - Telethon is a runtime dependency
                peer = "me"
            return runtime, peer
        writers = self.pool.channel_writers(target.channel_id, target.linked_account_ids)
        if uploader_id is not None:
            writers = tuple(pair for pair in writers if int(pair[0].telegram_user_id) == int(uploader_id))
        if not writers:
            raise RuntimeError("frozen Telegram writer is no longer available")
        runtime, access = writers[0]
        return runtime, access.peer

    def plan(
        self,
        *,
        target: FrozenStorageTarget,
        logical_file_id: str,
        rpc_kind: str,
        kind: str = "upload",
        group_id: Optional[str] = None,
        part_index: Optional[int] = None,
        uploader_id: Optional[int] = None,
        request_metadata: Optional[dict] = None,
        random_id: Optional[int] = None,
        operation_id: Optional[str] = None,
    ) -> PlannedOperation:
        runtime, target_peer = self._writer(target, uploader_id)
        uploader = int(runtime.telegram_user_id)
        random_id = int(generate_random_id() if random_id is None else random_id)
        operation_id = operation_id or uuid.uuid4().hex
        payload = {
            "operation_id": operation_id,
            "kind": kind,
            "logical_file_id": str(logical_file_id),
            "group_id": group_id,
            "part_index": part_index,
            "uploader_id": uploader,
            "target_kind": target.storage_mode,
            "target_channel_id": target.channel_id,
            "target_peer_key": target.target_peer_key,
            "created_target_version": int(target.target_version),
            "created_accounts_version": int(target.accounts_version),
            "random_id": str(random_id),
            "rpc_kind": str(rpc_kind),
            "request_metadata": dict(request_metadata or {}),
        }
        record = self.api.create_telegram_operation(payload)
        if not isinstance(record, Mapping):
            raise RuntimeError("backend did not durably create Telegram operation")
        return PlannedOperation(operation_id, uploader, random_id, target, target_peer, record)

    def _sending(self, planned: PlannedOperation) -> Mapping[str, object]:
        return self.api.patch_telegram_operation(planned.operation_id, {
            "expected_operation_version": _operation_version(planned.record),
            "state": "sending",
        })

    def _persist_write(self, operation_id: str, record: Mapping[str, object], write: DurableTelegramWrite):
        return self.api.reconcile_telegram_operation_result(operation_id, {
            "expected_operation_version": _operation_version(record),
            "mapping": write.mapping,
            "media_identity": write.media_identity,
        })

    def send_prepared_album(
        self,
        items: Sequence[object],
        *,
        target: Optional[FrozenStorageTarget] = None,
        logical_file_ids: Optional[Sequence[str]] = None,
        group_id: Optional[str] = None,
        timeout: float = 60.0,
        message_limiter=None,
    ) -> DurableAlbumResult:
        if not items:
            raise ValueError("album cannot be empty")
        target = target or self.freeze_target()
        group_id = group_id or uuid.uuid4().hex
        logical = tuple(logical_file_ids or [str(item.upload_name) for item in items])
        if len(logical) != len(items):
            raise ValueError("album requires one logical id per child")
        uploader_ids = {int(item.telegram_user_id) for item in items}
        if len(uploader_ids) != 1:
            raise ValueError("album children must share one uploader")
        uploader = next(iter(uploader_ids))
        plans = []
        for index, (item, logical_id) in enumerate(zip(items, logical)):
            plans.append(self.plan(
                target=target,
                logical_file_id=logical_id,
                rpc_kind="messages.SendMultiMedia",
                group_id=group_id,
                part_index=index,
                uploader_id=uploader,
                request_metadata={
                    "prepared_media_fingerprint": prepared_media_fingerprint(item),
                    "upload_name": str(item.upload_name),
                    "size": int(item.size),
                },
            ))
        sending = [self._sending(plan) for plan in plans]
        runtime, _peer = self._writer(target, uploader)
        try:
            writes = runtime.worker.send_album_to(
                items,
                target_peer=plans[0].target_peer,
                target_peer_key=target.target_peer_key,
                random_ids=[plan.random_id for plan in plans],
                timeout=timeout,
                message_limiter=message_limiter,
            )
        except BaseException:
            # The RPC may have crossed Telegram's commit point.  Recovery owns
            # the next action; this method must never replay the batch itself.
            for plan, record in zip(plans, sending):
                try:
                    self.api.patch_telegram_operation(plan.operation_id, {
                        "expected_operation_version": _operation_version(record),
                        "state": "recovering",
                    })
                except Exception:
                    pass
            raise
        if len(writes) != len(plans):
            raise AmbiguousTelegramWrite("album returned an incomplete durable mapping")
        persisted = []
        for plan, record, write in zip(plans, sending, writes):
            try:
                persisted.append(self._persist_write(plan.operation_id, record, write))
            except BaseException as exc:
                raise AmbiguousTelegramWrite(
                    f"Telegram album succeeded but operation {plan.operation_id} result commit failed"
                ) from exc
        return DurableAlbumResult(group_id, tuple(persisted), tuple(writes))

    def recover_operation(
        self,
        operation: Mapping[str, object],
        *,
        mapping_lookup: Optional[Callable[[int, int], object]] = None,
        readback: Optional[Callable[[Mapping[str, object], object], Optional[DurableTelegramWrite]]] = None,
    ) -> Mapping[str, object]:
        """Reconcile one ambiguous operation without ever issuing its send RPC."""
        state = str(operation.get("state") or "")
        if state in {"sent", "registered", "committed", "tombstoned"}:
            return operation
        if state == "uncertain":
            raise AmbiguousTelegramWrite("operation is uncertain and retry is blocked")
        current = operation
        operation_id = str(operation["operation_id"])
        if state == "sending":
            current = self.api.patch_telegram_operation(operation_id, {
                "expected_operation_version": _operation_version(operation),
                "state": "recovering",
            })
        uploader = int(current.get("uploader_id") or operation.get("uploader_id"))
        random_id = int(current.get("random_id") or operation.get("random_id"))
        evidence = write_from_backend_record(current)
        if evidence is None and mapping_lookup is not None:
            evidence = mapping_lookup(uploader, random_id)
        if evidence is not None and not isinstance(evidence, DurableTelegramWrite) and readback is not None:
            evidence = readback(current, evidence)
        if isinstance(evidence, DurableTelegramWrite):
            return self.api.reconcile_telegram_operation_result(operation_id, {
                "expected_operation_version": _operation_version(current),
                "mapping": evidence.mapping,
                "media_identity": evidence.media_identity,
            })
        if readback is not None:
            evidence = readback(current, evidence)
            if isinstance(evidence, DurableTelegramWrite):
                return self.api.reconcile_telegram_operation_result(operation_id, {
                    "expected_operation_version": _operation_version(current),
                    "mapping": evidence.mapping,
                    "media_identity": evidence.media_identity,
                })
        uncertain = self.api.patch_telegram_operation(operation_id, {
            "expected_operation_version": _operation_version(current),
            "state": "uncertain",
            "error_code": "incomplete_readback",
        })
        raise AmbiguousTelegramWrite(
            f"operation {operation_id} cannot be proved sent or unsent; retry blocked"
        )

    def cancel_operation(self, operation: Mapping[str, object]) -> Mapping[str, object]:
        state = str(operation.get("state") or "")
        if state == "planned":
            target_state = "tombstoned"
            extra = {"tombstone_reason": "cancelled_before_send"}
        elif state in {"sending", "recovering", "retryable"}:
            target_state = "uncertain"
            extra = {"error_code": "cancelled_during_possible_send"}
        else:
            return operation
        return self.api.patch_telegram_operation(str(operation["operation_id"]), {
            "expected_operation_version": _operation_version(operation),
            "state": target_state,
            **extra,
        })

    def register_album(self, result: DurableAlbumResult):
        return self.api.register_telegram_operation_group(result.group_id)

    def switch_relocated_file(
        self, file_id: str, *, expected_location_version: int,
        operation_id: str, result_version: int,
    ):
        return self.api.switch_file_location(file_id, {
            "expected_location_version": int(expected_location_version),
            "operation_id": str(operation_id),
            "result_version": int(result_version),
        })

    def switch_relocated_group(self, parts: Iterable[Mapping[str, object]]):
        """One backend call is the visibility boundary for all split parts."""
        return self.api.switch_file_location_group(tuple(dict(part) for part in parts))
