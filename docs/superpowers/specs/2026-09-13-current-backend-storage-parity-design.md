# TeleDrive WebDAV → Current Backend Storage Parity Design

**Date:** 2026-09-13

**Status:** Proposed

**Target:** `yoyotsao/teledrive-webdav` master

**Backend contract source:** current `yoyotsao/teledrive` master

## 1. Goal

Update `teledrive-webdav` so metadata, Telegram read routing, upload routing, deduplication, and crash-recovery semantics conform to the current TeleDrive backend storage contract.

WebDAV-specific behavior remains unchanged: file bytes stay between local Telegram clients and Telegram; FastAPI remains metadata-only; Range/seekable reads remain streaming; local durable staging remains the source of truth for pending filesystem writes; `/game` archive-as-directory behavior and rclone/VFS integration remain.

“Parity” means parity at the TeleDrive metadata and Telegram storage-location boundary, not literal parity of browser implementation. WebDAV intentionally adopts the stronger backend contract for all new message-producing writes: Saved Messages writes always use primary, and every new upload—including albums—uses durable Telegram operations.

## 2. Background

The previous model routed physical reads as `telegram_user_id → Saved Messages → telegram_message_id`. The current backend separates logical rows from canonical physical location:

```text
logical file / split part
    ↓
canonical FileLocation
    ├─ telegram_chat_id
    ├─ telegram_message_id
    ├─ telegram_media_kind
    ├─ telegram_media_id
    ├─ telegram_media_size
    ├─ telegram_photo_variant
    └─ location_version
```

A location is either Saved Messages (`telegram_chat_id = NULL`, exact `telegram_user_id` is required) or a shared channel (`telegram_chat_id = canonical channel ID`, uploader account is not read authority). For channel-backed files, any live linked local account that currently has access may read the same message.

## 3. Non-negotiable invariants

### 3.1 Metadata-only backend

No Telegram session, auth key, channel peer access hash, file payload, thumbnail payload, or media bytes may be sent to the backend. Document/file `access_hash` is media metadata and may be stored where the backend schema permits it. It is distinct from account-scoped channel peer access hashes, which remain local.

### 3.2 Backend owns logical state

The backend is authoritative for linked-account membership, storage-target mode/version, accounts version, logical files/folders, canonical physical location, location version, durable Telegram operation journal, and operation result version.

### 3.3 Telegram remains local

Local Telethon workers own peer resolution, Telegram message/media reads, byte uploads, forwarding, channel read/write checks, and frozen-result recovery.

### 3.4 Storage target is not existing-file location

The global storage target affects new work only. Existing files are always read from the canonical location stored on the file/part. Never derive an existing file’s Telegram location from current `/storage-target`.

### 3.5 Staged bytes are generation-owned

A logical WebDAV path is not a durable transfer identity. Every accepted staged payload receives a persistent `transfer_id` and `staging_generation`; its recovery cursor and immutable staged source are bound to that identity. Cleanup may remove only the exact source owned by the completing generation. An older transfer must never unlink, replace, or mark complete the staged bytes of a newer overwrite at the same logical path.

## 4. Canonical location model

Introduce a physical-location value object independent from `Entry`:

```text
FileLocation
    telegram_chat_id: str | None
    telegram_user_id: int | None
    telegram_message_id: int
    media_kind: document | photo
    media_id: str
    media_size: int
    photo_variant: str | None
    location_version: int
```

Rules:

1. `telegram_chat_id == null`: Saved Messages; `telegram_user_id` is required; reads use that exact account.
2. `telegram_chat_id != null`: shared channel; `telegram_user_id` is not read authority; any suitable linked account may read.
3. Canonical location is valid only when required media identity is complete.
4. Legacy pre-location rows remain readable through `telegram_user_id + telegram_message_id`; historical account ID `0` maps to primary only where the existing legacy compatibility path already requires it.
5. Non-null channel id with incomplete canonical media fields fails closed; never fall back to Saved Messages.
6. `file_id` is not canonical Telegram media identity. Canonical identity is `telegram_media_id`.

## 5. Cache identity

Any cache derived from Telegram physical bytes must key on the canonical physical location, logically including:

```text
target (saved_messages:<user> OR channel:<channel>)
telegram_message_id
media_kind
media_id
media_size
photo_variant
location_version
```

