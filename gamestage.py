"""/game staging: move a folder in, get a packed archive out.

Writes under ``/game/<Name>`` land in ``staging_dir`` first. Once that subtree
has been quiet for ``debounce_minutes`` the move is considered finished, and the
subtree is packed into ``<Name>.zip`` and uploaded to Telegram.

The zip is deliberately **stored, not compressed**: game files are already
compressed, so deflate buys nothing but would destroy the property that makes
this design work — with ZIP_STORED each member's bytes are a plain byte range of
the archive, so zipfs.py can serve one file out of a 60 GB archive by reading
only that file.

A first-level *file* dropped into /game is uploaded as-is (no zip wrapper): a
user who already packed their own archive should not get it double-wrapped.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from config import ext_path as _ext
from tgio import SEGMENT_SIZE, SegmentReader, plan_segments

log = logging.getLogger("gamestage")

# Same fingerprint as the browser (frontend/src/lib/hashFile.ts): SHA-256 of the
# first 100 MB, then ":<size>". Both producers must agree or dedup silently stops
# working across web and bridge uploads.
HASH_SAMPLE = 100 * 1024 * 1024

TICK_SECONDS = 15
RETRY_SECONDS = 600
MAX_ATTEMPTS = 5

# One segment failing outright (a part permanently exhausted its retries)
# shouldn't cost a whole unit retry — that re-packs the archive from scratch
# and re-uploads every already-committed segment. A couple of quick retries
# with a fresh upload (fresh file_id, since tgio/tgupload generate one per
# call) rides out the common transient case instead.
SEGMENT_RETRIES = 2
SEGMENT_RETRY_SECONDS = 30
ZIP_MIME = "application/zip"


def sample_hash(path: Path) -> str:
    """Dedup fingerprint of a local file, matching the web client's format."""
    size = path.stat().st_size
    digest = hashlib.sha256()
    remaining = min(size, HASH_SAMPLE)
    with open(_ext(path), "rb") as fh:
        while remaining > 0:
            chunk = fh.read(min(1 << 20, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            digest.update(chunk)
    return f"{digest.hexdigest()}:{size}"


def canonical_existing_parts(rows: Sequence[dict]) -> List[dict]:
    """Collapse same-hash rows from /files/check-hash to one upload's real parts.

    A port of frontend/src/lib/uploadPlanner.ts:canonicalExistingParts. The
    endpoint returns *every* row sharing the hash, including rows created by
    earlier dedup registrations; registering one new row per returned row makes
    the count double on every re-upload and fabricates split groups with
    thousands of bogus parts. Collapsing keeps a duplicate registration at
    exactly total_parts rows.
    """
    if not rows:
        return []

    def to_part(row: dict, index: int) -> dict:
        return {
            "filesize": int(row.get("filesize") or 0),
            "mime_type": row.get("mime_type"),
            "message_id": row.get("telegram_message_id"),
            "access_hash": row.get("access_hash"),
            "part_index": index,
        }

    single = next(
        (r for r in rows if not r.get("is_split_file") and r.get("telegram_message_id") is not None), None
    )
    if single is not None:
        return [to_part(single, 0)]

    groups: Dict[str, List[dict]] = {}
    for row in rows:
        key = row.get("split_group_id") or row.get("file_id")
        groups.setdefault(key, []).append(row)

    best: List[dict] = []
    best_distinct = -1
    for group in groups.values():
        distinct = len({(r.get("part_index") or 0) for r in group})
        if distinct > best_distinct:
            best_distinct, best = distinct, group

    by_index: Dict[int, dict] = {}
    for row in best:
        index = row.get("part_index") or 0
        if row.get("telegram_message_id") is not None and index not in by_index:
            by_index[index] = row
    return [to_part(by_index[i], i) for i in sorted(by_index)]


@dataclass
class Unit:
    """One pack unit: a first-level item under /game."""

    name: str
    last_write: float = field(default_factory=time.monotonic)
    state: str = "staging"
    attempts: int = 0
    retry_after: float = 0.0
    detail: str = ""


class GameStager:
    def __init__(self, cfg, api, worker):
        self.cfg = cfg
        self.api = api
        self.worker = worker
        self._units: Dict[str, Unit] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.cfg.staging_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.pack_dir.mkdir(parents=True, exist_ok=True)
        self._adopt_leftovers()

    # -- staging paths ---------------------------------------------------- #

    def path_for(self, segments: Sequence[str]) -> Optional[Path]:
        """Local staging path for a /game-relative path, or None if unsafe."""
        segments = list(segments)
        if not segments:
            return self.cfg.staging_dir
        if any(s in ("", ".", "..") or "/" in s or "\\" in s or s.startswith(".") for s in segments):
            return None
        path = self.cfg.staging_dir.joinpath(*segments)
        try:
            path.resolve().relative_to(self.cfg.staging_dir.resolve())
        except ValueError:
            return None
        return path

    def top_level_names(self) -> List[str]:
        try:
            return sorted(n for n in os.listdir(self.cfg.staging_dir) if not n.startswith("."))
        except OSError:
            return []

    def mkdir(self, segments: Sequence[str]) -> Path:
        path = self.path_for(segments)
        if path is None:
            raise PermissionError(f"unsafe staging path: {segments}")
        path.mkdir(parents=True, exist_ok=True)
        self.touch(list(segments)[0])
        return path

    def create_file(self, segments: Sequence[str]) -> Path:
        path = self.path_for(segments)
        if path is None:
            raise PermissionError(f"unsafe staging path: {segments}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        self.touch(list(segments)[0])
        return path

    def move(self, src: Path, dest_segments: Sequence[str]) -> None:
        """Rename inside staging (dest_segments starts with the /game element)."""
        rest = list(dest_segments)[1:]
        dest = self.path_for(rest)
        if dest is None:
            raise PermissionError("move destination is outside the staging area")
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(_ext(src), _ext(dest))
        if rest:
            self.touch(rest[0])

    def copy(self, src: Path, dest_segments: Sequence[str]) -> Path:
        """Copy inside staging (dest_segments starts with the /game element).

        Unlike move(), this has no WriteGuard destination check guaranteeing
        dest_segments[0] is the game folder — COPY is ungated (see WriteGuard
        in bridge.py) so this validates it itself.
        """
        if not dest_segments or dest_segments[0] != self.cfg.game_folder:
            raise PermissionError("copy destination must stay under /game while staged")
        rest = list(dest_segments)[1:]
        dest = self.path_for(rest)
        if dest is None:
            raise PermissionError(f"copy destination is outside the staging area: {dest_segments}")
        if src.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_ext(src), _ext(dest))
        if rest:
            self.touch(rest[0])
        return dest

    def touch(self, top: str) -> None:
        """Record write activity, restarting that unit's debounce window."""
        if not top or top.startswith("."):
            return
        with self._lock:
            unit = self._units.get(top)
            if unit is None:
                unit = Unit(name=top)
                self._units[top] = unit
                log.info("staging started: %s", top)
            unit.last_write = time.monotonic()
            if unit.state in ("done", "failed"):
                unit.state = "staging"
                unit.attempts = 0

    def _adopt_leftovers(self) -> None:
        """Pick up staging left behind by a crash, dated by newest file mtime."""
        now = time.time()
        for name in self.top_level_names():
            path = self.cfg.staging_dir / name
            newest = 0.0
            for root, _dirs, files in os.walk(_ext(path)):
                for fn in files:
                    try:
                        newest = max(newest, os.path.getmtime(os.path.join(root, fn)))
                    except OSError:
                        pass
            if newest == 0.0:
                try:
                    newest = path.stat().st_mtime
                except OSError:
                    newest = now
            idle = max(0.0, now - newest)
            self._units[name] = Unit(name=name, last_write=time.monotonic() - idle)
            log.info("adopted leftover staging unit %s (idle %.0fs)", name, idle)

    # -- background loop -------------------------------------------------- #

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="gamestage", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        now = time.monotonic()
        with self._lock:
            units = [
                {
                    "name": u.name,
                    "state": u.state,
                    "idle_seconds": round(now - u.last_write, 1),
                    "attempts": u.attempts,
                    "detail": u.detail,
                }
                for u in self._units.values()
            ]
        return {"debounce_minutes": self.cfg.debounce_minutes, "units": units}

    def _loop(self) -> None:
        debounce = self.cfg.debounce_minutes * 60
        while not self._stop.wait(TICK_SECONDS):
            try:
                for top in self._due(debounce):
                    self._process(top)
            except Exception:  # pragma: no cover - keep the loop alive
                log.exception("staging loop error")

    def _due(self, debounce: float) -> List[str]:
        now = time.monotonic()
        ready = []
        existing = set(self.top_level_names())
        with self._lock:
            for name, unit in list(self._units.items()):
                if name not in existing:
                    # Uploaded and cleaned up, or removed behind our back.
                    if unit.state != "uploading":
                        self._units.pop(name, None)
                    continue
                if unit.state not in ("staging", "failed"):
                    continue
                if unit.state == "failed" and now < unit.retry_after:
                    continue
                if now - unit.last_write >= debounce:
                    unit.state = "packing"
                    ready.append(name)
            # A unit can exist on disk without a Unit record after a manual copy
            # into staging_dir; adopt it so it is never stranded.
            for name in existing - set(self._units):
                self._units[name] = Unit(name=name)
        return ready

    def _process(self, top: str) -> None:
        source = self.cfg.staging_dir / top
        packed: Optional[Path] = None
        try:
            log.info("packing %s", top)
            packed, upload_name, temporary = self._pack(top, source)
            self._set_state(top, "uploading")
            self._upload_and_register(packed, upload_name)
            self._set_state(top, "done")
            log.info("uploaded %s — clearing staging", upload_name)
            shutil.rmtree(_ext(source), ignore_errors=True)
            if temporary and packed.exists():
                packed.unlink()
            with self._lock:
                self._units.pop(top, None)
        except Exception as exc:
            log.exception("packing/upload of %s failed", top)
            with self._lock:
                unit = self._units.get(top) or Unit(name=top)
                unit.attempts += 1
                unit.detail = f"{type(exc).__name__}: {exc}"
                if unit.attempts >= MAX_ATTEMPTS:
                    unit.state = "abandoned"
                    log.error("giving up on %s after %s attempts — staging kept", top, unit.attempts)
                else:
                    unit.state = "failed"
                    unit.retry_after = time.monotonic() + RETRY_SECONDS
                self._units[top] = unit
            # Only discard the pack once the unit is truly abandoned. A
            # transient failure keeps it on disk (see _pack's reuse check) so
            # the unit retry doesn't re-zip a potentially 60 GB tree just
            # because one segment's upload failed.
            if unit.state == "abandoned" and packed is not None and packed.exists() and packed.parent == self.cfg.pack_dir:
                packed.unlink(missing_ok=True)

    def _set_state(self, top: str, state: str) -> None:
        with self._lock:
            unit = self._units.get(top)
            if unit is not None:
                unit.state = state

    # -- packing ---------------------------------------------------------- #

    def _pack(self, top: str, source: Path):
        """Return ``(archive_path, upload_name, is_temporary)``."""
        if source.is_file():
            # Already a single file — upload verbatim.
            return source, source.name, False

        target = self.cfg.pack_dir / f"{top}.zip"
        if target.exists():
            if zipfile.is_zipfile(target):
                # A previous attempt already packed this and only the upload
                # failed transiently (_process keeps the zip in that case,
                # see the exception handler there) — reuse it instead of
                # re-zipping a potentially 60 GB tree.
                log.info("reusing existing pack %s from a previous attempt", target.name)
                return target, f"{top}.zip", True
            target.unlink()
        root = _ext(source)
        count = 0
        with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for dirpath, dirnames, filenames in os.walk(root):
                rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
                prefix = "" if rel_dir == "." else rel_dir + "/"
                if prefix:
                    # Explicit directory entries keep empty folders alive.
                    info = zipfile.ZipInfo(prefix)
                    info.external_attr = (0o040755 << 16) | 0x10
                    zf.writestr(info, b"")
                dirnames.sort()
                for name in sorted(filenames):
                    full = os.path.join(dirpath, name)
                    try:
                        zf.write(full, arcname=prefix + name)
                        count += 1
                    except OSError as exc:
                        raise OSError(f"cannot read {full}: {exc}") from exc
        log.info("packed %s files into %s (%.1f GiB)", count, target.name, target.stat().st_size / 2**30)
        return target, f"{top}.zip", True

    # -- uploading -------------------------------------------------------- #

    def _upload_and_register(self, archive: Path, upload_name: str) -> None:
        game = self.api.ensure_folder(self.cfg.game_folder)
        upload_and_register(self.api, self.worker, archive, upload_name, game.file_id, ZIP_MIME)


# -- uploading (shared with uploadstage.py's generic, non-/game writes) --- #


def upload_and_register(
    api, worker, archive: Path, upload_name: str, parent_id: Optional[str], mime_type: str
) -> None:
    """Upload one already-local file to Telegram and register it in TeleDrive.

    Dedups against ``check_hash`` first, same fingerprint the browser uses, so
    content already on Telegram is registered without a second upload.
    """
    size = archive.stat().st_size
    if size == 0:
        # Telegram rejects a 0-part file with an opaque RPC error; fail
        # fast and readably instead of reaching that path.
        raise ValueError(f"{upload_name} is empty (0 bytes) — nothing to upload")
    file_hash = sample_hash(archive)

    existing = _lookup_duplicate(api, file_hash)
    if existing:
        log.info("%s already on Telegram (%s parts) — registering without uploading", upload_name, len(existing))
        parts = existing
    else:
        parts = _upload_segments(worker, archive, size, upload_name)

    split_group_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:7]}"
    total = len(parts)
    for index, part in enumerate(parts):
        api.register(
            filename=upload_name,
            filesize=part["filesize"],
            message_id=part["message_id"],
            file_id=part.get("file_id") or f"{split_group_id}-{index}",
            access_hash=part.get("access_hash"),
            mime_type=mime_type,
            parent_id=parent_id,
            is_split_file=total > 1,
            original_name=upload_name,
            part_index=index,
            total_parts=total,
            split_group_id=split_group_id,
            file_hash=file_hash,
        )
    api.invalidate(parent_id)


