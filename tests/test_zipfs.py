"""Offline tests for the virtual zip expansion.

A real store-mode zip is built locally, then served through a counting seekable
object. That lets the tests assert the central claim of the design: browsing and
reading one member never downloads the whole archive.
"""

import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import zipfs  # noqa: E402
from tdapi import JsonStore  # noqa: E402

CONTENTS = {
    "bin/game.exe": b"MZ" + bytes(range(256)) * 40,
    "bin/data/pak0.pak": bytes((i * 7) % 251 for i in range(50_000)),
    "readme.txt": "遊戲說明 — non-ASCII name test\n".encode("utf-8"),
    "資料/中文檔名.dat": b"\x00\x01\x02" * 1000,
    "deep/a/b/c/leaf.bin": b"leaf",
}
EMPTY_DIRS = ["empty/", "deep/a/empty2/"]


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "MyGame.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for name in EMPTY_DIRS:
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o040755 << 16) | 0x10
            zf.writestr(info, b"")
        for name, blob in CONTENTS.items():
            zf.writestr(name, blob)
    return path


class CountingStream(io.RawIOBase):
    """Seekable view over bytes that records how much was actually read."""

    total_read = 0

    def __init__(self, data, stats):
        super().__init__()
        self._buf = io.BytesIO(data)
        self._stats = stats

    def readable(self):
        return True

    def seekable(self):
        return True

    def seek(self, offset, whence=io.SEEK_SET):
        return self._buf.seek(offset, whence)

    def tell(self):
        return self._buf.tell()

    def read(self, size=-1):
        data = self._buf.read(size)
        self._stats["read"] += len(data)
        return data

    def readinto(self, buf):
        data = self.read(len(buf))
        buf[: len(data)] = data
        return len(data)


@pytest.fixture
def view(archive):
    data = archive.read_bytes()
    stats = {"read": 0, "opens": 0}

    def opener():
        stats["opens"] += 1
        return CountingStream(data, stats)

    return zipfs.ZipView(opener, name="MyGame"), stats, data


# --------------------------------------------------------------------------- #
# tree building
# --------------------------------------------------------------------------- #


def test_build_tree_structure(archive):
    with zipfile.ZipFile(archive) as zf:
        root = zipfs.build_tree(zf.infolist(), root_name="MyGame")
    assert set(root.children) == {"bin", "readme.txt", "資料", "deep", "empty"}
    assert root.children["bin"].is_dir
    assert set(root.children["bin"].children) == {"game.exe", "data"}
    assert root.children["readme.txt"].size == len(CONTENTS["readme.txt"])
    assert root.children["資料"].children["中文檔名.dat"].size == len(CONTENTS["資料/中文檔名.dat"])


def test_build_tree_keeps_empty_directories(archive):
    with zipfile.ZipFile(archive) as zf:
        root = zipfs.build_tree(zf.infolist())
    assert root.children["empty"].is_dir
    assert root.children["empty"].children == {}
    assert root.children["deep"].children["a"].children["empty2"].is_dir


def test_build_tree_creates_implicit_directories():
    infos = [zipfile.ZipInfo("a/b/c.txt")]
    root = zipfs.build_tree(infos)
    assert root.children["a"].is_dir and root.children["a"].children["b"].is_dir
    assert not root.children["a"].children["b"].children["c.txt"].is_dir


def test_build_tree_rejects_traversal():
    infos = [zipfile.ZipInfo("../escape.txt"), zipfile.ZipInfo("ok.txt")]
    root = zipfs.build_tree(infos)
    assert set(root.children) == {"ok.txt"}


def test_lookup_and_walk(archive):
    with zipfile.ZipFile(archive) as zf:
        root = zipfs.build_tree(zf.infolist())
    assert zipfs.lookup(root, []) is root
    assert zipfs.lookup(root, ["bin", "game.exe"]).size == len(CONTENTS["bin/game.exe"])
    assert zipfs.lookup(root, ["bin", "nope"]) is None
    assert zipfs.lookup(root, ["readme.txt", "x"]) is None
    files = {rel for rel, node in zipfs.walk(root) if not node.is_dir}
    assert files == set(CONTENTS)


