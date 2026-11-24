"""Virtual directory tree from a zip archive's central directory.

A zip's central directory sits at the end of the file, so a few tens of KB of
range reads reveal every name, size and offset — the archive is browsable without
downloading it. For ZIP_STORED entries (what gamestage.py produces) an entry's
bytes are a plain slice of the archive, so reading one file inside a 60 GB game
zip costs exactly that file's bytes.

The parsed tree is cached on disk, after which browsing costs zero network.
"""

from __future__ import annotations

import io
import logging
import re
import struct
import time
import zlib
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from teledrive_webdav.core import fileid
from teledrive_webdav.telegram.tgio import SlicedReader

log = logging.getLogger("zipfs")

_LOCAL_HEADER = struct.Struct("<IHHHHHIIIHH")
_LOCAL_SIG = 0x04034B50
_LOCAL_SIZE = 30


@dataclass
class ZipNode:
    """One node of the virtual tree: a directory or a single archive entry."""

    name: str
    is_dir: bool
    size: int = 0
    mtime: float = 0.0
    zip_name: str = ""  # full name inside the archive (files only)
    header_offset: int = -1
    compress_type: int = zipfile.ZIP_STORED
    compress_size: int = 0
    data_offset: Optional[int] = None  # filled lazily from the local header
    children: Dict[str, "ZipNode"] = field(default_factory=dict)

    @property
    def stored(self) -> bool:
        return self.compress_type == zipfile.ZIP_STORED


def _mtime(info: zipfile.ZipInfo) -> float:
    try:
        return time.mktime(tuple(info.date_time) + (0, 0, -1))
    except (ValueError, OverflowError):
        return time.time()


def _split_name(name: str) -> Optional[List[str]]:
    """Normalise an archive member name to path segments, or None if unsafe."""
    name = name.replace("\\", "/")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return parts


# Names without the UTF-8 flag (0x800) are whatever codepage the packer's
# Windows ran in; zipfile decodes them as cp437, so a Big5 name comes out as
# '«g║δ¡╡'. cp437 maps all 256 bytes, so the original bytes are recoverable
# exactly and the codepage is guessed once per archive (one packer, one codepage).
_UTF8_FLAG = 0x800
_LEGACY_CODEPAGES = ("cp932", "cp950", "gbk")  # Windows tables: cp950 has the ETEN extensions big5 lacks
# Mojibake lands anywhere in a codepage, so it is mostly rare characters; real
# names are mostly common ones. "Common" is judged on the double-byte code in
# the candidate's own table: punctuation, kana and level-1 kanji/hanzi.
def _common_cp932(lead: int, trail: int) -> bool:
    code = lead << 8 | trail
    return (
        0x8141 <= code <= 0x81AC  # punctuation, ー
        or 0x829F <= code <= 0x82F1  # hiragana
        or 0x8340 <= code <= 0x8396  # katakana
        or 0x889F <= code <= 0x9872  # JIS level 1 kanji
    )


def _common_big5(lead: int, trail: int) -> bool:
    return 0xA1 <= lead <= 0xA3 or 0xA4 <= lead <= 0xC6  # symbols, frequent hanzi


def _common_gbk(lead: int, trail: int) -> bool:
    # GB2312 proper has both bytes >= 0xA1; lower trails are the GBK extension
    return trail >= 0xA1 and (0xA1 <= lead <= 0xA5 or 0xB0 <= lead <= 0xD7)


_COMMON = {"cp932": _common_cp932, "cp950": _common_big5, "gbk": _common_gbk}
_BAD_CHARS = re.compile("[｡-ﾟ─-╿-]")  # halfwidth kana, box drawing, PUA
_MIN_SCORE = 1  # rare characters score 0, so random bytes stay below this


def _legacy_raw(info: zipfile.ZipInfo) -> Optional[bytes]:
    """The name's original bytes, if zipfile decoded them as cp437 and they are not ASCII."""
    if info.flag_bits & _UTF8_FLAG:
        return None
    # orig_filename: on Windows zipfile turns '\\' into '/' in .filename, and
    # 0x5C is a valid Big5/Shift-JIS trail byte (許, 表 ...).
    raw = getattr(info, "orig_filename", info.filename).encode("cp437")
    return None if raw.isascii() else raw


