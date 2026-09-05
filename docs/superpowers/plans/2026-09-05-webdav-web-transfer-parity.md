# WebDAV / Web Transfer Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make WebDAV uploads and routed reads produce and consume the same Telegram messages and TeleDrive metadata as the Web client while retaining durable staging, seekable Range reads, rclone caching, and `/game` ZIP semantics.

**Architecture:** Add explicit routed transfer models, a per-account worker pool, persisted per-account limiters, and a streaming upload coordinator above the existing Telethon worker. The coordinator owns protocol selection, deduplication, albums, concurrency, registration, and durable state; the existing bridge and `/game` paths consume the coordinator rather than implementing transfer policy themselves.

**Tech Stack:** Python 3.11+, Telethon, requests, Pillow, ffmpeg subprocesses, pytest, WsgiDAV, rclone/WinFsp.

**Spec:** `docs/superpowers/specs/2026-09-05-webdav-web-transfer-parity-design.md`

## Global Constraints

- Preserve durable staged PUTs, restart adoption, seekable Range reads, rclone VFS caching, and `/game` `ZIP_STORED` virtual directories.
- Never send a Telegram session string or file bytes to the FastAPI backend.
- Keep the sampled fingerprint as `SHA-256(first 100 MiB) + ":" + byte size`.
- Use 3 file slots/account, 12 chunk slots/account, hash concurrency 2, hash-check concurrency 8, registration concurrency 8, album size 10, album timeout 60 seconds, and message rate 3/s with burst 6.
- Exactly 10,485,760 bytes uses the small protocol; exactly 524,288,000 bytes is one Telegram message; 524,288,001 bytes is a two-segment split whose tail still uses `SaveBigFilePart`.
- Use 128 KiB `SaveFilePart` chunks and four workers for non-album files at or below 10 MiB; use 512 KiB parts for albums and every big-file segment.
- Route non-zero `telegram_user_id` only to the exact account. Map historical zero/missing account IDs to primary, and never fall back for another non-zero ID.
- Validate returned Telegram media ID against backend `file_id` before any byte or thumbnail read.
- Keep all default tests offline; live Telegram/backend probes require explicit user permission.
- Baseline is commit `0502bce`, where 276 Python tests and the temporary native build pass.

## File Structure

- Create `transfer_models.py`: immutable routed part, account, prepared-album, and transfer-result value types.
- Create `telegram_accounts.py`: account-file loading, worker lifecycle, exact-ID routing, linked-account filtering, and upload slot selection.
- Create `upload_limiter.py`: persisted adaptive chunk limiter and independent message token bucket.
- Create `media_thumbnail.py`: still-image and ffmpeg video thumbnail capture with explicit result classification.
- Create `upload_engine.py`: hash/check/register pipeline, batch-local claims, protocol routing, album queues, exact coverage, and timing/status events.
- Modify `config.py` and `config.example.ini`: validated parity settings and account/ffmpeg paths.
- Add `accounts.example.json`: documented multi-account schema without credentials.
- Modify `tdapi.py`: routed metadata, linked-account listing, thread-local sessions, and registration account IDs.
- Modify `tgupload.py`: small/big upload primitives using a shared per-account limiter.
- Modify `tgio.py`: expected-file validation, per-account upload RPCs, routed cache keys, and read primitives.
- Modify `gamestage.py`, `uploadstage.py`, `bridge.py`, and `warmup.py`: consume the pool/engine and expose durable status.
- Modify `.gitignore` and `CLAUDE.md`: protect local credential/state files and document operation.
- Add focused tests under `tests/` for configuration, routing, protocols, limiters, engine scheduling, albums, durable state, and end-to-end behavior.

---

### Task 1: Transfer Models and Validated Configuration

**Files:**
- Create: `transfer_models.py`
- Create: `accounts.example.json`
- Create: `tests/test_transfer_config.py`
- Modify: `config.py:31-181`
- Modify: `config.example.ini:1-45`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `AccountSpec`, `RemotePart`, `UploadedPart`, `PreparedAlbumItem`, `TransferResult`, and `QueueStage`.
- Produces: `Config.accounts_file`, `upload_files`, `upload_parts`, `hash_concurrency`, `hash_check_concurrency`, `register_concurrency`, `album_batch`, `album_timeout_seconds`, `message_rate`, `message_burst`, and `ffmpeg`.

- [ ] **Step 1: Write failing configuration and model tests**

```python
def test_parity_defaults(tmp_path, monkeypatch):
    cfg = load_minimal_config(tmp_path, monkeypatch)
    assert (cfg.upload_files, cfg.upload_parts) == (3, 12)
    assert (cfg.hash_concurrency, cfg.hash_check_concurrency) == (2, 8)
    assert (cfg.register_concurrency, cfg.album_batch) == (8, 10)
    assert (cfg.album_timeout_seconds, cfg.message_rate, cfg.message_burst) == (60.0, 3.0, 6)

def test_non_positive_concurrency_is_rejected(tmp_path, monkeypatch):
    path = write_config(tmp_path, "[upload]\nhash_concurrency = 0\n")
    with pytest.raises(ConfigError, match="hash_concurrency"):
        load_config(path)

def test_uploaded_part_requires_storage_identity():
    part = UploadedPart(0, 12, "991", None, 7, 44)
    assert (part.index, part.telegram_user_id, part.file_id) == (0, 44, "991")
```

- [ ] **Step 2: Run the focused tests and confirm missing fields/types fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_transfer_config.py -q`

Expected: FAIL because `transfer_models` and the new `Config` fields do not exist.

- [ ] **Step 3: Add immutable transfer types and range-checked configuration**

```python
class QueueStage(str, Enum):
    STAGING = "staging"
    PLANNING = "planning"
    UPLOADING = "uploading"
    SENDING = "sending"
    REGISTERING = "registering"
    FAILED = "failed"
    ABANDONED = "abandoned"

@dataclass(frozen=True)
class AccountSpec:
    telegram_user_id: int
    label: str
    session: str = field(repr=False)

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
    access_hash: Optional[str]
    size: int
    telegram_user_id: int
    has_thumbnail: bool = False

