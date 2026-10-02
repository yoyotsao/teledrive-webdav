# 拆開 /game 與一般路徑的等待時間，並避免還沒寫完就上傳 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一般路徑（`/game` 以外）的上傳等待時間獨立成 10 秒的新設定；`/game` 維持 5 分鐘；兩邊都不會在 PUT 還在進行（或失敗留下半個檔）時就開始上傳。

**Architecture:** 兩個 stager（`UploadStager`、`GameStager`）各自在 pending record 上加一個「進行中的寫入數」`writers`。`begin_write` +1、`end_write` -1，`_due` 對 `writers > 0` 一律不算到期；到期的計時從 `end_write` 才重新開始。PUT 失敗（`with_errors=True`）時丟掉那份半成品，不讓它留到下一輪被當成完整檔上傳。`writers` 卡住（thread 異常沒走到 `end_write`）有 1 小時的保險逾時。

**Tech Stack:** Python 3.10、wsgidav（`begin_write` / `end_write` 回呼）、pytest。

**Spec:** 沒有獨立的 spec 文件，需求來自本次對話：(1) 一般路徑等 10 秒就夠；(2) `/game` 與一般路徑的等待時間要分開設定；(3) 大檔案還沒複製完不能被上傳。背景讀 `CLAUDE.md` 的「一般路徑的寫入」與「`/game` 的 RAR 一律轉成 zip」兩節。

**不 commit：** 工作樹裡 `bridge.py`、`config.py`、`config.example.ini`、`README.md`、`CLAUDE.md` 已有與本計畫無關的未提交修改（mountctl、console 日誌佇列等）。本計畫每個 task 結尾只跑測試，不 commit；等使用者在真的 `H:` 上確認後，由使用者決定怎麼提交（專案記憶：沒在 `H:` 驗證前不 push）。

## Global Constraints

- 測試指令：`.venv\Scripts\python.exe -m pytest tests -q`（離線、不需 Telegram）。
- 改完 Python 的收尾是 `restart.bat`（只重啟 bridge，不碰 rclone），重啟前先看 `/rpc/status` 有沒有 debounce 中的項目。
- **暫存檔是唯一副本**：只有在「訊息送出 + 每一筆註冊成功」之後才能刪暫存。本計畫唯一新增的「刪暫存」是 **PUT 本身失敗**的那份半成品，它從來沒被當成完整檔，不違反這條。
- `_due(debounce)` 的簽名不變（既有測試用 `_due(0.0)`）。
- 不在 log 或 `/rpc/status` 放憑證；`/rpc/status` 的現有欄位只增不減。
- 文字用 zh-TW；識別字與註解依周圍程式碼風格（註解是英文為主、說明「為什麼」）。

## Review Focus

- **PUT 進行中被判到期**：大檔 PUT 超過等待秒數時，不可轉成 `UPLOADING`（Task 2、4）。
- **PUT 失敗留下半個檔**：連線中斷、`end_write(with_errors=True)` 後，半成品不可被上傳，也不可留在列表裡裝成完整檔（Task 2、3、4）。
- **覆寫既有檔的 PUT**：走的是 `RemoteFileResource.begin_write`，目前失敗時完全沒有任何收尾（Task 3）。
- **同一個檔同時兩個 PUT**（rclone 重試與原請求重疊）：`writers` 是計數而不是布林，一個結束不可把另一個的保護解除（Task 2）。
- **`end_write` 沒被呼叫**（thread 例外）：`writers` 永遠 > 0 會讓檔案永遠不上傳，所以要有逾時保險（Task 2、4）。
- **重啟後的接收**：`_adopt_leftovers` 建出的 record `writers` 必須是 0，否則 crash 前進行中的檔會永遠卡住（Task 2）。

---

## File Structure

