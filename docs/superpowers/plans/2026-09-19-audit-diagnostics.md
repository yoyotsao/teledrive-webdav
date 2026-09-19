# Audit Diagnostics Implementation Plan (rev 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 bridge、`isolate.exe` 與縮圖 DLL 能誠實回答「這次量測到底有沒有真的發生、是誰造成的」，作為 live browse audit 的前置。

**Architecture:** 只新增唯讀 diagnostics，不改任何資料路徑或產品行為。三件事：
(1) **origin 從 request 來源一路標到 wire**，不是只標最底層；
(2) bridge 回答 cache 狀態（記憶體 ＋ 磁碟，一代 physical location 算完）與 sweep 狀態；
(3) shell 側的逐檔 telemetry 與可在 process 存活期間改變的 DLL 記錄開關。

**Tech Stack:** Python 3 / pytest、cheroot WSGI、Telethon、C++（MSVC，`shellthumb/*.bat`）

**Spec:** `docs/superpowers/specs/2026-09-19-live-browse-audit-design.md`（rev 3.5）

## Global Constraints

- **實作 base：** `feat/live-browse-audit`，cut from `feat/current-backend-storage-parity` @ `39ad472`。不要把這些 commit 放回 parity 線上。
- **驗收不是「套件綠」。** base 有 77 個既存失敗。每個任務結束跑 `python scripts/baseline_check.py`（Task 0 建立），它比對的是 **nodeid ＋ 正規化過的失敗簽章**，不是只比 nodeid。
- **不改變任何資料路徑或產品行為。** 只加唯讀 diagnostics（spec §3.0）。
- **不得洩漏憑證。** 新端點與新 counter 一律不含 session string 或 JWT。
- **`origin` 的預設值一律 `"unknown"`，沒有例外。** 包含 `ZipView.open`、
  `Resolver.open_remote` / `thumbs_for` / `props_for` / `heads_for`、
  `SeekableRemoteFile`、`read_part`。真正的 `dav_read` / `thumb` / `thumb_prefetch`
  / `head` / `fetch_local` **一律由 caller 明寫**。給一個「合理的」預設，
  等於讓漏標的 call site 躲進一個合法的桶裡拿到假 PASS；預設 `unknown` 則會讓
  那個窗判 `NOT_MEASURED`。
- **這個 repo 有三層 import-time rebind，改任何函式前先 grep 過有沒有人蓋掉它：**

  | 蓋的人 | 被蓋的 |
  |---|---|
  | `tgio.py` | `_legacy.read_part`、`_legacy.make_preview` |
  | `bridge.py` | `Resolver.open_remote` / `thumbs_for` / `props_for` / `heads_for` / `_cache_key` / `_thumb_path` / `needs_warming` |
  | **`strict_routing.py`** | **`tgio.read_part` 與 `tgio._legacy.read_part`（兩個都蓋）** |

  **`strict_routing.read_part` 才是 live 讀取真正的 seam。** 改 `tgio.read_part`
  而不改它，等於什麼都沒改；而 signature 測試會對著一個沒人呼叫的函式通過。
  所有 signature 檢查一律對**最終被綁定的那個物件**（`bridge.Resolver`、
  `tgio.read_part` 在 import `strict_routing` 之後的值）。
- **絕不 `taskkill /f /im dllhost.exe`。** 只殺載入了 `TeleDriveThumb.dll` 的 COM surrogate PID（spec §8.4、§15）。
  **而且不要假設一定有一個。** `install_thumb.py` 設了 `DisableProcessIsolation=1`，
  handler 通常就載入在 `explorer.exe` 裡——killer 找不到目標是正常結果，
  不是失敗；任何「需要冷 host」的驗證都不能建立在「殺得掉某個 dllhost」上。
- **薄層架構事實：** `RpcApp` / `build_app()` / `main()` 在 `_bridge_legacy.py`；`Resolver` 的 physical cache identity（`_fresh_parts` / `_cache_key` / `_thumb_path`）在薄層 `bridge.py`；`Entry` / `_to_entry()` / `JsonStore` / `ShardedJsonStore` 在 `_tdapi_legacy.py`；wire I/O 在 `_tgio_legacy.py` 的 `_thumbnail_bytes()` / `_chunk()`。
- **測試指令：** `.venv\Scripts\python.exe -m pytest tests -q`

---

### Task 0: Baseline comparator

**Files:**
- Create: `scripts/baseline_check.py`
- Modify: `tests/known_failures.txt`（改成帶簽章的格式）
- Test: `tests/test_baseline_check.py`

**Interfaces:**
- Consumes: 無
- Produces: `scripts/baseline_check.py`（CLI，exit 0 = 沒有退步）、`baseline_check.normalise(text: str) -> str`、`baseline_check.parse_report(text: str) -> dict[str, str]`

現在的 gate 只比 nodeid，答不出**「本來就紅的測試有沒有被我改成另一種錯」**。
而這些任務正好要碰 `_bridge_legacy.py`、`tgio`、zip、warmup——那 77 紅大量涵蓋的
區域，所以這個洞不是理論上的。

簽章必須先正規化：記憶體位址、暫存路徑、行號每次都不一樣，不洗掉的話每一次
比對都會說「全部都變了」，於是沒人再看它。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_baseline_check.py
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.baseline_check import normalise, parse_report, compare


def test_addresses_and_temp_paths_are_normalised_away():
    a = normalise("RecursionError at <object at 0x000002514ED46320>")
    b = normalise("RecursionError at <object at 0x00007FFABCDEF012>")
    assert a == b


def test_line_numbers_are_normalised_away():
    a = normalise("tgio.py:218: in read_part")
    b = normalise("tgio.py:241: in read_part")
    assert a == b


def test_pytest_tmp_dirs_are_normalised_away():
    a = normalise(r"C:\Users\x\AppData\Local\Temp\pytest-of-x\pytest-91\t0\f.json")
    b = normalise(r"C:\Users\x\AppData\Local\Temp\pytest-of-x\pytest-7\t0\f.json")
    assert a == b


def test_the_error_type_survives_normalisation():
    # The whole point is to notice a failure changing kind.
    assert "RecursionError" in normalise("E   RecursionError: maximum recursion depth")
    assert "KeyError" in normalise("E   KeyError: 'origin'")


def test_parse_report_pairs_each_nodeid_with_its_signature():
    report = (
        "FAILED tests/test_a.py::test_one - KeyError: 'origin'\n"
        "FAILED tests/test_b.py::test_two - RecursionError: maximum recursion depth\n"
        "2 failed, 1 passed\n"
    )
    parsed = parse_report(report)
    assert set(parsed) == {"tests/test_a.py::test_one", "tests/test_b.py::test_two"}
    assert "KeyError" in parsed["tests/test_a.py::test_one"]


def test_a_brand_new_failure_is_a_regression():
    base = {"tests/test_a.py::test_one": "KeyError"}
    now = {"tests/test_a.py::test_one": "KeyError",
           "tests/test_b.py::test_two": "AssertionError"}
    added, changed, fixed = compare(base, now)
    assert added == ["tests/test_b.py::test_two"]
    assert changed == []


def test_a_known_failure_that_changes_kind_is_also_a_regression():
    # The hole the nodeid-only gate had: same test, different bug, still red.
    base = {"tests/test_a.py::test_one": "KeyError"}
    now = {"tests/test_a.py::test_one": "RecursionError"}
    added, changed, fixed = compare(base, now)
    assert added == []
    assert changed == ["tests/test_a.py::test_one"]