def test_tree_json_round_trip(archive):
    with zipfile.ZipFile(archive) as zf:
        root = zipfs.build_tree(zf.infolist(), root_name="MyGame")
    clone = zipfs.tree_from_json(zipfs.tree_to_json(root))
    original = {rel: (n.is_dir, n.size, n.header_offset) for rel, n in zipfs.walk(root)}
    copied = {rel: (n.is_dir, n.size, n.header_offset) for rel, n in zipfs.walk(clone)}
    assert original == copied
    assert clone.name == "MyGame"


# --------------------------------------------------------------------------- #
# byte-exact entry reads
# --------------------------------------------------------------------------- #


def test_local_data_offset_points_at_the_member_bytes(archive):
    data = archive.read_bytes()
    with zipfile.ZipFile(archive) as zf:
        info = zf.getinfo("bin/data/pak0.pak")
    stream = io.BytesIO(data)
    start = zipfs.local_data_offset(stream, info.header_offset)
    assert data[start : start + info.file_size] == CONTENTS["bin/data/pak0.pak"]


def test_open_entry_returns_exact_bytes(view):
    zv, _stats, _data = view
    for name, blob in CONTENTS.items():
        node = zv.lookup(name.split("/"))
        assert node is not None and not node.is_dir
        with zv.open(node) as fh:
            assert fh.read() == blob, name


def test_open_entry_supports_ranges(view):
    zv, _stats, _data = view
    blob = CONTENTS["bin/data/pak0.pak"]
    node = zv.lookup(["bin", "data", "pak0.pak"])
    with zv.open(node) as fh:
        fh.seek(1000)
        assert fh.read(500) == blob[1000:1500]
        assert fh.seek(0, io.SEEK_END) == len(blob)
        fh.seek(len(blob) - 4)
        assert fh.read(99) == blob[-4:]


def test_browsing_reads_far_less_than_the_archive(view):
    zv, stats, data = view
    names = {rel for rel, node in zv.walk() if not node.is_dir}
    assert names == set(CONTENTS)
    # Only the end-of-archive central directory should have been touched.
    assert stats["read"] < len(data) // 4, (stats["read"], len(data))


def test_reading_one_member_does_not_download_the_archive(view):
    zv, stats, data = view
    zv.root  # parse the central directory first
    before = stats["read"]
    node = zv.lookup(["deep", "a", "b", "c", "leaf.bin"])
    with zv.open(node) as fh:
        assert fh.read() == b"leaf"
    # The local header plus 4 bytes of payload — nothing like the whole archive.
    assert stats["read"] - before < 1024


def test_directory_cache_makes_browsing_free(view, tmp_path):
    zv, stats, _data = view
    store = JsonStore(tmp_path / "zip_dirs.json")
    cached = zipfs.ZipView(zv._open_stream, name="MyGame", cache=store, cache_key="file-1")
    assert cached.lookup(["readme.txt"]) is not None
    opens_after_first = stats["opens"]

    reopened = zipfs.ZipView(zv._open_stream, name="MyGame", cache=store, cache_key="file-1")
    assert reopened.lookup(["bin", "game.exe"]).size == len(CONTENTS["bin/game.exe"])
    assert stats["opens"] == opens_after_first  # no stream needed at all


def test_deflated_member_still_readable(tmp_path):
    path = tmp_path / "compressed.zip"
    blob = b"highly compressible " * 5000
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("note.txt", blob)
    data = path.read_bytes()
    stats = {"read": 0, "opens": 0}
    zv = zipfs.ZipView(lambda: CountingStream(data, stats), name="compressed")
    node = zv.lookup(["note.txt"])
    assert not node.stored
    with zv.open(node) as fh:
        assert fh.read() == blob
        fh.seek(10)
        assert fh.read(5) == blob[10:15]


