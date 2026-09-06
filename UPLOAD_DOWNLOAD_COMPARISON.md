# TeleDrive 與 teledrive-webdav 上傳／下載機制比較

> 比較對象：`D:\python\teledrive` 與 `D:\python\teledrive-webdav` 目前工作目錄中的程式碼  
> 分析日期：2026-09-05  
> 範圍：檔案本體的上傳、註冊、去重、分段、下載、Range 串流、快取及失敗處理；不深入比較 UI 樣式、登入畫面或一般 CRUD。

## 1. 結論摘要

兩個專案不是兩套互不相干的儲存系統，而是同一套 TeleDrive 資料模型的兩種資料面（data plane）：

- `teledrive` 是 Web 介面。瀏覽器用 GramJS 直接連 Telegram，上傳與下載的檔案 bytes 不經過 TeleDrive FastAPI backend；backend 只保存 SQLite metadata。
- `teledrive-webdav` 是 Windows/WebDAV 介面。Windows 程式先經 `H:`、rclone、WsgiDAV 到本機 Python bridge，再由 Telethon 直連 Telegram；TeleDrive backend 同樣只收到 metadata，但檔案 bytes 會通過本機 Python bridge，且上傳一定先落到本機 staging 目錄。
- 兩邊的 fresh upload 都先以 **10 MiB** 區分 small-file protocol，再以每個 Telegram message 的精確容量 **500 MiB** 區分 single-message 與 multi-message split；因此不能把所有檔案概括成同一種上傳法。完整決策表見 4.1。
- 兩者刻意共用相同的去重 fingerprint、large/split upload 的 512 KiB part、每個 Telegram message 最多 1000 parts，以及 `split_group_id`／`part_index` 等 backend schema，因此單一帳號下的普通檔案大致互通。
- 最大的行為差異在於：Web 版偏向「即時、瀏覽器內處理」；WebDAV 版偏向「先持久化到本機、靜置後上傳、可被任意 Windows 程式 Range-read」。多帳號、平行、album 這三項原本也在差異清單裡，現在不是了（見下面兩條）。
- `/game` 是 WebDAV 專屬的特殊流程：一個第一層目錄會先打包成 `ZIP_STORED`，在 Telegram／backend 中是一個 `.zip`，掛載後再被虛擬展開成資料夾。Web 版上傳資料夾則建立真正的 backend folder tree，並逐檔上傳。
- ~~目前存在一個重要互通缺口：Web 版會把一般檔案及大檔 segments 分派到不同 Telegram linked accounts；WebDAV 下載端只使用單一 Telegram session⋯~~ **已修**（2026-09-06）：`Entry` 與 part table 保存 `telegram_user_id` 與 `file_id`，`telegram_accounts.TelegramAccountPool` 為每個設定的帳號各持一條 session，讀取一律以 `(telegram_user_id, message_id, file_id)` 精確路由，`file_id` 對不上就在發 `GetFile` 之前擋下來。`telegram_user_id = 0` 的舊 row 走 primary。上傳側同樣分派：一個 split 的每個 segment 各自租一個帳號。
- ~~去重完整性也有差異⋯~~ **已修**（2026-09-06）：`upload_engine.assert_parts_cover_file` 要求 part index 從 0 連續、沒有負數大小、加總**精確等於**檔案長度，去重挑候選時套同一條，所以歷史上只註冊了 part 0 的殘缺 split group 不會被重用。
- album 分組（`image/*`／`video/*`、≤ 10 MiB、排除 `image/webp`、每 10 個一批）與網頁端的自適應 chunk limiter 也都已移植，狀態逐帳號存在 `meta/upload-rate-<telegram_user_id>.json`。

## 2. 整體架構位置

### 2.1 `teledrive`（Web）

```text
上傳：Browser File
        -> React upload pipeline
        -> GramJS / MTProto
        -> Telegram Saved Messages
        -> POST /api/v1/files/register
        -> FastAPI + SQLite（metadata only）

下載：React 先取得列表／split metadata
        -> 選對 telegram_user_id 的 GramJS client
        -> Telegram GetFile/downloadMedia
        -> Blob、瀏覽器下載，或 Service Worker 206 Range response
```

關鍵依據：

- GramJS manager 明確負責 browser-based Telegram upload/download：`../teledrive/frontend/src/lib/gramjs.ts:194`。
- 上傳完成後才呼叫 metadata registration：`../teledrive/frontend/src/components/ChonkyDrive.tsx:880`。
- backend 的 `/files/register` 文件也明確表示「frontend 先直傳 Telegram，再註冊 metadata」：`../teledrive/backend/app/api/routes.py:268`。
- backend service 只把 metadata 寫入 SQLite：`../teledrive/backend/app/services/file_service.py:106`、`../teledrive/backend/app/services/file_service.py:180`。

### 2.2 `teledrive-webdav`（Windows/WebDAV）

```text
上傳：Windows application
        -> H:（rclone mount）
        -> WebDAV PUT
        -> WsgiDAV resource
        -> 本機 uploads/ 或 staging/
        -> debounce background worker
        -> Telethon / MTProto
        -> Telegram Saved Messages
        -> POST /api/v1/files/register

下載：Windows application
        -> H:（rclone VFS）
        -> WebDAV GET / Range
        -> SeekableRemoteFile
        -> Telethon GetFile over connection pool
        -> WebDAV response -> rclone cache -> application
```

關鍵依據：

- 遠端檔案由 `RemoteFileResource.get_content()` 開成 `SeekableRemoteFile`：`bridge.py:685`、`bridge.py:692`。
- 普通 PUT 先建立本機 staged file：`bridge.py:794`、`bridge.py:1016`。
- staged file 靜置後才進 `upload_and_register()`：`uploadstage.py:190`、`uploadstage.py:219`。
- bridge 啟動一個 Telegram worker、兩種 stager，再由 rclone 掛載：`bridge.py:1500`、`bridge.py:1510`、`start.bat:76`。

### 2.3 「檔案不經 backend」在兩者中的精確含義

共同不經過的是 `D:\python\teledrive\backend` 的 FastAPI/SQLite 服務；它只保存 `filename`、`filesize`、`message_id`、`file_id`、`access_hash`、split metadata 等欄位。

但兩條資料路徑仍不相同：

- Web：bytes 位於 browser memory／Blob，直接從瀏覽器送到 Telegram。
- WebDAV：bytes 會經過本機的 rclone + Python bridge，並在上傳前完整寫入本機 staging。故「不經 TeleDrive backend」成立，但「不經任何 Python process」不成立。

## 3. 共同的基礎協定與資料模型

