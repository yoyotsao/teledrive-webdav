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
import tgio  # noqa: E402
from config import Config  # noqa: E402
from fetchlocal import LocalFetcher  # noqa: E402
from gamestage import GameStager  # noqa: E402
from tdapi import TeleDriveClient  # noqa: E402
from uploadstage import UploadStager  # noqa: E402

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
        self.reads = []
        self._next_id = 1000

    def add_message(self, blob: bytes) -> int:
        self._next_id += 1
        self.messages[self._next_id] = blob
        return self._next_id

    def read(self, message_id: int, offset: int, length: int) -> bytes:
        self.reads.append((message_id, offset, length))
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
        # Like the real one: every message that could be read gets an entry, and
        # one with nothing to report answers {} rather than going missing.
        return {m: self.media.get(m, {}) for m in message_ids if m in self.messages}

    def upload_segment(self, stream, size, file_name, progress=None, preview=None):
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
        # Recorded, not ignored: the preview and its dimensions are the only
        # reason /rpc/thumb and /rpc/props can answer for our own uploads, and
        # the file it points at is deleted as soon as the upload returns.
        if preview is not None:
            assert preview[0].exists() and preview[0].suffix == ".jpg"
            preview = (preview[0].read_bytes(), preview[1], preview[2])
        self.uploads.append(
            {"name": file_name, "size": size, "message_id": message_id, "preview": preview}
        )
        return {"message_id": message_id, "file_id": f"doc{message_id}", "access_hash": "ah", "size": size}

    def stop(self):
        pass


class FakeBackend:
    """Minimal but faithful re-implementation of the TeleDrive endpoints used."""

    def __init__(self):
        self.rows = []
        self.dms = []  # (bot_username, nonce) the bridge sent over MTProto
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
        if path == "/auth/challenge":
            return {"nonce": "test-nonce", "bot_username": "TestBot", "expires_in": 120}
        if path == "/auth/verify":
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

    def login(self, force=False, **kw):
        # Not stubbed out: the real login() runs, so the challenge handshake is
        # part of what this rig covers. Only the transport below is fake.
        self.set_dm_sender(lambda username, text: self.backend.dms.append((username, text)))
        return super().login(force=force, **kw)

    def _post_unauth(self, path, payload, *, waiting_ok=False):
        return self.backend.call("POST", path, None, payload)

    def _call(self, method, path, *, params=None, payload=None, **kw):
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
    def __init__(self, base, cfg, backend, worker, stager, resolver, upload_stager=None):
        self.base = base
        self.cfg = cfg
        self.backend = backend
        self.worker = worker
        self.stager = stager
        self.resolver = resolver
        self.upload_stager = upload_stager

    def request(self, method, path, **kw):
        return requests.request(method, self.base + path, timeout=30, **kw)

    def propfind(self, path, depth="1"):
        return self.request("PROPFIND", path, headers={"Depth": depth})

    def entry_for(self, path):
        return self.resolver.api.resolve(path.split("/"))

    def blob_for(self, path):
        """A cloud file's real bytes, straight out of the fake Telegram.

        Clipped to total_size like the provider does: the backend's filesize is
        rounded up to whole 512 KB chunks, so the concatenated parts are longer
        than the file.
        """
        entry = self.entry_for(path)
        whole = b"".join(self.worker.messages[mid] for mid, _ in self.resolver.api.parts_for(entry))
        return whole[: self.resolver.api.total_size(entry)]

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
        upload_dir=tmp_path / "uploads",
        debounce_minutes=0.0,
    )
    for path in (cfg.cache_dir, cfg.local_dir, cfg.staging_dir, cfg.pack_dir, cfg.upload_dir):
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
    api.login()  # as bridge.main does, and for the same reason: nothing works without it
    resolver = bridge.Resolver(cfg, api, worker)
    stager = GameStager(cfg, api, worker)
    resolver.stager = stager
    upload_stager = UploadStager(cfg, api, worker)
    resolver.upload_stager = upload_stager
    app = bridge.build_app(cfg, resolver, stager, LocalFetcher(cfg, resolver), upload_stager)

    server = wsgi.Server((cfg.host, 0), app, numthreads=8)
    server.prepare()
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    host, port = server.bind_addr[0], server.bind_addr[1]
    try:
        yield Rig(f"http://{host}:{port}", cfg, backend, worker, stager, resolver, upload_stager)
    finally:
        server.stop()
        thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# M1 — read-only browsing and reading
