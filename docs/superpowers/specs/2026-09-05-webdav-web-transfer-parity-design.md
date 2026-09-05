# WebDAV / Web transfer parity design

**Date:** 2026-09-05  
**Status:** Approved
**Source analysis:** `UPLOAD_DOWNLOAD_COMPARISON.md`

## Goal

Make `teledrive-webdav` produce and consume the same Telegram messages and
TeleDrive metadata as the Web client, including multi-account routing,
concurrency, media albums, segmentation, deduplication checks, thumbnails, and
rate limiting.

Parity is defined at the Telegram and TeleDrive backend boundary. The WebDAV
product keeps the behaviours required by its filesystem interface:

- every PUT is durably staged before asynchronous Telegram upload;
- the debounce queue survives process restarts;
- reads remain seekable WebDAV/Range streams backed by rclone's VFS cache;
- `/game/<directory>` remains a `ZIP_STORED` archive exposed as a virtual
  directory.

Those four behaviours have no Web equivalent and are not removed in pursuit of
parity.

## Non-goals

- Do not send file bytes through the TeleDrive FastAPI backend.
- Do not replace WebDAV Range reads with whole-file Blob assembly.
- Do not remove `/game` archive-as-folder semantics.
- Do not add a browser-profile or IndexedDB reader. Browser credentials are not
  a stable headless interface.
- Do not add upload transaction or orphan reconciliation backend APIs. Neither
  client currently has such a transaction, and it is outside transfer parity.
- Do not change the shared sampled fingerprint algorithm in this change.

## Shared constants and exact boundaries

The Web constants become the authoritative defaults for WebDAV:

| Behaviour | Value |
|---|---:|
| Per-account file slots | 3 |
| Per-account chunk slots | 12 |
| Hash workers | 2 |
| Hash-check requests | 8 |
| Register requests | 8 |
| Album batch | 10 files |
| Album send timeout | 60 seconds |
| Message rate | 3 messages/second |
| Message burst | 6 |
| Big-upload part size | 512 KiB |
| Parts per Telegram message | 1000 |
| Telegram message payload boundary | 524,288,000 bytes (500 MiB) |
| Small-file boundary | 10,485,760 bytes (10 MiB), inclusive |
| Sampled fingerprint | SHA-256(first 100 MiB) + `:` + byte size |

Exactly 10 MiB uses the small-file protocol. Exactly 500 MiB uses one Telegram
message and is not a logical split file. A 500 MiB + 1 byte file uses two
`SaveBigFilePart` segments; the one-byte tail does not fall back to
`SaveFilePart`.

## Credentials and account configuration

### Backward-compatible primary account

The existing `session` setting remains valid and is the primary account when no
account file is configured. Existing installations therefore continue to start
without a migration.

The primary account performs the TeleDrive bot-challenge login and owns the
drive JWT. Secondary accounts never authenticate separately to the backend;
they must already be linked to the primary drive through TeleDrive.

### Multi-account file

An optional `accounts_file` setting points to a local JSON file outside source
control. Its schema is:

```json
{
  "accounts": [
    {
      "telegram_user_id": 123456789,
      "label": "primary",
      "session": "Telethon StringSession"
    },
    {
      "telegram_user_id": 987654321,
      "label": "secondary",
      "session": "Telethon StringSession"
    }
  ]
}
```

Array order determines the primary account and the initial round-robin order.
On connection, the session's actual user ID must equal the configured
`telegram_user_id`; a mismatch disables that account and emits an error without
printing the session. Duplicate IDs are rejected.

When both the legacy session and an accounts file exist, the accounts file is
authoritative. A checked-in `accounts.example.json` documents the format; the
real default file under the configured data root is ignored by Git. Status and
logs may expose account ID and label but never a session string.

After primary authentication, configured secondary IDs are compared with the
backend's linked-account list. An unlinked account is excluded from new uploads.
If existing metadata references a configured but currently unlinked account,
reads are still attempted because preserving access to existing bytes is safer
than silently routing to the wrong primary account.

