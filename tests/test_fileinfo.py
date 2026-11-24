"""``fileinfo.lookup``: what a file id turns out to be, without saying what it is called."""
import json
import re
from pathlib import Path

import pytest

import fileid
import fileinfo
from fileid import path_id

MARK = "PRIVATE-名前-123.jpg"
FOLDER = "PRIVATE-folder"
GB = 1 << 30


def _row(file_id, name, parent=None, *, is_dir=False, size=0, user=0, **extra):
    row = {
        "file_id": file_id, "filename": name, "isDir": is_dir, "filesize": size,
        "created_at": "2026-07-30T12:00:00", "mime_type": None, "telegram_message_id": 0,
        "access_hash": "", "is_split_file": False, "split_group_id": None, "file_hash": None,
        "has_thumbnail": False, "telegram_user_id": user, "parent_id": parent,
    }
    row.update(extra)
    return row


def _write_dir(meta, key, rows):
    d = meta / "dirs"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{key}.json").write_text(json.dumps({"v": 2, "at": 1.0, "rows": rows}), encoding="utf-8")


@pytest.fixture
def drive(tmp_path):
    """data root with a small cached drive: a folder, a photo, a split movie, a packed game."""
    meta = tmp_path / "data" / "meta"
    meta.mkdir(parents=True)
    root = [
        _row("F1", FOLDER, is_dir=True),
        _row("G1", "game", is_dir=True),
        _row("M1", "movie.mkv", size=2 * GB + 1234, is_split_file=True, split_group_id="sg",
             telegram_message_id=11, telegram_user_id=222, mime_type="video/x-matroska",
             file_hash="abcdef:" + str(2 * GB)),
    ]
    _write_dir(meta, "__root__", root)
    photo = _row("P1", MARK, "F1", size=524288, telegram_message_id=12, telegram_user_id=111,
                 mime_type="image/jpeg", file_hash="deadbeef:524000", has_thumbnail=True)
    twin = _row("P2", MARK, "F1", size=100, telegram_message_id=13, telegram_user_id=111)
    _write_dir(meta, "F1", [photo, twin])
    zip_row = _row("Z1", "MyGame.zip", "G1", size=999, telegram_message_id=14, telegram_user_id=111,
                   mime_type="application/zip", file_hash="cafe:999")
    _write_dir(meta, "G1", [zip_row])
    tree = {"n": "MyGame", "d": True, "ch": {"readme.txt": {"n": "readme.txt", "d": False, "s": 5, "z": "readme.txt"}}}
    (meta / "zips").mkdir()
    (meta / "zips" / "111-Z1.json").write_text(json.dumps({**tree, "v": 1}), encoding="utf-8")
    (meta / "media_props.json").write_text(
        json.dumps({"111-P1": {"width": 1920, "height": 1080}}), encoding="utf-8")
    (meta / "thumbs").mkdir()
    (meta / "thumbs" / "111-P1.jpg").write_bytes(b"jpg")
    return meta


def _lookup(meta, path, **kw):
    return fileinfo.lookup(path_id(path), meta, primary_user_id=111, account_ids=(111, 222), **kw)


def test_general_file_reports_format_size_media_and_cache(drive):
    info = _lookup(drive, f"{FOLDER}/{MARK}")
    assert info["format"] == {"extension": ".jpg", "mime": "image/jpeg"}
    assert info["size"]["backend_minus_real_bytes"] == 524288 - 524000
    assert re.fullmatch(r"~[\d.]+ (KiB|MiB)", info["size"]["approx"])
    assert info["media"] == {"width": 1920, "height": 1080}
    assert info["location"] == {"kind": "general", "depth": 2}
    assert info["storage"] == {"kind": "single", "accounts": ["primary"]}
    assert info["state"]["cache"] == {"thumbnail": True, "props": True, "has_thumbnail_flag": True}
    assert info["state"]["same_name_rows"] == 2
    assert info["state"]["created"].startswith("2026-07-30")


def test_split_file_reports_cross_account_storage(drive):
    info = _lookup(drive, "movie.mkv")
    assert info["storage"]["kind"] == "split"
    assert info["storage"]["accounts"] == ["#2"]
    assert info["location"]["depth"] == 1


