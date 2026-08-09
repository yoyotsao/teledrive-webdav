# CRUD Audit — COPY/MOVE Correctness + Staging COPY Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix a real correctness bug (COPY/MOVE of an unsupported resource returns HTTP 500 instead of a clean 403) and add real local COPY support for content that is still staged (not yet uploaded to Telegram/TeleDrive) — in both `/game` and general (non-`/game`) paths, on the same principle already applied to DELETE: the dividing line is "staged locally" vs. "already uploaded", never the path.

**Architecture:** `bridge.py` maps every WebDAV path to one of a small set of wsgidav resource classes (`bridge.Resolver.resolve()` decides which). Two families matter here:
- **Already-uploaded, immutable** — `RemoteFileResource`/`ZipFileResource` (via shared base `_ReadOnlyFile`) and `RootCollection`/`FolderCollection`/`ZipDirCollection` (via shared base `_ReadOnlyCollection`). No backend endpoint exists to copy or rename these, ever.
- **Still staged, mutable** — `StagingFileResource`/`StagingCollection` (backed by `gamestage.GameStager`, rooted at `/game/<top>`) and `UploadFileResource` (backed by `uploadstage.UploadStager`, one file per full path, anywhere outside `/game`). These are plain local files/directories until a debounce timer uploads them, so local filesystem operations (copy, move, delete) are always safe.

wsgidav calls `copy_move_single(dest_path, *, is_move)` on the *source* resource for both COPY and the file-by-file MOVE fallback. `DAVCollection` (any folder-like class) already has a safe default (`raise DAVError(HTTP_FORBIDDEN)`), but `DAVNonCollection` (any file-like class) has **no default at all** — it inherits `_DAVResource.copy_move_single()`, which is a bare `raise NotImplementedError`. wsgidav's request handler *does* catch that (unlike the DELETE bug fixed in the previous session, this one never crashes the process), but it converts it to `DAVError(HTTP_INTERNAL_ERROR)` — the client sees a 500 for something that should be a clean, well-understood 403.

**Tech Stack:** Python 3.10, wsgidav 4.3.5, pytest. No new dependencies.

## Global Constraints

- Every new/changed behavior needs a test in `tests/test_bridge_e2e.py` (the existing e2e rig — real HTTP against the real `bridge.build_app`, fake Telegram/backend). Run the full suite (`.venv\Scripts\python.exe -m pytest tests -q`) after every task; it must stay at 100% pass.
- Match the existing code's tone: no comments explaining *what* the code does, only non-obvious *why* (see any existing docstring in `bridge.py` for the house style).
- Do not touch `MOVE` outside `/game` — `UploadStager` has no rename primitive, and that gap is explicitly documented in `CLAUDE.md`'s "明確不做" section. This plan only touches `COPY`.
- Copying into a destination whose immediate parent is inside an **already-packed** `/game` zip (e.g. `/game/MyGame/bin/newfile.exe` when `MyGame` has no active staging tree) is expected to keep failing exactly like it already does for MOVE/PUT there (`ZipDirCollection.create_empty_resource` raises `DAVError(HTTP_FORBIDDEN, PACKED_MESSAGE)` for PUT; the pre-existing `GameStager.move()` has the same blind spot for MOVE). Fixing that is a separate, deeper problem (write-protecting packed subtrees at the stager level) — out of scope here. Do not attempt it; do not regress it either.
- **Task order matters and must not be reshuffled.** Task 3 (ungating COPY in `WriteGuard`) runs *before* Tasks 4-5 (the `/game` staging COPY implementation) specifically so that Task 5's cross-`/game`-boundary test is red-then-green against the real mechanism (the new per-resource guard), not against `WriteGuard`'s pre-existing, soon-to-be-removed destination check. Tasks 1 and 2 are unaffected by this ordering (both exercise paths entirely inside `/game`, which `WriteGuard` has always let through) and could in principle run anywhere before Task 3, but keep them first — they are the simplest, lowest-risk changes.

