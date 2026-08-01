"""End-to-end tests over real HTTP, with Telegram and the backend faked.

Everything under test is production code: the wsgidav provider, the write guard,
the RPC plane, tdapi's caching/resolution/split collapsing, gamestage's packing
and registration, fetchlocal's copying. Only two things are substituted:

* ``FakeBackend`` answers TeleDriveClient's HTTP calls from an in-memory row set,
  reproducing the endpoints' real behaviour (split rows collapse to part_index=0,
  /files and /folders are disjoint, check-hash returns every same-hash row).
* ``FakeWorker`` stands in for MTProto, serving and accepting bytes in memory.

This is what makes M1-M4 verifiable without credentials, rclone or WinFsp.
"""

import hashlib
import os
import io
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bridge  # noqa: E402
import gamestage  # noqa: E402
from config import Config  # noqa: E402
from fetchlocal import LocalFetcher  # noqa: E402
from gamestage import GameStager  # noqa: E402
from tdapi import TeleDriveClient  # noqa: E402

PAGE_SIZE = 10000


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


THUMB_JPEG = bytes.fromhex("ffd8ffe0") + b"fake jpeg preview" * 3 + bytes.fromhex("ffd9")


class FakeWorker:
    """In-memory stand-in for TelegramWorker."""

    user_id = 4242

    def __init__(self):
        self.messages = {}
        self.thumbs = {}
        self.media = {}
        self.uploads = []
        self._next_id = 1000

    def add_message(self, blob: bytes) -> int:
        self._next_id += 1
        self.messages[self._next_id] = blob
        return self._next_id

    def read(self, message_id: int, offset: int, length: int) -> bytes:
        blob = self.messages[message_id]
        return blob[offset : offset + length]

    def set_thumb(self, message_id: int, data: bytes) -> None:
        self.thumbs[message_id] = data

    def thumbnails(self, message_ids):
        self.thumb_batches = getattr(self, "thumb_batches", [])
        self.thumb_batches.append(list(message_ids))
        return {m: self.thumbs[m] for m in message_ids if m in self.thumbs}

    def set_media(self, message_id: int, info: dict) -> None:
        self.media[message_id] = info

    def media_info(self, message_ids):
        self.media_batches = getattr(self, "media_batches", [])
        self.media_batches.append(list(message_ids))
        return {m: self.media[m] for m in message_ids if m in self.media}

    def upload_segment(self, stream, size, file_name, progress=None):
        data = bytearray()
        while len(data) < size:
            chunk = stream.read(min(1 << 16, size - len(data)))
            if not chunk:
                break
            data += chunk
            if progress:
                progress(len(data), size)
        assert len(data) == size, f"segment short read: {len(data)} != {size}"
        message_id = self.add_message(bytes(data))
        self.uploads.append({"name": file_name, "size": size, "message_id": message_id})
        return {"message_id": message_id, "file_id": f"doc{message_id}", "access_hash": "ah", "size": size}

    def stop(self):
        pass


