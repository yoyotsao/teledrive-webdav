"""Directory listings: how few backend round trips a folder click can cost.

Offline. The backend here is a stub that records what was asked and when.

The thing being defended is a cost model measured against the real deployment,
where the backend is deliberately reached over the internet (bridge on one
network, backend on another) and answers in about 0.52s:

    resolve "pixiv/user-955496"  ->  4 calls, 2.1s
        GET /folders  0.52s   the level holding pixiv
        GET /files    0.52s
        GET /folders  0.53s   pixiv's own children
        GET /files    0.52s

Two calls per path level, in sequence, is why clicking a subfolder cost 1.06s —
and why the count of files in it made no difference (2 items and 84 items timed
the same). So there is nothing to make faster, only round trips to avoid:
issue the two listings together, and remember the answer across restarts.
"""

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tdapi  # noqa: E402
from tdapi import TeleDriveClient  # noqa: E402


def row(file_id, name, is_dir=False, message_id=None, telegram_user_id=0, parent_id=None):
    return {
        "file_id": file_id,
        "filename": name,
        "parent_id": parent_id,
        "isDir": is_dir,
        "filesize": 1024,
        "created_at": "2026-08-22T14:29:07Z",
        "telegram_message_id": message_id,
        "telegram_user_id": telegram_user_id,
        "has_thumbnail": not is_dir,
    }


class Cfg:
    def __init__(self, tmp_path, dir_cache_seconds=3600):
        self.base_url = "https://backend.example"
        self.api_base = "https://backend.example/api/v1"
        self.session = "S"
        self.cache_dir = tmp_path
        self.dir_cache_seconds = dir_cache_seconds


class Backend:
    """Stands in for the REST API: canned rows, and a record of every call.

    ``hold`` lets a test prove the two listings overlap: /files blocks until
    /folders has been entered, so a sequential implementation deadlocks (and
    fails on the timeout) instead of quietly passing.
    """

    def __init__(self, tree=None, hold=False):
        self.tree = tree or {}
        self.calls = []
        self.lock = threading.Lock()
        self.folders_started = threading.Event()
        self.hold = hold

    def __call__(self, method, path, *, params=None, payload=None, **kw):
        params = params or {}
        parent = params.get("parent_id")
        with self.lock:
            self.calls.append((path, parent))
        if path == "/folders":
            self.folders_started.set()
        elif path == "/files" and self.hold:
            if not self.folders_started.wait(timeout=5):
                raise AssertionError("/folders never started: the two are sequential")
        rows = self.tree.get(parent, [])
        wanted = [r for r in rows if bool(r.get("isDir")) == (path == "/folders")]
        return {"files": wanted, "total": len(wanted)}


def client(tmp_path, backend, **cfg):
    api = TeleDriveClient(Cfg(tmp_path, **cfg))
    api._call = backend
    api._token = "JWT"
    return api


TREE = {
    None: [row("pixiv", "pixiv", is_dir=True)],
    "pixiv": [
        row("u1", "user-955496", is_dir=True, parent_id="pixiv"),
        row("f1", "cover.jpg", message_id=5, parent_id="pixiv"),
    ],
    "u1": [row("f2", "142759167_p0.jpg", message_id=7, parent_id="u1")],
}


# --------------------------------------------------------------------------- #
# one round trip per level instead of two
# --------------------------------------------------------------------------- #


def test_folders_and_files_are_fetched_together(tmp_path):
    backend = Backend(TREE, hold=True)
    api = client(tmp_path, backend)

    names = sorted(e.name for e in api.list_dir("pixiv"))

    assert names == ["cover.jpg", "user-955496"]
    assert sorted(p for p, _ in backend.calls) == ["/files", "/folders"]


def test_a_click_costs_one_level_not_the_whole_path(tmp_path):
    """Being in a folder means its parents are resolved; only the leaf is new."""
    backend = Backend(TREE)
    api = client(tmp_path, backend)

    api.resolve(["pixiv"])
    backend.calls.clear()
    entry = api.resolve(["pixiv", "user-955496"])

    assert entry.file_id == "u1"
    # The one new level, both its listings, and nothing re-walked: two calls,
    # both against pixiv. Four would mean the root was resolved again.
    assert sorted(backend.calls) == [("/files", "pixiv"), ("/folders", "pixiv")]


# --------------------------------------------------------------------------- #
# surviving a restart
# --------------------------------------------------------------------------- #


def test_a_listing_survives_a_new_client(tmp_path):
    """The first click after a bridge restart is the one that used to cost 1.06s."""
    backend = Backend(TREE)
    api = client(tmp_path, backend)
    first = api.list_dir("pixiv")
    assert len(backend.calls) == 2

    fresh_process = client(tmp_path, Backend(TREE))  # nothing in memory
    again = fresh_process.list_dir("pixiv")

    assert [e.file_id for e in again] == [e.file_id for e in first]
    assert fresh_process._call.calls == []  # answered entirely off disk


