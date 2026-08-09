"""TeleDrive REST client: JWT handling, path resolution, metadata caches.

Only metadata crosses this module. Bytes are tgio.py's job.

Endpoint contract (all under /api/v1, all Bearer-authenticated except login):
    POST /auth/login                  {session_string} -> {token, user_id, ...}
    GET  /files?parent_id=&page_size=  split parts collapsed to part_index=0
    GET  /folders?parent_id=           folders only (the two listings are disjoint)
    GET  /files/{id}/download          message_id + access_hash
    GET  /files/by-split-group/{id}    every part, sorted by part_index
    POST /files/register               metadata for an MTProto-uploaded file
    POST /folders                      create folder
    GET  /files/check-hash?hash=       dedup lookup
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import requests

log = logging.getLogger("tdapi")

PAGE_SIZE = 10000
TIMEOUT = 60


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


@dataclass(frozen=True)
class Entry:
    """One row of TeleDrive metadata, as seen through a directory listing."""

    file_id: str
    name: str
    is_dir: bool
    size: int  # for split files this is part 0 only — use total_size()
    mtime: float
    mime: Optional[str] = None
    message_id: Optional[int] = None
    access_hash: Optional[str] = None
    is_split: bool = False
    split_group_id: Optional[str] = None
    file_hash: Optional[str] = None
    has_thumbnail: bool = False

    @property
    def real_size(self) -> Optional[int]:
        """True byte length, or None if the backend did not record one.

        ``filesize`` is the *padded* upload length: the uploader sends 512 KB
        parts and the backend stores parts x 512 KB, so it over-reports by up to
        one part (523,424 bytes observed). Advertising that padding makes clients
        read past the end of the Telegram document, where they wait out a long
        timeout and get zero bytes back — which is what stalls video players that
        look for a trailing moov atom. The ":<n>" suffix of ``file_hash`` is the
        real length.
        """
        return _hash_size(self.file_hash)


def _hash_size(file_hash) -> Optional[int]:
    text = str(file_hash or "")
    if ":" not in text:
        return None
    tail = text.rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _clip_parts(parts: List[Tuple[int, int]], real: Optional[int]) -> List[Tuple[int, int]]:
    """Trim a part table so it carries at most ``real`` bytes.

    Only the tail shrinks: every part but the last is a full segment. When the
    parts add up to less than ``real`` the file is under-registered and the table
    is returned untouched — there is nothing to trim, the bytes are simply gone.
    """
    if real is None:
        return parts
    out: List[Tuple[int, int]] = []
    used = 0
    for message_id, size in parts:
        if used >= real:
            break
        take = min(size, real - used)
        out.append((message_id, take))
        used += take
    return out


def _parse_time(value) -> float:
    if not value:
        return time.time()
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            # The backend stores naive UTC (datetime.utcnow).
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return time.time()


def _to_entry(row: dict) -> Entry:
    return Entry(
        file_id=row["file_id"],
        name=row["filename"],
        is_dir=bool(row.get("isDir")),
        size=int(row.get("filesize") or 0),
        mtime=_parse_time(row.get("created_at")),
        mime=row.get("mime_type"),
        message_id=row.get("telegram_message_id"),
        access_hash=row.get("access_hash"),
        is_split=bool(row.get("is_split_file")),
        split_group_id=row.get("split_group_id"),
        file_hash=row.get("file_hash"),
        has_thumbnail=bool(row.get("has_thumbnail")),
    )


class JsonStore:
    """Tiny thread-safe JSON dict persisted to disk.

    Holds facts that can never change once written (a split group's part table,
    a zip's central directory), so there is no invalidation problem.
    """

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict = {}
        self._dirty = False
        try:
            self._data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._data = {}

    def get(self, key: str):
        with self._lock:
            return self._data.get(key)

    def put(self, key: str, value, *, defer: bool = False) -> None:
        with self._lock:
            self._data[key] = value
            self._dirty = True
        if not defer:
            self.flush()

    def flush(self) -> None:
        """Merge this process's entries into the file and write it back.

        Writing ``self._data`` wholesale loses everything another process added
        since this one loaded the file. That is not hypothetical: a warm-up run
        filled 21,228 media-property entries while the bridge was up, and the
        bridge — still holding the older, nearly empty view — overwrote it back
        down to 2,371 on its next write.

        Every value here is derived from an immutable Telegram message, so two
        writers never disagree about a key and a plain merge is enough. Loading
        the file again on each flush costs a read the bridge does rarely, and
        only when something actually changed.
        """
        with self._lock:
            if not self._dirty:
                return
            mine = dict(self._data)
            self._dirty = False

        merged = {}
        try:
            merged = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(merged, dict):
                merged = {}
        except (OSError, ValueError):
            merged = {}
        merged.update(mine)

        with self._lock:
            self._data = merged

        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(merged), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:  # pragma: no cover - cache is best-effort
            log.warning("could not persist %s: %s", self._path.name, exc)


class TeleDriveClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self._session = requests.Session()
        self._token: Optional[str] = None
        self._auth_lock = threading.Lock()
        self._dir_cache: Dict[Optional[str], Tuple[float, List[Entry]]] = {}
        self._dir_lock = threading.Lock()
        cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        self._split_cache = JsonStore(cfg.cache_dir / "split_parts.json")
        self._token_path = cfg.cache_dir / "token.txt"
        try:
            self._token = self._token_path.read_text(encoding="utf-8").strip() or None
        except OSError:
            self._token = None

    # -- auth ------------------------------------------------------------- #

    def login(self, force: bool = False) -> str:
        """Exchange the Telethon StringSession for a JWT.

        The backend accepts Telethon StringSessions directly (TeleDrive
        routes.py:112), so the bridge needs no session format conversion.
        """
        with self._auth_lock:
            if self._token and not force:
                return self._token
            url = f"{self.cfg.api_base}/auth/login"
            resp = self._session.post(url, json={"session_string": self.cfg.session}, timeout=TIMEOUT)
            if resp.status_code != 200:
                raise ApiError(resp.status_code, resp.text[:300])
            token = resp.json()["token"]
            self._token = token
            try:
                self._token_path.write_text(token, encoding="utf-8")
            except OSError:
                pass
            log.info("obtained JWT from %s", self.cfg.base_url)
            return token

    def _call(self, method: str, path: str, *, params=None, payload=None, _retry=True):
        token = self._token or self.login()
        url = f"{self.cfg.api_base}{path}"
        resp = self._session.request(
            method,
            url,
            params=params,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
        if resp.status_code == 401 and _retry:
            # Expired or backend-restarted JWT: log in once more, then retry.
            log.info("JWT rejected — re-authenticating")
            self.login(force=True)
            return self._call(method, path, params=params, payload=payload, _retry=False)
        if resp.status_code >= 400:
            raise ApiError(resp.status_code, resp.text[:300])
        if not resp.content:
            return None
        return resp.json()

    # -- listings --------------------------------------------------------- #

    def _list_paginated(self, path: str, params: dict) -> List[dict]:
        rows: List[dict] = []
        page = 1
        while True:
            data = self._call("GET", path, params={**params, "page": page, "page_size": PAGE_SIZE})
            batch = data.get("files") or []
            rows.extend(batch)
            total = int(data.get("total") or 0)
            if len(batch) < PAGE_SIZE or len(rows) >= total:
                break
            page += 1
        return rows

    def list_dir(self, parent_id: Optional[str], *, fresh: bool = False) -> List[Entry]:
        """List one folder's children (folders + files), cached for dir_cache_seconds."""
        now = time.monotonic()
        if not fresh:
            with self._dir_lock:
                hit = self._dir_cache.get(parent_id)
            if hit and now - hit[0] < self.cfg.dir_cache_seconds:
                return hit[1]

        params = {} if parent_id is None else {"parent_id": parent_id}
        folders = self._list_paginated("/folders", params)
        files = self._list_paginated("/files", params)
        entries = [_to_entry(r) for r in folders] + [_to_entry(r) for r in files]
        with self._dir_lock:
            self._dir_cache[parent_id] = (now, entries)
        return entries

    def children_by_name(self, parent_id: Optional[str], *, fresh: bool = False) -> Dict[str, Entry]:
        """Name -> entry for one folder.

        TeleDrive has no UNIQUE(filename, parent_id), so duplicates are possible;
        the newest row wins and the shadowed ones are logged (plan risk #6).
        """
        out: Dict[str, Entry] = {}
        for entry in self.list_dir(parent_id, fresh=fresh):
            prev = out.get(entry.name)
            if prev is None:
                out[entry.name] = entry
            elif entry.mtime > prev.mtime:
                log.warning("duplicate name %r under %s — using the newer row %s", entry.name, parent_id, entry.file_id)
                out[entry.name] = entry
            else:
                log.warning("duplicate name %r under %s — ignoring older row %s", entry.name, parent_id, entry.file_id)
        return out

    def resolve(
        self, segments: Sequence[str], *, parent_id: Optional[str] = None, fresh: bool = False
    ) -> Optional[Entry]:
        """Walk path segments from ``parent_id`` (default: the drive root).

        ``[]`` means the starting folder itself, for which None is returned since
        the root has no Entry row.
        """
        entry: Optional[Entry] = None
        for i, name in enumerate(segments):
            children = self.children_by_name(parent_id, fresh=fresh)
            entry = children.get(name)
            if entry is None:
                return None
            if i < len(segments) - 1:
                if not entry.is_dir:
                    return None
                parent_id = entry.file_id
        return entry

    def invalidate(self, parent_id: Optional[str] = "__all__") -> None:
        with self._dir_lock:
            if parent_id == "__all__":
                self._dir_cache.clear()
            else:
                self._dir_cache.pop(parent_id, None)

    # -- split parts ------------------------------------------------------ #

    def parts_for(self, entry: Entry) -> List[Tuple[int, int]]:
        """``[(message_id, size), ...]`` making up a logical file, in order.

        Sizes are clipped to ``entry.real_size`` so the table never claims bytes
        the Telegram documents do not hold — see ``Entry.real_size``.
        """
        if not (entry.is_split and entry.split_group_id):
            if entry.message_id is None:
                raise ApiError(404, f"{entry.name} has no Telegram message")
            return _clip_parts([(entry.message_id, entry.size)], entry.real_size)

        cached = self._split_cache.get(entry.split_group_id)
        if cached:
            return _clip_parts([(int(m), int(s)) for m, s in cached], entry.real_size)

        data = self._call("GET", f"/files/by-split-group/{entry.split_group_id}")
        rows = sorted(data.get("files") or [], key=lambda r: r.get("part_index") or 0)
        parts: List[Tuple[int, int]] = []
        seen = set()
        for row in rows:
            message_id = row.get("telegram_message_id")
            # A genuine split never reuses a message across parts. Collapsing
            # duplicates guards against the historical dedup bug that registered
            # one message thousands of times.
            if message_id is None or message_id in seen:
                continue
            seen.add(message_id)
            parts.append((int(message_id), int(row.get("filesize") or 0)))
        if len(seen) != len(rows):
            log.warning("split group %s had %s duplicate part rows", entry.split_group_id, len(rows) - len(seen))
        if not parts:
            raise ApiError(404, f"split group {entry.split_group_id} has no usable parts")
        # Cache the raw table: clipping is cheap and depends on the entry.
        self._split_cache.put(entry.split_group_id, [[m, s] for m, s in parts])
        return _clip_parts(parts, entry.real_size)

    def total_size(self, entry: Entry) -> int:
        """Logical size, never larger than the bytes Telegram actually holds.

        For split files this sums every part (part 0's filesize is only the first
        segment) and the result is cached on disk. Both the sum and a plain
        entry's filesize are padded, so the real length wins when it is smaller.
        A file whose parts do not even cover the real length is under-registered
        (an upload that stopped after part 0); reporting the registered bytes
        keeps it readable instead of trailing megabytes that read back empty.
        """
        if not (entry.is_split and entry.split_group_id):
            available = entry.size
        else:
            available = sum(size for _, size in self.parts_for(entry))
        real = entry.real_size
        return min(available, real) if real is not None else available

    # -- writes (metadata only) ------------------------------------------- #

    def download_info(self, file_id: str) -> dict:
        return self._call("GET", f"/files/{file_id}/download")

    def check_hash(self, file_hash: str) -> dict:
        return self._call("GET", "/files/check-hash", params={"hash": file_hash})

    def create_folder(self, name: str, parent_id: Optional[str] = None) -> Entry:
        data = self._call("POST", "/folders", payload={"name": name, "parent_id": parent_id})
        self.invalidate(parent_id)
        return _to_entry(data)

    def ensure_folder(self, name: str, parent_id: Optional[str] = None) -> Entry:
        existing = self.children_by_name(parent_id, fresh=True).get(name)
        if existing and existing.is_dir:
            return existing
        return self.create_folder(name, parent_id)

    def register(
        self,
        *,
        filename: str,
        filesize: int,
        message_id: int,
        file_id: str,
        access_hash: Optional[str] = None,
        mime_type: Optional[str] = None,
        parent_id: Optional[str] = None,
        is_split_file: bool = False,
        original_name: Optional[str] = None,
        part_index: Optional[int] = None,
        total_parts: Optional[int] = None,
        split_group_id: Optional[str] = None,
        file_hash: Optional[str] = None,
    ) -> dict:
        payload = {
            "filename": filename,
            "filesize": filesize,
            "mime_type": mime_type,
            "message_id": message_id,
            "file_id": file_id,
            "access_hash": access_hash,
            "parent_id": parent_id,
            "has_thumbnail": False,
            "is_split_file": is_split_file,
            "original_name": original_name or filename,
            "part_index": part_index,
            "total_parts": total_parts,
            "split_group_id": split_group_id,
            "file_hash": file_hash,
        }
        data = self._call("POST", "/files/register", payload=payload)
        self.invalidate(parent_id)
        return data

    def trash(self, file_id: str) -> None:
        """Soft-delete: the backend stamps trashed_at on the whole subtree and
        keeps every Telegram message untouched. Listings already exclude
        trashed rows by default, so there is nothing else to filter here.
        """
        self._call("DELETE", f"/files/{file_id}")
        self.invalidate()

    def move(self, file_id: str, *, parent_id: Optional[str], filename: str) -> None:
        """Rename/reparent in place — children stay attached, they key off
        this row's stable file_id, never off its name or path."""
        self._call("PATCH", f"/files/{file_id}", payload={"parent_id": parent_id, "filename": filename})
        self.invalidate()

    def duplicate(self, entry: Entry, *, filename: str, parent_id: Optional[str]) -> None:
        """Metadata-only copy: a new row pointing at the same Telegram message(s).

        Safe because there is no UNIQUE(telegram_message_id) constraint — the
        existing hash-dedup path in register() already relies on the same
        fact to avoid re-uploading identical content.
        """
        if not (entry.is_split and entry.split_group_id):
            self.register(
                filename=filename,
                filesize=entry.size,
                message_id=entry.message_id,
                file_id=uuid.uuid4().hex,
                access_hash=entry.access_hash,
                mime_type=entry.mime,
                parent_id=parent_id,
                file_hash=entry.file_hash,
            )
            return

        # A fresh fetch, not parts_for(): that method's cache only keeps
        # (message_id, size) and is relied on elsewhere in that exact shape —
        # a copy needs each part's access_hash too, which parts_for() drops.
        data = self._call("GET", f"/files/by-split-group/{entry.split_group_id}")
        rows = sorted(data.get("files") or [], key=lambda r: r.get("part_index") or 0)
        deduped = []
        seen = set()
        for row in rows:
            message_id = row.get("telegram_message_id")
            if message_id is None or message_id in seen:
                continue
            seen.add(message_id)
            deduped.append(row)
        new_group = uuid.uuid4().hex
        total = len(deduped)
        for index, row in enumerate(deduped):
            self.register(
                filename=filename,
                filesize=row.get("filesize") or 0,
                message_id=row["telegram_message_id"],
                file_id=uuid.uuid4().hex,
                access_hash=row.get("access_hash"),
                mime_type=entry.mime,
                parent_id=parent_id,
                is_split_file=True,
                original_name=filename,
                part_index=index,
                total_parts=total,
                split_group_id=new_group,
                file_hash=entry.file_hash,
            )
