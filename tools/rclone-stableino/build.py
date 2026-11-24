"""Build rclone with stable inode numbers and ID-only logs (see stable_ino.go, stable_log.go, CLAUDE.md).

    python tools\\rclone-stableino\\build.py

Downloads the pinned rclone release source into ``src/`` (not in git), applies
the patches, and writes ``rclone.new.exe`` next to this file. ``mountctl.py``
prefers ``rclone.exe`` over whatever ``rclone`` is on PATH; the new binary is
written under another name because the running ``rclone.exe`` is held open by
the mount and Windows will not let it be overwritten. Swap it in between
``stop.bat`` and ``start.bat``.

Each patch is applied by exact string match and the build fails if its line is
not found: a newer rclone that moved or changed it must be looked at by a
person, not silently built without the fix.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

RCLONE_VERSION = "v1.74.4"
HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
OUT = HERE / "rclone.new.exe"
STAMP = SRC / ".teledrive-version"

ORIGINAL = "\tstat.Ino = node.Inode() // FIXME do we need to set the inode number?\n"
PATCHED = "\tstat.Ino = stableIno(node) // TeleDrive patch: stable across mounts, see stable_ino.go\n"


def go_exe() -> str:
    found = shutil.which("go")
    if found:
        return found
    default = Path(r"C:\Program Files\Go\bin\go.exe")
    if default.exists():
        return str(default)
    sys.exit("Go is not installed (winget install GoLang.Go)")


def fetch_source() -> None:
    if STAMP.exists() and STAMP.read_text().strip() == RCLONE_VERSION:
        return
    if SRC.exists():
        shutil.rmtree(SRC)
    url = f"https://codeload.github.com/rclone/rclone/tar.gz/refs/tags/{RCLONE_VERSION}"
    print(f"downloading rclone {RCLONE_VERSION} source")
    with urllib.request.urlopen(url, timeout=300) as resp:
        blob = resp.read()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts[1:]  # strip "rclone-<version>/"
            if not parts or ".." in parts or Path(member.name).is_absolute():
                continue
            if not (member.isfile() or member.isdir()):
                continue  # no links or devices out of a downloaded archive
            member.name = str(Path(*parts))
            tar.extract(member, SRC)
    STAMP.write_text(RCLONE_VERSION)


NL, TAB = chr(10), chr(9)
LOG_GO = ("fs", "log.go")
LOG_PATCHES = [
    (
        "func logSlogWithObject(level LogLevel, o any, text string, attrs []any) {" + NL,
        "func logSlogWithObject(level LogLevel, o any, text string, attrs []any) {" + NL
        + TAB + "text = stableLogText(text) // TeleDrive patch: ids, not paths, see stable_log.go" + NL,
    ),
    (
        TAB * 3 + '"object", fmt.Sprintf("%+v", o),' + NL,
        TAB * 3 + '"object", stableLogObject(o), // TeleDrive patch: an id, not a path' + NL,
    ),
]


def _patch(path: Path, original: str, patched: str) -> None:
    text = path.read_text(encoding="utf-8")
    if patched in text:
        return
    if text.count(original) != 1:
        sys.exit(f"{path}: the line to patch was not found exactly once; "
                 f"rclone {RCLONE_VERSION} changed — update the patch by hand")
    path.write_text(text.replace(original, patched), encoding="utf-8")


def apply_patch() -> None:
    shutil.copyfile(HERE / "stable_ino.go", SRC / "cmd" / "cmount" / "stable_ino.go")
    for name in ("stable_id.go", "stable_log.go", "stable_log_test.go"):
        shutil.copyfile(HERE / name, SRC / "fs" / name)
    _patch(SRC / "cmd" / "cmount" / "fs.go", ORIGINAL, PATCHED)
    for original, patched in LOG_PATCHES:
        _patch(SRC.joinpath(*LOG_GO), original, patched)


def test() -> None:
    """The patch's own Go tests: the ID vectors and the log rewrites."""
    subprocess.run(
        [go_exe(), "test", "./fs", "-run", "StableID|StableLog", "-count=1"],
        cwd=SRC, env={**__import__("os").environ, "GOFLAGS": "-mod=mod"}, check=True,
    )


def build() -> None:
    env = {**__import__("os").environ, "CGO_ENABLED": "0", "GOFLAGS": "-mod=mod"}
    version = f"{RCLONE_VERSION}-stableino"
    subprocess.run(
        [go_exe(), "build", "-tags", "cmount", "-trimpath",
         "-ldflags", f"-s -X github.com/rclone/rclone/fs.Version={version}",
         "-o", str(OUT), "."],
        cwd=SRC, env=env, check=True,
    )
    out = subprocess.run([str(OUT), "version"], capture_output=True, text=True, check=True)
    print(out.stdout.splitlines()[0])


def main() -> int:
    fetch_source()
    apply_patch()
    test()
    build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