# --------------------------------------------------------------------------- #


def test_login_answered_the_bot_challenge(rig):
    """The backend dropped /auth/login; a JWT now costs one nonce DMed to the
    bot from this account. Browsing at all proves the handshake ran, but pin the
    DM too -- a silent fallback here is what took H: down."""
    assert rig.backend.dms == [("TestBot", "test-nonce")]


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
        ("DELETE", "/photos/small.txt"),
        ("DELETE", "/movie.mkv"),
        ("PROPPATCH", "/photos/small.txt"),
        ("LOCK", "/photos/small.txt"),
        ("DELETE", "/game"),
    ],
)
def test_writes_outside_game_are_forbidden(rig, method, path):
    # PUT and MKCOL are not in this list — see test_uploadstage.py's write path
    # and test_mkcol_outside_game_creates_a_real_backend_folder below. Both map
    # onto real backend endpoints; everything here does not.
    # DELETE carries no body over the wire (rclone/Explorer never send one);
    # a body would earn its own 415 from wsgidav before ever reaching the
    # resource, since DELETE is no longer intercepted by WriteGuard itself.
    body = None if method == "DELETE" else b"x"
    resp = rig.request(method, path, data=body)
    assert resp.status_code == 403, (method, path, resp.status_code)


@pytest.mark.parametrize("path", ["/newdir", "/photos/newdir"])
def test_mkcol_outside_game_creates_a_real_backend_folder(rig, path):
    # Folder creation maps onto the backend's own POST /folders, so — unlike
    # file PUT, which has no such endpoint — it is not limited to /game.
    resp = rig.request("MKCOL", path)
    assert resp.status_code == 201, (path, resp.status_code, resp.text)
    parent, name = path.rsplit("/", 1)
    assert name in rig.names(parent or "/")


def test_move_into_a_read_only_path_is_forbidden(rig):
    resp = rig.request(
        "MOVE", "/game/MyGame", headers={"Destination": rig.base + "/photos/stolen"}
    )
    assert resp.status_code == 403


def test_copy_already_uploaded_file_outside_game_is_forbidden(rig):
    resp = rig.request(
        "COPY", "/photos/small.txt", headers={"Destination": rig.base + "/photos/copy.txt"}
    )
    assert resp.status_code == 403, resp.status_code
    assert "already uploaded" in resp.text, resp.text
    assert "copy.txt" not in rig.names("/photos")


def test_copy_already_uploaded_folder_outside_game_is_forbidden(rig):
    # _ReadOnlyCollection.handle_copy() via FolderCollection, not
    # ZipDirCollection — the only other collection coverage
    # (test_copy_already_packed_game_folder_does_not_walk_the_archive)
    # exercises the zip-archive subclass exclusively.
    resp = rig.request("COPY", "/photos", headers={"Destination": rig.base + "/photos2"})
    assert resp.status_code == 403, resp.status_code
    assert "photos2" not in rig.names("/")


def test_copy_already_uploaded_file_into_game_staging_is_forbidden(rig):
    # Mirror image of test_copy_already_uploaded_file_outside_game_is_forbidden:
    # that test copies already-uploaded content to another already-uploaded
    # destination. This one crosses into /game staging instead — still the
    # same _ReadOnlyFile.copy_move_single(), which has no special case for a
    # staging destination.
    rig.request("MKCOL", "/game/Temp")

    resp = rig.request(
        "COPY", "/photos/small.txt", headers={"Destination": rig.base + "/game/Temp/x.txt"}
    )
    assert resp.status_code == 403, resp.status_code
    assert not (rig.cfg.staging_dir / "Temp" / "x.txt").exists()


def test_read_only_paths_are_unchanged_after_rejected_deletes(rig):
    rig.request("DELETE", "/photos/small.txt")
    assert rig.names("/photos") == ["shot.png", "small.txt"]


# --------------------------------------------------------------------------- #
# M1b — plain writes outside /game (uploadstage.py)
# --------------------------------------------------------------------------- #


