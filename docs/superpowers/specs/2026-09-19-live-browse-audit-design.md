# 真實 `H:` 巡檢與上傳往返探測 — 設計

**日期：** 2026-09-19（rev 3.4）

**實作 base：** `feat/current-backend-storage-parity` @ `39ad472`（vs `master` ahead 40 / behind 0）。
Audit 實作走 `feat/live-browse-audit`，**不與 parity 的修復 commit 混在同一條開發線**。
既存測試失敗記在 `tests/known_failures.txt`（77 筆），以便分辨 audit 弄壞的與本來就紅的。

**狀態：** Proposed

**目標倉庫：** `yoyotsao/teledrive-webdav`

---

## 0. 版本與引用規則

### 0.1 為什麼這份 spec 只引用 symbol

rev 2 寫了 `_tdapi_legacy.py:819` 這種定位，review 指出 master 上沒有那些檔。
兩邊都對，指的是不同的 tree：

| tree | `tdapi.py` | `_*_legacy.py` | `trash()` 在哪 |
|---|---|---|---|
| `master` | 883 行，完整實作 | 無 | `tdapi.py` |
| `feat/current-backend-storage-parity` | 310 行薄層 | 四個，`d0f63b2` 引入 | `_tdapi_legacy.py` |

**把引用改回 master 定位不是解**——這個 branch 一 merge，master 的行號就換成
同樣的命運。所以本版起：

> **只引用 symbol（`TeleDriveClient.trash()`、`Resolver._zip_cache`、
> `ZipView.root()`、`tgio` 的兩個 `iter_download` 呼叫點），不引用檔案路徑加行號。**
> §13 的檔案清單同時列出兩棵樹，因為實作必須真的打開某個檔。

### 0.2 rev 3 處理的項目

| # | rev 2 的問題 | 本版 |
|---|---|---|
| 1 | 架構敘述綁在單一 tree | §0.1 改引用 symbol |
| 2 | cache 路徑寫錯 | §5 依實際 store 重寫 |
| 3 | 只看磁碟檔判 cold，忽略記憶體快取 | §5 改由 bridge 提供 per-key cache-state |
| 4 | `LogPath` TTL 會產生 data race | §3.2 SRWLOCK + path/timestamp 一起同步 |
| 5 | `fetch-local` 對 ZIPDIR 的語意寫錯 | §8.3 改成「完整虛擬目錄取回」 |
| 6 | validity 只有全域一個 | §4 改 per-operation |
| 7 | counter 來源只有 rpc/sweep | §6 改顯式 origin |
| 8 | 類別 A 假設「前 N 個檔都是靜態圖」 | §8.1 manifest |
| 9 | 類別 A 的 props validity 退化 | §8.1 補回 |
| 10 | 類別 B 的跨界檢查沒有 oracle | §8.2 直接讀 part message 當 oracle |
| 11 | `zip_index_reads_total` 會被 warm cache 掩蓋 | §8.3 拆 `zip_open_attempts_total` |
| 12 | `preexisting` 的影響範圍寫反 | §8.3 只影響該 zip 的 cold-open |
| 13 | 90 秒 idle 不代表真的 idle | §8.4 用 counter 靜止證明 |
| 14 | `--with-sweep` 多半碰不到 active | §8.4 接受 `NOT_MEASURED` + `next_run_at` |
| 15 | `taskkill dllhost.exe` 殺全機 | §8.4 只殺載入了本 DLL 的 PID |
| 16 | roundtrip 等 `uploads` 排空不夠 | §9.2 也等 game staging unit |
| 17 | sustain 背景負載走 `H:` 仍會被 rclone 吃掉 | §8.4 背景走 bridge HTTP |
| 18 | `--sustain-max-bytes` 算 requested 會低估 | §8.4 依 `download_bytes_total` 實際增量 |
| 19 | p95 樣本數未定義 | §10.2 `max` 當 gate，p95 需 N≥20 |
| 20 | 時間出現在 functional 判定裡 | §10 correctness 只管內容 |
| 21 | `means` 過度推論 | §12.1 因果需要 byte evidence |
| 22 | 「每項 diagnostics 都有離線測試」涵蓋不到 C++ | §3.0 縮小宣稱，preflight 即 harness |

三項不照 review 的建議走，理由見 §9.3（purge）、§8.4（sweep 觸發）、§3.0（C++ 測試）。

### 0.3 rev 3.2 追加

實作計畫 review 指出六件在 rev 3 還會造成假量測的事：

| # | 問題 | 本版 |
|---|---|---|
| 23 | named counter 只定義不接線，永遠是 0 —— §8.3 的 `/game` 檢查因此**無條件通過** | §6.1b 列出四個 bump 點 |
| 24 | origin 只穿過 `tgio` seam，沒穿過 request 來源 | §6.1 補完整 provenance chain 與 `ZipView` 的 per-call origin |
| 25 | 成功才記一筆，retry 與失敗 attempt 不算 | §6.1a 拆 `record_request` / `record_bytes`，記在 attempt 上 |
| 26 | `resolve()` 收段落串列、回 `Loc` 不是 `Entry`；`_cache_key()` 與 `_thumb_path()` 各打一次 backend | §5 改在薄層一次算完，legacy 只做 HTTP |
| 27 | `key in Resolver._zips` 不等於 warm —— 列 `/game` 就會建 view | §5 改判 `view._root is not None` |
| 28 | `JsonStore` 是 `_data` + 單一 `_path`，跟 `ShardedJsonStore._memory` 不同 | §5 兩個 store 分開實作，不共用 helper |

### 0.5 rev 3.4 追加

consolidated review 走完了七條路徑，找到的設計層問題：

| # | 問題 | 本版 |
|---|---|---|
| 31 | **`strict_routing.py` 才是 live read 的 seam** —— 它把 `tgio.read_part` 與 `tgio._legacy.read_part` 都重新綁到自己，而前三版完全沒提到它 | §13 file map 列入；實作計畫的 origin 任務以它為主 seam |
| 32 | 宣稱 sweep 的位元組都落在 `warmup` 桶 —— `shell_warm()` 叫 shell 經 `H:` 回來打 bridge，那些請求不知道自己源自 sweep | §6.2 改成「`warmup` 只代表 in-process I/O」，與 sweep 重疊的窗一律 `NOT_MEASURED` |
| 33 | §10.3 總表還留舊的單桶 `props == 0` | 同步成三桶 |
| 34 | Class B 的 oracle 寫成「直接讀 part message」，在 canonical identity 下會讀錯檔 | §8.2 改成從 `current_parts(entry)` 取 authoritative part |

**第 31 項跟第 24 項（rev 3.2）是同一個錯誤犯第二次**：都是「以為找到了最底層，
其實下面還有一層 rebind」。這個 repo 有三層 monkey-patch（`tgio.py` 蓋 legacy、
`bridge.py` 蓋 `Resolver`、`strict_routing.py` 蓋 `read_part`），**任何
「這個函式就是實作」的判斷都必須先 grep 過有沒有人在 import 之後重新綁定它。**

### 0.4 rev 3.3 追加

| # | 問題 | 本版 |
|---|---|---|
| 29 | Class B 的「屬性不讀位元組」只看 `props` 一個桶 —— DLL delegate 之後 Windows 自己從 `H:` 讀原檔，那些位元組落在 `dav_read`，檢查照樣通過 | §8.2 改看整個窗的 `props` ＋ `dav_read` ＋ `unknown` |
| 30 | `kinds` 列了 `listing` 但無從實作 | §5 拿掉；`api_metadata` 在 `/rpc/forget` 之後必然 cold，不需要查 |