This applies to split-part metadata caches, Telegram document/reference caches, thumbnails, media properties, ZIP source identity, prefetched heads, and future bridge byte caches. A `location_version` change must naturally cause a miss. Directory name/listing caches may retain their TTL but cannot be physical-location authority. Bump incompatible on-disk cache schemas rather than interpreting old physical rows as canonical.

## 6. Read path

### 6.1 Open-time refresh

A byte-open must refresh physical location even if a directory listing is still cached.

Non-split:

```text
WebDAV open
→ GET /files/{file_id}/download
→ parse canonical FileLocation
→ resolve Telegram reader
→ validate message/media identity
→ expose SeekableRemoteFile
```

Split:

```text
WebDAV open
→ fetch current split-group rows
→ sort by part_index
→ resolve canonical FileLocation for every part
→ concatenate resolved parts
```

### 6.2 Saved Messages reader

Use the exact stored account, peer `me`, exact message id, then verify canonical media identity. Do not fall back from a non-zero historical account to primary.

### 6.3 Channel reader

Consider local accounts that are online, backend-linked, and have valid local sessions. For each account independently resolve the channel through that account’s peer/entity cache, refresh dialogs if needed, verify read access, fetch the message, then validate media kind, media id, media size, and photo variant. Account-local failure may try another account. Canonical media mismatch is not account-local and fails the read.

### 6.4 Peer isolation

A channel entity/access hash resolved by account A must never be reused by account B. Peer/access caches are scoped by Telegram session generation + account + channel id. Relogin/session replacement invalidates that account’s cached channel peers/permissions.

## 7. Storage-target client

Consume `GET /api/v1/storage-target` fields `storage_mode`, `channel_id`, `channel_title`, `version`, `accounts_version`, and `verifications`. Stored verifications are historical enable-time audit only, not runtime Telegram authority.

Introduce immutable `FrozenStorageTarget` containing at least storage mode, channel id, target peer key, target version, accounts version, primary account id, and linked account ids. A transfer retains this snapshot for its durable operation. A settings change after operation start never silently retargets it.

## 8. Upload writer routing

### 8.1 Saved Messages

All new WebDAV Saved Messages writes, including album media, go to the primary account’s Saved Messages. Do not round-robin them across linked accounts. This is an intentional normalization. Secondary Saved Messages remain readable for historical files.

### 8.2 Channel

All locally available backend-linked accounts that can currently write the frozen channel may act as writers. Split parts may be uploaded by different accounts while all messages live in the same shared channel. The uploader account is not future read authority.

## 9. Durable Telegram operation protocol

Every new message-producing write uses backend Telegram operations. New uploads must stop using `send Telegram message → POST /files/register` as the commit path.

### 9.1 Single-message intent

Before a single-message Telegram RPC:

1. Generate `operation_id` and stable Telegram `random_id`.
2. `POST /telegram-operations` with kind, logical file id, group id when applicable, part index, uploader id, target kind/channel/peer key, created target/accounts versions, random id, RPC kind, and request metadata.
3. Transition the operation to `sending`.
4. Persist the same immutable operation identity to local WebDAV recovery state.
5. Only then start the Telegram send.

An operation-create topology conflict is automatically replannable only while no Telegram message-producing RPC for that logical transfer/group has started.

### 9.2 Split/album group intent barrier

Split uploads and albums are group operations for send-safety purposes. They use one frozen target and one durable local group manifest.

Before any child message-producing RPC may start:

1. freeze target/accounts once for the entire group;
2. allocate one `group_id` plus every child `operation_id` and `random_id`;
3. choose each child uploader/peer against that same frozen target;
4. create **all** child operations successfully with the same frozen target/accounts versions;
5. persist the complete child-operation manifest locally;
6. transition every child to `sending` and persist those versions/cursors;
7. arm the group for send only after every child is durably represented and no create conflict remains.

No child Telegram send may occur before this barrier is complete.

If any child operation creation returns a target/accounts topology conflict during preflight, zero Telegram sends have occurred. Previously created unsent child intents are tombstoned/abandoned with a preflight-conflict reason where the backend supports it, the local group manifest records the aborted preflight, and the whole group may be rebuilt from a fresh topology snapshot with new operation/random IDs.

