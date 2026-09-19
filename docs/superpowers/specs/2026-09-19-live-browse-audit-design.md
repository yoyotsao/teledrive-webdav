# 真實 `H:` 巡檢與上傳往返探測 — 設計

**日期：** 2026-09-19（rev 2，2026-09-19 依 review 修訂）

**狀態：** Proposed（rev 1 為 Needs revision，本版處理該次 review 的 10 項）

**目標倉庫：** `yoyotsao/teledrive-webdav`

---

## 0. rev 2 改了什麼

rev 1 有六個會造成**假通過**的缺陷，以及三項照目前 repo 根本寫不出來。共通的成因是
rev 1 假設「不動產品程式碼」，於是只能從外部觀察，而外部觀察在這個系統上不足以
分辨「沒量到」與「很快」。

| # | rev 1 的問題 | 本版 |
|---|---|---|
| 1 | `isolate.exe` 只有整批輸出，拿不到單張 max | §3.1 加 `--jsonl` 逐檔輸出 |
| 2 | `Log()` 每 host process 只讀一次 `LogPath`，動態設定對已載入的 surrogate 無效 | §3.2 改 DLL + preflight contract |
| 3 | `/rpc/forget` 只清 dir listing，被當成清光全部快取 | §5 每個快取分別建模，`unknown` 是合法狀態 |
| 4 | health 無 `cryptg`、status 無 sweep | §3.3 加 read-only diagnostics |
| 5 | 拿被 throttle 的 log 行數當「沒有下載」的反證 | §6 改用 process 內 counter，並處理併發污染 |
| 6 | sustain 重複瀏覽同一資料夾，幾輪後不再碰 Telegram | §8.3 拆成背景負載 + 前景 probe 兩條 lane |
| 7 | 宣稱 backend row 刪不掉 | **錯誤**。`tdapi.trash()` 存在，§9.3 改用它 |
| 8 | sample 選擇要求 backend 給不出的資訊，且自我矛盾 | §7 改成 bounded discovery + 可得資訊 |
| 9 | 全域 `<5s` 與 zip `<8s` 互相矛盾 | §10 三級 severity，UX 與 functional 分開 |
| 10 | `warm/cold >= 20x` 當硬門檻 | §10 降為 diagnostic |

**第 7 項是事實錯誤，要記下來。** rev 1 寫「backend 沒有刪除端點」，依據是 grep `tdapi.py`。
但 `tdapi.py` 是 storage-parity 薄層，開頭把 `_tdapi_legacy.py` 的 `vars()` 整份灌進
`globals()`；`trash()` 在 `_tdapi_legacy.py:819`，是 `DELETE /files/{file_id}`，
soft-delete 整棵子樹、Telegram 訊息不動，`tests/test_bridge_e2e.py:941` 已在使用。
**這個 repo 目前是 `tdapi.py` / `bridge.py` / `tgio.py` 三層薄殼疊在 `_*_legacy.py` 上，
任何「這個功能不存在」的判斷都必須連 legacy 一起看。**

---

## 1. 這份 spec 要回答的問題

> **「使用體驗可以跟本機硬碟一樣嗎？頂多開檔速度慢一點，但是不應該一直轉。」**

「一直轉」可以量測，但**不能用平均值量**。一個資料夾 100 張圖，99 張 40 ms、
1 張 30 秒，平均 340 ms 看起來很健康，而使用者只看到那 30 秒。所以全程用
`max` 與 `p95`，並把「單一操作 ≥ 5 秒」直接定義成使用者感知得到的 stall。

而這個問題**不能只靠自己新上傳的檔案回答**——理由見 §2。

---

## 2. 為什麼主角是唯讀巡檢

腳本自己剛上傳的資料夾是**整個 drive 上最幸運的樣本**：全是 `document` 型、
在 primary 帳號自己的 DC 上、剛註冊所以 `has_thumbnail` 與 `file_id` 都正確、
數量小、只有一層深。

而讓人一直轉的每一條坑都需要特定觸發條件，**新資料夾一條都不滿足**：

