# SQLite Sessions and Idle Segment Failover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace plaintext Telegram sessions with directory-discovered SQLite session files, then add lease-safe idle-account failover for premium-flooded large upload segments.

**Architecture:** The first phase makes `<telegram_user_id>.session` files the only runtime account registry; one Telethon control client owns each SQLite file while auxiliary clients use non-persistent in-memory clones. The second phase adds a thread-safe account activity/speed layer and central `SegmentScheduler`; it may revoke a premium-flooded attempt, drain only that attempt's in-flight part RPCs, reserve a truly idle account, restart with a new Telegram `file_id`, and grant message finalization to exactly one generation.

**Tech Stack:** Python 3, Telethon 1.44.x, asyncio, `threading.Condition`, `ThreadPoolExecutor`, SQLite sessions, pytest, PowerShell/Windows ACLs.

**Specs:**

- `docs/superpowers/specs/2026-09-09-telethon-session-files-design.md`
- `docs/superpowers/specs/2026-09-07-idle-segment-failover-design.md` (behavioral port from browser/GramJS to this Python bridge)

## Global Constraints

- Runtime account discovery uses only `[telegram].primary_user_id`, `[telegram].session_dir`, and direct-child `<positive telegram user id>.session` files.
- A directory containing only primary is the one-account case; additional valid files enable the same pool without a separate single-account branch.
- Each SQLite file has exactly one Telethon owner. Download/upload auxiliary clients receive distinct in-memory sessions and never open the SQLite path.
- Pin `telethon>=1.44,<2`; do not attempt a Telethon 2 migration in this change.
- SQLite sessions are unencrypted bearer credentials. Never log their paths, contents, authorization keys, internal StringSession serialization, login codes, 2FA passwords, JWTs, or authorization headers.
- The bridge owns `<session_dir>/.teledrive-session.lock` for its lifetime; `sessionctl` must obtain the same lock before login or migration.
- Failover applies only to fresh `force_big=True` segment attempts. Small uploads, album items, and thumbnails affect account idleness but are never migration candidates.
- A candidate must be active for at least 30 seconds, have a `FLOOD_PREMIUM_WAIT` in the most recent 30 seconds, be incomplete, and have `migration_count == 0`.
- A replacement must be online, linked, truly idle, absent from `attempted_account_ids`, and have an effective-speed snapshot no older than 300 seconds.
- Migrate only when `(replacement_speed / current_speed) * remaining_ratio > 2`; equality does not migrate. Candidate current speed zero maps to infinity only after all candidate gates pass.
- Migration increments the attempt generation and removes the old account's finalize right before the replacement can start. A segment migrates at most once and always restarts at part zero with a new Telegram `file_id`.
- Drain waits only for the revoked attempt's RPC counter. It must not wait for unrelated work on the old account. A sent MTProto RPC is never hard-cancelled and has a 120-second wrapper deadline.
- Effective progress counts first success of each current-generation part and may decrease only at migration. Physical bytes count every explicitly successful part RPC, including late revoked-generation success, and never decrease.
- Premium flood sets limiter mode to `frozen` without lowering its rate. Success cannot ramp while frozen; after the wait, a real send starts a 60-second flood-free window, then `cautious` ramps by at most 0.1 parts/s every 30 seconds.
- File bytes continue to flow only between the local bridge and Telegram. The TeleDrive FastAPI backend remains metadata-only.
- Automated tests are offline. No session revocation, live login, Telegram upload, credential deletion, or live failover probe runs without explicit user authorization.
- Preserve every unrelated uncommitted worktree change. Each task stages only the files named in its commit step.

## Execution preflight

- [ ] Read both source specs completely before editing code.
- [ ] Run `git status --short --untracked-files=all` and identify the intended base commit. The current working tree already contains unrelated user changes, so use `superpowers:using-git-worktrees` for execution unless the user first commits or otherwise reconciles them. Do not stash, discard, or absorb those changes.
- [ ] Run `.venv\Scripts\python.exe -m pytest -q` in the selected clean execution tree and record any baseline failures before Task 1.
- [ ] Complete and verify Phase A before starting Phase B. The phases are separately committable, but Phase B deliberately builds on Phase A's common account pool and client lifecycle.

---

## Phase A: SQLite session-file account discovery

### Task 1: Configuration and deterministic session discovery

**Files:**
- Create: `telegram_sessions.py`
- Create: `tests/test_session_discovery.py`
- Modify: `config.py:19-65,150-220`
- Modify: `transfer_models.py:21-26`
- Modify: `config.example.ini:7-25`

**Interfaces:**
- Consumes: config file path and application root.
- Produces: `AccountSpec(telegram_user_id: int, session_path: Path)`, `SessionConfig`, and `discover_account_specs(session_dir, primary_user_id, app_root) -> list[AccountSpec]`.

- [ ] **Step 1: Write failing configuration and discovery tests**

```python
def test_one_file_is_one_account_and_primary_is_first(tmp_path):
    root = tmp_path / "app"
    sessions = tmp_path / "sessions"
    root.mkdir()
    sessions.mkdir()
    (sessions / "42.session").write_bytes(b"sqlite")
    specs = discover_account_specs(sessions, 42, root)
    assert specs == [AccountSpec(42, (sessions / "42.session").resolve())]


def test_secondaries_are_sorted_numerically_after_primary(tmp_path):
    root = tmp_path / "app"
    sessions = tmp_path / "sessions"
    root.mkdir()
    sessions.mkdir()
    for name in ("20.session", "3.session", "10.session"):
        (sessions / name).write_bytes(b"sqlite")
    assert [s.telegram_user_id for s in discover_account_specs(sessions, 20, root)] == [20, 3, 10]


def test_missing_primary_is_rejected_before_client_creation(tmp_path):
    root = tmp_path / "app"
    sessions = tmp_path / "sessions"
    root.mkdir()
    sessions.mkdir()
    (sessions / "2.session").write_bytes(b"sqlite")
    with pytest.raises(ConfigError, match="primary Telegram account 1"):
        discover_account_specs(sessions, 1, root)


def test_invalid_candidate_is_identified_without_printing_its_name(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    secret_name = "1AABBCCD-secret.session"
    (sessions / secret_name).write_bytes(b"sqlite")
    with pytest.raises(ConfigError) as raised:
        discover_account_specs(sessions, 1, tmp_path / "app")
    assert secret_name not in str(raised.value)
    assert "sha256=" in str(raised.value) and "length=" in str(raised.value)
```

