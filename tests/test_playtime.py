import json
import threading
from pathlib import Path

import pytest

from gamelaunch import ProcessInfo, ProcessRef, RunningSession
from playtime import PlaytimeQueue, PlaytimeSender, PlaytimeService, RunningSessionStore


def session(session_id="s1", pid=11, create_time=100.0):
    return RunningSession(session_id, 7, "PC", 100, "C:/games/A", [ProcessRef(pid, create_time)], 120, "game/A")


def test_running_store_roundtrip_is_deep_copy_and_serializes_concurrent_mutations(tmp_path):
    path = tmp_path / "running.json"
    store = RunningSessionStore(path)
    items = [session(f"s{i}", i + 1) for i in range(40)]
    threads = [threading.Thread(target=store.save, args=(item,)) for item in items]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    snapshot = store.all()
    snapshot[0].pids.clear()
    assert len(RunningSessionStore(path).all()) == 40
    assert len(store.all()[0].pids) == 1


def test_running_store_remove_and_parallel_save_leave_disk_equal_to_memory(tmp_path):
    path = tmp_path / "running.json"
    store = RunningSessionStore(path)
    store.save(session("remove-me"))
    barrier = threading.Barrier(3)

    def remove():
        barrier.wait()
        store.remove("remove-me")

    def save():
        barrier.wait()
        for index in range(80):
            store.save(session("keep-me", index + 20, 100 + index))

    workers = [threading.Thread(target=remove), threading.Thread(target=save)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join()
    disk = json.loads(path.read_text(encoding="utf-8"))
    assert {item["session_id"] for item in disk} == {item.session_id for item in store.all()} == {"keep-me"}


def test_queue_truncates_only_invalid_unterminated_tail_and_keeps_prefix(tmp_path):
    path = tmp_path / "queue.jsonl"
    first = {"id": "one", "seconds": 1}
    path.write_bytes((json.dumps(first) + "\n").encode() + b'{"id":"broken"')
    queue = PlaytimeQueue(path)
    queue.append({"id": "two", "seconds": 2})
    assert [item["id"] for item in queue.pending()] == ["one", "two"]
    assert all(json.loads(line) for line in path.read_text().splitlines())


def test_queue_preserves_valid_unterminated_tail_and_ack_rewrites(tmp_path):
    path = tmp_path / "queue.jsonl"
    path.write_text('{"id":"one"}', encoding="utf-8")
    queue = PlaytimeQueue(path)
    assert queue.contains("one")
    queue.append({"id": "two"})
    queue.ack("one")
    assert queue.pending() == [{"id": "two"}]


def test_middle_corruption_rejects_append_and_ack_without_changing_bytes(tmp_path):
    path = tmp_path / "queue.jsonl"
    original = b'{"id":"one"}\nBAD\n{"id":"two"}\n'
    path.write_bytes(original)
    with pytest.raises(ValueError, match="line 2"):
        PlaytimeQueue(path)
    queue = PlaytimeQueue.__new__(PlaytimeQueue)
    queue.path, queue._lock, queue._records, queue._failed = path, threading.RLock(), [], False
    with pytest.raises(ValueError):
        queue.append({"id": "three"})
    with pytest.raises(ValueError):
        queue.ack("one")
    assert path.read_bytes() == original


def test_finish_queue_first_survives_remove_failure_and_retry_deduplicates(tmp_path, monkeypatch):
    store = RunningSessionStore(tmp_path / "running.json")
    queue = PlaytimeQueue(tmp_path / "queue.jsonl")
    item = session()
    store.save(item)
    original_remove = store.remove

    def fail_remove(session_id):
        raise OSError("injected remove failure")

    monkeypatch.setattr(store, "remove", fail_remove)
    service = PlaytimeService(store, queue)
    with pytest.raises(OSError):
        service.finish(item)
    assert queue.contains("s1")
    assert store.all()
    monkeypatch.setattr(store, "remove", original_remove)
    service.finish(item)
    assert len(queue.pending()) == 1
    assert not store.all()


def test_append_fsync_failure_keeps_running_and_restart_repairs_to_queue(tmp_path, monkeypatch):
    store = RunningSessionStore(tmp_path / "running.json")
    queue_path = tmp_path / "queue.jsonl"
    queue = PlaytimeQueue(queue_path)
    item = session()
    store.save(item)
    monkeypatch.setattr("playtime.os.fsync", lambda _fd: (_ for _ in ()).throw(OSError("injected fsync")))
    with pytest.raises(OSError):
        PlaytimeService(store, queue).finish(item)
    assert store.all()
    monkeypatch.undo()
    restarted_queue = PlaytimeQueue(queue_path)
    PlaytimeService(store, restarted_queue).recover_sessions()
    assert restarted_queue.contains("s1")
    assert not store.all()


def test_same_instance_fsync_failure_is_untrusted_until_reopen_barrier(tmp_path, monkeypatch):
    store = RunningSessionStore(tmp_path / "running.json")
    item = session("retry-after-barrier")
    store.save(item)
    path = tmp_path / "queue.jsonl"
    queue = PlaytimeQueue(path)
    service = PlaytimeService(store, queue)
    real_fsync = __import__("os").fsync

    def fail_fsync(_fd):
        raise OSError("injected fsync failure")

    monkeypatch.setattr("playtime.os.fsync", fail_fsync)
    with pytest.raises(OSError):
        service.finish(item)
    with pytest.raises(RuntimeError, match="reopen"):
        queue.contains(item.session_id)
    with pytest.raises(RuntimeError, match="reopen"):
        service.finish(item)
    assert store.all()
    with pytest.raises(OSError):
        PlaytimeQueue(path)
    assert store.all()

    monkeypatch.setattr("playtime.os.fsync", real_fsync)
    reopened = PlaytimeQueue(path)
    PlaytimeService(store, reopened).finish(store.all()[0])
    assert [record["id"] for record in reopened.pending()] == [item.session_id]
    assert not store.all()


def test_truncate_fsync_failure_keeps_running_until_restart_repair(tmp_path, monkeypatch):
    store = RunningSessionStore(tmp_path / "running.json")
    item = session()
    store.save(item)
    queue_path = tmp_path / "queue.jsonl"
    queue_path.write_bytes(b'{"id":"prefix"}\n{"id":"torn"')
    monkeypatch.setattr("playtime.os.fsync", lambda _fd: (_ for _ in ()).throw(OSError("injected fsync")))
    with pytest.raises(OSError):
        PlaytimeQueue(queue_path)
    assert store.all()
    monkeypatch.undo()
    queue = PlaytimeQueue(queue_path)
    PlaytimeService(store, queue).recover_sessions()
    assert queue.contains("prefix") and queue.contains("s1")
    assert not store.all()


def test_ack_and_append_share_lock_without_lost_append(tmp_path):
    path = tmp_path / "queue.jsonl"
    queue = PlaytimeQueue(path)
    for index in range(25):
        queue.append({"id": f"old-{index}"})
    barrier = threading.Barrier(3)

    def append():
        barrier.wait()
        for index in range(25):
            queue.append({"id": f"new-{index}"})

    def ack():
        barrier.wait()
        for index in range(25):
            queue.ack(f"old-{index}")

    workers = [threading.Thread(target=append), threading.Thread(target=ack)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join()
    assert {item["id"] for item in queue.pending()} == {f"new-{index}" for index in range(25)}


def test_sender_refreshes_own_jwt_once_and_acks_both_accepted_values(tmp_path):
    class Response:
        def __init__(self, status, body):
            self.status_code, self.body = status, body

        def json(self):
            return self.body

    class Http:
        def __init__(self):
            self.responses = [Response(401, {}), Response(200, {"accepted": False})]
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return self.responses.pop(0)

    class Api:
        def __init__(self):
            self.http = Http()
            self.forces = []

        def login(self, force=False):
            self.forces.append(force)
            return "bridge-own-jwt"

        def _http_session(self):
            return self.http

    class Config:
        reina_server_url = "https://reina.example/"

    queue = PlaytimeQueue(tmp_path / "queue.jsonl")
    queue.append({"id": "s1"})
    api = Api()
    sender = PlaytimeSender(api, Config(), queue)
    assert sender.send_once() == (True, 0)
    assert api.forces == [False, True]
    assert [call[1]["headers"]["Authorization"] for call in api.http.calls] == [
        "Bearer bridge-own-jwt", "Bearer bridge-own-jwt",
    ]
    assert all(call[1]["allow_redirects"] is False for call in api.http.calls)
    assert not queue.pending()


def test_recovery_attaches_matching_pid_or_root_fallback_and_closes_dead(tmp_path):
    class Adapter:
        def __init__(self, values):
            self.values = values

        def snapshot_all(self):
            return self.values

    store = RunningSessionStore(tmp_path / "running.json")
    queue = PlaytimeQueue(tmp_path / "queue.jsonl")
    active = session("active")
    child = session("child", 12, 110.0)
    dead = session("dead", 99)
    store.save(active)
    store.save(child)
    store.save(dead)
    adapter = Adapter([
        ProcessInfo(ProcessRef(11, 100.0), "C:/games/A/game.exe", None),
        ProcessInfo(ProcessRef(12, 110.0), "C:/games/A/child.exe", None),
    ])
    class Launcher:
        def __init__(self):
            self._lock = threading.RLock()
            self.sessions = {}
            self.store = store
            self.owners = {ref: item.session_id for item in store.all() for ref in item.pids}

        def claim_process(self, running, ref):
            owner = self.owners.get(ref)
            if owner is None:
                self.owners[ref] = running.session_id
                return True
            return owner == running.session_id

    launcher = Launcher()
    PlaytimeService(store, queue, launcher, adapter).recover_sessions()
    assert set(launcher.sessions) == {"active", "child"}
    assert queue.pending() == [{"id": "dead", "game_id": 7, "device": "PC", "start": 100,
                                "end": 120, "seconds": 20}]
    assert {item.session_id for item in store.all()} == {"active", "child"}


def test_recovery_repairs_queue_before_removing_already_recorded_session(tmp_path):
    store = RunningSessionStore(tmp_path / "running.json")
    store.save(session())
    path = tmp_path / "queue.jsonl"
    path.write_bytes(b'{"id":"s1","game_id":7}')
    queue = PlaytimeQueue(path)
    PlaytimeService(store, queue).recover_sessions()
    assert not store.all()
    assert queue.pending() == [{"id": "s1", "game_id": 7}]


def test_recovery_appends_after_repaired_tail_then_another_finish(tmp_path):
    store = RunningSessionStore(tmp_path / "running.json")
    first = session("already-queued")
    second = session("recover-me", 22)
    store.save(second)
    queue_path = tmp_path / "queue.jsonl"
    queue_path.write_bytes(b'{"id":"already-queued"}\n{"id":"torn')
    queue = PlaytimeQueue(queue_path)
    service = PlaytimeService(store, queue)
    service.recover_sessions()
    third = session("finish-after-recovery", 33)
    store.save(third)
    service.finish(third)
    records = [json.loads(line) for line in queue_path.read_text(encoding="utf-8").splitlines()]
    assert [item["id"] for item in records] == ["already-queued", "recover-me", "finish-after-recovery"]
    assert not store.all()


def test_root_fallback_recovers_child_but_rejects_reused_recorded_pid(tmp_path):
    class Adapter:
        def snapshot_all(self):
            return [
                ProcessInfo(ProcessRef(11, 999.0), "C:/games/A/reused.exe", None),
                ProcessInfo(ProcessRef(12, 150.0), "C:/games/A/child.exe", None),
            ]

    class Launcher:
        def __init__(self, store):
            self._lock = threading.RLock()
            self.sessions = {}
            self.store = store

        def claim_process(self, running, ref):
            return True

        def resume(self, running):
            pass

    store = RunningSessionStore(tmp_path / "running.json")
    original = session("root-recover", 11, 100.0)
    store.save(original)
    queue = PlaytimeQueue(tmp_path / "queue.jsonl")
    launcher = Launcher(store)
    PlaytimeService(store, queue, launcher, Adapter()).recover_sessions()
    assert launcher.sessions["root-recover"].pids == [ProcessRef(12, 150.0)]


def test_middle_corruption_recovery_leaves_running_and_queue_bytes_untouched(tmp_path):
    store = RunningSessionStore(tmp_path / "running.json")
    store.save(session())
    path = tmp_path / "queue.jsonl"
    raw = b'{"id":"one"}\nBROKEN\n'
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="line 2"):
        PlaytimeQueue(path)
    assert store.all()[0].session_id == "s1"
    assert path.read_bytes() == raw


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self.body = status, body if body is not None else {}

    def json(self):
        return self.body


class _ScriptedApi:
    """依 record id 回應的假 TeleDrive/reina-server 通道，同時記錄 login 次數。"""

    def __init__(self, by_id):
        self.by_id, self.posts, self.forces = by_id, [], []
        self.http = self

    def login(self, force=False):
        self.forces.append(force)
        return "bridge-own-jwt"

    def _http_session(self):
        return self

    def post(self, url, json, **kwargs):
        self.posts.append(json["id"])
        response = self.by_id[json["id"]]
        return response() if callable(response) else response


class _Cfg:
    reina_server_url = "https://reina.example"


def test_permanent_rejection_of_first_record_does_not_block_later_records(tmp_path):
    queue = PlaytimeQueue(tmp_path / "queue.jsonl")
    for record_id in ("deleted-game", "bad-record", "good"):
        queue.append({"id": record_id})
    api = _ScriptedApi({
        "deleted-game": _Resp(404, {"code": "not_found"}),
        "bad-record": _Resp(400, {"code": "invalid_session"}),
        "good": _Resp(200, {"accepted": True}),
    })
    now = [1000.0]
    sender = PlaytimeSender(api, _Cfg(), queue, clock=lambda: now[0])
    sent, _delay = sender.send_once()
    assert sent is True
    assert [item["id"] for item in queue.pending()] == ["deleted-game", "bad-record"]
    # 被擱置的紀錄在退避期內不會被重送，佇列裡只剩它們時回報等待時間
    api.posts.clear()
    assert sender.send_once() == (False, 900)
    assert api.posts == []
    now[0] += 901
    sender.send_once()
    assert api.posts[0] == "deleted-game"


def test_transient_server_error_still_holds_the_queue_in_order(tmp_path):
    queue = PlaytimeQueue(tmp_path / "queue.jsonl")
    queue.append({"id": "first"})
    queue.append({"id": "second"})
    api = _ScriptedApi({"first": _Resp(503), "second": _Resp(200, {"accepted": True})})
    sender = PlaytimeSender(api, _Cfg(), queue, clock=lambda: 0.0)
    assert sender.send_once() == (False, 5)
    assert api.posts == ["first"]


def test_repeated_unauthorized_pauses_instead_of_forcing_a_bot_login_each_retry(tmp_path):
    queue = PlaytimeQueue(tmp_path / "queue.jsonl")
    queue.append({"id": "s1"})
    api = _ScriptedApi({"s1": _Resp(401)})
    now = [0.0]
    sender = PlaytimeSender(api, _Cfg(), queue, clock=lambda: now[0])
    sent, delay = sender.send_once()
    assert sent is False and delay >= 1800
    assert api.forces == [False, True]
    # 暫停期間不再登入或送出
    now[0] += 600
    sent, delay = sender.send_once()
    assert sent is False and 1 <= delay <= 1200
    assert api.forces == [False, True]
    # 暫停期滿後才再試一次
    now[0] += 1300
    sender.send_once()
    assert api.forces == [False, True, False, True]


def test_corrupt_playtime_files_are_quarantined_instead_of_aborting_startup(tmp_path):
    from playtime import open_playtime_state

    queue_path = tmp_path / "playtime-queue.jsonl"
    running_path = tmp_path / "playtime-running.json"
    queue_path.write_bytes(b'{"id":"a"}\nnot json at all\n{"id":"c"}\n')
    running_path.write_text("{broken", encoding="utf-8")

    store, queue = open_playtime_state(tmp_path)

    assert store.all() == [] and queue.pending() == []
    queue.append({"id": "fresh"})
    assert [item["id"] for item in queue.pending()] == ["fresh"]
    # 原檔改名保留，內容一個位元組都不刪
    kept = sorted(path.name for path in tmp_path.glob("*.corrupt-*"))
    assert len(kept) == 2
    assert next(tmp_path.glob("playtime-queue.jsonl.corrupt-*")).read_bytes() == b'{"id":"a"}\nnot json at all\n{"id":"c"}\n'


def test_healthy_playtime_files_are_opened_untouched(tmp_path):
    from playtime import open_playtime_state

    (tmp_path / "playtime-queue.jsonl").write_bytes(b'{"id":"a"}\n')
    _store, queue = open_playtime_state(tmp_path)
    assert [item["id"] for item in queue.pending()] == ["a"]
    assert not list(tmp_path.glob("*.corrupt-*"))
