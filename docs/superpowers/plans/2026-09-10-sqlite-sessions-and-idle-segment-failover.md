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
- The control owner must call `get_me()` once at startup and require the returned ID to equal the positive ID encoded in `<telegram_user_id>.session`; mismatch errors expose only expected and actual IDs.
- Pin `telethon>=1.44,<2`; do not attempt a Telethon 2 migration in this change.
- SQLite sessions are unencrypted bearer credentials. Except for `sessionctl`'s explicit success output required by the spec, never expose their directory/file paths, contents, authorization keys, internal StringSession serialization, login codes, 2FA passwords, JWTs, or authorization headers in logs, status, exceptions, causes, or tracebacks.
- The bridge owns `<session_dir>/.teledrive-session.lock` for its lifetime; `sessionctl` must obtain the same lock before login or migration.
- Failover applies only to fresh `force_big=True` segment attempts. Small uploads, album items, and thumbnails affect account idleness but are never migration candidates.
- A candidate must be active for at least 30 seconds, have a `FLOOD_PREMIUM_WAIT` in the most recent 30 seconds, be incomplete, and have `migration_count == 0`.
- A replacement must be online, linked, truly idle, absent from `attempted_account_ids`, and have an effective-speed snapshot no older than 300 seconds.
- Migrate only when `(replacement_speed / current_speed) * remaining_ratio > 2`; equality does not migrate. Candidate current speed zero maps to infinity only after all candidate gates pass.
- Migration increments the attempt generation and removes the old account's finalize right before the replacement can start. A segment migrates at most once and always restarts at part zero with a new Telegram `file_id`.
- Drain waits only for the revoked attempt's RPC counter. It must not wait for unrelated work on the old account. A sent MTProto RPC is never hard-cancelled and has a 120-second wrapper deadline.
- A successful `begin_request()` is the committed-send point of no return: the returned token authorizes exactly one RPC even if migration commits before the worker invokes the sender. Revoke prevents acquiring the next token; every committed token must be sent or settled with an explicit local submission failure, and is included in its original attempt's drain barrier.
- For scheduled segments, `SegmentScheduler` alone owns and closes the actual `UploadLease` instance stored in `SegmentTask.active_upload_lease`. Executors borrow its runtime and report preparation/quiescence; they never close it. During handoff, the old active lease and target reservation are separately owned until the old lease drains.
- Every submitted executor Future is retained and its outcome consumed. Assignment is claimed atomically before submission; an unexpected exception, cancelled Future, or failed submission must wake the coordinator and produce a task or scheduler failure.
- Logical task events require the current generation. Transport settlement and confirmed physical-success events use immutable RPC tokens and remain accepted idempotently for a draining, stale, or terminal generation without reopening task state.
- Effective progress counts first success of each current-generation part and may decrease only at migration. Physical bytes count every explicitly successful part RPC, including late revoked-generation success, and never decrease.
- Premium flood sets limiter mode to `frozen` without lowering its rate. Success cannot ramp while frozen; after the wait, the first actual invocation/scheduling of `sender.send()` starts a 60-second flood-free window. Token acquisition alone does not start that window. `cautious` subsequently ramps by at most 0.1 parts/s every 30 seconds.
- The cross-thread lock order is `SegmentScheduler → TelegramAccountPool → AccountActivityRegistry → UploadSpeedTracker`. Code holding a later lock never calls an earlier layer. The activity registry never invokes callbacks; it returns immutable transition data that callers publish only after releasing every outer lock. Revocation crosses into a worker event loop only through `loop.call_soon_threadsafe(event.set)`.
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
- Produces: `AccountSpec(telegram_user_id: int, session_path: Path)`, `Config.primary_user_id: int`, `Config.session_dir: Path`, `safe_resolve_existing(path, *, kind) -> Path`, and `discover_account_specs(session_dir, primary_user_id, app_root) -> list[AccountSpec]`.

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


def test_missing_session_path_has_no_path_in_exception_chain_or_traceback(tmp_path):
    secret = tmp_path / "private-name" / "sessions"
    with pytest.raises(ConfigError) as raised:
        discover_account_specs(secret, 1, tmp_path)
    rendered = "".join(traceback.format_exception(raised.value))
    assert str(secret) not in rendered
    assert raised.value.__cause__ is None
```

Also cover an empty/missing directory, zero/negative/non-decimal names, a session directory inside the resolved application root, a symlink/reparse point escaping the directory, SQLite sidecars, relative path resolution, non-empty legacy/new conflicts, and ignored-but-warned `TELEGRAM_SESSION_STRING` when the new settings are complete. For every filesystem failure, assert the directory basename and absolute path are absent from `str(exc)`, formatted traceback, `caplog`, and `exc.__cause__`.

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


# config.py: add these required fields to Config; keep the remaining fields unchanged.
primary_user_id: int
session_dir: Path = field(repr=False)


# telegram_sessions.py
ACCOUNT_FILE = re.compile(r"^([1-9][0-9]*)\.session$")


def safe_resolve_existing(path: Path, *, kind: str) -> Path:
    resolved = None
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        pass
    if resolved is None:
        raise ConfigError(f"{kind} is unavailable") from None
    return resolved


def safe_direct_children(directory: Path) -> tuple[Path, ...]:
    children = None
    try:
        children = tuple(directory.iterdir())
    except OSError:
        pass
    if children is None:
        raise ConfigError("Telegram session directory cannot be read") from None
    return children


def discover_account_specs(session_dir: Path, primary_user_id: int, app_root: Path) -> list[AccountSpec]:
    directory = safe_resolve_existing(session_dir, kind="Telegram session directory")
    root = safe_resolve_existing(app_root, kind="application root")
    if not directory.is_dir() or directory == root or root in directory.parents:
        raise ConfigError("session_dir must be an existing directory outside the application root")
    specs = []
    for candidate in safe_direct_children(directory):
        if not candidate.name.endswith(".session"):
            continue
        match = ACCOUNT_FILE.fullmatch(candidate.name)
        if match is None:
            digest = hashlib.sha256(candidate.name.encode("utf-8", "surrogatepass")).hexdigest()[:12]
            raise ConfigError(f"invalid .session candidate sha256={digest} length={len(candidate.name)}")
        resolved = safe_resolve_existing(
            candidate, kind=f"Telegram session for account {match.group(1)}",
        )
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
- Consumes: `TelegramWorker(api_id, api_hash, expected_user_id, session_path, connections, upload_parts=...)`.
- Produces: `_new_control_client()`, `_connect_and_validate_control()`, `_new_auxiliary_client(*, upload: bool)`, `user_id`, and ordered SQLite-last shutdown.

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


def test_control_session_identity_must_match_filename(worker_factory, session_file, caplog):
    worker = worker_factory(session_file, expected_user_id=123, actual_user_id=456)
    with pytest.raises(RuntimeError, match="expected 123, got 456") as raised:
        worker.start()
    rendered = "".join(traceback.format_exception(raised.value)) + caplog.text
    assert str(session_file) not in rendered
    assert raised.value.__cause__ is None


def test_control_start_cancellation_disconnects_and_propagates_cancelled_error(worker_factory):
    async def exercise():
        worker = worker_factory.blocked_connect()
        startup = asyncio.create_task(worker._connect_and_validate_control())
        await worker_factory.connect_entered.wait()
        startup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await startup
        assert worker_factory.disconnect_calls == 1
        assert worker._client is None
    asyncio.run(exercise())
```

