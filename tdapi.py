"""TeleDrive REST client: JWT handling, path resolution, metadata caches.

Only metadata crosses this module. Bytes are tgio.py's job.

Endpoint contract (all under /api/v1, all Bearer-authenticated except the challenge):
    POST /auth/challenge              {} -> {nonce, bot_username, expires_in}
    POST /auth/verify                 {nonce} -> {token, ...}; 202 while waiting
    GET  /files?parent_id=&page_size=  split parts collapsed to part_index=0
    GET  /folders?parent_id=           folders only (the two listings are disjoint)
    GET  /files/{id}/download          message_id + access_hash
    GET  /files/by-split-group/{id}    every part, sorted by part_index
    POST /files/register               metadata for an MTProto-uploaded file
    POST /folders                      create folder
    GET  /files/check-hash?hash=       dedup lookup
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import requests

from transfer_models import RemotePart

log = logging.getLogger("tdapi")

PAGE_SIZE = 10000
TIMEOUT = 60

# Bumped whenever the shape stored in meta/dirs/ changes, so an old file is
# re-listed instead of being read as if it meant the same thing.
DIR_CACHE_VERSION = 2
# The backend expires a challenge nonce after 120s (bot_challenge.TTL_SECONDS);
# give up a shade earlier rather than redeem one it has already pruned.
CHALLENGE_TTL = 110
CHALLENGE_POLL = 1.0


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
    telegram_user_id: int = 0

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


def _clip_remote_parts(parts: Sequence[RemotePart], real: Optional[int]) -> List[RemotePart]:
    """Apply the existing size clipping rule without dropping routing metadata."""
    clipped = _clip_parts([(part.message_id, part.size) for part in parts], real)
    return [
        RemotePart(part.message_id, size, part.telegram_user_id, part.file_id)
        for part, (_, size) in zip(parts, clipped)
    ]


def _cached_remote_parts(cached, fallback_file_id: str) -> List[RemotePart]:
    """Decode current and historical split-cache rows.

    Earlier cache files held ``[message_id, size]`` only.  They have no account
    information, so zero is the deliberate legacy route; their containing file
    remains the only usable file identity.
    """
    parts = []
    for row in cached:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        message_id, size = row[:2]
        telegram_user_id = row[2] if len(row) > 2 else 0
        file_id = row[3] if len(row) > 3 else fallback_file_id
        parts.append(RemotePart(
            int(message_id),
            int(size),
            int(telegram_user_id or 0),
            str(file_id or fallback_file_id),
        ))
    return parts


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
        telegram_user_id=int(row.get("telegram_user_id") or 0),
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
        self._http_local = threading.local()
        self._token: Optional[str] = None
        self._dm_sender = None  # set_dm_sender(); the bot challenge needs a Telegram client
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

    def _http_session(self) -> requests.Session:
        """The requests session owned by this calling thread.

        ``requests.Session`` pools connections but is not safe to share among
        WsgiDAV's request threads. JWT and login coordination intentionally
        remain client-wide; only the HTTP transport is thread-local.
        """
        session = getattr(self._http_local, "session", None)
        if session is None:
            session = requests.Session()
            self._http_local.session = session
        return session

    # -- auth ------------------------------------------------------------- #

    def _post_unauth(self, path: str, payload: dict, *, waiting_ok: bool = False):
        """POST without a Bearer token. The challenge handshake is the only
        thing that runs before there is one -- and the only seam tests replace.

        Returns None for the backend's 202 "keep polling" when `waiting_ok`.
        """
        resp = self._http_session().post(f"{self.cfg.api_base}{path}", json=payload, timeout=TIMEOUT)
        if waiting_ok and resp.status_code == 202:
            return None
        if resp.status_code != 200:
            raise ApiError(resp.status_code, resp.text[:300])
        return resp.json()

    def set_dm_sender(self, sender) -> None:
        """Wire in the Telegram client that will DM the login nonce.

        Kept as an injected callable rather than a TelegramWorker import: this
        module is the metadata half and has no business connecting to MTProto.
        """
        self._dm_sender = sender

    def login(self, force: bool = False, *, _sleep=time.sleep) -> str:
        """Get a JWT through the backend's bot challenge.

        The old handshake (POST /auth/login with the Telethon StringSession) was
        removed by TeleDrive's "restore the metadata-only boundary" change --
        handing a backend an auth_key gives it the whole Telegram account, which
        is exactly the boundary this bridge exists to keep. What replaced it:
        ask for a nonce, DM it to the named bot *from the account being
        authenticated*, and trade the nonce back for a JWT. The proof of
        identity is the ``from`` on the update the bot receives, so nothing
        secret crosses the wire.

        That flow is interactive on the web, but not here -- the bridge already
        holds the user's Telethon client, so it sends its own DM and the whole
        thing stays headless. The cost is one bot DM per token; JWTs last 24h
        and survive a restart via token.txt, so that is roughly one a day.
        """
        with self._auth_lock:
            if self._token and not force:
                return self._token
            if self._dm_sender is None:
                raise RuntimeError(
                    "cannot log in: no Telegram client wired in. The backend's bot "
                    "challenge needs the nonce DMed from the account itself — call "
                    "set_dm_sender(worker.send_dm) with a started TelegramWorker."
                )

            challenge = self._post_unauth("/auth/challenge", {})
            nonce = challenge["nonce"]
            bot = challenge["bot_username"]
            deadline = time.monotonic() + min(int(challenge.get("expires_in") or CHALLENGE_TTL), CHALLENGE_TTL)

            # Exact text, no prefix: the backend matches the message body against
            # its pending nonces (bot_challenge.ingest_updates).
            self._dm_sender(bot, nonce)

            while True:
                verified = self._post_unauth("/auth/verify", {"nonce": nonce}, waiting_ok=True)
                if verified is not None:
                    break
                # None = 202: the bot's getUpdates long-poll has not delivered
                # our DM yet. Anything else already raised.
                if time.monotonic() >= deadline:
                    raise ApiError(408, f"login challenge {nonce} was never seen by @{bot}")
                _sleep(CHALLENGE_POLL)

            token = verified["token"]
            self._token = token
            try:
                self._token_path.write_text(token, encoding="utf-8")
            except OSError:
                pass
            log.info("obtained JWT from %s via @%s", self.cfg.base_url, bot)
            return token

    def _call(self, method: str, path: str, *, params=None, payload=None,
              _auth_retry=True, _conn_retry=True):
        token = self._token or self.login()
        url = f"{self.cfg.api_base}{path}"
        try:
            resp = self._http_session().request(
                method,
                url,
                params=params,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=TIMEOUT,
            )
        except requests.exceptions.ConnectionError:
            # A pooled keep-alive socket the backend had already closed. uvicorn
            # drops an idle connection after a few seconds, so any gap between
            # metadata calls leaves one behind and the next request dies before
            # the server reads a byte — which makes this safe to repeat even for
            # a POST: nothing reached the app to be applied twice.
            #
            # Not cosmetic. This surfaces as a 500 on /rpc/thumb, and a 500 there
            # is not a slow preview: the DLL cannot tell it from "no thumbnail
            # exists", delegates to the built-in handler, and that reads the
            # whole original off Telegram. One dropped socket was measured as a
            # 6.8 MB sequential download, and a few of those starve the pool into
            # FLOOD_WAIT — which is what a folder that "just spins" looks like.
            if not _conn_retry:
                raise
            log.info("backend connection dropped on %s %s — retrying once", method, path)
            # Only the connection budget is spent: a fresh socket that then comes
            # back 401 still deserves its one re-login.
            return self._call(method, path, params=params, payload=payload,
                              _auth_retry=_auth_retry, _conn_retry=False)
        if resp.status_code == 401 and _auth_retry:
            # Expired or backend-restarted JWT: log in once more, then retry.
            log.info("JWT rejected — re-authenticating")
            self.login(force=True)
            return self._call(method, path, params=params, payload=payload,
                              _auth_retry=False, _conn_retry=_conn_retry)
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
        """List one folder's children (folders + files), cached for dir_cache_seconds.

        Three layers, because the backend is the slow part and it is meant to be:
        it is reached over the internet on purpose (so that "bridge here, backend
        elsewhere" is what gets tested), and one call measured 0.52s steady —
        0.36-1.2s of that connect plus TLS when the connection is new. Nothing in
        this file can make a round trip cheaper, so all three layers are about
        making fewer of them.

        * memory, for the rest of this session
        * disk (``meta/dirs/``), so the first click after a restart is free —
          the sweep re-lists the whole tree every pass anyway (with ``fresh``),
          which is what keeps these files current
        * the backend, with ``/folders`` and ``/files`` in flight together

        Both caches honour the same ``dir_cache_seconds``: they answer the same
        question and go stale at the same rate, so a second TTL would be a
        distinction without a difference. Web-UI changes still need
        ``/rpc/forget`` (and ``rclone rc vfs/forget``) exactly as before.
        """
        now = time.monotonic()
        stamped = time.time()
        if not fresh:
            with self._dir_lock:
                hit = self._dir_cache.get(parent_id)
            if hit and now - hit[0] < self.cfg.dir_cache_seconds:
                return hit[1]
            rows = self._dir_from_disk(parent_id, stamped)
            if rows is not None:
                entries = [_to_entry(r) for r in rows]
                with self._dir_lock:
                    self._dir_cache[parent_id] = (now, entries)
                return entries

        params = {} if parent_id is None else {"parent_id": parent_id}
        folders, files = self._list_both(params)
        entries = [_to_entry(r) for r in folders] + [_to_entry(r) for r in files]
        with self._dir_lock:
            self._dir_cache[parent_id] = (now, entries)
        self._dir_to_disk(parent_id, folders + files, stamped)
        return entries

    def _list_both(self, params: dict) -> Tuple[List[dict], List[dict]]:
        """``/folders`` and ``/files`` at the same time rather than one after the other.

        They are independent GETs against a backend that answers in about half a
        second, so doing them in sequence is what made every folder click cost
        1.06s — measured, and independent of how many files the folder holds
        (2 items and 84 items cost the same), which is how you can tell it is
        round trips and not volume.

        One extra thread per listing rather than a pool: a pool sized for this
        would serialise the wsgidav worker threads against each other, and a
        thread costs microseconds against a half-second call.
        """
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self._list_paginated, "/files", params)
            folders = self._list_paginated("/folders", params)
            return folders, pending.result()

    # -- the listing cache on disk ---------------------------------------- #

    def _dir_disk_path(self, parent_id: Optional[str]) -> Path:
        key = parent_id or "__root__"
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", key):
            key = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.cfg.cache_dir / "dirs" / f"{key}.json"

    def _dir_from_disk(self, parent_id: Optional[str], stamped: float) -> Optional[List[dict]]:
        """The rows for one folder off disk, or None if missing, old or foreign.

        One file per folder, like ``thumbs/``, not one shared JSON: a listing is
        the one cached thing here that *changes*, so JsonStore's merge-on-flush
        (last writer wins, safe only because its values never change) does not
        apply — and a shared file would mean rewriting megabytes per folder
        during a sweep of thousands.
        """
        try:
            blob = json.loads(self._dir_disk_path(parent_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(blob, dict) or blob.get("v") != DIR_CACHE_VERSION:
            return None  # written by an older shape: re-list rather than guess
        try:
            age = stamped - float(blob.get("at") or 0)
        except (TypeError, ValueError):
            return None
        if age < 0 or age > self.cfg.dir_cache_seconds:
            return None
        rows = blob.get("rows")
        return rows if isinstance(rows, list) else None

    def _dir_to_disk(self, parent_id: Optional[str], rows: List[dict], stamped: float) -> None:
        path = self._dir_disk_path(parent_id)
        blob = {"v": DIR_CACHE_VERSION, "at": stamped, "rows": rows}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}-{threading.get_ident()}.part")
            tmp.write_text(json.dumps(blob), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:  # pragma: no cover - the cache is best-effort
            log.warning("could not cache the listing for %s: %s", parent_id, exc)

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
        """Forget cached listings, in memory *and* on disk.

        Dropping only the memory copy would make ``/rpc/forget`` a no-op that
        looks like it worked: the very next listing reads back off disk exactly
        what was just forgotten, and the web-UI upload the person was trying to
        make visible stays invisible for another hour.
        """
        with self._dir_lock:
            if parent_id == "__all__":
                self._dir_cache.clear()
            else:
                self._dir_cache.pop(parent_id, None)
        try:
            if parent_id == "__all__":
                for path in (self.cfg.cache_dir / "dirs").glob("*.json"):
                    path.unlink(missing_ok=True)
            else:
                self._dir_disk_path(parent_id).unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - best effort
            log.warning("could not clear the listing cache on disk: %s", exc)

    # -- split parts ------------------------------------------------------ #

    def parts_for(self, entry: Entry) -> List[RemotePart]:
        """Routed Telegram parts making up a logical file, in order.

        Sizes are clipped to ``entry.real_size`` so the table never claims bytes
        the Telegram documents do not hold — see ``Entry.real_size``.
        """
        if not (entry.is_split and entry.split_group_id):
            if entry.message_id is None:
                raise ApiError(404, f"{entry.name} has no Telegram message")
            return _clip_remote_parts(
                [RemotePart(int(entry.message_id), entry.size, entry.telegram_user_id, entry.file_id)],
                entry.real_size,
            )

        cache_key = f"{entry.telegram_user_id}:{entry.file_id}"
        cached = self._split_cache.get(cache_key)
        # Old single-account caches were keyed only by split group. They are
        # safe to reuse only for legacy route zero; a nonzero account must never
        # inherit bytes cached for another account with the same identifiers.
        if cached is None and entry.telegram_user_id == 0:
            cached = self._split_cache.get(entry.split_group_id)
        if cached:
            return _clip_remote_parts(_cached_remote_parts(cached, entry.file_id), entry.real_size)

        data = self._call("GET", f"/files/by-split-group/{entry.split_group_id}")
        rows = sorted(data.get("files") or [], key=lambda r: r.get("part_index") or 0)
        parts: List[RemotePart] = []
        seen = set()
        for row in rows:
            message_id = row.get("telegram_message_id")
            # A genuine split never reuses a message across parts. Collapsing
            # duplicates guards against the historical dedup bug that registered
            # one message thousands of times.
            if message_id is None or message_id in seen:
                continue
            seen.add(message_id)
            parts.append(RemotePart(
                int(message_id),
                int(row.get("filesize") or 0),
                int(row.get("telegram_user_id") or 0),
                str(row.get("file_id") or entry.file_id),
            ))
        if len(seen) != len(rows):
            log.warning("split group %s had %s duplicate part rows", entry.split_group_id, len(rows) - len(seen))
        if not parts:
            raise ApiError(404, f"split group {entry.split_group_id} has no usable parts")
        # Cache the raw table: clipping is cheap and depends on the entry.
        self._split_cache.put(cache_key, [
            [part.message_id, part.size, part.telegram_user_id, part.file_id]
            for part in parts
        ])
        return _clip_remote_parts(parts, entry.real_size)

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
            available = sum(part.size for part in self.parts_for(entry))
        real = entry.real_size
        return min(available, real) if real is not None else available

    # -- writes (metadata only) ------------------------------------------- #

    def download_info(self, file_id: str) -> dict:
        return self._call("GET", f"/files/{file_id}/download")

    def check_hash(self, file_hash: str) -> dict:
        return self._call("GET", "/files/check-hash", params={"hash": file_hash})

    def linked_account_ids(self) -> set[int]:
        body = self._call("GET", "/accounts")
        return {int(row["telegram_user_id"]) for row in body.get("accounts", [])}

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
        filename: str,
        filesize: int,
        mime_type: Optional[str],
        message_id: int,
        file_id: str,
        access_hash: Optional[str] = None,
        *,
        telegram_user_id: int = 0,
        parent_id: Optional[str] = None,
        is_split_file: bool = False,
        original_name: Optional[str] = None,
        part_index: Optional[int] = None,
        total_parts: Optional[int] = None,
        split_group_id: Optional[str] = None,
        file_hash: Optional[str] = None,
        has_thumbnail: bool = False,
    ) -> dict:
        payload = {
            "filename": filename,
            "filesize": filesize,
            "mime_type": mime_type,
            "message_id": message_id,
            "file_id": file_id,
            "telegram_user_id": telegram_user_id,
            "access_hash": access_hash,
            "parent_id": parent_id,
            # The backend's own definition of this field is "a thumbnail is
            # embedded in the file's own Telegram message", which is exactly
            # what tgio.make_preview attaches -- and exactly the question
            # Resolver.thumbs_for asks before it will look for a preview at
            # all. Hard-coding False meant every upload this bridge made was
            # registered as having none, so /rpc/thumb never even tried: it
            # answered 404 in 0.12s off the flag, the shell handler read that
            # as "no preview", and Explorer went and read the whole image to
            # draw its icon. Attaching the thumbnail (tgio) and admitting to it
            # (here) are two separate fixes and both are needed.
            "has_thumbnail": bool(has_thumbnail),
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
