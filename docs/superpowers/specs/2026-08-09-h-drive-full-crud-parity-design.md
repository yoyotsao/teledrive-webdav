# H: Drive Full CRUD Parity — Design

## Goal

`H:` (the rclone/WinFsp mount in front of `bridge.py`) should feel like an
ordinary local drive: delete, rename/move, and copy should all work on
already-uploaded content, not just on content still sitting in local
staging. Today, DELETE and MOVE/COPY of already-uploaded content are
blocked with a 403 — a deliberate, documented choice made when the backend
(`teledrive`, a sibling project at `D:\python\teledrive`) was assumed to have
no endpoints for any of this. That assumption turns out to be wrong.

## What the backend already has (zero backend changes needed)

Read directly from `D:\python\teledrive\backend\app\api\routes.py` and
`app\services\file_service.py`:

- **`DELETE /api/v1/files/{id}`** — `file_service.trash_file()`. Soft-deletes
  the row and its whole subtree (folders recurse via `_collect_subtree_rows`;
  split files expand every part via `split_group_id`) by stamping
  `trashed_at`. Telegram messages are untouched. `GET /files`/`GET /folders`
  default to `trashed=false`, so a trashed item disappears from every normal
  listing with no extra filtering needed on the bridge side.
- **`PATCH /api/v1/files/{id}`** with `{parent_id?, filename?}` —
  `file_service.update_file()`. A real rename/reparent. Children are
  unaffected by their parent being renamed/moved, because they reference the
  parent by its stable `file_id`, not by name or path.
- **`POST /api/v1/files/register`** — already used for dedup: registering a
  new row that points at an *existing* `telegram_message_id`/`access_hash`
  is how the existing hash-dedup path avoids re-uploading identical content.
  There is no `UNIQUE(telegram_message_id)` constraint
  (`backend/app/services/database.py`, `files` table: only `file_id` is a
  primary key). This is exactly the primitive a **copy** needs: register a
  new row with a new `file_id`/`filename`/`parent_id` but the same
  `message_id`/`access_hash` — no bytes move, no backend change.

None of `trash`, `restore`, `purge`, or `update` are used by `tdapi.py`
today; its documented contract only covers login/list/download/register/
check-hash. This design adds three client methods and wires them into the
existing "staged locally vs. already uploaded" split that already governs
DELETE (from the prior session) and COPY (from today's earlier session) —
extending that same split to cover MOVE, and to make already-uploaded COPY
real instead of a 403.

## Scope

Applies uniformly to `/game` and general paths, mirroring the DELETE/COPY
work already done. **No change to the staged-content half of any verb**
except MOVE for general-path pending uploads, which today has no primitive
at all (a known, documented gap) and needs one to reach parity with
`/game`'s existing staging MOVE.

| Verb | Already-uploaded, today | Already-uploaded, after this | Still-staged, today | Still-staged, after this |
|---|---|---|---|---|
| DELETE | 403 | real trash (`DELETE /files/{id}`) | local unlink | unchanged |
| MOVE | 403 everywhere except `/game` | real rename/reparent (`PATCH /files/{id}`) | works in `/game` (mixin), 403 outside `/game` | works everywhere (new `UploadStager.move()`) |
| COPY | 403 | real metadata-duplicate (`POST /files/register`, reused message) | works everywhere (built today) | unchanged |

Not in scope: `restore`/`purge` (no WebDAV verb maps to them — left to the
existing TeleDrive web UI), `PROPPATCH`/`LOCK` (already audited, no gap),
block-level partial writes (a separate, much larger existing limitation).

## Components

### `tdapi.py` — three new `TeleDriveClient` methods

- **`trash(file_id) -> None`** — `DELETE /files/{file_id}`, then
  `self.invalidate()`. One call handles files and folders identically (the
  backend's own `trash_file` doesn't distinguish); no bridge-side recursion
  needed for folders.
- **`move(file_id, *, parent_id, filename) -> None`** — `PATCH /files/{file_id}`
  with both fields always present (the bridge always knows the full
  destination path, so there is no "field omitted vs. explicitly null"
  ambiguity to handle — every call sends both). `self.invalidate()` after.
- **`duplicate(entry: Entry, *, filename: str, parent_id) -> None`** — the
  copy primitive. For a non-split entry: one `register()` call reusing
  `entry.message_id`/`entry.access_hash`/`entry.mime`/`entry.file_hash`, a
  fresh `file_id`, the new `filename`/`parent_id`. For a split entry: fetch
  every part's full row (message_id **and** access_hash — a fresh
  `_call("GET", f"/files/by-split-group/{entry.split_group_id}")`, deliberately
  *not* reusing `parts_for()`'s cache, which only keeps `(message_id, size)`
  and is relied on elsewhere in a format this must not disturb), generate one
  new `split_group_id`, and `register()` each part with a fresh `file_id` but
  the same per-part `message_id`/`access_hash`. `self.invalidate(parent_id)`
  after.