Also cover an empty/missing directory, zero/negative/non-decimal names, a session directory inside the resolved application root, a symlink/reparse point escaping the directory, SQLite sidecars, relative path resolution, non-empty legacy/new conflicts, and ignored-but-warned `TELEGRAM_SESSION_STRING` when the new settings are complete.

- [ ] **Step 2: Run the new tests and confirm the old model fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_session_discovery.py -q`

Expected: FAIL because `telegram_sessions`, `Config.primary_user_id`, and `Config.session_dir` do not exist.

- [ ] **Step 3: Implement the new configuration value types and discovery function**

```python
# transfer_models.py
@dataclass(frozen=True)
class AccountSpec:
    telegram_user_id: int
    session_path: Path = field(repr=False)


# telegram_sessions.py
ACCOUNT_FILE = re.compile(r"^([1-9][0-9]*)\.session$")


def discover_account_specs(session_dir: Path, primary_user_id: int, app_root: Path) -> list[AccountSpec]:
    try:
        directory = Path(session_dir).resolve(strict=True)
        root = Path(app_root).resolve(strict=True)
    except OSError as exc:
        raise ConfigError("session_dir and application root must exist") from exc
    if not directory.is_dir() or directory == root or root in directory.parents:
        raise ConfigError("session_dir must be an existing directory outside the application root")
    specs = []
    for candidate in directory.iterdir():
        if not candidate.name.endswith(".session"):
            continue
        match = ACCOUNT_FILE.fullmatch(candidate.name)
        if match is None:
            digest = hashlib.sha256(candidate.name.encode("utf-8", "surrogatepass")).hexdigest()[:12]
            raise ConfigError(f"invalid .session candidate sha256={digest} length={len(candidate.name)}")
        resolved = candidate.resolve(strict=True)
        if resolved.parent != directory or not resolved.is_file():
            raise ConfigError(f"Telegram session {match.group(1)} must resolve to a direct regular file")
        specs.append(AccountSpec(int(match.group(1)), resolved))
    by_id = {spec.telegram_user_id: spec for spec in specs}
    if primary_user_id not in by_id:
        raise ConfigError(f"primary Telegram account {primary_user_id} has no session file")
    return [by_id[primary_user_id], *sorted(
        (spec for spec in specs if spec.telegram_user_id != primary_user_id),
        key=lambda spec: spec.telegram_user_id,
    )]
```

Change `Config` to require `primary_user_id: int` and `session_dir: Path`, remove runtime `session` and `accounts_file`, and resolve `session_dir` against `config.ini`'s directory. Treat only non-empty legacy keys as conflicts. When explicit new settings are complete, never read the old environment value; log only the obsolete key name.

- [ ] **Step 4: Run configuration tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_session_discovery.py tests/test_transfer_config.py -q`

Expected: PASS.

- [ ] **Step 5: Commit configuration and discovery**

```powershell
git add telegram_sessions.py config.py config.example.ini transfer_models.py tests/test_session_discovery.py tests/test_transfer_config.py
git commit -m "feat: discover Telegram accounts from session files"
```

### Task 2: One SQLite owner and in-memory auxiliary clients

**Files:**
- Create: `tests/test_session_clients.py`
- Modify: `tgio.py:267-430`
- Modify: `requirements.txt:1-8`
- Modify: existing tests constructing `TelegramWorker` directly

**Interfaces:**
- Consumes: `TelegramWorker(api_id, api_hash, session_path, connections, upload_parts=...)`.
- Produces: `_new_control_client()`, `_new_auxiliary_client(*, upload: bool)`, and ordered SQLite-last shutdown.

- [ ] **Step 1: Write lifecycle tests with injected fake client/session factories**

```python
def test_only_control_client_receives_the_sqlite_path(worker_factory, session_file):
    worker = worker_factory(session_file, connections=3)
    worker.start()
    worker.run(worker._download_pool())
    worker.run(worker._upload_client())
    assert worker_factory.sqlite_paths == [str(session_file)]
    assert len(worker_factory.memory_sessions) == 3
    assert len({id(item) for item in worker_factory.memory_sessions}) == 3


def test_auxiliary_clients_disconnect_before_sqlite_owner(worker_factory, session_file):
    worker = worker_factory(session_file, connections=2)
    worker.start()
    worker.run(worker._download_pool())
    worker.run(worker._upload_client())
    worker.stop()
    assert worker_factory.disconnect_order[-1] == "control"
```

Also assert the file is rechecked immediately before construction, `receive_updates=False` is passed to every client, `control.session.save_entities` is false, unauthorized sessions fail, memory serialization is absent from exception text, and recreating an auxiliary derives a fresh session from current control state.

- [ ] **Step 2: Run lifecycle tests and verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_session_clients.py -q`

Expected: FAIL because `TelegramWorker` still accepts and stores a StringSession.

- [ ] **Step 3: Implement the control/auxiliary split**

```python
def _new_control_client(self):
    path = self._session_path.resolve(strict=True)
    if not path.is_file():
        raise RuntimeError(f"Telegram session file for account {self._expected_user_id} is unavailable")
    client = TelegramClient(
        str(path), self._api_id, self._api_hash, receive_updates=False,
    )
    client.session.save_entities = False
    return client


def _new_auxiliary_client(self, *, upload: bool):
    serialized = StringSession.save(self._client.session)
    memory = StringSession(serialized)
    options = {"receive_updates": False}
    if upload:
        options["flood_sleep_threshold"] = 0
    return TelegramClient(memory, self._api_id, self._api_hash, **options)
```

Do not store `serialized` on `AccountSpec`, status objects, or exception objects. Set the local reference to `None` after constructing the memory session. Disconnect pool members and upload client before control; control disconnect is the only SQLite close/commit path.

- [ ] **Step 4: Pin Telethon and run worker/download/upload regression tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_session_clients.py tests/test_read_pace.py tests/test_upload_protocol.py tests/test_thumbnails.py -q`

Expected: PASS with `telethon>=1.44,<2` in `requirements.txt`.

- [ ] **Step 5: Commit session ownership**

```powershell
git add tgio.py requirements.txt tests/test_session_clients.py tests/test_read_pace.py tests/test_upload_protocol.py tests/test_thumbnails.py
git commit -m "feat: load SQLite sessions with one owning client"
```

### Task 3: Account-pool integration and process lock

**Files:**
- Modify: `telegram_sessions.py`
- Modify: `telegram_accounts.py:1-345`
- Modify: `bridge.py:1588-1661`
- Modify: `tests/test_account_pool.py`
- Modify: `tests/test_transfer_status.py`
- Create: `tests/test_session_lock.py`