def test_deflated_member_does_not_reparse_the_central_directory(tmp_path, monkeypatch):
    """Opening a compressed member must not re-read the archive's directory.

    Archives uploaded from the browser are ZIP_DEFLATED. Opening a member used
    to build a fresh ``zipfile.ZipFile`` every time, and every backward seek
    opened another one: each re-read the end record and central directory over
    Telegram. On the live drive the same offset was fetched ~220 times a minute
    and all 16 worker threads sat waiting on it, so even ``PROPFIND /game/``
    queued for minutes. The tree already knows where the member is.
    """
    path = tmp_path / "compressed.zip"
    blobs = {f"dir/f{i}.txt": (f"member {i} ".encode() * 4000) for i in range(3)}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, blob in blobs.items():
            zf.writestr(name, blob)
    data = path.read_bytes()
    stats = {"read": 0, "opens": 0}

    def opener():
        stats["opens"] += 1
        return CountingStream(data, stats)

    zv = zipfs.ZipView(opener, name="compressed")
    zv.root  # directory parsed once, as for browsing

    def no_zipfile(*_a, **_k):
        raise AssertionError("central directory re-parsed")

    monkeypatch.setattr(zipfs.zipfile, "ZipFile", no_zipfile)
    for name, blob in blobs.items():
        node = zv.lookup(name.split("/"))
        assert not node.stored
        with zv.open(node) as fh:
            assert fh.read(100) == blob[:100]
            fh.seek(7)  # backward: restarts the member, not the archive
            assert fh.read(50) == blob[7:57]
            fh.seek(len(blob) - 20)
            assert fh.read() == blob[-20:]
        stats["read"] = 0
        with zv.open(node) as fh:
            fh.read(10)
        assert stats["read"] < node.compress_size + 4096


def test_truncated_deflated_member_raises_instead_of_returning_short_data(tmp_path):
    path = tmp_path / "compressed.zip"
    blob = bytes(range(256)) * 400
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("x.bin", blob)
    data = path.read_bytes()
    zv = zipfs.ZipView(lambda: CountingStream(data, {"read": 0}), name="c")
    node = zv.lookup(["x.bin"])
    node.compress_size //= 2
    with zv.open(node) as fh, pytest.raises(OSError):
        fh.read()


def test_zip_name_helpers():
    assert zipfs.is_zip_name("MyGame.zip") and zipfs.is_zip_name("A.ZIP")
    assert not zipfs.is_zip_name("MyGame.rar")
    assert zipfs.strip_zip_suffix("MyGame.zip") == "MyGame"
    assert zipfs.strip_zip_suffix("plain.txt") == "plain.txt"


# -- legacy-codepage names (no UTF-8 flag) ----------------------------------- #


def legacy_zip(members) -> bytes:
    """A zip whose names are raw codepage bytes without the UTF-8 flag (what
    WinRAR / old 7-Zip write on a Big5 or Shift-JIS Windows)."""
    buf = io.BytesIO()
    placeholders = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for i, (raw, blob) in enumerate(members.items()):
            stand_in = (f"~{i}~".encode() + b"x" * len(raw))[: len(raw)]
            placeholders[stand_in] = raw
            zf.writestr(stand_in.decode(), blob)
    data = buf.getvalue()
    for stand_in, raw in placeholders.items():
        assert data.count(stand_in) == 2  # local header + central directory
        data = data.replace(stand_in, raw)
    return data


def legacy_view(members, cache=None, key=""):
    data = legacy_zip(members)
    return zipfs.ZipView(lambda: io.BytesIO(data), name="Game", cache=cache, cache_key=key)


def test_big5_names_are_shown_decoded_and_still_open():
    members = {
        "www/audio/se/射精音5.rpgmvo".encode("big5"): b"a",
        "www/img/許可.png".encode("big5"): b"b",  # 許 = B3 5C: a '\' trail byte
        "使用AI翻方法/說明.txt".encode("big5"): b"c",
    }
    view = legacy_view(members)
    files = {rel: node for rel, node in view.walk() if not node.is_dir}
    assert set(files) == {raw.decode("big5") for raw in members}
    for raw, blob in members.items():
        with view.open(files[raw.decode("big5")]) as fp:
            assert fp.read() == blob


def test_shift_jis_and_flagless_utf8_names_are_recognised():
    sjis = legacy_view({"セーブ/データ.rvdata2".encode("cp932"): b"x", "画像.png".encode("cp932"): b"y"})
    assert {rel for rel, n in sjis.walk() if not n.is_dir} == {"セーブ/データ.rvdata2", "画像.png"}
    utf8 = legacy_view({"資料/設定.ini".encode("utf-8"): b"x"})
    assert {rel for rel, n in utf8.walk() if not n.is_dir} == {"資料/設定.ini"}


