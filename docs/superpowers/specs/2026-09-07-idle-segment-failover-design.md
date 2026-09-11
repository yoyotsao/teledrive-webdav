# TeleDrive Idle Segment Failover 設計規格

日期：2026-09-07
狀態：設計與書面審閱已完成，實作計畫已建立，待實作

## 摘要

TeleDrive 將以瀏覽器端中央 `SegmentScheduler` 管理大型分段檔案的派工、
attempt ownership、完成權與 idle work stealing。當某個大型 segment 因
`FLOOD_PREMIUM_WAIT` 長時間低速，而另一個 Telegram 帳號真正 idle 時，
Scheduler 才評估是否值得放棄慢帳號的暫存 parts，並讓 idle 帳號以新的
`fileId` 從 part 0 重傳該 segment。

Failover 不得中斷健康帳號原本正在執行的 byte upload，也不得讓兩個帳號同時
競速同一個 segment。Migration 的核心 invariant 是：

> 先讓舊帳號失去這個 segment 的完成權，再讓新帳號從頭接手。

第一版採用保守規則：只有 `FLOOD_PREMIUM_WAIT` 能產生 migration candidate；
接手帳號必須完全 idle 且具有五分鐘內的有效速度快照；分數嚴格大於 2 才搬移；
每個 segment 最多 migration 一次。

本功能完全位於前端。檔案 bytes 仍只在 Browser 與 Telegram CDN 之間傳輸，
Python backend 僅處理 SQLite metadata，不得代理檔案內容。

## 背景與目前問題

目前 `uploadFileSpread()` 會先切出所有 segment，再用 eager `Promise.all()`
為每個 segment 呼叫 `withAccountSlot()`。帳號只在 segment 開始時選擇一次；
一旦進入 `TelegramClientManager.uploadSegment()`，該 segment 的 `fileId`、
chunk pacer、semaphore 與最終 message 都固定屬於同一個 Telegram 帳號。

這可以在上傳開始時把 segments 分散到不同帳號，卻沒有執行中的重新排程：

- 某帳號持續 `FLOOD_PREMIUM_WAIT` 時，只會在原帳號內等待及重試。
- 其他帳號完成既有工作後會閒置，不會接手慢帳號的 segment。
- 最外層仍等待全部 segment；任何一個未完成，整個檔案都不能完成或登記
  metadata。
- Premium flood 目前只呼叫 `pause()`，不更新 `lastFloodAt`。較早發出的成功
  request 仍可觸發 fast ramp，形成「等待、原速重送、再次限流」的循環。
- 現有 log 沒有記錄兩次 premium flood 之間明確成功的 parts，無法量出
  Telegram 每輪實際接受多少資料。

## 目標

1. 只讓真正 idle 的帳號接手值得重傳的 premium-flood segment。
2. 不搶占或降低健康帳號既有工作的吞吐量。
3. 以單一 Scheduler 管理 segment ownership、migration 與 finalize 權限。
4. Migration 決策成立後立即使舊 attempt 的所有 lease 失效。
5. 確保舊 attempt 永遠不能在 migration 後建立 Telegram message。
6. 將有效進度與實際傳輸量分開，使 UI 能如實回退，統計仍保留重傳成本。
7. Premium flood 發生後立即停止 ramp，避免等待結束後又被成功 request 加速。
8. 記錄每個 flood cycle 的實際成功 bytes，使參數可依真實資料調整。
9. 保持檔案 bytes 不經過 Python backend 的架構邊界。
10. 全部驗收以自動化測試及 Playwright MCP 完成，不使用人工測試。

## 非目標

- 第一版不因普通 `FLOOD_WAIT`、RPC timeout、一般慢速或網路錯誤而跨帳號
  migration。
- 不讓忙碌但尚有 semaphore slot 的帳號接手 migration。
- 不讓兩個帳號同時競速同一個 segment。
- 不跨帳號續傳舊 `fileId` 的剩餘 parts；接手者一定從 part 0 重傳。
- 每個 segment 不進行第二次 migration。
- 不支援頁面重新整理後恢復 Scheduler、attempt 或 migration 狀態。
- 不為小檔案、album 或 thumbnail 本身增加跨帳號 migration；它們只參與帳號
  是否 idle 的判定。