| 項目 | 兩邊共同作法 | 程式碼位置 |
|---|---|---|
| Telegram 儲存位置 | 都把檔案存成目前帳號 Saved Messages 中的 document message | Web：`../teledrive/frontend/src/lib/gramjs.ts:471`、`../teledrive/frontend/src/lib/gramjs.ts:547`；WebDAV：`tgio.py:766` |
| large/split upload part 大小 | 都是 512 KiB；但 Web 的一般 `<=10 MiB` 單檔 `sendFile` 讓 GramJS 自選 part size，故「所有上傳一律 512 KiB」並不成立 | Web：`../teledrive/frontend/src/config.ts:23`、`../teledrive/frontend/src/lib/gramjs.ts:463`；WebDAV：`tgupload.py:43`、`tgio.py:733` |
| 每 message 最大 part 數 | 1000 | Web：`../teledrive/frontend/src/config.ts:26`；WebDAV：`tgupload.py:44` |
| 每 segment 的實際上限 | `512 KiB * 1000 = 524,288,000 bytes = 500 MiB` | Web：`../teledrive/frontend/src/lib/segmentPlan.ts:19`；WebDAV：`tgio.py:129` |
| 大檔邏輯模型 | 一個 logical file 拆成多個 Telegram messages；backend 每段一 row，共用 `split_group_id`，以 `part_index` 排序 | Web：`../teledrive/frontend/src/components/ChonkyDrive.tsx:882`；WebDAV：`gamestage.py:428` |
| 去重 fingerprint | SHA-256(first 100 MiB) + `:` + 原始 byte size | Web：`../teledrive/frontend/src/lib/hashFile.ts:1`；WebDAV：`gamestage.py:38` |
| metadata API | 上傳 Telegram 成功後 POST `/files/register`；split download 透過 backend 取回各 row | Backend：`../teledrive/backend/app/api/routes.py:240`、`../teledrive/backend/app/api/routes.py:268`、`../teledrive/backend/app/api/routes.py:580` |
| thumbnail 儲存概念 | thumbnail 嵌在檔案自己的 Telegram message，而非獨立檔案 message | Web：`../teledrive/frontend/src/lib/gramjs.ts:433`；WebDAV：`tgio.py:757`；schema：`../teledrive/backend/app/models/schemas.py:24` |
| Range 的 MTProto 限制 | 都處理 4096-byte alignment，單次 GetFile 最大使用 512 KiB | Web：`../teledrive/frontend/src/lib/gramjs.ts:1050`；WebDAV：`tgio.py:32` |
| 過期 file reference | 重新取得 Telegram message/media 後重試 | Web：`../teledrive/frontend/src/lib/gramjs.ts:1077`；WebDAV：`tgio.py:588` |

> 名稱注意：部分 README／註解把分段門檻簡稱為「512 MB」，但依常數計算是 524,288,000 bytes，也就是精確的 500 MiB（約 524.288 MB 十進位），不是 512 MiB。

## 4. 上傳流程詳細比較

### 4.1 先按檔案大小區分上傳方式

以下門檻只適用於 **fresh upload**。若 fingerprint 去重命中且既有 metadata 被接受，兩邊都可能完全略過 Telegram byte upload，直接註冊既有 message。

令 `S` 為真正送入共用上傳函式的檔案大小。WebDAV 普通檔案以 staged file 的大小判斷；`/game` 目錄則先完成 `ZIP_STORED` 打包，再以產生的 `.zip` 大小判斷：`gamestage.py:406`、`gamestage.py:414`。

- 小檔門檻：`10 * 1024 * 1024 = 10,485,760 bytes`。
- 單一 Telegram message 的 segment 上限：`1000 * 512 * 1024 = 524,288,000 bytes = 500 MiB`。

| 大小區間 | `teledrive` Web | `teledrive-webdav` | 最後的 backend 形態 |
|---|---|---|---|
| `S <= 10 MiB` | 一般檔走 GramJS `sendFile(CustomFile)`；符合條件的 image/video 則走 album 專用的 512 KiB `SaveFilePart` → `messages.UploadMedia` → 最多 10 個一組的 `messages.SendMultiMedia` | Telethon `client.upload_file(part_size_kb=512)` 的 small-file path，使用 `SaveFilePart` 並保留 MD5 verification，再 `send_file("me", ...)` | 每個檔案各有一個 Telegram message；album 只是把多個 message 分組送出。`is_split_file=false` |
| `10 MiB < S <= 500 MiB` | 規劃成一個 segment；以 512 KiB `SaveBigFilePart` 平行送 parts，再用 `InputFileBig` 建立一個 message | 規劃成一個 segment；以 512 KiB `SaveBigFilePart` 平行送 parts，再由 Telethon `send_file` 建立一個 message | 一個 Telegram message；仍是 `is_split_file=false`，不是 logical split file |
| `S > 500 MiB` | 拆成多個最多 500 MiB 的 segments；所有 segments 用 `Promise.all()` 平行上傳，可分散到不同 linked accounts；**即使最後一段只有 10 MiB 以下，仍走 `SaveBigFilePart`** | 拆成多個最多 500 MiB 的 segments，按順序逐段上傳；每段再個別判斷大小，`>10 MiB` 走 `SaveBigFilePart`，所以 **最後一段若 `<=10 MiB` 會改走 Telethon small-file path** | 每個 segment 一個 Telegram message；多 row 共用 `split_group_id`，`is_split_file=true` |

門檻的 inclusive/exclusive 關係是：

- 恰好 `10 MiB` 仍是小檔；`10 MiB + 1 byte` 才進大檔 protocol：Web 判斷見 `../teledrive/frontend/src/lib/splitUpload.ts:47`，WebDAV 判斷見 `tgio.py:733`。
- 恰好 `500 MiB` 只有一個 segment，仍註冊成非 split file；`500 MiB + 1 byte` 才產生兩個 Telegram messages：Web 規劃見 `../teledrive/frontend/src/lib/segmentPlan.ts:19`，WebDAV 規劃見 `tgio.py:198` 與 `gamestage.py:464`。
- 具體而言，`500 MiB + 1 byte` 在 Web 會產生兩個 `SaveBigFilePart` segments；WebDAV 則是第一段用 `SaveBigFilePart`，最後 1 byte segment 走 Telethon small-file upload。兩者 metadata 都會註冊成兩段，下載時仍能依 `part_index` 合併。

#### 4.1.1 小檔路徑其實也不完全相同

- Web 非 album 小檔會先將整個 `File` 讀成 `ArrayBuffer`／`Buffer`，交給 GramJS `sendFile`，其 upload part size 由 GramJS 內部決定：`../teledrive/frontend/src/lib/gramjs.ts:463`。
- Web album 小檔不是上述 `sendFile` 路徑；它明確切成 512 KiB `SaveFilePart`，先取得 `InputFile`、呼叫 `messages.UploadMedia`，最後才組 `SendMultiMedia`：`../teledrive/frontend/src/lib/gramjs.ts:572`、`../teledrive/frontend/src/lib/gramjs.ts:631`、`../teledrive/frontend/src/lib/gramjs.ts:715`。
- WebDAV 小檔從 staged file stream 讀取，由 Telethon `upload_file()` 處理，明確傳入 `part_size_kb=512`；它不做 album batching：`tgio.py:730`。

