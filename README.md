# teledrive-webdav

把 [TeleDrive](../teledrive) 掛成 Windows 本機磁碟（預設 `E:`）：唯讀瀏覽、`/game` 移入即自動打包、
Explorer 右鍵「儲存在本地」。跑在客戶端 PC，不進 Docker。

```
Explorer 右鍵「儲存在本地」──▶ registry verb ──▶ POST 127.0.0.1:8081/rpc/fetch-local
                                                          │
E:  ──rclone mount(WinFsp)──▶ http://127.0.0.1:8081  (bridge.py, Python)
                                        │
                     metadata (HTTPS + JWT)          位元組 (MTProto)
                                        ▼                    ▼
                     teledrive…dpdns.org/api/v1        Telegram CDN
```

**核心不變量沒有變**：位元組只在「本機 ↔ Telegram」之間流動，metadata 才走 TeleDrive backend。
bridge 完全只用現有 public API，沒有為它新增任何會讀寫二進位資料的端點。

## 檔案

| 檔案 | 職責 |
|---|---|
| `bridge.py` | wsgidav provider、寫入保護、`/rpc/*`、cheroot 伺服器（綁 127.0.0.1） |
| `tdapi.py` | TeleDrive REST client：JWT 取得/快取/401 自動重登、路徑解析、split part 表快取 |
| `tgio.py` | split 位移數學、Telethon worker（背景 event loop）、`SeekableRemoteFile`、分段上傳 |
| `zipfs.py` | 讀 zip central directory → 虛擬目錄樹；單一 entry 的 range 讀取 |
| `gamestage.py` | `/game` staging + debounce 打包（`ZIP_STORED`）+ 上傳 + 去重 + 清理 |
| `fetchlocal.py` | 「儲存在本地」：伺服端複製邏輯 + 右鍵 verb 用的進度顯示 CLI |
| `install_menu.py` | 註冊/移除 Explorer 右鍵 verb |
| `shellthumb/` | C++ shell 擴充：`IThumbnailProvider`（顯示 Telegram 預覽圖）+ `IPropertyStore`（從 metadata 回答尺寸），同一份 DLL |
| `install_thumb.py` | 註冊/移除上述兩個 handler，逐副檔名記錄被取代的既有 handler |
| `config.py` | 讀 `config.ini`，空值回退環境變數，再回退 `env_file` |
| `start.bat` | 啟動 bridge + `rclone mount` |
| `tests/` | 離線測試（不需 Telegram、不需 rclone）：見「驗證」 |

## 安裝

### 1. 相依套件

```powershell
winget install Rclone.Rclone
winget install WinFsp.WinFsp          # rclone mount 在 Windows 需要
```

Python 3.10+。`start.bat` 第一次執行會自己建 `.venv` 並安裝 `requirements.txt`。

`requirements.txt` 裡的 **`cryptg` 不是可選的**。少了它 Telethon 會退回純 Python 的
AES-IGE，解密本身就把下載壓在 ~0.15 MiB/s，連線再多也沒用。啟動時 log 若出現
`cryptg not installed ... falling back to (slower) Python encryption`，就是沒裝到；
正常應該是 `cryptg detected, it will be used for encryption`。

### 2. 產生 Telethon session（**必須手動做一次**）

bridge 需要一組 Telethon StringSession，同一份字串同時用於 MTProto 直連與換 JWT
（backend 已能直接接受 Telethon StringSession，見 TeleDrive `routes.py:112`）。

```powershell
cd D:\python\teledrive
python generate_session.py            # 需要手機號碼 + Telegram 驗證碼
```

它會把 `TELEGRAM_SESSION_STRING` 寫回 `D:\python\teledrive\.env`。

### 3. config.ini

```powershell
copy config.example.ini config.ini
```

預設的 `env_file` 就指向 `D:\python\teledrive\.env`，所以 **api_id / api_hash / session 三個欄位留空即可**，
憑證只存在 `.env` 一份。要調的通常只有：