| 成因（皆出自 CLAUDE.md 的實測紀錄） | 觸發條件 | 新資料夾滿足嗎 |
|---|---|---|
| exported sender 60 秒計時器 → 8 條連線同時重連 → `Server closed the connection` | 跨 DC 檔案 ＋ >60 秒空檔 | ✗ 兩者皆無 |
| FLOOD_WAIT 累積 | 持續拉取數分鐘 | ✗ |
| backend keep-alive 斷線 → `/rpc/thumb` 500 → `delegating` 讀整檔 | 閒置數秒後再請求 | ✗ |
| DLL `Settings` magic-static race | handler 冷載入的頭幾百微秒 | ✗ |
| sweep 跟前景搶 | `BackgroundWarmup` 正在跑 | ✗ |
| 路徑解析每層 0.52 秒 | 深層路徑 + 未快取 | ✗ 只有一層 |
| 列 `/game` 打開每一個封存 | `/game` 底下有幾十上百個 zip | ✗ |
| chat import 的 `photo` 型 media | 該資料夾是 chat import 來的 | ✗ 結構上不可能 |

**上傳往返證明「新東西是對的」，唯讀巡檢證明「舊東西不會卡」。**

---

## 3. 產品程式碼的改動（rev 1 的「不動」已撤銷）

### 3.0 取代後的不變量

> **不改變任何資料路徑或產品行為；只允許新增唯讀的 diagnostics（逐檔 telemetry、
> counter、狀態欄位），且每一項都要有離線測試。**

代價要講清楚：**這支 audit 的正確性開始依賴它自己在測的程式碼**。一個記錯的 counter
會讓 audit 往「假通過」的方向錯，跟沒有 counter 一樣糟。防線是 §3.4。

### 3.1 `shellthumb/isolate.cpp` — 加 `--jsonl`

現況 `isolate.cpp:92` 是整批跑完才印一行 aggregate 到 stdout，沒有逐檔 latency、
沒有檔名、沒有 stderr 進度。rev 1 要的「單張 thumb max」與「逾時被 kill 也說得出
做到哪一張」，**原始資訊根本不存在**，不是 Python parser 能補的。

加 `--jsonl`，**每個檔一結束就往 stderr 印一行並 flush**，最後 stdout 仍印原本的
aggregate（人工使用不受影響）：

```
{"op":"thumb","file":"a.jpg","elapsed_ms":83,"answered":true,"hr":"0x00000000"}
{"op":"thumb","file":"b.jpg","elapsed_ms":7412,"answered":true,"hr":"0x00000000"}
```

**走 stderr 而不是 stdout，narrow UTF-8 而不是 `fwprintf`** —— 這兩點都是
`warmshell.cpp` 已經踩過的：寬字元輸出會被轉成 console codepage，而這裡的路徑
大半是非 ASCII，那正是「URL 跳脫」那條坑的同一種死法。

選 `isolate.cpp` 而不是新增 `liveprobe.cpp`：不需要第三套 shell driver，
而且 `isolate` 本來就是「單獨量一條路徑」的工具，逐檔輸出是它自然的延伸。

### 3.2 `shellthumb/TeleDriveThumb.cpp` — `LogPath` 的 lifecycle

現況 `Log()`（`TeleDriveThumb.cpp:150-164`）是 `static bool checked`，
**每個 host process 只讀一次 registry**。所以：

```
dllhost 已載入 handler，當時沒有 LogPath   →  checked=true, path=""
audit 寫入 HKCU\...\LogPath
同一個 dllhost 再被呼叫                     →  不重讀 registry
audit 看不到任何 DLL log
                                            →  依 §4 判成「沒量到」
```

**這正是這份設計要避免的 measurement trap，而 rev 1 自己掉進去了。**

改法（選 A）：`Log()` 的 path 快取加一個**便宜的 TTL**——每 2 秒最多重讀一次
registry。diagnostics 的效能要求跟 `GetSettings()` 不同：`GetSettings()` 在
每個檔案的熱路徑上且結果永不變，所以必須是 magic static；`Log()` 在沒開記錄時
第一件事就是 `path.empty()` 提早 return，加一個 `GetTickCount64()` 比較的成本
可以忽略。

> **不要改成 `GetSettings()` 那種 magic static。** 那條坑（Explorer 進資料夾時
> 多執行緒同時冷載入，旗標在讀取之前就立起來）是**設定**的，不是記錄的；
> 記錄需要的恰好是相反的性質——可以在 process 存活期間改變。

並列成 preflight contract（§11）：audit 啟動時若偵測到 `LogPath` 是這次才設的、
而且已經有 dllhost 在跑，就**明確告知**「DLL 記錄可能要 2 秒後才生效」，
並在第一批量測前 warm 一次丟棄。

