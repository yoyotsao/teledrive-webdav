"""Start the bridge (teledrive_webdav.dav.bridge) with no console window.

A visible console can stall every write to it (a click starts a text selection),
and a thread that writes to stderr directly -- an unraisable exception from the
garbage collector, say -- then blocks while holding the stream lock. Run on the
Telegram loop thread, that froze every upload and read for half an hour with no
log line. bridge.log is the only log anyone needs, so the bridge gets no window:
CREATE_NO_WINDOW still gives it a (invisible) console, which child processes
inherit, so rclone / warmshell / ffmpeg do not flash windows either, and its
stdout/stderr go to NUL, which never blocks.

stop_bridge.ps1 finds the process by its command line (``teledrive_webdav.dav.bridge``), not by a
window, so stopping is unchanged.
"""
import os
import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def launch(root: str, python: str = sys.executable) -> subprocess.Popen:
    return subprocess.Popen(
        [python, "-m", "teledrive_webdav.dav.bridge"],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
    )


if __name__ == "__main__":
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    launch(root)
