"""Convert RARs already uploaded to /game into stored zips, in the background.

gamestage converts a ``.rar`` at the moment it is *dropped into* /game. The
ones already on the drive -- uploaded before that existed, or from the web --
are found here, once per warm-up pass:

1. download the rar into ``staging/.convert/<file_id>.part`` (resumable, and
   yielding to foreground requests between chunks, like the rest of the sweep)
2. move it into staging under its own name, where gamestage extracts it with
   7-Zip and packs ``<stem>.zip`` exactly as it would a dropped folder
3. once ``<stem>.zip`` is on the drive *and* its directory reads back with at
   least one member, trash the original (a soft delete: it stays restorable
   from the web's trash, and its Telegram messages are untouched)

Only rars this module staged are ever trashed; they are recorded in
``meta/rar-convert.json`` by file id. A rar that already has a ``.zip``
sibling nobody here made is left alone.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Callable, Dict, List

from config import ext_path as _ext

log = logging.getLogger("rarconvert")

CHUNK = 8 * 1024 * 1024
PROGRESS_EVERY = 256 * 1024 * 1024


def _is_rar(name: str) -> bool:
    return name.lower().endswith(".rar")


def _zip_name(rar_name: str) -> str:
    return rar_name[: -len(".rar")] + ".zip"


class RarConverter:
    def __init__(
        self,
        cfg,
        api,
        stager,
        *,
        open_reader: Callable,
        zip_has_files: Callable,
        wait_quiet: Callable[[], None],
        chunk: int = CHUNK,
    ):
        self.cfg = cfg
        self.api = api
        self.stager = stager
        self._open_reader = open_reader
        self._zip_has_files = zip_has_files
        self._wait_quiet = wait_quiet
        self._chunk = chunk
        self._stop = threading.Event()
        self._state_path = Path(cfg.cache_dir) / "rar-convert.json"
        self._work_dir = Path(cfg.staging_dir) / ".convert"

    def stop(self) -> None:
        self._stop.set()

    # -- one pass --------------------------------------------------------- #

    def run(self) -> None:
        game = self.api.resolve([self.cfg.game_folder])
        if game is None:
            return
        # Fresh: the whole point of a pass is to notice what the web uploaded.
        children = self.api.children_by_name(game.file_id, fresh=True)
        state = self._load()
        self._cleanup(game.file_id, children, state)
        for entry in self._pending(children, state):
            if self._stop.is_set():
                return
            try:
                if self._download(entry):
                    state[str(entry.file_id)] = {"name": entry.name}
                    self._save(state)
                    self.stager.touch(entry.name)
                    log.info("rar convert: %s staged for conversion to %s",
                             entry.name, _zip_name(entry.name))
            except Exception as exc:  # one bad archive must not stop the rest
                log.warning("rar convert: download of %s failed: %s", entry.name, exc)

    def _pending(self, children: Dict, state: Dict) -> List:
        out = []
        for name, entry in sorted(children.items()):
            if entry.is_dir or not _is_rar(name) or str(entry.file_id) in state:
                continue
            if _zip_name(name) in children:
                continue  # converted before, or someone else's zip: not ours to touch
            if (Path(self.cfg.staging_dir) / name).exists():
                continue  # already in gamestage's hands
            out.append(entry)
        return out

    def _cleanup(self, game_id: str, children: Dict, state: Dict) -> None:
        by_id = {str(e.file_id): e for e in children.values()}
        changed = False
        for file_id, record in list(state.items()):
            name = record["name"]
            if file_id not in by_id:
                log.info("rar convert: %s is gone from /game; forgetting it", name)
                state.pop(file_id)
                changed = True
                continue
            if (Path(self.cfg.staging_dir) / name).exists():
                continue  # still being converted
            zip_entry = children.get(_zip_name(name))
            if zip_entry is None:
                continue  # not uploaded yet, or the unit failed and kept the rar
            try:
                readable = self._zip_has_files(zip_entry)
            except Exception as exc:
                log.warning("rar convert: cannot read %s yet: %s", zip_entry.name, exc)
                readable = False
            if not readable:
                continue
            self.api.trash(file_id, game_id)
            log.info("rar convert: %s replaced by %s; original moved to trash",
                     name, zip_entry.name)
            state.pop(file_id)
            changed = True
        if changed:
            self._save(state)

    # -- download --------------------------------------------------------- #

    def _download(self, entry) -> bool:
        """Fetch ``entry`` into staging. False if stopped or incomplete."""
        size = self.api.total_size(entry)
        self._work_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(entry.file_id))
        part = self._work_dir / f"{safe}.part"
        done = part.stat().st_size if part.exists() else 0
        if done > size:
            part.unlink()
            done = 0
        if done:
            log.info("rar convert: resuming %s at %.1f of %.1f GiB",
                     entry.name, done / 2**30, size / 2**30)
        else:
            log.info("rar convert: downloading %s (%.1f GiB)", entry.name, size / 2**30)
        reader = self._open_reader(entry)
        try:
            reader.seek(done)
            next_note = done + PROGRESS_EVERY
            with open(_ext(part), "ab") as out:
                while done < size:
                    if self._stop.is_set():
                        return False
                    self._wait_quiet()
                    if self._stop.is_set():
                        return False
                    data = reader.read(min(self._chunk, size - done))
                    if not data:
                        break
                    out.write(data)
                    done += len(data)
                    if done >= next_note:
                        log.info("rar convert: %s %.1f/%.1f GiB",
                                 entry.name, done / 2**30, size / 2**30)
                        next_note += PROGRESS_EVERY
        finally:
            close = getattr(reader, "close", None)
            if close:
                close()
        if done != size:
            log.warning("rar convert: %s ended at %s of %s bytes; will retry next pass",
                        entry.name, done, size)
            return False
        os.replace(_ext(part), _ext(Path(self.cfg.staging_dir) / entry.name))
        return True

    # -- state ------------------------------------------------------------ #

    def _load(self) -> Dict:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, state: Dict) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(dir=self._state_path.parent, suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        os.replace(name, self._state_path)