class FakeBackend:
    """Minimal but faithful re-implementation of the TeleDrive endpoints used."""

    def __init__(self):
        self.rows = []
        self._clock = datetime(2026, 7, 30, 12, 0, 0)

    # -- row helpers ------------------------------------------------------ #

    def _stamp(self) -> str:
        self._clock += timedelta(seconds=1)
        return self._clock.isoformat()

    def add_folder(self, name, parent_id=None):
        row = self._row(name, 0, parent_id=parent_id, is_dir=True)
        self.rows.append(row)
        return row

    def add_file(self, name, blob, worker, parent_id=None, mime="application/octet-stream", segment=None,
                 thumb=None, media=None):
        """Register a file, splitting it into parts when ``segment`` is given."""
        if segment is None:
            message_id = worker.add_message(blob)
            row = self._row(name, len(blob), parent_id=parent_id, mime=mime, message_id=message_id)
            if thumb is not None:
                worker.set_thumb(message_id, thumb)
                row["has_thumbnail"] = True
            if media is not None:
                worker.set_media(message_id, media)
            self.rows.append(row)
            return [row]
        group = uuid.uuid4().hex[:8]
        parts = [blob[i : i + segment] for i in range(0, len(blob), segment)]
        out = []
        for index, chunk in enumerate(parts):
            message_id = worker.add_message(chunk)
            row = self._row(
                name,
                len(chunk),
                parent_id=parent_id,
                mime=mime,
                message_id=message_id,
                is_split=True,
                group=group,
                part_index=index,
            )
            self.rows.append(row)
            out.append(row)
        return out

    def _row(
        self,
        name,
        size,
        *,
        parent_id=None,
        is_dir=False,
        mime=None,
        message_id=None,
        is_split=False,
        group=None,
        part_index=None,
        file_hash=None,
    ):
        return {
            "file_id": uuid.uuid4().hex,
            "filename": name,
            "filesize": size,
            "mime_type": mime,
            "file_type": "other",
            "telegram_message_id": message_id,
            "has_thumbnail": False,
            "created_at": self._stamp(),
            "direct_url": None,
            "access_hash": "ah" if message_id else None,
            "parent_id": parent_id,
            "isDir": is_dir,
            "is_split_file": is_split,
            "split_group_id": group,
            "part_index": part_index,
            "file_hash": file_hash,
        }

    # -- endpoint dispatch ------------------------------------------------ #

    def call(self, method, path, params, payload):
        params = params or {}
        if path == "/auth/login":
            return {"token": "test-jwt", "user_id": 4242}
        if path == "/folders" and method == "GET":
            return self._list(params, want_dir=True)
        if path == "/files" and method == "GET":
            return self._list(params, want_dir=False)
        if path == "/folders" and method == "POST":
            return self.add_folder(payload["name"], payload.get("parent_id"))
        if path.startswith("/files/by-split-group/"):
            group = path.rsplit("/", 1)[1]
            rows = sorted(
                (r for r in self.rows if r["split_group_id"] == group), key=lambda r: r["part_index"] or 0
            )
            return {"files": rows, "total": len(rows), "page": 1, "page_size": len(rows)}
        if path == "/files/check-hash":
            rows = [r for r in self.rows if r["file_hash"] and r["file_hash"] == params.get("hash")]
            return {"found": bool(rows), "files": rows}
        if path == "/files/register":
            row = self._row(
                payload["filename"],
                payload["filesize"],
                parent_id=payload.get("parent_id"),
                mime=payload.get("mime_type"),
                message_id=payload["message_id"],
                is_split=payload.get("is_split_file", False),
                group=payload.get("split_group_id"),
                part_index=payload.get("part_index"),
                file_hash=payload.get("file_hash"),
            )
            row["file_id"] = payload["file_id"]
            self.rows.append(row)
            return row
        raise AssertionError(f"unexpected API call {method} {path}")

    def _list(self, params, want_dir):
        parent = params.get("parent_id")
        rows = [
            r
            for r in self.rows
            if bool(r["isDir"]) is want_dir
            and r["parent_id"] == parent
            # Split parts collapse to the primary part, as the real query does.
            and (not r["is_split_file"] or (r["part_index"] or 0) == 0)
        ]
        page = int(params.get("page", 1))
        size = int(params.get("page_size", PAGE_SIZE))
        start = (page - 1) * size
        return {"files": rows[start : start + size], "total": len(rows), "page": page, "page_size": size}


class FakeClient(TeleDriveClient):
    """The real client with only its HTTP transport replaced."""

    def __init__(self, cfg, backend):
        super().__init__(cfg)
        self.backend = backend

    def login(self, force=False):
        self._token = "test-jwt"
        return self._token

    def _call(self, method, path, *, params=None, payload=None, _retry=True):
        return self.backend.call(method, path, params, payload)


# --------------------------------------------------------------------------- #
# Rig
# --------------------------------------------------------------------------- #


ZIP_MEMBERS = {
    "bin/game.exe": b"MZ" + bytes(range(256)) * 30,
    "bin/pak0.pak": bytes((i * 13) % 251 for i in range(40_000)),
    "說明.txt": "中文內容\n".encode("utf-8"),
    # Larger than tgio's 1 MiB read block, so "browsing does not download the
    # archive" is actually measurable rather than trivially true.
    "big/filler.bin": b"\x5a" * 5_000_000,
}

SMALL = b"hello teledrive\n" * 8
BIG = bytes((i * 31) % 256 for i in range(300_000))