- 不改變 segment 大小、512 KiB chunk 大小、Telegram message 格式或下載合併
  順序。
- 不新增後端檔案代理、暫存或續傳 API。

## 方案選擇

### 採用：中央 Segment Scheduler

`SegmentScheduler` 持有唯一權威狀態，負責 pending、active、migrating、
finalizing、completed、failed、attempt generation、帳號 reservation、score
與有效進度。GramJS 只執行 Scheduler 指定的 attempt，不自行決定 migration。

此方案能在單一位置原子處理「保留接手帳號」與「撤銷舊完成權」，也能一致拒絕
late callback。

### 不採用：在既有 Account Pool 加 idle callback

保留 eager promise 並在 idle 時通知原 promise 中止，表面改動較少，但
ownership 會分散在 `accountPool`、`splitUpload` 與 `gramjs`。Migration、
progress rollback 和 finalize race 難以由單一 invariant 保護。

### 不採用：新舊帳號同時競速

Idle 帳號直接重傳並讓兩者競速，可以降低切換延遲，但會浪費頻寬，且舊帳號可能
在背景偷偷完成並建立重複 Telegram message，違反本規格的核心原則。

## 架構與責任

### SegmentScheduler

Scheduler 是 segment lifecycle 的唯一寫入者：

- 管理所有大型檔案的 segment task queue。
- 派發正常 pending segment。
- 接收帳號活動、速度與 premium flood 事件。
- 判斷 idle 帳號應接手 migration、取得正常 pending，或保持 idle。
- 建立與撤銷 attempt lease。
- 原子提交 migration reservation。
- 原子授予 finalize 權。
- 計算檔案有效進度並發出受控回退事件。
- 在 terminal state 統一釋放 reservation 與 timers。

### UploadSpeedTracker

速度追蹤器保存：

- 各 active segment attempt 最近 30 秒、真正推進有效進度的 unique bytes
  buckets。
- 各帳號最近 30 秒的 effective bytes buckets；只包含真正推進有效工作的
  unique bytes。
- 各帳號最近 30 秒的 physical bytes buckets；包含所有收到明確成功回應的
  upload RPC bytes，包括 retry、重複 part 與 revoked attempt。
- Premium flood 時間與 wait 結束／feed resumed 時間。
- 帳號從 active 轉為 idle 時的速度快照及有效期限。
- 每個 flood cycle 的 accepted physical bytes。
- Logical bytes 與 physical bytes 兩套互不覆蓋的計數。

### SegmentAttemptExecutor

GramJS executor 只負責一個 Scheduler lease 所代表的 attempt：

- 建立該 attempt 專用的新 `fileId`。
- 依 part index 執行 `SaveBigFilePart`。
- 回報 request start、明確成功、flood、settle、error 與 chunks complete。
- 響應 lease revoke，取消尚未送出的 chunk、pacer wait 與 retry timer。
- 不硬斷已經送出的 MTProto RPC。
- 只有取得 Scheduler finalize grant 後才可發出 message/finalize RPC。

### AccountActivityRegistry

帳號是否 idle 是全域 Browser → Telegram byte workload，而非只看 Scheduler
內的大型 segment。所有上傳路徑必須向同一個 registry 回報工作與 RPC：

- 大型 `SaveBigFilePart` segment。
- 小檔案 bytes upload。
- Album preparation。
- Thumbnail upload。
- 尚未 settle 的 upload RPC。

Metadata register、SQLite API、JWT refresh、列表刷新與其他不傳檔案 bytes 的
工作不計入 registry。

## 狀態模型

### Segment task

概念模型如下；實作可依 TypeScript 模組邊界拆型別，但不得改變語意：

