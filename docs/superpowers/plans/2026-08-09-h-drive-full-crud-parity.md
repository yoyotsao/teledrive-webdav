# H: Drive Full CRUD Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make DELETE, MOVE, and COPY of already-uploaded content on the `H:` mount real backend operations instead of a 403, so `H:` behaves like an ordinary drive letter — following the design in `docs/superpowers/specs/2026-08-09-h-drive-full-crud-parity-design.md`.

**Architecture:** The `teledrive` backend (a sibling project at `D:\python\teledrive`, unmodified by this plan) already has everything needed: `DELETE /api/v1/files/{id}` soft-deletes a whole subtree to a trash flag (`trashed_at`) that normal listings already exclude by default; `PATCH /api/v1/files/{id}` renames/reparents by updating `filename`/`parent_id` (children stay attached — they reference the parent by its stable `file_id`, never by path); `POST /api/v1/files/register` already tolerates multiple rows pointing at one Telegram message (the existing hash-dedup path proves it), which is exactly what a metadata-only copy needs. `tdapi.py` gets three new client methods (`trash`, `move`, `duplicate`) that call these; `bridge.py`'s already-uploaded resource classes (`RemoteFileResource`, `FolderCollection`, `GameCollection`) call them instead of raising 403. Ungating MOVE (mirroring today's earlier COPY/DELETE ungating) reopens the same cross-`/game`-boundary gap COPY had — `GameStager.move()` and a new `UploadStager.move()` close it the same way `GameStager.copy()` already does.

**Tech Stack:** Python 3.10, wsgidav 4.3.5, pytest. No new dependencies. No backend changes — `D:\python\teledrive` is read-only reference for this plan, never edited.

## Global Constraints

- Every new/changed behavior needs a test in `tests/test_bridge_e2e.py`. Run the full suite (`.venv\Scripts\python.exe -m pytest tests -q`) after every task; it must stay at 100% pass.
- Match the existing code's tone: no comments explaining *what* the code does, only non-obvious *why*.
- Do not touch `D:\python\teledrive` (the backend). It already has every endpoint this plan needs.
- **Task order matters.** Task 4 (ungating MOVE in `WriteGuard`) must run before Task 7 (`UploadStager.move()`) for the same reason established earlier today: a cross-boundary test written before its gate is ungated passes for the wrong reason (via `WriteGuard`, not the resource-level guard), which defeats the point of writing it. Tasks 1-2 (client methods) have no such ordering constraint but come first because everything else calls them.
- `PROPPATCH`/`LOCK` are unaffected by this plan — already audited earlier today, no gap found, out of scope here.
- `ZipFileResource`/`ZipDirCollection` (packed-archive-internal virtual nodes) and bare `RootCollection` keep their existing 403 default for DELETE/MOVE/COPY — none of them has a real backend row to operate on. Only `RemoteFileResource`, `FolderCollection`, and `GameCollection` (the guard-only exception) get new overrides in this plan.

---

### Task 1: `tdapi.py` — `trash()` and `move()`, plus fake-backend support

**Files:**
- Modify: `tdapi.py` (`TeleDriveClient` class)
- Modify: `tests/test_bridge_e2e.py` (`FakeBackend` class)
- Test: `tests/test_bridge_e2e.py`

**Interfaces:**
- Consumes: `TeleDriveClient._call(method, path, *, params=None, payload=None)` (existing), `TeleDriveClient.invalidate()` (existing).
- Produces: `TeleDriveClient.trash(file_id: str) -> None`, `TeleDriveClient.move(file_id: str, *, parent_id: Optional[str], filename: str) -> None`.

- [ ] **Step 1: Write the failing tests**

Add near the other resolver/API-level tests in `tests/test_bridge_e2e.py` (a good spot: right after the existing dedup/upload tests, before the M2 zip-expansion section header):

```python
def test_api_trash_marks_a_row_and_excludes_it_from_listings(rig):
    entry = rig.entry_for("photos/small.txt")
    rig.resolver.api.trash(entry.file_id)
    assert "small.txt" not in rig.names("/photos")
    row = next(r for r in rig.backend.rows if r["file_id"] == entry.file_id)
    assert row["trashed_at"] is not None
    assert row["filename"] == "small.txt"  # still there, just excluded — a soft delete


def test_api_trash_of_a_folder_cascades_to_its_children(rig):
    photos = rig.entry_for("photos")
    rig.resolver.api.trash(photos.file_id)
    assert rig.names("/") == ["game", "movie.mkv"]
    row = next(r for r in rig.backend.rows if r["filename"] == "small.txt")
    assert row["trashed_at"] is not None


def test_api_move_renames_and_reparents(rig):
    entry = rig.entry_for("photos/small.txt")
    game = rig.entry_for("game")
    rig.resolver.api.move(entry.file_id, parent_id=game.file_id, filename="renamed.txt")
    assert "small.txt" not in rig.names("/photos")
    assert "renamed.txt" in rig.names("/game")
```

- [ ] **Step 2: Run them, confirm they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_bridge_e2e.py::test_api_trash_marks_a_row_and_excludes_it_from_listings tests/test_bridge_e2e.py::test_api_trash_of_a_folder_cascades_to_its_children tests/test_bridge_e2e.py::test_api_move_renames_and_reparents -v`
Expected: FAIL — `AttributeError: 'FakeClient' object has no attribute 'trash'` (and similarly for `move`), and even once those exist, `AssertionError: unexpected API call DELETE /files/...` from `FakeBackend.call()` until Step 3's fixture change lands too.

- [ ] **Step 3: Implement**

In `tdapi.py`, inside `class TeleDriveClient:`, directly below `register()`:

```python
    def trash(self, file_id: str) -> None:
        """Soft-delete: the backend stamps trashed_at on the whole subtree and
        keeps every Telegram message untouched. Listings already exclude
        trashed rows by default, so there is nothing else to filter here.
        """
        self._call("DELETE", f"/files/{file_id}")
        self.invalidate()

    def move(self, file_id: str, *, parent_id: Optional[str], filename: str) -> None:
        """Rename/reparent in place — children stay attached, they key off
        this row's stable file_id, never off its name or path."""
        self._call("PATCH", f"/files/{file_id}", payload={"parent_id": parent_id, "filename": filename})
        self.invalidate()
