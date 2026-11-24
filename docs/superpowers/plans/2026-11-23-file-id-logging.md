# Log 只認檔案 ID 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `bridge.log`、`rclone.log`、`/rpc/status` 不再出現 drive 上的檔名／路徑／message id，一律以 rclone 的穩定檔案 ID（`id=0x%016x`）識別。

**Architecture:** 新增 `fileid.py`（ID 計算與本機路徑對應）與 `logscrub.py`（`ScrubbingFormatter`，輸出端防漏網）；逐一把 log 呼叫改成印 `fileid.tag*()`；`/rpc/status` 改回 ID；新增 `fileid.py info/tag` 查詢工具；rclone patch 抽出共用 `fs.StableID` 並讓 rclone 自己的 log 也印 ID。

**Tech Stack:** Python（pytest、logging）、Go（rclone v1.74.4 patch，`tools/rclone-stableino/`）。

**Spec:** `docs/superpowers/specs/2026-11-22-file-id-logging-design.md`

## Global Constraints

- ID 定義：`FNV-64a(utf8(rclone VFS path)) & ~(1 << 63)`；結果為 0 則為 1；顯示 `id=0x%016x`。VFS path 相對掛載根、`/` 分隔、無開頭 `/`、根目錄為空字串。
- **重構 rclone patch 不得改變任何既有 ID**（`stableIno` 的值是 Windows thumbcache 的 key）。
- 不提供從 ID 反查檔名／路徑的方法；D6 輸出不得含：檔名、路徑、上層資料夾名、`file_hash`、backend `file_id`／`parent_id`、message id、telegram user id、精確位元組大小。
- message id 在 log 裡也換成檔案 ID；查不到時印 `id=?`，**不退回印 message id**。
- 不改 `/rpc/game/*`、`/rpc/fetch-local` 的回應；不處理 Telethon 的 log；`meta/` 底下程式自己的檔案路徑（`could not persist`、`upload_limiter` 狀態檔）維持原樣。
- 既有 log 裡的 telegram user id（`accounts=`、premium flood、migration）不在本規格範圍。
- `D3` 必須是 `logging.Formatter`，不能是 `logging.Filter`（filter 看不到 traceback）。
- **工作樹已有大量使用者未提交的修改**（含 `bridge.py`、`tgio.py`、`gamestage.py`、`uploadstage.py`、`tdapi.py`、`warmup.py`、`zipfs.py`、`CLAUDE.md`）。不要整檔 `git add` 這些檔；不 commit、不 push，除非使用者明說。每個 task 結尾的 checkpoint 只跑該 task 的測試。
- 只跑相關測試檔，不跑整個 suite（`.venv\Scripts\python.exe -m pytest <檔> -q`）。
- 收尾動作按 CLAUDE.md：Python 改動跑完測試後自己 `restart.bat`（背景、完整路徑），不叫使用者跑；rclone patch 要 `stop.bat` + `start.bat`，且**等進行中的上傳結束**。

## Review Focus

