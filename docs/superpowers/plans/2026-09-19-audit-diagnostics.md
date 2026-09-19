# Audit Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 bridge、`isolate.exe` 與縮圖 DLL 能誠實回答「這次量測到底有沒有真的發生」，作為 live browse audit 的前置。

**Architecture:** 只新增唯讀 diagnostics，不改任何資料路徑或產品行為。三個面向：
(1) process 內的 origin-tagged download counter，取代被 `ThrottleRepeats` 打敗的 log 行數；
(2) bridge 回答 cache 狀態（記憶體 ＋ 磁碟）與 warmup 狀態；
(3) shell 側的逐檔 telemetry 與可在 process 存活期間改變的 DLL 記錄開關。

**Tech Stack:** Python 3 / pytest、cheroot WSGI、Telethon、C++（MSVC，`shellthumb/*.bat`）

**Spec:** `docs/superpowers/specs/2026-09-19-live-browse-audit-design.md`（rev 3.1）

## Global Constraints

- **實作 base：** `feat/live-browse-audit`，cut from `feat/current-backend-storage-parity` @ `39ad472`。不要把這些 commit 放回 parity 線上。
- **既存紅燈：** `tests/known_failures.txt` 記著 base 上已經失敗的 77 個測試。**每個任務的驗收是「新測試通過，且失敗集合沒有超出那份清單」**，不是「全套件綠」。
- **不改變任何資料路徑或產品行為。** 只加唯讀 diagnostics（spec §3.0）。
- **不得洩漏憑證。** 新端點與新 counter 一律不含 session string 或 JWT（spec §3.4）。
- **Python 側每一項 diagnostics 都要有離線測試**；C++ 那兩項沒有離線測試，由 Task 7/8 各自的手動 probe 驗證（spec §3.0）。
- **薄層架構事實：** `RpcApp` / `build_app()` / `main()` 定義在 `_bridge_legacy.py`；`Entry` / `_to_entry()` 在 `_tdapi_legacy.py`；wire I/O 在 `_tgio_legacy.py` 的 `_thumbnail_bytes()` / `_chunk()`；canonical 入口在薄層 `tgio.py`。
- **測試指令：** `.venv\Scripts\python.exe -m pytest tests -q`

---

### Task 1: Origin-tagged download counters

**Files:**
- Create: `diagnostics.py`
- Test: `tests/test_diagnostics.py`

**Interfaces:**
- Consumes: 無
- Produces: `diagnostics.ORIGINS: tuple[str, ...]`、`diagnostics.Counters`（方法 `record_download(origin: str, nbytes: int) -> None`、`bump(name: str, n: int = 1) -> None`、`snapshot() -> dict`、`reset() -> None`）、模組級單例 `diagnostics.COUNTERS`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_diagnostics.py
import threading

import pytest

from diagnostics import ORIGINS, Counters


def test_download_totals_are_kept_per_origin():
    c = Counters()
    c.record_download("props", 0)
    c.record_download("dav_read", 1024)
    c.record_download("dav_read", 512)
    snap = c.snapshot()
    assert snap["download_requests_total"]["props"] == 1
    assert snap["download_bytes_total"]["props"] == 0
    assert snap["download_requests_total"]["dav_read"] == 2
    assert snap["download_bytes_total"]["dav_read"] == 1536


def test_an_origin_never_used_reads_as_zero_not_missing():
    # The audit subtracts before/after snapshots. A missing key would make the
    # subtraction raise instead of proving "nothing was downloaded".
    snap = Counters().snapshot()
    for origin in ORIGINS:
        assert snap["download_requests_total"][origin] == 0
        assert snap["download_bytes_total"][origin] == 0


def test_an_unknown_origin_is_rejected_rather_than_silently_counted():
    # A typo'd origin that lands in its own bucket would make
    # download_bytes{props} == 0 look like proof when it is just misfiled.
    with pytest.raises(ValueError):
        Counters().record_download("propz", 10)


def test_named_counters_are_monotonic_and_start_at_zero():
    c = Counters()
    assert c.snapshot()["zip_open_attempts_total"] == 0
    c.bump("zip_open_attempts_total")
    c.bump("zip_open_attempts_total", 3)
    assert c.snapshot()["zip_open_attempts_total"] == 4


def test_concurrent_records_do_not_lose_counts():
    c = Counters()

    def work():
        for _ in range(500):
            c.record_download("dav_read", 1)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert c.snapshot()["download_requests_total"]["dav_read"] == 4000
    assert c.snapshot()["download_bytes_total"]["dav_read"] == 4000