### 3.3 read-only diagnostics（`_bridge_legacy.py` 的 `RpcApp`）

`_health`（`:1399`）目前只有 `ok` / `telegram_user_id` / `base_url` /
`mount_drive` / `game_folder`；`_status`（`:1411`）是 stager + uploads + pool，
而 `RpcApp(cfg, resolver, fetcher, stager, upload_stager)`（`:1569`）
**根本沒收到 `BackgroundWarmup`**。

新增：

```json
// GET /rpc/health
{ "cryptg": true }

// GET /rpc/status
{ "warmup": {"enabled": true, "active": false, "phase": "idle", "pass": 3} }

// GET /rpc/counters   ← 新端點，見 §6
```

`BackgroundWarmup` 要被傳進 `RpcApp`，並長出一個 `status()`。這是三處裡唯一
會碰到 wiring 的改動，仍然不改行為。

### 3.4 diagnostics 自己的防線

- 每個 counter 都要有離線測試，斷言「做了 N 次讀取，counter 剛好加 N」。
- counter 植入點**只有兩處**：`_tgio_legacy.py:665`（`_thumbnail_bytes`）與
  `:795`（`_chunk`）——這是整個 repo 僅有的兩個 `iter_download` 呼叫點。
  **植在更上層的便利函式會漏，而漏掉的方向正好是假通過。**
- `/rpc/counters` 不得出現任何憑證，跟 `/rpc/status` 同一條規矩。

---

## 4. 四層模型

rev 1 讓 `Threshold` 直接面對一堆可能缺資料的 metrics。本版改成四層，
**最重要的新不變量是：**

> **只有 `validity == valid` 的量測才有資格進入 threshold evaluator。
> 其餘一律是 `NOT_MEASURED`，既不是 pass 也不是 fail。**

```
1. Discovery         找候選樣本，且不可造成重負載
2. Measurement validity   handler 真的被呼叫了嗎？bridge 真的收到請求了嗎？
                          每個快取的狀態是 cold / warm / unknown？
                          → 不成立就 NOT_MEASURED，不進第 3、4 層
3. Functional correctness  bytes / SHA256 / CRC32 / split 邊界 / 尾端讀取
4. UX performance         max / p95 / stall 次數 / 持續負載下的衰退
```

第 3 層與第 4 層**分開產生 finding**：一個 zip 冷開 6.5 秒在功能上完全正確
（讀 central directory 是該付的成本），在體驗上是一次 stall。rev 1 用一個布林
把這兩件事混在一起，於是同一筆同時 PASS 又是 finding。

---

## 5. 快取狀態模型

`/rpc/forget`（`_bridge_legacy.py:1429`）**只呼叫 `api.invalidate()`**，
而 `invalidate()`（`_tdapi_legacy.py:646`）只清 `_dir_cache` 與 `meta/dirs/*.json`。
它**不清** thumbnail 快取、property 快取、`meta/zips/` 的 `ShardedJsonStore`。

rev 1 寫「冷測之前 `POST /rpc/forget`」，等於宣告了一個它沒有做到的事。
最嚴重的是類別 C：若 `meta/zips/<key>.json` 昨天就存在，**第一次進 zip 也是暖的**，
量出 `<0.1s / <0.1s`，數字漂亮但 central-directory 冷路徑完全沒被測到。

所以每個快取分別建模，**`unknown` 是合法且常見的狀態，不准壓成 boolean**：