def _upload_now(rig, *segments):
    """Run what uploadstage's debounce loop would run, without waiting."""
    key = tuple(segments)
    due = rig.upload_stager._due(0.0)
    assert key in due, f"{key} not due; pending={rig.upload_stager.status()}"
    rig.upload_stager._process(key)


def test_put_outside_game_is_visible_locally_before_upload(rig):
    payload = b"brand new content" * 50
    assert rig.request("PUT", "/photos/fresh.bin", data=payload).status_code == 201
    assert "fresh.bin" in rig.names("/photos")
    assert rig.request("GET", "/photos/fresh.bin").content == payload


def test_put_outside_game_uploads_verbatim_and_registers_at_the_real_parent(rig):
    import mimetypes

    payload = b"plain file, no zip" * 100
    assert rig.request("PUT", "/photos/fresh.bin", data=payload).status_code == 201
    _upload_now(rig, "photos", "fresh.bin")

    assert rig.worker.messages[rig.worker.uploads[-1]["message_id"]] == payload
    photos_id = next(r["file_id"] for r in rig.backend.rows if r["filename"] == "photos")
    row = next(r for r in rig.backend.rows if r["filename"] == "fresh.bin")
    assert row["parent_id"] == photos_id
    assert row["mime_type"] == (mimetypes.guess_type("fresh.bin")[0] or "application/octet-stream")
    assert row["is_split_file"] is False
    assert row["file_hash"].endswith(f":{len(payload)}")

    # Staging is gone and the file now browses as a normal cloud entry.
    assert not (rig.cfg.upload_dir / "photos" / "fresh.bin").exists()
    assert rig.request("GET", "/photos/fresh.bin").content == payload


def test_putting_an_image_attaches_a_preview_and_its_dimensions(rig):
    """The whole path, PUT to send_file, for the one file type H: is full of.

    Without this the upload reaches Telegram as a bare document: doc.thumbs is
    empty, there are no dimensions, /rpc/thumb answers 404, and the shell
    handler -- which cannot tell that from a failed fetch -- delegates to the
    built-in one, which reads the whole image back down to draw an icon.
    """
    pil = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    pil.new("RGB", (1400, 900), (10, 120, 200)).save(buf, "JPEG")

    assert rig.request("PUT", "/photos/shot.jpg", data=buf.getvalue()).status_code == 201
    _upload_now(rig, "photos", "shot.jpg")

    preview = rig.worker.uploads[-1]["preview"]
    assert preview is not None
    data, width, height = preview
    assert (width, height) == (1400, 900)  # the original's, for /rpc/props
    assert pil.open(io.BytesIO(data)).format == "JPEG"
    assert len(data) <= tgio.PREVIEW_MAX_BYTES


def test_put_at_drive_root_registers_under_no_parent(rig):
    payload = b"root drop" * 10
    assert rig.request("PUT", "/fresh.bin", data=payload).status_code == 201
    _upload_now(rig, "fresh.bin")
    row = next(r for r in rig.backend.rows if r["filename"] == "fresh.bin")
    assert row["parent_id"] is None
    assert rig.request("GET", "/fresh.bin").content == payload


def test_overwriting_an_existing_remote_file_registers_a_newer_row(rig):
    """No backend UNIQUE(filename, parent_id) — same shadowing rule as dedup
    registrations (CLAUDE.md plan risk #5): the newest row wins on read, the
    old one is never deleted."""
    new_payload = b"replacement content" * 20
    assert rig.request("PUT", "/photos/small.txt", data=new_payload).status_code < 300
    _upload_now(rig, "photos", "small.txt")

    rows = [r for r in rig.backend.rows if r["filename"] == "small.txt"]
    assert len(rows) == 2
    assert rig.request("GET", "/photos/small.txt").content == new_payload


def test_put_dedup_reuses_an_identical_upload(rig):
    payload = b"shared bytes" * 200
    rig.request("PUT", "/photos/one.bin", data=payload)
    _upload_now(rig, "photos", "one.bin")
    uploads_after_first = len(rig.worker.uploads)

    rig.request("PUT", "/two.bin", data=payload)
    _upload_now(rig, "two.bin")

    assert len(rig.worker.uploads) == uploads_after_first, "identical content should not re-upload"
    rows = [r for r in rig.backend.rows if r["filename"] in ("one.bin", "two.bin")]
    assert len({r["telegram_message_id"] for r in rows}) == 1


