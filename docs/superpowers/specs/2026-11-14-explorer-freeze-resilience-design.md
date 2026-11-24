# H: 故障不拖垮檔案總管 設計規格

日期：2026-11-14
狀態：D1–D5 與 README 已實作（離線測試通過）；D6 另案。現場驗證見文末

## 摘要

`H:` 底下任何一層出錯（後端、網路、壞掉的 zip、rclone 本身），目前都會走同一條路，
最後讓**整個檔案總管**凍住：

> bridge 對列表回 5xx 或卡住數十秒 → rclone 不放棄、一直重試，期間抓著那個資料夾
> 的 dir lock → Explorer 一碰到那個資料夾（或只是查它上層的屬性）就排在鎖後面 →
> 所有檔案總管視窗共用同一個 `explorer.exe`，連 C:、D: 都開不了。

本規格的目標是讓這條鏈在 bridge 這一端就斷掉：**列表類請求永遠在幾秒內回一個
rclone 會接受的答案**。最好的情況是新的列表，其次是舊的列表，最差是一個快速的
錯誤。不是 rclone 會無限重試的 500，也不是讓它乾等 60 秒。

## 背景：實際發生過的卡死

| 日期 | 觸發 | 當時 bridge 的行為 | 結果 |
|---|---|---|---|
| 10-15 | Cloudflare 公開網址整個回 404 | `PROPFIND /` 列表打 backend 失敗 → 500 | `H:` 進不去 |
| 10-15 | Telegram session 失效（`AuthKeyUnregistered`） | bridge 起不來 | rclone 對 connection refused 一直重試 |
| 11-01 | rclone 跑 7.4 天後 pacer 名額漏光（5 條在等、0 條在用） | bridge 正常（PROPFIND 0.003 秒） | 所有沒快取的資料夾卡在同一把 dir lock 上 |
| 11-13 | `/game` 裡一個不是 zip 的 `.zip` → `BadZipFile` | `/game` 列表 500，rclone 每 25 秒重試 | 碰到 `H:\game` 的視窗全部卡住（已修，見 `ZipDirCollection.node`） |
| 11-13 | 本機對外網路斷約一分鐘 | `/files` 等滿 `TIMEOUT = 60` 秒才逾時 | 那一分鐘所有過期的列表全部停住 |

另外還有「慢但不是卡死」的一類：Windows thumbcache 沒被填，JPEG 檔頭要去 Telegram 抓
（warmup 因 warmshell 卡在 PageIn 而關閉）。**不在本規格範圍**，另案處理。

### 現況程式碼的三個缺口

1. **列表只有「新的」或「失敗」兩種結果。** `tdapi.list_dir` 在 TTL 過期後一定打 backend，
   `meta/dirs/` 裡明明有上次的答案，失敗時卻不拿來用，例外直接往上冒到
   `RootCollection.get_member_names` / `GameCollection.get_member_names` → wsgidav 回 500。
2. **等太久。** `tdapi.TIMEOUT = 60`，而且是 per-request 的 read timeout。分頁、重試、
   401 重登疊起來，一次列表最壞會遠超過 60 秒。這段時間 rclone 一直抓著 dir lock。
3. **暫時性失敗被當成「不存在」。** `get_resource_inst` 在 resolve 丟 `ApiError` 或其他例外時
   `return None`，也就是 404。rclone 會把它當成「這個資料夾被刪了」寫進 dir cache，
   網路恢復前，使用者看到的是資料夾消失，而不是暫時讀不到。

## 目標

- G1：任何列表（PROPFIND Depth 0/1）在 **`LIST_DEADLINE`（預設 8 秒）內**回應。
- G2：backend 或網路暫時失敗時，只要這個資料夾曾經被列過（記憶體或 `meta/dirs/`），
  就回**舊的列表**，不回錯誤。
- G3：真的沒有任何答案時，回 **503**，不回 404 也不回 500；在 bridge 端要快速失敗。
- G4：單一子項目（壞 zip、讀不到的暫存檔、格式異常的 row）出錯，只影響那一個子項，
  不影響整個列表。
