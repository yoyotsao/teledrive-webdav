# 真實 `H:` 巡檢與上傳往返探測 — 設計

**日期：** 2026-09-19

**狀態：** Proposed

**目標倉庫：** `yoyotsao/teledrive-webdav`

**新增：** `scripts/_liveprobe.py`、`scripts/live_browse_audit.py`、`scripts/live_shell_roundtrip.py`、
`tests/live/test_shell_roundtrip.py`、`tests/test_liveprobe.py`

**不動：** `bridge.py`、`tgio.py`、`tdapi.py` 與其餘既有模組。這兩支是觀測工具，不是產品程式碼。

---

## 1. 這份 spec 要回答的問題

使用者的問題不是「速度數字是多少」，而是：

> **「使用體驗可以跟本機硬碟一樣嗎？頂多開檔速度慢一點，但是不應該一直轉。」**

「一直轉」是可以量測的，但**不是用平均值量**。平均值會把它藏起來：一個資料夾 100 張圖，
99 張 40 ms、1 張 30 秒，平均是 340 ms，看起來很好，而使用者看到的是那 30 秒。
所以這份設計從頭到尾用的是**尾端**——`max` 與 `p95`，而且把
「任何單一操作超過 5 秒」直接定義成 finding。

第二件事：**這個問題不能只靠自己新上傳的檔案來回答**。理由在第 3 節。

---

## 2. 目前有什麼、缺什麼

| 既有 | 覆蓋 | 缺口 |
|---|---|---|
| `pytest tests -q` | 離線邏輯 | MTProto 與 backend 都是假的。CLAUDE.md 記的每一條坑（DC 遷移、thumbcache、URL 跳脫、Settings race、exported sender 計時器）沒有一條是測試抓到的 |
| `scripts/live_transfer_parity.py` | 真實**上傳**的協定分界、album、去重 | 完全不碰讀取；不碰 shell；不碰 `/game` 的 zip |
| `shellthumb\isolate.exe` / `bench.exe` | 單一資料夾的 shell 計時 | 手動、單點、不判定、不解析 log、不知道冷暖 |
| `warmup.py` | 填快取 | 它是被測對象之一，不是量測工具 |

缺的是一支**會判定**的東西：跑完直接說「通過」或「這裡會轉」，而不是吐一堆數字讓人自己看。

---

## 3. 為什麼要唯讀巡檢，而不只是上傳往返

一個腳本自己剛上傳的資料夾是**整個 drive 上最幸運的樣本**：

- 全是 `document` 型、存在 primary 帳號**自己的 DC** 上。而「`H:` 打不開、讀 16 bytes 要 202 秒」
  那次（CLAUDE.md 第六種假象）的成因是 chat import 進來的 `photo` 型、在 DC 1 的檔案——
  自己上傳的資料夾**在結構上不可能產生**那個情境。
- 剛註冊，所以 `has_thumbnail` 是對的、`file_id` 是真的數字 document id。
  那 143 筆 `/game` 裡有 127 筆帶著 `1788435722109-52da4qq-3` 形狀的舊值，一筆都不會出現。
- 數量小、只有一層深。而會爆的東西幾乎都是數量與深度驅動的：3,756 檔的資料夾、
  143 個封存 × 6 秒 = 一次列表 15 分鐘、每層路徑 0.52 秒。

更關鍵的是，**讓人一直轉的每一條坑都需要特定觸發條件，而一個新上傳的小資料夾一條都不滿足**：

| 成因（皆出自 CLAUDE.md 的實測紀錄） | 觸發條件 | 新資料夾滿足嗎 |
|---|---|---|
| exported sender 60 秒計時器 → 8 條連線同時重連 → `Server closed the connection` | 跨 DC 檔案 **＋ >60 秒空檔** | ✗ 兩者皆無 |
| FLOOD_WAIT 累積 | 持續拉取數分鐘 | ✗ |
| backend keep-alive 斷線 → `/rpc/thumb` 500 → `delegating` 讀整檔 | 閒置數秒後再請求 | ✗ |
| DLL `Settings` magic-static race | handler **冷載入**的頭幾百微秒 | ✗ 除非先殺 `dllhost.exe` |
| BackgroundWarmup 的 sweep 跟前景搶 | sweep 正在跑 | ✗ |
| 路徑解析每層 0.52 秒 | 深層路徑 + 未快取 | ✗ 只有一層 |
| 列 `/game` 打開每一個封存 | `/game` 底下有幾十上百個 zip | ✗ |