#### 4.1.2 大檔與超大檔的共同點及真正差異

- `10 MiB < S <= 500 MiB` 時，兩邊在 Telegram 層非常接近：都是一個 message、512 KiB `SaveBigFilePart`、預設最高 12 個 part requests in flight。
- `S > 500 MiB` 才發生 logical split。差異不在「有沒有切段」——兩邊都切——而在 Web 同時平行送所有 segments 且可跨帳號；WebDAV 只用一個帳號並循序送 segments。
- Web 是先看**整個檔案**是否超過 10 MiB，再決定所有 segments 都走 large path；WebDAV 是在 `_upload_segment()` 對**每個 segment**重新套用 10 MiB 判斷，因此超大檔的小尾段 protocol 會不同。

### 4.2 `teledrive` Web 上傳流程

#### A. 入口與排程

1. 使用者從 file picker、drag-and-drop 或 folder picker 選檔；一般檔案進 `startUploadBatch()`，資料夾進 `uploadFolder()`：`../teledrive/frontend/src/components/ChonkyDrive.tsx:923`、`../teledrive/frontend/src/components/ChonkyDrive.tsx:1069`、`../teledrive/frontend/src/components/ChonkyDrive.tsx:1120`。
2. 每個檔案先計算 first-100-MiB fingerprint，再查 backend dedup index。hash concurrency 為 2、hash-check concurrency 為 8：`../teledrive/frontend/src/lib/uploadPlanner.ts:7`、`../teledrive/frontend/src/config.ts:54`。
3. routing 是 streaming pipeline：第一個 fresh file 可在後面的檔案仍 hashing 時開始上傳，不存在整批前置 barrier：`../teledrive/frontend/src/components/ChonkyDrive.tsx:942`。
4. 同一批選取中，若多個檔案內容相同，第一個檔案真正上傳，其他檔案等待它公布 Telegram parts，之後只新增 metadata：`../teledrive/frontend/src/components/ChonkyDrive.tsx:951`。

#### B. 去重

1. `/files/check-hash` 可能回傳歷次 dedup 所建立的許多重複 rows。
2. `canonicalExistingParts()` 會先按 single file 或 `split_group_id`／`part_index` 壓成一套真正 parts。
3. 只有 parts 的 byte sum **精確等於本機 `file.size`** 才可重用；否則走 fresh upload，防止把中斷上傳留下的短檔繼續複製：`../teledrive/frontend/src/lib/uploadPlanner.ts:156`、`../teledrive/frontend/src/lib/uploadPlanner.ts:172`。
4. 重用時不再傳 bytes，只為新檔名／新 parent 建立一組 metadata rows；並保留原 part 的 `telegram_user_id`：`../teledrive/frontend/src/lib/uploadPlanner.ts:237`。

#### C. 小檔與 media album

- 檔案 `<= 10 MiB` 的基本路徑使用 GramJS `sendFile` + `CustomFile`，整個檔案先讀成 ArrayBuffer/Buffer：`../teledrive/frontend/src/lib/splitUpload.ts:47`、`../teledrive/frontend/src/lib/gramjs.ts:463`。
- `<= 10 MiB` 且符合 album 條件的 image/video 會走 producer/consumer album pipeline；每個檔案先 upload bytes + `messages.UploadMedia`，最多 10 個同帳號檔案用 `messages.SendMultiMedia` 一次送出：`../teledrive/frontend/src/components/ChonkyDrive.tsx:140`、`../teledrive/frontend/src/config.ts:78`。
- WebP 被明確排除 album，改走單檔 `sendFile`，因 Telegram album 對 WebP 的既有相容性問題：`../teledrive/frontend/src/components/ChonkyDrive.tsx:130`。
- `SendMultiMedia` 失敗／60 秒 timeout 時，會 fallback 成逐檔 `sendFile`：`../teledrive/frontend/src/lib/gramjs.ts:701`。

#### D. 大檔與分段

1. `10 MiB < S <= 500 MiB` 會規劃成一個 segment，用 big-file protocol 上傳，但完成後只有一個 Telegram message，`is_split_file=false`。
2. `S > 500 MiB` 才會規劃成多個 logical segments，metadata 才標成真正的 split file。`planSegments()` 依 512 KiB chunks、每 segment 最多 1000 chunks 規劃：`../teledrive/frontend/src/lib/segmentPlan.ts:19`。
3. 每個 segment 自己產生 Telegram upload `fileId`，其中的 chunks 用 `SaveBigFilePart` 平行送出；此方法不會因最後一段小於 10 MiB 而改用 small-file protocol：`../teledrive/frontend/src/lib/gramjs.ts:508`。
4. 預設每帳號最多 12 個 chunks 同時 in-flight；每帳號另有最多 3 個 file slots：`../teledrive/frontend/src/config.ts:11`、`../teledrive/frontend/src/config.ts:21`。
5. `uploadFileSpread()` 對所有 segments 使用 `Promise.all()`；每段透過 round-robin／available-slot 選擇 linked account。因此同一個超大檔的不同 segments 可同時存到不同 Telegram 帳號：`../teledrive/frontend/src/lib/splitUpload.ts:31`、`../teledrive/frontend/src/lib/accountPool.ts:28`。同一個 account dispatcher 也包住 `<=10 MiB` 的普通單檔路徑，所以 secondary-account 相容性問題不只限於 split file：`../teledrive/frontend/src/lib/splitUpload.ts:44`。
6. 完成後按 `segment.index` 排序，不能按 `message_id` 排序，因為 message ID 只在各自帳號內單調增加：`../teledrive/frontend/src/lib/splitUpload.ts:62`。
7. 註冊前用 `assertPartsCoverFile()` 確認所有 segment 大小的總和等於原檔大小：`../teledrive/frontend/src/lib/uploadPlanner.ts:222`。

#### E. thumbnail

- Web 版會從本機 image/video capture thumbnail；可解碼的 media 若取不到 thumbnail，通常讓該檔上傳失敗，而不是默默註冊成沒有縮圖：`../teledrive/frontend/src/components/ChonkyDrive.tsx:826`。
- 大檔只把 thumbnail 放在 segment 0：`../teledrive/frontend/src/lib/splitUpload.ts:62`。
- 無法由瀏覽器 codec 解碼的影片可例外上傳成沒有 thumbnail。

#### F. rate limit 與失敗處理