**第 29 項是這一輪最重要的。** 它是「假通過」的教科書範例：檢查本身沒寫錯，
但**失敗的位元組會流進另一個桶**，於是那條 gate 在它最該響的時候完全安靜。
同一個形狀值得記住——每加一個「某個桶必須是 0」的斷言，都要先問
**「真正的失敗會不會記到別的桶去」**。

另外三點是 review 之外補的：**`origin` 的預設值一律 `unknown`**（冒充 `dav_read`
是最難發現的錯標）、**`unknown` 非零即 validity 失敗**（§4）、
**baseline 的 failure signature 要正規化**（否則記憶體位址與行號讓它每次都「變了」）。

---

## 1. 這份 spec 要回答的問題

> **「使用體驗可以跟本機硬碟一樣嗎？頂多開檔速度慢一點，但是不應該一直轉。」**

「一直轉」可以量測，但不能用平均值。100 張圖裡 99 張 40 ms、1 張 30 秒，
平均 340 ms 看起來健康，而使用者只看到那 30 秒。全程用 `max`，
並把「單一操作 `>= 5 s`」定義成使用者感知得到的 stall（全篇一致用 `>=`）。

而這個問題不能只靠自己新上傳的檔案回答——見 §2。

---

## 2. 為什麼主角是唯讀巡檢

腳本剛上傳的資料夾是整個 drive 上最幸運的樣本：全 `document` 型、在 primary 帳號
自己的 DC 上、剛註冊所以 `has_thumbnail` 與 `file_id` 都正確、數量小、一層深。

讓人一直轉的每一條坑都需要特定觸發條件，**新資料夾一條都不滿足**：

| 成因（皆出自 CLAUDE.md 的實測紀錄） | 觸發條件 | 新資料夾 |
|---|---|---|
| exported sender 60 秒計時器 → 8 條連線同時重連 → `Server closed the connection` | 跨 DC 檔案 ＋ >60 秒空檔 | ✗ |
| FLOOD_WAIT 累積 | 持續拉取數分鐘 | ✗ |
| backend keep-alive 斷線 → `/rpc/thumb` 500 → `delegating` 讀整檔 | 閒置數秒後再請求 | ✗ |
| DLL `Settings` magic-static race | handler 冷載入的頭幾百微秒 | ✗ |
| sweep 跟前景搶 | `BackgroundWarmup` 正在跑 | ✗ |
| 路徑解析每層 0.52 秒 | 深層路徑 + 未快取 | ✗ |
| 列 `/game` 打開每一個封存 | `/game` 底下幾十上百個 zip | ✗ |
| chat import 的 `photo` 型 media | 該資料夾是 chat import 來的 | ✗ 結構上不可能 |

**上傳往返證明「新東西是對的」，唯讀巡檢證明「舊東西不會卡」。**

---

## 3. 產品程式碼的改動

### 3.0 不變量與其誠實範圍

> **不改變任何資料路徑或產品行為；只新增唯讀 diagnostics。**

覆蓋的誠實說法（rev 2 這句寫得太滿）：

- **Python 側的每一項 diagnostics 都有離線測試**（counter 增量、`warmup.status()`
  的狀態機、cache-state 回報）。
- **C++ 那兩項沒有離線測試，也不假裝有。** Python parser 的測試只證明 parser，
  不證明 `isolate.exe` 真的輸出合法 UTF-8 JSONL；thread-safety 更不是測得出來的，
  是 SRWLOCK 保證的。**改由 preflight 當那個 harness**：它本來就要 probe
  `--jsonl` 與 LogPath 重讀，那次 probe 就是這兩項每次執行前的驗證（§11）。

代價要講清楚：**audit 的正確性開始依賴它自己在測的程式碼**。一個記錯的 counter
會往「假通過」的方向錯。防線是 §3.4。

### 3.1 `isolate` — 加 `--jsonl` 與 manifest

現況是整批跑完才印一行 aggregate 到 stdout，沒有逐檔 latency、沒有檔名、
沒有進度。「單張 thumb max」與「逾時被 kill 也說得出做到哪一張」**原始資訊
根本不存在**，不是 Python parser 能補的。

兩個改動：

**(a) `--jsonl`** — 每個檔一結束就往 **stderr** 印一行並 flush，stdout 仍印
原本的 aggregate（人工使用不受影響）：

```
{"op":"thumb","file":"a.jpg","elapsed_ms":83,"answered":true,"hr":"0x00000000"}
{"op":"thumb","file":"b.jpg","elapsed_ms":7412,"answered":true,"hr":"0x00000000"}
```

走 stderr、**narrow UTF-8 而不是 `fwprintf`**——寬字元輸出會被轉成 console
codepage，而這裡的路徑大半是非 ASCII，那正是「URL 跳脫」那條坑的同一種死法。
`warmshell` 已經踩過。

**(b) `--manifest <file>`** — 從檔案讀要測的路徑清單，取代「掃資料夾前 N 個檔」。
現況混到 `.txt` / `.zip` / 影片時，`answered == N` 與 `delegating == 0`
就變成**錯誤的門檻**——那些檔本來就不該有縮圖。由 Python 端決定測哪些，
才能讓門檻有意義。

### 3.2 DLL — `LogPath` 的 lifecycle，且必須 thread-safe

現況 `Log()` 用 `static bool checked`，**每個 host process 只讀一次 registry**：

```
dllhost 已載入 handler，當時沒有 LogPath  →  checked=true, path=""
audit 寫入 HKCU\...\LogPath
同一個 dllhost 再被呼叫                    →  不重讀  →  audit 看不到任何 log
                                           →  依 §4 判成 NOT_MEASURED
```

**這正是這份設計要避免的 measurement trap，而 rev 1 掉了進去。**

改法：path 快取加 **2 秒 TTL**，且 **`path` 與 timestamp 由同一個 SRWLOCK
一起保護**。rev 2 只寫了 TTL，那會在多執行緒 dllhost 上產生對
`static std::wstring` 的 data race——Explorer 進一個資料夾就是好幾個執行緒
同時進來，這不是理論風險，是這個 DLL 已經因為類似原因壞過一次的地方。

讀取路徑用 `AcquireSRWLockShared` 拿現值；過期才升級成 exclusive 重讀，
並在拿到 exclusive 之後**再檢查一次**時間戳（另一個執行緒可能已經刷新過）。

> **不要改成 `GetSettings()` 那種 magic static。** 那條坑（多執行緒冷載入時
> 旗標在讀取之前就立起來）是**設定**的：熱路徑、結果永不變，所以必須是
> magic static。記錄需要的恰好相反——可以在 process 存活期間改變。
> 這兩個相反的需求要寫在程式碼註解裡，否則下一個人會「順手統一」。

### 3.3 read-only diagnostics（`RpcApp`）

`_health` 目前只有 `ok` / `telegram_user_id` / `base_url` / `mount_drive` /
`game_folder`；`_status` 是 stager + uploads + pool，而 `RpcApp` 的建構
**根本沒收到 `BackgroundWarmup`**。

新增：

```json
// GET /rpc/health
{ "cryptg": true }

// GET /rpc/status
{ "warmup": {"enabled": true, "active": false, "phase": "idle",
             "pass": 3, "next_run_at": "2026-09-20T02:00:00"} }

// GET /rpc/counters      ← §6
// GET /rpc/cache-state   ← §5
```

