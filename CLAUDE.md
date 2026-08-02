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
| `zipfs.py` | 讀 zip central directory → 虛擬目錄樹；單一 entry 的 range 讀取 |
| `gamestage.py` | `/game` staging + debounce 打包（`ZIP_STORED`）+ 上傳 + 去重 + 清理 |
| `fetchlocal.py` | 「儲存在本地」：伺服端複製邏輯 + 右鍵 verb 用的進度顯示 CLI |
| `warmup.py` | 走遍整棵樹批次填滿縮圖與屬性快取，可續跑 |
| `install_menu.py` | 註冊/移除 Explorer 右鍵 verb |
| `install_thumb.py` | 註冊/移除 shell handler，逐副檔名記錄被取代的既有 CLSID |
| `shellthumb/` | C++ shell 擴充：`IThumbnailProvider` + `IPropertyStore`，同一份 DLL 兩個 CLSID |
| `config.py` | 讀 `config.ini`，空值回退環境變數，再回退 `env_file`；由單一 `cache_dir` 推導所有路徑 |
| `start.bat` | 啟動 bridge + `rclone mount` |

`config.ini` 只有 `cache_dir` 一個路徑設定，底下的 `meta/` `rclone/` `local/` `staging/`
是程式的實作細節而非設定 —— 先前四個獨立路徑設定的結果就是它們各自漂移，
使用者以為改了一個地方其實只改到四分之一。`start.bat` 也是問 `config.py` 要路徑，
不自己寫死。

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
`_last_demand`，預抓永遠等不到安靜期，等於自己擋自己。

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
| `tests/test_bridge_e2e.py` | 真的用 HTTP 跑整個 bridge（PROPFIND / GET / Range / 403 / MKCOL+PUT → 打包 → 上傳 → 再瀏覽 / `/rpc/*` / fetch-local），只有 MTProto 與 backend 是假的 |

掛載後仍需手動走一遍（測試無法代替）：

1. `rclone ls teledrive:` 與網頁列表一致
2. 小檔 / 非 split 大檔 / split 大檔各取一份，`certutil -hashfile <檔> SHA256` 與網頁下載相同
3. 多層資料夾移入 `H:\game\`，debounce 到期後網頁出現 `<名稱>.zip`，`H:` 上仍是資料夾且內容正確
4. 對虛擬 zip 資料夾右鍵取回 → 解壓後遊戲能執行
5. 快取到 `--vfs-cache-max-size` 上限時淘汰正常
6. 瀏覽器開網頁確認 `/game` 上傳的 zip 顯示、下載正常

**`/game` 上傳路徑從未對真實 TeleDrive 跑過** —— 它有副作用，測試環境只用假 backend。

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

## 明確不做

- 一般檔案的 PUT / 覆寫 / 版本回收（照片影片上傳走網頁，那邊有縮圖、去重、album 分組）
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
