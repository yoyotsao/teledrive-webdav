"""WebDAV bridge: TeleDrive as a local read-only drive, with a writable /game.

Layout served at http://127.0.0.1:<port>/ :

    /                       the TeleDrive root (folders + files, read-only)
    /<folder>/...           mirrors the cloud tree, read-only
    /game/                  writable — see gamestage.py
    /game/<Name>/...        virtual expansion of the packed <Name>.zip (read-only)
    /game/<Name>/...        the staging tree while an upload is still in flight
    /rpc/*                  local control plane (fetch-local, health, forget)

Bytes always travel browser-free: metadata over HTTPS to the TeleDrive backend,
file content straight from Telegram over MTProto. Nothing binary touches the
Python backend, which is TeleDrive's core invariant.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import logging
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import unquote, urlsplit

from wsgidav.dav_error import HTTP_FORBIDDEN, HTTP_INTERNAL_ERROR, DAVError
from wsgidav.dav_provider import DAVCollection, DAVNonCollection, DAVProvider
from wsgidav.wsgidav_app import WsgiDAVApp

import zipfs
from config import Config, ext_path as _ext, load_config
from tdapi import ApiError, Entry, JsonStore, TeleDriveClient
from tgio import REQUEST_SIZE, STREAM_BLOCK_SIZE, SeekableRemoteFile, TelegramWorker

log = logging.getLogger("bridge")

# Verbs that mutate. Everything outside /game/<something> gets 403 for these,
# rather than mounting the whole drive read-only (which would kill /game too).
# MKCOL, PUT, DELETE, COPY and MOVE are exempted below (WriteGuard) — none of
# the five needs /game specifically. MKCOL and PUT map onto real backend
# endpoints (POST /folders, and the same stage-upload-register pipeline
# /game uses). DELETE, COPY and MOVE have real backend endpoints too now
# (trash, register-reuse, and rename/reparent respectively) but none of them
# needs /game either: the resources themselves already draw the real line —
# still-staged writes (StagingFileResource/StagingCollection,
# UploadFileResource) accept them as local filesystem operations,
# already-uploaded resources (_ReadOnlyCollection, _ReadOnlyFile,
# RemoteFileResource, FolderCollection) call the matching backend operation
# — so gating by path on top would only block the /game case for no reason.
# PROPPATCH/LOCK have no such per-resource distinction and stay path-gated
# below.
WRITE_METHODS = {"PUT", "DELETE", "MKCOL", "MOVE", "COPY", "PROPPATCH", "LOCK", "UNLOCK"}
UNGATED_METHODS = {"MKCOL", "PUT", "DELETE", "COPY", "MOVE"}

ROOT = "root"
FOLDER = "folder"
FILE = "file"
GAME = "game"
ZIPDIR = "zipdir"
ZIPFILE = "zipfile"
STAGE_DIR = "stage_dir"
STAGE_FILE = "stage_file"
UPLOAD_FILE = "upload_file"
MISSING = "missing"

# Telegram stores a small preview beside every photo and video. It is served to
# the shell thumbnail handler over /rpc/thumb rather than shown as files here:
# Explorer asked for a thumbnail reads every byte of the original (measured:
# 18.8s and the full 18.6 MB for one 256px preview), and the only supported way
# to change that is a thumbnail provider, not a different file.
THUMB_SUFFIX = ".jpg"
# Ceiling on one background prefetch, so a folder with thousands of files does
# not turn one thumbnail request into an unbounded run against Telegram.
THUMB_PREFETCH_MAX = 2000
# Previews per warm-up step. Each step is one get_messages plus that many
# GetFile calls spread over the connection pool, so a wide slice is what makes
# the first visit to a folder finish quickly rather than trickle.
THUMB_PREFETCH_SLICE = 100
# Seconds of quiet before the warm-up takes another slice. Kept short on
# purpose: the point is to yield between slices, not to stall — a long wait
# just moves the cost onto the person waiting for the folder to fill in.
THUMB_PREFETCH_IDLE = 0.1
# How long a request will wait for the folder warm-up to reach its file before
# fetching it alone. Generous because waiting is the faster path: the batch
# delivers 33 previews a second, a lone fetch about 8.
THUMB_WAIT = 10.0

# Bytes kept from the start of every still image while the shell warm is using
# it, and the extensions it applies to. The thumbnail provider does not end the
# shell's interest in the file: measured on cold folders with previews and
# dimensions both answered in 20ms, eight JPEGs still took 13.0s and every one
# of them was read, while eight PNGs took 0.8s and none were. Isolating the two
# halves put the reads squarely in IShellItemImageFactory::GetImage, after
# IThumbnailProvider returned a valid bitmap — WIC opening the file directly,
# which no registered handler can intercept. So the read is made cheap instead
# of prevented.
#
# This is a scratch file, not a cache: it exists only for the one read a batch's
# warmshell pass makes, and gets deleted right after (see Warmer.fill in
# warmup.py). Keeping it around bought nothing once warmshell existed — rereading
# a shell-warmed file lands in rclone's own VFS cache 11 times out of 12 measured
# (5-13ms, never reaching this process), and once thumbcache_*.db has the
# thumbnail the shell does not open the file at all (274/s, handler uncalled).
# 18,451 of these at the old always-on lifetime cost 8.67 GB on disk for no
# measurable benefit past the first touch.
#
# One Telegram request wide, which is also the reader's block 0. Size is not a
# free choice: what has to be covered is whatever rclone pulls when the shell
# reads, and that is its own read-ahead rather than the shell's request — 252 KB
# most files, 508 KB at the top of everything measured. A 128 KB head was tried
# and left a cold folder at 35s for twelve files, because every read still ran
# off the end of it. Going wider costs no extra round trips (the fetch is one
# request either way, and requests are what the warm-up is bound by) — only a
# transient disk cost now, since it is deleted right after use.
#
# PNG is included despite never having been caught reading: four cold PNG
# folders is thin evidence to hang "this extension is exempt" on, and the
# alternative is a class of files that stays mysteriously slow.
HEAD_SIZE = REQUEST_SIZE
HEAD_SUFFIX = ".head"
HEAD_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
# Heads fetched at once. One head is a single Telegram request on a single
# pooled connection, so the batch is what keeps the other seven busy.
HEAD_BATCH = 8

PACKED_MESSAGE = (
    "This name is already a packed archive on TeleDrive and is read-only. "
    "Delete the .zip from the web UI first, or stage the new copy under a different name."
)


def split_dav_path(path: str) -> List[str]:
    return [seg for seg in path.replace("\\", "/").split("/") if seg]


def _write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` so no reader can see a half-written file.

    Nothing here is ever invalidated — every cached byte is derived from an
    immutable Telegram message — so a partial file would be a permanent one.

    The temp name is unique per writer: the background warm-up and a foreground
    request routinely race for the same file, and on Windows the loser of a
    shared temp name cannot rename over it. Losing the race is not a failure
    either, since both writers had the same bytes; Windows also refuses the
    rename while a reader holds the target open, and that reader is getting the
    right content anyway.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}-{threading.get_ident()}.part")
    tmp.write_bytes(data)
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        if not path.exists():
            raise


@dataclass
class Loc:
    """What a WebDAV path points at."""

    kind: str
    entry: Optional[Entry] = None
    view: Optional[zipfs.ZipView] = None
    node: Optional[zipfs.ZipNode] = None
    local: Optional[Path] = None
    top: Optional[str] = None  # first-level /game name, i.e. the pack unit
    segments: Optional[List[str]] = None  # full path, for UPLOAD_FILE
    parent_id: Optional[str] = None  # resolved destination folder, for UPLOAD_FILE


class Resolver:
    """Path resolution shared by the DAV provider and the fetch-local RPC.

    Keeping this out of the wsgidav resource classes means fetchlocal.py can
    resolve paths without faking a WSGI environ.
    """

    def __init__(self, cfg: Config, api: TeleDriveClient, worker: TelegramWorker, stager=None, upload_stager=None):
        self.cfg = cfg
        self.api = api
        self.worker = worker
        self.stager = stager
        self.upload_stager = upload_stager
        self._zips: Dict[str, zipfs.ZipView] = {}
        self._zip_lock = threading.Lock()
        self._zip_cache = JsonStore(cfg.cache_dir / "zip_dirs.json")
        # Media properties never change once a message exists, same as previews.
        self._prop_cache = JsonStore(cfg.cache_dir / "media_props.json")
        self._thumb_lock = threading.Lock()
        self._thumb_warming = set()  # parent_ids with a prefetch in flight
        self._last_demand = 0.0      # monotonic time of the last foreground request

    # -- helpers ---------------------------------------------------------- #

    def open_remote(self, entry: Entry) -> SeekableRemoteFile:
        """A fresh seekable reader over a cloud file (split parts concatenated)."""
        self.note_demand()
        return SeekableRemoteFile(
            self.worker,
            self.api.parts_for(entry),
            name=entry.name,
            head=self.cached_head(entry),
        )

    def zip_view(self, entry: Entry) -> zipfs.ZipView:
        with self._zip_lock:
            view = self._zips.get(entry.file_id)
            if view is None:
                view = zipfs.ZipView(
                    lambda e=entry: self.open_remote(e),
                    name=zipfs.strip_zip_suffix(entry.name),
                    cache=self._zip_cache,
                    cache_key=entry.file_id,
                )
                self._zips[entry.file_id] = view
            return view

    # -- thumbnails ------------------------------------------------------- #

    def _thumb_path(self, file_id: str) -> Path:
        return self.cfg.cache_dir / "thumbs" / f"{file_id}{THUMB_SUFFIX}"

    def cached_thumb(self, entry: Entry) -> Optional[bytes]:
        """The preview already on disk, or None. Never touches the network."""
        try:
            return self._thumb_path(entry.file_id).read_bytes()
        except OSError:
            return None

    def thumbs_for(self, entries: List[Entry]) -> Dict[str, bytes]:
        """``{file_id: jpeg}`` for those entries that have a preview.

        Cached on disk: a preview is derived from an immutable message, so once
        written it never needs invalidating. Everything still missing is fetched
        in one batch, which matters because a listing has to know each preview's
        length before it can answer PROPFIND — one request per file would make
        opening a folder as slow as the thing this replaces.
        """
        found: Dict[str, bytes] = {}
        missing: List[Entry] = []
        for entry in entries:
            if entry.is_dir or not entry.has_thumbnail or entry.message_id is None:
                continue
            hit = self.cached_thumb(entry)
            if hit is None:
                missing.append(entry)
            else:
                found[entry.file_id] = hit
        if not missing:
            return found

        fetched = self.worker.thumbnails([e.message_id for e in missing])
        for entry in missing:
            data = fetched.get(entry.message_id)
            if not data:
                continue
            found[entry.file_id] = data
            path = self._thumb_path(entry.file_id)
            try:
                _write_atomic(path, data)
            except OSError as exc:  # pragma: no cover - cache is best-effort
                log.warning("could not cache thumbnail %s: %s", path.name, exc)
        return found

    def thumb_bytes(self, entry: Entry) -> Optional[bytes]:
        return self.thumbs_for([entry]).get(entry.file_id)

    # -- file heads -------------------------------------------------------- #

    def _head_path(self, file_id: str) -> Path:
        return self.cfg.cache_dir / "heads" / f"{file_id}{HEAD_SUFFIX}"

    def wants_head(self, entry: Entry) -> bool:
        """Whether this file is one the shell will go and read the front of.

        Split files are excluded rather than handled: a head is read from
        ``entry.message_id`` alone, which is only the start of the logical file
        when there is exactly one part. Nothing in HEAD_EXTENSIONS is anywhere
        near the 500 MiB split threshold, so this costs nothing.
        """
        return (
            not entry.is_dir
            and entry.message_id is not None
            and not entry.is_split
            and os.path.splitext(entry.name)[1].lower() in HEAD_EXTENSIONS
        )

    def cached_head(self, entry: Entry) -> bytes:
        """The first bytes of this file if they are on disk, else empty."""
        try:
            return self._head_path(entry.file_id).read_bytes()
        except OSError:
            return b""

    def _head_complete(self, entry: Entry) -> bool:
        """Whether the cached head is as long as it should be.

        Length, not existence: HEAD_SIZE is chosen from measurements and may be
        raised again, and a head cached under the old value is exactly the case
        that looks warm and still reads off the end into Telegram.
        """
        try:
            have = self._head_path(entry.file_id).stat().st_size
        except OSError:
            return False
        return have >= min(HEAD_SIZE, self.api.total_size(entry))

    def heads_for(self, entries: List[Entry], *, before=None) -> int:
        """Cache the first HEAD_SIZE bytes of each still image; returns how many.

        Concurrent because one head is a single Telegram request on a single
        pooled connection: sequentially this runs at 0.57 files a second and
        leaves seven connections idle. Eight at a time reaches 1.15 a second —
        latency-bound rather than bandwidth-bound, so widening it further only
        queues on the pool.

        ``before`` runs ahead of each group and stops the run by returning
        False. Heads are much heavier than previews — a whole tree is hours,
        not minutes — so the caller has to be able to yield between groups, and
        the grouping lives here to keep HEAD_BATCH in one place.
        """
        missing = [
            e for e in entries
            if self.wants_head(e) and not self._head_complete(e)
        ]
        if not missing:
            return 0

        def one(entry: Entry) -> bool:
            try:
                data = self.worker.read(entry.message_id, 0, HEAD_SIZE)
                if not data:
                    return False
                _write_atomic(self._head_path(entry.file_id), data)
                return True
            except Exception as exc:  # pragma: no cover - cache is best-effort
                log.warning("could not cache head of %s: %s", entry.name, exc)
                return False

        done = 0
        for at in range(0, len(missing), HEAD_BATCH):
            if before is not None and before() is False:
                break
            group = missing[at : at + HEAD_BATCH]
            with ThreadPoolExecutor(max_workers=HEAD_BATCH) as pool:
                done += sum(1 for ok in pool.map(one, group) if ok)
        return done

    def drop_heads(self, entries: List[Entry]) -> None:
        """Remove the scratch head file for each entry, once the shell warm is done with it.

        Best-effort: a head is disposable, so a stray one left behind by a crash
        mid-batch is not worth raising over. The next pass that finds it still
        there via _head_complete just reuses it instead of refetching.
        """
        for entry in entries:
            try:
                self._head_path(entry.file_id).unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover - best-effort cleanup
                log.warning("could not drop head of %s: %s", entry.name, exc)

    def clear_heads(self) -> None:
        """Delete every scratch head file still on disk.

        A backstop around Warmer.fill's per-batch drop_heads: if the process
        died mid-batch, this is what keeps "heads/ is empty between passes" a
        fact rather than an invariant that quietly depends on nothing crashing.
        """
        try:
            paths = list((self.cfg.cache_dir / "heads").iterdir())
        except OSError:
            return
        for path in paths:
            try:
                path.unlink()
            except OSError as exc:  # pragma: no cover - best-effort cleanup
                log.warning("could not clear head %s: %s", path.name, exc)

    def props_for(self, entries: List[Entry], *, demand: bool = True) -> Dict[str, dict]:
        """``{file_id: {...}}`` media properties, cached on disk.

        Same shape as thumbs_for and for the same reason: Explorer asks per file,
        Telegram answers per hundred. Unlike previews these cost no bytes at all —
        the numbers are already in the document's attributes — so the whole point
        is to stop Explorer reading file headers to work them out itself.

        ``demand=False`` for warm-up callers: a background sweep that marked its
        own fetches as demand would keep resetting the quiet timer it is waiting
        on, and so never get to run.
        """
        found: Dict[str, dict] = {}
        missing: List[Entry] = []
        for entry in entries:
            if entry.is_dir or entry.message_id is None:
                continue
            hit = self._prop_cache.get(entry.file_id)
            if hit is None:
                missing.append(entry)
            else:
                found[entry.file_id] = hit
        if not missing:
            return found

        if demand:
            self.note_demand()
        fetched = self.worker.media_info([e.message_id for e in missing])
        for entry in missing:
            info = fetched.get(entry.message_id)
            if info is None:
                continue
            found[entry.file_id] = info
            self._prop_cache.put(entry.file_id, info, defer=True)
        self._prop_cache.flush()
        return found

    def note_demand(self) -> None:
        """Record that a client is waiting on Telegram right now.

        Both previews and ordinary file reads count: Explorer reads the head of
        each image for its properties (measured: 258 KB of a 2 MB JPEG) and that
        travels the same single client loop as the warm-up.
        """
        self._last_demand = time.monotonic()

    def wait_for_quiet(self, quiet: float = THUMB_PREFETCH_IDLE) -> None:
        """Hold a warm-up back while Explorer is actively asking.

        Every Telegram request funnels through one client loop, so a warm-up
        running flat out competes with the very requests it exists to serve — and
        with Explorer's own reads of the originals. Observed on a 3,119-file
        folder: the handler answered in 47ms but Explorer stalled up to 9.7s
        between files. Prefetching is only worth doing in the gaps.

        ``quiet`` is how long the line has to have been idle. The folder prefetch
        keeps it short because someone is watching that folder fill in; the
        whole-tree sweep in warmup.py asks for much more, being speculative.
        """
        while True:
            idle = time.monotonic() - self._last_demand
            if idle >= quiet:
                return
            # Never longer than one slice's worth: callers are now waiting on
            # this warm-up, so stalling it stalls them.
            time.sleep(min(quiet - idle, THUMB_PREFETCH_IDLE))

    def needs_warming(self, entry: Entry) -> bool:
        """Whether this file still owes the caches a preview or its dimensions.

        Deliberately checks that the preview file *exists* rather than reading
        it: a whole-tree sweep asks this about every file it has ever seen, and
        reading a hundred thousand small files to throw the bytes away is the
        kind of thing that makes a warm-up look expensive.

        The head cache is not a condition here on purpose: it is a scratch file
        deleted right after each batch's shell warm uses it (see Warmer.fill),
        so a completed pass never has one lying around, and asking about it
        would make every subsequent pass think the whole tree needs warming
        again just because it cleaned up after itself.
        """
        if entry.is_dir or entry.message_id is None:
            return False
        if entry.has_thumbnail and not self._thumb_path(entry.file_id).exists():
            return True
        return self._prop_cache.get(entry.file_id) is None

    def await_thumb(self, entry: Entry, timeout: float = THUMB_WAIT) -> Optional[bytes]:
        """Wait briefly for a running warm-up to produce this preview."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hit = self.cached_thumb(entry)
            if hit is not None:
                return hit
            with self._thumb_lock:
                warming = bool(self._thumb_warming)
            if not warming:
                return None
            time.sleep(0.02)
        return None

    def prefetch_folder_thumbs(self, parent_id: Optional[str]) -> None:
        """Warm a whole folder's previews in the background, once.

        Explorer asks for thumbnails one file at a time, and a single preview
        costs two Telegram round trips (resolve the message, then download). In a
        batch those collapse into one ``get_messages`` per hundred files plus
        GetFile calls spread over the connection pool: measured cold, 84 previews
        land in 2.6s (33 per second) against 0.56s for the first one alone. So the
        first request answers on its own and hands the rest to a worker, and by
        the time Explorer works through the folder the answers come off disk.
        """
        with self._thumb_lock:
            if parent_id in self._thumb_warming:
                return
            self._thumb_warming.add(parent_id)

        def run():
            try:
                entries = [e for e in self.api.list_dir(parent_id) if not e.is_dir]
                if len(entries) > THUMB_PREFETCH_MAX:
                    log.info(
                        "prefetching previews for the first %s of %s files in one folder",
                        THUMB_PREFETCH_MAX, len(entries),
                    )
                    entries = entries[:THUMB_PREFETCH_MAX]
                # In slices, not one call: every Telegram request funnels through
                # a single client loop, so one batch of a hundred blocks any
                # foreground request behind it — measured, the second file in a
                # folder waited 11.7s for the warm-up to drain. Slicing lets those
                # requests interleave, at the cost of one extra get_messages per
                # slice.
                for at in range(0, len(entries), THUMB_PREFETCH_SLICE):
                    self.wait_for_quiet()
                    slice_ = entries[at : at + THUMB_PREFETCH_SLICE]
                    self.thumbs_for(slice_)
                    # Properties ride the same documents, and Explorer wants them
                    # for the same files at the same moment.
                    self.props_for(slice_, demand=False)
            except Exception as exc:  # a warm-up failure must never surface
                log.warning("preview prefetch failed: %s", exc)
            finally:
                with self._thumb_lock:
                    self._thumb_warming.discard(parent_id)

        threading.Thread(target=run, name="thumb-prefetch", daemon=True).start()

    def game_entry(self) -> Optional[Entry]:
        """The cloud folder /game maps to, or None if it does not exist yet."""
        entry = self.api.resolve([self.cfg.game_folder])
        return entry if entry is not None and entry.is_dir else None

    def game_children(self) -> Dict[str, Entry]:
        game = self.game_entry()
        return self.api.children_by_name(game.file_id) if game else {}

    # -- resolution ------------------------------------------------------- #

    def resolve(self, segments: List[str]) -> Loc:
        if not segments:
            return Loc(ROOT)
        if segments[0] == self.cfg.game_folder:
            return self._resolve_game(segments[1:])
        # A pending write (not yet uploaded+registered) is the newest truth
        # for that exact path, same priority rule as /game staging.
        if self.upload_stager is not None:
            pending = self.upload_stager.get(segments)
            if pending is not None:
                local = self.upload_stager.path_for(segments)
                if local is not None and local.exists():
                    return Loc(UPLOAD_FILE, local=local, segments=list(segments), parent_id=pending.parent_id)
        entry = self.api.resolve(segments)
        if entry is None:
            return Loc(MISSING)
        return Loc(FOLDER if entry.is_dir else FILE, entry=entry)


    def _resolve_game(self, rest: List[str]) -> Loc:
        if not rest:
            return Loc(GAME)
        top = rest[0]

        # 1. An in-flight staging tree wins: it is the newest truth for that name.
        if self.stager is not None:
            local = self.stager.path_for(rest)
            if local is not None and local.exists():
                kind = STAGE_DIR if local.is_dir() else STAGE_FILE
                return Loc(kind, local=local, top=top)

        children = self.game_children()

        # 2. <top>.zip presented as a folder, expanded from its central directory.
        zip_entry = children.get(top + ".zip")
        if zip_entry is not None and not zip_entry.is_dir:
            view = self.zip_view(zip_entry)
            try:
                node = view.lookup(rest[1:])
            except Exception as exc:
                log.warning("cannot read zip directory of %s: %s", zip_entry.name, exc)
                return Loc(MISSING)
            if node is None:
                return Loc(MISSING)
            return Loc(ZIPDIR if node.is_dir else ZIPFILE, entry=zip_entry, view=view, node=node, top=top)

        # 3. Anything else inside the game folder is shown as-is.
        game = self.game_entry()
        if game is None:
            return Loc(MISSING)
        entry = self.api.resolve(rest, parent_id=game.file_id)
        if entry is None:
            return Loc(MISSING)
        return Loc(FOLDER if entry.is_dir else FILE, entry=entry, top=top)

    def dav_path_from_windows(self, win_path: str) -> Optional[List[str]]:
        """Translate ``E:\\game\\X`` into DAV segments, or None if off-mount."""
        raw = win_path.strip().strip('"')
        drive = self.cfg.mount_drive.rstrip("\\/").lower()
        norm = raw.replace("/", "\\")
        if not norm.lower().startswith(drive):
            return None
        rel = norm[len(drive) :].lstrip("\\")
        return [seg for seg in rel.split("\\") if seg]