def test_upload_status_reports_pending_then_clears(rig):
    rig.request("PUT", "/photos/pending.bin", data=b"waiting")
    status = rig.request("GET", "/rpc/status").json()
    assert any(p["path"] == "photos/pending.bin" for p in status["uploads"]["pending"])

    _upload_now(rig, "photos", "pending.bin")

    status = rig.request("GET", "/rpc/status").json()
    assert status["uploads"]["pending"] == []


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


def test_writing_into_a_staged_folder_never_asks_the_backend(rig):
    """Per-file backend round trips are what made a real copy into the mount crawl.

    Measured on the live mount: bridge answers a 1 MB PUT in 3 ms, yet copying
    300 small files took 52.6 s against 0.3 s for the same copy onto a local
    disk. The cost was resolution, not writing — a file that does not exist yet
    misses the staging check, and the old code then asked the backend twice
    (game_children, then api.resolve) to confirm a name that a local directory
    listing already rules out.
    """
    assert rig.request("MKCOL", "/game/NewGame").status_code == 201
    assert rig.request("MKCOL", "/game/NewGame/data").status_code == 201

    seen = []
    original = rig.backend.call

    def spy(method, path, params, payload):
        seen.append(f"{method} {path}")
        return original(method, path, params, payload)

    rig.backend.call = spy
    try:
        for i in range(3):
            assert rig.request("PUT", f"/game/NewGame/data/f{i}.bin", data=b"x" * 1024).status_code == 201
        # MKCOL of a fresh subdirectory is the same question about a name.
        assert rig.request("MKCOL", "/game/NewGame/data/more").status_code == 201
    finally:
        rig.backend.call = original

    assert seen == [], f"writing under a staged folder must not touch the backend, got {seen}"


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


def test_split_segment_sizes_are_exact_not_inflated(rig, monkeypatch):
    # The browser uploader records the *inflated* size of the boundary
    # segment (parts_in_segment * PART_SIZE, frontend/src/lib/gramjs.ts:504,581)
    # -- tdapi's real_size/_clip_parts exists specifically to undo that
    # padding on read. webdav's own segment planning must never regress to
    # it: every part's registered filesize must be the exact byte count.
    monkeypatch.setattr(gamestage, "SEGMENT_SIZE", 4096)
    payload = bytes((i * 3) % 256 for i in range(4096 + 1))
    rig.request("PUT", "/game/Exact.zip", data=payload)
    _pack_now(rig, "Exact.zip")

    rows = sorted(
        (r for r in rig.backend.rows if r["filename"] == "Exact.zip"),
        key=lambda r: r["part_index"],
    )
    assert len(rows) == 2
    assert [r["filesize"] for r in rows] == [4096, 1]
    assert sum(r["filesize"] for r in rows) == len(payload)

    assert rig.request("GET", "/game/Exact.zip").content == payload


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


def test_copy_inside_staging_creates_an_independent_second_file(rig):
    rig.request("MKCOL", "/game/Temp")
    rig.request("PUT", "/game/Temp/a.bin", data=b"junk")

    resp = rig.request(
        "COPY", "/game/Temp/a.bin", headers={"Destination": rig.base + "/game/Temp/b.bin"}
    )
    assert resp.status_code == 201, resp.status_code
    assert rig.names("/game/Temp") == ["a.bin", "b.bin"]
    assert (rig.cfg.staging_dir / "Temp" / "a.bin").read_bytes() == b"junk"
    assert (rig.cfg.staging_dir / "Temp" / "b.bin").read_bytes() == b"junk"

    # Independent afterwards: deleting one must not touch the other.
    rig.request("DELETE", "/game/Temp/a.bin")
    assert rig.names("/game/Temp") == ["b.bin"]