def test_snapshot_is_a_copy_so_a_later_read_cannot_mutate_an_earlier_one():
    c = Counters()
    before = c.snapshot()
    c.record_download("thumb", 99)
    assert before["download_bytes_total"]["thumb"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_diagnostics.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'diagnostics'`

- [ ] **Step 3: Write minimal implementation**

```python
# diagnostics.py
"""Process-wide read-only counters for the live audit.

Log lines cannot answer "did this operation download anything": the bridge
installs ThrottleRepeats on telethon.client.downloads, so a suppressed line
and a line that never happened look identical. A counter that only ever goes
up can be snapshotted before and after a window instead.

Origin is an explicit argument, never inferred. Every read shares one asyncio
worker loop, so there is no thread-local or contextvar that survives the hop
and still says who asked.
"""

from __future__ import annotations

import threading

#: Every caller that can reach Telegram wire I/O. "unknown" is the default so
#: an untagged call site shows up as untagged rather than being misfiled into
#: a real origin and quietly ruining that origin's before/after subtraction.
ORIGINS = (
    "props",
    "thumb",
    "thumb_prefetch",
    "dav_read",
    "zip_index",
    "fetch_local",
    "warmup",
    "head",
    "unknown",
)

NAMED = (
    "zip_open_attempts_total",
    "zip_index_cache_misses_total",
    "thumb_requests_total",
    "props_requests_total",
)


class Counters:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests = {origin: 0 for origin in ORIGINS}
        self._bytes = {origin: 0 for origin in ORIGINS}
        self._named = {name: 0 for name in NAMED}

    def record_download(self, origin: str, nbytes: int) -> None:
        if origin not in self._requests:
            raise ValueError(f"unknown download origin {origin!r}")
        with self._lock:
            self._requests[origin] += 1
            self._bytes[origin] += int(nbytes)

    def bump(self, name: str, n: int = 1) -> None:
        if name not in self._named:
            raise ValueError(f"unknown counter {name!r}")
        with self._lock:
            self._named[name] += n

    def snapshot(self) -> dict:
        with self._lock:
            out = {
                "download_requests_total": dict(self._requests),
                "download_bytes_total": dict(self._bytes),
            }
            out.update(self._named)
            return out

    def reset(self) -> None:
        """Tests only. The live process never resets: the audit subtracts."""
        with self._lock:
            for table in (self._requests, self._bytes, self._named):
                for key in table:
                    table[key] = 0


COUNTERS = Counters()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_diagnostics.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add diagnostics.py tests/test_diagnostics.py
git commit -m "feat: add origin-tagged download counters for the live audit"
```

---

### Task 2: Thread `origin` through the read and thumbnail paths

**Files:**
- Modify: `_tgio_legacy.py` — `read_part()`, `TelegramWorker.read()`, `TelegramWorker._read()`, `TelegramWorker._chunk()`, `TelegramWorker.thumbnails()`, `TelegramWorker._thumbnail_bytes()`
- Modify: `tgio.py` — `read_part()`, `_read_location()`, `_thumbnail_location()`
- Test: `tests/test_diagnostics_wiring.py`

**Interfaces:**
- Consumes: `diagnostics.COUNTERS`, `diagnostics.ORIGINS` (Task 1)
- Produces: 每個入口都接受 keyword-only `origin: str = "unknown"`：
  `tgio.read_part(pool, part, offset, length, *, origin="dav_read")`、
  `TelegramWorker.read(..., *, origin="dav_read")`、
  `TelegramWorker.read_location(..., *, origin="dav_read")`、
  `TelegramWorker.thumbnails(parts, *, origin="thumb")`、
  `TelegramWorker.thumbnail_location(..., *, origin="thumb")`

**這個任務是跨 seam 的。** 記帳只發生在 `_chunk()` 與 `_thumbnail_bytes()`（全 repo 僅有的兩個 wire I/O 點），但 canonical 入口在薄層 `tgio.py`、legacy Saved Messages 入口在 `_tgio_legacy.py`。只改其中一個檔，另一條路徑的 origin 會永遠是 `unknown`，而**沒有任何東西會報錯**。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_diagnostics_wiring.py
"""Both entry points must tag their reads. Only one of them living in the
thin layer is exactly how half the traffic ends up filed as "unknown"."""

import inspect

import tgio
import _tgio_legacy


def _kwonly_default(func, name):
    param = inspect.signature(func).parameters[name]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, f"{func} takes {name} positionally"
    return param.default


def test_every_read_and_thumb_entry_point_accepts_an_origin():
    assert _kwonly_default(tgio.read_part, "origin") == "dav_read"
    assert _kwonly_default(_tgio_legacy.read_part, "origin") == "dav_read"
    assert _kwonly_default(_tgio_legacy.TelegramWorker.read, "origin") == "dav_read"
    assert _kwonly_default(tgio.TelegramWorker.read_location, "origin") == "dav_read"
    assert _kwonly_default(_tgio_legacy.TelegramWorker.thumbnails, "origin") == "thumb"
    assert _kwonly_default(tgio.TelegramWorker.thumbnail_location, "origin") == "thumb"


def test_the_wire_io_helpers_take_an_origin_they_can_record():
    # _chunk and _thumbnail_bytes are the only two iter_download call sites in
    # the repo; the counter has to be recorded there and nowhere higher, or a
    # retry above them is counted once and a short read is counted never.
    assert "origin" in inspect.signature(_tgio_legacy.TelegramWorker._chunk).parameters
    assert "origin" in inspect.signature(_tgio_legacy.TelegramWorker._thumbnail_bytes).parameters
```

```python
# append to tests/test_diagnostics_wiring.py
import asyncio

from diagnostics import COUNTERS


class _FakeIter:
    def __init__(self, payload):
        self._payload = payload

    def __aiter__(self):
        async def gen():
            yield self._payload
        return gen()


def test_chunk_records_bytes_against_the_origin_it_was_given(monkeypatch):
    before = COUNTERS.snapshot()["download_bytes_total"]["props"]

    worker = _tgio_legacy.TelegramWorker.__new__(_tgio_legacy.TelegramWorker)

    class _Client:
        def iter_download(self, *a, **kw):
            return _FakeIter(b"x" * 4096)

    doc = type("Doc", (), {"dc_id": 1})()
    monkeypatch.setattr(_tgio_legacy, "_download_location", lambda d: object(), raising=False)

    asyncio.run(worker._chunk(_Client(), doc, 0, origin="props"))

    after = COUNTERS.snapshot()["download_bytes_total"]["props"]
    assert after - before == 4096
```

> 若 `_chunk` 的內部細節與上面的假物件對不上，**調整假物件以符合真實簽章，不要改 `_chunk` 去遷就測試**。這個測試要證明的只有一件事：`_chunk` 收到的 `origin` 就是它記帳用的那一個。

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_diagnostics_wiring.py -q`
Expected: FAIL with `KeyError: 'origin'`

- [ ] **Step 3: Write minimal implementation**

`_tgio_legacy.py` — 記帳點：

```python
    async def _chunk(self, client, doc, offset: int, *, origin: str = "unknown") -> bytes:
        # ... existing body unchanged, keeping the result in `data` ...
        diagnostics.COUNTERS.record_download(origin, len(data))
        return data
```

```python
    async def _thumbnail_bytes(self, doc, *, origin: str = "unknown") -> Optional[bytes]:
        # ... existing body unchanged, keeping the result in `blob` ...
        diagnostics.COUNTERS.record_download(origin, len(blob or b""))
        return blob
```

`_tgio_legacy.py` — 一路往上傳：

```python
def read_part(pool, part: RemotePart, offset: int, length: int, *, origin: str = "dav_read") -> bytes:
    return pool.for_read(part.telegram_user_id).worker.read(
        ..., origin=origin,
    )
```

```python
    def read(self, ..., *, origin: str = "dav_read") -> bytes:
        ...
            return self.run(self._read(doc, offset, length, origin=origin))   # both call sites

    async def _read(self, doc, offset: int, length: int, *, origin: str = "unknown") -> bytes:
        ...
        tasks = [self._chunk(self._next_client(pool), doc, start, origin=origin) for start in starts]

    def thumbnails(self, parts, *, origin: str = "thumb") -> dict:
        ...
                    return key, await self._thumbnail_bytes(doc, origin=origin)
```

`tgio.py` — canonical 那一半（**這是只改 legacy 就會漏掉的部分**）：

```python
def read_part(pool, part, offset: int, length: int, *, origin: str = "dav_read") -> bytes:
    if not isinstance(part, ResolvedRemotePart):
        return _legacy_read_part(pool, part, offset, length, origin=origin)
    ...
            return runtime.worker.read_location(part.location, peer, offset, length, origin=origin)


def _read_location(self, location, peer, offset, length, *, origin: str = "dav_read"):
    ...
        return self.run(self._read(media, offset, length, origin=origin))   # both call sites


def _thumbnail_location(self, location, peer, *, origin: str = "thumb"):
    ...
        return self.thumbnails([part], origin=origin).get(...)
    ...
    return self.run(self._thumbnail_bytes(media, origin=origin), timeout=60)
```

在 `_tgio_legacy.py` 與 `tgio.py` 頂端各加 `import diagnostics`。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_diagnostics_wiring.py tests/test_split_math.py tests/test_read_pace.py tests/test_thumbnails.py -q`
Expected: PASS，且 `test_split_math` 仍是 37/37

- [ ] **Step 5: Check the baseline did not grow**

```bash
.venv/Scripts/python.exe -m pytest tests -q 2>&1 | grep "^FAILED" | sed 's/ - .*//;s/^FAILED //' | sort > /tmp/now.txt
comm -13 tests/known_failures.txt /tmp/now.txt
```

Expected: 沒有輸出（沒有新的失敗）。有輸出就是這個任務弄壞的，修掉再往下。

- [ ] **Step 6: Commit**

```bash
git add diagnostics.py tgio.py _tgio_legacy.py tests/test_diagnostics_wiring.py
git commit -m "feat: tag telegram reads with their origin across the tgio seam"
```

---

### Task 3: `BackgroundWarmup.status()`

**Files:**
- Modify: `warmup.py` — `BackgroundWarmup`
- Test: `tests/test_warmup_status.py`

**Interfaces:**
- Consumes: 無
- Produces: `BackgroundWarmup.status() -> dict`，鍵為 `enabled: bool`、`active: bool`、`phase: str`（`"idle"` / `"walking"` / `"filling"` / `"shell_warm"` / `"stopped"`）、`pass: int`、`next_run_at: str | None`（ISO 8601 本地時間）

`active` 是 audit 判定 counter 有沒有被 sweep 污染的唯一依據（spec §6.2），所以它必須在 `_pass()` **開始時**就為真、結束才轉回 `idle`，不能只在某個子階段為真。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_warmup_status.py
import time

from warmup import BackgroundWarmup


class _Resolver:
    def clear_heads(self):
        pass


def _fresh():
    return BackgroundWarmup.__new__(BackgroundWarmup)


def test_a_warmup_that_never_started_is_idle_and_not_active():
    w = BackgroundWarmup(_Resolver(), interval_minutes=60, start_delay=999)
    s = w.status()
    assert s["active"] is False
    assert s["phase"] == "idle"
    assert s["pass"] == 0


def test_phase_reports_which_stage_is_running_and_active_covers_all_of_them():
    w = BackgroundWarmup(_Resolver(), interval_minutes=60, start_delay=999)
    seen = []
    for phase in ("walking", "filling", "shell_warm"):
        w._enter(phase)
        s = w.status()
        seen.append((s["phase"], s["active"]))
    assert seen == [("walking", True), ("filling", True), ("shell_warm", True)]
    w._enter("idle")
    assert w.status()["active"] is False


def test_pass_counts_completed_passes_and_next_run_at_is_set_after_one():
    w = BackgroundWarmup(_Resolver(), interval_minutes=60, start_delay=999)
    assert w.status()["next_run_at"] is None
    w._enter("walking")
    w._finish_pass()
    s = w.status()
    assert s["pass"] == 1
    assert s["phase"] == "idle"
    assert s["next_run_at"] is not None


def test_status_never_blocks_on_the_warmup_thread():
    # The audit calls this between measurement windows; if it waited on the
    # sweep it would perturb exactly what it is trying to observe.
    w = BackgroundWarmup(_Resolver(), interval_minutes=60, start_delay=999)
    w._enter("filling")
    started = time.monotonic()
    w.status()
    assert time.monotonic() - started < 0.05
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_warmup_status.py -q`
Expected: FAIL with `AttributeError: 'BackgroundWarmup' object has no attribute 'status'`

- [ ] **Step 3: Write minimal implementation**

在 `BackgroundWarmup.__init__` 末尾加：

```python
        self._phase = "idle"
        self._passes = 0
        self._next_run_at: Optional[float] = None
        self._phase_lock = threading.Lock()
```

新增三個方法：

```python
    def _enter(self, phase: str) -> None:
        with self._phase_lock:
            self._phase = phase

    def _finish_pass(self) -> None:
        with self._phase_lock:
            self._phase = "idle"
            self._passes += 1
            self._next_run_at = time.time() + self.interval

    def status(self) -> dict:
        """Read-only. Never waits on the sweep thread -- the audit calls this
        between measurement windows to decide whether a counter delta is
        trustworthy, so blocking here would disturb what it is observing."""
        with self._phase_lock:
            phase, passes, nxt = self._phase, self._passes, self._next_run_at
        return {
            "enabled": self._thread is not None,
            "active": phase not in ("idle", "stopped"),
            "phase": phase,
            "pass": passes,
            "next_run_at": (
                time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(nxt)) if nxt else None
            ),
        }