class Rig:
    def __init__(self, base, cfg, backend, worker, stager, resolver):
        self.base = base
        self.cfg = cfg
        self.backend = backend
        self.worker = worker
        self.stager = stager
        self.resolver = resolver

    def request(self, method, path, **kw):
        return requests.request(method, self.base + path, timeout=30, **kw)

    def propfind(self, path, depth="1"):
        return self.request("PROPFIND", path, headers={"Depth": depth})

    def prop(self, path, name, depth="0"):
        """One live property's text, without assuming wsgidav's XML prefix."""
        import re

        body = self.propfind(path, depth).text
        found = re.search(rf"<(?:\w+:)?{name}>([^<]*)</(?:\w+:)?{name}>", body)
        return found.group(1) if found else None

    def has_tag(self, path, name, depth="0"):
        import re

        body = self.propfind(path, depth).text
        return re.search(rf"<(?:\w+:)?{name}\s*/?>", body) is not None

    def names(self, path, depth="1"):
        """Child hrefs of a PROPFIND response, decoded and de-prefixed."""
        from urllib.parse import unquote

        import re

        body = self.propfind(path, depth).text
        hrefs = [unquote(h) for h in re.findall(r"<(?:\w+:)?href>([^<]+)</(?:\w+:)?href>", body)]
        prefix = path.rstrip("/") + "/"
        out = []
        for href in hrefs:
            if href.rstrip("/") == path.rstrip("/") or not href.startswith(prefix):
                continue
            out.append(href[len(prefix) :].strip("/"))
        return sorted(out)


@pytest.fixture
def rig(tmp_path):
    from cheroot import wsgi

    cfg = Config(
        api_id=1,
        api_hash="hash",
        session="session",
        base_url="http://backend.invalid",
        game_folder="game",
        dir_cache_seconds=0.0,  # every listing is fresh: the fake backend is the truth
        host="127.0.0.1",
        port=0,
        mount_drive="E:",
        log_level="WARNING",
        cache_dir=tmp_path / "cache",
        local_dir=tmp_path / "local",
        staging_dir=tmp_path / "staging",
        debounce_minutes=0.0,
    )
    for path in (cfg.cache_dir, cfg.local_dir, cfg.staging_dir, cfg.pack_dir):
        path.mkdir(parents=True, exist_ok=True)

    worker = FakeWorker()
    backend = FakeBackend()

    # A cloud tree: a plain file, a split file, a folder, and a packed game.
    photos = backend.add_folder("photos")
    backend.add_file("small.txt", SMALL, worker, parent_id=photos["file_id"], mime="text/plain")
    # one file with a Telegram preview, to exercise /.thumbs
    backend.add_file(
        "shot.png", SMALL * 40, worker, parent_id=photos["file_id"],
        mime="image/png", thumb=THUMB_JPEG,
        media={"width": 1920, "height": 1080, "mime": "image/png"},
    )
    backend.add_file("movie.mkv", BIG, worker, mime="video/x-matroska", segment=100_000)
    game = backend.add_folder("game")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for name, blob in ZIP_MEMBERS.items():
            zf.writestr(name, blob)
    backend.add_file("MyGame.zip", buf.getvalue(), worker, parent_id=game["file_id"], mime="application/zip")

    api = FakeClient(cfg, backend)
    resolver = bridge.Resolver(cfg, api, worker)
    stager = GameStager(cfg, api, worker)
    resolver.stager = stager
    app = bridge.build_app(cfg, resolver, stager, LocalFetcher(cfg, resolver))

    server = wsgi.Server((cfg.host, 0), app, numthreads=8)
    server.prepare()
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    host, port = server.bind_addr[0], server.bind_addr[1]
    try:
        yield Rig(f"http://{host}:{port}", cfg, backend, worker, stager, resolver)
    finally:
        server.stop()
        thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# M1 — read-only browsing and reading
# --------------------------------------------------------------------------- #


def test_propfind_root_lists_the_drive(rig):
    resp = rig.propfind("/")
    assert resp.status_code == 207
    assert "multistatus" in resp.text
    assert rig.names("/") == ["game", "movie.mkv", "photos"]


def test_propfind_reports_the_logical_size_of_a_split_file(rig):
    """part 0's filesize is only the first segment — every part must be summed."""
    assert rig.prop("/movie.mkv", "getcontentlength") == str(len(BIG))
    first_part = next(
        r["filesize"] for r in rig.backend.rows if r["filename"] == "movie.mkv" and r["part_index"] == 0
    )
    assert first_part == 100_000 < len(BIG)


def test_get_small_file(rig):
    resp = rig.request("GET", "/photos/small.txt")
    assert resp.status_code == 200
    assert resp.content == SMALL
    assert resp.headers["Accept-Ranges"] == "bytes"


def test_get_split_file_whole(rig):
    resp = rig.request("GET", "/movie.mkv")
    assert resp.status_code == 200
    assert resp.content == BIG
    assert hashlib.sha256(resp.content).hexdigest() == hashlib.sha256(BIG).hexdigest()


@pytest.mark.parametrize(
    "start,end",
    [(0, 99), (100, 200), (99_990, 100_010), (299_000, 299_999), (0, 299_999), (250_000, 999_999)],
)
def test_get_split_file_ranges(rig, start, end):
    resp = rig.request("GET", "/movie.mkv", headers={"Range": f"bytes={start}-{end}"})
    assert resp.status_code == 206
    expected = BIG[start : end + 1]
    assert resp.content == expected
    assert resp.headers["Content-Range"] == f"bytes {start}-{min(end, len(BIG) - 1)}/{len(BIG)}"


