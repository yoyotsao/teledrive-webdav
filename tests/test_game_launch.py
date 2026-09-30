from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from gamelaunch import GameLauncher, LaunchError, ProcessInfo, ProcessRef, RunningSession


class FakeState:
    def __init__(self, root: Path):
        self.root = root

    def canonical_game_segments(self, path: str) -> list[str]:
        if not isinstance(path, str) or not path.startswith("game/") or ".." in path.split("/"):
            raise ValueError("invalid game path")
        return path.split("/")

    def _root_for(self, path: str, segments: list[str]) -> Path:
        return self.root


@dataclass
class FakeProcess:
    pid: int


class FakeAdapter:
    def __init__(self):
        self.processes: list[ProcessInfo] = []
        self.spawned: list[tuple[list[str], str]] = []
        self.next_pid = 100

    def snapshot_all(self) -> list[ProcessInfo]:
        return list(self.processes)

    def is_alive(self, ref: ProcessRef) -> bool:
        return any(item.ref == ref for item in self.processes)

    def exe_path(self, pid: int) -> str | None:
        return next((item.exe_path for item in self.processes if item.ref.pid == pid), None)

    def parent_pid(self, pid: int) -> int | None:
        return next((item.parent_pid for item in self.processes if item.ref.pid == pid), None)

    def process_ref(self, pid: int) -> ProcessRef | None:
        return next((item.ref for item in self.processes if item.ref.pid == pid), None)

    def spawn(self, command: list[str], cwd: str) -> FakeProcess:
        self.spawned.append((command, cwd))
        process = FakeProcess(self.next_pid)
        self.processes.append(ProcessInfo(ProcessRef(process.pid, 2000.0), command[-1], 1))
        self.next_pid += 1
        return process


class FakeStore:
    def __init__(self, fail=False):
        self.sessions: dict[str, RunningSession] = {}
        self.fail = fail

    def save(self, session: RunningSession) -> None:
        if self.fail:
            raise OSError("disk unavailable")
        self.sessions[session.session_id] = session

    def remove(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)


@pytest.fixture
def game(tmp_path):
    root = tmp_path / "GameA"
    root.mkdir()
    (root / ".reina-complete").write_text("ready", encoding="utf-8")
    (root / "bin").mkdir()
    (root / "bin" / "game.exe").write_bytes(b"exe")
    (root / "start.exe").write_bytes(b"exe")
    adapter = FakeAdapter()
    store = FakeStore()
    now = [2000.0]
    launcher = GameLauncher(
        FakeState(root), store=store, process_adapter=adapter,
        clock=lambda: now[0], device="TEST-PC", start_monitor=False,
    )
    return root, adapter, store, now, launcher


def test_list_exes_requires_ready_root_and_returns_sorted_relative_paths(game):
    root, _, _, _, launcher = game
    assert launcher.list_exes("game/A") == ["bin/game.exe", "start.exe"]
    (root / ".reina-complete").unlink()
    with pytest.raises(LaunchError) as error:
        launcher.list_exes("game/A")
    assert error.value.status == 409
    assert error.value.code == "bridge_game_not_ready"


@pytest.mark.parametrize("exe", ["../outside.exe", "C:\\outside.exe", "/outside.exe"])
def test_launch_rejects_traversal_and_absolute_executable_paths(game, exe):
    *_, launcher = game
    with pytest.raises(LaunchError) as error:
        launcher.launch("game/A", exe, 1)
    assert error.value.status == 409
    assert error.value.code == "bridge_exe_invalid"


def test_launch_rejects_symlink_resolved_outside_root(game, tmp_path):
    root, _, _, _, launcher = game
    outside = tmp_path / "outside.exe"
    outside.write_bytes(b"exe")
    try:
        (root / "escape.exe").symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")
    with pytest.raises(LaunchError) as error:
        launcher.launch("game/A", "escape.exe", 1)
    assert error.value.code == "bridge_exe_invalid"


def test_normal_launch_uses_argument_vector_and_saves_before_success(game):
    _, adapter, store, _, launcher = game
    session = launcher.launch("game/A", "bin/game.exe", 42)
    assert adapter.spawned == [([str(Path(launcher.state.root) / "bin" / "game.exe")], str(Path(launcher.state.root) / "bin"))]
    assert store.sessions[session.session_id] is session
    assert session.game_id == 42
    assert session.device == "TEST-PC"
    assert session.pids == [ProcessRef(100, 2000.0)]


def test_launch_rechecks_root_before_spawn_when_launcher_exited_before_first_poll(game):
    root, adapter, store, _, launcher = game
    session = launcher.launch("game/A", "start.exe", 41)
    child = ProcessInfo(ProcessRef(401, 2001.0), str(root / "bin" / "game.exe"), 1)
    adapter.processes = [child]
    spawn_count = len(adapter.spawned)

    with pytest.raises(LaunchError) as error:
        launcher.launch("game/A", "start.exe", 41)

    assert error.value.status == 409
    assert error.value.code == "bridge_game_not_ready"
    assert len(adapter.spawned) == spawn_count
    assert session.pids == [child.ref]
    assert store.sessions[session.session_id].pids == [child.ref]


def test_store_failure_reports_started_but_untracked_and_does_not_kill_process(game):
    _, adapter, store, _, launcher = game
    store.fail = True
    with pytest.raises(LaunchError, match="游戏已启动") as error:
        launcher.launch("game/A", "start.exe", 3)
    assert error.value.status == 500
    assert adapter.processes[0].ref.pid == 100
    assert launcher.sessions == {}