Also assert the file is rechecked immediately before construction, `receive_updates=False` is passed to every client, `control.session.save_entities` is false, unauthorized sessions fail, identity validation happens before the worker is marked ready or its username is logged, memory serialization is absent from exception text, and recreating an auxiliary derives a fresh session from current control state. Missing/invalid SQLite errors must pass through an account-ID-scoped sanitizer and have no raw cause, path, or basename in formatted traceback or `caplog`.

- [ ] **Step 2: Run lifecycle tests and verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_session_clients.py -q`

Expected: FAIL because `TelegramWorker` still accepts and stores a StringSession.

- [ ] **Step 3: Implement the control/auxiliary split**

```python
class SessionClientError(RuntimeError):
    pass


class SessionAuthorizationError(SessionClientError):
    pass


class SessionIdentityError(SessionClientError):
    pass


def _new_control_client(self):
    path = safe_resolve_existing(
        self._session_path, kind=f"Telegram session for account {self._expected_user_id}",
    )
    if not path.is_file():
        raise RuntimeError(
            f"Telegram session for account {self._expected_user_id} is unavailable"
        ) from None
    client = TelegramClient(
        str(path), self._api_id, self._api_hash, receive_updates=False,
    )
    client.session.save_entities = False
    return client


async def _connect_and_validate_control(self) -> None:
    client = None
    me = None
    failure = None
    validated = False
    try:
        client = self._new_control_client()
        await client.connect()
        if not await client.is_user_authorized():
            raise SessionAuthorizationError(
                f"Telegram session for account {self._expected_user_id} is not authorized"
            )
        me = await client.get_me()
        actual = int(me.id)
        if actual != self._expected_user_id:
            raise SessionIdentityError(
                f"Telegram session user ID mismatch: expected {self._expected_user_id}, got {actual}"
            )
        validated = True
    except asyncio.CancelledError:
        raise
    except (SessionAuthorizationError, SessionIdentityError) as exc:
        failure = exc
    except Exception as exc:
        failure = SessionClientError(
            f"Telegram session for account {self._expected_user_id} failed "
            f"({type(exc).__name__})"
        )
    finally:
        if client is not None and not validated:
            try:
                await client.disconnect()
            except Exception:
                # Preserve cancellation or the original sanitized failure.
                pass
    if failure is not None:
        raise failure from None
    self._client = client
    self._me = me


def _new_auxiliary_client(self, *, upload: bool):
    serialized = StringSession.save(self._client.session)
    memory = StringSession(serialized)
    options = {"receive_updates": False}
    if upload:
        options["flood_sleep_threshold"] = 0
    return TelegramClient(memory, self._api_id, self._api_hash, **options)
```

Do not store `serialized` on `AccountSpec`, status objects, or exception objects. Set the local reference to `None` after constructing the memory session. Map dependency/filesystem failures to sanitized account-ID-scoped errors outside their `except` blocks so raw exception chaining is absent. Disconnect pool members and upload client before control; control disconnect is the only SQLite close/commit path.

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
        stream = None
        failed = False
        try:
            stream = self.path.open("a+b")
            _lock_one_byte_nonblocking(stream)
        except OSError:
            failed = True
        if failed:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
            raise SessionLockError("Telegram session directory is already in use or unavailable") from None
        self._stream = stream
        return self

    def release(self) -> None:
        if self._stream is not None:
            stream = self._stream
            self._stream = None
            failed = False
            try:
                _unlock_one_byte(stream)
            except OSError:
                failed = True
            try:
                stream.close()
            except OSError:
                failed = True
            if failed:
                raise SessionLockError(
                    "Telegram session directory lock could not be released"
                ) from None


def _lock_one_byte_nonblocking(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        if stream.read(1) == b"":
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_one_byte(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
```

Both Windows lock and unlock explicitly seek to byte zero; the first locker initializes that byte before locking it. Lock/open/unlock errors are converted outside the `except` block to a path-free `SessionLockError`. Add tests for a nonzero file pointer, second-process contention, unlock/reacquire, and formatted traceback/caplog redaction. `TelegramAccountPool.from_config()` calls `discover_account_specs`; `_runtimes[0]` is primary because discovery orders it first, and each worker receives both `spec.telegram_user_id` and `spec.session_path`. Keep the pool's expected/actual ID check as defense in depth after Task 2's worker-level validation. Remove `load_account_specs`, labels, session redaction-by-string, and zero-ID construction. Scope the lock around pool startup through final pool shutdown in `bridge.main()`.

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
- Consumes: API credentials from a bootstrap config parser, `SessionDirectoryLock`, `SessionPermissionPolicy`, and `--session-dir`.
- Produces: `login_session(config_path: Path, session_dir: Path, client_factory=..., permission_policy=...) -> Path`, `WindowsSessionPermissionPolicy`, `PosixSessionPermissionPolicy`, and fail-if-exists atomic promotion.

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
    with pytest.raises(SessionExistsError, match="account 42") as raised:
        rig.login(user_id=42)
    assert destination.read_bytes() == b"existing"
    assert str(destination) not in "".join(traceback.format_exception(raised.value))


def test_start_and_disconnect_failure_preserves_sanitized_start_error(rig):
    rig.client.start_error = RuntimeError(f"login failed at {rig.secret_staging_path}")
    rig.client.disconnect_error = RuntimeError("disconnect also failed")
    with pytest.raises(SessionCtlError, match="RuntimeError") as raised:
        rig.login(user_id=42)
    rendered = "".join(traceback.format_exception(raised.value))
    assert str(rig.secret_staging_path) not in rendered
    assert raised.value.__cause__ is None
```

Also test config-relative CLI path resolution, private staging subdirectory use, cleanup on connect/login/disconnect failure, lock contention before Telethon construction, no secret CLI arguments, a destination-creation race, Windows allow-ACE policy, POSIX `0700`/`0600`, and redacted output/traceback/caplog.

- [ ] **Step 2: Run the command tests and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_sessionctl.py -q`

Expected: FAIL because `sessionctl.py` and permission adapters do not exist.

- [ ] **Step 3: Implement `login` and platform permission adapters**

