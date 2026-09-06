"""Opt-in live parity probe: does a real WebDAV upload look like a web upload?

**This writes real data.** It uploads to the configured Telegram account and
registers rows in the real TeleDrive backend, and Telegram messages cannot be
deleted afterwards. It therefore refuses to run without an explicit folder and
an explicit byte budget, and it never deletes anything: whether the probe files
stay is a decision for whoever authorised the run, made after they have looked.

Everything the offline suite covers is faked at exactly the two places that
matter most: MTProto and the backend. This script is the only thing that can
answer whether the boundaries are right against the real ones -- the 10 MiB
small/big switch, the 500 MiB message boundary, album batching, and the
deduplication fingerprint agreeing with the browser's.

Route under test, end to end::

    write  ->  H:  ->  rclone/WinFsp  ->  WebDAV PUT  ->  uploads/  ->  engine
    read   <-  bridge HTTP (not H:, so rclone's VFS cache cannot answer)

Reading back through the bridge's own HTTP port rather than through ``H:`` is
deliberate: rclone caches what it wrote, so a read off the mount would prove
only that the local cache still has the bytes.

Usage (PowerShell)::

    .venv\\Scripts\\python.exe scripts/live_transfer_parity.py `
        --folder _parity-probe --max-bytes 1200000000

The report is written next to the log directory and contains only names,
sizes, hashes, message/file IDs and protocol classifications -- never bytes,
never a session, never a token.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config  # noqa: E402
from tdapi import TeleDriveClient  # noqa: E402

MiB = 1024 * 1024


@dataclass(frozen=True)
class Case:
    name: str
    count: int
    sizes: tuple[int, ...]
    suffix: str = ".bin"
    identical: bool = False
    #: What the engine should choose, per tgupload.decide_protocol.
    expect: str = "big"
    expect_thumbnail: bool = False


CASES = (
    # Eleven, not ten: the eleventh is what proves a tail flushes rather than
    # sitting in the queue until something else happens to arrive.
    Case("jpeg-album-11", count=11, sizes=(1 * MiB,), suffix=".jpg",
         expect="album", expect_thumbnail=True),
    # webp is media but deliberately excluded from albums: the web client
    # recorded MEDIA_EMPTY and lost thumbnails for every webp sent that way.
    Case("webp-small", count=1, sizes=(1 * MiB,), suffix=".webp",
         expect="small", expect_thumbnail=True),
    Case("small-boundaries", count=2, sizes=(10 * MiB, 10 * MiB + 1),
         expect="boundary"),
    Case("segment-boundaries", count=2, sizes=(500 * MiB, 500 * MiB + 1),
         expect="boundary"),
    Case("duplicate-pair", count=2, sizes=(1 * MiB,), identical=True,
         expect="small"),
)


def payload(size: int, seed: int) -> bytes:
    """Deterministic, incompressible-ish bytes, so a rerun hashes the same."""
    out = bytearray()
    block = 0
    while len(out) < size:
        out += hashlib.sha256(f"{seed}:{block}".encode()).digest()
        block += 1
    return bytes(out[:size])


def jpeg_payload(size: int, seed: int) -> bytes:
    """A real decodable image, padded to ``size`` with a trailing comment."""
    from io import BytesIO

    from PIL import Image

    rng = payload(64 * 1024, seed)
    image = Image.frombytes("RGB", (128, 128), rng[: 128 * 128 * 3].ljust(128 * 128 * 3, b"\0"))
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=95)
    body = buffer.getvalue()
    if len(body) >= size:
        return body
    # Padding after EOI: still a valid JPEG to every decoder that matters, and
    # it keeps the file size exactly on the boundary being tested.
    return body + payload(size - len(body), seed + 1)


def webp_payload(size: int, seed: int) -> bytes:
    from io import BytesIO

    from PIL import Image

    rng = payload(64 * 1024, seed)
    image = Image.frombytes("RGB", (128, 128), rng[: 128 * 128 * 3].ljust(128 * 128 * 3, b"\0"))
    buffer = BytesIO()
    image.save(buffer, "WEBP", quality=95)
    body = buffer.getvalue()
    return body if len(body) >= size else body + payload(size - len(body), seed + 1)


@dataclass
class Planned:
    case: str
    name: str
    size: int
    sha256: str
    expect: str
    expect_thumbnail: bool
    local: Path
    findings: list[str] = field(default_factory=list)
    observed: dict = field(default_factory=dict)


def plan(cases, mount: Path, folder: str) -> list[Planned]:
    planned: list[Planned] = []
    seed = 0
    for case in cases:
        for index in range(case.count):
            size = case.sizes[index % len(case.sizes)]
            seed = seed if case.identical else seed + 1
            if case.suffix == ".jpg":
                body = jpeg_payload(size, seed)
            elif case.suffix == ".webp":
                body = webp_payload(size, seed)
            else:
                body = payload(size, seed)
            name = f"{case.name}-{index}{case.suffix}"
            planned.append(Planned(
                case=case.name, name=name, size=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
                expect=case.expect, expect_thumbnail=case.expect_thumbnail,
                local=mount / folder / name,
                observed={"bytes": len(body)},
            ))
            planned[-1].__dict__["_body"] = body
    return planned


def write_all(planned: list[Planned], mount: Path, folder: str) -> None:
    target = mount / folder
    target.mkdir(parents=True, exist_ok=True)
    for item in planned:
        body = item.__dict__.pop("_body")
        print(f"  writing {item.name} ({item.size / MiB:.1f} MiB)", flush=True)
        item.local.write_bytes(body)


def wait_for_quiet(rpc: str, expected: set[str], deadline: float) -> bool:
    """Wait for every written file to reach the stager, then for it to drain.

    Both halves are needed. An empty queue does not mean "done" until the
    files have actually arrived: a write to ``H:`` lands in rclone's cache
    first and reaches the bridge on its writeback, so polling straight after
    the last write sees a queue that is empty because it has not started.
    """
    seen: set[str] = set()
    last = None
    while time.monotonic() < deadline:
        try:
            status = requests.get(f"{rpc}/rpc/status", timeout=30).json()
        except Exception as exc:  # the bridge may be busy; keep waiting
            print(f"  [status unavailable: {type(exc).__name__}]", flush=True)
            time.sleep(30)
            continue
        pending = status.get("uploads", {}).get("pending", [])
        seen |= {p["path"].rsplit("/", 1)[-1] for p in pending}
        missing = expected - seen
        if not pending and not missing:
            return True
        summary = (f"{len(pending)} in queue, {len(missing)} not arrived yet: "
                   + ", ".join(f"{p['path'].rsplit('/', 1)[-1]}={p['stage']}"
                               for p in pending[:4]))
        if summary != last:
            print(f"  {summary}", flush=True)
            last = summary
        stuck = [p for p in pending if p["stage"] in ("failed", "abandoned")]
        if stuck and len(stuck) == len(pending) and not missing:
            for p in stuck:
                print(f"  [{p['stage']}] {p['path']}: {p['detail']}", flush=True)
            return False
        time.sleep(20)
    return False


def entries_for(api: TeleDriveClient, folder: str) -> dict:
    """Every registered file under the probe folder, by name.

    Through the ordinary listing rather than raw rows: that is the same path
    the mount uses, so a row this cannot see is a row the drive cannot serve.
    """
    entry = api.resolve([folder])
    if entry is None:
        raise SystemExit(f"the probe folder /{folder} does not exist on the backend")
    return {
        child.name: child
        for child in api.list_dir(entry.file_id, fresh=True)
        if not child.is_dir
    }


def verify(item: Planned, entry, api: TeleDriveClient, rpc: str, folder: str) -> None:
    from tgupload import MESSAGE_MAX, SMALL_FILE_MAX

    if entry is None:
        item.findings.append("no backend row was registered")
        return

    try:
        parts = api.parts_for(entry)
    except Exception as exc:
        item.findings.append(f"parts_for failed: {type(exc).__name__}: {exc}")
        return

    item.observed["rows"] = len(parts)
    item.observed["accounts"] = sorted({part.telegram_user_id for part in parts})
    item.observed["message_ids"] = [part.message_id for part in parts]
    item.observed["file_ids"] = [part.file_id for part in parts]
    item.observed["split"] = bool(entry.is_split)
    item.observed["split_group_id"] = entry.split_group_id
    item.observed["part_sizes"] = [part.size for part in parts]
    item.observed["has_thumbnail"] = bool(entry.has_thumbnail)
    item.observed["declared_size"] = entry.real_size
    item.observed["file_hash"] = entry.file_hash

    expected_segments = 1 if item.size <= MESSAGE_MAX else -(-item.size // MESSAGE_MAX)
    item.observed["protocol"] = (
        "split" if expected_segments > 1
        else ("small" if item.size <= SMALL_FILE_MAX else "big")
    )
    if len(parts) != expected_segments:
        item.findings.append(
            f"expected {expected_segments} segment(s), the part table has {len(parts)}"
        )
    if expected_segments > 1 and not entry.is_split:
        item.findings.append("a multi-segment file was not registered as split")
    if sum(part.size for part in parts) != item.size:
        item.findings.append(
            f"parts cover {sum(p.size for p in parts)} bytes, wrote {item.size}"
        )
    if entry.real_size != item.size:
        item.findings.append(f"declared size {entry.real_size}, wrote {item.size}")

    if item.expect_thumbnail and not entry.has_thumbnail:
        item.findings.append("registered without has_thumbnail, so no preview on either client")

    # Media attributes and the preview both come back through the account that
    # stores the message, which is the whole point of the routing: ask the
    # wrong one and these answer 404 rather than answering wrongly.
    windows_path = f"{item.local}"
    for endpoint in ("props", "thumb"):
        try:
            response = requests.get(
                f"{rpc}/rpc/{endpoint}", params={"path": windows_path}, timeout=120,
            )
        except Exception as exc:
            item.findings.append(f"/rpc/{endpoint} failed: {type(exc).__name__}: {exc}")
            continue
        if endpoint == "props":
            item.observed["props"] = (
                response.json() if response.ok else f"{response.status_code}"
            )
        else:
            item.observed["thumb_bytes"] = len(response.content) if response.ok else 0
            if item.expect_thumbnail and not response.ok:
                item.findings.append(
                    f"/rpc/thumb answered {response.status_code}: the shell handler "
                    "cannot tell that from a failed fetch and reads the whole file"
                )

    # The real proof: read it back through the bridge, not off the mount, and
    # hash what comes out.
    url = f"{rpc}/{folder}/{item.name}"
    try:
        digest = hashlib.sha256()
        read = 0
        with requests.get(url, stream=True, timeout=1800) as response:
            response.raise_for_status()
            for chunk in response.iter_content(1 << 20):
                digest.update(chunk)
                read += len(chunk)
        item.observed["read_bytes"] = read
        item.observed["read_sha256"] = digest.hexdigest()
        if read != item.size:
            item.findings.append(f"read back {read} bytes, wrote {item.size}")
        elif digest.hexdigest() != item.sha256:
            item.findings.append("read back the right length but different bytes")
    except Exception as exc:
        item.findings.append(f"read back failed: {type(exc).__name__}: {exc}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True,
                        help="drive-relative folder to write into, e.g. _parity-probe")
    parser.add_argument("--max-bytes", type=int, required=True,
                        help="refuse to run if the matrix would upload more than this")
    parser.add_argument("--cases", default="",
                        help="comma-separated case names; default is all of them")
    parser.add_argument("--timeout-minutes", type=float, default=240.0)
    parser.add_argument("--report", default="")
    parser.add_argument("--dry-run", action="store_true",
                        help="plan and print the budget; write nothing, upload nothing")
    parser.add_argument("--verify-only", action="store_true",
                        help="skip the writes and check what a previous run already uploaded")
    args = parser.parse_args(argv)

    cfg = load_config()
    rpc = f"http://{cfg.host}:{cfg.port}"
    mount = Path(cfg.mount_drive + "\\")
    if not mount.exists():
        raise SystemExit(f"{cfg.mount_drive} is not mounted; start.bat first")

    selected = CASES
    if args.cases:
        wanted = {name.strip() for name in args.cases.split(",") if name.strip()}
        selected = tuple(case for case in CASES if case.name in wanted)
        if not selected:
            raise SystemExit(f"no such case(s): {sorted(wanted)}")

    total = sum(
        sum(case.sizes[i % len(case.sizes)] for i in range(case.count))
        for case in selected
    )
    print(f"planned upload: {total / MiB:.1f} MiB across "
          f"{sum(case.count for case in selected)} files")
    if total > args.max_bytes:
        raise SystemExit(
            f"refusing to run: {total} bytes exceeds the authorised {args.max_bytes}"
        )

    free = shutil.disk_usage(cfg.cache_dir.drive + "\\").free
    if free < total * 3:
        print(f"[warn] only {free / MiB:.0f} MiB free on {cfg.cache_dir.drive}; "
              f"rclone's cache and uploads/ both hold a copy")

    if args.dry_run:
        for case in selected:
            print(f"  {case.name}: {case.count} file(s), "
                  f"{[s / MiB for s in case.sizes]} MiB, expect {case.expect}")
        print("dry run: nothing written")
        return 0

    planned = plan(selected, mount, args.folder)
    if not args.verify_only:
        print(f"writing into {mount / args.folder} ...")
        write_all(planned, mount, args.folder)
    else:
        for item in planned:
            item.__dict__.pop("_body", None)

    deadline = time.monotonic() + args.timeout_minutes * 60
    print(f"waiting for the debounce ({cfg.debounce_minutes} min) and the uploads ...")
    if not wait_for_quiet(rpc, {i.name for i in planned}, deadline):
        print("[error] the upload queue did not drain within the timeout")

    api = TeleDriveClient(cfg)
    api.invalidate()
    grouped = entries_for(api, args.folder)

    print("verifying ...")
    for item in planned:
        verify(item, grouped.get(item.name), api, rpc, args.folder)
        state = "ok" if not item.findings else "MISMATCH"
        print(f"  [{state}] {item.name}: {item.observed.get('protocol')} "
              f"rows={item.observed.get('rows')} accounts={item.observed.get('accounts')}")
        for finding in item.findings:
            print(f"      - {finding}")

    # Dedup is a property of the pair, not of either file on its own.
    pair = [i for i in planned if i.case == "duplicate-pair"]
    dedup = None
    if len(pair) == 2:
        first, second = (i.observed.get("message_ids") for i in pair)
        dedup = first == second and first not in (None, [None])
        print(f"  [{'ok' if dedup else 'MISMATCH'}] duplicate-pair reused the same "
              f"message: {first} vs {second}")

    report = {
        "folder": args.folder,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_bytes": total,
        "duplicate_reused_message": dedup,
        "files": [
            {
                "case": i.case, "name": i.name, "size": i.size,
                "sha256": i.sha256, "expect": i.expect,
                "observed": i.observed, "findings": i.findings,
            }
            for i in planned
        ],
    }
    path = Path(args.report) if args.report else cfg.cache_dir / "parity-report.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"report: {path}")

    mismatches = sum(len(i.findings) for i in planned) + (0 if dedup is not False else 1)
    print(f"{mismatches} finding(s)")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
