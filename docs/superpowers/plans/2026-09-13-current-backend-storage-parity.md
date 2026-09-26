# TeleDrive WebDAV Current Backend Storage Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Use the approved design as the source of truth when this plan is ambiguous.

**Goal:** Update `yoyotsao/teledrive-webdav` so canonical Telegram reads, storage-target routing, uploads, albums, deduplication, crash recovery, COPY semantics, authentication, thumbnails, and `/game` behavior conform to the current TeleDrive backend storage contract without stale-generation cleanup or split-group replay hazards.

**Architecture:** Separate logical WebDAV metadata from canonical Telegram physical location. Resolve physical location at byte-open time, route Saved Messages through the exact storage account and shared-channel reads/writes through account-local peers, and move every new message-producing write behind the backend Telegram-operation journal. Staged writes gain generation-owned immutable sources, and split/album/relocation groups gain an all-child intent barrier before any Telegram send. Preserve current streaming, limiter, staging, rclone, and `/game` behavior.

**Tech Stack:** Python 3.11+, Telethon, requests, WsgiDAV, pytest, Pillow, ffmpeg, rclone/WinFsp.

**Spec:** `docs/superpowers/specs/2026-09-13-current-backend-storage-parity-design.md`

**Implementation baseline:** `yoyotsao/teledrive-webdav` master at `6a2f3dbbde612317cbdeb8b097258ea74b8fa065` when this branch was created.

## Global constraints

- Never send Telegram sessions, auth keys, channel peer access hashes, file bytes, thumbnails, or other binary payloads to the FastAPI backend.
- Document/file `access_hash` is valid media metadata; channel peer access hashes stay account-local.
- The backend is authoritative for linked accounts, storage target, topology versions, canonical file location, `location_version`, Telegram operation state, and `result_version`.
- Existing files are always read from their canonical location, never from the current `/storage-target`.
- Incomplete canonical channel locations fail closed; do not fall back to Saved Messages.
- Legacy Saved Messages rows remain readable through the existing compatibility path.
- Every new message-producing WebDAV write uses durable Telegram operations, including albums and `/game`.
- New Saved Messages writes always use the primary account. This is an intentional WebDAV normalization.
- Shared-channel writes may use all usable backend-linked local accounts that can write the frozen channel.
- Channel peers/entities/access hashes are account/session-local and must never be reused across accounts.
- Never blindly resend a Telegram operation whose outcome is uncertain.
- `random_id` is not a historical message locator.
- If neither the backend nor local state has a verifiable exact destination result, persist `uncertain`, retain staged bytes, and exclude the item from ordinary retries.
- Every staged payload has persistent `transfer_id`, per-logical-key `staging_generation`, and generation-specific immutable `source_path`.
- Same-path generations commit FIFO. A newer overwrite may stage while an older transfer is pending, but it must not send/register ahead of the older nonterminal generation.
- Cleanup deletes only the exact immutable source owned by the completing `transfer_id + staging_generation`; it never deletes by current logical path.
- MOVE/DELETE of a logical item with any nonterminal staging generation fail closed with `423 Locked`; they do not retarget/cancel the durable transfer.
- Split, album, and multi-part relocation groups create and persist every child intent before any child message-producing RPC may start.
- A topology conflict is whole-group replannable only during preflight while send count is provably zero. After any child send begins, the frozen group/target/child operation IDs stay fixed and completed children are never replayed.
- Preserve current upload protocol boundaries, sampled fingerprint format, limiter behavior, Range reads, rclone/VFS integration, and `/game` ZIP_STORED behavior.
- Default tests remain offline; live Telegram/backend acceptance is opt-in.

## Target file structure