## Architecture

### Account manager

Introduce a `TelegramAccountPool` facade above per-account workers. Every
account owns independent instances of:

- one Telethon control client;
- its download connection pool;
- its dedicated upload client;
- a three-slot file semaphore;
- a twelve-slot upload-part semaphore;
- an adaptive chunk limiter;
- a message limiter;
- document/file-reference caches.

The pool provides:

- lookup by exact Telegram user ID;
- historical account ID `0` mapped to the primary account;
- round-robin upload selection, preferring an online account with a free file
  slot;
- one-account failure isolation;
- orderly startup and shutdown of all clients.

No request may silently fall back from a non-zero account ID to the primary.
Missing/offline routed accounts produce a readable error.

### Routed metadata types

Replace positional `(message_id, size)` part tuples with explicit data classes:

```python
@dataclass(frozen=True)
class RemotePart:
    message_id: int
    size: int
    telegram_user_id: int
    file_id: str

@dataclass(frozen=True)
class UploadedPart:
    index: int
    message_id: int
    file_id: str
    access_hash: str | None
    size: int
    telegram_user_id: int
    has_thumbnail: bool = False
```

`Entry`, `_to_entry()`, directory caches, split-part caches, upload results, and
registration payloads carry `telegram_user_id`. Historical rows with zero use
the primary account.

## Upload routing

After a staged file becomes due, it enters the same streaming decision pipeline
as a Web `File`. There is no whole-batch hash barrier:

1. Hash at most two files concurrently.
2. Check at most eight fingerprints concurrently.
3. Apply exact-size canonical deduplication.
4. Claim the fingerprint within the current due batch.
5. Route a fresh upload to an online account with an available file slot.
6. Release the file slot as soon as Telegram byte preparation finishes.
7. Send/group messages and register metadata outside the file slot.

If two staged files in the same due batch share a fingerprint, the first claims
it. Later files await the claimed upload and register aliases to the same
canonical parts. A failed claimant wakes followers with failure; no follower
starts a duplicate physical upload during that batch.

### Deduplication coverage

Port the Web `canonicalExistingParts(files, originalSize)` semantics exactly:

- group split rows by `split_group_id` and order by `part_index`;
- collapse duplicate database aliases and duplicate Telegram message IDs;
- treat a non-split row as one candidate;
- accept only a candidate whose part sizes sum exactly to the local file size;
- prefer a complete candidate deterministically;
- upload fresh bytes when no complete candidate exists.

Before any new registration, `assert_parts_cover_file()` verifies that uploaded
part sizes sum exactly to the staged file size.

### Protocol decision table

#### Non-album file at or below 10 MiB

Match the installed GramJS `sendFile(CustomFile, workers=4)` behaviour:

- use `SaveFilePart`;
- choose a 128 KiB part size for files below 100 MB;
- send at most four parts concurrently for that file;
- return `InputFile`;
- attach an optional uploaded thumbnail through `InputMediaUploadedDocument`;
- create one message with `SendMedia`/Telethon equivalent;
- register one non-split row.

The small-file upload keeps a valid MD5 checksum in `InputFile`. This is stricter
than GramJS's empty checksum but does not change Telegram or backend observable
behaviour.

#### Album-eligible media at or below 10 MiB

Eligibility matches the Web route: supported image/video MIME types, excluding
`image/webp`.

For each file under its account's file slot:

1. Capture or create its thumbnail.
2. Upload the original as 512 KiB `SaveFilePart` requests through the account's
   shared chunk limiter and chunk semaphore.
3. Upload the thumbnail through the same account limiter.
4. Call `messages.UploadMedia` with `InputMediaUploadedDocument`.
5. Release the file slot and enqueue the resulting `InputMediaDocument` in that
   account's album queue.

Each account has a separate queue because Telegram cannot group prepared media
owned by different accounts. Ten prepared items trigger a batch immediately;
the tail batch flushes when due-file discovery completes. One
`messages.SendMultiMedia` creates the final messages. Results are mapped back by
Telegram document ID, never by response order.