`BackgroundWarmup` 要被傳進 `RpcApp` 並長出 `status()`。這是唯一碰到 wiring
的改動，仍不改行為。

### 3.4 diagnostics 自己的防線

- 每個 counter 都要有離線測試，斷言「做了 N 次讀取，counter 剛好加 N」。
- **記帳點**只有兩處：`_thumbnail_bytes` 與 `_chunk` 的 `iter_download` 呼叫——
  這是整個 repo 僅有的兩個 wire I/O 點。**植在更上層的便利函式會漏，
  而漏掉的方向正好是假通過。**
- **但 origin 的傳遞是跨 seam 的。** canonical 的入口
  （`read_location()` / `thumbnail_location()`）在薄層 `tgio.py`，最後才呼叫到
  inherited 的 `_read()` / `_thumbnail_bytes()`；legacy Saved Messages 的入口
  則在 legacy 那邊。所以「只改 legacy 檔」會讓 canonical 那條路徑的 origin
  永遠是預設值——**又是一個只會靜靜給錯答案、不會報錯的漏法**。
- `/rpc/counters` 與 `/rpc/cache-state` 不得出現任何憑證，跟 `/rpc/status` 同規矩。

---

## 4. 四層模型與 per-operation validity

```
1. Discovery              找候選樣本，且不可造成重負載
2. Measurement validity   每個 operation 各自判定
3. Functional correctness 只管內容：bytes / SHA256 / CRC32 / 覆蓋 / 邊界
4. UX performance         只管時間：max / stall / 持續負載下的衰退
```

> **不變量：只有 `validity == valid` 的量測才進 threshold evaluator。
> 其餘是 `NOT_MEASURED`，既不是 pass 也不是 fail。**

**validity 是 per-operation 的，不是全域一個布林**（rev 2 寫錯了）。
「這個 drive 上沒有跨 DC 樣本」不該讓 A/B/C 已經成功量到的東西全部作廢。
每個 measurement window 自己帶：

```json
{"op": "class_B.props_no_bytes",
 "validity": "not_measured",
 "why": "BackgroundWarmup was active during the window"}
```

top-level 只做 summary：`"valid_ops": 14, "not_measured": 3`。

**一條全域的 validity 規則：量測窗內 `download_requests_total["unknown"]` 的增量必須是 0。** 非零代表還有 call site 沒有標 origin，於是那個窗裡「某個 origin 是 0」這種斷言全部失去意義——它可能只是流量被記到 `unknown` 去了。

第 3 層與第 4 層**分開產生 finding**，而且**時間不得出現在 functional 判定裡**
（rev 2 的「functional budget 8s」仍然混淆了兩者——9 秒但位元組完全正確不是
correctness 失敗）。見 §10。

---

## 5. Cache state：記憶體 + 磁碟，`unknown` 是合法狀態

`/rpc/forget` **只呼叫 `TeleDriveClient.invalidate()`**，而它只清 `_dir_cache`
與 `<cache_dir>/dirs/*.json`。它**不清**縮圖、屬性、zip 索引。

而且——**只看磁碟檔存不存在仍然判不出 cold**：

| store | 磁碟 | 記憶體 |
|---|---|---|
| zip 索引 | `<cache_dir>/zips/`（`ShardedJsonStore`） | `ShardedJsonStore._memory`、`Resolver._zips` 的 `ZipView`、`ZipView._root` memo |
| 屬性 | `<cache_dir>/media_props.json`（`JsonStore`） | `JsonStore` 的 in-memory dict |
| 縮圖 | `<cache_dir>/thumbs/` | — |
| 檔頭 | `<cache_dir>/heads/` | — |
| listing | `<cache_dir>/dirs/` | `TeleDriveClient._dir_cache` |

一個已經跑了一天的 bridge，`Resolver._zips` 裡可能已經有那個 `ZipView` 且
`_root` 已 memo——**磁碟上沒有索引檔也照樣是暖的**。rev 2 用檔案存在與否判定，
會把 warm 判成 cold，然後給出一個漂亮又錯誤的「冷開 0.08 秒」。

所以改由 bridge 回答，**而且 audit 傳路徑，不傳 key**：

```
GET /rpc/cache-state?path=H:\...\foo.zip&kinds=zip,thumb,props
→ {"zip": {"memory": true, "disk": true},
   "thumb": {"memory": false, "disk": false},
   ...}
```

bridge 自己走 `dav_path_from_windows(path)` → `resolve(segments) -> Loc` →
`loc.entry` → 檢查記憶體與磁碟。（`Resolver.resolve()` 收的是路徑**段落串列**、
回的是 `Loc` 不是 `Entry`；解不出 entry 要明確回 4xx，不要回一個看起來像
「沒快取」的答案。）

**實作要落在薄層 `bridge.py`，不是 `_bridge_legacy.py`。** 這條 branch 的
cache identity 只有薄層知道：`_cache_key()` 與 `_thumb_path()` **各自**呼叫
`_fresh_parts()` → `api.current_parts(entry)`，也就是各打一次 backend。
照 legacy 那邊直覺實作，一個 `/rpc/cache-state` 回應會打兩三次 backend，
而且**剛好在 storage migration 發生時，同一個回應裡會混到兩代 physical
generation**。正確作法是一次算完：

```python
# bridge.py（薄層）
parts = self._fresh_parts(entry)
key = _physical_set_key(parts)
# zip / thumb / props 全部用這同一代 parts 判斷
```

`_bridge_legacy.py` 的 `RpcApp._cache_state()` 只負責 HTTP 與路徑解析。

**`key in Resolver._zips` 不等於 warm。** 列 `/game` 本身就會建立 `ZipView`
（`Loc(ZIPDIR, node=None)`），但那個 view 的 `_root` 可能根本還沒 parse——
那正是「列表不打開封存」要保證的事。所以 zip 的 memory 判定是
**`view._root is not None`**，或 `_zip_cache` 的記憶體命中；光有 view 不算。

**audit 不可以自己算 key。** 這條 branch 的 cache key 不再是
`telegram_user_id-file_id`，而是：

```
current backend physical location
  → current_parts(entry)
  → physical_location_key(part.location)
  → ordered set hash
  → loc3-<sha256>
```

而且 `_cache_key(entry)` **每次都會重新向 backend 取得目前的 physical row**。
client 手上算出來的 key 因此隨時可能跟 bridge 此刻認定的 authoritative
physical location 不一致——而「physical location 會更新」正是這條 branch
存在的理由之一。用一個過期的 key 去問快取狀態，答案會是
「沒快取」，audit 於是把一個暖的東西當成冷的量，**這正是本節要防的那個錯誤，
只是換了一條路徑發生**。

Python 端據此推出 `cold` / `warm` / `unknown`：

| 快取 | cold 的條件 | 何時 unknown |
|---|---|---|
| `api_metadata` | `/rpc/forget` 之後 | — 不經 `/rpc/cache-state`：`/rpc/forget` 之後它必然 cold |
| `zip_index` | memory 與 disk 皆 false | — |
| `thumb_cache` | 同上 | — |
| `props_cache` | 同上 | — |
| `rclone_vfs` | `rclone rc vfs/forget` **只清 dir cache，不清已快取的位元組** | **多數情況 unknown**，除非刪 `<cache_dir>/rclone/vfs/` 對應檔 |
| `windows_thumb` | 只有全新的資料夾路徑能確定 | **既有資料夾一律 unknown** |

