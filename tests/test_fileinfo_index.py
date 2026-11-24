"""The fileinfo index: built once, refreshed only when a listing file changes."""
import json

import pytest

import fileinfo
from fileid import path_id
from test_fileinfo import FOLDER, MARK, _lookup, _row, drive  # noqa: F401


def test_second_lookup_reads_no_listing_files(drive, monkeypatch):
    assert _lookup(drive, f"{FOLDER}/{MARK}") is not None  # builds the index
    assert _lookup(drive, "game/MyGame/readme.txt") is not None  # and the zip member ids
    reads = []
    real = fileinfo._read_json
    monkeypatch.setattr(fileinfo, "_read_json", lambda p: reads.append(p) or real(p))
    assert _lookup(drive, "movie.mkv") is not None
    assert _lookup(drive, f"{FOLDER}/{MARK}") is not None
    assert _lookup(drive, "game/MyGame/readme.txt") is not None
    touched = [p for p in reads if "dirs" in str(p) or "zips" in str(p)]
    assert not touched, touched


def test_index_notices_a_changed_listing(drive):
    assert _lookup(drive, "late-arrival.bin") is None
    root = json.loads((drive / "dirs" / "__root__.json").read_text(encoding="utf-8"))
    root["rows"].append(_row("L1", "late-arrival.bin", size=10))
    (drive / "dirs" / "__root__.json").write_text(json.dumps(root), encoding="utf-8")
    assert _lookup(drive, "late-arrival.bin") is not None


def test_index_notices_a_removed_listing(drive):
    assert _lookup(drive, f"{FOLDER}/{MARK}") is not None
    (drive / "dirs" / "F1.json").unlink()
    assert _lookup(drive, f"{FOLDER}/{MARK}") is None


def test_index_notices_a_changed_zip_tree(drive):
    assert _lookup(drive, "game/MyGame/added.txt") is None
    tree = json.loads((drive / "zips" / "111-Z1.json").read_text(encoding="utf-8"))
    tree["ch"]["added.txt"] = {"n": "added.txt", "d": False, "s": 1, "z": "added.txt"}
    (drive / "zips" / "111-Z1.json").write_text(json.dumps(tree) + " ", encoding="utf-8")
    assert _lookup(drive, "game/MyGame/added.txt") is not None