- G5：rclone pacer 名額耗盡要**被偵測並回報**，不能再靠使用者發現「H: 很慢」才知道。

## 非目標

- 不改 rclone 本身，也不換掉 rclone/WinFsp。
- 不處理縮圖、檔頭讀取的速度（thumbcache 那條另案處理）。
- 不讓 bridge 自動重啟或重新掛載 rclone（預設行為；見 D5 的 opt-in）。
- 不處理 bridge 整個行程不在的情況（重啟期間約 20 秒）。這段時間 rclone 一定會重試，
  只能靠 `restart.bat` 盡量縮短。

## 不變量

- 位元組仍只在本機與 Telegram 之間流動；本規格只動 metadata 路徑。
- **sweep（`fresh=True`）永遠不吃舊資料。** warmup 走樹的意義就是發現新東西，
  失敗時要照實失敗，不能用 stale 結果覆寫 `meta/dirs/` 或刷新它的時間戳。
- 舊列表只拿來**回答**，不拿來**寫入**：MKCOL / PUT / DELETE 需要的 parent 解析，
  若只能拿到 stale 結果，照常進行（folder id 不會因為過期而變錯）；但「這個名字
  不存在」這種**否定**判斷，若只有 stale 結果作依據，不能拿來做破壞性決定。

## 設計

### D1：列表 stale-on-error（`tdapi.list_dir`）

```
list_dir(parent_id, fresh=False):
    命中 TTL 內的快取（記憶體 → 磁碟）            → 回傳        （同現況）
    否則向 backend 取，整體期限 LIST_DEADLINE
        成功                                       → 寫快取、回傳（同現況）
        失敗或逾時，且有任何舊副本（忽略 TTL）    → 回傳舊副本，記 warning，
                                                     背景排一次 refresh
        失敗或逾時，且完全沒有副本                → raise BackendUnavailable
list_dir(parent_id, fresh=True):
    向 backend 取；失敗就 raise                   （同現況，不碰舊副本）
```

- **舊副本的來源**：記憶體 `_dir_cache`（即使過期也保留，不要在過期時刪掉），
  其次是 `meta/dirs/<parent_id>.json`。`_dir_from_disk` 需要多一個「忽略 age」的讀法；
  格式不符（舊版檔案）仍視為沒有副本。
- **單一飛行（single-flight）**：同一個 `parent_id` 同時只有一個 backend 請求。
  backend 慢的時候，wsgidav 的 16 條 worker 不會一起去打同一個資料夾，
  也不會把 worker 全部佔光。後到的請求等第一個的結果，共用同一個期限。
- **背景 refresh**：stale 回應之後排一個 refresh，同一個 `parent_id` 只排一個，
  每個資料夾最多每 `STALE_RETRY_SECONDS`（30 秒）試一次，避免 backend 掛掉時被打爆。
  成功後照常寫入記憶體與磁碟。refresh 不需要通知 rclone：rclone 的 dir cache 到期後
  會自己再列一次。
- **warning 去重**：每個 `parent_id` 進入 stale 狀態時記一行 warning，
  恢復時記一行 info，中間不要每次都印。`/rpc/status` 加一個 `stale_listings` 計數。

### D2：期限與逾時（`tdapi`）

- 新增 `LIST_DEADLINE`（`config.ini [bridge] list_deadline_seconds`，預設 8，範圍 2–60），
  涵蓋 `_list_both` 的整個過程：兩個並行的 GET、所有分頁、連線重試。
  到期就放棄（底下的執行緒繼續跑完沒關係，結果丟掉或交給背景 refresh 使用）。
- `requests` 的 timeout 改成 `(connect, read)` 的 tuple：列表用
  `(LIST_CONNECT_TIMEOUT=5, LIST_DEADLINE)`；其他呼叫（註冊、刪除、上傳相關）
  維持現在的 60 秒，它們不在 Explorer 的關鍵路徑上，而且中途放棄的代價較高。
- 401 重登（bot challenge DM）不受 `LIST_DEADLINE` 限制，但重登期間的列表請求
  直接走 stale 路徑，不等它。重登成功後由背景 refresh 補上。