```json
"cache_state": {
  "api_metadata": "cold", "zip_index": "preexisting",
  "thumb_cache": "cold", "props_cache": "warm",
  "rclone_vfs": "unknown", "windows_thumb": "unknown"
}
```

**冷門檻只對 `cold` 套用**；`preexisting` / `unknown` 記錄實測值、標
`NOT_MEASURED`、並說明原因。「這個 zip 的索引昨天就在了」比一個漂亮的
0.08 秒有用得多。

---

## 6. Counter，以及它為什麼不能是 log 行數

bridge 對 `telethon.client.downloads` 掛了 `ThrottleRepeats`：相同 template
60 秒內只放一行過。所以「屬性階段 `iter_download` 行數 == 0」有乾淨的假通過：

```
T-5s  某個 iter_download 被記錄（template 配額用掉）
T+0s  props 量測開始
T+1s  props 錯誤地下載原檔  →  同 template，被 suppress
T+3s  讀新增區段  →  0 行  →  PASS
```

**log 適合找 positive evidence；被 suppress 的 log 不能當 negative evidence。**
那條 throttle 是這個專案自己為了讓 `bridge.log` 可讀而加的，rev 1 又設計了
一個被它打敗的檢查。

### 6.1 Origin 必須顯式傳遞

rev 2 的 `source="rpc"|"sweep"` 不夠：位元組還可能來自 DAV read、zip 索引讀取、
`fetch-local`、縮圖預抓。而且**所有請求共用同一個 asyncio worker loop，
不能靠 thread-local 猜來源**。

改成顯式傳一個 origin 到 `tgio` 的兩個 `iter_download` 呼叫點：

```
props | thumb | thumb_prefetch | dav_read | zip_index | fetch_local | warmup | head | unknown
```

**但 origin 的起點不是 `tgio`，是 request 的來源。** 真實的鏈是：

```
DAV read / fetch-local / zip / warmup head
        ↓
Resolver.open_remote(entry, *, origin)
        ↓
SeekableRemoteFile(..., origin)      ← 目前完全不知道 origin
        ↓
_fetch() → read_part(..., origin=self._origin)
```

只改 `tgio` 那一段的話，zip 的 central directory、`fetch-local`、warmup 的檔頭
**全部會被記成 `dav_read`**；縮圖那邊則是資料夾預抓與 sweep 全部被記成 `thumb`。
數字看起來很乾淨，但 §8.2 的「屬性階段沒讀位元組」以外的每一條都在問錯的桶。

**`ZipView` 要特別處理。** 它只有一個零參數的 `_open_stream` callback，同時服務
central directory 解析、member 的 DAV 讀取、以及 `fetch-local` 的 member 讀取。
把它綁成 `lambda: open_remote(entry, origin="zip_index")` 會讓**讀 zip 裡的檔案
也被算成索引讀取**。介面要改成收 origin：

```python
ZipView(lambda origin: resolver.open_remote(entry, origin=origin))

view.root                          → _open_stream("zip_index")
view.open(node)                    → _open_stream("dav_read")
view.open(node, origin="fetch_local")  → _open_stream("fetch_local")
```

**預設值一律是 `unknown`，不是 `dav_read`。** 一個沒更新到的 call site 冒充成
DAV 讀取，會落進最大的那個桶裡，是最難發現的一種錯標。連帶得到一個免費的
自我檢查：**量測窗內 `unknown` 非零就是 validity 失敗**——代表還有 call site
沒標到，而不是「沒有流量」。

```
GET /rpc/counters
→ {"download_requests_total": {"props": 0, "dav_read": 1284, ..., "unknown": 0},
   "download_bytes_total":    {"props": 0, "dav_read": 673185792, ...},
   "zip_open_attempts_total": 12,
   "zip_index_cache_misses_total": 3,
   "thumb_requests_total": 8401,
   "props_requests_total": 3120}
```

**origin 是參數，不是推斷。** 這會讓呼叫鏈上每一層都要帶著它——
那是刻意的成本：推斷出來的來源在出錯時不會報錯，只會給出一個可信的錯誤答案。

### 6.1a request 與 bytes 要分開記，而且記在 attempt 上

**不可以「成功回傳之後才記一筆」。** 那樣 retry、部分下載、以及失敗的 attempt
全部不算，於是：

- §8.4 的 idle 靜止判定會**假通過**——閒置窗內有失敗的重試，counter 卻沒動
- `--sustain-max-bytes` 會**低估真正燒掉的 Telegram 額度**

所以拆成兩個動作：

```
每次 iter_download attempt 一開始       → record_request(origin)
每個 yield 回來的 chunk 立刻            → record_bytes(origin, len(chunk))
```

連帶語意要寫進註解：`download_requests_total` 是 **wire attempt 次數**，
不是邏輯讀取次數（retry 會重複計）。這對「花掉多少額度」是對的讀法，
拿它當讀取次數用就是錯的。

### 6.1b named counter 必須真的被 bump

`zip_open_attempts_total` / `zip_index_cache_misses_total` /
`thumb_requests_total` / `props_requests_total` 只定義不接線的話會**永遠是 0**，
而 §8.3 那條「列 `/game` 期間 `zip_open_attempts_total` 增量 == 0」就會
**無條件通過**——這份 spec 最在意的那個 bug 因此永遠測不出來。接線點：

| counter | bump 的位置 |
|---|---|
| `zip_open_attempts_total` | `ZipView.root()` 被請求時，**不管答案從哪來** |
| `zip_index_cache_misses_total` | 同上，但只在真的要去解析 central directory 時 |
| `thumb_requests_total` | `RpcApp._thumb` 入口 |
| `props_requests_total` | `RpcApp._props` 入口 |

### 6.2 併發污染

`before == after` 在 sweep 同時跑時必然假失敗。

**但 origin 沒辦法把 sweep 完全隔開，這一點要說死。** `Warmer.fill()` 在
process 內直接發的讀取可以標成 `warmup`，**而 `shell_warm()` 不行**——它是叫
Windows 的 shell 去要縮圖，shell 於是透過 `H:` 回來打 bridge，那些請求抵達時
只知道自己是 `dav_read` / `thumb` / `props`，**沒有任何地方記得它們源自 sweep**。
`Warmer.fill()` 觸發的 shell 讀取（第 4 節的檔頭）也一樣。

所以：

- **`warmup` 桶只代表「sweep 在 process 內直接發出的 I/O」**，不是「sweep 造成的
  全部流量」。
- **量測窗只要與 `BackgroundWarmup.active` 重疊，整個 op 就判 `NOT_MEASURED`**，
  不要試圖用扣掉 `warmup` 桶的方式救它——扣不乾淨，而扣得不乾淨的結果是一個
  看起來精確的錯誤數字。

`warmup.active` 因此不只是報告欄位，是 validity 的一部分。

---

## 7. Discovery

**Cross-DC / photo — review 指出這比 rev 2 說的便宜。** backend 的 `FileInfo`
已經回 `telegram_media_kind` / `telegram_chat_id` / `telegram_media_id` /
`telegram_media_size`，只是 `_to_entry()` 把它們丟掉了（目前 `Entry` 只取
13 個欄位）。本 branch 的 parity 層已經在 `parse_file_location` 消費這些欄位，
所以把 `telegram_media_kind` 與 `telegram_chat_id` 納入 `Entry` 是自然的延伸。