# --------------------------------------------------------------------------- #
# wsgidav resources
# --------------------------------------------------------------------------- #


class _ReadOnlyFile(DAVNonCollection):
    """Common read-only, range-capable non-collection behaviour."""

    def __init__(self, path, environ, size: int, mtime: float, etag: str, mime: Optional[str] = None):
        super().__init__(path, environ)
        self._size = size
        self._mtime = mtime
        self._etag = etag
        self._mime = mime

    def get_content_length(self):
        return self._size

    def get_content_type(self):
        return self._mime or super().get_content_type()

    def get_last_modified(self):
        return self._mtime

    def get_etag(self):
        return self._etag

    def support_etag(self):
        return True

    def support_ranges(self):
        return True

    # Covers RemoteFileResource (an already-registered backend file, /game or
    # not) and ZipFileResource (packed archive content). Unlike DAVCollection,
    # wsgidav's DAVNonCollection has no default delete() — it falls back to
    # _DAVResource's bare NotImplementedError, which do_DELETE does not catch
    # (only DAVError is), so an unpatched already-uploaded file 500s instead
    # of 403ing. This also runs during a recursive folder delete, where
    # do_DELETE calls delete() directly on every descendant file.
    def delete(self):
        raise DAVError(HTTP_FORBIDDEN, "already uploaded — TeleDrive has no delete endpoint for this.")

    # wsgidav's MOVE handling calls support_recursive_move() on the source
    # unconditionally, whether or not it is a collection. _DAVResource's
    # inherited default is `assert self.is_collection; raise
    # NotImplementedError` — for a file that assertion itself fails, which
    # do_MOVE does not catch (only DAVError is), so MOVE of an
    # already-uploaded file 500s before ever reaching copy_move_single()
    # below. Returning False here is what routes MOVE onto the same
    # file-by-file fallback COPY already uses; together the two overrides
    # are what make both verbs 403 cleanly instead of 500ing.
    def support_recursive_move(self, dest_path):
        return False

    def copy_move_single(self, dest_path, *, is_move):
        raise DAVError(HTTP_FORBIDDEN, "already uploaded — TeleDrive has no copy/rename endpoint for this.")