**Interfaces:**
- Consumes: ordered `AccountSpec` values from Task 1.
- Produces: `SessionDirectoryLock`, primary-by-ID pool construction, credential-path-free status, and `reserve`-ready `AccountRuntime` values for Phase B.

- [ ] **Step 1: Write failing pool and lock tests**

```python
def test_pool_uses_configured_primary_not_a_legacy_zero_account(config, worker_factory):
    pool = TelegramAccountPool.from_config(config, worker_factory=worker_factory)
    assert pool.primary.telegram_user_id == config.primary_user_id
    assert pool.for_read(0) is pool.primary


def test_second_lock_fails_without_opening_sessions(tmp_path):
    first = SessionDirectoryLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(SessionLockError, match="already in use"):
            SessionDirectoryLock(tmp_path).acquire()
    finally:
        first.release()
```

Update existing pool tests to build `AccountSpec(id, path)` and fake workers by path. Assert status contains IDs, `primary`, `online`, `linked`, and limiter data but no label or path.

- [ ] **Step 2: Run pool and lock tests to verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_account_pool.py tests/test_session_lock.py tests/test_transfer_status.py -q`

Expected: FAIL because pool loading still branches between a plaintext session and `accounts_file`, and no lifetime lock exists.

- [ ] **Step 3: Implement primary-by-ID construction and the lifetime lock**

```python
class SessionDirectoryLock:
    def __init__(self, session_dir: Path):
        self.path = Path(session_dir) / ".teledrive-session.lock"
        self._stream = None

    def acquire(self) -> "SessionDirectoryLock":
        self._stream = self.path.open("a+b")
        _lock_one_byte_nonblocking(self._stream)
        return self

    def release(self) -> None:
        if self._stream is not None:
            _unlock_one_byte(self._stream)
            self._stream.close()
            self._stream = None
```

Use `msvcrt.locking` on Windows and `fcntl.flock` on POSIX behind `_lock_one_byte_nonblocking`. `TelegramAccountPool.from_config()` calls `discover_account_specs`; `_runtimes[0]` is primary because discovery orders it first. Remove `load_account_specs`, labels, session redaction-by-string, and zero-ID construction. Scope the lock around pool startup through final pool shutdown in `bridge.main()`.

- [ ] **Step 4: Run account and bridge regression tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_account_pool.py tests/test_session_lock.py tests/test_transfer_status.py tests/test_auth_challenge.py tests/test_bridge_e2e.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the pool migration**

```powershell
git add telegram_sessions.py telegram_accounts.py bridge.py tests/test_account_pool.py tests/test_session_lock.py tests/test_transfer_status.py tests/test_auth_challenge.py tests/test_bridge_e2e.py
git commit -m "feat: start account pool from locked session directory"
```

### Task 4: Secure session creation command

**Files:**
- Create: `sessionctl.py`
- Create: `tests/test_sessionctl.py`
- Modify: `telegram_sessions.py`

**Interfaces:**
- Consumes: API credentials from a bootstrap config parser, `SessionDirectoryLock`, and `--session-dir`.
- Produces: `login_session(config_path: Path, session_dir: Path, client_factory=...) -> Path` and exact Windows/POSIX permission enforcement.

- [ ] **Step 1: Write failing login, atomicity, and permission tests**

```python
def test_login_names_session_from_get_me_and_does_not_edit_config(rig):
    result = rig.login(user_id=42)
    assert result == rig.session_dir / "42.session"
    assert result.exists()
    assert rig.config.read_bytes() == rig.original_config


def test_login_refuses_existing_destination_byte_for_byte(rig):
    destination = rig.session_dir / "42.session"
    destination.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        rig.login(user_id=42)
    assert destination.read_bytes() == b"existing"
```

Also test config-relative CLI path resolution, private staging subdirectory use, cleanup on connect/login/disconnect failure, lock contention before Telethon construction, no secret CLI arguments, Windows allow-ACE policy, POSIX `0700`/`0600`, and redacted output.

- [ ] **Step 2: Run the command tests and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_sessionctl.py -q`

Expected: FAIL because `sessionctl.py` and permission adapters do not exist.

- [ ] **Step 3: Implement `login` and platform permission adapters**

```python
async def _login_to_staging(temporary: Path, api_id: int, api_hash: str, client_factory) -> int:
    client = client_factory(str(temporary), api_id, api_hash)
    await client.start()
    try:
        return int((await client.get_me()).id)
    finally:
        await client.disconnect()


def login_session(config_path: Path, session_dir: Path, *, client_factory) -> Path:
    api_id, api_hash = load_bootstrap_credentials(config_path)
    directory = prepare_private_session_dir(session_dir)
    with SessionDirectoryLock(directory), TemporaryDirectory(dir=directory) as staging:
        temporary = Path(staging) / "pending.session"
        user_id = asyncio.run(_login_to_staging(
            temporary, api_id, api_hash, client_factory,
        ))
        destination = directory / f"{user_id}.session"
        if destination.exists():
            raise FileExistsError(f"Telegram session for account {user_id} already exists")
        restrict_session_file(temporary)
        os.replace(temporary, destination)
        return destination
```

`sessionctl` is a standalone process, so `asyncio.run()` owns its event loop. The CLI accepts `--config` and `--session-dir`, while phone/code/2FA remain Telethon interactive prompts. Successful output contains only user ID and destination; errors never contain input credentials.

- [ ] **Step 4: Run login and lock tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_sessionctl.py tests/test_session_lock.py -q`

Expected: PASS.

- [ ] **Step 5: Commit session creation tooling**

```powershell
git add sessionctl.py telegram_sessions.py tests/test_sessionctl.py tests/test_session_lock.py
git commit -m "feat: create private Telegram session files"
```

### Task 5: Legacy plaintext migration

**Files:**
- Modify: `sessionctl.py`
- Modify: `telegram_sessions.py`
- Create: `tests/test_session_migration.py`

**Interfaces:**
- Consumes: legacy `[telegram].session`, environment fallback, env-file fallback, or ordered `accounts_file`.
- Produces: `migrate_legacy_sessions(config_path, session_dir, client_factory=...) -> MigrationResult` and a new atomic `config.ini` only after all accounts validate.

- [ ] **Step 1: Write failing migration transaction tests**

```python
def test_accounts_file_is_authoritative_and_first_account_becomes_primary(rig):
    rig.write_accounts([(20, "s20"), (3, "s3")])
    rig.write_config(session="ignored", accounts_file=rig.accounts_file)
    result = rig.migrate({"s20": 20, "s3": 3, "ignored": 99})
    assert result.primary_user_id == 20
    assert sorted(path.name for path in rig.session_dir.glob("*.session")) == ["20.session", "3.session"]
    assert "session_dir" in rig.config.read_text("utf-8")
    assert "accounts_file" not in rig.config.read_text("utf-8")