- **路徑正規化對不上**：含 `:`／`?`（rclone 編碼成全形）、全形字、結尾空白、NFD 的檔名，ID 不同於 `fsutil` 的值。預期：向量測試與現場驗證都涵蓋這些名字（Task 1、Task 7）。
- **traceback 漏網**：`log.exception` 印出的 `OSError` 路徑（`uploads\`、`staging\`、`local\`、VFS 快取）。預期：換成 tag，`.venv\...` 保留（Task 2）。
- **stderr／excepthook 漏網**：thread 內未捕捉的例外帶路徑。預期：同樣被 scrub（Task 2）。
- **新增 log 把檔名帶回來**：預期全域守門測試失敗（Task 3）。
- **重構 `stableIno` 作廢 thumbcache**：預期寫死值測試與 `SIIGBF_INCACHEONLY` 探針都 HIT（Task 6、Task 7）。

---

## File Structure

| 檔案 | 動作 | 職責 |
|---|---|---|
| `fileid.py` | Create | `path_id`／`tag`／`tag_dav`／`tag_local`、CLI（`tag`、`info`） |
| `logscrub.py` | Create | `ScrubbingFormatter`、`install_excepthooks()` |
| `bridge.py` | Modify | 掛 formatter 與 excepthook；D2 呼叫；`/rpc/status`；`/rpc/fileinfo` |
| `gamestage.py` `uploadstage.py` `upload_engine.py` `rarconvert.py` `zipfetch.py` `fetchlocal.py` `tdapi.py` `tgio.py` `warmup.py` `zipfs.py` `rejectedputs.py` | Modify | D2 逐處替換 |
| `tools/rclone-stableino/stable_id.go` `stable_log.go` `stable_ino.go` `build.py` | Create/Modify | D5 |
| `tests/test_fileid.py` `tests/test_log_privacy.py` `tests/test_fileinfo.py` | Create | 見各 task |
| `CLAUDE.md` | Modify | Task 7 |

---

### Task 1: `fileid.py` 核心與向量

**Files:**
- Create: `fileid.py`, `tests/test_fileid.py`

**Interfaces:**
- Produces:
  - `path_id(vfs_path: str) -> int`
  - `tag(vfs_path: str) -> str`（`"id=0x%016x"`）
  - `tag_dav(dav_path: str) -> str`（去開頭 `/`、`urllib.parse.unquote` 後呼叫 `tag`）
  - `tag_local(path, cache_dir: Path) -> str | None`
  - `lookup(id_int: int, cache_dir: Path, live=None) -> dict | None`（Task 5 實作）
  - `UNKNOWN_TAG = "id=?"`
  - CLI：`python fileid.py tag "<H:\\路徑>"`

- [ ] **Step 0: 先確認正規化（決定餵進 hash 的字串形式，放最前面）。** (a) 在 `tools/rclone-stableino/src` grep `cmd/cmount` 與 `backend/webdav` 對 `encoder`／`Enc` 的使用，判斷 `node.Path()` 是編碼前（remote 名稱）還是編碼後（Windows 名稱）；(b) 對 2–3 個真實 `H:` 檔（含全形字、結尾空白）跑 `fsutil file queryFileID`，與「remote 名稱」「Windows 名稱」兩種候選字串的 FNV 值比對。結論寫回 spec「ID 的定義」，`tag_dav`／`H:` 形式依此決定轉換方向。
- [ ] **Step 1: 用 Go 算出寫死向量。** 在 scratchpad 寫一支只含 `stableIno` 同款邏輯（`fnv.New64a`、`&^ (1<<63)`、0→1）的 Go 程式，對下列路徑印出 `0x%016x`：`""`、`a.txt`、`Hololive/a/00000.m2ts`、`日本語/名前.jpg`、`with space/x y.png`、`game/a.zip/inner/f.bin`、`name：full.jpg`（全形冒號）、`trail .txt`、NFD 版 `é.jpg`（`e\u0301.jpg`）。把輸出貼進測試當常數。
- [ ] **Step 2: 寫失敗測試** `test_path_id_matches_go_vectors`（上述向量逐一 `assert path_id(p) == 常數`）、`test_tag_format`（`tag("a.txt") == "id=0x%016x" % path_id("a.txt")`）、`test_tag_dav_strips_slash_and_unquotes`（`tag_dav("/%E6%97%A5/x.jpg") == tag("日/x.jpg")`）、`test_tag_local_mappings`：`uploads\a\b.jpg`→`a/b.jpg`；`staging\top\x\y`→`game/top/x/y`；`staging\.pack\top.zip` 與 `staging\.convert\<id>.part`→所屬 unit 的 `game/<top>`（unit 名由呼叫端另傳，見 Step 4）；`rclone\vfs\remote\a\b`、`rclone\vfsMeta\remote\a\b`→`a/b`；`local\g\x`→`g/x`；`rejected\a\b`→`a/b`；`savedata-backups\g\ts\f`、`meta\x.json`、`C:\other\f`→`None`。`test_cli_tag`：`subprocess` 跑 `fileid.py tag "H:\a\b.jpg"` 輸出等於 `tag("a/b.jpg")`。
- [ ] **Step 3: 跑測試確認失敗**：`.venv\Scripts\python.exe -m pytest tests/test_fileid.py -q`，預期 ImportError。
- [ ] **Step 4: 實作 `fileid.py`。** FNV-64a 用 Python 整數運算（offset `0xcbf29ce484222325`、prime `0x100000001b3`）。`tag_local` 簽章加可選 `unit: str | None = None` 給 `.pack`／`.convert` 用；路徑比對一律不分大小寫且接受 `\\?\` 前綴。`H:\...` 形式（CLI 與 D3 用）：去掉磁碟機代號、`\`→`/`。**不做任何 Unicode 正規化**，原樣取位元組。
- [ ] **Step 5: 跑測試確認通過**，同 Step 3 指令。

### Task 2: `ScrubbingFormatter` 與 excepthook

**Files:**
- Create: `logscrub.py`, `tests/test_log_privacy.py`（本 task 只寫 formatter／excepthook 兩組）
- Modify: `bridge.py`（`main()` 的 handler 建立處約 1900–1925 行，抽成 `configure_logging(cfg, log_file) -> QueueListener`；cheroot server 建立處）

**Interfaces:**
- Consumes: `fileid.tag`、`fileid.tag_local`
- Produces:
  - `class ScrubbingFormatter(logging.Formatter)`，`__init__(self, fmt, cache_dir: Path, drive: str = "H:")`；覆寫 `format()`，對 `super().format(record)` 整段輸出做替換
  - `install_excepthooks() -> None`（`sys.excepthook`、`threading.excepthook` 改走 `logging.getLogger("bridge").error(..., exc_info=...)`）

- [ ] **Step 1: 寫失敗測試**：`test_formatter_scrubs_oserror_message`（`OSError` 訊息含 `<cache>\uploads\PRIVATE-名前.jpg`，輸出含 `tag("PRIVATE-名前.jpg")`、不含 `PRIVATE`）；`test_formatter_scrubs_logged_exception_traceback`（`log.exception` 在 traceback 的 `FileNotFoundError: ... staging\...` 行，同上）；`test_formatter_scrubs_local_and_vfs_cache`；`test_formatter_keeps_venv_traceback_paths`（`.venv\Lib\site-packages\telethon\x.py` 原樣保留）；`test_formatter_maps_drive_paths_and_urls`（`H:\a\b.jpg`→tag；`http://127.0.0.1:8081/%E6%97%A5/x`→`http://127.0.0.1:8081/<tag>`）；`test_unknown_cache_path_becomes_cache_placeholder`（→`<cache>\…`）；`test_threading_excepthook_is_scrubbed`（thread 拋出帶路徑的例外，捕捉輸出不含標記）。
- [ ] **Step 2: 跑測試確認失敗。**
- [ ] **Step 3: 實作 `logscrub.py`。** 路徑辨識用單一 regex（`[A-Za-z]:\\[^\s'"<>|]*` 與 `\\\\\?\\...`），逐一 match 後依 `tag_local`／`H:`／其他分流。
- [ ] **Step 4: 抽出 `bridge.configure_logging(cfg, log_file) -> QueueListener`，把 `ScrubbingFormatter(fmt=原 format 字串)` 設在 `handlers` list 裡的兩個 handler 上：`_DropOnFull`（`QueueHandler.prepare()` 用它自己的 formatter 組出含 traceback 的 `record.msg` 並清掉 `exc_info`）與 `RotatingFileHandler`。listener 裡的 `StreamHandler` 維持無 formatter，否則時間戳印兩次。呼叫 `install_excepthooks()`。**
- [ ] **Step 4b: cheroot。** 讀 `.venv` 裡 `cheroot/server.py` 的 `HTTPServer.error_log`；若它直接寫 `sys.stderr`，在 bridge 建 server 處把 `server.error_log` 換成走 logging 的函式（經同一個 formatter）。
- [ ] **Step 4c: 新增測試** `test_console_queue_path_is_scrubbed_with_single_timestamp`、`test_cheroot_worker_exception_is_scrubbed`。
- [ ] **Step 5: 跑 `tests/test_log_privacy.py` 與 `tests/test_log_noise.py` 確認通過。**