class RemoteFileResource(_ReadOnlyFile):
    def __init__(self, path, environ, resolver: Resolver, entry: Entry):
        self.resolver = resolver
        self.entry = entry
        size = resolver.api.total_size(entry)
        super().__init__(path, environ, size, entry.mtime, f"{entry.file_id}-{size}", entry.mime)

    def get_content(self):
        return self.resolver.open_remote(self.entry)

    # Overwriting an existing plain file: there is no backend "replace" call,
    # so this is a new write of the same name into the same parent — the
    # newest row wins (tdapi.children_by_name), same as a fresh upload that
    # happens to collide with dedup registrations (plan risk #5 in CLAUDE.md).
    def begin_write(self, *, content_type=None):
        if self.resolver.upload_stager is None:
            raise DAVError(HTTP_FORBIDDEN)
        self._upload_segments = split_dav_path(self.path)
        parent = self.resolver.api.resolve(self._upload_segments[:-1]) if len(self._upload_segments) > 1 else None
        self._upload_parent_id = parent.file_id if parent is not None else None
        local = self.resolver.upload_stager.create_file(self._upload_segments, self._upload_parent_id)
        return local.open("wb")

    def end_write(self, *, with_errors):
        if with_errors or self.resolver.upload_stager is None:
            return
        self.resolver.upload_stager.touch(self._upload_segments, self._upload_parent_id)

    def delete(self):
        self.resolver.api.trash(self.entry.file_id)