def test_weak_evidence_keeps_the_cp437_name():
    view = legacy_view({b"\x82\x82.txt": b"x"})  # one CJK char at best: not enough
    assert [rel for rel, _ in view.walk()] == ["éé.txt"]


def test_old_cache_with_non_ascii_names_is_reparsed_once(tmp_path):
    raw = "說明.txt".encode("big5")
    store = JsonStore(tmp_path / "zips.json")
    mojibake = raw.decode("cp437")
    store.put("k", {"n": "Game", "d": True, "ch": {mojibake: {"n": mojibake, "d": False, "z": mojibake}}})
    view = legacy_view({raw: b"x"}, cache=store, key="k")
    assert [rel for rel, _ in view.walk()] == ["說明.txt"]
    assert store.get("k")["v"] == zipfs.TREE_VERSION


def test_old_ascii_only_cache_is_kept_without_a_reread(tmp_path):
    store = JsonStore(tmp_path / "zips.json")
    store.put("k", {"n": "遊戲", "d": True, "ch": {"a.txt": {"n": "a.txt", "d": False, "z": "a.txt"}}})

    def no_network():
        raise AssertionError("an ASCII-only tree cannot hold mojibake; must not re-read")

    view = zipfs.ZipView(no_network, name="遊戲", cache=store, cache_key="k")
    assert [rel for rel, _ in view.walk()] == ["a.txt"]


def test_big5_is_not_mistaken_for_gbk_extension_characters():
    # 繁体中文 in Big5 decodes in GBK too: as two extension-area hanzi plus kana
    raw = "localization-繁体中文(zh-tw).bundle".encode("big5")
    assert zipfs.guess_codepage([raw]) == "cp950"
    assert zipfs.guess_codepage(["Actor1、５.png".encode("big5"), "★SEX".encode("big5")]) == "cp950"


def test_genuine_cp437_accents_are_left_alone():
    assert zipfs.guess_codepage(["Alizée.ogg".encode("cp437")]) is None


def test_mixed_codepage_repack_decodes_each_name():
    # a Chinese repack: GBK readmes added next to the original Shift-JIS soundtrack
    members = {
        "【女装神话】H场景追加资料 说明.txt".encode("gbk"): b"a",
        "【女装神话】H场景追加资料 说明.png".encode("gbk"): b"b",
        "Theme/00_OP_愛の超越デスティニー(Full).mp3".encode("cp932"): b"c",
    }
    view = legacy_view(members)
    assert {rel for rel, n in view.walk() if not n.is_dir} == {
        "【女装神话】H场景追加资料 说明.txt",
        "【女装神话】H场景追加资料 说明.png",
        "Theme/00_OP_愛の超越デスティニー(Full).mp3",
    }


def utf8_zip_view(names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, b"x")  # non-ASCII names get the UTF-8 flag
    data = buf.getvalue()
    return zipfs.ZipView(lambda: io.BytesIO(data), name="Game")


def test_mojibake_baked_into_utf8_names_is_repaired():
    # a repacker read the Shift-JIS original as cp437 and saved it as UTF-8,
    # then added its own readme correctly
    baked = "Theme/00_OP_愛の超越デスティニー(Full).mp3".encode("cp932").decode("cp437")
    view = utf8_zip_view([baked, "【女装神话】说明.txt"])
    assert {rel for rel, n in view.walk() if not n.is_dir} == {
        "Theme/00_OP_愛の超越デスティニー(Full).mp3",
        "【女装神话】说明.txt",
    }


def test_genuine_utf8_accents_are_not_reinterpreted():
    names = ["año.txt", "niño.txt", "señor.txt", "Pokémon Café.png", "Alizée.ogg", "naïve résumé.doc"]
    view = utf8_zip_view(names)
    assert {rel for rel, n in view.walk() if not n.is_dir} == set(names)


def test_one_character_baked_names_follow_the_archive():
    bake = lambda s: s.encode("big5").decode("cp437")  # noqa: E731
    view = utf8_zip_view([bake("說明1.jpg"), bake("img/裸.png"), bake("bgm/20春(spring).ogg")])
    assert {rel for rel, n in view.walk() if not n.is_dir} == {"說明1.jpg", "img/裸.png", "bgm/20春(spring).ogg"}