@dataclass(frozen=True)
class TransferRequest:
    source: Path
    upload_name: str
    mime_type: str
    parent_id: Optional[str]
    logical_size: int
    allow_album: bool = True

@dataclass(frozen=True)
class TransferResult:
    request: TransferRequest
    fingerprint: str
    parts: tuple[UploadedPart, ...]

@dataclass(frozen=True)
class PreparedAlbumItem:
    source: Path
    upload_name: str
    mime_type: str
    size: int
    telegram_user_id: int
    document_id: str
    access_hash: Optional[str]
    has_thumbnail: bool
```

Implement `positive_int(name, raw)`, `positive_float(name, raw)`, path resolution relative to `config.ini`, blank `accounts_file` fallback, and blank `ffmpeg` discovery input. Add `accounts.json`, `upload-rate-*.json`, and the resolved local accounts filename to `.gitignore`; keep `accounts.example.json` tracked.

- [ ] **Step 4: Run configuration tests and the existing config tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_transfer_config.py tests/test_config.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the configuration foundation**

```text
git add .gitignore accounts.example.json config.example.ini config.py transfer_models.py tests/test_transfer_config.py
git commit -m "feat: add transfer parity configuration"
```

### Task 2: Routed Backend Metadata and Thread-Local HTTP Sessions

**Files:**
- Create: `tests/test_routed_metadata.py`
- Modify: `tdapi.py:54-145,214-650`
- Modify: `tests/test_dir_cache.py`
- Modify: `tests/test_backend_retry.py`

**Interfaces:**
- Consumes: `RemotePart` from Task 1.
- Produces: `Entry.telegram_user_id: int`, `Entry.file_id: str`, `TeleDriveClient.parts_for(entry) -> list[RemotePart]`, `linked_account_ids() -> set[int]`, and `register(..., telegram_user_id: int)`.

- [ ] **Step 1: Add failing routed-row, cache, linked-account, and registration tests**

```python
def test_entry_and_parts_keep_account_and_file_identity(api):
    entry = tdapi._to_entry({"id": "row", "file_id": "9001", "filename": "x.bin",
        "filesize": 8, "telegram_message_id": 77, "telegram_user_id": 42})
    assert entry.telegram_user_id == 42
    assert api.parts_for(entry) == [RemotePart(77, 8, 42, "9001")]

def test_registration_sends_storage_account(api):
    api.register("x", 8, "application/octet-stream", 77, "9001", None,
                 telegram_user_id=42)
    assert api.last_payload["telegram_user_id"] == 42

def test_linked_accounts_are_returned_as_ids(api):
    api.script_get("/accounts", {"accounts": [{"telegram_user_id": 1}, {"telegram_user_id": 42}]})
    assert api.linked_account_ids() == {1, 42}
```

Also persist/read `telegram_user_id` and `file_id` through the directory and split cache fixtures, with missing IDs normalized to zero.

- [ ] **Step 2: Run the focused tests and confirm identity is currently lost**

Run: `.venv\Scripts\python.exe -m pytest tests/test_routed_metadata.py tests/test_dir_cache.py -q`

Expected: FAIL on missing account fields and tuple-based parts.

- [ ] **Step 3: Implement routed parsing and thread-local sessions**

```python
@dataclass(frozen=True)
class Entry:
    file_id: str
    name: str
    is_dir: bool
    size: int
    mtime: float
    mime: Optional[str] = None
    message_id: Optional[int] = None
    access_hash: Optional[str] = None
    is_split: bool = False
    split_group_id: Optional[str] = None
    file_hash: Optional[str] = None
    has_thumbnail: bool = False
    telegram_user_id: int = 0

def linked_account_ids(self) -> set[int]:
    body = self._call("GET", "/accounts")
    return {int(row["telegram_user_id"]) for row in body.get("accounts", [])}
```

Replace the shared `requests.Session` with `threading.local()` and `_http_session()`. Keep `_token`, `_auth_lock`, and challenge state process-wide. Route every request through the thread-local session and update cache schema version so old disk entries are safely re-listed.

- [ ] **Step 4: Run metadata, retry, cache, and size suites**

Run: `.venv\Scripts\python.exe -m pytest tests/test_routed_metadata.py tests/test_backend_retry.py tests/test_dir_cache.py tests/test_sizes.py -q`

Expected: PASS.

- [ ] **Step 5: Commit routed metadata**

```text
git add tdapi.py tests/test_routed_metadata.py tests/test_backend_retry.py tests/test_dir_cache.py tests/test_sizes.py
git commit -m "feat: preserve storage account metadata"
```

### Task 3: Multi-Account Worker Pool and Exact Routing

**Files:**
- Create: `telegram_accounts.py`
- Create: `tests/test_account_pool.py`
- Modify: `tgio.py:224-385`

**Interfaces:**
- Consumes: `AccountSpec` and parity slot counts.
- Produces: `AccountRuntime`, `TelegramAccountPool.start()`, `stop()`, `primary`, `for_read(account_id)`, `acquire_upload()`, `status()`, and `eligible_upload_ids`.
- `AccountRuntime.worker` is one `TelegramWorker`; each runtime owns independent file/chunk/message limiters.

- [ ] **Step 1: Add failing account-file and routing tests**

```python
def test_zero_routes_to_primary(pool):
    assert pool.for_read(0) is pool.primary

def test_nonzero_never_falls_back(pool):
    with pytest.raises(AccountUnavailableError, match="99"):
        pool.for_read(99)

def test_duplicate_configured_ids_are_rejected(tmp_path):
    path = write_accounts(tmp_path, [(7, "a", "s1"), (7, "b", "s2")])
    with pytest.raises(ConfigError, match="duplicate.*7"):
        load_account_specs(path)

def test_round_robin_skips_a_busy_account(two_account_pool):
    two_account_pool.runtime(1).file_slots.acquire()
    two_account_pool.runtime(1).file_slots.acquire()
    two_account_pool.runtime(1).file_slots.acquire()
    assert two_account_pool.acquire_upload(timeout=0.1).telegram_user_id == 2
```

Cover configured/session user-ID mismatch, one-account startup failure isolation, backend-unlinked exclusion from new uploads, and read availability for configured historical accounts.

- [ ] **Step 2: Run tests and confirm the single-worker design fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_account_pool.py -q`