def test_packed_zip_and_its_member(drive):
    zip_info = _lookup(drive, "game/MyGame")  # the archive is a folder on H:
    assert _lookup(drive, "game/MyGame.zip") is None  # there is no such path on H:
    assert zip_info["location"]["kind"] == "game-packed"
    member = _lookup(drive, "game/MyGame/readme.txt")
    assert member["location"] == {"kind": "zip-member", "depth": 3}
    assert member["format"]["extension"] == ".txt"


def test_local_staging_and_uploads_are_found(drive):
    data = drive.parent
    (data / "uploads" / FOLDER).mkdir(parents=True)
    (data / "uploads" / FOLDER / "new.bin").write_bytes(b"x" * 10)
    (data / "staging" / "NewGame").mkdir(parents=True)
    (data / "staging" / "NewGame" / "run.exe").write_bytes(b"x")
    (drive / "upload-queue.json").write_text(json.dumps({"version": 1, "pending": {
        f"{FOLDER}/new.bin": {"stage": "failed", "attempts": 2, "detail": "boom"}}}), encoding="utf-8")
    up = _lookup(drive, f"{FOLDER}/new.bin")
    assert up["location"]["kind"] == "general"
    assert up["state"]["upload"] == {"stage": "failed", "attempts": 2, "abandoned": False}
    stage = _lookup(drive, "game/NewGame/run.exe")
    assert stage["location"]["kind"] == "game-staging"
    assert stage["state"]["upload"]["stage"] == "staged"


def test_unknown_id_is_none_and_never_calls_a_backend(drive):
    class Boom:
        def __getattr__(self, name):  # any attribute access means a backend call
            raise AssertionError("backend was called")

    assert _lookup(drive, "no/such/file.bin", api=Boom()) is None


FORBIDDEN = ["PRIVATE", "名前", "deadbeef", "abcdef", "cafe", "P1", "P2", "F1", "M1", "Z1", "G1"]


def test_output_has_no_names_hashes_backend_ids_message_ids_or_user_ids_or_exact_sizes(drive):
    blob = json.dumps(
        [_lookup(drive, f"{FOLDER}/{MARK}"), _lookup(drive, "movie.mkv"),
         _lookup(drive, "game/MyGame.zip"), _lookup(drive, "game/MyGame/readme.txt")],
        ensure_ascii=False,
    )
    for bad in FORBIDDEN:
        assert bad not in blob, bad
    for number in ("111", "222", str(2 * GB + 1234), "524288", "524000", "11", "12", "14"):
        assert not re.search(rf"(?<![\d.]){number}(?![\d.])", blob), number


def test_cli_info_prints_json_for_a_known_id(drive, capsys, monkeypatch):
    monkeypatch.setattr(fileinfo, "_load_cache_dir", lambda: drive)
    monkeypatch.setattr(fileinfo, "_load_accounts", lambda: (111, (111, 222)))
    assert fileid.main(["info", "0x%016x" % path_id("movie.mkv")]) == 0
    assert json.loads(capsys.readouterr().out)["storage"]["kind"] == "split"
    assert fileid.main(["info", "0x%016x" % path_id("nope")]) == 1


# --------------------------------------------------------------------------- #
# /rpc/fileinfo (needs the running bridge's rig)
# --------------------------------------------------------------------------- #

from test_bridge_e2e import _upload_now, rig  # noqa: E402,F401


def test_rpc_fileinfo_adds_live_upload_state(rig):
    assert rig.request("PUT", "/pending-thing.bin", data=b"x" * 400).status_code == 201
    target = "0x%016x" % path_id("pending-thing.bin")
    body = rig.request("GET", "/rpc/fileinfo", params={"id": target})
    assert body.status_code == 200
    info = body.json()
    assert info["location"]["kind"] == "general"
    assert info["state"]["upload"]["stage"] == "staging"
    assert "pending-thing" not in body.text


def test_rpc_fileinfo_rejects_bad_ids_and_unknown_ones(rig):
    assert rig.request("GET", "/rpc/fileinfo", params={"id": "nope"}).status_code == 400
    assert rig.request("GET", "/rpc/fileinfo").status_code == 400
    assert rig.request("GET", "/rpc/fileinfo", params={"id": "0x1234"}).status_code == 404
