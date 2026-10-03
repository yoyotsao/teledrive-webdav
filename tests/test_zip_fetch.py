"""Offline tests for whole-archive game downloads (zipfetch.fetch_archive)."""

import io
import json
import logging
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import zipfetch  # noqa: E402
import zipfs  # noqa: E402

MEMBERS = {
    "readme.txt": b"hello",
    "bin/game.exe": b"MZ" + b"\x00" * 3000,
    "data/資料/設定.ini": "設定".encode("utf-8") * 50,
}


def make_zip(members=MEMBERS, compression=zipfile.ZIP_STORED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, blob in members.items():
            zf.writestr(name, blob)
    return buf.getvalue()


class Reader(io.BytesIO):
    def __init__(self, data, log):
        super().__init__(data)
        self._log = log

    def seek(self, pos, whence=0):
        self._log.append(("seek", pos))
        return super().seek(pos, whence)


class Rig:
    def __init__(self, tmp_path, data, file_id="f1"):
        self.data = data
        self.opens = []
        self.local = tmp_path / "local"
        self.entry = SimpleNamespace(file_id=file_id, name="Game.zip")
        self.resolver = SimpleNamespace(
            api=SimpleNamespace(total_size=lambda e: len(self.data)),
            open_remote=self._open,
        )
        self.view = zipfs.ZipView(lambda: io.BytesIO(self.data), name="Game")
        self.base = self.local / "Game"

    def _open(self, entry):
        self.opens.append(entry)
        self.reads = []
        return Reader(self.data, self.reads)

    def run(self, **kw):
        kw.setdefault("free_bytes", lambda _p: 10**12)
        return list(
            zipfetch.fetch_archive(
                resolver=self.resolver,
                entry=self.entry,
                view=self.view,
                base=self.base,
                local_dir=self.local,
                **kw,
            )
        )

    @property
    def part(self):
        return self.local / "Game.zip.part"

    @property
    def meta(self):
        return self.local / "Game.zip.part.meta"


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path, make_zip())


def assert_extracted(rig, members=MEMBERS):
    for name, blob in members.items():
        assert (rig.base / name).read_bytes() == blob, name


def test_downloads_whole_zip_once_and_extracts(rig):
    lines = rig.run()
    assert lines[-1] == f"OK {rig.base}", lines
    assert len(rig.opens) == 1  # one sequential read, not one reader per member
    assert_extracted(rig)
    assert not (rig.local / "Game.zip").exists()
    assert not rig.part.exists() and not rig.meta.exists()
    assert any(line.startswith("PROGRESS ") for line in lines)


def test_extracted_names_match_the_virtual_tree_for_utf8_and_cp437(tmp_path):
    members = {"é.txt": b"a", "資料/夾.txt": b"b", "plain.txt": b"c"}
    data = make_zip(members)
    # an old archive: no UTF-8 flag, name bytes are cp437 (0x82 == é)
    legacy = make_zip({"XX.txt": b"x"}).replace(b"XX.txt", b"\x82\x82.txt")
    for payload in (data, legacy):
        rig = Rig(tmp_path / str(len(payload)), payload)
        expected = {rel for rel, n in rig.view.walk() if not n.is_dir}
        lines = rig.run()
        assert lines[-1].startswith("OK "), lines
        found = {p.relative_to(rig.base).as_posix() for p in rig.base.rglob("*") if p.is_file()}
        assert found == expected
    assert "éé.txt" in expected


def test_resume_continues_from_part_length_when_meta_matches(rig):
    rig.local.mkdir(parents=True)
    half = len(rig.data) // 2
    rig.part.write_bytes(rig.data[:half])
    rig.meta.write_text(json.dumps({"file_id": "f1", "total_size": len(rig.data)}))
    lines = rig.run()
    assert lines[-1].startswith("OK "), lines
    assert ("seek", half) in rig.reads
    assert_extracted(rig)


def test_stale_part_is_discarded_when_file_id_or_size_differs(rig):
    for meta in ({"file_id": "other", "total_size": len(rig.data)}, {"file_id": "f1", "total_size": 7}):
        rig.local.mkdir(parents=True, exist_ok=True)
        rig.part.write_bytes(b"garbage-from-an-older-zip")
        rig.meta.write_text(json.dumps(meta))
        lines = rig.run()
        assert lines[-1].startswith("OK "), lines
        assert ("seek", 0) in rig.reads or not any(k == "seek" and v > 0 for k, v in rig.reads)
        assert_extracted(rig)


def test_cancel_keeps_part_and_meta(rig, monkeypatch):
    monkeypatch.setattr(zipfetch, "COPY_CHUNK", 512)
    cancel = threading.Event()
    seen = []

    def gen():
        for line in zipfetch.fetch_archive(
            resolver=rig.resolver,
            entry=rig.entry,
            view=rig.view,
            base=rig.base,
            local_dir=rig.local,
            cancel=cancel,
            free_bytes=lambda _p: 10**12,
        ):
            seen.append(line)
            if line.startswith("PROGRESS "):
                cancel.set()

    monkeypatch.setattr(zipfetch, "PROGRESS_INTERVAL", 0)
    gen()
    assert seen[-1].startswith("CANCELLED")
    assert rig.part.exists() and rig.meta.exists()
    assert 0 < rig.part.stat().st_size < len(rig.data)