---

### Task 1: `_ReadOnlyFile.copy_move_single()` — clean 403 for already-uploaded files

**Files:**
- Modify: `bridge.py` (`_ReadOnlyFile` class, right after the `delete()` override added in the previous session)
- Test: `tests/test_bridge_e2e.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_ReadOnlyFile.copy_move_single(dest_path, *, is_move)` — every subclass (`RemoteFileResource`, `ZipFileResource`) inherits it.

- [ ] **Step 1: Write the failing test**

Add near `test_delete_already_packed_game_folder_is_forbidden_not_a_crash` in `tests/test_bridge_e2e.py`:

```python
def test_copy_already_packed_game_file_is_forbidden_cleanly(rig):
    # bin/game.exe lives inside the already-uploaded MyGame.zip (see the rig
    # fixture) — this exercises ZipFileResource via _ReadOnlyFile, which has
    # no backend copy endpoint to call.
    resp = rig.request(
        "COPY",
        "/game/MyGame/bin/game.exe",
        headers={"Destination": rig.base + "/game/MyGame/bin/copy.exe"},
    )
    assert resp.status_code == 403, resp.status_code
    assert rig.names("/game/MyGame/bin") == ["game.exe", "pak0.pak"]
```

This works today already, before any of this plan's `WriteGuard` changes: both source and destination are inside `/game`, and `WriteGuard` has always let `/game`-to-`/game` requests through for every gated verb.

- [ ] **Step 2: Run it, confirm it fails with 500**

Run: `.venv\Scripts\python.exe -m pytest tests/test_bridge_e2e.py::test_copy_already_packed_game_file_is_forbidden_cleanly -v`
Expected: FAIL — `assert 500 == 403`.

- [ ] **Step 3: Implement**

In `bridge.py`, inside `class _ReadOnlyFile(DAVNonCollection):`, directly below the `delete()` method added in the DELETE fix:

```python
    # Same reasoning as delete() above: DAVNonCollection has no default
    # copy_move_single() either, so COPY (and MOVE's file-by-file fallback,
    # since these classes have no support_recursive_move()) of an
    # already-uploaded file currently 500s instead of 403ing.
    def copy_move_single(self, dest_path, *, is_move):
        raise DAVError(HTTP_FORBIDDEN, "already uploaded — TeleDrive has no copy/rename endpoint for this.")
```

- [ ] **Step 4: Run it, confirm it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_bridge_e2e.py::test_copy_already_packed_game_file_is_forbidden_cleanly -v`
Expected: PASS.

- [ ] **Step 5: Full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest tests -q` — expect all passing, no regressions.

```bash
git add bridge.py tests/test_bridge_e2e.py
git commit -m "fix: 403 (not 500) when copying/moving an already-uploaded file"
```

---

### Task 2: `_ReadOnlyCollection.handle_copy()`/`handle_move()` — skip the descendant walk

**Files:**
- Modify: `bridge.py` (`_ReadOnlyCollection` class, right after the `handle_delete()` override)
- Test: `tests/test_bridge_e2e.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_ReadOnlyCollection.handle_copy(dest_path, *, depth_infinity)` and `handle_move(dest_path)` — inherited by `RootCollection`, `FolderCollection`, `ZipDirCollection`.

`DAVCollection.copy_move_single()` already raises a clean `DAVError(HTTP_FORBIDDEN)` by default, so Task 1's problem does not apply to collections — but without a `handle_copy`/`handle_move` short-circuit, wsgidav still walks the *entire* subtree first (`get_descendants(depth="infinity")`, same cost concern already documented on `handle_delete()`) before rejecting every member one by one. For a large game archive that is thousands of zip-entry lookups wasted on a request that was always going to fail.

- [ ] **Step 1: Write the failing test**

This one is about efficiency, not correctness (the request already 403s or 207s today) — write it as a call-count assertion instead of a status check, right after `test_copy_already_packed_game_file_is_forbidden_cleanly`. Patch `bridge.ZipDirCollection.get_member_names`, which is what a full descendant walk would call repeatedly:

```python
def test_copy_already_packed_game_folder_does_not_walk_the_archive(rig, monkeypatch):
    calls = []
    original = bridge.ZipDirCollection.get_member_names

    def counting(self):
        calls.append(self.node.zip_name)
        return original(self)

    monkeypatch.setattr(bridge.ZipDirCollection, "get_member_names", counting)

    resp = rig.request(
        "COPY", "/game/MyGame", headers={"Destination": rig.base + "/game/MyGame2"}
    )
    assert resp.status_code == 403, resp.status_code
    assert calls == [], f"handle_copy should short-circuit before any member listing, got {calls}"
```

(`bridge` is already imported at the top of `tests/test_bridge_e2e.py`.)

- [ ] **Step 2: Run it, confirm it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_bridge_e2e.py::test_copy_already_packed_game_folder_does_not_walk_the_archive -v`
Expected: FAIL — `calls` is non-empty (or the whole request errors some other way) because nothing short-circuits yet.

- [ ] **Step 3: Implement**

In `bridge.py`, inside `class _ReadOnlyCollection(DAVCollection):`, directly below `handle_delete()`:

```python
    def handle_copy(self, dest_path, *, depth_infinity):
        raise DAVError(HTTP_FORBIDDEN, "already uploaded — TeleDrive has no copy/rename endpoint for this.")

    def handle_move(self, dest_path):
        raise DAVError(HTTP_FORBIDDEN, "already uploaded — TeleDrive has no copy/rename endpoint for this.")
```

- [ ] **Step 4: Run it, confirm it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_bridge_e2e.py::test_copy_already_packed_game_folder_does_not_walk_the_archive -v`
Expected: PASS.

- [ ] **Step 5: Full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest tests -q`

```bash
git add bridge.py tests/test_bridge_e2e.py
git commit -m "perf: reject COPY/MOVE of an already-uploaded folder before walking it"
```

---

### Task 3: `WriteGuard` — ungate COPY

**Files:**
- Modify: `bridge.py` (`WRITE_METHODS`/`UNGATED_METHODS` constants and the `WriteGuard` docstring)
- Test: `tests/test_bridge_e2e.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `UNGATED_METHODS` now includes `"COPY"`.

This is the one change in this plan that widens what reaches the DAV layer at all: today, COPY with a source or destination outside `/game` never reaches a resource — `WriteGuard` 403s it first. After this task, COPY is decided per-resource exactly like DELETE already is: already-uploaded content (`RemoteFileResource`/`ZipFileResource`, Task 1) refuses it with a clean 403 that it could not even reach before; staged content will accept it once Tasks 4-6 give it a real implementation (until then, staged content outside `/game` still 500s via the same unimplemented-`copy_move_single` gap Task 1 just fixed for the already-uploaded case — that is expected and temporary, not a regression to chase down mid-task).

This task must run *before* Tasks 4-6: it is what makes COPY reach `UploadFileResource`/`StagingFileResource`/`StagingCollection` at all, and Task 5's cross-`/game`-boundary test needs COPY already ungated so it is red-then-green against the real per-resource guard, not against this `WriteGuard` check.

- [ ] **Step 1: Write the failing test**

This test's *current* result is already 403 (via `WriteGuard`) — after this change it needs to still be 403, but via a different mechanism (Task 1's override, now reachable because this task ungates COPY). Write it so it would catch a regression in either mechanism:

```python
def test_copy_already_uploaded_file_outside_game_is_forbidden(rig):
    resp = rig.request(
        "COPY", "/photos/small.txt", headers={"Destination": rig.base + "/photos/copy.txt"}
    )
    assert resp.status_code == 403, resp.status_code
    assert "copy.txt" not in rig.names("/photos")
```

- [ ] **Step 2: Run it, confirm it currently passes for the wrong reason**

