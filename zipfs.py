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
import struct
import time
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from tgio import SlicedReader

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


def build_tree(infos: Sequence[zipfile.ZipInfo], root_name: str = "") -> ZipNode:
    """Build the virtual tree from a central directory listing.

    Directories implied by a member's path are created even when the archive
    carries no explicit entry for them.
    """
    root = ZipNode(name=root_name, is_dir=True)
    for info in infos:
        parts = _split_name(info.filename)
        if parts is None:
            log.warning("skipping unsafe zip member %r", info.filename)
            continue
        is_dir = info.is_dir()
        node = root
        for segment in parts[:-1]:
            child = node.children.get(segment)
            if child is None:
                child = ZipNode(name=segment, is_dir=True)
                node.children[segment] = child
            elif not child.is_dir:
                log.warning("zip member %r collides with a file entry", info.filename)
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
            if cached:
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
        self._cache.put(self._cache_key, tree_to_json(self._root))
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
        if not node.stored:
            # Compressed member: decompress from the archive's own reader.
            def open_member():
                zf = zipfile.ZipFile(self._open_stream())
                return zf.open(node.zip_name)

            stream.close()
            return _DecompressReader(open_member, node.size, name=node.name)

        if node.data_offset is None:
            node.data_offset = local_data_offset(stream, node.header_offset)
            self._dirty = True
            self.save()
        return SlicedReader(stream, node.data_offset, node.size, name=node.name)


def is_zip_name(name: str) -> bool:
    return name.lower().endswith(".zip")


def strip_zip_suffix(name: str) -> str:
    return name[:-4] if is_zip_name(name) else name
