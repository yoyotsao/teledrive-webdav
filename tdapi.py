"""TeleDrive REST client storage-parity layer over the legacy metadata client."""
from __future__ import annotations

import os
import tempfile
import threading
import uuid

import _tdapi_legacy as _legacy

for _name, _value in vars(_legacy).items():
    if _name not in {"__name__", "__loader__", "__package__", "__spec__"}:
        globals()[_name] = _value

from transfer_models import FileLocation, FrozenStorageTarget, LegacySavedMessagesLocation, ResolvedRemotePart

DIR_CACHE_VERSION = 3
_legacy.DIR_CACHE_VERSION = DIR_CACHE_VERSION


class LocationMetadataError(ValueError):
    pass


def _present(value):
    return value is not None and value != ""


def parse_file_location(row: dict):
    if not isinstance(row, dict):
        raise LocationMetadataError("file location row must be an object")
    chat_id = row.get("telegram_chat_id")
    canonical_fields = ("telegram_media_kind", "telegram_media_id", "telegram_media_size", "telegram_photo_variant")
    # The backend defaults location_version to zero, including rows without
    # canonical media metadata. That default alone is not a format marker.
    raw_version = row.get("location_version")
    version_evidence = _present(raw_version) and raw_version not in (0, "0")
    canonical = _present(chat_id) or version_evidence or any(_present(row.get(name)) for name in canonical_fields)
    message_id = row.get("telegram_message_id", row.get("message_id"))
    if not canonical:
        if message_id is None or not _present(row.get("file_id")):
            raise LocationMetadataError("legacy row has incomplete Saved Messages identity")
        return LegacySavedMessagesLocation(
            int(row.get("telegram_user_id") or 0), int(message_id), str(row["file_id"]),
            int(row.get("filesize") or row.get("telegram_media_size") or 0),
        )
    required = {
        "telegram_message_id": message_id,
        "telegram_media_kind": row.get("telegram_media_kind"),
        "telegram_media_id": row.get("telegram_media_id"),
        "telegram_media_size": row.get("telegram_media_size"),
        "location_version": row.get("location_version"),
    }
    missing = [key for key, value in required.items() if value is None or value == ""]
    if missing:
        raise LocationMetadataError("incomplete canonical location: " + ", ".join(missing))
    kind = str(required["telegram_media_kind"]).lower()
    if kind not in {"document", "photo"}:
        raise LocationMetadataError("unsupported canonical media kind")
    user_id = row.get("telegram_user_id")
    if not _present(chat_id):
        if user_id in (None, "", 0, "0"):
            raise LocationMetadataError("canonical Saved Messages location has no exact storage account")
        user_id, chat_id = int(user_id), None
    else:
        user_id, chat_id = (int(user_id) if user_id not in (None, "") else None), str(chat_id)
    try:
        size, version, message_id = int(required["telegram_media_size"]), int(required["location_version"]), int(message_id)
    except (TypeError, ValueError) as exc:
        raise LocationMetadataError("canonical location has non-numeric id/size/version") from exc
    if size < 0 or version < 0:
        raise LocationMetadataError("canonical location has invalid size/version")
    variant = row.get("telegram_photo_variant")
    if kind == "photo" and not _present(variant):
        raise LocationMetadataError("canonical photo location has no photo variant")
    if kind == "document" and _present(variant):
        raise LocationMetadataError("canonical document location cannot have photo variant")
    return FileLocation(chat_id, user_id, message_id, kind, str(required["telegram_media_id"]), size,
                        str(variant) if _present(variant) else None, version)


def _current_file_row(self, file_id):
    # /download is the byte-open authority in the WebDAV contract. During
    # backend rollout tolerate the older response shape only when it carries no
    # canonical evidence, which parse_file_location treats as explicit legacy.
    row = dict(self._call("GET", f"/files/{file_id}/download") or {})
    row.setdefault("file_id", file_id)
    return row


def _current_parts(self, entry):
    if not (entry.is_split and entry.split_group_id):
        row = self.current_file_row(entry.file_id)
        return [ResolvedRemotePart(entry.file_id, 0, parse_file_location(row))]
    body = self._call("GET", f"/files/by-split-group/{entry.split_group_id}") or {}
    rows = sorted(body.get("files") or [], key=lambda item: int(item.get("part_index") or 0))
    if not rows:
        raise ApiError(404, f"split group {entry.split_group_id} has no parts")
    out, seen = [], set()
    for ordinal, raw in enumerate(rows):
        row = dict(raw)
        index = int(row.get("part_index") if row.get("part_index") is not None else ordinal)
        if index in seen or not _present(row.get("file_id")):
            raise LocationMetadataError("invalid split physical metadata")
        seen.add(index)
        out.append(ResolvedRemotePart(str(row["file_id"]), index, parse_file_location(row)))
    return out