```python
class SessionCtlError(RuntimeError):
    pass


class SessionExistsError(SessionCtlError):
    pass


class SessionPermissionPolicy(Protocol):
    def prepare_directory(self, path: Path) -> None:
        pass

    def verify_staged_file(self, path: Path) -> None:
        pass


async def _login_to_staging(temporary: Path, api_id: int, api_hash: str, client_factory) -> int:
    client = client_factory(str(temporary), api_id, api_hash)
    primary_error = None
    try:
        await client.start()
        return int((await client.get_me()).id)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            await client.disconnect()
        except BaseException:
            if primary_error is None:
                raise


def _promote_no_replace(temporary: Path, destination: Path) -> None:
    promotion_error = None
    try:
        os.link(temporary, destination)
    except FileExistsError:
        promotion_error = SessionExistsError(
            f"Telegram session for account {destination.stem} already exists"
        )
    except OSError:
        promotion_error = SessionCtlError("could not finalize Telegram session")
    if promotion_error is not None:
        raise promotion_error from None
    cleanup_failed = False
    try:
        temporary.unlink()
    except (OSError, RuntimeError):
        cleanup_failed = True
    if cleanup_failed:
        raise SessionCtlError("Telegram session finalized; staging cleanup failed") from None


def _login_session_impl(config_path: Path, session_dir: Path, *, client_factory,
                  permission_policy: SessionPermissionPolicy) -> Path:
    api_id, api_hash = load_bootstrap_credentials(config_path)
    directory = resolve_session_dir_for_cli(config_path, session_dir)
    permission_policy.prepare_directory(directory)
    with SessionDirectoryLock(directory), TemporaryDirectory(dir=directory) as staging:
        temporary = Path(staging) / "pending.session"
        login_error = None
        try:
            user_id = asyncio.run(_login_to_staging(
                temporary, api_id, api_hash, client_factory,
            ))
        except Exception as exc:
            login_error = SessionCtlError(
                f"Telegram login failed ({type(exc).__name__})"
            )
        if login_error is not None:
            raise login_error from None
        destination = directory / f"{user_id}.session"
        permission_policy.verify_staged_file(temporary)
        _promote_no_replace(temporary, destination)
        return destination


def login_session(config_path: Path, session_dir: Path, *, client_factory,
                  permission_policy: SessionPermissionPolicy) -> Path:
    failure = None
    try:
        return _login_session_impl(
            config_path, session_dir, client_factory=client_factory,
            permission_policy=permission_policy,
        )
    except SessionExistsError as exc:
        failure = SessionExistsError(str(exc))  # This project's path-free message.
    except Exception as exc:
        root = exc
        seen = {id(root)}
        while root.__context__ is not None and id(root.__context__) not in seen:
            root = root.__context__
            seen.add(id(root))
        category = type(root).__name__
        detail = str(root) if isinstance(root, SessionCtlError) else category
        suffix = "; cleanup failed" if root is not exc else ""
        failure = SessionCtlError(f"Telegram session operation failed ({detail}){suffix}")
    # All context exits, unlink, and cleanup have finished before sanitization.
    raise failure from None
```

`sessionctl` is a standalone process, so `asyncio.run()` owns its event loop. `_login_to_staging()` enters cleanup immediately after client construction: a `start()` failure still disconnects, and a secondary disconnect failure never replaces the primary login error. The outer boundary replaces dependency errors after leaving their `except` scope so neither cause nor context is rendered.

The public `login_session()` boundary surrounds the entire implementation, including `_promote_no_replace()`'s `temporary.unlink()` and `TemporaryDirectory.__exit__()`. Apply the same outer boundary to migration. On a context cleanup failure following a primary failure, report only the primary failure category plus a fixed `cleanup failed` flag, never retain raw exception objects in the public error. Add separate injected symlink-loop `RuntimeError`, unlink-failure, and temporary-directory-exit-failure tests, including primary-plus-cleanup failure; check formatted traceback, cause, context, and caplog for paths. A cleanup failure after promotion must leave the completed destination intact and report that finalization may already have occurred; it must not delete or overwrite that session.

`WindowsSessionPermissionPolicy` creates/protects the directory DACL with inheritance disabled and Full Control allow ACEs only for the calling-user SID, `S-1-5-18` (`SYSTEM`), and `S-1-5-32-544` (`BUILTIN\\Administrators`); it audits existing directories before staging and fails closed on every other allow SID. `PosixSessionPermissionPolicy` creates directories as `0700`, files as `0600`, and rejects any existing group/other permission bit. Both adapters are selected by `os.name`, expose no path in failure text, and are injected in tests.

The CLI accepts `--config` and `--session-dir`, while phone/code/2FA remain Telethon interactive prompts. Successful output contains only user ID and final path; errors never contain input credentials or filesystem paths. `_promote_no_replace()` uses a same-filesystem hard-link promotion so concurrent creation cannot overwrite an existing destination; the private staging file is unlinked only after the destination link succeeds.

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

Also cover config > process environment > env-file precedence for the one-account source, duplicate actual IDs, configured/actual mismatch, no plaintext backup, interrupted finalization rerun with matching DC/auth key, conflict with a different existing destination, obsolete environment-source warning by key name only, and cleanup of staged files. Inject failures from `StringSession`, `SQLiteSession`, connect, `get_me`, staged-file promotion, and config replacement; assert legacy values and session/config paths are absent from `str(exc)`, formatted traceback, `caplog`, and `exc.__cause__`.

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

For every staged SQLite file, connect and call `get_me()` before finalization. Reject duplicate actual IDs. Finalize sessions first; then use a same-directory temporary config, `flush`, `os.fsync`, and `os.replace`. A rerun may adopt an existing destination only when its DC/auth key matches the source and it authenticates as the expected user. Catch dependency/filesystem exceptions, retain only an account ID plus exception type/category, exit the `except` block, and raise the sanitized migration error `from None`; never include `raw`, a session path, or a config path in exceptions or reprs.

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
- Produces: `AttemptLease`, `UploadRpcToken`, `IdleSpeedSnapshot`, `FloodCycleSnapshot`, `ActivityChange`, `UploadSpeedTracker`, `AccountActivityRegistry`, and immutable activity snapshots used by the pool and scheduler.
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
    token = UploadRpcToken("small:1", 1, 1, 0, 1)
    registry.request_started(token)
    assert not registry.snapshot(1).idle
    registry.request_settled(token)
    assert registry.reserve_if_idle(1, "task:1")
    assert not registry.snapshot(1).idle


def test_idle_snapshot_uses_unique_effective_bytes_not_physical_retries(clock):
    tracker = UploadSpeedTracker(clock=clock, window=30, snapshot_ttl=300)
    lease = AttemptLease("task", 1, 1)
    token1 = UploadRpcToken("task", 1, 1, 0, 1)
    token2 = UploadRpcToken("task", 1, 1, 0, 2)
    assert tracker.record_effective(lease, 0, 300)
    assert not tracker.record_effective(lease, 0, 300)
    assert tracker.record_physical(token1, 300)
    assert tracker.record_physical(token2, 300)
    snapshot = tracker.freeze_idle_snapshot(1)
    assert snapshot.bytes_per_second == 10


def test_effective_identity_includes_attempt_generation(clock):
    tracker = UploadSpeedTracker(clock=clock, window=30, snapshot_ttl=300)
    old = AttemptLease("task", 1, 1)
    replacement = AttemptLease("task", 2, 2)
    assert tracker.record_effective(old, part_index=0, nbytes=300)
    assert tracker.record_effective(replacement, part_index=0, nbytes=600)
    assert tracker.live_speed(old) == 10
    assert tracker.live_speed(replacement) == 20


def test_premium_flood_cycle_reports_and_resets_physical_totals(clock):
    tracker = UploadSpeedTracker(clock=clock, window=30, snapshot_ttl=300)
    lease = AttemptLease("task", 1, 1)
    tracker.record_physical(UploadRpcToken("task", 1, 1, 0, 1), 512)
    first = tracker.close_premium_flood_cycle(lease, wait_seconds=17)
    second = tracker.close_premium_flood_cycle(lease, wait_seconds=19)
    assert (first.task_accepted_parts, first.task_accepted_bytes) == (1, 512)
    assert (first.account_accepted_parts, first.account_accepted_bytes) == (1, 512)
    assert (second.task_accepted_parts, second.task_accepted_bytes) == (0, 0)
    assert (second.account_accepted_parts, second.account_accepted_bytes) == (0, 0)