- Modify `transfer_models.py`: canonical location, frozen target, durable operation/result/cursor models, staging generation identity, group send-barrier state, recovery queue states.
- Modify `tdapi.py`: canonical metadata parsing, fresh physical lookup, JWT refresh, storage-target/account snapshots, durable-operation API, location-switch API.
- Modify `telegram_accounts.py`: exact Saved Messages routing, account-local channel peer/access resolution, reader/writer selection.
- Modify `tgio.py`: explicit peers, canonical media validation, target-aware sends, stable random IDs, relocation forwarding.
- Modify `tgupload.py`: random-ID helpers while preserving byte upload behavior.
- Modify `upload_engine.py`: frozen topology, target-aware dedup, group preflight barrier, durable send orchestration, CAS reconciliation, group registration, relocation.
- Create `operation_state.py`: atomic local transfer/group state keyed by logical key + generation + transfer ID and shared by ordinary PUT and `/game`.
- Modify `uploadstage.py`: generation allocation, immutable sources, same-key FIFO, pending mutation lock, uncertain guard, recovery-before-retry, generation-safe cleanup.
- Modify `gamestage.py`: same generation/cursor/uncertain rules for `/game` pack units.
- Modify `bridge.py`: fresh physical resolution at open time, canonical physical cache keys, pending MOVE/DELETE lock surface.
- Modify `warmup.py` / `zipfs.py` where callers assume account/file-id physical identity.
- Add focused tests for storage locations, auth refresh, target routing, channel routing, cache identity, durable operations, recovery, staging generations, group barriers, and dedup relocation.

---

## Phase 0 — Lock the contract and baseline

### Task 0: Commit the design and implementation plan

**Files**
- Create `docs/superpowers/specs/2026-09-13-current-backend-storage-parity-design.md`
- Create `docs/superpowers/plans/2026-09-13-current-backend-storage-parity.md`

Before implementation changes, run the current offline baseline:

```powershell
.venv\Scripts\python.exe -m pytest tests -q
```

Initial docs commit:

```text
docs: define current backend storage parity
```

---

## Phase 1 — Canonical reads

### Task 1: Add canonical physical-location and recovery identity types

**Files:** modify `transfer_models.py`; create `tests/test_storage_location.py`; create `tests/test_staging_generation.py`.

Add `FileLocation`, `LegacySavedMessagesLocation`, `ResolvedRemotePart`, `FrozenStorageTarget`, `DurableOperationIdentity`, `DurableSendResult`, `DurableOperationCursor`, `StagingIdentity`, `GroupSendManifest`, `physical_location_key()`, plus `QueueStage.RECOVERING` and `QueueStage.UNCERTAIN`.

`StagingIdentity` contains exactly:

```python
@dataclass(frozen=True)
class StagingIdentity:
    logical_key: str
    transfer_id: str
    staging_generation: int
    source_path: str
```

`GroupSendManifest` contains the stable `group_id`, frozen target/version fields, ordered child operation IDs/random IDs/uploaders, `send_armed`, and `send_started`. It never stores Telegram sessions, peer access hashes, JWTs, or payload bytes.

Tests first: cache key changes with `location_version`; Saved Messages key includes storage account; legacy location identity stays explicit; recovery states exist; two generations at the same logical key have different transfer/source identities; group manifest child order is stable. Canonical cache identity must include target kind/id, message id, media kind/id/size/photo variant, and location version. `file_id` is not canonical media identity.

Verify:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_storage_location.py tests/test_staging_generation.py tests/test_routed_metadata.py tests/test_split_math.py -q
```

Commit: `feat: add canonical location and staging identity models`.

### Task 2: Parse canonical backend locations and refresh physical metadata at byte-open

**Files:** modify `tdapi.py`, `tests/test_routed_metadata.py`, `tests/test_dir_cache.py`; extend `tests/test_storage_location.py`.

Add `parse_file_location(row)`, `LocationMetadataError`, `TeleDriveClient.current_file_row(file_id)`, and `TeleDriveClient.current_parts(entry)`.

Behavior:
- non-split open uses fresh `/files/{file_id}/download`;
- split open fetches current split rows and sorts by `part_index`;
- incomplete canonical channel metadata fails closed;
- canonical Saved Messages requires exact account;
- pre-schema rows use explicit legacy fallback;
- listing caches may remain stale for names but cannot be byte-routing authority;
- bump incompatible on-disk physical cache schemas.

Verify:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_storage_location.py tests/test_routed_metadata.py tests/test_dir_cache.py tests/test_sizes.py -q
```

Commit: `feat: resolve fresh canonical telegram locations`.

### Task 3: Add account-local channel resolution and canonical media validation

**Files:** modify `telegram_accounts.py`, `tgio.py`, `tests/test_account_routing.py`, `tests/test_photo_media.py`; create `tests/test_channel_routing.py`.