def test_any_account_failure_preserves_original_config(rig):
    before = rig.config.read_bytes()
    rig.write_accounts([(1, "good"), (2, "bad")])
    with pytest.raises(RuntimeError, match="account 2"):
        rig.migrate({"good": 1, "bad": RuntimeError("unauthorized")})
    assert rig.config.read_bytes() == before
```

Also cover config > process environment > env-file precedence for the one-account source, duplicate actual IDs, configured/actual mismatch, no plaintext backup, interrupted finalization rerun with matching DC/auth key, conflict with a different existing destination, obsolete environment-source warning by key name only, and cleanup of staged files.

- [ ] **Step 2: Run migration tests and confirm failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_session_migration.py -q`

Expected: FAIL because `migrate` is not implemented.

- [ ] **Step 3: Implement staged conversion and config-last replacement**

```python
@dataclass(frozen=True)
class MigrationResult:
    primary_user_id: int
    session_dir: Path
    account_ids: tuple[int, ...]
    obsolete_sources: tuple[str, ...]


def copy_string_session_to_sqlite(raw: str, destination: Path) -> None:
    source = StringSession(raw)
    target = SQLiteSession(str(destination))
    try:
        target.set_dc(source.dc_id, source.server_address, source.port)
        target.auth_key = source.auth_key
        target.save()
    finally:
        target.close()
```

For every staged SQLite file, connect and call `get_me()` before finalization. Reject duplicate actual IDs. Finalize sessions first; then use a same-directory temporary config, `flush`, `os.fsync`, and `os.replace`. A rerun may adopt an existing destination only when its DC/auth key matches the source and it authenticates as the expected user. Never include `raw` in exceptions or reprs.

- [ ] **Step 4: Run migration and configuration tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_session_migration.py tests/test_session_discovery.py tests/test_sessionctl.py -q`

Expected: PASS.

- [ ] **Step 5: Commit migration**

```powershell
git add sessionctl.py telegram_sessions.py tests/test_session_migration.py tests/test_session_discovery.py tests/test_sessionctl.py
git commit -m "feat: migrate plaintext Telegram sessions to SQLite"
```

### Task 6: Session documentation and complete Phase A regression

**Files:**
- Modify: `.gitignore`
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Delete: `accounts.example.json`
- Modify: `tests/test_account_pool.py`
- Modify: `tests/test_bridge_e2e.py`
- Modify: `tests/test_read_pace.py`
- Modify: `tests/test_thumbnails.py`
- Modify: `tests/test_transfer_status.py`
- Modify: `tests/test_upload_album.py`
- Modify: `tests/test_upload_engine.py`
- Modify: `tests/test_upload_protocol.py`
- Modify: `tests/test_upload_scheduler.py`

**Interfaces:**
- Consumes: Tasks 1-5.
- Produces: one documented session-directory workflow and no runtime plaintext session path.

- [ ] **Step 1: Add a repository-wide regression assertion**

```python
def test_runtime_has_no_plaintext_session_configuration():
    assert "TELEGRAM_SESSION_STRING" not in Path("config.py").read_text("utf-8")
    assert "accounts_file" not in Path("telegram_accounts.py").read_text("utf-8")
    assert 'get("telegram", "session")' not in Path("config.py").read_text("utf-8")
```

- [ ] **Step 2: Run the assertion and verify remaining legacy references fail it**

Run: `.venv\Scripts\python.exe -m pytest tests/test_session_discovery.py::test_runtime_has_no_plaintext_session_configuration -q`

Expected: FAIL until all runtime legacy references are removed.

- [ ] **Step 3: Update examples, operations, and ignore rules**

Document this exact minimal configuration:

```ini
[telegram]
api_id =
api_hash =
primary_user_id = 123456789
session_dir = D:\TeleDriveSessions
```

Add `*.session-journal`, `*.session-wal`, and `*.session-shm` to `.gitignore`. Explain that the files are unencrypted bearer credentials, the current exposed credential must be revoked rather than migrated, every file is `<user_id>.session`, and account additions require restart. Remove `accounts.example.json` only after copying its linked-account and primary-account safety notes into `README.md` and `CLAUDE.md`.

- [ ] **Step 4: Run Phase A and full offline regression**

Run: `.venv\Scripts\python.exe -m pytest tests/test_session_discovery.py tests/test_session_clients.py tests/test_session_lock.py tests/test_sessionctl.py tests/test_session_migration.py tests/test_account_pool.py tests/test_auth_challenge.py tests/test_account_routing.py tests/test_routed_metadata.py -q`

Then run: `.venv\Scripts\python.exe -m pytest -q`

Expected: both commands PASS.

- [ ] **Step 5: Commit Phase A documentation and compatibility updates**

```powershell
git add .gitignore README.md CLAUDE.md tests/test_account_pool.py tests/test_bridge_e2e.py tests/test_read_pace.py tests/test_thumbnails.py tests/test_transfer_status.py tests/test_upload_album.py tests/test_upload_engine.py tests/test_upload_protocol.py tests/test_upload_scheduler.py
git rm accounts.example.json
git commit -m "docs: switch setup to Telegram session directory"
```

---

## Phase B: Idle premium-flood segment failover

This phase ports the failover invariants from the browser/GramJS spec into the local Python bridge. It applies to every fresh transfer whose existing protocol decision has `force_big=True`, including a one-segment big upload; it does not move file bytes into the TeleDrive FastAPI service.

### Task 7: Account activity and effective/physical speed tracking

**Files:**
- Create: `upload_activity.py`
- Create: `tests/test_upload_activity.py`
- Modify: `transfer_models.py`

**Interfaces:**
- Produces: `AttemptLease`, `IdleSpeedSnapshot`, `UploadSpeedTracker`, `AccountActivityRegistry`, and immutable activity snapshots used by the pool and scheduler.
- Consumes: monotonic clock and account IDs; no Telethon dependency.

- [ ] **Step 1: Write deterministic activity and speed tests**

```python
def test_idle_requires_no_jobs_rpcs_or_reservation(clock):
    registry = AccountActivityRegistry(clock=clock)
    registry.add_account(1)
    assert registry.snapshot(1).idle
    registry.begin_job(1, "small:1")
    assert not registry.snapshot(1).idle
    registry.end_job(1, "small:1")
    registry.request_started(1, "rpc:1")
    assert not registry.snapshot(1).idle
    registry.request_settled(1, "rpc:1")
    assert registry.reserve_if_idle(1, "task:1")
    assert not registry.snapshot(1).idle