```

在 `_pass()` 裡標記階段——`walking` 包住 `warmer.pending()`、`filling` 包住 `warmer.fill(todo)`、`shell_warm` 包住 `warmer.shell_warm(files)`，並在 `_pass()` 結尾（`clear_heads()` 之後）呼叫 `self._finish_pass()`。`stop()` 裡加 `self._enter("stopped")`。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_warmup_status.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Check the baseline did not grow**

```bash
.venv/Scripts/python.exe -m pytest tests -q 2>&1 | grep "^FAILED" | sed 's/ - .*//;s/^FAILED //' | sort > /tmp/now.txt
comm -13 tests/known_failures.txt /tmp/now.txt
```

Expected: 沒有輸出

- [ ] **Step 6: Commit**

```bash
git add warmup.py tests/test_warmup_status.py
git commit -m "feat: report which sweep phase BackgroundWarmup is in"
```

---

### Task 4: `/rpc/health` 的 `cryptg` 與 `/rpc/status` 的 `warmup`

**Files:**
- Modify: `_bridge_legacy.py` — `RpcApp.__init__`、`RpcApp._health`、`RpcApp._status`、`build_app()`、`main()`
- Test: `tests/test_rpc_diagnostics.py`

**Interfaces:**
- Consumes: `BackgroundWarmup.status()` (Task 3)
- Produces: `RpcApp(cfg, resolver, fetcher, stager, upload_stager=None, warmup=None)`；`GET /rpc/health` 多一個 `"cryptg": bool`；`GET /rpc/status` 多一個 `"warmup": {...}`

`cryptg` 用 `importlib.util.find_spec` 判斷，不 import 它——這個端點在每次 preflight 都會被打，不該有副作用。少了 `cryptg`，Telethon 會退回純 Python AES-IGE，把下載壓在 ~0.15 MiB/s，那時候量任何延遲都沒有意義。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rpc_diagnostics.py
import json

import _bridge_legacy as legacy


class _Sink:
    def __init__(self):
        self.status = None

    def __call__(self, status, headers):
        self.status = status
        return lambda _b: None


def _body(app, route):
    sink = _Sink()
    chunks = app({"PATH_INFO": f"/rpc{route}", "REQUEST_METHOD": "GET"}, sink)
    assert sink.status.startswith("200")
    return json.loads(b"".join(chunks).decode("utf-8"))


def _app(warmup=None):
    app = legacy.RpcApp.__new__(legacy.RpcApp)
    app.cfg = type("C", (), {"base_url": "https://example/api/v1",
                             "mount_drive": "H:", "game_folder": "game"})()
    app.resolver = type("R", (), {"pool": type("P", (), {
        "primary": type("A", (), {"worker": type("W", (), {"user_id": 4242})()})(),
        "status": staticmethod(lambda: {"accounts": []}),
    })()})()
    app.stager = None
    app.upload_stager = None
    app.warmup = warmup
    return app


def test_health_reports_whether_cryptg_is_importable():
    body = _body(_app(), "/health")
    assert isinstance(body["cryptg"], bool)


def test_status_carries_the_warmup_view():
    class _W:
        @staticmethod
        def status():
            return {"enabled": True, "active": True, "phase": "filling",
                    "pass": 2, "next_run_at": None}

    body = _body(_app(_W()), "/status")
    assert body["warmup"]["phase"] == "filling"
    assert body["warmup"]["active"] is True


def test_status_says_disabled_rather_than_omitting_warmup_when_none_is_wired():
    # A missing key and "not running" must not look the same: the audit uses
    # this to decide whether a counter delta can be trusted.
    body = _body(_app(None), "/status")
    assert body["warmup"] == {"enabled": False, "active": False,
                              "phase": "idle", "pass": 0, "next_run_at": None}


def test_neither_endpoint_leaks_a_credential():
    for route in ("/health", "/status"):
        raw = json.dumps(_body(_app(), route))
        for needle in ("session", "token", "jwt", "auth_key"):
            assert needle not in raw.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_rpc_diagnostics.py -q`