class ZipFileResource(_ReadOnlyFile):
    def __init__(self, path, environ, resolver: Resolver, view: zipfs.ZipView, node: zipfs.ZipNode, entry: Entry):
        self.resolver = resolver
        self.view = view
        self.node = node
        # Digest rather than the raw member name: an ETag becomes a response
        # header, and WSGI headers must be latin-1 encodable (game archives are
        # full of non-ASCII names).
        ident = hashlib.sha1(f"{entry.file_id}:{node.zip_name}".encode("utf-8")).hexdigest()[:16]
        super().__init__(path, environ, node.size, node.mtime, f"{ident}-{node.size}")

    def get_content(self):
        return self.view.open(self.node)

    def begin_write(self, *, content_type=None):
        raise DAVError(HTTP_FORBIDDEN, PACKED_MESSAGE)


class _ReadOnlyCollection(DAVCollection):
    def get_creation_date(self):
        return self._mtime

    def get_last_modified(self):
        return self._mtime

    def get_display_info(self):
        return {"type": "Directory"}

    # Covers RootCollection/FolderCollection (already-registered backend
    # folders, /game or not) and ZipDirCollection (packed archive contents).
    # wsgidav's own DAVCollection.delete() already answers HTTP_FORBIDDEN by
    # default, so this is not strictly needed for correctness — but without
    # it, do_DELETE first walks the whole subtree (get_descendants(depth=
    # "infinity")) just to reject every member one by one, which for a large
    # game archive means enumerating thousands of zip entries (or backend
    # files) before ever reporting the 403. handle_delete() is checked first
    # and skips straight past that walk. handle_copy()/handle_move() below
    # exist for the same reason, and arguably need the comment more:
    # DAVCollection.copy_move_single() already 403s by default too, so
    # without these overrides the request would still fail correctly — just
    # after paying for the same wasted subtree walk first.
    def handle_delete(self):
        raise DAVError(HTTP_FORBIDDEN, "already uploaded — TeleDrive has no delete endpoint for this.")

    def handle_copy(self, dest_path, *, depth_infinity):
        raise DAVError(HTTP_FORBIDDEN, "already uploaded — TeleDrive has no copy/rename endpoint for this.")

    def handle_move(self, dest_path):
        raise DAVError(HTTP_FORBIDDEN, "already uploaded — TeleDrive has no copy/rename endpoint for this.")


