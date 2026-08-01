r"""Register the Explorer right-click verb 「儲存在本地」.

Two keys are needed — one for files, one for folders — both under HKCU so no
administrator rights are involved:

    HKCU\Software\Classes\*\shell\TeleDriveFetchLocal
    HKCU\Software\Classes\Directory\shell\TeleDriveFetchLocal

``AppliesTo`` restricts the entry to the mounted drive. If Explorer ignores it on
a given build, fetchlocal.py still refuses anything outside the mount, so the
worst case is a menu entry that appears where it is useless.

Windows 11 note: the compact context menu only lists MSIX-packaged
``IExplorerCommand`` handlers, so a registry verb appears under
「顯示更多選項」 (Shift+F10). That placement is accepted for this version.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

VERB = "TeleDriveFetchLocal"
LABEL = "儲存在本地 (TeleDrive)"
PARENTS = (r"Software\Classes\*\shell", r"Software\Classes\Directory\shell")

HERE = Path(__file__).resolve().parent


def _require_windows() -> None:
    if os.name != "nt":
        raise SystemExit("install_menu.py only works on Windows")


def _python_exe() -> Path:
    """Prefer the project venv so the verb works regardless of PATH."""
    venv = HERE / ".venv" / "Scripts" / "python.exe"
    return venv if venv.exists() else Path(sys.executable)


def _command() -> str:
    return f'"{_python_exe()}" "{HERE / "fetchlocal.py"}" "%1"'


def install(drive: str) -> None:
    import winreg

    _require_windows()
    command = _command()
    applies_to = f'System.ItemPathDisplay:"{drive}\\*"'
    for parent in PARENTS:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{parent}\\{VERB}") as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, LABEL)
            winreg.SetValueEx(key, "AppliesTo", 0, winreg.REG_SZ, applies_to)
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, str(_python_exe()))
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{parent}\\{VERB}\\command") as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, command)
    print(f"installed verb {VERB!r} for {drive}\\*")
    print(f"  command: {command}")
    print("Win11: look under 「顯示更多選項」 (Shift+F10) if you do not see it.")


def uninstall() -> None:
    import winreg

    _require_windows()
    for parent in PARENTS:
        for sub in (f"{parent}\\{VERB}\\command", f"{parent}\\{VERB}"):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
            except FileNotFoundError:
                pass
    print(f"removed verb {VERB!r}")


def status() -> int:
    import winreg

    _require_windows()
    found = 0
    for parent in PARENTS:
        path = f"{parent}\\{VERB}"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                label, _ = winreg.QueryValueEx(key, None)
                applies, _ = winreg.QueryValueEx(key, "AppliesTo")
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{path}\\command") as key:
                command, _ = winreg.QueryValueEx(key, None)
            print(f"[installed] HKCU\\{path}\n  label: {label}\n  appliesTo: {applies}\n  command: {command}")
            found += 1
        except FileNotFoundError:
            print(f"[missing]   HKCU\\{path}")
    return 0 if found == len(PARENTS) else 1


def main(argv=None) -> int:
    from config import load_endpoint

    parser = argparse.ArgumentParser(description="Manage the TeleDrive Explorer verb")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--install", action="store_true")
    group.add_argument("--uninstall", action="store_true")
    group.add_argument("--status", action="store_true")
    parser.add_argument("--drive", default=None, help="override the mount drive, e.g. E:")
    args = parser.parse_args(argv)

    drive = (args.drive or load_endpoint()[2]).rstrip("\\/")
    if args.install:
        install(drive)
    elif args.uninstall:
        uninstall()
    else:
        return status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