def _text_score(raw: bytes, enc: str) -> Optional[int]:
    """How much ``raw`` decoded as ``enc`` looks like real text; None if it does not decode."""
    try:
        text = raw.decode(enc)
    except UnicodeDecodeError:
        return None
    score = -3 * len(_BAD_CHARS.findall(text))
    for ch in text:
        code = ch.encode(enc)
        if len(code) == 2 and _COMMON[enc](code[0], code[1]):
            score += 1
    return score


def guess_codepage(raws: Sequence[bytes]) -> Optional[str]:
    """The codepage that turns these names into the most real text, or None (keep cp437)."""
    if not raws:
        return None
    try:
        for raw in raws:
            raw.decode("utf-8")
        return "utf-8"  # flag missing but the bytes are UTF-8 (7-Zip, macOS)
    except UnicodeDecodeError:
        pass
    best, best_score = None, _MIN_SCORE - 1
    for enc in _LEGACY_CODEPAGES:
        score = sum(-5 if (s := _text_score(raw, enc)) is None else s for raw in raws)
        if score > best_score:
            best, best_score = enc, score
    return best


def _decode_one(raw: bytes, enc: str) -> Optional[str]:
    """``raw`` in the archive's codepage, unless another one reads clearly better.

    Repacks mix codepages: a Chinese translator's GBK readme next to the
    original Shift-JIS soundtrack, which GBK still "decodes" into rare hanzi.
    A fallback must stand on its own evidence and beat the archive's codepage
    by a wide margin, so a stray cp437 'é' is not turned into a fullwidth letter
    and a Big5 name is not flipped to GBK by one lucky character.
    """
    main = _text_score(raw, enc)
    scored = [(s, alt) for alt in _LEGACY_CODEPAGES if alt != enc and (s := _text_score(raw, alt)) is not None]
    if scored:
        score, alt = max(scored)
        if score >= _MIN_SCORE and (main is None or score > 2 * max(main, 0) + 1):
            return raw.decode(alt)
    return None if main is None else raw.decode(enc)  # None: keep zipfile's cp437 name


def _baked_raw(info: zipfile.ZipInfo) -> Optional[bytes]:
    """Bytes behind a UTF-8 name that is itself cp437 mojibake, else None.

    A repacker that read a legacy archive as cp437 and saved it with the UTF-8
    flag writes the garbage itself ('êñé╠Æ┤' for 愛の超越). Every such name is
    cp437-encodable; most genuine non-ASCII names (CJK, most accents) are not.
    """
    if not info.flag_bits & _UTF8_FLAG:
        return None
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return None
    # 'año', 'Pokémon' are cp437-encodable too, but only mojibake carries cp437's
    # box drawing, Greek and math (╗ α ⌐ ¼): real Latin names have just letters.
    if not any(not ch.isascii() and (ord(ch) > 0xFF or not ch.isalpha()) for ch in info.filename):
        return None
    return raw


# Baked names were deliberately written as UTF-8, so the bar is higher than for
# flagless ones: 'año' is one common Big5 character away from 'a大'. The
# archive's baked names are judged together first; once they agree on a
# codepage, a one-character name like '裸.png' needs no more evidence than that.
_BAKED_MIN_SCORE = 2


def _baked_names(raws: Dict[int, bytes]) -> Dict[int, str]:
    enc = None
    totals = {}
    for alt in _LEGACY_CODEPAGES:
        scores = [_text_score(raw, alt) for raw in raws.values()]
        totals[alt] = sum(s for s in scores if s is not None and s > 0)
    if totals and max(totals.values()) >= _BAKED_MIN_SCORE:
        enc = max(totals, key=totals.get)
    out = {}
    for key, raw in raws.items():
        if enc is not None and (_text_score(raw, enc) or 0) >= _MIN_SCORE:
            out[key] = raw.decode(enc)
            continue
        scored = [(s, alt) for alt in _LEGACY_CODEPAGES if (s := _text_score(raw, alt)) is not None]
        if scored and max(scored)[0] >= _BAKED_MIN_SCORE:
            out[key] = raw.decode(max(scored)[1])
    return out