class RootCollection(_ReadOnlyCollection):
    def __init__(self, path, environ, resolver: Resolver, parent_id: Optional[str], mtime: float):
        super().__init__(path, environ)
        self.resolver = resolver
        self.parent_id = parent_id
        self._mtime = mtime

    def get_member_names(self):
        names = list(self.resolver.api.children_by_name(self.parent_id).keys())
        if self.parent_id is None and self.resolver.cfg.game_folder not in names:
            # /game must exist as a drop target even before anything is uploaded.
            names.append(self.resolver.cfg.game_folder)
        if self.resolver.upload_stager is not None:
            for name in self.resolver.upload_stager.names_under(split_dav_path(self.path)):
                if name not in names:
                    names.append(name)
        return names

    def create_collection(self, name):
        # Folder creation is real backend metadata (POST /folders), not a file
        # upload, so it is not limited to /game the way PUT is: WriteGuard lets
        # MKCOL through everywhere and this is where it lands.
        try:
            self.resolver.api.create_folder(name, parent_id=self.parent_id)
        except ApiError as exc:
            log.warning("create folder %r under %s failed: %s", name, self.parent_id, exc)
            raise DAVError(HTTP_INTERNAL_ERROR, str(exc))
        return None

    def create_empty_resource(self, name):
        # Same reasoning as create_collection: a plain write has a real
        # backend endpoint to land on (stage -> upload -> register, the exact
        # pipeline /game uses — see uploadstage.py), so it is not limited to
        # /game either. WriteGuard lets PUT through everywhere for this reason.
        if self.resolver.upload_stager is None:
            raise DAVError(HTTP_FORBIDDEN)
        segments = split_dav_path(self.path) + [name]
        local = self.resolver.upload_stager.create_file(segments, self.parent_id)
        return UploadFileResource(
            self.path.rstrip("/") + "/" + name, self.environ, self.resolver.upload_stager, local, segments, self.parent_id
        )


class GameCollection(DAVCollection):
    """/game — the only writable path.

    Members are the union of packed archives (shown as folders, .zip hidden),
    plain members of the cloud folder, and whatever is still staging locally.
    """

    def __init__(self, path, environ, resolver: Resolver):
        super().__init__(path, environ)
        self.resolver = resolver
        self.stager = resolver.stager

    def get_creation_date(self):
        return time.time()

    def get_last_modified(self):
        return time.time()

    def get_member_names(self):
        names = []
        for name, entry in self.resolver.game_children().items():
            names.append(zipfs.strip_zip_suffix(name) if not entry.is_dir and zipfs.is_zip_name(name) else name)
        if self.stager is not None:
            for name in self.stager.top_level_names():
                if name not in names:
                    names.append(name)
        return names

    def create_collection(self, name):
        if self.stager is None:
            raise DAVError(HTTP_FORBIDDEN)
        self.stager.mkdir([name])
        return None

    def create_empty_resource(self, name):
        if self.stager is None:
            raise DAVError(HTTP_FORBIDDEN)
        path = self.stager.create_file([name])
        return StagingFileResource(self.path.rstrip("/") + "/" + name, self.environ, self.stager, path, name)

    # /game itself is a drop target, never a thing to delete.
    def handle_delete(self):
        raise DAVError(HTTP_FORBIDDEN)


class FolderCollection(RootCollection):
    def __init__(self, path, environ, resolver: Resolver, entry: Entry):
        super().__init__(path, environ, resolver, entry.file_id, entry.mtime)
        self.entry = entry

    def handle_delete(self):
        self.resolver.api.trash(self.entry.file_id)
        return True


class ZipDirCollection(_ReadOnlyCollection):
    def __init__(self, path, environ, node: zipfs.ZipNode):
        super().__init__(path, environ)
        self.node = node
        self._mtime = node.mtime or time.time()

    def get_member_names(self):
        return list(self.node.children.keys())

    # Writing into an already-packed archive would produce a repack containing
    # only the newly written files, silently dropping everything else. Refuse
    # instead: delete the .zip from the web UI, or stage under a new name.
    def create_collection(self, name):
        raise DAVError(HTTP_FORBIDDEN, PACKED_MESSAGE)

    def create_empty_resource(self, name):
        raise DAVError(HTTP_FORBIDDEN, PACKED_MESSAGE)