| 檔案 | 責任 |
|---|---|
| `config.py` | 新欄位 `upload_debounce_seconds`（`[upload] debounce_seconds`，預設 10）；`debounce_minutes` 維持只管 `/game` |
| `config.example.ini` / `README.md` | 文件化新設定，並把 `debounce_minutes` 的說明收斂成「只管 /game」 |
| `uploadstage.py` | `PendingUpload.writers`、`begin_write` / `end_write`、`_due` 跳過進行中、改讀 `upload_debounce_seconds` |
| `gamestage.py` | `Unit.writers`、同樣的 `begin_write` / `end_write` / `_due` 保護（等待時間仍是 `debounce_minutes`） |
| `bridge.py` | 四個 resource 的 `begin_write` / `end_write` 改呼叫 stager 的新方法 |
| `tests/test_transfer_config.py` | 新設定的預設與範圍 |
| `tests/test_upload_scheduler.py` | `UploadStager` 的 in-flight 行為 |
| `tests/test_game_stager_inflight.py`（新） | `GameStager` 的 in-flight 行為 |
| `tests/test_bridge_e2e.py` | resource 層：覆寫、失敗收尾 |

共用的「逾時保險」常數放在 `gamestage.py`（`uploadstage.py` 本來就從那裡 import `TICK_SECONDS` 等）。

---

### Task 1: 拆開設定

**Files:**
- Modify: `config.py:51`（欄位）、`config.py:259` 附近（解析）
- Modify: `config.example.ini:24-35`（`[upload]` 區段）、`config.example.ini:75-78`（`[game]` 註解）
- Modify: `README.md:63`
- Test: `tests/test_transfer_config.py`

**Interfaces:**
- Produces: `Config.upload_debounce_seconds: float`（預設 `10.0`，必須為有限正數）。Task 2 的 `UploadStager` 讀它。

- [ ] **Step 1: 寫失敗測試**

附加在 `tests/test_transfer_config.py` 的 `test_parity_defaults` 之後：

```python
def test_upload_debounce_is_separate_from_game_debounce(tmp_path, monkeypatch):
    cfg = load_minimal_config(tmp_path, monkeypatch)
    assert cfg.upload_debounce_seconds == 10.0
    assert cfg.debounce_minutes == 5.0  # /game is untouched


def test_upload_debounce_can_be_set_and_must_be_positive(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.delenv("TELEGRAM_SESSION_STRING", raising=False)
    cfg = load_config(write_config(tmp_path, "[upload]\ndebounce_seconds = 3\n"))
    assert cfg.upload_debounce_seconds == 3.0
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, "[upload]\ndebounce_seconds = 0\n"))
```

- [ ] **Step 2: 確認失敗**

Run: `.venv\Scripts\python.exe -m pytest tests/test_transfer_config.py -q`
Expected: FAIL，`AttributeError: ... 'upload_debounce_seconds'`。

- [ ] **Step 3: 實作**

`config.py` 的 `Config` dataclass 在 `debounce_minutes: float = 5.0` 下一行加：

```python
    upload_debounce_seconds: float = 10.0
```

在 `load_config` 的 `hash_concurrency=...` 那一組（`[upload]` 區段）旁加：

```python
        upload_debounce_seconds=positive_float(
            "upload_debounce_seconds", get("upload", "debounce_seconds", "10")
        ),
```

`config.example.ini` 的 `[upload]` 區段（`album_timeout_seconds = 60` 之後）加：

```ini
; A plain file written outside /game is uploaded once its PUT has finished and
; no further write has arrived for this many seconds. /game has its own,
; much longer window (debounce_minutes below) because a folder is packed whole.
debounce_seconds = 10
```

並把 `[game]` 的註解改成明說「只管 /game」：

```ini
; A /game/<folder> subtree with no writes for this long is considered finished,
; then it gets packed (ZIP_STORED) and uploaded. Applies to /game only; plain
; files elsewhere use [upload] debounce_seconds.
debounce_minutes = 5
```

`README.md:63` 該列改為：

```
| `debounce_minutes` | `5` | 丟進 `H:\game\` 的資料夾靜置多久算「搬完了」，然後開始打包。只管 `/game`；其他資料夾的檔案用 `[upload] debounce_seconds`（預設 10 秒，寫完後再等這麼久才上傳） |
```

- [ ] **Step 4: 確認通過**

Run: `.venv\Scripts\python.exe -m pytest tests/test_transfer_config.py -q`
Expected: PASS。

---

### Task 2: `UploadStager` 追蹤進行中的寫入