def test_idle_snapshot_uses_unique_effective_bytes_not_physical_retries(clock):
    tracker = UploadSpeedTracker(clock=clock, window=30, snapshot_ttl=300)
    assert tracker.record_effective(1, "work", 0, 300)
    assert not tracker.record_effective(1, "work", 0, 300)
    tracker.record_physical(1, "work", 0, 300)
    tracker.record_physical(1, "work", 0, 300)
    snapshot = tracker.freeze_idle_snapshot(1)
    assert snapshot.bytes_per_second == 10
```

Also test fixed 30-second denominators, no snapshot without successful effective bytes, five-minute expiration, invalidation on new work, revoked physical success not becoming effective, attempt-specific live speed, and counters never becoming negative under repeated cleanup.

- [ ] **Step 2: Run tests and verify the module is missing**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_activity.py -q`

Expected: FAIL because `upload_activity.py` does not exist.

- [ ] **Step 3: Implement thread-safe trackers and shared lease identity**

```python
@dataclass(frozen=True)
class AttemptLease:
    task_id: str
    attempt_id: int
    account_id: int


@dataclass(frozen=True)
class IdleSpeedSnapshot:
    bytes_per_second: float
    created_at: float
    expires_at: float


class UploadSpeedTracker:
    def record_effective(self, account_id: int, work_id: str, part_index: int, nbytes: int) -> bool:
        key = (work_id, part_index)
        with self._lock:
            if key in self._effective_parts:
                return False
            self._effective_parts.add(key)
            self._effective[account_id].append((self._clock(), nbytes))
            self._work_effective[work_id].append((self._clock(), nbytes))
            return True

    def live_speed(self, work_id: str) -> float:
        with self._lock:
            return self._window_bytes(self._work_effective[work_id]) / self.window
```

`AccountActivityRegistry` owns `active_byte_upload_jobs`, `in_flight_upload_rpcs`, `reserved_task_id`, and idle snapshot per account under one `threading.Condition`. Create/revoke snapshots only on transitions defined in the spec.

- [ ] **Step 4: Run tracker tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_activity.py -q`

Expected: PASS.

- [ ] **Step 5: Commit activity tracking**

```powershell
git add upload_activity.py transfer_models.py tests/test_upload_activity.py
git commit -m "feat: track account upload activity and speed"
```

### Task 8: Frozen and cautious premium-flood pacing

**Files:**
- Modify: `upload_limiter.py:134-288`
- Modify: `tests/test_upload_limiter.py`

**Interfaces:**
- Consumes: existing `AdaptiveUploadLimiter.flood(seconds, premium=...)`, `pace()`, and `success()` calls.
- Produces: `PacerMode.NORMAL/FROZEN/CAUTIOUS`, a 60-second post-resume clean window, and cautious-only ramping.

- [ ] **Step 1: Add failing premium-mode tests**

```python
def test_premium_flood_freezes_without_lowering_rate(limiter, clock):
    before = limiter.snapshot().rate
    limiter.flood(17, premium=True)
    assert limiter.snapshot().mode == "frozen"
    assert limiter.snapshot().rate == before
    clock.advance(100)
    limiter.success(0)
    assert limiter.snapshot().rate == before


def test_clean_window_starts_on_first_post_wait_send(limiter, clock):
    limiter.flood(10, premium=True)
    clock.advance(30)
    assert limiter.snapshot().mode == "frozen"
    asyncio.run(limiter.pace())
    clock.advance(59.9)
    limiter.success(0)
    assert limiter.snapshot().mode == "frozen"
    clock.advance(0.1)
    limiter.success(0)
    assert limiter.snapshot().mode == "cautious"
```

Also assert any ordinary or premium flood resets the clean window, ordinary flood still changes rate/ceiling, cautious increases no more than 0.1 every 30 seconds, frozen/cautious state is not persisted, and a new limiter starts normal even when restoring rate state.

- [ ] **Step 2: Run limiter tests and verify the missing states fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_limiter.py -q`

Expected: FAIL because limiter snapshots have no `mode` and premium waits do not block success ramp.

- [ ] **Step 3: Implement the session-only pacer state machine**

```python
class PacerMode(str, Enum):
    NORMAL = "normal"
    FROZEN = "frozen"
    CAUTIOUS = "cautious"


def flood(self, seconds, *, premium=False):
    now = self.now()
    if premium:
        self._mode = PacerMode.FROZEN
        self._clean_window_start = None
        self._last_flood_at = now
    else:
        self._apply_ordinary_flood(now, seconds)
        if self._mode is not PacerMode.NORMAL:
            self._mode = PacerMode.FROZEN
            self._clean_window_start = None
    self._extend_penalty(now, seconds)
```

In `pace()`, set `_clean_window_start` only when a paced send is actually admitted after `_penalty_until`. In `success()`, transition frozen to cautious only after 60 clean seconds and use `slow_step=0.1`, `slow_interval=30` forever for that process session. Keep persisted schema limited to rate and ceiling.

- [ ] **Step 4: Run limiter and upload-part flood tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_limiter.py tests/test_upload_parts.py::test_send_part_classifies_premium_flood_before_retrying tests/test_upload_parts.py::test_send_part_honors_premium_flood_wait_longer_than_old_cap -q`

Expected: PASS.

- [ ] **Step 5: Commit pacer states**

```powershell
git add upload_limiter.py tests/test_upload_limiter.py tests/test_upload_parts.py
git commit -m "feat: freeze upload ramp after premium floods"
```

### Task 9: Lease-aware part RPC observation and drain-safe cancellation

**Files:**
- Modify: `tgupload.py:140-338`
- Modify: `tgio.py:817-860`
- Create: `tests/test_upload_attempts.py`
- Modify: `tests/test_upload_parts.py`

**Interfaces:**
- Consumes: optional `UploadObserver`, optional worker-loop `asyncio.Event` revoke signal, and 120-second request deadline.
- Produces: `AttemptRevoked`, exact request start/success/settle callbacks, cancelable admission/retry waits, and no hard cancellation of a sent MTProto RPC.

- [ ] **Step 1: Write failing cancellation and telemetry tests**

```python
def test_revocation_stops_new_parts_but_drains_sent_rpc(rig):
    attempt = rig.start_attempt(parts=3, workers=1)
    rig.sender.wait_until_started(0)
    attempt.revoke()
    rig.sender.succeed(0)
    with pytest.raises(AttemptRevoked):
        attempt.result()
    assert rig.sender.started_parts == [0]
    assert rig.observer.events == [
        ("start", 0), ("success", 0), ("settle", 0),
    ]