> 8 秒是先拍的數字：要在現場量最大的資料夾（例如 3,756 個檔那個）冷列表要多久，
> 確認正常情況下不會誤判成逾時。量完再定預設值。

### D3：錯誤到 HTTP 狀態的對應（`bridge.py`）

| 狀況 | 現在 | 改成 |
|---|---|---|
| 列表 backend 失敗，有舊副本 | 500 | 207，內容為舊列表（D1） |
| 列表 backend 失敗，沒有副本 | 500 | PROPFIND：**207 空列表**＋恢復後 `vfs/forget`；其他方法：**503**（見「現場驗證結果」） |
| `get_resource_inst` resolve 時 backend 失敗 | `None`（404） | **503**（raise `DAVError(HTTP_SERVICE_UNAVAILABLE)`） |
| resolve 成功、名字確實不存在 | 404 | 404（不變） |
| 單一子項建構失敗（壞 zip、讀不到的暫存檔、異常 row） | 整個列表 500 | 略過或降級那一個子項，記 warning（D4） |

503 的目的不是讓 rclone 不重試（它還是會重試），而是：
(a) 跟 404 分開，不讓 rclone 把資料夾從 dir cache 裡刪掉；
(b) **快速回應**，讓 rclone 拿著 dir lock 的時間從 60 秒以上縮短到幾秒。

**已查 rclone 原始碼（`backend/webdav/webdav.go` 的 `retryErrorCodes` / `shouldRetry`）：**

- 500 與 503 **同樣重試**：`low_level_retries`（預設 10 次），pacer 睡眠從 10ms 指數成長、
  上限 2 秒，十次加總約 6–7 秒（不含每次的回應時間）。所以 503 本身不會減少重試，
  **縮短持鎖時間靠的是 bridge 回得快**：D1 的「剛失敗過就直接回」讓每次重試都是毫秒級。
- webdav backend **不讀 `Retry-After`**，所以不帶。帶了也沒用；萬一將來版本開始尊重它，
  30 秒 × 10 次反而會把持鎖時間拉長到五分鐘。
- 404 不重試，直接變成 `ErrorDirNotFound` 寫進 dir cache —— 這正是 503 要避免的。
- resolve 時的非 `ApiError` 例外（程式 bug）改回 500 而不是 `None`/404：
  rclone 會重試幾秒然後回錯誤，資料夾不會被當成已刪除。

### D4：子項目隔離（`bridge.py` 各 collection 的 `get_member_names` / `get_member`）

把 11-13 對 `ZipDirCollection.node` 做的修法推廣成一條規則：

> 列一個資料夾時，某一個子項目無法建構，只能讓那一個子項目消失或降級，
> 不能讓整個 PROPFIND 回 500。

- `get_member_names` 本身只回名字，通常不會因為單一子項出錯。風險在 wsgidav 對
  Depth 1 逐一呼叫 `get_member` / `get_resource_inst` 取屬性時，某個子項丟出例外。
  **包裝點**：wsgidav 的 `DAVCollection.get_member_list` 對每個子項
  `assert member is not None` —— 子項 resolve 回 `None` 本身就會讓整個 PROPFIND 500。
  `bridge._IsolatedMembers` 覆寫它（`_ReadOnlyCollection`、`GameCollection`、
  `StagingCollection` 都繼承），單一子項的例外或 `None` 記 warning 並略過。
  **例外：子項的 503 照樣往上丟**——因為後端掛掉而把子項從列表拿掉，等於告訴 rclone
  它被刪了，違反「否定判斷不能只靠失敗」的不變量。`get_member_names` 的 `ApiError`
  （含 `BackendUnavailable`）在同一處轉成 503。
- 已知的降級規則：壞 zip → 空資料夾（現況）；暫存檔讀不到（Defender 鎖住等）→
  略過並 warning。

### D5：rclone pacer 監測（`mountctl.py` + bridge 背景執行緒）

11-01 那次 bridge 完全正常，問題在 rclone 自己。bridge 能做的是**看見並回報**：

