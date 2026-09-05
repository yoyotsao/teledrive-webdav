r"""Register the shell thumbnail handler for files on the TeleDrive mount.

Explorer builds a thumbnail by reading the file, so a folder of photos on the
mount costs its full size to browse: measured, one 256px preview of an 18.6 MB
PNG read all 18,629,212 bytes and took 18.8 seconds. Nothing in the files helps
(these JPEGs carry no APP1/Exif preview and PNG has none by design), and Windows
cannot be told to show a different image for a file. A thumbnail provider is the
one supported interception point — Explorer calls it instead of decoding — so the
handler answers with the small preview Telegram already stores.

Everything goes under HKCU\Software\Classes: no administrator rights, no effect
on other users, and uninstall puts back exactly what was there.

The awkward part is that a thumbnail handler is registered per file type, not per
drive, so registering at all means intercepting every .jpg on the machine. Two
things keep that honest:

* the DLL forwards anything outside the mount to the handler registered before
  it, which is why this script records the displaced CLSID per extension;
* --uninstall restores those recorded values and deletes the keys it created.

Usage:
    python install_thumb.py --install      # build first: shellthumb\build.bat
    python install_thumb.py --uninstall
    python install_thumb.py --status
"""

from __future__ import annotations

import argparse
import sys
import winreg
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config import load_endpoint

HERE = Path(__file__).resolve().parent
DLL = HERE / "shellthumb" / "TeleDriveThumb.dll"

CLSID = "{7A3F1C28-9B6D-4E51-8F42-C0D3E5A91B74}"
PROP_CLSID = "{7A3F1C28-9B6D-4E51-8F42-C0D3E5A91B75}"
NAME = "TeleDrive thumbnail handler"
PROP_NAME = "TeleDrive property handler"

# IThumbnailProvider — the interface id doubles as the ShellEx subkey name.
THUMB_IID = "{e357fccd-a995-4576-b01f-234630154e96}"

SETTINGS_KEY = r"Software\TeleDriveWebDAV"
FALLBACK_KEY = SETTINGS_KEY + r"\Fallback"
PROP_FALLBACK_KEY = SETTINGS_KEY + r"\PropFallback"

# Property handlers are looked up here and nowhere else. There is no HKCU
# equivalent, so unlike the thumbnail handler this half needs administrator
# rights and lands for every user on the machine.
PROP_HANDLERS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\PropertySystem\PropertyHandlers"

