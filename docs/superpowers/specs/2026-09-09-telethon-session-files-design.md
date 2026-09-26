# Telethon SQLite session-file discovery design

**Date:** 2026-09-09
**Status:** Proposed

## Summary

Replace plaintext Telethon `StringSession` values in `config.ini` and
`accounts.json` with one Telethon SQLite `.session` file per Telegram account.
The configured session directory becomes the complete account registry:

```text
D:\TeleDriveSessions\
|-- 123456789.session
|-- 246813579.session
`-- 987654321.session
```

Each filename stem is the account's Telegram user ID. Configuration names the
directory and the one account that acts as primary:

```ini
[telegram]
api_id =
api_hash =
primary_user_id = 123456789
session_dir = D:\TeleDriveSessions
```

One file means one-account operation. More than one file means multi-account
operation. Both cases use the same discovery, validation, worker, routing, and
shutdown paths; there is no separate single-account mode.

Telethon directly opens each account's `.session` file for its control client.
The existing per-account download and upload client pool remains, but auxiliary
clients must not open the same SQLite file concurrently. They receive separate,
non-persistent in-memory session instances derived from the already validated
control session. This is an internal pool detail, not a configuration format or
operator workflow.

## Context and problem

The bridge currently accepts a Telethon `StringSession` in three places:

- `[telegram].session` in `config.ini`;
- `TELEGRAM_SESSION_STRING` or the configured environment file;
- each `accounts[].session` value in `accounts.json`.

The value is a portable serialization of the Telegram authorization key. It is
easy to copy into logs, command output, environment dumps, backups, issue
reports, and source-controlled files. Possession of either a StringSession or a
SQLite `.session` file is sufficient to access the Telegram account; the new
format does not provide encryption at rest. Its benefit is narrower: credentials
become opaque files with a stable lifecycle instead of long secret strings
embedded in general-purpose text configuration.

Telethon normally creates or loads an SQLite session when `TelegramClient`
receives a filename. The bridge cannot simply give that same filename to every
client in its pool. Telethon documents that two clients using one SQLite session
concurrently can raise `sqlite3.OperationalError: database is locked`. This
bridge currently creates one control client, multiple download clients, and one
dedicated upload client per account. The design therefore assigns exactly one
SQLite owner and keeps the remaining pool sessions in memory.

## Goals

- Store every configured Telegram account in one `<telegram_user_id>.session`
  file outside the application root.
- Use one account-discovery model for both one-account and multi-account
  deployments.
- Let Telethon directly load and persist the control client's SQLite session.
- Preserve the existing per-account multi-client download and upload topology
  without concurrent access to one SQLite database.
- Validate filenames against the identity returned by Telegram before an
  account can serve reads or uploads.
- Preserve primary-only backend authentication, linked-account eligibility,
  exact-ID reads, round-robin uploads, and cross-account split files.
- Provide a safe way to create fresh session files and migrate legacy plaintext
  configuration without printing credentials.
- Make invalid configuration and partial account failures observable without
  exposing session contents or local credential paths.

## Non-goals

- Encrypt `.session` files at rest.
- Treat `.session` files as safe to disclose. They remain bearer credentials.
- Keep a permanent plaintext `StringSession` compatibility path in normal
  bridge startup.
- Open one SQLite session file from several `TelegramClient` instances.
- Create a separate persistent session file for every download or upload
  connection.
- Dynamically add or remove accounts while the bridge is running. Discovery
  occurs once at startup.
- Change TeleDrive backend authentication, account linking, upload scheduling,
  Telegram rate limiting, transfer protocols, or metadata schemas.
- Automatically revoke Telegram sessions or delete legacy credential files.
- Defend against a malicious local administrator or an attacker who can replace
  files between operating-system path checks and SQLite open. Such an attacker
  can already read the deliberately unencrypted credential.

## Configuration contract

### Required settings

`[telegram]` retains `api_id` and `api_hash` and adds:

- `primary_user_id`: a positive Telegram user ID;
- `session_dir`: the directory whose direct child `.session` files define all
  configured accounts.

The path may be absolute or relative to the directory containing `config.ini`.
The loader resolves it to an absolute path before discovery. It must exist, be a
directory, and resolve outside the application root. In a source checkout, the
application root is `Path(config.__file__).resolve().parent`; in a frozen build,
it is `Path(sys.executable).resolve().parent`. Both root and session directory
are resolved before the descendant check. The bridge never creates the session
directory implicitly during normal startup.

The resolved path of every discovered account file must remain a direct child
of the resolved session directory. Symlinks and reparse points that resolve
outside it are rejected.

`session`, `TELEGRAM_SESSION_STRING`, and `accounts_file` are removed from the
normal runtime configuration model. If a non-empty legacy setting is present
without the new settings, startup fails with an actionable command for the
session-management tool. It must not silently load the plaintext value. If a
non-empty legacy `session` or `accounts_file` key and the new settings are both
present in configuration files, startup also fails so an obsolete secret cannot
remain unnoticed. A legacy environment variable or environment-file value is
never loaded when the explicit new settings are complete; its key name produces
a removal warning but its presence does not block startup. Empty legacy keys
inherited from an older example file do not create a conflict.

### Account discovery

Every direct child whose name ends exactly in `.session` is treated as an
account candidate and must match `^[1-9][0-9]*\.session$`. The numeric filename
stem is the configured `telegram_user_id`.

Discovery follows these rules:

1. Reject an empty session directory.
2. Reject a `.session` filename whose stem is not a positive decimal integer;
   do not silently ignore a likely mistyped account. Identify it in errors only
   by a short SHA-256 digest of its basename and its character count, never by
   printing the basename itself.
3. Require `<primary_user_id>.session` to exist before constructing a Telethon
   client. This avoids Telethon creating an empty database for a missing file.
4. Place the primary account first in runtime order.
5. Order remaining accounts by numeric Telegram user ID. This makes initial
   round-robin upload selection deterministic without a separate JSON list.
6. Ignore SQLite sidecar and bridge-temporary files because their names do not
   end exactly in `.session`.

Labels are removed from account configuration and status. Telegram user ID is
the stable operational identity. Usernames may be logged after a successful
connection but never become routing keys.

## Runtime types and boundaries

`AccountSpec` becomes a credential-free reference:

```python
@dataclass(frozen=True)
class AccountSpec:
    telegram_user_id: int
    session_path: Path
