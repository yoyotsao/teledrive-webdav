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

The transfer itself belongs to ``UploadEngine``: dedup, protocol choice,
albums, account routing and registration are shared with /game. This module
owns the staging half — landing bytes, debouncing, dispatching a whole due
batch at once, and the durable record of where each file got to.

**The staged source is the only copy.** It is deleted after registration has
settled and never before, so a crash anywhere in between leaves a file that
``_adopt_leftovers`` picks up again on the next start. The queue state beside
it (``meta/upload-queue.json``) only remembers *how many times* a source has
already failed and what went wrong; the sources on disk, not that file, are
what the queue actually is.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from config import ext_path as _ext
from gamestage import MAX_ATTEMPTS, RETRY_SECONDS, TICK_SECONDS
from transfer_models import QueueStage, TransferRequest
from upload_engine import guess_mime_type, redact

log = logging.getLogger("uploadstage")

STATE_FILE = "upload-queue.json"
STATE_VERSION = 1

#: Stages a file can sit in between debounce windows. Anything else is a
#: transient in-flight stage that a restart re-derives from the source itself.
_RESTING = (QueueStage.STAGING, QueueStage.FAILED, QueueStage.ABANDONED)


@dataclass
class PendingUpload:
    """One file waiting to go up: identified by its full destination path."""

    segments: Tuple[str, ...]
    parent_id: Optional[str]
    last_write: float = field(default_factory=time.monotonic)
    stage: QueueStage = QueueStage.STAGING
    attempts: int = 0
    retry_after: float = 0.0
    detail: str = ""
    accounts: Tuple[int, ...] = ()

    @property
    def name(self) -> str:
        return self.segments[-1]

    @property
    def path(self) -> str:
        return "/".join(self.segments)


