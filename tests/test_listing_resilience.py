"""A slow or failing backend must not hold a folder listing hostage.

rclone keeps the folder's lock for as long as a listing takes, and Explorer
queues behind it (measured 2026-11-14: one stalled /files froze H:\\pixiv for
80s+). So: answer within list_deadline_seconds, from the last known copy if
need be; with no copy at all, fail fast with 503.
"""

import json
import threading
import time

import pytest

import tdapi
from tdapi import BackendUnavailable, TeleDriveClient


class Cfg:
    def __init__(self, tmp_path, dir_cache_seconds=3600, list_deadline_seconds=0.3):
        self.base_url = "https://backend.example"
        self.api_base = "https://backend.example/api/v1"
        self.cache_dir = tmp_path
        self.dir_cache_seconds = dir_cache_seconds
        self.list_deadline_seconds = list_deadline_seconds


def row(fid, name, is_dir=False, parent_id=None):
    return {"file_id": fid, "filename": name, "isDir": is_dir, "parent_id": parent_id,
            "filesize": 1, "telegram_message_id": None if is_dir else 7,
            "created_at": "2026-01-01T00:00:00"}


class Backend:
    """Canned rows; can be switched to hang, fail, or answer."""

    def __init__(self, rows):
        self.rows = rows
        self.mode = "ok"  # ok | fail | hang
        self.release = threading.Event()
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self, method, path, *, params=None, **kw):
        with self.lock:
            self.calls += 1
        if self.mode == "fail":
            raise tdapi.ApiError(530, "origin unreachable")
        if self.mode == "hang":
            self.release.wait(timeout=5)
        wanted = [r for r in self.rows if bool(r["isDir"]) == (path == "/folders")]
        return {"files": wanted, "total": len(wanted)}


def make(tmp_path, backend, **cfg):
    api = TeleDriveClient(Cfg(tmp_path, **cfg))
    api._call = backend
    api._token = "JWT"
    return api


ROWS = [row("f1", "a.jpg", parent_id="P"), row("d1", "sub", is_dir=True, parent_id="P")]


def expire(api, parent_id):
    """Age the memory copy and the disk copy past dir_cache_seconds."""
    with api._dir_lock:
        stamp, entries = api._dir_cache[parent_id]
        api._dir_cache[parent_id] = (stamp - 10_000, entries)
    path = api._dir_disk_path(parent_id)
    blob = json.loads(path.read_text(encoding="utf-8"))
    blob["at"] -= 10_000
    path.write_text(json.dumps(blob), encoding="utf-8")


def names(entries):
    return sorted(e.name for e in entries)


def test_a_hung_backend_answers_from_the_expired_memory_copy_within_the_deadline(tmp_path):
    backend = Backend(ROWS)
    api = make(tmp_path, backend)
    assert names(api.list_dir("P")) == ["a.jpg", "sub"]
    expire(api, "P")
    backend.mode = "hang"
    started = time.monotonic()
    assert names(api.list_dir("P")) == ["a.jpg", "sub"]
    assert time.monotonic() - started < 2.0
    assert api.stale_listing_count() == 1
    backend.release.set()


def test_after_a_restart_the_disk_copy_answers_at_any_age(tmp_path):
    backend = Backend(ROWS)
    first = make(tmp_path, backend)
    first.list_dir("P")
    expire(first, "P")
    backend.mode = "fail"
    second = make(tmp_path, backend)  # a fresh process: memory is empty
    assert names(second.list_dir("P")) == ["a.jpg", "sub"]


def test_no_copy_at_all_is_a_prompt_503(tmp_path):
    backend = Backend(ROWS)
    backend.mode = "fail"
    api = make(tmp_path, backend)
    with pytest.raises(BackendUnavailable) as err:
        api.list_dir("P")
    assert err.value.status == 503


def test_a_folder_that_just_failed_is_answered_without_waiting(tmp_path):
    backend = Backend(ROWS)
    api = make(tmp_path, backend)
    api.list_dir("P")
    expire(api, "P")
    backend.mode = "hang"
    api.list_dir("P")  # waits the deadline once
    calls = backend.calls
    started = time.monotonic()
    for _ in range(5):
        api.list_dir("P")
    assert time.monotonic() - started < 0.2
    assert backend.calls == calls  # no new requests during the cooldown
    backend.release.set()