```ts
type SegmentState =
  | 'pending'
  | 'active'
  | 'migrating'
  | 'finalizing'
  | 'completed'
  | 'failed';

interface AttemptLease {
  taskId: string;
  attemptId: number;
  accountId: number;
}

interface SegmentTask {
  taskId: string;
  fileJobId: string;
  segment: Segment;
  state: SegmentState;
  attemptId: number;
  currentAccountId: number | null;
  migrationCount: 0 | 1;
  attemptedAccountIds: Set<number>;
  attemptStartedAt: number | null;
  /** Key 是這個 task 的 attemptId；只用於 lease-scoped drain。 */
  attemptInFlightRPCs: Map<number, number>;
  /** Migration commit 時保存被 revoke 的舊 attemptId，drain 完成後清除。 */
  drainingAttemptId: number | null;
  logicalUploadedBytes: number;
  completedPartIndices: Set<number>;
  result: SegmentResult | null;
}
```

`taskId` 在整個檔案工作期間不變；`attemptId` 是 ownership generation。任何
chunk 成功、進度、錯誤、retry、timer callback、chunks complete 或 finalize
要求都必須攜帶完整 `AttemptLease`。

Scheduler 只在以下條件全部相符時接受會修改 task 的事件：

```text
task 非 terminal
&& lease.taskId == task.taskId
&& lease.attemptId == task.attemptId
&& lease.accountId == task.currentAccountId
&& event 適用於 task 當前 state
```

### Account runtime

```ts
interface IdleSpeedSnapshot {
  bytesPerSecond: number;
  createdAt: number;
  expiresAt: number;
}

interface AccountRuntime {
  accountId: number;
  activeByteUploadJobs: number;
  inFlightUploadRPCs: number;
  reservedTaskId: string | null;
  idleSnapshot: IdleSpeedSnapshot | null;
  online: boolean;
  ready: boolean;
}
```

Migration reservation 與 active byte-upload job 是兩個互斥階段。Drain 期間只設定
`reservedTaskId`，不增加 `activeByteUploadJobs`；A 一旦被保留就不再符合 idle，
也不能取得正常 pending 工作。Drain 完成、A 真正開始 attempt 時，必須在同一個
同步 transition 清除 `reservedTaskId` 並將 `activeByteUploadJobs` 精確加一。

## 狀態轉移與完成權

```text
pending → active → finalizing → completed
              │
              ├→ migrating → active（新帳號、新 fileId）
              └→ failed

migrating → failed
finalizing → failed
```

禁止以下轉移：

```text
migrating → 原 B active
finalizing → migrating
completed / failed → 任何非 terminal state
```

### Migration 立即撤銷 lease

`attemptId` 必須在 `active → migrating` 成功的同一個同步決策內立即遞增，
不能等到新帳號開始傳輸才遞增：

```text
active, attemptId=1, account=B
→ commitMigration(A, task)
→ migrating, attemptId=2, currentAccountId=null
```

如此 B 的 lease 在 migration 決策成立當下就失效。`state === migrating` 是額外
防線，不是主要 generation guard。

### Finalize point of no return

當目前 attempt 的所有 parts 完成後，executor 必須呼叫
`grantFinalize(lease)`。Scheduler 以 CAS 語意執行：

```text
lease 仍有效 && state == active
→ state = finalizing
→ grant
```

只有 grant 成功後，GramJS 才能發出建立 Telegram message 的 RPC。Task 一旦
進入 `finalizing` 就永久禁止 migration，因為已送出的 message RPC 無法可靠
取消。

`commitMigration()` 與 `grantFinalize()` 對同一 task 互斥；同一事件循環中
無論哪一方先提交，另一方都必須失敗。

## Idle 定義與速度樣本

### 真正 idle

帳號只有在以下條件同時成立時才算 idle：

```text
activeByteUploadJobs == 0
&& inFlightUploadRPCs == 0
&& reservedTaskId == null
```

Semaphore 尚有空位不代表 idle。帳號還有任何 segment、chunk、小檔案、album、
thumbnail bytes 工作，或仍有未 settle 的 upload RPC，都不允許接手 migration。

### 30 秒有效速度

B 的 live speed 以目前 segment attempt 的固定 wall-clock window 計算：

```text
B_live_speed = 最近 30 秒明確成功的 bytes / 30 秒
```

分母固定為 30 秒，不排除 `FLOOD_PREMIUM_WAIT`、pacer pause 或沒有成功 bytes
的時間。這能讓持續等待的 B 自然降速。