- 每 5 分鐘抓一次 `http://127.0.0.1:5572/debug/pprof/goroutine?debug=2`
  （rc 已開 `--rc-no-auth`），計算：
  - `waiting`：stack 停在 `pacer.(*Pacer).beginCall` 的 goroutine 數
  - `active`：stack 在 webdav backend 發 HTTP 請求中的 goroutine 數
- 連續 3 次取樣（約 10 分鐘）都是 `waiting > 0 && active == 0`，判定為
  **pacer 名額耗盡**：
  - `bridge.log` 記一行 ERROR，帶上 rclone 執行時間與 waiting 數。
  - `/rpc/status` 加 `mount.pacer_exhausted = true`。
- **自動恢復預設關閉**（`[bridge] auto_recover_mount = false`）。開啟時，只有在
  rclone 沒有待上傳的 writeback（`rc vfs/stats`）而且 bridge 沒有進行中的讀取時，
  才走 `mountctl stop`（`core/quit`，永不強殺）再 `ensure`。
  理由：重新掛載會卸掉 `H:` 並清空 dir cache，這是使用者要自己決定的事。
- 抓 goroutine dump 的成本：幾百 KB 的文字，每 5 分鐘一次，可以接受。
  stack 的字串比對寫在 `mountwatch.count_pacer`。11-01 那份 dump 沒有留下來，
  測試資料是照 rclone 真實的 frame 名稱與格式（對照目前 live rclone 的 dump 與
  `lib/pacer/pacer.go`）組出來的；rclone 升級後函式名變了，測試會先發現。
- **實作時多加的一個條件**：`beginCall` 裡有一個補 token 的計時 goroutine
  （`beginCall.func1`，在 `time.Sleep`）。它存在就表示 token 正在路上，只是在節流，
  不算卡死。判定式是 `waiting > 0 && active == 0 && refilling == 0`。
  它的 `created by ...beginCall` 那一行要排除，否則會被誤算成 waiting。
- rc 連不上（rclone 不在）不算證據，streak 歸零；那是 `mountctl` 的問題。
- 自動恢復每次「卡死事件」只嘗試一次；`vfs/stats` 讀不到也視為忙碌、不動。

### D6：bridge 在 Telegram 失效時仍提供列表（第二階段）

10-15 那次 session 失效時 bridge 直接起不來，`H:` 整個消失。列表其實不需要 Telegram：
只要 `token.txt` 還有效（JWT 24 小時），或 `meta/dirs/` 有副本，就答得出來。

- Telegram worker 啟動失敗時，bridge 照常啟動 DAV server，進入 degraded 模式：
  列表正常（D1 的 stale 規則照用）、讀檔與縮圖回 503、上傳暫停（暫存照收）。
- `/rpc/health` 回 `ok: false, telegram: "unauthorized"`；`bridge.log` 每小時提醒一次。
- 這一項動到啟動流程與帳號 pool，範圍比 D1–D5 大，**排在第二階段**，本次實作不含。

## 使用者端的緩解（文件，不改程式）

README 加一段：資料夾選項 →「檢視」→ 勾選「在不同的處理程序中啟動資料夾視窗」。
`H:` 卡住時，其他已開的視窗比較不會被一起拖住。只能緩解，不能根治：導覽窗格與
「本機」仍然會去問每一顆磁碟。

## 測試（離線）

新增 `tests/test_listing_resilience.py`：

- backend 丟例外 / 逾時，且有記憶體舊副本 → 回舊副本，且在 `LIST_DEADLINE` 內返回
  （用會卡住的假 backend；如果實作沒有遵守期限，測試應該卡住而不是靜靜通過）。
- 同上，只有 `meta/dirs/` 舊副本（模擬重啟後）→ 回磁碟副本。
- 完全沒有副本 → `BackendUnavailable`。
- `fresh=True` 失敗 → raise，且 `meta/dirs/` 的內容與 mtime 都沒被改。
- single-flight：10 條執行緒同時列同一個過期資料夾 → backend 只被打一次。
- 背景 refresh 成功後，下一次列表拿到新資料；backend 持續失敗時，30 秒內最多重試一次。
- stale warning 每個資料夾只記一次，恢復時記一次 info。