- chunk RPC 由每帳號一個 `AdaptiveRateLimiter` 控制；初始 4 parts/s、最低 0.5、最高 12，遇一般 FLOOD_WAIT 乘法降速，乾淨期再加法提升：`../teledrive/frontend/src/lib/gramjs.ts:73`、`../teledrive/frontend/src/config.ts:97`。
- rate／learned ceiling 依帳號存到 localStorage；具 ceiling slow-zone、probe、escalation 等邏輯：`../teledrive/frontend/src/lib/gramjs.ts:88`、`../teledrive/frontend/src/lib/adaptiveRateLimiter.ts:84`。
- FLOOD_PREMIUM_WAIT 只 pause、不拿來降低一般 rate ceiling：`../teledrive/frontend/src/lib/gramjs.ts:60`、`../teledrive/frontend/src/lib/gramjs.ts:293`。
- 每 chunk 最多 3 次外層 retry，非 flood transient failure 使用 1s、2s、4s... backoff：`../teledrive/frontend/src/config.ts:29`、`../teledrive/frontend/src/lib/gramjs.ts:526`。
- message-creating RPC 另用 3 messages/s、burst 6 的 limiter，與 chunk bucket 分離：`../teledrive/frontend/src/config.ts:39`。
- 失敗狀態顯示在 UI，但沒有 WebDAV 式的 durable staging queue。若 bytes 已到 Telegram、但最後 send/register 失敗，可能留下 backend 看不到的 orphan upload／message。

### 4.3 `teledrive-webdav` 上傳流程

#### A. WebDAV PUT 一定先落本機

一般路徑：

1. rclone 把 Windows write 轉成 WebDAV PUT。
2. `RootCollection.create_empty_resource()` 在 `upload_dir/<完整目的路徑>` 建檔：`bridge.py:794`。
3. `UploadFileResource.begin_write()` 用 `wb` 寫本機檔，`end_write()` 更新 debounce 時間：`bridge.py:1070`。
4. 同一檔持續寫入會重設 timer；靜置 `debounce_minutes` 後 background worker 才呼叫 `upload_and_register()`：`uploadstage.py:121`、`uploadstage.py:199`。
5. 成功才刪本機 staged copy；失敗保留 staged file：`uploadstage.py:219`。

這表示 WebDAV PUT 對 Windows 程式可很快完成，Telegram upload 是後續非同步工作；代價是必須準備足以容納完整待傳檔案的本機空間。實際雲端進度可查預設位址 `GET http://127.0.0.1:8081/rpc/status`：回應中的頂層 `units` 是 `/game` queue，`uploads.pending` 是普通路徑 queue；完整紀錄預設在 `<cache_dir>/bridge.log`：`bridge.py:1215`、`bridge.py:1257`、`bridge.py:1478`。

#### B. `/game` 特殊打包流程

- `/game/<top-level-file>`：原檔直接上傳，不包 zip：`gamestage.py:358`。
- `/game/<top-level-directory>/...`：整棵 subtree 靜置後，以 `ZIP_STORED` + Zip64 打成 `<top>.zip`：`gamestage.py:364`。
- upload/register 成功後移除 staging tree 與暫存 pack；upload retry 可重用已經建立的 zip，避免重打數十 GiB：`gamestage.py:315`、`gamestage.py:365`。
- 下載／瀏覽時，backend/Telegram 仍只知道一個 `.zip`；WebDAV 用 `ZipView` 讀 central directory，把它呈現成虛擬目錄。`ZIP_STORED` entry 可直接映射成 archive 中的一段 byte range：`zipfs.py:258`、`zipfs.py:315`。

#### C. 去重

- 使用與 Web 完全相同的 first-100-MiB + size fingerprint：`gamestage.py:57`。
- 查 `/files/check-hash`，若命中則不重傳 Telegram bytes，直接以既有 message IDs 註冊新 rows：`gamestage.py:406`。
- 也會把歷史重複 rows 壓成 canonical part set：`gamestage.py:72`。
- **差異／風險**：WebDAV 的 `canonical_existing_parts()` 沒有接收本機原檔大小，也沒有驗證既有 parts 的 byte sum 是否完整；相較之下 Web 版把 size coverage 當成重用的必要條件。因此 WebDAV 可能重用過去中斷上傳留下的不完整 split set。

#### D. 小檔、單訊息大檔與多訊息超大檔

- `<= 10 MiB`：使用 Telethon 內建 `client.upload_file()`，保留其小檔 MD5-verified path：`tgio.py:730`。
- `> 10 MiB` 且 `<= 500 MiB`：只有一個 segment；切成 512 KiB parts，直接平行送 `SaveBigFilePart`，但 metadata 仍是非 split file：`tgupload.py:319`、`gamestage.py:439`。
- `> 500 MiB`：logical file 切成多個上限 500 MiB 的 segments；segments 在 `_upload_segments()` 中依序上傳，不像 Web 版可同時把 segments fan out 到多帳號：`gamestage.py:461`。
- 10 MiB 門檻是在每次 `_upload_segment()` 呼叫內判斷，不是只判斷 logical file 一次；所以超大檔最後一段若 `<=10 MiB`，該尾段使用 `client.upload_file()`，其餘較大 segments 才使用平行 `SaveBigFilePart`：`tgio.py:730`、`gamestage.py:470`。
- 每個 segment 上傳成功後用 `send_file("me", ..., force_document=True)` 建立 message，再取得實際 Telegram document ID/access hash：`tgio.py:757`。
- 所有 segments 都完成後，才逐 row 呼叫 backend register；register 是循序而不是 `Promise.all()`：`gamestage.py:428`。

#### E. concurrency 與帳號

- bridge 設定中只有一組 `api_id`、`api_hash`、Telethon StringSession；啟動時建立單一 `TelegramWorker`：`config.example.ini:7`、`bridge.py:1500`。
- download 預設另開 8 個同 session connections；upload 另有一條 dedicated connection，避免 upload payload 阻塞 download：`tgio.py:287`、`tgio.py:316`。
- 大 segment 預設最多 12 個 parts in-flight：`config.example.ini:22`。
- `GameStager` 與 `UploadStager` 各有 background thread，但各自的 queue 是逐 unit 處理；它們共用同一 Telegram worker/upload gate。沒有 Web 版的多 linked-account dispatch。
- register payload 沒傳 `telegram_user_id`；backend 會把 storage account 預設成目前 authenticated user：`tdapi.py:623`、`../teledrive/backend/app/api/routes.py:277`。

#### F. thumbnail

- 只有「單一 segment 的 still image」建立 JPEG preview；split image、video、`/game` zip 都不產生 upload thumbnail：`gamestage.py:464`。
- preview 限 320x320、20 KiB，並附 `DocumentAttributeImageSize`：`tgio.py:66`、`tgio.py:757`。
- 因此 WebDAV 上傳的影片通常不像 Web 上傳影片一樣帶 embedded thumbnail；WebDAV 的 shell 預覽能力仍可能透過讀取 media／檔案本體或 warmup 機制補足，但不是同一種 upload-time thumbnail 策略。