每個明確成功的唯一 current-attempt part 同時更新該 segment 的 effective speed
sample。Retry、重複 part success、revoked attempt、timeout、結果未知及 rejected
request 不得進入 effective speed。

Physical speed 另行計算：

```text
physicalSpeed = 最近 30 秒所有明確成功 RPC bytes / 30 秒
```

Physical speed 只用於流量與 flood-cycle 診斷，不得用於 failover score。

### Idle 帳號速度快照

A 從 active 轉成真正 idle 時，凍結該帳號最近 30 秒的 account-wide effective
speed：

- 最近 30 秒必須至少有一筆明確成功 bytes，否則不建立快照。
- 快照建立後有效 5 分鐘。
- A 接受任何新 byte-upload 工作時立即使快照失效。
- A 再次完成工作並進入真正 idle 時重新建立。
- 快照過期或不存在時，A 不參與 failover，只能先取得正常 pending 工作以重新
  取得速度樣本，或保持 idle。

Account-wide snapshot 代表 A 最近可提供的整體 Telegram 上傳能力；B live
speed 則代表 candidate segment 在當前帳號實際得到的完成速度。兩者都只使用
真正推進 logical work 的 unique effective bytes；physical bytes 不得進入 A
snapshot 或 failover score。

## Migration Candidate 與分數

第一版只有同時符合以下條件的 segment 才是 candidate：

```text
state == active
&& current attempt 已執行至少 30 秒
&& 最近 30 秒內至少一次 FLOOD_PREMIUM_WAIT
&& migrationCount == 0
&& segment 尚未完成
```

普通 `FLOOD_WAIT`、RPC timeout、一般網路錯誤或單純低速不能建立 candidate，
仍走既有 backoff、retry 與 timeout。

對每一組 idle A 與 candidate B task 計算：

```text
remainingRatio =
  (segment.size - logicalUploadedBytes) / segment.size

failoverScore =
  (A_idle_snapshot_speed / B_live_speed) * remainingRatio
```

若 `B_live_speed > 0`，正常計算；若 `B_live_speed == 0`，只有在 candidate
條件已經成立後才將 score 視為 `Infinity`。這避免剛啟動、一般 timeout 或
尚未送出第一批 bytes 的 attempt 被誤判。

只有嚴格 `failoverScore > 2` 才允許 migration；`score == 2` 不搬移。這個
公式等價於要求「B 完成剩餘部分的預估時間」超過「A 從 part 0 重傳整個
segment 的預估時間」兩倍，為第一版保留足夠安全邊際。

## 排程規則

當 A 成為真正 idle，或既有 idle A 存在而 candidate 資格／分數發生變化時：

```text
找出 A 可接手且 score 最大的 candidate
↓
最大 score > 2
  → commitMigration(A, task)
否則
  → 取得正常 pending segment
否則
  → 保持 idle
```

Migration 的優先級高於正常 pending，但只在 A 真正 idle 時評估，因此不會
搶走 A 正在執行的健康工作。若沒有值得搬移的 candidate，正常 pending 仍可依
既有 per-account concurrency 填入可用容量。

Scheduler 必須串行提交派工決策。多個 idle 帳號可以同時計算候選，但只能由
一個同步 commit 改變 task 與帳號狀態；提交前必須重新驗證所有條件。

重新評估至少由以下事件驅動：

- 帳號轉為真正 idle。
- 收到 premium flood。
- Candidate attempt 到達 30 秒資格時間。
- Candidate logical bytes／B live speed 更新。
- Idle snapshot 建立、失效或過期。
- Task 進入 terminal state 並釋放帳號。

不得依賴 A 再次發生 idle transition；若 A 已 idle，而 B 稍後才符合資格，仍要
主動評估。實作應以狀態事件加上最近的單一資格 timer 驅動，不為每個 part 建立
長期 polling timer。

## 原子 Migration Commit

`commitMigration(A, task)` 不得包含 `await`，並必須在一次同步交易內重新驗證
及完成以下操作：