`SendMultiMedia` is bounded by 60 seconds. Error or timeout follows the Web
fallback exactly: re-read each source file and independently call the equivalent
of `sendFile(workers=1, forceDocument=true)` without a thumbnail. Successful
fallback rows therefore report `has_thumbnail=false`. Any prepared-but-uncommitted
upload may become an orphan, matching the Web client's current semantics.

#### File above 10 MiB and at or below 500 MiB

- plan one segment;
- use 512 KiB `SaveBigFilePart` requests;
- share the account's twelve chunk slots and adaptive limiter;
- create one document message;
- register one row with `is_split_file=false`.

#### File above 500 MiB

- plan all segments from the original file once;
- upload all segments concurrently;
- select an account independently for each segment using round-robin/free-slot
  preference;
- always use `SaveBigFilePart`, including a final segment at or below 10 MiB;
- sort results by the planned segment index;
- assert exact byte coverage;
- register all rows with one `split_group_id`, explicit `part_index`, and each
  segment's `telegram_user_id`.

Only segment zero may carry the logical file's thumbnail.

### Registration

Registration is bounded to eight concurrent POST requests. Every request sends
the storage account's `telegram_user_id`; the backend remains metadata-only.

For a logical split, all part registrations settle before the staged source is
removed. Partial backend success can still leave partial metadata because the
shared backend has no transaction endpoint. The staged source remains on any
failure and the retry policy applies to that logical file.

The metadata HTTP client uses thread-local `requests.Session` objects so
concurrent check/register calls do not share mutable connection-pool state. JWT
state and reauthentication remain process-wide and protected by the existing
authentication lock.

## Thumbnail parity

### Still images

Continue using Pillow to produce a JPEG thumbnail within Telegram's constraints
and include true image dimensions. Capture work participates in the three-file
pipeline instead of blocking the entire queue.

### Video

Attempt video-frame extraction with the configured or discoverable `ffmpeg`
binary. Produce the same bounded JPEG form used for still images. If the video
cannot be decoded, classify it as `undecodable` and allow an upload without a
thumbnail, matching the Web exception. A decodable media file whose thumbnail
generation fails is marked failed rather than silently registered without the
thumbnail.

### Split media and `/game`

Only segment zero of a split media file receives a thumbnail. `/game` directory
archives are ZIP documents and receive no media thumbnail. A top-level `/game`
media file follows the same thumbnail rules as another logical file.

## Rate limiting

Port the Web adaptive chunk limiter state machine and constants rather than
retuning the existing simpler `UploadGate`:

- initial 4 parts/s, minimum 0.5, maximum 12;
- twelve-part concurrency window;
- multiplicative decrease and additive increase;
- learned ceiling, first-flood backoff, slow zone, clean interval, probing,
  failed-probe cooldown, escalation, and de-escalation;
- a distinct `FLOOD_PREMIUM_WAIT` path that pauses without lowering the learned
  ordinary-flood ceiling;
- per-account persisted state under the WebDAV metadata directory;
- atomic state-file writes and corruption-tolerant reads.

Chunk pacing is per account and shared by small album uploads, thumbnails, and
large segments. Message-creating RPCs use a separate per-account token bucket of
3 messages/s with burst 6. A flood in one bucket or account must not penalize
another.

Non-flood part failures receive three attempts with 1-second then 2-second
backoff. Flood retry and disconnect handling retain the existing bounded
behaviour where it is stronger than the Web implementation, without changing
successful observable results.

## Durable staging and failure semantics

The durable queue wraps the Web-style transfer engine:

- `staging`: writes are still within debounce;
- `planning`: hash, dedup, thumbnail, and account selection;
- `uploading`: Telegram bytes or prepared media are in progress;
- `sending`: final message/album operation is in progress;
- `registering`: backend metadata is in progress;
- `failed`: retryable failure with the existing ten-minute delay;
- `abandoned`: five failed logical attempts; staged source retained.