於是：

- `telegram_media_kind == "photo"` 佔比高 → **chat import 候選，零額外 Telegram 呼叫**
- 要證明**真的跨 DC**才需要查 Telegram 的 `dc_id`，那一步 `--probe-crossdc`
  才做，預設關
- `--sample-crossdc <H: 路徑>` 永遠可以明確指定

**Zip 樣本**：rev 2 同時要求「依 entry 數挑」與「不可開任何 zip」，而不讀
central directory 就不知道 entry 數。改成：

- `<cache_dir>/zips/` 已有索引 → 可依 entry 數挑（並標該 zip `zip_index: preexisting`）
- 沒有索引 → 依封存的邏輯大小 / part 數挑，backend 給得出

**Bounded discovery**：遞迴走完整棵 metadata tree 每層 2 個往返，可能非常昂貴。

```
--discovery-max-folders 200
--discovery-max-seconds 30
```

超過就從目前最佳候選挑，並記下 discovery 是否被截斷。
**不要為了開始 benchmark 先 benchmark 半小時。**

找不到某一類就明確跳過並在報告說明。**「沒測到」要說出來。**

---

## 8. 三個結構類別與壓力情境

### 8.1 類別 A — 小檔（單一 message，非 split）

先由 Python 端組 **manifest**：從樣本資料夾挑出符合條件的靜態圖
（`IMAGE_EXTS`、非 split、`has_thumbnail == true`），寫成檔案交給
`isolate --manifest`。**不要讓 `isolate` 自己掃前 N 個檔**——混到
`.txt` / `.zip` / 影片時 `answered == N` 與 `delegating == 0` 是錯誤的門檻。

冷列舉 → `isolate --jsonl thumb` → `isolate --jsonl props` → `bench` →
抽樣整檔讀回比對 → 立刻重跑 thumb（暖）。

步驟 2、3 **必須分開跑**：CLAUDE.md §4 就是靠分開量才發現「只做縮圖 13.02 秒
且 8 個檔全被讀，只做屬性 0.81 秒且一個都沒讀」。合併量的話 `bench` 的總時間
說不出是哪一條在拖。

| 層 | 指標 | 判準 |
|---|---|---|
| Validity | DLL log 的 `GetThumbnail` 行數 | `== manifest 長度`，否則 `NOT_MEASURED` |
| Validity | DLL log 的 props `Initialize` 行數與目標路徑 | 同上（rev 2 漏了這半） |
| Validity | `thumb_requests_total` / `props_requests_total` 增量 | 皆 > 0 |
| Functional | `delegating` 次數 | `== 0` |
| Functional | thumb answered | `== manifest 長度` |
| Functional | props 回報 dimensions 的數量 | `== manifest 長度` |
| Functional | SHA256 | 全中 |
| UX | 單張 thumb `max` | budget `2 s`；`>= 5 s` 為 `UX_STALL` |

### 8.2 類別 B — 大檔（split，多 part）

樣本優先挑各 part `telegram_user_id` **不同**的（跨帳號 split 讀得回來
唯一的實證）。

1. 冷列舉
2. **屬性**：這一段不可以下載任何位元組，寬高／duration 該來自 Telegram
   document attributes。**但只檢查 `props` 那個桶是不夠的**——真正的失敗長這樣：

   ```
   property 操作 → DLL 的 /rpc/props 失敗 → delegate
                → Windows 自己去開 H: 上的原檔 → WebDAV Range
                → origin = dav_read
   ```

   於是 `download_bytes{props} == 0` **照樣成立**，而那正是「shell 為了拿寬高
   去讀整張原圖」——這條檢查存在的全部理由。所以判定的是**整個量測窗的前景
   位元組**：`props`、`dav_read`、`unknown` 三個增量都必須是 0。
   `props` 那個桶保留，用來定位 `/rpc/props` 自己的 regression，
   **但不單獨當 correctness gate**。（`warmup` 桶不列入——sweep 活躍時
   依既有規則整個 op 判 `NOT_MEASURED`。）
3. **seek 三處**各讀 1 MiB：頭、**刻意跨 part 邊界的中點**、**尾**
4. 起播模擬：只讀頭 256 KB
5. 整檔 SHA256 — `--full-hash` 才做

**跨界檢查需要 oracle（rev 2 沒有）。** part 表只有 message / offset / size，
**沒有「這一段正確的位元組是什麼」**，所以「與 part 表對得上」是一句沒有
判定方法的話。改成：

```
current_parts(entry)                    ← authoritative physical parts
        ↓
對邊界左右兩側，分別直接讀 part N 的尾端與 part N+1 的開頭
（各自走該 part 自己的 FileLocation，繞過 split 串接層）
        ↓
拼成期望的 1 MiB
        ↓
與從 H: 讀同一個 logical range 的結果逐位元組比對
```

**oracle 繞過的只有 split 串接，不是 physical identity。** 這條 branch 的
authoritative location 可能是 channel 或 photo 型的 `FileLocation`，
所以 oracle 必須從 `current_parts(entry)` 拿到當下的 physical part，
再逐 part 直接讀——**退回「對 Saved Messages 的 message id 發請求」會讓
oracle 自己讀錯檔案**，然後把一個正確的實作判成失敗。

這樣 oracle 與被測路徑是兩條獨立的程式路徑，差異才有意義。

**尾端那 1 MiB 是這一類最重要的單一檢查。** 後端 `filesize` 以 512 KB 為單位
進位（實測多報 523,424 bytes），真實長度在 `file_hash` 的 `:<n>` 後綴。
`_clip_parts` 若失效，尾端會等一段長 timeout 然後拿到 **0 bytes**——
那正是非 faststart MP4 無法起播的成因，在檔案總覽上完全看不出來。

| 層 | 指標 | 判準 |
|---|---|---|
| Functional | 屬性階段 `download_bytes` 的 `props` **＋ `dav_read` ＋ `unknown`** 增量 | 三者皆 `== 0`，見上 |
| Functional | 尾端 1 MiB | 讀到 1 MiB 真實資料，非 0、非短讀 |
| Functional | 跨界 1 MiB | 與 oracle 逐位元組相符 |
| UX | 任一 seek | budget `3 s`；`>= 5 s` 為 `UX_STALL` |
| UX | 起播（頭 256 KB） | budget `3 s` |

### 8.3 類別 C — `/game` 的 zip

1. **列 `/game` 本身**（`/rpc/forget` 後的冷值）
2. 進第一個 zip → 列虛擬樹 → 列第二、三層
3. 開裡面一個檔 → 與 central directory 的 **CRC32** 對照
   （巡檢半沒有本機原檔；CRC32 是 zip 自帶、唯一可離線驗證的真值。
   上傳往返半才用本機素材的 SHA256）
4. **第二次進同一個 zip**
5. **完整虛擬目錄取回**（見下）

**步驟 5 的語意，rev 2 寫錯了。** 產品行為不是「下載整個 `.zip` 再解壓」，
而是 walk 虛擬樹、**每個 member 各做 range read、直接寫進 `local_dir`**。
所以這一項測的是**完整虛擬目錄取回**，判定用**每一個 entry 的 CRC32 與大小**，
不是「解壓出來的 zip 對不對」。

只在封存 ≤ `--fetch-local-max-bytes`（預設 200 MB）時做，否則記 `skipped`。