```text
1. A 仍 online、ready、真正 idle，且 snapshot 尚未過期
2. A 不在 task.attemptedAccountIds
3. task 仍 active、candidate 成立、migrationCount == 0
4. 重新計算的 score 仍 > 2
5. reserve A，令 A 立即不再 idle
6. invalidate A.idleSnapshot
7. task.state = migrating
8. task.attemptId += 1，立即撤銷 B lease
9. task.drainingAttemptId = 被撤銷的舊 attemptId
10. task.currentAccountId = null
11. task.logicalUploadedBytes = 0
12. task.completedPartIndices.clear()
13. task.migrationCount = 1
14. task.attemptedAccountIds.add(A.accountId)
```

初始帳號在第一次派工時就加入 `attemptedAccountIds`。新帳號必須不同於原帳號，
且從未出現在該集合中。

`migrationCount` 在 commit 時設為 1，而不是等 A 開始後才設定。即使 handoff
期間發生錯誤，也不得再次 migration 或退回 B。

## Handoff 與 Drain

Migration commit 後依序執行：

1. 通知 B executor lease 已撤銷。
2. B 停止取得新的 chunk work。
3. 取消 B 尚未送出的 pacer wait、retry delay 與 qualification timer。
4. 已送出的 `SaveBigFilePart` 不硬斷，等待其 wrapper settle。
5. 每個 wrapper 在 `finally` 路徑，同時精確減少該 lease 的
   `attemptInFlightRPCs[oldAttemptId]` 與 B 帳號全域 `inFlightUploadRPCs`。
6. Wrapper 仍受既有 `CHUNK_SEND_TIMEOUT_MS`（120 秒）限制，因此 drain 不會
   無限等待。
7. 只等待被撤銷 lease 的 `attemptInFlightRPCs[oldAttemptId] == 0`；不得等待 B
   帳號全域 `inFlightUploadRPCs == 0`。B 的其他 segment、小檔案或 thumbnail
   不得拖住這次 handoff。
8. Drain barrier 通過後，再確認 task 仍是相同 `migrating + attemptId`，並清除
   `drainingAttemptId` 與舊 attempt counter。
9. A 建立新 `fileId`。Scheduler 在一次同步 transition 中驗證
   `A.reservedTaskId == taskId`，接著清除 `reservedTaskId`、將
   `activeByteUploadJobs` 精確加一、設定 `currentAccountId=A`、`state=active`、
   `attemptStartedAt=now`，最後發出新 lease。
10. A 從 part 0 重傳整個 segment。

A 在 drain 期間已被 reserved，不得接正常 pending 工作。Migration commit
一旦成功便不可撤銷。

Reservation 階段不增加 `activeByteUploadJobs`；啟動 attempt 的 transition 只增加
一次。Terminal cleanup 必須依 task 當時持有的是 reservation 或 active job 分別
執行「清除 reservation」或「active job 減一」，不得兩者都做，也不得重複計數。

若 A 在 drain 期間離線或失效：

- B 繼續 drain，但永遠不恢復 lease。
- Drain 完成後，A 依既有 sender reconnect／retry／timeout 機制等待恢復。
- A 最終仍不可用時，task 進入 failed。
- 不選擇第三個帳號，也不進行第二次 migration。

### 舊 attempt 回應

舊 B request 回來時：

- 一律解除對應 in-flight bookkeeping。
- 只有 `SaveBigFilePart` 明確成功回應才計入 physical traffic。
- Timeout、取消、拒絕或結果未知不得算成功 bytes。
- 不更新 `logicalUploadedBytes` 或 `completedPartIndices`。
- 不發出有效進度。
- 不得取得 finalize grant。
- 不得寫入 `SegmentResult` 或改變 task state。

Transport-level physical telemetry 可在 Scheduler task event 之外記錄已確認成功
bytes；Scheduler 不接受 stale lease 對 task 的狀態修改。

## Premium Flood Pacer

帳號 chunk pacer 增加 session-only 模式：

```text
normal → frozen → cautious
            ↑         │
            └─ FLOOD ─┘
```

### Frozen

收到 `FLOOD_PREMIUM_WAIT` 時：

```text
等待 Telegram 指定秒數
rate 保持不變
mode = frozen
禁止 reportSuccess 觸發任何 ramp
```