#### G. rate limit、retry 與 crash recovery

- WebDAV 的 `UploadGate` 一開始不設 parts/s cap，只用最大 window；首次 flood 後，才依當下 window/RTT 估算 rate 並同時縮小 window：`tgupload.py:83`。
- rate floor 是 0.25 parts/s；沒有 Web 版的 persisted ceiling、slow-zone、probe、premium-flood 特判或 escalation state：`tgupload.py:57`。
- 單一 part 有 3 次 outer retry；可容忍最多 10 次 flood retry、30 次 disconnect retry，upload flood wait 上限 600 秒：`tgupload.py:47`、`tgupload.py:227`。
- segment 另有最多 2 次快速重試，每次等 30 秒：`gamestage.py:47`、`gamestage.py:514`。
- 整個 staged unit 最多嘗試 5 次，失敗間隔 600 秒；最終 abandoned 仍保留本機 staging：`gamestage.py:43`、`uploadstage.py:233`。
- process 重啟會重新 adopt staging leftovers：`uploadstage.py:137`。
- 流程不是 transaction：如果前幾個 Telegram segments 已成功、後段最終失敗，整個 unit retry 仍可能重傳先前成功的 segments；未註冊的舊 messages 會成為 orphan。快速 segment retry 只是降低這個機率，沒有 resumable upload manifest。

### 4.4 上傳差異總表

| 面向 | `teledrive` Web | `teledrive-webdav` |
|---|---|---|
| 使用者入口 | 瀏覽器 picker／drag-drop／folder picker | 任意 Windows 程式對 `H:` 寫檔，經 rclone/WebDAV PUT |
| bytes 執行端 | Browser + GramJS | 本機 Python bridge + Telethon |
| TeleDrive backend 是否接收 bytes | 否 | 否 |
| upload 前是否完整落地 | 不一定；大檔按 File slices 讀，小檔／album 常整檔讀入 memory | 是；先完整寫到 `uploads/` 或 `staging/` |
| `<=10 MiB` protocol | 非 album：GramJS `sendFile`；album：明確的 512 KiB `SaveFilePart` + `UploadMedia` + `SendMultiMedia` | Telethon `upload_file` small path，512 KiB `SaveFilePart` + MD5；無 album |
| `10 MiB < S <= 500 MiB` | 一個 `SaveBigFilePart` segment／一個 message／非 split | 一個 `SaveBigFilePart` segment／一個 message／非 split |
| `>500 MiB` | 多 segments 平行、可跨 linked accounts；小尾段仍走 big protocol | 多 segments 循序、單帳號；尾段 `<=10 MiB` 時改走 small protocol |
| 啟動時機 | 選檔後立即 pipeline | 寫入靜止達 debounce 後 |
| crash 後續傳工作 | 無 durable queue | 可 adopt staged leftovers，但不是 Telegram part-level resume |
| 資料夾 | backend folder tree + 逐檔上傳 | 普通路徑同樣建 backend folders；`/game` subtree 則整包成一個 ZIP |
| 小 media 最佳化 | 可將最多 10 個同帳號檔案組 Telegram album | 無 album batching |
| 大檔 segment | 平行，可跨 linked accounts | 循序、單帳號 |
| 每帳號 file concurrency | 3 | 每個 stager queue 基本為 1 unit；兩個 stager 可同時活動 |
| chunk concurrency | 12/account | 12/shared upload gate（預設） |
| rate control | 持久化的 per-account adaptive limiter | process-local、首次 flood 後才啟用 rate cap |
| dedup | backend dedup + batch-local dedup + exact size coverage | backend dedup + canonical rows；沒有 exact size coverage gate |
| thumbnail | image/video 為主，segment 0；media capture failure 通常阻止註冊 | 僅 single-segment still image；video/split/zip 無 upload preview |
| register | parts 多半平行 POST，先驗證 coverage | parts 循序 POST，無 coverage assertion |
| 完成語意 | UI 顯示 Telegram + registration 結果 | WebDAV PUT 完成只代表 staged；真正完成要看 stager status/log |

## 5. 下載流程詳細比較

### 5.1 `teledrive` Web 完整下載

#### A. 非 split file

1. 使用列表中的 `FileInfo`，依 `telegram_user_id` 選擇能讀該 message 的 GramJS client：`../teledrive/frontend/src/lib/download.ts:6`。
2. 以 `message_id` 從該帳號 Saved Messages 取得 media。
3. 小於 10 MiB 的影片特例用 GramJS `downloadMedia()`；其他檔案優先走 512 KiB `upload.GetFile` 平行下載：`../teledrive/frontend/src/lib/gramjs.ts:873`。
4. full download 以 concurrency 6 抓 chunks；`ChunkAssembly` 要求每個 slot 都存在且 byte sum 等於 Telegram 宣告大小：`../teledrive/frontend/src/lib/gramjs.ts:951`、`../teledrive/frontend/src/lib/chunkAssembly.ts:17`。
5. 120 秒沒有任何 chunk 完成視為 stalled；chunked path 失敗才 fallback `downloadMedia()`，fallback 仍驗證長度：`../teledrive/frontend/src/lib/gramjs.ts:969`。
6. 最終組成 Blob，以 temporary object URL 觸發瀏覽器存檔：`../teledrive/frontend/src/lib/download.ts:106`。

#### B. split file 完整下載

1. 向 backend 查整個 `split_group_id`。
2. 以 `part_index` 排序，並移除重複的 `telegram_message_id` rows：`../teledrive/frontend/src/lib/download.ts:53`。
3. 最多 3 個 parts 同時下載；每個 part 依自己的 `telegram_user_id` 選 GramJS client：`../teledrive/frontend/src/lib/download.ts:89`。
4. 每個 part 各自做完整性驗證，再按順序建立 merged Blob；最後再驗證 merged size。

因此完整下載是「先把 logical file 的所有 bytes 組成 Blob，再交給 browser save」。它不是 streamed-to-disk download；極大檔案雖使用 Blob storage、可由瀏覽器 spill to disk，但仍需要瀏覽器持有完整 Blob 的生命週期。

#### C. 一般 preview

- image、audio、PDF 等先走 `fetchFileBlob()` 抓完整檔，再建立 Blob URL；text 超過 1 MiB 不顯示內容：`../teledrive/frontend/src/components/ChonkyDrive.tsx:1468`。
- image preview Blob URL 只保留最近 5 個，避免無限累積：`../teledrive/frontend/src/components/ChonkyDrive.tsx:1487`。

#### D. video Range streaming

影片不先抓完整 Blob，而是使用虛擬 URL：

- non-split：`/preview-video/{fileId}/{messageId}/{accountId}`。
- split：`/preview-video/split/{splitGroupId}`。

