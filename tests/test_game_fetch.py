from __future__ import annotations

import threading
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import bridge
from fetchlocal import LocalFetcher
from gamestate import GameState, GameStateError


class Resolver:
    def __init__(self, loc, root):
        self.loc = loc
        self.root = root
        self.paths = []

    def resolve(self, segments):
        self.paths.append(list(segments))
        return self.loc

    def dav_path_from_windows(self, path):
        assert path == r"E:\game\A"
        return ["game", "A"]


class Reader:
    def __init__(self, data, on_first_read=None):
        self.data = data
        self.offset = 0
        self.on_first_read = on_first_read

    def read(self, size):
        if self.on_first_read is not None:
            callback, self.on_first_read = self.on_first_read, None
            callback()
        chunk = self.data[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk

    def close(self):
        pass


def fetcher_for(tmp_path, data=b"payload", on_open=None):
    remote = SimpleNamespace(name="A", file_id="a", is_dir=False)
    api = SimpleNamespace(total_size=lambda entry: len(data), list_dir=lambda _id: [])
    resolver = Resolver(None, tmp_path / "local")
    resolver.api = api
    resolver.open_remote = lambda _entry: Reader(data, on_open)
    resolver.loc = SimpleNamespace(kind=bridge.FILE, entry=remote)
    cfg = SimpleNamespace(local_dir=tmp_path / "local", mount_drive="E:")
    fetcher = LocalFetcher(cfg, resolver)
    return fetcher, resolver


def test_destination_for_matches_existing_plan_root_for_canonical_segments(tmp_path):
    fetcher, resolver = fetcher_for(tmp_path)
    loc = resolver.loc
    items, root = fetcher._plan(loc, ["game", "遊戲, A"])
    resolver.loc = SimpleNamespace(kind=bridge.FILE, entry=SimpleNamespace(name="遊戲, A", file_id="id"))
    assert items
    assert fetcher.destination_for(["game", "遊戲, A"]) == root.parent / "遊戲, A"
    assert resolver.paths == [["game", "遊戲, A"]]


@pytest.mark.parametrize("kind", [bridge.FOLDER, bridge.ZIPDIR, bridge.ZIPFILE, bridge.STAGE_DIR, bridge.STAGE_FILE])
def test_destination_for_uses_plan_root_for_all_existing_location_kinds(tmp_path, kind):
    fetcher, resolver = fetcher_for(tmp_path)
    local = tmp_path / "staging" / "遊戲, A"
    local.mkdir(parents=True)
    (local / "data.bin").write_bytes(b"stage")
    node = SimpleNamespace(name="member.bin", size=3, is_dir=False)
    view = SimpleNamespace(name="遊戲, A", walk=lambda _node: [(Path("member.bin"), node)], open=lambda _node: Reader(b"zip"))
    loc = SimpleNamespace(
        kind=kind,
        entry=SimpleNamespace(name="遊戲, A", file_id="folder", is_dir=True),
        local=local,
        view=view,
        top="遊戲, A",
        zip_node=lambda: node,
    )
    resolver.loc = loc
    segments = ["game", "遊戲, A"]
    _items, planned_root = fetcher._plan(loc, segments)
    assert fetcher.destination_for(segments) == planned_root


def test_fetch_windows_path_keeps_target_output_and_delegates_segments(tmp_path, monkeypatch):
    fetcher, _resolver = fetcher_for(tmp_path)
    calls = []

    def fetch_segments(segments, **kwargs):
        calls.append((segments, kwargs))
        return iter(["OK destination"])

    monkeypatch.setattr(fetcher, "fetch_segments", fetch_segments, raising=False)
    assert list(fetcher.fetch(r"E:\game\A")) == ["target: E:\\game\\A", "OK destination"]
    assert calls == [(["game", "A"], {"skip_existing": False, "cancel": None})]


def test_fetch_segments_rejects_short_read_and_removes_part(tmp_path):
    fetcher, resolver = fetcher_for(tmp_path, data=b"short")
    resolver.loc.entry.name = "short.bin"
    resolver.api.total_size = lambda _entry: 8
    lines = list(fetcher.fetch_segments(["game", "A"], skip_existing=True))
    target = tmp_path / "local" / "short.bin"
    assert any(line.startswith("ERROR ") for line in lines)
    assert not target.exists()
    assert not target.with_name(target.name + ".part").exists()


def test_fetch_segments_cancellation_keeps_partial_and_resume_skips_completed(tmp_path):
    cancel = threading.Event()
    payload = b"x" * (1024 * 1024 + 7)
    fetcher, resolver = fetcher_for(
        tmp_path,
        payload,
        on_open=lambda: None,
    )
    resolver.loc.entry.name = "game.bin"
    # 讓第一個 chunk 寫入後取消，確保未完成檔不會被 rename。
    original_open = resolver.open_remote

    class CancellingReader(Reader):
        def read(self, size):
            chunk = super().read(min(size, 1024 * 1024))
            if chunk:
                cancel.set()
            return chunk

    resolver.open_remote = lambda _entry: CancellingReader(payload)
    lines = list(fetcher.fetch_segments(["game", "A"], skip_existing=True, cancel=cancel))
    target = tmp_path / "local" / "game.bin"
    assert any(line.startswith("CANCELLED") for line in lines)
    assert not target.exists()
    assert target.with_name(target.name + ".part").exists()

    # 下一次忽略残留 part，完整拉取后以正式檔续上。
    cancel.clear()
    resolver.open_remote = original_open
    lines = list(fetcher.fetch_segments(["game", "A"], skip_existing=True, cancel=cancel))
    assert lines[-1].startswith("OK ")
    assert target.read_bytes() == payload
    assert not target.with_name(target.name + ".part").exists()


def test_game_state_validates_canonical_path_and_preserves_commas(tmp_path):
    cfg = SimpleNamespace(game_folder="game", cache_dir=tmp_path / "cache", local_dir=tmp_path / "local")
    resolver = SimpleNamespace()
    fetcher = SimpleNamespace(destination_for=lambda segments: cfg.local_dir / segments[-1])
    state = GameState(cfg, resolver, fetcher)
    assert state.canonical_game_segments("game/遊戲, A") == ["game", "遊戲, A"]
    for invalid in ("game", "game//A", "game/../A", "game/./A", "game\\A", "E:/game/A", "/game/A"):
        with pytest.raises(GameStateError):
            state.canonical_game_segments(invalid)


def test_game_state_background_cancel_resume_and_restart_recovery(tmp_path):
    cfg = SimpleNamespace(game_folder="game", cache_dir=tmp_path / "cache", local_dir=tmp_path / "local")
    root = cfg.local_dir / "遊戲, A"
    started = threading.Event()
    release = threading.Event()
    calls = []

    class FakeFetcher:
        def destination_for(self, _segments):
            return root

        def fetch_segments(self, segments, *, skip_existing, cancel):
            calls.append((segments, skip_existing))
            if len(calls) == 1:
                root.mkdir(parents=True, exist_ok=True)
                started.set()
                yield "PROGRESS 3 8 1/1 1 B/s part"
                release.wait(2)
                yield "CANCELLED download cancelled"
            else:
                root.mkdir(parents=True, exist_ok=True)
                (root / "game.bin").write_bytes(b"complete")
                yield "PROGRESS 8 8 1/1 done"
                yield f"OK {root}"

    fetcher = FakeFetcher()
    state = GameState(cfg, SimpleNamespace(), fetcher)
    assert state.states(["game/遊戲, A"])[0]["status"] == "absent"
    job = state.fetch("game/遊戲, A")
    assert started.wait(2)
    duplicate = state.fetch("game/遊戲, A")
    assert len(calls) == 1
    assert duplicate["status"] == "downloading"
    assert state.states(["game/遊戲, A"])[0]["completed_bytes"] == 3
    state.cancel("game/遊戲, A")
    release.set()
    assert state._jobs["game/遊戲, A"].finished.wait(2)
    assert state.states(["game/遊戲, A"])[0]["status"] == "incomplete"
    assert not (root / GameState.COMPLETE_MARKER).exists()
    restarted_incomplete = GameState(cfg, SimpleNamespace(), fetcher)
    assert restarted_incomplete.states(["game/遊戲, A"])[0]["status"] == "incomplete"

    state.fetch("game/遊戲, A")
    assert state._jobs["game/遊戲, A"].finished.wait(2)
    assert state.states(["game/遊戲, A"])[0]["status"] == "ready"
    saved = json.loads((cfg.cache_dir / "reina-games.json").read_text(encoding="utf-8"))
    assert saved["game/遊戲, A"] == str(root)
    restarted = GameState(cfg, SimpleNamespace(), fetcher)
    assert restarted.states(["game/遊戲, A"])[0]["status"] == "ready"


def test_game_state_running_provider_has_highest_priority(tmp_path):
    cfg = SimpleNamespace(game_folder="game", cache_dir=tmp_path / "cache", local_dir=tmp_path / "local")
    root = cfg.local_dir / "A"
    root.mkdir(parents=True)
    (root / GameState.COMPLETE_MARKER).write_text("done", encoding="utf-8")
    state = GameState(cfg, SimpleNamespace(), SimpleNamespace(destination_for=lambda _segments: root), lambda _path: True)
    assert state.states(["game/A"])[0]["status"] == "running"


def test_running_state_overrides_an_active_download(tmp_path):
    cfg = SimpleNamespace(game_folder="game", cache_dir=tmp_path / "cache", local_dir=tmp_path / "local")
    root = cfg.local_dir / "A"
    started = threading.Event()
    release = threading.Event()

    class Fetcher:
        def destination_for(self, _segments):
            return root

        def fetch_segments(self, _segments, *, skip_existing, cancel):
            started.set()
            release.wait(2)
            yield "CANCELLED download cancelled" if cancel.is_set() else f"OK {root}"

    state = GameState(cfg, SimpleNamespace(), Fetcher(), lambda _path: True)
    state.fetch("game/A")
    assert started.wait(2)
    assert state.states(["game/A"])[0]["status"] == "running"
    state.cancel("game/A")
    release.set()
    assert state._jobs["game/A"].finished.wait(2)


def test_game_state_retains_sanitized_fetcher_error(tmp_path):
    cfg = SimpleNamespace(game_folder="game", cache_dir=tmp_path / "cache", local_dir=tmp_path / "local")
    root = cfg.local_dir / "A"

    class FailedFetcher:
        def destination_for(self, _segments):
            return root

        def fetch_segments(self, _segments, *, skip_existing, cancel):
            yield (
                r"ERROR Permission denied reading C:\private\save.bin; "
                r"session=secret-session authorization: Bearer abc.def.ghi"
            )

    state = GameState(cfg, SimpleNamespace(), FailedFetcher())
    state.fetch("game/A")
    assert state._jobs["game/A"].finished.wait(2)
    error = state.states(["game/A"])[0]["error"]
    assert "Permission denied" in error
    assert "[local path]" in error
    assert "C:\\private" not in error
    assert "abc.def.ghi" not in error
    assert "secret-session" not in error


@pytest.mark.parametrize(
    "local_path",
    [
        r"\\server\share\game\save.bin",
        r"\\server\Shared Games\game\save.bin",
        r"\\?\UNC\server\share\game\save.bin",
        r"\\?\UNC\server\Shared Games\game\save.bin",
        "//server/share/game/save.bin",
        "//server/Shared Games/game/save.bin",
        "//?/UNC/server/share/game/save.bin",
        "//?/UNC/server/Shared Games/game/save.bin",
        r"D:\local games\save.bin",
    ],
)
def test_game_state_redacts_unc_paths_from_returned_error(tmp_path, local_path):
    cfg = SimpleNamespace(game_folder="game", cache_dir=tmp_path / "cache", local_dir=tmp_path / "local")
    root = cfg.local_dir / "A"

    class FailedFetcher:
        def destination_for(self, _segments):
            return root

        def fetch_segments(self, _segments, *, skip_existing, cancel):
            yield f"ERROR Permission denied reading {local_path}"

    state = GameState(cfg, SimpleNamespace(), FailedFetcher())
    state.fetch("game/A")
    assert state._jobs["game/A"].finished.wait(2)
    error = state.states(["game/A"])[0]["error"]
    assert "Permission denied" in error
    assert "[local path]" in error
    assert local_path not in error


def test_cancel_wins_before_atomic_completion_commit(tmp_path):
    cfg = SimpleNamespace(game_folder="game", cache_dir=tmp_path / "cache", local_dir=tmp_path / "local")
    root = cfg.local_dir / "A"
    ok_ready = threading.Event()

    class SuccessFetcher:
        def destination_for(self, _segments):
            root.mkdir(parents=True, exist_ok=True)
            return root

        def fetch_segments(self, _segments, *, skip_existing, cancel):
            ok_ready.set()
            yield f"OK {root}"

    state = GameState(cfg, SimpleNamespace(), SuccessFetcher())
    with state._lock:
        state.fetch("game/A")
        assert ok_ready.wait(2)
        state.cancel("game/A")
    assert state._jobs["game/A"].finished.wait(2)
    assert not (root / GameState.COMPLETE_MARKER).exists()
    assert not (cfg.cache_dir / "reina-games.json").exists()
    assert state.states(["game/A"])[0]["status"] == "incomplete"


def test_completion_wins_and_late_cancel_cannot_be_accepted(tmp_path, monkeypatch):
    cfg = SimpleNamespace(game_folder="game", cache_dir=tmp_path / "cache", local_dir=tmp_path / "local")
    root = cfg.local_dir / "A"
    marker_write = threading.Event()
    release_marker = threading.Event()
    cancel_waiting = threading.Event()
    cancel_done = threading.Event()
    cancel_result = []

    class SuccessFetcher:
        def destination_for(self, _segments):
            root.mkdir(parents=True, exist_ok=True)
            return root

        def fetch_segments(self, _segments, *, skip_existing, cancel):
            yield f"OK {root}"

    class ObservedRLock:
        def __init__(self):
            self.lock = threading.RLock()

        def __enter__(self):
            if threading.current_thread().name == "late-cancel":
                cancel_waiting.set()
            self.lock.acquire()
            return self

        def __exit__(self, _kind, _value, _traceback):
            self.lock.release()

    state = GameState(cfg, SimpleNamespace(), SuccessFetcher())
    state._lock = ObservedRLock()
    original_atomic_write = state._atomic_write

    def pause_before_marker(path, payload):
        if path.name == GameState.COMPLETE_MARKER:
            marker_write.set()
            assert release_marker.wait(2)
        original_atomic_write(path, payload)

    monkeypatch.setattr(state, "_atomic_write", pause_before_marker)
    state.fetch("game/A")
    assert marker_write.wait(2)

    def cancel_late():
        try:
            cancel_result.append(state.cancel("game/A"))
        except GameStateError as exc:
            cancel_result.append(exc)
        finally:
            cancel_done.set()

    cancel_thread = threading.Thread(target=cancel_late, name="late-cancel")
    cancel_thread.start()
    assert cancel_waiting.wait(2)
    release_marker.set()
    assert cancel_done.wait(2)
    assert state._jobs["game/A"].finished.wait(2)
    cancel_thread.join(timeout=2)

    assert len(cancel_result) == 1
    assert isinstance(cancel_result[0], GameStateError)
    assert cancel_result[0].code == "download_not_active"
    assert (root / GameState.COMPLETE_MARKER).is_file()
    assert state.states(["game/A"])[0]["status"] == "ready"


def test_game_state_reports_absent_when_remote_game_is_gone(tmp_path):
    cfg = SimpleNamespace(game_folder="game", cache_dir=tmp_path / "cache", local_dir=tmp_path / "local")

    def gone(_segments):
        raise FileNotFoundError("not found: game/Gone")

    state = GameState(cfg, SimpleNamespace(), SimpleNamespace(destination_for=gone))
    game = state.states(["game/Gone"])[0]
    assert game["status"] == "absent"


def test_game_state_backend_failure_is_a_503_state_error(tmp_path):
    cfg = SimpleNamespace(game_folder="game", cache_dir=tmp_path / "cache", local_dir=tmp_path / "local")

    def down(_segments):
        raise RuntimeError("backend unreachable")

    state = GameState(cfg, SimpleNamespace(), SimpleNamespace(destination_for=down))
    with pytest.raises(GameStateError) as caught:
        state.states(["game/A"])
    assert caught.value.status == 503