IMAGE_EXTS = [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"]
VIDEO_EXTS = [".mp4", ".mkv", ".mov", ".m2ts", ".avi", ".webm", ".wmv", ".ts"]
EXTENSIONS = IMAGE_EXTS + VIDEO_EXTS

# The property handler is claimed for images only -- see install_props().
PROP_EXTS = IMAGE_EXTS


# --------------------------------------------------------------------------- #
# registry helpers
# --------------------------------------------------------------------------- #


def _read(root, path: str, name: str = "") -> Optional[str]:
    try:
        with winreg.OpenKey(root, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except OSError:
        return None


def _write(root, path: str, value: str, name: str = "") -> None:
    with winreg.CreateKey(root, path) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def _delete_tree(root, path: str) -> None:
    try:
        with winreg.OpenKey(root, path) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_tree(root, f"{path}\\{child}")
    except OSError:
        return
    try:
        winreg.DeleteKey(root, path)
    except OSError:
        pass


def _hkcr(path: str) -> Optional[str]:
    """Read through HKEY_CLASSES_ROOT, i.e. the merged per-user + machine view."""
    return _read(winreg.HKEY_CLASSES_ROOT, path)


# --------------------------------------------------------------------------- #
# where a type's handler currently lives
# --------------------------------------------------------------------------- #


def _registration_points(ext: str) -> List[str]:
    r"""Keys to claim for ``ext``, most specific first.

    The shell resolves a handler by ProgID before SystemFileAssociations, so
    claiming only the latter loses to whatever the ProgID already declares —
    on this machine .jpg resolves through ``jpegfile`` and .mp4 through
    ``VLC.mp4``. Both are claimed, under HKCU so the machine keys stay intact.
    """
    points = [rf"Software\Classes\SystemFileAssociations\{ext}\ShellEx\{THUMB_IID}"]
    progid = _hkcr(ext)
    if progid:
        points.insert(0, rf"Software\Classes\{progid}\ShellEx\{THUMB_IID}")
    return points


def _current_handler(ext: str) -> Optional[str]:
    """The CLSID Windows would use today, following its own lookup order."""
    progid = _hkcr(ext)
    candidates = []
    if progid:
        candidates.append(rf"{progid}\ShellEx\{THUMB_IID}")
    candidates.append(rf"SystemFileAssociations\{ext}\ShellEx\{THUMB_IID}")
    candidates.append(rf"{ext}\ShellEx\{THUMB_IID}")
    perceived = None
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, ext) as key:
            perceived, _ = winreg.QueryValueEx(key, "PerceivedType")
    except OSError:
        pass
    if perceived:
        candidates.append(rf"SystemFileAssociations\{perceived}\ShellEx\{THUMB_IID}")

    for path in candidates:
        value = _hkcr(path)
        if value and value.upper() != CLSID.upper():
            return value
    return None


# --------------------------------------------------------------------------- #
# install / uninstall
# --------------------------------------------------------------------------- #


def install() -> int:
    if not DLL.exists():
        print(f"[error] {DLL} is missing. Build it first:")
        print(r"        shellthumb\build.bat")
        return 1

    host, port, mount_drive = load_endpoint()
    drive = mount_drive.rstrip("\\/").upper()

    # 1. the COM server itself
    _write(winreg.HKEY_CURRENT_USER, rf"Software\Classes\CLSID\{CLSID}", NAME)
    _write(winreg.HKEY_CURRENT_USER, rf"Software\Classes\CLSID\{CLSID}\InprocServer32", str(DLL))
    _write(
        winreg.HKEY_CURRENT_USER,
        rf"Software\Classes\CLSID\{CLSID}\InprocServer32",
        "Apartment",
        "ThreadingModel",
    )
    # The shell hosts thumbnail providers in an isolated process by default, and
    # that sandbox has no file access — so it only offers IInitializeWithStream,
    # and a provider that initializes from a path is skipped entirely. Measured
    # without this value, the handler was never called: the thumbnail cache never
    # grew and timings stayed proportional to the original's size.
    #
    # A stream is useless here anyway: it would carry the original's bytes, which
    # is exactly what this handler exists to avoid reading.
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\CLSID\{CLSID}") as key:
        winreg.SetValueEx(key, "DisableProcessIsolation", 0, winreg.REG_DWORD, 1)

    # 2. what the DLL needs to know at run time
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, SETTINGS_KEY) as key:
        winreg.SetValueEx(key, "MountDrive", 0, winreg.REG_SZ, drive)
        winreg.SetValueEx(key, "Port", 0, winreg.REG_DWORD, int(port))

    # 3. claim each type, recording what we displace
    claimed = 0
    for ext in EXTENSIONS:
        previous = _current_handler(ext)
        if previous:
            _write(winreg.HKEY_CURRENT_USER, FALLBACK_KEY, previous, ext)
        for path in _registration_points(ext):
            _write(winreg.HKEY_CURRENT_USER, path, CLSID)
        claimed += 1

    print(f"[ok] registered for {claimed} file types, mount {drive}, bridge port {port}")
    print("     previous handlers recorded — files outside the mount are forwarded to them")
    print("     Explorer caches thumbnails: run this to see the change immediately")
    print("       cleanmgr /sagerun:1     (or delete %LocalAppData%\\Microsoft\\Windows\\Explorer\\thumbcache_*.db)")
    return 0