**`preexisting` 的影響範圍（rev 2 寫反了一部分）**：zip 索引是暖的，
只影響**該 zip 第一次開啟的 cold-open 量測**。它**不影響**步驟 1
（列 `/game` 跟某個 zip 的索引暖不暖無關），也**不影響**步驟 4
（那本來就是暖的第二次開啟）。

**`zip_open_attempts_total` 與 `zip_index_cache_misses_total` 要分開。**
rev 2 只有後者，於是若 bug 又變成「列 `/game` 對每個 zip 呼叫 lookup」但
索引全部已暖，remote read 是 0，**counter 仍然 0，bug 隱形**。
前者在 `ZipView.root()` 被請求時就 +1（不管答案從哪來），**列 `/game` 期間
它必須是 0**——因為列表根本不該問任何一個封存的樹長什麼樣子。

| 層 | 指標 | 判準 |
|---|---|---|
| Validity | `zip_index` 狀態 | `preexisting` → **只有**步驟 2 的 cold-open `NOT_MEASURED` |
| Functional | 列 `/game` 期間 `zip_open_attempts_total` 增量 | **`== 0`**。這比時間更早發現「列表打開每個封存」 |
| Functional | 步驟 3 的 CRC32 | 相符 |
| Functional | 步驟 5 每個 entry 的 CRC32 與大小 | 全中 |
| UX | 列 `/game` | budget `1 s` |
| UX | 第一次進 zip | budget `5 s` |
| UX | 第二次進同一個 zip | budget `0.1 s` |

### 8.4 壓力情境

**閒置後重訪（`--idle-seconds`，預設 90）** — 等 90 秒（Telethon 的
`_DISCONNECT_EXPORTED_AFTER` 是 60，要確定跨過），再重跑同一批縮圖。
這是唯一能抓到 exported sender 那條坑的方法：實測一份 `bridge.log` 有 248 次
`Disconnecting borrowed sender for DC 1`、387 次重連、138 次 `Server closed`。

**「等了 90 秒」不等於「idle 了 90 秒」（rev 2 漏了）。** `BackgroundWarmup`
或任何一個 Explorer 視窗只要中途碰 Telegram，exported sender 就沒有真正閒置。
判定改成：**idle window 前後 `download_requests_total` 全維度都不變**；
中途有任何活動就 `NOT_MEASURED`。只在樣本含跨 DC 檔案時執行。

**冷 COM surrogate（`--cold-surrogate`，預設關）** — 專打 `Settings` race
（只在冷載入的頭幾百微秒發作），順帶保證新 surrogate 讀得到新設的 `LogPath`。

**只殺載入了本 DLL 的 `dllhost.exe`（rev 2 的 `taskkill /f /im dllhost.exe`
會殺全機所有 COM surrogate）。** 用模組清單找出載入 `TeleDriveThumb.dll` 的
PID，只殺那些；一個都找不到就說「沒有需要殺的 surrogate」而不是照殺。

**持續負載（`--sustain-minutes`，預設 10）** — 兩條 lane：

```
背景負載產生器                            前景 UX probe
持續讀「未快取、不重複」的               每分鐘跑一次固定的
byte range，**直接打 bridge HTTP**        shell workload，走 H:
        │                                        ▲
        └──────▶ Telegram 連線池 ◀───────────────┘
```

**背景 lane 必須走 bridge 的 HTTP Range，不能走 `H:`**（rev 2 沒說清楚）——
走 `H:` 會被 rclone 的 VFS 快取吃掉，於是又回到 rev 1 那個「以為在壓 Telegram，
其實在壓快取」的問題。前景 lane 才走 `H:`，因為要量的就是 Explorer 的體感。

**`--sustain-max-bytes` 依 `download_bytes_total` 的實際增量停止**，
不是依 requested bytes：一個 64 KiB 的讀取會因為 512 KiB block 對齊而
實際抓更多，照 requested 算會嚴重低估真正燒掉的 Telegram 額度。

背景產生器自己撞到 FLOOD_WAIT **不自動算 finding**——那會讓前景因為
「不是 bug 的原因」而 fail。記成這次 run 的條件（`"background_flood_wait": true`），
讓讀報告的人判斷。

**sweep 併發（`--with-sweep`，預設關）** — warmup 的間隔可能是數小時，
只讀 status 可能一整天都碰不到 `active`。**接受這個情境經常 `NOT_MEASURED`**，
並在報告附上 `warmup.next_run_at` 讓人自己排時間。

> **不加「觸發 warmup 一次」的端點。** 那會直接違反 §3.0 的唯讀不變量，
> 而這個情境的價值不足以換掉那條不變量。

---

## 9. 上傳往返半

### 9.1 素材

刻意壓到最小。測試目的沒有一項需要大檔：

| 案例 | 內容 | 打的是 |
|---|---|---|
| `album-11` | 11 張 64 KB JPEG | album 湊滿 10 就送 + 尾巴 flush |
| `nonascii` | 1 張 64 KB JPEG，檔名含中文與假名 | URL 跳脫那條坑 |
| `png` | 1 張 64 KB PNG | `make_preview` 的非 JPEG 路徑 |
| `boundary` | 1 個 10 MiB + 1 | `decide_protocol` 的 small/big 分界 |
| `gamezip` | 三層深、8 個小檔的資料夾寫進 `H:\game\` | `/game` 打包 → zip 虛擬樹 |
| `split` | 1 個 500 MiB + 1 | **`--include-split` 才跑**，預設關 |

預設總計約 **11 MB**。

### 9.2 流程與「完成」的定義

Preflight → 建 `H:\_roundtrip-<時間戳>\`（全新名字，`windows_thumb` 因此是
**確定的 cold**，§5 裡少數能確定的一格）→ 寫入 → **等待完成** →
`/rpc/forget` → 跑 §8 的三類 → 清理。

**「等 `uploads` 排空」不夠（rev 2 的定義是壞的）。** `/game` 走 `GameStager`，
先 debounce、再 `packing`、再 `uploading`，而 `/rpc/status` 的 `uploads`
是**一般路徑**的佇列。一般佇列空掉時 `gamezip` 可能連打包都還沒開始。

完成的定義是**兩個條件同時成立**：

1. `/rpc/status` 的 `uploads` 為空
2. `/rpc/status` 的 `/game` `units` 裡，這次建立的那個 unit 已完成或消失

再加上 backend 真的查得到 row 且 `has_thumbnail` 正確（圖 `True`、zip `False`）。

### 9.3 清理

`TeleDriveClient.trash(file_id)` 是 `DELETE /files/{file_id}`，backend 在整棵
子樹蓋 `trashed_at`，Telegram 訊息不動，正常 listing 預設排除 trashed row。

```
自動清：
  - 本機素材 temp dir
  - POST /rpc/forget
  - rclone rc vfs/forget dir=<folder> ＋ 刪 <cache_dir>/rclone/vfs/ 對應檔
  - HKCU\...\LogPath 還原（含「原本就沒有這個值」的情況）
  - probe 子樹          → trash(<probe folder root_id>)
  - /game/<name>.zip    → trash(<那一筆>)   ← 它不在 probe 子樹底下

永久 residue：
  - Telegram 訊息（送出即永久，唯一真的清不掉的）