Once any child enters the message-producing phase, the group is pinned to its original `group_id`, frozen target, child operation IDs, random IDs, and planned uploaders. A later topology change must not cause whole-group replan, replacement child operations, or replay of an already-sent child. If an unsent child can no longer use its frozen writer after another child has started sending, the group becomes blocked/recovery-required until that original child operation can safely continue or is explicitly resolved; it does not silently cross to a new target/group.

For `SendMultiMedia`, the single bulk RPC itself is the group message-producing boundary: all child intents/cursors must satisfy the barrier before the RPC starts.

### 9.3 Telegram result

After Telegram confirms a message, collect destination message id, media kind/id/size, media access hash when applicable, and photo variant when applicable. Atomically persist this exact destination result locally, bound to the operation and frozen target, before backend reconciliation.

Then call `POST /telegram-operations/{operation_id}/reconcile-result` with expected operation version. Registration requires a durable `result_version`.

### 9.4 Logical registration and staged-source ownership

Single-message files use `POST /telegram-operations/{operation_id}/register`. Split/group uploads use `POST /telegram-operation-groups/{group_id}/register` only after every child has a durable result.

Backend operation journaling protects Telegram RPC → metadata commit; local WebDAV staging protects filesystem write → local source bytes. Both remain.

A successful logical registration does not authorize path-based cleanup. The stager may remove a source only when all of these match the completing record:

```text
logical staging key
transfer_id
staging_generation
immutable source_path
```

Cleanup unlinks that exact `source_path`; it must never look up the current logical path and delete whatever bytes now occupy it.

## 10. Operation recovery and staging generations

Core rule: never blindly resend a Telegram operation whose outcome is uncertain.

Automatic completion requires either a backend durable result or a locally persisted, uniquely identified destination result that can be fetched from the frozen destination and revalidated. If neither exists, the operation remains `uncertain` and its generation-owned staged bytes are retained.

`uploader_id + target peer + random_id + request_metadata` is not a destination message locator. The implementation must not assume direct historical lookup by `random_id`.

### 10.1 Recovery evidence order

1. Fetch current backend operation. If it already has durable result/result_version, resume registration or location switch.
2. Otherwise, if local recovery state has exact destination message id + canonical media identity, fetch that exact message from the frozen destination and validate it.
3. On a valid exact result, reconcile it then continue registration.
4. If neither backend nor local state has a usable destination locator, mark/persist `uncertain` and stop.
5. Do not scan Telegram history by filename, timestamp, size, or similarity.
6. Replaying a message-producing RPC—even with the same random id—is not part of baseline recovery.

On startup and before retrying interrupted staged work, recovery checks local cursor plus backend operations in sending/recovering/retryable/uncertain states. If no authoritative result exists, preserve a local send guard and do not generate a replacement operation/random id or perform a new message-producing RPC. One uncertain split child blocks group registration and source cleanup.

### 10.2 Persistent generation identity

Every accepted ordinary PUT or `/game` pack receives:

```text
logical_key
transfer_id: UUID
staging_generation: monotonically increasing integer per logical_key
source_path: generation-specific immutable staged file
created_at
```

The queue allocates `staging_generation` atomically under the same lock/transaction that records the new pending item. The recovery key includes both generation and transfer identity:

```text
upload:<normalized destination path>:<staging_generation>:<transfer_id>
game:<top-level pack unit>:<staging_generation>:<transfer_id>
```

The immutable `source_path` is never reused by a later overwrite.

### 10.3 Same-path overwrite ordering

A write B accepted while an older generation A of the same logical key is nonterminal creates a new immutable generation; it does not replace A's source/cursor.

Per logical key, nonterminal generations form a durable FIFO. Only the oldest nonterminal generation may enter a message-producing phase or logical registration. Therefore:

```text
A interrupted
→ B staged at same logical path as a newer generation
→ recover/settle A without touching B source
→ generation-safe cleanup of A
→ only then B may send/register
```

If A is `uncertain`, B remains staged and visible as waiting-behind-recovery; B is not sent ahead of A. Releasing B requires A to be recovered or explicitly operator-tombstoned/abandoned according to the durable-operation rules. This avoids both stale cleanup and commit-order inversion.

### 10.4 MOVE and DELETE while a generation is pending

