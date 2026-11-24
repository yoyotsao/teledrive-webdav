"""rejectedputs.RejectedPuts: a PUT refused often enough, for long enough, is
accepted into quarantine so rclone's endless writeback retry stops."""

import io

from rejectedputs import RejectedPuts


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def refusing_app(calls):
    def app(environ, start_response):
        calls.append(environ["REQUEST_METHOD"])
        # Generator, like WsgiDAVApp: start_response runs during iteration.
        def body():
            start_response("500 Internal Server Error", [("Content-Length", "0")])
            yield b""
        return body()
    return app


def ok_app(environ, start_response):
    def body():
        start_response("201 Created", [("Content-Length", "0")])
        yield b""
    return body()


def call(app, method, path, data=b"", depth=None):
    environ = {
        "REQUEST_METHOD": method,
        # WSGI carries the UTF-8 path as latin-1 text.
        "PATH_INFO": path.encode("utf-8").decode("latin-1"),
        "CONTENT_LENGTH": str(len(data)),
        "wsgi.input": io.BytesIO(data),
    }
    if depth is not None:
        environ["HTTP_DEPTH"] = depth
    seen = {}

    def start_response(status, headers, exc_info=None):
        seen["status"] = status

    result = app(environ, start_response)
    body = b"".join(result)
    if hasattr(result, "close"):
        result.close()
    return seen["status"], body


def test_refused_put_is_quarantined_only_after_limit_and_span(tmp_path):
    calls, clock = [], Clock()
    app = RejectedPuts(refusing_app(calls), tmp_path / "rejected", limit=3, min_span=1800, clock=clock)
    path = "/AV/資料夾/._1 (7).jpg"

    for _ in range(3):
        assert call(app, "PUT", path, b"x")[0].startswith("500")
    # Three quick failures are not enough: a short backend outage looks like this.
    assert call(app, "PUT", path, b"x")[0].startswith("500")
    assert len(calls) == 4

    clock.now += 1800
    status, _ = call(app, "PUT", path, b"payload")
    assert status.startswith("201")
    assert len(calls) == 4  # never reached the inner app
    assert (tmp_path / "rejected" / "AV" / "資料夾" / "._1 (7).jpg").read_bytes() == b"payload"


def test_quarantined_path_answers_rclones_confirming_propfind(tmp_path):
    calls, clock = [], Clock()
    app = RejectedPuts(refusing_app(calls), tmp_path, limit=1, min_span=0, hold_seconds=60, clock=clock)
    call(app, "PUT", "/a/b.bin", b"1")
    call(app, "PUT", "/a/b.bin", b"hello")

    status, body = call(app, "PROPFIND", "/a/b.bin", depth="0")
    assert status.startswith("207")
    assert b"<D:getcontentlength>5</D:getcontentlength>" in body

    clock.now += 61
    call(app, "PROPFIND", "/a/b.bin", depth="0")
    assert calls[-1] == "PROPFIND"  # hold expired: back to the real provider


def test_success_resets_the_count(tmp_path):
    clock = Clock()
    state = {"ok": False}

    def app(environ, start_response):
        return (ok_app if state["ok"] else refusing_app([]))(environ, start_response)

    guard = RejectedPuts(app, tmp_path, limit=2, min_span=0, clock=clock)
    call(guard, "PUT", "/f", b"x")
    state["ok"] = True
    call(guard, "PUT", "/f", b"x")
    state["ok"] = False
    assert call(guard, "PUT", "/f", b"x")[0].startswith("500")  # count restarted at 1


def test_existing_quarantine_file_is_not_overwritten(tmp_path):
    app = RejectedPuts(refusing_app([]), tmp_path, limit=1, min_span=0, clock=Clock())
    (tmp_path / "x.jpg").write_bytes(b"old")
    call(app, "PUT", "/x.jpg", b"1")
    call(app, "PUT", "/x.jpg", b"new")
    assert (tmp_path / "x.jpg").read_bytes() == b"old"
    assert (tmp_path / "x (1).jpg").read_bytes() == b"new"