```

**`gamezip` 打包後是 `/game/<name>.zip`，不在 probe 資料夾底下**，
trash 根目錄清不到，必須單獨一筆。

**去重的疑問已解，但有一個 review 沒展開的後果。** review 核對過 backend 的
hash lookup SQL 帶 `trashed_at IS NULL`，所以 trashed row 不會被 dedup 命中。
連帶結果：**trash 會打掉 `--reuse-folder` 的「零新增位元組」性質**
（下一次跑會真的重傳）。所以 `--reuse-folder` 與清理必須互斥，見 §9.4。

**`--purge` 是 opt-in，不是預設。** review 指出 backend 另有
`DELETE /files/{id}/purge`（只永久刪 metadata，不碰 Telegram）。
**這個 repo 驗不到那個端點**——沒有對應的 client 方法，端點在 backend repo。
所以：預設只 trash；`--purge` 才進一步永久刪；**preflight 探測該端點，
不存在就退回 trash 並說明**。一個 audit 工具預設永久刪 metadata 是壞的預設。

`uploads/` 若有殘留**不刪**——上傳失敗時那是唯一副本。報告點名，交給人決定。

```json
"remote_residue": {
  "backend_visible_rows": 0,
  "backend_trashed_rows": 14,
  "purged": false,
  "telegram_messages": [{"name": "...", "message_id": 12345, "file_id": "..."}]
}
```

### 9.4 `--reuse-folder <name>`

固定資料夾重複跑，同名覆寫命中去重，第二次起零新增位元組，
當長期 regression fixture 用。

**`--reuse-folder` 蘊含不 trash、不 purge，且與 `--purge` 互斥**
（trash 過的 row 不會被 dedup 命中，零位元組的前提就沒了）。
代價是 `windows_thumb` 已暖，所有冷門檻 `NOT_MEASURED`。

---

## 10. Severity

```
FAIL          functional 錯了（bytes 不對、覆蓋不完整、answered 不足、CRC 不符）
UX_STALL      >= 5 s，使用者感知得到的停頓
SLOW          超過該 subsystem 的 budget，但 < 5 s
OK            在 budget 內
NOT_MEASURED  validity 不成立（§4）
```

**時間不進 functional 判定。** rev 2 把 zip 冷開的 8 秒叫做「functional budget」，
那仍然混淆了兩件事——9 秒但位元組完全正確**不是** correctness 失敗。
correctness 只管內容；時間只有 `OK` / `SLOW` / `UX_STALL`。

一筆量測可以同時是 functional `OK` 與 `UX_STALL`：類別 C 冷開 zip 6.5 秒
正是這一格。**這不是雙重標準，是兩個不同的問題**——把它們壓成一個布林
會讓其中一個永遠被隱藏。

### 10.1 `warm/cold` ratio 是 diagnostic，不是 gate

rev 1 把 `>= 20x` 當硬門檻。兩個反例足以推翻：

| | cold | warm | ratio | 實際體驗 |
|---|---|---|---|---|
| 最佳化之後 | 180 ms | 25 ms | 7.2× | 很好，卻判 FAIL |
| 冷路徑很糟 | 10 s | 0.4 s | 25× | 很差，卻判 PASS |

ratio 保留在報告，用來判斷**快取有沒有產生效果**（接近 1 就是沒生效，
那是值得知道的事實），但不單獨產生 finding。**user-facing gate 一律是絕對延遲。**

### 10.2 統計量

**hard gate 一律用 `max`。** p95 只有在 **N ≥ 20** 時才報告；
少於 20 就輸出 `"p95": null, "p95_note": "insufficient samples (n=8)"`。
3 次或 8 次的「p95」實際上就是 `max`，掛一個統計學的名字只會讓人以為
它比實際更穩健。

### 10.3 門檻表

| 面向 | UX budget | Functional |
|---|---|---|
| **任何單一操作** | **`< 5 s`**（超過 = `UX_STALL`） | — |
| `delegating` | — | `== 0` |
| `Server closed the connection` | — | `== 0` |
| 開已快取資料夾 | `< 0.05 s` | — |
| 開未快取資料夾 | `< 1 s`（max） | — |
| 單張縮圖 | `< 2 s`（max） | answered == manifest 長度 |
| 屬性 | — | dimensions == manifest 長度 |
| 列 `/game` | `< 1 s` | `zip_open_attempts 增量 == 0` |
| 第一次進 zip | `< 5 s` | — |
| 第二次進同一個 zip | `< 0.1 s` | — |
| 大檔任意 seek | `< 3 s` | — |
| 大檔屬性階段 | — | `download_bytes` 的 `props` ＋ `dav_read` ＋ `unknown` 增量皆 `== 0`（§8.2；只看 `props` 會被 delegate 之後的原檔讀取繞過） |
| 大檔尾端 1 MiB | — | 讀到真實資料 |
| 大檔跨界 1 MiB | — | 與 oracle 逐位元組相符 |
| 虛擬目錄完整取回 | — | 每個 entry 的 CRC32 與大小全中 |
| 前景延遲在持續負載下的衰退 | `< 30%` | — |
| SHA256 / CRC32 | — | 相符 |
| `warm/cold` ratio | *diagnostic only* | *diagnostic only* |

門檻放在 `_liveprobe.py` 的 dataclass，`--thresholds <json>` 可覆寫，
報告記下實際用的是哪一組。

---

## 11. Preflight

**量出一個假數字比不量更糟。** 任一不成立就 `SystemExit(2)`：

| 檢查 | 怎麼查 | 不過的訊息 |
|---|---|---|
| bridge 活著 | `GET /rpc/health` | 先跑 `start.bat` |
| `cryptg` | `/rpc/health` 的新欄位 | 少了它解密把下載壓在 ~0.15 MiB/s，量什麼都沒意義 |
| `H:` 掛著 | `cfg.mount_drive` 存在 | 沒掛載時 `SHCreateItemFromParsingName` 微秒級失敗，log 上跟「handler 答錯了」一模一樣 |
| rclone rc 通 | `POST 127.0.0.1:5572/rc/noop` | 少了 `--rc-no-auth` 會回 `403 authentication must be set up` |
| **`isolate --jsonl` 輸出合法 UTF-8 JSONL** | 對一個已知的非 ASCII 檔名跑一次，解析回來比對 | 這就是 C++ 那半的 harness（§3.0）。先跑 `shellthumb\buildbench.bat` |
| **DLL 的 LogPath 會重讀** | 設 `LogPath` → 等 2 秒 → probe 一個已知檔案 → 看有沒有出現記錄 | 同上。先 `shellthumb\build.bat`（需先殺載入本 DLL 的 dllhost） |
| `/rpc/counters` 與 `/rpc/cache-state` 存在 | `GET` | bridge 是舊版，先 `restart.bat` |
| `/files/{id}/purge` 是否存在 | 僅在 `--purge` 時探測 | 不存在就退回 trash 並說明（§9.3） |
| DLL 已註冊 | HKCU 的 ProgID 與 `SystemFileAssociations` | 先跑 `install_thumb.py` |
| `bridge.log` 讀得到 | `cfg.cache_dir / "bridge.log"` | 沒有 log 就沒有 positive evidence |

---

## 12. 報告

```json
{
  "generated": "2026-09-19T15:30:12",
  "mount": "H:",
  "target_tree": {"branch": "...", "head": "aa59943"},
  "summary": {"ok": 11, "slow": 2, "ux_stall": 1, "fail": 0, "not_measured": 3},
  "cache_state": {
    "api_metadata": "cold", "zip_index": "preexisting",
    "thumb_cache": "cold", "props_cache": "warm",
    "rclone_vfs": "unknown", "windows_thumb": "unknown"
  },
  "discovery": {"truncated": false, "folders_scanned": 87, "seconds": 12.4},
  "samples": {"small": "...", "big": "...", "zip": "...", "crossdc": null},
  "operations": [
    {"op": "class_A.thumb_cold", "validity": "valid",
     "metrics": {"max_s": 7.4, "p95_s": null,
                 "p95_note": "insufficient samples (n=12)"},
     "verdicts": {"functional": "OK", "ux": "UX_STALL"}},
    {"op": "class_B.props_no_bytes", "validity": "not_measured",
     "why": "BackgroundWarmup was active during the window"}
  ],
  "counters": {"before": {}, "after": {}},
  "stress": {
    "idle_revisit": {"validity": "not_measured",
                     "why": "download_requests moved during the idle window"},
    "sustain": {"background_flood_wait": false,
                "background_bytes": 2147483648}
  },
  "warmup": {"active_during": {"class_B_props": false},
             "next_run_at": "2026-09-20T02:00:00"},
  "findings": []
}
```

### 12.1 `means` 的紀律

rev 2 的範例把 `thumb_max=7.4s` 標成「shell 讀了整張原圖」。
**那個數字本身推不出那個結論。**

> **因果解讀只有在同時看到 `delegating`，或看到明確的 byte evidence
> （`download_bytes{origin}` 的增量與原圖大小相當）時才寫。**
> 否則 `means` 留空，只報「慢」。

一個可信度很高但其實是猜的因果，比沒有解讀更糟——它會讓人去修錯的東西。

### 12.2 Exit code

`0` 全過、`1` 有 finding、`2` preflight 失敗、
**`3` 有 `NOT_MEASURED` 但沒有 finding**。
「跑了但有些沒量到」必須跟「跑了而且都好」分開，
否則 §4 的整個 validity 模型會被一個 `exit 0` 抹平。

---

## 13. 檔案清單

**新增**

| 檔案 | 內容 |
|---|---|
| `scripts/_liveprobe.py` | `ShellDriver`（JSONL + manifest）／`DllLog`／`BridgeLog`／`Counters`／`CacheState`／`Validity`／`Severity`／`Report` |
| `scripts/live_browse_audit.py` | 唯讀巡檢 |
| `scripts/live_shell_roundtrip.py` | 上傳往返 |
| `tests/test_liveprobe.py` | §14 |
| `tests/live/test_shell_roundtrip.py` | opt-in 包裝 |

**修改（唯讀 diagnostics，不改行為）** — symbol 在兩棵樹的落點不同：

| Symbol / 改動 | `master` | `feat/current-backend-storage-parity` | 收尾 |
|---|---|---|---|
| `isolate` 的 `--jsonl` / `--manifest` | `shellthumb/isolate.cpp` | 同左 | `shellthumb\buildbench.bat` |
| `Log()` 的 SRWLOCK + TTL | `shellthumb/TeleDriveThumb.cpp` | 同左 | `shellthumb\build.bat`（先殺載入本 DLL 的 dllhost） |
| `RpcApp._health` / `_status`／新增 `/rpc/counters`、`/rpc/cache-state`／`RpcApp` 收 `BackgroundWarmup` | `bridge.py` | `_bridge_legacy.py` | `pytest tests -q` → `restart.bat` |
| 記帳點：`_thumbnail_bytes()` / `_chunk()` 的兩個 `iter_download` | `tgio.py` | `_tgio_legacy.py` | 同上 |
| origin 的傳遞：**`strict_routing.py` 最後把 `tgio.read_part` 與 `tgio._legacy.read_part` 都重新綁到自己**，所以那是 live 讀取真正的 seam；再加上薄層的 canonical 入口、`Resolver` 的 monkey-patch、`ZipView`、`fetchlocal`、`warmup` | `tgio.py` | **`strict_routing.py` ＋ `tgio.py` ＋ `_tgio_legacy.py` ＋ `bridge.py` ＋ `_bridge_legacy.py` ＋ `zipfs.py` ＋ `fetchlocal.py` ＋ `warmup.py`** | 同上 |
| `Entry` / `_to_entry()` 納入 `telegram_media_kind`、`telegram_chat_id` | `tdapi.py` | `_tdapi_legacy.py`（本 branch 的 parity 層已消費這些欄位） | 同上 |
| `BackgroundWarmup.status()` | `warmup.py` | 同左 | 同上 |
| 「測試」節加這兩支；手動清單標註哪幾項有腳本代跑 | `CLAUDE.md` | 同左 | — |

**實作前要先確定落在哪一棵樹上。** 兩棵的 symbol 相同、檔名不同；
`d0f63b2` 之後的薄層架構若會 merge，就直接以 branch 為準。

---

## 14. 測試

`tests/test_liveprobe.py`（進 `pytest tests -q`）：

- `ShellDriver` 解析 JSONL：含非 ASCII 路徑、**逾時被 kill 的殘缺輸出仍說得出做到哪一張**
- `ShellDriver` 產生的 manifest 只含符合條件的靜態圖
- `DllLog` 解析 thumb 與 props 兩種記錄；**還原「原本沒有這個登錄值」的情況**
- `BridgeLog` 只讀新增區段
- `Counters` 的**每個 origin 各自**差值計算
- `CacheState` 的 `cold` / `warm` / `preexisting` / `unknown` 判定，
  **包含「磁碟沒有但記憶體有 → warm」**
- **`Validity` 不成立時 metrics 不進 threshold evaluator**，且**是 per-operation
  的，一個 op 失效不影響其他 op**（§4 的核心不變量，最容易寫錯的一條）
- `Severity` 三級，特別是**同一筆同時 functional `OK` 與 `UX_STALL`**
- `warm/cold` ratio **不**產生 finding
- **p95 在 N < 20 時輸出 `null` 加說明，不輸出一個數字**
- **`means` 在沒有 `delegating` 也沒有 byte evidence 時留空**
- 報告不含憑證

diagnostics 側（§3.4）：

- 每個 counter 的離線測試：「做了 N 次讀取，該 origin 的 counter 剛好加 N」
- `warmup.status()` 的狀態機
- `/rpc/cache-state` 對 memory-only 與 disk-only 兩種情況的回答

**C++ 那兩項沒有離線測試**，由 §11 的 preflight 每次執行前驗證（§3.0）。

**巡檢半整體只能靠真的跑一次**——這是它存在的全部理由。

---

## 15. 明確不做

- **不清 Windows 的 `thumbcache_*.db`**、**不重啟 `explorer.exe`**、
  **不殺 rclone、不卸載 `H:`**：影響 `H:` 以外的全機使用，而且開一個新資料夾
  就得到同樣乾淨的冷狀態。
- **不殺全部的 `dllhost.exe`**：只殺載入了本 DLL 的 PID（§8.4）。
- **不加「觸發 warmup」的端點**：違反唯讀不變量，價值不足以換（§8.4）。
- **不預設 purge**：opt-in，且端點存在與否要探測（§9.3）。
- **不刪 Telegram 訊息**：做不到。
- **不改任何資料路徑或產品行為**：只加唯讀 diagnostics（§3.0）。
- **不做 GUI、趨勢圖、歷史比較**：一次跑出一個判定就是全部的產出。
- **不量網頁端**：範圍是 `H:` 上的體驗。