def test_ten_threads_on_one_expired_folder_cost_one_backend_listing(tmp_path):
    backend = Backend(ROWS)
    api = make(tmp_path, backend, list_deadline_seconds=3)
    api.list_dir("P")
    expire(api, "P")
    before = backend.calls
    backend.mode = "hang"
    results = []
    threads = [threading.Thread(target=lambda: results.append(names(api.list_dir("P"))))
               for _ in range(10)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    backend.release.set()
    for t in threads:
        t.join(timeout=5)
    assert results == [["a.jpg", "sub"]] * 10
    assert backend.calls - before == 2  # one /folders + one /files


def test_recovery_retires_the_stale_copy(tmp_path, caplog):
    backend = Backend(ROWS)
    api = make(tmp_path, backend)
    api.list_dir("P")
    expire(api, "P")
    backend.mode = "fail"
    api.list_dir("P")
    assert api.stale_listing_count() == 1
    backend.mode = "ok"
    backend.rows = ROWS + [row("f2", "new.jpg", parent_id="P")]
    with api._dir_lock:
        api._list_failed_at["P"] -= tdapi.STALE_RETRY_SECONDS + 1
    api.list_dir("P")  # answers stale, refreshes behind it
    deadline = time.monotonic() + 2
    while api.stale_listing_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert api.stale_listing_count() == 0
    assert names(api.list_dir("P")) == ["a.jpg", "new.jpg", "sub"]


def test_a_fresh_listing_never_takes_the_stale_copy(tmp_path):
    backend = Backend(ROWS)
    api = make(tmp_path, backend)
    api.list_dir("P")
    path = api._dir_disk_path("P")
    before = path.read_bytes()
    backend.mode = "fail"
    with pytest.raises(tdapi.ApiError):
        api.list_dir("P", fresh=True)
    assert path.read_bytes() == before


def test_the_stale_warning_is_logged_once_per_folder(tmp_path, caplog):
    backend = Backend(ROWS)
    api = make(tmp_path, backend)
    api.list_dir("P")
    expire(api, "P")
    backend.mode = "fail"
    with caplog.at_level("WARNING", logger="tdapi"):
        for _ in range(3):
            api.list_dir("P")
            with api._dir_lock:
                api._list_failed_at.pop("P", None)  # skip the cooldown: hit the backend again
    assert caplog.text.count("answering from the last known copy") == 1


def test_list_deadline_must_be_between_2_and_60():
    from config import ConfigError, list_deadline
    assert list_deadline("8") == 8.0
    for bad in ("1", "61", "0", "x"):
        with pytest.raises(ConfigError):
            list_deadline(bad)


def test_a_down_backend_is_waited_on_once_per_path_not_once_per_level(tmp_path):
    """2026-11-15 on the real H:: root, pixiv, user-62133 each waited the full
    deadline in turn, so one click cost 24s instead of 8s. Once one listing has
    timed out, the others answer from their copies (or 503) without waiting."""
    backend = Backend(ROWS)
    api = make(tmp_path, backend, list_deadline_seconds=0.5)
    api.list_dir("P")
    api.list_dir("Q")
    expire(api, "P")
    expire(api, "Q")
    backend.mode = "hang"
    api.list_dir("P")  # pays the deadline once
    started = time.monotonic()
    assert names(api.list_dir("Q")) == ["a.jpg", "sub"]
    with pytest.raises(BackendUnavailable):
        api.list_dir("R")  # never listed
    assert time.monotonic() - started < 0.2
    backend.release.set()


def test_one_folder_refused_by_the_backend_does_not_mark_it_down(tmp_path):
    backend = Backend(ROWS)
    api = make(tmp_path, backend, list_deadline_seconds=0.5)
    api.list_dir("Q")
    expire(api, "Q")
    real = backend.__call__

    def refuse_p(method, path, *, params=None, **kw):
        if (params or {}).get("parent_id") == "P":
            raise tdapi.ApiError(404, "no such folder")
        return real(method, path, params=params, **kw)

    api._call = refuse_p
    with pytest.raises(BackendUnavailable):
        api.list_dir("P")
    backend.rows = ROWS + [row("f2", "new.jpg", parent_id="Q")]
    assert "new.jpg" in names(api.list_dir("Q"))  # asked the backend, did not short-circuit
