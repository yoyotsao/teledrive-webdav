"""遊戲遊玩時間的本機 durable store 與補送佇列。"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from dataclasses import asdict
from pathlib import Path

from gamelaunch import ProcessRef, RunningSession

log = logging.getLogger("bridge.playtime")


def _fsync_directory(path: Path) -> None:
    # Windows 不允許一般方式開啟目錄；檔案 fsync + replace 仍是必要保障。
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _session_dict(session: RunningSession) -> dict:
    return asdict(session)


def _session_from_dict(data: dict) -> RunningSession:
    data = dict(data)
    data["pids"] = [ProcessRef(int(ref["pid"]), float(ref["create_time"])) for ref in data["pids"]]
    data["path"] = str(data.get("path", ""))
    return RunningSession(**data)


def _atomic_write(path: Path, payload: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


class RunningSessionStore:
    """以單一鎖序列化記憶體 map 與磁碟 snapshot。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._sessions = {item["session_id"]: _session_from_dict(item) for item in data}
        else:
            self._sessions: dict[str, RunningSession] = {}

    def _persist(self) -> None:
        payload = json.dumps(
            [_session_dict(item) for item in self._sessions.values()],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        _atomic_write(self.path, payload)

    def save(self, session: RunningSession) -> None:
        with self._lock:
            previous = self._sessions.get(session.session_id)
            self._sessions[session.session_id] = copy.deepcopy(session)
            try:
                self._persist()
            except BaseException:
                if previous is None:
                    self._sessions.pop(session.session_id, None)
                else:
                    self._sessions[session.session_id] = previous
                raise

    def remove(self, session_id: str) -> None:
        with self._lock:
            previous = self._sessions.pop(session_id, None)
            try:
                self._persist()
            except BaseException:
                if previous is not None:
                    self._sessions[session_id] = previous
                raise

    def all(self) -> list[RunningSession]:
        with self._lock:
            return copy.deepcopy(list(self._sessions.values()))


class PlaytimeQueue:
    """帶尾行修復的 JSONL queue；索引只包含 durable records。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._records: list[dict] = []
        self._failed = False
        with self._lock:
            self._repair_and_load(durability_barrier=True)

    @staticmethod
    def _valid_record(raw: bytes) -> dict:
        record = json.loads(raw.decode("utf-8"))
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
            raise ValueError("queue record must be an object with a non-empty id")
        return record

    def _repair_and_load(self, *, durability_barrier: bool = False) -> None:
        if self._failed:
            raise RuntimeError("playtime queue durability is uncertain; reopen the queue after a successful fsync barrier")
        try:
            self._read_repair(durability_barrier=durability_barrier)
        except OSError:
            self._failed = True
            raise

    def _read_repair(self, *, durability_barrier: bool) -> None:
        if not self.path.exists():
            self._records = []
            return
        raw = self.path.read_bytes()
        lines = raw.splitlines(keepends=True)
        records: list[dict] = []
        offset = 0
        for index, line in enumerate(lines):
            complete = line.endswith(b"\n")
            content = line[:-1] if complete else line
            if not complete and index == len(lines) - 1:
                try:
                    record = self._valid_record(content)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    with open(self.path, "r+b") as stream:
                        stream.truncate(offset)
                        stream.flush()
                        os.fsync(stream.fileno())
                    _fsync_directory(self.path.parent)
                    log.warning("truncated incomplete playtime queue tail")
                    break
                with open(self.path, "ab") as stream:
                    stream.write(b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                _fsync_directory(self.path.parent)
                records.append(record)
                offset += len(line)
                continue
            try:
                records.append(self._valid_record(content))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"corrupt playtime queue line {index + 1}") from exc
            offset += len(line)
        if durability_barrier:
            with open(self.path, "r+b") as stream:
                os.fsync(stream.fileno())
        self._records = records

    def append(self, record: dict) -> None:
        if not isinstance(record, dict):
            raise ValueError("playtime record must be an object")
        payload = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            self._repair_and_load()
            external_id = record.get("id")
            if not isinstance(external_id, str) or not external_id:
                raise ValueError("playtime record must include id")
            if any(item["id"] == external_id for item in self._records):
                return
            try:
                with open(self.path, "ab") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                # 寫入結果可能已完整可讀，但 fsync 未成功前不能再把 UUID 當成 durable。
                self._failed = True
                raise
            _fsync_directory(self.path.parent)
            self._records.append(copy.deepcopy(record))

    def pending(self) -> list[dict]:
        with self._lock:
            self._repair_and_load()
            return copy.deepcopy(self._records)

    def contains(self, external_id: str) -> bool:
        with self._lock:
            self._repair_and_load()
            return any(item["id"] == external_id for item in self._records)

    def ack(self, external_id: str) -> None:
        with self._lock:
            self._repair_and_load()
            remaining = [item for item in self._records if item["id"] != external_id]
            payload = b"".join(
                (json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                for item in remaining
            )
            _atomic_write(self.path, payload)
            self._records = remaining


def _open_or_quarantine(factory, path: Path):
    """開啟 durable 檔；壞掉時改名保留原檔並用全新的空檔，不讓 bridge（含 H:）起不來。"""
    try:
        return factory(path)
    except Exception:
        import time

        target = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        log.error("playtime state file is corrupt; moved aside and playtime restarts empty (%s)", target.name)
        os.replace(path, target)
        return factory(path)


def open_playtime_state(directory: str | Path) -> tuple[RunningSessionStore, PlaytimeQueue]:
    directory = Path(directory)
    store = _open_or_quarantine(RunningSessionStore, directory / "playtime-running.json")
    queue = _open_or_quarantine(PlaytimeQueue, directory / "playtime-queue.jsonl")
    return store, queue


class PlaytimeService:
    """按 queue-first 次序結束會話，並在啟動時接回或結清舊會話。"""

    def __init__(self, store: RunningSessionStore, queue: PlaytimeQueue, launcher=None, adapter=None):
        self.store, self.queue, self.launcher, self.adapter = store, queue, launcher, adapter

    def finish(self, session: RunningSession) -> dict:
        record = {"id": session.session_id, "game_id": session.game_id, "device": session.device,
                  "start": session.start, "end": session.last_seen,
                  "seconds": max(0, session.last_seen - session.start)}
        self.queue.append(record)
        self.store.remove(session.session_id)
        return record

    def recover_sessions(self) -> None:
        sessions = self.store.all()
        if self.launcher:
            with self.launcher._lock:
                self.launcher.sessions.update({item.session_id: item for item in sessions})
        for session in sessions:
            if self.queue.contains(session.session_id):
                self.store.remove(session.session_id)
                if self.launcher:
                    self.launcher.sessions.pop(session.session_id, None)
                continue
            snapshot = self.adapter.snapshot_all() if self.adapter else []
            by_pid = {item.ref.pid: item for item in snapshot}
            found = [ref for ref in session.pids if (item := by_pid.get(ref.pid)) and item.ref == ref]
            if not found and self.adapter:
                reused_pids = {ref.pid for ref in session.pids if ref.pid in by_pid}
                root = Path(session.root).resolve()
                for item in snapshot:
                    if item.ref.pid in reused_pids:
                        continue
                    if item.ref.create_time < session.start or not item.exe_path:
                        continue
                    try:
                        if os.path.commonpath((str(root), str(Path(item.exe_path).resolve()))).casefold() == str(root).casefold():
                            found.append(item.ref)
                    except (OSError, ValueError):
                        continue
            if found:
                session.pids = sorted(set(found))
                if self.launcher:
                    with self.launcher._lock:
                        session.pids = [ref for ref in session.pids if self.launcher.claim_process(session, ref)]
                        if not session.pids:
                            self.launcher.sessions.pop(session.session_id, None)
                            self.finish(session)
                            continue
                        self.launcher.sessions[session.session_id] = session
                        self.launcher.store.save(session)
                        if hasattr(self.launcher, "resume"):
                            self.launcher.resume(session)
                else:
                    self.store.save(session)
                continue
            self.finish(session)
            if self.launcher:
                self.launcher.sessions.pop(session.session_id, None)


class PlaytimeSender:
    """使用 bridge 自己的 TeleDrive JWT，將 durable queue 送往 reina-server。"""

    BACKOFF = (5, 10, 30, 60, 300)
    # 單筆被 server 永久拒絕（遊戲已刪、資料無效）時只擱置那一筆，不擋住後面的紀錄
    DELETED_GAME_BACKOFF = 900
    INVALID_RECORD_BACKOFF = 3600
    # 連續 401 代表 reina-server 與 TeleDrive 的 JWT 設定不一致；拉長間隔避免反覆用帳號 DM bot
    AUTH_FAILURE_PAUSE = 1800

    def __init__(self, api, cfg, queue, *, sleep=None, clock=None):
        import time

        self.api, self.cfg, self.queue = api, cfg, queue
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._attempt = 0
        self._held_until: dict[str, float] = {}
        self._auth_paused_until = 0.0

    def _post(self, record: dict):
        token = self.api.login()
        url = self.cfg.reina_server_url.rstrip("/") + "/game/api/sessions"
        response = self.api._http_session().post(
            url, json=record, headers={"Authorization": f"Bearer {token}"},
            timeout=10, allow_redirects=False,
        )
        if response.status_code == 401:
            token = self.api.login(force=True)
            response = self.api._http_session().post(
                url, json=record, headers={"Authorization": f"Bearer {token}"},
                timeout=10, allow_redirects=False,
            )
            if response.status_code == 401:
                self._auth_paused_until = self._clock() + self.AUTH_FAILURE_PAUSE
                log.error("reina-server keeps rejecting the bridge JWT; pausing playtime uploads")
        return response

    def _transient_delay(self) -> int:
        delay = self.BACKOFF[min(self._attempt, len(self.BACKOFF) - 1)]
        self._attempt += 1
        return delay

    def send_once(self) -> tuple[bool, int]:
        import requests

        now = self._clock()
        if now < self._auth_paused_until:
            return False, max(1, int(self._auth_paused_until - now))
        records = self.queue.pending()
        if not records:
            return False, 0
        sent_any = False
        for record in records:
            now = self._clock()
            if self._held_until.get(record["id"], 0.0) > now:
                continue
            try:
                response = self._post(record)
            except (requests.RequestException, OSError, RuntimeError) as exc:
                log.warning("playtime send deferred (%s, session=%s)", type(exc).__name__, record["id"])
                return sent_any, 0 if sent_any else self._transient_delay()
            status = response.status_code
            if status == 200:
                try:
                    body = response.json()
                except (ValueError, json.JSONDecodeError):
                    body = {}
                if isinstance(body, dict) and isinstance(body.get("accepted"), bool):
                    self.queue.ack(record["id"])
                    self._held_until.pop(record["id"], None)
                    self._attempt = 0
                    sent_any = True
                    continue
            if status == 404:
                try:
                    code = response.json().get("code")
                except (ValueError, AttributeError):
                    code = None
                if code == "not_found":
                    log.warning("playtime record retained: game was deleted (session=%s)", record["id"])
                    self._held_until[record["id"]] = now + self.DELETED_GAME_BACKOFF
                    continue
            if status == 400:
                log.warning("playtime record retained: server rejected the record (session=%s)", record["id"])
                self._held_until[record["id"]] = now + self.INVALID_RECORD_BACKOFF
                continue
            if status == 403:
                log.warning("playtime record retained: server rejected access (session=%s)", record["id"])
                return sent_any, 0 if sent_any else self.DELETED_GAME_BACKOFF
            if status == 401:
                return sent_any, 0 if sent_any else max(1, int(self._auth_paused_until - self._clock()))
            log.warning("playtime send deferred (status=%s, session=%s)", status, record["id"])
            return sent_any, 0 if sent_any else self._transient_delay()
        if sent_any:
            return True, 0
        waits = [self._held_until[item["id"]] - now for item in records if item["id"] in self._held_until]
        return False, max(1, int(min(waits))) if waits else 5

    def _run(self):
        while not self._stop.is_set():
            try:
                sent, delay = self.send_once()
            except Exception as exc:
                log.warning("playtime sender paused (%s)", type(exc).__name__)
                sent, delay = False, self.BACKOFF[min(self._attempt, len(self.BACKOFF) - 1)]
                self._attempt += 1
            if not sent and self._stop.wait(delay or 5):
                return

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="playtime-sender", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join()