**結論：上傳往返證明「新東西是對的」，唯讀巡檢證明「舊東西不會卡」。回答使用者的問題要靠後者。**

---

## 4. 不變量

1. **`live_browse_audit.py` 零副作用。** 不上傳、不刪除、不清 Windows thumbcache、
   不重啟 `explorer.exe`、不卸載 `H:`、不改任何設定。唯一的例外是
   `HKCU\Software\TeleDriveWebDAV\LogPath`（開 DLL 記錄）與
   `taskkill /f /im dllhost.exe`（冷 surrogate 情境），**兩者都必須還原**，且後者要旗標才做。
2. **報告不含憑證。** 只有名稱、大小、雜湊、id、計時。session string 與 JWT 一律過
   `upload_engine.redact`。貼進 issue 必須是安全的。
3. **`live_shell_roundtrip.py` 會寫真資料，所以要明確授權。** 沿用
   `live_transfer_parity.py` 的規矩：`--max-bytes` 是硬上限、`--dry-run` 先看計畫。
4. **量測不可以被自己騙。** 見第 9 節——這份設計有一半的篇幅在講怎麼確認量到的是真的。

---

## 5. 架構

```
scripts/_liveprobe.py          共用：shell 驅動、log 解析、計時、門檻、報告
        ├── live_browse_audit.py      唯讀巡檢（主角，常跑）
        └── live_shell_roundtrip.py   上傳往返（寫真資料，少跑）
```

拆兩支的理由是**副作用不同**：一支你想什麼時候跑就跑，一支每跑一次就在 Telegram 上
留下刪不掉的東西。混在一個旗標後面，遲早會有人不小心跑到寫入那半。

### 5.1 `_liveprobe.py` 的單元

| 單元 | 職責 | 依賴 |
|---|---|---|
| `ShellDriver` | 呼叫 `isolate.exe` / `bench.exe`，解析 stdout 成結構化結果 | `shellthumb\*.exe` |
| `DllLog` | context manager：設 `LogPath` → 跑 → 還原 → 解析出 `delegating` / `onMount=0` / `preview fetch failed` / 每檔耗時 | HKCU |
| `BridgeLog` | 記下 `bridge.log` 的位移，跑完只讀新增那段，數關鍵字並抽出 `iter_download` 速率 | `cfg.cache_dir / "bridge.log"` |
| `Timed` | 單一操作計時，收集成 `max` / `p95` / `mean` | — |
| `Threshold` | 門檻定義與判定，產生 finding | — |
| `Report` | JSON + console 摘要 | — |

`ShellDriver` 與 `DllLog` 是分開的兩個單元，因為它們回答不同的問題：前者是
「shell 花了多久、答對幾個」，後者是「handler 到底有沒有被呼叫、有沒有 delegating」。
**兩個都要，因為只看前者分不出「答得快」與「根本沒問到我們」。**

---

## 6. 三個結構類別

三類的操作序列與失敗模式完全不同，不能共用一套流程。

### 6.1 類別 A — 小檔（單一 message，非 split）

代表「瀏覽一個圖片資料夾」，最吃 shell 的三條路徑。

**操作序列**

1. 冷列舉：`os.scandir(H:\<sample>)` 計時（真的走 WinFsp → rclone → PROPFIND）
2. `isolate.exe thumb <sample>` — 縮圖那條路單獨量
3. `isolate.exe props <sample>` — 屬性那條路單獨量
4. `bench.exe <sample>` — Explorer 實際體感，拿到 bind / GetImage / property store 分段
5. 抽樣 N 個檔整檔讀回，SHA256 與 backend 記錄的 `file_hash` 真實長度交叉比對
6. **立刻重跑步驟 2**（暖跑）