### Task 3: D2 逐處替換與全域守門

**Files:**
- Modify: spec D2 表列的每個檔（`bridge.py`、`gamestage.py`、`uploadstage.py`、`upload_engine.py`、`rarconvert.py`、`zipfetch.py`、`fetchlocal.py`、`tdapi.py`、`tgio.py`、`warmup.py`、`zipfs.py`、`rejectedputs.py`）
- Test: `tests/test_log_privacy.py`（加守門）、既有 `tests/test_failover_logging.py`、`tests/test_transfer_logging.py`、`tests/test_read_pace.py`、`tests/test_thumbnails.py`

**Interfaces:**
- Consumes: `fileid.tag`、`fileid.tag_dav`、`fileid.tag_local`、`fileid.UNKNOWN_TAG`
- Produces（Task 4／5 會用）：`tgio.note_read_label(message_id, tag: str)` 改存**檔案 tag**（不再存路徑）；`bridge.py:257` 呼叫端改傳 `tag_dav(...)`。

- [ ] **Step 1: 寫失敗的全域守門** `test_no_private_filename_in_logs_or_status`：以 `PRIVATE-名前-123.jpg`（及 `PRIVATE-folder` 資料夾）驅動 `tests/test_bridge_e2e.py` 的 fixture，跑 PUT 上傳、失敗重試（假 backend 讓註冊失敗）、`rejectedputs` 收下（同路徑 PUT 失敗 3 次並拉時鐘）、`/game` 打包、listing 失敗、zip 讀取失敗；以 `caplog` 加 stderr 擷取加 `GET /rpc/status` 的 JSON 全文，斷言標記一次都沒出現（含 `PRIVATE`、`名前`）。
- [ ] **Step 2: 跑守門確認失敗**，記下洩漏位置清單作為下面各步的核對表。
- [ ] **Step 3: `bridge.py`**：thumbnail/head 失敗、zip 目錄讀不到、`_unavailable`、listing 少項目（子項名稱→`tag_dav`）、建資料夾失敗、resolve crash、403 寫入保護。
- [ ] **Step 4: `gamestage.py`／`uploadstage.py`／`rarconvert.py`／`rejectedputs.py`**：全部改傳 tag（`rejectedputs` 的 `target` 本機路徑直接拿掉）。**打包完成與 RAR 轉換完成那一行印 `from=<tag> to=<tag>`**（`game/<top>`→`game/<top>.zip`；`.rar`→`.zip`），之後一律新 ID。
- [ ] **Step 5: `upload_engine.py`**：`transfer failed`、`registration failed`、`transfer complete`、`premium flood`／migration 加 tag；`transfer_models.TransferRequest` 加欄位 `attempt: int = 1`（由 `uploadstage`／`gamestage` 依佇列失敗次數 +1 填入），`transfer failed`／`transfer complete` 印 `attempt=%d`。`redact()` 之後的錯誤文字不變。
- [ ] **Step 6: `tdapi.py`（同名 row 警告 `entry.name`→tag，保留 `parent_id`／`file_id`）、`zipfs.py`（成員名→封存 tag + 成員序號）、`zipfetch.py`、`fetchlocal.py`、`warmup.py`（listing 失敗、warmshell 卡住路徑）。**
- [ ] **Step 7: `tgio.py`**：`READ_LABELS` 值改為 tag；`_doc_label` 改回 `f"{tag} dc{dc}"` 風格——查不到回 `UNKNOWN_TAG`，不用檔名、不用 `doc:<id>`；`chunk timing` 的 `top:` 改 `id=0x... dc5 n=7 mean=1027ms` 並依 `mean` 由大到小排序；`thumbnail for message %s failed`、`media info for message %s failed`、`file_reference expired for message %s`、`RemoteIdentityError` 文字全部改印 `READ_LABELS.get(message_id, UNKNOWN_TAG)`；預抓與 sweep（`_thumbnails`／`Resolver.thumbs_for`／`Warmer.fill` 這些沒經過 `open_remote` 的路徑）由呼叫端先 `note_read_label(message_id, tag_dav(path))` 登記，登記不到才是 `id=?`。
- [ ] **Step 8: 更新既有測試**（`test_failover_logging`、`test_transfer_logging`、`test_read_pace`、`test_thumbnails`）：斷言 tag 而非名稱／message id；新增 `test_unknown_label_prints_id_question_mark`。
- [ ] **Step 9: 跑守門與上述四個測試檔，預期通過。** 守門若仍有洩漏，回頭補洩漏點，不放寬守門。