def test_head_returns_size_without_body(rig):
    resp = rig.request("HEAD", "/movie.mkv")
    assert resp.status_code == 200
    assert resp.headers["Content-Length"] == str(len(BIG))
    assert resp.content == b""


def test_missing_path_is_404(rig):
    assert rig.request("GET", "/nope.bin").status_code == 404
    assert rig.propfind("/photos/nope").status_code == 404


# --------------------------------------------------------------------------- #
# M1 — write protection outside /game
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "method,path",
    [
        ("PUT", "/hack.txt"),
        ("PUT", "/photos/hack.txt"),
        ("DELETE", "/photos/small.txt"),
        ("DELETE", "/movie.mkv"),
        ("MKCOL", "/newdir"),
        ("MKCOL", "/photos/newdir"),
        ("PROPPATCH", "/photos/small.txt"),
        ("LOCK", "/photos/small.txt"),
        ("DELETE", "/game"),
    ],
)
def test_writes_outside_game_are_forbidden(rig, method, path):
    resp = rig.request(method, path, data=b"x")
    assert resp.status_code == 403, (method, path, resp.status_code)


def test_move_into_a_read_only_path_is_forbidden(rig):
    resp = rig.request(
        "MOVE", "/game/MyGame", headers={"Destination": rig.base + "/photos/stolen"}
    )
    assert resp.status_code == 403


def test_read_only_paths_are_unchanged_after_rejected_writes(rig):
    rig.request("PUT", "/photos/hack.txt", data=b"x")
    assert rig.names("/photos") == ["shot.png", "small.txt"]


# --------------------------------------------------------------------------- #
# M2 — zip virtual expansion
# --------------------------------------------------------------------------- #


def test_game_listing_hides_the_zip_and_shows_a_folder(rig):
    assert rig.names("/game") == ["MyGame"]


def test_zip_root_is_a_collection(rig):
    assert rig.has_tag("/game/MyGame", "collection")
    assert rig.names("/game/MyGame") == ["big", "bin", "說明.txt"]
    assert rig.names("/game/MyGame/bin") == ["game.exe", "pak0.pak"]


def test_zip_member_bytes_are_exact(rig):
    for name, blob in ZIP_MEMBERS.items():
        resp = rig.request("GET", "/game/" + "MyGame/" + name)
        assert resp.status_code == 200, name
        assert resp.content == blob, name


def test_zip_member_supports_ranges(rig):
    blob = ZIP_MEMBERS["bin/pak0.pak"]
    resp = rig.request("GET", "/game/MyGame/bin/pak0.pak", headers={"Range": "bytes=1000-1499"})
    assert resp.status_code == 206
    assert resp.content == blob[1000:1500]


def test_browsing_a_zip_does_not_download_it(rig):
    """Only the central directory at the tail may be read, never the payload."""
    archive_size = next(r["filesize"] for r in rig.backend.rows if r["filename"] == "MyGame.zip")
    read = {"bytes": 0}
    original = rig.worker.read

    def counting(message_id, offset, length):
        read["bytes"] += length
        return original(message_id, offset, length)

    rig.worker.read = counting
    rig.resolver._zips.clear()  # force a fresh central-directory parse
    assert rig.names("/game/MyGame/bin") == ["game.exe", "pak0.pak"]
    assert read["bytes"] < archive_size // 2, (read["bytes"], archive_size)


# --------------------------------------------------------------------------- #
# M3 — /game staging, packing and upload
# --------------------------------------------------------------------------- #


def _pack_now(rig, top):
    """Run what the debounce loop would run, without waiting."""
    due = rig.stager._due(0.0)
    assert top in due, f"{top} not due; units={rig.stager.status()}"
    rig.stager._process(top)


