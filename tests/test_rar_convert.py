"""The warm-up converts RARs already uploaded to /game into stored zips.

gamestage converts a .rar *dropped into* /game. The ones uploaded before that
existed (or from the web) are found by the background sweep: downloaded into
staging, handed to the same conversion, and the original trashed once the zip
is on the drive and readable.
"""

from __future__ import annotations

import io
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rarconvert  # noqa: E402
import warmup  # noqa: E402
from config import load_config  # noqa: E402
from tdapi import Entry  # noqa: E402


def _entry(file_id, name, *, is_dir=False, size=0, parent="g"):
    return Entry(file_id=file_id, name=name, is_dir=is_dir, size=size, mtime=0,
                 message_id=1)


class Api:
    def __init__(self, children):
        self.children = {c.name: c for c in children}
        self.trashed = []
        self.fresh_calls = 0

    def resolve(self, segments):
        assert segments == ["game"]
        return _entry("g", "game", is_dir=True, parent=None)

    def children_by_name(self, parent_id, *, fresh=False):
        assert parent_id == "g"
        self.fresh_calls += fresh
        return dict(self.children)

    def total_size(self, entry):
        return entry.size

    def trash(self, file_id, parent_id):
        self.trashed.append((file_id, parent_id))
        self.children = {n: e for n, e in self.children.items() if e.file_id != file_id}


class Stager:
    def __init__(self):
        self.touched = []

    def touch(self, name):
        self.touched.append(name)


class Reader(io.BytesIO):
    """A remote file: records where reading started and how much was read."""

    def __init__(self, data, log):
        super().__init__(data)
        self.log = log

    def seek(self, pos, whence=0):
        self.log.append(("seek", pos))
        return super().seek(pos, whence)

    def read(self, n=-1):
        data = super().read(n)
        self.log.append(("read", len(data)))
        return data


@pytest.fixture
def rig(tmp_path):
    cfg = SimpleNamespace(
        game_folder="game", staging_dir=tmp_path / "staging", cache_dir=tmp_path / "meta",
    )
    cfg.staging_dir.mkdir()
    cfg.cache_dir.mkdir()
    blobs = {"r1": b"Rar!" + bytes(range(256)) * 300}
    log = []
    verified = {}

    def build(children, **kw):
        api = Api(children)
        stager = Stager()
        conv = rarconvert.RarConverter(
            cfg, api, stager,
            open_reader=lambda e: Reader(blobs[e.file_id], log),
            zip_has_files=lambda e: verified.get(e.name, True),
            wait_quiet=lambda: log.append(("quiet",)),
            chunk=10_000,
            **kw,
        )
        return SimpleNamespace(api=api, stager=stager, conv=conv)

    return SimpleNamespace(cfg=cfg, blobs=blobs, log=log, verified=verified, build=build)


def _state(rig):
    path = rig.cfg.cache_dir / "rar-convert.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def test_a_pending_rar_is_downloaded_into_staging_and_recorded(rig):
    rar = _entry("r1", "Title.rar", size=len(rig.blobs["r1"]))
    t = rig.build([rar])

    t.conv.run()

    assert (rig.cfg.staging_dir / "Title.rar").read_bytes() == rig.blobs["r1"]
    assert t.stager.touched == ["Title.rar"]
    assert _state(rig) == {"r1": {"name": "Title.rar"}}
    assert t.api.trashed == []  # nothing is deleted until the zip exists
    # chunked, yielding to foreground requests between chunks
    assert rig.log.count(("quiet",)) >= len(rig.blobs["r1"]) // 10_000
    assert list((rig.cfg.staging_dir / ".convert").iterdir()) == []


def test_a_rar_with_an_existing_zip_sibling_that_we_did_not_make_is_left_alone(rig):
    rar = _entry("r1", "Title.rar", size=len(rig.blobs["r1"]))
    t = rig.build([rar, _entry("z1", "Title.zip", size=10)])

    t.conv.run()

    assert not (rig.cfg.staging_dir / "Title.rar").exists()
    assert t.api.trashed == []


def test_a_rar_already_in_staging_is_not_downloaded_again(rig):
    rar = _entry("r1", "Title.rar", size=len(rig.blobs["r1"]))
    (rig.cfg.staging_dir / "Title.rar").write_bytes(b"being converted")
    t = rig.build([rar])

    t.conv.run()

    assert rig.log == []
    assert (rig.cfg.staging_dir / "Title.rar").read_bytes() == b"being converted"


def test_an_interrupted_download_resumes_where_it_stopped(rig):
    data = rig.blobs["r1"]
    rar = _entry("r1", "Title.rar", size=len(data))
    part = rig.cfg.staging_dir / ".convert" / "r1.part"
    part.parent.mkdir()
    part.write_bytes(data[:30_000])
    t = rig.build([rar])

    t.conv.run()

    assert ("seek", 30_000) in rig.log
    assert sum(n for kind, *n in rig.log if kind == "read" for n in n) == len(data) - 30_000
    assert (rig.cfg.staging_dir / "Title.rar").read_bytes() == data