**為什麼步驟 3、4 要分開跑**：CLAUDE.md 第 4 節就是靠分開量才發現
「只做縮圖 13.02 秒且 8 個檔全被讀，只做屬性 0.81 秒且一個都沒讀」——
合在一起量的話，`bench.exe` 的總時間無法告訴你是哪一條在拖。

**門檻**

| 指標 | 門檻 | 沒過代表什麼 |
|---|---|---|
| `delegating` 次數 | `== 0` | shell 跑去讀整張原圖。URL 跳脫／Settings race／`/rpc/thumb` 500／photo 型 media 任一 |
| thumb answered | `== 檔案數` | 有檔案完全拿不到縮圖 |
| props answered | `== 影像檔數` | property handler 沒載入，或 `meta/` 的屬性快取沒收斂 |
| 單張 thumb `max` | `< 2 s` | 就是「一直轉」本人 |
| 暖跑 / 冷跑 | `≥ 20×` | thumbcache 沒被填。兩次都慢 = handler 根本沒被載入 |
| SHA256 | 全中 | 位元組錯了 |

### 6.2 類別 B — 大檔（split，多 part）

代表「播一部影片」「複製一個大檔」。這一類的 shell 路徑不重要，**位移數學才是**。

**樣本選擇優先序**：優先挑各 part 的 `telegram_user_id` **不相同**的——
那是「跨帳號 split 讀得回來」唯一的實證（CLAUDE.md 手動清單第 8 項）。
找不到跨帳號的就退而求其次挑 part 數最多的，並在報告裡註明。

**操作序列**

1. 冷列舉
2. **屬性**：`isolate.exe props` 或 `/rpc/props`。
   **這一步不可以讀到任何位元組**——duration／寬高該來自 Telegram document attributes。
   `bridge.log` 在這個區間若出現 `iter_download`，就是 finding。
3. **seek 三處**，各讀 1 MiB：
   - **頭** `offset 0`
   - **正中間** `total // 2`，**刻意對齊到跨 part 邊界**（取離中點最近的 part 邊界前後各 512 KiB）
   - **尾** `total - 1 MiB`
4. **起播模擬**：只讀頭 256 KB 計時。對應「按下播放到出畫面」。
5. 整檔 SHA256 — `--full-hash` 才做，預設關（大檔很貴）。

**尾端那 1 MiB 是這一類最重要的單一檢查。** 後端的 `filesize` 會灌水（以 512 KB 為單位
進位，實測多報 523,424 bytes），真實長度在 `file_hash` 的 `:<n>` 後綴。
`_clip_parts` 若失效，尾端讀取會等一段長 timeout 然後拿到 **0 bytes**——
那正是非 faststart MP4 完全無法起播的成因，而且在檔案總覽上完全看不出來。

**門檻**

| 指標 | 門檻 | 沒過代表什麼 |
|---|---|---|
| 屬性階段的 `iter_download` 行數 | `== 0` | 為了拿寬高去下載位元組 |
| 任一 seek | `< 3 s` | 跳轉會轉 |
| 尾端 1 MiB | **讀到 1 MiB 真實資料**，非 0、非短讀 | `filesize` 灌水沒裁掉 |
| 跨 part 邊界那次的內容 | 與 backend 的 part 表對得上 | split 位移數學錯 |
| 起播（頭 256 KB） | `< 3 s` | 按播放會等 |

### 6.3 類別 C — `/game` 的 zip，可看內容

最重的一類，因為它疊了三層：backend listing → Telegram 讀 central directory → 虛擬樹。

**操作序列**

1. **列 `/game` 本身** —— 這一步直接量「列 `/game` 不可以打開每一個封存」那條坑。
   計時取的是 `POST /rpc/forget` **之後**的冷值；暖值另外記一筆供對照，但不設門檻。