def test_copy_a_staging_folder_creates_an_empty_destination_and_copies_files_individually(rig):
    rig.request("MKCOL", "/game/Temp")
    rig.request("PUT", "/game/Temp/a.bin", data=b"junk")

    resp = rig.request(
        "COPY", "/game/Temp", headers={"Destination": rig.base + "/game/Temp2/"}
    )
    assert resp.status_code == 201, resp.status_code
    assert rig.names("/game/Temp2") == ["a.bin"]
    assert (rig.cfg.staging_dir / "Temp2" / "a.bin").read_bytes() == b"junk"
    assert (rig.cfg.staging_dir / "Temp" / "a.bin").exists(), "source must survive a COPY"


def test_copy_out_of_game_staging_is_forbidden(rig):
    rig.request("MKCOL", "/game/Temp")
    rig.request("PUT", "/game/Temp/a.bin", data=b"junk")

    resp = rig.request(
        "COPY", "/game/Temp/a.bin", headers={"Destination": rig.base + "/photos/escaped.bin"}
    )
    assert resp.status_code == 403, resp.status_code
    # Not just any 403: specifically GameStager.copy()'s game-folder-prefix
    # guard, distinguishing this from the path_for()-returns-None guard
    # exercised by test_copy_into_staging_dot_segment_is_forbidden.
    assert "must stay under /game while staged" in resp.text, resp.text
    assert "escaped.bin" not in rig.names("/photos")


def test_copy_into_staging_dot_segment_is_forbidden(rig):
    # GameStager.copy() has two distinct PermissionError sources: the
    # game-folder-prefix guard above (dest_segments[0] != game_folder), and
    # path_for() returning None for an unsafe segment. A dot-prefixed
    # destination segment *inside* /game exercises the second one — nothing
    # else in the suite reaches it, since it needs a destination whose parent
    # already resolves (so wsgidav's own dest-parent check does not 409
    # first) but whose full path GameStager.path_for() still rejects.
    rig.request("MKCOL", "/game/Temp")
    rig.request("PUT", "/game/Temp/a.bin", data=b"junk")

    resp = rig.request(
        "COPY", "/game/Temp/a.bin", headers={"Destination": rig.base + "/game/Temp/.hidden"}
    )
    assert resp.status_code == 403, resp.status_code
    assert not (rig.cfg.staging_dir / "Temp" / ".hidden").exists()


def test_delete_already_packed_game_folder_is_forbidden_not_a_crash(rig):
    # MyGame is already packed and uploaded (see the rig fixture) — there is
    # no local staging copy shadowing it, so this exercises ZipDirCollection,
    # which relies on _ReadOnlyCollection.handle_delete() for a clean 403
    # instead of crashing on the unimplemented support_recursive_delete().
    resp = rig.request("DELETE", "/game/MyGame")
    assert resp.status_code == 403
    assert rig.names("/game") == ["MyGame"]


def test_copy_already_packed_game_file_is_forbidden_cleanly(rig):
    # bin/game.exe lives inside the already-uploaded MyGame.zip (see the rig
    # fixture) — this exercises ZipFileResource via _ReadOnlyFile, which has
    # no backend copy endpoint to call.
    resp = rig.request(
        "COPY",
        "/game/MyGame/bin/game.exe",
        headers={"Destination": rig.base + "/game/MyGame/bin/copy.exe"},
    )
    assert resp.status_code == 403, resp.status_code
    assert rig.names("/game/MyGame/bin") == ["game.exe", "pak0.pak"]


def test_move_already_packed_game_file_is_forbidden_cleanly(rig):
    # wsgidav's MOVE handling calls support_recursive_move() on the source
    # unconditionally, not just for collections — without _ReadOnlyFile's
    # override, that hits _DAVResource's `assert self.is_collection` default
    # and 500s instead of falling through to copy_move_single()'s 403.
    resp = rig.request(
        "MOVE",
        "/game/MyGame/bin/game.exe",
        headers={"Destination": rig.base + "/game/MyGame/bin/renamed.exe"},
    )
    assert resp.status_code == 403, resp.status_code
    assert rig.names("/game/MyGame/bin") == ["game.exe", "pak0.pak"]