def test_account_cycle_includes_other_attempts_and_resets_independently(clock):
    tracker = UploadSpeedTracker(clock=clock, window=30, snapshot_ttl=300)
    a, b = AttemptLease("a", 1, 1), AttemptLease("b", 1, 1)
    tracker.record_physical(UploadRpcToken("a", 1, 1, 0, 1), 512)
    tracker.record_physical(UploadRpcToken("b", 1, 1, 0, 1), 1024)
    first = tracker.close_premium_flood_cycle(a, 17)
    assert (first.account_accepted_parts, first.account_accepted_bytes) == (2, 1536)
    assert (first.task_accepted_parts, first.task_accepted_bytes) == (1, 512)
    second = tracker.close_premium_flood_cycle(b, 19)
    assert (second.account_accepted_parts, second.account_accepted_bytes) == (0, 0)
    assert (second.task_accepted_parts, second.task_accepted_bytes) == (1, 1024)
```

Also test fixed 30-second denominators, no snapshot without successful effective bytes, five-minute expiration, invalidation on new work, revoked physical success not becoming effective, stale and terminal generations retaining physical dedupe, a new RPC token for each retry, per-attempt flood-cycle accepted part/byte reset, and counters never becoming negative under repeated cleanup.

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
class UploadRpcToken:
    task_id: str
    attempt_id: int
    account_id: int
    part_index: int
    sequence: int

    @property
    def lease(self) -> AttemptLease:
        return AttemptLease(self.task_id, self.attempt_id, self.account_id)


@dataclass(frozen=True)
class IdleSpeedSnapshot:
    bytes_per_second: float
    created_at: float
    expires_at: float


@dataclass(frozen=True)
class FloodCycleSnapshot:
    lease: AttemptLease
    wait_seconds: float
    account_accepted_parts: int
    account_accepted_bytes: int
    task_accepted_parts: int
    task_accepted_bytes: int


@dataclass
class MutableTotals:
    parts: int = 0
    bytes: int = 0


@dataclass(frozen=True)
class ActivityChange:
    account_id: int
    changed: bool
    became_idle: bool
    snapshot_changed: bool


class UploadSpeedTracker:
    # __init__ uses defaultdict(MutableTotals) for _account_cycles/_attempt_cycles and
    # sets for _effective_parts and _physical_rpc_tokens.
    def record_effective(self, lease: AttemptLease, part_index: int, nbytes: int) -> bool:
        key = (lease.task_id, lease.attempt_id, part_index)
        with self._lock:
            if key in self._effective_parts:
                return False
            self._effective_parts.add(key)
            self._effective[lease.account_id].append((self._clock(), nbytes))
            self._attempt_effective[lease].append((self._clock(), nbytes))
            return True

    def live_speed(self, lease: AttemptLease) -> float:
        with self._lock:
            return self._window_bytes(self._attempt_effective[lease]) / self.window

    def record_physical(self, token: UploadRpcToken, nbytes: int) -> bool:
        with self._lock:
            if token in self._physical_rpc_tokens:
                return False
            self._physical_rpc_tokens.add(token)
            self._physical[token.account_id].append((self._clock(), nbytes))
            for cycle in (self._account_cycles[token.account_id],
                          self._attempt_cycles[token.lease]):
                cycle.parts += 1
                cycle.bytes += nbytes
            return True

    def close_premium_flood_cycle(
        self, lease: AttemptLease, wait_seconds: float,
    ) -> FloodCycleSnapshot:
        with self._lock:
            account = self._account_cycles.pop(lease.account_id, MutableTotals())
            attempt = self._attempt_cycles.pop(lease, MutableTotals())
            return FloodCycleSnapshot(
                lease, wait_seconds, account.parts, account.bytes,
                attempt.parts, attempt.bytes,
            )
```

Add an `UploadRpcToken.lease` property returning `AttemptLease(task_id, attempt_id, account_id)`. Every retry receives a monotonically increasing `sequence`, so two confirmed sends of the same part are two physical events while effective progress remains unique per `(task_id, attempt_id, part_index)`.

Each premium event atomically closes/resets the account-wide cycle and the emitting attempt's cycle under the tracker lock. Other attempts' task cycles remain intact. Account cycles include small uploads, albums, thumbnails, retries, and confirmed stale-generation success. Task cycles are scoped by `AttemptLease` to avoid combining replacement generations. Late success is counted in the cycle open when it is confirmed. Expose all four counters in `FloodCycleSnapshot`; they correspond to the spec's account/task AcceptedParts/BytesSincePreviousPremiumFlood fields.

`AccountActivityRegistry` owns `active_byte_upload_jobs`, a set of in-flight `UploadRpcToken` values, `reserved_task_id`, and idle snapshot per account under one `threading.Condition`. Every mutator returns `ActivityChange` and never invokes an external callback. `request_started(token)` and `request_settled(token)` are idempotent; `request_settled` accepts stale/terminal tokens and never consults the task generation. Create/revoke snapshots only on transitions defined in the spec. Pool/scheduler callers copy the returned value through their outer critical section and publish it only after every held lock is released.

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
- Produces: `PacerMode.NORMAL/FROZEN/CAUTIOUS`, `mark_send_started()`, a 60-second post-resume clean window, and cautious-only ramping.

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
    clock.advance(120)
    assert limiter.snapshot().clean_window_start is None
    limiter.mark_send_started()
    clock.advance(59.9)
    limiter.success(0)
    assert limiter.snapshot().mode == "frozen"
    clock.advance(0.1)
    limiter.success(0)
    assert limiter.snapshot().mode == "cautious"
```

Also assert revocation after `pace()` but before `mark_send_started()` leaves the clean window unset, any ordinary or premium flood resets the clean window, ordinary flood still changes rate/ceiling, cautious increases no more than 0.1 every 30 seconds, frozen/cautious state is not persisted, and a new limiter starts normal even when restoring rate state.

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


def mark_send_started(self) -> None:
    now = self.now()
    if (
        self._mode is PacerMode.FROZEN
        and now >= self._penalty_until
        and self._clean_window_start is None
    ):
        self._clean_window_start = now
```

`pace()` only performs admission and never starts the clean window. `mark_send_started()` is called exactly once after an RPC task has been created at the `sender.send()` boundary; if mode is frozen, the penalty has elapsed, and no clean window exists, it sets `_clean_window_start=now`. In `success()`, transition frozen to cautious only after 60 clean seconds and use `slow_step=0.1`, `slow_interval=30` forever for that process session. Keep persisted schema limited to rate and ceiling.

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
- Consumes: optional `UploadObserver`, `RevokeHandle.worker_event`, `AttemptLease`, and 120-second request deadline.
- Produces: `AttemptRevoked`, one immutable `UploadRpcToken` per send attempt, exact request start/success/settle callbacks, cancelable admission/retry waits, and no hard cancellation of a sent MTProto RPC.

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


def test_revoke_and_slot_acquire_same_tick_releases_slot():
    async def exercise():
        revoked = asyncio.Event()
        slot = asyncio.BoundedSemaphore(1)
        @asynccontextmanager
        async def acquire_and_revoke():
            await slot.acquire()
            revoked.set()
            try:
                yield
            finally:
                slot.release()
        with pytest.raises(AttemptRevoked):
            async with _cancelable_context(acquire_and_revoke(), revoked):
                pytest.fail("revoked context body must not run")
        await asyncio.wait_for(slot.acquire(), timeout=0.1)
        slot.release()
    asyncio.run(exercise())