| 快取 | 怎麼確定 cold | 怎麼確定 warm | 何時是 unknown |
|---|---|---|---|
| `api_metadata` | `POST /rpc/forget` | 剛列過 | — 永遠可確定 |
| `zip_index` | 檢查 `meta/zips/<key>.json` 不存在 | 檔案存在 | — 可由檔案系統確定 |
| `thumb_cache` | 檢查 `meta/` 對應 key 不存在 | 存在 | — 可確定 |
| `props_cache` | 同上 | 同上 | — 可確定 |
| `rclone_vfs` | `rclone rc vfs/forget` 只清 dir cache，**不清已快取的位元組** | — | **多數情況 unknown**，除非刪 `<cache_dir>\rclone\vfs\` 對應檔 |
| `windows_thumb` | 只有全新的資料夾路徑才能確定 | 看過一次 | **既有資料夾一律 unknown** |

報告因此是：

```json
"cache_state": {
  "api_metadata": "cold",
  "zip_index": "preexisting",
  "thumb_cache": "cold",
  "props_cache": "warm",
  "rclone_vfs": "unknown",
  "windows_thumb": "unknown"
}
```

**冷門檻只對 `cold` 的快取套用。** 是 `preexisting` 或 `unknown` 就記錄實測值、
標成 `NOT_MEASURED`，並在 console 說明為什麼——「這個 zip 的索引昨天就在了，
所以這次量不到冷開成本」比一個漂亮的 0.08 秒有用得多。

---

## 6. Counter，以及它為什麼不能是 log 行數

`_bridge_legacy.py:1659` 對 `telethon.client.downloads` 掛了 `ThrottleRepeats`：
相同 message template 在 60 秒內只放一行過。所以 rev 1 的
「屬性階段 `iter_download` 行數 == 0」有一條乾淨的假通過路徑：

```
T-5s  某個 iter_download 被記錄（template 的配額用掉了）
T+0s  props 量測開始
T+1s  props 錯誤地下載了原檔  →  同一個 template，被 suppress
T+3s  BridgeLog 讀新增區段    →  0 行  →  PASS
```

**log 適合找 positive evidence，被 suppress 的 log 不能當 negative evidence。**
這條 throttle 是這個專案自己為了讓 `bridge.log` 可讀而加的，rev 1 又設計了一個
被它打敗的檢查——同一份文件裡的兩段互相抵銷。

改用 process 內的 monotonic counter，`GET /rpc/counters`：

```
download_requests_total
download_bytes_total
thumb_requests_total
props_requests_total
zip_index_reads_total
```

probe 前後各取一次 snapshot，`before == after` 才是真的「沒讀位元組」。

### 6.1 併發污染

**`before == after` 在 sweep 同時在跑時必然假失敗**——`BackgroundWarmup`
自己就在下載。這是 review 沒提到但會讓修法本身壞掉的地方。兩個做法：

- **首選**：counter 帶來源標籤（`download_bytes_total{source="rpc"|"sweep"}`），
  probe 只看自己那一維。
- **退路**：那段 probe 前先用 §3.3 的 `warmup.active` 確認 sweep idle；
  不 idle 就標 `NOT_MEASURED`，**不要等到它 idle**——那會讓 audit 的執行時間
  變成不可預測。

無論哪一種，`warmup.active` 都要記進報告。

---

## 7. Discovery（樣本挑選）

rev 1 有三個問題：要求 backend 給不出的資訊、自我矛盾、可能非常昂貴。

**Cross-DC / photo**：`Entry` 有 `file_id` / `mime` / `message_id` / `is_split` /
`split_group_id` / `has_thumbnail` / `telegram_user_id`，**沒有** media 型別
（photo vs document）、沒有 DC id、沒有「來源是 chat import」。所以
「從 backend listing 找 photo 佔比高的資料夾」做不到。改成：

- `--sample-crossdc <H: 路徑>` **明確指定**（首選；使用者知道哪些資料夾是 chat import）
- 沒指定時，可選的 `--probe-crossdc` 對候選資料夾抽樣 N 個檔做一次 Telegram
  metadata 查詢來分類，**預設關**，因為它本身就是負載

找不到就在報告寫「這個 drive 上沒有可辨識的跨 DC 樣本，§8.1 未執行」。
**「沒測到」要說出來**，不能靜靜略過。

**Zip entry count**：rev 1 同時要求「依 entry 數最多挑」與「不可開任何 zip」，
而 **不讀 central directory 就不知道 entry 數**。改成：

- `meta/zips/` 已有索引 → 可依 entry 數挑（順帶把該 zip 標成 `zip_index: preexisting`）
- 沒有索引 → 依封存的**邏輯大小 / part 數**挑，這兩個 backend 給得出

**「檔案數最多的資料夾」**：遞迴走完整棵 metadata tree 本身可能非常昂貴
（每層 2 個往返）。改成 bounded discovery：

```
--discovery-max-folders 200
--discovery-max-seconds 30
```

超過就從目前最佳候選挑，並在報告記下 discovery 是否被截斷。
**不要為了開始 benchmark 先 benchmark 半小時。**

---

## 8. 三個結構類別 + 壓力情境

### 8.1 類別 A — 小檔（單一 message，非 split）

冷列舉 → `isolate --jsonl thumb` → `isolate --jsonl props` → `bench` →
抽樣整檔讀回比對 → 立刻重跑 thumb（暖）。

步驟 2、3 **必須分開跑**：CLAUDE.md §4 就是靠分開量才發現「只做縮圖 13.02 秒
且 8 個檔全被讀，只做屬性 0.81 秒且一個都沒讀」。合併量的話 `bench` 的總時間
說不出是哪一條在拖。

| 層 | 指標 | 判準 |
|---|---|---|
| Validity | DLL log 的 `GetThumbnail` 行數 | 必須 == 檔案數，否則 `NOT_MEASURED` |
| Validity | `thumb_requests_total` 增量 | 必須 > 0 |
| Functional | `delegating` 次數 | `== 0` |
| Functional | answered | `== 檔案數` |
| Functional | SHA256 | 全中 |
| UX | 單張 thumb `max` | `< 2 s`（budget）／`>= 5 s` 為 stall |

### 8.2 類別 B — 大檔（split，多 part）

樣本優先挑各 part `telegram_user_id` **不同**的（跨帳號 split 讀得回來唯一的實證）。

1. 冷列舉
2. **屬性**——用 §6 的 counter 驗證：`download_bytes_total` 增量必須是 **0**。
   寬高／duration 該來自 Telegram document attributes。
3. **seek 三處**各讀 1 MiB：頭、**刻意跨 part 邊界的中點**、**尾**
4. 起播模擬：只讀頭 256 KB
5. 整檔 SHA256 — `--full-hash` 才做

**尾端那 1 MiB 是這一類最重要的單一檢查。** 後端 `filesize` 以 512 KB 為單位
進位（實測多報 523,424 bytes），真實長度在 `file_hash` 的 `:<n>` 後綴。
`_clip_parts` 若失效，尾端會等一段長 timeout 然後拿到 **0 bytes**——
那正是非 faststart MP4 無法起播的成因，而且在檔案總覽上完全看不出來。

| 層 | 指標 | 判準 |
|---|---|---|
| Validity | 屬性階段 sweep 是否 idle | 不 idle → 該項 `NOT_MEASURED` |
| Functional | 屬性階段 `download_bytes_total` 增量 | `== 0` |
| Functional | 尾端 1 MiB | 讀到 1 MiB 真實資料，非 0、非短讀 |
| Functional | 跨界那次的內容 | 與 backend part 表相符 |
| UX | 任一 seek | `< 3 s`（budget）／`>= 5 s` 為 stall |
| UX | 起播 | `< 3 s` |

### 8.3 類別 C — `/game` 的 zip

1. **列 `/game` 本身**（`/rpc/forget` 後的冷值）
2. 進第一個 zip → 列虛擬樹 → 列第二、三層
3. 開裡面一個檔 → 與 central directory 的 **CRC32** 對照
   （巡檢半沒有本機原檔；CRC32 是 zip 自帶、唯一可離線驗證的真值。
   上傳往返半才用本機素材的 SHA256）
4. **第二次進同一個 zip**
5. `POST /rpc/fetch-local` → 解壓驗證，**只在封存 ≤ `--fetch-local-max-bytes`
   （預設 200 MB）時做**，否則記 `skipped`

| 層 | 指標 | 判準 |
|---|---|---|
| Validity | `zip_index` 狀態 | `preexisting` → 步驟 1、4 的冷門檻 `NOT_MEASURED` |
| Functional | 列 `/game` 期間 `zip_index_reads_total` 增量 | `<= 1`。**這個指標比時間更早發現「列表打開每個封存」** |
| Functional | CRC32 | 相符 |
| UX | 列 `/game` | `< 1 s` |
| UX | 第一次進 zip | functional budget `8 s`，**但 `>= 5 s` 仍記 UX stall**（見 §10） |
| UX | 第二次進同一個 zip | `< 0.1 s` |

### 8.4 壓力情境

**閒置後重訪（`--idle-seconds`，預設 90）**——什麼都不做等 90 秒（Telethon 的
`_DISCONNECT_EXPORTED_AFTER` 是 60，要確定跨過），再重跑同一批縮圖。這是唯一能抓到
exported sender 那條坑的方法：實測一份 `bridge.log` 有 248 次
`Disconnecting borrowed sender for DC 1`、387 次重連、138 次 `Server closed`。
**只在樣本含跨 DC 檔案時有意義**，否則 `NOT_MEASURED`。

**冷 COM surrogate（`--cold-surrogate`，預設關）**——`taskkill /f /im dllhost.exe`
後立刻跑一批。專打 `Settings` race（只在冷載入的頭幾百微秒發作）。
**順帶解決 §3.2 的 LogPath 問題**：新 surrogate 一定讀得到新設的 `LogPath`。

**持續負載（`--sustain-minutes`，預設 10）—— rev 1 這裡是壞的。**
重複瀏覽同一個資料夾，幾輪之後答案全部來自 Windows thumbcache／bridge 的預覽快取／
rclone VFS，等於在量一個**完全不碰 Telegram 的 workload**。那當然 `flood wait == 0`，
但它沒有證明任何事。改成兩條 lane：

```
背景負載產生器                            前景 UX probe
持續讀「未快取、不重複」的               每分鐘跑一次固定的
Telegram byte range                      shell workload
        │                                        ▲
        └──────▶ Telegram 連線池 ◀───────────────┘