成功 request 仍正常記錄 logical／physical bytes 和速度樣本，只是不改變發送
rate。第一版不因 premium flood 自動降 rate，也不建立或降低 rate ceiling。

多個同一 penalty window 的 premium flood 以最大的 `penaltyUntil` 為準。60 秒
clean window 不從收到 flood 當下計算，也不包含 Telegram 指定的 wait：

```text
wait 全部結束
→ 第一個 post-wait send 真正恢復
→ cleanWindowStart = now
→ 連續 60 秒沒有任何 FLOOD
→ mode = cautious
```

若 wait 結束後沒有任何 request 發送，就不開始 clean window。期間再次收到
任何 flood，立即回到／維持 frozen；等新的 wait 結束且真正恢復傳輸後重新計
60 秒。

普通 `FLOOD_WAIT` 仍執行既有 rate backoff 與 ceiling learning，但同樣會中斷
上述 clean window。

### Cautious

進入 cautious 後：

- 每 30 秒最多增加 0.1 parts/s。
- 本頁面 session 內不再回到原本每 10 秒增加 0.5 parts/s 的 fast ramp。
- 再次收到任何 flood 便回到 frozen。
- `frozen`／`cautious` 模式不寫入 storage。
- 既有 rate persistence 行為維持不變，但 premium flood 本身不寫入額外的 rate
  penalty 或 ceiling。

## Logical Progress 與 Physical Traffic

### 有效進度

`logicalUploadedBytes` 代表目前有效 attempt 對最終 segment 有價值的 bytes：

- Current lease 的唯一 `partIndex` 第一次明確成功時才累加。
- 同一 part 的重複成功不得重複增加 logical progress。
- 正常情況只能增加。
- Migration commit 時清零，並清空 current-attempt completed-part set。
- 整檔有效進度為所有 segments 的 logical bytes 加總除以檔案大小。

Migration 是同一次檔案上傳中唯一允許有效進度下降的事件，且必須伴隨
`attemptId` 遞增。既有 Upload Center 對「進度不能倒退」的保護需加入這個明確
例外。

UI 在回退時顯示：

```text
重新分派上傳帳號，該區段將從頭重傳
```

### 實際傳輸量

`physicalTransferredBytes` 代表 Telegram 明確接受的 bytes：

- Current 或 revoked attempt 的明確成功回應都累加。
- 不因 migration、進度回退、task failed 或 UI 清除而回退。
- Timeout／unknown 不計。
- 用於既有每日帳號統計與 migration overhead 診斷，不作為 UI 有效百分比。

在單一大型檔案範圍內可計算：

```text
migrationOverhead =
  confirmed physical segment bytes - completed logical segment bytes
```

該值只包含本次大型 segments 的 confirmed chunk traffic；thumbnail、metadata
與未收到成功回應的未知 RPC 不混入此檔案的 migration overhead。

## Flood-cycle 與 Migration 診斷

每個 premium flood cycle 至少輸出：

```text
accountId / accountName
taskId / fileJobId / segmentIndex / attemptId
waitSeconds / penaltyUntil
pacerMode / scheduledRate
B_live_speed
logicalUploadedBytes / remainingRatio
accountAcceptedPartsSincePreviousPremiumFlood
accountAcceptedBytesSincePreviousPremiumFlood
taskAcceptedPartsSincePreviousPremiumFlood
taskAcceptedBytesSincePreviousPremiumFlood
```

`acceptedBytesSincePreviousPremiumFlood` 必須使用 physical success bytes。即使是
revoked attempt，只要 Telegram 明確回覆成功，仍算 Telegram 在該 cycle 實際
接受的 bytes。

Migration decision 至少輸出：

```text
taskId / segmentIndex / oldAttemptId / newAttemptId
fromAccountId / toAccountId
A_snapshot_speed / snapshotAge
B_live_speed / remainingRatio / failoverScore
abandonedLogicalBytes
migrationCount
```

Log 不得包含 session string、JWT、access hash、Telegram auth key 或其他憑證。

## Terminal Cleanup