Service Worker 攔截 `<video>` 的 Range request，經 `postMessage` 請主頁面的 GramJS client 抓 Telegram chunk，再回覆 HTTP 206：`../teledrive/frontend/src/service-worker/index.ts:358`、`../teledrive/frontend/src/main.tsx:242`。

細節：

- 每個 response 最大 512 KiB，處理 4 KiB alignment。
- rolling `PreloadBuffer` 預抓後面 3 chunks（約 1.5 MiB），同一 offset 的 in-flight promise 只共用一次，不重複 GetFile：`../teledrive/frontend/src/service-worker/index.ts:12`、`../teledrive/frontend/src/lib/preloadBuffer.ts:24`。
- 最多保留 4 個 message/part buffers：`../teledrive/frontend/src/service-worker/index.ts:18`。
- 每次 SW chunk request timeout 30 秒，最多 3 次、exponential backoff：`../teledrive/frontend/src/service-worker/index.ts:219`。
- split video 先把 logical offset 映射到 part-local offset，並選該 part 的帳號；單次 response 不跨 segment boundary：`../teledrive/frontend/src/service-worker/index.ts:396`。
- streaming 時每 15 秒 keepalive，關閉 preview 後 `StreamGate` 拒絕剩餘 preload：`../teledrive/frontend/src/main.tsx:7`、`../teledrive/frontend/src/lib/streamGate.ts:17`。

### 5.2 `teledrive-webdav` 下載流程

#### A. WebDAV GET／Range 到 logical file

1. Windows 程式讀 `H:`；rclone 先查自己的 VFS cache，miss 時發 WebDAV GET／Range。
2. WsgiDAV resource 宣告 `support_ranges() = True`，`get_content()` 回傳 fresh `SeekableRemoteFile`：`bridge.py:633`、`bridge.py:685`。
3. `TeleDriveClient.parts_for()` 對 single file 產生一段 `(message_id, size)`；split file 查 `/files/by-split-group/{id}`，按 `part_index` 排序、移除重複 message IDs：`tdapi.py:533`。
4. `SeekableRemoteFile` 建 logical part table，讓 caller 看見一個連續、可 seek 的 file；read 跨 segment boundary 時用 `map_range()` 自動拆成多個 part-local reads：`tgio.py:1075`、`tgio.py:1235`。
5. 每個 Telegram read 對齊到 4 KiB，切成 512 KiB GetFile requests，透過預設 8 條同 session connections round-robin + gather；每條連線可同時承擔多個 MTProto requests：`tgio.py:601`。
6. 遠端 file reference 過期時重新 `get_messages("me")` 後 retry；可容忍的 read FLOOD_WAIT 最多 retry 2 次：`tgio.py:588`、`tgio.py:685`。

#### B. 多層快取

- 每個 `SeekableRemoteFile` 有 512 KiB block LRU，預設保留 16 blocks（約 8 MiB）：`tgio.py:84`、`tgio.py:107`。
- 檔案開頭可由 bridge 的 on-disk head cache 覆蓋，減少 Explorer 掃描檔頭造成的 Telegram round trips：`tgio.py:1096`、`tgio.py:1168`。
- rclone 使用 `--vfs-cache-mode full`、最大 160 GiB、極長 age，且 sequential read chunk 可由 32 MiB grow 到 512 MiB：`start.bat:76`。
- ZIP central directory／tree、split table、media props、thumbnail 另有 metadata caches。

所以 WebDAV 的下載語意不是「按下載後一次組成完整檔」，而是提供一個長期可 seek 的遠端檔案。應用程式要讀多少就向下傳多少；rclone VFS 可把已讀內容長期留在本機。

#### C. `/game` 虛擬 ZIP 讀取

- 掛載端看到的是資料夾，但 Telegram 上是 `.zip`。
- `ZIP_STORED` entry 直接用 `SlicedReader` 映射 archive byte range，讀某個遊戲檔不需下載整個 zip：`zipfs.py:315`。
- 若遇到非 stored 的一般 compressed zip entry，為支援 seek 可能從 entry 開頭重新解壓到目標位置，random access 成本較高：`zipfs.py:183`、`zipfs.py:320`。

#### D. size 防護

WebDAV 額外解析 `file_hash` 的 `:<size>` 作為真實原檔長度，避免歷史 metadata 把最後一個 512 KiB part 的 padding 算入檔案長度；part table 只裁最後一段：`tdapi.py:71`、`tdapi.py:94`。

這與 Web 版的防護位置不同：Web 版 full download 完成後直接比較 Blob 與 metadata/Telegram 宣告大小；WebDAV 在建立 logical file table 時先 clip advertised length。

### 5.3 下載差異總表

| 面向 | `teledrive` Web | `teledrive-webdav` |
|---|---|---|
| 使用者語意 | 按下載、preview、video playback | 任意程式 open/read/seek `H:` 檔案 |
| bytes 執行端 | Browser GramJS | Python Telethon bridge，再經 WebDAV/rclone |
| full download | 所有 chunks/parts 組成完整 Blob 才 save | streaming file object；caller 可只讀部分 |
| split 組裝 | part-level concurrency 3，完成後 Blob concatenate | `SeekableRemoteFile` 即時把 logical range 映射到各 part |
| Telegram read concurrency | full file concurrency 6；split parts concurrency 3 | 8 connections，寬 read 可達 16 個 512 KiB requests in flight |
| multi-account | 依每 part `telegram_user_id` 選 client | 單一 configured session；part table 不含 account ID |
| video | Service Worker 提供 HTTP 206，預抓 3 chunks | 所有 file type 都由同一 WebDAV Range 機制處理；rclone 提供更大的 VFS readahead/cache |
| preview 非影片 | 常先下載完整檔 | Explorer 可取 embedded thumbnail、head、props；不足時才讀更多檔案 bytes |
| disk cache | 沒有一般檔案的持久 CacheStorage；Blob/少量 preview URL 與 rolling preload | bridge metadata/head cache + rclone full VFS cache（預設最多 160 GiB） |
| 完整性檢查 | chunk slots、Telegram size、每 part metadata size、merged size | logical part table、duplicate message collapse、`file_hash` real-size clipping；沒有 end-of-full-download assertion，因為不一定 full-read |
| file reference refresh | refresh GramJS media ref | refresh Telethon message/media cache |
| 專屬能力 | 多帳號、瀏覽器 video SW streaming | OS-wide filesystem、任意 Range/seek、`/game` ZIP 虛擬展開 |

## 6. 最重要的相同點

### 6.1 都把 backend 當 metadata/control plane

兩邊都不把檔案本體 POST 到 FastAPI。backend 負責驗證 owner、linked account、parent folder，並保存 Telegram coordinates。`/files/register` 對跨 owner 的 `file_id` 有 collision guard，也驗證指定 `telegram_user_id` 確實 linked：`../teledrive/backend/app/api/routes.py:277`。

### 6.2 split schema 完全對齊

