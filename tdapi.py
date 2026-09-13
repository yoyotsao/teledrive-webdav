"""TeleDrive REST client with canonical storage-location parity.

The pre-parity implementation is kept in :mod:`_tdapi_legacy` so existing CRUD,
listing and cache behaviour remains intact while canonical physical routing is
introduced behind explicit fresh-resolution APIs.
"""

from __future__ import annotations

import _tdapi_legacy as _legacy

# Re-export the complete legacy surface, including private helpers used by the
# existing offline tests. Methods defined below are then installed on the same
# TeleDriveClient class so legacy callers keep their type/identity.
for _name, _value in vars(_legacy).items():
    if _name not in {"__name__", "__loader__", "__package__", "__spec__"}:
        globals()[_name] = _value

from transfer_models import (  # noqa: E402
    FileLocation,
    LegacySavedMessagesLocation,
    ResolvedRemotePart,
)

# Physical rows written by older bridge versions are not canonical routing
# authority. Bump the directory/cache schema so stale rows are re-listed.
DIR_CACHE_VERSION = 3
_legacy.DIR_CACHE_VERSION = DIR_CACHE_VERSION


class LocationMetadataError(ValueError):
    """Raised when a row claims canonical routing but is incomplete/invalid."""


_CANONICAL_FIELDS = (
    "telegram_chat_id",
    "telegram_media_kind",
    "telegram_media_id",
    "telegram_media_size",
    "telegram_photo_variant",
    "location_version",
)


def _present(value) -> bool:
    return value is not None and value != ""


def parse_file_location(row: dict):
    """Parse backend physical metadata without guessing a different target.

    Rows with no canonical location data use the explicit historical Saved
    Messages compatibility identity. Once a row contains canonical location
    evidence, incomplete metadata fails closed; a channel row never falls back
    to Saved Messages.
    """
    if not isinstance(row, dict):
        raise LocationMetadataError("file location row must be an object")

    chat_id = row.get("telegram_chat_id")
    canonical = _present(chat_id) or any(
        _present(row.get(name)) for name in _CANONICAL_FIELDS if name != "telegram_chat_id"
    )

    message_id = row.get("telegram_message_id", row.get("message_id"))
    if not canonical:
        if message_id is None:
            raise LocationMetadataError("legacy row has no Telegram message id")
        file_id = row.get("file_id")
        if not _present(file_id):
            raise LocationMetadataError("legacy row has no file_id")
        return LegacySavedMessagesLocation(
            telegram_user_id=int(row.get("telegram_user_id") or 0),
            telegram_message_id=int(message_id),
            file_id=str(file_id),
            media_size=int(row.get("filesize") or row.get("telegram_media_size") or 0),
        )

    required = {
        "telegram_message_id": message_id,
        "telegram_media_kind": row.get("telegram_media_kind"),
        "telegram_media_id": row.get("telegram_media_id"),
        "telegram_media_size": row.get("telegram_media_size"),
        "location_version": row.get("location_version"),
    }
    missing = [name for name, value in required.items() if not _present(value) and value != 0]
    if missing:
        raise LocationMetadataError("incomplete canonical location: " + ", ".join(missing))

    media_kind = str(required["telegram_media_kind"]).lower()
    if media_kind not in {"document", "photo"}:
        raise LocationMetadataError(f"unsupported canonical media kind: {media_kind}")

    user_id = row.get("telegram_user_id")
    if not _present(chat_id):
        # Canonical Saved Messages must name the exact storage account. Account
        # id 0 belongs only to the explicit legacy compatibility path above.
        if user_id in (None, "", 0, "0"):
            raise LocationMetadataError("canonical Saved Messages location has no exact storage account")
        parsed_user_id = int(user_id)
        parsed_chat_id = None
    else:
        parsed_user_id = int(user_id) if user_id not in (None, "") else None
        parsed_chat_id = str(chat_id)

    try:
        media_size = int(required["telegram_media_size"])
        location_version = int(required["location_version"])
        parsed_message_id = int(message_id)
    except (TypeError, ValueError) as exc:
        raise LocationMetadataError("canonical location has non-numeric id/size/version") from exc
    if media_size < 0 or location_version < 1:
        raise LocationMetadataError("canonical location has invalid size/version")

    return FileLocation(
        telegram_chat_id=parsed_chat_id,
        telegram_user_id=parsed_user_id,
        telegram_message_id=parsed_message_id,
        media_kind=media_kind,
        media_id=str(required["telegram_media_id"]),
        media_size=media_size,
        photo_variant=(
            str(row["telegram_photo_variant"])
            if row.get("telegram_photo_variant") not in (None, "")
            else None
        ),
        location_version=location_version,
    )


def _current_file_row(self, file_id: str) -> dict:
    row = dict(self._call("GET", f"/files/{file_id}/download") or {})
    row.setdefault("file_id", file_id)
    return row


def _current_parts(self, entry):
    """Return fresh physical locations immediately before a byte open."""
    if not (entry.is_split and entry.split_group_id):
        row = self.current_file_row(entry.file_id)
        return [ResolvedRemotePart(entry.file_id, 0, parse_file_location(row))]

    body = self._call("GET", f"/files/by-split-group/{entry.split_group_id}") or {}
    rows = sorted(body.get("files") or [], key=lambda item: int(item.get("part_index") or 0))
    if not rows:
        raise ApiError(404, f"split group {entry.split_group_id} has no parts")
    out = []
    seen_indexes = set()
    for ordinal, raw in enumerate(rows):
        row = dict(raw)
        part_index = int(row.get("part_index") if row.get("part_index") is not None else ordinal)
        if part_index in seen_indexes:
            raise LocationMetadataError(f"duplicate split part_index {part_index}")
        seen_indexes.add(part_index)
        file_id = row.get("file_id")
        if not _present(file_id):
            raise LocationMetadataError(f"split part {part_index} has no file_id")
        out.append(ResolvedRemotePart(str(file_id), part_index, parse_file_location(row)))
    return out


TeleDriveClient.current_file_row = _current_file_row
TeleDriveClient.current_parts = _current_parts
