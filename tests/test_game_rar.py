"""A .rar dropped into /game is converted to a stored zip before upload.

zipfs can serve one member out of a stored zip by byte range; nothing in this
project can do that for RAR (no central directory, and no Python decoder for
compressed members). So a RAR is extracted with 7-Zip at pack time and goes
through the same ZIP_STORED packing as a folder would.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gamestage  # noqa: E402
from config import Config, load_config  # noqa: E402
from transfer_models import TransferResult, UploadedPart  # noqa: E402


class RecordingEngine:
    def __init__(self):
        self.requests = []
        self.members = []

    def transfer(self, request):
        self.requests.append(request)
        if request.mime_type == "application/zip" and zipfile.is_zipfile(request.source):
            with zipfile.ZipFile(request.source) as zf:
                self.members = [(i.filename, i.compress_type, zf.read(i)) for i in zf.infolist()]
        return TransferResult(request, "fp", (
            UploadedPart(0, 900, "doc", "ah", request.logical_size, 1),
        ))

    def register_result(self, result):
        pass


class Api:
    def ensure_folder(self, name):
        return SimpleNamespace(file_id=f"id-{name}")


@pytest.fixture
def game(tmp_path):
    cfg = Config(
        api_id=1, api_hash="hash", session="",
        base_url="http://backend", game_folder="game", dir_cache_seconds=60.0,
        host="127.0.0.1", port=0, mount_drive="E:", log_level="WARNING",
        cache_dir=tmp_path / "cache", local_dir=tmp_path / "local",
        staging_dir=tmp_path / "staging", upload_dir=tmp_path / "uploads",
        debounce_minutes=0.0, seven_zip="7z-test",
    )
    for path in (cfg.cache_dir, cfg.local_dir, cfg.staging_dir, cfg.pack_dir, cfg.upload_dir):
        path.mkdir(parents=True, exist_ok=True)
    engine = RecordingEngine()
    return SimpleNamespace(cfg=cfg, engine=engine, stager=gamestage.GameStager(cfg, Api(), engine))


def _fake_extract(calls):
    def extract(archive, dest, seven_zip):
        calls.append((Path(archive).name, seven_zip))
        (Path(dest) / "Game" / "data").mkdir(parents=True)
        (Path(dest) / "Game" / "run.exe").write_bytes(b"MZ" * 50)
        (Path(dest) / "Game" / "data" / "a.bin").write_bytes(b"payload")
    return extract


def test_a_rar_is_extracted_and_uploaded_as_a_stored_zip(game, monkeypatch):
    calls = []
    monkeypatch.setattr(gamestage, "extract_archive", _fake_extract(calls))
    (game.cfg.staging_dir / "Title.RAR").write_bytes(b"Rar!\x1a\x07\x01\x00")
    game.stager.touch("Title.RAR")

    game.stager._process("Title.RAR")

    assert calls == [("Title.RAR", "7z-test")]
    request = game.engine.requests[0]
    assert request.upload_name == "Title.zip"
    assert request.mime_type == "application/zip"
    assert request.allow_album is False
    files = {name: (ctype, data) for name, ctype, data in game.engine.members if not name.endswith("/")}
    assert files == {
        "Game/run.exe": (zipfile.ZIP_STORED, b"MZ" * 50),
        "Game/data/a.bin": (zipfile.ZIP_STORED, b"payload"),
    }
    # Done: the staged rar, the extraction and the temporary zip are all gone.
    assert list(game.cfg.staging_dir.iterdir()) == [game.cfg.pack_dir]
    assert list(game.cfg.pack_dir.iterdir()) == []


def test_a_failed_extraction_keeps_the_rar_and_reports_it(game, monkeypatch):
    def broken(archive, dest, seven_zip):
        (Path(dest) / "partial.bin").write_bytes(b"half")
        raise gamestage.ArchiveExtractError("7-Zip exited with 2: Wrong password")

    monkeypatch.setattr(gamestage, "extract_archive", broken)
    rar = game.cfg.staging_dir / "Locked.rar"
    rar.write_bytes(b"Rar!\x1a\x07\x01\x00")
    game.stager.touch("Locked.rar")

    game.stager._process("Locked.rar")

    assert game.engine.requests == []
    assert rar.read_bytes() == b"Rar!\x1a\x07\x01\x00"
    unit = game.stager.status()["units"][0]
    assert unit["state"] == "failed"
    assert "Wrong password" in unit["detail"]
    assert list(game.cfg.pack_dir.iterdir()) == []  # no half extraction left behind


def test_other_top_level_files_are_still_uploaded_verbatim(game, monkeypatch):
    monkeypatch.setattr(gamestage, "extract_archive", _fake_extract([]))
    (game.cfg.staging_dir / "Own.zip").write_bytes(b"PK\x03\x04" + b"\0" * 40)
    game.stager.touch("Own.zip")

    game.stager._process("Own.zip")

    assert game.engine.requests[0].upload_name == "Own.zip"
    # Cleared once uploaded -- a file left in staging is re-adopted and
    # re-uploaded on the next tick.
    assert not (game.cfg.staging_dir / "Own.zip").exists()
    assert game.stager._due(0) == []


def test_seven_zip_path_defaults_and_can_be_overridden(tmp_path):
    ini = tmp_path / "config.ini"
    base = (
        "[telegram]\napi_id = 1\napi_hash = h\nsession = s\n"
        "[teledrive]\nbase_url = http://x\n"
    )
    ini.write_text(base, encoding="utf-8")
    assert load_config(ini).seven_zip == r"C:\Program Files\7-Zip\7z.exe"
    ini.write_text(base + "[game]\nseven_zip = D:\\tools\\7z.exe\n", encoding="utf-8")
    assert load_config(ini).seven_zip == r"D:\tools\7z.exe"


# --------------------------------------------------------------------------- #
# the real 7-Zip, when this machine has one
# --------------------------------------------------------------------------- #

_SEVEN_ZIP = next(
    (p for p in (r"C:\Program Files\7-Zip\7z.exe", shutil.which("7z")) if p and Path(p).exists()),
    None,
)
needs_7z = pytest.mark.skipif(_SEVEN_ZIP is None, reason="7-Zip not installed")


@needs_7z
def test_extract_archive_runs_seven_zip(tmp_path):
    # 7-Zip detects the format from the bytes, not the extension, so a zip
    # named .rar exercises the same command line a real RAR does.
    archive = tmp_path / "t.rar"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("dir/é.txt", "hello")
    dest = tmp_path / "out"
    dest.mkdir()

    gamestage.extract_archive(archive, dest, _SEVEN_ZIP)

    assert (dest / "dir" / "é.txt").read_text(encoding="utf-8") == "hello"


def _vint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _rar5_block(body: bytes, data: bytes = b"") -> bytes:
    import zlib

    header = _vint(len(body)) + body
    return zlib.crc32(header).to_bytes(4, "little") + header + data


def _tiny_rar5(files: dict) -> bytes:
    """A minimal real RAR5 archive with stored (uncompressed) members.

    No RAR encoder is available here (7-Zip reads RAR but cannot write it),
    so the format is assembled by hand: signature, main header, one file
    header + data per member, end-of-archive header.
    """
    import zlib

    out = b"Rar!\x1a\x07\x01\x00"
    out += _rar5_block(_vint(1) + _vint(0) + _vint(0))  # main: type, flags, archive flags
    for name, data in files.items():
        raw = name.encode("utf-8")
        body = (
            _vint(2) + _vint(0x0002) + _vint(len(data))       # file header, has data area
            + _vint(0x0004) + _vint(len(data)) + _vint(0x20)  # CRC present, size, attributes
            + zlib.crc32(data).to_bytes(4, "little")
            + _vint(0) + _vint(0)                              # stored, host OS Windows
            + _vint(len(raw)) + raw
        )
        out += _rar5_block(body, data)
    out += _rar5_block(_vint(5) + _vint(0) + _vint(0))  # end of archive
    return out


@needs_7z
def test_a_real_rar_goes_all_the_way_to_a_stored_zip(game):
    import dataclasses

    game.stager.cfg = dataclasses.replace(game.cfg, seven_zip=_SEVEN_ZIP)
    payload = {"Game/run.exe": b"MZ" * 300, "Game/音声/voice.ogg": b"OggS" + bytes(500)}
    (game.cfg.staging_dir / "Real.rar").write_bytes(_tiny_rar5(payload))
    game.stager.touch("Real.rar")

    game.stager._process("Real.rar")

    assert game.engine.requests[0].upload_name == "Real.zip"
    files = {n: d for n, t, d in game.engine.members if not n.endswith("/")}
    assert files == payload
    assert all(t == zipfile.ZIP_STORED for _, t, _ in game.engine.members)


@needs_7z
def test_an_encrypted_archive_fails_instead_of_waiting_for_a_password(tmp_path):
    src = tmp_path / "secret.txt"
    src.write_text("x")
    archive = tmp_path / "locked.7z"
    subprocess.run(
        [_SEVEN_ZIP, "a", "-pP4ss", "-mhe=on", str(archive), str(src)],
        check=True, capture_output=True,
    )
    dest = tmp_path / "out"
    dest.mkdir()

    with pytest.raises(gamestage.ArchiveExtractError):
        gamestage.extract_archive(archive, dest, _SEVEN_ZIP)


def test_a_missing_seven_zip_is_a_clear_error(tmp_path):
    archive = tmp_path / "t.rar"
    archive.write_bytes(b"Rar!")
    with pytest.raises(gamestage.ArchiveExtractError, match="7-Zip not found"):
        gamestage.extract_archive(archive, tmp_path, str(tmp_path / "nope" / "7z.exe"))
