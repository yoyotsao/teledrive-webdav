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

import contextlib
import hashlib
import logging
import os
import shutil
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from config import ext_path as _ext
from tgio import make_preview
from transfer_models import TransferRequest
from upload_engine import guess_mime_type

log = logging.getLogger("gamestage")

# Same fingerprint as the browser (frontend/src/lib/hashFile.ts): SHA-256 of the
# first 100 MB, then ":<size>". Both producers must agree or dedup silently stops
# working across web and bridge uploads.
HASH_SAMPLE = 100 * 1024 * 1024

TICK_SECONDS = 15
RETRY_SECONDS = 600
MAX_ATTEMPTS = 5

# A transient failure used to be retried here, a whole segment at a time, so
# that one bad part did not cost a re-pack of a 60 GB tree. That now happens
# one level down and far more cheaply: tgupload.send_part retries each 512 KiB
# part three times before giving up, so the only retry left at this level is
# the unit's own, ten minutes later.
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
    def __init__(self, cfg, api, engine):
        self.cfg = cfg
        self.api = api
        self.engine = engine
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
        if not dest_segments or dest_segments[0] != self.cfg.game_folder:
            raise PermissionError("move destination must stay under /game while staged")
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
            packed, upload_name, temporary, archived = self._pack(top, source)
            self._set_state(top, "uploading")
            self._upload_and_register(packed, upload_name, archived=archived)
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
        """Return ``(archive_path, upload_name, is_temporary, is_archive)``.

        ``is_archive`` is whether *this* code made the zip, which is not the
        same question as whether the name ends in ``.zip``: a user who drops
        their own archive in gets it uploaded verbatim, and a user who drops a
        photo in must keep ``image/jpeg`` or lose its preview on both clients.
        """
        if source.is_file():
            # Already a single file — upload verbatim.
            return source, source.name, False, False

        target = self.cfg.pack_dir / f"{top}.zip"
        if target.exists():
            if zipfile.is_zipfile(target):
                # A previous attempt already packed this and only the upload
                # failed transiently (_process keeps the zip in that case,
                # see the exception handler there) — reuse it instead of
                # re-zipping a potentially 60 GB tree.
                log.info("reusing existing pack %s from a previous attempt", target.name)
                return target, f"{top}.zip", True, True
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
        return target, f"{top}.zip", True, True

    # -- uploading -------------------------------------------------------- #

    def _upload_and_register(self, archive: Path, upload_name: str, *, archived: bool) -> None:
        """Hand the finished archive to the one engine every write shares.

        Dedup, protocol choice, account routing and registration all live
        there; the only thing /game knows that the engine does not is that a
        zip it built itself is not media -- no preview to attach, and nothing
        to group into an album with the next one.
        """
        game = self.api.ensure_folder(self.cfg.game_folder)
        mime_type = ZIP_MIME if archived else guess_mime_type(upload_name)
        request = TransferRequest(
            source=archive,
            upload_name=upload_name,
            mime_type=mime_type,
            parent_id=game.file_id,
            logical_size=archive.stat().st_size,
            allow_album=not archived,
        )
        self.engine.register_result(self.engine.transfer(request))


# -- shared with upload_engine.py: the on-disk preview a message needs --- #


@contextlib.contextmanager
def _preview_file(image: Optional[Path], mime_type: str = "", ffmpeg: Optional[str] = None):
    """Yield ``(jpeg_path, width, height)`` for media ``image``, or None.

    On disk rather than in memory because Telethon uploads a thumbnail by name
    and Telegram ignores one that does not look like a ``.jpg`` file. In the
    system temp directory rather than beside the upload: ``uploads/`` and
    ``staging/`` are both scanned for work, and a stray file there would be
    read back as something the user asked to upload.
    """
    made = (make_preview(image, mime_type, ffmpeg) if ffmpeg is not None
            else make_preview(image, mime_type)) if image is not None else None
    if made is None:
        yield None
        return
    data, width, height = made
    fd, name = tempfile.mkstemp(suffix=".jpg", prefix="tdthumb-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        log.info("preview for %s: %sx%s, %s bytes", image.name, width, height, len(data))
        yield Path(name), width, height
    finally:
        try:
            os.unlink(name)
        except OSError:  # pragma: no cover - the upload already succeeded
            pass