def _atag(archive: str) -> str:
    """Log name of an archive (a direct child of the game folder): an id, never its name."""
    return fileid.tag("game/" + archive) if archive else fileid.UNKNOWN_TAG


def decoded_names(infos: Sequence[zipfile.ZipInfo], archive: str = "") -> Dict[int, str]:
    """``id(info) -> display name`` for members whose name re-decodes cleanly."""
    out: Dict[int, str] = {}
    raws = {id(info): raw for info in infos if (raw := _legacy_raw(info)) is not None}
    enc = guess_codepage(list(raws.values()))
    if enc is not None:
        out.update((key, name) for key, raw in raws.items() if (name := _decode_one(raw, enc)) is not None)
        log.info("zip %s: %d of %d legacy names decoded, mostly %s", _atag(archive), len(out), len(raws), enc)
    elif raws:
        log.info("zip %s: %d legacy names, codepage unknown, kept as cp437", _atag(archive), len(raws))

    baked = {id(info): raw for info in infos if (raw := _baked_raw(info)) is not None}
    fixed = _baked_names(baked)
    out.update(fixed)
    if fixed:
        log.info("zip %s: %d of %d UTF-8 names were stored as cp437 mojibake, repaired", _atag(archive), len(fixed), len(baked))
    return out


def build_tree(infos: Sequence[zipfile.ZipInfo], root_name: str = "") -> ZipNode:
    """Build the virtual tree from a central directory listing.

    Directories implied by a member's path are created even when the archive
    carries no explicit entry for them. ``zip_name`` keeps zipfile's own name
    (what ``ZipFile.open`` looks up); only the visible path is re-decoded.
    """
    root = ZipNode(name=root_name, is_dir=True)
    names = decoded_names(infos, root_name)
    for index, info in enumerate(infos):
        parts = _split_name(names.get(id(info), info.filename))
        if parts is None:
            log.warning("skipping unsafe zip member #%d of %s", index, _atag(root_name))
            continue
        is_dir = info.is_dir()
        node = root
        for segment in parts[:-1]:
            child = node.children.get(segment)
            if child is None:
                child = ZipNode(name=segment, is_dir=True)
                node.children[segment] = child
            elif not child.is_dir:
                log.warning("zip member #%d of %s collides with a file entry", index, _atag(root_name))
                child = None
                break
            node = child
        if node is None:
            continue
        leaf = parts[-1]
        if is_dir:
            existing = node.children.get(leaf)
            if existing is None:
                node.children[leaf] = ZipNode(name=leaf, is_dir=True, mtime=_mtime(info))
            continue
        node.children[leaf] = ZipNode(
            name=leaf,
            is_dir=False,
            size=info.file_size,
            mtime=_mtime(info),
            zip_name=info.filename,
            # Synthetic ZipInfo objects (not read from an archive) have no offset.
            header_offset=getattr(info, "header_offset", -1),
            compress_type=info.compress_type,
            compress_size=info.compress_size,
        )
    return root


def lookup(root: ZipNode, segments: Sequence[str]) -> Optional[ZipNode]:
    node = root
    for segment in segments:
        if not node.is_dir:
            return None
        node = node.children.get(segment)
        if node is None:
            return None
    return node


def walk(node: ZipNode, prefix: str = "") -> Iterator[Tuple[str, ZipNode]]:
    """Depth-first walk yielding ``(relative_path, node)`` for every descendant."""
    for name in sorted(node.children):
        child = node.children[name]
        rel = f"{prefix}{name}"
        yield rel, child
        if child.is_dir:
            yield from walk(child, rel + "/")


def tree_to_json(node: ZipNode) -> dict:
    return {
        "n": node.name,
        "d": node.is_dir,
        "s": node.size,
        "t": node.mtime,
        "z": node.zip_name,
        "h": node.header_offset,
        "c": node.compress_type,
        "cs": node.compress_size,
        "o": node.data_offset,
        "ch": {k: tree_to_json(v) for k, v in node.children.items()} if node.is_dir else {},
    }