```

要回答的是「**Telegram 正在被持續使用時，Explorer 前景會不會垮**」，
不是「Explorer 重畫同一批已快取的圖會不會垮」。只有這樣，
最後一分鐘 / 第一分鐘的**前景** latency ratio 才有意義。

背景產生器要有 `--sustain-max-bytes` 上限。而且**背景產生器自己撞到 FLOOD_WAIT
不自動算 finding**——那會讓前景因為「不是 bug 的原因」而 fail。它要記成這次 run 的
條件（`"background_flood_wait": true`），讓讀報告的人自己判斷。

**sweep 併發（`--with-sweep`，預設關）**——用 §3.3 的 `warmup.active` 對照
sweep 活躍與否時的前景延遲。門檻：活躍時的縮圖 `max` 不超過非活躍時的 3 倍。

---

## 9. 上傳往返半（`live_shell_roundtrip.py`）

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

### 9.2 流程

Preflight → 建 `H:\_roundtrip-<時間戳>\`（全新名字，`windows_thumb` 因此是
**確定的 cold**，這是 §5 裡少數能確定的一格）→ 寫入 → 等 `/rpc/status` 的
`uploads` 排空 → 用 `tdapi` 確認 row 存在且 `has_thumbnail` 正確 →
`/rpc/forget` → 跑 §8 的三類 → 清理。

### 9.3 清理（rev 1 的前提是錯的）

rev 1 寫「backend row 清不掉」。**錯。** `tdapi.trash(file_id)` 是
`DELETE /files/{file_id}`，backend 在整棵子樹蓋 `trashed_at`，
Telegram 訊息不動，正常 listing 預設排除 trashed row。

```
自動清：
  - 本機素材 temp dir
  - POST /rpc/forget
  - rclone rc vfs/forget dir=<folder>  ＋ 刪 <cache_dir>\rclone\vfs\ 對應檔
  - HKCU\...\LogPath 還原（含「原本就沒有這個值」的情況）
  - probe 子樹 → tdapi.trash(<probe folder root_id>)

