# Log 只認檔案 ID、不認檔名 設計規格

日期：2026-11-22
狀態：設計（未實作）

## 摘要

雲端硬碟的檔名與路徑是隱私：資料夾結構與檔名本身就說明了這個人存了什麼。
目前 `bridge.log`、`rclone.log`、`/rpc/status` 都直接寫出檔名與完整路徑，
而 log 是最常離開這台機器的東西（貼 issue、貼給 AI、傳給別人）。

本規格把「哪個檔案」在所有診斷輸出裡一律換成 **rclone 的穩定檔案 ID**：

```
id=0x2f8a91c04d7e3b15
```

除錯需要的是「有一個檔案出問題、它在哪一步出問題」，不是它叫什麼名字。

## 背景

- CLAUDE.md 的 redaction 規則（「憑證不外洩」）只涵蓋 JWT、session、auth key 等**憑證**，
  理由是帳號接管。它從來沒有回答「log 會不會洩漏內容」，所以檔名一直被當成可以印。
- 同一份文件說 `/rpc/status`「貼進 issue 是安全的」——那句話只對憑證成立，
  `uploads.pending[].path` 是完整路徑。
- 目前直接印檔名/路徑的 log 呼叫約 60 處（盤點見「D2」），另外還有例外訊息
  （`OSError` 會帶完整本機路徑）、Telethon 不帶檔名、rclone 的錯誤訊息帶遠端路徑
  與 VFS 快取路徑。

## 目標

1. `bridge.log` 不出現任何 drive 上的檔名或路徑，也不出現對應到 drive 路徑的本機路徑
   （`uploads/`、`staging/`、rclone VFS 快取）。
2. `rclone.log` 同上（盡力而為，見「風險」）。
3. `/rpc/status` 的檔案欄位改成 ID。
4. 同一個檔案在 `bridge.log`、`rclone.log`、Windows（`fsutil file queryFileID`、
   thumbcache）看到的是**同一個數字**，跨 bridge 重啟、跨重新掛載都不變。

## 非目標

- **不提供從 ID 反查檔名、路徑的方法**——連本機工具也不行。D6 的工具從 ID 只給得出
  除錯需要的屬性，不給名字。
- 不改回給瀏覽器的 `/rpc/game/*`、`/rpc/fetch-local` 回應：呼叫端自己送了那個路徑，
  回應裡出現它不是洩漏。（`_safe_download_error` 既有的本機路徑遮蔽維持。）
- 不改 log 等級設計、不合併重複行——那是另一件事（「log 可讀性」），只在
  `premium flood` 那行順手把 `file_job_id` 旁邊加上 ID（見 D2）。
- 不處理 Telethon 的 log：它不帶檔名。
- 不處理 log 裡既有的 **telegram user id**（`transfer complete accounts=`、premium flood、
  migration 那幾行，`upload_engine.py` 由 `p.telegram_user_id` 組成）。那是帳號識別資訊、
  不是檔名，另案處理；本規格只保證 D6 的輸出不帶它（改用帳號序號）。

## 不變量

- **ID 的定義只有一個**：rclone patch 裡的共用雜湊（D5 之後是 `fs.StableID(path)`，
  `stable_ino.go` 的 `stableIno` 呼叫它）。Python 端是它的逐位元移植，
  有測試釘住兩邊對同一組路徑算出相同的值。
- **重構 rclone patch 不得改變任何既有 ID**：`stableIno` 的值就是 Windows thumbcache 的 key，
  改一個位元等於把整個 `H:` 的縮圖快取再作廢一次（見 CLAUDE.md thumbcache 那條坑）。
- 任何新的 log/status/錯誤字串要提到檔案，走 ID helper；不允許「為了好讀印檔名」。

## ID 的定義

```
id = FNV-64a(utf8(rclone VFS path)) & ~(1 << 63)；若為 0 則為 1
顯示：id=0x%016x
```

**rclone VFS path** = `node.Path()`：相對於掛載根、`/` 分隔、沒有開頭的 `/`、
根目錄是空字串。例：`H:\Hololive\a\00000.m2ts` → `Hololive/a/00000.m2ts`。

bridge 內部的 DAV 路徑（`/Hololive/a/00000.m2ts`）去掉開頭 `/`、URL 解碼後就是它。
`/game` 底下的虛擬 zip 資料夾與 zip 成員一律用**它們在 `H:` 上出現的路徑**算，
不是 backend 的 row 名稱。