def test_missing_initial_process_identity_is_diagnostic_without_fake_create_time(game):
    _, adapter, store, _, launcher = game
    adapter.process_ref = lambda _pid: None
    with pytest.raises(LaunchError, match="无法取得程序身份") as error:
        launcher.launch("game/A", "start.exe", 31)
    assert error.value.status == 500
    assert "PID 100" in error.value.message
    assert adapter.processes[0].ref.create_time == 2000.0
    assert store.sessions == {}
    assert launcher.sessions == {}
    assert launcher._monitor_threads == {}


def test_locale_emulator_is_separate_executable_and_argument(game, tmp_path):
    root, adapter, _, _, _ = game
    le = tmp_path / "LEProc.exe"
    le.write_bytes(b"launcher")
    launcher = GameLauncher(
        FakeState(root), locale_emulator=str(le), store=FakeStore(), process_adapter=adapter,
        clock=lambda: 2000.0, device="TEST-PC", start_monitor=False,
    )
    launcher.launch("game/A", "start.exe", 5, locale_emulator=True)
    assert adapter.spawned[-1] == ([str(le.resolve()), str((root / "start.exe").resolve())], str(root))


def test_locale_emulator_requires_existing_configured_file(game):
    root, adapter, _, _, _ = game
    launcher = GameLauncher(
        FakeState(root), locale_emulator="", store=FakeStore(), process_adapter=adapter,
        start_monitor=False,
    )
    with pytest.raises(LaunchError) as error:
        launcher.launch("game/A", "start.exe", 6, locale_emulator=True)
    assert error.value.status == 409
    assert "Locale Emulator" in error.value.message


def test_monitor_recovers_descendants_and_root_fallback_after_launcher_exits(game):
    root, adapter, store, now, launcher = game
    session = launcher.launch("game/A", "start.exe", 7)
    adapter.processes = [
        ProcessInfo(ProcessRef(201, 2001.0), str(root / "bin" / "game.exe"), 1),
        ProcessInfo(ProcessRef(202, 2002.0), str(root / "child.exe"), 201),
    ]
    launcher.monitor_once(session)
    assert session.pids == [ProcessRef(201, 2001.0), ProcessRef(202, 2002.0)]
    assert store.sessions[session.session_id].pids == session.pids


def test_monitor_heartbeats_only_after_sixty_seconds_without_membership_change(game):
    _, adapter, store, now, launcher = game
    session = launcher.launch("game/A", "start.exe", 8)
    store.sessions.clear()
    now[0] += 59
    assert not launcher.monitor_once(session)
    assert not store.sessions
    now[0] += 1
    assert launcher.monitor_once(session)
    assert store.sessions[session.session_id].last_seen == 2060


def test_elapsed_uses_only_the_current_live_session(game):
    root, adapter, _, now, launcher = game
    old_session = launcher.launch("game/A", "start.exe", 33)
    adapter.processes = []
    launcher.monitor_once(old_session)
    now[0] = 4100.0
    current_ref = ProcessRef(404, 4000.0)
    current = RunningSession("current", 34, "TEST-PC", 4000, str(root), [current_ref], 4000, "game/A")
    launcher.sessions[current.session_id] = current
    adapter.processes = [ProcessInfo(current_ref, str(root / "start.exe"), 1)]
    assert launcher.elapsed_seconds("game/A") == 100


def test_membership_save_failure_rolls_back_for_retry(game):
    root, adapter, store, _, launcher = game
    session = launcher.launch("game/A", "start.exe", 32)
    previous = session.pids.copy()
    adapter.processes = [ProcessInfo(ProcessRef(303, 2001.0), str(root / "child.exe"), 1)]
    store.fail = True
    with pytest.raises(OSError):
        launcher.monitor_once(session)
    assert session.pids == previous
    store.fail = False
    assert launcher.monitor_once(session)
    assert session.pids == [ProcessRef(303, 2001.0)]


def test_process_claims_have_deterministic_single_owner(game):
    root, adapter, store, _, launcher = game
    first = launcher.launch("game/A", "start.exe", 9)
    ref = first.pids[0]
    second = RunningSession("zz-owner", 10, "TEST-PC", first.start, str(root), [ref], first.last_seen)
    launcher.sessions[second.session_id] = second
    store.save(second)
    assert launcher.claim_process(second, ref) is True
    assert ref not in first.pids
    assert ref in second.pids
    assert launcher.claim_process(first, ref) is False


def test_windows_containment_comparison_ignores_case():
    assert GameLauncher._inside(Path("C:/Games/GameA"), Path("c:/games/gamea/bin/game.exe"))


def test_list_exes_excludes_symlink_to_executable_outside_root(game, tmp_path):
    root, _, _, _, launcher = game
    outside = tmp_path / "outside.exe"
    outside.write_bytes(b"exe")
    try:
        (root / "outside-link.exe").symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")
    assert "outside-link.exe" not in launcher.list_exes("game/A")


def test_list_exes_does_not_traverse_directory_link_outside_root(game, tmp_path):
    root, _, _, _, launcher = game
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hidden.exe").write_bytes(b"exe")
    try:
        (root / "outside-link").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink creation is unavailable: {error}")
    assert launcher.list_exes("game/A") == ["bin/game.exe", "start.exe"]