def test_copy_already_packed_game_folder_does_not_walk_the_archive(rig, monkeypatch):
    calls = []
    original = bridge.ZipDirCollection.get_member_names

    def counting(self):
        calls.append(self.node.zip_name)
        return original(self)

    monkeypatch.setattr(bridge.ZipDirCollection, "get_member_names", counting)

    resp = rig.request(
        "COPY", "/game/MyGame", headers={"Destination": rig.base + "/game/MyGame2"}
    )
    assert resp.status_code == 403, resp.status_code
    assert calls == [], f"handle_copy should short-circuit before any member listing, got {calls}"


def test_delete_pending_general_upload_is_allowed(rig):
    """The same staged-vs-uploaded rule as /game, but outside it (uploadstage.py)."""
    rig.request("PUT", "/photos/pending.bin", data=b"waiting")
    assert "pending.bin" in rig.names("/photos")

    assert rig.request("DELETE", "/photos/pending.bin").status_code == 204

    assert "pending.bin" not in rig.names("/photos")
    assert not (rig.cfg.upload_dir / "photos" / "pending.bin").exists()
    status = rig.request("GET", "/rpc/status").json()
    assert status["uploads"]["pending"] == []
    # Never uploaded: the file must not have reached Telegram or the backend.
    assert not any(r["filename"] == "pending.bin" for r in rig.backend.rows)


def test_copy_pending_general_upload_creates_an_independent_second_file(rig):
    rig.request("PUT", "/photos/pending.bin", data=b"waiting")

    resp = rig.request(
        "COPY", "/photos/pending.bin", headers={"Destination": rig.base + "/photos/pending2.bin"}
    )
    assert resp.status_code == 201, resp.status_code
    assert rig.names("/photos") == ["pending.bin", "pending2.bin", "shot.png", "small.txt"]
    assert (rig.cfg.upload_dir / "photos" / "pending.bin").read_bytes() == b"waiting"
    assert (rig.cfg.upload_dir / "photos" / "pending2.bin").read_bytes() == b"waiting"

    # The two uploads are independent: uploading one must not affect the other.
    _upload_now(rig, "photos", "pending2.bin")
    assert (rig.cfg.upload_dir / "photos" / "pending.bin").exists()
    row = next(r for r in rig.backend.rows if r["filename"] == "pending2.bin")
    photos_id = next(r["file_id"] for r in rig.backend.rows if r["filename"] == "photos")
    assert row["parent_id"] == photos_id


def test_copy_general_pending_upload_across_game_boundary_is_forbidden(rig):
    rig.request("PUT", "/photos/pending.bin", data=b"waiting")

    resp = rig.request(
        "COPY", "/photos/pending.bin", headers={"Destination": rig.base + "/game/escaped.bin"}
    )
    assert resp.status_code == 403, resp.status_code
    # Specifically UploadFileResource.copy_move_single()'s /game guard, not
    # some other 403 (e.g. WriteGuard, which no longer even sees COPY).
    assert "cannot copy a pending upload into /game" in resp.text, resp.text
    assert "escaped.bin" not in rig.names("/game")


def test_copy_pending_upload_to_unsafe_destination_segment_is_forbidden(rig):
    rig.request("PUT", "/photos/pending.bin", data=b"waiting")

    # Destination with a leading-dot segment (.hidden) is unsafe; at root level so parent exists
    resp = rig.request(
        "COPY", "/photos/pending.bin", headers={"Destination": rig.base + "/.hidden"}
    )
    assert resp.status_code == 403, resp.status_code


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


# --------------------------------------------------------------------------- #
# warmup — the whole tree, in the background, without getting in the way
#
# Previews and dimensions only make a folder fast once they are already on disk.
# Fetching them on first visit still leaves that first visit slow, so the bridge
# walks the tree by itself and fills the caches in the gaps between requests.
# --------------------------------------------------------------------------- #


def _warmer(rig, **kw):
    from warmup import Warmer

    kw.setdefault("quiet", 0.0)
    # No shell warm by default: it drives the real Windows thumbnail pipeline
    # against a drive letter that does not exist here. shell_paths is tested on
    # its own, which is the part that can be wrong.
    kw.setdefault("shell_exe", None)
    return Warmer(rig.resolver, **kw)


def test_warmup_finds_every_file_in_the_tree(rig):
    files, todo = _warmer(rig).pending()
    # photos/small.txt, photos/shot.png, movie.mkv, game/MyGame.zip
    assert len(files) == 4
    assert len(todo) == 4