**正規化是最可能對不上的地方，不是雜湊本身。** 向量測試只證明 hash 移植正確；
餵進去的字串若跟 `node.Path()` 差一個字元，ID 就完全不同。實作前要先在真的 `H:` 上確認：

- **rclone 名稱編碼**：webdav backend 與 cmount 在 Windows 上會把 `: * ? " < > |`、
  結尾空白／句點換成全形字元。`node.Path()` 是編碼前（remote 名稱）還是編碼後
  （Windows 看到的名稱）要查清楚，`tag_dav`（remote 名稱）與 D3 從 `H:\...` 抓到的
  （Windows 名稱）要各自轉成同一種形式。
- **Unicode NFC/NFD**：不做任何正規化，原樣用 backend 的位元組（macOS 上傳的可能是 NFD）。
- **大小寫**：`H:` 不分大小寫，例外訊息裡的 `H:\...` 是呼叫端寫的大小寫，不一定是
  backend 的。D3 對 `H:` 路徑算出的 ID 因此是「盡力」，而 D2 一律用 resolver 解析過的
  canonical 路徑。

**已知性質（使用者已接受）**：FNV-64a 沒有密鑰。拿到 log 的人無法從 ID 反推檔名，
但若**猜到完整路徑**（每一層、大小寫、空白、副檔名都對）可以算出同一個 ID 來確認。
換成 HMAC 能擋掉這點，代價是 ID 跟 rclone/Windows 對不上，所以不採用。

## 設計

### D1：`fileid.py`（新模組）

```python
def path_id(vfs_path: str) -> int          # stableIno 的移植
def tag(vfs_path: str) -> str              # "id=0x2f8a91c04d7e3b15"
def tag_dav(dav_path: str) -> str          # 去開頭 "/"、unquote 後呼叫 tag
def tag_local(path) -> str | None          # cache_dir 底下對應到 H: 路徑的本機路徑 → 那個 H: 路徑的 tag；
                                           # 其他本機路徑回 None
```

`tag_local` 的對應：
- `<cache_dir>\uploads\<rel>` → `<rel>`（一般路徑的暫存，結構與 H: 相同）
- `<cache_dir>\staging\<top>\...` → `game/<top>/...`；`staging\.pack\<stem>.zip`、
  `staging\.convert\<file_id>.part` 這類程式自用的檔 → 所屬 unit 的 `game/<top>`
- `<cache_dir>\rclone\vfs\<remote>\<rel>` 與 `vfsMeta\<remote>\<rel>` → `<rel>`
- `<cache_dir>\local\<rel>` → `<rel>`（「儲存在本地」與 Reina 下載的遊戲，結構與 H: 相同；
  `gamestate`／`gamelaunch` 的 `log.exception` traceback 會帶出遊戲根目錄）
- `<cache_dir>\rejected\<rel>` → `<rel>`（`rejectedputs` 收下的 PUT）
- `<cache_dir>\savedata-backups\<game_id>\...` → 不是 drive 路徑，回 None，
  由 D3 遮成 `<cache>\…`（裡面是使用者存檔目錄的複本，名稱同樣不該外流）

另外提供 `python fileid.py tag "H:\..."`：**離線從路徑算 ID**，不需要檔案存在、
不需要掛載。這是「路徑 → ID」，不違反「不能從 ID 反查」的非目標；
除錯時用它比 `fsutil file queryFileID` 可靠（後者要求檔案存在、輸出可能是補零的 128-bit）。

### D2：逐一替換 log 呼叫

把每個以檔名/路徑為參數的 log 呼叫改成傳 `fileid.tag*()`。盤點（2026-11-22）——
**行號只是起點，會隨改動漂移；完整性以全域守門測試為準**，不是以這張表為準：