Expected: FAIL because `telegram_accounts.py` does not exist.

- [ ] **Step 3: Implement account loading, lifecycle, and lease-based selection**

```python
@dataclass
class AccountRuntime:
    spec: AccountSpec
    worker: TelegramWorker
    file_slots: threading.BoundedSemaphore
    online: bool = False
    linked: bool = False
    error: Optional[str] = None

@contextmanager
def acquire_upload(self, timeout: Optional[float] = None) -> Iterator[AccountRuntime]:
    runtime = self._choose_free_runtime(timeout)
    try:
        yield runtime
    finally:
        runtime.file_slots.release()
```

`start()` connects in declared order, validates `worker.user_id`, authenticates the backend through primary only, fetches `/accounts`, and marks upload eligibility. Exceptions must include ID/label but never `session`. `for_read(0)` returns primary; other values require exact configured/online runtime without checking current linked status.

- [ ] **Step 4: Run account-pool and authentication tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_account_pool.py tests/test_auth_challenge.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the account pool**

```text
git add telegram_accounts.py tgio.py tests/test_account_pool.py tests/test_auth_challenge.py
git commit -m "feat: add multi-account telegram pool"
```

### Task 4: Exact-ID Routed Reads, Caches, and Cross-Account Ranges

**Files:**
- Create: `tests/test_account_routing.py`
- Modify: `tgio.py:387-1242`
- Modify: `bridge.py:182-535`
- Modify: `tests/test_split_math.py`
- Modify: `tests/test_thumbnails.py`
- Modify: `tests/test_photo_media.py`

**Interfaces:**
- Consumes: `TelegramAccountPool.for_read()` and `RemotePart`.
- Produces: `TelegramWorker.get_document(message_id, expected_file_id)`, `read(message_id, expected_file_id, offset, length)`, and pool-routed `SeekableRemoteFile(parts, pool)`.

- [ ] **Step 1: Add failing identity/collision/range tests**

```python
def test_returned_file_id_is_checked_before_getfile(worker):
    worker.fake_message(message_id=5, file_id=700)
    with pytest.raises(RemoteIdentityError, match="expected 701.*got 700"):
        worker.read(5, "701", 0, 1)
    assert worker.getfile_calls == []

def test_duplicate_message_ids_do_not_cross_accounts(pool):
    pool.runtime(1).worker.put(9, "101", b"A")
    pool.runtime(2).worker.put(9, "202", b"B")
    part = RemotePart(9, 1, 2, "202")
    assert pool.for_read(part.telegram_user_id).worker.read(
        part.message_id, part.file_id, 0, 1
    ) == b"B"

def test_range_crosses_storage_accounts(pool):
    parts = [RemotePart(10, 3, 1, "110"), RemotePart(10, 3, 2, "210")]
    assert read_logical(parts, offset=2, length=3, pool=pool) == b"cDE"
```

Add cache-key assertions for document, thumbnail, property, head, and split caches using `(telegram_user_id, message_id)` or `(telegram_user_id, file_id)`.

- [ ] **Step 2: Run routed-read tests and confirm tuple/single-worker assumptions fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_account_routing.py tests/test_split_math.py -q`

Expected: FAIL before byte reads because existing APIs lack account/file identity.

- [ ] **Step 3: Implement identity validation and routed range assembly**

```python
def _assert_media_id(media, expected_file_id: str) -> None:
    actual = str(getattr(media, "id", ""))
    if actual != str(expected_file_id):
        raise RemoteIdentityError(f"Telegram file mismatch: expected {expected_file_id}, got {actual}")

def read_part(pool, part: RemotePart, offset: int, length: int) -> bytes:
    return pool.for_read(part.telegram_user_id).worker.read(
        part.message_id, part.file_id, offset, length
    )
```

Change `Part` to retain the whole `RemotePart`, keep logical offsets unchanged, and route each span returned by `map_range`. Key worker document caches by `(message_id, expected_file_id)` and bridge disk caches with account-prefixed names.

- [ ] **Step 4: Run all read/preview/property regression tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_account_routing.py tests/test_split_math.py tests/test_read_pace.py tests/test_thumbnails.py tests/test_photo_media.py -q`

Expected: PASS.

- [ ] **Step 5: Commit routed reads**

```text
git add bridge.py tgio.py tests/test_account_routing.py tests/test_split_math.py tests/test_read_pace.py tests/test_thumbnails.py tests/test_photo_media.py
git commit -m "feat: route reads by exact telegram account"
```

### Task 5: Persisted Web-Compatible Chunk Limiter and Message Bucket

**Files:**
- Create: `upload_limiter.py`
- Create: `tests/test_upload_limiter.py`
- Modify: `tgupload.py:83-214`
- Modify: `tests/test_upload_pace.py`

**Interfaces:**
- Produces: `LimiterConfig.web_defaults()`, `AdaptiveUploadLimiter.acquire()`, `success(duration)`, `flood(seconds, premium=False)`, `snapshot()`, and `MessageTokenBucket.acquire()/flood()`.
- Persists account-specific state to `<cache_dir>/meta/upload-rate-<telegram_user_id>.json` through atomic replace.

- [ ] **Step 1: Port the frontend limiter state-transition vectors as failing tests**

```python
def test_web_constants():
    cfg = LimiterConfig.web_defaults()
    assert (cfg.initial, cfg.minimum, cfg.maximum, cfg.burst) == (4.0, 0.5, 12.0, 2)
    assert (cfg.increase_step, cfg.increase_interval) == (0.5, 10.0)
    assert (cfg.clean_window, cfg.first_backoff, cfg.backoff) == (20.0, 0.5, 0.95)
    assert (cfg.slow_zone, cfg.slow_step, cfg.slow_interval) == (0.8, 0.1, 30.0)
    assert (cfg.probe_cooldown, cfg.probe_cooldown_max) == (300.0, 1800.0)
    assert (cfg.probe_step, cfg.probe_confirm) == (0.2, 60.0)
    assert (cfg.escalation_count, cfg.escalation_window) == (3, 120.0)

def test_premium_wait_does_not_lower_rate(fake_clock, limiter):
    before = limiter.snapshot()
    limiter.flood(17, premium=True)
    after = limiter.snapshot()
    assert (after.rate, after.ceiling) == (before.rate, before.ceiling)
    assert after.paused_until == fake_clock.now + 17
```