`tests/test_bridge_e2e.py` 補：

- 假 backend 掛掉時，PROPFIND 有快取的資料夾 → 207；沒快取的 → 503（不是 500、不是 404）。
- resolve 時 backend 掛掉 → 503，不是 404。
- Depth 1 列表中一個子項建構丟例外 → 207，其他子項都在。

`tests/test_mount_watch.py`：

- 照 11-01 形狀（5 waiting、0 active）組的 dump → 判定 exhausted；有 active 或有補 token
  的計時 goroutine → 不判定。
- 只有連續 3 次才判定，中間恢復一次就歸零。
- `auto_recover_mount = false` 時永遠不呼叫 `mountctl.stop`。

## 現場驗證（真的 H:）

照 CLAUDE.md 的收尾規則，測完要在真的 `H:` 上確認：

1. 列過的資料夾：暫時把 `config.ini` 的 backend URL 指到一個不存在的 host 並
   `restart.bat`，用檔案總管開那個資料夾 → 幾秒內看到內容（舊的），`bridge.log`
   有一行 stale warning。測完改回來。
2. 同條件下開一個從沒列過的資料夾 → 幾秒內回錯誤或空白，**而且其他檔案總管視窗
   （C:、D:）仍然能操作**。這一條才是本規格要證明的事。
3. 改回正確的 URL → 30 秒內 `bridge.log` 出現恢復的 info，`/rpc/status` 的
   `stale_listings` 歸零。
4. 量最大的資料夾冷列表時間，確認低於 `LIST_DEADLINE`，據此定預設值。
5. D5：沒辦法自然重現 pacer 漏光，只驗證監測在正常 rclone 上不誤報（觀察一天）。

## 風險與取捨

- **看到舊資料。** backend 掛掉期間，網頁上新增或刪除的東西在 `H:` 上看不到或還在。
  這本來就是 `dir_cache_seconds = 3600` 的既有行為，只是把可能的過期時間拉長到
  「backend 恢復為止」。比整個檔案總管卡死好。
- **舊資料上的寫入。** 在 stale 的資料夾裡 PUT，parent id 仍然正確，上傳流程本來就
  要等 backend 恢復才會註冊，暫存會留著，不會遺失。
- **503 對 rclone 的效果未驗證**（D3 已列為實作前要先查的事）。如果 rclone 對 503
  一樣抓著鎖長時間重試，G3 只能做到「快速失敗、縮短每次持鎖時間」，消除不了重試。
  有舊副本的情況（D1）不受影響，那才是大多數情況。
- **期限太短會誤判。** 大資料夾在 backend 正常時被誤判成逾時，會一直回 stale。
  先量再定預設值，並在 stale warning 中帶上實際耗時，方便調整。

## 實作順序

1. D2 + D1（期限、stale-on-error、single-flight、背景 refresh）：效果最大，
   能擋住 10-15 的 404 與 11-13 的網路中斷。
2. D3（503 對應）：先查 rclone 對 503 的行為。
3. D4（子項目隔離）：先找出 wsgidav 組 multistatus 的包裝點。
4. D5（pacer 監測，自動恢復預設關閉）。
5. README 的使用者端緩解。
6. D6 另開規格。

## 實作紀錄（2026-11-15）

| 項目 | 位置 | 測試 |
|---|---|---|
| D1 stale-on-error、single-flight、背景 refresh | `tdapi.list_dir` / `_fetch_listing` | `tests/test_listing_resilience.py` |
| D2 `list_deadline_seconds` | `config.py`、`tdapi._list_into_cache` | 同上 |
| D3 503 對應 | `bridge._unavailable`、`DAVProvider.get_resource_inst` | `tests/test_bridge_e2e.py`（`*_backend_fails*`） |
| D4 子項隔離 | `bridge._IsolatedMembers` | `tests/test_bridge_e2e.py`（`*cannot_be_built*`、`*resolves_to_nothing*`） |
| D5 pacer 監測 | `mountwatch.py`、`/rpc/status` 的 `mount`、`[bridge] auto_recover_mount` | `tests/test_mount_watch.py` |
| README 緩解 | README「`H:` 卡住，連 C:、D: …」 | — |