| 檔案 | 呼叫 |
|---|---|
| `bridge.py` | thumbnail/head 快取失敗（340、419、442、459）、zip 目錄讀不到（706、1241）、`_unavailable`（1004）、listing 少項目（1039–1045，含子項名稱）、建資料夾失敗（1109）、resolve crash（1518）、403 寫入保護（1592、1600） |
| `gamestage.py` | 刪除失敗（85）、收養 unit（293）、上傳完成（366）、沿用 pack（434）、7-Zip 解壓（449）、打包完成（479）、預覽（527） |
| `uploadstage.py` | 收養（~334）、放棄（528） |
| `upload_engine.py` | `transfer failed`（679、738）、`registration failed`（1169、1195）、`transfer complete`（1172、1205）、`premium flood`／migration（901、918，加上 ID） |
| `rarconvert.py` | 下載／讀不到／遺忘／進度（100–191） |
| `zipfetch.py` | 下載／解壓失敗（185、234，含回給 CLI 的 `ERROR` 行） |
| `fetchlocal.py` | 取回失敗（249） |
| `tdapi.py` | 同名 row 警告（786、789，`entry.name` → ID，`parent_id`/`file_id` 保留） |
| `tgio.py` | `chunk timing` 的 `top:`（`_doc_label`）、預覽失敗（1403、1406）；**印 message id 的**：`thumbnail for message %s failed`（742）、`media info for message %s failed`（772）、`file_reference expired for message %s`（902、925），以及 `RemoteIdentityError`（`Telegram file mismatch`）的例外文字 |
| `warmup.py` | listing 失敗（190）、warmshell 卡住的那個路徑 |
| `zipfs.py` | 不安全／衝突的成員名（239、249）→ 封存的 ID + 成員序號 |
| `rejectedputs.py` | 收下 PUT 的 WARNING（183）：`key`（DAV 路徑）→ `tag_dav`，`target`（`rejected\` 本機路徑）→ 拿掉，ID 已經足夠 |
| `gamestate.py` / `gamelaunch.py` | `log.exception`（147、158、184、463；407、483）本身的訊息不帶路徑，traceback 交給 D3（靠 `local\` 對應） |

**ID 會換的轉換點要印一次對照。** 改名＝新 ID，而有兩個轉換是程式自己造成的，
上傳排查正好要跨過它們：

- `/game` 打包：staging 的 `game/<top>` → 上傳後的 `game/<top>.zip`
- RAR 轉換：`game/<stem>.rar` → `game/<stem>.zip`

這兩處在完成那一行印 `from=id=0x... to=id=0x...`，只印這一次，之後一律用新 ID。
不印的話兩段 log 沒有名字可以接起來。

**同一路徑的多次寫入 ID 相同**（覆寫、失敗後重送）。`transfer failed`／`transfer complete`
帶上 `attempt=<n>`（`uploadstage` 佇列已經有的失敗次數），才分得出是第幾次。

**message id 也換成檔案 ID。** 它不是檔名，但不該出現在 log 裡：
(1) 整份 log 只有一種識別方式，才不需要交叉比對；(2) 有帳號權限的人拿 message id
就能在 Telegram 上打開那個檔案，是另一條找回內容的線索。Telethon 自己的 log
不帶 message id，所以換掉之後沒有東西需要拿它對照。真的要直接檢查 Telegram 上那則
訊息時，是開發者用帳號跑腳本，腳本吃檔案 ID、在內部換成 message id。
`tgio` 在這幾處手上只有 message id：沿用既有的 `note_read_label`／`READ_LABELS`
（message id → 最近一次開它的路徑），改成存**檔案 ID**而不是路徑；
預抓與 sweep 這類沒經過 `open_remote` 的呼叫，由呼叫端把 ID 一起傳下來。
查不到時印 `id=?`，**不退回印 message id**。

`tdapi.py` 的 `could not persist %s`、`upload_limiter.py` 的狀態檔路徑是**程式自己的檔案**
（`meta/` 底下），不是 drive 內容，維持原樣。

`chunk timing` 的 `top:` 改為 `id=0x... dc5 n=7 mean=1027ms`，排序改成依 `mean`
（最慢的在前）——原本依次數排序，每個 n=1 時列出的三個檔沒有意義。

### D3：輸出端的防漏網（`bridge.py` 的 log formatter）

D2 只管得到我們自己組的字串。例外訊息與 traceback（`log.exception`）在**格式化之後**處理。

**這必須是 `Formatter`，不能是 `logging.Filter`。** `Handler.handle()` 先跑 filter，
`emit()` 裡才呼叫 `Formatter.format()`；traceback 文字是 `format()`（`formatException`）
當下才從 `exc_info` 組出來的，filter 看到的只有 `record.msg`／`record.args` 跟還沒格式化的
`exc_info`。所以做法是 `ScrubbingFormatter(logging.Formatter)` 覆寫 `format()`，
對 `super().format(record)` 的**整段輸出**做替換，並且同時掛在 rotating file handler
與 console handler 上（使用者也會貼 console）。

**stderr 也要攔。** 不經過 logging 的輸出——`threading.excepthook`、`sys.excepthook`、
cheroot 自己印的錯誤——在 `bridge.main` 啟動時把兩個 excepthook 換成走 `log.exception`
的版本，讓它們也經過同一個 formatter。

替換規則：

- 落在 `cache_dir` 底下且 `tag_local` 認得的本機路徑 → 換成 tag。
- 其他絕對 Windows 路徑（`X:\...`、`\\?\...`）中，落在 `H:` 的 → `tag`；
  落在 `cache_dir` 但不認得的 → `<cache>\…`；其他維持（程式碼路徑如
  `.venv\...\telethon\...` 是 traceback 的必要資訊）。
- `http://127.0.0.1:8081/<path>` → `http://127.0.0.1:8081/<tag>`。