Add deterministic clock tests for first flood, learned ceiling, slow zone, probe confirmation/failure cooldown, three-flood escalation, ten-minute reset, corrupt state fallback, and independent account files/message buckets.

- [ ] **Step 2: Run limiter tests and confirm current `UploadGate` lacks parity states**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_limiter.py tests/test_upload_pace.py -q`

Expected: FAIL on missing ceiling/probe/persistence behavior.

- [ ] **Step 3: Implement the state machine and atomic persistence**

```python
@dataclass(frozen=True)
class LimiterSnapshot:
    rate: float
    ceiling: Optional[float]
    floods: int
    window: int
    paused_until: float

class AdaptiveUploadLimiter:
    @asynccontextmanager
    async def slot(self):
        await self._slots.acquire()
        try:
            yield
        finally:
            self._slots.release()

    async def pace(self) -> None:
        await self._wait_for_rate_and_penalty()

    def flood(self, seconds: Optional[float], *, premium: bool = False) -> None:
        self._state = transition_flood(self._state, self._now(), seconds, premium, self.config)
        self._store.save(self.account_id, self._state)
```

Mirror `frontend/src/lib/adaptiveRateLimiter.ts` transitions and `frontend/src/config.ts` constants exactly, converting milliseconds to seconds only at the Python boundary. Serialize a versioned JSON object through a same-directory `.part` plus `os.replace`.

- [ ] **Step 4: Run limiter suites**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_limiter.py tests/test_upload_pace.py -q`

Expected: PASS.

- [ ] **Step 5: Commit limiter parity**

```text
git add upload_limiter.py tgupload.py tests/test_upload_limiter.py tests/test_upload_pace.py
git commit -m "feat: port web upload rate limiters"
```

### Task 6: Exact Small and Big Upload Primitives

**Files:**
- Create: `tests/test_upload_protocol.py`
- Modify: `tgupload.py:1-380`
- Modify: `tgio.py:714-782`
- Modify: `tests/test_upload_parts.py`

**Interfaces:**
- Produces: `plan_small_parts(size, 128 * 1024)`, `plan_big_parts(size, 512 * 1024)`, `upload_small_file_parts(..., workers=4) -> InputFile`, and `upload_big_file_parts(...) -> InputFileBig`.
- Both primitives consume the account's shared twelve-slot `AdaptiveUploadLimiter`; caller chooses `force_big=True` for every logical split segment.

- [ ] **Step 1: Add failing boundary, constructor, and concurrency tests**

```python
@pytest.mark.parametrize("size,protocol,segments", [
    (10 * 1024 * 1024, "small", 1),
    (10 * 1024 * 1024 + 1, "big", 1),
    (500 * 1024 * 1024, "big", 1),
    (500 * 1024 * 1024 + 1, "split", 2),
])
def test_protocol_boundaries(size, protocol, segments):
    decision = decide_protocol(size, album_eligible=False)
    assert (decision.name, len(decision.segments)) == (protocol, segments)

def test_small_file_has_128k_parts_four_workers_and_md5(fake_sender):
    result = run(upload_small_file_parts(fake_sender, reader(b"x" * 700_000), 700_000, "x.bin"))
    assert max(fake_sender.in_flight) <= 4
    assert {r.part for r in fake_sender.requests} == set(range(6))
    assert result.md5_checksum == hashlib.md5(b"x" * 700_000).hexdigest()

def test_one_byte_split_tail_still_uses_big_parts(fake_sender):
    run(upload_big_file_parts(fake_sender, reader(b"x"), 1, "tail", force_big=True))
    assert fake_sender.requests[0].__class__.__name__ == "SaveBigFilePartRequest"
```

- [ ] **Step 2: Run protocol tests and observe the current small-file Telethon shortcut fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_protocol.py tests/test_upload_parts.py -q`

Expected: FAIL because small originals are currently delegated to sequential `client.upload_file`.

- [ ] **Step 3: Implement explicit part primitives and RPC retries**

```python
SMALL_PART_SIZE = 128 * 1024
BIG_PART_SIZE = 512 * 1024
SMALL_FILE_MAX = 10 * 1024 * 1024
MESSAGE_MAX = 500 * 1024 * 1024

def decide_protocol(size: int, album_eligible: bool) -> ProtocolDecision:
    if album_eligible and size <= SMALL_FILE_MAX:
        return ProtocolDecision("album", [(0, size)], False)
    if size <= SMALL_FILE_MAX:
        return ProtocolDecision("small", [(0, size)], False)
    segments = plan_segments(size, MESSAGE_MAX)
    return ProtocolDecision("big" if len(segments) == 1 else "split", segments, True)
```

Define `ProtocolDecision` as `@dataclass(frozen=True)` with `name: Literal["small", "album", "big", "split"]`, `segments: tuple[tuple[int, int], ...]`, and `force_big: bool`.

Use positional `SaveFilePartRequest`/`SaveBigFilePartRequest` indices, seek/read through `_PartReader`, three non-flood attempts with 1s then 2s backoff, premium-flood classification before Telethon erases the wire name, and account limiter acquisition around every part RPC.

- [ ] **Step 4: Run upload primitive tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_protocol.py tests/test_upload_parts.py tests/test_split_math.py -q`

Expected: PASS.

- [ ] **Step 5: Commit upload protocol parity**

```text
git add tgupload.py tgio.py tests/test_upload_protocol.py tests/test_upload_parts.py tests/test_split_math.py
git commit -m "feat: match web telegram upload protocols"
```

### Task 7: Image and Video Thumbnail Classification

**Files:**
- Create: `media_thumbnail.py`
- Create: `tests/test_media_thumbnail.py`
- Modify: `tgio.py:785-833`
- Modify: `tests/test_upload_preview.py`

**Interfaces:**
- Produces: `ThumbnailResult(kind, jpeg, width, height, error)`, where kind is `ready`, `not_media`, or `undecodable`.
- Produces: `capture_thumbnail(path, mime_type, ffmpeg) -> ThumbnailResult`.