Add `ChannelAccess`, account/session generation-aware channel peer cache, `TelegramAccountPool.read_routes(location)`, `TelegramAccountPool.channel_writers(channel_id, linked_ids)`, and `TelegramWorker.resolve_channel_access(channel_id)`.

Rules:
- Saved Messages reads use exact account + `me`;
- channel reads resolve peers independently per account;
- account-local resolution/read failures may fail over;
- canonical media kind/id/size/photo-variant mismatch fails globally;
- relogin/session replacement invalidates peer/access cache generation;
- if no channel route yielded, raise a routing/access error rather than returning an empty iterator silently.

Verify:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_channel_routing.py tests/test_account_routing.py tests/test_photo_media.py tests/test_read_pace.py -q
```

Commit: `feat: add shared channel read routing`.

### Task 4: Move bridge byte caches and ZIP identity to canonical locations

**Files:** modify `bridge.py`, `zipfs.py`, `warmup.py`, `tdapi.py`, `tests/test_bridge_e2e.py`, `tests/test_thumbnails.py`, `tests/test_zipfs.py`; create `tests/test_location_cache.py`.

`Resolver.open_remote(entry)` must fetch current physical parts immediately before opening bytes. Build stable physical-set identity from ordered canonical locations. Thumbnail, media-property, head, document/reference, split-part, and ZIP-source caches use canonical identity including `location_version`. Foreground thumbnail/property RPCs refresh current physical location **before** cache lookup. Background prefetch refreshes physical rows. Migration while mounted must create a new byte-cache identity without `/rpc/forget`.

COPY refreshes source location and copies canonical location fields; it never derives physical location from `/storage-target` and never performs relocation.

Verify:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_location_cache.py tests/test_bridge_e2e.py tests/test_thumbnails.py tests/test_zipfs.py tests/test_dir_cache.py -q
```

Commit: `feat: bind reads and caches to canonical locations`.

---

## Phase 2 — Authentication and topology

### Task 5: Add process-wide single-flight JWT refresh

**Files:** modify `tdapi.py`, `tests/test_auth_challenge.py`, `tests/test_backend_retry.py`; create `tests/test_auth_refresh.py`.

Keep thread-local `requests.Session` transports and process-wide JWT state. On 401, capture the token actually sent. If another thread already replaced it, retry with current token. Otherwise exactly one caller performs `/auth/refresh`; other threads wait. Persist refreshed token atomically. If refresh grace is rejected, run exactly one process-wide bot-challenge login. Never log JWT contents.

Verify:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_auth_refresh.py tests/test_auth_challenge.py tests/test_backend_retry.py tests/test_log_noise.py -q
```

Commit: `feat: refresh backend jwt with single flight`.

### Task 6: Freeze storage topology and route writers by target

**Files:** modify `tdapi.py`, `telegram_accounts.py`, `upload_engine.py`, `tests/test_account_pool.py`, `tests/test_account_routing.py`; create `tests/test_storage_target.py`.

Add `get_storage_target()`, `list_accounts()`, `freeze_storage_target()`, exact-primary Saved Messages admission, and current-access channel writer enumeration.

Freeze storage mode, channel id, target/accounts versions, linked account ids, primary id, and target peer key. Stored backend verifications are audit only. Saved Messages new writes—including albums—are primary-only. Channel writes may use all live backend-linked local accounts that currently verify write access. In-flight operations never retarget after settings change.

Verify:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_storage_target.py tests/test_account_pool.py tests/test_account_routing.py tests/test_upload_scheduler.py -q
```

Commit: `feat: freeze storage topology for uploads`.

---

## Phase 3 — Durable writes

### Task 7: Add durable Telegram-operation REST methods

**Files:** modify `tdapi.py`; create `tests/test_durable_operations.py`.

Implement exact wrappers for `/telegram-operations`, operation get/list/patch, `/reconcile-result`, single/group register, and single/group location switch. Preserve backend field names exactly. `/files/register` remains only for non-message-producing compatibility/alias operations.