Formatter 是防漏網不是主要機制：它認不出不帶目錄的裸檔名（`entry.name`），
所以 D2 仍然必須逐一改。

### D4：`/rpc/status`

`uploads.pending[].path`、`/game` 的 `units[].name` 改為 `id`（字串 `0x...`）。
`detail`（錯誤文字）過一次 D3 的同一套 scrub。CLAUDE.md 那句「貼進 issue 是安全的」
在這之後才真的成立。

### D5：rclone log patch（`tools/rclone-stableino/`）

rclone 所有帶檔案的 log 都經過 `fs/log.go` 的 `logSlogWithObject`，
它把 `o` 用 `fmt.Sprintf("%+v", o)` 變成 `object` attr；text 與 JSON 兩種輸出都讀這個 attr。
patch 改兩處：

1. **`object`**：`o` 不是 `fs.Fs`（那是 remote 名稱 `teledrive:`，不是檔案）時，
   把 `%+v` 的結果當成 VFS path 算 `stableIno` 同一個雜湊，`object` 換成 `id=0x...`。
   webdav backend 的 Object/Dir 的 `String()` 是相對 remote 根的路徑，
   VFS 的 File/Dir 是 `node.Path()`，兩者在這個掛載下相同，所以算出同一個 ID。
2. **訊息本文**：rclone 會把路徑寫進訊息文字。以規則替換兩種已知形狀：
   - `http://127.0.0.1:<port>/<escaped path>` → 解碼後算 ID
   - `<--cache-dir>\vfs\<remote>\<rel>`、`vfsMeta\...` → `<rel>` 的 ID

實作方式：新增 `stable_log.go`（與 `stable_ino.go` 同一個做法，`build.py` 複製進
`src/fs/`），`fs/log.go` 一行 patch 讓 `logSlogWithObject` 呼叫它。`build.py` 既有的
「patch 對不上就失敗」同樣套用。

**共用雜湊必須放在 `fs` 這一層。** `stableIno` 目前在 `package cmount`、吃 `vfs.Node`；
`cmount` 已經 import `fs`，`fs` 不能反過來呼叫 `cmount`（import cycle）。所以：

- 新增 `src/fs/stable_id.go`：`func StableID(path string) uint64`（FNV-64a、清最高位、0 → 1）。
- `stable_ino.go` 改成 `return fs.StableID(node.Path())`。
- 這等於動到 thumbcache 修法的那份 patch：`stable_log_test.go` 用同一組向量同時檢查
  `StableID` 與重構前 `stableIno` 的寫死值，**一個位元都不能變**（見「不變量」）。
  現場驗證另外用 `SIIGBF_INCACHEONLY` 探針確認重編後已暖過的資料夾仍是 HIT。

`--log-level` 維持現狀：檔名已經不在 log 裡，沒有理由再犧牲診斷能力。

### D6：查詢工具 `fileid.py info <id>`

從 log 拿到一個 ID 之後，回答「這是什麼檔、存在哪、現在什麼狀態」，**不回答它叫什麼**。
只在本機執行，輸出不寫進任何 log。輸出本身不含名字、路徑、內容指紋、帳號識別資訊，
以及可在 TeleDrive／Telegram 上找回檔案的 id，所以**可以直接貼出去**。

找檔的方式：掃 `meta/dirs/` 的快取樹（加上 `uploads/`、`staging/` 這兩個本機暫存），
對每個 `H:` 路徑算 ID 比對；名字只在記憶體裡用來比對，不輸出。
快取樹裡沒有的（例如從沒列過的資料夾）回「找不到」，不為了它打 backend。
也提供 `/rpc/fileinfo?id=` 給 bridge 開著時使用（多了記憶體裡的上傳與快取狀態）。