Expected: FAIL with `KeyError: 'cryptg'`

- [ ] **Step 3: Write minimal implementation**

`_bridge_legacy.py` 頂端：

```python
import importlib.util
```

`RpcApp.__init__` 簽章尾端加 `warmup=None`，並 `self.warmup = warmup`。

```python
_WARMUP_OFF = {"enabled": False, "active": False, "phase": "idle",
               "pass": 0, "next_run_at": None}
```

`_health` 的 dict 裡加：

```python
                # find_spec, not import: preflight hits this endpoint every run
                # and it must stay side-effect free. Without cryptg Telethon
                # falls back to pure-Python AES-IGE and pins downloads at
                # ~0.15 MiB/s, which makes every latency number meaningless.
                "cryptg": importlib.util.find_spec("cryptg") is not None,
```

`_status` 的 dict 裡加：

```python
                "warmup": self.warmup.status() if self.warmup else dict(_WARMUP_OFF),
```

`build_app()` 多收一個 `warmup=None` 並傳進 `RpcApp`；`main()` 在建好 `BackgroundWarmup` 之後把它交給 `build_app()`。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_rpc_diagnostics.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Check the baseline did not grow**

```bash
.venv/Scripts/python.exe -m pytest tests -q 2>&1 | grep "^FAILED" | sed 's/ - .*//;s/^FAILED //' | sort > /tmp/now.txt
comm -13 tests/known_failures.txt /tmp/now.txt
```

Expected: 沒有輸出

- [ ] **Step 6: Commit**

```bash
git add _bridge_legacy.py tests/test_rpc_diagnostics.py
git commit -m "feat: expose cryptg and sweep state on the rpc diagnostics"
```

---

### Task 5: `GET /rpc/counters`

**Files:**
- Modify: `_bridge_legacy.py` — `RpcApp.__call__` 路由表、新增 `RpcApp._counters`
- Test: `tests/test_rpc_diagnostics.py`（追加）

**Interfaces:**
- Consumes: `diagnostics.COUNTERS` (Task 1)
- Produces: `GET /rpc/counters` → `diagnostics.COUNTERS.snapshot()` 的 JSON

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_rpc_diagnostics.py
from diagnostics import COUNTERS, ORIGINS


