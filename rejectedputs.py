"""Stop rclone from retrying a PUT the bridge keeps refusing.

rclone's VFS writeback has no retry limit: the cached copy is the only one, so
it keeps re-sending a failed upload forever, backing off to every 5 minutes.
Nothing on its side can be configured to give up, and it treats a 4xx exactly
like a 500. Found in the field: 266 files at ``try #151``, 24,104 errors and a
163 MB ``rclone.log`` -- every retry also spending one of rclone's shared
backend call slots, the same slots a directory listing on H: waits for.

So the bridge gives up on its behalf. A path whose PUT has failed ``limit``
times, the first at least ``min_span`` seconds ago, is accepted on the next
try: the body is written under ``quarantine_dir`` (never dropped -- it may be
the only copy) and the PUT answers 201. rclone then confirms the upload with a
``Depth: 0`` PROPFIND (``webdav.Object.Update`` -> ``readMetaData``), which
would 404 and restart the loop, so that path keeps answering as a plain file
for ``hold_seconds``. After that it simply is not there, and the next listing
of its folder drops it from H:.

``min_span`` is what keeps a short backend outage from quarantining real
writes: rclone's first retries come seconds apart.
"""

from __future__ import annotations

import logging
import threading
import time
from email.utils import formatdate
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote
from xml.sax.saxutils import escape

import fileid
from wsgidav.util import re_encode_wsgi

from config import ext_path as _ext

log = logging.getLogger("rejectedputs")

REJECT_LIMIT = 3
REJECT_MIN_SPAN = 30 * 60
HOLD_SECONDS = 15 * 60
_FORBIDDEN = '<>:"|?*'
_COPY_BLOCK = 1024 * 1024


def _segments(path: str) -> List[str]:
    out = []
    for seg in path.replace("\\", "/").split("/"):
        if seg in ("", ".", ".."):
            continue
        out.append("".join("_" if c in _FORBIDDEN or ord(c) < 32 else c for c in seg))
    return out


class _Settle:
    """Pass a WSGI body through and run ``done`` exactly once when it closes."""

    def __init__(self, inner, done: Callable[[], None]):
        self._inner = inner
        self._done = done
        self._settled = False

    def __iter__(self):
        return iter(self._inner)

    def close(self):
        try:
            close = getattr(self._inner, "close", None)
            if close is not None:
                close()
        finally:
            if not self._settled:
                self._settled = True
                self._done()


class RejectedPuts:
    def __init__(self, app, quarantine_dir: Path, *, limit: int = REJECT_LIMIT,
                 min_span: float = REJECT_MIN_SPAN, hold_seconds: float = HOLD_SECONDS,
                 clock: Callable[[], float] = time.time):
        self.app = app
        self.quarantine_dir = Path(quarantine_dir)
        self.limit = limit
        self.min_span = min_span
        self.hold_seconds = hold_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self._failures: Dict[str, Tuple[int, float]] = {}  # path -> (count, first failure)
        self._held: Dict[str, Tuple[int, float, float]] = {}  # path -> (size, mtime, expires)

    # -- bookkeeping ------------------------------------------------------ #

    def _record(self, key: str, ok: bool) -> None:
        with self._lock:
            if ok:
                self._failures.pop(key, None)
                return
            count, first = self._failures.get(key, (0, self.clock()))
            self._failures[key] = (count + 1, first)

    def _tripped(self, key: str) -> Optional[Tuple[int, float]]:
        with self._lock:
            entry = self._failures.get(key)
        if entry is None:
            return None
        count, first = entry
        if count >= self.limit and self.clock() - first >= self.min_span:
            return entry
        return None

    def _held_entry(self, key: str) -> Optional[Tuple[int, float, float]]:
        with self._lock:
            entry = self._held.get(key)
            if entry is not None and entry[2] < self.clock():
                del self._held[key]
                entry = None
        return entry

    # -- WSGI ------------------------------------------------------------- #

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "").upper()
        if method not in ("PUT", "PROPFIND"):
            return self.app(environ, start_response)
        key = re_encode_wsgi(environ.get("PATH_INFO", "")).rstrip("/")

        if method == "PROPFIND":
            held = self._held_entry(key)
            if held is not None and environ.get("HTTP_DEPTH", "infinity") == "0":
                return self._propfind(key, held, start_response)
            return self.app(environ, start_response)

        tripped = self._tripped(key)
        if tripped is not None:
            return self._quarantine(environ, start_response, key, tripped)

        seen: Dict[str, str] = {}

        def capture(status, headers, exc_info=None):
            seen["status"] = status
            return start_response(status, headers, exc_info) if exc_info else start_response(status, headers)

        # WsgiDAVApp is a generator: start_response only runs once the body is
        # iterated, so the verdict is taken when the server closes the response.
        try:
            result = self.app(environ, capture)
        except Exception:
            self._record(key, ok=False)
            raise
        return _Settle(result, lambda: self._record(key, ok=seen.get("status", "500").startswith("2")))

    def _quarantine(self, environ, start_response, key: str, tripped: Tuple[int, float]):
        segments = _segments(key) or ["unnamed"]
        target = self.quarantine_dir.joinpath(*segments)
        n = 1
        while Path(_ext(target)).exists():
            stem, dot, ext = segments[-1].rpartition(".")
            name = f"{stem} ({n}).{ext}" if dot and stem else f"{segments[-1]} ({n})"
            target = self.quarantine_dir.joinpath(*segments[:-1], name)
            n += 1
        Path(_ext(target.parent)).mkdir(parents=True, exist_ok=True)

        stream = environ.get("wsgi.input")
        remaining = environ.get("CONTENT_LENGTH")
        remaining = int(remaining) if remaining and remaining.isdigit() else None
        size = 0
        with open(_ext(target), "wb") as out:
            while stream is not None and (remaining is None or remaining > 0):
                want = _COPY_BLOCK if remaining is None else min(_COPY_BLOCK, remaining)
                block = stream.read(want)
                if not block:
                    break
                out.write(block)
                size += len(block)
                if remaining is not None:
                    remaining -= len(block)

        count, first = tripped
        with self._lock:
            self._failures.pop(key, None)
            self._held[key] = (size, self.clock(), self.clock() + self.hold_seconds)
        log.warning(
            "PUT %s refused %d times over %.0f min -- accepted into the rejected folder so rclone stops retrying",
            fileid.tag(key.lstrip("/")), count, (self.clock() - first) / 60,
        )
        start_response("201 Created", [("Content-Length", "0")])
        return [b""]

    def _propfind(self, key: str, held: Tuple[int, float, float], start_response):
        size, mtime, _expires = held
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<D:multistatus xmlns:D="DAV:"><D:response>'
            f"<D:href>{escape(quote(key))}</D:href>"
            "<D:propstat><D:prop><D:resourcetype/>"
            f"<D:getcontentlength>{size}</D:getcontentlength>"
            f"<D:getlastmodified>{formatdate(mtime, usegmt=True)}</D:getlastmodified>"
            "<D:getcontenttype>application/octet-stream</D:getcontenttype>"
            "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
            "</D:response></D:multistatus>"
        ).encode("utf-8")
        start_response("207 Multi-Status", [
            ("Content-Type", 'application/xml; charset="utf-8"'),
            ("Content-Length", str(len(body))),
        ])
        return [body]