def test_late_success_after_deadline_is_physical_only(rig):
    attempt = rig.start_attempt(parts=1, rpc_timeout=120)
    rig.clock.advance(120)
    with pytest.raises(TimeoutError):
        attempt.result()
    rig.sender.succeed(0)
    assert rig.observer.physical_bytes == rig.part_size
    assert rig.observer.logical_bytes == 0
```

Also test revocation during limiter pacing, worker-slot waiting, and retry delay; explicit premium-flood callback; duplicate success; `finally` counter balance; and normal uploads with `observer=None` retaining old behavior.

- [ ] **Step 2: Run attempt tests and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_attempts.py -q`

Expected: FAIL because part uploads have no observer, revoke token, or wrapper deadline.

- [ ] **Step 3: Add the concrete observer protocol and cancellation boundaries**

```python
class UploadObserver(Protocol):
    def request_started(self, part_index: int, nbytes: int) -> None:
        pass

    def request_succeeded(self, part_index: int, nbytes: int) -> None:
        pass

    def premium_flood(self, seconds: float) -> None:
        pass

    def request_settled(self, part_index: int) -> None:
        pass

    def late_request_succeeded(self, part_index: int, nbytes: int) -> None:
        pass


def _observe_late_rpc(rpc, observer, part_index: int, nbytes: int) -> None:
    if observer is None:
        return

    def settled(future) -> None:
        if not future.cancelled() and future.exception() is None:
            observer.late_request_succeeded(part_index, nbytes)

    rpc.add_done_callback(settled)


async def send_part(sender_of, request, gate, label, *, part_index, nbytes,
                    observer=None, revoked=None, rpc_timeout=120.0):
    await _cancelable_checkpoint(revoked)
    sender = sender_of()
    observer and observer.request_started(part_index, nbytes)
    rpc = asyncio.ensure_future(sender.send(request))
    try:
        await asyncio.wait_for(asyncio.shield(rpc), timeout=rpc_timeout)
        observer and observer.request_succeeded(part_index, nbytes)
    except asyncio.TimeoutError:
        _observe_late_rpc(rpc, observer, part_index, nbytes)
        raise
    finally:
        observer and observer.request_settled(part_index)
```

Preserve the existing flood classification and retry loop around this request boundary; call `observer.premium_flood(seconds)` before the premium wait is handed to the limiter. Race the revoke event only against work that has not reached `sender.send`: semaphore admission, limiter pace, and retry sleep. Once `sender.send` starts, shield it from lease cancellation and wait for result or deadline. The late callback records physical bytes only; it cannot update effective progress or revive the expired wrapper.

- [ ] **Step 4: Run part protocol and attempt tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_attempts.py tests/test_upload_parts.py tests/test_upload_protocol.py -q`

Expected: PASS.

- [ ] **Step 5: Commit lease-aware part execution**

```powershell
git add tgupload.py tgio.py tests/test_upload_attempts.py tests/test_upload_parts.py tests/test_upload_protocol.py
git commit -m "feat: make upload parts observable and revoke-aware"
```

### Task 10: Central segment scheduler state machine and score

**Files:**
- Create: `segment_scheduler.py`
- Create: `tests/test_segment_scheduler.py`

**Interfaces:**
- Consumes: `AttemptLease`, planned `(index, offset, size)` segments, account activity snapshots, and speed tracker values.
- Produces: `SegmentTask`, `SegmentState`, `SegmentScheduler`, generation-checked events, migration selection, and finalize CAS.

- [ ] **Step 1: Write failing pure scheduler tests**

```python
def test_candidate_requires_age_premium_flood_and_strict_score(scheduler, clock):
    lease = scheduler.activate("task", account_id=2)
    scheduler.part_succeeded(lease, part_index=0, nbytes=100)
    scheduler.premium_flood(lease, seconds=30)
    clock.advance(30)
    scheduler.set_idle_snapshot(account_id=1, speed=20, age=0)
    scheduler.set_live_speed("task", 10)
    scheduler.set_remaining_ratio("task", 1.0)
    assert scheduler.score(1, "task") == 2
    assert not scheduler.try_migrate(1, "task")
    scheduler.set_idle_snapshot(account_id=1, speed=20.1, age=0)
    assert scheduler.try_migrate(1, "task")


def test_migration_revokes_before_replacement_and_old_lease_cannot_finalize(scheduler):
    old = scheduler.activate("task", account_id=2)
    scheduler.qualify_for_migration(old, replacement_id=1)
    task = scheduler.task("task")
    assert task.state is SegmentState.MIGRATING
    assert task.attempt_id == old.attempt_id + 1
    assert task.current_account_id is None
    assert not scheduler.grant_finalize(old)
```

Also test current-speed zero only after qualification, maximum-score selection, one migration maximum, attempted-account exclusion, two idle accounts racing one task, finalize/migration mutual exclusion, stale progress/error/completion rejection, logical reset with physical retention, terminal idempotence, and qualification timer cleanup.

- [ ] **Step 2: Run scheduler tests and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_segment_scheduler.py -q`

Expected: FAIL because `segment_scheduler.py` does not exist.

- [ ] **Step 3: Implement the locked task state machine**

```python
class SegmentState(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    MIGRATING = "migrating"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class SegmentTask:
    task_id: str
    file_job_id: str
    index: int
    offset: int
    size: int
    state: SegmentState = SegmentState.PENDING
    attempt_id: int = 0
    current_account_id: Optional[int] = None
    migration_count: int = 0
    attempted_account_ids: set[int] = field(default_factory=set)
    attempt_started_at: Optional[float] = None
    attempt_in_flight_rpcs: dict[int, int] = field(default_factory=dict)
    draining_attempt_id: Optional[int] = None
    logical_uploaded_bytes: int = 0
    completed_part_indices: set[int] = field(default_factory=set)
    result: Optional[UploadedPart] = None
```

All mutating methods acquire one `threading.Condition`. `_valid(lease, allowed_states)` performs task ID, attempt ID, account ID, non-terminal, and state checks. `commit_migration()` contains no blocking call: reserve target through the injected synchronous reservation callback, increment generation, clear logical state, and signal the old attempt. `grant_finalize()` changes ACTIVE to FINALIZING under the same lock.