**Files:**
- Modify: `uploadstage.py`（`PendingUpload`、`UploadStager`：`touch` 旁新增 `begin_write` / `end_write`，`status` / `status_for` 加欄位，`_loop` / `_due` 的等待時間來源）
- Modify: `gamestage.py`（只新增常數 `WRITER_STALE_SECONDS`）
- Modify: `tests/test_upload_scheduler.py:312-316`（fixture 加 `upload_debounce_seconds=0.0`，因為 fixture 是 `SimpleNamespace`）
- Test: `tests/test_upload_scheduler.py`

**Interfaces:**
- Consumes: `Config.upload_debounce_seconds`（Task 1）。
- Produces（Task 3 的 bridge 會呼叫，簽名固定）：
  - `UploadStager.begin_write(segments: Sequence[str], parent_id: Optional[str]) -> None`
  - `UploadStager.end_write(segments: Sequence[str], parent_id: Optional[str], *, ok: bool) -> None`：`ok=False` 時刪掉本機半成品並 `forget`。
  - `gamestage.WRITER_STALE_SECONDS = 3600.0`
  - `PendingUpload.writers: int`、`PendingUpload.writer_started: float`

- [ ] **Step 1: 寫失敗測試**

附加在 `tests/test_upload_scheduler.py` 結尾（`stager` fixture 已存在，先把 fixture 的 `SimpleNamespace` 加上 `upload_debounce_seconds=0.0`）：

```python
def test_a_file_mid_put_is_never_due(stager):
    path = stager.create_file(["big.bin"], "parent")
    stager.begin_write(["big.bin"], "parent")
    path.write_bytes(b"half")
    assert ("big.bin",) not in stager._due(0.0)
    stager.end_write(["big.bin"], "parent", ok=True)
    assert ("big.bin",) in stager._due(0.0)


def test_the_quiet_window_starts_when_the_put_ends(stager):
    stager.create_file(["slow.bin"], "parent")
    stager.get(("slow.bin",)).last_write -= 1000  # looks idle for ages
    stager.begin_write(["slow.bin"], "parent")
    stager.end_write(["slow.bin"], "parent", ok=True)
    assert ("slow.bin",) not in stager._due(60.0)  # just finished: not quiet yet


def test_overlapping_puts_need_both_to_finish(stager):
    stager.create_file(["twice.bin"], "parent")
    stager.begin_write(["twice.bin"], "parent")
    stager.begin_write(["twice.bin"], "parent")  # rclone retry overlapping the original
    stager.end_write(["twice.bin"], "parent", ok=True)
    assert ("twice.bin",) not in stager._due(0.0)
    stager.end_write(["twice.bin"], "parent", ok=True)
    assert ("twice.bin",) in stager._due(0.0)


def test_a_failed_put_drops_its_partial_file(stager):
    path = stager.create_file(["broken.bin"], "parent")
    stager.begin_write(["broken.bin"], "parent")
    path.write_bytes(b"partial")
    stager.end_write(["broken.bin"], "parent", ok=False)
    assert not path.exists()
    assert stager.get(("broken.bin",)) is None
    assert ("broken.bin",) not in stager._due(0.0)


def test_a_failed_overlap_keeps_the_file_the_other_put_is_writing(stager):
    path = stager.create_file(["shared.bin"], "parent")
    stager.begin_write(["shared.bin"], "parent")
    stager.begin_write(["shared.bin"], "parent")
    path.write_bytes(b"x")
    stager.end_write(["shared.bin"], "parent", ok=False)
    assert path.exists()  # the second PUT is still writing it
    assert stager.get(("shared.bin",)).writers == 1


def test_a_writer_that_never_ended_stops_blocking_after_the_stale_limit(stager):
    stager.create_file(["lost.bin"], "parent")
    stager.begin_write(["lost.bin"], "parent")
    stager.get(("lost.bin",)).writer_started -= WRITER_STALE_SECONDS + 1
    assert ("lost.bin",) in stager._due(0.0)


def test_adopted_leftovers_have_no_writers(stager, tmp_path):
    stager.stage_file("crashed.bin")
    revived = uploadstage.UploadStager(stager.cfg, stager.api, stager.engine)
    assert revived.get(("crashed.bin",)).writers == 0
```

并在檔案頂端 import 區加 `from gamestage import WRITER_STALE_SECONDS`。