def test_a_stop_mid_download_keeps_the_partial_file(rig):
    data = rig.blobs["r1"]
    rar = _entry("r1", "Title.rar", size=len(data))
    t = rig.build([rar])
    reads = []

    def quiet_then_stop():
        reads.append(1)
        if len(reads) == 3:
            t.conv.stop()

    t.conv._wait_quiet = quiet_then_stop

    t.conv.run()

    part = rig.cfg.staging_dir / ".convert" / "r1.part"
    assert 0 < part.stat().st_size < len(data)
    assert not (rig.cfg.staging_dir / "Title.rar").exists()
    assert _state(rig) == {}


def test_a_short_download_is_not_staged(rig):
    rar = _entry("r1", "Title.rar", size=len(rig.blobs["r1"]) + 5)  # remote claims more
    t = rig.build([rar])

    t.conv.run()

    assert not (rig.cfg.staging_dir / "Title.rar").exists()
    assert t.stager.touched == []


def test_the_original_is_trashed_once_its_zip_is_on_the_drive_and_readable(rig):
    rar = _entry("r1", "Title.rar", size=len(rig.blobs["r1"]))
    t = rig.build([rar])
    t.conv.run()  # staged
    (rig.cfg.staging_dir / "Title.rar").unlink()  # gamestage converted + uploaded it
    t.api.children["Title.zip"] = _entry("z1", "Title.zip", size=100)

    t.conv.run()

    assert t.api.trashed == [("r1", "g")]
    assert _state(rig) == {}


def test_finished_conversions_are_cleaned_up_between_downloads(rig):
    """A pass downloads every pending rar; one that finished converting while
    the next was downloading is trashed then, not a whole pass (hours) later."""
    rig.blobs["r2"] = b"Rar!" + bytes(50_000)
    first = _entry("r1", "A.rar", size=len(rig.blobs["r1"]))
    second = _entry("r2", "B.rar", size=len(rig.blobs["r2"]))
    t = rig.build([first, second])

    def converted(name):
        # gamestage extracts, packs and uploads A while B is downloading
        if name == "A.rar":
            (rig.cfg.staging_dir / "A.rar").unlink()
            t.api.children["A.zip"] = _entry("za", "A.zip", size=100)

    t.stager.touch = lambda name: (t.stager.touched.append(name), converted(name))

    t.conv.run()

    assert t.api.trashed == [("r1", "g")]
    assert _state(rig) == {"r2": {"name": "B.rar"}}


def test_the_original_stays_while_the_zip_is_missing_or_unreadable(rig):
    rar = _entry("r1", "Title.rar", size=len(rig.blobs["r1"]))
    t = rig.build([rar])
    t.conv.run()
    (rig.cfg.staging_dir / "Title.rar").unlink()

    t.conv.run()  # no zip yet (still packing, or the unit failed)
    assert t.api.trashed == []

    t.api.children["Title.zip"] = _entry("z1", "Title.zip", size=100)
    rig.verified["Title.zip"] = False
    t.conv.run()
    assert t.api.trashed == []
    assert _state(rig) == {"r1": {"name": "Title.rar"}}


def test_a_rar_removed_behind_our_back_drops_its_record(rig):
    rar = _entry("r1", "Title.rar", size=len(rig.blobs["r1"]))
    t = rig.build([rar])
    t.conv.run()
    (rig.cfg.staging_dir / "Title.rar").unlink()
    del t.api.children["Title.rar"]

    t.conv.run()

    assert _state(rig) == {}
    assert t.api.trashed == []


def test_listing_is_fresh_so_new_uploads_are_seen(rig):
    t = rig.build([])
    t.conv.run()
    assert t.api.fresh_calls >= 1


def test_the_background_warmup_runs_the_converter_before_the_sweep(monkeypatch):
    calls = []

    class Conv:
        def run(self):
            calls.append("convert")

        def stop(self):
            calls.append("stop")

    class FakeWarmer:
        def __init__(self, *a, **k):
            pass

        def pending(self):
            calls.append("sweep")
            return [], []

        def shell_warm(self, files):
            return 0

    monkeypatch.setattr(warmup, "Warmer", FakeWarmer)
    resolver = SimpleNamespace(clear_heads=lambda: None)
    bg = warmup.BackgroundWarmup(resolver, converter=Conv())

    bg._pass()
    bg.stop()

    assert calls[:2] == ["convert", "sweep"]
    assert "stop" in calls


def test_conversion_can_be_switched_off(tmp_path):
    ini = tmp_path / "config.ini"
    base = (
        "[telegram]\napi_id = 1\napi_hash = h\nsession = s\n"
        "[teledrive]\nbase_url = http://x\n"
    )
    ini.write_text(base, encoding="utf-8")
    assert load_config(ini).warmup_convert_rar is True
    ini.write_text(base + "[warmup]\nconvert_rar = false\n", encoding="utf-8")
    assert load_config(ini).warmup_convert_rar is False