**顯示的欄位：**

| 分組 | 欄位 | 為什麼需要 |
|---|---|---|
| 這是什麼檔 | 格式：副檔名 + MIME | 縮圖、屬性、album、`/game` 打包都按類型分流 |
| | 大小：真實大小的量級（`~1.2 GiB`），加上 backend `filesize` 與真實大小的**差值**（位元組） | 差值本身就是線索（「filesize 灌水」那條坑）；精確大小不顯示，理由見下表 |
| | 媒體屬性：寬高；影片加長度 | 縮圖／屬性問題 |
| 在哪一類位置 | 位置類型：一般路徑／`/game` 暫存中／`/game` 已打包 zip／zip 成員 | 四種走完全不同的程式路徑 |
| | 路徑深度（第幾層） | 每一層一次 backend 往返（「效能」第 7 節） |
| 存在 Telegram 哪裡 | 儲存方式：單一檔／split（幾個 part） | |
| | 每個 part 的帳號**序號**（`primary`／`#2`／`#3`…，照 session registry 的排序）與 DC | 跨帳號、跨 DC 是最常出事的地方；序號夠用來看「是不是跨帳號」 |
| 現在的狀態 | 上傳狀態：暫存中／上傳中／失敗次數／abandoned，與最後一次錯誤（已 scrub） | |
| | 快取狀態：預覽、屬性快取有沒有、`has_thumbnail` 旗標 | 「縮圖沒出來」先看這三個 |
| | 同名 row 數量 | backend 沒有唯一性（「已知限制」第 5 點） |
| | 建立時間 | |

**刻意不顯示的：**

| 欄位 | 為什麼 |
|---|---|
| 檔名、路徑、上層資料夾名稱 | 本規格的目的 |
| `file_hash`（內容指紋） | 比 ID 更危險：ID 要猜中完整路徑才能確認，指紋只要對方手上有同一份檔案就能確認。只顯示「有／無」 |
| backend `file_id`、`parent_id` | 有 TeleDrive 登入就能在網頁／API 查到名字，等於繞過 ID |
| Telegram message id | 有帳號權限就能打開檔案；除錯不需要（見 D2） |
| 精確位元組大小 | 跟 `file_hash` 同一個理由的弱化版：大檔的精確大小幾乎唯一，手上有同一份檔案就能確認。只給量級與「backend 差值」 |
| telegram user id | 帳號識別資訊。用序號取代 |

> `transfer complete bytes=` 目前在 log 裡印精確大小。本規格不改它（log 是開發者排查
> 上傳用的，精確長度是 split 數學的必要資訊），但 D6「可以直接貼出去」的標準比 log 嚴，
> 兩者的差別是刻意的。

### D7：清掉舊 log

上線並確認新 log 不含檔名之後，刪除 `meta/bridge.log*` 與 `meta/rclone.log*`
（含輪替的 `.1`–`.3`）。`rclone.log` 被執行中的 rclone 開著，要在 D5 重新掛載時、
rclone 停止與啟動之間刪除。

## 測試（離線）