兩邊都以同一原檔名、不同 Telegram message rows 表示 split file，並依 `part_index` 還原，不依賴 message ID 排序。這是 Web upload 能被 WebDAV logical reader 理論上讀取、WebDAV upload 能被 Web merged downloader 讀取的基礎。

### 6.3 Telegram protocol 限制的處理一致

兩邊的 large/split upload 都固定 512 KiB parts、每 message 1000 parts，download GetFile 也以 4 KiB alignment／512 KiB request 為核心。Web 的一般 `<=10 MiB` `sendFile` 是例外：其底層 part size 交由 GramJS 決定。互通真正依賴的是 Telegram message、精確 segment size 與 backend split metadata，不要求小檔曾以同一 upload part size 傳入。

### 6.4 去重 fingerprint 一致

Web 與 WebDAV 上傳相同內容時會命中同一 backend hash index，可直接建立 metadata alias、不重傳 Telegram bytes。

但這個 fingerprint 不是完整檔 SHA-256：只 hash 前 100 MiB，再附檔案長度。兩個不同檔案若 size 相同、前 100 MiB 相同、後段不同，會碰撞並被視為相同。這是兩邊共同的性能／正確性取捨，不是 cryptographic full-content identity。

## 7. 最重要的不同點與影響

### 7.1 即時 pipeline vs durable staging

- Web 優勢：選檔後馬上開始，沒有額外一份完整 disk copy，大檔可按 slice 讀取。
- Web 代價：tab crash／reload 後沒有 queue；已傳 chunks 的狀態不會恢復。
- WebDAV 優勢：PUT 完整內容先在本機，bridge crash 後可重新 adopt，且寫檔程式與 Telegram latency 解耦。
- WebDAV 代價：需要完整 staging 容量；PUT 成功不是雲端已完成，使用者必須另看 status/log。

### 7.2 真資料夾 vs `/game` archive-as-folder

Web 上傳資料夾後，單一檔案可獨立 move/rename/trash，backend 也看得到每個 child。WebDAV `/game` pack 讓大量小檔變成一個 Telegram logical file，降低 message/metadata 數量，而且 `ZIP_STORED` 仍可 Range-read；但 archive 內部 child 不是 backend entity，上傳完成後在 WebDAV 端是 read-only，修改單一 child 不能只更新該 child。

### 7.3 多帳號 throughput vs 單帳號可預測性

Web 會把普通檔案分散到不同帳號，對大檔則進一步以 segment-level parallelism 同時利用多個帳號及其獨立 limiter。WebDAV 只有一組 session，調優集中在一個帳號內的 part concurrency 與多條 download connections。Web 理論 aggregate throughput 更高；WebDAV 狀態較單純，卻無法原生消費存於其他 linked account 的 file/part。

### 7.4 下載目標不同

Web full download 的目標是產生一個完整、可保存的 Blob，所以可以在結束點做嚴格 size assertion。WebDAV 的目標是實作 filesystem file object，讀者可能永遠只取 64 KiB 或 seek 到中間，因此重點是 range mapping、block cache 和 EOF/advertised-size 正確性。

### 7.5 cache 層級不同

Web video 僅做短 rolling preload，避免 latency，但不以長期保存為目標。WebDAV 由 rclone full VFS cache 提供 persistent local reuse，適合 OS 程式反覆開檔；代價是預設可消耗大量本機空間，而且 cache consistency 另受 rclone dir-cache 與 bridge metadata cache 影響。

## 8. 現況相容性與正確性風險

### P0：WebDAV 無法可靠下載存於另一 linked account 的 Web 檔案

證據鏈：

1. Web upload 的 account dispatcher 同時用於普通小檔及大檔 segments；大檔更會把 sibling segments 平行分派到不同帳號：`../teledrive/frontend/src/lib/splitUpload.ts:44`、`../teledrive/frontend/src/lib/splitUpload.ts:62`。
2. backend row 有 `telegram_user_id`，Web download 會依它選 client：`../teledrive/backend/app/models/schemas.py:34`、`../teledrive/frontend/src/lib/download.ts:14`。
3. WebDAV `Entry`／`_to_entry()` 沒有 `telegram_user_id`：`tdapi.py:55`、`tdapi.py:128`。
4. WebDAV split table 只保存 `(message_id, size)`：`tdapi.py:533`。
5. Telethon read 固定對單一 session 的 `get_messages("me", ids=[message_id])`：`tgio.py:412`。

影響：secondary account 中的 file/part 在 configured account Saved Messages 並不存在；更危險的是不同帳號可能剛好有相同 message ID，現行 WebDAV 沒有用 expected Telegram document ID 驗證，理論上可能讀到錯誤 media，而不只是 404。這會影響完整存於 secondary account 的普通檔，也會影響只有部分 segments 存在 secondary accounts 的 split file。

建議方向：把 `telegram_user_id` 與預期 `file_id` 帶入 `Entry`／part table；`TelegramWorker` 改成 account-indexed worker/client pool，對每個 file/part 選正確 session，並在 message lookup 後核對 document ID。若短期不實作，應把所有需要由 WebDAV 讀取的 Web uploads 固定到與 bridge 相同的帳號，而不只是關閉跨帳號 segment spreading。

### P1：WebDAV dedup 缺少完整 parts coverage gate

Web 版 `canonicalExistingParts(files, originalSize)` 只回傳合計大小剛好等於原檔的 candidate；WebDAV `canonical_existing_parts(rows)` 沒有 `originalSize`，命中 truncated historical group 仍會直接註冊。

建議方向：讓 WebDAV helper 接收 `size`，比照 Web 的 `covers()`；同時加入 incomplete single、incomplete split、多 candidate 中挑完整 group 的測試。

### P1：兩邊都不是 Telegram upload + backend registration transaction

若 Telegram send 成功但 backend register 失敗，Telegram 會有 orphan message；若 split file 只完成部分 segments，部分 chunks/messages 也可能 orphan。WebDAV durable staging 能重跑原檔，但不會辨識或續用未註冊的已完成 segments；Web 版也沒有 server-side reconciliation manifest。

建議方向：若 orphan 數量成為實際問題，可新增「client-generated upload transaction ID + provisional part metadata + final commit」；仍只傳 metadata，不違反 backend 不收 binary 的原則。

### P2：fingerprint 是 sampled hash，不是完整內容 hash

這是有意的效能取捨，但文件或 UI 若稱它為「SHA-256 of file」會讓使用者誤以為是完整內容驗證。較精確名稱應是 sampled fingerprint。若資料正確性優先，可在背景計算 full hash，或至少在 dedup reuse 時加入尾端 sample。

### P2：`/files/{id}/download` 現在不是主要下載入口

