"""Small, shared primitives for safe upload deduplication.

This module intentionally contains no upload scheduling or Telegram I/O.  It
only turns a ``check-hash`` response into one verified, account-routed sequence
of parts and coordinates same-batch callers of a physical upload.
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from threading import Lock
from typing import Callable, Dict, Mapping, Sequence


@dataclass(frozen=True)
class UploadedPart:
    """One immutable Telegram segment, including the account that owns it."""

    index: int
    message_id: int
    file_id: str
    access_hash: str | None
    size: int
    telegram_user_id: int
    has_thumbnail: bool = False


class CoverageError(ValueError):
    """Metadata parts do not describe exactly the logical file bytes."""


def assert_parts_cover_file(parts: Sequence[UploadedPart], size: int) -> None:
    """Reject metadata that would advertise too few or too many bytes."""
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
            return future, True

    def run(self, fingerprint: str, producer: Callable[[], list[UploadedPart]]) -> list[UploadedPart]:
        future, owner = self._claim(fingerprint)
        if owner:
            try:
                future.set_result(producer())
            except BaseException as exc:
                future.set_exception(exc)
                # A failed physical upload belongs to no later retry.  Wake
                # current followers through this future before freeing the key.
                with self._lock:
                    if self._claims.get(fingerprint) is future:
                        self._claims.pop(fingerprint, None)
        return future.result()
