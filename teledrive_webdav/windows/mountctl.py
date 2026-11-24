"""Own the ``rclone mount`` that puts the bridge on a drive letter.

The bridge calls :func:`ensure_mounted` once it is serving, so starting the
bridge is the only step: there is no second program to forget. rclone is still
its own process (it is an external exe), but it is started *detached* — a
bridge restart must not take the drive away, see CLAUDE.md "restart.bat".

Nothing here ever force-kills rclone. A mount that is still being read from
(Explorer, warmshell) turns into an unreapable process when it is killed out
from under them, and only a reboot clears that. If rclone will not leave when
asked, :func:`unmount` says so and stops.

    python -m teledrive_webdav.windows.mountctl ensure   # mount if not mounted (idempotent)
    python -m teledrive_webdav.windows.mountctl stop     # graceful unmount, never a taskkill
    python -m teledrive_webdav.windows.mountctl check    # exit 0 when mounted
    python -m teledrive_webdav.windows.mountctl status
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional

from teledrive_webdav.core.config import Config, load_config

log = logging.getLogger("mountctl")

RC_ADDR = "127.0.0.1:5572"

# rclone built by tools\rclone-stableino\build.py. Stock rclone hands Windows an
# in-memory counter as each file's ID, which restarts on every mount, and the
# shell's thumbcache is keyed on that ID — so every remount orphaned every
# thumbnail ever cached for the drive. See CLAUDE.md.
STABLE_RCLONE = Path(__file__).resolve().parents[2] / "tools" / "rclone-stableino" / "rclone.exe"

# Pinned for the same reason: the volume serial is part of a file's identity to
# the shell, and WinFsp picks a new one per mount when none is given.
VOLUME_SERIAL = "9b15860e"


def _rc(command: str, timeout: float = 3.0, params: Optional[dict] = None) -> Optional[dict]:
    """Call rclone's remote-control API; None when nothing is answering."""
    req = urllib.request.Request(
        f"http://{RC_ADDR}/{command}", data=json.dumps(params or {}).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
        return json.loads(body) if body else {}
    except (urllib.error.URLError, OSError, ValueError):
        return None


def rclone_running() -> bool:
    return _rc("rc/noop") is not None


def rclone_exe() -> Optional[str]:
    """The stable-inode build when it exists, else rclone from PATH (with a warning)."""
    if STABLE_RCLONE.exists():
        return str(STABLE_RCLONE)
    exe = shutil.which("rclone")
    if exe is not None:
        log.warning("using stock rclone: Windows thumbnails will not survive a remount "
                    "(build tools\\rclone-stableino\\build.py)")
    return exe


def drive_present(cfg: Config) -> bool:
    return os.path.exists(cfg.mount_drive.rstrip("\\/") + "\\")


def mount_command(cfg: Config, exe: str) -> List[str]:
    drive = cfg.mount_drive.rstrip("\\/")
    # --vfs-cache-max-age is effectively disabled on purpose: evicting by age
    # re-downloads files that are still wanted and burns SSD TBW. Capacity-based
    # eviction is the only policy that fits "a few hours every few months".
    return [
        exe, "mount", ":webdav:", drive,
        "--network-mode",
        "--cache-dir", str(cfg.rclone_dir),
        "--vfs-cache-mode", "full",
        "--vfs-cache-max-size", cfg.vfs_cache_max_size,
        "--vfs-cache-max-age", "8760h",
        "--vfs-cache-min-free-space", cfg.vfs_cache_min_free_space,
        "--dir-cache-time", "1h",
        "--vfs-read-chunk-size", "32M",
        "--vfs-read-chunk-size-limit", "512M",
        "--transfers", "4",
        "--no-checksum",
        # rclone's Windows default (0666) has no execute bit, so every .exe on
        # the mount fails with "Access is denied". Games run straight from H:.
        "--file-perms", "0777",
        "-o", f"VolumeSerialNumber={VOLUME_SERIAL}",
        # vfs/forget needs --rc-no-auth, --rc alone is refused with 403.
        "--rc", "--rc-addr", RC_ADDR, "--rc-no-auth",
    ]


def ensure_mounted(cfg: Config, wait: float = 120.0) -> bool:
    """Start the mount unless it is already there. True when the drive is up."""
    if rclone_running():
        return True
    exe = rclone_exe()
    if exe is None:
        log.error("rclone is not on PATH (winget install Rclone.Rclone WinFsp.WinFsp)")
        return False
    if drive_present(cfg):
        # The letter exists but nothing answers on the rc port: someone else
        # owns it, or rclone is wedged. Starting a second one would only fight.
        log.error("%s exists but rclone is not answering on %s; not touching it",
                  cfg.mount_drive, RC_ADDR)
        return False
    cfg.rclone_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # Backend options via environment, not a ":webdav,url=http://...:" string:
    # rclone splits remote from path at the first colon.
    env["RCLONE_WEBDAV_URL"] = f"http://{cfg.host}:{cfg.port}"
    env["RCLONE_WEBDAV_VENDOR"] = "other"
    flags = 0
    if os.name == "nt":
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                 | subprocess.CREATE_NO_WINDOW)
    log_path = cfg.cache_dir / "rclone.log"
    with open(log_path, "ab") as out:
        subprocess.Popen(
            mount_command(cfg, exe) + ["--log-level", "NOTICE"],
            env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
            creationflags=flags, close_fds=True,
        )
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if drive_present(cfg) and rclone_running():
            log.info("mounted %s (rclone log: %s)", cfg.mount_drive, log_path)
            return True
        time.sleep(0.5)
    log.error("rclone started but %s did not appear within %.0fs; see %s",
              cfg.mount_drive, wait, log_path)
    return False


def unmount(cfg: Config, wait: float = 30.0) -> bool:
    """Ask rclone to leave. Never force-kills; False means it is still there."""
    if not rclone_running():
        return True
    _rc("core/quit")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if not rclone_running() and not drive_present(cfg):
            return True
        time.sleep(0.5)
    log.error("rclone did not leave within %.0fs and was NOT killed: a force-kill "
              "under open readers leaves an unreapable process. Close whatever "
              "has %s open and run stop again.", wait, cfg.mount_drive)
    return False


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    cfg = load_config()
    if cmd == "ensure":
        return 0 if ensure_mounted(cfg) else 1
    if cmd == "stop":
        return 0 if unmount(cfg) else 1
    if cmd == "check":  # exit 0 only when rclone answers AND the drive is there
        return 0 if (rclone_running() and drive_present(cfg)) else 1
    if cmd == "status":
        print(f"rclone: {'running' if rclone_running() else 'not running'}; "
              f"{cfg.mount_drive}: {'present' if drive_present(cfg) else 'absent'}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