backend 與兩個 client 都保留 download-info helper，但目前實際流程直接使用 directory listing／split listing 中的 metadata；repository search 沒有找到 `getDownloadInfo()` 或 `download_info()` 的呼叫者。它是可用 API，但不是現行 data path。維護文件時不應把它畫成每次下載必經步驟。

### P2：大小單位命名容易造成邊界誤解

實作是 1000 × 512 KiB = 500 MiB。建議所有 README、註解與 UI 統一寫「500 MiB per Telegram message（524,288,000 bytes）」；避免寫成可能被理解為 512 MiB 的「512 MB」。

## 9. 實際互通矩陣

| 檔案來源／型態 | Web 下載 | WebDAV 下載 | 備註 |
|---|---:|---:|---|
| WebDAV 普通單檔、single account | 可 | 可 | 共用 register schema；Web 依 backend 預設 account 下載 |
| WebDAV split file、single account | 可 | 可 | Web 會依 part_index merge |
| WebDAV `/game/<dir>` pack | 可下載為 `.zip` | 可展開成虛擬資料夾 | Web backend 只看見 `<dir>.zip` |
| Web 單檔、primary account | 可 | 可 | 一般互通情境 |
| Web 單檔、secondary linked account | 可 | **目前不可靠** | WebDAV reader 不依 `telegram_user_id` 切換 session |
| Web split、全部 parts 在 primary account | 可 | 可 | WebDAV 仍只靠 message ID，不核對 document ID |
| Web split、parts 跨 linked accounts | 可 | **目前不可靠** | WebDAV 丟掉 account routing metadata |
| Web imported chat photo/document | 可，GramJS media abstraction支援 | 多數可，Telethon reader接受 photo/document | 實際可讀性仍取決於該 configured account 是否看得到來源／轉存 message |

## 10. 效能特性判讀

### 上傳

- 許多 `<=10 MiB` media：Web 的 album batching、每帳號 file slots、多帳號分流通常更有優勢。
- 單一超大檔：Web 能同時送多個 segments、跨多帳號；WebDAV 每 segment 內可 12-way part parallel，但 segments 循序。
- 大量小遊戲檔：WebDAV `/game` 先包成 ZIP，能顯著減少 Telegram message 數與 backend rows；Web 則保留細粒度可管理性。
- 不穩定環境／應用程式關閉：WebDAV staging 更耐 crash；Web tab 被關掉時工作中止。

### 下載

- 一次完整另存：Web 的 6-way chunk download + Blob assembly 路徑直接，並有嚴格完整性 assertion。
- 播放影片：Web 用 SW 只抓 Range 並預載 1.5 MiB；WebDAV 由 media player -> filesystem Range -> rclone readahead，策略更通用但 cache/預讀可能較大。
- 反覆由多個桌面程式讀同一檔：WebDAV 的 rclone VFS persistent cache 明顯更適合。
- 極大 split 完整下載：Web 需要完成所有 part Blobs 後再組 merged Blob；WebDAV 可一路串流到 caller／rclone disk cache，對 browser memory lifecycle 的依賴較低。

## 11. 若要讓兩邊行為更一致，建議順序

1. **先補 WebDAV multi-account read routing**：這是資料可讀性的 P0 問題，也是 Web 版 multi-account upload 真正互通的必要條件。
2. **把 Web 的 exact coverage 檢查移植到 WebDAV dedup**：改動範圍小、可直接用既有 Web 行為當規格。
3. **統一 segment size 用語與共用測試向量**：至少測 10 MiB 邊界、500 MiB 邊界、500 MiB + 1、尾段非 512 KiB 整數倍。
4. **建立 upload transaction/reconciliation metadata**：用來辨識 Telegram orphan、partial registration 與可恢復 segment。
5. **明確定義 thumbnail parity**：決定 WebDAV 是否也為 video／split media 產縮圖；現在兩邊對 `has_thumbnail` 的使用者體驗不同。
6. **盤點並決定 `/download` endpoint 去留**：若保留，讓兩個 client 真正以它作授權後的 coordinate lookup；若不保留，調整 README，避免誤導 call flow。

## 12. 理解此功能最必要的檔案

### `teledrive`

- `../teledrive/frontend/src/components/ChonkyDrive.tsx`：上傳入口、資料夾 traversal、dedup routing、album pipeline、preview/download UI。
- `../teledrive/frontend/src/lib/gramjs.ts`：真正的 MTProto upload/download、chunk、message send、thumbnail 與 flood handling。
- `../teledrive/frontend/src/lib/splitUpload.ts`：large-file segments 與 multi-account spreading。
- `../teledrive/frontend/src/lib/segmentPlan.ts`：500 MiB message boundary 的純計算。
- `../teledrive/frontend/src/lib/uploadPlanner.ts`：hash concurrency、canonical dedup、coverage assertions、duplicate registration。
- `../teledrive/frontend/src/lib/download.ts`：full/split Blob download 與 merge。
- `../teledrive/frontend/src/lib/chunkAssembly.ts`：full download 完整性條件。
- `../teledrive/frontend/src/service-worker/index.ts`、`../teledrive/frontend/src/main.tsx`：video Range streaming 與 GramJS bridge。
- `../teledrive/backend/app/api/routes.py`、`../teledrive/backend/app/services/file_service.py`：共用 register/download metadata control plane。

### `teledrive-webdav`

- `bridge.py`：WebDAV resource、PUT/GET/Range 入口、staging resource 與 resolver。
- `uploadstage.py`：普通路徑 file-by-file debounce queue。
- `gamestage.py`：`/game` pack、hash dedup、segment upload、registration。
- `tgupload.py`：平行 `SaveBigFilePart` 與 WebDAV 專用 UploadGate。
- `tgio.py`：Telethon client pool、upload commit、GetFile ranges、`SeekableRemoteFile`。
- `tdapi.py`：backend metadata client、split part table、real-size clipping。
- `zipfs.py`：remote ZIP central directory 與 archive-entry Range view。
- `start.bat`：rclone VFS cache/readahead 的實際 mount 參數。

## 13. 最終判斷

兩邊的核心 wire format 與 backend schema 高度一致，明顯是刻意維持互通：相同 fingerprint、相同 large/split segment layout、相同 split metadata、相同 embedded-thumbnail 定義。差異主要來自產品入口不同，而不是儲存格式不同。

- Web 版最強的是即時 parallel pipeline、album batching、multi-account throughput、嚴格的 full-download／dedup completeness checks。
- WebDAV 版最強的是 durable local staging、OS filesystem 相容、任意 seek/Range、persistent rclone cache，以及 `/game` archive-as-folder。
- 若只用單一 Telegram 帳號，兩邊大部分上傳結果可以互相下載。
- 若 Web 版啟用多 linked accounts，任何存到 WebDAV configured session 之外的 file/part 都可能無法正確讀取；現在的 WebDAV download path 尚未達到 account-routing schema parity，這是優先於效能調優的正確性問題。