class _StagingCopyMove:
    """Shared copy/move plumbing for StagingCollection and StagingFileResource.

    Both wrap a plain local path under GameStager; a file vs. a directory
    makes no difference to GameStager.move()/copy() (os.replace and
    shutil.copy2/mkdir already branch on that internally), so the two
    classes need this identical regardless of which one they otherwise
    subclass. Mixed in first so its methods win the MRO over the base
    class's own default — DAVCollection's copy_move_single() for
    StagingCollection, and DAVNonCollection's inherited _DAVResource default
    for StagingFileResource (DAVNonCollection has no override of its own).
    """

    def support_recursive_move(self, dest_path):
        return self.stager.path_for(split_dav_path(dest_path)[1:]) is not None

    def move_recursive(self, dest_path):
        try:
            self.stager.move(self.local, split_dav_path(dest_path))
        except PermissionError as exc:
            raise DAVError(HTTP_FORBIDDEN, str(exc))

    def copy_move_single(self, dest_path, *, is_move):
        try:
            if is_move:
                self.stager.move(self.local, split_dav_path(dest_path))
            else:
                self.stager.copy(self.local, split_dav_path(dest_path))
        except PermissionError as exc:
            raise DAVError(HTTP_FORBIDDEN, str(exc))


class StagingCollection(_StagingCopyMove, DAVCollection):
    def __init__(self, path, environ, stager, local: Path, top: str):
        super().__init__(path, environ)
        self.stager = stager
        self.local = local
        self.top = top

    def get_creation_date(self):
        return self.local.stat().st_ctime

    def get_last_modified(self):
        return self.local.stat().st_mtime

    def get_display_info(self):
        return {"type": "Directory (staging)"}

    def get_member_names(self):
        try:
            return sorted(os.listdir(self.local))
        except OSError:
            return []

    def create_collection(self, name):
        (self.local / name).mkdir(parents=True, exist_ok=True)
        self.stager.touch(self.top)
        return None

    def create_empty_resource(self, name):
        path = self.local / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        self.stager.touch(self.top)
        return StagingFileResource(self.path.rstrip("/") + "/" + name, self.environ, self.stager, path, name)

    def support_recursive_delete(self):
        return True

    def delete(self):
        shutil.rmtree(self.local, ignore_errors=True)
        self.stager.touch(self.top)
        self.remove_all_properties(recursive=True)
        self.remove_all_locks(recursive=True)


class StagingFileResource(_StagingCopyMove, DAVNonCollection):
    """A file inside the /game staging tree: a real local file, writable."""

    def __init__(self, path, environ, stager, local: Path, name: str):
        super().__init__(path, environ)
        self.stager = stager
        self.local = local
        self.top = split_dav_path(path)[1] if len(split_dav_path(path)) > 1 else name

    def get_content_length(self):
        try:
            return self.local.stat().st_size
        except OSError:
            return 0

    def get_last_modified(self):
        try:
            return self.local.stat().st_mtime
        except OSError:
            return time.time()

    def get_etag(self):
        try:
            st = self.local.stat()
            return f"stage-{int(st.st_mtime)}-{st.st_size}"
        except OSError:
            return None

    def support_etag(self):
        return True

    def support_ranges(self):
        return True

    def get_content(self):
        return self.local.open("rb")

    def begin_write(self, *, content_type=None):
        self.local.parent.mkdir(parents=True, exist_ok=True)
        self.stager.touch(self.top)
        return self.local.open("wb")

    def end_write(self, *, with_errors):
        if with_errors:
            log.warning("PUT failed for %s — leaving the partial file in staging", self.local)
        self.stager.touch(self.top)

    def set_last_modified(self, dest_path, time_stamp, *, dry_run):
        if not dry_run:
            try:
                os.utime(self.local, (time_stamp, time_stamp))
            except OSError:
                return False
        return True

    def delete(self):
        try:
            self.local.unlink()
        except OSError:
            pass
        self.stager.touch(self.top)
        self.remove_all_properties(recursive=True)
        self.remove_all_locks(recursive=True)


class UploadFileResource(DAVNonCollection):
    """A plain file outside /game: staged locally until uploadstage.py lands
    it on Telegram and registers it at its real parent folder.

    Unlike StagingFileResource, the debounce key is the file's own full path
    rather than a name under a fixed /game/<top> — there is no packing unit
    above single-file granularity here. DELETE is offered (see delete()) since
    it is a purely local undo of a write that has not reached Telegram yet;
    likewise COPY (see copy_move_single()) is just a local filesystem copy
    plus a second independent staged registration, with no backend involved
    until each copy is uploaded on its own. MOVE never actually reaches this
    class outside /game: WriteGuard (bridge.py) still path-gates MOVE to
    /game/<pack-unit>/..., so a MOVE with a general-path source is 403'd
    before any resource is resolved. The is_move branch in
    copy_move_single() below is defensive rather than load-bearing — it is
    not "no rename primitive" that stops the request, it is a request that
    never arrives.
    """

    def __init__(self, path, environ, upload_stager, local: Path, segments: List[str], parent_id: Optional[str]):
        super().__init__(path, environ)
        self.upload_stager = upload_stager
        self.local = local
        self.segments = list(segments)
        self.parent_id = parent_id

    def get_content_length(self):
        try:
            return self.local.stat().st_size
        except OSError:
            return 0

    def get_last_modified(self):
        try:
            return self.local.stat().st_mtime
        except OSError:
            return time.time()

    def get_etag(self):
        try:
            st = self.local.stat()
            return f"upload-{int(st.st_mtime)}-{st.st_size}"
        except OSError:
            return None

    def support_etag(self):
        return True

    def support_ranges(self):
        return True

    def get_content(self):
        return self.local.open("rb")

    def begin_write(self, *, content_type=None):
        self.local.parent.mkdir(parents=True, exist_ok=True)
        self.upload_stager.touch(self.segments, self.parent_id)
        return self.local.open("wb")

    def end_write(self, *, with_errors):
        if with_errors:
            log.warning("PUT failed for %s — leaving the partial file staged", self.local)
        self.upload_stager.touch(self.segments, self.parent_id)

    def delete(self):
        try:
            self.local.unlink()
        except OSError:
            pass
        self.upload_stager.forget(self.segments)
        self.remove_all_properties(recursive=True)
        self.remove_all_locks(recursive=True)

    def copy_move_single(self, dest_path, *, is_move):
        if is_move:
            raise DAVError(HTTP_FORBIDDEN, "no rename primitive for a pending upload")
        dest_segments = split_dav_path(dest_path)
        if not dest_segments or dest_segments[0] == self.upload_stager.cfg.game_folder:
            raise DAVError(HTTP_FORBIDDEN, "cannot copy a pending upload into /game")
        try:
            parent = self.upload_stager.api.resolve(dest_segments[:-1]) if len(dest_segments) > 1 else None
            parent_id = parent.file_id if parent is not None else None
            dest_local = self.upload_stager.create_file(dest_segments, parent_id)
            shutil.copy2(_ext(self.local), _ext(dest_local))
        except PermissionError as exc:
            raise DAVError(HTTP_FORBIDDEN, str(exc))

    def set_last_modified(self, dest_path, time_stamp, *, dry_run):
        if not dry_run:
            try:
                os.utime(self.local, (time_stamp, time_stamp))
            except OSError:
                return False
        return True