永久 residue：
  - Telegram 訊息（送出即永久，這是唯一真的清不掉的）
```

**兩個邊界要處理：**

- **`gamezip` 打包後是 `/game/<name>.zip`，不在 probe 資料夾底下**，
  trash 根目錄清不到它。要單獨 trash 那一筆。
- **trashed row 與 `check_hash` 去重的互動未知。** 下一次跑同樣位元組會不會
  命中一筆已 trash 的 row（然後沿用它的 `has_thumbnail`、或註冊失敗）？
  **實作時必須實測並記錄結果**，這會決定 `--reuse-folder` 能不能跟 trash 並存。

- `uploads/` 若有殘留**不刪**——上傳失敗時那是唯一副本。報告點名，交給人決定。

報告：

```json
"remote_residue": {
  "backend_visible_rows": 0,
  "telegram_messages": [{"name": "...", "message_id": 12345, "file_id": "..."}]
}
```

### 9.4 `--reuse-folder <name>`

固定資料夾重複跑，同名覆寫命中去重，第二次起零新增位元組。
**定義為「不 trash」**，當長期 regression fixture 用。
代價是 `windows_thumb` 已暖，所有冷門檻 `NOT_MEASURED`。

---

## 10. Severity 模型

rev 1 的全域 `<5s` 與類別 C 的 `<8s` 直接矛盾：6.5 秒同時 PASS 又是 finding。
最上層的問題是「不應該一直轉」，所以 **global stall budget 優先**，
但**功能正確性與體驗要分開記**：

```
FAIL          functional 錯了（bytes 不對、覆蓋不完整、answered 不足）
UX_STALL      >= 5 s，使用者感知得到的停頓
SLOW          超過該 subsystem 的 budget，但 < 5 s
OK            在 budget 內
NOT_MEASURED  validity 不成立（§4）
```

一筆量測可以同時是 `OK`（functional）與 `UX_STALL`（體驗）。類別 C 冷開 zip
6.5 秒就是這一格：

```
class_C.cold_open: functional_budget=8s → OK
                   ux_budget=5s        → UX_STALL
