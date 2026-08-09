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

## 檔案

| 檔案 | 職責 |
|---|---|
| `bridge.py` | wsgidav provider、寫入保護、`/rpc/*`、cheroot 伺服器（綁 127.0.0.1） |
| `tdapi.py` | TeleDrive REST client：JWT 取得/快取/401 自動重登、路徑解析、split part 表快取、`JsonStore` |
| `tgio.py` | split 位移數學、Telethon worker（背景 event loop）、連線池、`SeekableRemoteFile`、分段上傳、縮圖與 media attributes |
| `tgupload.py` | `/game` 大檔案的並行分 part 上傳：自算 part index、`UploadGate`（window+rate 的 AIMD 節流）、繞過 `client._call` 直送 `SaveBigFilePart` |
| `zipfs.py` | 讀 zip central directory → 虛擬目錄樹；單一 entry 的 range 讀取 |
| `gamestage.py` | `/game` staging + debounce 打包（`ZIP_STORED`）+ 上傳 + 去重 + 清理；`upload_and_register` 給 `uploadstage.py` 共用 |
| `uploadstage.py` | `/game` 以外任意路徑的一般檔案寫入：落地 + debounce（無打包，單位是單一檔案）+ 上傳 + 去重 + 註冊到寫入時解析到的真實 parent |
| `fetchlocal.py` | 「儲存在本地」：伺服端複製邏輯 + 右鍵 verb 用的進度顯示 CLI |
| `warmup.py` | 走遍整棵樹批次填滿縮圖與屬性快取、跑 Windows 縮圖快取，可續跑；`BackgroundWarmup` 讓 bridge 自己跑 |
| `install_menu.py` | 註冊/移除 Explorer 右鍵 verb |
| `install_thumb.py` | 註冊/移除 shell handler，逐副檔名記錄被取代的既有 CLSID |
| `shellthumb/` | C++ shell 擴充：`IThumbnailProvider` + `IPropertyStore`，同一份 DLL 兩個 CLSID；`warmshell.exe` 把縮圖灌進 Windows thumbcache，`bench.exe` / `isolate.exe` 量測 |
| `config.py` | 讀 `config.ini`，空值回退環境變數，再回退 `env_file`；由單一 `cache_dir` 推導所有路徑 |
| `start.bat` | 啟動 bridge + `rclone mount` |

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
  上傳＋去重＋註冊（`gamestage.upload_and_register`，`uploadstage.py` 只補
  「落地/debounce」那一半），差別只在兩點：單位永遠是單一檔案（資料夾是真的，
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

沒有做的是縮圖與 album 分組——那些是網頁上傳流程專屬的功能，這裡沒有重做；
去重（`check_hash`，跟網頁同一套指紋）則是共用的，照樣套用。
`/rpc/status` 的 `uploads` 欄位回報目前 debounce 中的一般寫入，跟 `/game`
的 `units` 分開列。

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

## rclone 掛載參數

`start.bat` 裡已經帶好，重點：

- `--vfs-cache-mode full` + `--vfs-cache-max-size 160G`：sparse，只存讀到的區段，**容量驅動**淘汰。
- `--vfs-cache-max-age 8760h`：實質停用**時間**淘汰。age 到期就丟等於把還會用的資料重抓一次，
  白費頻寬又吃 SSD TBW。這個使用型態沒有別的資料競爭快取，上次用過的東西數月後很可能還在。
  SSD 壽命非問題：約 0.6 TB/年寫入，512 GB 消費級 NVMe TBW 約 300 TB。
- `--dir-cache-time 1h`：檔案多時 10s 會反覆打 API。網頁改動後用 `rclone rc vfs/forget`。
- **不套 `crypt` / `compress`**：否則網頁端下載到加密/壓縮後的內容，
  失去「瀏覽器也能直接看」這個核心價值。

## `/rpc`

