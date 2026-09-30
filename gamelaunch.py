"""Windows 游戏启动、程序树追踪与可替换的本地会话存储。"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Protocol

log = logging.getLogger("bridge.game_launch")


@dataclass(frozen=True, order=True)
class ProcessRef:
    pid: int
    create_time: float


@dataclass(frozen=True)
class ProcessInfo:
    ref: ProcessRef
    exe_path: str | None
    parent_pid: int | None


@dataclass
class RunningSession:
    session_id: str
    game_id: int
    device: str
    start: int
    root: str
    pids: list[ProcessRef]
    last_seen: int
    path: str = ""


class LaunchError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


class ProcessAdapter(Protocol):
    def snapshot_all(self) -> list[ProcessInfo]: ...
    def is_alive(self, ref: ProcessRef) -> bool: ...
    def exe_path(self, pid: int) -> str | None: ...
    def parent_pid(self, pid: int) -> int | None: ...
    def process_ref(self, pid: int) -> ProcessRef | None: ...
    def spawn(self, command: list[str], cwd: str): ...


class SessionStore(Protocol):
    def save(self, session: RunningSession) -> None: ...
    def remove(self, session_id: str) -> None: ...


class MemorySessionStore:
    """任務 13 的 store protocol 實作；durable store 由任務 14 接入。"""

    def __init__(self):
        self.sessions: dict[str, RunningSession] = {}

    def save(self, session: RunningSession) -> None:
        self.sessions[session.session_id] = session

    def remove(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)


class PsutilProcessAdapter:
    """把 psutil 和 subprocess 隔離在 launcher 的作業系統 adapter。"""

    def __init__(self):
        try:
            import psutil
        except ImportError as exc:
            raise RuntimeError("Game process monitoring requires psutil") from exc
        self.psutil = psutil

    def snapshot_all(self) -> list[ProcessInfo]:
        items = []
        for process in self.psutil.process_iter(["pid", "create_time", "exe", "ppid"]):
            try:
                info = process.info
                items.append(ProcessInfo(
                    ProcessRef(int(info["pid"]), float(info["create_time"])),
                    info.get("exe"),
                    int(info["ppid"]) if info.get("ppid") is not None else None,
                ))
            except (self.psutil.NoSuchProcess, self.psutil.AccessDenied, self.psutil.ZombieProcess,
                    KeyError, TypeError, ValueError):
                continue
        return items

    def is_alive(self, ref: ProcessRef) -> bool:
        try:
            process = self.psutil.Process(ref.pid)
            return process.is_running() and abs(process.create_time() - ref.create_time) < 0.01
        except (self.psutil.NoSuchProcess, self.psutil.AccessDenied, self.psutil.ZombieProcess):
            return False

    def exe_path(self, pid: int) -> str | None:
        try:
            return self.psutil.Process(pid).exe()
        except (self.psutil.NoSuchProcess, self.psutil.AccessDenied, self.psutil.ZombieProcess):
            return None

    def parent_pid(self, pid: int) -> int | None:
        try:
            return self.psutil.Process(pid).ppid()
        except (self.psutil.NoSuchProcess, self.psutil.AccessDenied, self.psutil.ZombieProcess):
            return None

    def process_ref(self, pid: int) -> ProcessRef | None:
        try:
            process = self.psutil.Process(pid)
            return ProcessRef(pid, process.create_time())
        except (self.psutil.NoSuchProcess, self.psutil.AccessDenied, self.psutil.ZombieProcess):
            return None

    def spawn(self, command: list[str], cwd: str):
        return subprocess.Popen(command, cwd=cwd, shell=False)


class GameLauncher:
    POLL_SECONDS = 2
    HEARTBEAT_SECONDS = 60

    def __init__(
        self,
        state,
        *,
        locale_emulator: str = "",
        store: SessionStore | None = None,
        process_adapter: ProcessAdapter | None = None,
        clock=time.time,
        device: str | None = None,
        start_monitor: bool = True,
    ):
        self.state = state
        self.locale_emulator = locale_emulator.strip()
        self.store = store or MemorySessionStore()
        self.process_adapter = process_adapter
        self.clock = clock
        self.device = device or os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "unknown"
        self.start_monitor = start_monitor
        self.sessions: dict[str, RunningSession] = {}
        self._launching: set[str] = set()
        self._lock = threading.RLock()
        self._monitor_threads: dict[str, threading.Thread] = {}
        self._empty_polls: dict[str, int] = {}
        # 最後一次確認程序樹仍存活的時間；只在記憶體，持久化仍受 60 秒心跳限制
        self._last_alive: dict[str, int] = {}
        self._stop = threading.Event()
        self.on_session_end = None

    def _adapter(self) -> ProcessAdapter:
        if self.process_adapter is None:
            self.process_adapter = PsutilProcessAdapter()
        return self.process_adapter

    @property
    def locale_emulator_available(self) -> bool:
        if not self.locale_emulator:
            return False
        try:
            return Path(self.locale_emulator).resolve(strict=True).is_file()
        except OSError:
            return False

    @staticmethod
    def _inside(root: Path, candidate: Path) -> bool:
        try:
            common = os.path.commonpath((str(root), str(candidate)))
        except (OSError, ValueError):
            return False
        # Windows 路径不区分大小写；casefold 也让非 Windows 测试中的模拟路径一致。
        return os.path.normcase(common).casefold() == os.path.normcase(str(root)).casefold()

    def _ready_root(self, path: str) -> Path:
        try:
            segments = self.state.canonical_game_segments(path)
            root = Path(self.state._root_for(path, segments)).resolve(strict=True)
        except Exception as exc:
            raise LaunchError(409, "bridge_game_not_ready", "game is not ready to launch") from exc
        if not root.is_dir() or not (root / ".reina-complete").is_file():
            raise LaunchError(409, "bridge_game_not_ready", "游戏尚未下载完成，无法启动。")
        return root

    def _resolve_exe(self, root: Path, exe_relpath: str) -> Path:
        if not isinstance(exe_relpath, str) or not exe_relpath or "\x00" in exe_relpath:
            raise LaunchError(409, "bridge_exe_invalid", "selected executable path is invalid")
        windows_path = PureWindowsPath(exe_relpath)
        if Path(exe_relpath).is_absolute() or windows_path.is_absolute() or windows_path.drive:
            raise LaunchError(409, "bridge_exe_invalid", "selected executable must be relative to the game folder")
        parts = exe_relpath.replace("\\", "/").split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise LaunchError(409, "bridge_exe_invalid", "selected executable path is invalid")
        candidate = root.joinpath(*parts)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise LaunchError(409, "bridge_exe_invalid", "selected executable does not exist") from exc
        if not self._inside(root, resolved) or not resolved.is_file() or resolved.suffix.casefold() != ".exe":
            raise LaunchError(409, "bridge_exe_invalid", "selected executable is outside the game folder or invalid")
        return resolved

    def list_exes(self, path: str) -> list[str]:
        root = self._ready_root(path)
        exes = []
        pending = [root]
        visited = set()
        while pending:
            directory = pending.pop()
            try:
                resolved_directory = directory.resolve(strict=True)
                key = os.path.normcase(str(resolved_directory)).casefold()
                if key in visited or not self._inside(root, resolved_directory):
                    continue
                visited.add(key)
                children = list(directory.iterdir())
            except (OSError, ValueError):
                continue
            for candidate in children:
                try:
                    resolved = candidate.resolve(strict=True)
                    if not self._inside(root, resolved):
                        continue
                    if candidate.is_dir():
                        pending.append(candidate)
                    elif candidate.suffix.casefold() == ".exe" and candidate.is_file():
                        exes.append(candidate.relative_to(root).as_posix())
                except (OSError, ValueError):
                    continue
        return sorted(set(exes), key=lambda item: (item.casefold(), item))

    def _is_game_running(self, path: str) -> bool:
        with self._lock:
            sessions = [session for session in self.sessions.values() if session.path.casefold() == path.casefold()]
        if not sessions:
            return False
        adapter = self._adapter()
        return any(adapter.is_alive(ref) for session in sessions for ref in session.pids)

    def elapsed_seconds(self, path: str) -> int:
        with self._lock:
            sessions = [session for session in self.sessions.values() if session.path.casefold() == path.casefold()]
        if not sessions:
            return 0
        adapter = self._adapter()
        active = [session for session in sessions if any(adapter.is_alive(ref) for ref in session.pids)]
        if not active:
            return 0
        return max(0, int(self.clock()) - min(session.start for session in active))

    def is_running(self, path: str) -> bool:
        return self._is_game_running(path)

    def launch(self, path: str, exe_relpath: str, game_id: int, locale_emulator: bool = False) -> RunningSession:
        root = self._ready_root(path)
        exe_path = self._resolve_exe(root, exe_relpath)
        if not isinstance(game_id, int) or isinstance(game_id, bool) or game_id <= 0:
            raise LaunchError(400, "invalid_game_id", "game id must be a positive integer")
        if locale_emulator:
            if not self.locale_emulator_available:
                raise LaunchError(409, "bridge_locale_emulator_unavailable", "已请求使用 Locale Emulator，但配置的启动程序不存在或不可用。")
            command = [str(Path(self.locale_emulator).resolve()), str(exe_path)]
        else:
            command = [str(exe_path)]

        path_key = os.path.normcase(str(root)).casefold()
        with self._lock:
            if path_key in self._launching:
                raise LaunchError(409, "bridge_game_running", "该游戏已经在运行。")
            root_sessions = [
                session for session in self.sessions.values()
                if os.path.normcase(session.root).casefold() == path_key
            ]
            for session in root_sessions:
                try:
                    # 在放行新启动前同步执行与 monitor 相同的 root recovery 扫描。
                    self.monitor_once(session)
                except Exception as exc:
                    log.warning("game process prelaunch scan failed: %s", type(exc).__name__)
                    raise LaunchError(
                        503, "bridge_process_scan_failed", "无法确认游戏当前是否已在运行，请稍后重试。",
                    ) from None
            if any(self._session_alive(session) for session in root_sessions):
                raise LaunchError(409, "bridge_game_running", "该游戏已经在运行。")
            self._launching.add(path_key)

        try:
            adapter = self._adapter()
            process = adapter.spawn(command, str(exe_path.parent if not locale_emulator else root))
            pid = int(process.pid)
            ref = adapter.process_ref(pid)
            if ref is None:
                log.error("game process started but initial process identity was unavailable (pid=%s)", pid)
                raise LaunchError(500, "game_started_untracked", f"游戏已启动（PID {pid}），但无法取得程序身份，计时未建立。")

            now = int(self.clock())
            session = RunningSession(
                str(uuid.uuid4()), game_id, self.device, int(ref.create_time), str(root), [ref], now, path,
            )
            with self._lock:
                # 串行化 durable snapshot 写入，避免并发 launch 覆盖 running map。
                self.store.save(session)
                self.sessions[session.session_id] = session
                self._launching.discard(path_key)
        except LaunchError:
            with self._lock:
                self._launching.discard(path_key)
            raise
        except Exception as exc:
            with self._lock:
                self._launching.discard(path_key)
            if "pid" in locals():
                log.error("game process started but initial session save failed (pid=%s): %s", pid, type(exc).__name__)
                raise LaunchError(500, "game_started_untracked", f"游戏已启动（PID {pid}），但计时记录建立失败；游戏可能仍在运行。") from None
            log.error("game process spawn failed: %s", type(exc).__name__)
            raise LaunchError(500, "game_launch_failed", "启动游戏失败，请检查可执行文件和 bridge 配置。") from None

        if self.start_monitor:
            thread = threading.Thread(target=self._monitor, args=(session,), name=f"game-monitor-{pid}", daemon=True)
            with self._lock:
                self._monitor_threads[session.session_id] = thread
            try:
                thread.start()
            except Exception as exc:
                with self._lock:
                    self._monitor_threads.pop(session.session_id, None)
                log.error("game process started but monitor thread failed (pid=%s): %s", pid, type(exc).__name__)
                raise LaunchError(500, "game_started_unmonitored", f"游戏已启动（PID {pid}）且计时记录已保存，但监控线程启动失败。") from None
        return session

    def _session_alive(self, session: RunningSession) -> bool:
        adapter = self._adapter()
        return any(adapter.is_alive(ref) for ref in session.pids)

    def claim_process(self, session: RunningSession, ref: ProcessRef) -> bool:
        with self._lock:
            claimants = [item for item in self.sessions.values() if ref in item.pids]
            if session not in claimants:
                claimants.append(session)
            eligible = [item for item in claimants if item.start <= ref.create_time]
            if not eligible:
                return False
            owner = max(eligible, key=lambda item: (item.start, item.root.casefold(), item.session_id))
            if owner.session_id != session.session_id:
                return False
            for claimant in claimants:
                if claimant.session_id == owner.session_id:
                    continue
                previous = claimant.pids
                claimant.pids = [item for item in previous if item != ref]
                try:
                    self.store.save(claimant)
                except Exception:
                    claimant.pids = previous
                    log.exception("failed to persist process claim transfer (session=%s)", claimant.session_id)
                    return False
            return True

    def monitor_once(self, session: RunningSession) -> bool:
        adapter = self._adapter()
        snapshot = adapter.snapshot_all()
        by_pid = {item.ref.pid: item for item in snapshot}
        retained = {
            ref for ref in session.pids
            if (item := by_pid.get(ref.pid)) is not None and item.ref == ref
        }
        # 从当前已知程序沿快照追踪 descendants，父进程退出后保留仍存活的子孙。
        changed = True
        while changed:
            changed = False
            known_pids = {ref.pid for ref in retained}
            for item in snapshot:
                if item.parent_pid in known_pids and item.ref not in retained:
                    retained.add(item.ref)
                    changed = True
        root = Path(session.root).resolve()
        for item in snapshot:
            if item.ref.create_time < session.start or not item.exe_path:
                continue
            try:
                executable = Path(item.exe_path).resolve()
            except OSError:
                continue
            if self._inside(root, executable):
                retained.add(item.ref)

        owned = {ref for ref in retained if self.claim_process(session, ref)}
        current = sorted(owned)
        if current:
            with self._lock:
                self._last_alive[session.session_id] = int(self.clock())
        with self._lock:
            previous = sorted(session.pids)
            if current != previous:
                session.pids = current
                try:
                    self.store.save(session)
                except Exception:
                    session.pids = previous
                    raise
                self._empty_polls[session.session_id] = 1 if not current else 0
                return True
            if not current:
                self._empty_polls[session.session_id] = self._empty_polls.get(session.session_id, 0) + 1
                return False
            self._empty_polls[session.session_id] = 0
            if int(self.clock()) - session.last_seen >= self.HEARTBEAT_SECONDS:
                previous_last_seen = session.last_seen
                session.last_seen = int(self.clock())
                try:
                    self.store.save(session)
                except Exception:
                    session.last_seen = previous_last_seen
                    raise
                return True
        return False

    def _monitor(self, session: RunningSession) -> None:
        while not self._stop.wait(self.POLL_SECONDS):
            try:
                self.monitor_once(session)
                with self._lock:
                    finished = self._empty_polls.get(session.session_id, 0) >= 2
                if finished:
                    self._apply_last_alive(session)
                    if self.on_session_end is not None:
                        self.on_session_end(session)
                    break
            except Exception:
                log.exception("game process monitor failed (session=%s)", session.session_id)

    def close(self) -> None:
        self.stop()
        self.save_all()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            threads = list(self._monitor_threads.values())
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join()

    def _apply_last_alive(self, session: RunningSession) -> None:
        """正常結束時以最後一次確認存活的時間當 end，而不是最多落後 60 秒的心跳。"""
        with self._lock:
            alive = self._last_alive.pop(session.session_id, None)
            if alive is not None and alive > session.last_seen:
                session.last_seen = alive

    def save_all(self) -> None:
        with self._lock:
            for session in self.sessions.values():
                alive = self._last_alive.get(session.session_id)
                if alive is not None and alive > session.last_seen:
                    session.last_seen = alive
                self.store.save(session)

    def resume(self, session: RunningSession) -> None:
        """為已恢復的活躍 session 啟動程序樹監控。"""
        if not self.start_monitor or session.session_id in self._monitor_threads:
            return
        thread = threading.Thread(
            target=self._monitor, args=(session,), name=f"game-monitor-recovered-{session.session_id}", daemon=True,
        )
        self._monitor_threads[session.session_id] = thread
        thread.start()