def test_committed_token_sends_after_migration_and_drains(rig):
    old = rig.start_paused_after_begin_request()
    token = old.committed_token
    rig.commit_profitable_migration()
    old.resume_sender()
    old.succeed_rpc()
    rig.await_wrapper_settled(token)
    assert old.sent_tokens == [token]
    assert rig.physical_bytes == rig.part_size
    assert rig.logical_bytes == 0
    assert rig.old_attempt_drained
    assert rig.try_begin_another_old_request() is None
```

Also test revocation during limiter pacing, worker-slot waiting, and retry delay; explicit premium-flood callback; duplicate success; `finally` counter balance; and normal uploads with `observer=None` retaining old behavior.

- [ ] **Step 2: Run attempt tests and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_upload_attempts.py -q`

Expected: FAIL because part uploads have no observer, revoke token, or wrapper deadline.

- [ ] **Step 3: Add the concrete observer protocol and cancellation boundaries**

```python
class UploadObserver(Protocol):
    def request_started(self, part_index: int, nbytes: int) -> UploadRpcToken:
        pass

    def request_succeeded(self, token: UploadRpcToken, nbytes: int) -> None:
        pass

    def premium_flood(self, seconds: float, pacer_snapshot: LimiterSnapshot) -> None:
        pass

    def request_settled(self, token: UploadRpcToken) -> None:
        pass

    def late_request_succeeded(self, token: UploadRpcToken, nbytes: int) -> None:
        pass


class RevokeHandle:
    def __init__(self, loop: asyncio.AbstractEventLoop, event: asyncio.Event):
        self._loop = loop
        self.worker_event = event

    @classmethod
    def create_on_worker_loop(cls) -> "RevokeHandle":
        return cls(asyncio.get_running_loop(), asyncio.Event())

    def revoke(self) -> None:
        self._loop.call_soon_threadsafe(self.worker_event.set)


async def _await_cleanup(awaitable):
    cleanup = asyncio.ensure_future(awaitable)
    interrupted = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            interrupted = True
    return cleanup.result(), interrupted


async def _wait_or_revoke(awaitable, revoked: asyncio.Event | None):
    if revoked is None:
        return await awaitable
    work = asyncio.ensure_future(awaitable)
    cancellation = asyncio.create_task(revoked.wait())
    try:
        await asyncio.wait({work, cancellation}, return_when=asyncio.FIRST_COMPLETED)
        if work.done():
            return work.result()  # Completion wins when both finish together.
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        if not work.cancelled() and work.exception() is None:
            return work.result()  # Enter may have completed during cancellation.
        raise AttemptRevoked()
    finally:
        cancellation.cancel()
        if not work.done():
            work.cancel()
        _, interrupted = await _await_cleanup(
            asyncio.gather(work, cancellation, return_exceptions=True),
        )
        if interrupted:
            raise asyncio.CancelledError()


@asynccontextmanager
async def _cancelable_context(cm, revoked: asyncio.Event | None):
    enter = asyncio.ensure_future(cm.__aenter__())
    try:
        value = await _wait_or_revoke(enter, revoked)
        if revoked is not None and revoked.is_set():
            raise AttemptRevoked()
        yield value
    finally:
        # Own the enter task even if _wait_or_revoke exits via cancellation.
        exit_args = sys.exc_info()
        if not enter.done():
            enter.cancel()
        _, interrupted = await _await_cleanup(
            asyncio.gather(enter, return_exceptions=True),
        )
        if not enter.cancelled() and enter.exception() is None:
            _, exit_interrupted = await _await_cleanup(cm.__aexit__(*exit_args))
            interrupted = interrupted or exit_interrupted
        if interrupted:
            raise asyncio.CancelledError()


def _observe_late_rpc(rpc, observer, token, nbytes: int) -> None:
    if rpc is None:
        return

    def settled(future) -> None:
        if (not future.cancelled() and future.exception() is None
                and observer is not None and token is not None):
            observer.late_request_succeeded(token, nbytes)

    rpc.add_done_callback(settled)


async def send_part(sender_of, request, gate, label, *, part_index, nbytes,
                    observer=None, revoked=None, rpc_timeout=120.0):
    while True:
        async with _cancelable_context(gate.slot(), revoked):
            await _wait_or_revoke(gate.pace(), revoked)
            if revoked is not None and revoked.is_set():
                raise AttemptRevoked()
            sender = sender_of()
            if sender is None:
                raise RuntimeError(f"{label}: upload client has no MTProto sender")
            token = observer.request_started(part_index, nbytes) if observer else None
            rpc = None
            try:
                # Token acquisition committed this RPC; do not recheck revoke.
                # Telethon send may return a Future, not a coroutine.
                rpc = asyncio.ensure_future(sender.send(request))
                gate.mark_send_started()
                await asyncio.wait_for(asyncio.shield(rpc), timeout=rpc_timeout)
                if observer:
                    observer.request_succeeded(token, nbytes)
            except asyncio.TimeoutError:
                _observe_late_rpc(rpc, observer, token, nbytes)
                raise
            except Exception as exc:
                flood = _flood_wait(exc)
                if flood is None:
                    raise
                seconds, premium = flood
                gate.flood(seconds, premium=premium)
                if premium and observer:
                    observer.premium_flood(seconds, gate.snapshot())
                continue
            finally:
                if observer:
                    observer.request_settled(token)
        gate.success(0)
        return
```

In `_upload_parts`, wrap worker-slot admission with `_cancelable_context(worker_slots, revoked)` and every exponential retry delay with `_wait_or_revoke(_sleep(...), revoked)`. A successful enter wins over simultaneous revoke; the context's own `try/finally` checks revoke and always exits an acquired slot before propagating `AttemptRevoked`. Use the dedicated context wrapper for resource acquisition; the generic wait helper alone does not own resources. Add direct task-cancellation coverage during admission as well. Production cleanup must retain and await its cleanup Future through repeated cancellation; no detached context-exit task may outlive worker shutdown.

A `RevokeHandle` is created on the worker loop and returned before the attempt starts. Scheduler threads call only `RevokeHandle.revoke()`. `begin_request()` rejects an already-revoked generation under the scheduler lock. If it succeeds, the worker must invoke the sender even if migration happens immediately afterward; this token belongs to the old attempt's drain barrier. Sender creation is inside the token's `try/finally`, so synchronous submission errors settle the token without recording bytes. `mark_send_started()` runs only after successful sender invocation/scheduling; this separate boundary governs pacer timing.

Replace `_upload_parts`' existing sibling `task.cancel()` failure cleanup for committed requests: signal revoke to stop uncommitted work, then await every committed wrapper through result/error/deadline before reporting executor quiescence. A revoked attempt, unexpected executor exception, or shutdown cannot abandon token settlement. The late callback records physical bytes only; it cannot recreate a settled token or update effective progress.

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
- Consumes: `AttemptLease`, `UploadRpcToken`, `RevokeHandle`, `SegmentDescriptor(index, offset, size)`, account activity snapshots, speed tracker values, and synchronous pool reservation/activation callbacks.
- Produces: `SegmentTask`, `SegmentState`, `SchedulerAction`, `SegmentScheduler.version`, `select_next_action() -> Optional[SchedulerAction]`, `next_deadline() -> Optional[float]`, `wait_for_change(observed_version, deadline) -> int`, `begin_request()`, `request_settled()`, `physical_success()`, `notify_premium_flood() -> FloodCycleSnapshot`, generation-checked logical events, `commit_migration()` as the sole ownership-changing migration API, and finalize CAS.

- [ ] **Step 1: Write failing pure scheduler tests**