- [ ] **Step 2: 確認失敗**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_scheduler.py -q`
Expected: FAIL（`ImportError: WRITER_STALE_SECONDS`）。

- [ ] **Step 3: 實作**

`gamestage.py` 在 `TICK_SECONDS` 等常數旁加：

```python
#: A PUT that began but never reported back (its thread died) must not hold a
#: file out of the queue forever. A loopback PUT of even tens of GB finishes
#: well inside this; past it, the writer is assumed gone.
WRITER_STALE_SECONDS = 3600.0
```

`uploadstage.py`：`from gamestage import MAX_ATTEMPTS, RETRY_SECONDS, TICK_SECONDS` 加上 `WRITER_STALE_SECONDS`。`PendingUpload` 加欄位：

```python
    writers: int = 0
    writer_started: float = 0.0
```

`UploadStager` 在 `touch` 之後新增：

```python
    def begin_write(self, segments: Sequence[str], parent_id: Optional[str] = None) -> None:
        """A PUT for this file has started: it cannot be due until it ends."""
        self.touch(segments, parent_id)
        with self._lock:
            pending = self._pending.get(tuple(segments))
            if pending is not None:
                if pending.writers == 0:
                    pending.writer_started = time.monotonic()
                pending.writers += 1

    def end_write(self, segments: Sequence[str], parent_id: Optional[str] = None, *, ok: bool) -> None:
        """The PUT ended. The quiet window restarts now, not when it began.

        A failed PUT leaves a truncated file that was never a complete copy of
        anything, so it is discarded rather than left for the debounce to
        upload — unless another PUT is still writing the same file.
        """
        key = tuple(segments)
        discard = False
        with self._lock:
            pending = self._pending.get(key)
            if pending is not None and pending.writers > 0:
                pending.writers -= 1
            if not ok and (pending is None or pending.writers == 0):
                discard = True
        if discard:
            path = self.path_for(key)
            if path is not None:
                try:
                    path.unlink()
                except OSError:
                    pass
            self.forget(key)
            return
        self.touch(key, parent_id)
```

`_due` 在 `if pending.stage not in (...)` 檢查之後、debounce 比對之前加：

```python
                if pending.writers > 0 and now - pending.writer_started < WRITER_STALE_SECONDS:
                    continue
```

`_loop` 的 `debounce = self.cfg.debounce_minutes * 60` 改為 `debounce = self.cfg.upload_debounce_seconds`；`status()` 的 `"debounce_minutes": self.cfg.debounce_minutes` 改為 `"debounce_seconds": self.cfg.upload_debounce_seconds`，每筆 pending 的字典加 `"writers": p.writers`。

先 `grep -rn "debounce_minutes" tests/` 與 `grep -rn '"uploads"' bridge.py tests/` 確認沒有東西讀 `uploads` 狀態裡的 `debounce_minutes`；有的話同步改。

- [ ] **Step 4: 確認通過**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_scheduler.py tests/test_transfer_status.py -q`
Expected: PASS。

---

### Task 3: 一般路徑的 resource 接上 stager

**Files:**
- Modify: `bridge.py`：`RemoteFileResource.begin_write` / `end_write`（約 776-800 行，覆寫既有檔）、`UploadFileResource.begin_write` / `end_write`（約 1240-1250 行）
- Test: `tests/test_bridge_e2e.py`（沿用 `rig`、`_upload_now`）

**Interfaces:**
- Consumes: `UploadStager.begin_write` / `end_write`（Task 2）。
- Produces: 無新增公開介面。

- [ ] **Step 1: 寫失敗測試**

附加在 `tests/test_bridge_e2e.py` 的 M1b 區段末尾：

```python
def test_a_put_that_is_still_writing_is_not_uploaded(rig):
    assert rig.request("PUT", "/photos/inflight.bin", data=b"first").status_code == 201
    resource = rig.resource("/photos/inflight.bin")  # see helper note below
    resource.begin_write()
    try:
        assert ("photos", "inflight.bin") not in rig.upload_stager._due(0.0)
    finally:
        resource.end_write(with_errors=False)
    assert ("photos", "inflight.bin") in rig.upload_stager._due(0.0)


def test_a_failed_put_leaves_nothing_to_upload(rig):
    assert rig.request("PUT", "/photos/torn.bin", data=b"ok").status_code == 201
    resource = rig.resource("/photos/torn.bin")
    resource.begin_write().write(b"par")
    resource.end_write(with_errors=True)
    assert rig.upload_stager.get(("photos", "torn.bin")) is None
    assert ("photos", "torn.bin") not in rig.upload_stager._due(0.0)
```

