# CLAUDE.md — 開發筆記

使用說明在 [README.md](README.md)。這裡是架構、為什麼這樣做，以及踩過的坑。

```
Explorer 右鍵「儲存在本地」──▶ registry verb ──▶ POST 127.0.0.1:8081/rpc/fetch-local
Explorer 縮圖 ──▶ shellthumb.dll (IThumbnailProvider) ──▶ GET /rpc/thumb
Explorer 屬性 ──▶ shellthumb.dll (IPropertyStore)     ──▶ GET /rpc/props
                                                          │
H:  ──rclone mount(WinFsp)──▶ http://127.0.0.1:8081  (bridge.py, Python)
                                        │
                     metadata (HTTPS + JWT)          位元組 (MTProto)
                                        ▼                    ▼
                     teledrive…dpdns.org/api/v1        Telegram CDN
```

**核心不變量**：位元組只在「本機 ↔ Telegram」之間流動，metadata 才走 TeleDrive backend。
bridge 只用現有 public API，沒有為它新增任何會讀寫二進位資料的端點。

## 改完之後自己收尾（不要留指令給使用者跑）

**這個專案的產出不是 diff，是「H: 上的行為」。** 所以改完程式碼要自己做完重編／重啟，
把它帶到「使用者直接點進 `H:` 就能看到結果」的狀態，再回報。跑完測試就交件是不算完成的：
測試裡的 MTProto 與 backend 都是假的，而這裡幾乎每個 bug 都活在真的 shell、真的 rclone、
真的 Telegram 上（DC 遷移、thumbcache、URL 跳脫那幾條坑沒有一條是測試抓到的）。

| 改了什麼 | 收尾動作 |
|---|---|
| Python（`bridge.py` / `tgio.py` / `tdapi.py` / stager / warmup…） | `.venv\Scripts\python.exe -m pytest tests -q` → `restart.bat` |
| `shellthumb/*.cpp` | `shellthumb\build.bat`（自己會叫 vcvars64），然後重新載入 handler |
| `install_menu.py` / `install_thumb.py` | 重跑安裝；property handler 那半要管理員（只有 HKLM） |
| `config.ini` | `restart.bat`（路徑全部由 `cache_dir` 推導，重讀才生效） |

- **`restart.bat` 只重啟 bridge，不碰 rclone。** rclone 是對 127.0.0.1 講 HTTP 並且會重試，
  所以 `H:` 不會斷、VFS 快取也還在；殺掉 rclone 等於卸載磁碟又白丟 dir cache。
  重啟前先看 `/rpc/status`：staging/uploads 的 debounce 計時器不會續命（檔案還在，計時歸零）。
- **DLL 正在被載著就覆寫不了。** 縮圖 handler 跑在 COM surrogate 裡，`build.bat` 會因為
  檔案被鎖而失敗 → 先 `taskkill /f /im dllhost.exe`，必要時再重啟 `explorer.exe`。
- **驗證縮圖／屬性的改動要換一個沒開過的資料夾。** 看過一次的資料夾由 Windows 自己的
  `thumbcache_*.db` 回答，handler 根本不會被呼叫（見「量測會被 Windows 自己的縮圖快取騙」），
  在舊資料夾上「看起來正常」什麼都證明不了。
- **日誌在 `<cache_dir>\meta\bridge.log`**（`config.py` 裡的 `cfg.cache_dir` 就是那層 `meta/`；
  rotating，8 MB × 3，同時仍印到 console）。
  要確認一個改動在真的 Telegram 上生效，讀這個檔比問使用者貼 console 快，
  而且 crash 之後還在。

## 檔案

| 檔案 | 職責 |
|---|---|
| `bridge.py` | wsgidav provider、寫入保護、`/rpc/*`、cheroot 伺服器（綁 127.0.0.1） |
| `tdapi.py` | TeleDrive REST client：JWT 取得（bot challenge，見「踩過的坑」）/快取/401 自動重登、路徑解析、listing 快取（記憶體 + `meta/dirs/`，`/folders` 與 `/files` 併發，見「效能」第 7 節）、split part 表快取、`JsonStore` |
| `tgio.py` | split 位移數學、Telethon worker（背景 event loop）、連線池、`SeekableRemoteFile`、分段上傳、縮圖與 media attributes（讀 Telegram 的預覽，以及 `make_preview` 產自己上傳的那張） |
| `tgupload.py` | 上傳的線路協定：`decide_protocol`（10 MiB / 500 MiB 兩個界線）、`SaveFilePart`（128 KiB × 4 workers）與 `SaveBigFilePart`（512 KiB）兩套 part 原語、繞過 `client._call` 直送 |
| `upload_engine.py` | **所有**上傳的唯一入口：指紋 → check-hash → 協定選擇 → album/一般 → 帳號租借 → 訊息 → 註冊；`transfer_batch` 的串流階段、`FingerprintClaims`、精確去重覆蓋、`TransferMetrics` 完成日誌、`redact` |
| `telegram_accounts.py` | 帳號檔載入與驗證、每個帳號一個 `TelegramWorker` 與各自的 file/chunk/message 額度、`for_read(id)` 精確路由、`acquire_upload()` round-robin 租借 |
| `upload_limiter.py` | 移植網頁端 `adaptiveRateLimiter.ts` 的狀態機（ceiling / slow zone / probe / escalation）+ 訊息 token bucket；每個帳號一份 `meta/upload-rate-<id>.json`，原子寫入 |
| `media_thumbnail.py` | 靜態圖（Pillow）與影片（ffmpeg）的預覽擷取，結果分成 `ready` / `not_media` / `undecodable` |
| `transfer_models.py` | 不可變值物件：`AccountSpec` / `RemotePart` / `UploadedPart` / `TransferRequest` / `TransferResult` / `PreparedAlbumItem` / `QueueStage` |
| `zipfs.py` | 讀 zip central directory → 虛擬目錄樹；單一 entry 的 range 讀取 |
| `gamestage.py` | `/game` staging + debounce 打包（`ZIP_STORED`）+ 清理；打包完就交給 `upload_engine`。另外持有 `sample_hash` 與 `_preview_file`（engine 會回頭呼叫） |
| `uploadstage.py` | `/game` 以外任意路徑的一般檔案寫入：落地 + debounce + **整批**交給 engine + 註冊 + 只在註冊落地後才刪暫存；佇列狀態持久化在 `meta/upload-queue.json` |
| `fetchlocal.py` | 「儲存在本地」：伺服端複製邏輯 + 右鍵 verb 用的進度顯示 CLI |
| `warmup.py` | 走遍整棵樹批次填滿縮圖與屬性快取、跑 Windows 縮圖快取，可續跑；`BackgroundWarmup` 讓 bridge 自己跑 |
| `install_menu.py` | 註冊/移除 Explorer 右鍵 verb |
| `install_thumb.py` | 註冊/移除 shell handler，逐副檔名記錄被取代的既有 CLSID |
| `shellthumb/` | C++ shell 擴充：`IThumbnailProvider` + `IPropertyStore`，同一份 DLL 兩個 CLSID；`warmshell.exe` 把縮圖灌進 Windows thumbcache，`bench.exe` / `isolate.exe` 量測 |
| `config.py` | 讀 `config.ini`，空值回退環境變數，再回退 `env_file`；由單一 `cache_dir` 推導所有路徑；並發參數做範圍檢查（0 或負數直接 `ConfigError`，不是靜靜跑一個壞值） |
| `start.bat` | 啟動 bridge + `rclone mount` |
| `restart.bat` | 只重啟 bridge（rclone 與 `H:` 不動），改完 Python 後的收尾 |

`config.ini` 只有 `cache_dir` 一個路徑設定，底下的 `meta/` `rclone/` `local/` `staging/` `uploads/`
是程式的實作細節而非設定 —— 先前四個獨立路徑設定的結果就是它們各自漂移，
使用者以為改了一個地方其實只改到四分之一。`start.bat` 也是問 `config.py` 要路徑，
不自己寫死。

### 一般路徑的寫入（`uploadstage.py`）

`/game` 以外，`H:` 上任何資料夾都能建立子資料夾、PUT 新檔、覆寫既有檔案、刪除還沒上傳的檔案。
`WriteGuard`（`bridge.py`）對 `MKCOL`、`PUT`、`DELETE` 全域放行，理由是這三個動詞
各自不需要 `/game` 的打包步驟——`DELETE` 甚至不是靠一個 backend 端點放行，而是完全
不需要看路徑：

- **`MKCOL`** 直接打 `POST /folders`（`RootCollection.create_collection`），
  沒有落地、沒有 debounce，是即時的真實寫入。
- **`PUT`** 落地到 `uploads/`，debounce 之後走跟 `/game` 一模一樣的
  上傳＋去重＋註冊（`upload_engine.UploadEngine`，`uploadstage.py` 只補
  「落地/debounce/整批派工」那一半），差別只在兩點：單位永遠是單一檔案（資料夾是真的，
  從不落地打包），以及 parent 是寫入當下解析到的真實資料夾，不是固定的 `/game`。
  覆寫既有檔案也走這條路（`RemoteFileResource.begin_write`）——backend 沒有
  `UNIQUE(filename, parent_id)`，所以覆寫就是用新內容再註冊一筆同名 row，
  新舊都在、讀取時新的蓋掉舊的（既有的「同名檔案」規則，見「已知限制」第 5 點）。
- **`DELETE`** 能不能做，看的是「這個名字現在解析到的是本機還沒上傳的暫存，
  還是 backend 已經註冊過的真實資料」，跟在不在 `/game` 底下無關——`/game` 跟
  一般路徑的差別只在上傳前有沒有先打包成 zip，不是刪除能力本身的分界。
  `UploadFileResource.delete()`（一般路徑）跟 `StagingFileResource`/`StagingCollection.delete()`
  （`/game`）都只是把本機暫存檔案／目錄刪掉，取消這次還沒發生的上傳；一旦真的
  上傳註冊過，兩邊都靠 `_ReadOnlyFile.delete()` / `_ReadOnlyCollection.handle_delete()`
  統一回 403——backend 沒有刪除端點，這點不因路徑而異。
- **`COPY`** 跟 `DELETE` 一樣看「還在暫存 vs. 已上傳」，不看路徑：還在暫存的來源
  （`UploadFileResource`/`StagingFileResource`/`StagingCollection`）真的用
  `shutil.copy2`／建空目錄複製一份，來源不受影響；已上傳的來源一律 403
  （`_ReadOnlyFile`/`_ReadOnlyCollection`）。複製目的地一旦跨過 `/game` 邊界
  （暫存中的一般檔案複製進 `/game`，或反過來）也是 403——那不是同一個 stager，
  沒有共通的落地邏輯可以套。`MOVE` 維持原樣只在 `/game` 放行：一般路徑的暫存
  沒有搬移原語（`UploadStager` 沒有 `move()`）。

album 分組現在**有**做（`upload_engine.AlbumQueue`），跟網頁同一套規則：
`image/*` 或 `video/*`、≤ 10 MiB、排除 `image/webp`，每 10 個一批送
`SendMultiMedia`，尾巴在批次結束時 flush。去重（`check_hash`，跟網頁同一套指紋）
一樣共用。`/rpc/status` 的 `uploads` 欄位回報目前 debounce 中的一般寫入，
跟 `/game` 的 `units` 分開列。

**暫存檔是唯一的副本，所以它只在「訊息送出 + 每一筆註冊都成功」之後才刪。**
中間任何一步掛掉，檔案都還在 `uploads/` 底下，下次啟動由 `_adopt_leftovers`
重新收養。旁邊的 `meta/upload-queue.json` 只記「這個來源已經失敗幾次、上一次的
錯誤是什麼」（錯誤先過 `upload_engine.redact` 才落地）——**佇列本身是磁碟上那些
檔案，不是那份 JSON**。第 5 次失敗標成 `abandoned` 並保留檔案，不再自動重試。

