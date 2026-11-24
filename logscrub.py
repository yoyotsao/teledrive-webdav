"""Output-side backstop: no drive file name leaves through a log line.

The call sites that *build* log strings pass ``fileid`` tags (that is the real
mechanism). This catches what they cannot: exception messages and tracebacks
(``OSError`` carries the full local path), and anything a library prints.

It is a ``Formatter`` and not a ``Filter`` on purpose. ``Handler.handle`` runs
filters first and ``emit`` formats afterwards, and a traceback is only turned
into text inside ``Formatter.format`` — a filter sees ``record.msg`` and an
unformatted ``exc_info`` and can never touch the traceback.

Rules, applied to the fully formatted text:

* a local path under ``cache_dir`` that ``fileid.tag_local`` recognises
  -> its ``id=`` tag; one it does not recognise -> ``<cache>\\...``
* a path on the mounted drive (``H:\\...``) -> its ``id=`` tag
* ``http://127.0.0.1:<port>/<path>`` and ``...?path=<path>`` -> ``id=`` tag
* every other absolute path stays: ``.venv\\...\\telethon\\x.py`` is exactly
  what a traceback is for.

Quoted paths end at their closing quote. An unquoted path may contain spaces
and nothing says where it ends, so it is taken to the end of the line: losing
the tail of a message is the safe direction for a backstop.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote

import fileid

_PATH_RE = re.compile(
    r"""(?P<q>['"])?"""
    r"""(?P<path>(?:\\{2,4}\?\\{1,2})?[A-Za-z]:\\{1,2}[^\r\n]*?)"""
    r"""(?(q)(?P=q)|(?=[\r\n]|$))"""
)
_URL_RE = re.compile(r"(?P<base>https?://127\.0\.0\.1:\d+)/(?P<rest>[^\s'\"]*)")
_PATH_PARAM_RE = re.compile(r"(?P<lead>[?&]paths?=)(?P<val>[^&\s'\"]*)")
_EXTENDED_PREFIX_RE = re.compile(r"^\\{2,4}\?\\{1,2}")
_RUNS_RE = re.compile(r"\\{2,}")


def _normalise(path: str) -> str:
    return _RUNS_RE.sub("\\\\", _EXTENDED_PREFIX_RE.sub("", path))


def _scrub_path(raw: str, cache_dir: Path, drive: str) -> Optional[str]:
    path = _normalise(raw)
    local = fileid.tag_local(path, cache_dir)
    if local:
        return local
    below = fileid._under(path, cache_dir)
    if below is not None:
        if below[0].lower() == "meta":
            return None  # the program's own state files, not drive content
        return "<cache>\\..."
    return fileid.tag_drive(path, drive)


def _scrub_url_value(value: str, drive: str) -> str:
    decoded = unquote(value)
    # decoded exactly once: a name that literally contains "%41" must hash as such
    return fileid.tag_drive(decoded, drive) or fileid.tag(decoded.strip("/"))


def scrub_text(text: str, cache_dir: Path, drive: str = "H:") -> str:
    cache_dir = Path(cache_dir)

    def url(match):
        base, rest = match.group("base"), match.group("rest")
        path, sep, query = rest.partition("?")
        if path and not path.startswith("rpc/"):  # endpoint names are not drive paths
            path = fileid.tag_dav("/" + path)
        if sep:
            query = _PATH_PARAM_RE.sub(
                lambda m: m.group("lead") + _scrub_url_value(m.group("val"), drive),
                sep + query,
            )
        return f"{base}/{path}{query}"

    def local(match):
        replacement = _scrub_path(match.group("path"), cache_dir, drive)
        quote = match.group("q") or ""
        if replacement is None:
            if quote:
                return match.group(0)
            # An unquoted match runs to the end of the line; this first path is
            # not ours (a code path, say), so keep looking in what follows it.
            head, space, tail = match.group(0).partition(" ")
            return head + space + _PATH_RE.sub(local, tail)
        return quote + replacement + quote

    return _PATH_RE.sub(local, _URL_RE.sub(url, text))


class ScrubbingFormatter(logging.Formatter):
    """``Formatter`` whose output has passed through :func:`scrub_text`."""

    def __init__(self, fmt: Optional[str] = None, *, cache_dir: Path, drive: str = "H:"):
        super().__init__(fmt)
        self._cache_dir = Path(cache_dir)
        self._drive = drive

    def format(self, record: logging.LogRecord) -> str:
        return scrub_text(super().format(record), self._cache_dir, self._drive)


def install_excepthooks(logger: Optional[logging.Logger] = None) -> None:
    """Route uncaught exceptions through logging so they get scrubbed too."""
    log = logger or logging.getLogger("bridge")

    def on_main(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("uncaught exception", exc_info=(exc_type, exc, tb))

    def on_thread(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        log.error(
            "uncaught exception in thread %s", name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = on_main
    threading.excepthook = on_thread


def make_cheroot_error_log(logger: Optional[logging.Logger] = None) -> Callable[..., None]:
    """Replacement for ``HTTPServer.error_log``, which writes straight to stderr."""
    log = logger or logging.getLogger("bridge")

    def error_log(msg="", level=logging.INFO, traceback=False):
        log.log(level, "%s", msg, exc_info=bool(traceback) or None)

    return error_log