```

In `tests/test_bridge_e2e.py`, inside `class FakeBackend:`, add a subtree helper next to `_row`/`_list`:

```python
    def _subtree_ids(self, root_id: str) -> set:
        ids = {root_id}
        changed = True
        while changed:
            changed = False
            for r in self.rows:
                if r["parent_id"] in ids and r["file_id"] not in ids:
                    ids.add(r["file_id"])
                    changed = True
        return ids
```

Add two new branches to `FakeBackend.call()`, right after the existing `/files/register` branch and before the final `raise AssertionError`:

```python
        if path.startswith("/files/") and method == "DELETE" and not path.endswith("/purge"):
            file_id = path.rsplit("/", 1)[1]
            ids = self._subtree_ids(file_id)
            stamp = self._stamp()
            for r in self.rows:
                if r["file_id"] in ids:
                    r["trashed_at"] = stamp
            return {"message": "Moved to trash", "file_id": file_id, "items_trashed": len(ids)}
        if path.startswith("/files/") and method == "PATCH":
            file_id = path.rsplit("/", 1)[1]
            row = next(r for r in self.rows if r["file_id"] == file_id)
            row["parent_id"] = payload.get("parent_id")
            row["filename"] = payload.get("filename")
            return row
```

Update `FakeBackend._row()` to default the new field, and `_list()` to exclude trashed rows — find these two existing methods and edit them:

```python
    def _row(
        self,
        name,
        size,
        *,
        parent_id=None,
        is_dir=False,
        mime=None,
        message_id=None,
        is_split=False,
        group=None,
        part_index=None,
        file_hash=None,
    ):
        return {
            "file_id": uuid.uuid4().hex,
            "filename": name,
            "filesize": size,
            "mime_type": mime,
            "file_type": "other",
            "telegram_message_id": message_id,
            "has_thumbnail": False,
            "created_at": self._stamp(),
            "direct_url": None,
            "access_hash": "ah" if message_id else None,
            "parent_id": parent_id,
            "isDir": is_dir,
            "is_split_file": is_split,
            "split_group_id": group,
            "part_index": part_index,
            "file_hash": file_hash,
            "trashed_at": None,
        }
```

(Only the added trailing `"trashed_at": None,` line changes here — keep every existing field as-is.)

```python
    def _list(self, params, want_dir):
        parent = params.get("parent_id")
        rows = [
            r
            for r in self.rows
            if bool(r["isDir"]) is want_dir
            and r["parent_id"] == parent
            and not r.get("trashed_at")
            # Split parts collapse to the primary part, as the real query does.
            and (not r["is_split_file"] or (r["part_index"] or 0) == 0)
        ]
        page = int(params.get("page", 1))
        size = int(params.get("page_size", PAGE_SIZE))
        start = (page - 1) * size
        return {"files": rows[start : start + size], "total": len(rows), "page": page, "page_size": size}
```

(Only the added `and not r.get("trashed_at")` line changes here.)

- [ ] **Step 4: Run them, confirm they pass**

Run the same command as Step 2. Expected: all three PASS.

- [ ] **Step 5: Full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest tests -q`

```bash
git add tdapi.py tests/test_bridge_e2e.py
git commit -m "feat: real trash and rename/reparent client methods, backed by endpoints the backend already has"
```

---

### Task 2: `tdapi.py` — `duplicate()`, the metadata-only copy primitive

