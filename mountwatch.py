"""Notice when rclone's pacer has leaked its tokens.

2026-11-01, after 7.4 days of uptime: every uncached folder on H: hung behind
one dir lock, and the holder was waiting in ``pacer.beginCall`` for a token
that never came (5 goroutines waiting, 0 inside a call). The bridge answered
PROPFIND in 3ms throughout -- nothing here was wrong, so the user found out
the only way left: "H: is slow".

This reads rclone's goroutine dump (rc is already up with ``--rc-no-auth``)
every few minutes and says so in ``bridge.log`` and ``/rpc/status``.
Remounting is the user's call (it unmounts H: and drops the dir cache), so
``auto_recover_mount`` is off by default.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger("mountwatch")

INTERVAL = 300.0     # seconds between samples
STREAK = 3           # stalled samples in a row before it counts (~10 minutes)
FETCH_TIMEOUT = 10.0

_WAIT = "lib/pacer.(*Pacer).beginCall("
_REFILL = "lib/pacer.(*Pacer).beginCall.func1("
_CALL = "lib/pacer.(*Pacer).call("


@dataclass(frozen=True)
class PacerSample:
    waiting: int     # parked in beginCall for a token
    active: int      # past beginCall, inside a paced call
    refilling: int   # the timer goroutine that puts the next token back

    @property
    def stalled(self) -> bool:
        # Waiters with nobody working and no token on its way: nothing will
        # ever wake them. A refill goroutine means the wait is just pacing.
        return self.waiting > 0 and self.active == 0 and self.refilling == 0


def count_pacer(dump: str) -> PacerSample:
    """Classify goroutines in a ``debug/pprof/goroutine?debug=2`` dump."""
    waiting = active = refilling = 0
    for block in dump.split("\n\n"):
        # "created by X" names the parent, not a frame of this goroutine.
        frames = [line for line in block.splitlines() if not line.startswith("created by ")]
        text = "\n".join(frames)
        if _REFILL in text:
            refilling += 1
        elif _WAIT in text:
            waiting += 1
        elif _CALL in text:
            active += 1
    return PacerSample(waiting, active, refilling)


def fetch_dump(rc_addr: str) -> Optional[str]:
    url = f"http://{rc_addr}/debug/pprof/goroutine?debug=2"
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return None


class MountWatch:
    def __init__(self, *, fetch: Callable[[], Optional[str]], auto_recover: bool = False,
                 recover: Optional[Callable[[], bool]] = None, interval: float = INTERVAL):
        self._fetch = fetch
        self._auto_recover = auto_recover
        self._recover = recover
        self._interval = interval
        self._streak = 0
        self._exhausted = False
        self._recover_tried = False
        self._last: Optional[PacerSample] = None
        self._checked_at: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def sample(self) -> None:
        text = self._fetch()
        if text is None:
            # rclone not answering is a different problem (mountctl's), and
            # not evidence either way about the pacer.
            self._streak = 0
            return
        s = count_pacer(text)
        self._last = s
        self._checked_at = time.time()
        if not s.stalled:
            self._streak = 0
            self._recover_tried = False
            if self._exhausted:
                self._exhausted = False
                log.info("rclone pacer recovered (waiting=%d active=%d)", s.waiting, s.active)
            return
        self._streak += 1
        if self._streak < STREAK or self._exhausted:
            return
        self._exhausted = True
        log.error(
            "rclone pacer exhausted: %d goroutines waiting for a token, none in a call, "
            "for %d samples in a row. Uncached folders on the drive will hang. "
            "Restart the mount (stop.bat, then start.bat) when nothing is uploading.",
            s.waiting, self._streak,
        )
        if self._auto_recover and not self._recover_tried and self._recover is not None:
            self._recover_tried = True
            try:
                done = self._recover()
            except Exception:
                log.exception("automatic mount recovery failed")
                return
            log.warning("automatic mount recovery %s", "done" if done else "skipped (mount busy)")

    def status(self) -> dict:
        s = self._last
        return {
            "pacer_exhausted": self._exhausted,
            "pacer_waiting": s.waiting if s else None,
            "pacer_active": s.active if s else None,
            "pacer_checked_at": self._checked_at,
        }

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="mountwatch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.sample()
            except Exception:
                log.exception("mount watch sample failed")


def remount_when_quiet(cfg, resolver, quiet: float = 60.0) -> bool:
    """Remount only with no writeback pending and no recent foreground read.

    Never force-kills: ``mountctl.unmount`` is ``core/quit`` and gives up if
    rclone will not leave.
    """
    import mountctl

    stats = mountctl._rc("vfs/stats") or {}
    cache = stats.get("diskCache") or {}
    if cache.get("uploadsInProgress", 1) or cache.get("uploadsQueued", 1):
        return False
    if time.monotonic() - getattr(resolver, "_last_demand", 0.0) < quiet:
        return False
    if not mountctl.unmount(cfg):
        return False
    return mountctl.ensure_mounted(cfg)