```

It no longer contains `label` or a serialized session. The full session path is
used internally for startup but is not included in status responses or normal
logs.

`TelegramAccountPool.from_config()` is responsible for directory discovery and
creating ordered specs. `TelegramWorker` accepts an `AccountSpec` or explicit
`session_path`, rather than accepting a session string. Routing interfaces stay
unchanged:

- `primary` returns the runtime whose ID equals `primary_user_id`;
- `for_read(0)` maps historical rows to primary;
- `for_read(nonzero_id)` requires the exact configured and online account;
- `acquire_upload()` considers only online, backend-linked accounts.

There is no branch named or implemented as single-account mode. A pool of
length one naturally provides the old one-account behavior.

## Client and session lifecycle

### Control client

Each account has exactly one control client backed by its SQLite file:

```python
TelegramClient(str(spec.session_path), api_id, api_hash, receive_updates=False)
```

The client is constructed on the worker's existing asyncio loop thread. Before
construction, the bridge confirms that the path still exists and is a regular
file. After connecting it must:

1. confirm the session is authorized;
2. call `get_me()`;
3. compare the actual user ID with the filename stem;
4. mark the account online only after the comparison succeeds.

The primary failing any step stops the pool and fails startup. A secondary
failure disables only that account, records a redacted error, and allows other
accounts to continue. A mismatched session is never renamed automatically
because that could silently change which stored files an account can read.

The bridge does not need Telethon's persistent entity cache for this workload.
`save_entities` is disabled to avoid growing the credential database with names,
phone numbers, and access hashes unrelated to bridge routing. Telegram document
and file-reference caches remain in the bridge's account-keyed cache layer.

### Auxiliary pool clients

After the control client is authorized and its identity matches, the worker
derives a fresh in-memory session instance for each auxiliary client from the
control session's current data-center and authorization-key state. Telethon's
supported `StringSession.save(control.session)` plus a new `StringSession` per
client is the expected implementation for Telethon 1.44, but this serialization
is contained inside one helper and is not part of any public interface.

The dependency is pinned to `telethon>=1.44,<2`; a Telethon 2 migration is a
separate compatibility change. The helper derives a fresh in-memory session
whenever an auxiliary client is first created or recreated, so it observes the
control session's latest persisted DC/auth-key state. Already connected clients
retain their own normal connection state until they disconnect.

Auxiliary clients include:

- every additional member of the download pool;
- the dedicated upload client.

They use `receive_updates=False`, never receive the `.session` path, never share
one mutable session object, and never persist their in-memory serialization.
No session serialization may be attached to an exception, logged, returned by
RPC status, or stored on `AccountSpec`.

This preserves the existing pool topology and throughput behavior. It does not
claim to eliminate all Telegram-side risks of using one authorization key over
multiple simultaneous connections; the existing optional live acceptance test
remains responsible for detecting connection/session warnings with the installed
Telethon version.

### Shutdown

Shutdown occurs in this order per account:

1. stop admitting new work;
2. disconnect every auxiliary download and upload client;
3. drop references to their in-memory sessions;
4. disconnect the control client so Telethon commits and closes SQLite;
5. stop the worker loop.

Python cannot guarantee zeroization of immutable strings. Dropping references
is best effort and must not be documented as secure memory erasure.

## Backend authentication and upload eligibility

The primary account remains the only account that sends the bot-challenge nonce
and obtains the drive JWT. Once primary authentication succeeds, the bridge
loads the backend's linked-account IDs.

- Primary is upload-eligible when online.
- A secondary is upload-eligible only when online and included in the backend
  linked-account IDs.
- A configured, online but unlinked secondary may still read existing rows that
  explicitly reference its ID.
- A missing or offline account never falls back to another session.

These semantics are unchanged by the new discovery source.

## Session-management tool

Add a non-daemon command, `sessionctl.py`, so operators never need to manufacture
or paste a serialized authorization key during normal setup.

The tool loads `api_id` and `api_hash` from the selected configuration file (or
the current environment fallback) using a bootstrap parser that does not require
runtime account settings to be valid yet. `--config` defaults to the normal
`config.ini` path.

A relative `--session-dir` is resolved against the directory containing the
selected configuration file, not the caller's working directory. `login` never
modifies configuration; it prints the resulting path after success. `migrate`
writes the absolute path to `config.ini` only after every account validates.
Failure of either command leaves configuration unchanged.

The bridge holds an exclusive advisory lock at
`<session_dir>/.teledrive-session.lock` for its lifetime. `sessionctl` must
acquire the same lock before creating, migrating, or replacing a session and
fails with a stop-the-bridge message when it cannot. This protects cooperating
project processes; SQLite's own lock error remains the fallback for unrelated
Telethon processes.

### Fresh login

```powershell
python sessionctl.py login --session-dir D:\TeleDriveSessions
```

The command:

1. requires the bridge to be stopped;
2. creates the session directory if necessary;
3. logs in interactively through Telethon using a temporary SQLite file inside
   a private staging subdirectory, not as a direct child discoverable by the
   bridge;
4. obtains the actual Telegram user ID from `get_me()`;
5. disconnects cleanly;
6. atomically renames the completed database to `<user_id>.session`;
7. refuses to overwrite an existing account file.

Phone number, login code, and 2FA password use Telethon's interactive input and
are never accepted as command-line arguments. The command prints only the user
ID and final path.

For the currently exposed credential, the operational path is to revoke the old
Telegram session and use `login`; copying that credential into a new database
would preserve the compromised authorization key.

### Legacy migration

```powershell
python sessionctl.py migrate --config config.ini --session-dir D:/TeleDriveSessions
```

Migration supports the previous resolved one-account session source (including
`[telegram].session`, `TELEGRAM_SESSION_STRING`, and the configured legacy
environment file) and the previous ordered `accounts_file` JSON source. It is a
transition tool only; the bridge runtime does not retain those loaders.

Legacy source precedence exactly matches the old runtime. A non-empty
`accounts_file` is authoritative and supplies the complete ordered account set;
the old single `session` value is not added as another account. Without an
accounts file, a non-empty `[telegram].session` wins, followed by
`TELEGRAM_SESSION_STRING`, then the configured environment-file value. Migration
reports every non-authoritative plaintext source so the operator can remove it,
but never prints its value.

For each legacy StringSession, migration creates a temporary SQLite session,
connects with it, obtains the actual user ID, verifies any previously configured
ID, disconnects, and stages `<actual_user_id>.session`. The former first account
becomes `primary_user_id`; a legacy one-account configuration naturally produces
one session file.

Migration stages and validates every account before changing `config.ini`.
Session files are finalized first and configuration is atomically replaced last,
so a crash cannot make runtime configuration refer to files that were never
created. Migration rejects two legacy inputs that resolve to the same Telegram
user ID. Rerunning after an interrupted finalization may reuse an existing
destination only when its stored DC/auth key equals the staged legacy source and
it authenticates as the expected user ID. Any other existing destination is a
conflict and is never overwritten.

On success, the tool removes `session` and `accounts_file` from `config.ini` and
writes `primary_user_id` and the absolute `session_dir`. It does not retain a
plaintext backup. It cannot mutate a parent-process environment variable or a
separate environment file: if either still defines a legacy session, the tool
names that source (never its value) and instructs the operator to remove it. An
external legacy accounts JSON file is not deleted automatically; the tool prints
a warning that it still contains credentials and should be removed after the new
bridge startup is verified.

No command accepts a StringSession as a CLI argument, prints one, or includes one
in an exception. Temporary databases and generated configuration files use
unpredictable names in their destination directories and are cleaned up on
handled failure.

## File protection and source-control boundaries

SQLite sessions are not encrypted. Documentation and command output must state
that possession of a `.session` file is equivalent to possession of the account
authorization.

The session directory must be outside the application root. On Windows, when
`sessionctl` creates it, inheritance is disabled and access is limited to Full
Control for the calling user, `SYSTEM`, and `BUILTIN\Administrators`; generated
session files inherit that policy. For an existing directory, any allow ACE for
a principal outside those three is broad and causes login/migration to abort
with remediation guidance before credentials are read or created. There is no
continue flag or interactive bypass.

On POSIX, a created directory uses mode `0700` and session files use `0600`.
Group/other permission bits on an existing directory or session file cause the
same fail-closed behavior. Evaluating extended POSIX ACLs is outside this change;
deployments using them must still ensure they do not broaden access. These
permission checks do not imply encryption and do not protect against root or
Windows administrators.

Git ignore rules retain `*.session` and add defense-in-depth patterns for:

```gitignore
*.session-journal
*.session-wal
*.session-shm
```

Runtime validation of an external session directory remains mandatory even
with these ignore rules.

## Logging and status

Logs and `/rpc/status` may expose:

- Telegram user ID;
- the connected Telegram username after successful identity validation;
- whether the account is primary;
- online and linked state;
- limiter state and redacted failure category.
- a short hash and character count for an invalid account candidate.

They must not expose:

- session directory names, filenames, or paths;
- SQLite contents or authorization keys;
- an internally serialized session;
- phone numbers, login codes, 2FA passwords, JWTs, or authorization headers.

Session-related exceptions are converted to account-ID-scoped messages. Raw
exception chaining is suppressed where a dependency exception could contain
constructor arguments or paths.

## Failure behavior

- Missing/non-directory `session_dir`: configuration error before network work.
- Empty directory: configuration error.
- Invalid `.session` filename: configuration error containing only a short hash
  of its basename and the basename's character count.
- Missing primary file: configuration error before Telethon can create it.
- Corrupt or unauthorized primary: stop all started workers and fail startup.
- Corrupt, unauthorized, or mismatched secondary: disable that account only.
- SQLite lock on the control file: fail that account with guidance to stop the
  other bridge/session process; never fall back to an in-memory credential from
  stale configuration.
- Additional download-client creation failure: retain the clients already
  connected; the validated control client is download member zero and therefore
  the minimum usable pool size is one.
- The dedicated upload client is created lazily. Failure to create it fails the
  current upload without taking the control/read client or unrelated accounts
  offline.
- Runtime loss of a routed account: return the existing deterministic account
  unavailable error without cross-account fallback.

## Migration and rollout

Recommended rollout for the current installation:

1. Stop the bridge and any script using its Telegram authorization.
2. Revoke the exposed Telegram session from an official Telegram client.
3. Create a fresh session for primary with `sessionctl login`.
4. Repeat login for every linked secondary account into the same directory.
5. Configure `primary_user_id` and `session_dir`.
6. Remove or unset obsolete plaintext session environment variables; runtime
   rejects a non-empty legacy source alongside the new configuration.
7. Start the bridge and confirm every intended account in `/rpc/status`.
8. Verify an existing file from each secondary and one cross-account split read.
9. Remove any remaining legacy accounts JSON after successful verification.

No live Telegram operation, revocation, or credential deletion occurs as part of
the implementation or offline test suite without explicit operator action.

## Test strategy

All automated tests remain offline and use fake Telethon clients/session objects
unless explicitly marked as live.

### Configuration and discovery tests

- One valid primary file produces a one-runtime pool through the same code path
  as multiple files.
- Multiple files produce primary-first, numeric-secondary order.
- Relative `session_dir` resolves against the config directory.
- Missing directory, repository-contained directory, empty directory, missing
  primary, zero/negative/non-numeric filename, and conflicting legacy settings
  fail before any client is constructed.
- SQLite sidecars and migration temporaries are not discovered as accounts.

### Worker lifecycle tests

- Control client receives the exact SQLite path.
- Auxiliary clients receive distinct in-memory session instances and never the
  SQLite path.
- Actual user ID must equal the filename stem.
- Primary failure tears down all workers; secondary failure is isolated.
- Auxiliary clients disconnect before the control client.
- Control disconnect occurs on the worker loop and persists the SQLite session.
- Status, logs, exceptions, and tracebacks contain neither session serialization
  nor full session paths.
- Multiple auxiliary connections perform concurrent fake transfers without a
  second SQLite opener or a database-lock path.

### Session-management tests

- Fresh login writes `<actual_user_id>.session` only after successful disconnect.
- Existing destination refusal leaves it byte-for-byte unchanged.
- Legacy one-account and multi-account inputs produce the same directory model.
- Config changes occur only after all accounts validate.
- User-ID mismatch, connection failure, invalid input, and interrupted staging
  preserve the original configuration and remove handled temporary files.
- Migration output and raised errors redact every supplied StringSession.
- The generated directory/file ACL policy is exercised behind an injectable
  platform adapter so tests do not alter developer machine permissions.

### Regression tests

- Historical `telegram_user_id = 0` reads use primary.
- Nonzero reads use only their exact worker.
- Unlinked secondary remains readable but upload-ineligible.
- Round-robin selection, per-account limits, albums, and cross-account split
  upload/read behavior remain unchanged.
- Authentication challenge still uses primary only.
- The complete existing offline suite remains green.

### Optional live acceptance

With explicit approval and disposable paths:

1. create two fresh `.session` files through `sessionctl login`;
2. restart twice and confirm neither account requests a new login code;
3. confirm status reports primary and linked secondary without paths;
4. upload a file large enough to split across both accounts;
5. read it back through WebDAV and compare SHA-256;
6. inspect logs for SQLite locks, wrong/new/old session-ID warnings, credential
   material, and unexpected reconnects.

Passing the offline suite is sufficient to mark implementation complete. The
design must be described as **offline-verified** until this result is recorded;
only then may documentation call it **live-validated** against real Telegram.

## Expected implementation surface

The implementation is expected to modify:

- `config.py` and `config.example.ini`;
- `transfer_models.py`;
- `telegram_accounts.py`;
- `tgio.py`;
- `bridge.py` startup/status wiring if required;
- `.gitignore`, `README.md`, and `CLAUDE.md`;
- account-pool, authentication, configuration, and routed-transfer tests.

It is expected to add:

- `sessionctl.py` or a focused module plus thin CLI;
- unit tests for session discovery, lifecycle, and migration.

`accounts.example.json` becomes obsolete and is removed after its useful safety
instructions have been incorporated into the new documentation.

## Acceptance criteria

The change is complete when:

1. Runtime account discovery depends only on `primary_user_id`, `session_dir`,
   and valid `<telegram_user_id>.session` files.
2. A directory containing only primary behaves as a normal one-account pool;
   adding valid secondary files enables the same existing multi-account pool on
   the next restart.
3. Every control client directly loads exactly one existing SQLite `.session`
   file, and no auxiliary client opens that file.
4. Telegram-reported identity is checked against every filename before the
   account serves reads or uploads.
5. Primary/backend authentication and linked-secondary eligibility retain their
   current behavior.
6. Normal bridge startup contains no plaintext StringSession configuration
   path, fallback, environment variable, or account JSON loader.
7. Fresh-login and legacy-migration workflows never print credentials and do
   not leave runtime configuration pointing at incomplete files.
8. Status, logs, and covered exception paths disclose neither credential
   contents nor full session paths; the optional live run includes a final log
   inspection for dependency-originated messages that fakes cannot prove.
9. Existing account routing, transfer, WebDAV, and authentication tests pass,
   together with the new session-file suite.
10. Documentation explicitly states that SQLite `.session` files are unencrypted
    bearer credentials and records the current installation's revoke-and-relogin
    requirement.
11. The bridge holds the session-directory advisory lock for its complete
    runtime, and concurrent `sessionctl` login/migration fails before opening a
    session file.
12. Resolved account files cannot escape `session_dir` through symlinks or
    reparse points, and every file is rechecked as an existing regular file
    immediately before Telethon construction.