def test_counters_endpoint_exposes_every_origin_even_at_zero():
    body = _body(_app(), "/counters")
    for origin in ORIGINS:
        assert origin in body["download_requests_total"]
        assert origin in body["download_bytes_total"]
    assert "zip_open_attempts_total" in body


def test_counters_endpoint_reflects_recorded_traffic():
    before = _body(_app(), "/counters")["download_bytes_total"]["zip_index"]
    COUNTERS.record_download("zip_index", 700)
    after = _body(_app(), "/counters")["download_bytes_total"]["zip_index"]
    assert after - before == 700
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_rpc_diagnostics.py -q -k counters`
Expected: FAIL — `sink.status` 是 `404 Not Found`

- [ ] **Step 3: Write minimal implementation**

`_bridge_legacy.py` 頂端 `import diagnostics`；路由表在 `/props` 之後加：

```python
            if route in ("/counters", "/counters/"):
                return self._counters(start_response)
```

```python
    def _counters(self, start_response):
        """Monotonic wire-I/O totals, by origin.

        The audit subtracts two snapshots to prove a window downloaded
        nothing. Log lines cannot do that job: ThrottleRepeats suppresses
        repeats of the same telethon template, so a suppressed download and
        an absent one read identically.
        """
        return _text_response(start_response, "200 OK",
                              json.dumps(diagnostics.COUNTERS.snapshot()),
                              "application/json")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_rpc_diagnostics.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Check the baseline did not grow**

```bash
.venv/Scripts/python.exe -m pytest tests -q 2>&1 | grep "^FAILED" | sed 's/ - .*//;s/^FAILED //' | sort > /tmp/now.txt
comm -13 tests/known_failures.txt /tmp/now.txt
```

Expected: 沒有輸出

- [ ] **Step 6: Commit**

```bash
git add _bridge_legacy.py tests/test_rpc_diagnostics.py
git commit -m "feat: serve the download counters over rpc"
```

---

### Task 6: `GET /rpc/cache-state?path=`

**Files:**
- Modify: `_bridge_legacy.py` — 路由表、新增 `RpcApp._cache_state`；`Resolver` 新增 `cache_state(entry, kinds)`
- Test: `tests/test_cache_state.py`

**Interfaces:**
- Consumes: `Resolver._cache_key(entry)`、`Resolver._zips`、`Resolver._zip_cache`、`Resolver._prop_cache`、`Resolver._thumb_path(entry)`
- Produces: `Resolver.cache_state(entry, kinds: Sequence[str]) -> dict[str, dict[str, bool]]`；`GET /rpc/cache-state?path=<Windows 路徑>&kinds=zip,thumb,props` → 同樣形狀的 JSON

**audit 不可以自己算 key。** 這條 branch 的 key 是 `loc3-<sha256>`，由 `current_parts(entry)` → `physical_location_key()` 逐次向 backend 取得目前的 physical row 算出來；client 手上的 key 隨時可能過期，而過期的 key 會回「沒快取」，audit 於是把暖的當冷的量——正是本設計要防的那個錯誤換一條路徑發生。

**磁碟沒檔 ≠ cold。** `Resolver._zips` 持有 `ZipView`、`ZipView._root` memo 過整棵樹、`ShardedJsonStore._memory` 是一個 dict。跑了一天的 bridge 會在完全沒有索引檔的情況下瞬間回答。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cache_state.py
import json

import pytest

import _bridge_legacy as legacy


class _Store:
    def __init__(self, memory=(), disk=()):
        self._memory = {k: 1 for k in memory}
        self._disk = set(disk)

    def has_in_memory(self, key):
        return key in self._memory

    def has_on_disk(self, key):
        return key in self._disk


class _Resolver:
    def __init__(self, key, zips=(), store=None, thumb=None, props=None, tmp_path=None):
        self._key = key
        self._zips = dict.fromkeys(zips, object())
        self._zip_cache = store or _Store()
        self._prop_cache = props or _Store()
        self._thumb = thumb
        self.cache_state = legacy.Resolver.cache_state.__get__(self)

    def _cache_key(self, entry):
        return self._key

    def _thumb_path(self, entry):
        return self._thumb


def test_a_view_held_only_in_memory_is_warm_not_cold(tmp_path):
    r = _Resolver("loc3-abc", zips=["loc3-abc"], store=_Store(memory=[], disk=[]))
    state = r.cache_state(object(), ["zip"])
    assert state["zip"] == {"memory": True, "disk": False}


def test_a_store_entry_held_only_in_memory_is_warm(tmp_path):
    r = _Resolver("loc3-abc", store=_Store(memory=["loc3-abc"], disk=[]))
    assert r.cache_state(object(), ["zip"])["zip"]["memory"] is True


def test_nothing_anywhere_is_cold(tmp_path):
    r = _Resolver("loc3-abc", store=_Store())
    assert r.cache_state(object(), ["zip"]) == {"zip": {"memory": False, "disk": False}}


def test_thumb_disk_state_comes_from_the_real_path(tmp_path):
    present = tmp_path / "loc3-abc.jpg"
    present.write_bytes(b"x")
    r = _Resolver("loc3-abc", thumb=present)
    assert r.cache_state(object(), ["thumb"])["thumb"] == {"memory": False, "disk": True}
    r2 = _Resolver("loc3-abc", thumb=tmp_path / "missing.jpg")
    assert r2.cache_state(object(), ["thumb"])["thumb"]["disk"] is False


def test_an_unknown_kind_raises_rather_than_reporting_a_confident_false():
    # Reporting {"memory": False, "disk": False} for a kind nobody implemented
    # would read as "cold" and license a cold-path threshold that never ran.
    r = _Resolver("loc3-abc")
    with pytest.raises(ValueError):
        r.cache_state(object(), ["nosuchkind"])
```

```python
# append to tests/test_cache_state.py
class _Sink:
    def __init__(self):
        self.status = None

    def __call__(self, status, headers):
        self.status = status
        return lambda _b: None


