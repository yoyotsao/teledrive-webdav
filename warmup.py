r"""Fetch every preview and dimension once, so no folder is ever cold.

Browsing speed has two regimes. Warm, the shell renders about 20 files a second
— the handlers answer in 8ms and 40ms and everything comes off local disk. Cold,
it is whatever Telegram will give: previews arrive in batches at between 8 and 33
a second depending on how hard the account has been pulled recently, and that
rate is not something the code can raise.

So the way to a reliably fast folder is to stop having cold ones. This walks the
tree once and fills the preview and property caches. It is resumable — anything
already cached is skipped — so it can be run repeatedly and after new uploads.

    .venv\Scripts\python.exe warmup.py            # everything
    .venv\Scripts\python.exe warmup.py pixiv      # one subtree
"""

from __future__ import annotations

import sys
import time
from typing import List

from config import load_config
from tdapi import Entry, JsonStore, TeleDriveClient
from tgio import TelegramWorker

BATCH = 100  # one get_messages covers this many


def walk(api: TeleDriveClient, parent_id, path: str, out: List[tuple]) -> None:
    for entry in api.list_dir(parent_id):
        if entry.is_dir:
            walk(api, entry.file_id, f"{path}/{entry.name}", out)
        elif entry.message_id is not None:
            out.append((f"{path}/{entry.name}", entry))


def main(argv: List[str]) -> int:
    cfg = load_config()
    api = TeleDriveClient(cfg)
    api.login()

    start_id = None
    label = "/"
    if argv:
        entry = api.resolve([s for s in argv[0].replace("\\", "/").split("/") if s])
        if entry is None or not entry.is_dir:
            print(f"[error] no such folder: {argv[0]}")
            return 1
        start_id, label = entry.file_id, argv[0]

    print(f"walking {label} ...", flush=True)
    files: List[tuple] = []
    walk(api, start_id, "", files)
    print(f"{len(files)} files", flush=True)

    thumbs = cfg.cache_dir / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    props = JsonStore(cfg.cache_dir / "media_props.json")

    # Dimensions matter as much as previews. Explorer asks for both per file, and
    # a cold property read costs about 140ms — enough on its own to hold the
    # shell under ten files a second. They come off the same documents the
    # preview batch already fetches, so warming them is nearly free.
    todo = [
        e for _, e in files
        if (e.has_thumbnail and not (thumbs / f"{e.file_id}.jpg").exists())
        or props.get(e.file_id) is None
    ]
    print(f"{len(files) - len(todo)} already cached, {len(todo)} to fetch", flush=True)
    if not todo:
        print("nothing to do")
        return 0

    worker = TelegramWorker(cfg.api_id, cfg.api_hash, cfg.session, cfg.download_connections)
    worker.start()

    done = 0
    started = time.monotonic()
    try:
        for at in range(0, len(todo), BATCH):
            chunk = todo[at : at + BATCH]
            ids = [e.message_id for e in chunk]
            fetched = worker.thumbnails(ids)
            media = worker.media_info(ids)
            for entry in chunk:
                info = media.get(entry.message_id)
                if info is not None:
                    props.put(entry.file_id, info, defer=True)
                data = fetched.get(entry.message_id)
                if not data:
                    continue
                path = thumbs / f"{entry.file_id}.jpg"
                tmp = path.with_suffix(".jpg.part")
                tmp.write_bytes(data)
                tmp.replace(path)
                done += 1
            props.flush()
            elapsed = time.monotonic() - started
            rate = done / elapsed if elapsed else 0
            left = (len(todo) - done) / rate if rate else 0
            print(f"  {done}/{len(todo)}  {rate:.1f}/s  ~{left / 60:.1f} min left", flush=True)
    except KeyboardInterrupt:
        print("\ninterrupted — rerun to continue where this stopped")
    finally:
        worker.stop()

    elapsed = time.monotonic() - started
    print(f"cached {done} previews in {elapsed / 60:.1f} min ({done / elapsed:.1f}/s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
