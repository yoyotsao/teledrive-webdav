"""What a file id turns out to be -- without saying what the file is called.

Given an ``id=0x...`` from a log, answer the questions debugging actually has
("what kind of file, where does it live, what state is it in") from what the
bridge has already cached on disk, and nothing else::

    python -m teledrive_webdav.core.fileid info 0x2f8a91c04d7e3b15

The output is meant to be pasteable, so it carries none of: the name, the path,
any folder name, the content fingerprint (``file_hash``: anyone holding the same
file can confirm it), backend ``file_id``/``parent_id`` (a TeleDrive login turns
them back into a name), Telegram message ids and user ids (an account turns
those into the file), or an exact byte count (big files are nearly unique).
Account ordinals (``primary``, ``#2``...) and a size magnitude stand in.

Only the caches are read. An id whose folder was never listed is "not found";
this never asks the backend, because looking something up must not become a
reason to hit the network.

Search order: ``meta/dirs/`` (backend rows, with the zip central directories in
``meta/zips/`` for members of packed archives), then ``uploads/`` and
``staging/`` (files that have not gone up yet). Names are used in memory to
compute ids and are never written out.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from teledrive_webdav.core import fileid

_SAFE_KEY = re.compile(r"[A-Za-z0-9._-]{1,80}")
GAME = "game"


def _safe_name(key: str) -> str:
    return key if _SAFE_KEY.fullmatch(key) else hashlib.sha1(key.encode("utf-8")).hexdigest()


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_magnitude = fileid.size_magnitude


def _real_size(file_hash) -> Optional[int]:
    text = str(file_hash or "")
    if ":" not in text:
        return None
    try:
        return int(text.rsplit(":", 1)[1])
    except ValueError:
        return None


def _account_label(user_id, primary: int, account_ids: Sequence[int]) -> str:
    user_id = int(user_id or 0)
    if user_id in (0, primary):
        return "primary"
    ordered = [primary] + sorted(i for i in account_ids if i != primary)
    return f"#{ordered.index(user_id) + 1}" if user_id in ordered else "#?"


# --------------------------------------------------------------------------- #
# The cached drive
# --------------------------------------------------------------------------- #


def _drive_paths(listings: Dict[str, List[dict]]) -> Iterator[Tuple[str, dict, int]]:
    """``(vfs path, row, sibling rows with the same name)`` for every cached row."""
    by_id: Dict[str, dict] = {}
    for rows in listings.values():
        for row in rows:
            by_id[row["file_id"]] = row
    memo: Dict[str, Optional[str]] = {"__root__": ""}

    def folder_path(key: str, depth: int = 0) -> Optional[str]:
        if key in memo:
            return memo[key]
        row = by_id.get(key)
        if row is None or depth > 64:
            return None
        parent = row.get("parent_id") or "__root__"
        base = folder_path(parent, depth + 1)
        memo[key] = None if base is None else (f"{base}/{row['filename']}" if base else row["filename"])
        return memo[key]

    for key, rows in listings.items():
        base = folder_path(key)
        if base is None:
            continue
        names: Dict[str, int] = {}
        for row in rows:
            names[row["filename"]] = names.get(row["filename"], 0) + 1
        for row in rows:
            name = row["filename"]
            if base == GAME and not row.get("isDir") and name.lower().endswith(".zip"):
                name = name[: -len(".zip")]  # the drive shows an archive as a folder
            path = f"{base}/{name}" if base else name
            yield path, row, names[row["filename"]]


def _zip_members(meta: Path, archive_path: str, row: dict) -> Iterator[Tuple[str, dict]]:
    """Paths of every member of a packed archive, as they appear on the drive."""
    key = f"{int(row.get('telegram_user_id') or 0)}-{row['file_id']}"
    blob = _read_json(meta / "zips" / f"{_safe_name(key)}.json")
    if not isinstance(blob, dict) or "n" not in blob:
        return
    from teledrive_webdav.dav import zipfs

    stem = archive_path[: -len(".zip")] if archive_path.lower().endswith(".zip") else archive_path
    root = zipfs.tree_from_json(blob)

    def walk(node, prefix):
        for name, child in node.children.items():
            path = f"{prefix}/{name}"
            yield path, child
            if child.is_dir:
                yield from walk(child, path)

    yield from walk(root, stem)


def _local_paths(data_root: Path) -> Iterator[Tuple[str, str, Path]]:
    """``(vfs path, kind, file)`` for files that are staged but not yet uploaded."""
    for sub, kind, prefix in (("uploads", "general", ""), ("staging", "game-staging", GAME + "/")):
        base = data_root / sub
        for dirpath, dirnames, filenames in os.walk(base):
            rel_dir = Path(dirpath).relative_to(base)
            if kind == "game-staging" and rel_dir == Path("."):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                rel = (rel_dir / fn).as_posix() if rel_dir != Path(".") else fn
                yield prefix + rel, kind, Path(dirpath) / fn


def _queue_state(meta: Path) -> Dict[str, dict]:
    blob = _read_json(meta / "upload-queue.json")
    return blob.get("pending", {}) if isinstance(blob, dict) else {}


# --------------------------------------------------------------------------- #
# The index: built once, refreshed only when a cache file changes
# --------------------------------------------------------------------------- #


class _Member:
    __slots__ = ("name", "size")

    def __init__(self, name: str, size: int):
        self.name, self.size = name, size


class DriveIndex:
    """``id -> where it lives`` over ``meta/dirs/`` and ``meta/zips/``.

    The first lookup used to re-read every listing file and every zip central
    directory and hash every path, which took ~59s on this drive and held a
    bridge worker thread the whole time. Now a listing file is re-read only
    when its (mtime, size) changes and the id table is rebuilt only then; a
    zip tree is read once, lazily, the first time an id is not a drive row.
    Looking for an id that does not exist costs one ``stat`` per cache file.
    """

    def __init__(self, meta: Path):
        self.meta = meta
        self._lock = threading.Lock()
        self._stamps: Dict[str, Tuple[int, int]] = {}
        self._listings: Dict[str, List[dict]] = {}
        self._by_id: Dict[int, dict] = {}
        self._zips: List[Tuple[str, dict]] = []
        self._zip_members: Dict[str, Tuple[Tuple[int, int], Dict[int, Tuple[str, str, int]]]] = {}

    @staticmethod
    def _stamp(path: Path) -> Optional[Tuple[int, int]]:
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def refresh(self) -> None:
        with self._lock:
            stamps: Dict[str, Tuple[int, int]] = {}
            for path in (self.meta / "dirs").glob("*.json"):
                stamp = self._stamp(path)
                if stamp is not None:
                    stamps[path.stem] = stamp
            if stamps == self._stamps:
                return
            for key, stamp in stamps.items():
                if self._stamps.get(key) == stamp:
                    continue
                blob = _read_json(self.meta / "dirs" / f"{key}.json")
                if isinstance(blob, dict) and isinstance(blob.get("rows"), list):
                    self._listings[key] = blob["rows"]
                else:
                    self._listings.pop(key, None)
            for gone in set(self._stamps) - set(stamps):
                self._listings.pop(gone, None)
            self._stamps = stamps
            self._rebuild()

    def _rebuild(self) -> None:
        by_id: Dict[int, dict] = {}
        zips: List[Tuple[str, dict]] = []
        for path, row, same in _drive_paths(self._listings):
            by_id.setdefault(fileid.path_id(path), {"path": path, "row": row, "same": same})
            if (not row.get("isDir") and path.startswith(GAME + "/")
                    and str(row.get("filename", "")).lower().endswith(".zip")):
                zips.append((path, row))
        self._by_id, self._zips = by_id, zips

    def find(self, target: int) -> Optional[dict]:
        return self._by_id.get(target)

    def find_zip_member(self, target: int):
        for path, row in self._zips:
            member = self._member_ids(path, row).get(target)
            if member is not None:
                member_path, name, size = member
                return member_path, _Member(name, size), row
        return None

    def _member_ids(self, archive_path: str, row: dict) -> Dict[int, Tuple[str, str, int]]:
        key = f"{int(row.get('telegram_user_id') or 0)}-{row['file_id']}"
        tree = self.meta / "zips" / f"{_safe_name(key)}.json"
        stamp = self._stamp(tree)
        cached = self._zip_members.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        ids = {
            fileid.path_id(member_path): (member_path, node.name, node.size)
            for member_path, node in _zip_members(self.meta, archive_path, row)
        } if stamp is not None else {}
        self._zip_members[key] = (stamp, ids)
        return ids


_INDEXES: Dict[str, DriveIndex] = {}
_INDEXES_LOCK = threading.Lock()


def _index_for(meta: Path) -> DriveIndex:
    key = str(meta)
    with _INDEXES_LOCK:
        index = _INDEXES.get(key)
        if index is None:
            index = _INDEXES[key] = DriveIndex(meta)
        return index


# --------------------------------------------------------------------------- #
# lookup
# --------------------------------------------------------------------------- #

MAX_ATTEMPTS = 5  # uploadstage / gamestage: the fifth failure abandons the file


def _format(name: str, mime: Optional[str]) -> dict:
    ext = os.path.splitext(name)[1].lower()
    return {"extension": ext, "mime": mime}


def lookup(target: int, cache_dir: Path, *, primary_user_id: int = 0,
           account_ids: Sequence[int] = (), live: Optional[dict] = None, api=None) -> Optional[dict]:
    """Facts about the file with id ``target``, or None. ``api`` is never touched."""
    meta = Path(cache_dir)
    data_root = meta.parent
    index = _index_for(meta)
    index.refresh()
    drive = index.find(target)
    zip_member = None if drive else index.find_zip_member(target)

    local = None
    for path, kind, file in _local_paths(data_root):
        if fileid.path_id(path) == target:
            local = (path, kind, file)
            break

    queue = _queue_state(meta)
    if drive:
        row, path = drive["row"], drive["path"]
        top_game = path.startswith(GAME + "/")
        kind = ("game-packed" if top_game and str(row.get("filename", "")).lower().endswith(".zip")
                else "general" if not top_game else "game-file")
        real = _real_size(row.get("file_hash"))
        key = f"{int(row.get('telegram_user_id') or 0)}-{row['file_id']}"
        props = _read_json(meta / "media_props.json") or {}
        media = {k: v for k, v in (props.get(key) or {}).items() if k in ("width", "height", "duration")}
        accounts = [_account_label(row.get("telegram_user_id"), primary_user_id, account_ids)]
        info = {
            "format": _format(row["filename"], row.get("mime_type")),
            "size": {"approx": _magnitude(real if real is not None else row.get("filesize")),
                     "backend_minus_real_bytes": (int(row.get("filesize") or 0) - real) if real is not None else None},
            "media": media,
            "location": {"kind": kind, "depth": path.count("/") + 1},
            "storage": {"kind": ("empty" if not row.get("telegram_message_id")
                                 else "split" if row.get("is_split_file") else "single"),
                        "accounts": accounts},
            "state": {
                "upload": None,
                "cache": {
                    "thumbnail": (meta / "thumbs" / f"{key}.jpg").exists(),
                    "props": key in props,
                    "has_thumbnail_flag": bool(row.get("has_thumbnail")),
                },
                "same_name_rows": drive["same"],
                "created": str(row.get("created_at") or "")[:10],
            },
        }
        if row.get("isDir"):
            info["storage"] = {"kind": "folder", "accounts": []}
        return _with_live(info, target, queue, path, live)
    if zip_member:
        member_path, node, row = zip_member
        info = {
            "format": _format(node.name, None),
            "size": {"approx": _magnitude(node.size), "backend_minus_real_bytes": None},
            "media": {},
            "location": {"kind": "zip-member", "depth": member_path.count("/") + 1},
            "storage": {"kind": "inside-archive",
                        "accounts": [_account_label(row.get("telegram_user_id"), primary_user_id, account_ids)]},
            "state": {"upload": None, "cache": {}, "same_name_rows": 1, "created": ""},
        }
        return info
    if local:
        path, kind, file = local
        try:
            size = file.stat().st_size
        except OSError:
            size = None
        info = {
            "format": _format(path, None),
            "size": {"approx": _magnitude(size), "backend_minus_real_bytes": None},
            "media": {},
            "location": {"kind": kind, "depth": path.count("/") + 1},
            "storage": {"kind": "local-only", "accounts": []},
            "state": {"upload": {"stage": "staged"}, "cache": {}, "same_name_rows": 0, "created": ""},
        }
        rec = queue.get(path)
        if rec:
            attempts = int(rec.get("attempts") or 0)
            info["state"]["upload"] = {
                "stage": rec.get("stage"), "attempts": attempts,
                "abandoned": rec.get("stage") == "abandoned" or attempts >= MAX_ATTEMPTS,
            }
        return _with_live(info, target, queue, path, live)
    return None


def _with_live(info: dict, target: int, queue: Dict[str, dict], path: str, live: Optional[dict]) -> dict:
    rec = queue.get(path)
    if rec and info["state"]["upload"] is None:
        attempts = int(rec.get("attempts") or 0)
        info["state"]["upload"] = {
            "stage": rec.get("stage"), "attempts": attempts,
            "abandoned": rec.get("stage") == "abandoned" or attempts >= MAX_ATTEMPTS,
        }
    if live:
        wanted = "0x%016x" % target
        for item in (live.get("uploads") or {}).get("pending", []):
            if item.get("id") == wanted:
                info["state"]["upload"] = {
                    "stage": item.get("stage"), "attempts": item.get("attempts"),
                    "abandoned": item.get("stage") == "abandoned",
                    "last_error": item.get("detail") or None,
                }
        for item in live.get("units", []):
            if item.get("id") == wanted:
                info["state"]["upload"] = {
                    "stage": item.get("state"), "attempts": item.get("attempts"),
                    "abandoned": item.get("state") == "abandoned",
                    "last_error": item.get("detail") or None,
                }
    return info


# --------------------------------------------------------------------------- #
# CLI glue (kept here so fileid.py stays import-light for the logging path)
# --------------------------------------------------------------------------- #


def _load_cache_dir() -> Path:
    from teledrive_webdav.core.config import load_config

    return load_config().cache_dir


def _load_accounts() -> Tuple[int, Tuple[int, ...]]:
    from teledrive_webdav.core.config import load_config

    cfg = load_config()
    ids = tuple(
        int(p.stem) for p in Path(cfg.session_dir).glob("*.session") if p.stem.isdigit()
    ) if Path(cfg.session_dir).is_dir() else ()
    return int(cfg.primary_user_id), ids


def info_main(argv: Sequence[str]) -> int:
    try:
        target = int(argv[0], 16)
    except (IndexError, ValueError):
        print("usage: python -m teledrive_webdav.core.fileid info 0x<id>", file=sys.stderr)
        return 2
    primary, ids = _load_accounts()
    result = lookup(target, _load_cache_dir(), primary_user_id=primary, account_ids=ids)
    if result is None:
        print("not found in the cached drive, uploads or staging", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