- `tests/test_fileid.py`：
  - `path_id` 對一組路徑（ASCII、中日文、空白、根目錄、`H:` 形式與 DAV 形式，
    **以及 rclone 會編碼的名字**：含 `:`／`?`、全形字、結尾空白、NFD）的值，
    跟用 Go 跑 `stableIno` 算出的固定向量相同（向量寫死在測試裡）。
  - `tag_local` 的每一種對應（含 `local\`、`rejected\`），以及不認得的本機路徑、
    `savedata-backups\` 回 None。
  - `fileid.py tag "H:\..."` 與 `path_id` 一致。
- `tests/test_log_privacy.py`：
  - D3 formatter：`OSError` 帶 `uploads\`、`staging\`、`local\`、VFS 快取路徑的訊息，
    **以及 `log.exception` 印出的 traceback** 被換成 tag；`.venv` 的 traceback 路徑保留。
    （用 `logging.Filter` 的實作會在 traceback 這條測試失敗——這就是它的目的。）
  - `threading.excepthook` 拋出帶路徑的例外，輸出同樣被 scrub。
  - **全域守門**：用一組帶明顯標記的檔名（如 `PRIVATE-名前-123.jpg`）跑過
    `test_bridge_e2e.py` 的主要情境（PUT 上傳、失敗重試、rejectedputs 收下、`/game` 打包、
    listing 失敗、zip 讀取失敗），捕捉所有 log 記錄、**stderr** 與 `/rpc/status`，
    斷言標記一次都沒出現。這條測試是防止之後新增的 log 又把檔名帶回來。
  - `/game` 打包完成那一行同時帶 `from=` 與 `to=` 兩個 ID，且分別等於 staging 路徑與
    `.zip` 路徑的 `path_id`。
- 既有 `test_failover_logging.py` / `test_transfer_logging.py` 改為斷言 ID 而非名稱。
- `tgio` 那幾條 message id 的 log 改印檔案 ID；查不到 ID 時是 `id=?` 而不是 message id。
- D6：`info` 對每一種位置類型給出正確欄位；輸出裡不含守門測試的標記檔名、
  `file_hash`、`file_id`、`parent_id`、message id、telegram user id、精確位元組大小；
  快取樹裡沒有的 ID 回找不到且不打 backend。
- rclone patch：`stable_log_test.go`，同一組向量 + 兩種訊息形狀的替換；
  `StableID` 與重構前 `stableIno` 的寫死值逐一相等。

## 現場驗證（真的 H:）

1. 重啟 bridge、重新掛載後，對 `H:` 做一輪：瀏覽、上傳一個檔、讓一次上傳失敗
   （例如上傳到 `/game` 已打包的名字）。
2. `bridge.log` 與 `rclone.log` 搜尋那幾個檔名的每一段資料夾名稱：0 筆。
3. 同一個檔案：`fileid.py tag H:\...` == `fsutil file queryFileID H:\...`（去掉補零）==
   `bridge.log` 的 `id=` == `rclone.log` 的 `id=`。**刻意挑會被正規化咬到的檔案**：
   檔名含 `:`／`?`（rclone 編碼成全形）、全形字、日文、結尾空白、網頁從 macOS 上傳的
   （可能是 NFD）各一個，不要只挑一般 ASCII 檔名。
4. `/rpc/status` 不含路徑。
5. 重編 rclone 後 thumbcache 沒有被作廢：挑一個之前暖過的資料夾，`SIIGBF_INCACHEONLY`
   探針仍是 HIT（D5 重構了 `stableIno`）。

## 風險與取捨

- **rclone 訊息本文是盡力而為。** 規則只認兩種已知形狀；rclone 別的訊息若用 `%q`
  帶出路徑，會漏。rclone 版本是釘住的，只在手動升級時變；升級的檢查清單加一項
  「跑一輪現場驗證第 2 點」。
- **除錯時看不到是哪個檔。** 這是刻意的。需要的話使用者自己用
  `fileid.py tag "H:\<懷疑的檔>"` 算出 ID 去比對 log。
- **ID 可被「猜中確認」**（見 ID 定義），使用者已接受。
- **改名 = 新 ID**。跟 Windows thumbcache 的行為一致。程式自己造成的兩個改名
  （`/game` 打包、RAR 轉換）印一次 `from=`/`to=` 對照（見 D2）。
- **正規化對不上是最可能的失敗形式**，而且它不會報錯，只會讓 ID 安靜地對不起來。
  所以現場驗證第 3 點挑的是會被編碼／正規化影響的檔名。
- **D5 重構碰到 thumbcache 修法**：向量測試與現場驗證第 5 點兩道保險。

## 實作順序

1. 先在真的 `H:` 確認正規化（`node.Path()` 是 rclone 編碼前還是編碼後、大小寫、NFD），
   結論寫回「ID 的定義」那一節
2. D1 + 測試向量（先用 Go 跑出向量，含編碼過的名字）
3. D3 formatter + excepthook（立刻擋住例外訊息這條最難逐一改的路）
4. D2 逐檔替換 + 全域守門測試
5. D4 `/rpc/status`
6. D6 查詢工具（`info` 與 `tag`）與 `/rpc/fileinfo`
7. D5 rclone patch（`fs.StableID` 抽出、`stable_log.go`）、`build.py`、重新編譯
8. 上線：**等進行中的上傳結束**（重啟 bridge 會讓進行中的上傳從頭再來；
   重新掛載要 `stop.bat` + `start.bat`）→ D7 刪舊 log → 現場驗證
9. CLAUDE.md：「憑證不外洩」擴充為「憑證與檔名都不外洩」，修正 `/rpc/status`
   那句說法，檔案表加 `fileid.py`，`/rpc` 表加 `/rpc/fileinfo`，rclone 掛載參數節加 log patch
