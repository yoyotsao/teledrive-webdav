"""Immutable value objects shared by routed transfer components."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class QueueStage(str, Enum):
    STAGING = "staging"
    PLANNING = "planning"
    UPLOADING = "uploading"
    SENDING = "sending"
    REGISTERING = "registering"
    RECOVERING = "recovering"
    UNCERTAIN = "uncertain"
    FAILED = "failed"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class FileLocation:
    """Canonical physical Telegram location for a backend file row."""

    telegram_chat_id: Optional[str]
    telegram_user_id: Optional[int]
    telegram_message_id: int
    media_kind: str
    media_id: str
    media_size: int
    photo_variant: Optional[str]
    location_version: int


@dataclass(frozen=True)
class LegacySavedMessagesLocation:
    """Explicit pre-canonical Saved Messages routing identity."""

    telegram_user_id: int
    telegram_message_id: int
    file_id: str
    media_size: int


PhysicalLocation = FileLocation | LegacySavedMessagesLocation


@dataclass(frozen=True)
class ResolvedRemotePart:
    """One logical part bound to the physical location used for byte reads."""

    file_id: str
    part_index: int
    location: PhysicalLocation

    @property
    def size(self) -> int:
        return self.location.media_size

    @property
    def message_id(self) -> int:
        """Compatibility view used by the unchanged seek/range table."""
        return self.location.telegram_message_id

    @property
    def telegram_user_id(self) -> int:
        """Legacy display identity only; channel routing never trusts this."""
        return int(self.location.telegram_user_id or 0)


def physical_location_key(location: PhysicalLocation) -> tuple[object, ...]:
    """Return a stable cache identity for bytes at *location*."""

    if isinstance(location, LegacySavedMessagesLocation):
        return (
            "legacy_saved_messages",
            location.telegram_user_id,
            location.telegram_message_id,
            location.file_id,
            location.media_size,
        )

    if location.telegram_chat_id is None:
        target = ("saved_messages", location.telegram_user_id)
    else:
        target = ("channel", location.telegram_chat_id)

    return (
        *target,
        location.telegram_message_id,
        location.media_kind,
        location.media_id,
        location.media_size,
        location.photo_variant,
        location.location_version,
    )


@dataclass(frozen=True)
class FrozenStorageTarget:
    storage_mode: str
    channel_id: Optional[str]
    target_peer_key: str
    target_version: int
    accounts_version: int
    primary_account_id: int
    linked_account_ids: tuple[int, ...]


@dataclass(frozen=True)
class DurableOperationIdentity:
    operation_id: str
    random_id: int
    uploader_id: int
    rpc_kind: str
    group_id: Optional[str] = None
    part_index: Optional[int] = None


@dataclass(frozen=True)
class DurableSendResult:
    location: FileLocation
    access_hash: Optional[str] = None


@dataclass(frozen=True)
class DurableOperationCursor:
    identity: DurableOperationIdentity
    target: FrozenStorageTarget
    operation_version: int
    state: str
    result: Optional[DurableSendResult] = None
    result_version: Optional[int] = None
    uncertain_reason: Optional[str] = None


@dataclass(frozen=True)
class StagingIdentity:
    logical_key: str
    transfer_id: str
    staging_generation: int
    source_path: str


@dataclass(frozen=True)
class GroupSendManifest:
    group_id: str
    target: FrozenStorageTarget
    children: tuple[DurableOperationIdentity, ...]
    send_armed: bool = False
    send_started: bool = False


@dataclass(frozen=True)
class AccountSpec:
    telegram_user_id: int
    label: str
    session: str = field(repr=False)


@dataclass(frozen=True)
class RemotePart:
    message_id: int
    size: int
    telegram_user_id: int
    file_id: str


@dataclass(frozen=True)
class UploadedPart:
    index: int
    message_id: int
    file_id: str
    access_hash: Optional[str]
    size: int
    telegram_user_id: int
    has_thumbnail: bool = False


@dataclass(frozen=True)
class TransferRequest:
    source: Path
    upload_name: str
    mime_type: str
    parent_id: Optional[str]
    logical_size: int
    allow_album: bool = True


@dataclass(frozen=True)
class TransferResult:
    request: TransferRequest
    fingerprint: str
    parts: tuple[UploadedPart, ...]
    #: Stage timings, carried here so registration can finish the one
    #: completion log line rather than emitting a second, partial one.
    #: Excluded from comparison: two aliases of the same upload are the same
    #: result even though each measured its own check-hash round trip.
    metrics: Optional[object] = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class PreparedAlbumItem:
    source: Path
    upload_name: str
    mime_type: str
    size: int
    telegram_user_id: int
    document_id: str
    access_hash: Optional[str]
    has_thumbnail: bool
    file_reference: bytes = field(default=b"", repr=False)