def uninstall() -> int:
    restored = 0
    for ext in EXTENSIONS:
        previous = _read(winreg.HKEY_CURRENT_USER, FALLBACK_KEY, ext)
        for path in _registration_points(ext):
            mine = _read(winreg.HKEY_CURRENT_USER, path)
            if mine is None or mine.upper() != CLSID.upper():
                continue  # someone else owns it now; leave it alone
            if previous and path.startswith(r"Software\Classes\SystemFileAssociations"):
                # Only the SystemFileAssociations key is restored: the ProgID key
                # under HKCU was created by us, so removing it uncovers the
                # machine key that was serving before.
                _write(winreg.HKEY_CURRENT_USER, path, previous)
            else:
                _delete_tree(winreg.HKEY_CURRENT_USER, path)
            restored += 1

    _delete_tree(winreg.HKEY_CURRENT_USER, FALLBACK_KEY)
    _delete_tree(winreg.HKEY_CURRENT_USER, rf"Software\Classes\CLSID\{CLSID}")
    print(f"[ok] removed {restored} registrations; the COM server is unregistered")
    print("     thumbnails already cached keep showing until the cache is cleared")
    return 0


def status() -> int:
    host, port, mount_drive = load_endpoint()
    print(f"dll            : {DLL} {'(present)' if DLL.exists() else '(MISSING — run build.bat)'}")
    server = _read(winreg.HKEY_CURRENT_USER, rf"Software\Classes\CLSID\{CLSID}\InprocServer32")
    print(f"com server     : {server or '(not registered)'}")
    print(f"mount drive    : {_read(winreg.HKEY_CURRENT_USER, SETTINGS_KEY, 'MountDrive') or '(unset)'}"
          f"   [config.ini says {mount_drive}]")
    print(f"bridge port    : {port}")
    prop_server = _read(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Classes\CLSID\{PROP_CLSID}\InprocServer32")
    print(f"property server: {prop_server or '(not registered)'}")
    print(f"elevated now   : {_elevated()}")
    print("types:")
    for ext in EXTENSIONS:
        # Ours wherever the shell would look, not just at one of the two keys
        # we claim — otherwise an extension served through PerceivedType reads
        # as unregistered when it is in fact working.
        mine = any(
            (_hkcr(path[len(r"Software\Classes\\") - 1:]) or "").upper() == CLSID.upper()
            for path in _registration_points(ext)
        )
        fallback = _read(winreg.HKEY_CURRENT_USER, FALLBACK_KEY, ext)
        effective = "ours" if mine else (_current_handler(ext) or "(none)")
        prop = _read(winreg.HKEY_LOCAL_MACHINE, rf"{PROP_HANDLERS}\{ext}")
        prop_mark = "ours" if (prop or "").upper() == PROP_CLSID.upper() else (prop or "(none)")
        print(f"  {ext:<7} thumb={effective:<38} props={prop_mark}")
    return 0


def _elevated() -> bool:
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _current_prop_handler(ext: str) -> Optional[str]:
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        value = _read(root, rf"{PROP_HANDLERS}\{ext}")
        if value and value.upper() != PROP_CLSID.upper():
            return value
    return None


def install_props() -> int:
    """Claim the property handler for the same file types.

    Explorer works out an image's pixel size by reading its header — measured on
    the mount, 258 KB of a 2 MB JPEG, once per file, and that is what is left
    once thumbnails are instant. Telegram already knows the dimensions, so the
    handler answers from metadata and no bytes move.

    Unlike the thumbnail half this must go in HKLM: PropertyHandlers has no
    per-user view. Settings and the fallback table are written to HKLM as well,
    because the search indexer loads property handlers as a different user and
    would otherwise find neither.

    Images only. Claiming the video types took every video thumbnail on the
    machine away, mount or not: a video has no thumbnail provider of its own --
    ``HKCR\.mp4\ShellEx\{e357fccd-...}`` names shell32's Property Thumbnail
    Handler ``{9DBD2C50-...}``, which pulls System.ThumbnailStream out of the
    file's *property store*. Redirect that store and the picture is gone. Worse,
    the shell does not even load this DLL for video extensions -- with logging
    on, a property probe of a local .mp4 wrote no line at all and came back
    0x8007000D -- so there was nothing to fix inside the handler either.
    Measured on one file copied under two names: as .mp4 no thumbnail at all,
    as .m4v (an extension we never claimed, same {9DBD2C50} provider, Windows'
    own property handler) a thumbnail in 0.19s.

    Nothing is lost by staying out: the handler was never being loaded for those
    types, so the mount's videos were not getting their duration and frame size
    from Telegram either. Images are unaffected because their provider
    ({C7657C4A-...}) decodes the file itself instead of asking the property
    store -- which is also why the JPEG header reads this half exists to stop
    were the image half all along.
    """
    if not DLL.exists():
        print(f"[error] {DLL} is missing. Build it first:")
        print(r"        shellthumbuild.bat")
        return 1
    if not _elevated():
        print("[error] property handlers live in HKLM, which needs an elevated prompt.")
        print("        Open PowerShell as administrator and run this again.")
        return 1

    host, port, mount_drive = load_endpoint()
    drive = mount_drive.rstrip("\/").upper()

    _write(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Classes\CLSID\{PROP_CLSID}", PROP_NAME)
    _write(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Classes\CLSID\{PROP_CLSID}\InprocServer32", str(DLL))
    _write(
        winreg.HKEY_LOCAL_MACHINE,
        rf"SOFTWARE\Classes\CLSID\{PROP_CLSID}\InprocServer32",
        "Both",
        "ThreadingModel",
    )

    with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, SETTINGS_KEY) as key:
        winreg.SetValueEx(key, "MountDrive", 0, winreg.REG_SZ, drive)
        winreg.SetValueEx(key, "Port", 0, winreg.REG_DWORD, int(port))

    claimed = 0
    for ext in PROP_EXTS:
        previous = _current_prop_handler(ext)
        if previous:
            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                _write(root, PROP_FALLBACK_KEY, previous, ext)
        _write(winreg.HKEY_LOCAL_MACHINE, rf"{PROP_HANDLERS}\{ext}", PROP_CLSID)
        claimed += 1

    # Hand back anything an older install claimed and this one does not, so a
    # re-run repairs the machine instead of leaving the damage in place.
    released = _release_props([e for e in EXTENSIONS if e not in PROP_EXTS])
    if released:
        print(f"[ok] released {released} file types an earlier install had claimed")

    print(f"[ok] property handler registered for {claimed} file types (machine-wide)")
    print("     previous handlers recorded; files outside the mount are forwarded to them")
    print("     restart Explorer to pick it up: taskkill /f /im explorer.exe && start explorer")
    return 0


def _release_props(exts: List[str]) -> int:
    """Put back the handler recorded for each of ``exts``, where we still own it.

    Shared by uninstall and by install: an install that claims fewer types than
    the one before it has to give the difference back, or the extensions dropped
    from the list keep pointing at this DLL forever.
    """
    released = 0
    for ext in exts:
        mine = _read(winreg.HKEY_LOCAL_MACHINE, rf"{PROP_HANDLERS}\{ext}")
        if not mine or mine.upper() != PROP_CLSID.upper():
            continue
        previous = _read(winreg.HKEY_CURRENT_USER, PROP_FALLBACK_KEY, ext) or _read(
            winreg.HKEY_LOCAL_MACHINE, PROP_FALLBACK_KEY, ext
        )
        if previous:
            _write(winreg.HKEY_LOCAL_MACHINE, rf"{PROP_HANDLERS}\{ext}", previous)
        else:
            _delete_tree(winreg.HKEY_LOCAL_MACHINE, rf"{PROP_HANDLERS}\{ext}")
        released += 1
    return released


def uninstall_props() -> int:
    if not _elevated():
        print("[error] removing the property handler needs an elevated prompt.")
        return 1
    restored = _release_props(EXTENSIONS)
    _delete_tree(winreg.HKEY_LOCAL_MACHINE, PROP_FALLBACK_KEY)
    _delete_tree(winreg.HKEY_CURRENT_USER, PROP_FALLBACK_KEY)
    _delete_tree(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Classes\CLSID\{PROP_CLSID}")
    print(f"[ok] property handler removed, {restored} file types restored")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--install", action="store_true", help="thumbnail handler (HKCU, no admin)")
    group.add_argument("--uninstall", action="store_true")
    group.add_argument("--install-props", action="store_true",
                       help="property handler (HKLM, needs an elevated prompt)")
    group.add_argument("--uninstall-props", action="store_true")
    group.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("[error] Windows only")
        return 1
    if args.install:
        return install()
    if args.uninstall:
        return uninstall()
    if args.install_props:
        return install_props()
    if args.uninstall_props:
        return uninstall_props()
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