- [ ] **Step 1: Add failing still/video/classification tests**

```python
def test_webp_is_media_but_not_album_eligible(tmp_path):
    result = capture_thumbnail(make_webp(tmp_path), "image/webp", None)
    assert result.kind == "ready"
    assert album_eligible("image/webp") is False

def test_decodable_video_uses_configured_ffmpeg(tmp_path, fake_ffmpeg):
    result = capture_thumbnail(tmp_path / "clip.mp4", "video/mp4", fake_ffmpeg)
    assert result.kind == "ready"
    assert result.jpeg.startswith(b"\xff\xd8")
    assert max(result.width, result.height) > 0

def test_unsupported_codec_is_explicitly_undecodable(tmp_path, fake_ffmpeg):
    fake_ffmpeg.fail_with("Invalid data found when processing input")
    assert capture_thumbnail(tmp_path / "bad.mkv", "video/x-matroska", fake_ffmpeg).kind == "undecodable"
```

Test JPEG <=20 KiB and <=320x320, EXIF orientation, alpha flattening, ffmpeg environment/PATH discovery, non-media classification, and decodable-media capture errors.

- [ ] **Step 2: Run thumbnail tests and confirm video support is absent**

Run: `.venv\Scripts\python.exe -m pytest tests/test_media_thumbnail.py tests/test_upload_preview.py -q`

Expected: FAIL because only Pillow still images are implemented.

- [ ] **Step 3: Extract image logic and add bounded ffmpeg capture**

```python
@dataclass(frozen=True)
class ThumbnailResult:
    kind: Literal["ready", "not_media", "undecodable"]
    jpeg: Optional[bytes] = None
    width: Optional[int] = None
    height: Optional[int] = None
    error: Optional[str] = None

def discover_ffmpeg(configured: str) -> Optional[str]:
    candidate = configured or os.environ.get("FFMPEG", "") or shutil.which("ffmpeg") or ""
    return candidate or None
```

Invoke ffmpeg without a shell, with a bounded timeout and arguments that select one early frame, scale within 320x320, and emit MJPEG to stdout. Recompress with Pillow when needed to enforce the 20 KiB Telegram limit. A media file that probes as decodable but cannot yield a thumbnail raises `ThumbnailError`; an ffmpeg unsupported/invalid-data result maps to `undecodable`.

- [ ] **Step 4: Run thumbnail suites**

Run: `.venv\Scripts\python.exe -m pytest tests/test_media_thumbnail.py tests/test_upload_preview.py -q`

Expected: PASS.

- [ ] **Step 5: Commit thumbnail parity**

```text
git add media_thumbnail.py tgio.py tests/test_media_thumbnail.py tests/test_upload_preview.py
git commit -m "feat: add web-compatible media thumbnails"
```

### Task 8: Exact Deduplication Coverage and Batch-Local Claims

**Files:**
- Create: `tests/test_upload_dedup.py`
- Create: `upload_engine.py`
- Modify: `gamestage.py:57-121,406-459`

**Interfaces:**
- Produces: `canonical_existing_parts(rows, original_size) -> list[UploadedPart]`, `assert_parts_cover_file(parts, size)`, and `FingerprintClaims.run(fingerprint, producer)`.

- [ ] **Step 1: Add failing coverage and claim tests**

```python
def test_incomplete_duplicate_is_rejected():
    rows = [row(group="g", index=0, size=500), row(group="g", index=2, size=500)]
    assert canonical_existing_parts(rows, original_size=1000) == []

def test_exact_complete_duplicate_keeps_each_storage_account():
    rows = [row(group="g", index=1, size=4, account=2), row(group="g", index=0, size=6, account=1)]
    parts = canonical_existing_parts(rows, original_size=10)
    assert [(p.index, p.size, p.telegram_user_id) for p in parts] == [(0, 6, 1), (1, 4, 2)]

def test_two_batch_aliases_run_one_producer():
    calls = 0
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(lambda _: claims.run("same:10", producer), range(2)))
    assert calls == 1
    assert results[0] == results[1]
```

Cover duplicate DB aliases, duplicate Telegram message IDs, deterministic complete-candidate choice, over-counted candidates, failed claimant propagation, and a later retry getting a fresh claim.

- [ ] **Step 2: Run dedup tests and confirm size-blind current behavior fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_dedup.py -q`

Expected: FAIL because existing `canonical_existing_parts` lacks `original_size` and batch claims.

- [ ] **Step 3: Implement exact canonicalization and future-backed claims**

```python
def assert_parts_cover_file(parts: Sequence[UploadedPart], size: int) -> None:
    total = sum(part.size for part in parts)
    if total != size:
        raise CoverageError(f"uploaded parts cover {total} bytes, expected {size}")

class FingerprintClaims:
    def run(self, key: str, producer: Callable[[], list[UploadedPart]]) -> list[UploadedPart]:
        future, owner = self._claim(key)
        if owner:
            try:
                future.set_result(producer())
            except BaseException as exc:
                future.set_exception(exc)
        return future.result()
```

Group split candidates by non-empty `split_group_id`, sort and require contiguous indices from zero, collapse aliases/message duplicates, compare exact summed bytes, then consider non-split candidates. Return immutable `UploadedPart` values with original account identity.

- [ ] **Step 4: Run dedup and legacy upload-preview tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_dedup.py tests/test_upload_preview.py -q`

Expected: PASS.

- [ ] **Step 5: Commit exact deduplication**

```text
git add gamestage.py upload_engine.py tests/test_upload_dedup.py tests/test_upload_preview.py
git commit -m "fix: require exact upload dedup coverage"
```

### Task 9: Non-Album Transfer Engine and Multi-Account Splits

**Files:**
- Create: `tests/test_upload_engine.py`
- Modify: `upload_engine.py`
- Modify: `tgio.py:714-782`
- Modify: `gamestage.py:406-535`

**Interfaces:**
- Consumes: pool leases, protocol decision, thumbnail result, claims, limiters, `UploadedPart`.
- Produces: `UploadEngine.transfer(TransferRequest) -> TransferResult` and `register_result(result)`.