```python
def test_candidate_requires_age_premium_flood_and_strict_score(scheduler, clock):
    lease = scheduler.activate("task", account_id=2)
    scheduler.part_succeeded(lease, part_index=0, nbytes=100)
    scheduler.notify_premium_flood(lease, seconds=30, pacer_snapshot=scheduler.frozen_snapshot)
    clock.advance(30)
    scheduler.set_idle_snapshot(account_id=1, speed=20, age=0)
    scheduler.set_live_speed(lease, 10)
    assert scheduler.score(1, "task") == 2
    assert scheduler.commit_migration(1, "task") is None
    scheduler.set_idle_snapshot(account_id=1, speed=20.1, age=0)
    assert scheduler.commit_migration(1, "task") is not None


def test_qualification_never_changes_ownership_and_only_commit_migrates(scheduler, clock):
    old = scheduler.activate("task", account_id=2)
    scheduler.notify_premium_flood(old, seconds=30, pacer_snapshot=scheduler.frozen_snapshot)
    clock.advance(30)
    assert scheduler.task("task").state is SegmentState.ACTIVE
    assert scheduler.task("task").attempt_id == old.attempt_id
    scheduler.set_idle_snapshot(account_id=1, speed=30, age=0)
    scheduler.set_live_speed(old, 1)
    commit = scheduler.commit_migration(1, "task")
    assert commit is not None
    task = scheduler.task("task")
    assert task.state is SegmentState.MIGRATING
    assert task.attempt_id == old.attempt_id + 1
    assert task.current_account_id is None
    assert not scheduler.grant_finalize(old)


def test_stale_settlement_drains_tokens_without_changing_logical_state(scheduler, clock):
    old = scheduler.activate("task", account_id=2)
    token = scheduler.begin_request(old, part_index=0)
    scheduler.notify_premium_flood(old, seconds=30, pacer_snapshot=scheduler.frozen_snapshot)
    clock.advance(30)
    scheduler.set_idle_snapshot(account_id=1, speed=30, age=0)
    scheduler.set_live_speed(old, 1)
    scheduler.commit_migration(1, "task")
    assert not scheduler.part_succeeded(old, part_index=0, nbytes=512)
    assert scheduler.physical_success(token, nbytes=512)
    assert scheduler.request_settled(token)
    assert not scheduler.request_settled(token)
    assert scheduler.drained("task", old.attempt_id)
    assert scheduler.task("task").logical_uploaded_bytes == 0


def test_select_next_action_cannot_issue_same_attempt_twice(scheduler):
    scheduler.register_one_pending_segment()
    first = scheduler.select_next_action()
    assert first is not None
    assert scheduler.select_next_action() is None
    assert scheduler.task(first.task_id).active_upload_lease is not None


def test_prepared_and_terminal_cleanup_close_the_same_lease_once(scheduler):
    lease = scheduler.activate("task", account_id=1)
    owned = scheduler.task("task").active_upload_lease
    scheduler.fake_prepare_all_parts(lease)
    scheduler.bytes_prepared(lease)
    scheduler.fail_attempt(lease, "message failed")
    scheduler.attempt_quiesced(lease)
    assert owned.fake_release_count == 1
    assert scheduler.task("task").active_upload_lease is None
```

Also test current-speed zero only after every candidate gate passes, maximum-score selection, one migration maximum, attempted-account exclusion, two idle accounts racing one task, finalize/migration mutual exclusion, stale progress/error/completion rejection, late physical success after terminal state, logical reset with physical retention, terminal idempotence, and qualification deadline cleanup. Add deterministic wakeup tests for: candidate age reaching 30 seconds while a target is already idle, premium-recency expiry, progress/speed change, idle snapshot creation/invalidation/expiry, and migration selection taking priority over assigning a normal pending segment.

The `scheduler` test fixture injects fake activity and speed providers; its `set_idle_snapshot()` and `set_live_speed()` conveniences mutate only those fakes and then call the production `notify_account_changed()` or `notify_speed_changed()` entry point. They are not production scheduler setters.

- [ ] **Step 2: Run scheduler tests and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_segment_scheduler.py -q`

Expected: FAIL because `segment_scheduler.py` does not exist.

- [ ] **Step 3: Implement the locked task state machine**

```python
@dataclass(frozen=True)
class SegmentDescriptor:
    index: int
    offset: int
    size: int