Baseline behavior is fail closed. If a logical path/pack unit has any nonterminal staging generation, WebDAV MOVE/rename or DELETE/trash against that pending logical item returns a locked/conflict response (`423 Locked` at the WebDAV boundary) and does not mutate the generation's logical key, immutable source, recovery cursor, or frozen operation identity.

This change does not attempt to retarget or cancel an in-flight durable Telegram operation. MOVE/DELETE is allowed again after all generations for that logical item reach a terminal state. A later design may add generation-aware deferred metadata mutations, but they are not part of this implementation.

## 11. CAS and topology changes

Storage-target version, accounts version, operation version, result version, and file `location_version` are correctness controls.

- Single-message operation-create topology conflict: no Telegram RPC occurred; refetch target/accounts and replan.
- Split/album operation-create topology conflict: the group intent barrier guarantees the conflict is discovered before **any** child send. Abort/tombstone that preflight and replan the whole group from a fresh snapshot.
- After the group barrier is armed, and especially after any child send starts or succeeds, the group never replans to a new target/group because topology changed. Existing child operations/results remain authoritative; unsent children stay bound to the frozen group.
- Reconcile conflict after send: GET current operation; accept identical durable result if already present; otherwise retry metadata CAS with current version if still safe; never resend Telegram payload because metadata advanced.
- Existing logical rows moved to another physical location use `/file-locations/{file_id}/switch` or `/file-location-groups/switch` with expected location version, operation id, and result version. Never update canonical location through a normal rename/update request.

## 12. Dedup semantics

Keep existing exact-size fingerprint and complete-split validation, then add target compatibility.

### Saved Messages target

Reuse only when every selected part is already in frozen primary Saved Messages. Candidates in another account’s Saved Messages or any channel are not reusable for that target.

### Channel target

- all parts already in frozen channel → reuse;
- all complete parts in Saved Messages → durable relocation;
- another channel or mixed incompatible locations → reject candidate or upload fresh before relocation starts.

### Durable Saved Messages → channel relocation

For each source part, freeze target, require exact source Saved Messages account locally, resolve target channel through that source account, create durable operation with source-location snapshot and `rpc_kind = messages.forwardMessages`, mark sending, forward using stable random id, persist/reconcile destination identity, and require result version. After every part is durable, perform single/group location-switch CAS. Logical file id, parent, filename, hash, trash state, and other logical metadata do not change.

Multi-part relocation applies the same group intent barrier: create every forward operation before forwarding the first part. A preflight topology conflict is replannable only before any forward starts; after one part is forwarded the original frozen group is retained and no completed forward is replayed.

## 13. COPY, MOVE, rename, delete

For backend-committed files with no pending staging generation, MOVE/rename stay metadata-only. Trash/delete keep backend soft-delete semantics and do not remove Telegram bytes.

COPY remains metadata-only where backend aliases are permitted, but source physical locations are refreshed first; canonical location fields are copied rather than rebuilt from `/storage-target`; new logical rows start with backend-defined location version; split COPY preserves ordered canonical locations. COPY does not trigger storage-target relocation.

Pending local generations follow Section 10.4: MOVE/DELETE fail closed rather than mutating in-flight recovery identity.

## 14. Thumbnail, preview, `/game`

Thumbnail/media-property reads use the same canonical resolver as full-file reads. Thumbnail caches include physical location version. `/game` ZIP readers open through resolved canonical parts. Existing lazy/sharded archive optimizations remain. A storage-location change produces a new ZIP source identity without invalidating unrelated archive caches.

Every message-producing `/game` upload uses the same durable operation cursor, staging-generation ownership, FIFO overwrite ordering, group barrier where a pack produces multiple Telegram messages, and uncertain-send guard as ordinary PUT uploads.

## 15. JWT lifecycle

Replace `401 → immediate bot challenge` with process-wide single-flight refresh:

```text
request 401
→ if token was already replaced by another thread, retry with current token
→ otherwise one POST /auth/refresh using stale Bearer token
→ atomically persist returned JWT
→ retry original request once
```

Other WsgiDAV threads wait for the refresh result. Keep thread-local `requests.Session`; keep JWT shared process-wide; never log JWT contents. If refresh grace is rejected, execute exactly one process-wide challenge login and retry once.

