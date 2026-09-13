"""Local crash cursor for the gap between Telegram success and backend CAS.

The backend remains authoritative.  This store only preserves exact evidence
that the current process already observed so restart recovery can validate the
specific destination message instead of replaying or scanning history.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Mapping, Optional

from transfer_models import DurableTelegramWrite


class OperationCursorStore:
    VERSION = 1

    def __init__(self, root: Path):
        self.root = Path(root) / ".parity-operations"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe(operation_id: str) -> str:
        value = str(operation_id)
        if not value or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in value):
            return uuid.uuid5(uuid.NAMESPACE_URL, value).hex
        return value

    def path(self, operation_id: str) -> Path:
        return self.root / f"{self._safe(operation_id)}.json"

    def _write(self, operation_id: str, body: dict) -> None:
        target = self.path(operation_id)
        temp = target.with_name(target.name + f".{uuid.uuid4().hex}.tmp")
        payload = json.dumps(body, sort_keys=True, separators=(",", ":"))
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        finally:
            try:
                temp.unlink()
            except OSError:
                pass

    def planned(self, operation: Mapping[str, object]) -> None:
        self._write(str(operation["operation_id"]), {
            "version": self.VERSION,
            "operation_id": str(operation["operation_id"]),
            "operation_version": int(operation.get("version") or 0),
            "state": str(operation.get("state") or "planned"),
            "uploader_id": int(operation["uploader_id"]),
            "random_id": int(operation["random_id"]),
            "target_peer_key": str(operation["target_peer_key"]),
        })

    def transition(self, operation: Mapping[str, object]) -> None:
        body = self.load(str(operation["operation_id"])) or {}
        body.update({
            "version": self.VERSION,
            "operation_id": str(operation["operation_id"]),
            "operation_version": int(operation.get("version") or 0),
            "state": str(operation.get("state") or body.get("state") or ""),
            "uploader_id": int(operation.get("uploader_id") or body.get("uploader_id")),
            "random_id": int(operation.get("random_id") or body.get("random_id")),
            "target_peer_key": str(operation.get("target_peer_key") or body.get("target_peer_key")),
        })
        self._write(str(operation["operation_id"]), body)

    def destination(self, operation_id: str, write: DurableTelegramWrite) -> None:
        body = self.load(operation_id) or {
            "version": self.VERSION,
            "operation_id": str(operation_id),
        }
        body["destination"] = {
            "uploader_id": int(write.uploader_id),
            "random_id": int(write.random_id),
            "target_peer_key": str(write.target_peer_key),
            "destination_message_id": int(write.destination_message_id),
            "media_kind": str(write.media_kind),
            "media_id": str(write.media_id),
            "media_size": int(write.media_size),
            "access_hash": write.access_hash,
            "photo_variant": write.photo_variant,
        }
        self._write(operation_id, body)

    def result_version(self, operation_id: str, value: int) -> None:
        body = self.load(operation_id) or {"version": self.VERSION, "operation_id": str(operation_id)}
        body["result_version"] = int(value)
        self._write(operation_id, body)

    def load(self, operation_id: str) -> Optional[dict]:
        try:
            body = json.loads(self.path(operation_id).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(body, dict) or body.get("version") != self.VERSION:
            return None
        return body

    def write_from(self, operation_id: str) -> Optional[DurableTelegramWrite]:
        body = self.load(operation_id)
        destination = body.get("destination") if body else None
        if not isinstance(destination, dict):
            return None
        required = {
            "uploader_id", "random_id", "target_peer_key", "destination_message_id",
            "media_kind", "media_id", "media_size",
        }
        if not required.issubset(destination):
            return None
        return DurableTelegramWrite(
            uploader_id=int(destination["uploader_id"]),
            random_id=int(destination["random_id"]),
            target_peer_key=str(destination["target_peer_key"]),
            destination_message_id=int(destination["destination_message_id"]),
            media_kind=str(destination["media_kind"]),
            media_id=str(destination["media_id"]),
            media_size=int(destination["media_size"]),
            access_hash=(str(destination["access_hash"]) if destination.get("access_hash") is not None else None),
            photo_variant=(str(destination["photo_variant"]) if destination.get("photo_variant") is not None else None),
        )

    def remove(self, operation_id: str) -> None:
        try:
            self.path(operation_id).unlink()
        except OSError:
            pass

    def pending(self) -> tuple[dict, ...]:
        out = []
        for path in sorted(self.root.glob("*.json")):
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if isinstance(body, dict) and body.get("version") == self.VERSION:
                out.append(body)
        return tuple(out)