- [ ] **Step 1: Add failing single/split/account/registration tests**

```python
def test_500m_plus_one_dispatches_two_big_segments(engine):
    result = engine.transfer(request(size=500 * MiB + 1))
    assert [p.index for p in result.parts] == [0, 1]
    assert [p.telegram_user_id for p in result.parts] == [1, 2]
    assert engine.rpc_kinds == ["SaveBigFilePart", "SaveBigFilePart"]

def test_results_are_restored_by_plan_index(engine):
    engine.finish_segment_order = [1, 0]
    result = engine.transfer(request(size=500 * MiB + 1))
    assert [p.index for p in result.parts] == [0, 1]

def test_every_registered_part_names_its_storage_account(engine):
    result = engine.transfer(request(size=500 * MiB + 1))
    engine.register_result(result)
    assert [p["telegram_user_id"] for p in engine.api.payloads] == [1, 2]
```

Test small protocol four-worker path, one big segment, segment concurrency across accounts, only index zero receiving a thumbnail, file-slot release before message/registration, exact coverage assertion before registration, and registration concurrency <=8.

- [ ] **Step 2: Run engine tests and confirm no coordinator exists**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_engine.py -q`

Expected: FAIL on missing `UploadEngine.transfer`.

- [ ] **Step 3: Implement the non-album engine**

```python
class UploadEngine:
    def transfer(self, request: TransferRequest) -> TransferResult:
        fingerprint = sample_hash(request.source)
        existing = canonical_existing_parts(self.api.check_hash(fingerprint).get("files", []), request.logical_size)
        parts = existing or self.claims.run(fingerprint, lambda: self._upload_fresh(request))
        assert_parts_cover_file(parts, request.logical_size)
        return TransferResult(request, fingerprint, tuple(sorted(parts, key=lambda p: p.index)))
```

Use the `TransferRequest` and `TransferResult` definitions from Task 1. Define `CoverageError`, `AccountUnavailableError`, `RemoteIdentityError`, and `ThumbnailError` as focused `RuntimeError` subclasses in the modules that raise them.

For splits, submit each planned segment independently so every segment obtains its own account lease; pass `force_big=True` to all segments. Build messages through each runtime's message bucket, store actual worker user ID in the result, and settle all registration futures before success.

- [ ] **Step 4: Run engine and protocol tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_engine.py tests/test_upload_protocol.py tests/test_upload_dedup.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the transfer engine**

```text
git add gamestage.py tgio.py upload_engine.py tests/test_upload_engine.py
git commit -m "feat: add multi-account upload engine"
```

### Task 10: Per-Account Album Preparation, Send, Mapping, and Fallback

**Files:**
- Create: `tests/test_upload_album.py`
- Modify: `transfer_models.py`
- Modify: `upload_engine.py`
- Modify: `tgio.py`

**Interfaces:**
- Produces: `TelegramWorker.prepare_album_item(...) -> PreparedAlbumItem`, `send_album(items, timeout) -> list[UploadedPart]`, and `AlbumQueue.flush()`.
- Prepared items retain source path, account ID, uploaded document ID, original size, thumbnail flag, and planned alias.

- [ ] **Step 1: Add failing eligibility/batch/mapping/fallback tests**

```python
@pytest.mark.parametrize("mime,size,eligible", [
    ("image/jpeg", 10 * MiB, True),
    ("video/mp4", 10 * MiB, True),
    ("image/webp", 1, False),
    ("image/jpeg", 10 * MiB + 1, False),
])
def test_album_eligibility(mime, size, eligible):
    assert album_eligible(mime, size) is eligible

def test_eleven_items_flush_as_ten_and_one(engine):
    engine.transfer_batch([request_jpeg(i) for i in range(11)])
    assert engine.album_batch_sizes == [10, 1]

def test_shuffled_updates_map_by_document_id(engine):
    engine.album_updates = [message(doc_id="b", msg_id=2), message(doc_id="a", msg_id=1)]
    parts = engine.send_album([prepared("a"), prepared("b")])
    assert [p.message_id for p in parts] == [1, 2]

def test_timeout_fallback_rereads_without_thumb_and_one_worker(engine):
    engine.album_timeout = True
    part = engine.transfer_batch([request_jpeg(1)])[0].parts[0]
    assert engine.fallback_calls == [{"workers": 1, "force_document": True, "thumb": None}]
    assert part.has_thumbnail is False
```

Also assert separate accounts never share a `SendMultiMedia`, album originals/thumbnails use 512 KiB and shared account gates, `UploadMedia` uses the message bucket, and one failed album does not cancel another account's batch.

- [ ] **Step 2: Run album tests and confirm album primitives are absent**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_album.py -q`

Expected: FAIL on missing album preparation/queue behavior.

- [ ] **Step 3: Implement account-local album queues and exact fallback**

```python
def album_eligible(mime_type: str, size: int) -> bool:
    return size <= SMALL_FILE_MAX and mime_type != "image/webp" and (
        mime_type.startswith("image/") or mime_type.startswith("video/")
    )

def map_album_updates(items, updates):
    by_doc = {str(media_document(update).id): update for update in updates}
    return [uploaded_part_from_message(item, by_doc[str(item.document_id)]) for item in items]
```

Prepare originals with 512 KiB `SaveFilePart`, upload optional thumbnail through the same limiter, call `messages.UploadMedia`, release the file lease, and enqueue by account. Flush immediately at ten and flush tails after discovery ends. Wrap `SendMultiMedia` in 60-second timeout; on any batch failure, re-open each source and call the non-album small sender with exactly one worker, `force_document=True`, and no thumbnail.