def _alias_payload(entry, row, *, filename, parent_id, part_index=None, total_parts=None, split_group_id=None):
    location = parse_file_location(row)
    payload = dict(filename=filename, filesize=location.media_size, mime_type=entry.mime,
                   message_id=location.telegram_message_id, file_id=uuid.uuid4().hex,
                   access_hash=row.get("access_hash"), parent_id=parent_id, file_hash=entry.file_hash,
                   has_thumbnail=bool(row.get("has_thumbnail", entry.has_thumbnail)),
                   is_split_file=total_parts is not None, original_name=filename,
                   part_index=part_index, total_parts=total_parts, split_group_id=split_group_id)
    if isinstance(location, LegacySavedMessagesLocation):
        payload["telegram_user_id"] = location.telegram_user_id
    else:
        payload.update(telegram_user_id=location.telegram_user_id, telegram_chat_id=location.telegram_chat_id,
                       telegram_media_kind=location.media_kind, telegram_media_id=location.media_id,
                       telegram_media_size=location.media_size, telegram_photo_variant=location.photo_variant)
    return payload


def _duplicate(self, entry, *, filename, parent_id):
    if not (entry.is_split and entry.split_group_id):
        row = self.current_file_row(entry.file_id)
        self._call("POST", "/files/register", payload=_alias_payload(entry, row, filename=filename, parent_id=parent_id))
    else:
        rows = sorted((self._call("GET", f"/files/by-split-group/{entry.split_group_id}") or {}).get("files") or [],
                      key=lambda item: int(item.get("part_index") or 0))
        deduped = []
        seen = set()
        for row in rows:
            location = parse_file_location(row)
            if isinstance(location, LegacySavedMessagesLocation) or location.telegram_chat_id is None:
                identity = ("me", location.telegram_user_id, location.telegram_message_id)
            else:
                identity = ("channel", location.telegram_chat_id, location.telegram_message_id)
            if identity in seen:
                continue
            seen.add(identity)
            deduped.append(row)
        if not deduped:
            raise ApiError(404, f"split group {entry.split_group_id} has no usable parts")
        if len(deduped) != len(rows):
            log.warning("split group %s had %s duplicate physical rows",
                        entry.split_group_id, len(rows) - len(deduped))
        group_id = uuid.uuid4().hex
        for index, row in enumerate(deduped):
            self._call("POST", "/files/register", payload=_alias_payload(
                entry, row, filename=filename, parent_id=parent_id, part_index=index,
                total_parts=len(deduped), split_group_id=group_id))
    self.invalidate(parent_id)


# Auth coordination is shared only by clients that point at the same backend
# and token file. A single process may host tests/tools with independent cache
# roots; letting one client's JWT overwrite another's makes an unrelated client
# skip its own challenge or refresh with the wrong token.
class _AuthState:
    def __init__(self):
        self.condition = threading.Condition(threading.Lock())
        self.login_lock = threading.Lock()
        self.refreshing = False
        self.error = None
        self.token = None


_AUTH_STATES_LOCK = threading.Lock()
_AUTH_STATES = {}


def _auth_state(self):
    endpoint = getattr(self.cfg, "api_base", None) or getattr(self.cfg, "base_url", None) or ""
    key = (str(endpoint).rstrip("/"), os.path.abspath(os.fspath(self._token_path)))
    with _AUTH_STATES_LOCK:
        state = _AUTH_STATES.get(key)
        if state is None:
            state = _AuthState()
            _AUTH_STATES[key] = state
        return state


def _persist_token_atomic(self, token):
    try:
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self._token_path.parent, prefix=self._token_path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(token)
            os.replace(name, self._token_path)
        except BaseException:
            try: os.unlink(name)
            except OSError: pass
            raise
    except OSError as exc:
        log.warning("could not persist refreshed backend token: %s", exc)


_original_init = TeleDriveClient.__init__
_original_login = TeleDriveClient.login


def _parity_init(self, cfg):
    _original_init(self, cfg)
    state = _auth_state(self)
    with state.condition:
        if state.token is None:
            state.token = self._token
        elif state.token:
            self._token = state.token


def _parity_login(self, force=False, *, _sleep=time.sleep):
    state = _auth_state(self)
    with state.condition:
        observed = state.token
        if observed and not force:
            self._token = observed
            return observed
    # _original_login has an instance lock. This second lock extends the
    # single-flight guarantee across separate clients that share one token file.
    with state.login_lock:
        with state.condition:
            if state.token and (not force or state.token != observed):
                self._token = state.token
                return state.token
        token = _original_login(self, force=force, _sleep=_sleep)
        with state.condition:
            state.token = token
            self._token = token
        _persist_token_atomic(self, token)
        return token


def _refresh_after_401(self, sent_token):
    state = _auth_state(self)
    with state.condition:
        if state.token and state.token != sent_token:
            self._token = state.token
            return state.token
        if state.refreshing:
            while state.refreshing:
                state.condition.wait()
            if state.token and state.token != sent_token:
                self._token = state.token
                return state.token
            if state.error is not None:
                raise state.error
        state.refreshing, state.error = True, None
    error = None
    try:
        resp = self._http_session().request("POST", f"{self.cfg.api_base}/auth/refresh",
                                            headers={"Authorization": f"Bearer {sent_token}"}, timeout=TIMEOUT)
        if resp.status_code == 200:
            token = str((resp.json() or {}).get("token") or "")
            if not token:
                raise ApiError(502, "refresh response did not contain a token")
        elif resp.status_code in (401, 403):
            # login may be replaced by an embedding/test seam. Publish its
            # returned token here instead of assuming the wrapper did it.
            token = self.login(force=True)
        else:
            raise ApiError(resp.status_code, resp.text[:300])
        with state.condition:
            state.token = token
            self._token = token
        _persist_token_atomic(self, token)
        return token
    except BaseException as exc:
        error = exc
        raise
    finally:
        with state.condition:
            state.error, state.refreshing = error, False
            state.condition.notify_all()