Only complete Telegram message creation plus complete backend registration
removes a staged file. One file's failure does not cancel unrelated files or
other album batches. Restart adopts every remaining staged source and restarts
planning; part-level resume is not claimed.

`/rpc/status` reports configured/online accounts, limiter statistics, queue
stage, account assignment when known, part progress, and error detail. It never
reports session strings or JWTs.

## Multi-account read parity

Every remote read is routed with `(telegram_user_id, message_id, file_id)`:

1. Map account ID zero to primary; otherwise require the exact configured
   account worker.
2. Fetch the Saved Messages entry from that account.
3. Extract its document/photo identity.
4. Compare the returned Telegram media ID with expected `file_id`.
5. Refuse mismatches before returning any bytes.
6. Use that account's download pool for GetFile requests and file-reference
   refresh.

Split-part tables retain account ID and expected file ID per part. A logical
Range crossing segment boundaries may therefore route consecutive subranges to
different account workers while preserving the existing seekable stream API.

Directory, document, thumbnail, property, head, and split caches include account
ID in their key wherever Telegram message IDs would otherwise collide.

If metadata names an account whose session is absent or offline, WebDAV returns
a deterministic read error and logs the account ID. It must never try the same
message ID on another account.

## `/game` integration

`/game` keeps its archive rules. Once a top-level unit is packed, its resulting
file uses the same account dispatcher, protocol boundaries, adaptive limiter,
registration account field, coverage assertion, and retry engine as ordinary
uploads.

A packed directory is one logical ZIP and is not eligible for albums. Multiple
independent `/game` units may occupy file slots concurrently, but a single
unit's pack remains exclusive and atomic as today.

## Logging and observability

Add concise timing logs compatible with the Web performance logs:

- hash, hash-check, thumbnail, slot wait, byte upload, message/album, register,
  and total duration;
- account ID for every upload, message, flood, and routed read error;
- batch size for album sends;
- limiter rate, ceiling, window, and flood count at batch completion;
- protocol (`small`, `album`, `big`, `split`) and exact byte/part counts.

Logs must make queued versus actively-uploading files distinguishable. Existing
Telethon download-log throttling remains.

## Configuration changes

Add backward-compatible defaults to `Config` and `config.example.ini`:

```ini
[telegram]
accounts_file =
upload_files = 3
upload_parts = 12

[upload]
hash_concurrency = 2
hash_check_concurrency = 8
register_concurrency = 8
album_batch = 10
album_timeout_seconds = 60
message_rate = 3
message_burst = 6
ffmpeg =
```

Blank `accounts_file` means legacy single-account mode. Blank `ffmpeg` performs
normal executable discovery, including the existing `FFMPEG` environment
variable. Values are range-validated at startup and invalid values fail with a
clear configuration error.

## Test strategy

All tests are offline unless explicitly labelled as an optional live probe.
Production changes follow red-green-refactor.

### Pure protocol tests

- GramJS-compatible part-size vectors below 100 MB.
- 10 MiB, 10 MiB + 1, 500 MiB, 500 MiB + 1, and non-aligned tail vectors.
- Small `SaveFilePart` uses at most four file workers.
- Album upload uses 512 KiB parts and the shared twelve-slot account gate.
- Every segment of a logical split uses `SaveBigFilePart`.
- Segment results are restored by index, not message ID.
- Exact coverage accepts complete single/split candidates and rejects incomplete
  or over-counted candidates.

### Scheduler tests

- At most three files per account prepare concurrently.
- More accounts provide independent file and chunk budgets.
- Round-robin prefers an account with a free slot.
- Eleven eligible files become album batches of ten and one per account.
- Mixed-account prepared media never share an album.
- Batch-local duplicate files upload bytes once and register every alias.
- Hash, check, byte upload, and registration overlap without an all-batch
  planning barrier.

### Album and thumbnail tests

- `UploadMedia` receives file, attributes, dimensions, and thumbnail.
- `SendMultiMedia` response order is deliberately shuffled and maps by document
  ID.