def test_the_endpoint_resolves_the_path_itself_and_never_takes_a_key():
    seen = {}

    class _App(legacy.RpcApp):
        pass

    app = _App.__new__(_App)
    app.resolver = type("R", (), {
        "resolve": staticmethod(lambda p: seen.setdefault("path", p) or "entry"),
        "cache_state": staticmethod(lambda e, kinds: {"zip": {"memory": True, "disk": False}}),
    })()
    sink = _Sink()
    chunks = app({"PATH_INFO": "/rpc/cache-state",
                  "QUERY_STRING": "path=H%3A%5Cgame%5Cfoo.zip&kinds=zip",
                  "REQUEST_METHOD": "GET"}, sink)
    assert sink.status.startswith("200")
    assert seen["path"] == "H:\\game\\foo.zip"
    assert json.loads(b"".join(chunks).decode("utf-8"))["zip"]["memory"] is True
```

> `_Resolver` 的假 store 需要一個 `has_on_disk`。若真實的 `ShardedJsonStore` / `JsonStore` 還沒有這個方法，**在 Step 3 一併加上**（讀目錄項目或檢查檔案存在，不要把值讀進記憶體——讀進去就把冷的變暖了）。

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cache_state.py -q`
Expected: FAIL with `AttributeError: type object 'Resolver' has no attribute 'cache_state'`

- [ ] **Step 3: Write minimal implementation**

`_tdapi_legacy.py` 的 `JsonStore` 與 `ShardedJsonStore` 各加：

```python
    def has_in_memory(self, key) -> bool:
        return key in self._memory

    def has_on_disk(self, key) -> bool:
        """Existence only -- reading the value in would warm the very cache
        the caller is asking about."""
        return self._path_for(key).exists()      # ShardedJsonStore
```

`_bridge_legacy.py` 的 `Resolver`：

```python
_CACHE_KINDS = ("zip", "thumb", "props")


    def cache_state(self, entry, kinds) -> dict:
        """Is this entry's cached answer already available, and from where?

        Disk absence is not coldness: _zips holds live ZipViews, ZipView
        memoises its root, and the json stores keep a dict. A bridge that has
        been up for a day answers instantly with no file on disk, and calling
        that "cold" is how a warm measurement gets a cold threshold.
        """
        key = self._cache_key(entry)
        out = {}
        for kind in kinds:
            if kind not in _CACHE_KINDS:
                raise ValueError(f"unknown cache kind {kind!r}")
            if kind == "zip":
                out[kind] = {
                    "memory": key in self._zips or self._zip_cache.has_in_memory(key),
                    "disk": self._zip_cache.has_on_disk(key),
                }
            elif kind == "props":
                out[kind] = {
                    "memory": self._prop_cache.has_in_memory(key),
                    "disk": self._prop_cache.has_on_disk(key),
                }
            else:
                path = self._thumb_path(entry)
                out[kind] = {"memory": False, "disk": bool(path and path.exists())}
        return out
```

`RpcApp` 路由表加 `/cache-state`，並：

```python
    def _cache_state(self, environ, start_response):
        params = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
        path = (params.get("path") or [""])[0]
        kinds = [k for k in (params.get("kinds") or ["zip,thumb,props"])[0].split(",") if k]
        # The bridge resolves the path itself. A key computed by the client can
        # already disagree with the authoritative physical location -- and a
        # stale key answers "not cached", which reads as cold.
        entry = self.resolver.resolve(path)
        body = json.dumps(self.resolver.cache_state(entry, kinds))
        return _text_response(start_response, "200 OK", body, "application/json")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cache_state.py tests/test_sizes.py -q`
Expected: PASS（`test_sizes.py` 涵蓋兩個 store，必須仍然全過）

- [ ] **Step 5: Check the baseline did not grow**

```bash
.venv/Scripts/python.exe -m pytest tests -q 2>&1 | grep "^FAILED" | sed 's/ - .*//;s/^FAILED //' | sort > /tmp/now.txt
comm -13 tests/known_failures.txt /tmp/now.txt
```

Expected: 沒有輸出

- [ ] **Step 6: Commit**

```bash
git add _bridge_legacy.py _tdapi_legacy.py tests/test_cache_state.py
git commit -m "feat: answer cache state by path, covering memory and disk"
```

---

### Task 7: `isolate.exe` 的 `--jsonl` 與 `--manifest`

**Files:**
- Modify: `shellthumb/isolate.cpp`
- Test: 無離線測試（C++）。驗證見 Step 4，並由後續 audit 的 preflight 每次執行前重跑。

**Interfaces:**
- Consumes: 無
- Produces: `isolate [--jsonl] [--manifest <file>] <thumb|props> <folder> [count] [px]`。`--jsonl` 時每個檔往 **stderr** 印一行並 flush；stdout 仍印原本的 aggregate。

**必須是 narrow UTF-8，不能用 `fwprintf`。** 寬字元輸出會被轉成 console codepage，而這裡的路徑大半是非 ASCII——那正是「URL 跳脫」那條坑的同一種死法，`warmshell.cpp` 已經踩過。

- [ ] **Step 1: Add the two flags and the manifest reader**

在 `wmain` 的參數解析前加旗標掃描；`--manifest <file>` 時**不掃資料夾**，改逐行讀 UTF-8 檔案（去掉 BOM 與行尾 `\r`），每行一個完整路徑。

```cpp
static void EmitJsonl(const wchar_t* op, const std::wstring& path,
                      double ms, bool answered, HRESULT hr) {
    // stderr, narrow UTF-8, flushed per file: a batch killed on a deadline
    // still says how far it got, and non-ASCII paths survive. fwprintf would
    // transcode to the console codepage and mangle most of these names.
    const int n = WideCharToMultiByte(CP_UTF8, 0, path.c_str(), -1, nullptr, 0, nullptr, nullptr);
    std::string utf8(n > 0 ? n - 1 : 0, '\0');
    if (n > 0) WideCharToMultiByte(CP_UTF8, 0, path.c_str(), -1, utf8.data(), n, nullptr, nullptr);
    std::string escaped;
    for (char c : utf8) {
        if (c == '"' || c == '\\') escaped += '\\';
        escaped += c;
    }
    fprintf(stderr, "{\"op\":\"%ls\",\"file\":\"%s\",\"elapsed_ms\":%.0f,"
                    "\"answered\":%s,\"hr\":\"0x%08X\"}\n",
            op, escaped.c_str(), ms, answered ? "true" : "false", (unsigned)hr);
    fflush(stderr);
}
```