def test_game_accepts_a_folder_and_packs_it(rig):
    assert rig.request("MKCOL", "/game/NewGame").status_code == 201
    assert rig.request("MKCOL", "/game/NewGame/data").status_code == 201
    assert rig.request("MKCOL", "/game/NewGame/空目錄").status_code == 201
    assert rig.request("PUT", "/game/NewGame/run.exe", data=b"EXE" * 500).status_code == 201
    assert rig.request("PUT", "/game/NewGame/data/資料.bin", data=b"\x01\x02" * 900).status_code == 201

    # Staging is visible while the move is still in flight.
    assert rig.names("/game") == ["MyGame", "NewGame"]
    assert rig.names("/game/NewGame") == ["data", "run.exe", "空目錄"]
    assert rig.request("GET", "/game/NewGame/run.exe").content == b"EXE" * 500

    _pack_now(rig, "NewGame")

    # A single stored zip was uploaded and registered under the game folder.
    assert [u["name"] for u in rig.worker.uploads] == ["NewGame.zip"]
    rows = [r for r in rig.backend.rows if r["filename"] == "NewGame.zip"]
    assert len(rows) == 1
    assert rows[0]["mime_type"] == "application/zip"
    assert rows[0]["is_split_file"] is False
    assert rows[0]["file_hash"].endswith(f":{rows[0]['filesize']}")

    # Staging is gone and the archive now browses as a folder.
    assert not (rig.cfg.staging_dir / "NewGame").exists()
    assert list(rig.cfg.pack_dir.iterdir()) == []
    assert rig.names("/game") == ["MyGame", "NewGame"]
    assert rig.names("/game/NewGame") == ["data", "run.exe", "空目錄"]
    assert rig.request("GET", "/game/NewGame/data/資料.bin").content == b"\x01\x02" * 900


def test_packed_zip_is_stored_not_deflated(rig):
    rig.request("MKCOL", "/game/Stored")
    rig.request("PUT", "/game/Stored/a.bin", data=b"a" * 10_000)
    _pack_now(rig, "Stored")
    blob = rig.worker.messages[rig.worker.uploads[-1]["message_id"]]
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert [i.compress_type for i in zf.infolist()] == [zipfile.ZIP_STORED]


def test_a_single_file_dropped_into_game_is_uploaded_as_is(rig):
    payload = b"already packed" * 100
    assert rig.request("PUT", "/game/Manual.zip", data=payload).status_code == 201
    _pack_now(rig, "Manual.zip")
    assert rig.worker.messages[rig.worker.uploads[-1]["message_id"]] == payload
    assert [r["filename"] for r in rig.backend.rows if r["filename"] == "Manual.zip"]


def test_large_pack_is_split_and_reads_back_intact(rig, monkeypatch):
    # 500 MiB segments cannot be exercised in a test; shrink the boundary instead.
    monkeypatch.setattr(gamestage, "SEGMENT_SIZE", 4096)
    rig.request("MKCOL", "/game/Huge")
    payload = bytes((i * 7) % 251 for i in range(30_000))
    rig.request("PUT", "/game/Huge/blob.bin", data=payload)
    _pack_now(rig, "Huge")

    rows = [r for r in rig.backend.rows if r["filename"] == "Huge.zip"]
    assert len(rows) > 1, "expected a split registration"
    assert all(r["is_split_file"] for r in rows)
    assert sorted(r["part_index"] for r in rows) == list(range(len(rows)))
    assert len({r["split_group_id"] for r in rows}) == 1
    # Every segment is exactly one message, and the last one is the remainder.
    assert [r["filesize"] for r in rows[:-1]] == [4096] * (len(rows) - 1)

    # The virtual folder still resolves, which means the parts concatenate.
    assert rig.request("GET", "/game/Huge/blob.bin").content == payload


def _stage_and_pack(rig, top, member, payload, mtime=1_770_000_000):
    """Stage one file and pack it, with a fixed mtime so the zip is reproducible.

    zipfile records each member's mtime, so identical content only yields an
    identical archive (and therefore an identical dedup hash) when the timestamps
    match — which is exactly the real-world case of re-copying the same game.
    """
    rig.request("MKCOL", f"/game/{top}")
    rig.request("PUT", f"/game/{top}/{member}", data=payload)
    for path in (rig.cfg.staging_dir / top).rglob("*"):
        os.utime(path, (mtime, mtime))
    _pack_now(rig, top)


def test_repacking_identical_content_deduplicates(rig):
    _stage_and_pack(rig, "DupA", "x.bin", b"same bytes" * 1000)
    uploads_after_first = len(rig.worker.uploads)

    _stage_and_pack(rig, "DupB", "x.bin", b"same bytes" * 1000)

    assert len(rig.worker.uploads) == uploads_after_first, "should not re-upload a known hash"
    hashes = {r["file_hash"] for r in rig.backend.rows if r["filename"] in ("DupA.zip", "DupB.zip")}
    assert len(hashes) == 1
    # Both names exist, pointing at the same Telegram message.
    rows = [r for r in rig.backend.rows if r["filename"] in ("DupA.zip", "DupB.zip")]
    assert len(rows) == 2
    assert len({r["telegram_message_id"] for r in rows}) == 1