2. 進第一個 zip → 列它的虛擬目錄樹
3. 列 zip **內部的第二、三層子目錄**
4. 開 zip 裡一個檔（單 entry range 讀取）→ SHA256 與**同一個 entry 的 CRC32**（來自 central
   directory）對照。巡檢半沒有本機原檔可比，CRC32 是 zip 自己帶的、唯一可離線驗證的真值；
   上傳往返半才用本機素材的 SHA256。
5. **第二次進同一個 zip** → 量 `meta/zips/` 有沒有命中
6. `POST /rpc/fetch-local` 取回整包 → 解壓 → 驗證內容。
   **只在封存 ≤ `--fetch-local-max-bytes`（預設 200 MB）時做**，否則跳過並在報告註明——
   真實 drive 上的遊戲封存動輒數 GB，一支「唯讀巡檢」不該花半小時拉一包回來。
   上傳往返半自己造的 zip 遠小於這個上限，所以那半一定會跑到。

**門檻**

| 指標 | 門檻 | 沒過代表什麼 |
|---|---|---|
| 列 `/game` | `< 1 s` | 退回「解析每個子項就打開每個封存」。實測那是 15 分鐘，久到 rclone 放棄、整個掛載卡死 |
| 列 `/game` 期間 `bridge.log` 的 zip 讀取次數 | `<= 1` | 同上，而且這個指標比時間更早發現問題 |
| 第一次進 zip | `< 8 s` | 讀 central directory 是應該的成本 |
| **第二次進同一個 zip** | **`< 0.1 s`** | `ShardedJsonStore`（`meta/zips/`）沒生效 |
| zip 內檔案的 CRC32（巡檢半）／SHA256（往返半） | 相符 | 單 entry range 讀取錯 |
| `fetch-local` 解壓 | 成功且內容相符（超過上限則記 `skipped`，不算 finding） | 整包取回路徑壞了 |

---

## 7. 巡檢半的四個壓力情境

第 6 節是「一次乾淨的瀏覽」。這四個才是專門去踩那些需要條件才發作的坑。

### 7.1 閒置後重訪（`--idle-seconds`，預設 90）

跑完第 6 節後**什麼都不做等 90 秒**，再重跑同一批的縮圖。

90 而不是 60：Telethon 的 `_DISCONNECT_EXPORTED_AFTER` 是 60 秒，要**確定跨過**它。
這是唯一能抓到 exported sender 那條坑的方法——實測一份 `bridge.log` 有
248 次 `Disconnecting borrowed sender for DC 1`、387 次重連、138 次
`Server closed the connection`，而每一次關掉都是一張失敗的預覽、一次 `delegating`、
一次讀整張原圖。

**門檻**：閒置後那一批的 `delegating == 0`、`Server closed the connection == 0`。
這一條只在樣本裡真的有跨 DC 檔案時才有意義，所以樣本挑選（第 8 節）要刻意找一個。

### 7.2 冷 COM surrogate（`--cold-surrogate`，預設關）

`taskkill /f /im dllhost.exe` 之後**立刻**跑一批小檔縮圖。

專打 `Settings` magic-static race：它**只在 handler 冷載入的頭幾百微秒發作**，
暖起來之後單獨重試同一個檔案又完全正常，所以平常測不到。實測一個新的 COM surrogate，
同一毫秒進來的 4 個檔案中了 3 個。

**門檻**：第一批的 `onMount=0` 次數 `== 0`、`delegating == 0`。

### 7.3 持續壓力（`--sustain-minutes`，預設 10）

連續瀏覽樣本資料夾 10 分鐘，每分鐘記一次速率。

**門檻**：最後一分鐘的速率 ≥ 第一分鐘的 70%（衰減 < 30%），且期間
`bridge.log` 的 `flood wait` 行數 `== 0`。

這一條對應已知限制第 3 點：Telegram 會隨持續拉取逐步節流。門檻不是「不准衰減」，
是「不准垮掉」。

### 7.4 sweep 併發（`--with-sweep`，預設關）