### `bridge.py` — already-uploaded resources

- **`RemoteFileResource.delete()`** *(new override)* — `trash(entry.file_id)`.
  `_ReadOnlyFile.delete()` keeps its existing 403 default, which now reads as
  "no real backend row to trash" — still correct for `ZipFileResource` (a
  member of an immutable packed zip has no row of its own).
- **`FolderCollection.handle_delete()`** *(new override)* — same, via
  `entry.file_id`; the whole subtree goes with it automatically.
  `_ReadOnlyCollection.handle_delete()` keeps its 403 default, still correct
  for bare `RootCollection` (deleting the drive root makes no sense) and
  `ZipDirCollection` (virtual, no row).
- **`RemoteFileResource.copy_move_single(dest_path, *, is_move)`** *(new
  override, replacing today's unconditional 403)* — resolve the destination's
  parent folder id and final segment (the same `api.resolve(dest_segments[:-1])`
  pattern already used in `begin_write`); `is_move` calls `move()`, otherwise
  calls `duplicate()`.
- **`FolderCollection.handle_move(dest_path)`** *(new override)* — resolve
  destination, call `move()`, return `True` (fully handled — no reason to
  walk children for a metadata-only rename).
- **`FolderCollection.handle_copy(dest_path, *, depth_infinity)`** *(new
  override)* — return `False`. This deliberately opts `FolderCollection` back
  *out* of `_ReadOnlyCollection.handle_copy()`'s blanket 403, letting
  wsgidav's normal per-node walk proceed: it creates the destination folder
  itself (next bullet) and then COPY-ies each descendant individually,
  reusing the exact same "framework does the recursion, each node handles
  only itself" mechanics already built today for `/game` staging COPY.
- **`FolderCollection.copy_move_single(dest_path, *, is_move)`** *(new
  override)* — only the `is_move=False` branch is ever reached (`is_move=True`
  is already fully handled by `handle_move` before wsgidav would get here);
  creates the destination folder via the existing `create_folder`-backed
  `RootCollection.create_collection` machinery.
- **`GameCollection`** *(two new overrides, alongside its existing
  `handle_delete()`)* — `handle_copy()`/`handle_move()` both raise
  `DAVError(HTTP_FORBIDDEN)`. `/game` itself must never be renamed, moved, or
  copied. This class sits outside the `_ReadOnlyCollection` family, so it
  needs its own copies of the same guard (matching why `handle_delete()`
  needed one there too).

### `bridge.py` / `WriteGuard`

- Add `"MOVE"` to `UNGATED_METHODS`. Update the constant's comment and the
  `WriteGuard` docstring to fold MOVE into the same explanation already
  written for DELETE/COPY (staged-vs-uploaded is decided per-resource now,
  not by path, for all three).

### `gamestage.py` — close the boundary gap MOVE's ungating opens

- **`GameStager.move()`** *(modify existing method)* — add the same
  `dest_segments[0] != self.cfg.game_folder` guard already added to `copy()`
  earlier today. Today this method is safe only because `WriteGuard`'s
  destination check guarantees the destination stays under `/game`; once
  MOVE is ungated, that guarantee disappears and this method must enforce it
  itself, exactly as `copy()` already does.

### `uploadstage.py` — the missing staged-content MOVE primitive

- **`UploadStager.move(src: Path, dest_segments) -> Path`** *(new method)* —
  validates the destination stays *outside* `/game` (mirroring
  `UploadFileResource.copy_move_single`'s existing cross-boundary check for
  COPY); physically `os.replace`s the local file; re-keys the `_pending` dict
  entry from the old segment-tuple key to the new one, updating its
  `parent_id` to match. A pending upload's identity in this dict *is* its
  full path, so a move must move the dict entry, not just the file on disk —
  unlike `GameStager`, where the unit key is only ever the first segment and
  never changes shape on a rename.

### `bridge.py` — `UploadFileResource`

- **`support_recursive_move(dest_path)`** / **`move_recursive(dest_path)`**
  *(new methods, mirroring `_StagingCopyMove`'s pattern)* — delegate to
  `UploadStager.move()`. This makes MOVE of a pending general-path upload go
  through the efficient single-hook path instead of falling through to
  `copy_move_single`'s `is_move=True` branch (which stays as a defensive,
  practically-unreachable fallback, exactly like the equivalent branch in
  `_StagingCopyMove` today).

### Test fixture — `tests/test_bridge_e2e.py`'s `FakeBackend`

The fake backend's `call()` dispatcher needs three new cases to stay a
faithful stand-in:

- `DELETE /files/{id}` → stamp a `trashed_at`-equivalent marker and, for a
  folder, cascade to every row whose `parent_id` chain leads back to it (the
  fake doesn't need real SQL recursion — a simple repeated pass over
  `self.rows` is enough at test scale). Excluded from `_list()`'s results
  from then on.
- `PATCH /files/{id}` → update the row's `parent_id`/`filename` in place.
- `POST /files/register` already exists and needs no change — `duplicate()`
  calls it exactly the way dedup already does, reusing an existing
  `message_id`.

## Error handling

- All three new client methods raise `ApiError` on a non-2xx response,
  exactly like every existing `tdapi.py` method — callers don't need new
  exception handling beyond what `RemoteFileResource.begin_write` etc.
  already model (catch `ApiError`, surface as `DAVError(HTTP_INTERNAL_ERROR)`).
- Destination-resolution failures (a `Destination` header pointing at a
  parent that doesn't exist) reuse the existing pattern: `api.resolve()`
  returning `None` for the parent segments means `parent_id=None`, which the
  backend already treats as "the drive root" — consistent with how a fresh
  upload to an unresolvable-but-permitted path already behaves elsewhere in
  this codebase. No new validation needed.
- The cross-`/game`-boundary guards (`GameStager.move()`, `UploadStager.move()`)
  raise `PermissionError`, converted to `DAVError(HTTP_FORBIDDEN, str(exc))` at
  the call site — the exact pattern `_StagingCopyMove`/`UploadFileResource`
  already use today.

## Testing

Extend `tests/test_bridge_e2e.py` with the same real-HTTP-against-the-rig
style used throughout today's work:

- DELETE an already-uploaded file/folder → 204, gone from listings, and (via
  the fake backend) marked trashed rather than removed from `self.rows`
  outright — so a test can assert the row still technically exists but is
  excluded from normal listing, mirroring the real backend's soft-delete.
- MOVE an already-uploaded file/folder to a different already-real folder,
  and to a new name in the same folder (rename) — confirm it appears at the
  new location/name, is gone from the old one, and (for a folder) its
  children are still reachable underneath it at the new location.
- MOVE a pending general-path upload to a new path/name outside `/game` —
  confirm the local file moved, the pending-upload bookkeeping key moved with
  it, and it still uploads correctly afterward.
- MOVE attempts across the `/game` boundary (both directions) — 403, mirroring
  the equivalent COPY tests already written today.
- COPY an already-uploaded file — confirm two independent rows exist
  afterward pointing at the same underlying Telegram message, original
  untouched.
- COPY an already-uploaded folder (with a nested file inside) — confirm the
  whole subtree exists at the destination, original untouched, matching the
  existing "folder copy creates an empty destination, files copied
  individually" test style from today's `/game`-staging COPY work.
- COPY of a split (multi-part) already-uploaded file — confirm every part
  gets a fresh row/`file_id` under a new `split_group_id`, all still pointing
  at the original per-part Telegram messages.
- `/game` and `GameCollection` itself remain non-deletable/non-movable/
  non-copyable (three small tests, mirroring the existing `DELETE /game`
  test).
