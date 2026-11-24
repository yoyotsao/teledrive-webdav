"""File IDs for logs: identify a drive file by rclone's stable inode, never by name.

A drive's file names and folder layout say what its owner stores, and logs are
the thing that most often leaves the machine. Everything diagnostic therefore
says ``id=0x...`` instead. The ID is exactly the one rclone hands to WinFsp
(see tools/rclone-stableino/stable_ino.go), so the same file reads the same
number in bridge.log, rclone.log, ``fsutil file queryFileID`` and thumbcache::

    id = FNV-64a(utf8(rclone VFS path)) & ~(1 << 63)      (0 becomes 1)

The VFS path is relative to the mount root, ``/``-separated, no leading ``/``;
the root is the empty string. It is used byte for byte: no Unicode
normalisation, no case folding (a different spelling is a different ID, which
is also what Windows' thumbcache does).

    python fileid.py tag "H:\\some\\file.jpg"     # path -> id, offline
"""

from __future__ import annotations

import re
import sys
from pathlib import Path, PureWindowsPath
from typing import Optional
from urllib.parse import unquote

UNKNOWN_TAG = "id=?"

_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_MASK64 = 0xFFFFFFFFFFFFFFFF
_TOP_BIT = 1 << 63


def path_id(vfs_path: str) -> int:
    """Port of ``stableIno``: the same value rclone reports for this path."""
    h = _FNV_OFFSET
    for byte in vfs_path.encode("utf-8", "surrogatepass"):
        h ^= byte
        h = (h * _FNV_PRIME) & _MASK64
    h &= ~_TOP_BIT & _MASK64
    return h or 1


def size_magnitude(size: Optional[int]) -> Optional[str]:
    """A size as a magnitude (``~56.7 MiB``): enough to tell what kind of file it is
    without being an exact byte count that could identify it."""
    if size is None:
        return None
    for unit, shift in (("GiB", 30), ("MiB", 20), ("KiB", 10)):
        if size >= 1 << shift:
            return f"~{size / (1 << shift):.1f} {unit}"
    return "<1 KiB"


def hex_id(vfs_path: str) -> str:
    """The id as a bare ``0x...`` string, for JSON (``/rpc/status``)."""
    return "0x%016x" % path_id(vfs_path)


def tag(vfs_path: str) -> str:
    return "id=0x%016x" % path_id(vfs_path)


def tag_dav(dav_path: str) -> str:
    """Tag for a bridge DAV path (``/a/b.jpg``, possibly percent-encoded)."""
    return tag(unquote(dav_path).lstrip("/"))


def _strip_extended(path: str) -> str:
    return path[4:] if path.startswith("\\\\?\\") else path


def tag_drive(path: str, drive: str = "H:") -> Optional[str]:
    """Tag for a path as Windows sees it on the mount (``H:\\a\\b``), else None."""
    path = _strip_extended(str(path))
    if len(path) < 2 or path[:2].lower() != drive.lower():
        return None
    rest = path[2:].replace("\\", "/").strip("/")
    return tag(rest)


def _under(path: str, base: Path) -> Optional[list]:
    """Components of ``path`` below ``base`` (case-insensitive), or None."""
    p = PureWindowsPath(_strip_extended(str(path)))
    b = PureWindowsPath(str(base))
    pp = [x.lower() for x in p.parts]
    bp = [x.lower() for x in b.parts]
    if len(pp) <= len(bp) or pp[: len(bp)] != bp:
        return None
    return list(p.parts[len(bp):])


def tag_local(path, cache_dir: Path, unit: Optional[str] = None) -> Optional[str]:
    """Tag for a local file that stands in for a drive path, else None.

    uploads/, local/, rejected/ and the rclone VFS cache mirror the drive's
    layout; staging/ is the ``/game`` folder; staging/.pack and .convert are
    the program's own scratch for one unit (``unit`` names it for .convert,
    whose file name is a backend id, not a name).
    """
    parts = _under(str(path), Path(cache_dir))
    if not parts:
        return None
    head, rest = parts[0].lower(), parts[1:]
    if head in ("uploads", "local", "rejected"):
        return tag("/".join(rest)) if rest else None
    if head == "staging":
        if not rest:
            return None
        if rest[0].lower() == ".pack":
            if len(rest) < 2:
                return None
            name = rest[1]
            for suffix in (".zip", ".extract"):
                if name.lower().endswith(suffix):
                    name = name[: -len(suffix)]
                    break
            return tag("game/" + name)
        if rest[0].lower() == ".convert":
            return tag("game/" + unit) if unit else None
        return tag("game/" + "/".join(rest))
    if head == "rclone" and len(rest) >= 3 and rest[0].lower() in ("vfs", "vfsmeta"):
        # rclone/<vfs|vfsMeta>/<remote>/<rel...>
        return tag("/".join(rest[2:]))
    return None


def game_tag(name: str, folder: str = "game") -> str:
    """Tag for a direct child of the game folder, as the drive presents it.

    A ``.zip`` there is shown on the drive as a *folder* named without the
    extension (``GameRootCollection.get_member_names`` strips it), so that
    stemmed path is what rclone hashes. A ``.rar`` or any other file keeps its
    own name.
    """
    if name.lower().endswith(".zip"):
        name = name[: -len(".zip")]
    return tag(f"{folder}/{name}")


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "tag":
        result = tag_drive(args[1])
        if result is None:
            print("not a path on the mounted drive (expected H:\\...)", file=sys.stderr)
            return 2
        print(result)
        return 0
    if args and args[0] == "info":
        import fileinfo  # heavier; the logging path never needs it

        return fileinfo.info_main(args[1:])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