**Files:**
- Modify: `tdapi.py` (`TeleDriveClient` class; add `import uuid` to the module's imports)
- Test: `tests/test_bridge_e2e.py`

**Interfaces:**
- Consumes: `TeleDriveClient.register()` (existing), `TeleDriveClient._call()` (existing), `Entry` fields (existing: `file_id`, `is_split`, `split_group_id`, `size`, `message_id`, `access_hash`, `mime`, `file_hash`).
- Produces: `TeleDriveClient.duplicate(entry: Entry, *, filename: str, parent_id: Optional[str]) -> None`.

- [ ] **Step 1: Write the failing tests**

Add next to Task 1's new tests:

```python
def test_api_duplicate_registers_a_second_row_at_the_same_message(rig):
    entry = rig.entry_for("photos/small.txt")
    game = rig.entry_for("game")
    rig.resolver.api.duplicate(entry, filename="copy.txt", parent_id=game.file_id)

    assert "small.txt" in rig.names("/photos")  # original untouched
    assert "copy.txt" in rig.names("/game")
    original_row = next(r for r in rig.backend.rows if r["filename"] == "small.txt")
    copy_row = next(r for r in rig.backend.rows if r["filename"] == "copy.txt")
    assert copy_row["file_id"] != original_row["file_id"]
    assert copy_row["telegram_message_id"] == original_row["telegram_message_id"]
    assert copy_row["access_hash"] == original_row["access_hash"]


def test_api_duplicate_of_a_split_file_copies_every_part(rig):
    entry = rig.entry_for("movie.mkv")
    game = rig.entry_for("game")
    rig.resolver.api.duplicate(entry, filename="movie2.mkv", parent_id=game.file_id)

    assert "movie2.mkv" in rig.names("/game")
    original_parts = [r for r in rig.backend.rows if r["filename"] == "movie.mkv"]
    copy_parts = sorted(
        (r for r in rig.backend.rows if r["filename"] == "movie2.mkv"),
        key=lambda r: r["part_index"] or 0,
    )
    assert len(copy_parts) == len(original_parts)
    original_by_index = {r["part_index"] or 0: r for r in original_parts}
    for part in copy_parts:
        original = original_by_index[part["part_index"] or 0]
        assert part["telegram_message_id"] == original["telegram_message_id"]
        assert part["access_hash"] == original["access_hash"]
        assert part["file_id"] != original["file_id"]
    assert len({p["split_group_id"] for p in copy_parts}) == 1
    assert copy_parts[0]["split_group_id"] != original_parts[0]["split_group_id"]
```

- [ ] **Step 2: Run them, confirm they fail**

Run:
```
.venv\Scripts\python.exe -m pytest tests/test_bridge_e2e.py::test_api_duplicate_registers_a_second_row_at_the_same_message tests/test_bridge_e2e.py::test_api_duplicate_of_a_split_file_copies_every_part -v
```
Expected: FAIL — `AttributeError: 'FakeClient' object has no attribute 'duplicate'`.

- [ ] **Step 3: Implement**

Add `import uuid` to `tdapi.py`'s existing import block (alongside `import threading`, etc. — keep the existing alphabetical-ish grouping).

In `tdapi.py`, inside `class TeleDriveClient:`, directly below `move()`:

```python
    def duplicate(self, entry: Entry, *, filename: str, parent_id: Optional[str]) -> None:
        """Metadata-only copy: a new row pointing at the same Telegram message(s).

        Safe because there is no UNIQUE(telegram_message_id) constraint — the
        existing hash-dedup path in register() already relies on the same
        fact to avoid re-uploading identical content.
        """
        if not (entry.is_split and entry.split_group_id):
            self.register(
                filename=filename,
                filesize=entry.size,
                message_id=entry.message_id,
                file_id=uuid.uuid4().hex,
                access_hash=entry.access_hash,
                mime_type=entry.mime,
                parent_id=parent_id,
                file_hash=entry.file_hash,
            )
            return

        # A fresh fetch, not parts_for(): that method's cache only keeps
        # (message_id, size) and is relied on elsewhere in that exact shape —
        # a copy needs each part's access_hash too, which parts_for() drops.
        data = self._call("GET", f"/files/by-split-group/{entry.split_group_id}")
        rows = sorted(data.get("files") or [], key=lambda r: r.get("part_index") or 0)
        new_group = uuid.uuid4().hex
        total = len(rows)
        for index, row in enumerate(rows):
            self.register(
                filename=filename,
                filesize=row.get("filesize") or 0,
                message_id=row["telegram_message_id"],
                file_id=uuid.uuid4().hex,
                access_hash=row.get("access_hash"),
                mime_type=entry.mime,
                parent_id=parent_id,
                is_split_file=True,
                original_name=filename,
                part_index=index,
                total_parts=total,
                split_group_id=new_group,
                file_hash=entry.file_hash,
            )
```

`register()` already calls `self.invalidate(parent_id)` at the end of every call it makes (verify this by reading the existing `register()` method above it) — so `duplicate()` needs no invalidate call of its own.

- [ ] **Step 4: Run them, confirm they pass**

Run the same command as Step 2. Expected: both PASS.

- [ ] **Step 5: Full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest tests -q`

```bash
git add tdapi.py tests/test_bridge_e2e.py
git commit -m "feat: real metadata-only copy for already-uploaded content, including split files"
```

---

### Task 3: `bridge.py` — real DELETE for already-uploaded files and folders

**Files:**
- Modify: `bridge.py` (`RemoteFileResource`, `FolderCollection`)
- Test: `tests/test_bridge_e2e.py`

**Interfaces:**
- Consumes: `TeleDriveClient.trash()` (Task 1).
- Produces: `RemoteFileResource.delete()`, `FolderCollection.handle_delete()`.

- [ ] **Step 1: Write the failing tests**

Add near `test_delete_already_packed_game_folder_is_forbidden_not_a_crash` in `tests/test_bridge_e2e.py`:

```python
def test_delete_already_uploaded_file_really_trashes_it(rig):
    resp = rig.request("DELETE", "/photos/small.txt")
    assert resp.status_code == 204, resp.status_code
    assert "small.txt" not in rig.names("/photos")
    row = next(r for r in rig.backend.rows if r["filename"] == "small.txt")
    assert row["trashed_at"] is not None


def test_delete_already_uploaded_folder_trashes_the_whole_subtree(rig):
    resp = rig.request("DELETE", "/photos")
    assert resp.status_code == 204, resp.status_code
    assert "photos" not in rig.names("/")
    row = next(r for r in rig.backend.rows if r["filename"] == "small.txt")
    assert row["trashed_at"] is not None
```

Note this changes the meaning of the *existing* test `test_delete_already_packed_game_folder_is_forbidden_not_a_crash` not at all — `/game/MyGame` there is a `ZipDirCollection` (packed-archive-internal), not a `FolderCollection`, and keeps its 403 default. Do not modify that test.

**This task breaks two pre-existing tests that assumed DELETE of already-uploaded content always 403s — fix both as part of this task, not as an afterthought:**

1. `test_writes_outside_game_are_forbidden`'s `@pytest.mark.parametrize` list currently includes `("DELETE", "/photos/small.txt")` and `("DELETE", "/movie.mkv")` — both are exactly the case this task makes succeed. Remove those two tuples from the list, keeping `("PROPPATCH", "/photos/small.txt")`, `("LOCK", "/photos/small.txt")`, and `("DELETE", "/game")` (still correctly forbidden — `GameCollection.handle_delete()` is untouched).
2. `test_read_only_paths_are_unchanged_after_rejected_deletes` deletes `/photos/small.txt` and asserts the folder is unchanged — but after this task, that delete is no longer rejected. Repurpose it to use a target that is still genuinely rejected:

```python
def test_read_only_paths_are_unchanged_after_rejected_deletes(rig):
    rig.request("DELETE", "/game")
    assert rig.names("/") == ["game", "movie.mkv", "photos"]
```

- [ ] **Step 2: Run the new tests, confirm they fail**

Run:
```
.venv\Scripts\python.exe -m pytest tests/test_bridge_e2e.py::test_delete_already_uploaded_file_really_trashes_it tests/test_bridge_e2e.py::test_delete_already_uploaded_folder_trashes_the_whole_subtree -v
```
Expected: both FAIL with 403 (today's default), not 204.

- [ ] **Step 3: Implement**

In `bridge.py`, inside `class RemoteFileResource(_ReadOnlyFile):`, directly below its existing `end_write()` method (which is the last method in the class today):

```python
    def delete(self):
        self.resolver.api.trash(self.entry.file_id)
```

In `bridge.py`, inside `class FolderCollection(RootCollection):`, add (this class currently has no body beyond `__init__` — add this as its first method):

```python
    def handle_delete(self):
        self.resolver.api.trash(self.entry.file_id)
        return True
```

- [ ] **Step 4: Run them, confirm they pass**

Run the same command as Step 2. Expected: both PASS.

- [ ] **Step 5: Full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest tests -q` — confirm `test_delete_already_packed_game_folder_is_forbidden_not_a_crash` still passes untouched, and the repurposed `test_read_only_paths_are_unchanged_after_rejected_deletes` / trimmed `test_writes_outside_game_are_forbidden` both pass with their Step 1 edits.

```bash
git add bridge.py tests/test_bridge_e2e.py
git commit -m "feat: real DELETE (trash) for already-uploaded files and folders"
```

---

### Task 4: `WriteGuard` ungate MOVE, and close the boundary gap it reopens

**Files:**
- Modify: `bridge.py` (`WRITE_METHODS`/`UNGATED_METHODS`, `WriteGuard` docstring)
- Modify: `gamestage.py` (`GameStager.move()`)
- Test: `tests/test_bridge_e2e.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `UNGATED_METHODS` now includes `"MOVE"`. `GameStager.move()` gains the same boundary guard `copy()` already has.

This mirrors exactly what Task 3 of today's earlier COPY plan did for COPY, for the same reason: `WriteGuard`'s destination check is the only thing today making `GameStager.move()` safe (it guarantees `dest_segments[0] == "game"` for every caller). Once MOVE is ungated, that guarantee disappears, and `move()` must enforce it itself — exactly the bug `copy()` would have had if we'd forgotten this step earlier today.

- [ ] **Step 1: Write the failing test**

Add next to `test_move_into_a_read_only_path_is_forbidden` in `tests/test_bridge_e2e.py`:

```python
def test_move_out_of_game_staging_is_forbidden(rig):
    rig.request("MKCOL", "/game/Temp")
    rig.request("PUT", "/game/Temp/a.bin", data=b"junk")

    resp = rig.request(
        "MOVE", "/game/Temp/a.bin", headers={"Destination": rig.base + "/photos/escaped.bin"}
    )
    assert resp.status_code == 403, resp.status_code
    assert "escaped.bin" not in rig.names("/photos")
    assert "a.bin" in rig.names("/game/Temp")
```

- [ ] **Step 2: Run it, confirm it currently passes for the wrong reason**

Run: `.venv\Scripts\python.exe -m pytest tests/test_bridge_e2e.py::test_move_out_of_game_staging_is_forbidden -v`
Expected: PASS already — via `WriteGuard`, before this task's change. This is a baseline snapshot; re-verify after Step 3 that it still passes, now for the intended reason (`GameStager.move()`'s own guard, not `WriteGuard`).

- [ ] **Step 3: Implement**

In `bridge.py`, update the constant and its comment:

```python
# Verbs that mutate. Everything outside /game/<something> gets 403 for these,
# rather than mounting the whole drive read-only (which would kill /game too).
# MKCOL, PUT, DELETE, COPY and MOVE are exempted below (WriteGuard) — none of
# the five needs /game specifically. MKCOL and PUT map onto real backend
# endpoints (POST /folders, and the same stage-upload-register pipeline
# /game uses). DELETE, COPY and MOVE have real backend endpoints too now
# (trash, register-reuse, and rename/reparent respectively) but none of them
# needs /game either: the resources themselves already draw the real line —
# still-staged writes (StagingFileResource/StagingCollection,
# UploadFileResource) accept them as local filesystem operations,
# already-uploaded resources (_ReadOnlyCollection, _ReadOnlyFile,
# RemoteFileResource, FolderCollection) call the matching backend operation
# — so gating by path on top would only block the /game case for no reason.
# PROPPATCH/LOCK have no such per-resource distinction and stay path-gated
# below.
WRITE_METHODS = {"PUT", "DELETE", "MKCOL", "MOVE", "COPY", "PROPPATCH", "LOCK", "UNLOCK"}
UNGATED_METHODS = {"MKCOL", "PUT", "DELETE", "COPY", "MOVE"}
```

Update the `WriteGuard` class docstring the same way — find the paragraph already covering DELETE/COPY and fold MOVE into it (do not duplicate the whole docstring, just extend the existing sentences).

In `gamestage.py`, update `GameStager.move()` — add the boundary check as its very first lines, matching `copy()`'s exact style (`copy()` sits right above/below it in the file):

```python
    def move(self, src: Path, dest_segments: Sequence[str]) -> None:
        """Rename inside staging (dest_segments starts with the /game element)."""
        if not dest_segments or dest_segments[0] != self.cfg.game_folder:
            raise PermissionError("move destination must stay under /game while staged")
        rest = list(dest_segments)[1:]
        dest = self.path_for(rest)
        if dest is None:
            raise PermissionError("move destination is outside the staging area")
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(_ext(src), _ext(dest))
        if rest:
            self.touch(rest[0])
```

(Only the new `if not dest_segments or dest_segments[0] != self.cfg.game_folder: raise PermissionError(...)` block at the top is new — everything after it is the existing method body, unchanged.)

- [ ] **Step 4: Run it, confirm it still passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_bridge_e2e.py::test_move_out_of_game_staging_is_forbidden -v`
Expected: still PASS — now via `GameStager.move()`'s new guard instead of `WriteGuard`.

- [ ] **Step 5: Full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest tests -q` — pay attention to `test_move_into_a_read_only_path_is_forbidden` (moves `/game/MyGame`, an already-packed folder, to `/photos/stolen` — should still 403, now reachable via the resource layer rather than `WriteGuard` for the *destination* check, but the *source* being `ZipDirCollection`-backed still means `_ReadOnlyCollection`'s existing `handle_move()` 403 fires regardless of where the guard sits).

```bash
git add bridge.py gamestage.py tests/test_bridge_e2e.py
git commit -m "feat: let MOVE reach the DAV layer everywhere, and close the boundary gap that reopens"
```

---

### Task 5: `RemoteFileResource.copy_move_single()` — real MOVE and COPY for already-uploaded files

**Files:**
- Modify: `bridge.py` (`RemoteFileResource`)
- Test: `tests/test_bridge_e2e.py`

**Interfaces:**
- Consumes: `TeleDriveClient.move()` (Task 1), `TeleDriveClient.duplicate()` (Task 2), `split_dav_path()` (existing helper), `Resolver.api.resolve()` (existing).
- Produces: `RemoteFileResource.copy_move_single(dest_path, *, is_move)` — replaces the unconditional-403 override added earlier today.

- [ ] **Step 1: Write the failing tests**

Add next to the tests from Task 3:

```python
def test_move_already_uploaded_file_renames_and_reparents(rig):
    resp = rig.request(
        "MOVE", "/photos/small.txt", headers={"Destination": rig.base + "/game/renamed.txt"}
    )
    assert resp.status_code in (201, 204), resp.status_code
    assert "small.txt" not in rig.names("/photos")
    assert "renamed.txt" in rig.names("/game")


def test_copy_already_uploaded_file_creates_an_independent_row(rig):
    resp = rig.request(
        "COPY", "/photos/small.txt", headers={"Destination": rig.base + "/game/copy.txt"}
    )
    assert resp.status_code == 201, resp.status_code
    assert "small.txt" in rig.names("/photos")  # original untouched
    assert "copy.txt" in rig.names("/game")
    assert rig.request("GET", "/game/copy.txt").content == rig.blob_for("photos/small.txt")


def test_move_already_uploaded_split_file_preserves_all_parts(rig):
    # blob_for() must run BEFORE the move: it resolves the path via
    # entry_for(), which stops working the instant the old path is gone.
    original_bytes = rig.blob_for("movie.mkv")
    resp = rig.request(
        "MOVE", "/movie.mkv", headers={"Destination": rig.base + "/game/movie2.mkv"}
    )
    assert resp.status_code in (201, 204), resp.status_code
    assert "movie.mkv" not in rig.names("/")
    assert "movie2.mkv" in rig.names("/game")
    assert rig.request("GET", "/game/movie2.mkv").content == original_bytes
```

- [ ] **Step 2: Run them, confirm they fail**

Run:
```
.venv\Scripts\python.exe -m pytest tests/test_bridge_e2e.py::test_move_already_uploaded_file_renames_and_reparents tests/test_bridge_e2e.py::test_copy_already_uploaded_file_creates_an_independent_row tests/test_bridge_e2e.py::test_move_already_uploaded_split_file_preserves_all_parts -v
```
Expected: all three FAIL with 403 (today's unconditional default).

- [ ] **Step 3: Implement**

Do not modify `_ReadOnlyFile` itself — it must keep 403ing `copy_move_single()` for `ZipFileResource`, which has no real backend row. Instead, inside `class RemoteFileResource(_ReadOnlyFile):`, add an override directly below `delete()` (added in Task 3):

```python
    def copy_move_single(self, dest_path, *, is_move):
        dest_segments = split_dav_path(dest_path)
        parent = self.resolver.api.resolve(dest_segments[:-1]) if len(dest_segments) > 1 else None
        parent_id = parent.file_id if parent is not None else None
        filename = dest_segments[-1]
        if is_move:
            self.resolver.api.move(self.entry.file_id, parent_id=parent_id, filename=filename)
        else:
            self.resolver.api.duplicate(self.entry, filename=filename, parent_id=parent_id)
```

**This task breaks a pre-existing test.** `test_copy_already_uploaded_file_outside_game_is_forbidden` (added in an earlier session today) asserts COPY of `/photos/small.txt` to `/photos/copy.txt` is 403 — exactly the case this task makes succeed. Its scenario is now fully covered by this task's own `test_copy_already_uploaded_file_creates_an_independent_row` (same idea, different destination folder). Delete `test_copy_already_uploaded_file_outside_game_is_forbidden` entirely as part of Step 1 — do not leave it modified-to-something-else, it would just be a worse duplicate of the new test right next to it.

- [ ] **Step 4: Run them, confirm they pass**

Run the same command as Step 2. Expected: all three PASS.

- [ ] **Step 5: Full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest tests -q` — confirm `test_copy_already_packed_game_file_is_forbidden_cleanly` (a `ZipFileResource`, must still 403 — unaffected since `_ReadOnlyFile.copy_move_single()` is untouched) still passes, and that the deleted `test_copy_already_uploaded_file_outside_game_is_forbidden` is gone rather than failing.

```bash
git add bridge.py tests/test_bridge_e2e.py
git commit -m "feat: real MOVE and COPY for already-uploaded files, split files included"
```

---

### Task 6: `FolderCollection`/`GameCollection` — real MOVE/COPY for already-uploaded folders

**Files:**
- Modify: `bridge.py` (`FolderCollection`, `GameCollection`)
- Test: `tests/test_bridge_e2e.py`

**Interfaces:**
- Consumes: `TeleDriveClient.move()` (Task 1), `RootCollection.create_collection`-backed folder creation (existing, via `self.resolver.api.create_folder`).
- Produces: `FolderCollection.handle_move()`, `FolderCollection.handle_copy()`, `FolderCollection.copy_move_single()`, `GameCollection.handle_copy()`, `GameCollection.handle_move()`.

- [ ] **Step 1: Write the failing tests**

Add next to Task 5's tests:

```python
def test_move_already_uploaded_folder_renames_it_children_intact(rig):
    resp = rig.request(
        "MOVE", "/photos", headers={"Destination": rig.base + "/renamed_photos"}
    )
    assert resp.status_code in (201, 204), resp.status_code
    assert "photos" not in rig.names("/")
    assert "renamed_photos" in rig.names("/")
    assert sorted(rig.names("/renamed_photos")) == ["shot.png", "small.txt"]


def test_copy_already_uploaded_folder_duplicates_the_whole_subtree(rig):
    resp = rig.request(
        "COPY", "/photos", headers={"Destination": rig.base + "/game/photos2/"}
    )
    assert resp.status_code == 201, resp.status_code
    assert sorted(rig.names("/photos")) == ["shot.png", "small.txt"]  # original untouched
    assert sorted(rig.names("/game/photos2")) == ["shot.png", "small.txt"]
    assert rig.request("GET", "/game/photos2/small.txt").content == rig.blob_for("photos/small.txt")


def test_move_game_folder_itself_is_forbidden(rig):
    resp = rig.request("MOVE", "/game", headers={"Destination": rig.base + "/renamed_game"})
    assert resp.status_code == 403, resp.status_code
    assert "game" in rig.names("/")


def test_copy_game_folder_itself_is_forbidden(rig):
    resp = rig.request("COPY", "/game", headers={"Destination": rig.base + "/game_copy"})
    assert resp.status_code == 403, resp.status_code
    assert "game_copy" not in rig.names("/")
```

- [ ] **Step 2: Run them, confirm they fail**

Run:
```
.venv\Scripts\python.exe -m pytest tests/test_bridge_e2e.py::test_move_already_uploaded_folder_renames_it_children_intact tests/test_bridge_e2e.py::test_copy_already_uploaded_folder_duplicates_the_whole_subtree tests/test_bridge_e2e.py::test_move_game_folder_itself_is_forbidden tests/test_bridge_e2e.py::test_copy_game_folder_itself_is_forbidden -v
```
Expected: the first two FAIL with 403 (today's `_ReadOnlyCollection` default). The last two currently PASS already (MOVE via `WriteGuard`'s path-length check for a single-segment path — wait, MOVE is now ungated as of Task 4, so `/game` as a *source* no longer gets `WriteGuard`'s protection either; check whether it currently 403s via the framework's existing per-node walk hitting `DAVCollection.copy_move_single()`'s default, or 500s some other way — run it and record what actually happens before assuming). COPY of `/game` should already 403 today (COPY was ungated earlier today, and nothing added a `GameCollection.handle_copy()` override yet, so it falls through to the framework's descendant walk and 403s at `DAVCollection.copy_move_single()`'s default eventually — confirm this empirically, don't assume).

- [ ] **Step 3: Implement**

In `bridge.py`, inside `class FolderCollection(RootCollection):`, directly below `handle_delete()` (added in Task 3):

```python
    def handle_move(self, dest_path):
        dest_segments = split_dav_path(dest_path)
        parent = self.resolver.api.resolve(dest_segments[:-1]) if len(dest_segments) > 1 else None
        parent_id = parent.file_id if parent is not None else None
        self.resolver.api.move(self.entry.file_id, parent_id=parent_id, filename=dest_segments[-1])
        return True

    # Opts back OUT of _ReadOnlyCollection's blanket handle_copy() 403: a real
    # copy is possible now, and wsgidav's own per-node descendant walk already
    # does the recursion for free — this class only needs to answer for
    # itself (create the destination folder; see copy_move_single below).
    def handle_copy(self, dest_path, *, depth_infinity):
        return False

    def copy_move_single(self, dest_path, *, is_move):
        dest_segments = split_dav_path(dest_path)
        parent = self.resolver.api.resolve(dest_segments[:-1]) if len(dest_segments) > 1 else None
        parent_id = parent.file_id if parent is not None else None
        self.resolver.api.create_folder(dest_segments[-1], parent_id=parent_id)
```

In `bridge.py`, inside `class GameCollection(DAVCollection):`, directly below its existing `handle_delete()`:

```python
    def handle_copy(self, dest_path, *, depth_infinity):
        raise DAVError(HTTP_FORBIDDEN)

    def handle_move(self, dest_path):
        raise DAVError(HTTP_FORBIDDEN)
```

- [ ] **Step 4: Run them, confirm they pass**

Run the same command as Step 2. Expected: all four PASS (the first two newly; the last two for the same or a corrected reason — resolve whatever Step 2 actually observed).

- [ ] **Step 5: Full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest tests -q`

```bash
git add bridge.py tests/test_bridge_e2e.py
git commit -m "feat: real MOVE and COPY for already-uploaded folders; /game itself stays protected"
```

---

### Task 7: `UploadStager.move()` and `UploadFileResource` — MOVE for pending general-path uploads

**Files:**
- Modify: `uploadstage.py` (`UploadStager`)
- Modify: `bridge.py` (`UploadFileResource`)
- Test: `tests/test_bridge_e2e.py`

**Interfaces:**
- Consumes: `UploadStager.path_for()` (existing), `config.ext_path as _ext` (already imported in `uploadstage.py`), `time.monotonic` (already imported).
- Produces: `UploadStager.move(old_segments, new_segments, *, parent_id) -> Path`, `UploadFileResource.support_recursive_move(dest_path)`, `UploadFileResource.move_recursive(dest_path)`.

Unlike `GameStager`, whose staging unit is keyed only by its top-level name (never changes shape on a rename), `UploadStager`'s `_pending` dict is keyed by a file's *entire* destination path — a move must re-key that dict entry, not just move the file on disk.

- [ ] **Step 1: Write the failing tests**

Add next to `test_copy_pending_general_upload_creates_an_independent_second_file` in `tests/test_bridge_e2e.py`:

```python
def test_move_pending_general_upload_renames_and_still_uploads(rig):
    rig.request("PUT", "/photos/pending.bin", data=b"waiting")

    resp = rig.request(
        "MOVE", "/photos/pending.bin", headers={"Destination": rig.base + "/photos/renamed.bin"}
    )
    assert resp.status_code in (201, 204), resp.status_code
    assert "pending.bin" not in rig.names("/photos")
    assert "renamed.bin" in rig.names("/photos")
    assert (rig.cfg.upload_dir / "photos" / "renamed.bin").read_bytes() == b"waiting"

    _upload_now(rig, "photos", "renamed.bin")
    row = next(r for r in rig.backend.rows if r["filename"] == "renamed.bin")
    photos_id = next(r["file_id"] for r in rig.backend.rows if r["filename"] == "photos")
    assert row["parent_id"] == photos_id


def test_move_pending_general_upload_to_another_folder(rig):
    rig.request("PUT", "/photos/pending.bin", data=b"waiting")

    resp = rig.request(
        "MOVE", "/photos/pending.bin", headers={"Destination": rig.base + "/moved.bin"}
    )
    assert resp.status_code in (201, 204), resp.status_code
    assert "pending.bin" not in rig.names("/photos")
    assert "moved.bin" in rig.names("/")

    _upload_now(rig, "moved.bin")
    row = next(r for r in rig.backend.rows if r["filename"] == "moved.bin")
    assert row["parent_id"] is None


def test_move_pending_general_upload_across_game_boundary_is_forbidden(rig):
    rig.request("PUT", "/photos/pending.bin", data=b"waiting")

    resp = rig.request(
        "MOVE", "/photos/pending.bin", headers={"Destination": rig.base + "/game/escaped.bin"}
    )
    assert resp.status_code == 403, resp.status_code
    assert "escaped.bin" not in rig.names("/game")
    assert "pending.bin" in rig.names("/photos")
```

- [ ] **Step 2: Run them, confirm they fail**

Run:
```
.venv\Scripts\python.exe -m pytest tests/test_bridge_e2e.py::test_move_pending_general_upload_renames_and_still_uploads tests/test_bridge_e2e.py::test_move_pending_general_upload_to_another_folder tests/test_bridge_e2e.py::test_move_pending_general_upload_across_game_boundary_is_forbidden -v
```
Expected: all three FAIL with a 500, not 403. `UploadFileResource` has no `support_recursive_move()` override today, and wsgidav calls that method unconditionally for every MOVE regardless of whether the resource is a collection (`request_server.py`, in `_copy_or_move`) — the inherited default (`_DAVResource.support_recursive_move`) is `assert self.is_collection; raise NotImplementedError`, and since `is_collection` is `False` for this class, the `assert` itself fails with an uncaught `AssertionError` before `copy_move_single`'s existing `is_move=True` branch is ever reached. This is the exact bug class found and fixed for `_ReadOnlyFile` in the final review of today's earlier COPY plan — same root cause, different class, previously unreachable here because `WriteGuard` blocked general-path MOVE entirely until Task 4 of this plan ungated it.

- [ ] **Step 3: Implement**

In `uploadstage.py`, inside `class UploadStager:`, directly below `create_file()`:

```python
    def move(self, old_segments: Sequence[str], new_segments: Sequence[str], *, parent_id: Optional[str]) -> Path:
        """Move a pending upload, re-keying its bookkeeping entry.

        Unlike GameStager, a unit's identity here IS its full destination
        path (see the module docstring) — a move must move the _pending
        dict entry, not just the file on disk.
        """
        new_key = tuple(new_segments)
        if not new_key or new_key[0] == self.cfg.game_folder:
            raise PermissionError("move destination must stay outside /game")
        old_key = tuple(old_segments)
        src = self.path_for(old_key)
        dest = self.path_for(new_key)
        if src is None or dest is None:
            raise PermissionError(f"move endpoint is outside the upload area: {old_key} -> {new_key}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(_ext(src), _ext(dest))
        with self._lock:
            pending = self._pending.pop(old_key, None)
            if pending is not None:
                pending.segments = new_key
                pending.parent_id = parent_id
                pending.last_write = time.monotonic()
                self._pending[new_key] = pending
        return dest
```

`uploadstage.py` already imports `os` at module level — verify this before adding the call (it does, per its existing `create_file`/`_adopt_leftovers` methods).

In `bridge.py`, inside `class UploadFileResource(DAVNonCollection):`, directly below `copy_move_single()` (added earlier today):

```python
    def support_recursive_move(self, dest_path):
        dest_segments = split_dav_path(dest_path)
        return bool(dest_segments) and dest_segments[0] != self.upload_stager.cfg.game_folder

    def move_recursive(self, dest_path):
        dest_segments = split_dav_path(dest_path)
        parent = self.upload_stager.api.resolve(dest_segments[:-1]) if len(dest_segments) > 1 else None
        parent_id = parent.file_id if parent is not None else None
        try:
            self.upload_stager.move(self.segments, dest_segments, parent_id=parent_id)
        except PermissionError as exc:
            raise DAVError(HTTP_FORBIDDEN, str(exc))
```

Also update the class docstring (the one already covering DELETE/COPY, from earlier today) to mention MOVE is now real too — find the sentence "MOVE is not offered..." and replace it: MOVE is now offered, via a `support_recursive_move`/`move_recursive` pair mirroring `_StagingCopyMove`'s pattern.

- [ ] **Step 4: Run them, confirm they pass**

Run the same command as Step 2. Expected: all three PASS.

- [ ] **Step 5: Full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest tests -q`

```bash
git add uploadstage.py bridge.py tests/test_bridge_e2e.py
git commit -m "feat: real MOVE for pending general-path uploads, closing the last staged-content gap"
```

---

### Task 8: Update `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:** none — documentation only.

- [ ] **Step 1: Rewrite the "一般路徑的寫入" DELETE/COPY bullets to describe the new backend-backed behavior**

Find the bullets describing `DELETE`'s and `COPY`'s "staged vs. already-uploaded" split (added in earlier sessions today). Both bullets currently say already-uploaded content gets a uniform 403. Rewrite them to say: already-uploaded DELETE now calls the backend's real trash endpoint (`DELETE /files/{id}`, soft-delete, whole subtree, restorable from the TeleDrive web UI); already-uploaded COPY now registers a metadata-only duplicate pointing at the same Telegram message(s) (no bytes move, split files handled part-by-part). Keep the existing staged-content half of both bullets as-is (unchanged behavior).

- [ ] **Step 2: Add a MOVE bullet, replacing its old "限定在 /game" framing**

Find the "明確不做" bullet that currently lists `MOVE`/`PROPPATCH`/`LOCK` as `/game`-only (from earlier today). Remove `MOVE` from that bullet (it is no longer limited to `/game`) and add a new bullet to "一般路徑的寫入" describing it: already-uploaded content gets a real rename/reparent via `PATCH /files/{id}`; staged content moves locally in both `/game` (existing `_StagingCopyMove` mixin) and general paths (new `UploadStager.move()`); crossing the `/game` boundary while staged is still 403 either direction, for the same "different stager, no shared local-filesystem logic" reason COPY already documents. `PROPPATCH`/`LOCK` keep their own bullet, unaffected by this plan.

- [ ] **Step 3: Add a bullet to 已知限制 noting the backend now supports trash/restore**

Something like: 已刪除（trashed）的內容可以從 TeleDrive 網頁的垃圾桶還原或永久清除（`POST /files/{id}/restore`、`DELETE /files/{id}/purge`）——這個 bridge 沒有對應的 WebDAV 操作可以觸發還原，只能刪（trash）。想找回東西，去網頁那邊操作。

- [ ] **Step 4: Update the test coverage table row for `tests/test_bridge_e2e.py`**

Append a clause: `MOVE/COPY/DELETE 對已上傳內容打真實的 backend trash/rename/register-reuse 操作，split file 的複製與搬移涵蓋所有 part，/game 本身與 zip 內部虛擬節點維持 403`.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: describe real DELETE/MOVE/COPY for already-uploaded content"
```

---

### Task 9: Final full-suite verification

**Files:** none — verification only.

- [ ] **Step 1: Run the whole suite one more time**

```
.venv\Scripts\python.exe -m pytest tests -q
```
Expected: 100% pass.

- [ ] **Step 2: Manual sanity check against the real TeleDrive backend (cannot be automated)**

Per `CLAUDE.md`'s existing manual-test checklist: mount `H:` for real, then in Explorer — delete an already-uploaded file and folder (confirm they disappear and show up in the TeleDrive web UI's trash, not gone forever); rename a file and a folder (F2) both within the same folder and by dragging to a different one; copy a file and a folder (Ctrl+C/Ctrl+V) including at least one split (>500 MiB) file if practical, confirm the copy plays/opens correctly and the original is untouched. This is the same category of manual step already listed in `CLAUDE.md`'s 「測試」section for earlier bridge features — add it there as an addendum once done.

- [ ] **Step 3: Report back**

Summarize: tasks completed, anything found during manual testing that needs a follow-up plan (in particular, note whether Explorer's actual COPY/MOVE gestures against the real rclone mount issue the WebDAV verbs this plan implements, or fall back to read+write/delete+create at the rclone layer — this determines whether the real-world experience matches what the test suite verifies).