def tree_from_json(data: dict) -> ZipNode:
    node = ZipNode(
        name=data["n"],
        is_dir=data["d"],
        size=data.get("s", 0),
        mtime=data.get("t", 0.0),
        zip_name=data.get("z", ""),
        header_offset=data.get("h", -1),
        compress_type=data.get("c", zipfile.ZIP_STORED),
        compress_size=data.get("cs", 0),
        data_offset=data.get("o"),
    )
    for key, child in (data.get("ch") or {}).items():
        node.children[key] = tree_from_json(child)
    return node


# 4: legacy-codepage and baked cp437 names re-decoded (decoded_names; 2-4 were drafts). Older trees with
# non-ASCII names may hold cp437 mojibake and are re-read once; ASCII-only
# trees cannot be affected, so they are kept and cost no Telegram round trip.
TREE_VERSION = 5


def _ascii_tree(data: dict) -> bool:
    return data["n"].isascii() and all(_ascii_tree(c) for c in (data.get("ch") or {}).values())


def _cache_current(data: dict) -> bool:
    if data.get("v") == TREE_VERSION:
        return True
    # the root's own name is the archive's, not a member's
    return all(_ascii_tree(c) for c in (data.get("ch") or {}).values())


def local_data_offset(fp, header_offset: int) -> int:
    """Read a member's local file header and return where its data starts.

    The local header's extra field can differ in length from the central
    directory's, so this cannot be derived from the central entry alone.
    """
    fp.seek(header_offset)
    raw = fp.read(_LOCAL_SIZE)
    if len(raw) < _LOCAL_SIZE:
        raise ValueError("truncated local file header")
    fields = _LOCAL_HEADER.unpack(raw)
    if fields[0] != _LOCAL_SIG:
        raise ValueError(f"bad local header signature at {header_offset}: {fields[0]:#x}")
    name_len, extra_len = fields[9], fields[10]
    return header_offset + _LOCAL_SIZE + name_len + extra_len


class _InflateReader(io.RawIOBase):
    """Forward-only raw-deflate stream over one member's compressed bytes.

    Built from the tree's own offsets, so opening a member costs one read at
    its data, not a fresh ``zipfile.ZipFile`` that re-reads the end record and
    the central directory (see ZipView.open).
    """

    CHUNK = 256 * 1024

    def __init__(self, compressed: io.RawIOBase, size: int):
        super().__init__()
        self._src = compressed
        self._size = size
        self._inflater = zlib.decompressobj(-15)
        self._buf = b""
        self._out = 0

    def readable(self) -> bool:
        return True

    def _fill(self, want: int) -> None:
        while len(self._buf) < want and not self._inflater.eof:
            raw = self._inflater.unconsumed_tail or self._src.read(self.CHUNK)
            if not raw:
                raise OSError("zip member data ended before the deflate stream did")
            self._buf += self._inflater.decompress(raw, max(want - len(self._buf), 1 << 16))

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        remaining = self._size - self._out
        if size is None or size < 0 or size > remaining:
            size = remaining
        if size <= 0:
            return b""
        self._fill(size)
        data, self._buf = self._buf[:size], self._buf[size:]
        self._out += len(data)
        if len(data) < size:
            raise OSError("zip member inflated to fewer bytes than its recorded size")
        return data

    def close(self) -> None:
        try:
            self._src.close()
        finally:
            super().close()