Terminal cleanup is idempotent: cancel qualification timers, detach lease callbacks, and release exactly the resource the task currently owns (an active job or a reservation, never both). Repeated completion/error callbacks and late revoked-attempt events cannot decrement counters twice or change a terminal result.

- [ ] **Step 4: Run scheduler tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_segment_scheduler.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the scheduler core**

```powershell
git add segment_scheduler.py tests/test_segment_scheduler.py
git commit -m "feat: add lease-safe segment scheduler"
```

### Task 11: Exact idle reservations in the account pool

**Files:**
- Modify: `telegram_accounts.py:63-311`
- Modify: `upload_activity.py`
- Modify: `tests/test_account_pool.py`
- Create: `tests/test_account_failover.py`

**Interfaces:**
- Consumes: `AccountActivityRegistry` from Task 7 and scheduler reservation callbacks from Task 10.
- Produces: `UploadLease.activity`, `try_reserve_idle(account_id, task_id)`, `activate_reservation(account_id, task_id)`, and `release_reservation(account_id, task_id)`.

- [ ] **Step 1: Write failing reservation/account-activity tests**

```python
def test_busy_account_with_free_file_slots_cannot_be_failover_target(pool):
    with pool.acquire_upload(work_id="small:1") as runtime:
        assert runtime.telegram_user_id == 1
        assert not pool.try_reserve_idle(1, "segment:2")


def test_reservation_blocks_normal_work_and_activation_counts_once(pool):
    assert pool.try_reserve_idle(1, "segment:2")
    with pool.acquire_upload(timeout=0) as other:
        assert other.telegram_user_id == 2
    lease = pool.activate_reservation(1, "segment:2")
    assert pool.activity.snapshot(1).active_byte_upload_jobs == 1
    lease.close()
    assert pool.activity.snapshot(1).active_byte_upload_jobs == 0
```

Also cover online/linked gates, missing/expired idle snapshot, in-flight RPC blocking, two threads reserving the same account, reservation cleanup on failure, and snapshots invalidating when normal work begins.

- [ ] **Step 2: Run pool failover tests and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_account_failover.py tests/test_account_pool.py -q`

Expected: FAIL because the pool only exposes anonymous semaphore leases.

- [ ] **Step 3: Implement owned upload leases and synchronous reservations**

```python
class UploadLease:
    def __init__(self, runtime, activity, work_id, *, reserved=False):
        self.runtime = runtime
        self.activity = activity
        self.work_id = work_id
        self.reserved = reserved
        self._closed = False

    def __enter__(self):
        return self.runtime

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.activity.end_job(self.runtime.telegram_user_id, self.work_id)
        self.runtime.file_slots.release()

    def __exit__(self, *_exc):
        self.close()
```

Normal `acquire_upload()` acquires a file slot and begins activity atomically under the pool lock. `try_reserve_idle()` rechecks online, linked, exact idleness, valid snapshot, and exclusion before setting `reserved_task_id`; reservation makes normal selection skip the entire account even though its semaphore has remaining capacity. Activation clears reservation and begins exactly one job while retaining one acquired slot.

- [ ] **Step 4: Run account/activity tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_account_failover.py tests/test_account_pool.py tests/test_upload_activity.py -q`

Expected: PASS.

- [ ] **Step 5: Commit exact reservation support**

```powershell
git add telegram_accounts.py upload_activity.py tests/test_account_failover.py tests/test_account_pool.py
git commit -m "feat: reserve truly idle upload accounts"
```

### Task 12: Execute scheduler attempts through Telegram workers

**Files:**
- Modify: `segment_scheduler.py`
- Modify: `upload_engine.py:349-722`
- Modify: `tgio.py:817-860,1001-1045`
- Modify: `upload_activity.py`
- Create: `tests/test_upload_failover.py`
- Modify: `tests/test_upload_engine.py`
- Modify: `tests/test_upload_album.py`

**Interfaces:**
- Consumes: force-big protocol decisions, `SegmentScheduler`, pool leases/reservations, and observer-enabled worker part uploads.
- Produces: `_upload_big_with_scheduler(request, decision, preview) -> list[UploadedPart]` and `_execute_segment_attempt(task, lease, assignment, preview) -> None`.

- [ ] **Step 1: Write failing end-to-end fake-executor failover tests**

```python
def test_idle_account_restarts_premium_flooded_segment_from_part_zero(rig):
    job = rig.start_big_upload(accounts=(1, 2), segment_size=rig.three_parts)
    rig.account(1).succeed_parts(0, 1)
    rig.account(1).premium_flood(wait=60)
    rig.clock.advance(30)
    rig.make_idle_snapshot(account_id=2, bytes_per_second=100)
    rig.scheduler_tick()
    assert rig.account(1).revoked
    rig.account(1).settle_in_flight()
    assert rig.account(2).started_parts == [0]
    assert rig.account(2).file_id != rig.account(1).file_id
    rig.account(2).finish()
    assert job.result().telegram_user_id == 2


def test_old_attempt_cannot_send_message_after_migration(rig):
    job = rig.start_migrating_upload()
    rig.old_account.finish_all_parts_late()
    rig.replacement.finish()
    result = job.result()
    assert rig.old_account.messages == []
    assert len(rig.replacement.messages) == 1
    assert result.file_id == rig.replacement.document_id
```

Also cover a busy replacement, an idle account with no/expired snapshot, score equal to two, one-account behavior, ordinary flood/timeout/general slowness, migration versus finalize race, old late physical success, drain ignoring old-account unrelated work, replacement failure without a third migration, segment-zero thumbnail ownership, sorted final results, and small/album/thumbnail paths blocking idle while never becoming candidates.

- [ ] **Step 2: Run integration tests and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_failover.py -q`

Expected: FAIL because `_upload_fresh()` still submits permanently account-bound eager futures and workers have no attempt observer.

- [ ] **Step 3: Route every force-big transfer through one scheduler**

```python
def _upload_fresh(self, request):
    decision = decide_protocol(request.logical_size, album_eligible=False)
    preview_cm = _preview_file(request.source, request.mime_type, self.ffmpeg)
    preview = preview_cm.__enter__()
    try:
        if decision.force_big:
            return self._upload_big_with_scheduler(request, decision, preview)
        return [self._upload_segment_once(
            request, 0, decision.segments[0][0], decision.segments[0][1],
            force_big=False, split=False, preview=preview,
        )]
    finally:
        preview_cm.__exit__(None, None, None)