@pytest.mark.parametrize("bad", ["../evil.txt", "/abs.txt", "C:/win.txt", "ok/../../up.txt", "a:stream"])
def test_zip_slip_is_rejected_and_nothing_is_written(tmp_path, bad):
    data = make_zip({"good.txt": b"g", "x.txt": b"x"}).replace(b"x.txt", b"X" * 0 + b"y.txt")
    # build the hostile archive through zipfile so lengths stay consistent
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("good.txt", b"g")
        zf.writestr(zipfile.ZipInfo(bad), b"evil")
    rig = Rig(tmp_path, buf.getvalue())
    lines = rig.run()
    assert lines[-1].startswith("ERROR"), lines
    assert not rig.base.exists() or not any(rig.base.rglob("*"))
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "up.txt").exists()
    assert (rig.local / "Game.zip").exists()  # kept so a retry need not re-download


def test_skip_existing_leaves_matching_members_alone(rig):
    rig.base.mkdir(parents=True)
    (rig.base / "readme.txt").write_bytes(b"HELLO")  # same size, different bytes
    lines = rig.run(skip_existing=True)
    assert lines[-1].startswith("OK "), lines
    assert (rig.base / "readme.txt").read_bytes() == b"HELLO"
    assert (rig.base / "bin/game.exe").read_bytes() == MEMBERS["bin/game.exe"]


def test_without_skip_existing_members_are_rewritten(rig):
    rig.base.mkdir(parents=True)
    (rig.base / "readme.txt").write_bytes(b"HELLO")
    rig.run()
    assert (rig.base / "readme.txt").read_bytes() == b"hello"


def test_not_enough_disk_space_refuses_before_downloading(rig):
    lines = rig.run(free_bytes=lambda _p: 100)
    assert lines[-1].startswith("ERROR"), lines
    assert rig.opens == []
    assert not rig.part.exists()


def test_extract_failure_keeps_the_downloaded_zip(rig, monkeypatch):
    real = zipfetch._extract_one

    def boom(zf, node, dest):
        if node.name == "game.exe":
            raise OSError("disk full")
        return real(zf, node, dest)

    monkeypatch.setattr(zipfetch, "_extract_one", boom)
    lines = rig.run()
    assert lines[-1].startswith("ERROR"), lines
    assert (rig.local / "Game.zip").exists()
    # the next run reuses it without touching Telegram
    monkeypatch.setattr(zipfetch, "_extract_one", real)
    rig.opens.clear()
    lines = rig.run(skip_existing=True)
    assert lines[-1].startswith("OK "), lines
    assert rig.opens == []
    assert_extracted(rig)
    assert not (rig.local / "Game.zip").exists()


def test_short_download_is_an_error_and_keeps_the_part(rig):
    full = rig.data
    rig.resolver.open_remote = lambda e: Reader(full[:-10], [])
    lines = rig.run()
    assert lines[-1].startswith("ERROR"), lines
    assert rig.part.exists() and not (rig.local / "Game.zip").exists()


def test_completion_log_has_every_field_and_no_credentials(rig, caplog):
    with caplog.at_level(logging.INFO, logger="fetchlocal"):
        rig.run()
    line = next(r.getMessage() for r in caplog.records if "download complete" in r.getMessage())
    for field in ("files=3", "bytes=", "mode=zip", "download_ms=", "extract_ms=", "total_ms=", "MiB/s="):
        assert field in line, line
    assert "eyJ" not in line and "session" not in line.lower()


def test_fetch_segments_routes_archive_root_whole_and_subfolder_per_member(tmp_path, monkeypatch):
    import bridge
    from fetchlocal import LocalFetcher

    rig = Rig(tmp_path, make_zip())
    cfg = SimpleNamespace(local_dir=rig.local)
    calls = []
    monkeypatch.setattr(zipfetch, "fetch_archive", lambda **kw: iter(calls.append(kw) or ["OK x"]))
    root = rig.view.root
    opened = []
    rig.view.open = lambda n: opened.append(n.name) or io.BytesIO(MEMBERS[n.zip_name])

    def fetch(node):
        loc = bridge.Loc(bridge.ZIPDIR, entry=rig.entry, view=rig.view, node=node)
        resolver = SimpleNamespace(resolve=lambda segs: loc)
        return list(LocalFetcher(cfg, resolver).fetch_segments(["game", "Game"]))

    assert fetch(root)[-1] == "OK x" and len(calls) == 1 and opened == []
    calls.clear()
    lines = fetch(root.children["bin"])
    assert lines[-1].startswith("OK ") and calls == [] and opened == ["game.exe"]