def _lookup_duplicate(api, file_hash: str) -> List[dict]:
    try:
        result = api.check_hash(file_hash)
    except Exception as exc:
        log.warning("dedup check failed (%s) — uploading anyway", exc)
        return []
    if not result or not result.get("found"):
        return []
    return canonical_existing_parts(result.get("files") or [])


def _upload_segments(worker, archive: Path, size: int, upload_name: str) -> List[dict]:
    segments = plan_segments(size, SEGMENT_SIZE)
    parts: List[dict] = []
    for index, (offset, seg_size) in enumerate(segments):
        name = upload_name if len(segments) == 1 else f"{upload_name}.part{index + 1}"
        log.info(
            "uploading %s (%s/%s, %.1f MiB)", name, index + 1, len(segments), seg_size / 2**20
        )
        reader = SegmentReader(_ext(archive), offset, seg_size)
        try:
            result = _upload_one_segment(worker, reader, seg_size, name)
        finally:
            reader.close()
        parts.append({**result, "filesize": result["size"]})
    return parts


def _upload_one_segment(worker, reader: SegmentReader, seg_size: int, name: str) -> dict:
    for attempt in range(SEGMENT_RETRIES + 1):
        try:
            return worker.upload_segment(
                reader, seg_size, name, progress=_progress_logger(name, seg_size)
            )
        except Exception:
            if attempt >= SEGMENT_RETRIES:
                raise
            log.warning(
                "segment %s failed (attempt %s/%s) — retrying in %ss",
                name, attempt + 1, SEGMENT_RETRIES + 1, SEGMENT_RETRY_SECONDS,
            )
            reader.seek(0)
            time.sleep(SEGMENT_RETRY_SECONDS)


def _progress_logger(name: str, total: int):
    state = {"last": 0.0}

    def report(sent: int, _total: int = total) -> None:
        pct = (sent / total * 100) if total else 100.0
        if pct - state["last"] >= 10:
            state["last"] = pct
            log.info("  %s: %.0f%%", name, pct)

    return report