```

**這不是雙重標準，是兩個不同的問題。** 「技術上這個成本合理」與
「使用者會覺得卡」可以同時為真，而把它們壓成一個布林會讓其中一個永遠被隱藏。

### 10.1 `warm/cold` ratio 降為 diagnostic

rev 1 把 `>= 20x` 當硬門檻。兩個反例足以推翻：

| | cold | warm | ratio | 實際體驗 |
|---|---|---|---|---|
| 最佳化之後 | 180 ms | 25 ms | 7.2× | 很好，但 rev 1 判 FAIL |
| 冷路徑很糟 | 10 s | 0.4 s | 25× | 很差，但 rev 1 判 PASS |

ratio 保留在報告裡，用來判斷**快取有沒有產生效果**（接近 1 就是快取沒生效，
那是個值得知道的事實），但**不單獨產生 finding**。
**user-facing gate 一律是絕對延遲。**

### 10.2 總門檻表

| 面向 | UX budget | Functional budget |
|---|---|---|
| **任何單一操作** | **`< 5 s`**（超過 = `UX_STALL`） | — |
| `delegating` | — | `== 0` |
| `Server closed the connection` | — | `== 0` |
| 開已快取資料夾 | `< 0.05 s` | — |
| 開未快取資料夾 p95 | `< 1 s` | — |
| 單張縮圖 max | `< 2 s` | — |
| 列 `/game` | `< 1 s` | `zip_index_reads <= 1` |
| 第一次進 zip | `< 5 s` | `< 8 s` |
| 第二次進同一個 zip | `< 0.1 s` | — |
| 大檔任意 seek | `< 3 s` | — |
| 大檔屬性階段 | — | `download_bytes 增量 == 0` |
| 大檔尾端 1 MiB | — | 讀到真實資料 |
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
| `cryptg` | `/rpc/health` 的新欄位（§3.3） | 少了它解密把下載壓在 ~0.15 MiB/s，量什麼都沒意義 |
| `H:` 掛著 | `cfg.mount_drive` 存在 | 沒掛載時 `SHCreateItemFromParsingName` 微秒級失敗，log 上跟「handler 答錯了」一模一樣 |
| rclone rc 通 | `POST 127.0.0.1:5572/rc/noop` | 少了 `--rc-no-auth` 會回 `403 authentication must be set up` |
| `isolate.exe` 支援 `--jsonl` | 跑一次 `--help` | 先跑 `shellthumb\buildbench.bat` |
| DLL 版本支援 LogPath 重讀 | 設 `LogPath` → 等 2 秒 → 用一個已知檔案 probe | 先跑 `shellthumb\build.bat`（需先 `taskkill /f /im dllhost.exe`） |
| `/rpc/counters` 存在 | `GET` | bridge 是舊版，先 `restart.bat` |
| DLL 已註冊 | HKCU 的 ProgID 與 `SystemFileAssociations` | 先跑 `install_thumb.py` |
| `bridge.log` 讀得到 | `cfg.cache_dir / "bridge.log"` | 沒有 log 就沒有 positive evidence |

---

## 12. 報告

```json
{
  "generated": "2026-09-19T15:30:12",
  "mount": "H:",
  "measurement": {
    "valid": false,
    "invalid_reasons": ["DLL logging was not active in the existing COM surrogate"]
  },
  "cache_state": {
    "api_metadata": "cold", "zip_index": "preexisting",
    "thumb_cache": "cold", "props_cache": "warm",
    "rclone_vfs": "unknown", "windows_thumb": "unknown"
  },
  "discovery": {"truncated": false, "folders_scanned": 87, "seconds": 12.4},
  "samples": {"small": "...", "big": "...", "zip": "...", "crossdc": null},
  "classes": {"A": {"metrics": {}, "verdicts": {}}, "B": {}, "C": {}},
  "stress": {
    "idle_revisit": {"status": "NOT_MEASURED", "why": "no cross-DC sample"},
    "cold_surrogate": {}, "sustain": {"background_flood_wait": false},
    "with_sweep": {}
  },
  "counters": {"before": {}, "after": {}},
  "warmup_active_during": {"class_B_props": false},
  "findings": [
    {"class": "A", "layer": "ux", "metric": "thumb_max_s",
     "observed": 7.4, "budget": 2.0, "severity": "UX_STALL",
     "means": "shell 讀了整張原圖"}
  ]
}
```

**Exit code**：`0` 全過、`1` 有 finding、`2` preflight 失敗、
**`3` 有 `NOT_MEASURED` 但沒有 finding**——「跑了但有些沒量到」必須跟
「跑了而且都好」區分開，否則 §4 的整個設計會被一個 `exit 0` 抹平。

---

## 13. 檔案清單

**新增**

| 檔案 | 內容 |
|---|---|
| `scripts/_liveprobe.py` | `ShellDriver`（JSONL）／`DllLog`／`BridgeLog`／`Counters`／`CacheState`／`Validity`／`Severity`／`Report` |
| `scripts/live_browse_audit.py` | 唯讀巡檢 |
| `scripts/live_shell_roundtrip.py` | 上傳往返 |
| `tests/test_liveprobe.py` | 見 §14 |
| `tests/live/test_shell_roundtrip.py` | opt-in 包裝 |

**修改（唯讀 diagnostics，不改行為）**

| 檔案 | 改動 | 收尾 |
|---|---|---|
| `shellthumb/isolate.cpp` | `--jsonl` 逐檔 stderr | `shellthumb\buildbench.bat` |
| `shellthumb/TeleDriveThumb.cpp` | `Log()` 的 path 加 2 秒 TTL | `shellthumb\build.bat`（先 `taskkill /f /im dllhost.exe`） |
| `_bridge_legacy.py` | `_health` 加 `cryptg`；`_status` 加 `warmup`；新增 `/rpc/counters`；`RpcApp` 收 `BackgroundWarmup` | `pytest tests -q` → `restart.bat` |
| `_tgio_legacy.py` | 兩處 `iter_download`（`:665`、`:795`）加 counter | 同上 |
| `warmup.py` | `BackgroundWarmup.status()` | 同上 |
| `CLAUDE.md` | 「測試」節加這兩支；手動清單標註哪幾項現在有腳本代跑 | — |

---

## 14. 測試

`tests/test_liveprobe.py`（進 `pytest tests -q`）：

- `ShellDriver` 解析 JSONL：含非 ASCII 路徑、**逾時被 kill 的殘缺輸出仍說得出做到哪一張**
- `DllLog` 解析；**還原「原本沒有這個登錄值」的情況**
- `BridgeLog` 只讀新增區段
- `Counters` 的差值計算，以及 **sweep 活躍時標成 `NOT_MEASURED` 而不是 fail**
- `CacheState` 的 `cold` / `warm` / `preexisting` / `unknown` 判定
- **`Validity` 不成立時 metrics 不進 threshold evaluator**（這是 §4 的核心不變量，
  也是最容易寫錯的一條）
- `Severity` 的三級判定，特別是**同一筆同時 `OK`(functional) 與 `UX_STALL`(ux)**
- `warm/cold` ratio **不**產生 finding
- 報告不含憑證

diagnostics 那半（§3.4）：

- counter 的離線測試，斷言「做了 N 次讀取，counter 剛好加 N」
- `warmup.status()` 的狀態機

**巡檢半整體只能靠真的跑一次**——這是它存在的全部理由。

---

## 15. 明確不做

- **不清 Windows 的 `thumbcache_*.db`**、**不重啟 `explorer.exe`**、
  **不殺 rclone、不卸載 `H:`**：影響 `H:` 以外的全機使用，而且開一個新資料夾
  就得到同樣乾淨的冷狀態。
- **不刪 Telegram 訊息**：做不到。
- **不改任何資料路徑或產品行為**：只加唯讀 diagnostics（§3.0）。
- **不做 GUI、趨勢圖、歷史比較**：一次跑出一個判定就是全部的產出。
- **不量網頁端**：範圍是 `H:` 上的體驗。