def test_dedup_registration_does_not_multiply_rows(rig):
    """Guards the historical bug where each re-upload doubled a hash's row count."""
    for i in range(4):
        _stage_and_pack(rig, f"Stable{i}", "y.bin", b"stable" * 500)
    rows = [r for r in rig.backend.rows if r["filename"].startswith("Stable")]
    assert len(rows) == 4, f"expected exactly one row per registration, got {len(rows)}"
    assert all(r["is_split_file"] is False for r in rows)
    assert len({r["file_hash"] for r in rows}) == 1
    assert len(rig.worker.uploads) == 1


def test_writing_over_a_packed_archive_is_refused(rig):
    """A name already packed in the cloud is read-only: a partial repack would
    silently drop every file that is not re-uploaded."""
    _stage_and_pack(rig, "Sealed", "a.bin", b"sealed" * 100)
    assert rig.names("/game/Sealed") == ["a.bin"]
    assert rig.request("PUT", "/game/Sealed/b.bin", data=b"new").status_code == 403
    assert rig.request("PUT", "/game/Sealed/a.bin", data=b"new").status_code == 403
    assert rig.request("MKCOL", "/game/Sealed/sub").status_code == 403
    assert rig.request("GET", "/game/Sealed/a.bin").content == b"sealed" * 100


def test_canonical_existing_parts_collapses_corrupt_groups():
    rows = [
        {"file_id": "a", "filesize": 10, "telegram_message_id": 1, "is_split_file": True,
         "split_group_id": "g1", "part_index": 0, "mime_type": None},
        {"file_id": "b", "filesize": 10, "telegram_message_id": 1, "is_split_file": True,
         "split_group_id": "g1", "part_index": 0, "mime_type": None},
        {"file_id": "c", "filesize": 5, "telegram_message_id": 2, "is_split_file": True,
         "split_group_id": "g1", "part_index": 1, "mime_type": None},
        {"file_id": "d", "filesize": 10, "telegram_message_id": 9, "is_split_file": True,
         "split_group_id": "g2", "part_index": 0, "mime_type": None},
    ]
    parts = gamestage.canonical_existing_parts(rows)
    assert [p["part_index"] for p in parts] == [0, 1]
    assert [p["message_id"] for p in parts] == [1, 2]


def test_delete_inside_staging_is_allowed(rig):
    rig.request("MKCOL", "/game/Temp")
    rig.request("PUT", "/game/Temp/a.bin", data=b"junk")
    assert rig.request("DELETE", "/game/Temp/a.bin").status_code == 204
    assert rig.names("/game/Temp") == []
    assert rig.request("DELETE", "/game/Temp").status_code == 204
    assert not (rig.cfg.staging_dir / "Temp").exists()


# --------------------------------------------------------------------------- #
# M4 — RPC plane and fetch-local
# --------------------------------------------------------------------------- #


def test_health(rig):
    data = rig.request("GET", "/rpc/health").json()
    assert data["ok"] is True
    assert data["telegram_user_id"] == 4242
    assert data["game_folder"] == "game"


def test_status_lists_staging_units(rig):
    rig.request("MKCOL", "/game/Watch")
    rig.request("PUT", "/game/Watch/a.bin", data=b"x")
    data = rig.request("GET", "/rpc/status").json()
    assert [u["name"] for u in data["units"]] == ["Watch"]
    assert data["units"][0]["state"] == "staging"


def test_forget_clears_caches(rig):
    assert rig.request("POST", "/rpc/forget").status_code == 200


def test_unknown_rpc_is_404(rig):
    assert rig.request("POST", "/rpc/nope").status_code == 404


def _fetch(rig, win_path):
    resp = rig.request("POST", "/rpc/fetch-local", data={"path": win_path})
    assert resp.status_code == 200
    lines = [ln for ln in resp.text.splitlines() if ln.strip()]
    return lines


def test_fetch_local_extracts_a_virtual_zip_folder(rig):
    lines = _fetch(rig, r"E:\game\MyGame")
    assert lines[-1].startswith("OK "), lines
    base = rig.cfg.local_dir / "MyGame"
    for name, blob in ZIP_MEMBERS.items():
        assert (base / name).read_bytes() == blob, name
    assert not list(base.rglob("*.part"))


def test_fetch_local_copies_a_split_file(rig):
    lines = _fetch(rig, r"E:\movie.mkv")
    assert lines[-1].startswith("OK ")
    assert (rig.cfg.local_dir / "movie.mkv").read_bytes() == BIG