### Task 4: `/rpc/status`（D4）

**Files:**
- Modify: `bridge.py`（`/rpc/status` 組裝處）、`gamestage.py`、`uploadstage.py`（status 輸出）
- Test: `tests/test_transfer_status.py`、`tests/test_log_privacy.py`

**Interfaces:**
- Consumes: `fileid.tag`
- Produces: `uploads.pending[].id`、`units[].id`（字串 `"0x..."`）；`detail` 經 `ScrubbingFormatter` 同一套 scrub（抽出 `logscrub.scrub_text(text, cache_dir) -> str` 共用）

- [ ] **Step 1: 寫失敗測試**：`test_status_uses_ids_not_paths`（`uploads.pending[0]` 有 `id`、無 `path`；`units[0]` 有 `id`、無 `name`）；`test_status_detail_is_scrubbed`；擴充守門讀 status 全文。
- [ ] **Step 2: 跑確認失敗。**
- [ ] **Step 3: 實作**：從 `Formatter` 抽出 `scrub_text`；status 欄位改 `id`。
- [ ] **Step 4: 跑 `tests/test_transfer_status.py`、`tests/test_log_privacy.py` 通過。**

### Task 5: 查詢工具 `fileid.py info` 與 `/rpc/fileinfo`（D6）

**Files:**
- Modify: `fileid.py`（加 `lookup` 與 `info` 子命令）、`bridge.py`（`/rpc/fileinfo`）
- Test: `tests/test_fileinfo.py`

