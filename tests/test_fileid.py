"""File IDs: the one definition shared by bridge.log, rclone.log and Windows.

The vectors were produced by running the same FNV-64a / clear-top-bit / 0->1
logic as tools/rclone-stableino/stable_ino.go under Go. If these change, every
thumbnail Windows cached for the mount is orphaned.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from teledrive_webdav.core import fileid
from teledrive_webdav.core.fileid import UNKNOWN_TAG, path_id, tag, tag_dav, tag_drive, tag_local

VECTORS = {
    "": 0x4BF29CE484222325,
    "a.txt": 0x7ED582B5571BBD5A,
    "Hololive/a/00000.m2ts": 0x53FBFF8F92ACD838,
    "日本語/名前.jpg": 0x152DC4F5A7F60E1C,
    "with space/x y.png": 0x6EA1F2F292AC0CFA,
    "game/a.zip/inner/f.bin": 0x10166415923EFBDF,
    "name：full.jpg": 0x72BD39366A4A7069,
    "trail .txt": 0x76ACEE19A2E3427F,
    "e\u0301.jpg": 0x70AD05DABFA61EF2,  # NFD: must not be normalised
}


@pytest.mark.parametrize("path,expected", sorted(VECTORS.items()))
def test_path_id_matches_go_vectors(path, expected):
    assert path_id(path) == expected


def test_tag_format():
    assert tag("a.txt") == "id=0x%016x" % path_id("a.txt")
    assert tag("a.txt") == "id=0x7ed582b5571bbd5a"


def test_unknown_tag_is_not_a_message_id():
    assert UNKNOWN_TAG == "id=?"


def test_tag_dav_strips_slash_and_unquotes():
    assert tag_dav("/%E6%97%A5/x.jpg") == tag("日/x.jpg")
    assert tag_dav("/") == tag("")
    assert tag_dav("a/b") == tag("a/b")


def test_tag_drive_accepts_drive_paths():
    assert tag_drive("H:\\a\\b.jpg") == tag("a/b.jpg")
    assert tag_drive("h:\\a\\b.jpg") == tag("a/b.jpg")
    assert tag_drive("H:\\") == tag("")
    assert tag_drive("\\\\?\\H:\\a\\b.jpg") == tag("a/b.jpg")
    assert tag_drive("C:\\a\\b.jpg") is None


@pytest.fixture
def cache(tmp_path):
    return tmp_path / "cache"


@pytest.mark.parametrize(
    "rel,expected",
    [
        ("uploads\\a\\b.jpg", "a/b.jpg"),
        ("staging\\top\\x\\y", "game/top/x/y"),
        ("staging\\.pack\\top.zip", "game/top"),
        ("staging\\.pack\\top.extract\\inner\\f", "game/top"),
        ("rclone\\vfs\\teledrive\\a\\b", "a/b"),
        ("rclone\\vfsMeta\\teledrive\\a\\b", "a/b"),
        ("local\\g\\x", "g/x"),
        ("rejected\\a\\b", "a/b"),
    ],
)
def test_tag_local_mappings(cache, rel, expected):
    assert tag_local(str(cache) + "\\" + rel, cache) == tag(expected)


def test_tag_local_convert_part_uses_given_unit(cache):
    p = str(cache) + "\\staging\\.convert\\123.part"
    assert tag_local(p, cache, unit="top") == tag("game/top")
    assert tag_local(p, cache) is None


@pytest.mark.parametrize(
    "rel",
    ["savedata-backups\\g\\20260101_000000\\f", "meta\\x.json", "uploads", "staging"],
)
def test_tag_local_unmapped_returns_none(cache, rel):
    assert tag_local(str(cache) + "\\" + rel, cache) is None


def test_tag_local_outside_cache_is_none(cache):
    assert tag_local("C:\\other\\f", cache) is None


def test_tag_local_is_case_insensitive_and_accepts_extended_prefix(cache):
    p = "\\\\?\\" + str(cache).upper() + "\\UPLOADS\\a\\b.jpg"
    assert tag_local(p, cache) == tag("a/b.jpg")


def test_cli_tag():
    out = subprocess.run(
        [sys.executable, str(Path(fileid.__file__)), "tag", "H:\\a\\b.jpg"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == tag("a/b.jpg")


def test_game_tag_uses_the_name_the_drive_shows():
    # On H:, game/Foo.zip is presented as the folder game/Foo (GameRootCollection
    # strips the .zip), so that is the path rclone hashes. A .rar stays a file.
    assert fileid.game_tag("Foo.zip") == tag("game/Foo")
    assert fileid.game_tag("Foo.ZIP") == tag("game/Foo")
    assert fileid.game_tag("x.rar") == tag("game/x.rar")
    assert fileid.game_tag("run.exe") == tag("game/run.exe")
    assert fileid.game_tag("Foo", folder="g") == tag("g/Foo")
