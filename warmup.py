r"""Fetch every preview and dimension once, so no folder is ever cold.

Browsing speed has two regimes. Warm, the shell renders about 20 files a second
— the handlers answer in 8ms and 40ms and everything comes off local disk. Cold,
it is whatever Telegram will give: previews arrive in batches at between 8 and 33
a second depending on how hard the account has been pulled recently, and that
rate is not something the code can raise.

So the way to a reliably fast folder is to stop having cold ones. This walks the
tree and fills the preview and property caches. It is resumable — anything
already cached is skipped — so it can be run repeatedly and after new uploads.

The bridge runs the same sweep by itself in the background (`[warmup] auto`), so
normally nothing needs to be run by hand. Do it from the command line when the
whole tree should be warm *now* rather than eventually:

    .venv\Scripts\python.exe warmup.py            # everything
    .venv\Scripts\python.exe warmup.py pixiv      # one subtree

Running it while the bridge is up is safe but pointless: both processes end up
queueing on the same Telegram client loop, and the caches they write are shared.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from config import load_config
from tdapi import Entry, TeleDriveClient
from tgio import TelegramWorker

log = logging.getLogger("warmup")

BATCH = 100  # one get_messages covers this many

# Asks the shell for each thumbnail the way Explorer does, which is the only way
# to get anything into thumbcache_*.db. Nothing here can write that file, and it
# is what separates a folder that has been looked at (266 previews a second, the
# handler never even called) from one this warm-up has filled every cache it can
# reach for (about 3 a second, because the shell still does its own work per
# file whatever we answer).
SHELL_EXE = Path(__file__).resolve().parent / "shellthumb" / "warmshell.exe"
# Paths per warmshell run. Small: this is the only place the shell warm gets to
# yield, and a cold JPEG costs the shell 0.7s (1.5 a second measured, four at a
# time), so a batch of 200 would hold the line for two minutes at a stretch.
# The extra process spawns are ~80ms each and add up to about a minute over the
# whole tree.
SHELL_BATCH = 25
SHELL_THREADS = 4
# Extensions worth asking for a thumbnail. The same set the head cache uses:
# these are the files the shell renders and then goes and reads.
SHELL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# Seconds of quiet before the sweep takes another batch. Much longer than the
# folder prefetch's 0.1s: that one is finishing a folder somebody is looking at,
# this one is speculative and must never be what a request waits behind.
QUIET = 3.0
# Grace period before the first pass. The bridge has only just come up, rclone
# is mounting behind it, and whatever the person came to do beats a head start.
START_DELAY = 30.0
# Between passes. The tree walk costs one listing per folder, and its only
# purpose on a second pass is to notice files uploaded from the web UI since —
# which is not something that needs catching within the hour.
DEFAULT_INTERVAL_MINUTES = 360.0
# Seconds between progress lines while a pass runs. One line per batch would be
# a thousand lines for a hundred thousand files.
PROGRESS_EVERY = 60.0


def walk(
    api: TeleDriveClient,
    parent_id,
    path: str,
    out: List[tuple],
    before: Optional[Callable[[], bool]] = None,
) -> None:
    """Collect ``(path, entry)`` for every file under ``parent_id``.

    ``before`` runs ahead of each listing and stops the walk by returning False.
    The background sweep uses it for both of its manners: yielding to foreground
    requests — the walk is HTTPS to the backend rather than Telegram, but a few
    thousand listings back to back still slow the path resolution every browse
    depends on — and giving up promptly when the bridge is shutting down.
    """
    if before is not None and before() is False:
        return
    for entry in api.list_dir(parent_id):
        if entry.is_dir:
            walk(api, entry.file_id, f"{path}/{entry.name}", out, before)
        elif entry.message_id is not None:
            out.append((f"{path}/{entry.name}", entry))


class Warmer:
    """One pass over the tree, filling the caches the bridge serves from.

    Everything goes through the Resolver instead of writing the cache files
    here. It already batches by hundreds, writes previews atomically under a
    per-writer temp name, and knows when to hold back — reimplementing any of
    that in a second place is how the two copies come to disagree.
    """

    def __init__(
        self,
        resolver,
        *,
        quiet: float = QUIET,
        stop: Optional[threading.Event] = None,
        progress: Optional[Callable[[int, int], None]] = None,
        shell_exe: Optional[Path] = SHELL_EXE,
    ):
        self.resolver = resolver
        self.quiet = quiet
        self.stop = stop
        self.progress = progress or (lambda done, total: None)
        self.shell_exe = shell_exe

    def _stopped(self) -> bool:
        return self.stop is not None and self.stop.is_set()

    def pending(self, start_id=None, base: str = "") -> Tuple[List[tuple], List[tuple]]:
        """``(every file, the ones still owing a preview or dimensions)``.

        A head is not a condition here: it is a scratch file fill() fetches,
        uses and deletes within one batch, so it is never something a later
        pass finds itself still owing.

        Both lists are ``(path, entry)``: the shell warm needs the path, because
        it asks Windows for the thumbnail of a file on the mount rather than
        asking Telegram for anything.

        ``base`` is where ``start_id`` sits in the tree, and is not optional in
        practice when one is given — the walk numbers paths from wherever it
        starts, so warming a subtree without it produces ``H:\\photo.jpg`` for a
        file three folders down. The shell answers that instantly and warms
        nothing, which looks exactly like success.
        """
        files: List[tuple] = []
        walk(self.resolver.api, start_id, base.rstrip("/"), files, before=self._yield_)
        return files, [(p, e) for p, e in files if self.resolver.needs_warming(e)]

    def _yield_(self) -> bool:
        """Wait for a quiet line; False means the caller should stop entirely."""
        if self._stopped():
            return False
        self.resolver.wait_for_quiet(self.quiet)
        return not self._stopped()

    def shell_paths(self, files: List[tuple]) -> List[str]:
        """Windows paths on the mount for the files the shell renders."""
        drive = self.resolver.cfg.mount_drive
        return [
            drive + path.replace("/", "\\")
            for path, entry in files
            if not entry.is_dir
            and "." + entry.name.rsplit(".", 1)[-1].lower() in SHELL_EXTENSIONS
        ]

    def _run_warmshell(self, paths: List[str]) -> int:
        """Run warmshell.exe over ``paths`` in SHELL_BATCH-sized groups; returns how many it warmed.

        Shared by shell_warm() (every still image, every pass) and fill() (just
        this batch, right after its heads land) — both want the same batching,
        subprocess handling, and "count what it warmed, not what it was handed"
        logic, so there is exactly one place that gets it right.
        """
        if self.shell_exe is None or not self.shell_exe.exists():
            return 0
        done = 0
        for at in range(0, len(paths), SHELL_BATCH):
            if not self._yield_():
                break
            group = paths[at : at + SHELL_BATCH]
            try:
                out = subprocess.run(
                    [str(self.shell_exe), str(SHELL_THREADS), "256"],
                    input=("\n".join(group) + "\n").encode("utf-8"),
                    capture_output=True,
                    timeout=600,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("shell warm stopped after %s/%s: %s", done, len(paths), exc)
                break
            # What it warmed, not what it was handed. A path that is not on the
            # mount fails in microseconds, so counting the batch would turn the
            # fastest possible failure into the best-looking number in the log.
            try:
                warmed = int(out.stdout.split()[0])
            except (IndexError, ValueError):
                warmed = 0
            if warmed < len(group):
                log.warning("shell warm: %s of %s in this batch produced nothing (%s ...)",
                            len(group) - warmed, len(group), group[0])
            done += warmed
        return done

    def shell_warm(self, files: List[tuple]) -> int:
        """Ask the shell for every thumbnail, so Windows caches them itself.

        Runs over everything rather than only what still needs warming, and
        keeps no record of what it has done. thumbcache_*.db is Windows' to
        evict — Disk Cleanup empties it, and it trims itself — so a warm-up that
        remembered "already done" would go quiet exactly when the cache it fills
        had been thrown away. Re-warming what is still cached costs 4ms a file.

        Unlike the preview and property warm-ups this one does register as
        demand, and deliberately so: its reads go out through rclone and the
        provider, so they are indistinguishable from somebody browsing. The
        effect is a 3s wait after each batch rather than the batch waiting on
        itself — the reads are finished by the time the next _yield_ runs — and
        that extra politeness is wanted for the one warm-up that drives the
        whole shell pipeline.
        """
        if self.shell_exe is None or not self.shell_exe.exists():
            log.info("no warmshell.exe — skipping the shell warm (build shellthumb\\)")
            return 0
        return self._run_warmshell(self.shell_paths(files))

    def fill(self, todo: List[tuple]) -> int:
        """Fetch previews, properties and thumbnails for ``todo``; returns files processed.

        Each batch also gets its own shell warm right after its heads land, so
        the read the shell insists on making per JPEG (see HEAD_SIZE in
        bridge.py) comes off disk instead of Telegram — the whole reason the
        head cache exists. The head is scratch: it is deleted the moment that
        batch's shell warm is done with it, whether or not the warm succeeded,
        because nothing past that one read ever looks at it again (thumbcache
        or rclone's own VFS cache answer everything after).

        A batch that raises ends the pass rather than moving on to the next one.
        Whatever went wrong — Telegram throttling, the session dropping — will
        almost certainly hit the next batch too, and a sweep that keeps trying is
        a sweep that keeps failing at speed. The next pass starts over, and
        everything already cached is skipped.
        """
        done = 0
        for at in range(0, len(todo), BATCH):
            if not self._yield_():
                break
            pairs = todo[at : at + BATCH]
            chunk = [e for _, e in pairs]
            try:
                self.resolver.thumbs_for(chunk)
                self.resolver.props_for(chunk, demand=False)
                try:
                    self.resolver.heads_for(chunk, before=self._yield_)
                    self._run_warmshell(self.shell_paths(pairs))
                finally:
                    self.resolver.drop_heads(chunk)
            except Exception as exc:
                log.warning("warm-up stopped after %s/%s files: %s", done, len(todo), exc)
                break
            done += len(chunk)
            self.progress(done, len(todo))
        return done


class BackgroundWarmup:
    """Keeps the sweep running for as long as the bridge is up.

    In-process rather than a scheduled task launching warmup.py: this process
    already holds the Telegram connection and the caches, and a second one only
    adds a competitor for the same single client loop — the thing the whole
    quiet-period dance exists to keep clear.
    """

    def __init__(
        self,
        resolver,
        *,
        interval_minutes: float = DEFAULT_INTERVAL_MINUTES,
        start_delay: float = START_DELAY,
    ):
        self.resolver = resolver
        self.interval = max(60.0, interval_minutes * 60.0)
        self.start_delay = start_delay
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._logged_at = 0.0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="warmup", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            # Only long enough to leave the current batch; the thread is a daemon
            # and every cache it writes is complete after each batch anyway.
            self._thread.join(timeout=5)

    def _run(self) -> None:
        if self._stop.wait(self.start_delay):
            return
        while not self._stop.is_set():
            try:
                self._pass()
            except Exception as exc:  # a warm-up failure must never take the bridge down
                log.warning("warm-up pass failed: %s", exc)
            self._stop.wait(self.interval)

    def _pass(self) -> None:
        warmer = Warmer(self.resolver, stop=self._stop, progress=self._note)
        files, todo = warmer.pending()
        if self._stop.is_set():
            return
        started = time.monotonic()
        self._logged_at = started
        if todo:
            log.info("warm-up: %s of %s files need a preview or dimensions",
                     len(todo), len(files))
            done = warmer.fill(todo)
            log.info("warm-up: cached %s files in %.1f min", done, (time.monotonic() - started) / 60)
        else:
            log.info("warm-up: all %s files already cached", len(files))
        if self._stop.is_set():
            return
        # Every pass, even when nothing needed caching: this one fills Windows'
        # thumbnail cache, which Windows also empties without telling anyone.
        started = time.monotonic()
        warmed = warmer.shell_warm(files)
        if warmed:
            log.info("warm-up: asked the shell for %s thumbnails in %.1f min",
                     warmed, (time.monotonic() - started) / 60)
        # Backstop, not the primary cleanup: fill() already drops each batch's
        # heads right after using them. This only catches what a crash mid-batch
        # left behind.
        self.resolver.clear_heads()

    def _note(self, done: int, total: int) -> None:
        now = time.monotonic()
        if now - self._logged_at < PROGRESS_EVERY:
            return
        self._logged_at = now
        log.info("warm-up: %s/%s", done, total)


def main(argv: List[str]) -> int:
    # Imported here, not at module scope: bridge.py imports this module, and the
    # two would import each other.
    from bridge import Resolver

    cfg = load_config()
    api = TeleDriveClient(cfg)
    api.login()

    start_id = None
    label = base = ""
    if argv:
        parts = [s for s in argv[0].replace("\\", "/").split("/") if s]
        entry = api.resolve(parts)
        if entry is None or not entry.is_dir:
            print(f"[error] no such folder: {argv[0]}")
            return 1
        # base, not just a label: paths are what the shell warm asks Windows
        # about, and the walk numbers them from wherever it is told to start.
        start_id, base = entry.file_id, "/" + "/".join(parts)
        label = base

    worker = TelegramWorker(cfg.api_id, cfg.api_hash, cfg.session, cfg.download_connections)
    worker.start()
    resolver = Resolver(cfg, api, worker)
    (cfg.cache_dir / "thumbs").mkdir(parents=True, exist_ok=True)

    started = time.monotonic()  # reset once the walk is done and fetching starts

    def show(done: int, total: int) -> None:
        elapsed = time.monotonic() - started
        rate = done / elapsed if elapsed else 0
        left = (total - done) / rate if rate else 0
        print(f"  {done}/{total}  {rate:.1f}/s  ~{left / 60:.1f} min left", flush=True)

    # Nothing is competing for the Telegram loop when this is run on its own, so
    # the quiet period only costs time. The bridge's own requests still register
    # as demand if it happens to be up.
    warmer = Warmer(resolver, quiet=0.0, progress=show)

    print(f"walking {label or '/'} ...", flush=True)
    files, todo = warmer.pending(start_id, base)
    print(f"{len(files)} files", flush=True)
    print(f"{len(files) - len(todo)} already cached, {len(todo)} to fetch", flush=True)

    # Dimensions matter as much as previews. Explorer asks for both per file, and
    # a cold property read costs about 140ms — enough on its own to hold the
    # shell under ten files a second. They come off the same documents the
    # preview batch already fetches, so warming them is nearly free.
    started = time.monotonic()
    done = warmed = 0
    try:
        if todo:
            done = warmer.fill(todo)
            elapsed = time.monotonic() - started
            print(f"cached {done} files in {elapsed / 60:.1f} min ({done / max(elapsed, 1e-9):.1f}/s)")
        # Unconditional: the caches above make the handler fast, this one makes
        # Windows stop asking it at all. Nothing records that a file has been
        # through it, because Windows evicts thumbcache_*.db on its own.
        print("asking the shell for thumbnails ...", flush=True)
        started = time.monotonic()
        warmed = warmer.shell_warm(files)
    except KeyboardInterrupt:
        print("\ninterrupted — rerun to continue where this stopped")
    finally:
        resolver.clear_heads()
        worker.stop()

    elapsed = time.monotonic() - started
    if warmed:
        print(f"shell-warmed {warmed} files in {elapsed / 60:.1f} min "
              f"({warmed / max(elapsed, 1e-9):.1f}/s)")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s")
    raise SystemExit(main(sys.argv[1:]))