在既有的 per-file 迴圈裡計時（用同一個 `Now(freq)`），每個檔結束呼叫一次 `EmitJsonl`。

- [ ] **Step 2: Build**

Run: `shellthumb\buildbench.bat`
Expected: 產生新的 `shellthumb\isolate.exe`，沒有編譯錯誤

- [ ] **Step 3: Verify the aggregate output is unchanged**

Run: `shellthumb\isolate.exe thumb <一個本機圖片資料夾> 4`
Expected: 仍然只有原本那一行 `thumb: 4 files in ...`，stderr 沒有東西

- [ ] **Step 4: Verify JSONL round-trips a non-ASCII path**

建一個含中文檔名的本機資料夾（例如 `湊あくあ.jpg`），然後：

```powershell
shellthumb\isolate.exe --jsonl thumb <那個資料夾> 1 2>jsonl.txt
.venv\Scripts\python.exe -c "import json,io; [print(json.loads(l)['file']) for l in io.open('jsonl.txt',encoding='utf-8') if l.strip()]"
```

Expected: 印出的檔名與磁碟上完全相同（不是 `æ¹...`）。**這一步是 Task 7 唯一的驗證，不可跳過**——它同時證明 UTF-8 與 JSON 逸出都對。

- [ ] **Step 5: Verify the manifest path is honoured**

把兩個路徑寫進 `m.txt`（其中一個是 `.txt` 檔），然後 `isolate.exe --jsonl --manifest m.txt thumb .`
Expected: stderr 剛好兩行，`file` 就是 manifest 裡那兩個，且 `.txt` 那行 `answered` 為 `false`

- [ ] **Step 6: Commit**

```bash
git add shellthumb/isolate.cpp shellthumb/isolate.exe
git commit -m "feat: per-file jsonl telemetry and an explicit manifest for isolate"
```

---

### Task 8: DLL `LogPath` 可在 process 存活期間改變

**Files:**
- Modify: `shellthumb/TeleDriveThumb.cpp` — `Log()`
- Test: 無離線測試（C++、thread-safety）。驗證見 Step 3-4。

**Interfaces:**
- Consumes: 無
- Produces: `Log()` 每 2 秒最多重讀一次 `HKCU\Software\TeleDriveWebDAV\LogPath`，`path` 與時間戳由同一個 SRWLOCK 保護。

現況是 `static bool checked`，**每個 host process 只讀一次**。已載入的 `dllhost` 在 audit 設定 `LogPath` 之後永遠不會開始記錄，於是 audit 依 spec §4 把整批判成 `NOT_MEASURED`——**這正是這份設計要避免的 measurement trap**。

- [ ] **Step 1: Replace the one-shot flag with a locked TTL**

```cpp
// Diagnostics. Unlike GetSettings(), this MUST be able to change while the
// host process lives: the audit turns logging on against a surrogate that is
// already running, and a one-shot read leaves it permanently silent.
//
// Do NOT "simplify" this into a magic static the way GetSettings() is. That
// one is on the per-file hot path and its answer never changes, which is
// exactly why it must be immutable; this one is the opposite on both counts.
static SRWLOCK gLogLock = SRWLOCK_INIT;
static std::wstring gLogPath;
static ULONGLONG gLogPathCheckedAt = 0;   // 0 = never
static const ULONGLONG kLogPathTtlMs = 2000;

static std::wstring CurrentLogPath() {
    const ULONGLONG now = GetTickCount64();

    AcquireSRWLockShared(&gLogLock);
    const bool fresh = gLogPathCheckedAt != 0 && (now - gLogPathCheckedAt) < kLogPathTtlMs;
    std::wstring cached = fresh ? gLogPath : std::wstring();
    ReleaseSRWLockShared(&gLogLock);
    if (fresh) return cached;

    AcquireSRWLockExclusive(&gLogLock);
    // Re-check: another thread may have refreshed while we waited.
    if (gLogPathCheckedAt != 0 && (GetTickCount64() - gLogPathCheckedAt) < kLogPathTtlMs) {
        std::wstring current = gLogPath;
        ReleaseSRWLockExclusive(&gLogLock);
        return current;
    }
    std::wstring found;
    HKEY key = nullptr;
    if (RegOpenKeyExW(HKEY_CURRENT_USER, kSettingsKey, 0, KEY_READ, &key) == ERROR_SUCCESS) {
        found = ReadString(key, L"LogPath");
        RegCloseKey(key);
    }
    if (found.empty()) found = ReadSetting(nullptr, L"LogPath");
    gLogPath = found;
    gLogPathCheckedAt = GetTickCount64();
    std::wstring current = gLogPath;
    ReleaseSRWLockExclusive(&gLogLock);
    return current;
}
```

`Log()` 開頭改成 `const std::wstring path = CurrentLogPath(); if (path.empty()) return;`，其餘不動。

- [ ] **Step 2: Build**

```powershell
taskkill /f /im dllhost.exe
shellthumb\build.bat
```

Expected: 建置成功。被鎖住就再殺一次 `dllhost.exe`，必要時重啟 `explorer.exe`。

- [ ] **Step 3: Verify logging turns on without restarting the surrogate**

```powershell
reg delete "HKCU\Software\TeleDriveWebDAV" /v LogPath /f
# 先讓 surrogate 載入 handler：開一個沒看過的 H: 圖片資料夾
reg add "HKCU\Software\TeleDriveWebDAV" /v LogPath /t REG_SZ /d C:\Temp\dll.log /f
# 等 3 秒（> TTL），再開另一個沒看過的資料夾
type C:\Temp\dll.log
```

Expected: `dll.log` 有 `GetThumbnail` 行。**這是 Task 8 的核心驗證**——修改之前這裡會是空的。