def _parity_call(self, method, path, *, params=None, payload=None, _auth_retry=True, _conn_retry=True):
    state = _auth_state(self)
    with state.condition:
        if state.token:
            self._token = state.token
    token = self._token or self.login()
    try:
        resp = self._http_session().request(method, f"{self.cfg.api_base}{path}", params=params, json=payload,
                                            headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
    except requests.exceptions.ConnectionError:
        if not _conn_retry: raise
        return self._call(method, path, params=params, payload=payload, _auth_retry=_auth_retry, _conn_retry=False)
    if resp.status_code == 401 and _auth_retry:
        self._refresh_after_401(token)
        return self._call(method, path, params=params, payload=payload, _auth_retry=False, _conn_retry=_conn_retry)
    if resp.status_code >= 400:
        raise ApiError(resp.status_code, resp.text[:300])
    return None if not resp.content else resp.json()


# -- storage topology -----------------------------------------------------
def _get_storage_target(self):
    return dict(self._call("GET", "/storage-target") or {})


def _list_accounts(self):
    return list((self._call("GET", "/accounts") or {}).get("accounts") or [])


def _freeze_storage_target(self):
    target = self.get_storage_target()
    accounts = self.list_accounts()
    linked = tuple(sorted(int(a["telegram_user_id"]) for a in accounts))
    primaries = [int(a["telegram_user_id"]) for a in accounts if bool(a.get("is_primary"))]
    if len(primaries) != 1:
        raise ApiError(409, "linked accounts do not contain exactly one primary")
    primary = primaries[0]
    mode = target.get("storage_mode")
    if mode not in {"saved_messages", "channel"}:
        raise ApiError(409, "unsupported storage target")
    channel_id = target.get("channel_id")
    if mode == "channel" and not channel_id:
        raise ApiError(409, "channel storage target has no channel id")
    peer_key = str(channel_id) if mode == "channel" else f"me:{primary}"
    return FrozenStorageTarget(mode, str(channel_id) if channel_id is not None else None, peer_key,
                               int(target.get("version") or 0), int(target.get("accounts_version") or 0),
                               primary, linked)


# -- durable operation REST ----------------------------------------------
def _create_operation(self, payload): return self._call("POST", "/telegram-operations", payload=payload)
def _list_operations(self, include_terminal=False):
    return list((self._call("GET", "/telegram-operations", params={"include_terminal": str(bool(include_terminal)).lower()}) or {}).get("operations") or [])
def _get_operation(self, operation_id): return self._call("GET", f"/telegram-operations/{operation_id}")
def _patch_operation(self, operation_id, payload): return self._call("PATCH", f"/telegram-operations/{operation_id}", payload=payload)
def _reconcile_operation_result(self, operation_id, payload): return self._call("POST", f"/telegram-operations/{operation_id}/reconcile-result", payload=payload)
def _register_operation(self, operation_id): return self._call("POST", f"/telegram-operations/{operation_id}/register", payload=None)
def _register_operation_group(self, group_id): return self._call("POST", f"/telegram-operation-groups/{group_id}/register", payload=None)
def _switch_file_location(self, file_id, payload): return self._call("POST", f"/file-locations/{file_id}/switch", payload=payload)
def _switch_file_location_group(self, parts): return self._call("POST", "/file-location-groups/switch", payload={"parts": list(parts)})


TeleDriveClient.__init__ = _parity_init
TeleDriveClient.login = _parity_login
TeleDriveClient._persist_token_atomic = _persist_token_atomic
TeleDriveClient._refresh_after_401 = _refresh_after_401
TeleDriveClient._call = _parity_call
TeleDriveClient.current_file_row = _current_file_row
TeleDriveClient.current_parts = _current_parts
TeleDriveClient.duplicate = _duplicate
TeleDriveClient.get_storage_target = _get_storage_target
TeleDriveClient.list_accounts = _list_accounts
TeleDriveClient.freeze_storage_target = _freeze_storage_target
TeleDriveClient.create_telegram_operation = _create_operation
TeleDriveClient.list_telegram_operations = _list_operations
TeleDriveClient.get_telegram_operation = _get_operation
TeleDriveClient.patch_telegram_operation = _patch_operation
TeleDriveClient.reconcile_telegram_operation_result = _reconcile_operation_result
TeleDriveClient.register_telegram_operation = _register_operation
TeleDriveClient.register_telegram_operation_group = _register_operation_group
TeleDriveClient.switch_file_location = _switch_file_location
TeleDriveClient.switch_file_location_group = _switch_file_location_group