class _DecompressReader(io.RawIOBase):
    """Seekable adapter over a compressed member.

    Only needed for archives the bridge did not create (gamestage.py always uses
    ZIP_STORED). Backward seeks restart the stream and discard forward, which is
    slow but keeps Range requests correct.
    """

    def __init__(self, open_member: Callable[[], io.RawIOBase], size: int, name: str = ""):
        super().__init__()
        self._open_member = open_member
        self._size = size
        self._name = name
        self._stream = open_member()
        self._pos = 0

    @property
    def name(self) -> str:  # noqa: A003
        return self._name

    @property
    def size(self) -> int:
        return self._size

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self._pos + offset
        elif whence == io.SEEK_END:
            target = self._size + offset
        else:
            raise ValueError(f"invalid whence {whence}")
        if target < 0:
            raise OSError("negative seek position")
        if target < self._pos:
            self._stream.close()
            self._stream = self._open_member()
            self._pos = 0
        while self._pos < target:
            chunk = self._stream.read(min(1 << 20, target - self._pos))
            if not chunk:
                break
            self._pos += len(chunk)
        return self._pos

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        data = self._stream.read(size if size is not None and size >= 0 else None)
        self._pos += len(data)
        return data

    def readinto(self, buf) -> int:
        data = self.read(len(buf))
        buf[: len(data)] = data
        return len(data)

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            super().close()


class ZipView:
    """Lazily parsed, disk-cached view of one remote zip archive.

    ``open_stream`` must return a *fresh* seekable file object over the archive
    each time it is called, so concurrent readers never share a file position.
    """

    SAVE_INTERVAL = 5.0

    def __init__(self, open_stream: Callable[[], io.RawIOBase], *, name: str, cache=None, cache_key: str = ""):
        self._open_stream = open_stream
        self._name = name
        self._cache = cache
        self._cache_key = cache_key
        self._root: Optional[ZipNode] = None
        self._dirty = False
        self._last_save = 0.0

    @property
    def name(self) -> str:  # noqa: A003
        return self._name

    @property
    def root(self) -> ZipNode:
        if self._root is not None:
            return self._root
        if self._cache is not None and self._cache_key:
            cached = self._cache.get(self._cache_key)
            if cached and _cache_current(cached):
                self._root = tree_from_json(cached)
                return self._root
        stream = self._open_stream()
        try:
            with zipfile.ZipFile(stream) as zf:
                self._root = build_tree(zf.infolist(), root_name=self._name)
        finally:
            stream.close()
        self._dirty = True
        self.save(force=True)
        return self._root

    def save(self, force: bool = False) -> None:
        if not self._dirty or self._cache is None or not self._cache_key or self._root is None:
            return
        now = time.monotonic()
        if not force and now - self._last_save < self.SAVE_INTERVAL:
            return
        self._cache.put(self._cache_key, {**tree_to_json(self._root), "v": TREE_VERSION})
        self._dirty = False
        self._last_save = now

    def lookup(self, segments: Sequence[str]) -> Optional[ZipNode]:
        return lookup(self.root, segments)

    def walk(self, node: Optional[ZipNode] = None) -> Iterator[Tuple[str, ZipNode]]:
        return walk(node if node is not None else self.root)

    def open(self, node: ZipNode) -> io.RawIOBase:
        """Open one member for reading. Seekable, so Range requests work."""
        if node.is_dir:
            raise IsADirectoryError(node.name)
        stream = self._open_stream()
        if node.data_offset is None:
            try:
                node.data_offset = local_data_offset(stream, node.header_offset)
            except BaseException:
                stream.close()
                raise
            self._dirty = True
            self.save()
        if node.stored:
            return SlicedReader(stream, node.data_offset, node.size, name=node.name)

        if node.compress_type == zipfile.ZIP_DEFLATED:
            # Browser-uploaded archives are deflated. Inflate straight from the
            # member's bytes: building a zipfile.ZipFile per open (and per
            # backward seek) re-read the central directory over Telegram each
            # time, which is what kept every worker thread busy under /game.
            stream.close()

            def open_member():
                return _InflateReader(
                    SlicedReader(self._open_stream(), node.data_offset, node.compress_size),
                    node.size,
                )

            return _DecompressReader(open_member, node.size, name=node.name)

        # Other codecs (bzip2, lzma, ...) are rare: let zipfile handle them.
        def open_member():
            zf = zipfile.ZipFile(self._open_stream())
            return zf.open(node.zip_name)

        stream.close()
        return _DecompressReader(open_member, node.size, name=node.name)


def is_zip_name(name: str) -> bool:
    return name.lower().endswith(".zip")


def strip_zip_suffix(name: str) -> str:
    return name[:-4] if is_zip_name(name) else name