- [ ] **Step 4: Run album, thumbnail, and engine tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_album.py tests/test_media_thumbnail.py tests/test_upload_engine.py -q`

Expected: PASS.

- [ ] **Step 5: Commit album parity**

```text
git add transfer_models.py upload_engine.py tgio.py tests/test_upload_album.py
git commit -m "feat: match web media album uploads"
```

### Task 11: Streaming Batch Scheduler and Durable Queue States

**Files:**
- Create: `tests/test_upload_scheduler.py`
- Modify: `upload_engine.py`
- Modify: `uploadstage.py:37-250`
- Modify: `tests/test_bridge_e2e.py`

**Interfaces:**
- Produces: `UploadEngine.transfer_batch(requests, status_sink)`, `UploadStager` concurrent due-file dispatch, durable `PendingUpload.stage`, attempt count, assignment/progress, and error.

- [ ] **Step 1: Add failing pipeline and durability tests**

```python
def test_hash_check_upload_and_register_overlap(scheduler):
    scheduler.block_register("first")
    scheduler.submit(["first", "second", "third"])
    assert scheduler.started_hash("third")
    assert scheduler.started_upload("second")

def test_only_affected_source_remains_after_failure(stager):
    stager.engine.fail_register_for("bad.bin")
    stager.process_due(["good.bin", "bad.bin"])
    assert not stager.source("good.bin").exists()
    assert stager.source("bad.bin").exists()
    assert stager.status_for("bad.bin")["stage"] == "failed"

def test_fifth_failure_is_abandoned_and_retained(stager):
    for _ in range(5):
        stager.fail_one("x.bin")
    assert stager.status_for("x.bin")["stage"] == "abandoned"
    assert stager.source("x.bin").exists()
```

Assert hash <=2, checks <=8, registrations <=8, file preparations <=3 per account, unrelated failures continue, restart adopts every staged source, batch duplicates upload once/register each alias, and successful source deletion happens after all registrations.

- [ ] **Step 2: Run scheduler tests and confirm the current serial loop fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_scheduler.py -q`

Expected: FAIL because `UploadStager._loop()` processes due keys one at a time.

- [ ] **Step 3: Implement bounded streaming stages and durable status records**

```python
class UploadEngine:
    def transfer_batch(self, requests, status_sink):
        with (
            ThreadPoolExecutor(max_workers=self.cfg.hash_concurrency) as hash_pool,
            ThreadPoolExecutor(max_workers=self.cfg.hash_check_concurrency) as check_pool,
            ThreadPoolExecutor(max_workers=self.cfg.register_concurrency) as register_pool,
        ):
            return self._stream_requests(requests, hash_pool, check_pool, register_pool, status_sink)
```

Use futures/queues between stages instead of waiting for every hash before checks. Persist per-source state atomically beside queue metadata on each transition; redact exceptions before status storage. Keep existing ten-minute retry, mark the fifth logical failure abandoned, and remove source/state only after message creation plus complete registration.

- [ ] **Step 4: Run scheduler and WebDAV end-to-end suites**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_scheduler.py tests/test_bridge_e2e.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the durable scheduler**

```text
git add upload_engine.py uploadstage.py tests/test_upload_scheduler.py tests/test_bridge_e2e.py
git commit -m "feat: stream durable staged uploads"
```

### Task 12: `/game`, Bridge Lifecycle, Status, and Warmup Integration

**Files:**
- Create: `tests/test_transfer_status.py`
- Modify: `gamestage.py:123-535`
- Modify: `bridge.py:182-535,1217-1515`
- Modify: `warmup.py:449-480`
- Modify: `tests/test_bridge_e2e.py`
- Modify: `tests/test_shell_warm.py`

**Interfaces:**
- Consumes: one shared `TelegramAccountPool` and `UploadEngine`.
- Produces: `/rpc/status` account/limiter/queue views with no credentials and `/game` transfer requests with `allow_album=False` for packed directories.

- [ ] **Step 1: Add failing `/game`, lifecycle, and redaction tests**

```python
def test_game_directory_uses_engine_as_zip(game_stager):
    game_stager.pack_and_upload("title")
    request = game_stager.engine.requests[0]
    assert request.upload_name == "title.zip"
    assert request.mime_type == "application/zip"
    assert request.allow_album is False

def test_status_reports_accounts_and_limiter_without_credentials(rpc):
    body = rpc.get_json("/rpc/status")
    assert body["accounts"][0]["telegram_user_id"] == 1
    assert {"rate", "ceiling", "window", "floods"} <= body["accounts"][0]["limiter"]
    rendered = json.dumps(body).lower()
    assert "session" not in rendered and "bearer" not in rendered and "jwt" not in rendered
```

Cover top-level `/game` media thumbnail rules, packed-directory no-thumbnail behavior, primary-only bot challenge, all-account shutdown, routed warmup thumbnails/properties, and historical account-zero reads.

- [ ] **Step 2: Run integration tests and confirm constructors still require one worker**

Run: `.venv\Scripts\python.exe -m pytest tests/test_transfer_status.py tests/test_bridge_e2e.py tests/test_shell_warm.py -q`

Expected: FAIL on pool/engine injection and missing status fields.

- [ ] **Step 3: Wire one pool/engine through all application entry points**

```python
pool = TelegramAccountPool.from_config(cfg, worker_factory=TelegramWorker)
pool.start(api)
engine = UploadEngine(cfg, api, pool)
resolver = Resolver(cfg, api, pool)
game_stager = GameStager(cfg, api, engine)
upload_stager = UploadStager(cfg, api, engine)
```

Start primary and secondary clients before stagers, use primary DM sender for login, stop stagers before pool shutdown, and make `RpcApp._status()` merge both stager states plus `pool.status()`. `GameStager._upload_and_register()` must send a `TransferRequest` for the packed archive and retain the existing exclusive atomic pack.

- [ ] **Step 4: Run all integration and regression suites**

Run: `.venv\Scripts\python.exe -m pytest tests/test_transfer_status.py tests/test_bridge_e2e.py tests/test_shell_warm.py tests/test_auth_challenge.py -q`

Expected: PASS.

- [ ] **Step 5: Commit application integration**

```text
git add bridge.py gamestage.py warmup.py tests/test_transfer_status.py tests/test_bridge_e2e.py tests/test_shell_warm.py
git commit -m "feat: integrate transfer engine across webdav"
```

### Task 13: Timing Logs, Operator Documentation, and Full Verification

**Files:**
- Create: `tests/test_transfer_logging.py`
- Modify: `upload_engine.py`
- Modify: `CLAUDE.md`
- Modify: `UPLOAD_DOWNLOAD_COMPARISON.md`
- Modify: `docs/superpowers/specs/2026-09-05-webdav-web-transfer-parity-design.md`