**Helper note：** 先 `grep -n "def request\|def names\|class Rig\|def resource" tests/test_bridge_e2e.py` 看 `rig` 現有的輔助方法。若沒有 `rig.resource(path)`，在 `Rig` 加一個：用 `rig.provider.get_resource_inst(path, environ)` 取回 resource（`environ` 用該檔案裡其他測試建 DAV 環境的同一個寫法）。這個 helper 是測試輔助，不改 production。

另加一個覆寫既有已上傳檔的情境（`RemoteFileResource.begin_write` 路徑）：

```python
def test_overwriting_an_uploaded_file_is_protected_while_it_writes(rig):
    resource = rig.resource("/photos/existing.jpg")  # a registered remote file in the rig's fake backend
    resource.begin_write()
    assert ("photos", "existing.jpg") not in rig.upload_stager._due(0.0)
    resource.end_write(with_errors=False)
    assert ("photos", "existing.jpg") in rig.upload_stager._due(0.0)
```

`existing.jpg` 用 rig 現有 fixture 裡已註冊的某個遠端檔（`grep -n "photos" tests/test_bridge_e2e.py | head` 找實際名稱，替換上面的路徑與 key）。

- [ ] **Step 2: 確認失敗**

Run: `.venv\Scripts\python.exe -m pytest tests/test_bridge_e2e.py -q -k "still_writing or failed_put or overwriting"`
Expected: FAIL（`writers` 沒被維護，`_due(0.0)` 在 `begin_write()` 之後仍回傳該檔）。

- [ ] **Step 3: 實作**

`UploadFileResource`（`bridge.py` 約 1240 行起）：

```python
    def begin_write(self, *, content_type=None):
        self.local.parent.mkdir(parents=True, exist_ok=True)
        self.upload_stager.begin_write(self.segments, self.parent_id)
        return self.local.open("wb")

    def end_write(self, *, with_errors):
        if with_errors:
            log.warning("PUT failed for %s — discarding the partial file", self.local)
        self.upload_stager.end_write(self.segments, self.parent_id, ok=not with_errors)
```

`RemoteFileResource`（覆寫既有檔，約 776-800 行）：

```python
    def begin_write(self, *, content_type=None):
        if self.resolver.upload_stager is None:
            raise DAVError(HTTP_FORBIDDEN)
        self._upload_segments = split_dav_path(self.path)
        parent = self.resolver.api.resolve(self._upload_segments[:-1]) if len(self._upload_segments) > 1 else None
        self._upload_parent_id = parent.file_id if parent is not None else None
        stager = self.resolver.upload_stager
        local = stager.create_file(self._upload_segments, self._upload_parent_id)
        stager.begin_write(self._upload_segments, self._upload_parent_id)
        return local.open("wb")

    def end_write(self, *, with_errors):
        if self.resolver.upload_stager is None:
            return
        self.resolver.upload_stager.end_write(
            self._upload_segments, self._upload_parent_id, ok=not with_errors
        )
```

（原本 `with_errors` 時直接 `return`，等於什麼收尾都沒做，半成品會一路留到 debounce 到期被上傳。）

- [ ] **Step 4: 確認通過**

Run: `.venv\Scripts\python.exe -m pytest tests/test_bridge_e2e.py -q`
Expected: PASS（含既有的 PUT、覆寫、去重測試）。

---

### Task 4: `/game` 套同樣的保護（等待時間不變）

**Files:**
- Modify: `gamestage.py`：`Unit`、`GameStager`（`touch` 旁新增 `begin_write` / `end_write`，`_due`）
- Modify: `bridge.py`：`StagingFileResource.begin_write` / `end_write`（約 1158-1166 行）
- Test: `tests/test_game_stager_inflight.py`（新）