任何 task 進入 `completed` 或 `failed` 時，Scheduler 必須在同一個 terminal
transition 中：

```text
釋放 current account 或 migration target reservation
結束 activeByteUploadJob bookkeeping
取消 qualification timer
取消 retry timer
取消仍可取消的 pacer wait
清除 task 專屬排程 callback
使所有 lease 永久失效
```

Terminal task 不得再接受任何 lease event，也不得再次進入 pending、active、
migrating 或 finalizing。晚到的 transport settlement 只能完成 transport-level
in-flight cleanup；明確成功的 bytes 可由獨立 physical telemetry 記錄，但不能
改變 terminal task。

`AccountActivityRegistry.inFlightUploadRPCs` 是 transport-level counter，不屬於
terminal task。Terminal transition 不得為了清理 task 而把它強制歸零；若底層
RPC 尚未 settle，它必須繼續讓帳號維持非 idle，直到對應 wrapper success、error
或 deadline 的 `finally` 精確減一。Task 自身不保留該 RPC 的 lease callback，
因此 settlement 不會重新打開 terminal state。

`SegmentTask.attemptInFlightRPCs` 只服務特定 attempt 的 drain barrier；帳號全域
counter 只服務 idle 判斷。兩者在同一個 request start／settle 邊界成對增減，但
不得互相替代或以其中一個強制歸零另一個。

Cleanup 必須可重入；重複呼叫不得重複釋放 reservation、把 counter 減成負數，
或產生新的 timer。這避免錯誤路徑留下永久 reserved 帳號，使 Scheduler 之後誤判
它不 idle。

## 與既有流程整合

`uploadFileSpread()` 改為：

1. 使用既有 `planSegments()` 產生 segment descriptors。
2. 將 descriptors 註冊為同一 `fileJobId` 下的 Scheduler tasks。
3. 由 Scheduler 動態派工，而非在建立 promise 時永久綁定帳號。
4. 等待全部 task terminal；任一 failed 則沿用既有檔案失敗處理。
5. 全部 completed 後依 `segment.index` 排序 `SegmentResult`。
6. 依既有流程登記 metadata。

Segment 0 的 thumbnail 仍只在取得 finalize grant 後由該 segment 的 current
account 附加。若 segment 0 migration，舊 B 因沒有 finalize grant 不會建立
message；A 使用新 `fileId` 完成後正常附加 thumbnail。

Small file、album 與 thumbnail 路徑不加入 candidate queue，但必須透過
`AccountActivityRegistry` 正確阻止帳號被誤判為 idle。

Python backend、資料表及 metadata API 不需因 Scheduler 新增 binary upload
能力；必要的 UI 狀態仍留在當前頁面工作階段內。

## 自動化驗證

### 單元測試

以 Vitest fake timers 與 deterministic clock 驗證：

1. 30 秒 B live window 使用固定分母並包含 premium wait。
2. 最近 30 秒沒有成功 bytes 時 B speed 為 0。
3. Idle snapshot 只有最近窗口存在明確成功 bytes 才建立。
4. Idle snapshot 只使用 effective unique bytes；retry、重複 part 與 revoked
   attempt 的 physical bytes 不得提高 snapshot speed。
5. Idle snapshot 五分鐘後失效，帳號接工作時立即失效。
6. `activeByteUploadJobs > 0`、`inFlightUploadRPCs > 0` 或存在 reservation 時皆
   不算 idle。
7. Candidate 必須 active 至少 30 秒且最近 30 秒發生 premium flood。
8. 普通 flood、timeout 與一般慢速不建立 candidate。
9. B speed 為 0 只有 candidate 成立後才產生 Infinity score。
10. `score == 2` 不 migration，`score > 2` 才 migration。
11. 最大 score candidate 被選中；已嘗試帳號永不被選中。
12. 每個 segment 最多 migration 一次。

### Scheduler race 測試

以可控 deferred promises 驗證：

1. `commitMigration` 在一次同步提交內同時 reserve A、清除 snapshot、撤銷 B
   lease、遞增 attemptId 及清零 logical progress。