class SegmentState(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    MIGRATING = "migrating"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class SchedulerAction:
    task_id: str
    descriptor: SegmentDescriptor
    account_id: int
    attempt_id: int
    migrated: bool

    @property
    def lease(self) -> AttemptLease:
        return AttemptLease(self.task_id, self.attempt_id, self.account_id)


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
    draining_attempt_id: Optional[int] = None
    logical_uploaded_bytes: int = 0
    completed_part_indices: set[int] = field(default_factory=set)
    attempt_in_flight_rpcs: dict[int, set[UploadRpcToken]] = field(default_factory=dict)
    last_premium_flood_at: Optional[float] = None
    qualification_deadline: Optional[float] = None
    reserved_account_id: Optional[int] = None
    active_upload_lease: Optional[UploadLease] = field(default=None, repr=False)
    active_upload_attempt_id: Optional[int] = None
    executor_quiescent: set[int] = field(default_factory=set, repr=False)
    revoke_handle: Optional[RevokeHandle] = field(default=None, repr=False)
    result: Optional[UploadedPart] = None


def close_active_upload_lease(self, lease: AttemptLease) -> bool:
    # Caller has established prepared or token-empty + quiescent eligibility.
    with self._condition:
        task = self._tasks[lease.task_id]
        owned = task.active_upload_lease
        if (owned is None or task.active_upload_attempt_id != lease.attempt_id
                or owned.runtime.telegram_user_id != lease.account_id):
            return False
        task.active_upload_lease = None
        task.active_upload_attempt_id = None
        change = owned.close()
        self._pending_activity_changes.append(change)
        return True
```

Remove the earlier integer `attempt_in_flight_rpcs` field; the token-set field above is authoritative. All mutating methods acquire one `threading.Condition`. `_valid_current(lease, allowed_states)` performs task ID, attempt ID, account ID, non-terminal, and state checks and is used only by logical progress, errors, completion, and `grant_finalize()`.

`begin_request(lease, part_index)` validates the current lease under the scheduler condition, creates a unique `UploadRpcToken`, inserts it into `attempt_in_flight_rpcs[attempt_id]`, and calls `AccountActivityRegistry.request_started(token)` while preserving the global lock order. `request_settled(token)` does **not** call `_valid_current`: it removes the exact token if present, calls the account registry's idempotent settlement, wakes drain waiters, and returns whether anything changed. `physical_success(token, nbytes)` is an independent idempotent transport path into `UploadSpeedTracker.record_physical`; it accepts stale/draining/terminal tokens but never touches `SegmentTask` logical fields. `part_succeeded(lease, part_index, nbytes)` remains generation-validated and records effective bytes only for the current attempt.

`notify_premium_flood()` updates flood recency, installs the nearest qualification deadline without changing ownership, closes the current `UploadSpeedTracker` flood cycle, and returns its `FloodCycleSnapshot` to the diagnostics sink. Progress/speed changes call `notify_speed_changed()`. Activity changes call `notify_account_changed()` only after releasing all activity/pool locks. `next_deadline()` returns the nearest absolute monotonic candidate-age, premium-recency, or idle-snapshot-expiry boundary; `wait_for_change(observed_version, deadline)` computes the remaining timeout and uses the condition instead of a polling timer. On every wake, `select_next_action()` recomputes migration candidates before normal pending assignments, so an already-idle account can migrate a segment that qualifies later.

Initial assignment initializes `attempt_id=1`. Subsequently `commit_migration(target_account_id, task_id)` is the only method allowed to enter `MIGRATING` or increment `attempt_id`. Under the scheduler condition and with no `await` or I/O, it rechecks target online/linked/idle state, snapshot age, attempted-account exclusion, current candidate gates, latest score strictly greater than 2, and `migration_count == 0`; invokes the synchronous pool reservation callback (which invalidates its snapshot in the same activity transition); sets `MIGRATING`; increments the generation immediately; records the old generation as draining; clears current account/logical bytes/completed parts; sets migration count to one; and adds the target to attempted accounts. It then invokes `RevokeHandle.revoke()`. Qualification/timer methods can never call this transition implicitly.

`SegmentTask.active_upload_lease` stores the single pool-issued object; `active_upload_attempt_id` identifies its owner even after migration increments the task generation. Initial assignment and replacement activation store that exact instance. Executors receive only the borrowed runtime plus `AttemptLease`; they never call `UploadLease.close()`, `__exit__()`, `end_job()`, or `file_slots.release()`.

`bytes_prepared(lease)` accepts only the current ACTIVE generation, verifies all segment bytes are prepared and its token set is empty, then calls `close_active_upload_lease(lease)` under the scheduler condition. Preparation includes any pre-finalize thumbnail byte work. This releases file admission before message admission. Stale preparation cannot close the replacement lease. `grant_finalize()` then competes with migration under that same condition; denied finalize never sends a message.

`close_active_upload_lease(lease)` matches both `active_upload_attempt_id` and account ID, detaches the stored object before calling its one `close()`, and returns false if already detached. Pool close returns transition data; scheduler publishes notifications only after releasing all outer locks. `UploadLease` is declared by Task 11 in `telegram_accounts.py`; use `TYPE_CHECKING` and postponed annotations in Task 10 to avoid a runtime import cycle.

Migration keeps the old active lease in the task while separately holding the target reservation; reservation has no `UploadLease` yet. After the old token set is empty and that executor reports `attempt_quiesced(old_lease)` (no further byte work), scheduler closes the old object exactly once. `activate_reserved_replacement()` then revalidates the same MIGRATING generation/reservation, acquires one target slot, stores the new pool-issued object with the replacement generation, installs its `RevokeHandle`, sets ACTIVE and the new start time, and returns the replacement lease. This barrier never waits on unrelated jobs on the source account.

Terminal transition invalidates logical leases and releases any target reservation immediately. It marks any remaining active lease for scheduler-owned close as soon as the corresponding executor is quiescent and its tokens have settled. Thus terminal state does not erase live transport bookkeeping or release a slot while its borrower is still using it. Reentrant cleanup, preparation, quiescence, and Future-completion callbacks all converge on `close_active_upload_lease()`; they never manufacture a second resource wrapper.

`select_next_action()` claims `(task_id, attempt_id)` in a scheduler-owned set under its condition, installs account/UploadLease ownership, and sets ACTIVE before returning an immutable action. Returning the same attempt again is forbidden, including while it is waiting to enter the thread pool. Migration itself is committed by `commit_migration()`; no replacement action is returned until the drain/quiescence barrier and activation finish. Expose `submission_failed(action, error_category)`, `enqueue_executor_completion(future, action, error_category)`, `take_executor_completions()`, and `executor_finished(action, error_category)`. A submission failure releases an unstarted claim's resources and fails the task. An unexpected Future error aborts the file scheduler, revokes other active work, wakes waiters, and completes cleanup. A successful Future that left its current task ACTIVE without a valid outcome is also a scheduler failure. No Future error is silently discarded as a stale logical event.

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
- Produces: `UploadLease.runtime`, `UploadLease.close() -> ActivityChange`, `try_reserve_idle(account_id, task_id) -> bool`, `activate_reservation(account_id, task_id) -> UploadLease`, and `release_reservation(account_id, task_id) -> ActivityChange`.

- [ ] **Step 1: Write failing reservation/account-activity tests**

```python
def test_busy_account_with_free_file_slots_cannot_be_failover_target(pool):
    with pool.acquire_upload(work_id="small:1") as runtime:
        assert runtime.telegram_user_id == 1
        assert not pool.try_reserve_idle(1, "segment:2")


def test_reservation_blocks_normal_work_and_activation_counts_once(pool):
    before = pool.fake_slots[1].acquire_count
    assert pool.try_reserve_idle(1, "segment:2")
    assert pool.fake_slots[1].acquire_count == before
    assert pool.activity.snapshot(1).idle_snapshot is None
    with pool.acquire_upload(timeout=0) as other:
        assert other.telegram_user_id == 2
    lease = pool.activate_reservation(1, "segment:2")
    assert pool.fake_slots[1].acquire_count == before + 1
    assert pool.activity.snapshot(1).active_byte_upload_jobs == 1
    lease.close()
    assert pool.activity.snapshot(1).active_byte_upload_jobs == 0
```

Also cover online/linked gates, missing/expired idle snapshot, in-flight RPC blocking, two threads reserving the same account, reservation cleanup on failure, and snapshots invalidating when normal work begins. Add a lock-order test with instrumented locks proving reservation follows scheduler → pool → activity, and that an activity notification reaches the scheduler only after both the activity and pool locks have been released.

- [ ] **Step 2: Run pool failover tests and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_account_failover.py tests/test_account_pool.py -q`

Expected: FAIL because the pool only exposes anonymous semaphore leases.

- [ ] **Step 3: Implement owned upload leases and synchronous reservations**

```python
class UploadLease:
    def __init__(self, pool, runtime, work_id):
        self.pool = pool
        self.runtime = runtime
        self.work_id = work_id
        self._closed = False

    def __enter__(self):
        return self.runtime

    def close(self):
        with self.pool._lock:
            if self._closed:
                return ActivityChange(self.runtime.telegram_user_id, False, False, False)
            self._closed = True
            return self.pool._release_upload_locked(self.runtime, self.work_id)

    def __exit__(self, *_exc):
        self.close()
```

Normal `acquire_upload()` acquires a file slot and begins activity atomically under the pool lock. `_release_upload_locked()` requires that same pool lock, ends the specific job and releases exactly one slot, then returns `ActivityChange` without invoking callbacks. `UploadLease.close()` serializes its idempotence check under that lock. The scheduler stores and closes the returned object; small/album callers outside the scheduler may continue owning their own context-managed leases. Notification delivery belongs to the outermost caller after every lock has been released.

Reservation does not acquire a semaphore or create an active job. `try_reserve_idle()` takes pool then activity locks (under scheduler lock in production), rechecks eligibility, and in one activity transition sets `reserved_task_id` and invalidates the snapshot. All normal dispatch, including exact-account album fallback, must reject a reserved account; direct semaphore bypasses are removed. `activate_reservation()` verifies the reservation and eligibility, non-blockingly acquires exactly one file slot, then atomically clears reservation and begins one job. It returns a new `UploadLease` for the scheduler to store. Failure to acquire leaves no partially-created job and leads to explicit task failure/reservation cleanup. Releasing an unused reservation never releases a semaphore. Pool and activity operations never call back into scheduler while locked.

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
- Produces: `_upload_big_with_scheduler(request, decision, preview) -> list[UploadedPart]`, `_run_scheduler_loop(scheduler, executor) -> list[UploadedPart]`, and `_execute_scheduler_action(scheduler, action: SchedulerAction) -> None`.

- [ ] **Step 1: Write failing end-to-end fake-executor failover tests**

```python
def test_idle_account_restarts_premium_flooded_segment_from_part_zero(rig):
    job = rig.start_big_upload(accounts=(1, 2), segment_size=rig.three_parts)
    rig.account(1).succeed_parts(0, 1)
    rig.account(1).premium_flood(wait=60)
    rig.advance_clock(30)
    rig.make_idle_snapshot(account_id=2, bytes_per_second=100)
    rig.wait_until_scheduler_reacts()
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


def test_executor_exception_cannot_leave_task_active_forever(rig):
    rig.executor_raise_before_handler = AssertionError("synthetic executor defect")
    job = rig.start_big_upload(accounts=(1,), segment_size=rig.three_parts)
    with pytest.raises(SchedulerExecutionError):
        job.result(timeout=2)
    assert rig.scheduler.all_terminal()
    assert rig.retained_future_count == 0
    assert rig.active_upload_job_count == 0
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


def _upload_big_with_scheduler(self, request, decision, preview):
    descriptors = [
        SegmentDescriptor(index=index, offset=offset, size=size)
        for index, (offset, size) in enumerate(decision.segments)
    ]
    scheduler = self._new_segment_scheduler(request, descriptors)
    with ThreadPoolExecutor(max_workers=self._segment_concurrency) as executor:
        return self._run_scheduler_loop(scheduler, executor)


def _run_scheduler_loop(self, scheduler, executor):
    submitted = {}

    def on_done(future, action):
        category = None
        try:
            future.result()  # Consume every exception, including cancellation.
        except BaseException as exc:
            category = type(exc).__name__
        scheduler.enqueue_executor_completion(future, action, category)

    observed_version = scheduler.version
    while True:
        for future, action, category in scheduler.take_executor_completions():
            submitted.pop(future)
            scheduler.executor_finished(action, category)
        if scheduler.all_terminal() and not submitted:
            break
        action = (scheduler.select_next_action()
                  if len(submitted) < self._segment_concurrency else None)
        if action is not None:
            try:
                future = executor.submit(self._execute_scheduler_action, scheduler, action)
            except Exception as exc:
                scheduler.submission_failed(action, type(exc).__name__)
                continue
            submitted[future] = action
            future.add_done_callback(lambda done, claim=action: on_done(done, claim))
            continue
        deadline = scheduler.next_deadline()
        observed_version = scheduler.wait_for_change(observed_version, deadline)
    return scheduler.completed_results_by_index()


class SchedulerUploadObserver:
    def __init__(self, scheduler: SegmentScheduler, lease: AttemptLease):
        self.scheduler = scheduler
        self.lease = lease

    def request_started(self, part_index: int, nbytes: int) -> UploadRpcToken:
        return self.scheduler.begin_request(self.lease, part_index)

    def request_succeeded(self, token: UploadRpcToken, nbytes: int) -> None:
        self.scheduler.physical_success(token, nbytes)
        self.scheduler.part_succeeded(self.lease, token.part_index, nbytes)

    def premium_flood(self, seconds: float, pacer_snapshot: LimiterSnapshot) -> None:
        self.scheduler.notify_premium_flood(self.lease, seconds, pacer_snapshot)

    def request_settled(self, token: UploadRpcToken) -> None:
        self.scheduler.request_settled(token)

    def late_request_succeeded(self, token: UploadRpcToken, nbytes: int) -> None:
        self.scheduler.physical_success(token, nbytes)
```

`decision.segments` remains the existing `list[tuple[offset, size]]`; enumerate it exactly once into `SegmentDescriptor(index, offset, size)`. `next_deadline()` converts the nearest monotonic deadline into the condition timeout internally. Worker callbacks for premium flood, effective progress, request settlement, account idle transitions, and errors increment the scheduler version and notify the condition. Therefore production uses no manual polling hook and wakes when an already-idle target becomes useful at the candidate's 30-second deadline.

For each scheduler attempt, open a new `SegmentReader` using the source path, descriptor offset/size and `force_big=True`, then pass the observer and worker revoke event to `worker.prepare_segment`. The executor borrows the scheduler-owned runtime. On completed preparation it calls `scheduler.bytes_prepared(lease)`, which closes the stored upload lease before message admission; the executor then requests `grant_finalize(lease)` before `send_uploaded_segment`. Denied finalize discards the handle. Only the current segment-zero attempt prepares/attaches the thumbnail, with any thumbnail byte work included before `bytes_prepared()`.

The executor catches `AttemptRevoked` as the expected handoff result and drains all committed wrappers in its `finally` before `scheduler.attempt_quiesced(lease)`. It never closes an UploadLease. Expected operational failures go through `fail_attempt()`; programming errors propagate to the retained Future, whose outcome is always consumed by `on_done`. `executor_finished()` independently verifies/quiesces the action when its Future ends, including failures before the executor entered its own `try/finally`. A stale unexpected error aborts the file scheduler rather than disappearing under a generation check. `SchedulerUploadObserver.request_succeeded()` records physical success before attempting current-generation logical progress.

`enqueue_executor_completion()` appends immutable completion data and increments version/notifies under the scheduler condition. It performs no state transitions, diagnostics, or calls back into a worker, so the callback also works when attached to an already-completed Future. Only the coordinator mutates `submitted` and consumes outcomes. On abort it keeps draining retained Futures; `completed_results_by_index()` raises the recorded failure after all borrowers have stopped. Tests inject submit failure, cancelled-before-start Future, exception before executor cleanup setup, and a normally-returning executor that forgot its task outcome.

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
- Modify: `upload_activity.py`
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


def test_premium_flood_log_uses_closed_physical_cycle(status_rig, caplog):
    lease = AttemptLease("segment:0", 1, 2)
    status_rig.confirm_rpc(lease, part_index=0, sequence=1, nbytes=512)
    status_rig.confirm_rpc(lease, part_index=0, sequence=2, nbytes=512)
    cycle = status_rig.premium_flood(lease, wait_seconds=30)
    assert cycle.task_accepted_parts == cycle.account_accepted_parts == 2
    assert cycle.task_accepted_bytes == cycle.account_accepted_bytes == 1024
    assert "task_accepted_parts=2" in caplog.text
    assert "account_accepted_bytes=1024" in caplog.text
    assert "pacer_mode=frozen" in caplog.text
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

Call `gate.flood(seconds, premium=True)` before `UploadObserver.premium_flood(seconds, gate.snapshot())`. The observer forwards the immutable post-flood snapshot to `SegmentScheduler.notify_premium_flood(bound_lease, seconds, pacer_snapshot)`. That method closes the tracker cycle and queues diagnostics using this supplied snapshot (never a cross-thread read of the live limiter); the log therefore shows frozen mode, updated penalty, and the preserved premium rate. Publish diagnostics outside locks. `FloodCycleSnapshot` supplies `account_accepted_parts`, `account_accepted_bytes`, `task_accepted_parts`, and `task_accepted_bytes`; logical progress cannot substitute for any of these physical counters.

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
git add upload_engine.py upload_activity.py uploadstage.py telegram_accounts.py bridge.py README.md CLAUDE.md tests/test_failover_logging.py tests/test_transfer_status.py tests/test_transfer_logging.py
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