def test_warmup_caches_previews_and_dimensions(rig):
    warmer = _warmer(rig)
    _, todo = warmer.pending()
    assert warmer.fill(todo) == len(todo)
    assert list((rig.cfg.cache_dir / "thumbs").glob("*.jpg"))
    assert (rig.cfg.cache_dir / "media_props.json").exists()


def test_warmup_is_resumable(rig):
    """A second pass must find nothing to do, including for files with no media.

    A file whose document reports no dimensions has to cache that fact, or every
    pass asks Telegram about it again for as long as the bridge is up.
    """
    warmer = _warmer(rig)
    _, todo = warmer.pending()
    warmer.fill(todo)
    files, again = warmer.pending()
    assert len(files) == 4
    assert again == []


def test_warmup_serves_later_requests_from_disk(rig):
    warmer = _warmer(rig)
    _, todo = warmer.pending()
    warmer.fill(todo)
    rig.worker.thumb_batches = []
    rig.worker.media_batches = []
    assert rig.request("GET", "/rpc/thumb", params={"path": r"E:\photos\shot.png"}).status_code == 200
    assert rig.request("GET", "/rpc/props", params={"path": r"E:\photos\shot.png"}).status_code == 200
    _settle(rig)
    assert rig.worker.thumb_batches == []
    assert rig.worker.media_batches == []


def test_fill_fetches_the_head_of_still_images_and_drops_it_after(rig):
    """The front of every image, because the shell reads it whatever we answer.

    Explorer opens each JPEG from inside its thumbnail pipeline, after the
    provider has already handed it a bitmap. fill() fetches that head and hands
    it to that batch's shell warm — but the head is scratch, not a cache: it
    gets deleted the moment the warm is done with it, whether or not the warm
    itself found anything to do (shell_exe is None in these tests), because
    nothing past that one read ever looks at it again.
    """
    from bridge import HEAD_SIZE

    warmer = _warmer(rig)
    _, todo = warmer.pending()
    png = rig.entry_for("photos/shot.png")
    warmer.fill(todo)
    assert (png.message_id, 0, HEAD_SIZE) in rig.worker.reads
    # movie.mkv and small.txt are not files the shell reads the front of, and
    # shot.png's own head must not have survived past its batch.
    assert not list((rig.cfg.cache_dir / "heads").glob("*.head")), \
        "a head is scratch — nothing should be left once fill() returns"


def test_a_fetched_head_is_served_without_asking_telegram_again(rig):
    """What the head cache is actually for: SeekableRemoteFile answering from it.

    Not routed through fill() — that fetches and immediately drops the head, so
    this calls heads_for() directly to catch it while it exists, the same way a
    batch's shell warm briefly gets to use it.
    """
    png = rig.entry_for("photos/shot.png")
    rig.resolver.heads_for([png])
    rig.worker.reads = []
    body = rig.request("GET", "/photos/shot.png", headers={"Range": "bytes=0-31"}).content
    assert body == rig.blob_for("photos/shot.png")[:32]
    assert rig.worker.reads == [], "the head region must not go back to Telegram"


def test_a_read_past_the_head_still_returns_the_whole_file(rig):
    png = rig.entry_for("photos/shot.png")
    rig.resolver.heads_for([png])
    assert rig.request("GET", "/photos/shot.png").content == rig.blob_for("photos/shot.png")


def test_needs_warming_does_not_check_for_a_head(rig):
    """A head is scratch, not a persisted condition — see Warmer.fill.

    Preview and properties cached, no head file anywhere (fill() already
    deleted it), and needs_warming must still say this file is done rather than
    sending the whole tree through another pass just because it cleaned up
    after itself.
    """
    warmer = _warmer(rig)
    _, todo = warmer.pending()
    warmer.fill(todo)
    png = rig.entry_for("photos/shot.png")
    assert not (rig.cfg.cache_dir / "heads" / f"{png.file_id}.head").exists()
    assert rig.resolver.needs_warming(png) is False


def test_drop_heads_is_a_no_op_for_a_file_with_no_head(rig):
    png = rig.entry_for("photos/shot.png")
    rig.resolver.drop_heads([png])  # never had a head cached — must not raise