> **`_adopt_leftovers` 在 Windows 上曾經完全沒作用。** `os.walk(ext_path(...))`
> 產出的 root 帶 `\?\` 前綴，而 `Path(root)/fn` 對純路徑的 `upload_dir` 做
> `relative_to` 永遠 `ValueError` → `continue`，於是**每一個 crash 掉的上傳都被
> 靜靜地從佇列裡丟掉，位元組卻永遠留在磁碟上**。現在路徑段是從 walk 自己的
> 相對 root 編出來的。

**縮圖是有的，而且它不是「網頁那邊的加工」，是這個專案自己的效能前提。**
而它壞掉的方式跟看起來的完全不一樣，所以先講量到的事實：

**Telegram 自己會替 `image/*` 的 document 產縮圖，也自己補
`DocumentAttributeImageSize`。** 拿改動之前上傳的三個檔案直接問 Telegram
（message 80824 / 80825 / 80830）：三個都有 `PhotoSize 'm'`（23,567 / 29,980 /
21,785 bytes），而且 `attributes` 裡就有 `DocumentAttributeImageSize` ——
那不是 bridge 送的，那時候 `_upload_segment` 只送 `DocumentAttributeFilename`。
**所以「上傳的圖沒有縮圖」從來不是 Telegram 上沒有縮圖。**

真正的 bug 只有一個：`tdapi.register()` 把 `has_thumbnail` **寫死成 `False`**。
而那個旗標是**兩邊**的閘門 ——

- bridge：`Resolver.thumbs_for` 與 `needs_warming`（`bridge.py`）不看旗標就不去找預覽，
  於是 `/rpc/thumb` **0.12 秒**回一個 404、根本沒問 Telegram（那時候訊息上已經有
  `PhotoSize 320x200, 16,489 bytes`）。DLL 分不出「沒有預覽」跟「抓取失敗」，就
  `delegating` 給內建 handler 去讀**整張原圖**（第七種「看起來只是冷資料夾慢」的假象，
  而且這次是自己造出來的）。
- 網頁：`ChonkyDrive.tsx` 的 `loadThumbnails` 也 filter `f.has_thumbnail`。

所以症狀是「上傳的圖在 `H:` 和網頁上**都**沒有縮圖」，而 Telegram 上兩邊要的東西
一直都在。backend 對這個欄位的定義本來就是 "a thumbnail is embedded in the file's
own Telegram message"（`schemas.py`），跟兩邊的閘門問的是同一件事，所以修法是
照實回報。CLAUDE.md 原本寫「那是 backend 自己有沒有產縮圖…兩件事無關」，
那句話錯了，而它正好掩護了這個 bug。

**已經註冊成 `False` 的 row 修不回來**（`PATCH /files/{id}` 只收 `parent_id` 與
`filename`），而且**重新上傳同一份位元組也沒有用**：`check_hash` 命中舊 row 走去重，
沿用那筆 row 的旗標（實測踩到：兩個位元組相同的測試檔，第二個 `already on Telegram
(1 parts)`、旗標照樣 `False`，即使那則訊息上其實有 `PhotoSize`）。

`tgio.make_preview()`（本機用 Pillow 解出 320px、≤ 20 KB 的 JPEG，連同**原圖的**
寬高一起送）**不是這個 bug 的修法** —— 對 JPEG/PNG 來說它跟 Telegram 自己做的重複。
留著的理由是 `IMAGE_EXTS` 還有 `.gif` / `.webp` / `.bmp`，Telegram 對這些不保證會做
（前端 `ChonkyDrive.tsx` 就記著 webp 走 album 會 `MEDIA_EMPTY` 且掉縮圖，
DB 確認 0/84），而且自己送的預覽尺寸是確定的 320px。實作上的兩個坑：
**尺寸與縮圖要一起送**（Telegram 會把沒有宣告尺寸的 document 縮圖丟掉），
而 `thumb` 必須是磁碟上一個真的 `.jpg` 路徑（Telethon 按檔名上傳，Telegram 不認
不像 JPEG 的東西），所以走 `tempfile` 而不是 `uploads/`／`staging/` ——
那兩個目錄都會被掃成待辦工作。只對「單一 segment 且 mime 是 `image/`」做，
產不出來一律回 None：**產不出預覽永遠不能讓上傳失敗**。

## 多帳號與上傳引擎

**這一層存在的理由是網頁端已經是多帳號的，而 bridge 以前不是。** 網頁把一般檔案
與大檔的每個 segment 分派到不同的 linked account；bridge 只有一條 session，而且
`Entry`／part 表根本沒存 `telegram_user_id`——所以**凡是存在另一個帳號底下的
file/part，bridge 一律讀不到**，跨帳號的 split file 只是最明顯的那個症狀。

### 帳號

`config.ini` 的 `accounts_file` 指向一份 JSON（格式見 `accounts.example.json`，
真檔要放在 repo 外，`.gitignore` 也擋著 `accounts.json`）。留空就是舊行為：
`session` 那一條就是 primary，也是唯一的上傳目標。

- **順序有意義。** 第一個是 primary：只有它對 backend 做 bot challenge 認證，
  也只有它負責回答 `telegram_user_id = 0` 的舊 row（多帳號之前註冊的全部是 0）。
- **`telegram_user_id` 對不上 session 真正的帳號就停用那一個帳號並說明原因**，
  其他帳號照常起來。錯誤訊息會把 session string 換成 `[redacted]`。
- **`for_read(id)` 不做 fallback。** 0 走 primary，其他值必須是「有設定且連上」
  的那一個帳號，找不到就 `AccountUnavailableError`。悄悄改用 primary 讀會拿到
  別的檔案或空手而回，那比一個明確的錯誤糟得多。
- **新上傳只會發到「有設定 + 連上 + backend 說已 linked」的帳號**
  （`eligible_upload_ids`）。沒 link 的帳號仍可讀它歷史上存的東西。

每個帳號各自持有：3 個 file slot（同時處理幾個檔）、12 個 chunk slot、
一份 `AdaptiveUploadLimiter`、一個訊息 token bucket（3/s，burst 6）。
**互不影響是重點**——一個帳號撞 FLOOD_WAIT 不該把另一個帳號也節流掉。

### 引擎

`UploadEngine.transfer_batch()` 是唯一的上傳路徑，`/game` 與一般路徑都走它：

```
指紋(2 併發) ─┐
              ├─▶ check-hash(8 併發) ─▶ 上傳(序列) ─▶ 訊息 ─▶ 註冊(8 併發，呼叫端的 pool)
下一個檔 ─────┘
```

- **指紋與 check-hash 跑在上傳前面**，各有自己的上限。一個 100 MiB 的 sample hash
  跟一個 0.5 秒的 backend 往返放同一個 pool，慢的那個會決定另一個的上限。
- **上傳那一段是刻意序列的**（在驅動執行緒上）。album 是按到達順序湊滿 10 個就送，
  而「湊滿就送、不等整批走完」正是它跟一次性 flush 的差別；把上傳並行化會讓
  批次邊界變成看誰先做完。真正的並行在更下面兩層：一個 split 的各 segment 分別
  租不同帳號，每個帳號自己有 3 個 file slot。
- **結果一產生就交給 `on_result`，不等整批結束。** 所以第一個檔在註冊的同時，
  第二個檔已經在送位元組、第三個已經在算指紋。`on_result` 因此**不可以阻塞**——
  stager 的做法是丟進 register pool 就回來。
- **去重的範圍是 `(檔名, parent)`，不是只有指紋。** 這一條是線上探測 2026-09-06
  量出來的，而且它推翻了原本的設計：**後端 `files` 表的主鍵是 `file_id`，
  `insert_file` 是 `INSERT OR REPLACE`**（TeleDrive `backend/app/services/database.py`），
  所以拿同一個 Telegram document id 用第二個名字去註冊**不會新增一筆 row，
  而是把第一筆蓋掉** —— 這個 drive 在資料模型上就是「一份 document 一個名字」。
  實測：兩個位元組相同、名字不同的 1 MiB 檔案上傳完，後端只剩一筆 row，
  而 `uploadstage` 因為看到「註冊成功」把**兩份暫存都刪了**，暫存是唯一副本。
  離線測試抓不到是因為 `tests/test_bridge_e2e.py` 的假 backend 是 append rows；
  現在它照著真的做 replace，同一個缺陷在 `/game` 也立刻現形（四個不同名、內容相同
  的 zip 塌成一筆）。
  代價要說清楚：**內容相同但名字不同就會真的再上傳一次**，多佔一份上行與 Telegram
  空間。換來的是使用者丟兩個檔就看到兩個檔。同名覆寫仍然命中去重、完全免費。
  連帶結果是 **`/game` 現在永遠不會命中去重** —— 已經打包過的名字再 stage 會被
  `PACKED_MESSAGE` 擋掉（見「已知限制」第 10 點旁邊那條），所以 `/game` 的 check-hash
  必然是 miss，只剩一次 backend 呼叫的成本。
- **`FingerprintClaims` 的 key 同樣帶 `(檔名, parent)`。** 只按指紋收斂，等於用另一條
  路徑把第一個名字的 document id 交給第二個名字，結果一模一樣。同一批裡**同名**寫兩次
  才共用一次上傳。失敗的 claim 會被移除，之後的重試拿得到新的 claim。
- **走去重那條路的註冊，事後會回頭確認那個名字真的讀得回來**（`_assert_registered`）。
  呼叫端就是靠這個回傳值決定刪掉唯一一份位元組的，而註冊可以「成功」卻沒有留下
  任何以那個名字作答的 row。讀不回來就丟 `CoverageError`：暫存留著、標成失敗，
  比一個安靜消失的檔案好。全新上傳不做這個檢查，它的 document id 沒有別人擁有。
- **覆蓋率是精確比對，不是「有就好」。** `assert_parts_cover_file` 要求 part index
  從 0 連續、沒有負數大小、而且**加總完全等於**檔案長度。去重也套同一條：
  歷史上那些只註冊了 part 0 的殘缺 split group（見「已知限制」第 6 點）因此
  不會被當成可重用的重複內容。

### 完成日誌

一個邏輯檔案跑完會在 `bridge.log` 留一行：

```
transfer complete protocol=split bytes=629145601 parts=2 hash_ms=812 check_ms=530
  thumb_ms=0 slot_ms=3 upload_ms=118442 message_ms=402 register_ms=1104
  total_ms=61230 accounts=(1, 2) rate=4.00 ceiling=None
```

**各階段是「花掉的工作量」而不是 `total_ms` 的切片**：split 的 segment 是並行的，
所以 `upload_ms` 可以比 wall clock 還大。那個讀法才有用——它說的是這個檔吃掉了
帳號多少額度。這一行印在**註冊之後**，因為一個檔要位元組在 Telegram 上、
row 在 drive 裡，才算完成；中途失敗印的是 `transfer failed`，錯誤先過 `redact`。

## 效能：這整個專案真正的難題

一般瀏覽慢的原因不是頻寬，是 **Explorer 為了畫一個檔案會去讀那個檔案**。三條路徑要分別堵：

### 1. 縮圖（`IThumbnailProvider` + `/rpc/thumb`）

Explorer 產生縮圖會讀**整個原始檔案**。實測 `IShellItemImageFactory`（Explorer 走的就是這支）
對 18.6 MB 的 PNG 產 256px 縮圖，讀滿 18,629,212 bytes、18.8 秒。檔案層面沒有捷徑：
這批 pixiv 的 JPEG 只有 JFIF + ICC，**沒有 APP1/Exif 內嵌縮圖**，PNG 格式本來就沒有。

Telegram 在每張照片、每部影片旁邊都存了一張約 200x200、平均 17 KB 的預覽圖。
Windows 唯一支援「不要讀檔案」的介入點就是縮圖處理常式。實測 6.97s → 0.05s。

註冊細節：

- 縮圖那半全寫 `HKCU\Software\Classes`，不需管理員。
- 依副檔名註冊，沒有磁碟機作用域 → DLL 必須自己判斷 `onMount`，不在掛載碟的檔案
  轉交原 handler（影像多為 `{C7657C4A-…}`、影片 `{9DBD2C50-…}`），安裝時逐副檔名記錄。
- 註冊點要同時涵蓋 ProgID 與 `SystemFileAssociations\.<ext>`：shell 解析 ProgID 優先，
  只寫後者會被 `jpegfile` / `VLC.mp4` 這類既有註冊蓋過。

### 2. 屬性（`IPropertyStore` + `/rpc/props`）

縮圖修好之後仍然慢。DLL 記錄顯示 handler 每張只花 47ms、沒有一張超過 0.5 秒，
但 Explorer **兩次呼叫之間**的間隔中位數 5.2 秒、最長 43.9 秒 —— 它另外去讀每張圖的
**檔頭取尺寸**：20 個檔案有 16 個被讀，每個 258 KB–1 MB。這條路徑是 `IPropertyStore`，
跟縮圖完全無關。Telegram 的 document attributes 本來就帶寬高與長度，
所以 bridge 一個位元組都不用下載。12.664s → 0.036s。

- Property handler **只認 `HKLM\...\PropertySystem\PropertyHandlers\<.ext>`，沒有 HKCU 版本**
  → 需要管理員，且影響全機所有使用者。
- 設定與 fallback 表要**同時寫進 HKCU 和 HKLM**：搜尋索引器以別的使用者身分載入 handler，
  只寫 HKCU 它兩者都讀不到。
- 回報 `PKEY_Image_HorizontalSize` / `VerticalSize` / `Dimensions`，影片再加
  `PKEY_Media_Duration` / `PKEY_Video_FrameWidth` / `FrameHeight`。
- **property handler 只註冊影像副檔名（`install_thumb.PROP_EXTS`）**，影片那八個不碰 ——
  碰了會把全機的影片縮圖弄掉，見「踩過的坑」最後一條。影片的 `PKEY_Media_Duration`
  那幾行留著沒刪：H: 上的影片走的是縮圖那半，屬性這半反正從來沒被 shell 載入過。

### 3. 冷資料夾 → `warmup.py`

瀏覽速度有兩種狀態。暖的時候 shell 每秒約 20 個檔案，全部來自本機磁碟。
冷的時候就是 Telegram 給多少算多少（每秒 8–33 張，看帳號最近被拉多兇），
**這個速率不是程式能提升的**。所以做法是讓資料夾不要是冷的：走一次樹填滿快取。

`/rpc/thumb` 未命中時也會在背景預抓整個資料夾（`THUMB_PREFETCH_MAX` 上限，
每批 `THUMB_PREFETCH_SLICE` 個 id）。兩件事讓它快得起來：批次讓 `get_messages`
一次涵蓋 100 個 id，而縮圖下載走**連線池**而非單一控制連線。

批次之間會等 `THUMB_PREFETCH_IDLE` 的安靜期才繼續，讓前景請求優先。
**`_thumb` 刻意不呼叫 `note_demand()`** —— 呼叫的話等待中的 `/rpc/thumb` 會一直刷新
`_last_demand`，預抓永遠等不到安靜期，等於自己擋自己。同理，預抓與整棵樹的 sweep
呼叫 `props_for(..., demand=False)`：把自己的抓取記成 demand 就是自己等自己。

整棵樹的 sweep 由 bridge **在自己的行程裡**跑（`BackgroundWarmup`，`[warmup] auto`），
不是排程去啟動 `warmup.py`。理由是所有 Telegram 請求都擠在同一個 client loop 上，
第二個行程只會變成競爭者 —— 而 `wait_for_quiet` 的禮讓只在同一個行程內看得到。
`warmup.py` 的 CLI 保留，走的是同一個 `Warmer`，只是 `quiet=0`（沒人要禮讓）。

sweep 每批之間等 `warmup.QUIET`（3 秒，比資料夾預抓的 0.1 秒長得多：預抓是在補完
有人正在看的資料夾，sweep 是投機性的）。走樹本身也會禮讓 —— 那是打 backend 的 HTTPS
不是 Telegram，但幾千次 listing 連著打一樣會拖慢每次瀏覽都要的路徑解析。

**沒有 media attributes 的檔案也要寫進快取。** `media_info` 現在對「讀得到但沒東西可報」
的訊息回 `{}`（讀不到才是缺 key），`props_for` 因此存得下這個事實。否則 `.txt` / `.zip`
這種檔案永遠算「未快取」，每一輪 sweep 都會再問一次 Telegram，永遠收斂不了。

### 4. 檔頭（`HEAD_SIZE` + `heads/`）

縮圖與屬性都答對了，沒開過的 JPEG 資料夾還是 2–5 秒一張。分開量兩條路徑就知道為什麼：
同樣 8 個冷 JPEG，**只做縮圖 13.02 秒且 8 個檔案全被讀，只做屬性 0.81 秒且一個都沒讀**。
shell 是在 `IShellItemImageFactory::GetImage` 裡、在 `IThumbnailProvider` 已經回傳
有效點陣圖**之後**，自己用 WIC 去開那個檔案 —— 那不是任何可註冊的介面，攔不掉。
（PNG 不會。試過回報 `System.Photo.Orientation` 讓它別去讀，沒有用。）

所以改成讓那個讀取變便宜：把每張靜態圖的前 `HEAD_SIZE` 存進 `meta/heads/`，
`SeekableRemoteFile` 收一個 `head` 參數，落在範圍內的讀取直接從磁碟回答。

**這是暫存檔，不是快取。** 一度整棵樹常駐存著（18,451 張圖 8.67 GB），但量出來發現
每個檔案的檔頭只會被用到一次 —— 就是它第一次被 `warmshell.exe` 那次（見第 5 節）。
之後 rclone 自己的 VFS 快取接手：shell-warm 過的檔案重讀，12 個裡 11 個在 5–13ms 內
由 rclone 本機回答，根本沒到 bridge；thumbcache 命中之後 shell 連檔案都不開。所以
常駐這份檔頭純粹是跟 rclone 快取重複的死重量。改成 `Warmer.fill()` 每一批自己的
迴圈：抓檔頭 → 跑 `warmshell` → 用 `finally` 刪掉（`Resolver.drop_heads`），
磁碟峰值從 8.67 GB 降到一批的量級。`needs_warming()` 因此**不再**檢查檔頭
存不存在 —— 檔頭不再是收斂條件，用完刪掉不會讓下一輪 sweep 又把整棵樹當成沒暖過。
`BackgroundWarmup._pass` 收尾另外呼叫 `Resolver.clear_heads()` 當保底，防止行程
在某一批中途掛掉時留下的碎片累積。

- **`HEAD_SIZE` 要蓋住的是 rclone 抓多少，不是 shell 讀多少。** shell 只讀檔頭，
  但 rclone 的 read-ahead 會放大到 252 KB（最大量到 508 KB）。128 KB 試過，
  12 個檔案還是 35.4 秒，因為每次都讀出界。設成 `REQUEST_SIZE`（512 KB，也就是
  reader 的 block 0）之後降到 3.7 秒。放大不增加往返次數 —— 反正就是一個 Telegram
  請求，而 warmup 是被請求數綁住的，不是頻寬 —— 現在只是暫存，磁碟成本是過渡性的。
- **`_head_complete` 比對長度而不是存在。** 除了 `HEAD_SIZE` 可能再調之外，
  同一批內 `heads_for` 到 `drop_heads` 之間若行程中斷，殘留的檔頭要能被正確識別
  並重用，而不是被當成「已經處理過」直接跳過。
- **split 檔案直接排除。** 檔頭是從 `entry.message_id` 讀的，那只有在單一 part 時
  才是邏輯檔案的開頭。靜態圖離 500 MiB 的切割門檻很遠，所以不花成本。
- **只在 sweep 裡做，不在資料夾預抓裡做。** 一個檔頭是真的檔案讀取，預覽只是 20 KB
  現成的東西；一百個檔頭要一分半。擋在正在看資料夾的人前面是錯的取捨。
- PNG 也一起暖。四個冷 PNG 資料夾沒抓到它讀檔，但那不足以拿來當「這個副檔名免除」
  的依據，代價是多一類莫名其妙變慢的檔案。

### 5. Windows 自己的 thumbcache → `warmshell.exe`

上面三層全部命中，冷資料夾也只有每秒 3 張左右 —— 因為不管我們答什麼，shell 每個檔案
還是有它自己的工作要做。**看過一次的資料夾是每秒 274 張，而且連 handler 都不會被呼叫**，
差別在 `thumbcache_*.db`，Python 這邊沒有任何東西寫得進去。

唯一的入口就是用 Explorer 的方式去要縮圖，讓 shell 自己存起來。`warmshell.exe`
從 stdin 讀 UTF-8 路徑，每個呼叫 `IShellItemImageFactory::GetImage`
（`SIIGBF_THUMBNAILONLY` 不能省，否則 shell 可能回一個檔案類型圖示、根本沒碰檔案，
那什麼都沒快取到）。實測同一個冷資料夾跑兩次：第一次 handler 被呼叫 10 次、
第二次 **0 次**。

| 狀態 | 速率 |
|---|---|
| 全冷 | 0.3 張/秒 |
| bridge 端全暖（預覽+屬性+檔頭） | 1.5–3 張/秒 |
| **thumbcache 命中** | **274 張/秒** |

- **每一輪都跑，而且不記錄做過什麼。** thumbcache 是 Windows 的，磁碟清理會清空、
  它自己也會修剪。記住「已經做過」的預熱，會正好在它填的快取被丟掉時安靜下來。
  重跑已經在快取裡的檔案是一個 4ms。
- **`SHELL_BATCH` 要小（25）。** 這是 shell warm 唯一的禮讓點，而一個冷 JPEG 要
  0.7 秒，批次 200 等於連續佔線兩分鐘。
- **這一層刻意會登記成 demand**（跟預覽/屬性那兩層相反）：它的讀取真的走 rclone 和
  provider 出去，跟有人在瀏覽分不出來。效果是每批之後多等 3 秒，不是自己等自己 ——
  下一次 `_yield_` 跑的時候讀取已經結束了。
- **`pending(start_id, base)` 的 `base` 不能省。** walk 是從它被告知的起點開始編路徑的，
  所以只暖一個子樹卻不給 base，三層深的檔案會變成 `H:\photo.jpg`。shell 對這種路徑
  瞬間回答、什麼都沒暖 —— 在計時上跟成功完全一樣。`shell_warm` 因此回報 exe 真正暖成
  的數量，不是送出去的數量。
- **這一層整個死掉幾週而沒人看得出來，因為它報不出任何東西。** 一份 log 裡 87 批
  全是 `shell warm stopped after 0/100: ... timed out after 600 seconds` —— 每 100 個檔
  燒掉十分鐘、一輪 sweep 的 wall clock 幾乎全在這裡，而 `thumbcache_*.db`（274 張/秒
  那一層）**從來沒被填過一筆**。那個平坦的 600 秒等於一個檔 24 秒，所以「掛載掉了」跟
  「一個檔案把 shell 卡住」在 log 上完全一樣，而 `capture_output` 在被 kill 之後把
  stdout 上那個唯一的數字也丟了。三個改動：
  - `warmshell.exe` 每個檔一結束就往 **stderr** 印 `+ <ms> <路徑>` / `- <ms> <路徑>`
    並 flush（narrow UTF-8，不是 `fwprintf` —— 寬字元輸出會被轉成 console codepage，
    而這裡的路徑大半是非 ASCII，那正是「URL 跳脫」那條坑的同一種死法）。
    被 kill 的批次因此還是說得出暖成幾個、以及**還開著哪一個**。
  - 期限按檔數算（`SHELL_SECONDS_PER_FILE`，10 秒/檔），而不是不管幾個檔都 600 秒。
  - 卡住就**停掉這一輪的 shell warm**，不再把後面 24 批排在同一個問題後面。

  修完在真的 `H:` 上量同一個 chat import 資料夾：冷的一批 25 個檔 **2.7 秒暖成 25 個**
  （9.4 檔/秒），同一批立刻再跑 **0.1 秒**（403 檔/秒）—— 也就是 thumbcache 真的被填了。
  對照那個平坦的 600 秒：健康的一批只花它的 0.5%，所以那個上限從來不是保護，只是
  把「壞了」偽裝成「很慢」。
- **`H:` 沒掛載的時候不要問 shell。** `SHCreateItemFromParsingName` 對不存在的磁碟機
  是微秒級失敗，於是 25 個檔「一個都沒暖成」瞬間回來 —— log 上跟「handler 答錯了」
  一模一樣（同一份歷史裡兩種形狀都有：`25 of 25 in this batch produced nothing`，
  以及撞滿期限的那些）。`_mount_ready()` 先看磁碟機在不在，不在就跳過並說一次。

### 6. 每條連線的深度（`READS_IN_FLIGHT`）

前面五層都是「不要讀檔案」。真的要讀的時候（影片播放、整檔複製、`fetch-local`），
速度由兩個數字相乘決定，而以前只有一個：

- `DOWNLOAD_CONNECTIONS`（8）—— 幾條獨立連線。這個已經量過（交錯取樣，8 條比 1 條快
  1.72 倍），而且**不能再往上加**：16 條以上開始收到 `Server closed the connection`。
- `READS_IN_FLIGHT`（2）—— **每條連線同時有幾個未完成的 GetFile**。MTProto 是多工的，
  一條在等回覆的連線可以先把下一個請求送出去；一條一個的話，每條連線在兩個 chunk
  之間就是整整一個往返在閒著。

`_read` 是一次 `gather` 把整個寬度發出去的，所以「同時有幾個請求」其實是由讀取寬度
決定的 —— `STREAM_BLOCK_SIZE` 因此是 `REQUEST_SIZE × DOWNLOAD_CONNECTIONS ×
READS_IN_FLIGHT`（8 MiB），不是只填滿連線數的 4 MiB。連帶 `BLOCKS_CACHED` 也要
跟著寬度走（16 個 block）：block 快取比一次讀取還窄的話，`_blocks_for` 會把自己
剛剛抓回來的那批前半段馬上丟掉，下一個要同一段的人（rclone 用更小的片段回頭問、
zipfile 往回 seek）就要再付一次網路。

預覽那條路早就是每條連線 2 個（`THUMB_CONCURRENCY = DOWNLOAD_CONNECTIONS * 2`）
而且沒有招來 FLOOD_WAIT —— 深度跟連線數不一樣，加深度不會被 Telegram 當成新連線洪水。

### 7. Metadata 的往返次數（`meta/dirs/` + 併發 listing）

**後端是刻意留在遠端的**（bridge 在一個網路、backend 在另一個，經 Cloudflare），
所以每一個 metadata 呼叫就是 **0.52 秒**，其中 0.36–1.2 秒是 connect、加上 TLS 到 2.7 秒。
這一層沒有「讓往返變快」的辦法，只有「少跑幾次」。

成本模型量出來非常乾淨 —— 一次 `resolve pixiv/user-955496` 是 **4 個循序呼叫、2.1 秒**：

```
GET /folders  0.52s ┐ 第 1 層：pixiv 在哪
GET /files    0.52s ┘
GET /folders  0.53s ┐ 第 2 層：pixiv 裡有什麼
GET /files    0.52s ┘
```

**每一層路徑 = 2 個呼叫。** 所以「已經在資料夾裡、點一個子資料夾」是 2 × 0.52 = **1.06 秒**，
而且**跟資料夾裡有幾個檔完全無關**（2 個項目和 84 個項目一樣快）—— 這是分辨
「往返延遲」與「資料量」的關鍵證據，也是為什麼這條跟縮圖無關：縮圖會隨檔數變多。

三層答案，全部只為了少跑往返：

- **`/folders` 與 `/files` 併發**（`_list_both`）。兩個獨立的 GET，循序做等於每層付兩次。
  一次 listing 開一條執行緒而不是用 pool —— pool 大小若照這裡設，wsgidav 的工作執行緒
  會互相排隊，而一條執行緒的成本是微秒級，對面是半秒。
- **listing 存到磁碟**（`meta/dirs/<parent_id>.json`，一個資料夾一個檔）。重啟後
  「這個 session 第一次點」不必再付。**一個資料夾一個檔而不是共用一份 JSON**：listing 是
  這裡唯一「會變」的快取，所以 `JsonStore` 的 merge-on-flush（last-writer-wins，只因為
  它的值永不改變才安全）不適用；而共用一份的話，sweep 走幾千個資料夾就是幾千次
  數十 MB 的重寫。
- **兩層共用同一個 `dir_cache_seconds`**（預設從 60 秒改為 **3600**，對齊 rclone 的
  `--dir-cache-time`）。它們回答同一個問題、以同樣速度過期，分兩個 TTL 是假的區分。

**sweep 是唯一的更新機制**，所以 `warmup.walk` 預設 `fresh=True`：它在第二輪之後的
全部意義就是發現網頁端新上傳的東西，讀回自己填的快取就等於對那件事失明，也會讓
`meta/dirs/` 到期而不是被續命。`invalidate()`（`/rpc/forget`）**必須連磁碟一起刪** ——
只清記憶體的話下一次 listing 直接把剛剛「忘掉」的東西讀回來，看起來像成功了。

實測（同一批子資料夾，`/rpc/forget` 之後為冷）：

| | 改之前 | 改之後 |
|---|---|---|
| 第一次點，什麼快取都沒有 | 1.06 s | **0.58 s**（每層一個往返） |
| bridge 重啟後第一次點，之前列過 | 1.06 s | **0.016 s**（從 `meta/dirs/` 來） |
| TTL 內重訪 | 0.02 s | 0.02 s（TTL 從 60 秒變 1 小時） |

## 踩過的坑

- **`cryptg` 不是可選的。** 少了它 Telethon 退回純 Python AES-IGE，解密本身就把下載壓在
  ~0.15 MiB/s，連線開再多都沒用。啟動 log 要看到 `cryptg detected`。
- **連線池的 round-robin 別用請求內索引。** `pool[i % len(pool)]` 裡的 `i` 若是單一請求內的
  chunk 序號，所有單 chunk 讀取都會打到 `pool[0]`。要用跨請求的 cursor。
- **`download_media` 走的是單一控制連線**，縮圖預抓要自己發 `GetFileRequest` 到池子裡。
- **MTProto 請求不能跨 1 MiB 邊界**，否則 `LimitInvalidError`。對齊要用
  `offset - (offset % REQUEST_SIZE)`，不是對齊到更大的單位。
- **`BLOCK_SIZE` 不等於 wsgidav 的 `block_size`。** 曾經把 BLOCK_SIZE 設成 4 MB，
  結果一次 64 KB 的讀取要 3.95 秒。分成 `BLOCK_SIZE`（512 KB，配 `_blocks_for` 批次）
  與 `STREAM_BLOCK_SIZE`（給串流用）。
- **後端的 `filesize` 會灌水。** 上傳端以 512 KB 為單位切塊，後端存「塊數 × 512 KB」，
  比真實長度大最多一塊（實測 523,424 bytes）。真實長度在 `file_hash` 的 `:<n>` 後綴，
  由 `Entry.real_size` / `_clip_parts` 裁掉。照著 `filesize` 宣告會讓客戶端讀不存在的尾巴 ——
  等一段長 timeout 後拿到 0 bytes，非 faststart 的 MP4 因此完全無法起播。
- **`JsonStore` 兩個寫入者會互相清空。** bridge 和 `warmup.py` 同時開著時，`flush()` 若寫出
  整份 in-memory dict，後寫的那個會蓋掉對方 —— 實測屬性快取從 21,228 筆掉回 2,371 筆，
  瀏覽速度整個垮掉。`flush()` 必須先讀回檔案再 merge。所有值都衍生自不可變的 Telegram
  訊息，所以 last-writer-wins 是安全的。
- **URL 跳脫不能用 `iswalnum()` 判斷 UTF-8 位元組。** 它收的是寬字元，所以續接位元組
  `0xE6` 被當成 `U+00E6`（`æ`，是個字母）而原樣送出，鄰居卻被跳脫 —— `湊あくあ` 變成
  `æ¹%8Aã%81%82…`，bridge 解不出路徑。**含非 ASCII 的路徑因此縮圖與屬性全部 404**，
  DLL 退回內建 handler 去讀整檔，等於那些資料夾完全沒有這個專案。修法是明確列 ASCII
  範圍，連 `isalnum()` 也不用：CRT 的 locale 是宿主行程決定的，不該依賴。
  症狀跟「冷資料夾慢」一模一樣，但成因無關 —— 分辨方法是看 DLL 記錄有沒有 `delegating`。
- **DLL 的 Settings 一次性初始化必須交給 compiler，不能自己寫旗標。** `GetSettings()` 曾經是
  `static bool loaded; if (loaded) return settings; loaded = true;` 然後才去讀 registry ——
  旗標在讀取**之前**就立起來了。Explorer 進一個資料夾會同時起好幾個執行緒，第一個還在讀
  registry 的那幾百微秒內，其他執行緒看到旗標已立就拿走**還是空的** `settings`：`root` 是
  空字串，`OnMount()` 於是回 false，那些檔案全部 delegate 給內建 handler 去**讀整個原圖**。
  實測一個新的 COM surrogate，同一毫秒進來的 4 個檔案中了 3 個（一個記成 `onMount=0`，
  兩個 `preview fetch failed`），6 個檔案 9.36 秒；改成 magic static
  （`static const Settings settings = LoadSettings();`）之後 8 個檔案 0.177 秒、
  `delegating` 0 次、`onMount` 全是 1。這是第三種「看起來只是冷資料夾慢」的假象
  （另兩種是 URL 跳脫與 FILE_MIGRATE），而且它**只在 handler 冷載入的頭幾百微秒發作**——
  也就是每次 Explorer 進一個新資料夾的那一刻，剛好是最需要它答對的時候，暖起來之後
  單獨重試同一個檔案又完全正常，所以很容易被當成「就是冷」。連帶後果是 `warmshell.exe`
  每一批都撞 600 秒 timeout 並回報 0 個暖成（log 連續 50 批全是 `shell warm stopped
  after 0/...`），Windows 自己的 thumbcache——274 張/秒 那一層——因此從來沒被填過。
- **寫進 `/game` 的每一個新檔，都會先問 backend 它存不存在。** `_resolve_game` 第一步查
  staging，但**新檔案在 PUT 之前必然不在 staging**，於是往下走 `game_children()` 和
  `api.resolve()` —— 兩條都是打 TeleDrive backend 的 HTTPS。實測複製 300 個 512 KB 的檔案
  進 `H:\game` 要 **52.6 秒**，同一批複製到本機 E: 只要 0.2 秒，而 bridge 自己收一個 1 MB
  的 PUT 是 **3 毫秒** —— 成本全在路徑解析，不在寫入，也不在「rclone 快取 + staging 兩份
  寫入」（複製結束當下 staging 還是 0 個檔，第二份根本不在關鍵路徑上）。修法是 step 1b：
  staging 未命中但**父目錄在 staging 裡**時直接回 `MISSING`，本機目錄列表已經是完整答案，
  backend 不可能有同一條路徑的子項目。**52.6 秒 → 4.2 秒。** 只在第二層以下短路 ——
  `/game/<top>` 自己還是要問 backend，因為 staging 裡的 `<top>` 和已上傳的 `<top>.zip`
  是兩個不同的名字，只有 backend 知道後者。連帶效果在 rclone 的 writeback 佇列上更誇張：
  原本 785 個檔卡著每秒排不掉一個，現在 300 個檔在複製結束後幾乎立刻排空。
  診斷方法是看 `bridge.log` 的 stack trace 有沒有 `get_resource_inst → resolver.resolve
  → tdapi._list_paginated`；backend 一不穩（`ConnectionError` 重試）就會把每檔的成本
  從毫秒放大到秒。
- **`keep_alive_conn_limit = 0`。** Windows 上 cheroot 的 connection manager 不會在
  閒置連線變成可讀時被喚醒，只能輪詢，上限寫死 50ms（`cheroot/connections.py`：
  "select() does not return when a socket is ready"）。重用連線上的每一個請求因此都要
  等下一輪：實測 50ms 對比新連線的 1ms，shell handler 的每一次 `/rpc` 和 rclone 的
  每一次 range 讀取都在付。loopback 開新連線只要 0.2ms，沒有什麼好 keep alive 的。
  用 curl 量不出來 —— curl 每次都是新行程新連線，要用 WinHTTP 才會重現。
- **量測會被 Windows 自己的縮圖快取（`thumbcache_*.db`）騙。** 看過一次的資料夾再測，
  回應 0.04 秒但**根本沒呼叫到 handler**。用 `shellthumb\bench.exe` 或把
  `HKCU\Software\TeleDriveWebDAV\LogPath` 設成檔案路徑來確認 handler 真的有跑：

  ```
  DllGetClassObject
  Initialize: H:\pixiv\user-9016\142759167_p0.jpg
  GetThumbnail: cx=256 onMount=1 path=...
    preview 30828 bytes        <- 成功。出現 "delegating" 表示退回讀整檔
  ```

  完全沒有記錄，就代表 Windows 用了自己的快取。刪掉那個登錄值即關閉。
- **PowerShell 寫的縮圖測試工具不可靠。** `flags=0` 會回一個圖示但根本沒碰檔案；
  介面在兩個 statement 之間傳遞會拿到 `E_NOINTERFACE`。用原生的 `thumbprobe.exe` / `bench.exe`。
- **Telethon 的 `__call__` 收下 `flood_sleep_threshold` 就丟掉。** `client(request, flood_sleep_threshold=...)`
  簽章有這個參數，但實作（`client/users.py:29-30`）直接 `return await self._call(self._sender, request, ordered=ordered)`，
  完全沒有傳下去 —— per-call 覆寫是假的，真正睡覺的那行讀的是 `self.flood_sleep_threshold`。
  `tgupload.send_part` 因此完全繞過 `_call`，直接 `client._sender.send(request)`：這樣才能讓
  FLOOD_WAIT 老實地 raise 上來給自己的 pacer 處理，而不是被 Telethon 用同一顆 client
  的全域設定靜默吞掉，也才不會誤觸 `_call` 裡按請求型別記憶的 `_flood_waited_requests` 閘門
  （一次 flood 後，同型別的下一個請求會直接 raise 或自己先睡，讓並行送出的其他 part 全部誤判）。
- **512 KB 的本機讀取不能在 `tg-loop` 上做。** 那條 asyncio loop 同時服務所有 rclone range read
  與 `/rpc/*`，同步的 `seek()+read()` 會直接卡住瀏覽。`tgupload._PartReader` 用單執行緒
  `ThreadPoolExecutor` 跑這兩個呼叫 —— 單執行緒本身就是鎖（seek+read 不是 atomic），
  同時滿足「離開 loop」。win32 沒有 `os.pread`，這是唯一乾淨的做法。
- **`asyncio.gather()` 不會取消手足 task。** 一堆平行 task 裡第一個丟例外，`gather()` 會立刻
  把例外傳出來，但**其他還在跑的 task 不會被取消**，會繼續吃併發額度與頻寬上傳一個
  已經不可能 commit 的 segment。`tgupload.upload_file_parts` 在 `except` 裡明確
  `t.cancel()` 每一個未完成的 task，再 `gather(..., return_exceptions=True)` 排空。
- **縮圖不能用裸的 `client(GetFileRequest(...))`。** 文件的 `dc_id` 跟 session 的 DC 不同時，
  Telegram 回的是 FILE_MIGRATE，而 Telethon 的 `_call`（`client/users.py:126`）只跟隨
  Phone/Network/User 三種 migrate，**檔案那種是在 `iter_download` 裡處理的**
  （`client/downloads.py`：開頭就依 `dc_id` 借一個 exported sender，`FileMigrateError`
  再重試）。所以 `_thumbnail_bytes` 一律走 `client.iter_download(location, dc_id=doc.dc_id, ...)`。
  症狀是整個資料夾的預覽同時失敗、log 刷
  `thumbnail for message N failed: The file to be accessed is currently stored in DC 1`，
  DLL 於是退回讀整檔 —— 又是一種「看起來只是冷資料夾慢」的假象（另一種是 URL 跳脫那條）。
  一般讀取不會中這個坑，因為 `_chunk` 本來就走 `iter_download`。
- **一次送整批預覽會自己招來 FLOOD_WAIT。** `_thumbnails` 曾經把整個
  `THUMB_PREFETCH_SLICE`（100 個 id）一口氣 `gather` 出去，Telegram 回 FLOOD_WAIT，
  而這些呼叫走的是 `_call` —— 它先自己睡（log 上一排 `Sleeping for 2s on GetFileRequest
  flood wait`），再按請求型別把 `_flood_waited_requests` 閘門架起來，於是**同一批裡其他
  預覽也一起被拖累或直接失敗**（跟上傳那條同一個閘門）。現在由 `THUMB_CONCURRENCY`
  （每條連線 2 個）節流，semaphore 存在 worker 上並在 client loop 裡建立，讓資料夾預抓
  跟前景請求共用同一個上限，不會疊加成兩倍爆量。`_thumbnail_bytes` 也補上跟 `_chunk`
  一樣的短 FLOOD_WAIT 重試。
- **八個 client 共用一份 session，會在第一個跨 DC 檔案上互相打掉 exported auth。**
  `_borrow_exported_sender` 是每個 client 各自一份，所以一批裡第一個跨 DC 的檔案會讓
  8 條連線同時 `ExportAuthorization` + `ImportAuthorization`，其中幾個被 Telegram 以
  `AUTH_BYTES_INVALID`（「The provided authorization is invalid」）打回。實測一輪 sweep：
  開頭兩分鐘掉 19 張預覽，之後 1,679 次抓取一次都沒有。Telethon 既不重試，也不會把
  import 失敗前就已經連上的 sender 斷掉 —— **log 裡成對的 `Task was destroyed but it is
  pending` 就是它**（19 次失敗 × 4 條 task = 76 條，數字對得上）。`_thumbnail_bytes` 因此
  把 `AuthBytesInvalidError` 也當成暫時性錯誤重試（`_is_export_race`），換一條連線再試。
- **property handler 不能碰影片副檔名 —— 影片的縮圖是從屬性來的。** 影像有自己的
  `IThumbnailProvider`（`{C7657C4A-…}`，自己解碼檔案），影片**沒有**：
  `HKCR\.mp4\ShellEx\{e357fccd-…}` 指的是 shell32 的 Property Thumbnail Handler
  `{9DBD2C50-…}`，它是去 **property store 拿 `System.ThumbnailStream`**。所以把
  `PropertyHandlers\.mp4` 換成我們的 CLSID，等於把全機（不只 H:）的影片縮圖整個拔掉 ——
  而且不是「我們答錯」，是**這個 DLL 在影片副檔名上根本不會被載入**：開了 `LogPath`
  去 probe 一個本機 .mp4，一行都沒有，`SHGetPropertyStoreFromParsingName` 回 0x8007000D
  （同一顆 DLL 同一個 CLSID，換成 .jpg 就正常載入並記錄），所以 handler 裡面也沒有東西
  可以修。同一個檔案複製成兩個名字量：`.mp4` 完全沒有縮圖，`.m4v`（我們沒註冊、同樣走
  `{9DBD2C50}`、用 Windows 自己的 property handler）0.19 秒就有。
  修法是 `PROP_EXTS = IMAGE_EXTS`，`install_props()` 另外把舊安裝claim過而現在不claim的
  副檔名還回去（`_release_props`），所以重跑一次 `--install-props`（要管理員）就修好。
  **不損失任何東西**：那八個副檔名的 handler 從來沒被載入過，H: 上的影片本來就沒從
  Telegram 拿到 duration/寬高；而屬性這半當初量到的 12.664s → 0.036s 全部是影像的檔頭讀取。
  症狀是第四種「看起來只是冷資料夾慢」的假象，而且它連 H: 都不在 —— 使用者看到的是
  「其他正常資料夾的影片沒有縮圖」。診斷方式：`isolate.exe thumb <資料夾>` 看 answered
  數，再把同一個檔案改成 `.m4v` 對照。

- **轉交 fallback handler 少了 `IInitializeWithItem`，影片就一張都沒有。** `Delegate()`
  原本只試 `IInitializeWithFile` 跟 `IInitializeWithStream`；影片的 fallback
  `{9DBD2C50-…}`（Property Thumbnail Handler）**兩個都不支援**，它要的是 shell item
  —— 因為它是從 item 開 property store 去拿 `System.ThumbnailStream` 的。兩個都 QI 失敗
  時 `hr` 停在 `E_FAIL`，`Delegate()` 就把 `E_FAIL` 回給 shell，shell 不會再去問別人，
  於是**全機**（註冊是按副檔名的）不在 H: 上的影片縮圖全部消失。修法是中間插一段
  `SHCreateItemFromParsingName` + `IInitializeWithItem`。實測同一個檔案：修之前 `.mp4`
  0/3 有縮圖、對照組 `.m4v`（沒被我們接手）3/3；修之後 `.mp4` 3/3。
  這個坑跟上面那條（property handler 不要碰影片）是**兩個獨立的 bug，兩個都要修才會好**：
  只修一個的話另一個照樣把影片縮圖擋掉，而且症狀完全一樣。
  另外 `TeleDriveProps.cpp` 的 `InitDelegate()` **刻意不加** `IInitializeWithItem` ——
  property handler 用 shell item 初始化會再繞回 property store 的查表，也就是繞回自己。

- **不是每個檔案都是 document —— chat import 進來的是 photo，而整個 `tgio` 是照
  document 寫的。** backend 的 chat-media import 直接把聊天室裡的訊息註冊成檔案，
  那些是 `MessageMediaPhoto`；`_fetch_document` 只認 `msg.document`，於是那些 entry
  的**讀取、縮圖、屬性三條路一起死**：bridge 回 500，而 DLL 分不出 500 跟「這個檔沒有
  預覽」的差別（見上一條），照樣 `delegating` 去讀整檔 —— 那個讀取也是 500。
  平常看不出來，直到 warmup 走到那個資料夾：實測一個 3,756 個檔的資料夾裡有 **2,066 個**
  是 photo，一輪 sweep 下去 Telegram 把 8 條 pool 連線全部踢掉
  （log 刷 `Connection closed while receiving data: 0 bytes read`），此後**所有**讀取停擺，
  讀 16 bytes 要 202 秒，Explorer 整個卡死 —— 使用者看到的是「H: 打不開」，
  第六種假象，而且這次連根目錄都還列得出來，所以更難認。
  修法是 `tgio` 全面接受兩種 media：
  - **`_media_size`**：document 是 `.size`；photo 是 `sizes[-1]` 的位元組數。
    **`PhotoSizeProgressive` 沒有 `size`，它的 `sizes` 是每一遍 progressive scan 的
    累積長度，所以整張圖是最後一個元素，不是總和** —— 加總會多報三倍，客戶端就讀過界。
    拿四則真實訊息對過，`sizes[-1]` 的位元組數跟 backend 記的 filesize 完全相等
    （62678 / 184878 / 283437 / 37317，四個全中）。
  - **`_best_thumb`**：document 的 `thumbs` 全是小圖，取最大的就對；**photo 的 `sizes`
    不是** —— 它一路排到接近原圖（實測一張 283 KB 的圖，`m` 是 32 KB 而 `x` 是 150 KB）。
    所以 photo 走 `THUMB_PREVIEW_MAX`（64 KB）上限取最大的那個，取不到再退回整份清單，
    寧可拿一張過大的預覽也不要回 404 把 shell 推去讀原圖。
  - **`_thumbnail_bytes`**：photo 要用 `InputPhotoFileLocation`，用
    `InputDocumentFileLocation` 會被回 LOCATION_INVALID。
  - **`_media_attributes`**：photo 沒有 `attributes` 也沒有 `mime_type`，寬高從
    `sizes[-1]` 拿，mime 固定 `image/jpeg`。
  修完同一個資料夾：**12 個檔 0.23 秒、51.2 張/秒、`delegating` 0 次**，整檔下載的長度
  與 backend 記錄逐位元組相符且以 `ffd9` 收尾。
  **`entry.has_thumbnail` 不是決定這條 photo/document 分支的東西** —— 這條講的是
  media 的**型別**，chat import 進來的旗標一樣是 `True`。但那個旗標**確實**是
  「要不要去找預覽」的閘門（`Resolver.thumbs_for`、`needs_warming`，網頁那邊的
  `loadThumbnails` 也一樣），而 backend 對它的定義就是「Telegram 訊息身上有沒有
  內嵌縮圖」—— 這裡曾經寫成「跟 bridge 要的預覽兩件事無關」，那句話錯了，
  而它正好掩護了 `register()` 把它寫死成 `False` 的那個 bug
  （見「一般路徑的寫入」那一節）。

- **重啟 bridge 會留下孤兒 `warmshell.exe`。** 那 600 秒的期限住在父行程的
  `subprocess.run(timeout=600)` 裡，所以殺掉 bridge 之後 `warmshell` 還在跑，而且
  再也沒有人會把它 timeout 掉。它繼續向 shell 要縮圖、繼續透過 `H:` 讀檔，正好跟
  「重啟是為了讓瀏覽變快」對著幹。`restart.bat` 因此連 `warmshell.exe` 一起
  `taskkill` —— 它沒有任何需要收尾的狀態。

- **後端斷一條閒置的 keep-alive 連線，代價是一次整檔下載。** `tdapi._call` 用
  `requests.Session` 對 backend 保持連線池，而 uvicorn 幾秒沒動就把閒置連線關掉 ——
  下一個請求拿到那個死掉的 socket，在伺服器讀到任何一個位元組**之前**就
  `RemoteDisconnected`。原本沒有重試，於是它變成 `/rpc/thumb` 的一個 500。
  **`/rpc/thumb` 的 500 不是「縮圖慢一點」** —— DLL 分不出它跟「這個檔沒有預覽」的差別
  （兩者都只是 fetch 失敗），於是 `delegating` 給內建 handler，內建 handler 去讀
  **整張原圖**。實測 log 上就是一條從 offset 0 排到 6.8 MB 的循序下載，幾個這種就把
  連線池吃光並招來 FLOOD_WAIT，`warmshell` 那一批 25 個檔於是撞滿 600 秒 timeout、
  回報 `shell warm stopped after 0/100`，然後下一批再來一次 —— 前景瀏覽就一直轉。
  修法是 `_call` 對 `ConnectionError` 重試一次；**因為請求根本沒抵達 app，POST 重試也是
  安全的**。重試預算跟 401 重登的預算要**分開兩個旗標**，否則一條死 socket 會把重登的
  額度用掉（restart 過的 backend 正好會先回 401）。
  診斷方式：`bridge.log` 找 `rpc /thumb failed` 跟 `RemoteDisconnected`，再開
  `HKCU\Software\TeleDriveWebDAV\LogPath` 看 DLL 有沒有 `preview fetch failed -> delegating`
  —— 這是第五種「看起來只是冷資料夾慢」的假象。

- **後端把 `POST /auth/login` 拿掉了，症狀是 `H:` 整個打不開。** TeleDrive commit
  `22734f4 "security: restore the metadata-only boundary and harden the deployment"`
  移除了「拿 Telethon StringSession 換 JWT」那個端點 —— 理由是對的：交出 auth_key 等於
  把整個 Telegram 帳號交給後端，而這座 bridge 存在的意義就是守住那條界線。取而代之的是
  bot challenge：`POST /auth/challenge` 拿一個 nonce → **由要被驗證的那個帳號**把 nonce
  DM 給指名的 bot → `POST /auth/verify` 換 JWT（202 表示 bot 的 `getUpdates` 長輪詢還沒
  收到，繼續等；401 是 nonce 過期或不存在）。身分證明是那則 update 的 `from`，所以線上
  沒有任何秘密。
  **這在網頁上是互動流程，在這裡不是** —— bridge 手上本來就有使用者的 Telethon client，
  自己把 DM 送出去就好，全程無人介入（`TelegramWorker.send_dm`，由 `bridge.main` 用
  `api.set_dm_sender(worker.send_dm)` 注入；`tdapi.py` 是 metadata 那一半，不該自己碰
  MTProto）。代價是每張 token 一則 bot DM，JWT 活 24 小時又有 `token.txt` 撐過重啟，
  大約一天一則。
  **沒修之前的故障鏈長得完全不像 auth 問題**：`/auth/login` 404 → 拿不到 JWT →
  每個 `/folders`、`/files` 都 401 → `_call` 以為是過期 JWT，於是無限重登（`bridge.log`
  刷滿 `JWT rejected — re-authenticating`）→ PROPFIND `/` 回 500 → rclone 沒有樹可以掛 →
  Explorer 說「因為 I/O 裝置錯誤，所以無法執行要求」。**先打一次
  `curl -X PROPFIND 127.0.0.1:8081/`**：500 就是 bridge 這層，跟 rclone、WinFsp、
  縮圖 handler 都無關，再往 `bridge.log` 找真正的 4xx。
- **檔案的 DC 不是 session 的 DC，而 Telethon 的 exported sender 有一個 60 秒的計時器。**
  這個帳號的 session 在 DC 5（`91.108.56.140`），而 **chat import 進來的檔案在 DC 1** ——
  轉發進來的 media 保留來源 chat 的 DC（TeleDrive 前端 commit `4d397f8` 記的同一件事），
  所以那些資料夾的**每一次**讀取和**每一張**預覽都是 exported sender 在答。
  實測 `少女镇2.0版本重生/photo_20260822_222816_24239.jpg`（message 79357）：
  `file dc 1 / session dc 5`，預覽 32,092 bytes；自己上傳的 `/game` split part
  （message 24716）則是 `dc 5`，走主連線、不經過這條路。Telethon 每次下載借一條、下載結束還回去，
  最後一條還回去之後 60 秒（`_DISCONNECT_EXPORTED_AFTER`）就把它斷掉
  （`telethon/client/telegrambaseclient.py`）。60 秒在這裡什麼都不是 —— sweep 兩批之間的
  空檔、沒人點的那一分鐘 —— 於是下一批預覽會讓 8 條 pool 連線**同時**重連同一個 DC，
  Telegram 的回答是關連線。實測一份 `bridge.log`：248 次 `Disconnecting borrowed sender
  for DC 1`、387 次重連、138 次 `Server closed the connection`。
  **每一次關掉都是一張失敗的預覽，而失敗的預覽不是「慢一點的縮圖」**：DLL 分不出它跟
  「這個檔沒有預覽」的差別，於是 `delegating` 去讀整張原圖（見上面第五種假象）。
  修法是 `_pin_exported_sender`：每個 pool client 對那個 DC 借一次、**永遠不還**，
  參照計數就不會回到 0，`should_disconnect()` 永遠不成立，連線活到 bridge 結束。
  每次下載照樣在這條之上自己借還，Telegram 真的把連線關掉時 MTProtoSender 也照樣自己
  重連 —— 差別只在不再由計時器主動拆掉再 8 條一起重建。
  這跟 TeleDrive 前端的 `senderDcFor`（commit `4d397f8`）是同一件事的兩面：**誰來答
  這個 GetFile 值得講清楚，因為答錯了只會表現成「這個資料夾很冷」，永遠不會表現成錯誤。**
- **`bridge.log` 裡的 `Starting indirect file download` 不是警告，但它是一個埋著的 2 倍。**
  Telethon 選 `_DirectDownloadIter` 的條件裡有一條 `offset % limit == 0`，而那個 `limit`
  是**檔案的 chunk 數**（500 MiB / 512 KiB = 1000），不是 `request.limit` ——
  拿位元組 offset 去模一個數量。512 KiB 的倍數模 1000 只有每 125 個才是 0，所以
  **約 99% 的 chunk 讀取走的是 `_GenericDownloadIter`**。目前不多花位元組：
  那條路徑先算 `bad = offset % request.limit`，而 `SeekableRemoteFile` 的 block 是
  512 KiB 對齊、part 大小也是 512 KiB 的整數倍，所以 `bad` 永遠是 0，一個 chunk 還是
  一個 GetFile。但 `bad != 0` 的時候它要**抓兩次 512 KiB 才湊得出一個 chunk** ——
  也就是說任何一天有人讓 `_read` 收到非 512 KiB 對齊的 offset（自己算的 range、
  改了 `BLOCK_SIZE`、part 大小不再是 512 KiB 的倍數），讀取成本就默默變兩倍。
  要擋掉的話 `_chunk` 傳 `limit=1` 就永遠是 direct（反正只取一個 chunk），
  但那沒有量到差別，所以沒改。
- **Telethon 每一個 `iter_download` 都會在 INFO 印一行，而這座 bridge 每 512 KiB 一個 `iter_download`。**
  所以 `Starting direct file download in chunks of 524288 at 0, stride 524288` 的速率
  就是讀取速率 —— sweep 的 shell warm 走遍整棵樹的靜態圖，而 shell 每一張都要自己
  讀一次檔頭（見「效能」第 4 節），因此持續約 **360 行/分**。實測一份 `bridge.log`：
  2,100 行裡 **1,993 行是這一行**，8 MB 的 rotation 幾分鐘就輪完，而同一份檔裡那 5 行
  `Sleeping for 12s on ... flood wait` 根本看不到。**代價不是難看，是這份 log 就是
  這份文件每一條坑的診斷工具。** 修法是 `bridge.ThrottleRepeats`：按 `record.msg`
  （模版，不是成形的那一行）每 60 秒放一行過，並把壓下的筆數接在後面
  （`(+357 more in the last 60s)`）。**不是把 `telethon.client.downloads` 整個降到 WARNING** ——
  同一個 logger 帶的是這裡最常讀的下載診斷：`File lives in another DC`、
  `File ref expired during download`，以及 direct/indirect 那個埋著的 2 倍（下一條）。
  按模版分鍵也意味著 direct 洗版擋不住第一行 indirect。

- **`_chunk` 提早 `return` 會漏掉 exported sender 的歸還**（`iter_download` 只有跑到底或
  短讀時才 `close()`；`RequestIter` 的歸還只寫在 `close()` 裡，`async for` 中途 return
  到不了）。一個 chunk 就是整個請求，所以這條路徑**每次**都是中途離開，跨 DC 的讀取因此
  只記 borrow 不記 return。現在由 `_close_download` 明確關掉迭代器補上 ——
  不是為了那條閒置連線（上一條反而是刻意留著它），而是因為**只會往上加的計數跟「刻意
  釘住」分不出來**，而上一條的正確性就建立在那個計數上。

- **舊 `/game` 的 `file_id` 不是 Telegram document id，拿它去驗身分等於把它們全部鎖死。**
  舊版 `gamestage` 註冊 split part 時寫的是 `f"{split_group_id}-{index}"`（上傳沒回
  document id 時的替代品），所以那些 row 的 `file_id` 長成 `1788435722109-52da4qq-3`。
  Task 4 加上 `_assert_media_id` 之後，這種值跟 Telegram 回的 id 永遠不相等 ——
  實測這個 drive 的 `/game` **143 筆裡有 127 筆**帶著它，全部讀不到，連 `H:` 都打不開。
  修法是**只驗證看起來是 document id（純數字）的值**：不是 id 的東西不帶身分資訊，
  沒有東西可以驗，退回加上這個檢查之前的行為（信任 message_id）。真的有 id 的照驗。
- **網頁上傳的大檔，`file_id` 是上傳 id 不是 document id —— 純數字也一樣驗不了。**
  網頁端（TeleDrive `frontend/src/lib/gramjs.ts`）小檔登記的是 `msg.media.document.id`，
  但走 `SaveBigFilePart` 的（≥ 10 MiB、以及每一個 split segment）登記的是客戶端自己
  隨機產生的 InputFileBig id。兩者都是 64-bit 純數字，從值本身分不出來，所以上一條的
  「只驗純數字」擋不住：實測 2026-09-26，`Okayu/Posts` 連續 16 則訊息檔名與大小跟
  backend 逐位元組相同、**id 一則都不同**，一天內 3,672 個檔案的讀取／縮圖／屬性全被
  `Telegram file mismatch` 擋掉，DLL 於是 `delegating` 去讀整張原圖（又一種「看起來
  只是冷資料夾慢」）。現在 id 對不上時改用那個 part 記錄的大小驗證（`_size_confirms`：
  完全相等，或是 backend 的 512 KiB 補齊範圍內），大小也不合才拒絕。

- **列 `/game` 不可以打開每一個封存。** 解析 `/game/<name>` 曾經呼叫
  `view.lookup([])` 只為了回答「這是不是目錄」—— 而那個答案 `.zip` 這個副檔名就給了。
  PROPFIND `Depth: 1` 會解析每一個子項，所以 143 個封存就是 143 次 Telegram 往返、
  每次約 6 秒：**一次列表 15 分鐘**，久到 rclone 放棄、整個掛載卡死。
  現在 `Loc(ZIPDIR, node=None)` 表示「封存本身」，樹留到真的有人往裡面看才讀
  （`Loc.zip_node()`）。實測 **15 分鐘 → 0.042 秒**。
  這條之前之所以沒炸，純粹是因為上面那 127 筆瞬間失敗 —— 快而錯，不是對。
- **網頁上傳的封存是 deflate，每開一次成員就重讀一次 central directory。**
  bridge 自己打包的是 `ZIP_STORED`，走 `SlicedReader` 直接切位移；但網頁上傳的 zip
  是 `ZIP_DEFLATED`，`ZipView.open` 以前對這種成員每次都 `zipfile.ZipFile(新串流)`，
  **每一次 backward seek 又再開一個** —— 每次都從 Telegram 重讀 end record 與 central
  directory，而每個新串流的 block 快取都是冷的。實測 2026-09-26：log 上同一個 offset
  一分鐘被抓約 220 次，16 條 cheroot worker 有 12 條卡在 `get_document`，於是連
  `PROPFIND /game/` 都排不到 thread，**超過 5 分鐘沒回**（使用者看到的是「開 game 裡的
  資料夾轉半天」）。樹裡本來就記著 `header_offset` / `compress_size`，現在 deflate 成員
  直接從資料位移用 `zlib`（raw，`wbits=-15`）解（`_InflateReader`），backward seek
  只重開這個成員。修完 `/game/` 0.05 秒、zip 資料夾瞬間、讀成員 1–3 秒。
  其他壓縮法（bzip2/lzma）少見，仍走 `zipfile`。
- **`JsonStore.flush` 不能把活的 dict 交給 `json.dump`。** merge 完 `self._data = merged`
  之後在鎖外 dump 同一個物件，另一條 worker 的 `put()` 就會
  `dictionary changed size during iteration` → `/rpc/props` 500 → DLL 退回去讀整檔。
  同一段還會把「snapshot 之後、merge 之前」進來的 put 從記憶體裡丟掉。

- **`zip_dirs.json` 一份共用的 JSON 會變成每讀一個封存重寫幾十 MB。**
  一個 central directory 可以是好幾 MB，這個 drive 上 276 個封存讓那份檔案長到
  **132 MB**，而 `JsonStore.put` 是整份重寫 —— 列一次 `/game` 等於寫約 18 GB。
  這跟 `meta/dirs/` 早就記下的教訓是同一條，只是 zip 快取沒跟著改。現在是
  `ShardedJsonStore`：一個封存一個檔（`meta/zips/`），寫入只花自己那一份。
- **`JsonStore.flush` 的暫存檔名不能固定。** wsgidav 用 16 條 worker thread 回答一次
  列表，每條填完一個 zip 目錄就 flush，共用 `.tmp` 名字的結果是某條的 `os.replace`
  打在另一條還開著的檔案上 —— Windows 上是硬邦邦的 `WinError 32`，那次寫入直接丟掉。
  看起來像 merge-on-flush 要處理的「兩個行程互搶」，其實是**同一個行程跟自己搶**。
  現在每個寫入者用 `mkstemp` 拿自己的名字。

## rclone 掛載參數

`start.bat` 裡已經帶好，重點：

- `--vfs-cache-mode full` + `--vfs-cache-max-size 160G`：sparse，只存讀到的區段，**容量驅動**淘汰。
- `--vfs-cache-max-age 8760h`：實質停用**時間**淘汰。age 到期就丟等於把還會用的資料重抓一次，
  白費頻寬又吃 SSD TBW。這個使用型態沒有別的資料競爭快取，上次用過的東西數月後很可能還在。
  SSD 壽命非問題：約 0.6 TB/年寫入，512 GB 消費級 NVMe TBW 約 300 TB。
- `--dir-cache-time 1h`：檔案多時 10s 會反覆打 API。網頁改動後用
  `rclone rc vfs/forget dir=<相對路徑>`（例：`dir=game`）。
  **這需要 `--rc-no-auth`，光有 `--rc` 不夠** —— rclone 對未設定驗證的 rc server 把
  `vfs/forget` 算成需要驗證的呼叫，回 `403 authentication must be set up`。掉了這個旗標的
  後果是「網頁上已經有、bridge 的 PROPFIND 也看得到、但 `H:` 就是沒有」只能等一小時
  （或重新 mount，連帶卸掉 `H:`）。診斷方式就是兩邊各列一次：`ls H:\game` 跟直接對
  `127.0.0.1:8081/game/` 發 PROPFIND，不一致就是 rclone 這層的 dir cache，跟 bridge 的
  `dir_cache_seconds`（1 小時，記憶體與 `meta/dirs/` 共用）和 `/rpc/forget` 那層沒關係。
- **不套 `crypt` / `compress`**：否則網頁端下載到加密/壓縮後的內容，
  失去「瀏覽器也能直接看」這個核心價值。

## `/rpc`

| 端點 | 用途 |
|---|---|
| `GET /rpc/health` | 連線狀態、telegram user id |
| `GET /rpc/status` | `/game` 的 `units`、一般路徑的 `uploads`（stage / attempts / 已用帳號 / 已 redact 的錯誤）、每個帳號的 `accounts`（online / linked / limiter 的 rate / ceiling / window / floods）與 `eligible_upload_ids`。**這裡不會出現任何憑證**，貼進 issue 是安全的 |
| `POST /rpc/forget` | 清 metadata 快取（rclone 那層另外用 `rclone rc vfs/forget`） |
| `POST /rpc/fetch-local` | `path=<Windows 路徑>`，串流回進度 |
| `GET /rpc/thumb` | `path=<Windows 路徑>` → Telegram 預覽圖（JPEG）；沒有就 404 讓 DLL 走 fallback |
| `GET /rpc/props` | `path=<Windows 路徑>` → `{width,height,duration,size}`，不讀檔案內容 |

## 測試

```powershell
.venv\Scripts\python.exe -m pytest tests -q
```

離線，不需 Telegram / rclone / 憑證。

| 檔案 | 覆蓋 |
|---|---|
| `tests/test_split_math.py` | offset→(part, 內部 offset) 映射、跨界切段、`SeekableRemoteFile` 的 seek/range/block 快取、block 快取裝得下一整個串流讀取寬度（不會把剛抓回來的那批丟掉） |
| `tests/test_zipfs.py` | 虛擬樹結構（空目錄、非 ASCII、隱含目錄、traversal 防護）、local header 偏移、單 entry range |
| `tests/test_sizes.py` | `filesize` 灌水的裁切（`_hash_size` / `_clip_parts` / `total_size`）、`JsonStore` 並行合併、`ShardedJsonStore` 只寫改動的那個 key（一個封存一個檔）、跨行程讀得回、任何 key 都產生安全檔名、不留 `.tmp` |
| `tests/test_photo_media.py` | photo 型 media：progressive `sizes` 取最後一個而非總和、`_fetch_document` 接受 photo、預覽受 `THUMB_PREVIEW_MAX` 上限且全部超標時仍給答案、`InputPhotoFileLocation`、寬高從 `sizes[-1]`；document 那半的行為不變 |
| `tests/test_backend_retry.py` | backend 斷掉閒置 keep-alive 時 `_call` 重試一次（GET 與 POST 都是，因為請求沒抵達 app）、不無限重試、真正的 HTTP 錯誤不重試、連線重試與 401 重登的預算互不吃掉 |
| `tests/test_dir_cache.py` | listing 的往返次數：`/folders` 與 `/files` 真的併發（循序會卡住測試而不是靜靜通過）、一次點擊只付新的那一層、`meta/dirs/` 撐過換 client（重啟）、與記憶體共用同一個 TTL、舊格式的檔案重列而不是誤讀、`fresh` 兩層都繞過並改寫磁碟、`invalidate` 連磁碟一起清、root 與含 `..` 的 id 都產生安全檔名 |
| `tests/test_auth_challenge.py` | bot challenge 登入：nonce 原文 DM 給 challenge 指名的 bot、202 continue 輪詢、session string 一個位元組都不上線、token.txt 重用、沒接 Telegram client 時明確報錯、並行 401 重登只送一個 nonce |
| `tests/test_thumbnails.py` | 預覽走 DC-aware 的 `iter_download`（跨 DC 不再 FILE_MIGRATE）、整批預覽受 `THUMB_CONCURRENCY` 節流、短 FLOOD_WAIT 重試 |
| `tests/test_read_pace.py` | 一次串流讀取在每條連線上排 `READS_IN_FLIGHT` 個請求且全部同時在飛、窄讀取仍只付一個請求、檔案 DC 的 exported sender 每個連線只借一次且刻意不還（session 自己的 DC 不釘）、讀取成功與失敗都會關掉下載迭代器 |
| `tests/test_upload_preview.py` | 上傳的縮圖：JPEG／帶 alpha 的 PNG／EXIF 旋轉都給得出「≤ 320px、≤ 20 KB 的 JPEG + 原圖寬高」、zip 與截斷的檔回 None（不讓上傳失敗）、只有 segment 0 帶預覽、暫存的 `.jpg` 用完就刪；以及 `register()` 照實回報 `has_thumbnail`（圖 True、zip False、去重沿用原 row），沒有它前面那半等於沒做 |
| `tests/test_transfer_config.py` | parity 參數的預設值與範圍檢查（0 或負數是 `ConfigError`）、路徑相對 `config.ini` 解析、`transfer_models` 的值物件 |
| `tests/test_routed_metadata.py` | `Entry` / part 表保存 `telegram_user_id` 與 `file_id`、`linked_account_ids()`、註冊帶上儲存帳號、thread-local HTTP session |
| `tests/test_account_pool.py` | 帳號檔驗證（重複 id、空 label/session）、user id 對不上就只停用那一個、`for_read(0)` 走 primary 而非零值 fallback、未 linked 的帳號不接新上傳、round-robin 跳過忙碌帳號、例外訊息不含 session |
| `tests/test_account_routing.py` | 讀取前先驗 `file_id`（不對就不發 GetFile）、兩個帳號上相同 message id 不會互串、跨帳號 split 的 Range 拼接正確、快取 key 帶帳號 |
| `tests/test_upload_limiter.py` | 移植自 `adaptiveRateLimiter.ts` 的狀態轉移向量：首次 flood、學到的 ceiling、slow zone、probe 確認/失敗冷卻、三次 flood 升級、十分鐘重置、premium 等待不降速、壞掉的狀態檔回退、每帳號各自一份 |
| `tests/test_upload_protocol.py` | `decide_protocol` 的三個界線（10 MiB / 500 MiB / 500 MiB + 1）、small 走 128 KiB × 4 workers 且 md5 正確、split 的尾巴仍用 `SaveBigFilePart` |
| `tests/test_media_thumbnail.py` | `ready` / `not_media` / `undecodable` 的分類、ffmpeg 的探索與逾時、webp 是 media 但不進 album |
| `tests/test_upload_dedup.py` | 精確覆蓋：殘缺的 split group 不可重用、別名與重複 message id 收斂、完整重複保留各 part 的儲存帳號、同批的兩個別名只上傳一次 |
| `tests/test_upload_engine.py` | 引擎的整合：small 四 worker、單一 big segment、split 的 segment 分租不同帳號且結果按 plan index 還原、只有 index 0 帶縮圖、file lease 在訊息與註冊前就放掉、註冊上限 8、Telegram 回報長度不符就不註冊 |
| `tests/test_upload_album.py` | album：適用規則、湊滿 10 就送而不等整批、按 document id 對回亂序的 updates、逾時/失敗的逐檔 fallback（單 worker、不帶縮圖）、不同帳號不共用一個 `SendMultiMedia` |
| `tests/test_upload_scheduler.py` | 串流階段（指紋 ≤ 2、check ≤ 8、第三個檔在算指紋時第二個已在上傳、第一個還在註冊）、durable 佇列（失敗只留下受影響的來源、第 5 次 abandoned 且保留、重啟收養每一個暫存檔並沿用 attempts、原子寫入、狀態不含憑證） |
| `tests/test_transfer_status.py` | 一份 pool 一份 engine 貫穿全程：`/game` 打包後送出的是 `.zip` + `application/zip` + 不進 album、直接丟進 `/game` 的檔案保留自己的型別、`/rpc/status` 列出帳號與 limiter 且不含憑證、只有 primary 回答 bot challenge、停止時每個帳號都停、sweep 的縮圖與屬性按帳號路由 |
| `tests/test_transfer_logging.py` | 完成日誌帶齊每一個計時欄位且數字讀得出來、去重的那筆報 0 上傳時間、失敗只記一行且 session/JWT 被 redact、兩份 example 設定檔不含任何憑證值 |
| `tests/test_log_noise.py` | 日誌可讀性：單一 call site 的洪水收成一行並報出壓了幾筆、同一個 logger 的其他診斷不被延遲（這就是不用 `setLevel(WARNING)` 的理由）、direct 的洪水擋不住第一行 indirect |
| `tests/test_shell_warm.py` | shell warm 的記帳：逐檔 stderr 回報的解析（含非 ASCII 路徑）、被 kill 的批次仍報得出暖成幾個與還卡在哪一個、卡住就停掉這一輪而不是把後面幾十批排在後面、期限按檔數算、沒掛載就不去問 shell |
| `tests/test_upload_pace.py` | `tgupload.UploadGate`：distinct-event guard、window/rate 的 AIMD、rate cap 從量測值算出且爬回不再綁得住時拆掉、注入假時鐘 |
| `tests/test_upload_parts.py` | `plan_parts`、`_PartReader` 的隨機讀取、`send_part` 的 flood/斷線重試（繞過 `client._call`）、`upload_file_parts` 的 segment-relative index、bytes↔offset、永久失敗時取消手足 task |
| `tests/test_bridge_e2e.py` | 真的用 HTTP 跑整個 bridge（PROPFIND / GET / Range / 403 / MKCOL+PUT → 打包 → 上傳 → 再瀏覽 / `/rpc/*` / fetch-local / warmup sweep 的續跑與禮讓 / split part 的精確大小非灌水 / 一般路徑的 MKCOL、PUT 新檔、覆寫、去重、`/rpc/status` 的 `uploads` 欄位 / DELETE 在 `/game` 與一般路徑對「還在暫存」一致放行、對「已上傳」一致 403 且不因遞迴列出整棵樹而 500 / COPY 對已上傳內容一致 403（檔案與資料夾兩種 resource 都不因遞迴列出整棵樹而 500）、對還在暫存的內容（`/game` 與一般路徑）做出真正的本機複製、跨 `/game` 邊界複製一律 403 / 父目錄已在 staging 時的 PUT 與 MKCOL 一次 backend 都不打），只有 MTProto 與 backend 是假的 |

掛載後仍需手動走一遍（測試無法代替）：

1. `rclone ls teledrive:` 與網頁列表一致
2. 小檔 / 非 split 大檔 / split 大檔各取一份，`certutil -hashfile <檔> SHA256` 與網頁下載相同
3. 多層資料夾移入 `H:\game\`，debounce 到期後網頁出現 `<名稱>.zip`，`H:` 上仍是資料夾且內容正確 ——
   要用 **> 500 MiB 的多 segment** 遊戲跑一次，這是並行上傳第一次對真的 TeleDrive 跑
4. 對虛擬 zip 資料夾右鍵取回 → 解壓後遊戲能執行
5. 快取到 `--vfs-cache-max-size` 上限時淘汰正常
6. 瀏覽器開網頁確認 `/game` 上傳的 zip 顯示、下載正常
7. `/game` 以外的資料夾建立子資料夾、丟一個檔案進去，debounce 到期後網頁能看到、下載內容正確；
   同名再丟一次，確認覆寫後讀到的是新內容
8. **多帳號**：設好 `accounts_file` 之後，確認 `/rpc/status` 的 `accounts` 每一個都
   `online` + `linked`；丟一個 > 500 MiB 的檔案，看網頁上兩個 part 的
   `telegram_user_id` 真的不同，再從 `H:` 讀回來比對 SHA256（這是「跨帳號 split 讀得回來」
   唯一的實證）。另外挑一個存在**次要**帳號的既有檔案，確認縮圖與內容都出得來。
9. **album**：一次丟 11 張 ≤ 10 MiB 的 JPEG 到 `/game` 以外的資料夾，網頁上 11 張都在、
   都有縮圖；`bridge.log` 應該看得到兩批（10 + 1）。再丟一張 `.webp`，確認它走的是
   一般上傳而不是 album（網頁端記著 webp 走 album 會 `MEDIA_EMPTY` 且掉縮圖）。

**`/game`、一般路徑的上傳、多帳號路由與 album 都從未對真實 TeleDrive 跑過** ——
全都有副作用，測試環境只用假 backend 與假 MTProto。

## 已知限制

1. **首次上傳是主要成本**：8 TB 在上行 100 Mbps 約 7.4 天，期間會撞 FLOOD_WAIT。
2. **唯一副本風險**：Telegram 帳號被封或誤刪即全失，無版本保護 → 定位為第二份冷封存。
3. **Telegram 會隨著持續拉取逐步節流**，長時間取回的實際速度低於一開始的爆發值。
   量測多條連線的吞吐時必須交錯取樣，否則先跑的那組永遠比較快。
4. **4K remux 直接播會邊緣**：1080p remux（~30 Mbps）順；4K（50–80 Mbps）先取回本機。
5. **同名檔案**：TeleDrive 沒有 `UNIQUE(filename, parent_id)` → 取 `created_at` 最新者並記 warning。
6. **上傳中斷的檔案**：`split_group_id` 有值但只註冊了 part 0，那是真的少資料，只能刪掉重傳。
   `truncated.csv` 記著目前已知的 38 個。
7. **一機一份 bridge**：只有跑 bridge 的那台 PC 能掛磁碟。所有設定的帳號都由這一份
   bridge 連線，session string 全部留在這台機器上。
8. **進入未快取資料夾的第一個請求約 0.58 秒**（路徑解析：每一層一個 backend 往返，見「效能」第 7 節），之後每張 15ms。重啟後若那個資料夾之前列過，是 0.016 秒。
9. Windows 11 右鍵選單只能出現在「顯示更多選項」（第一層要 MSIX + `IExplorerCommand`）。
10. **COPY/MOVE 到已打包的 `/game/<name>` 底下不會失敗，會悄悄開一個新的 shadow staging unit**：
    跟 PUT 不一樣（`ZipDirCollection.create_empty_resource` 會擋下並提示 `PACKED_MESSAGE`），
    `GameStager.copy()`/`.move()` 只驗證目的地留在 `/game` 底下，不檢查該名字是不是已經打包
    上傳過——結果是新建一筆同名 staging unit，下一輪 debounce 打包後蓋掉真正的舊封存
    （見「同名檔案」那條限制）。解法跟 PUT 一樣：改用新名字，或先從網頁刪掉舊的 zip。
11. **`/game` 一個 unit 失敗就整包重來**：以前 gamestage 自己會逐 segment 重試，現在
    重試下放到 `tgupload.send_part`（每個 512 KiB part 三次）。part 層面的暫時性失敗
    因此便宜得多，但一個 segment 真的失敗仍然是整個 unit 十分鐘後從頭再跑一次。
12. **`/game` 與一般路徑的上傳仍未對真實 TeleDrive 跑過**（測試用的 backend 與 MTProto
    都是假的），多帳號路由與 album 也一樣——見「測試」節尾的手動清單第 8、9 項。

## 明確不做

- **版本回收**：backend 沒有這個概念，覆寫就是新增一筆同名 row，舊的還在只是被蓋掉
  （已知限制第 5 點），不是真的版本歷史。
- **`MOVE`/`PROPPATCH`/`LOCK` 限定在 `/game/<name>/...`**：
  這些動詞在 `/game` 以外沒有對得到的 backend 端點（沒有真正的改名），也沒有
  `DELETE`/`COPY` 那種「本機暫存 vs. 已上傳」的乾淨分界可以套——連還在 staging
  的一般路徑寫入也沒有搬移原語（`upload_stager` 不像 `gamestage.GameStager`
  有 `move()`）。`WriteGuard`（`bridge.py`）放行 `MKCOL`（`POST /folders`）、
  `PUT`、`DELETE`、`COPY`（見「一般路徑的寫入」），其餘維持 403。
- **block 級部分更改**：WebDAV 只有整檔 PUT，rclone 也是整檔重傳。真要做得改用 WinFsp
  （`winfspy`）自己實作檔案系統才會收到 `write(offset, len)`；儲存端不用改
  （`split_group_id` + `part_index` 已是 block 結構），但整個 bridge 幾乎重做。
- **Cloud Filter API（`cfapi.h`）取代 rclone + WinFsp** —— 試過，放棄了。
  dehydrated placeholder 拿不到縮圖：shell 直接回 `WTS_E_FAILEDEXTRACTION`，
  **根本不會問到 provider**，等於整個縮圖方案失效。另外 sync root 一旦沒有 provider 在聽，
  placeholder 連刪都刪不掉（要重新連一個 provider 進去 revert 才清得掉）。
- 偵測執行 exe 就自動下載後無縫啟動：Windows 執行 exe 是記憶體映射載入映像，
  loader 不會等下載解壓完成，會直接失敗或判定無回應 → 取回只能是明確的動作。
- MSIX + `IExplorerCommand`、PyInstaller 打包 exe、開機自啟、GUI 設定介面