def test_fetch_local_copies_a_cloud_folder(rig):
    lines = _fetch(rig, r"E:\photos")
    assert lines[-1].startswith("OK ")
    assert (rig.cfg.local_dir / "photos" / "small.txt").read_bytes() == SMALL


def test_fetch_local_copies_one_member_of_a_zip(rig):
    lines = _fetch(rig, r"E:\game\MyGame\bin\pak0.pak")
    assert lines[-1].startswith("OK ")
    assert (rig.cfg.local_dir / "pak0.pak").read_bytes() == ZIP_MEMBERS["bin/pak0.pak"]


def test_fetch_local_reports_progress(rig):
    lines = _fetch(rig, r"E:\game\MyGame\bin\pak0.pak")
    assert any(ln.startswith("PROGRESS ") for ln in lines)


def test_fetch_local_rejects_paths_off_the_mount(rig):
    assert any("ERROR" in ln for ln in _fetch(rig, r"C:\Windows\notepad.exe"))
    assert any("ERROR" in ln for ln in _fetch(rig, "E:\\"))


def test_fetch_local_reports_a_missing_path(rig):
    assert any("ERROR" in ln and "not found" in ln for ln in _fetch(rig, r"E:\game\Ghost"))


def test_windows_path_translation(rig):
    resolver = rig.resolver
    assert resolver.dav_path_from_windows(r"E:\game\MyGame") == ["game", "MyGame"]
    assert resolver.dav_path_from_windows("e:/game/MyGame/bin") == ["game", "MyGame", "bin"]
    assert resolver.dav_path_from_windows("E:\\") == []
    assert resolver.dav_path_from_windows(r"D:\other") is None


# --------------------------------------------------------------------------- #
# Robustness
# --------------------------------------------------------------------------- #


def test_duplicate_names_resolve_to_the_newest_row(rig, caplog):
    """The DB has no UNIQUE(filename, parent_id); the newer row must win."""
    newer = rig.backend.add_file("small.txt", b"NEWER CONTENT", rig.worker,
                                 parent_id=rig.backend.rows[0]["file_id"], mime="text/plain")
    assert newer[0]["created_at"] > rig.backend.rows[1]["created_at"]
    rig.resolver.api.invalidate()
    assert rig.request("GET", "/photos/small.txt").content == b"NEWER CONTENT"


def test_split_part_table_is_cached_on_disk(rig):
    rig.request("GET", "/movie.mkv", headers={"Range": "bytes=0-9"})
    store = rig.cfg.cache_dir / "split_parts.json"
    assert store.exists() and store.stat().st_size > 2