def test_directory_cache_preserves_storage_identity(tmp_path):
    tree = {"pixiv": [row("f1", "cover.jpg", message_id=5, telegram_user_id=42)]}
    api = client(tmp_path, Backend(tree))
    api.list_dir("pixiv")

    fresh_process = client(tmp_path, Backend(tree))
    cached = fresh_process.list_dir("pixiv")

    assert [(entry.file_id, entry.telegram_user_id) for entry in cached] == [("f1", 42)]
    assert fresh_process._call.calls == []


def test_the_disk_copy_expires_with_the_same_ttl(tmp_path):
    """Aged by rewriting the stamp, not by waiting or by a zero TTL.

    A zero TTL is not the same test: Windows' clock moves in ~15.6ms steps, so
    write and read can land in the same tick and the entry is legitimately not
    yet expired — which failed this test once in three runs while the code was
    behaving correctly.
    """
    api = client(tmp_path, Backend(TREE), dir_cache_seconds=1.0)
    api.list_dir("pixiv")
    path = api._dir_disk_path("pixiv")
    blob = json.loads(path.read_text(encoding="utf-8"))
    blob["at"] = time.time() - 10
    path.write_text(json.dumps(blob), encoding="utf-8")

    later = client(tmp_path, Backend(TREE), dir_cache_seconds=1.0)
    later.list_dir("pixiv")

    assert len(later._call.calls) == 2  # too old to trust, listed again


def test_a_foreign_shape_on_disk_is_ignored(tmp_path):
    api = client(tmp_path, Backend(TREE))
    path = api._dir_disk_path("pixiv")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"v": tdapi.DIR_CACHE_VERSION + 99, "at": time.time(),
                                "rows": [row("x", "ghost.jpg")]}), encoding="utf-8")

    names = [e.name for e in api.list_dir("pixiv")]

    assert "ghost.jpg" not in names
    assert len(api._call.calls) == 2


def test_fresh_bypasses_both_layers_and_rewrites_disk(tmp_path):
    """The sweep is what keeps the cached listings current, so it must not read them."""
    backend = Backend(TREE)
    api = client(tmp_path, backend)
    api.list_dir("pixiv")
    backend.tree = dict(TREE, pixiv=[row("f9", "uploaded-from-the-web.jpg", message_id=9)])
    backend.calls.clear()

    assert [e.name for e in api.list_dir("pixiv")] == ["user-955496", "cover.jpg"]  # cached
    assert [e.name for e in api.list_dir("pixiv", fresh=True)] == ["uploaded-from-the-web.jpg"]
    assert len(backend.calls) == 2

    after = client(tmp_path, Backend(TREE))
    assert [e.name for e in after.list_dir("pixiv")] == ["uploaded-from-the-web.jpg"]
    assert after._call.calls == []


def test_invalidate_clears_the_disk_copy_too(tmp_path):
    """/rpc/forget that only clears memory is a no-op that looks like it worked."""
    backend = Backend(TREE)
    api = client(tmp_path, backend)
    api.list_dir("pixiv")
    api.list_dir(None)

    api.invalidate()
    backend.calls.clear()
    api.list_dir("pixiv")

    assert len(backend.calls) == 2


def test_invalidate_one_folder_leaves_the_others(tmp_path):
    backend = Backend(TREE)
    api = client(tmp_path, backend)
    api.list_dir("pixiv")
    api.list_dir(None)

    api.invalidate("pixiv")
    backend.calls.clear()
    api.list_dir(None)

    assert backend.calls == []  # the root was not forgotten


def test_trash_removes_the_entry_without_refetching_any_directory(tmp_path):
    backend = Backend(TREE)
    api = client(tmp_path, backend)
    api.list_dir(None)
    entry = api.resolve(["pixiv", "cover.jpg"])

    api.trash(entry.file_id, entry.parent_id)
    backend.calls.clear()

    assert [item.name for item in api.list_dir(None)] == ["pixiv"]
    assert [item.name for item in api.list_dir("pixiv")] == ["user-955496"]
    assert backend.calls == []


def test_the_root_and_odd_ids_get_usable_filenames(tmp_path):
    api = client(tmp_path, Backend(TREE))
    root = api._dir_disk_path(None)
    weird = api._dir_disk_path("../../etc/passwd")

    assert root.name == "__root__.json"
    assert weird.parent == root.parent and weird.suffix == ".json"
    assert "/" not in weird.stem and ".." not in weird.stem


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
