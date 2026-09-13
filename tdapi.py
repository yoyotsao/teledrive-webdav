"""TeleDrive REST client with canonical storage-location parity.

The pre-parity implementation is kept in :mod:`_tdapi_legacy` so existing CRUD,
listing and cache behaviour remains intact while canonical physical routing is
introduced behind explicit fresh-resolution APIs.
"""

from __future__ import annotations

import os
import tempfile
import threading
import uuid
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
    """Parse backend physical metadata without guessing a different target."""
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
        photo_variant=(str(row["telegram_photo_variant"])
                       if row.get("telegram_photo_variant") not in (None, "") else None),
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


def _alias_payload(entry, row, *, filename, parent_id, part_index=None, total_parts=None, split_group_id=None):
    location = parse_file_location(row)
    payload = {
        "filename": filename,
        "filesize": int(getattr(location, "media_size", row.get("filesize") or 0)),
        "mime_type": entry.mime,
        "message_id": int(location.telegram_message_id),
        "telegram_message_id": int(location.telegram_message_id),
        "file_id": uuid.uuid4().hex,
        "access_hash": row.get("access_hash"),
        "parent_id": parent_id,
        "file_hash": entry.file_hash,
        "has_thumbnail": bool(row.get("has_thumbnail", entry.has_thumbnail)),
        "is_split_file": total_parts is not None,
        "original_name": filename,
        "part_index": part_index,
        "total_parts": total_parts,
        "split_group_id": split_group_id,
    }
    if isinstance(location, LegacySavedMessagesLocation):
        payload["telegram_user_id"] = location.telegram_user_id
    else:
        payload.update(
            telegram_user_id=location.telegram_user_id,
            telegram_chat_id=location.telegram_chat_id,
            telegram_media_kind=location.media_kind,
            telegram_media_id=location.media_id,
            telegram_media_size=location.media_size,
            telegram_photo_variant=location.photo_variant,
        )
    return payload


def _duplicate(self, entry, *, filename: str, parent_id):
    """COPY from current canonical rows, never from listing/storage-target state."""
    if not (entry.is_split and entry.split_group_id):
        row = self.current_file_row(entry.file_id)
        self._call("POST", "/files/register",
                   payload=_alias_payload(entry, row, filename=filename, parent_id=parent_id))
        self.invalidate(parent_id)
        return

    body = self._call("GET", f"/files/by-split-group/{entry.split_group_id}") or {}
    rows = sorted(body.get("files") or [], key=lambda item: int(item.get("part_index") or 0))
    for row in rows:
        parse_file_location(row)
    new_group = uuid.uuid4().hex
    total = len(rows)
    for ordinal, row in enumerate(rows):
        index = int(row.get("part_index") if row.get("part_index") is not None else ordinal)
        self._call(
            "POST", "/files/register",
            payload=_alias_payload(entry, row, filename=filename, parent_id=parent_id,
                                   part_index=index, total_parts=total, split_group_id=new_group),
        )
    self.invalidate(parent_id)


# -- process-wide token refresh ------------------------------------------- #


def _ensure_refresh_state(self):
    if getattr(self, "_parity_refresh_condition", None) is None:
        # Assignment is idempotent under the GIL; if two first callers race,
        # the auth lock serializes their first login before either can receive
        # a 401. Normal construction calls this eagerly via patched __init__.
        self._parity_refresh_condition = threading.Condition(threading.Lock())
        self._parity_refreshing = False
        self._parity_refresh_error = None


def _persist_token_atomic(self, token: str) -> None:
    try:
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self._token_path.parent,
                                    prefix=self._token_path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(token)
            os.replace(name, self._token_path)
        except BaseException:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise
    except OSError as exc:
        log.warning("could not persist refreshed backend token: %s", exc)


_original_init = TeleDriveClient.__init__
_original_login = TeleDriveClient.login


def _parity_init(self, cfg):
    _original_init(self, cfg)
    _ensure_refresh_state(self)


def _parity_login(self, force=False, *, _sleep=time.sleep):
    token = _original_login(self, force=force, _sleep=_sleep)
    _persist_token_atomic(self, token)
    return token


def _refresh_after_401(self, sent_token: str) -> str:
    """Single-flight refresh; only the leader may fall back to bot challenge."""
    _ensure_refresh_state(self)
    condition = self._parity_refresh_condition
    with condition:
        if self._token and self._token != sent_token:
            return self._token
        if self._parity_refreshing:
            while self._parity_refreshing:
                condition.wait()
            if self._token and self._token != sent_token:
                return self._token
            if self._parity_refresh_error is not None:
                raise self._parity_refresh_error
        self._parity_refreshing = True
        self._parity_refresh_error = None

    error = None
    try:
        resp = self._http_session().request(
            "POST",
            f"{self.cfg.api_base}/auth/refresh",
            headers={"Authorization": f"Bearer {sent_token}"},
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            token = str((resp.json() or {}).get("token") or "")
            if not token:
                raise ApiError(502, "refresh response did not contain a token")
            self._token = token
            _persist_token_atomic(self, token)
            return token
        if resp.status_code in (401, 403):
            # Refresh grace was rejected. Exactly this single-flight leader runs
            # the challenge; waiters remain behind the same condition.
            return self.login(force=True)
        raise ApiError(resp.status_code, resp.text[:300])
    except BaseException as exc:
        error = exc
        raise
    finally:
        with condition:
            self._parity_refresh_error = error
            self._parity_refreshing = False
            condition.notify_all()


def _parity_call(self, method: str, path: str, *, params=None, payload=None,
                 _auth_retry=True, _conn_retry=True):
    token = self._token or self.login()
    url = f"{self.cfg.api_base}{path}"
    try:
        resp = self._http_session().request(
            method, url, params=params, json=payload,
            headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT,
        )
    except requests.exceptions.ConnectionError:
        if not _conn_retry:
            raise
        log.info("backend connection dropped on %s %s — retrying once", method, path)
        return self._call(method, path, params=params, payload=payload,
                          _auth_retry=_auth_retry, _conn_retry=False)
    if resp.status_code == 401 and _auth_retry:
        # Capture the exact token sent. If a sibling already replaced it, the
        # refresh helper returns current state without another network refresh.
        self._refresh_after_401(token)
        return self._call(method, path, params=params, payload=payload,
                          _auth_retry=False, _conn_retry=_conn_retry)
    if resp.status_code >= 400:
        raise ApiError(resp.status_code, resp.text[:300])
    if not resp.content:
        return None
    return resp.json()


TeleDriveClient.__init__ = _parity_init
TeleDriveClient.login = _parity_login
TeleDriveClient._persist_token_atomic = _persist_token_atomic
TeleDriveClient._refresh_after_401 = _refresh_after_401
TeleDriveClient._call = _parity_call
TeleDriveClient.current_file_row = _current_file_row
TeleDriveClient.current_parts = _current_parts
TeleDriveClient.duplicate = _duplicate