def test_concurrent_reads_are_independent(rig):
    """Two readers on one file must not share a position."""
    results = {}

    def grab(key, start, end):
        resp = rig.request("GET", "/movie.mkv", headers={"Range": f"bytes={start}-{end}"})
        results[key] = resp.content

    threads = [
        threading.Thread(target=grab, args=("a", 0, 4999)),
        threading.Thread(target=grab, args=("b", 150_000, 154_999)),
        threading.Thread(target=grab, args=("c", 295_000, 299_999)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert results["a"] == BIG[0:5000]
    assert results["b"] == BIG[150_000:155_000]
    assert results["c"] == BIG[295_000:300_000]


def test_bridge_never_asks_the_backend_for_bytes(rig):
    """The core invariant: no metadata endpoint is used to move file content."""
    seen = []
    original = rig.resolver.api.backend.call

    def spy(method, path, params, payload):
        seen.append(path)
        return original(method, path, params, payload)

    rig.resolver.api.backend.call = spy
    rig.request("GET", "/movie.mkv")
    rig.request("GET", "/game/MyGame/bin/game.exe")
    assert seen, "expected metadata calls"
    assert not any("stream" in p or "download" in p and "by-split" not in p for p in seen if "stream" in p)
    assert all(not p.endswith("/stream") for p in seen)



# --------------------------------------------------------------------------- #
# /rpc/thumb — Telegram's stored preview, for the shell thumbnail handler
#
# Explorer asked for a thumbnail reads every byte of the original, so the only
# way to make it cheap is a thumbnail provider that fetches the preview instead.
# This endpoint is what that provider calls; it speaks Windows paths because its
# caller has nothing else to give.
# --------------------------------------------------------------------------- #


def test_thumb_rpc_returns_the_preview(rig):
    resp = rig.request("GET", "/rpc/thumb", params={"path": r"E:\photos\shot.png"})
    assert resp.status_code == 200
    assert resp.content == THUMB_JPEG
    assert resp.headers["Content-Type"] == "image/jpeg"
    assert resp.headers["Content-Length"] == str(len(THUMB_JPEG))


def test_thumb_rpc_is_much_smaller_than_the_original(rig):
    resp = rig.request("GET", "/rpc/thumb", params={"path": r"E:\photos\shot.png"})
    assert len(resp.content) < len(SMALL * 40)


def test_thumb_rpc_404s_for_a_file_without_one(rig):
    resp = rig.request("GET", "/rpc/thumb", params={"path": r"E:\photos\small.txt"})
    assert resp.status_code == 404


def test_thumb_rpc_404s_off_the_mount(rig):
    resp = rig.request("GET", "/rpc/thumb", params={"path": r"C:\Windows\notepad.exe"})
    assert resp.status_code == 404


def test_thumb_rpc_404s_for_a_missing_file(rig):
    resp = rig.request("GET", "/rpc/thumb", params={"path": r"E:\photos\nope.png"})
    assert resp.status_code == 404


def test_thumb_rpc_caches_on_disk_and_stops_fetching(rig):
    rig.worker.thumb_batches = []
    for _ in range(4):
        assert rig.request("GET", "/rpc/thumb", params={"path": r"E:\photos\shot.png"}).status_code == 200
    _settle(rig)
    # The first miss also warms the rest of the folder in the background, so the
    # count is not exactly one — what matters is that it stops, rather than
    # growing once per request.
    fetched = len(rig.worker.thumb_batches)
    assert 1 <= fetched <= 2
    for _ in range(4):
        assert rig.request("GET", "/rpc/thumb", params={"path": r"E:\photos\shot.png"}).status_code == 200
    _settle(rig)
    assert len(rig.worker.thumb_batches) == fetched
    assert list((rig.cfg.cache_dir / "thumbs").glob("*.jpg"))


def test_thumb_rpc_warms_the_whole_folder(rig):
    """A miss should leave the folder's other previews on disk too.

    Explorer asks file by file, and each preview costs two Telegram round trips
    on its own; batching the folder is what keeps opening it from taking as long
    as the originals would.
    """
    thumbs = rig.cfg.cache_dir / "thumbs"
    for stale in thumbs.glob("*.jpg"):
        stale.unlink()
    rig.worker.set_thumb(rig.worker._next_id, THUMB_JPEG)  # noqa: SLF001 - test fake
    rig.request("GET", "/rpc/thumb", params={"path": r"E:\photos\shot.png"})
    _settle(rig)
    assert list(thumbs.glob("*.jpg"))


def _settle(rig, timeout=5.0):
    """Wait for background preview prefetching to finish."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not rig.resolver._thumb_warming:  # noqa: SLF001 - white-box on purpose
            return
        time.sleep(0.02)
    raise AssertionError("thumbnail prefetch did not settle")


# --------------------------------------------------------------------------- #
# /rpc/props — dimensions without reading the file
#
# Explorer reads the head of every image to work out its size; measured on the
# mount that was 258 KB of a 2 MB JPEG, per file, and it is what makes a folder
# crawl even once thumbnails are instant. Telegram already carries the numbers in
# the document's attributes, so this endpoint costs no bytes at all.
# --------------------------------------------------------------------------- #


def test_props_rpc_returns_dimensions(rig):
    resp = rig.request("GET", "/rpc/props", params={"path": r"E:\photos\shot.png"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["width"] == 1920
    assert body["height"] == 1080
    assert body["size"] == len(SMALL * 40)


def test_props_rpc_reads_no_bytes(rig):
    """The whole point: answering must not touch the file's content."""
    before = list(rig.worker.messages)
    rig.request("GET", "/rpc/props", params={"path": r"E:\photos\shot.png"})
    # FakeWorker.read is the only way to get bytes; nothing should have called it
    assert not hasattr(rig.worker, "read_calls")
    assert list(rig.worker.messages) == before


def test_props_rpc_is_empty_for_a_file_without_media(rig):
    resp = rig.request("GET", "/rpc/props", params={"path": r"E:\photos\small.txt"})
    assert resp.status_code == 200
    body = resp.json()
    assert "width" not in body
    assert body["size"] == len(SMALL)


def test_props_rpc_404s_off_the_mount(rig):
    assert rig.request("GET", "/rpc/props", params={"path": r"C:\Windows\notepad.exe"}).status_code == 404


def test_props_rpc_caches_on_disk(rig):
    rig.worker.media_batches = []
    for _ in range(4):
        assert rig.request("GET", "/rpc/props", params={"path": r"E:\photos\shot.png"}).status_code == 200
    _settle(rig)
    assert len(rig.worker.media_batches) == 1
    assert (rig.cfg.cache_dir / "media_props.json").exists()