```

For each scheduler attempt, open a new `SegmentReader` so migration starts at offset zero within that logical segment, pass the scheduler observer/revoke signal to `worker.prepare_segment`, and call `grant_finalize(lease)` before `send_uploaded_segment`. Release the byte-upload/file-slot lease after prepare and before the message bucket, preserving current admission semantics. If finalize is denied, discard the handle and never send a message. Only the current attempt for segment index zero uploads/attaches the thumbnail.

Update small, album, album fallback, and thumbnail preparation calls to pass generic account activity observers so they update snapshots and block idle decisions without entering the scheduler candidate set.

- [ ] **Step 4: Run engine, album, scheduler, and routing tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_failover.py tests/test_upload_engine.py tests/test_upload_album.py tests/test_segment_scheduler.py tests/test_account_failover.py tests/test_upload_attempts.py tests/test_account_routing.py -q`

Expected: PASS.

- [ ] **Step 5: Commit scheduler integration**

```powershell
git add upload_engine.py segment_scheduler.py upload_activity.py tgio.py tests/test_upload_failover.py tests/test_upload_engine.py tests/test_upload_album.py
git commit -m "feat: fail over premium-flooded upload segments"
```

### Task 13: Failover progress, status, diagnostics, and full verification

**Files:**
- Modify: `upload_engine.py:32-72,507-613,724-790`
- Modify: `uploadstage.py:52-70,180-240,376-410`
- Modify: `telegram_accounts.py:313-327`
- Modify: `bridge.py:1338-1353`
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Create: `tests/test_failover_logging.py`
- Modify: `tests/test_transfer_status.py`
- Modify: `tests/test_transfer_logging.py`

**Interfaces:**
- Consumes: scheduler task events, effective/physical trackers, limiter modes, and existing status sink.
- Produces: migration-aware logical progress, physical/migration metrics, credential-free account/scheduler status, and diagnostic flood/migration logs.

- [ ] **Step 1: Write failing progress, status, and redaction tests**

```python
def test_progress_may_drop_only_with_migration_generation(status_rig):
    status_rig.progress(task="s0", attempt=1, logical=200)
    with pytest.raises(AssertionError):
        status_rig.progress(task="s0", attempt=1, logical=100)
    status_rig.migrate(task="s0", old_attempt=1, new_attempt=2, abandoned=200)
    status_rig.progress(task="s0", attempt=2, logical=0)
    assert status_rig.detail == "重新分派上傳帳號，該區段將從頭重傳"


def test_status_and_logs_expose_metrics_but_not_credentials(status_rig, caplog):
    status_rig.complete_failover()
    rendered = json.dumps(status_rig.rpc_status()) + caplog.text
    assert "migration_overhead_bytes" in rendered
    assert "physical_transferred_bytes" in rendered
    assert "pacer_mode" in rendered
    for secret in status_rig.secrets_and_paths:
        assert secret not in rendered
```

Also assert flood-cycle logs include account/task/segment/attempt IDs, wait/penalty, mode/rate, live speed, logical bytes, remaining ratio, and accepted physical parts/bytes; migration logs include old/new generations, from/to IDs, snapshot age/speeds, score, abandoned bytes, and migration count. Assert access hashes, auth keys, session material, and JWTs are redacted.

- [ ] **Step 2: Run observability tests and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_failover_logging.py tests/test_transfer_status.py tests/test_transfer_logging.py -q`

Expected: FAIL because current metrics do not distinguish logical, physical, or migration work.

- [ ] **Step 3: Add metrics/status wiring and operating documentation**

```python
@dataclass
class TransferMetrics:
    # Existing timing/protocol fields remain unchanged.
    logical_uploaded_bytes: int = 0
    physical_transferred_bytes: int = 0
    migration_overhead_bytes: int = 0
    migration_count: int = 0
```

Do not remove the existing timing fields. Extend status details with scheduler state only while a transfer is active; terminal queue persistence keeps the final account IDs and redacted failure detail, not ephemeral lease objects or timers. Add the exact candidate constants and operational interpretation to `CLAUDE.md`; add user-facing session-directory setup, migration, status fields, and failover behavior to `README.md`.

- [ ] **Step 4: Run focused verification**

Run: `.venv\Scripts\python.exe -m pytest tests/test_failover_logging.py tests/test_transfer_status.py tests/test_transfer_logging.py tests/test_upload_failover.py tests/test_segment_scheduler.py tests/test_upload_activity.py tests/test_upload_limiter.py tests/test_upload_attempts.py -q`

Expected: PASS.

- [ ] **Step 5: Run the complete offline suite and static repository checks**

Run: `.venv\Scripts\python.exe -m pytest -q`

Expected: PASS with no live Telegram/backend traffic.

Run: `git diff --check`

Expected: no whitespace errors.

Run: `rg -n -i "session\s*[=:]\s*[A-Za-z0-9_-]{40}|authorization:\s*bearer|ey[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\." config.example.ini README.md CLAUDE.md tests telegram_sessions.py telegram_accounts.py tgio.py sessionctl.py upload_activity.py segment_scheduler.py upload_engine.py`

Expected: no real credential values; redaction-test fixtures may use short synthetic markers that do not match the production-secret pattern.

- [ ] **Step 6: Commit observability and documentation**

```powershell
git add upload_engine.py uploadstage.py telegram_accounts.py bridge.py README.md CLAUDE.md tests/test_failover_logging.py tests/test_transfer_status.py tests/test_transfer_logging.py
git commit -m "docs: expose idle segment failover diagnostics"
```

## Optional live acceptance gate

Do not execute this section without explicit permission, disposable test paths, named accounts, an upload-byte ceiling, and cleanup authorization.

1. Revoke the previously exposed StringSession and create fresh `<user_id>.session` files with `sessionctl login`.
2. Restart twice and verify no account requests another login code.
3. Confirm `/rpc/status` shows the intended primary/linked accounts without session paths.
4. Upload one force-big segment with controlled premium-flood injection or a disposable account already subject to premium wait.
5. Verify the source attempt cannot create a message after migration, replacement starts at part zero with a new `file_id`, registered metadata names the replacement account, and downloaded SHA-256 matches the source.
6. Inspect `bridge.log` for SQLite locks, wrong/new/old session-ID warnings, two-message races, leaked credentials, unbalanced activity counters, and the required flood/migration metrics.
7. Record the result as **live-validated** only if every check passes; otherwise retain the documented **offline-verified** status.