Verify:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_durable_operations.py tests/test_backend_retry.py tests/test_routed_metadata.py -q
```

Commit: `feat: add telegram operation api client`.

### Task 8: Make Telegram message RPCs target-aware and deterministic

**Files:** modify `tgupload.py`, `tgio.py`, `transfer_models.py`, `tests/test_upload_parts.py`, `tests/test_upload_album.py`; extend `tests/test_channel_routing.py`.

Add signed-int64 stable random-id helpers, explicit target peer + random id to ordinary sends, explicit peer + one random id per album child, and durable forwarding of existing messages. Message-producing functions must not generate their own random id once an operation exists. Return canonical destination message/media identity for reconciliation.

Verify:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_upload_parts.py tests/test_upload_album.py tests/test_channel_routing.py tests/test_photo_media.py -q
```

Commit: `feat: make telegram sends target aware`.

### Task 9: Add generation-owned staging, atomic operation state, and split group preflight

**Files:** create `operation_state.py`, `tests/test_operation_recovery.py`, `tests/test_staging_generation.py`, `tests/test_group_send_barrier.py`; modify `transfer_models.py`, `upload_engine.py`, `uploadstage.py`, `gamestage.py`, `bridge.py`, `tests/test_upload_engine.py`, `tests/test_upload_scheduler.py`, `tests/test_bridge_e2e.py`.

#### 9.1 Persist immutable staging identity

For each accepted ordinary PUT or `/game` pack, allocate under the queue lock:

```text
logical_key
transfer_id = UUID
staging_generation = previous generation for logical_key + 1
source_path = generation-specific immutable staged file
```

Do not overwrite a previous generation's source file. Persist without credentials: staging identity, frozen target ids/versions, group/operation ids, uploader ids, stable target peer keys, stable random ids, RPC kinds, request metadata, operation versions/states, exact destination results when known, result versions, and uncertain reasons. Every mutation is atomic.

Recovery keys:

```text
upload:<normalized destination path>:<staging_generation>:<transfer_id>
game:<top-level pack unit>:<staging_generation>:<transfer_id>
```

The scheduler keeps a durable FIFO per `logical_key`. Only the oldest nonterminal generation may enter send/registration. A later overwrite is accepted and staged as a new generation but waits behind the older one. If the head generation is `uncertain`, later generations stay staged and unsent until the head is recovered or explicitly operator-tombstoned/abandoned.

Generation-safe completion:

```python
def can_cleanup(record, current_record):
    return (
        record.logical_key == current_record.logical_key
        and record.transfer_id == current_record.transfer_id
        and record.staging_generation == current_record.staging_generation
        and record.source_path == current_record.source_path
    )
```

Actual implementation may structure the lookup differently, but cleanup must enforce the same four-field ownership check and unlink only `record.source_path`, never the current logical staging path.

Pending MOVE/DELETE behavior is deliberately conservative: if any generation for the logical item is nonterminal, the WebDAV operation returns `423 Locked` and does not rename, retarget, cancel, unlink, or rewrite the durable transfer state.

#### 9.2 Build the split group barrier

For a split transfer, freeze target once and build all child plans first. Use one `group_id`. Before sending part 1:

```text
for every child part:
    allocate operation_id + random_id + uploader
    POST /telegram-operations with the same frozen target/accounts versions
persist complete ordered group manifest
transition every child to sending
persist every child cursor/version
set send_armed = true
only now allow any Telegram message-producing RPC
```

If child N operation-create returns a topology conflict before `send_armed`, send count must still be zero. Mark already-created unsent child intents tombstoned/aborted with `group_preflight_conflict` where supported, persist the aborted manifest, refetch topology, allocate a new group/child operation set, and retry preflight.

Once `send_armed` is true, the group identity is immutable. Once any child send starts, set `send_started = true` before allowing another scheduler path to replan. Target/accounts changes after part 1 succeeds do not permit whole-group replan, replacement operation for part 1, or replay. An unsent child whose frozen writer is unavailable becomes blocked/recovery-required under the same group; it does not silently switch to a new target/group.

Fresh single-message path remains:

```text
freeze target
→ choose verified writer/peer
→ upload raw file parts if needed
→ create operation_id + random_id
→ POST /telegram-operations
→ mark sending
→ save generation-owned local cursor
→ Telegram send
→ atomically save exact destination result locally
→ reconcile-result
→ save result_version
→ register
```

For split, the child-create/send steps are replaced by the group barrier above and group registration happens only after all child results are durable.