與原設計的差異：

- `LIST_CONNECT_TIMEOUT` 不是 5 秒而是等於 `LIST_DEADLINE`：2026-11-14 量到 TLS 交握
  超過 5 秒，connect 逾時設短會把正常但慢的連線誤判成失敗。
- 503 不帶 `Retry-After`（理由見 D3）。


## 現場驗證結果（2026-11-15，真的 H:，`base_url` 指向不可達的 `10.255.255.1`）

實測中多修了兩個問題：

1. **後端連不上時 bridge 根本起不來。** `TelegramAccountPool.start()` 把 `GET /accounts`
   （查哪些次要帳號已 linked）的失敗當成驗證失敗而退出，而且這個呼叫用預設 60 秒逾時、
   加連線重試，`restart.bat` 等不到。D1 的舊列表因此完全派不上用場。
   現在 `/accounts` 失敗只降級（主帳號照常、次要帳號先不接新上傳、背景每 60 秒重問），
   逾時改用 `(LIST_CONNECT_TIMEOUT, LIST_DEADLINE)`。登入失敗仍然退出。
2. **路徑每一層各等一次期限。** `root → pixiv → user-62133` 三層各等 8 秒 = 24 秒。
   現在任一列表因「連不上」（逾時、連線錯誤、5xx）失敗後 30 秒內，其他資料夾不再等：
   有副本就回副本，沒有就立即 503，背景照常重試。單一資料夾的 4xx 不算。

| 測項 | 結果 |
|---|---|
| bridge 啟動（後端不可達） | 約 10 秒起來，log 有降級 warning |
| 直接 PROPFIND 列過的資料夾 | 第一次 8.08 秒、之後 0.08 秒，207 舊內容 |
| 直接 PROPFIND 沒列過的資料夾 / 底下的檔案 | 503，0.004 秒（不再是 404） |
| `ls H:\pixiv\user-62133`（rclone dir cache 先 forget） | 2.1 秒，69 項 |
| `ls C:\` 同時進行 | 0.028 秒，不受影響 |
| `ls H:\` 沒列過的資料夾 | **I/O error，但要 60–72 秒** |
| 還原設定後 | `stale_listings` 歸零、`H:` 正常 |

**G3 實際效果不如預期。** rclone DEBUG log 顯示：bridge 每次 4ms 回 503，但 rclone 的
pacer 是**整個掛載共用的**，503 讓它把間隔拉到上限 2 秒，10 次 low-level retry ≈ 20 秒
一次 readdir；Windows 再重試 readdir 3 次 ≈ 60–70 秒。這段期間 `H:` 上**其他**請求也被
pacer 壓成每 2 秒一個。C: 不受影響（G1 對整個檔案總管成立），但 `H:` 本身會變慢。
**決定（2026-11-15）：沒有副本的資料夾，瀏覽時暫時回空列表。** `bridge.PlaceholderListings`：
只在 PROPFIND 時這樣答（COPY/DELETE 走訪子樹仍回 503，「暫時看起來空」不能變成破壞性操作的依據）；
回過空列表的資料夾記在記憶體，背景每 35 秒用 fresh 列表探一次，成功就呼叫 rclone
`vfs/forget dir=<path>`，否則 rclone 會把那份空列表留滿 `--dir-cache-time`（1 小時）。
實測同條件：`ls H:\pixiv\<沒列過>` 從 60–72 秒 I/O error 變成 **0.2 秒、空資料夾**。

待 forget 的清單同時存在 `meta/placeholders.json`，斷線期間重啟 bridge 也會接著處理
（探測改由 `Resolver.probe_listing` 依路徑重新解析，不依賴原本的 collection 物件）。

同一輪順手修的：`restart.bat` / `start.bat` 的等待改用 `PING.EXE -n 3`——`timeout.exe` 在
stdin 被導向時（`!`、背景執行）立即失敗，等待迴圈因此空轉，斷線時 bridge 起得慢就被誤報
「did not come up」；`config.ini` 的 `[bridge] auto_mount` 之前根本沒被讀取。