**Interfaces:**
- Consumes: `tdapi` 的 `meta/dirs/<parent_id>.json` 格式、`uploads/`、`staging/`
- Produces: `fileid.lookup(id_int: int, cache_dir: Path, live=None) -> dict | None`；`GET /rpc/fileinfo?id=0x...`；CLI `python fileid.py info 0x...`

- [ ] **Step 1: 寫失敗測試**：對一般路徑、`/game` 暫存、`/game` 已打包 zip、zip 成員各建一個假快取樹，`lookup` 回的欄位齊全（格式、真實大小量級與 backend 差值、媒體屬性、位置類型、路徑深度、storage、part 數、各 part 帳號**序號**與 DC、上傳狀態、快取狀態、同名 row 數、建立時間）；`test_info_output_has_no_forbidden_fields`（序列化輸出不含標記檔名、`file_hash`、`file_id`、`parent_id`、message id、telegram user id、精確位元組數）；`test_unknown_id_returns_none_without_backend_call`（假 `api` 計次為 0）。
- [ ] **Step 2: 跑確認失敗。**
- [ ] **Step 3: 實作**：掃 `meta/dirs/` 樹、對每個 `H:` 路徑算 `path_id` 比對（名字只在記憶體）；帳號序號依 session registry 排序（primary=`primary`，其餘 `#2`…）；大小輸出量級字串（`~1.2 GiB`）加差值。
- [ ] **Step 4: `/rpc/fileinfo` 接 bridge（多帶記憶體裡的上傳與快取狀態），不打 backend。**
- [ ] **Step 5: 跑 `tests/test_fileinfo.py` 通過。**