def test_clear_heads_removes_any_leftover_head_files(rig):
    """The backstop for a crash between heads_for and drop_heads inside a batch."""
    png = rig.entry_for("photos/shot.png")
    rig.resolver.heads_for([png])
    assert list((rig.cfg.cache_dir / "heads").glob("*.head"))
    rig.resolver.clear_heads()
    assert not list((rig.cfg.cache_dir / "heads").glob("*.head"))


def test_clear_heads_is_a_no_op_when_the_directory_does_not_exist(rig):
    import shutil

    shutil.rmtree(rig.cfg.cache_dir / "heads", ignore_errors=True)
    rig.resolver.clear_heads()  # must not raise


def test_shell_warm_targets_the_images_on_the_mount(rig):
    """The shell warm asks Windows about paths, not Telegram about entries.

    Only the files the shell renders and then goes and reads: a .txt or a .zip
    would cost a process round trip to be told there is no thumbnail.
    """
    warmer = _warmer(rig)
    files, _ = warmer.pending()
    assert warmer.shell_paths(files) == [rig.cfg.mount_drive + r"\photos\shot.png"]


def test_shell_warm_is_a_no_op_when_the_exe_is_not_built(rig):
    warmer = _warmer(rig)
    files, _ = warmer.pending()
    assert warmer.shell_warm(files) == 0


def test_warmup_does_not_mark_its_own_fetches_as_demand(rig):
    """Otherwise the sweep waits for a quiet line it is itself keeping busy."""
    warmer = _warmer(rig, quiet=30.0)
    _, todo = warmer.pending()
    rig.resolver._last_demand = 0.0  # noqa: SLF001 - white-box on purpose
    started = time.monotonic()
    warmer.fill(todo)
    assert time.monotonic() - started < 5.0


def test_warmup_waits_while_a_request_is_being_served(rig):
    warmer = _warmer(rig, quiet=0.3)
    _, todo = warmer.pending()
    rig.resolver.note_demand()
    started = time.monotonic()
    warmer.fill(todo)
    assert time.monotonic() - started >= 0.25


def test_warmup_stops_mid_pass_when_asked(rig):
    stop = threading.Event()
    stop.set()
    warmer = _warmer(rig, stop=stop)
    _, todo = warmer.pending()
    assert warmer.fill(todo) == 0
    assert not list((rig.cfg.cache_dir / "thumbs").glob("*.jpg"))


def test_background_warmup_runs_a_pass_and_shuts_down(rig):
    from warmup import BackgroundWarmup

    warmup = BackgroundWarmup(rig.resolver, interval_minutes=60, start_delay=0.0)
    warmup.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and _warmer(rig).pending()[1]:
            time.sleep(0.02)
        assert _warmer(rig).pending()[1] == []
    finally:
        warmup.stop()
    assert not warmup._thread.is_alive()  # noqa: SLF001 - white-box on purpose


def test_a_failing_batch_ends_the_pass_rather_than_spinning(rig, monkeypatch):
    calls = []

    def boom(entries):
        calls.append(entries)
        raise RuntimeError("telegram said no")

    monkeypatch.setattr(rig.resolver, "thumbs_for", boom)
    warmer = _warmer(rig)
    _, todo = warmer.pending()
    assert warmer.fill(todo) == 0
    assert len(calls) == 1


def test_warmup_abandons_the_tree_walk_when_stopped(rig):
    """Shutdown must not leave a sweep listing thousands of folders behind it."""
    stop = threading.Event()
    stop.set()
    files, todo = _warmer(rig, stop=stop).pending()
    assert (files, todo) == ([], [])


def test_shell_warm_paths_are_absolute_when_warming_a_subtree(rig):
    r"""A subtree walk still has to produce paths rooted at the drive.

    The walk numbers paths from wherever it starts, so warming one folder
    without telling it where that folder is gave H:\shot.png for a file one
    level down. The shell answers a path like that instantly and warms nothing,
    which is indistinguishable from success in the timings.
    """
    warmer = _warmer(rig)
    photos = rig.entry_for("photos")
    files, _ = warmer.pending(photos.file_id, "/photos")
    assert warmer.shell_paths(files) == [rig.cfg.mount_drive + r"\photos\shot.png"]