- Timeout/error enters per-file fallback.
- WebP and media above 10 MiB never enter the album queue.
- Decodable image/video without a thumbnail fails.
- Undecodable video may upload without one.
- Only split segment zero carries a thumbnail.

### Account-routing tests

- Historical account zero routes to primary.
- Non-zero IDs route only to the exact worker.
- Duplicate message IDs on two accounts cannot cross-read.
- A returned document ID mismatch fails before GetFile.
- Split Range reads crossing accounts concatenate the correct byte ranges.
- Missing/offline account errors do not fall back to primary.
- Registration includes the storage account on every part.

### Limiter tests

- Web constant parity and initial/minimum/maximum rates.
- Ordinary flood decrease, clean ramp, ceiling slow zone, probe confirmation,
  failed-probe cooldown, escalation, and reset.
- Premium flood pauses but does not lower rate/ceiling.
- State persists separately per account and survives corrupt-state fallback.
- Message and chunk buckets are independent.
- Different accounts cannot cross-penalize.

### Durable queue and regression tests

- A successful file is deleted only after registration.
- Prepare, send, or register failure retains only affected staged sources.
- Restart adopts leftovers without duplicating completed metadata aliases.
- `/rpc/status` reports stages/accounts and contains no credentials.
- Existing WebDAV CRUD, `/game`, preview, property, Range, cache, authentication,
  retry, and split-size tests remain green.

### Optional live acceptance probe

With explicit permission and disposable test names, upload a matrix through both
clients and compare backend rows plus Telegram media:

- eleven JPEG files at or below 10 MiB;
- one WebP at or below 10 MiB;
- 10 MiB and 10 MiB + 1 byte files;
- 500 MiB and 500 MiB + 1 byte files;
- one decodable and one unsupported-codec video;
- one duplicate batch;
- one multi-account split.

The live probe is not part of the default test suite and never runs implicitly.

## Migration and compatibility

- Single-account installations require no new configuration.
- Existing rows with `telegram_user_id` missing/zero continue to use primary.
- Existing directory and split cache files are either schema-versioned and
  migrated or invalidated safely; no remote data is deleted.
- Existing staged files are adopted by the new scheduler.
- Existing rate state absent/corrupt starts from Web defaults.
- The implementation may add metadata cache files but does not rewrite backend
  rows merely to migrate local state.

## Security requirements

- Session strings remain local and are never sent to FastAPI.
- Account configuration and persisted JWT/rate files are excluded from Git.
- Exceptions and status output redact sessions, authorization headers, and JWTs.
- Secondary account registration is accepted only after the backend confirms it
  is linked to the authenticated primary drive.
- Telegram document ID validation prevents same-message-ID cross-account data
  substitution.

## Expected files

The implementation is expected to modify or add focused modules rather than
placing the entire pool and scheduler in existing large files:

- `config.py`, `config.example.ini`, `accounts.example.json`
- `tdapi.py`
- `tgupload.py` and a persisted limiter module
- `tgio.py` and an account-pool module
- `gamestage.py`, `uploadstage.py`, `bridge.py`, `warmup.py`
- upload, limiter, account-routing, thumbnail, split, and end-to-end tests
- `CLAUDE.md` operational documentation

Exact filenames for new modules are chosen in the implementation plan, but the
interfaces and behaviour above are fixed.

## Acceptance criteria

The change is complete when:

1. Every upload decision in the Web decision table has a WebDAV equivalent with
   the same thresholds, protocol family, concurrency, account selection,
   thumbnail rules, and registration fields.
2. Every file/part created by any configured Web account is readable through
   WebDAV using the recorded account, with expected document ID verification.
3. Exact dedup coverage and batch-local dedup match the Web planner.
4. Adaptive chunk and message rate control match Web constants and state
   transitions independently per account.
5. Durable staging, `/game`, seekable Range reads, and existing backward
   compatibility remain intact.
6. The complete offline suite passes with no live Telegram/backend traffic.
7. No existing uncommitted user change is overwritten or included in the design
   commit.