**Interfaces:**
- Consumes: `WRITER_STALE_SECONDS`（Task 2）。
- Produces:
  - `GameStager.begin_write(top: str) -> None`
  - `GameStager.end_write(top: str, local: Path, *, ok: bool) -> None`：`ok=False` 時刪掉 `local`（只刪那一個檔，不動整個 unit）。
  - `Unit.writers: int`、`Unit.writer_started: float`

- [ ] **Step 1: 寫失敗測試**

先看 `tests/test_game_rar.py:55-70` 怎麼用 `SimpleNamespace` 建 `GameStager`，在新檔 `tests/test_game_stager_inflight.py` 照同樣方式建立 fixture（`staging_dir`、`pack_dir`、`debounce_minutes=0.0`、假 `api` / `engine`），然後：

```python
from gamestage import WRITER_STALE_SECONDS


def test_a_unit_with_a_put_in_flight_is_not_due(stager):
    stager.touch("Game")
    stager.begin_write("Game")
    assert "Game" not in stager._due(0.0)
    stager.end_write("Game", stager.cfg.staging_dir / "Game" / "a.bin", ok=True)
    assert "Game" in stager._due(0.0)


def test_one_finished_file_does_not_release_a_unit_whose_other_file_is_still_writing(stager):
    stager.touch("Game")
    stager.begin_write("Game")
    stager.begin_write("Game")
    stager.end_write("Game", stager.cfg.staging_dir / "Game" / "a.bin", ok=True)
    assert "Game" not in stager._due(0.0)


def test_a_failed_put_removes_only_its_own_partial_file(stager):
    folder = stager.cfg.staging_dir / "Game"
    folder.mkdir(parents=True)
    keep, torn = folder / "keep.bin", folder / "torn.bin"
    keep.write_bytes(b"whole")
    torn.write_bytes(b"par")
    stager.touch("Game")
    stager.begin_write("Game")
    stager.end_write("Game", torn, ok=False)
    assert keep.exists() and not torn.exists()


def test_a_stuck_writer_stops_blocking_the_unit_after_the_stale_limit(stager):
    stager.touch("Game")
    stager.begin_write("Game")
    stager._units["Game"].writer_started -= WRITER_STALE_SECONDS + 1
    assert "Game" in stager._due(0.0)
```

`_due` 會比對 `top_level_names()`（磁碟上要有 `staging_dir/Game`）；每個測試開頭先 `(stager.cfg.staging_dir / "Game").mkdir(parents=True, exist_ok=True)`。

- [ ] **Step 2: 確認失敗**

Run: `.venv\Scripts\python.exe -m pytest tests/test_game_stager_inflight.py -q`
Expected: FAIL（`AttributeError: begin_write`）。

- [ ] **Step 3: 實作**

`Unit` 加 `writers: int = 0`、`writer_started: float = 0.0`。`GameStager` 在 `touch` 之後：

```python
    def begin_write(self, top: str) -> None:
        """A PUT inside this unit has started: the unit cannot be packed yet."""
        self.touch(top)
        with self._lock:
            unit = self._units.get(top)
            if unit is not None:
                if unit.writers == 0:
                    unit.writer_started = time.monotonic()
                unit.writers += 1

    def end_write(self, top: str, local: Path, *, ok: bool) -> None:
        """The PUT ended; the unit's quiet window restarts now.

        A failed PUT's truncated file is removed on its own — leaving it would
        pack a corrupt member into the zip. The rest of the unit is untouched.
        """
        with self._lock:
            unit = self._units.get(top)
            if unit is not None and unit.writers > 0:
                unit.writers -= 1
        if not ok:
            try:
                local.unlink()
            except OSError:
                pass
        self.touch(top)
```

`_due` 在 `if unit.state not in ("staging", "failed"): continue` 之後加：

```python
                if unit.writers > 0 and now - unit.writer_started < WRITER_STALE_SECONDS:
                    continue
```

`status()` 的每個 unit 字典加 `"writers": u.writers`。

`bridge.py` 的 `StagingFileResource`：

```python
    def begin_write(self, *, content_type=None):
        self.local.parent.mkdir(parents=True, exist_ok=True)
        self.stager.begin_write(self.top)
        return self.local.open("wb")

    def end_write(self, *, with_errors):
        if with_errors:
            log.warning("PUT failed for %s — discarding the partial file", self.local)
        self.stager.end_write(self.top, self.local, ok=not with_errors)
```