| 端點 | 用途 |
|---|---|
| `GET /rpc/health` | 連線狀態、telegram user id |
| `GET /rpc/status` | `/game` staging 各單位的狀態與閒置秒數 |
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
| `tests/test_split_math.py` | offset→(part, 內部 offset) 映射、跨界切段、`SeekableRemoteFile` 的 seek/range/block 快取 |
| `tests/test_zipfs.py` | 虛擬樹結構（空目錄、非 ASCII、隱含目錄、traversal 防護）、local header 偏移、單 entry range |
| `tests/test_sizes.py` | `filesize` 灌水的裁切（`_hash_size` / `_clip_parts` / `total_size`）、`JsonStore` 並行合併 |
| `tests/test_upload_pace.py` | `tgupload.UploadGate`：distinct-event guard、window/rate 的 AIMD、rate cap 從量測值算出且爬回不再綁得住時拆掉、注入假時鐘 |
| `tests/test_upload_parts.py` | `plan_parts`、`_PartReader` 的隨機讀取、`send_part` 的 flood/斷線重試（繞過 `client._call`）、`upload_file_parts` 的 segment-relative index、bytes↔offset、永久失敗時取消手足 task |
| `tests/test_bridge_e2e.py` | 真的用 HTTP 跑整個 bridge（PROPFIND / GET / Range / 403 / MKCOL+PUT → 打包 → 上傳 → 再瀏覽 / `/rpc/*` / fetch-local / warmup sweep 的續跑與禮讓 / split part 的精確大小非灌水 / 一般路徑的 MKCOL、PUT 新檔、覆寫、去重、`/rpc/status` 的 `uploads` 欄位 / DELETE 在 `/game` 與一般路徑對「還在暫存」一致放行、對「已上傳」一致 403 且不因遞迴列出整棵樹而 500 / COPY 對已上傳內容一致 403（檔案與資料夾兩種 resource 都不因遞迴列出整棵樹而 500）、對還在暫存的內容（`/game` 與一般路徑）做出真正的本機複製、跨 `/game` 邊界複製一律 403），只有 MTProto 與 backend 是假的 |

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

**`/game` 與一般路徑的上傳都從未對真實 TeleDrive 跑過** —— 兩者都有副作用，測試環境只用假 backend。

## 已知限制

1. **首次上傳是主要成本**：8 TB 在上行 100 Mbps 約 7.4 天，期間會撞 FLOOD_WAIT。
2. **唯一副本風險**：Telegram 帳號被封或誤刪即全失，無版本保護 → 定位為第二份冷封存。
3. **Telegram 會隨著持續拉取逐步節流**，長時間取回的實際速度低於一開始的爆發值。
   量測多條連線的吞吐時必須交錯取樣，否則先跑的那組永遠比較快。
4. **4K remux 直接播會邊緣**：1080p remux（~30 Mbps）順；4K（50–80 Mbps）先取回本機。
5. **同名檔案**：TeleDrive 沒有 `UNIQUE(filename, parent_id)` → 取 `created_at` 最新者並記 warning。
6. **上傳中斷的檔案**：`split_group_id` 有值但只註冊了 part 0，那是真的少資料，只能刪掉重傳。
   `truncated.csv` 記著目前已知的 38 個。
7. **一機一份 bridge**：只有跑 bridge 的那台 PC 能掛磁碟。
8. **進入未快取資料夾的第一個請求約 11 秒**（路徑解析），之後每張 15ms。
9. Windows 11 右鍵選單只能出現在「顯示更多選項」（第一層要 MSIX + `IExplorerCommand`）。
10. **COPY/MOVE 到已打包的 `/game/<name>` 底下不會失敗，會悄悄開一個新的 shadow staging unit**：
    跟 PUT 不一樣（`ZipDirCollection.create_empty_resource` 會擋下並提示 `PACKED_MESSAGE`），
    `GameStager.copy()`/`.move()` 只驗證目的地留在 `/game` 底下，不檢查該名字是不是已經打包
    上傳過——結果是新建一筆同名 staging unit，下一輪 debounce 打包後蓋掉真正的舊封存
    （見「同名檔案」那條限制）。解法跟 PUT 一樣：改用新名字，或先從網頁刪掉舊的 zip。

## 明確不做

- **一般檔案的縮圖與 album 分組**：PUT/覆寫本身已支援（見「一般路徑的寫入」一節），
  但那是網頁上傳流程專屬的加工，這裡沒有重做。去重是共用的，不算例外。
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