確認 `BackgroundWarmup` 正在跑的時候，前景瀏覽還可不可以用。
從 `/rpc/status` 判斷 sweep 是否活躍；活躍時重跑類別 A 並與非活躍時對照。

**門檻**：sweep 活躍時的縮圖 `max` 不超過非活躍時的 3 倍。
`wait_for_quiet` 的禮讓若失效，這裡會現形。

---

## 8. 樣本挑選

巡檢半預設**自動挑**，也允許明確指定。自動挑要挑到「有代表性」而不是「最方便」：

| 類別 | 自動挑選規則 | 指定旗標 |
|---|---|---|
| A 小檔 | 檔案數最多的資料夾中，隨機取一個；**若能找到含非 ASCII 檔名的，優先**（專打 URL 跳脫） | `--sample-small <H: 路徑>` |
| A' 跨 DC | 從 chat import 來的資料夾（`photo` 型 media 佔比高的），供 7.1 使用 | `--sample-crossdc <H: 路徑>` |
| B 大檔 | `split_group_id` 有值且各 part `telegram_user_id` 不同的；退而求其次挑 part 數最多的 | `--sample-big <H: 路徑>` |
| C zip | `/game` 底下 entry 數最多的 `.zip` | `--sample-zip <H: 路徑>` |

**挑選本身不可以很貴。** 走 backend listing 而不是走 `H:`，而且
**不可以為了分類去開任何一個 zip**——那正是要測的坑。分類只看副檔名與 backend 的
part 表。

找不到某一類就**明確跳過並在報告裡說**，不要靜靜略過：
「這個 drive 上沒有跨帳號的 split 檔案」本身就是一個值得知道的事實。

---

## 9. 怎麼確認量到的是真的

這一節存在的理由是 CLAUDE.md 記了太多次「量測會騙人」。每一條都要有對應的防線。

| 騙法 | 防線 |
|---|---|
| Windows 的 `thumbcache_*.db` 回答，handler 根本沒被呼叫 | `DllLog` 必須看到對應數量的 `GetThumbnail` 行。沒有記錄 = 這次量測**作廢**，不是「很快」 |
| rclone 的 VFS 快取回答，根本沒到 bridge | 跑之前 `BridgeLog` 記位移，跑完確認新增的請求數 > 0 |
| bridge 的 metadata 快取回答 | 冷測之前 `POST /rpc/forget` + `rclone rc vfs/forget` |
| `SIIGBF_THUMBNAILONLY` 沒給 → shell 回檔案類型圖示、沒碰檔案 | 已由 `isolate.exe` / `warmshell.exe` 帶好，不改 |
| 交錯偏差：先跑的那組永遠比較快（已知限制第 3 點） | 多組對照時**交錯取樣**，不是跑完一組再跑下一組 |
| `capture_output` 在子行程被 kill 之後把數字一起丟掉 | 讀 `isolate.exe` 的 stderr 逐檔回報，逾時也要說得出做到哪 |
| 單次跑的雜訊 | 每個門檻至少 3 次取樣取 `max`；`--repeat` 可調 |

**「沒量到」與「很快」必須是兩個不同的結果。** 這是整份設計最容易寫錯的地方，
而它一旦寫錯，這支腳本就會變成一個永遠通過的裝飾品——跟那個死了幾週、
每批都回報 `shell warm stopped after 0/100` 卻沒人看得出來的 shell warm 一樣。

---

## 10. 上傳往返半（`live_shell_roundtrip.py`）

### 10.1 素材

**刻意壓到最小**，因為遠端刪不掉（見 10.3）。測試目的沒有一項需要大檔：