**Interfaces:**
- Produces: one logical-file completion log containing protocol, bytes, parts, hash/check/thumbnail/slot/upload/message/register/total durations, account IDs, and limiter summary.

- [ ] **Step 1: Add failing structured-log assertions**

```python
def test_completion_log_contains_web_timing_fields(engine, caplog):
    engine.transfer(request(size=1024))
    line = next(r.message for r in caplog.records if "transfer complete" in r.message)
    for field in ("protocol=", "bytes=", "parts=", "hash_ms=", "check_ms=",
                  "thumb_ms=", "slot_ms=", "upload_ms=", "message_ms=",
                  "register_ms=", "total_ms=", "accounts=", "rate=", "ceiling="):
        assert field in line

def test_error_log_redacts_session_and_jwt(engine, caplog):
    engine.fail_with(RuntimeError("session=secret Authorization: Bearer token"))
    assert "secret" not in caplog.text and "token" not in caplog.text
```

- [ ] **Step 2: Run logging tests and confirm timing fields are incomplete**

Run: `.venv\Scripts\python.exe -m pytest tests/test_transfer_logging.py -q`

Expected: FAIL on missing structured completion fields.

- [ ] **Step 3: Add timing accumulation, redaction, and operating documentation**

```python
log.info(
    "transfer complete protocol=%s bytes=%d parts=%d hash_ms=%.0f check_ms=%.0f "
    "thumb_ms=%.0f slot_ms=%.0f upload_ms=%.0f message_ms=%.0f register_ms=%.0f "
    "total_ms=%.0f accounts=%s rate=%.2f ceiling=%s",
    metrics.protocol, metrics.bytes, metrics.parts, metrics.hash_ms, metrics.check_ms,
    metrics.thumb_ms, metrics.slot_ms, metrics.upload_ms, metrics.message_ms,
    metrics.register_ms, metrics.total_ms, metrics.account_ids, metrics.rate, metrics.ceiling,
)
```

Define `TransferMetrics` in `upload_engine.py` as a dataclass with the exact fields used above: `protocol`, `bytes`, `parts`, `hash_ms`, `check_ms`, `thumb_ms`, `slot_ms`, `upload_ms`, `message_ms`, `register_ms`, `total_ms`, `account_ids`, `rate`, and `ceiling`. Build it from monotonic timestamps captured around each stage.

Document account-file creation, linked-account prerequisites, ffmpeg discovery, limiter state locations, status meanings, migration from legacy session-only mode, and expected protocol boundaries. Mark the design status `Implemented` only after Step 5 passes.

- [ ] **Step 4: Run focused log/security checks and the entire offline suite**

Run: `.venv\Scripts\python.exe -m pytest tests/test_transfer_logging.py -q`

Run: `rg -n -i "session.*=|authorization:|bearer |jwt" accounts.example.json config.example.ini`

Expected: logging tests PASS; the scan shows only blank/example field names and explanatory comments, never credentials.

- [ ] **Step 5: Run final build/verification and commit**

Run: `.venv\Scripts\python.exe -m pytest tests -q`

Run: copy `shellthumb/build.bat`, `TeleDriveThumb.cpp`, `TeleDriveProps.cpp`, `TeleDriveThumb.def`, and `warmshell.cpp` to a fresh temporary directory and execute `build.bat` there so a loaded production DLL cannot block the link test.

Run: `git diff --check`

Expected: all offline tests PASS, native build exits 0, and `git diff --check` reports no whitespace errors.

```text
git add CLAUDE.md UPLOAD_DOWNLOAD_COMPARISON.md upload_engine.py tests/test_transfer_logging.py docs/superpowers/specs/2026-09-05-webdav-web-transfer-parity-design.md
git commit -m "docs: document web transfer parity"
```

### Task 14: Optional Live Boundary Matrix

**Files:**
- Create only with explicit permission: `scripts/live_transfer_parity.py`
- Create only with explicit permission: `tests/live/test_transfer_parity.py`

**Interfaces:**
- Consumes: configured disposable Telegram accounts, backend, and isolated test folder name supplied by the user.
- Produces: a JSON report comparing WebDAV-created backend rows/Telegram media with Web-client expectations; it does not delete user data unless the user separately authorizes cleanup.

- [ ] **Step 1: Obtain explicit permission and disposable target details**

Ask for the test folder, accounts allowed for upload, maximum transfer bytes, and whether cleanup is authorized. Do not run or create live probes without the answer.

- [ ] **Step 2: Add the opt-in live matrix script**

```python
@dataclass(frozen=True)
class Case:
    name: str
    count: int
    sizes: tuple[int, ...]
    identical: bool = False

CASES = (
    Case("jpeg-album-11", count=11, sizes=(1 * MiB,)),
    Case("webp-small", count=1, sizes=(1 * MiB,)),
    Case("small-boundaries", count=2, sizes=(10 * MiB, 10 * MiB + 1)),
    Case("segment-boundaries", count=2, sizes=(500 * MiB, 500 * MiB + 1)),
    Case("duplicate-pair", count=2, sizes=(1 * MiB,), identical=True),
)
```

Generate deterministic payloads, submit through the normal staged WebDAV route, poll `/rpc/status`, read backend rows, fetch Telegram media metadata through the exact account, and write only IDs/hashes/sizes/protocol classifications to the report.

- [ ] **Step 3: Run only the authorized matrix and inspect every mismatch**

Run after setting the user-approved values: `.venv\Scripts\python.exe scripts/live_transfer_parity.py --folder $env:TD_PARITY_FOLDER --max-bytes $env:TD_PARITY_MAX_BYTES`

Expected: every case reports exact account ID, file ID, message count, split flag, part order/sizes, thumbnail flag, and byte hash parity.

- [ ] **Step 4: Re-run the offline suite after any correction**

Run: `.venv\Scripts\python.exe -m pytest tests -q`

Expected: PASS with no live network test collected by default.

- [ ] **Step 5: Commit the opt-in probe only if it was authorized and used**

```text
git add scripts/live_transfer_parity.py tests/live/test_transfer_parity.py
git commit -m "test: add opt-in transfer parity probe"
```