class TeleDriveProvider(DAVProvider):
    def __init__(self, resolver: Resolver):
        super().__init__()
        self.resolver = resolver

    def is_readonly(self):
        # /game is writable; the guard middleware enforces the rest.
        return False

    def get_resource_inst(self, path: str, environ: dict):
        segments = split_dav_path(path)
        try:
            loc = self.resolver.resolve(segments)
        except ApiError as exc:
            log.warning("resolve %s failed: %s", path, exc)
            return None
        except Exception:
            log.exception("resolve %s crashed", path)
            return None

        res = self.resolver
        if loc.kind == ROOT:
            return RootCollection(path, environ, res, None, time.time())
        if loc.kind == GAME:
            return GameCollection(path, environ, res)
        if loc.kind == FOLDER:
            return FolderCollection(path, environ, res, loc.entry)
        if loc.kind == FILE:
            try:
                return RemoteFileResource(path, environ, res, loc.entry)
            except ApiError as exc:
                log.warning("cannot size %s: %s", path, exc)
                return None
        if loc.kind == ZIPDIR:
            return ZipDirCollection(path, environ, loc.node)
        if loc.kind == ZIPFILE:
            return ZipFileResource(path, environ, res, loc.view, loc.node, loc.entry)
        if loc.kind == STAGE_DIR:
            return StagingCollection(path, environ, res.stager, loc.local, loc.top)
        if loc.kind == STAGE_FILE:
            return StagingFileResource(path, environ, res.stager, loc.local, loc.local.name)
        if loc.kind == UPLOAD_FILE:
            return UploadFileResource(path, environ, res.upload_stager, loc.local, loc.segments, loc.parent_id)
        return None


# --------------------------------------------------------------------------- #
# WSGI plumbing
# --------------------------------------------------------------------------- #


def _text_response(start_response, status: str, body: str, content_type="text/plain; charset=utf-8"):
    payload = body.encode("utf-8")
    start_response(status, [("Content-Type", content_type), ("Content-Length", str(len(payload)))])
    return [payload]


class WriteGuard:
    """Reject every mutating verb outside /game/<pack-unit> — except MKCOL, PUT, DELETE, COPY and MOVE.

    MKCOL and PUT map onto a real backend endpoint that needs no packing:
    MKCOL is `POST /folders` (`RootCollection.create_collection`), and PUT is
    the same stage -> upload -> register pipeline /game uses, generalized to
    an arbitrary destination by uploadstage.py instead of a fixed /game
    folder. DELETE, COPY and MOVE have real backend endpoints too (trash,
    register-reuse, and rename/reparent respectively) but gating them by path
    would be the wrong axis: the actual line is staged-vs-uploaded, and the
    resources enforce that themselves (StagingFileResource/UploadFileResource
    implement them as local undo/copy/move operations; _ReadOnlyCollection/_ReadOnlyFile
    refuse them via handle_delete/copy_move_single/handle_move or the wsgidav default).
    PROPPATCH and LOCK have no such per-resource distinction, so they stay gated.

    rclone's global --read-only is not usable here because it would also freeze
    /game, so the rule lives on this side of the mount.
    """

    def __init__(self, app, game_folder: str):
        self.app = app
        self.game_folder = game_folder

    def _allowed(self, path: str) -> bool:
        segments = split_dav_path(unquote(path))
        return len(segments) >= 2 and segments[0] == self.game_folder

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "").upper()
        if method in WRITE_METHODS and method not in UNGATED_METHODS:
            path = environ.get("PATH_INFO", "")
            if not self._allowed(path):
                log.info("403 %s %s (read-only path)", method, path)
                return _text_response(
                    start_response,
                    "403 Forbidden",
                    f"TeleDrive is read-only here. Only /{self.game_folder}/<name>/... accepts writes.\n",
                )
            dest = environ.get("HTTP_DESTINATION")
            if dest and not self._allowed(urlsplit(dest).path):
                log.info("403 %s %s -> %s (destination read-only)", method, path, dest)
                return _text_response(
                    start_response, "403 Forbidden", "Destination is outside the writable /game area.\n"
                )
        return self.app(environ, start_response)


class RpcApp:
    """Local control plane used by the Explorer verb and for diagnostics."""

    def __init__(self, cfg: Config, resolver: Resolver, fetcher, stager, upload_stager=None):
        self.cfg = cfg
        self.resolver = resolver
        self.fetcher = fetcher
        self.stager = stager
        self.upload_stager = upload_stager

    def __call__(self, environ, start_response):
        route = environ.get("PATH_INFO", "")[len("/rpc") :]
        try:
            if route in ("/health", "/health/"):
                return self._health(start_response)
            if route in ("/status", "/status/"):
                return self._status(start_response)
            if route in ("/forget", "/forget/"):
                return self._forget(start_response)
            if route in ("/fetch-local", "/fetch-local/"):
                return self._fetch_local(environ, start_response)
            if route in ("/thumb", "/thumb/"):
                return self._thumb(environ, start_response)
            if route in ("/props", "/props/"):
                return self._props(environ, start_response)
        except Exception as exc:
            log.exception("rpc %s failed", route)
            return _text_response(start_response, "500 Internal Server Error", f"{type(exc).__name__}: {exc}\n")
        return _text_response(start_response, "404 Not Found", f"no such rpc: {route}\n")

    def _health(self, start_response):
        body = json.dumps(
            {
                "ok": True,
                "telegram_user_id": self.resolver.worker.user_id,
                "base_url": self.cfg.base_url,
                "mount_drive": self.cfg.mount_drive,
                "game_folder": self.cfg.game_folder,
            }
        )
        return _text_response(start_response, "200 OK", body, "application/json")

    def _status(self, start_response):
        body = json.dumps(
            {
                **(self.stager.status() if self.stager else {}),
                "uploads": self.upload_stager.status() if self.upload_stager else {},
            },
            default=str,
        )
        return _text_response(start_response, "200 OK", body, "application/json")

    def _forget(self, start_response):
        self.resolver.api.invalidate()
        return _text_response(start_response, "200 OK", "metadata caches cleared\n")

    def _thumb(self, environ, start_response):
        """Telegram's stored preview for one file, as JPEG bytes.

        Called by the shell thumbnail handler with the Windows path Explorer is
        rendering, so it answers in the caller's own terms rather than making a
        native DLL learn DAV paths. Anything without a preview is a plain 404 —
        the handler then falls back to whatever Windows would have done.
        """
        self.resolver.note_demand()
        raw = self._read_param(environ, "path") or ""
        segments = self.resolver.dav_path_from_windows(raw)
        if not segments:
            return _text_response(start_response, "404 Not Found", "not a path on the mount\n")
        loc = self.resolver.resolve(segments)
        if loc.kind != FILE or loc.entry is None:
            return _text_response(start_response, "404 Not Found", "no such file\n")

        cached = self.resolver.cached_thumb(loc.entry)
        if cached is None:
            # First file touched in this folder: Explorer is about to ask for the
            # rest, so warm them together.
            parent = self.resolver.api.resolve(segments[:-1]) if len(segments) > 1 else None
            self.resolver.prefetch_folder_thumbs(parent.file_id if parent else None)
            # Wait for the batch to deliver this file rather than racing it with a
            # single fetch. Batched, previews land at 33 a second; asked for one at
            # a time they cost ~126ms each and compete with the batch for the same
            # Telegram loop. Waiting is faster than helping.
            cached = self.resolver.await_thumb(loc.entry)
        data = cached if cached is not None else self.resolver.thumb_bytes(loc.entry)
        if not data:
            return _text_response(start_response, "404 Not Found", "no thumbnail for this file\n")
        start_response(
            "200 OK",
            [("Content-Type", "image/jpeg"), ("Content-Length", str(len(data)))],
        )
        return [data]

    def _props(self, environ, start_response):
        """Media properties for one file, as JSON.

        The property handler asks in Windows paths for the same reason the
        thumbnail handler does. An empty object is a valid answer — it means the
        file has no dimensions to report, and the handler should say so rather
        than let Explorer go read the file.
        """
        self.resolver.note_demand()
        raw = self._read_param(environ, "path") or ""
        segments = self.resolver.dav_path_from_windows(raw)
        if not segments:
            return _text_response(start_response, "404 Not Found", "not a path on the mount\n")
        loc = self.resolver.resolve(segments)
        if loc.kind != FILE or loc.entry is None:
            return _text_response(start_response, "404 Not Found", "no such file\n")
        info = dict(self.resolver.props_for([loc.entry]).get(loc.entry.file_id) or {})
        info["size"] = self.resolver.api.total_size(loc.entry)
        payload = json.dumps(info).encode("utf-8")
        start_response(
            "200 OK",
            [("Content-Type", "application/json"), ("Content-Length", str(len(payload)))],
        )
        return [payload]

    def _read_param(self, environ, name: str) -> Optional[str]:
        from urllib.parse import parse_qs

        query = parse_qs(environ.get("QUERY_STRING", ""))
        if name in query:
            return query[name][0]
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length:
            body = environ["wsgi.input"].read(length).decode("utf-8", "replace")
            parsed = parse_qs(body)
            if name in parsed:
                return parsed[name][0]
            try:
                return json.loads(body).get(name)
            except ValueError:
                return body.strip() or None
        return None

    def _fetch_local(self, environ, start_response):
        target = self._read_param(environ, "path")
        if not target:
            return _text_response(start_response, "400 Bad Request", "missing 'path'\n")
        start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8"), ("Cache-Control", "no-cache")])

        def stream():
            for line in self.fetcher.fetch(target):
                yield (line.rstrip("\n") + "\n").encode("utf-8")

        return stream()