### Task 6: rclone patch（D5）

**Files:**
- Create: `tools/rclone-stableino/stable_id.go`、`stable_log.go`、`stable_log_test.go`
- Modify: `tools/rclone-stableino/stable_ino.go`、`tools/rclone-stableino/build.py`

**Interfaces:**
- Produces（Go）：`fs.StableID(path string) uint64`（放 `src/fs/stable_id.go`，避免 `fs`↔`cmount` import cycle）；`stable_ino.go` 的 `stableIno(node)` 改為 `return fs.StableID(node.Path())`；`stable_log.go` 提供給 `fs/log.go` `logSlogWithObject` 的 hook：`object` 非 `fs.Fs` 時以 `StableID(%+v)` 替換成 `id=0x...`；訊息本文替換 `http://127.0.0.1:<port>/<escaped path>`（解碼後算 ID）與 `<--cache-dir>\vfs\<remote>\<rel>`、`vfsMeta\...`（`<rel>` 的 ID）。

- [ ] **Step 1: 寫失敗 Go 測試** `stable_log_test.go`：Task 1 的向量對 `StableID`；**重構前 `stableIno` 的寫死值**（先用現有 `stable_ino.go` 對同組向量算出，寫進測試）與 `StableID` 相等；兩種訊息形狀的替換。
- [ ] **Step 2: 實作** `stable_id.go`、重構 `stable_ino.go`、`stable_log.go`；`build.py` 的 `apply_patch()` 複製新檔進 `src/fs/`，並對 `fs/log.go` 加一條「對不上就失敗」的 exact-match patch（沿用 `ORIGINAL`／`PATCHED` 寫法，必須剛好出現一次）。
- [ ] **Step 3: `go test` 該套件通過**（在 `src/` 下以 `-tags cmount`）。
- [ ] **Step 4: `build.py` 輸出改成 `rclone.new.exe`**（執行中的 `rclone.exe` 被 mountctl 開著，Windows 不能覆寫）；執行 `python tools\rclone-stableino\build.py`，`rclone.new.exe version` 顯示 `-stableino`。換名在 Task 7。

### Task 7: 上線與現場驗證

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: 跑 Task 1–5 的測試檔全過**（`test_fileid`、`test_log_privacy`、`test_fileinfo`、`test_transfer_status`、`test_failover_logging`、`test_transfer_logging`、`test_read_pace`、`test_thumbnails`、`test_log_noise`、`test_rejected_puts`、`test_bridge_e2e`）。
- [ ] **Step 2: 看 `/rpc/status`，確認沒有進行中的上傳／debounce 計時**；有就等，不重啟。
- [ ] **Step 3: 等上傳結束後只做一次：`stop.bat` → 把 `rclone.new.exe` 換名成 `rclone.exe`、刪 `meta/bridge.log*` 與 `meta/rclone.log*`（D7）→ `start.bat`。**（不另跑 `restart.bat`。）
- [ ] **Step 4: 現場驗證（真的 `H:`，spec「現場驗證」1–5）**：瀏覽、上傳一個檔、讓一次上傳失敗；兩份 log 搜尋檔名每一段資料夾名稱 0 筆；同一檔 `fileid.py tag` == `fsutil file queryFileID`（去補零）== `bridge.log` 的 `id=` == `rclone.log` 的 `id=`，**含全形字、日文、結尾空白、NFD 各一個**；`/rpc/status` 不含路徑；重編 rclone 後既有暖過資料夾 `SIIGBF_INCACHEONLY` 仍 HIT。
- [ ] **Step 5: 更新 `CLAUDE.md`**：「憑證不外洩」擴充為「憑證與檔名都不外洩」並修正 `/rpc/status`「貼進 issue 是安全的」那句；檔案表加 `fileid.py`／`logscrub.py`；`/rpc` 表加 `/rpc/fileinfo`；rclone 掛載參數節加 log patch；升級 rclone 檢查清單加「跑一輪現場驗證第 2 點」。
