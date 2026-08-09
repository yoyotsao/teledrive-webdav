"""Plain writes outside /game: land locally, debounce, then upload as-is.

Mirrors gamestage.py's staging model file for file: a write lands under
``upload_dir`` first, and once quiet for ``debounce_minutes`` it is uploaded
to Telegram and registered with TeleDrive. The differences are only what
follows from there being no packing step:

- A unit here is always exactly one file. Folders outside /game are real
  TeleDrive folders (``POST /folders``, created the moment MKCOL arrives —
  see ``bridge.RootCollection.create_collection``), never staged, so there is
  nothing to zip.
- A unit's destination parent is whatever folder the write actually resolved
  to, captured at write time, instead of a folder fixed in advance.

Dedup, segment upload, and registration are the exact same code /game uses
(``gamestage.upload_and_register``) — this module only supplies the staging
and debounce half.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from config import ext_path as _ext
from gamestage import MAX_ATTEMPTS, RETRY_SECONDS, TICK_SECONDS, upload_and_register

log = logging.getLogger("uploadstage")


@dataclass
class PendingUpload:
    """One file waiting to go up: identified by its full destination path."""

    segments: Tuple[str, ...]
    parent_id: Optional[str]
    last_write: float = field(default_factory=time.monotonic)
    state: str = "staging"
    attempts: int = 0
    retry_after: float = 0.0
    detail: str = ""

    @property
    def name(self) -> str:
        return self.segments[-1]


class UploadStager:
    def __init__(self, cfg, api, worker):
        self.cfg = cfg
        self.api = api
        self.worker = worker
        self._pending: Dict[Tuple[str, ...], PendingUpload] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.cfg.upload_dir.mkdir(parents=True, exist_ok=True)
        self._adopt_leftovers()

    # -- staging paths ------------------------------------------------- #

    def path_for(self, segments: Sequence[str]) -> Optional[Path]:
        """Local staging path for a full destination path, or None if unsafe."""
        segments = list(segments)
        if not segments or any(s in ("", ".", "..") or "/" in s or "\\" in s or s.startswith(".") for s in segments):
            return None
        path = self.cfg.upload_dir.joinpath(*segments)
        try:
            path.resolve().relative_to(self.cfg.upload_dir.resolve())
        except ValueError:
            return None
        return path

    def get(self, segments: Sequence[str]) -> Optional[PendingUpload]:
        with self._lock:
            return self._pending.get(tuple(segments))

    def pending_for(self, segments: Sequence[str]) -> Optional[Path]:
        """The local staged copy for a not-yet-uploaded write, if any."""
        if self.get(segments) is None:
            return None
        path = self.path_for(segments)
        return path if path is not None and path.exists() else None

    def names_under(self, parent_segments: Sequence[str]) -> List[str]:
        """Direct children currently staged under one folder."""
        prefix = tuple(parent_segments)
        depth = len(prefix) + 1
        with self._lock:
            return sorted({
                key[len(prefix)]
                for key in self._pending
                if len(key) == depth and key[: len(prefix)] == prefix
            })

    def create_file(self, segments: Sequence[str], parent_id: Optional[str]) -> Path:
        path = self.path_for(segments)
        if path is None:
            raise PermissionError(f"unsafe upload path: {segments}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        self.touch(segments, parent_id)
        return path

    def forget(self, segments: Sequence[str]) -> None:
        """Drop a pending record outright (a delete, not a debounce reset).

        ``names_under`` lists straight from ``_pending``, unlike ``top_level_names``
        in gamestage.py which lists the staging directory itself — so unlinking the
        local file alone leaves a name in that listing with nothing left to resolve
        to, and the next PROPFIND crashes trying to look it up.
        """
        with self._lock:
            self._pending.pop(tuple(segments), None)

    def touch(self, segments: Sequence[str], parent_id: Optional[str] = None) -> None:
        """Record write activity, restarting that file's debounce window."""
        key = tuple(segments)
        with self._lock:
            pending = self._pending.get(key)
            if pending is None:
                pending = PendingUpload(segments=key, parent_id=parent_id)
                self._pending[key] = pending
                log.info("upload staged: %s", "/".join(key))
            if parent_id is not None:
                pending.parent_id = parent_id
            pending.last_write = time.monotonic()
            if pending.state in ("done", "failed"):
                pending.state = "staging"
                pending.attempts = 0

    def _adopt_leftovers(self) -> None:
        """Pick up staged files left behind by a crash.

        The parent folder is re-resolved from the backend rather than
        remembered — folders outside /game are real and outlive a crash —
        so a leftover is never orphaned without a destination to register to.
        """
        now = time.time()
        for root, _dirs, files in os.walk(_ext(self.cfg.upload_dir)):
            for fn in files:
                full = Path(root) / fn
                try:
                    segments = tuple(full.relative_to(self.cfg.upload_dir).parts)
                except ValueError:
                    continue
                try:
                    idle = max(0.0, now - full.stat().st_mtime)
                except OSError:
                    idle = 0.0
                parent_id = None
                if len(segments) > 1:
                    parent = self.api.resolve(list(segments[:-1]))
                    parent_id = parent.file_id if parent is not None else None
                self._pending[segments] = PendingUpload(
                    segments=segments, parent_id=parent_id, last_write=time.monotonic() - idle,
                )
                log.info("adopted leftover upload %s (idle %.0fs)", "/".join(segments), idle)

    # -- background loop -------------------------------------------------- #

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="uploadstage", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        now = time.monotonic()
        with self._lock:
            pending = [
                {
                    "path": "/".join(p.segments),
                    "state": p.state,
                    "idle_seconds": round(now - p.last_write, 1),
                    "attempts": p.attempts,
                    "detail": p.detail,
                }
                for p in self._pending.values()
            ]
        return {"debounce_minutes": self.cfg.debounce_minutes, "pending": pending}

    def _loop(self) -> None:
        debounce = self.cfg.debounce_minutes * 60
        while not self._stop.wait(TICK_SECONDS):
            try:
                for key in self._due(debounce):
                    self._process(key)
            except Exception:  # pragma: no cover - keep the loop alive
                log.exception("upload staging loop error")

    def _due(self, debounce: float) -> List[Tuple[str, ...]]:
        now = time.monotonic()
        ready = []
        with self._lock:
            for key, pending in list(self._pending.items()):
                path = self.path_for(key)
                if path is None or not path.exists():
                    # Uploaded and cleaned up, or removed behind our back.
                    if pending.state != "uploading":
                        self._pending.pop(key, None)
                    continue
                if pending.state not in ("staging", "failed"):
                    continue
                if pending.state == "failed" and now < pending.retry_after:
                    continue
                if now - pending.last_write >= debounce:
                    pending.state = "uploading"
                    ready.append(key)
        return ready

    def _process(self, key: Tuple[str, ...]) -> None:
        path = self.path_for(key)
        pending = self.get(key)
        if pending is None or path is None:
            return
        label = "/".join(key)
        try:
            log.info("uploading %s", label)
            mime_type = mimetypes.guess_type(pending.name)[0] or "application/octet-stream"
            upload_and_register(self.api, self.worker, path, pending.name, pending.parent_id, mime_type)
            log.info("uploaded %s — clearing staging", label)
            path.unlink(missing_ok=True)
            with self._lock:
                self._pending.pop(key, None)
        except Exception as exc:
            log.exception("upload of %s failed", label)
            with self._lock:
                unit = self._pending.get(key) or PendingUpload(segments=key, parent_id=pending.parent_id)
                unit.attempts += 1
                unit.detail = f"{type(exc).__name__}: {exc}"
                if unit.attempts >= MAX_ATTEMPTS:
                    unit.state = "abandoned"
                    log.error("giving up on %s after %s attempts — staging kept", label, unit.attempts)
                else:
                    unit.state = "failed"
                    unit.retry_after = time.monotonic() + RETRY_SECONDS
                self._pending[key] = unit