class Dispatcher:
    def __init__(self, dav_app, rpc_app):
        self.dav_app = dav_app
        self.rpc_app = rpc_app

    def __call__(self, environ, start_response):
        if environ.get("PATH_INFO", "").startswith("/rpc"):
            return self.rpc_app(environ, start_response)
        return self.dav_app(environ, start_response)


def build_app(cfg: Config, resolver: Resolver, stager, fetcher, upload_stager=None):
    provider = TeleDriveProvider(resolver)
    dav_config = {
        "provider_mapping": {"/": provider},
        # user_mapping {"*": True} = anonymous; the socket is loopback-only.
        "http_authenticator": {
            "domain_controller": None,
            "accept_basic": True,
            "accept_digest": False,
            "default_to_digest": False,
        },
        "simple_dc": {"user_mapping": {"*": True}},
        # No dead-property storage (nothing here has writable properties);
        # lock_storage keeps wsgidav's default in-memory locks for /game writes.
        "property_manager": False,
        "dir_browser": {"enable": True, "davmount": False},
        # Bigger than the 8 KB default so streaming a movie is not 128 reads per
        # MiB, and wide enough that one read spans the whole connection pool.
        "block_size": STREAM_BLOCK_SIZE,
        "verbose": 1,
        # Keep wsgidav from reconfiguring the root logger set up in main().
        "logging": {"enable": False, "enable_loggers": []},
    }
    dav_app = WsgiDAVApp(dav_config)
    guarded = WriteGuard(dav_app, cfg.game_folder)
    return Dispatcher(guarded, RpcApp(cfg, resolver, fetcher, stager, upload_stager))


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="TeleDrive WebDAV bridge")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.port:
        cfg = dataclasses.replace(cfg, port=args.port)

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
    )
    for path in (cfg.cache_dir, cfg.staging_dir, cfg.pack_dir, cfg.local_dir, cfg.upload_dir):
        path.mkdir(parents=True, exist_ok=True)

    # Imported here so `python bridge.py --help` works without Telethon present.
    from fetchlocal import LocalFetcher
    from gamestage import GameStager
    from uploadstage import UploadStager
    from warmup import BackgroundWarmup

    worker = TelegramWorker(
        cfg.api_id, cfg.api_hash, cfg.session, cfg.download_connections, upload_parts=cfg.upload_parts
    )
    worker.start()
    api = TeleDriveClient(cfg)
    api.login()

    resolver = Resolver(cfg, api, worker)
    stager = GameStager(cfg, api, worker)
    resolver.stager = stager
    upload_stager = UploadStager(cfg, api, worker)
    resolver.upload_stager = upload_stager
    fetcher = LocalFetcher(cfg, resolver)
    stager.start()
    upload_stager.start()

    # The tree is warmed from in here rather than by running warmup.py on a
    # schedule: this process already holds the Telegram connection and knows
    # when a request is waiting, and a second process would just queue behind it.
    warmer = None
    if cfg.warmup_auto:
        warmer = BackgroundWarmup(resolver, interval_minutes=cfg.warmup_interval_minutes)
        warmer.start()

    app = build_app(cfg, resolver, stager, fetcher, upload_stager)

    from cheroot import wsgi

    server = wsgi.Server((cfg.host, cfg.port), app, numthreads=16, request_queue_size=64)
    # No keep-alive. On Windows cheroot's connection manager does not get woken
    # when an idle connection becomes readable, so it polls instead, capped at
    # 50ms (cheroot/connections.py, "select() does not return when a socket is
    # ready"). Every request after the first on a reused connection therefore
    # waits for the next poll: measured 50ms against 1ms for a fresh one, on
    # every /rpc call the shell handlers make and every range read rclone does.
    # A new loopback connection costs 0.2ms, so there is nothing to keep alive
    # for.
    server.keep_alive_conn_limit = 0
    log.info("bridge listening on http://%s:%s (game folder: /%s)", cfg.host, cfg.port, cfg.game_folder)
    # The url must be quoted: rclone splits remote from path at the first colon,
    # so an unquoted "http://" truncates the option value to "http".
    log.info(
        'mount with: rclone mount :webdav,url="http://%s:%s",vendor=other: %s',
        cfg.host, cfg.port, cfg.mount_drive,
    )
    try:
        server.start()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.stop()
        if warmer is not None:
            warmer.stop()
        stager.stop()
        upload_stager.stop()
        worker.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
