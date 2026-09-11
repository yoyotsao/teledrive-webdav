# teledrive-webdav — 把 TeleDrive 掛成 Windows 磁碟

裝好之後，你的 Telegram 雲端空間就是檔案總管裡的一台磁碟（預設 `H:`）。

- **瀏覽**：像本機資料夾一樣看，有縮圖、有圖片尺寸、影片可以直接點開播
- **看**：檔案不會整份下載，只讀你實際用到的部分
- **存遊戲**：把遊戲資料夾丟進 `H:\game\`，它會自己打包上傳；之後在 `H:` 上仍然看得到裡面每個檔案
- **整理**：一般檔案與資料夾可直接新增、覆寫、刪除；刪除會移到 TeleDrive 網頁的垃圾桶
- **取回**：右鍵「儲存在本地 (TeleDrive)」把東西抓回硬碟

> `H:\game\` 裡已打包 ZIP 所呈現的內部檔案是虛擬節點，不能單獨修改或刪除；
> 一般路徑上的真實檔案與資料夾則可寫入與刪除。

---

## 需要準備什麼

- Windows 10/11
- Python 3.10 以上
- 一組 Telegram 帳號（會用手機收驗證碼）
- 一顆放快取的硬碟。預設用 `E:\teledrive`，建議 SSD，抓過的東西會留在這裡，
  下次開就是本機速度。

---

## 安裝（大約 20 分鐘，只做一次）

### 1. 裝兩個外部程式

在 PowerShell 執行：

```powershell
winget install Rclone.Rclone
winget install WinFsp.WinFsp
```

裝完**關掉 PowerShell 再開一個新的**（讓 PATH 生效）。

### 2. 產生 Telegram 登入憑證

```powershell
cd D:\python\teledrive
python generate_session.py
```

會問你手機號碼，然後 Telegram App 會收到驗證碼。輸入完成後憑證就寫好了，之後不用再做。

### 3. 建立 config.ini

```powershell
cd D:\python\teledrive-webdav
copy config.example.ini config.ini
notepad config.ini
```

大部分不用改。通常只會動這幾個：

| 設定 | 預設 | 意思 |
|---|---|---|
| `cache_dir` | `E:\teledrive` | **所有東西**都放這裡（快取、取回的檔案、暫存）。要指到空間夠的碟。 |
| `mount_drive` | `H:` | 要掛成哪個磁碟機代號。**改了的話 `start.bat` 裡的 `MOUNT=` 也要一起改。** |
| `debounce_minutes` | `5` | 丟進 `H:\game\` 的資料夾靜置多久算「搬完了」，然後開始打包 |

帳號密碼三個欄位（`api_id` / `api_hash` / `session`）**留空就好** —— 它會自己去讀
步驟 2 產生的檔案，憑證只存在一個地方。

### 4. 第一次啟動

```powershell
.\start.bat
```

第一次會自己建 Python 環境、裝套件（要等幾分鐘），然後掛上 `H:`。
打開檔案總管應該就看得到 `H:` 了。

**這個視窗要一直開著**，關掉或按 Ctrl+C 就等於把磁碟拔掉。
以後每次要用，就是執行 `start.bat`。

### 5. 裝右鍵選單

```powershell
.venv\Scripts\python.exe install_menu.py --install
```

之後在 `H:` 底下的檔案按右鍵 → **顯示更多選項**（或按 Shift+F10）→
會看到「儲存在本地 (TeleDrive)」。

> Windows 11 的第一層精簡選單只給市集 App 用，所以這一項固定在「顯示更多選項」裡面。

### 6. 裝縮圖與屬性（**強烈建議，不裝會很慢**）

不裝的話，檔案總管每產生一張縮圖就會把**整張原圖下載一遍**——
一張 10 MB 的圖就是 10 MB 流量，一個資料夾看下來要好幾分鐘。

裝了之後，改成抓 Telegram 存的那張約 17 KB 的預覽圖，快 100 倍以上。

```powershell
shellthumb\build.bat
.venv\Scripts\python.exe install_thumb.py --install
```

（`build.bat` 會編出兩個東西：處理常式的 DLL，以及步驟 7 預熱要用的
`warmshell.exe`。需要 Visual Studio 的 C++ 工具。）

接著**以系統管理員身分**開一個 PowerShell，再跑：

```powershell
cd D:\python\teledrive-webdav
.venv\Scripts\python.exe install_thumb.py --install-props
```

（第二行處理的是「圖片尺寸」。少了它，檔案總管會為了知道長寬去讀每張圖的檔頭，
一樣會慢。這一步一定要管理員權限。）

想確認裝好了沒：

```powershell
.venv\Scripts\python.exe install_thumb.py --status
```

**要移除**：`--uninstall`（縮圖）和 `--uninstall-props`（屬性，需管理員）。
會把原本的設定完整還原。

<details>
<summary>裝這個會影響到什麼？（點開看）</summary>

Windows 的縮圖與屬性處理是**依副檔名**註冊的，沒有「只對某台磁碟機生效」這種選項，
所以裝了之後，這台電腦上所有 `.jpg` / `.png` / `.mp4` 等 14 種檔案都會先問到它。

程式因此做了一件事：**只要檔案不在 `H:` 上，就原封不動轉交給原本的處理常式**，
行為跟沒裝一樣。安裝時會逐個副檔名記下原本是誰在處理，反安裝時還原回去。

在 `H:` 上但抓不到預覽的檔案（例如 bridge 沒開），也會退回原本的做法，
最差就是慢，不會變成空白圖示。

差別在影響範圍：縮圖那半寫在你的使用者設定裡，只影響你；
屬性那半 Windows 只認機器層級的設定，所以會影響這台電腦的所有使用者，也因此要管理員權限。
</details>

### 7. 預熱（自動，不用做任何事）

`start.bat` 開著的時候，bridge 會自己走遍整棵目錄樹，抓縮圖、抓尺寸，然後
**用檔案總管的方式跟 Windows 要一次縮圖**，讓 Windows 存進它自己的快取。
全部跑完之後，**任何資料夾第一次打開都跟看過一樣快**（每秒約 270 張）。

最後那一步靠 `shellthumb\warmshell.exe`，它由步驟 6 的 `build.bat` 一起編出來。
少了它其他兩層還是有效，只是大約每秒 3 張而不是 270 張。

- 它只在**沒有請求在等**的空檔跑，你在用的時候會自動讓路，不會拖慢瀏覽
- 縮圖、尺寸抓過的自動跳過，關掉 bridge 也沒關係，下次開起來接著跑
- 跟 Windows 要縮圖那一步每輪都會重跑，因為 Windows 自己的快取會被磁碟清理清掉，
  重跑已經在裡面的檔案一個只要 4ms
- 每 6 小時再走一次，網頁那邊新上傳的東西會自己被補上

想立刻抓完（例如剛裝好、今晚就要用），可以另外手動跑一次：

```powershell
.venv\Scripts\python.exe warmup.py            # 全部
.venv\Scripts\python.exe warmup.py pixiv      # 只抓某一區
```

手動跑不會禮讓，所以快得多，但跑的時候瀏覽會比較卡。**不需要跟 bridge 同時跑**——
兩邊會擠在同一條 Telegram 連線上，不會比較快。

不想要自動預抓的話，`config.ini` 裡設 `[warmup] auto = false`。

---

## 日常使用

### 瀏覽

`H:` 就是你的雲端。用檔案總管、看圖軟體、播放器打開都可以。

- 影片可以直接點開播（1080p 順暢；4K 建議先取回本機）
- 遊戲**不要直接在 `H:` 上執行**，先取回本機再玩

### 把遊戲存上去

1. 把整個 `<遊戲名>` 資料夾拖進 `H:\game\`
2. 等 5 分鐘（`debounce_minutes`）沒有新的寫入，它就會自動打包上傳
3. 上傳完之後，`H:\game\<遊戲名>\` 還是一個資料夾，裡面每個檔案都看得到、也能單獨取回

已經上傳過的名字不能再改（往裡面寫會被拒絕）。要更新的話，先去網頁把那個 `.zip` 刪掉，或換個名字。

也可以直接丟**單一檔案**（例如自己壓好的 `X.zip`）進去，會原樣上傳，不再多包一層。

### 取回本機

在 `H:` 底下的檔案或資料夾按右鍵 → 顯示更多選項 → **儲存在本地 (TeleDrive)**。

會跳出一個視窗顯示進度，抓完自動打開資料夾。東西放在 `cache_dir\local\`。

也可以用指令：

```powershell
.venv\Scripts\python.exe fetchlocal.py "H:\game\MyGame"
```

---

## 遇到問題

### `H:` 沒出現 / start.bat 跑失敗

- 訊息裡有 `rclone is not on PATH` → rclone 沒裝好，或裝完沒重開 PowerShell
- 訊息裡有 `bridge did not come up` → 執行 `.venv\Scripts\python.exe bridge.py`，
  視窗裡會直接印出原因（通常是 config.ini 或憑證的問題）
- `H:` 已經被別的東西佔用 → 改 `config.ini` 的 `mount_drive` 和 `start.bat` 的 `MOUNT=`，兩邊要一致

### 瀏覽變慢、縮圖跑不出來

依序試：

1. `start.bat` 的視窗還開著嗎？關掉就沒有磁碟了
2. `.venv\Scripts\python.exe install_thumb.py --status` —— 兩個 handler 都有裝嗎？
3. `.venv\Scripts\python.exe warmup.py` —— 新上傳的東西背景預抓還沒輪到，手動跑一次就好

正常速度是**每秒 10 張以上**。進一個從沒開過的資料夾，第一個檔案可能要等十秒左右
（在解析路徑），之後就順了。

### 影片點開沒反應 / 播不動

先確認同一個檔案在 TeleDrive 網頁上能不能正常下載。如果網頁也不行，
就是當初上傳時中斷了，只能到網頁刪掉重新上傳。

### 檔案總管的縮圖怪怪的、想整個關掉

```powershell
.venv\Scripts\python.exe install_thumb.py --uninstall
# 系統管理員身分：
.venv\Scripts\python.exe install_thumb.py --uninstall-props
```

會完整還原成沒裝之前的狀態。

### 網頁上改了東西，`H:` 沒跟著變

目錄清單有快取。等一小時，或直接重跑 `start.bat`。

---

## 要注意的事

1. **這是你唯一的一份備份的話，風險很高。** Telegram 帳號被封或誤刪就全沒了，
   沒有版本回溯。建議當成第二份冷備份，不要當唯一備份。
2. **第一次把大量資料傳上去很花時間。** 8 TB 在 100 Mbps 上行約 7 天，中途 Telegram 會限速。
3. **只有跑 `start.bat` 的那台電腦看得到 `H:`。** 這不是網路磁碟。
4. **快取會長大。** `cache_dir` 預設上限 160 GB，滿了會自動淘汰最舊的，不會塞爆硬碟。
   想清空的話，`cache_dir\meta` 和 `cache_dir\rclone` 兩個資料夾可以直接刪
   （只是要重抓）；**`cache_dir\local` 不要刪**，那是你取回的檔案本體。
5. **看幾萬張照片還是建議用網頁。** 那是這種掛載方式最不擅長的場景。

---

## 開發者

架構、設計取捨、測試方式請看 [CLAUDE.md](CLAUDE.md)。