Run: `.venv\Scripts\python.exe -m pytest tests/test_bridge_e2e.py::test_copy_already_uploaded_file_outside_game_is_forbidden -v`
Expected: PASS already (`WriteGuard` still blocks COPY outside `/game`, before this task's change). That is fine — this step is just a baseline snapshot before the gate changes; re-verify it still passes after Step 3, now for the intended reason.

- [ ] **Step 3: Implement**

In `bridge.py`, update the constant and its surrounding comment:

```python
# Verbs that mutate. Everything outside /game/<something> gets 403 for these,
# rather than mounting the whole drive read-only (which would kill /game too).
# MKCOL, PUT, DELETE and COPY are exempted below (WriteGuard) — none of the
# four needs /game specifically. MKCOL and PUT map onto real backend endpoints
# (POST /folders, and the same stage-upload-register pipeline /game uses).
# DELETE and COPY have no backend endpoint anywhere, /game included, but
# neither needs /game either: the resources themselves already draw the real
# line — still-staged writes (StagingFileResource/StagingCollection,
# UploadFileResource) accept them as local filesystem operations,
# already-uploaded resources (_ReadOnlyCollection, _ReadOnlyFile) refuse them
# — so gating by path on top would only block the /game case for no reason.
# MOVE/PROPPATCH/LOCK have no such per-resource distinction (no rename
# primitive exists even for staged content outside /game) and stay
# path-gated below.
WRITE_METHODS = {"PUT", "DELETE", "MKCOL", "MOVE", "COPY", "PROPPATCH", "LOCK", "UNLOCK"}
UNGATED_METHODS = {"MKCOL", "PUT", "DELETE", "COPY"}
```

And update the `WriteGuard` class docstring the same way (find the paragraph added for DELETE in the previous session and extend it to mention COPY — do not duplicate the whole docstring, just fold COPY into the existing sentences about DELETE).

- [ ] **Step 4: Run it, confirm it still passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_bridge_e2e.py::test_copy_already_uploaded_file_outside_game_is_forbidden -v`
Expected: still PASS — now via Task 1's `_ReadOnlyFile.copy_move_single()` instead of `WriteGuard`.

- [ ] **Step 5: Full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest tests -q` — pay special attention to the existing `test_writes_outside_game_are_forbidden` parametrized test and `test_move_into_a_read_only_path_is_forbidden`; neither targets COPY today, so neither should change behavior, but re-read them once to confirm.

```bash
git add bridge.py tests/test_bridge_e2e.py
git commit -m "feat: let COPY reach the DAV layer everywhere, like DELETE already does"
```

---

### Task 4: `GameStager.copy()` — the local-copy primitive for `/game` staging

**Files:**
- Modify: `gamestage.py` (`GameStager` class, directly below the existing `move()` method)
- Test: none yet (exercised through Task 5's DAV-level test; this task is pure plumbing)

**Interfaces:**
- Consumes: `GameStager.path_for(segments)` (existing), `GameStager.touch(top)` (existing), `config.ext_path as _ext` (already imported in `gamestage.py`).
- Produces: `GameStager.copy(src: Path, dest_segments: Sequence[str]) -> Path`.

This mirrors the existing `move()` method exactly, except it copies instead of renaming, and — because Task 3 above already removed the `WriteGuard` destination check that used to silently guarantee `dest_segments[0] == "game"` for every caller of `move()` — it must check that itself instead of trusting the caller.

- [ ] **Step 1: Implement**

In `gamestage.py`, directly below `move()`:

```python
    def copy(self, src: Path, dest_segments: Sequence[str]) -> Path:
        """Copy inside staging (dest_segments starts with the /game element).

        Unlike move(), this has no WriteGuard destination check guaranteeing
        dest_segments[0] is the game folder — COPY is ungated (see WriteGuard
        in bridge.py) so this validates it itself.
        """
        if not dest_segments or dest_segments[0] != self.cfg.game_folder:
            raise PermissionError("copy destination must stay under /game while staged")
        rest = list(dest_segments)[1:]
        dest = self.path_for(rest)
        if dest is None:
            raise PermissionError(f"copy destination is outside the staging area: {dest_segments}")
        if src.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_ext(src), _ext(dest))
        if rest:
            self.touch(rest[0])
        return dest
```

Note this creates an **empty** directory when `src.is_dir()` — it does not recurse. wsgidav's COPY handler walks the source tree itself (top-down: collections first, empty; then each file individually) and calls `copy_move_single` once per descendant — see the comment at `request_server.py:1069` ("Collections are simply created (without members)"). If `copy()` also recursed with `shutil.copytree`, every file would be copied twice.

- [ ] **Step 2: No standalone test — proceed to Task 5, which exercises this through the DAV layer.**

- [ ] **Step 3: Commit alongside Task 5** (see Task 5's commit step — do not commit this alone, since nothing calls it yet and there is nothing to verify in isolation).

---

### Task 5: `_StagingCopyMove` mixin — real COPY for `/game` staging, without duplicating it twice

**Files:**
- Modify: `bridge.py` (new `_StagingCopyMove` mixin; `StagingFileResource` and `StagingCollection` change their base classes and drop their own now-duplicate `support_recursive_move`/`move_recursive`)
- Test: `tests/test_bridge_e2e.py`

**Interfaces:**
- Consumes: `GameStager.copy()` (Task 4), `GameStager.move()` (existing), `split_dav_path()` (existing helper in `bridge.py`).
- Produces: `_StagingCopyMove.support_recursive_move(dest_path)`, `_StagingCopyMove.move_recursive(dest_path)`, `_StagingCopyMove.copy_move_single(dest_path, *, is_move)` — all three inherited by both `StagingFileResource` and `StagingCollection`.

`StagingFileResource` and `StagingCollection` already have byte-for-byte identical `support_recursive_move`/`move_recursive` bodies today (both just delegate to `self.stager`, and a file vs. a directory makes no difference to `GameStager.move()`, which uses `os.replace` either way). Adding a third identical method (`copy_move_single`) to both would triple that duplication instead of fixing it. Pull all three into one mixin instead, and delete the two classes' existing copies of `support_recursive_move`/`move_recursive` as part of this task.

- [ ] **Step 1: Write the failing tests**

Add next to `test_delete_inside_staging_is_allowed` in `tests/test_bridge_e2e.py`:

```python
def test_copy_inside_staging_creates_an_independent_second_file(rig):
    rig.request("MKCOL", "/game/Temp")
    rig.request("PUT", "/game/Temp/a.bin", data=b"junk")

    resp = rig.request(
        "COPY", "/game/Temp/a.bin", headers={"Destination": rig.base + "/game/Temp/b.bin"}
    )
    assert resp.status_code == 201, resp.status_code
    assert rig.names("/game/Temp") == ["a.bin", "b.bin"]
    assert (rig.cfg.staging_dir / "Temp" / "a.bin").read_bytes() == b"junk"
    assert (rig.cfg.staging_dir / "Temp" / "b.bin").read_bytes() == b"junk"

    # Independent afterwards: deleting one must not touch the other.
    rig.request("DELETE", "/game/Temp/a.bin")
    assert rig.names("/game/Temp") == ["b.bin"]


def test_copy_a_staging_folder_creates_an_empty_destination_and_copies_files_individually(rig):
    rig.request("MKCOL", "/game/Temp")
    rig.request("PUT", "/game/Temp/a.bin", data=b"junk")

    resp = rig.request(
        "COPY", "/game/Temp", headers={"Destination": rig.base + "/game/Temp2/"}
    )
    assert resp.status_code == 201, resp.status_code
    assert rig.names("/game/Temp2") == ["a.bin"]
    assert (rig.cfg.staging_dir / "Temp2" / "a.bin").read_bytes() == b"junk"
    assert (rig.cfg.staging_dir / "Temp" / "a.bin").exists(), "source must survive a COPY"


def test_copy_out_of_game_staging_is_forbidden(rig):
    rig.request("MKCOL", "/game/Temp")
    rig.request("PUT", "/game/Temp/a.bin", data=b"junk")

    resp = rig.request(
        "COPY", "/game/Temp/a.bin", headers={"Destination": rig.base + "/photos/escaped.bin"}
    )
    assert resp.status_code == 403, resp.status_code
    assert "escaped.bin" not in rig.names("/photos")
```

The third test relies on Task 3 having already ungated COPY: with `WriteGuard` no longer checking the destination path for COPY, the only thing standing between a staged file and an escape out of `/game` is `GameStager.copy()`'s own guard (Task 4). Before this task's Step 3, that request 500s (unimplemented `copy_move_single`) rather than 403s — it is red for the right reason, not passing by accident via `WriteGuard`.

- [ ] **Step 2: Run them, confirm they fail**

Run:
```
.venv\Scripts\python.exe -m pytest tests/test_bridge_e2e.py::test_copy_inside_staging_creates_an_independent_second_file tests/test_bridge_e2e.py::test_copy_a_staging_folder_creates_an_empty_destination_and_copies_files_individually tests/test_bridge_e2e.py::test_copy_out_of_game_staging_is_forbidden -v
```

Expected: all three FAIL (500s, since `copy_move_single` is unimplemented on these classes today).

- [ ] **Step 3: Implement the mixin and wire both classes to it**

In `bridge.py`, directly above `class StagingCollection(DAVCollection):`, add:

```python
class _StagingCopyMove:
    """Shared copy/move plumbing for StagingCollection and StagingFileResource.

    Both wrap a plain local path under GameStager; a file vs. a directory
    makes no difference to GameStager.move()/copy() (os.replace and
    shutil.copy2/mkdir already branch on that internally), so the two
    classes need this identical regardless of which one they otherwise
    subclass. Mixed in first so its methods win the MRO over DAVCollection's
    own copy_move_single() default.
    """

    def support_recursive_move(self, dest_path):
        return self.stager.path_for(split_dav_path(dest_path)[1:]) is not None

    def move_recursive(self, dest_path):
        self.stager.move(self.local, split_dav_path(dest_path))

    def copy_move_single(self, dest_path, *, is_move):
        try:
            if is_move:
                self.stager.move(self.local, split_dav_path(dest_path))
            else:
                self.stager.copy(self.local, split_dav_path(dest_path))
        except PermissionError as exc:
            raise DAVError(HTTP_FORBIDDEN, str(exc))
```

Change the class declaration and delete the now-duplicate methods:

```python
class StagingCollection(_StagingCopyMove, DAVCollection):
```

Remove `StagingCollection`'s own `support_recursive_move` and `move_recursive` methods (currently right after `delete()`) — they are now inherited from `_StagingCopyMove`.

Do the same for `StagingFileResource`:

```python
class StagingFileResource(_StagingCopyMove, DAVNonCollection):
```

Remove `StagingFileResource`'s own `support_recursive_move` and `move_recursive` methods (currently right after `delete()`) the same way.

The `is_move` branch inside `copy_move_single` is defensive/never actually reached in practice today — `support_recursive_move()` makes wsgidav use `move_recursive()` instead whenever it returns `True`, which it always does here. It costs nothing to handle correctly anyway, and it keeps the method's contract honest.

- [ ] **Step 4: Run the Step 1 tests again, confirm they pass**

Run the same command as Step 2.
Expected: all three PASS.

- [ ] **Step 5: Full suite + commit (Tasks 4 and 5 together)**

Run: `.venv\Scripts\python.exe -m pytest tests -q`

```bash
git add bridge.py gamestage.py tests/test_bridge_e2e.py
git commit -m "feat: real COPY for content still staged under /game"
```

---

### Task 6: `UploadFileResource.copy_move_single()` — real COPY for general-path staging

**Files:**
- Modify: `bridge.py` (`UploadFileResource` class)
- Test: `tests/test_bridge_e2e.py`

**Interfaces:**
- Consumes: `UploadStager.create_file(segments, parent_id)` (existing), `UploadStager.api.resolve(segments)` (existing `TeleDriveClient` method, already used the same way in `RemoteFileResource.begin_write`), `config.ext_path as _ext` (needs importing into `bridge.py` — see Step 3).
- Produces: `UploadFileResource.copy_move_single(dest_path, *, is_move)`.

- [ ] **Step 1: Write the failing tests**

Add next to `test_delete_pending_general_upload_is_allowed` in `tests/test_bridge_e2e.py`:

```python
def test_copy_pending_general_upload_creates_an_independent_second_file(rig):
    rig.request("PUT", "/photos/pending.bin", data=b"waiting")

    resp = rig.request(
        "COPY", "/photos/pending.bin", headers={"Destination": rig.base + "/photos/pending2.bin"}
    )
    assert resp.status_code == 201, resp.status_code
    assert rig.names("/photos") == ["pending.bin", "pending2.bin", "shot.png", "small.txt"]
    assert (rig.cfg.upload_dir / "photos" / "pending.bin").read_bytes() == b"waiting"
    assert (rig.cfg.upload_dir / "photos" / "pending2.bin").read_bytes() == b"waiting"

    # The two uploads are independent: uploading one must not affect the other.
    _upload_now(rig, "photos", "pending2.bin")
    assert (rig.cfg.upload_dir / "photos" / "pending.bin").exists()
    row = next(r for r in rig.backend.rows if r["filename"] == "pending2.bin")
    photos_id = next(r["file_id"] for r in rig.backend.rows if r["filename"] == "photos")
    assert row["parent_id"] == photos_id


def test_copy_general_pending_upload_across_game_boundary_is_forbidden(rig):
    rig.request("PUT", "/photos/pending.bin", data=b"waiting")

    resp = rig.request(
        "COPY", "/photos/pending.bin", headers={"Destination": rig.base + "/game/escaped.bin"}
    )
    assert resp.status_code == 403, resp.status_code
    assert "escaped.bin" not in rig.names("/game")
```

These both rely on Task 3 having already ungated COPY outside `/game` — without it, both requests never even reach `UploadFileResource`.

- [ ] **Step 2: Run them, confirm they fail**

Run:
```
.venv\Scripts\python.exe -m pytest tests/test_bridge_e2e.py::test_copy_pending_general_upload_creates_an_independent_second_file tests/test_bridge_e2e.py::test_copy_general_pending_upload_across_game_boundary_is_forbidden -v
```
Expected: both FAIL (500s — `copy_move_single` unimplemented on `UploadFileResource`).

- [ ] **Step 3: Implement**

Add the import at the top of `bridge.py`, alongside the existing `from config import Config, load_config`:

```python
from config import Config, ext_path as _ext, load_config
```

Then, inside `class UploadFileResource(DAVNonCollection):`, directly below `delete()`:

```python
    def copy_move_single(self, dest_path, *, is_move):
        if is_move:
            raise DAVError(HTTP_FORBIDDEN, "no rename primitive for a pending upload")
        dest_segments = split_dav_path(dest_path)
        if not dest_segments or dest_segments[0] == self.upload_stager.cfg.game_folder:
            raise DAVError(HTTP_FORBIDDEN, "cannot copy a pending upload into /game")
        parent = self.upload_stager.api.resolve(dest_segments[:-1]) if len(dest_segments) > 1 else None
        parent_id = parent.file_id if parent is not None else None
        dest_local = self.upload_stager.create_file(dest_segments, parent_id)
        shutil.copy2(_ext(self.local), _ext(dest_local))
```

Also update the class docstring (added in the DELETE fix) to mention COPY is now real too — find the sentence "DELETE is offered (see delete())..." and extend it in the same spirit, noting COPY is likewise a local filesystem operation with no backend involvement.

- [ ] **Step 4: Run them, confirm they pass**

Run the same command as Step 2.
Expected: both PASS.

- [ ] **Step 5: Full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest tests -q`

```bash
git add bridge.py tests/test_bridge_e2e.py
git commit -m "feat: real COPY for pending general-path uploads"
```

---

### Task 7: Update `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:** none — documentation only.

- [ ] **Step 1: Update the "一般路徑的寫入" section**

Find the bullet list added for `MKCOL`/`PUT`/`DELETE` in the previous session (search for `**`DELETE`** 能不能做`). Add a fourth bullet for `COPY`, following the same voice:

```markdown
- **`COPY`** 跟 `DELETE` 一樣看「還在暫存 vs. 已上傳」，不看路徑：還在暫存的來源
  （`UploadFileResource`/`StagingFileResource`/`StagingCollection`）真的用
  `shutil.copy2`／建空目錄複製一份，來源不受影響；已上傳的來源一律 403
  （`_ReadOnlyFile`/`_ReadOnlyCollection`）。複製目的地一旦跨過 `/game` 邊界
  （暫存中的一般檔案複製進 `/game`，或反過來）也是 403——那不是同一個 stager，
  沒有共通的落地邏輯可以套。`MOVE` 維持原樣只在 `/game` 放行：一般路徑的暫存
  沒有搬移原語（`UploadStager` 沒有 `move()`）。
```

- [ ] **Step 2: Update the "明確不做" section**

Find the bullet starting with `**MOVE`/`COPY`/`PROPPATCH`/`LOCK` 限定在...`` (updated in the previous session). Change it to drop `COPY` from that list, since it is no longer limited to `/game`:

```markdown
- **`MOVE`/`PROPPATCH`/`LOCK` 限定在 `/game/<name>/...`**：
  這些動詞在 `/game` 以外沒有對得到的 backend 端點（沒有真正的改名），也沒有
  `DELETE`/`COPY` 那種「本機暫存 vs. 已上傳」的乾淨分界可以套——連還在 staging
  的一般路徑寫入也沒有搬移原語（`upload_stager` 不像 `gamestage.GameStager`
  有 `move()`）。`WriteGuard`（`bridge.py`）放行 `MKCOL`（`POST /folders`）、
  `PUT`、`DELETE`、`COPY`（見「一般路徑的寫入」），其餘維持 403。
```

- [ ] **Step 3: Update the test coverage table**

Find the `tests/test_bridge_e2e.py` row in the "測試" section's table. Append a clause covering COPY, following the existing style (comma-separated Chinese description of what got added):

```
/ COPY 對已上傳內容一致 403（檔案與資料夾兩種 resource 都不因遞迴列出整棵樹而 500）、
對還在暫存的內容（`/game` 與一般路徑）做出真正的本機複製、跨 `/game` 邊界複製一律 403
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: describe the new COPY behavior in CLAUDE.md"
```

---

### Task 8: Final full-suite verification

**Files:** none — verification only.

- [ ] **Step 1: Run the whole suite one more time from a clean checkout state**

```
.venv\Scripts\python.exe -m pytest tests -q
```
Expected: 100% pass, no warnings about unraised exceptions or leaked threads.

- [ ] **Step 2: Manual sanity check (cannot be automated — no real TeleDrive in CI)**

Per `CLAUDE.md`'s existing manual-test checklist, after mounting for real: copy a file within an active (not-yet-packed) `/game/<name>` staging folder in Explorer, confirm both copies appear and both eventually upload as independent entries after the debounce window. This is the same category of manual step already listed in `CLAUDE.md`'s 「測試」 section (#7) for the general-path PUT feature — add it there as an addendum once done, or note if it surfaces something this plan did not anticipate.

- [ ] **Step 3: Report back**

Summarize: tasks completed, any test added beyond what this plan specified, anything found during manual testing that needs a follow-up plan.