class UploadStager:
    def __init__(self, cfg, api, engine):
        self.cfg = cfg
        self.api = api
        self.engine = engine
        self._pending: Dict[Tuple[str, ...], PendingUpload] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state_lock = threading.Lock()
        self.cfg.upload_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
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
        self._save()

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
            if pending.stage in (QueueStage.FAILED,):
                pending.stage = QueueStage.STAGING
                pending.attempts = 0
                pending.detail = ""
        self._save()

    # -- durable queue state ---------------------------------------------- #

    @property
    def _state_path(self) -> Path:
        return self.cfg.cache_dir / STATE_FILE

    def _save(self) -> None:
        """Rewrite the queue record atomically.

        A ``.part`` in the same directory plus ``os.replace``: a half-written
        record read back after a crash would either lose the attempt count of
        every file or, worse, resurrect one already registered.
        """
        with self._lock:
            body = {
                "version": STATE_VERSION,
                "pending": {
                    unit.path: {
                        "stage": unit.stage.value,
                        "attempts": unit.attempts,
                        "detail": unit.detail,
                        "parent_id": unit.parent_id,
                        "accounts": list(unit.accounts),
                    }
                    for unit in self._pending.values()
                },
            }
        target = self._state_path
        temp = target.with_suffix(".part")
        with self._state_lock:
            try:
                temp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
                os.replace(temp, target)
            except OSError:  # pragma: no cover - state is an optimisation, not the queue
                log.warning("could not persist the upload queue state", exc_info=True)

    def _load_state(self) -> dict:
        try:
            body = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(body, dict) or body.get("version") != STATE_VERSION:
            return {}
        pending = body.get("pending")
        return pending if isinstance(pending, dict) else {}

    def _set_stage(self, key, stage: QueueStage, detail: str = "") -> None:
        with self._lock:
            unit = self._pending.get(key)
            if unit is None:
                return
            unit.stage = stage
            if detail:
                unit.detail = detail
        self._save()

    def status_for(self, segments: Sequence[str]) -> Optional[dict]:
        unit = self.get(segments)
        if unit is None:
            return None
        return {
            "path": unit.path,
            "stage": unit.stage.value,
            "attempts": unit.attempts,
            "detail": unit.detail,
            "accounts": list(unit.accounts),
        }

    def _adopt_leftovers(self) -> None:
        """Pick up staged files left behind by a crash.

        The parent folder is re-resolved from the backend rather than
        remembered — folders outside /game are real and outlive a crash —
        so a leftover is never orphaned without a destination to register to.
        The persisted record supplies only the attempt count and the last
        error, so a file that has already burnt four attempts is not handed a
        fresh five by a restart.
        """
        now = time.time()
        remembered = self._load_state()
        # Walked through the extended-length form so a deep staged tree is
        # still readable, but the destination segments come from the walk's own
        # relative root: ``Path(r"\\?\D:\...")`` is never ``relative_to`` the
        # plain ``upload_dir``, so deriving them that way silently adopted
        # nothing on Windows and every crashed upload was dropped from the
        # queue while its bytes stayed on disk forever.
        base = _ext(self.cfg.upload_dir)
        for root, _dirs, files in os.walk(base):
            relative = os.path.relpath(root, base)
            prefix = () if relative == "." else tuple(Path(relative).parts)
            for fn in files:
                full = Path(root) / fn
                segments = prefix + (fn,)
                try:
                    idle = max(0.0, now - full.stat().st_mtime)
                except OSError:
                    idle = 0.0
                parent_id = None
                if len(segments) > 1:
                    parent = self.api.resolve(list(segments[:-1]))
                    parent_id = parent.file_id if parent is not None else None
                unit = PendingUpload(
                    segments=segments, parent_id=parent_id, last_write=time.monotonic() - idle,
                )
                record = remembered.get("/".join(segments))
                if isinstance(record, dict):
                    unit.attempts = int(record.get("attempts") or 0)
                    unit.detail = str(record.get("detail") or "")
                    unit.accounts = tuple(int(a) for a in record.get("accounts") or ())
                    if parent_id is None and record.get("parent_id"):
                        unit.parent_id = record["parent_id"]
                    try:
                        stage = QueueStage(record.get("stage"))
                    except ValueError:
                        stage = QueueStage.STAGING
                    # An in-flight stage did not survive the process that owned
                    # it: the source is still here, so it is staged again.
                    unit.stage = stage if stage in _RESTING else QueueStage.STAGING
                self._pending[segments] = unit
                log.info("adopted leftover upload %s (idle %.0fs, %s)",
                         unit.path, idle, unit.stage.value)
        self._save()

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
                    "path": p.path,
                    "stage": p.stage.value,
                    "idle_seconds": round(now - p.last_write, 1),
                    "attempts": p.attempts,
                    "accounts": list(p.accounts),
                    "detail": p.detail,
                }
                for p in self._pending.values()
            ]
        return {"debounce_minutes": self.cfg.debounce_minutes, "pending": pending}

    def _loop(self) -> None:
        debounce = self.cfg.debounce_minutes * 60
        while not self._stop.wait(TICK_SECONDS):
            try:
                due = self._due(debounce)
                if due:
                    self.process_due(due)
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
                    if pending.stage is not QueueStage.UPLOADING:
                        self._pending.pop(key, None)
                    continue
                if pending.stage not in (QueueStage.STAGING, QueueStage.FAILED):
                    continue
                if pending.stage is QueueStage.FAILED and now < pending.retry_after:
                    continue
                if now - pending.last_write >= debounce:
                    pending.stage = QueueStage.UPLOADING
                    ready.append(key)
        return ready

    # -- one due batch ---------------------------------------------------- #

    def process_due(self, keys: Sequence[Sequence[str]]) -> None:
        """Transfer every due file in one streaming batch, then settle each.

        One batch rather than a loop of single files: the engine's fingerprint
        claims only collapse duplicates that are in flight together, and its
        album queue only fills from a batch. Registration runs on its own pool
        so a slow backend never holds up the next file's bytes.
        """
        keys = [tuple(key) for key in keys]
        requests: Dict[str, Tuple[str, ...]] = {}
        failures: Dict[Tuple[str, ...], str] = {}
        registrations: Dict[Tuple[str, ...], object] = {}
        dispatched: List[Tuple[str, ...]] = []
        lock = threading.Lock()

        def key_of(request) -> Optional[Tuple[str, ...]]:
            return requests.get(str(request.source))

        def generate():
            for key in keys:
                request = self._request_for(key)
                if request is None:
                    continue
                requests[str(request.source)] = key
                dispatched.append(key)
                log.info("uploading %s", "/".join(key))
                yield request

        def status_sink(request, stage, detail=""):
            key = key_of(request)
            if key is None:  # pragma: no cover - every request came from generate()
                return
            if stage is QueueStage.FAILED:
                with lock:
                    failures.setdefault(key, detail)
                return
            self._set_stage(key, stage)

        pool = ThreadPoolExecutor(
            max_workers=max(1, int(getattr(self.cfg, "register_concurrency", 8))),
            thread_name_prefix="uploadstage-register",
        )

        def on_result(result):
            key = key_of(result.request)
            if key is None:  # pragma: no cover - every request came from generate()
                return
            with self._lock:
                unit = self._pending.get(key)
                if unit is not None:
                    unit.accounts = tuple(sorted({p.telegram_user_id for p in result.parts}))
            self._set_stage(key, QueueStage.REGISTERING)
            with lock:
                registrations[key] = pool.submit(self.engine.register_result, result)

        try:
            self.engine.transfer_batch(
                generate(), status_sink, on_result=on_result,
                lookahead=max(1, int(getattr(self.cfg, "hash_concurrency", 2))),
            )
        except Exception as exc:
            # Per-request failures already arrived through status_sink; this is
            # only the batch's first one surfacing again.
            log.warning("upload batch reported %s", redact(exc))
        finally:
            pool.shutdown(wait=True)

        for key, future in registrations.items():
            try:
                future.result()
            except Exception as exc:
                log.exception("registering %s failed", "/".join(key))
                with lock:
                    failures.setdefault(key, redact(exc))

        for key in dispatched:
            detail = failures.get(key)
            if detail is None:
                self._complete(key)
            else:
                self._record_failure(key, detail)

    def _request_for(self, key: Tuple[str, ...]) -> Optional[TransferRequest]:
        path = self.path_for(key)
        unit = self.get(key)
        if unit is None or path is None or not path.exists():
            return None
        try:
            size = path.stat().st_size
        except OSError:
            return None
        mime_type = guess_mime_type(unit.name)
        return TransferRequest(
            source=path, upload_name=unit.name, mime_type=mime_type,
            parent_id=unit.parent_id, logical_size=size,
        )

    def _complete(self, key: Tuple[str, ...]) -> None:
        """Registration settled: only now may the one local copy go away."""
        path = self.path_for(key)
        if path is not None:
            path.unlink(missing_ok=True)
        with self._lock:
            self._pending.pop(key, None)
        log.info("uploaded %s — clearing staging", "/".join(key))
        self._save()

    def _record_failure(self, key: Tuple[str, ...], detail: str) -> None:
        label = "/".join(key)
        with self._lock:
            unit = self._pending.get(key)
            if unit is None:
                return
            unit.attempts += 1
            unit.detail = detail
            if unit.attempts >= MAX_ATTEMPTS:
                unit.stage = QueueStage.ABANDONED
                log.error("giving up on %s after %s attempts — staging kept", label, unit.attempts)
            else:
                unit.stage = QueueStage.FAILED
                unit.retry_after = time.monotonic() + RETRY_SECONDS
        self._save()
