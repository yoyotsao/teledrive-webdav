"""整包下載遊戲封存：一個 zip 當單一檔案循序讀，再於本機解壓。

為什麼不逐成員：一個 497 檔的遊戲逐成員下載要 829 秒（63.7 KiB/s），因為每個成員
開新 reader、冷快取，一個 100 KB 的檔也付一次 512 KiB 的 Telegram 往返。整包循序讀
只付一條串流，成員切換的成本消失。只用於封存根目錄；子資料夾與單一成員仍走逐成員。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, Iterator, Optional

from teledrive_webdav.core import fileid
from teledrive_webdav.core.config import ext_path
from teledrive_webdav.upload.upload_engine import redact

log = logging.getLogger("fetchlocal")

COPY_CHUNK = 4 * 1024 * 1024
PROGRESS_INTERVAL = 1.0
SPACE_MARGIN = 1.05

_DRIVE = re.compile(r"^[A-Za-z]:")


def _human(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TiB"


def _unsafe_name(name: str) -> bool:
    """True for a member name that could land outside the extraction root."""
    norm = name.replace("\\", "/")
    if norm.startswith("/") or _DRIVE.match(norm):
        return True
    return any(part == ".." or ":" in part for part in norm.split("/"))


def _meta_matches(meta: Path, entry, total: int) -> bool:
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return data.get("file_id") == str(entry.file_id) and data.get("total_size") == total


def _unlink(path: Path) -> None:
    try:
        os.unlink(ext_path(path))
    except FileNotFoundError:
        pass


_FORBIDDEN = re.compile(r'[<>"|?*\x00-\x1f]')


def _safe_rel(rel: str) -> str:
    """Windows cannot hold ``<>"|?*`` or a trailing dot/space in a name.

    Archives written in another codepage show up as mojibake that contains such
    characters (``|`` is common), so the name is mapped instead of failing the
    whole game. Names that are already valid are untouched.
    """
    out = []
    for seg in rel.split("/"):
        seg = _FORBIDDEN.sub("_", seg)
        if seg.endswith(".") or seg.endswith(" "):
            seg = seg.rstrip(". ") + "_"
        out.append(seg)
    return "/".join(out)


def _extract_one(zf: zipfile.ZipFile, node, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        with zf.open(node.zip_name) as src, open(ext_path(tmp), "wb") as out:
            shutil.copyfileobj(src, out, COPY_CHUNK)
        os.replace(ext_path(tmp), ext_path(dest))
    except BaseException:
        try:
            _unlink(tmp)
        except OSError:
            pass  # never let cleanup hide the error that got us here
        raise


def _log_tag(resolver, entry) -> str:
    """Log name of a zip under the game folder: its drive path, as an id."""
    folder = getattr(getattr(resolver, "cfg", None), "game_folder", "game")
    return fileid.game_tag(entry.name, folder)


def fetch_archive(
    *,
    resolver,
    entry,
    view,
    base: Path,
    local_dir: Path,
    skip_existing: bool = False,
    cancel: Optional[threading.Event] = None,
    free_bytes: Optional[Callable[[Path], int]] = None,
) -> Iterator[str]:
    """Download ``entry`` (a zip) whole, then extract it to ``base``.

    Yields the same line protocol as ``LocalFetcher.fetch_segments`` and ends with
    ``OK <base>``, ``CANCELLED …`` or ``ERROR …``. The downloaded zip survives any
    failure after the download, and a cancelled/interrupted download keeps its
    ``.part`` plus a meta file so the next run resumes instead of starting over.
    """
    free_bytes = free_bytes or (lambda p: shutil.disk_usage(p).free)
    started = time.monotonic()
    cancelled = lambda: cancel is not None and cancel.is_set()  # noqa: E731

    members = [(rel, node) for rel, node in view.walk() if not node.is_dir]
    unpack = sum(node.size for _rel, node in members)
    total = resolver.api.total_size(entry)

    local_dir.mkdir(parents=True, exist_ok=True)
    zip_path = local_dir / entry.name
    part = local_dir / (entry.name + ".part")
    meta = local_dir / (entry.name + ".part.meta")

    # ---- what is already on disk ------------------------------------------ #
    complete = False
    have = 0
    if _meta_matches(meta, entry, total):
        try:
            if zip_path.is_file() and zip_path.stat().st_size == total:
                complete = True
            elif part.is_file() and part.stat().st_size <= total:
                have = part.stat().st_size
        except OSError:
            pass
    if not complete and have == 0:
        _unlink(part)
        _unlink(zip_path)
    if not complete and have > 0:
        _unlink(zip_path)

    # ---- space ------------------------------------------------------------ #
    need = (0 if complete else total - have) + int(unpack * SPACE_MARGIN)
    try:
        free = free_bytes(local_dir)
    except OSError as exc:
        yield f"ERROR cannot check free disk space: {exc}"
        return
    if free < need:
        yield f"ERROR not enough disk space: need {_human(need)}, free {_human(free)}"
        return

    # ---- download --------------------------------------------------------- #
    download_ms = 0
    downloaded = 0
    if not complete:
        t0 = time.monotonic()
        meta.write_text(json.dumps({"file_id": str(entry.file_id), "total_size": total}), encoding="utf-8")
        last = 0.0
        try:
            source = resolver.open_remote(entry)
            try:
                with open(ext_path(part), "ab" if have else "wb") as out:
                    # Sliding window over the whole archive: one request completes,
                    # the next goes out, instead of 8 MiB batches that each wait
                    # for their slowest chunk.
                    for chunk in source.iter_stream(have):
                        out.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if now - last >= PROGRESS_INTERVAL:
                            last = now
                            rate = downloaded / max(0.001, now - t0)
                            yield f"PROGRESS {have + downloaded} {total} download {_human(rate)}/s {entry.name}"
                        if cancelled():
                            yield "CANCELLED download cancelled"
                            return
            finally:
                source.close()
        except Exception as exc:
            log.exception("download of %s failed", _log_tag(resolver, entry))
            yield f"ERROR {entry.name}: {exc}"
            return
        if have + downloaded != total:
            yield f"ERROR {entry.name}: expected {total} bytes, received {have + downloaded}"
            return
        if cancelled():
            yield "CANCELLED download cancelled"
            return
        os.replace(ext_path(part), ext_path(zip_path))
        download_ms = int((time.monotonic() - t0) * 1000)
    yield f"PROGRESS {total} {total} download done {entry.name}"

    # ---- extract ---------------------------------------------------------- #
    t1 = time.monotonic()
    try:
        with zipfile.ZipFile(ext_path(zip_path)) as zf:
            for info in zf.infolist():
                if _unsafe_name(info.filename):
                    yield f"ERROR unsafe path in archive: {info.filename!r}"
                    return
            root = base.resolve()
            targets = []
            for rel, node in members:
                dest = base / _safe_rel(rel)
                if root not in dest.resolve().parents:
                    yield f"ERROR unsafe path in archive: {rel!r}"
                    return
                targets.append((rel, node, dest))

            last = 0.0
            for index, (rel, node, dest) in enumerate(targets, 1):
                if cancelled():
                    yield "CANCELLED download cancelled"
                    return
                if skip_existing:
                    try:
                        if dest.is_file() and dest.stat().st_size == node.size:
                            continue
                    except OSError:
                        pass
                _extract_one(zf, node, dest)
                now = time.monotonic()
                if now - last >= PROGRESS_INTERVAL:
                    last = now
                    yield f"PROGRESS {total} {total} extract {index}/{len(targets)} {dest.name}"
            if not targets:
                base.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        log.exception("extract of %s failed", _log_tag(resolver, entry))
        yield f"ERROR {entry.name}: {exc}"
        return
    extract_ms = int((time.monotonic() - t1) * 1000)

    _unlink(zip_path)
    _unlink(meta)
    total_ms = int((time.monotonic() - started) * 1000)
    rate = downloaded / 1048576 / (download_ms / 1000) if download_ms else 0.0
    log.info(
        "%s",
        redact(
            f"download complete files={len(members)} bytes={unpack} mode=zip download_ms={download_ms} "
            f"extract_ms={extract_ms} total_ms={total_ms} MiB/s={rate:.2f}"
        ),
    )
    yield f"PROGRESS {total} {total} extract {len(members)}/{len(members)} done"
    yield f"done in {total_ms / 1000:.0f}s ({_human(unpack / max(total_ms / 1000, 0.001))}/s)"
    yield f"OK {base}"