| 設定 | 預設 | 說明 |
|---|---|---|
| `local_dir` | `D:\teledrive-local` | 「儲存在本地」的落點 |
| `staging_dir` | `staging` | `/game` 打包前的暫存，需要 ≈ 一款遊戲的空間 |
| `debounce_minutes` | `5` | `/game/<資料夾>` 靜止多久算「移入完成」 |
| `mount_drive` | `E:` | 要跟 `start.bat` 的 `MOUNT` 一致 |

### 4. 啟動

```powershell
.\start.bat
```

會先起 bridge、等 `/rpc/health` 回應，再 `rclone mount`。在該視窗按 Ctrl+C 即卸載。
`start.bat` 開頭的 `RCLONE_CACHE` **必須指到 NVMe**（rclone 預設放 `%LOCALAPPDATA%`，通常在系統碟）。

### 5. 右鍵選單

```powershell
.venv\Scripts\python.exe install_menu.py --install     # --uninstall / --status
```

Windows 11 的第一層精簡選單只顯示 MSIX + `IExplorerCommand` 的項目，
所以這個 registry verb 會出現在「**顯示更多選項**」（Shift+F10）裡。這一版接受這個位置。

## 使用

### 唯讀瀏覽

`E:` 就是雲端目錄樹。split 大檔在 `E:` 上是**一個**檔案（虛擬串接多則 Telegram 訊息），
`getcontentlength` 會加總所有 parts，不是只有第一段。

`/game` 以外的所有寫入動詞（PUT/DELETE/MKCOL/MOVE/COPY/PROPPATCH/LOCK）一律回 **403**。
不用 rclone 的 `--read-only`，因為那樣 `/game` 也會被凍住。

照片建議走網頁看：幾萬張小檔是 WebDAV + rclone 最弱的場景。

### 縮圖：`shellthumb/` + `/rpc/thumb`

**問題**：Explorer 產生縮圖時會讀**整個原始檔案**。實測用 shell 自己的
`IShellItemImageFactory`（Explorer 走的就是這支 API）對一張 18.6 MB 的 PNG 產生
256px 縮圖，讀滿 18,629,212 bytes、花 18.8 秒。而且檔案層面沒有捷徑：這批 pixiv 的
JPEG 只有 JFIF + ICC，**沒有 APP1/Exif 內嵌縮圖**，PNG 格式本來就沒有。

**解法**：Telegram 在每張照片、每部影片旁邊都存了一張約 200x200、平均 17 KB 的預覽圖。
Windows 唯一支援「不要讀檔案」的介入點是縮圖處理常式（`IThumbnailProvider`）——
Explorer 會改成呼叫它，於是我們回傳那張預覽。

```powershell
shellthumb\build.bat                                   # 需要 MSVC（x64）
.venv\Scripts\python.exe install_thumb.py --install    # --uninstall / --status
```

實測（4.3 MB 的 PNG，shell 產生 256px 縮圖）：**6.97s → 0.05s**。

註冊細節與風險：

- 全部寫在 `HKCU\Software\Classes`：不需管理員權限、不影響其他使用者。
- 縮圖處理常式是**依副檔名**註冊的，沒有「只對某個磁碟機」這種作用域，所以裝了就會攔截
  整台機器上的 `.jpg`/`.png`/`.mp4` 等 14 種類型。DLL 因此會把**不在掛載磁碟上的檔案
  轉交給原本的處理常式** —— 安裝時逐副檔名記錄被取代的 CLSID（影像多半是
  `{C7657C4A-…}`、影片是 `{9DBD2C50-…}`），`--uninstall` 會還原回去。
- 掛載磁碟上的檔案若取不到預覽（沒有縮圖、bridge 沒開），一樣走 fallback，
  最差就是退回原本「讀整檔」的行為，不會變成空白圖示。
- 註冊點同時涵蓋 ProgID 與 `SystemFileAssociations\.<ext>`：shell 解析時 ProgID 優先，
  只寫後者會被 `jpegfile` / `VLC.mp4` 這類既有註冊蓋過。

**預抓**：Explorer 是一個檔案一個檔案問的，而單張預覽要兩趟 Telegram（取 document、
下載）。所以第一次在某資料夾取不到快取時，會在背景把整個資料夾（上限
`THUMB_PREFETCH_MAX`）分批預抓。實測冷資料夾 **84 張 2.6 秒（33 張/秒）**，
第一張單獨 0.56 秒。