2. 兩個 idle account 同時選取同一 task 時只有一個 commit 成功。
3. Migration 與 finalize 同時競爭時只有一個 CAS 成功。
4. 舊 attempt 的 chunk success 不增加 logical progress，也不能 finalize。
5. 舊 attempt 明確成功可增加 physical bytes；timeout／unknown 不增加。
6. B 不再取得新 chunk；只有 revoked attempt 自己的 RPC 全部 settle／deadline
   後 A 才開始。B 帳號其他工作的 global in-flight 即使大於零也不得阻塞 A。
7. A 在 drain 期間離線不恢復 B lease；A 最終失敗使 task failed。
8. Reservation 不增加 active job；reservation → active 只增加一次，terminal
   cleanup 依持有階段清理，不會 double increment／decrement。
9. Terminal cleanup 釋放 reservation、清除 timers，且重複 cleanup 不破壞 counter。
10. Terminal task 拒絕所有 late lease event。

### Pacer 測試

1. Premium flood 立即進入 frozen，但不降低 rate 或建立 ceiling。
2. Frozen 期間任何成功 request 都不能 ramp。
3. 60 秒 clean window 從 wait 結束後第一個實際 send 開始。
4. Wait 結束但沒有 send 時不得進入 cautious。
5. Clean window 中任何 flood 都重設 wait 與計時起點。
6. 60 秒 clean 後進入 cautious，每 30 秒最多增加 0.1 parts/s。
7. 本頁面 session 內不再恢復 fast ramp。

### 整合與 Playwright MCP

使用 fake Telegram executors 驅動前端整合情境：

1. B 上傳至部分進度並反覆 premium flood。
2. A 忙碌時 B 只標記為 candidate，B 不被中止。
3. A 真正 idle 後 score 超過 2，發生 migration。
4. Upload Center 顯示重新分派訊息，整檔有效進度按 abandoned bytes 回退。
5. Late B completion 不會覆寫 A attempt 或產生第二個 completed result。
6. A 尚有 in-flight upload RPC 時不得接手。
7. Score 不足時 A 取得正常 pending segment，不發生 migration。
8. 全部完成後 metadata 仍依 segment index 與實際 storage account 登記。
9. 單帳號模式下，premium candidate 可以成立，但因沒有 replacement 而不
   migration、不更換 `fileId`；原 attempt 繼續 wait／retry 並可正常完成。
10. 三個帳號同時 premium flood 且都不符合 idle replacement 時，不發生
    A→B→C migration；其中一個帳號稍後真正 idle 且有有效 snapshot 時，才重新
    計算並只在 score > 2 時 migration。

Playwright 驗證必須透過 Playwright MCP 執行；不得以人工點擊作為驗收證據。

## 驗收條件

1. 健康且忙碌的帳號永遠不因 failover 被中斷或額外派工。
2. 只有具有效快照的真正 idle 帳號能成為接手者。
3. 只有符合 premium candidate 條件且 score 嚴格大於 2 才 migration。
4. Migration commit 當下即撤銷 B lease 並 reserve A，不存在可觀察的中間狀態。
5. B 在 migration 後永遠不能 finalize 或建立該 segment 的 Telegram message。
6. A 一律以新 `fileId` 從 part 0 重傳。
7. 每個 segment 最多 migration 一次，且不回到嘗試過的帳號。
8. 有效進度只在 migration generation change 時允許回退；physical traffic 永不
   回退。
9. Premium flood wait 期間及其後 60 秒 clean window 內不會 ramp。
10. 每輪 premium flood 可從 log 讀出 Telegram 明確接受的 parts 與 bytes。
11. Terminal task 不留下 task-owned reservation、lease callback 或 timers；獨立的
    account transport in-flight counter 會在 wrapper settle／deadline 時精確清除，
    且清除前帳號不得被視為 idle。
12. 檔案 bytes 全程不接觸 Python backend。
13. Drain 只等待 revoked attempt 的 lease-scoped in-flight barrier，不等待來源
    帳號其他工作的全域 in-flight 歸零。
14. Migration reservation 不計為 active byte job；真正啟動接手 attempt 時才
    精確增加一次 active job。
15. A snapshot 與 failover score 只使用 effective unique bytes，不使用 physical
    traffic。