- [ ] **Step 4: Verify it turns back off**

```powershell
reg delete "HKCU\Software\TeleDriveWebDAV" /v LogPath /f
# 等 3 秒，再開另一個沒看過的資料夾
```

Expected: `dll.log` 沒有再長大（關掉也要在同一個 process 內生效）

- [ ] **Step 5: Commit**

```bash
git add shellthumb/TeleDriveThumb.cpp shellthumb/TeleDriveThumb.dll
git commit -m "fix: let the DLL pick up LogPath changes without a new surrogate"
```

---

### Task 9: `Entry` 帶上 `telegram_media_kind` 與 `telegram_chat_id`

**Files:**
- Modify: `_tdapi_legacy.py` — `Entry`、`_to_entry()`
- Test: `tests/test_entry_media_kind.py`

**Interfaces:**
- Consumes: 無
- Produces: `Entry.telegram_media_kind: str | None`、`Entry.telegram_chat_id: str | None`，兩者皆預設 `None`

Discovery 要找「chat import ＋ photo」的候選樣本（spec §7）。backend 的 `FileInfo`
已經回這兩個欄位，`_to_entry()` 只是把它們丟掉。撿回來之後，分類跨 DC 候選**不必
額外打任何一輪 Telegram**——只有要證明「真的跨 DC」時才需要查 `dc_id`。

**兩個欄位都必須有預設值。** `Entry` 被到處建構（測試、快取回讀、舊格式），
沒有預設值會讓這個純粹加值的改動變成一堆 `TypeError`。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_entry_media_kind.py
from _tdapi_legacy import Entry, _to_entry


def _row(**over):
    row = {
        "file_id": "f1", "filename": "a.jpg", "isDir": False, "filesize": 12,
        "created_at": None, "mime_type": "image/jpeg",
        "telegram_message_id": 8, "telegram_user_id": 42,
    }
    row.update(over)
    return row


def test_media_kind_and_chat_id_survive_the_row_conversion():
    e = _to_entry(_row(telegram_media_kind="photo", telegram_chat_id="-100123"))
    assert e.telegram_media_kind == "photo"
    assert e.telegram_chat_id == "-100123"


def test_a_row_without_them_reads_as_none_rather_than_raising():
    e = _to_entry(_row())
    assert e.telegram_media_kind is None
    assert e.telegram_chat_id is None


def test_the_new_fields_are_optional_when_constructing_an_entry_directly():
    # Entry is built in tests, in cache read-back and for pre-schema rows.
    # A required field would turn a purely additive change into TypeErrors.
    e = Entry(file_id="f", name="a", is_dir=False, size=1)
    assert e.telegram_media_kind is None
    assert e.telegram_chat_id is None


def test_an_empty_string_from_the_backend_is_normalised_to_none():
    # "" and None both mean "the backend did not say"; leaving both shapes in
    # makes every consumer write the same two-way check.
    e = _to_entry(_row(telegram_media_kind="", telegram_chat_id=""))
    assert e.telegram_media_kind is None
    assert e.telegram_chat_id is None
```

> 若 `Entry` 目前不是全部欄位都有預設值，`test_the_new_fields_are_optional...`
> 需要補齊該測試裡的必填參數——**不要為了讓測試過而給既有欄位加預設值**。

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_entry_media_kind.py -q`
Expected: FAIL with `AttributeError: 'Entry' object has no attribute 'telegram_media_kind'`

- [ ] **Step 3: Write minimal implementation**

`Entry` 加兩個帶預設值的欄位（放在最後，避免打亂既有的位置引數）：

```python
    telegram_media_kind: Optional[str] = None
    telegram_chat_id: Optional[str] = None
```

`_to_entry()` 加兩行：

```python
        telegram_media_kind=(row.get("telegram_media_kind") or None),
        telegram_chat_id=(row.get("telegram_chat_id") or None),
```

`or None` 同時處理缺鍵、`None` 與空字串三種形狀。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_entry_media_kind.py tests/test_routed_metadata.py tests/test_dir_cache.py -q`
Expected: PASS（後兩支涵蓋 `Entry` 的序列化與 listing 快取，必須仍然全過）

- [ ] **Step 5: Check the baseline did not grow**

```bash
.venv/Scripts/python.exe -m pytest tests -q 2>&1 | grep "^FAILED" | sed 's/ - .*//;s/^FAILED //' | sort > /tmp/now.txt
comm -13 tests/known_failures.txt /tmp/now.txt
```

Expected: 沒有輸出

- [ ] **Step 6: Commit**

```bash
git add _tdapi_legacy.py tests/test_entry_media_kind.py
git commit -m "feat: keep telegram media kind and chat id on Entry"
```

---

## 這份計畫不包含什麼

覆蓋 spec 的 §3、§5、§6、§7 所需的 `Entry` 欄位。**不**覆蓋：
§4（validity 模型）、§8（三個類別與壓力情境）、§9（roundtrip）、
§10（severity）、§11（preflight）、§12（報告）、§14 的 `tests/test_liveprobe.py`。

那些是 `scripts/_liveprobe.py` 與兩支腳本的工作，寫成第二份計畫，
**因為它們消費的介面要等這份落地才算定案**——counter 的 snapshot 形狀、
cache-state 的回答、`isolate --jsonl` 的實際輸出，寫在紙上跟跑出來不一定一樣。

## 完成後

跑一次完整套件並與 baseline 比對：

```bash
.venv/Scripts/python.exe -m pytest tests -q 2>&1 | grep "^FAILED" | sed 's/ - .*//;s/^FAILED //' | sort > /tmp/now.txt
comm -13 tests/known_failures.txt /tmp/now.txt   # 應該沒有輸出
```

然後 `restart.bat`（改了 Python，且 rclone 與 `H:` 不動），並確認：

```powershell
curl 127.0.0.1:8081/rpc/health
curl 127.0.0.1:8081/rpc/counters
curl "127.0.0.1:8081/rpc/cache-state?path=H:\game&kinds=zip"
```

三個都要回 200 且內容合理。這一步完成後，`scripts/_liveprobe.py` 與兩支腳本的實作計畫才有可以依賴的介面。