## 16. API surface

Existing metadata APIs remain. Mandatory integrations include:

```text
POST /auth/refresh
GET  /storage-target
POST /telegram-operations
GET  /telegram-operations
GET  /telegram-operations/{id}
PATCH /telegram-operations/{id}
POST /telegram-operations/{id}/reconcile-result
POST /telegram-operations/{id}/register
POST /telegram-operation-groups/{group_id}/register
POST /file-locations/{file_id}/switch
POST /file-location-groups/switch
```

`POST /files/register` may remain only for compatibility operations that do not create a new Telegram message.

## 17. Module boundaries

- `tdapi.py`: JWT refresh, storage-target wire model, canonical location parsing, current physical metadata lookup, operation API/CAS, location-switch API. It does not resolve Telegram peers.
- `transfer_models.py`: FileLocation, ResolvedRemotePart, FrozenStorageTarget, DurableOperationIdentity, DurableSendResult, generation-owned recovery cursor/state. Account-only `RemotePart` becomes legacy-only.
- `telegram_accounts.py`: exact Saved Messages routing, per-account channel peer/access validation, cache generation, reader/writer selection.
- `tgio.py`: explicit-peer reads/sends, canonical media validation, target-aware message RPCs, relocation forwarding.
- `tgupload.py`: stable random-id helpers while preserving byte-part upload mechanics.
- `upload_engine.py`: freeze target → dedup → group preflight barrier → choose writers → persist operation intents → Telegram send → persist/reconcile result → logical/group registration.
- `operation_state.py`: atomic local transfer/group manifests keyed by logical key + staging generation + transfer id; immutable source ownership; group send barrier; recovery cursor/result state.
- `uploadstage.py` and `gamestage.py`: allocate generations, preserve immutable source versions, FIFO same-key ordering, pending MOVE/DELETE lock, uncertain guard, complete only after backend logical registration.
- `bridge.py`: logical path resolution, fresh canonical physical open, same resolver for thumbnails/ZIP, WebDAV lock/conflict surface for pending mutations.

## 18. Migration coexistence

WebDAV does not become the storage-migration controller. Migration remains owned by TeleDrive frontend/maintenance workflows. The mount may remain running while files move: names/logical ids stay usable; next byte-open fetches current canonical location; `location_version` invalidates stale physical caches; channel-backed files can be read through any usable local account; no manual `/rpc/forget` is required solely because physical storage changed.

## 19. Failure behavior

- No channel reader: return a readable access/routing error, not “file missing”.
- No channel writer: retain staged source; do not fall back to Saved Messages.
- Canonical media mismatch or incomplete canonical channel metadata: fail closed.
- Legacy Saved Messages metadata: keep compatibility path.
- Target/account CAS conflict during single-message create or group preflight: replan only because the send barrier proves zero Telegram messages have been produced.
- Topology change after group send phase begins: keep the frozen group; do not replay completed children or generate a replacement group.
- CAS conflict after RPC: reconcile metadata; never blind resend.
- Send outcome with no verifiable destination result: mark `uncertain`, retain generation-owned staged source and frozen identity, surface recovery-required state, and do not enqueue replacement upload.
- Same-path overwrite during pending transfer: create a later immutable generation; never overwrite/delete the older source and never let older cleanup touch the newer generation.
- MOVE/DELETE against a nonterminal staged logical item: fail closed with `423 Locked`; do not retarget or cancel the operation implicitly.

## 20. Tests

Add unit/integration coverage for: single-flight refresh; refresh fallback login; primary-only Saved Messages including albums; current channel reader/writer selection; peer isolation; canonical location parsing; incomplete-channel fail-closed; legacy Saved Messages fallback; location-version cache keys; media kind/id/size/photo-variant mismatch; durable intent-before-send; result-before-registration; reconcile CAS without resend; recovery from backend result; recovery from exact local result; uncertain persistence across restarts; no replay by same random id; uncertain split blocking group registration; target-aware dedup; Saved Messages → channel location switch; other-channel candidate rejection.

Generation-specific tests are mandatory:

1. stage A, interrupt after send/result but before final cleanup, stage B at the same logical path, recover A, and prove A cleanup removes only A's immutable source while B's source/queue record remain intact;
2. prove B does not begin sending/registering before older generation A reaches a terminal state;
3. prove an uncertain A blocks B rather than allowing commit-order inversion;
4. prove pending MOVE and DELETE return the locked/conflict path without mutating transfer identity or deleting bytes;
5. run the same ownership checks for `/game` pack generations.

Group-barrier tests are mandatory:

1. for a split upload, every child operation-create succeeds and every local cursor is persisted before the first Telegram send call;
2. if part 1 operation-create succeeds and part 2 create hits a topology conflict, send count remains zero and whole-group preflight may restart from a fresh snapshot;
3. after part 1 has successfully sent, change target/accounts topology before part 2; assert the original group/child operation IDs stay fixed, part 1 send count remains one, and no whole-group replan/replay occurs;
4. album `SendMultiMedia` starts only after all child intents satisfy the same barrier;
5. multi-part relocation creates all forward intents before the first forward.

Fake-backend integration must cover target/accounts changes during planning, migration while listings remain cached, channel reads through non-uploader accounts, mixed-uploader split parts in one channel, same-path overwrite across recovery, and the major crash windows around send/result/register.

Existing CRUD, protocol boundary, limiter, album, thumbnail, split, ZIP, shell warm, directory cache, `/game`, and live parity regressions remain mandatory.

## 21. Rollout order

### Phase 1 — canonical reads
Location models, fresh physical lookup, channel peer resolution/read failover, versioned caches. No new channel writes yet.

### Phase 2 — auth/topology
`/auth/refresh`, single-flight renewal, storage-target snapshot, primary-only Saved Messages routing, channel writer discovery.

### Phase 3 — durable writes
Operation API, stable random ids, target-aware sends, generation-owned staging/recovery identity, group preflight barrier, result reconciliation, single/group registration, durable albums and `/game`.

### Phase 4 — recovery and dedup relocation
Evidence-based reconciliation, persistent uncertain state, generation-safe cleanup, channel-aware dedup, Saved Messages → channel relocation, location-switch CAS.

### Phase 5 — cleanup
Remove canonical dependence on account-only `RemotePart`, bump incompatible physical caches, update docs, supersede conflicting assumptions in the 2026-09-05 design.

## 22. Acceptance criteria

1. Legacy Saved Messages files remain readable.
2. Channel-backed files are readable and can fail over to a non-uploader account.
3. Physical-location changes require no bridge restart and cannot reuse stale `location_version` byte caches.
4. New Saved Messages uploads, including albums, use primary only.
5. New channel uploads may use all currently verified local writers.
6. No new Telegram message is sent before durable operation intent exists; no new-message path commits through legacy direct registration.
7. Successful Telegram sends are never blindly repeated because backend metadata commit raced or failed.
8. Split logical registration waits for every durable child result.
9. Split/album/relocation groups create every child intent before the first message-producing RPC; preflight conflicts are replannable only while send count is zero.
10. After any child send begins, topology changes do not cause whole-group replan or replay of completed children.
11. Every staged payload has persistent generation/transfer identity and immutable source ownership.
12. Recovering an older generation can never delete or mark complete a newer same-path overwrite.
13. Same-path generations commit in FIFO order; an uncertain older generation blocks later sends until explicit resolution.
14. MOVE/DELETE against nonterminal staging fails closed rather than retargeting/canceling recovery state.
15. Dedup never reuses content from an incompatible target.
16. Saved Messages → channel dedup relocation performs CAS-protected physical switch.
17. Concurrent expired-JWT requests create one refresh/login flow.
18. Telegram bytes remain local and never pass through FastAPI.
19. Existing WebDAV/rclone/Range/cache/thumbnail/limiter/ZIP/`/game` regressions remain green.
20. Recovery completes without another send when backend/local exact result evidence exists; otherwise the item remains uncertain with its exact generation-owned staged bytes intact across restarts.

## 23. Non-goals

This change does not add a storage-channel UI, create Telegram channels, link/unlink accounts, run bulk Saved Messages migration, proxy Telegram bytes through FastAPI, remove local staging, duplicate files across destinations, redesign rclone/VFS, or rewrite unchanged upload protocol/limiter behavior. It also does not implement deferred MOVE/DELETE for an in-flight staging generation; baseline behavior is to return `423 Locked` until the generation is terminal.