#### 9.3 Mandatory regression tests

Add these exact cases before implementation:

```text
A interrupted → stage B at same path → recover A
assert A cleanup deletes only A immutable source
assert B source and queue record survive
assert B does not send before A terminal
```

```text
split child 1 intent created → child 2 create topology conflict
assert telegram_send_count == 0
assert group is aborted/replanned only from preflight
```

```text
all split intents created → part 1 sent successfully → topology changes
assert part 1 send_count == 1
assert original group_id and child operation ids are unchanged
assert no whole-group replan or replay occurs
```

Also test pending MOVE and DELETE return the locked path without mutating source/cursor identity, and repeat generation ownership for `/game`.

Verify:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_staging_generation.py tests/test_group_send_barrier.py tests/test_operation_recovery.py tests/test_upload_engine.py tests/test_upload_scheduler.py tests/test_bridge_e2e.py -q
```

Commit: `feat: journal generation safe durable uploads`.

### Task 10: Put albums behind the same group barrier

**Files:** modify `upload_engine.py`, `tgio.py`, `operation_state.py`, `tests/test_upload_album.py`, `tests/test_group_send_barrier.py`, `tests/test_upload_engine.py`.

Every logical album child owns an operation. Before one `SendMultiMedia` RPC begins, every child operation exists under the same frozen group, every child is `sending`, every local cursor/random id is persisted, and `send_armed` is true. Saved Messages albums are primary-only. Channel album batches are pinned to one verified writer for the batch. Persist/reconcile every returned child independently. Partial/ambiguous bulk responses after possible success make unresolved children `uncertain`; preserve any known exact child results and do not blindly fall back to individual sends.

A topology conflict during album child operation creation aborts preflight with zero Telegram sends. A topology change after `SendMultiMedia` starts never causes group recreation or fallback replay.

Verify:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_upload_album.py tests/test_group_send_barrier.py tests/test_upload_engine.py tests/test_operation_recovery.py -q
```

Commit: `feat: make album uploads durable`.

---

## Phase 4 — Recovery and dedup relocation

### Task 11: Implement evidence-based restart recovery and generation-safe cleanup

**Files:** modify `operation_state.py`, `upload_engine.py`, `uploadstage.py`, `gamestage.py`, `bridge.py`, `tests/test_operation_recovery.py`, `tests/test_staging_generation.py`, `tests/test_group_send_barrier.py`, `tests/test_bridge_e2e.py`.

Recovery order:

```text
load generation-owned local cursor/group manifest
→ GET backend operation
→ if backend has result_version: resume register/switch in same generation/group
→ else if local cursor has exact destination id + canonical media identity:
     fetch that exact message from frozen destination
     validate media kind/id/size/photo variant
     reconcile-result
     register/switch
→ else mark/persist uncertain and stop without sending
```

Never scan history by filename/timestamp/size/similarity. Never treat random id as a direct lookup key. Recovery never performs a message-producing RPC. One uncertain split child blocks group registration and cleanup. Repeated restarts must not increase send count.

After successful logical registration, delete only the immutable source owned by that `transfer_id + staging_generation`. Re-read/lock queue state before unlink. If a newer generation exists for the same logical key, it remains untouched and becomes schedulable only after the older generation reaches terminal state.

On reconcile 409 after send: fetch current operation; accept identical durable result if `result_version`/destination identity already match; otherwise retry metadata CAS only if still safe; never resend because operation metadata advanced. Do not rely on a hard-coded terminal state name when a durable `result_version` is authoritative.

After restart, a group manifest with any child already in/after message-producing phase remains pinned to its original group and frozen target. Topology changes are not grounds for rebuilding the group.