def test_an_aborted_run_is_never_mistaken_for_progress(monkeypatch):
    """A collection error emits ERROR lines and no FAILED lines. Parsed
    naively that is 'all 77 known failures fixed', which reads as a huge
    improvement and silently disarms the gate."""
    import scripts.baseline_check as bc

    class _Proc:
        returncode = 2
        stdout = "ERROR tests/test_x.py - ImportError: no module named diagnostics\n"
        stderr = ""

    monkeypatch.setattr(bc.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(bc.RunAborted):
        bc.run_pytest()


def test_a_normal_failing_run_is_not_treated_as_aborted(monkeypatch):
    import scripts.baseline_check as bc

    class _Proc:
        returncode = 1
        stdout = "FAILED tests/test_a.py::test_one - KeyError: 'origin'\n1 failed\n"
        stderr = ""

    monkeypatch.setattr(bc.subprocess, "run", lambda *a, **k: _Proc())
    assert "FAILED" in bc.run_pytest()


def test_a_real_path_difference_survives_normalisation():
    # Only temp roots are washed out. A signature naming a different file is
    # a regression worth seeing.
    a = normalise(r"no such file: D:\python\teledrive-webdav\meta\zips\a.json")
    b = normalise(r"no such file: D:\python\teledrive-webdav\meta\zips\b.json")
    assert a != b


def test_a_failure_that_went_green_is_reported_but_is_not_a_regression():
    base = {"tests/test_a.py::test_one": "KeyError"}
    added, changed, fixed = compare(base, {})
    assert fixed == ["tests/test_a.py::test_one"]
    assert added == [] and changed == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_baseline_check.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.baseline_check'`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/baseline_check.py
"""Did this change add a failure, or change what an existing one fails with?

The branch this work sits on is 77 tests red, so "is the suite green" cannot
be the gate. Comparing node ids alone is not enough either: these tasks touch
_bridge_legacy.py, tgio, zip and warmup, which is where most of those 77 live,
so a test can stay red for a brand new reason and a node-id gate would call
that unchanged.

Signatures are normalised first. Addresses, temp dirs and line numbers differ
on every run; left in, every comparison reports everything as changed and the
gate stops being read.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests" / "known_failures.txt"

_NOISE = (
    (re.compile(r"0x[0-9a-fA-F]{4,}"), "0xADDR"),
    (re.compile(r"pytest-\d+"), "pytest-N"),
    (re.compile(r"(?<=\.py):\d+"), ":LINE"),
    # Only the volatile temp roots, not every Windows path: a signature that
    # names the wrong file is a real regression worth noticing, and blanketing
    # C:\ would wash that away too.
    (re.compile(r"[A-Za-z]:\\[^\s'\"]*[Tt]emp\\[^\s'\"]+"), "TMPPATH"),
    (re.compile(r"/tmp/[^\s'\"]+"), "TMPPATH"),
    (re.compile(r"\s+"), " "),
)


def normalise(text: str) -> str:
    out = text.strip()
    for pattern, repl in _NOISE:
        out = pattern.sub(repl, out)
    return out.strip()


def parse_report(text: str) -> dict:
    """nodeid -> normalised signature, from `pytest -q` output."""
    found = {}
    for line in text.splitlines():
        if not line.startswith("FAILED "):
            continue
        body = line[len("FAILED "):]
        nodeid, _, signature = body.partition(" - ")
        found[nodeid.strip()] = normalise(signature)
    return found


def compare(base: dict, now: dict):
    added = sorted(k for k in now if k not in base)
    changed = sorted(k for k in now if k in base and now[k] != base[k])
    fixed = sorted(k for k in base if k not in now)
    return added, changed, fixed


def load(path: Path) -> dict:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        nodeid, _, signature = line.partition("\t")
        out[nodeid.strip()] = signature.strip()
    return out


class RunAborted(RuntimeError):
    """pytest did not finish a normal run, so its output cannot be compared."""


def run_pytest() -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    text = (proc.stdout or "") + (proc.stderr or "")
    # 0 = all passed, 1 = tests failed. Everything else means the run did not
    # happen the way we think: 2 interrupted, 3 internal error, 4 usage error,
    # 5 nothing collected. A collection error is the dangerous one -- it emits
    # ERROR lines and no FAILED lines, so a naive parse sees zero failures and
    # reports all 77 known ones as "fixed", which reads as a large improvement.
    if proc.returncode not in (0, 1):
        raise RunAborted(f"pytest exited {proc.returncode}\n{text[-4000:]}")
    if re.search(r"^ERROR ", text, re.MULTILINE) or "error during collection" in text:
        raise RunAborted("pytest reported collection errors\n" + text[-4000:])
    return text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="overwrite the baseline with the current run")
    args = ap.parse_args(argv)

    try:
        now = parse_report(run_pytest())
    except RunAborted as exc:
        # Never rewrite the baseline from a run that did not complete: that
        # would bake "no failures" in and disarm the gate permanently.
        print(f"[ABORTED] {exc}")
        return 2

    if args.write:
        header = BASELINE.read_text(encoding="utf-8").splitlines()
        header = [l for l in header if l.lstrip().startswith("#") or not l.strip()]
        body = "".join(f"{k}\t{v}\n" for k, v in sorted(now.items()))
        BASELINE.write_text("\n".join(header).rstrip() + "\n" + body, encoding="utf-8")
        print(f"baseline rewritten: {len(now)} known failures")
        return 0

    added, changed, fixed = compare(load(BASELINE), now)
    for nodeid in added:
        print(f"[NEW]     {nodeid}\n            {now[nodeid]}")
    for nodeid in changed:
        print(f"[CHANGED] {nodeid}\n            {now[nodeid]}")
    for nodeid in fixed:
        print(f"[fixed]   {nodeid}")
    print(f"{len(added)} new, {len(changed)} changed, {len(fixed)} fixed "
          f"({len(now)} failing now, {len(load(BASELINE))} in baseline)")
    return 1 if (added or changed) else 0


if __name__ == "__main__":
    sys.exit(main())
```

建 `scripts/__init__.py`（空檔）讓測試 import 得到。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_baseline_check.py -q`
Expected: PASS (11 passed)

- [ ] **Step 5: Rewrite the baseline in the new format**

Run: `.venv\Scripts\python.exe scripts\baseline_check.py --write`
Expected: `baseline rewritten: 77 known failures`，且 `tests/known_failures.txt`
每行是 `<nodeid>\t<簽章>`，檔頭註解保留

- [ ] **Step 6: Verify the gate is quiet on an unchanged tree**

Run: `.venv\Scripts\python.exe scripts\baseline_check.py`
Expected: `0 new, 0 changed, 0 fixed`，exit code 0

- [ ] **Step 7: Commit**

```bash
git add scripts/__init__.py scripts/baseline_check.py tests/known_failures.txt tests/test_baseline_check.py
git commit -m "test: gate on failure signatures, not just which tests are red"
```

---

### Task 1: Counters — request 與 bytes 分開，attempt 計時

**Files:**
- Create: `diagnostics.py`
- Test: `tests/test_diagnostics.py`

**Interfaces:**
- Consumes: 無
- Produces: `diagnostics.ORIGINS: tuple[str, ...]`（含 `"unknown"`）、`diagnostics.NAMED: tuple[str, ...]`、`diagnostics.Counters`（`record_request(origin) -> None`、`record_bytes(origin, n) -> None`、`bump(name, n=1) -> None`、`snapshot() -> dict`、`reset() -> None`）、單例 `diagnostics.COUNTERS`

**不可以「成功回傳之後才記一筆」。** retry、部分下載、失敗的 attempt 全都不算的話，
§8.4 的 idle 靜止判定會假通過，`--sustain-max-bytes` 會低估真正燒掉的額度。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_diagnostics.py
import threading

import pytest

from diagnostics import NAMED, ORIGINS, Counters


def test_unknown_is_a_real_origin_so_untagged_traffic_is_visible():
    # Not a fallback into dav_read: an untagged call site hiding inside the
    # biggest bucket is the one mislabelling nobody would ever notice.
    assert "unknown" in ORIGINS


def test_requests_and_bytes_are_recorded_separately():
    c = Counters()
    c.record_request("props")          # attempt started
    c.record_bytes("props", 512)       # first chunk
    c.record_bytes("props", 512)       # second chunk
    snap = c.snapshot()
    assert snap["download_requests_total"]["props"] == 1
    assert snap["download_bytes_total"]["props"] == 1024


def test_a_failed_attempt_still_counts_as_a_request_with_no_bytes():
    # This is the whole reason for the split: an idle window that saw a failed
    # retry must not read as "nothing happened".
    c = Counters()
    c.record_request("dav_read")
    snap = c.snapshot()
    assert snap["download_requests_total"]["dav_read"] == 1
    assert snap["download_bytes_total"]["dav_read"] == 0


def test_a_retry_counts_two_attempts_not_one_logical_read():
    c = Counters()
    for _ in range(2):
        c.record_request("thumb")
    c.record_bytes("thumb", 20_000)
    assert c.snapshot()["download_requests_total"]["thumb"] == 2


def test_every_origin_and_named_counter_reads_as_zero_not_missing():
    snap = Counters().snapshot()
    for origin in ORIGINS:
        assert snap["download_requests_total"][origin] == 0
        assert snap["download_bytes_total"][origin] == 0
    for name in NAMED:
        assert snap[name] == 0


def test_an_unknown_origin_string_is_rejected():
    with pytest.raises(ValueError):
        Counters().record_request("propz")
    with pytest.raises(ValueError):
        Counters().record_bytes("propz", 1)


def test_concurrent_records_do_not_lose_counts():
    c = Counters()

    def work():
        for _ in range(500):
            c.record_request("dav_read")
            c.record_bytes("dav_read", 1)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap = c.snapshot()
    assert snap["download_requests_total"]["dav_read"] == 4000
    assert snap["download_bytes_total"]["dav_read"] == 4000


def test_snapshot_is_a_copy():
    c = Counters()
    before = c.snapshot()
    c.record_bytes("thumb", 99)
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
worker loop, so no thread-local or contextvar survives the hop and still says
who asked. "unknown" is the default everywhere: a call site nobody tagged has
to be visible, not hidden inside dav_read.
"""

from __future__ import annotations

import threading

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

    def record_request(self, origin: str) -> None:
        """One wire attempt began.

        Counted at the start, so a retry counts twice and a failed attempt
        counts at all. That makes this "attempts", not "logical reads" -- the
        right reading for "how much did this cost", the wrong one for "how
        many times did the caller ask".
        """
        self._add(self._requests, origin, 1)

    def record_bytes(self, origin: str, n: int) -> None:
        """Bytes actually delivered, recorded per chunk as they arrive."""
        self._add(self._bytes, origin, int(n))

    def _add(self, table: dict, origin: str, n: int) -> None:
        if origin not in table:
            raise ValueError(f"unknown download origin {origin!r}")
        with self._lock:
            table[origin] += n

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
Expected: PASS (8 passed)

- [ ] **Step 5: Baseline gate**

Run: `.venv\Scripts\python.exe scripts\baseline_check.py`
Expected: `0 new, 0 changed`

- [ ] **Step 6: Commit**

```bash
git add diagnostics.py tests/test_diagnostics.py
git commit -m "feat: add origin-tagged wire counters with attempt-level requests"
```

---

### Task 2: 記帳點 — `_chunk` 與 `_thumbnail_bytes`

**Files:**
- Modify: `_tgio_legacy.py` — `TelegramWorker._chunk()`、`TelegramWorker._thumbnail_bytes()`
- Test: `tests/test_diagnostics_wiring.py`

**Interfaces:**
- Consumes: `diagnostics.COUNTERS` (Task 1)
- Produces: `_chunk(self, client, doc, offset, *, origin="unknown")`、`_thumbnail_bytes(self, doc, *, origin="unknown")`

這是整個 repo 僅有的兩個 `iter_download` 呼叫點。記在更上層會漏掉 retry；
記在更下層沒有地方可記。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_diagnostics_wiring.py
import inspect

import _tgio_legacy


def _has_kwonly(func, name, default):
    param = inspect.signature(func).parameters[name]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, f"{func} takes {name} positionally"
    assert param.default == default
    return True


def test_the_two_wire_io_helpers_take_an_origin_defaulting_to_unknown():
    _has_kwonly(_tgio_legacy.TelegramWorker._chunk, "origin", "unknown")
    _has_kwonly(_tgio_legacy.TelegramWorker._thumbnail_bytes, "origin", "unknown")
```

```python
# append to tests/test_diagnostics_wiring.py
import asyncio

import pytest

from diagnostics import COUNTERS


class _Chunks:
    """Stands in for iter_download."""

    def __init__(self, *chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk
        return gen()

    async def close(self):
        pass


async def _noop_async(*a, **kw):
    return None


class _NullSemaphore:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Thumb:
    """A PhotoSize-shaped object _best_thumb() can rank and pick."""

    def __init__(self, type_, size):
        self.type = type_
        self.size = size


def _doc_with_thumb(size):
    """A Document carrying the fields _best_thumb() and _thumbnail_bytes() read.

    A bare fake makes _best_thumb() return None, _thumbnail_bytes() returns
    before issuing anything, and the counter assertions then hold trivially --
    the test passes having measured nothing. The `assert out` in the test is
    what catches it if these field names drift; keep that assertion.
    """
    return type("Doc", (), {
        "id": 555,
        "access_hash": 777,
        "file_reference": b"ref",
        "dc_id": 1,
        "size": size,
        "mime_type": "image/jpeg",
        "attributes": [],
        "thumbs": [_Thumb("m", 8_000), _Thumb("s", 900)],
    })()


def _thumbnail_worker(chunks, monkeypatch):
    """A TelegramWorker whose pooled connection yields `chunks`.

    _thumbnail_bytes issues on a pooled connection rather than the control
    one, so a worker with no pool raises before the counter is ever touched.
    """
    worker = _tgio_legacy.TelegramWorker.__new__(_tgio_legacy.TelegramWorker)

    class _Client:
        def iter_download(self, *a, **kw):
            return chunks

    client = _Client()
    worker._pool = [client]
    worker._rr = 0
    monkeypatch.setattr(worker, "_next_client", lambda *a, **k: client, raising=False)
    monkeypatch.setattr(worker, "_pin_exported_sender", _noop_async, raising=False)
    monkeypatch.setattr(worker, "_thumb_semaphore", _NullSemaphore(), raising=False)
    return worker


def test_chunk_counts_one_attempt_and_the_chunk_it_returns(monkeypatch):
    # ONE chunk, because that is the product contract: _chunk is "one
    # REQUEST_SIZE read" and returns on the first yield. A fixture yielding
    # two and asserting the sum would push the implementer into draining the
    # iterator -- i.e. into breaking the read path to satisfy a test.
    before = COUNTERS.snapshot()
    worker = _tgio_legacy.TelegramWorker.__new__(_tgio_legacy.TelegramWorker)

    class _Client:
        def iter_download(self, *a, **kw):
            return _Chunks(b"a" * 1024)

    doc = type("Doc", (), {"dc_id": 1, "size": 1024})()
    asyncio.run(worker._chunk(_Client(), doc, 0, origin="props"))

    after = COUNTERS.snapshot()
    assert after["download_requests_total"]["props"] - before["download_requests_total"]["props"] == 1
    assert after["download_bytes_total"]["props"] - before["download_bytes_total"]["props"] == 1024


def test_thumbnail_bytes_counts_the_whole_preview_it_drains(monkeypatch):
    # The other wire I/O point, and unlike _chunk it really does iterate to
    # the end -- so this is where multi-chunk accumulation belongs. Without
    # this test the "every Python diagnostic has an offline test" invariant
    # is false at one of the only two places that matter.
    #
    # The fake document MUST carry a thumbnail _best_thumb() will pick, and the
    # worker MUST have a pool the download can be issued on. A bare fake doc
    # makes _best_thumb() return None, the function returns before any wire
    # call, and the test passes while measuring nothing -- see
    # tests/test_thumbnails.py and tests/test_photo_media.py for the shapes
    # this repo already uses for both document and photo media.
    before = COUNTERS.snapshot()
    worker = _thumbnail_worker(_Chunks(b"j" * 8000, b"k" * 2000), monkeypatch)
    doc = _doc_with_thumb(10_000)

    out = asyncio.run(worker._thumbnail_bytes(doc, origin="thumb_prefetch"))

    assert out, "the fake never reached iter_download; the counts below are vacuous"
    after = COUNTERS.snapshot()
    assert after["download_requests_total"]["thumb_prefetch"] - before["download_requests_total"]["thumb_prefetch"] == 1
    assert after["download_bytes_total"]["thumb_prefetch"] - before["download_bytes_total"]["thumb_prefetch"] == 10_000


def test_a_failing_attempt_still_records_the_request(monkeypatch):
    before = COUNTERS.snapshot()["download_requests_total"]["head"]
    worker = _tgio_legacy.TelegramWorker.__new__(_tgio_legacy.TelegramWorker)

    class _Boom:
        def iter_download(self, *a, **kw):
            raise RuntimeError("connection closed")

    doc = type("Doc", (), {"dc_id": 1, "size": 1})()
    with pytest.raises(RuntimeError):
        asyncio.run(worker._chunk(_Boom(), doc, 0, origin="head"))

    after = COUNTERS.snapshot()["download_requests_total"]["head"]
    assert after - before == 1
```

> `_chunk` / `_thumbnail_bytes` 的真實內部（FLOOD_WAIT 重試、`_close_download`、
> DC 處理）比這兩個假物件複雜。**調整假物件去符合真實簽章，不要改產品程式碼
> 去遷就測試。** 這兩個測試要證明的只有：attempt 一開始就記 request，
> 每個 chunk 到手就記 bytes。

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_diagnostics_wiring.py -q`
Expected: FAIL with `KeyError: 'origin'`

- [ ] **Step 3: Write minimal implementation**

`_tgio_legacy.py` 頂端 `import diagnostics`。

```python
    async def _chunk(self, client, doc, offset: int, *, origin: str = "unknown") -> bytes:
        # Recorded at the attempt, before anything can fail: a retried or
        # aborted read still cost Telegram quota, and an idle window that saw
        # one must not read as "nothing happened".
        diagnostics.COUNTERS.record_request(origin)
        ...
        async for chunk in pull:
            diagnostics.COUNTERS.record_bytes(origin, len(chunk))
            ...
```

**`_chunk` 有一個 `while True` 的 FLOOD_WAIT 重試迴圈（最多 3 次 attempt），
而且它拿到第一個 chunk 就 `return bytes(chunk)`。** 所以：

- `record_request(origin)` 放在**迴圈裡、`iter_download(...)` 那一行旁邊**，
  不是函式開頭——每一次 attempt 都要算，這正是 attempt 計數的意義。
- `record_bytes(origin, len(chunk))` 放在 `return bytes(chunk)` **之前**，
  只會執行一次。**不要為了「累加所有 chunk」把那個 `return` 改成迴圈**——
  「一個 `_chunk` = 一個 REQUEST_SIZE 讀取」是產品 contract。

`_thumbnail_bytes` 相反：它真的會迭代到底，所以那裡才是逐段累加的地方。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_diagnostics_wiring.py tests/test_read_pace.py tests/test_thumbnails.py -q`
Expected: PASS

- [ ] **Step 5: Baseline gate**

Run: `.venv\Scripts\python.exe scripts\baseline_check.py`
Expected: `0 new, 0 changed`

- [ ] **Step 6: Commit**

```bash
git add _tgio_legacy.py tests/test_diagnostics_wiring.py
git commit -m "feat: record every telegram wire attempt against an origin"
```

---

### Task 3: Origin provenance chain

**Files:**
- Modify: **`strict_routing.py` — `read_part()`（live seam，最重要的一個）**
- Modify: `_tgio_legacy.py` — `read_part()`、`TelegramWorker.read()`、`_read()`、`thumbnails()`、`SeekableRemoteFile.__init__()` 與 `_fetch()`
- Modify: `tgio.py` — `read_part()`、`_read_location()`、`_thumbnail_location()`
- Modify: `zipfs.py` — `ZipView.__init__()`、`root`、`open()`
- Modify: **`bridge.py`（薄層，live 的就是這一份）** — `_open_remote()`、`_thumbs_for()`、`_props_for()`、`_heads_for()`
- Modify: `_bridge_legacy.py` — ZipView lambda、`RemoteFileResource.get_content()`、**`ZipFileResource.get_content()`**、**`Resolver.thumb_bytes()`**、`RpcApp._thumb` / `_props`、`prefetch_folder_thumbs()`
- Modify: `fetchlocal.py` — **兩個 `open_remote` lambda ＋ 兩個 `v.open(node)` lambda**
- Modify: `warmup.py` — sweep 的縮圖／屬性／檔頭呼叫
- Modify: `tests/test_zipfs.py` — 四個 `ZipView(...)` 建構點的 callable 改收 origin
- Modify: **既有測試的 fake 簽章**（origin 變成 kw-only 之後會連帶紅，這不是 product regression）：
  `tests/test_split_math.py` 的 `FakeReader`、`tests/test_account_routing.py` 的 fake worker、
  `tests/test_bridge_e2e.py` 的 `FakeWorker`、`tests/test_thumbnails.py` monkeypatch 的
  `_thumbnail_bytes(doc)`。**`test_split_math` 必須維持 37/37。**
- Test: `tests/test_origin_chain.py`

**Interfaces:**
- Consumes: Task 1、Task 2
- Produces（**全部預設 `"unknown"`**）：`Resolver.open_remote(entry, *, origin="unknown")`、
  `Resolver.thumbs_for(entries, *, origin="unknown")`、
  `Resolver.props_for(entries, *, demand=True, origin="unknown")`、
  `Resolver.heads_for(entries, *, before=None, origin="unknown")`、
  `SeekableRemoteFile(..., origin="unknown")`、
  `ZipView(open_stream: Callable[[str], io.RawIOBase], ...)`、
  `ZipView.open(node, *, origin="unknown")`

**這是整個 plan 最容易做一半的任務。** 只改 `tgio` 那一段的話：

```
H: 普通讀       → dav_read   ✓
zip central dir → dav_read   ✗ 應為 zip_index
fetch-local     → dav_read   ✗
warmup head     → dav_read   ✗
資料夾預抓縮圖   → thumb      ✗ 應為 thumb_prefetch
sweep 縮圖      → thumb      ✗ 應為 warmup
```

`ZipView` 特別危險：它只有一個零參數 `_open_stream`，同時服務 central directory
解析（`root`）與 member 讀取（`open()`，兩處）。綁死成 `zip_index` 會讓
**讀 zip 裡的檔案也算成索引讀取**。

**改哪一層更危險，而且有兩層要注意。**

第一層：`bridge.py` 在 import legacy 之後 monkey-patch `Resolver.open_remote` /
`thumbs_for` / `props_for` / `heads_for`。**live 跑的是薄層那一份**，
只改 `_bridge_legacy.Resolver` 的同名方法，`inspect.signature` 會很好看
而實際行為一個位元組都沒變。

第二層更深：**`strict_routing.py` 最後做**

```python
tgio.read_part = read_part
tgio._legacy.read_part = read_part
```

**兩個 surface 都蓋掉**，所以每一次 HEAD / range / 整檔讀取都是
`strict_routing.read_part` 在答。它目前不收 `origin`——**在它沒改之前，
底下 `tgio` 與 `_tgio_legacy` 加的 origin 參數永遠收到預設值**，
而所有測試都會通過。它要跟 `tgio.read_part` 一樣的處理：legacy fallback
與 canonical `worker.read_location()` 兩條都把 origin 傳下去。

`fetchlocal.py` 有**四個** call site，不是兩個：兩個 `resolver.open_remote(...)`
（一般檔案與 split），以及兩個 `v.open(node)`（虛擬 zip 目錄的 member 取回）。
漏掉後兩個，spec §8.3 的「完整虛擬目錄取回」整段會被算成 `dav_read`。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_origin_chain.py
"""Origin has to start where the request starts. Tagging only the wire leaves
zip index parsing, fetch-local and warmup all claiming to be DAV reads."""

import inspect
import io

import pytest

import zipfs


def test_zipview_open_takes_a_per_call_origin():
    # One zero-argument callback cannot distinguish parsing the central
    # directory from reading a member, and those are different origins.
    # Asserted on behaviour, not on the annotation text: the annotation may
    # be a string or an object depending on `from __future__ import
    # annotations`, and a test reading it would pass for the wrong reason.
    param = inspect.signature(zipfs.ZipView.open).parameters["origin"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default == "unknown"


def test_reading_the_root_asks_for_the_zip_index_origin(tmp_path):
    import zipfile

    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("inner/file.txt", b"hello")

    asked = []

    def open_stream(origin):
        asked.append(origin)
        return io.FileIO(archive, "rb")

    view = zipfs.ZipView(open_stream, name="a.zip")
    assert view.root is not None
    assert asked == ["zip_index"]


def test_reading_a_member_does_not_claim_to_be_the_index(tmp_path):
    import zipfile

    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("inner/file.txt", b"hello")

    asked = []

    def open_stream(origin):
        asked.append(origin)
        return io.FileIO(archive, "rb")

    view = zipfs.ZipView(open_stream, name="a.zip")
    node = view.lookup(["inner", "file.txt"])
    asked.clear()
    view.open(node, origin="dav_read").read()
    assert asked and all(o == "dav_read" for o in asked)


def test_an_unlabelled_member_read_is_unknown_not_a_plausible_default():
    # If open() defaulted to dav_read, a caller nobody updated would be
    # indistinguishable from a real DAV read. unknown makes the window
    # NOT_MEASURED instead, which is the whole point of the default rule.
    assert inspect.signature(zipfs.ZipView.open).parameters["origin"].default == "unknown"


def test_a_member_read_can_be_attributed_to_fetch_local(tmp_path):
    import zipfile

    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("f.txt", b"hello")

    asked = []

    def open_stream(origin):
        asked.append(origin)
        return io.FileIO(archive, "rb")

    view = zipfs.ZipView(open_stream, name="a.zip")
    node = view.lookup(["f.txt"])
    asked.clear()
    view.open(node, origin="fetch_local").read()
    assert asked and all(o == "fetch_local" for o in asked)
```

```python
# append to tests/test_origin_chain.py
import _bridge_legacy as legacy
import _tgio_legacy


def test_the_live_resolver_methods_carry_an_origin_defaulting_to_unknown():
    # bridge.py monkey-patches these over the legacy class, so the legacy
    # ones are not what runs. Checking _bridge_legacy here would pass while
    # live traffic stayed untagged.
    import bridge

    for name in ("open_remote", "thumbs_for", "props_for", "heads_for"):
        param = inspect.signature(getattr(bridge.Resolver, name)).parameters["origin"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert param.default == "unknown", name


def test_the_patched_methods_are_the_thin_layer_ones():
    # Guards the whole point above: if a later refactor stops patching, the
    # signature test could start passing against the legacy definition.
    import bridge

    for name in ("open_remote", "thumbs_for", "props_for", "heads_for"):
        assert getattr(bridge.Resolver, name).__module__ == "bridge", name


def test_the_reader_carries_an_origin():
    assert inspect.signature(_tgio_legacy.SeekableRemoteFile.__init__).parameters["origin"].default == "unknown"


def test_the_reader_hands_its_origin_to_read_part(monkeypatch):
    seen = {}

    def fake_read_part(pool, part, offset, length, *, origin="unknown"):
        seen["origin"] = origin
        return b"\0" * length

    monkeypatch.setattr(_tgio_legacy, "read_part", fake_read_part)
    part = _tgio_legacy.RemotePart(1, 1024, 0, "f1")
    fh = _tgio_legacy.SeekableRemoteFile(object(), [part], name="a.bin", origin="fetch_local")
    fh.read(16)
    assert seen["origin"] == "fetch_local"


def test_thumbs_for_hands_its_origin_to_the_worker(monkeypatch, tmp_path):
    # NOT a signature test and NOT exception-swallowing. An earlier draft did
    # `except Exception: pass` then `seen.get("origin", "thumb_prefetch")`,
    # which passes when the worker is never called at all -- i.e. it passes on
    # a completely unwired implementation, which is the one outcome it exists
    # to catch.
    import bridge

    seen = {}

    class _Worker:
        def thumbnails(self, parts, *, origin="unknown"):
            seen["origin"] = origin
            return {(p.message_id, str(p.file_id)): b"jpegbytes" for p in parts}

    entry = _eligible_entry()          # has_thumbnail=True, not split
    resolver = _resolver_with_worker(bridge, tmp_path, _Worker(), monkeypatch)

    bridge.Resolver.thumbs_for(resolver, [entry], origin="thumb_prefetch")

    assert seen == {"origin": "thumb_prefetch"}, "worker.thumbnails was never reached"


def test_read_part_after_strict_routing_carries_origin():
    # tgio.read_part is rebound by strict_routing at import time, so this must
    # be checked on the bound value, not on the definition in tgio.py.
    import strict_routing  # noqa: F401  (imported for its rebinding side effect)
    import tgio

    param = inspect.signature(tgio.read_part).parameters["origin"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default == "unknown"
    assert tgio.read_part.__module__ == "strict_routing", (
        "strict_routing no longer owns read_part; re-check which layer is live")


def test_the_live_read_part_hands_origin_to_the_worker(monkeypatch):
    import strict_routing
    import tgio

    seen = {}

    class _Worker:
        def read_location(self, location, peer, offset, length, *, origin="unknown"):
            seen["origin"] = origin
            return b"\0" * length

    pool = _pool_routing_to(_Worker())
    tgio.read_part(pool, _canonical_part(), 0, 16, origin="fetch_local")
    assert seen == {"origin": "fetch_local"}
```

```python
# append to tests/test_origin_chain.py
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _eligible_entry():
    """An Entry thumbs_for will actually act on.

    has_thumbnail must be true (thumbs_for skips everything else) and it must
    not be a directory. An ineligible entry -- or an empty list -- makes the
    provenance assertion vacuous, which is the exact failure this test exists
    to rule out.
    """
    from _tdapi_legacy import Entry

    return Entry(
        file_id="f1", name="a.jpg", is_dir=False, size=1234,
        mime="image/jpeg", message_id=8, has_thumbnail=True,
        telegram_user_id=42,
    )


class _Runtime:
    def __init__(self, worker):
        self.worker = worker


class _Pool:
    """Routes every read to one worker, whichever account is asked for."""

    def __init__(self, worker):
        self._runtime = _Runtime(worker)

    def for_read(self, account_id):
        return self._runtime

    def read_routes(self, location):
        yield self._runtime, None


def _resolver_with_worker(bridge, tmp_path, worker, monkeypatch):
    """A Resolver that actually reaches `worker`: empty on-disk caches so
    thumbs_for misses, and _fresh_parts stubbed so no backend is needed."""
    from _tdapi_legacy import JsonStore, ShardedJsonStore

    resolver = object.__new__(bridge.Resolver)
    resolver.cfg = type("C", (), {"cache_dir": tmp_path})()
    resolver.pool = _Pool(worker)
    resolver._zips = {}
    resolver._zip_cache = ShardedJsonStore(tmp_path / "zips")
    resolver._prop_cache = JsonStore(tmp_path / "media_props.json")
    (tmp_path / "thumbs").mkdir(exist_ok=True)
    monkeypatch.setattr(resolver, "note_demand", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(
        resolver, "_fresh_parts",
        lambda entry: (_tgio_legacy.RemotePart(entry.message_id, entry.size,
                                               entry.telegram_user_id, entry.file_id),),
        raising=False,
    )
    return resolver


def _pool_routing_to(worker):
    return _Pool(worker)


def _canonical_part():
    """A ResolvedRemotePart, so tgio.read_part takes the canonical branch
    instead of falling through to the legacy one."""
    from transfer_models import FileLocation, ResolvedRemotePart

    location = FileLocation(None, 42, 8, "document", "9001", 1024, None, 1)
    return ResolvedRemotePart(location=location, index=0, size=1024)


> 這四個 fake 若跟真實簽章對不上，修 fake，不要改產品程式碼。上面每個測試都
> 帶一個「worker 真的被呼叫到」的斷言，所以形狀不對會直接紅，不會靜靜通過。


def test_no_provenance_call_site_was_left_untagged():
    """Every caller names its origin. An untagged one silently becomes
    'unknown', and a window that sees unknown traffic is invalid -- which is
    better than it impersonating dav_read, but still a hole worth closing at
    the source."""
    # Named provenance APIs only. A generic ".open(" scan flags Path.open,
    # item.open and zipfile.open -- noise that gets the whole test disabled --
    # while still missing strict_routing.py, which is the one that matters.
    # The behavioural tests above are the real coverage; this is a reminder
    # for call sites nobody thought about.
    offenders = []
    watched = ("open_remote(", "thumbs_for(", "props_for(", "heads_for(",
               "read_part(", "view.open(", "self.view.open(")
    for name in ("_bridge_legacy.py", "fetchlocal.py", "bridge.py", "warmup.py",
                 "strict_routing.py"):
        for i, line in enumerate((ROOT / name).read_text(encoding="utf-8").splitlines(), 1):
            if "def " in line or "origin=" in line or line.lstrip().startswith("#"):
                continue
            if any(call in line for call in watched):
                offenders.append(f"{name}:{i}: {line.strip()}")
    assert not offenders, "untagged provenance call sites:\n" + "\n".join(offenders)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_origin_chain.py -q`
Expected: FAIL — `ZipView.open` 沒有 `origin` 參數

- [ ] **Step 3: Write minimal implementation**

`zipfs.py`：

```python
    def __init__(self, open_stream: Callable[[str], io.RawIOBase], *, name: str,
                 cache=None, cache_key: str = ""):
        # Takes the origin as an argument rather than being bound to one: the
        # same callback serves the central-directory parse and every member
        # read, and those are different kinds of traffic. A bound origin makes
        # reading a file out of an archive look like reading its index.
        self._open_stream = open_stream
```

`root` 那處改 `self._open_stream("zip_index")`；`open()` 改
`def open(self, node, *, origin: str = "unknown")`（**`unknown`，不是
`dav_read`**——真正的 DAV caller 在 `ZipFileResource.get_content()` 自己明寫），
內部兩處（含 `open_member` 閉包）都用 `self._open_stream(origin)`。

`_tgio_legacy.py`：

```python
    def __init__(self, pool, parts, *, name="", head=b"", origin: str = "unknown"):
        ...
        self._origin = origin

    def _fetch(self, start, length):
        ...
            out += read_part(self._pool, part.remote, inner, nbytes, origin=self._origin)
```

`read_part` / `worker.read` / `_read` / `thumbnails` 一路加 keyword-only
`origin`，往下傳到 Task 2 的兩個記帳點。`tgio.py` 的 `read_part` /
`_read_location` / `_thumbnail_location` 同樣處理——**canonical 那一半在薄層，
只改 legacy 會讓它永遠是預設值**。

`bridge.py` 的 `_open_remote(self, entry, *, origin="unknown")` 把 origin 傳給
`SeekableRemoteFile`。

各 call site 標上正確的 origin：

| 位置 | 檔案 | origin |
|---|---|---|
| **`read_part()`（live seam）** | **`strict_routing.py`** | 收 `origin`，兩條分支都往下傳 |
| `RemoteFileResource.get_content()` | `_bridge_legacy.py` | `dav_read` |
| **`ZipFileResource.get_content()`** | `_bridge_legacy.py` | **`dav_read`**（`self.view.open(self.node, origin="dav_read")`；不明寫就會落進 `unknown`） |
| **`Resolver.thumb_bytes()`** | `_bridge_legacy.py` | 收 `origin="unknown"` 並傳給 `thumbs_for` |
| `RpcApp._thumb` → `thumb_bytes(...)` | `_bridge_legacy.py` | `thumb` |
| ZipView 建構的 lambda | `_bridge_legacy.py` | `lambda origin, e=entry: self.open_remote(e, origin=origin)` |
| 兩個 `resolver.open_remote(...)` | `fetchlocal.py` | `fetch_local` |
| **兩個 `v.open(node)`** | `fetchlocal.py` | **`fetch_local`** |
| `_heads_for()` 內部的讀取 | **`bridge.py`** | `head` |
| `RpcApp._props` → `props_for(...)` | `_bridge_legacy.py` | `props` |
| `prefetch_folder_thumbs()` → `thumbs_for(...)` | `_bridge_legacy.py` | `thumb_prefetch` |
| `Warmer` 的縮圖／屬性／檔頭 | `warmup.py` | `warmup` |

`tests/test_zipfs.py` 的四個建構點（`opener` 函式與兩個 `lambda:`）都要改成
收一個 origin 參數，例如 `def opener(origin="unknown"):`、
`lambda origin="unknown": CountingStream(data, stats)`。**這是必要的連帶修改，
不是「測試壞了」**——callback 的契約真的變了。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_origin_chain.py tests/test_zipfs.py tests/test_split_math.py -q`
Expected: PASS，且 `test_split_math` 仍 37/37、`test_zipfs` 在改過建構點之後全過

- [ ] **Step 5: Baseline gate**

Run: `.venv\Scripts\python.exe scripts\baseline_check.py`
Expected: `0 new, 0 changed`

- [ ] **Step 6: Commit**

```bash
git add zipfs.py tgio.py _tgio_legacy.py bridge.py _bridge_legacy.py fetchlocal.py warmup.py tests/test_origin_chain.py tests/test_zipfs.py
git commit -m "feat: carry origin from the request that caused it to the wire"
```

---

### Task 4: Named counter wiring

**Files:**
- Modify: `zipfs.py` — `ZipView.root`
- Modify: `_bridge_legacy.py` — `RpcApp._thumb`、`RpcApp._props`
- Test: `tests/test_named_counters.py`

**Interfaces:**
- Consumes: `diagnostics.COUNTERS` (Task 1)
- Produces: 四個 named counter 真的會動

**沒有這個任務，spec §8.3 最重要的那條檢查會無條件通過。** 「列 `/game` 期間
`zip_open_attempts_total` 增量 == 0」在 counter 永遠是 0 的情況下永遠成立，
於是那個曾經讓掛載卡死 15 分鐘的 bug 回來了也測不出來。

`zip_open_attempts_total` 要記在 **`root` 被請求時，不管答案從哪來**——
包含 memo 命中與快取命中。只記 remote read 的話，索引全暖時 bug 隱形。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_named_counters.py
import io
import zipfile

import zipfs
from diagnostics import COUNTERS


def _view(tmp_path, cache=None, key=""):
    archive = tmp_path / "a.zip"
    if not archive.exists():
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("f.txt", b"hello")
    return zipfs.ZipView(lambda origin: io.FileIO(archive, "rb"),
                         name="a.zip", cache=cache, cache_key=key)


def test_asking_for_the_root_counts_an_open_attempt(tmp_path):
    before = COUNTERS.snapshot()["zip_open_attempts_total"]
    view = _view(tmp_path)
    assert view.root is not None
    assert COUNTERS.snapshot()["zip_open_attempts_total"] - before == 1


def test_a_memoised_root_still_counts_as_an_attempt(tmp_path):
    # The bug this guards is "listing /game asks every archive for its tree".
    # Once the trees are warm the remote reads are zero, so counting reads
    # would let the bug back in invisibly. Count the asking.
    view = _view(tmp_path)
    view.root
    before = COUNTERS.snapshot()["zip_open_attempts_total"]
    view.root
    view.root
    assert COUNTERS.snapshot()["zip_open_attempts_total"] - before == 2


def test_only_a_real_central_directory_parse_counts_as_a_miss(tmp_path):
    before = COUNTERS.snapshot()["zip_index_cache_misses_total"]
    view = _view(tmp_path)
    view.root
    after_first = COUNTERS.snapshot()["zip_index_cache_misses_total"]
    assert after_first - before == 1
    view.root
    assert COUNTERS.snapshot()["zip_index_cache_misses_total"] == after_first


def test_a_cache_hit_is_an_open_attempt_but_not_a_miss(tmp_path):
    # Without this, an implementation that bumps the miss counter before
    # consulting _cache.get() passes every other test in this file: the memo
    # hit is covered, the cold parse is covered, and the cached-tree path --
    # the one that actually distinguishes the two counters -- is not.
    from _tdapi_legacy import ShardedJsonStore

    store = ShardedJsonStore(tmp_path / "zips")
    warm = _view(tmp_path, cache=store, key="file-1")
    warm.root                                   # populates the store
    warm.save(force=True)

    fresh = _view(tmp_path, cache=store, key="file-1")   # new view, cached tree
    opens = COUNTERS.snapshot()["zip_open_attempts_total"]
    misses = COUNTERS.snapshot()["zip_index_cache_misses_total"]
    assert fresh.root is not None
    assert COUNTERS.snapshot()["zip_open_attempts_total"] - opens == 1
    assert COUNTERS.snapshot()["zip_index_cache_misses_total"] - misses == 0
```

```python
# append to tests/test_named_counters.py
import json

import _bridge_legacy as legacy


class _Sink:
    def __init__(self):
        self.status = None

    def __call__(self, status, headers):
        self.status = status
        return lambda _b: None


def test_thumb_and_props_endpoints_count_their_requests(monkeypatch):
    app = legacy.RpcApp.__new__(legacy.RpcApp)
    app.resolver = type("R", (), {
        "dav_path_from_windows": staticmethod(lambda p: None),
    })()
    before = COUNTERS.snapshot()
    for route in ("/thumb", "/props"):
        app({"PATH_INFO": f"/rpc{route}", "QUERY_STRING": "path=H%3A%5Cx.jpg",
             "REQUEST_METHOD": "GET"}, _Sink())
    after = COUNTERS.snapshot()
    assert after["thumb_requests_total"] - before["thumb_requests_total"] == 1
    assert after["props_requests_total"] - before["props_requests_total"] == 1
```

> 這個測試刻意讓 `dav_path_from_windows` 回 `None`（路徑解不出來）：
> **counter 要記在入口，不是記在成功之後**。一個 404 也是一次請求。

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_named_counters.py -q`
Expected: FAIL — 增量是 0

- [ ] **Step 3: Write minimal implementation**

`zipfs.py` 的 `root` property，**第一行**就 bump：

```python
    @property
    def root(self):
        # Counted on every ask, including memo and cache hits. The failure this
        # exists to catch -- listing /game resolving every archive's tree -- has
        # zero remote reads once the indexes are warm, so counting reads would
        # hide exactly the regression it is meant to catch.
        diagnostics.COUNTERS.bump("zip_open_attempts_total")
        if self._root is not None:
            return self._root
        ...
        # only where the central directory is actually parsed:
        diagnostics.COUNTERS.bump("zip_index_cache_misses_total")
        stream = self._open_stream("zip_index")
```

`_bridge_legacy.py` 的 `_thumb` / `_props` 各自第一行
`diagnostics.COUNTERS.bump("thumb_requests_total")` / `"props_requests_total"`。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_named_counters.py tests/test_zipfs.py -q`
Expected: PASS

- [ ] **Step 5: Baseline gate**

Run: `.venv\Scripts\python.exe scripts\baseline_check.py`
Expected: `0 new, 0 changed`

- [ ] **Step 6: Commit**

```bash
git add zipfs.py _bridge_legacy.py tests/test_named_counters.py
git commit -m "feat: count zip open attempts and rpc thumb/props requests"
```

---

### Task 5: `BackgroundWarmup.status()`，exception-safe

**Files:**
- Modify: `warmup.py` — `BackgroundWarmup`
- Test: `tests/test_warmup_status.py`

**Interfaces:**
- Consumes: 無
- Produces: `BackgroundWarmup.status() -> dict`，鍵 `enabled` / `active` / `phase` / `pass` / `next_run_at`

兩個 rev 1 的錯：`_pass()` 有 early return 也可能丟例外（被 `_run()` catch），
只在 happy path 復原 phase 會讓它**永遠停在 active**，於是 audit 之後每一個
需要 sweep idle 的量測都被判 `NOT_MEASURED`——工具靜靜地不再回答任何事。
而 `next_run_at` 在第一次 sweep 之前是 `None`，正好是 `--with-sweep` 最需要它的時候。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_warmup_status.py
import threading
import time

import pytest

from warmup import BackgroundWarmup


class _Resolver:
    def clear_heads(self):
        pass


def _w(**kw):
    kw.setdefault("interval_minutes", 60)
    kw.setdefault("start_delay", 999)
    return BackgroundWarmup(_Resolver(), **kw)


def test_a_warmup_that_never_started_is_idle():
    s = _w().status()
    assert (s["active"], s["phase"], s["pass"]) == (False, "idle", 0)


def test_next_run_at_is_known_from_start_not_only_after_the_first_pass():
    # --with-sweep needs to tell the operator when to come back, and that is
    # most needed before any pass has run.
    w = _w(start_delay=30)
    w.start()
    try:
        assert w.status()["next_run_at"] is not None
    finally:
        w.stop()


def test_active_covers_every_phase_of_a_pass():
    w = _w()
    for phase in ("walking", "filling", "shell_warm"):
        w._enter(phase)
        assert w.status() == {**w.status(), "phase": phase, "active": True}
    w._enter("idle")
    assert w.status()["active"] is False


def test_a_pass_that_raises_still_returns_to_idle(monkeypatch):
    # _run() catches whatever _pass() throws. Leaving phase stuck on "filling"
    # makes every later measurement NOT_MEASURED -- the tool goes quiet instead
    # of going wrong, which is worse because nobody notices.
    w = _w()
    monkeypatch.setattr(w, "_run_pass_body", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        w._pass()
    assert w.status()["active"] is False
    assert w.status()["phase"] == "idle"


def test_a_pass_that_raises_does_not_increment_the_pass_count(monkeypatch):
    w = _w()
    monkeypatch.setattr(w, "_run_pass_body", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        w._pass()
    assert w.status()["pass"] == 0


def test_a_stop_induced_early_return_does_not_count_as_a_completed_pass(monkeypatch):
    # _run_pass_body() returns normally when asked to stop, so "it returned"
    # cannot mean "it finished". It reports completion explicitly.
    w = _w()
    monkeypatch.setattr(w, "_run_pass_body", lambda: False)
    w._pass()
    assert w.status()["pass"] == 0


def test_a_completed_pass_counts(monkeypatch):
    w = _w()
    monkeypatch.setattr(w, "_run_pass_body", lambda: True)
    w._pass()
    assert w.status()["pass"] == 1


def test_stopping_wins_over_the_idle_reset(monkeypatch):
    # stop() sets "stopped"; _pass()'s finally must not immediately overwrite
    # it with "idle" and make a shut-down sweep look merely idle.
    w = _w()
    monkeypatch.setattr(w, "_run_pass_body", lambda: w._stop.set() or False)
    w._pass()
    assert w.status()["phase"] == "stopped"


def test_the_real_pass_moves_through_its_phases(monkeypatch):
    # Calling _enter() directly proves the accessor, not the wiring. This
    # proves _run_pass_body actually marks the stages it runs.
    seen = []

    class _Warmer:
        def __init__(self, *a, **kw):
            pass

        def pending(self):
            seen.append(("pending", w.status()["phase"]))
            return [], []

        def fill(self, todo):
            seen.append(("fill", w.status()["phase"]))
            return 0

        def shell_warm(self, files):
            seen.append(("shell_warm", w.status()["phase"]))
            return 0

    import warmup as warmup_mod

    monkeypatch.setattr(warmup_mod, "Warmer", _Warmer)
    w = _w()
    w._run_pass_body()
    assert ("pending", "walking") in seen
    assert ("fill", "filling") in seen
    assert ("shell_warm", "shell_warm") in seen


def test_a_pass_whose_fill_did_not_finish_is_not_a_completed_pass(monkeypatch):
    # Warmer.fill() catches its own per-batch errors and returns a partial
    # count rather than raising, so "_run_pass_body returned normally" is not
    # evidence the pass finished. Completion is done == len(todo) and no stop.
    class _Warmer:
        def __init__(self, *a, **kw):
            pass

        def pending(self):
            return ["f1", "f2"], ["f1", "f2"]

        def fill(self, todo):
            return 1                 # one of two: a batch failed silently

        def shell_warm(self, files):
            return 0

    import warmup as warmup_mod

    monkeypatch.setattr(warmup_mod, "Warmer", _Warmer)
    w = _w()
    assert w._run_pass_body() is False
    w._pass()
    assert w.status()["pass"] == 0


def test_status_does_not_block_on_the_sweep_thread():
    w = _w()
    w._enter("filling")
    started = time.monotonic()
    w.status()
    assert time.monotonic() - started < 0.05
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_warmup_status.py -q`
Expected: FAIL with `AttributeError: ... has no attribute 'status'`

- [ ] **Step 3: Write minimal implementation**

`__init__` 末尾：

```python
        self._phase = "idle"
        self._passes = 0
        self._next_run_at: Optional[float] = None
        self._phase_lock = threading.Lock()
```

```python
    def _enter(self, phase: str) -> None:
        with self._phase_lock:
            self._phase = phase

    def status(self) -> dict:
        """Read-only, never waits on the sweep thread: the audit calls this
        between measurement windows to decide whether a counter delta can be
        trusted, so blocking here would disturb what it is observing."""
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

`start()` 裡，起執行緒之前先 `self._next_run_at = time.time() + self.start_delay`。

把 `_pass()` 現有的本體整段搬進 `_run_pass_body()`，階段標記寫在裡面
（`_enter("walking")` 包 `pending()`、`"filling"` 包 `fill()`、
`"shell_warm"` 包 `shell_warm()`），**並回報這一輪有沒有真的跑完**：

```python
    def _run_pass_body(self) -> bool:
        """Returns whether the pass ran to completion.

        The existing early returns are normal returns, so "it returned" does
        not mean "it finished" -- the pass count has to be told explicitly.
        """
        self._enter("walking")
        warmer = Warmer(self.resolver, stop=self._stop, progress=self._note)
        files, todo = warmer.pending()
        if self._stop.is_set():
            return False
        self._enter("filling")
        done = warmer.fill(todo) if todo else 0
        if self._stop.is_set():
            return False
        self._enter("shell_warm")
        warmer.shell_warm(files)
        self.resolver.clear_heads()
        # fill() swallows per-batch failures and returns a partial count, so
        # a normal return is not evidence the pass finished.
        return not self._stop.is_set() and done == len(todo)
```

```python
    def _pass(self) -> None:
        # try/finally, not a happy-path reset: _run() swallows whatever this
        # raises, and a phase left on "filling" makes every later measurement
        # NOT_MEASURED -- the tool goes quiet rather than wrong, which is the
        # harder failure to notice.
        completed = False
        try:
            completed = self._run_pass_body()
        finally:
            with self._phase_lock:
                # stop() also writes "stopped"; do not clobber it back to
                # idle, or a shut-down sweep reads as merely resting.
                self._phase = "stopped" if self._stop.is_set() else "idle"
                self._next_run_at = time.time() + self.interval
        if completed:
            with self._phase_lock:
                self._passes += 1
```

`stop()` 裡加 `self._enter("stopped")`。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_warmup_status.py -q`
Expected: PASS (11 passed)

- [ ] **Step 5: Baseline gate**

Run: `.venv\Scripts\python.exe scripts\baseline_check.py`
Expected: `0 new, 0 changed`

- [ ] **Step 6: Commit**

```bash
git add warmup.py tests/test_warmup_status.py
git commit -m "feat: report sweep phase, and never leave it stuck on a raise"
```

---

### Task 6: `/rpc/health` 的 `cryptg`、`/rpc/status` 的 `warmup`、`/rpc/counters`

**Files:**
- Modify: `_bridge_legacy.py` — `RpcApp.__init__`、`_health`、`_status`、`__call__` 路由、新增 `_counters`、`build_app()`、`main()`
- Test: `tests/test_rpc_diagnostics.py`

**Interfaces:**
- Consumes: `BackgroundWarmup.status()` (Task 5)、`diagnostics.COUNTERS` (Task 1)
- Produces: `RpcApp(cfg, resolver, fetcher, stager, upload_stager=None, warmup=None)`；`/rpc/health` 多 `"cryptg": bool`；`/rpc/status` 多 `"warmup": {...}`；新端點 `GET /rpc/counters`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rpc_diagnostics.py
import json

import _bridge_legacy as legacy
from diagnostics import COUNTERS, NAMED, ORIGINS


class _Sink:
    def __init__(self):
        self.status = None

    def __call__(self, status, headers):
        self.status = status
        return lambda _b: None


def _body(app, route):
    sink = _Sink()
    chunks = app({"PATH_INFO": f"/rpc{route}", "REQUEST_METHOD": "GET"}, sink)
    assert sink.status.startswith("200"), sink.status
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
    assert isinstance(_body(_app(), "/health")["cryptg"], bool)


def test_build_app_actually_hands_the_warmup_to_the_rpc_app(monkeypatch):
    """The tests above poke app.warmup in by hand, so they stay green even if
    main() forgets to pass the warmer to build_app() -- which is the only way
    this wiring can be wrong in production."""
    captured = {}
    real = legacy.RpcApp

    class _Spy(real):
        def __init__(self, *a, **kw):
            captured["warmup"] = kw.get("warmup", a[5] if len(a) > 5 else None)

    monkeypatch.setattr(legacy, "RpcApp", _Spy)
    sentinel = object()
    # build_app only wires objects together -- nothing here is called during
    # construction -- so bare stand-ins keep the seam visible without a rig.
    cfg = type("C", (), {"base_url": "https://example/api/v1", "mount_drive": "H:",
                         "game_folder": "game", "port": 8081})()
    resolver = type("R", (), {"cfg": cfg})()
    legacy.build_app(cfg, resolver, object(), None, None, warmup=sentinel)
    assert captured["warmup"] is sentinel


def test_cryptg_is_false_when_the_native_module_cannot_be_imported(monkeypatch):
    # A present-but-broken wheel is the case find_spec() gets wrong, and it is
    # the one that silently drops every download to ~0.15 MiB/s.
    import builtins

    import _bridge_legacy as legacy_mod

    legacy_mod._cryptg_usable.cache_clear()
    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == "cryptg":
            raise ImportError("DLL load failed")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert legacy_mod._cryptg_usable() is False
    legacy_mod._cryptg_usable.cache_clear()


def test_status_carries_the_warmup_view():
    class _W:
        @staticmethod
        def status():
            return {"enabled": True, "active": True, "phase": "filling",
                    "pass": 2, "next_run_at": None}

    body = _body(_app(_W()), "/status")
    assert body["warmup"]["phase"] == "filling"


def test_status_says_disabled_rather_than_omitting_warmup():
    # A missing key and "not running" must not look the same: this decides
    # whether a counter delta can be trusted.
    assert _body(_app(None), "/status")["warmup"] == {
        "enabled": False, "active": False, "phase": "idle",
        "pass": 0, "next_run_at": None}


def test_counters_endpoint_exposes_every_origin_and_named_counter():
    body = _body(_app(), "/counters")
    for origin in ORIGINS:
        assert origin in body["download_requests_total"]
        assert origin in body["download_bytes_total"]
    for name in NAMED:
        assert name in body


def test_counters_endpoint_reflects_recorded_traffic():
    before = _body(_app(), "/counters")["download_bytes_total"]["zip_index"]
    COUNTERS.record_bytes("zip_index", 700)
    after = _body(_app(), "/counters")["download_bytes_total"]["zip_index"]
    assert after - before == 700


def test_no_diagnostic_endpoint_leaks_a_credential():
    for route in ("/health", "/status", "/counters"):
        raw = json.dumps(_body(_app(), route)).lower()
        for needle in ("session", "token", "jwt", "auth_key"):
            assert needle not in raw
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_rpc_diagnostics.py -q`
Expected: FAIL with `KeyError: 'cryptg'`

- [ ] **Step 3: Write minimal implementation**

`_bridge_legacy.py` 頂端加 `import diagnostics`，並加：

```python
@functools.lru_cache(maxsize=1)
def _cryptg_usable() -> bool:
    """Can Telethon actually use the C extension?

    find_spec() only proves the package directory is there. A broken wheel or
    a missing VC runtime makes the native module fail at import time, and
    Telethon then falls back to pure-Python AES-IGE, which pins downloads at
    ~0.15 MiB/s -- every latency number the audit produces would be measuring
    that instead. Import it, which is what Telethon does anyway, and cache the
    answer so the endpoint stays cheap.
    """
    try:
        import cryptg  # noqa: F401
    except Exception:
        return False
    return True
```

（`functools` 若尚未 import 就一併加上。）

```python
_WARMUP_OFF = {"enabled": False, "active": False, "phase": "idle",
               "pass": 0, "next_run_at": None}
```

`RpcApp.__init__` 簽章尾端加 `warmup=None`，`self.warmup = warmup`。

`_health` 的 dict 加：

```python
                "cryptg": _cryptg_usable(),
```

`_status` 的 dict 加 `"warmup": self.warmup.status() if self.warmup else dict(_WARMUP_OFF),`。

路由表在 `/props` 之後加 `/counters`，並：

```python
    def _counters(self, start_response):
        """Monotonic wire totals by origin.

        The audit subtracts two snapshots to prove a window downloaded
        nothing. Log lines cannot: ThrottleRepeats suppresses repeats of the
        same telethon template, so a suppressed download and an absent one
        read identically.
        """
        return _text_response(start_response, "200 OK",
                              json.dumps(diagnostics.COUNTERS.snapshot()),
                              "application/json")
```

`build_app()` 多收 `warmup=None` 並傳進 `RpcApp`；`main()` 建好 `BackgroundWarmup`
之後交給 `build_app()`。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_rpc_diagnostics.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Baseline gate**

Run: `.venv\Scripts\python.exe scripts\baseline_check.py`
Expected: `0 new, 0 changed`

- [ ] **Step 6: Commit**

```bash
git add _bridge_legacy.py tests/test_rpc_diagnostics.py
git commit -m "feat: expose cryptg, sweep state and the counters over rpc"
```

---

### Task 7: `GET /rpc/cache-state?path=`

**Files:**
- Modify: `bridge.py`（薄層）— 新增 `Resolver.cache_state`
- Modify: `_bridge_legacy.py` — 路由、新增 `RpcApp._cache_state`
- Modify: `_tdapi_legacy.py` — `JsonStore` 與 `ShardedJsonStore` 各自的 `has_in_memory` / `has_on_disk`
- Test: `tests/test_cache_state.py`

**Interfaces:**
- Consumes: `bridge._fresh_parts`、`bridge._physical_set_key`、`bridge._thumb_path_for`
- Produces: `Resolver.cache_state(entry, kinds: Sequence[str]) -> dict[str, dict[str, bool]]`（`bridge.py`）；`GET /rpc/cache-state?path=<Windows 路徑>&kinds=zip,thumb,props`

四個 rev 1 的錯，全部會讓暖的被判成冷的：

1. `Resolver.resolve()` 收的是 `List[str]`、回的是 `Loc` 不是 `Entry`。
   要走 `dav_path_from_windows(path)`（`_thumb` / `_props` 已經在用）→
   `resolve(segments)` → `loc.entry`；解不出 entry 要明確 4xx。
2. `_cache_key()` 與 `_thumb_path()` **各自**呼叫 `_fresh_parts()` →
   `api.current_parts(entry)`。分開叫等於一個回應打兩次 backend，而且
   **storage migration 剛好發生時會混到兩代 physical generation**。
   所以實作要落在薄層、一次 `_fresh_parts()` 算完。
3. `key in Resolver._zips` **不等於 warm**：列 `/game` 本身就會建 `ZipView`，
   而那個 view 的 `_root` 可能根本沒 parse。判定要用 `view._root is not None`。
4. `JsonStore` 是 `_data` ＋ 單一 `_path`，`ShardedJsonStore` 是 `_memory` ＋
   一鍵一檔。**兩個 store 要分開實作，不能共用一段 helper。**
5. **`disk=true` 對兩個 store 的意義不一樣，端點照實回報即可，但解讀要分開。**
   `ShardedJsonStore.get()` 在 miss 時會去讀磁碟，所以 `disk=true` 代表下一次
   查詢真的會命中；`JsonStore` 只在 constructor 載入一次，之後只查 `_data`，
   所以別的 process 後來寫進 `media_props.json` 的東西，**對現在這個 bridge
   仍然是 miss**。端點回 `{"memory": false, "disk": true}` 是誠實的；
   把它一律當 warm 的是第二份計畫的 classifier，那裡要 store-specific。
   這一條寫進 `_liveprobe` 的 TODO，不在本任務實作。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cache_state.py
import json

import pytest

import bridge as thin
import _bridge_legacy as legacy
from _tdapi_legacy import JsonStore, ShardedJsonStore


def test_jsonstore_reports_memory_and_disk_separately(tmp_path):
    store = JsonStore(tmp_path / "props.json")
    assert store.has_in_memory("k") is False
    assert store.has_on_disk("k") is False
    store.put("k", {"w": 1})
    assert store.has_in_memory("k") is True


def test_shardedstore_disk_check_does_not_warm_memory(tmp_path):
    a = ShardedJsonStore(tmp_path / "zips")
    a.put("k", {"tree": 1})
    b = ShardedJsonStore(tmp_path / "zips")      # fresh process
    assert b.has_on_disk("k") is True
    assert b.has_in_memory("k") is False, "checking disk must not read the value in"


class _View:
    def __init__(self, parsed):
        self._root = {"x": 1} if parsed else None


def _resolver(tmp_path, *, zips=None, zip_store=None, prop_store=None):
    """Every store is real. A half-built ShardedJsonStore via __new__ blows up
    on the first attribute it touches, which would make the call-count test
    below fail before it ever counted anything."""
    r = type("R", (), {})()
    r._fresh_parts = lambda entry: ("parts",)
    r._zips = zips or {}
    r._zip_cache = zip_store if zip_store is not None else ShardedJsonStore(tmp_path / "zips")
    r._prop_cache = prop_store if prop_store is not None else JsonStore(tmp_path / "media_props.json")
    r.cfg = type("C", (), {"cache_dir": tmp_path})()
    r.cache_state = thin.Resolver.cache_state.__get__(r)
    return r


def test_a_zipview_that_exists_but_never_parsed_its_root_is_cold(tmp_path, monkeypatch):
    # Listing /game creates a ZipView for every archive without parsing any of
    # them -- that is the behaviour the audit exists to protect. Treating the
    # view's existence as warmth would report every archive warm right after
    # the listing that proved they were untouched.
    monkeypatch.setattr(thin, "_physical_set_key", lambda parts: "loc3-abc")
    r = _resolver(tmp_path, zips={"loc3-abc": _View(parsed=False)})
    assert r.cache_state(object(), ["zip"])["zip"]["memory"] is False


def test_a_zipview_with_a_parsed_root_is_warm(tmp_path, monkeypatch):
    monkeypatch.setattr(thin, "_physical_set_key", lambda parts: "loc3-abc")
    r = _resolver(tmp_path, zips={"loc3-abc": _View(parsed=True)})
    assert r.cache_state(object(), ["zip"])["zip"]["memory"] is True


def test_the_physical_rows_are_fetched_once_for_the_whole_answer(tmp_path, monkeypatch):
    # _cache_key() and _thumb_path() each call current_parts(). Asking twice
    # costs two backend round trips and, mid-migration, can mix two physical
    # generations into one response.
    calls = []
    monkeypatch.setattr(thin, "_physical_set_key", lambda parts: "loc3-abc")
    r = _resolver(tmp_path)
    r._fresh_parts = lambda entry: calls.append(1) or ("parts",)
    r.cache_state(object(), ["zip", "thumb", "props"])
    assert len(calls) == 1


def test_an_unknown_kind_raises_rather_than_reporting_a_confident_cold(tmp_path):
    r = _resolver(tmp_path)
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


def _call(app, qs):
    sink = _Sink()
    chunks = app({"PATH_INFO": "/rpc/cache-state", "QUERY_STRING": qs,
                  "REQUEST_METHOD": "GET"}, sink)
    return sink.status, b"".join(chunks).decode("utf-8")


def test_the_endpoint_converts_the_windows_path_itself():
    # Plain functions, not `setdefault(...) or [...]`: a Windows path is
    # truthy, so that expression returns the path string instead of the
    # segment list, and a resolve() fake that accepts anything then passes on
    # an implementation that never converted the path at all.
    seen = {}

    def dav_path_from_windows(p):
        seen["win"] = p
        return ["game", "a.zip"]

    def resolve(segments):
        seen["segments"] = segments
        return type("L", (), {"entry": "E"})()

    def cache_state(entry, kinds):
        seen["entry"] = entry
        return {"zip": {"memory": True, "disk": False}}

    app = legacy.RpcApp.__new__(legacy.RpcApp)
    app.resolver = type("R", (), {
        "dav_path_from_windows": staticmethod(dav_path_from_windows),
        "resolve": staticmethod(resolve),
        "cache_state": staticmethod(cache_state),
    })()
    status, body = _call(app, "path=H%3A%5Cgame%5Ca.zip&kinds=zip")
    assert status.startswith("200")
    assert seen["win"] == "H:\\game\\a.zip"
    assert seen["segments"] == ["game", "a.zip"]
    assert seen["entry"] == "E"
    assert json.loads(body)["zip"]["memory"] is True


def test_the_cache_state_response_carries_no_credential():
    app = legacy.RpcApp.__new__(legacy.RpcApp)
    app.resolver = type("R", (), {
        "dav_path_from_windows": staticmethod(lambda p: ["game", "a.zip"]),
        "resolve": staticmethod(lambda segs: type("L", (), {"entry": "E"})()),
        "cache_state": staticmethod(lambda e, kinds: {"zip": {"memory": True, "disk": False}}),
    })()
    _, body = _call(app, "path=H%3A%5Cgame%5Ca.zip&kinds=zip")
    for needle in ("session", "token", "jwt", "auth_key"):
        assert needle not in body.lower()


def test_a_path_with_no_entry_is_a_4xx_not_a_cold_looking_answer():
    # Answering {"memory": false, "disk": false} for an unresolvable path
    # reads as "cold" and licenses a cold threshold that never ran.
    app = legacy.RpcApp.__new__(legacy.RpcApp)
    app.resolver = type("R", (), {
        "dav_path_from_windows": staticmethod(lambda p: None),
    })()
    status, _ = _call(app, "path=Z%3A%5Cnope")
    assert status.startswith("4")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cache_state.py -q`
Expected: FAIL with `AttributeError: has_in_memory`

- [ ] **Step 3: Write minimal implementation**

`_tdapi_legacy.py`，**兩個 store 分開寫**：

```python
class JsonStore:
    def has_in_memory(self, key) -> bool:
        with self._lock:
            return key in self._data

    def has_on_disk(self, key) -> bool:
        """One shared file: on disk means the file holds this key. Read the
        file, not the in-memory dict, and do not merge it in -- warming the
        cache is exactly what the caller is trying to detect."""
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                return key in json.load(fh)
        except (OSError, ValueError):
            return False


class ShardedJsonStore:
    def has_in_memory(self, key) -> bool:
        with self._lock:
            return key in self._memory

    def has_on_disk(self, key) -> bool:
        """One file per key: existence is the whole answer, and checking it
        does not read the value in."""
        return self._path_for(key).exists()
```

> `ShardedJsonStore` 若沒有 `_path_for`，用它 `get`/`put` 內部算檔名的同一段
> 邏輯抽成一個方法——**不要在這裡重新實作一次檔名規則**，兩處算不一樣就會
> 永遠回報 cold。

`bridge.py`（薄層）：

```python
_CACHE_KINDS = ("zip", "thumb", "props")


def _cache_state(self, entry, kinds):
    """Is this entry's cached answer already available, and from where?

    One _fresh_parts() for the whole answer: _cache_key() and _thumb_path()
    each fetch the current physical rows, so asking them separately costs two
    backend round trips and, if a storage migration lands between them, mixes
    two physical generations into one response.

    Disk absence is not coldness. The json stores keep dicts, Resolver keeps
    live ZipViews, and a bridge that has been up for a day answers instantly
    with no file present.
    """
    parts = self._fresh_parts(entry)
    key = _physical_set_key(parts)
    out = {}
    for kind in kinds:
        if kind not in _CACHE_KINDS:
            raise ValueError(f"unknown cache kind {kind!r}")
        if kind == "zip":
            view = self._zips.get(key)
            out[kind] = {
                # A view exists for every archive after a /game listing, and
                # that listing deliberately parses none of them. Only a parsed
                # root counts.
                "memory": (view is not None and view._root is not None)
                          or self._zip_cache.has_in_memory(key),
                "disk": self._zip_cache.has_on_disk(key),
            }
        elif kind == "props":
            out[kind] = {"memory": self._prop_cache.has_in_memory(key),
                         "disk": self._prop_cache.has_on_disk(key)}
        else:
            path = _thumb_path_for(self, parts)
            out[kind] = {"memory": False, "disk": path.exists()}
    return out


Resolver.cache_state = _cache_state
```

`_bridge_legacy.py` 的 import 要補 `parse_qs`——現況只有
`from urllib.parse import unquote, urlsplit`，照寫 `urllib.parse.parse_qs`
會 `NameError`：

```python
from urllib.parse import parse_qs, unquote, urlsplit
```

路由加 `/cache-state`，並：

```python
    def _cache_state(self, environ, start_response):
        params = parse_qs(environ.get("QUERY_STRING", ""))
        raw = (params.get("path") or [""])[0]
        kinds = [k for k in (params.get("kinds") or ["zip,thumb,props"])[0].split(",") if k]
        # The bridge resolves the path. A key computed by the client can
        # already disagree with the authoritative physical location, and a
        # stale key answers "not cached" -- which reads as cold.
        segments = self.resolver.dav_path_from_windows(raw)
        entry = None
        if segments is not None:
            entry = getattr(self.resolver.resolve(segments), "entry", None)
        if entry is None:
            return _text_response(start_response, "404 Not Found",
                                  f"no entry for {raw}\n")
        body = json.dumps(self.resolver.cache_state(entry, kinds))
        return _text_response(start_response, "200 OK", body, "application/json")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cache_state.py tests/test_sizes.py -q`
Expected: PASS（`test_sizes.py` 涵蓋兩個 store，必須仍然全過）

- [ ] **Step 5: Baseline gate**

Run: `.venv\Scripts\python.exe scripts\baseline_check.py`
Expected: `0 new, 0 changed`

- [ ] **Step 6: Commit**

```bash
git add bridge.py _bridge_legacy.py _tdapi_legacy.py tests/test_cache_state.py
git commit -m "feat: answer cache state by path, one physical generation per reply"
```

---

### Task 8: `Entry` 帶上 `telegram_media_kind` 與 `telegram_chat_id`

**Files:**
- Modify: `_tdapi_legacy.py` — `Entry`、`_to_entry()`
- Test: `tests/test_entry_media_kind.py`

**Interfaces:**
- Consumes: 無
- Produces: `Entry.telegram_media_kind: Optional[str] = None`、`Entry.telegram_chat_id: Optional[str] = None`

Discovery 要找「chat import ＋ photo」候選（spec §7）。backend 的 `FileInfo`
已經回這兩個欄位，`_to_entry()` 只是丟掉。撿回來就**不必為了分類額外打一輪
Telegram**；只有要證明「真的跨 DC」才需要查 `dc_id`。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_entry_media_kind.py
from _tdapi_legacy import Entry, _to_entry


def _row(**over):
    row = {"file_id": "f1", "filename": "a.jpg", "isDir": False, "filesize": 12,
           "created_at": None, "mime_type": "image/jpeg",
           "telegram_message_id": 8, "telegram_user_id": 42}
    row.update(over)
    return row


def test_media_kind_and_chat_id_survive_the_row_conversion():
    e = _to_entry(_row(telegram_media_kind="photo", telegram_chat_id="-100123"))
    assert (e.telegram_media_kind, e.telegram_chat_id) == ("photo", "-100123")


def test_a_row_without_them_reads_as_none():
    e = _to_entry(_row())
    assert e.telegram_media_kind is None and e.telegram_chat_id is None


def test_an_integer_chat_id_is_normalised_to_str():
    # The backend can send this as a number while the thin layer's
    # parse_file_location works in strings. Two shapes for one field makes
    # `entry.telegram_chat_id == location.telegram_chat_id` fail silently.
    e = _to_entry(_row(telegram_chat_id=-100123))
    assert e.telegram_chat_id == "-100123"


def test_media_kind_is_normalised_to_lower_case():
    e = _to_entry(_row(telegram_media_kind="Photo"))
    assert e.telegram_media_kind == "photo"


def test_an_empty_string_is_normalised_to_none():
    # "" and None both mean "the backend did not say"; leaving both shapes in
    # makes every consumer write the same two-way check.
    e = _to_entry(_row(telegram_media_kind="", telegram_chat_id=""))
    assert e.telegram_media_kind is None and e.telegram_chat_id is None


def test_the_new_fields_are_optional_when_building_an_entry_directly():
    # Entry is built in tests, in cache read-back and for pre-schema rows. A
    # required field turns a purely additive change into TypeErrors.
    e = Entry(file_id="f", name="a", is_dir=False, size=1)
    assert e.telegram_media_kind is None and e.telegram_chat_id is None
```

> 若 `Entry` 的既有欄位不是全部都有預設值，補齊最後一個測試裡的必填參數——
> **不要為了讓測試過而給既有欄位加預設值**。

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_entry_media_kind.py -q`
Expected: FAIL with `AttributeError: 'Entry' object has no attribute 'telegram_media_kind'`

- [ ] **Step 3: Write minimal implementation**

`Entry` 最後加兩個帶預設值的欄位（放最後，不打亂既有位置引數）：

```python
    telegram_media_kind: Optional[str] = None
    telegram_chat_id: Optional[str] = None
```

`_to_entry()` 加兩行。`or None` 同時處理缺鍵、`None` 與空字串；
**`telegram_chat_id` 還要 `str()`**——backend 可能回整數，而薄層的
`parse_file_location` 已經是按字串處理的，兩邊形狀不一致會讓
`Entry.telegram_chat_id == location.telegram_chat_id` 這種比較靜靜失敗：

```python
        telegram_media_kind=(str(k).lower() if (k := row.get("telegram_media_kind")) else None),
        telegram_chat_id=(str(c) if (c := row.get("telegram_chat_id")) else None),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_entry_media_kind.py tests/test_routed_metadata.py tests/test_dir_cache.py -q`
Expected: PASS

- [ ] **Step 5: Baseline gate**

Run: `.venv\Scripts\python.exe scripts\baseline_check.py`
Expected: `0 new, 0 changed`

- [ ] **Step 6: Commit**

```bash
git add _tdapi_legacy.py tests/test_entry_media_kind.py
git commit -m "feat: keep telegram media kind and chat id on Entry"
```

---

### Task 9: `isolate.exe` 的 `--jsonl` 與 `--manifest`

**Files:**
- Modify: `shellthumb/isolate.cpp`
- Test: 無離線測試（C++）。驗證見 Step 3-5，之後由 audit 的 preflight 每次重跑。

**Interfaces:**
- Consumes: 無
- Produces: `isolate [--jsonl] [--manifest <file>] <thumb|props> <folder> [count] [px]`

**必須是 narrow UTF-8，不能用 `fwprintf`。** 寬字元輸出會被轉成 console codepage，
而這裡的路徑大半是非 ASCII——「URL 跳脫」那條坑的同一種死法，`warmshell.cpp`
已經踩過。

**`hr` 記哪一個 HRESULT 要講死**，否則兩個實作者會得到兩種語意：

| mode | `hr` | `answered` |
|---|---|---|
| `thumb` | `IShellItemImageFactory::GetImage` 的回傳值；若 `SHCreateItemFromParsingName` 就失敗，記它的 | `SUCCEEDED(hr) && bitmap != nullptr` |
| `props` | `IPropertyStore::GetValue(PKEY_Image_Dimensions)` 的回傳值；若 `SHGetPropertyStoreFromParsingName` 就失敗，記它的 | `SUCCEEDED(hr) && value.vt != VT_EMPTY` |

也就是**最後一個實際被呼叫到的 COM 方法的 HRESULT**。這跟現有 `isolate.cpp`
計算 `got` 的條件完全一致，所以 aggregate 那行的數字與 JSONL 的 `answered`
數量必然相等——**這本身就是一個可以斷言的自我一致性檢查**。

- [ ] **Step 1: Add the flags, the manifest reader and the emitter**

```cpp
static std::string Utf8(const std::wstring& w) {
    if (w.empty()) return std::string();
    // Convert exactly w.size() code units and ask for no NUL, so the length
    // returned is the length needed. Passing -1 counts the terminator, and
    // sizing the buffer to n-1 while still writing n bytes overruns it.
    //
    // &out[0], not out.data(): the non-const data() overload is C++17 and the
    // build scripts set no /std: flag, so MSVC compiles these at its default.
    // warmshell.cpp already does it this way; copy it rather than making the
    // first diagnostics change a compiler-version argument.
    const int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(),
                                      nullptr, 0, nullptr, nullptr);
    if (n <= 0) return std::string();
    std::string out((size_t)n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(),
                        &out[0], n, nullptr, nullptr);
    return out;
}

static void EmitJsonl(const char* op, const std::wstring& path,
                      double ms, bool answered, HRESULT hr) {
    // stderr, narrow UTF-8, flushed per file: a batch killed on a deadline
    // still says how far it got, and non-ASCII paths survive. fwprintf would
    // transcode to the console codepage and mangle most of these names.
    std::string escaped;
    for (char c : Utf8(path)) {
        if (c == '"' || c == '\\') escaped += '\\';
        escaped += c;
    }
    fprintf(stderr, "{\"op\":\"%s\",\"file\":\"%s\",\"elapsed_ms\":%.0f,"
                    "\"answered\":%s,\"hr\":\"0x%08X\"}\n",
            op, escaped.c_str(), ms, answered ? "true" : "false", (unsigned)hr);
    fflush(stderr);
}
```

`--manifest <file>` 時**不掃資料夾**：逐行讀 UTF-8 檔案（去 BOM、去行尾 `\r`），
每行一個完整路徑。在既有 per-file 迴圈裡用同一個 `Now(freq)` 計時，
每檔結束呼叫一次 `EmitJsonl`。

**`--pause-after <n> --pause-seconds <s>`** — 處理完第 n 個檔之後睡 s 秒再繼續，
預設 `0`（不暫停）。只有一個用途，而那個用途沒有它就做不到：Task 10 要證明 DLL
在**同一個 host lifetime** 內重讀 `LogPath`，而兩次獨立執行是兩個 lifetime、
證不出那件事。操作者在暫停期間改登錄值，前後兩段就確定發生在同一個載入的 DLL 上。

```cpp
if (pauseAfter > 0 && processed == pauseAfter && pauseSeconds > 0) {
    fflush(stderr);            // the operator watches stderr to know it began
    Sleep(pauseSeconds * 1000);
}
```

- [ ] **Step 2: Teach buildbench.bat to build isolate as well**

`buildbench.bat` 現在**只編 `bench.cpp`**，完全沒碰 `isolate.cpp`——
照 rev 3 寫的 `Run: shellthumb\buildbench.bat` 不會產生新的 `isolate.exe`，
於是後面每一步都在測舊的執行檔。在 `bench.exe` 那行後面加：

```bat
cl /nologo /O2 /EHsc /W3 /utf-8 /DUNICODE /D_UNICODE isolate.cpp /Fe:isolate.exe
if errorlevel 1 exit /b 1
```

並把結尾的 `echo [ok] bench.exe` 改成 `echo [ok] bench.exe isolate.exe`。

Run: `shellthumb\buildbench.bat`
Expected: `[ok] bench.exe isolate.exe`，且 `isolate.exe` 的時間戳是剛剛

- [ ] **Step 3: Verify the aggregate output is unchanged**

Run: `shellthumb\isolate.exe thumb <一個本機圖片資料夾> 4`
Expected: 仍只有原本那行 `thumb: 4 files in ...`，stderr 空的

- [ ] **Step 4: Verify JSONL round-trips a non-ASCII path**

建一個含中文檔名的本機資料夾（例如 `湊あくあ.jpg`），然後：

```powershell
shellthumb\isolate.exe --jsonl thumb <那個資料夾> 1 2>jsonl.txt
.venv\Scripts\python.exe -c "import json,io; [print(json.loads(l)['file']) for l in io.open('jsonl.txt',encoding='utf-8') if l.strip()]"
```

Expected: 印出的檔名與磁碟上完全相同（不是 `æ¹...`）。**這是 Task 9 唯一能驗證 UTF-8 轉換與 JSON 逸出的步驟，不可跳過。**
（它是 preflight harness 的一部分，不是 `pytest` 會跑到的自動化測試——
C++ 那半沒有離線測試，見 Global Constraints。）

- [ ] **Step 5: Verify the manifest is honoured**

把兩個路徑寫進 `m.txt`：一個真的 JPEG，一個**不存在的路徑**。跑
`shellthumb\isolate.exe --jsonl --manifest m.txt thumb .`

Expected: stderr 剛好兩行、`file` 就是 manifest 裡那兩個（**不是目錄掃描的結果**）、
不存在那行 `answered` 為 `false`。

> 不要用 `.txt` 當「一定失敗」的樣本——Windows shell 對文字檔給不給縮圖
> 不是穩定契約，而且那跟「manifest 有沒有被遵守」無關。不存在的路徑才是
> 確定會失敗的。真正要驗的是**送出去的清單就是 manifest 的內容**。

- [ ] **Step 6: Commit**

```bash
# Source and build script only: .gitignore excludes *.exe / *.dll and none of
# the binaries are tracked. `git add` on isolate.exe would need -f and would
# start tracking a build artefact this repo deliberately does not.
git add shellthumb/isolate.cpp shellthumb/buildbench.bat
git commit -m "feat: per-file jsonl telemetry and an explicit manifest for isolate"
```

---

### Task 10: DLL `LogPath` 可在 process 存活期間改變

**Files:**
- Create: `scripts/kill_thumb_hosts.py`
- Modify: `shellthumb/TeleDriveThumb.cpp` — `Log()`
- Test: 無離線測試（C++、thread-safety）。驗證見 Step 4-5。

**Interfaces:**
- Consumes: 無
- Produces: `Log()` 每 2 秒最多重讀一次 `HKCU\Software\TeleDriveWebDAV\LogPath`，`path` 與時間戳由同一個 SRWLOCK 保護；`scripts/kill_thumb_hosts.py` 只殺載入了本 DLL 的 PID

現況 `static bool checked` **每個 host process 只讀一次**。已載入的 `dllhost`
在 audit 設定 `LogPath` 之後永遠不會開始記錄，audit 依 spec §4 把整批判成
`NOT_MEASURED`——**這正是這份設計要避免的 measurement trap**。

- [ ] **Step 1: Write the targeted killer first**

```python
# scripts/kill_thumb_hosts.py
"""Kill only the COM surrogates that have our thumbnail DLL loaded.

`taskkill /f /im dllhost.exe` takes out every COM surrogate on the machine,
including ones that have nothing to do with this project. Anything that needs
a cold surrogate -- rebuilding the DLL, or the audit's cold-surrogate probe --
goes through here instead.
"""

from __future__ import annotations

import subprocess
import sys

DLL = "TeleDriveThumb.dll"


#: Only surrogates. Killing, say, explorer.exe because it happens to have the
#: handler mapped would be a much bigger hammer than anything this is for.
ALLOWED_IMAGES = {"dllhost.exe"}


def hosts() -> list[tuple[str, int]]:
    proc = subprocess.run(
        ["tasklist", "/m", DLL, "/fo", "csv", "/nh"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"tasklist failed ({proc.returncode}): {proc.stderr.strip()}")
    found = []
    for line in proc.stdout.splitlines():
        parts = [p.strip('" ') for p in line.split('","')]
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        image = parts[0].lower()
        if image not in ALLOWED_IMAGES:
            print(f"skipping {image} (pid {parts[1]}): not a COM surrogate")
            continue
        found.append((image, int(parts[1])))
    return found


def main() -> int:
    try:
        targets = hosts()
    except RuntimeError as exc:
        print(f"[error] {exc}")
        return 2
    if not targets:
        print(f"no COM surrogate has {DLL} loaded; nothing to kill")
        return 0
    failed = 0
    for image, pid in targets:
        proc = subprocess.run(["taskkill", "/f", "/pid", str(pid)],
                              capture_output=True, text=True)
        if proc.returncode == 0:
            print(f"killed {image} {pid}")
        else:
            # Do not print "killed" for something that is still running: the
            # next step is a DLL rebuild, and a false success there turns into
            # a confusing file-lock error instead of an actionable one.
            failed += 1
            print(f"[error] taskkill {pid} exited {proc.returncode}: "
                  f"{(proc.stderr or proc.stdout).strip()}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Replace the one-shot flag with a locked TTL**

```cpp
// Diagnostics. Unlike GetSettings(), this MUST be able to change while the
// host process lives: the audit turns logging on against a surrogate that is
// already running, and a one-shot read leaves it permanently silent.
//
// Do NOT "simplify" this into a magic static the way GetSettings() is. That
// one sits on the per-file hot path and its answer never changes, which is
// exactly why it must be immutable; this one is the opposite on both counts.
static SRWLOCK gLogLock = SRWLOCK_INIT;
static std::wstring gLogPath;
static ULONGLONG gLogPathCheckedAt = 0;   // 0 = never
static const ULONGLONG kLogPathTtlMs = 2000;

static std::wstring CurrentLogPath() {
    AcquireSRWLockShared(&gLogLock);
    const bool fresh = gLogPathCheckedAt != 0 &&
                       (GetTickCount64() - gLogPathCheckedAt) < kLogPathTtlMs;
    std::wstring cached = fresh ? gLogPath : std::wstring();
    ReleaseSRWLockShared(&gLogLock);
    if (fresh) return cached;

    AcquireSRWLockExclusive(&gLogLock);
    // Re-check under the exclusive lock: another thread may have refreshed
    // while we waited, and Explorer starts several threads per folder.
    if (gLogPathCheckedAt != 0 &&
        (GetTickCount64() - gLogPathCheckedAt) < kLogPathTtlMs) {
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

- [ ] **Step 3: Build**

```powershell
.venv\Scripts\python.exe scripts\kill_thumb_hosts.py
shellthumb\build.bat
```

**`install_thumb.py` 設了 `DisableProcessIsolation=1`，所以 handler 通常
載入在呼叫端的 process 裡（Explorer 或 `isolate.exe`），不是 `dllhost.exe`。**
killer 回報「沒有需要殺的 surrogate」是正常結果，不是錯誤。

**DLL 被 Explorer 鎖住時，這一步就明確失敗並停下來報告。** spec §15 明訂
不重啟 `explorer.exe`，而一個觀測工具不該為了自己好做而擴大既定的破壞範圍。
要不要重啟是使用者的決定，不是這份計畫的步驟：

```
[blocked] TeleDriveThumb.dll is locked and no COM surrogate holds it, so the
          handler is loaded in a long-lived process (Explorer). Restarting
          Explorer is out of scope for this plan (spec §15) -- do it yourself
          if you want to, then re-run.
```

無論如何**不要用 `taskkill /f /im dllhost.exe`**。

- [ ] **Step 4: Verify the SAME host re-reads LogPath**

rev 3 與 rev 4 的寫法**都證不出任何事**。rev 3 是「設值 → 等 → probe」；
rev 4 改成 A/C 兩次 `isolate.exe`，但那是**兩個 host lifetime**，
第二次的全新載入在舊的 one-shot 實作下一樣會記錄。

要證明的是**一個 lifetime 內先讀到空值、TTL 之後讀到新值**，所以兩段必須在
**同一次執行**裡，中間留一個空檔讓操作者改登錄值——這就是 Task 9 的
`--pause-after` / `--pause-seconds` 存在的唯一理由。

```powershell
reg delete "HKCU\Software\TeleDriveWebDAV" /v LogPath /f
Remove-Item C:\Temp\dll.log -ErrorAction SilentlyContinue

# 兩個沒看過的 H: 圖片，一行一個
Set-Content -Encoding utf8 m.txt @("H:\<資料夾A>\one.jpg", "H:\<資料夾B>\two.jpg")

# 一次執行：檔案 1 載入 handler（讀到空值）→ 暫停 12 秒 → 檔案 2
Start-Job { shellthumb\isolate.exe --jsonl --manifest m.txt `
              --pause-after 1 --pause-seconds 12 thumb . 2>$null } | Out-Null

Start-Sleep 4      # 確定已經進入暫停
reg add "HKCU\Software\TeleDriveWebDAV" /v LogPath /t REG_SZ /d C:\Temp\dll.log /f
Start-Sleep 12     # 等第二個檔跑完

Get-Content C:\Temp\dll.log
```

**判定有兩個條件，缺一不可：**

1. `dll.log` 有 `GetThumbnail` 行 —— 重讀生效了
2. **第一行的「DLL 載入後毫秒數」`>= 12000`**

第 2 條才是真正的證據。`Log()` 每一行都帶 `GetTickCount64() - start`，
也就是**DLL 載入至今多久**。那個數字若接近 0，代表 DLL 是暫停**之後**才載入的，
這次量測什麼都沒證明；`>= 12000` 才代表它在暫停**之前**就在了——
也就是它真的在同一個 lifetime 內重讀了 registry。

`DisableProcessIsolation=1` 在這裡幫上忙：handler 載入在 `isolate.exe` 自己的
process，所以一次執行就是一個 host lifetime——**結構上保證，再由那個毫秒數實證。**

- [ ] **Step 5: Verify it turns back off in the same host**

同一個形狀反過來：一次執行裡先記錄、暫停中刪掉登錄值、再跑第二個檔。

```powershell
reg add "HKCU\Software\TeleDriveWebDAV" /v LogPath /t REG_SZ /d C:\Temp\off.log /f
Remove-Item C:\Temp\off.log -ErrorAction SilentlyContinue

Start-Job { shellthumb\isolate.exe --jsonl --manifest m.txt `
              --pause-after 1 --pause-seconds 12 thumb . 2>$null } | Out-Null
Start-Sleep 4
$before = (Get-Item C:\Temp\off.log).Length     # 第一個檔已經寫進去了
reg delete "HKCU\Software\TeleDriveWebDAV" /v LogPath /f
Start-Sleep 12

$before -gt 0 -and (Get-Item C:\Temp\off.log).Length -eq $before
```

Expected: `True`。**`$before > 0` 那一半不能省**——否則「檔案沒有變大」
可能只是因為從頭到尾就沒記錄過，那同樣什麼都沒證明。

- [ ] **Step 6: Commit**

```bash
# Source only -- *.dll is gitignored and untracked (see Task 9).
git add scripts/kill_thumb_hosts.py shellthumb/TeleDriveThumb.cpp
git commit -m "fix: let the DLL pick up LogPath changes without a new surrogate"
```

---

## 這份計畫不包含什麼

覆蓋 spec 的 §3、§5、§6，以及 §7 discovery 所需的 `Entry` 欄位。**不**覆蓋：
§4（validity 模型）、§8（三個類別與壓力情境）、§9（roundtrip）、§10（severity）、
§11（preflight）、§12（報告）、§14 的 `tests/test_liveprobe.py`。

那些是 `scripts/_liveprobe.py` 與兩支腳本的工作，寫成第二份計畫——
它們消費的介面（counter snapshot 的形狀、cache-state 的回答、
`isolate --jsonl` 的實際輸出）要等這份落地才算定案。

## 完成後

```powershell
.venv\Scripts\python.exe scripts\baseline_check.py
restart.bat
curl 127.0.0.1:8081/rpc/health
curl 127.0.0.1:8081/rpc/counters
# H:\game itself is Loc(GAME) with no entry, so it 404s by design (Task 7).
# Point at an actual archive.
curl "127.0.0.1:8081/rpc/cache-state?path=H:\game\<某個封存>.zip&kinds=zip"
```

baseline gate 要 `0 new, 0 changed`；三個端點都要 200 且內容合理。

最後在真的 `H:` 上跑一次 sanity check，確認 origin 真的分得開：

```powershell
# 記下 counters → 開一個沒看過的 /game zip → 再記一次
```

`zip_index` 的 bytes 應該 > 0，而 `dav_read` 幾乎不動；讀 zip 裡一個檔之後
反過來。**兩者都動到同一個桶就代表 Task 3 只做了一半**——而那是這個 plan 最
容易做一半的地方。