兩件事讓它快得起來：批次讓 `get_messages` 一次涵蓋 100 個 id，而縮圖下載走**連線池**
而非單一控制連線 —— 用 `download_media` 時每張都排在同一條連線上，正是先前預抓
慢如牛步的原因。批次之間會等 `THUMB_PREFETCH_IDLE` 的安靜期才繼續，讓前景請求
（含 Explorer 讀原檔取屬性）優先。

預抓完成後同資料夾為 **0.00–0.02 秒**。快取在 `cache_dir/thumbs/<file_id>.jpg`，
內容衍生自不可變的訊息，所以不需要失效機制。

**量測時注意**：Windows 自己也有縮圖快取（`thumbcache_*.db`）。看過一次的資料夾再測，
回應會是 0.04 秒但**根本沒呼叫到這個 handler** —— 拿它判斷快慢會得到錯誤結論。
要確認 handler 真的在跑，把 `HKCU\Software\TeleDriveWebDAV\LogPath` 設成一個檔案路徑，
DLL 就會逐次寫入：

```
DllGetClassObject
Initialize: H:\pixiv\user-9016\142759167_p0.jpg
GetThumbnail: cx=256 onMount=1 path=...
  preview 30828 bytes        <- 成功。出現 "delegating" 表示退回讀整檔
```

完全沒有記錄產生，就代表 Windows 用了自己的快取、沒問到我們。刪掉那個登錄值即關閉。

### 屬性：property handler + `/rpc/props`

縮圖修好之後，瀏覽仍然慢 —— 實測 DLL 記錄顯示 handler 每張只花 **47ms**、沒有一張超過
0.5 秒，但 Explorer **兩次呼叫之間**的間隔中位數 5.2 秒、最長 43.9 秒。原因是 Explorer
另外去讀每張圖的**檔頭來取得尺寸**：20 個檔案有 16 個被讀取，每個讀 258 KB–1 MB
（2 MB 的 JPEG 讀 12.6%、3.2 MB 的讀 32.9%）。這條路徑是 `IPropertyStore`，
跟縮圖完全無關。

Telegram 的 document attributes 本來就帶著寬高與長度，所以 bridge 可以直接回答，
**一個位元組都不用下載**（實測 0.08–0.34 秒，之後走快取）。

```powershell
# 以系統管理員身分執行
.venv\Scripts\python.exe install_thumb.py --install-props    # --uninstall-props
```

**這一半跟縮圖 handler 不同，務必先看清楚**：

- Property handler 只認 `HKLM\...\PropertySystem\PropertyHandlers\<.ext>`，**沒有 HKCU 版本** ——
  所以需要**管理員權限**，而且影響**這台機器的所有使用者**。縮圖那半是 HKCU、只影響你。
- 它壞掉的話影響面比縮圖大：檔案總管的欄位、搜尋索引、其他讀取圖片屬性的程式都會受影響。
  非掛載磁碟的檔案一律轉交原本的 handler（影像 `{a38b883c-…}`、mp4 `{f81b1b56-…}`、
  mkv `{C591F150-…}`，逐副檔名記錄），`--uninstall-props` 會還原。
- 設定與 fallback 表**同時寫進 HKCU 和 HKLM**：搜尋索引器是以別的使用者身分載入
  property handler 的，只寫 HKCU 的話它兩者都讀不到。

回報的屬性：`PKEY_Image_HorizontalSize` / `VerticalSize` / `Dimensions`，影片再加上
`PKEY_Media_Duration` 與 `PKEY_Video_FrameWidth` / `FrameHeight`。

### `/game`：移入即打包