Verify:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_operation_recovery.py tests/test_staging_generation.py tests/test_group_send_barrier.py tests/test_bridge_e2e.py tests/test_upload_scheduler.py -q
```

Commit: `feat: recover telegram operations without stale cleanup`.

### Task 12: Make dedup target-aware and add durable Saved Messages → channel relocation

**Files:** modify `upload_engine.py`, `tdapi.py`, `tgio.py`, `operation_state.py`, `tests/test_upload_dedup.py`, `tests/test_upload_engine.py`, `tests/test_group_send_barrier.py`; create `tests/test_dedup_relocation.py`.

Policy:
- Saved Messages target reuses only frozen-primary Saved Messages candidates.
- Channel target reuses only candidates already in the frozen channel.
- Complete Saved Messages candidate for channel target performs durable relocation.
- Other-channel/mixed candidates are rejected or replaced by fresh upload before relocation starts.

For a multi-part relocation, apply the same all-child preflight barrier: freeze one target/group, require each exact source account locally, resolve the target channel through each source account, create every `messages.forwardMessages` operation with stable random id before the first forward, persist the complete manifest, then arm sends. If create conflicts during preflight, zero forwards occurred and the group may replan. After one forward succeeds, topology changes do not permit replay or whole-group replacement.

Persist/reconcile each destination and require result versions. Once all parts are durable, switch single/group location with expected old `location_version` + operation/result versions. Logical metadata stays unchanged.

Verify:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_dedup_relocation.py tests/test_upload_dedup.py tests/test_group_send_barrier.py tests/test_upload_engine.py tests/test_operation_recovery.py -q
```

Commit: `feat: relocate dedup hits into shared storage`.

---

## Phase 5 — Integration and cleanup

### Task 13: Add cross-feature fake-backend acceptance coverage

**Files:** modify `tests/test_bridge_e2e.py`, `tests/test_thumbnails.py`, `tests/test_zipfs.py`, `tests/test_upload_album.py`, `tests/test_upload_scheduler.py`, `tests/test_staging_generation.py`, `tests/test_group_send_barrier.py`; create `tests/test_storage_parity_e2e.py`.

Required scenarios:
1. stale listing has Saved Messages v3, backend changes to channel v4, next open uses v4 without `/rpc/forget`;
2. uploader account unavailable, second account reads same channel file;
3. split channel file uses mixed uploaders but reads as one logical file;
4. single-message crash after operation creation before send: zero duplicates;
5. crash after `sending` with no destination evidence: uncertain, no resend;
6. crash after local destination persistence before backend reconciliation: exact-message recovery, no resend;
7. crash after backend result before register: registration resumes, no resend;
8. one uncertain split child blocks group registration/cleanup;
9. relocation durable before switch: restart completes CAS switch without another forward;
10. legacy pre-location-schema rows remain readable;
11. A is interrupted, B overwrites the same WebDAV path, A later recovers/registers/cleans up, and B's immutable source plus queue generation survive intact;
12. pending MOVE and DELETE return `423 Locked` and do not mutate the transfer identity;
13. split child 1 create succeeds but child 2 create hits target/accounts conflict: send count stays zero and whole-group preflight is the only replannable unit;
14. split part 1 succeeds, target/accounts change, part 2 remains in the original frozen group: part 1 is not resent and no replacement group is generated;
15. album and multi-part relocation both prove all child intents exist before their first message-producing RPC.

Verify full offline suite:

```powershell
.venv\Scripts\python.exe -m pytest tests -q
```

Commit: `test: cover current backend storage parity`.

### Task 14: Remove transitional assumptions and update docs

**Files:** modify `transfer_models.py`, `tdapi.py`, `bridge.py`, `tgio.py`, `uploadstage.py`, `gamestage.py`, `README.md`, `UPLOAD_DOWNLOAD_COMPARISON.md`, `CLAUDE.md`; add only a supersession note to the 2026-09-05 design.

Audit:

```powershell
rg "RemotePart|telegram_user_id.*message_id|InputPeerSelf|send_file\(\"me\"|/files/register|split_parts.json|upload:<normalized destination path>$|game:<top-level pack unit>$" .
```

Remaining matches must be legacy compatibility, metadata-only alias/COPY, historical docs/tests, or explicit examples showing what not to use. Remove canonical dependence on account-only `RemotePart`; bump incompatible physical cache namespaces; document current storage target, primary-only Saved Messages writes, channel routing, durable operation lifecycle, staging generations, FIFO overwrite behavior, pending MOVE/DELETE lock, group preflight barrier, `uncertain`, and `/game` durable sends.

Verify:

```powershell
.venv\Scripts\python.exe -m pytest tests -q
git diff --check
```

Commit: `docs: complete backend storage parity rollout`.

---

## Live acceptance gate

Run only with explicit permission and real backend/accounts:

```powershell
$env:TD_PARITY_FOLDER = "_storage-parity-probe"
$env:TD_PARITY_MAX_BYTES = "1200000000"
.venv\Scripts\python.exe -m pytest tests/live -q
```

Extend the live matrix to cover primary-only Saved Messages ordinary/album/split uploads; channel ordinary/split writes; multi-writer channel sends; reads after uploader goes offline; mixed-uploader split reads; live Saved Messages → channel migration without bridge restart; crash after local result before reconcile; crash after backend result before register; crash after Telegram acceptance with no destination evidence remaining uncertain across repeated restarts with zero duplicate messages; same-path A→B overwrite while A is interrupted with B surviving A recovery/cleanup; and a topology change after the first split part succeeds without replaying or rebuilding the group.

## Completion gates

**Phase 1:** legacy Saved Messages, canonical Saved Messages, and channel-backed reads work; `location_version` invalidates physical caches; migration coexistence needs no restart; staging/group identity types exist.

**Phase 2:** concurrent expired JWT requests create one refresh/login flow; Saved Messages new writes are primary-only; channel writers use current local permission checks.

**Phase 3:** every ordinary/split/album/`/game` message send has durable intent before send; every staged payload has generation-owned immutable bytes; split/album groups cannot send until every child intent is durable; logical registration only uses durable results.

**Phase 4:** uncertain operations cannot blind-resend; recovery cleanup cannot delete a newer overwrite; in-flight groups cannot be replanned after child send begins; dedup relocation uses durable forwards and CAS-protected location switch.

**Phase 5:** offline suite and final audit pass; canonical paths no longer depend on account-only physical identity; live matrix passes when authorized.

## Final acceptance checklist

- [ ] Legacy Saved Messages files remain readable.
- [ ] Canonical Saved Messages files use exact storage account.
- [ ] Channel files can be read by a non-uploader account.
- [ ] Canonical media kind/id/size/photo variant is validated before bytes are exposed.
- [ ] Incomplete canonical channel metadata fails closed.
- [ ] Byte-open refreshes physical location.
- [ ] `location_version` changes physical cache identity.
- [ ] Thumbnail/property/head/ZIP reads use canonical routing.
- [ ] New Saved Messages writes use primary only, including albums.
- [ ] Channel uploads may fan out over verified writers while sharing one physical channel.
- [ ] Channel peer/access hashes never cross account/session boundaries.
- [ ] Concurrent 401s produce one refresh/login flow.
- [ ] Every new Telegram message has durable intent before send.
- [ ] Every staged payload has persistent `transfer_id`, `staging_generation`, and immutable `source_path`.
- [ ] Same-path overwrite creates a newer generation instead of overwriting the older recovery source.
- [ ] Older-generation recovery/cleanup cannot unlink or complete a newer generation.
- [ ] Same-path generations enter send/registration FIFO; an uncertain head blocks later generations.
- [ ] Pending MOVE/DELETE fail closed with `423 Locked` without mutating durable transfer identity.
- [ ] Exact destination result is locally persisted before backend reconciliation when available.
- [ ] Successful sends are not repeated after metadata CAS conflict.
- [ ] Split/album/relocation groups create all child intents before the first message-producing RPC.
- [ ] A child create conflict during group preflight occurs with zero sends and can replan the whole preflight safely.
- [ ] After any child send begins, topology changes do not rebuild the group or replay completed children.
- [ ] Split registration waits for all durable child results.
- [ ] Album sends use durable child operations and stable random ids.
- [ ] `/game` uses the same generation-owned cursor, FIFO, and uncertain guard.
- [ ] Missing destination evidence stays `uncertain` across restarts.
- [ ] Recovery performs no message-producing RPC.
- [ ] Dedup reuse requires target compatibility.
- [ ] Saved Messages → channel relocation uses durable forwarding, the group barrier for multiple parts, and location-switch CAS.
- [ ] COPY copies current canonical physical location instead of deriving it from `/storage-target`.
- [ ] Committed-file MOVE/rename are metadata-only; trash/delete remain soft-delete.
- [ ] Telegram payload bytes never cross FastAPI.
- [ ] Existing CRUD, Range/rclone, limiter, album, thumbnail, ZIP, `/game`, and live-parity behavior remain intact.