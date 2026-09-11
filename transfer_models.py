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
    FAILED = "failed"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class AccountSpec:
    telegram_user_id: int
    session_path: Path = field(repr=False)


@dataclass(frozen=True)
class AttemptLease:
    task_id: str
    attempt_id: int
    account_id: int


@dataclass(frozen=True)
class UploadRpcToken:
    task_id: str
    attempt_id: int
    account_id: int
    part_index: int
    sequence: int

    @property
    def lease(self) -> "AttemptLease":
        return AttemptLease(self.task_id, self.attempt_id, self.account_id)


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