1. 把 `<遊戲名>` 資料夾拖進 `E:\game\`。內容先落在 `staging_dir`，同時在 `E:` 上就看得到。
2. 該子樹靜止 `debounce_minutes` → 打包成 `<遊戲名>.zip` → 上傳 → 清 staging。
3. 之後 `E:\game\<遊戲名>\` 仍然顯示為資料夾，內容由 zip 的 central directory 虛擬展開（唯讀）。
   網頁端看到的是正常的 `<遊戲名>.zip`，可以直接下載。

zip 用 **`ZIP_STORED` 不壓縮**：遊戲檔本來就壓過，省不到空間，但換來「單一 entry 的位元組就是
壓縮檔裡的一段連續 range」——所以從 60 GB 的包裡讀一個檔，只花那個檔的流量。

`>500 MiB`（= 1000 parts × 512 KB，與網頁端同邊界）會切成多則訊息，
帶 `is_split_file / split_group_id / part_index / total_parts / original_name` 註冊 N 列。
上傳前查 `/files/check-hash` 去重（前 100 MB sample hash，與網頁端同語意）。

直接丟一個**檔案**進 `/game`（例如自己壓好的 `X.zip`）→ 原樣上傳，不再包一層。

已經打包上傳的名字是唯讀的：對 `E:\game\<已上傳的名字>\` 寫入會得到 403。
（部分重打包會靜默丟掉沒被重寫的檔案，寧可拒絕。要更新就先在網頁刪掉那個 `.zip`，或換個名字。）

### 儲存在本地

對 `E:` 底下的檔案或資料夾按右鍵 →「儲存在本地 (TeleDrive)」。console 視窗顯示進度，完成後開 Explorer。

- 虛擬 zip 資料夾 → 逐 entry range 讀取直接解壓到 `local_dir\<名稱>\`，**不需要先下載整包、也不留暫存檔**
- 一般檔案 / 資料夾 → 直接串流複製

也可以用 CLI：`.venv\Scripts\python.exe fetchlocal.py "E:\game\MyGame"`

**做不到**：偵測執行 exe 就自動下載後無縫啟動。Windows 執行 exe 是記憶體映射載入映像，
loader 不會等下載解壓完成，會直接失敗或判定無回應 → 取回只能是明確的動作。
遊戲也不要直接在 `E:` 跑（反作弊 + mmap 會擋），先取回本機再玩。

### `/rpc`

| 端點 | 用途 |
|---|---|
| `GET /rpc/health` | 連線狀態、telegram user id |
| `GET /rpc/status` | `/game` staging 各單位的狀態與閒置秒數 |
| `POST /rpc/forget` | 清 metadata 快取（網頁改動後想立刻看到；rclone 那層另外用 `rclone rc vfs/forget`） |
| `POST /rpc/fetch-local` | `path=<Windows 路徑>`，串流回進度 |
| `GET /rpc/thumb` | `path=<Windows 路徑>` → Telegram 的預覽圖（JPEG）。縮圖處理常式專用；沒有預覽就回 404，讓它去走 fallback |
| `GET /rpc/props` | `path=<Windows 路徑>` → `{width,height,duration,size}`，取自 document attributes，不讀檔案內容 |

## rclone 掛載參數

`start.bat` 裡已經帶好，重點：

- `--vfs-cache-mode full` + `--vfs-cache-max-size 460G`：sparse，只存讀到的區段，**容量驅動**淘汰。
- `--vfs-cache-max-age 8760h`：實質停用**時間**淘汰。age 到期就丟等於把還會用的資料重抓一次，
  白費頻寬又吃 SSD TBW。這個使用型態沒有別的資料競爭快取，上次用過的東西數月後很可能還在，
  第二次開即本機速度。SSD 壽命非問題：約 0.6 TB/年寫入，512 GB 消費級 NVMe TBW 約 300 TB。
- `--dir-cache-time 1h`：檔案多時 10s 會反覆打 API。網頁改動後用 `rclone rc vfs/forget`。
- **不套 `crypt` / `compress`**：否則網頁端下載到加密/壓縮後的內容，
  失去「瀏覽器也能直接看」這個核心價值。

## 驗證

離線測試（不需 Telegram、不需 rclone、不需憑證）：

```powershell
.venv\Scripts\python.exe -m pytest tests -q
```

| 檔案 | 覆蓋 |
|---|---|
| `tests/test_split_math.py` | offset→(part, 內部 offset) 映射、跨界切段、切檔邊界、`SeekableRemoteFile` 的 seek/range/block 快取 |
| `tests/test_zipfs.py` | 虛擬樹結構（含空目錄、非 ASCII、隱含目錄、traversal 防護）、local header 偏移、單 entry 位元組範圍、目錄快取 |
| `tests/test_bridge_e2e.py` | 真的用 HTTP 跑整個 bridge（PROPFIND / GET / Range / 403 / MKCOL+PUT → 打包 → 上傳 → 再瀏覽 / `/rpc/*` / fetch-local），只有 MTProto 與 backend 是假的 |

掛載後還需要手動走一遍（這部分測試無法代替）：

1. `rclone ls teledrive:` 與網頁列表一致
2. 小檔 / 非 split 大檔 / split 大檔各取一份，`certutil -hashfile <檔> SHA256` 與網頁下載相同
3. 量測冷讀吞吐（決定要不要做並行 chunk 下載）
4. 把多層資料夾移入 `E:\game\`，debounce 到期後網頁出現 `<名稱>.zip`，`E:` 上仍是資料夾且內容正確
5. 對虛擬 zip 資料夾右鍵取回 → 解壓後遊戲能執行
6. 快取到 `--vfs-cache-max-size` 上限時淘汰正常
7. 最後在瀏覽器開 `https://teledrive.yoyotsaoteledrive.dpdns.org` 確認 `/game` 上傳的 zip 顯示、下載正常

## 已知限制與風險

1. **首次上傳是主要成本**：8 TB 在上行 100 Mbps 約 7.4 天、20 MB/s 約 4.5 天，期間會撞 FLOOD_WAIT。
2. **唯一副本風險**：Telegram 帳號被封或誤刪即全失，無版本保護 → 定位為第二份冷封存。
3. **session 開始要等取回**：先按「儲存在本地」預熱仍是最有效的做法。
   並行 chunk 下載已經做了（`download_connections`，預設 8 條獨立連線，實測 1.72x），
   但 Telegram 會隨著持續拉取逐步節流，長時間取回的實際速度會低於一開始的爆發值。
4. **照片要先裝縮圖處理常式**（見「縮圖」一節）。沒裝的話 Explorer 會為每張縮圖讀完整張原圖，
   一張 10 MB 的圖就是 10 MB 的流量；裝了之後改抓 Telegram 的 17 KB 預覽。
   代價是那個註冊會攔截整台機器的圖片/影片類型（有 fallback，可完整反安裝）。
5. **4K remux 直接播會邊緣**：1080p remux（~30 Mbps）順；4K（50–80 Mbps）先取回本機。
6. **同名檔案**：TeleDrive 沒有 `UNIQUE(filename, parent_id)` → 取 `created_at` 最新者並記 warning。
6b. **後端的 `filesize` 會灌水**：上傳端以 512 KB 為單位切塊，後端存的是「塊數 × 512 KB」，
   所以比真實長度大最多一塊（實測最多 523,424 bytes）。真實長度在 `file_hash` 的 `:<n>` 後綴。
   照著 `filesize` 宣告會讓客戶端讀到不存在的尾巴 —— 等一段長 timeout 後拿到 0 bytes，
   非 faststart 的 MP4 因此完全無法起播。`Entry.real_size` / `_clip_parts` 就是在裁掉這段。
   另外要注意有些檔案是**上傳中斷**：只註冊了第 1 段（`split_group_id` 有值但只有 part 0），
   那是真的少資料，只能刪掉重傳。
7. **一機一份 bridge**：只有跑 bridge 的那台 PC 能掛 `E:`。
8. Windows 11 右鍵選單位置限制（見上）。

## 明確不做

- 一般檔案的 PUT / 覆寫 / 版本回收（照片影片上傳走網頁，那邊有縮圖、去重、album 分組）
- **block 級部分更改**：WebDAV 只有整檔 PUT，rclone 也是整檔重傳。真要做得改用 WinFsp（`winfspy`）
  自己實作檔案系統才會收到 `write(offset, len)`；儲存端不用改（`split_group_id` + `part_index` 已是 block 結構），
  但整個 bridge 幾乎重做。三類內容（照片備份 / 遊戲 / 影片）都不需要。
- 並行 chunk 下載（M5，先量測再決定）
- MSIX + `IExplorerCommand`、PyInstaller 打包 exe、開機自啟、GUI 設定介面