- [ ] **Step 4: 確認通過**

Run: `.venv\Scripts\python.exe -m pytest tests -q`
Expected: 全部 PASS（`/game` 的既有 e2e、RAR 轉換、`test_game_rar.py:131` 的 `_due(0) == []` 都不受影響）。

---

### Task 5: 文件與真實 `H:` 驗證

**Files:**
- Modify: `CLAUDE.md`（「一般路徑的寫入」一節與「`config.ini`」說明）、`README.md:217` 附近

- [ ] **Step 1: 更新文件**

`CLAUDE.md` 的「一般路徑的寫入」節，在 `PUT` 那條後面補一小段：一般路徑的等待是 `[upload] debounce_seconds`（10 秒，從 **PUT 結束**起算），`/game` 是 `[game] debounce_minutes`（5 分鐘）；兩個 stager 都用 `writers` 計數擋住進行中的 PUT，失敗的 PUT 丟掉半成品，`WRITER_STALE_SECONDS`（1 小時）是 thread 異常時的保險。`README.md` 的「等 5 分鐘」那句不變（那是 `/game`），但在一般路徑的說明處補「約 10 秒」。

- [ ] **Step 2: 全部測試**

Run: `.venv\Scripts\python.exe -m pytest tests -q`
Expected: PASS。

- [ ] **Step 3: 收尾重啟**

先 `curl -s http://127.0.0.1:8081/rpc/status`，記下 `uploads` 有哪些在 debounce 中；然後從 PowerShell 跑 `restart.bat`（只重啟 bridge，`H:` 不動）。確認 `curl http://127.0.0.1:8081/rpc/health` 回 200，且 `rpc/status` 的 `uploads` 區塊出現 `debounce_seconds: 10.0`。

- [ ] **Step 4: 在真的 `H:` 驗證（會真的上傳到 Telegram，做之前先跟使用者確認）**

1. 在 `H:` 一個測試資料夾裡放一個小檔（例如 1 MB）。約 10 秒內 `/rpc/status` 的該檔 `stage` 應變成 `uploading`，`bridge.log` 出現 `transfer complete`。
2. 用節流的 PUT 模擬大檔：`curl -T bigfile.bin --limit-rate 1M http://127.0.0.1:8081/<測試資料夾>/slow.bin`（30 MB 約 30 秒）。傳輸期間每 5 秒查一次 `/rpc/status`：該檔的 `writers` 應為 1、`stage` 應維持 `staging`；PUT 結束後約 10 秒才變 `uploading`。
3. 傳到一半 `Ctrl+C` 中斷 `curl`：`uploads/` 底下不應留下 `slow.bin`，`/rpc/status` 也不應有它。
4. 測試用的檔案之後在網頁刪掉（軟刪除，進垃圾桶）。

Expected: 以上三點都符合；任何一點不符就回頭查，不要 push。

---

## Self-Review

- **需求對照：** 拆開設定 → Task 1；一般路徑 10 秒 → Task 1、2；不在寫入中上傳 → Task 2、3、4；失敗不留半成品 → Task 2、3、4；`/game` 等待時間不變 → Task 4（仍讀 `debounce_minutes`）。
- **佔位檢查：** Task 3 的 `rig.resource(...)` 與 `existing.jpg` 路徑需要依 `tests/test_bridge_e2e.py` 現有 rig 確認，步驟裡已寫明怎麼找與怎麼補；Task 4 的 fixture 照 `tests/test_game_rar.py:55-70` 建。這兩處是對現有測試輔助的依賴，不是未決的設計。
- **型別一致：** `begin_write(segments, parent_id)` / `end_write(segments, parent_id, *, ok)`（`UploadStager`）與 `begin_write(top)` / `end_write(top, local, *, ok)`（`GameStager`）在 Task 2、3、4 的呼叫處一致；`writers`、`writer_started`、`WRITER_STALE_SECONDS` 名稱全文一致。
- **已知取捨：** 沒有做「上傳前再比對大小與 mtime」的第三層保險；`writers` 計數加逾時已涵蓋已知情境，多一層檔案系統查詢沒有對應的失敗情境要擋。
