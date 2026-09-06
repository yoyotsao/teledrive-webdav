"""「儲存在本地」 — copy something off the mount onto a real local disk.

Two roles in one file:

* ``LocalFetcher`` (server side) — resolves a Windows path handed over by the
  Explorer verb, then streams the bytes into ``local_dir``, emitting progress
  lines as it goes. A virtual zip folder is extracted entry by entry using range
  reads, so nothing is downloaded twice and no temporary archive is needed.
* ``python fetchlocal.py <path>`` (client side) — the trigger the registry verb
  runs. It POSTs to the bridge, renders the progress stream, and opens Explorer
  at the result. All logic stays on the server side.

Why an explicit action instead of auto-fetch on execute: Windows runs an .exe by
memory-mapping the image, and the loader will not wait for a download — it fails
or hangs instead. Copying first, running second, is the only reliable order.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, List, Optional

from config import ext_path

log = logging.getLogger("fetchlocal")

COPY_CHUNK = 4 * 1024 * 1024
PROGRESS_INTERVAL = 1.0


def human(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TiB"


@dataclass
class Item:
    """One file to copy: how to open it, how big it is, where it goes."""

    open: Callable[[], object]
    size: int
    dest: Path


class LocalFetcher:
    def __init__(self, cfg, resolver):
        self.cfg = cfg
        self.resolver = resolver

    # -- planning --------------------------------------------------------- #

    def _plan(self, loc, segments: List[str]) -> tuple:
        """Return ``(items, root_destination)`` for a resolved location."""
        import bridge  # local import: bridge imports this module

        root = self.cfg.local_dir
        items: List[Item] = []

        if loc.kind == bridge.ZIPFILE:
            node = loc.zip_node()
            dest = root / node.name
            items.append(Item(lambda n=node, v=loc.view: v.open(n), node.size, dest))
            return items, dest

        if loc.kind == bridge.ZIPDIR:
            # The zip's own root maps to the game name; a subfolder keeps its name.
            node = loc.zip_node()
            base_name = (node.name if node is not None else "") or loc.view.name or (segments[-1] if segments else "download")
            base = root / base_name
            for rel, member in loc.view.walk(node):
                if member.is_dir:
                    continue
                items.append(Item(lambda n=member, v=loc.view: v.open(n), member.size, base / rel))
            return items, base

        if loc.kind == bridge.FILE:
            size = self.resolver.api.total_size(loc.entry)
            dest = root / loc.entry.name
            items.append(Item(lambda e=loc.entry: self.resolver.open_remote(e), size, dest))
            return items, dest

        if loc.kind == bridge.FOLDER:
            base = root / loc.entry.name
            self._plan_folder(loc.entry.file_id, base, items)
            return items, base

        if loc.kind in (bridge.STAGE_FILE, bridge.STAGE_DIR):
            local = loc.local
            base = root / local.name
            if local.is_file():
                items.append(Item(lambda p=local: open(ext_path(p), "rb"), local.stat().st_size, base))
            else:
                for path in local.rglob("*"):
                    if path.is_file():
                        items.append(
                            Item(
                                lambda p=path: open(ext_path(p), "rb"),
                                path.stat().st_size,
                                base / path.relative_to(local),
                            )
                        )
            return items, base

        raise ValueError(f"nothing to fetch for {loc.kind}")

    def _plan_folder(self, parent_id: str, base: Path, items: List[Item]) -> None:
        for entry in self.resolver.api.list_dir(parent_id):
            if entry.is_dir:
                self._plan_folder(entry.file_id, base / entry.name, items)
            else:
                size = self.resolver.api.total_size(entry)
                items.append(Item(lambda e=entry: self.resolver.open_remote(e), size, base / entry.name))

    # -- execution -------------------------------------------------------- #

    def fetch(self, windows_path: str) -> Iterator[str]:
        """Copy ``windows_path`` into local_dir, yielding progress lines."""
        import bridge

        yield f"target: {windows_path}"
        segments = self.resolver.dav_path_from_windows(windows_path)
        if segments is None:
            yield f"ERROR only paths on {self.cfg.mount_drive} can be fetched"
            return
        if not segments:
            yield "ERROR refusing to fetch the whole drive — pick a folder or file"
            return

        try:
            loc = self.resolver.resolve(segments)
        except Exception as exc:
            yield f"ERROR resolve failed: {exc}"
            return
        if loc.kind == bridge.MISSING:
            yield f"ERROR not found: {'/'.join(segments)}"
            return

        try:
            items, root = self._plan(loc, segments)
        except Exception as exc:
            yield f"ERROR cannot plan the copy: {exc}"
            return

        total = sum(i.size for i in items)
        yield f"{len(items)} file(s), {human(total)} -> {root}"
        if not items:
            root.mkdir(parents=True, exist_ok=True)
            yield f"OK {root}"
            return

        done = 0
        last = 0.0
        started = time.monotonic()
        for index, item in enumerate(items, 1):
            item.dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = item.dest.with_name(item.dest.name + ".part")
            try:
                source = item.open()
                try:
                    with open(ext_path(tmp), "wb") as out:
                        while True:
                            chunk = source.read(COPY_CHUNK)
                            if not chunk:
                                break
                            out.write(chunk)
                            done += len(chunk)
                            now = time.monotonic()
                            if now - last >= PROGRESS_INTERVAL:
                                last = now
                                rate = done / max(0.001, now - started)
                                yield (
                                    f"PROGRESS {done} {total} {index}/{len(items)} "
                                    f"{human(rate)}/s {item.dest.name}"
                                )
                finally:
                    source.close()
                os.replace(ext_path(tmp), ext_path(item.dest))
            except Exception as exc:
                log.exception("fetch of %s failed", item.dest)
                try:
                    os.unlink(ext_path(tmp))
                except OSError:
                    pass
                yield f"ERROR {item.dest.name}: {exc}"
                return
        elapsed = time.monotonic() - started
        yield f"PROGRESS {total} {total} {len(items)}/{len(items)} done"
        yield f"done in {elapsed:.0f}s ({human(total / max(elapsed, 0.001))}/s)"
        yield f"OK {root}"


# --------------------------------------------------------------------------- #
# Client side — what the Explorer verb actually launches
# --------------------------------------------------------------------------- #


def _render(line: str) -> Optional[str]:
    """Print one server line; return the OK destination when it arrives."""
    if line.startswith("PROGRESS "):
        _, done, total, *rest = line.split(" ", 3)
        try:
            pct = int(done) / max(1, int(total)) * 100
        except ValueError:
            pct = 0.0
        detail = rest[0] if rest else ""
        width = 30
        filled = int(width * pct / 100)
        bar = "#" * filled + "-" * (width - filled)
        print(f"\r[{bar}] {pct:5.1f}%  {human(int(done))}  {detail[:60]:<60}", end="", flush=True)
        return None
    print("\r" + " " * 110 + "\r" + line, flush=True)
    return line[3:].strip() if line.startswith("OK ") else None


def main(argv=None) -> int:
    import argparse

    import requests

    from config import load_endpoint

    parser = argparse.ArgumentParser(description="Fetch a path off the TeleDrive mount to local disk")
    parser.add_argument("path", help=r"Windows path on the mount, e.g. E:\game\MyGame")
    parser.add_argument("--config", default=None)
    parser.add_argument("--no-explorer", action="store_true")
    args = parser.parse_args(argv)

    host, port, mount = load_endpoint(args.config)
    url = f"http://{host}:{port}/rpc/fetch-local"
    print(f"TeleDrive — 儲存在本地\n{args.path}\n")

    failed = False
    destination = None
    try:
        with requests.post(url, data={"path": args.path}, stream=True, timeout=(10, None)) as resp:
            if resp.status_code != 200:
                print(f"bridge returned HTTP {resp.status_code}: {resp.text[:200]}")
                failed = True
            else:
                for raw in resp.iter_lines(decode_unicode=True):
                    if raw is None:
                        continue
                    line = raw.strip()
                    if not line:
                        continue
                    if line.startswith("ERROR"):
                        failed = True
                    got = _render(line)
                    if got:
                        destination = got
    except Exception as exc:
        print(f"cannot reach the bridge at {url}: {exc}")
        print("Is bridge.py running? Start it with start.bat.")
        failed = True

    if destination and not args.no_explorer:
        target = Path(destination)
        try:
            if target.is_dir():
                os.startfile(target)  # noqa: S606 - opens a local folder for the user
            else:
                # Argument list, not a shell string: the destination contains
                # cloud/zip-provided file names, which must never be parsed by cmd.
                subprocess.run(["explorer", f"/select,{target}"], check=False)
        except OSError:
            pass

    if failed:
        print()
        try:
            input("按 Enter 關閉…")
        except EOFError:
            pass
        return 1
    time.sleep(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