| 案例 | 內容 | 打的是 |
|---|---|---|
| `album-11` | 11 張 64 KB JPEG | album 湊滿 10 就送 + 尾巴 flush |
| `nonascii` | 1 張 64 KB JPEG，檔名含中文與日文假名 | URL 跳脫那條坑 |
| `png` | 1 張 64 KB PNG | 縮圖 `make_preview` 的非 JPEG 路徑 |
| `boundary` | 1 個 10 MiB + 1 | `decide_protocol` 的 small/big 分界 |
| `gamezip` | 一個三層深、共 8 個小檔的資料夾寫進 `H:\game\` | `/game` 打包 → zip 虛擬樹（類別 C 的新鮮樣本） |
| `split` | 1 個 500 MiB + 1 | **`--include-split` 才跑**，預設關 |

預設總計約 **11 MB**。

### 10.2 流程

1. Preflight（第 11 節）
2. 建 `H:\_roundtrip-<時間戳>\`——全新名字，Windows thumbcache 從沒見過，**冷是免費的**
3. 寫入 → 等 `/rpc/status` 的 `uploads` 排空 → 用 `tdapi` 確認 backend 真的有 row，
   且 `has_thumbnail` 對得上（圖 `True`、zip `False`）
4. `POST /rpc/forget` + `rclone rc vfs/forget`
5. 對這個新資料夾跑第 6 節的類別 A；`gamezip` 跑類別 C；`split`（若有）跑類別 B
6. 清理（10.3）

### 10.3 清理：能清的全清，清不掉的講清楚

**自動清掉**

- 本機產生的素材 temp dir
- `POST /rpc/forget`（含 `meta/dirs/`）
- `rclone rc vfs/forget dir=<folder>` ＋ 刪 `<cache_dir>\rclone\vfs\` 底下該資料夾的快取檔
- `HKCU\...\TeleDriveWebDAV\LogPath` 還原成跑之前的值（包含「原本就沒有這個值」的情況）

**刻意不清**

- `uploads/` 若有殘留**不刪**。上傳失敗時那是**唯一副本**，刪掉就真的沒了。
  改成在報告裡點名，交給人決定。

**清不掉，這是 backend 與 Telegram 的性質**

- backend 的 row：`tdapi.py` 沒有任何 DELETE，`_ReadOnlyFile.delete()` 一律回 403
- Telegram 訊息：送出即永久

所以報告最後固定印一段 `remote residue`：資料夾名、每個檔的 `file_id` / `message_id`。
**這不是免責聲明，是交付物的一部分**——它讓人知道網頁上多了什麼。

### 10.4 `--reuse-folder <name>`

固定資料夾重複跑。同名覆寫命中去重（完全免費），**第二次起零新增位元組**。
代價是 Windows thumbcache 已經暖了，冷路徑那半量不到，所以報告要標記
`cold=false` 並跳過所有冷門檻。日常回歸用這個，真的要驗冷路徑才開新資料夾。

---

## 11. Preflight：缺東西就拒跑

**量出一個假數字比不量更糟。** 以下任一不成立就 `SystemExit`，講清楚缺什麼、怎麼補：

| 檢查 | 怎麼查 | 不過的訊息 |
|---|---|---|
| bridge 活著 | `GET /rpc/health` | 先跑 `start.bat` |
| `H:` 掛著 | `cfg.mount_drive` 存在 | 沒掛載時 `SHCreateItemFromParsingName` 是微秒級失敗，25 個檔「一個都沒暖成」瞬間回來——log 上跟「handler 答錯了」一模一樣 |
| rclone rc 通 | `POST 127.0.0.1:5572/rc/noop` | 少了 `--rc-no-auth` 會回 `403 authentication must be set up` |
| `isolate.exe` / `bench.exe` 在 | 檔案存在 | 先跑 `shellthumb\buildbench.bat` |
| DLL 有註冊 | HKCU 的 ProgID 與 `SystemFileAssociations` 都在 | 先跑 `install_thumb.py` |
| `bridge.log` 讀得到 | `cfg.cache_dir / "bridge.log"` | 沒有 log 就沒有第 9 節的防線，直接拒跑 |
| `cryptg` | `/rpc/health` 回報 | 少了它解密就把下載壓在 ~0.15 MiB/s，量什麼都沒意義 |

---

## 12. 報告與結果

### 12.1 Console

一行一個判定，`[ok]` / `[SLOW]` / `[FAIL]`，後面接實測值與門檻。最後一段是
`remote residue`（僅上傳半）與 finding 總數。

### 12.2 JSON

寫到 `<cache_dir>\browse-audit-report.json` / `<cache_dir>\roundtrip-report.json`：

```json
{
  "generated": "2026-09-19T15:30:12",
  "mount": "H:",
  "cold": true,
  "samples": {"small": "...", "big": "...", "zip": "...", "crossdc": "..."},
  "classes": {
    "A": {"metrics": {}, "thresholds": {}, "findings": []},
    "B": {}, "C": {}
  },
  "stress": {"idle_revisit": {}, "cold_surrogate": {},
             "sustain": {}, "with_sweep": {}},
  "dll_log": {"calls": 0, "delegating": 0, "onmount_zero": 0, "fetch_failed": 0},
  "bridge_log": {"server_closed": 0, "flood_wait": 0,
                 "remote_disconnected": 0, "file_migrate": 0,
                 "iter_download": 0},
  "findings": [{"class": "A", "metric": "thumb_max_s",
                "observed": 7.4, "threshold": 2.0,
                "means": "shell 讀了整張原圖"}]
}
```

### 12.3 Exit code

`0` 全過、`1` 有 finding、`2` preflight 失敗（**跟「有 finding」分開**，
因為「沒跑成」跟「跑了但結果不好」是兩件完全不同的事）。

---

## 13. 總門檻表

| 面向 | 門檻 |
|---|---|
| **任何單一操作** | **`< 5 s`** — 這條直接對應「不應該一直轉」 |
| `delegating` | `== 0`（全程） |
| `Server closed the connection` | `== 0`（全程，含閒置重訪之後） |
| 開已快取資料夾 | `< 0.05 s` |
| 開未快取資料夾 p95 | `< 1 s` |
| 單張縮圖 max | `< 2 s` |
| 暖跑 / 冷跑 | `>= 20x` |
| 列 `/game` | `< 1 s` |
| 第二次進同一個 zip | `< 0.1 s` |
| 大檔任意 seek | `< 3 s` |
| 大檔尾端 1 MiB | 讀到真實資料 |
| 10 分鐘後速率衰減 | `< 30%` |
| 所有 SHA256 | 相符 |

門檻放在 `_liveprobe.py` 的一個 dataclass 裡，`--thresholds <json>` 可覆寫，
預設值就是上表。**改門檻要留下痕跡**，報告會記下實際用的是哪一組。

---

## 14. 測試

| 檔案 | 覆蓋 |
|---|---|
| `tests/test_liveprobe.py`（進 `pytest tests -q`） | `ShellDriver` 解析 `isolate`/`bench` 的輸出（含非 ASCII 路徑、逾時被 kill 的殘缺輸出）、`DllLog` 解析與**還原原本沒有登錄值的情況**、`BridgeLog` 只讀新增區段且數對關鍵字、`Threshold` 的邊界判定、報告不含憑證、**「沒量到」不會被判成通過** |
| `tests/live/test_shell_roundtrip.py` | opt-in 包裝，沿用 `test_transfer_parity.py` 的形狀（環境變數沒設就 skip） |

**巡檢半整體只能靠真的跑一次**——這是它存在的全部理由。

---

## 15. 明確不做

- **不清 Windows 的 `thumbcache_*.db`**：那會影響 `H:` 以外全機所有資料夾的縮圖，
  而且沒必要——開一個新資料夾就得到同樣乾淨的冷狀態。
- **不重啟 `explorer.exe`**：同上，代價遠大於收益。
- **不殺 rclone、不卸載 `H:`**：跟 `restart.bat` 的理由一樣，殺掉等於白丟 dir cache 與 VFS 快取。
- **不刪遠端任何東西**：沒有端點，做不到（見 10.3）。
- **不做 GUI、不做趨勢圖、不存歷史比較**：一次跑出一個判定就是全部的產出。
  要比較就留兩份 JSON 自己 diff。
- **不量網頁端**：這份 spec 的範圍是 `H:` 上的體驗。
