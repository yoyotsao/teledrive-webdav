"""Crash-durable state used by storage-parity uploads.

The backend is authoritative for Telegram operation state.  This module owns
only local facts that must survive a bridge restart before a backend operation
can be committed: generation-owned staging paths and cross-instance group
barriers.  Every destructive action is conditional on the same generation
that created the bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

from transfer_models import StagingIdentity


class StateConflictError(RuntimeError):
    """Durable local state changed underneath the caller."""


def _atomic_json(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    data = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _read_json(path: Path) -> Optional[dict]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return body if isinstance(body, dict) else None


def _key(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


class _DirectoryLock:
    """A tiny cross-process lock using atomic directory creation.

    No platform-specific fcntl/msvcrt contract is required.  A process killed
    while owning a lock leaves a directory; locks older than ``stale_after``
    are reclaimed because all protected writes use atomic replace.
    """

    def __init__(self, path: Path, *, timeout: float = 10.0, stale_after: float = 30.0):
        self.path = path
        self.timeout = timeout
        self.stale_after = stale_after
        self.owned = False

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self.path.mkdir()
                self.owned = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    age = 0
                if age > self.stale_after:
                    try:
                        self.path.rmdir()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out acquiring state lock {self.path.name}")
                time.sleep(0.01)

    def __exit__(self, *_exc):
        if self.owned:
            try:
                self.path.rmdir()
            except OSError:
                pass
            self.owned = False


class StageGenerationStore:
    """Unique physical source per logical path generation.

    ``begin`` never reuses a path.  Consequently an old writer may continue to
    hold its old file descriptor, but it cannot mutate or remove the newer
    generation.  ``cleanup`` and ``finish`` compare the persisted active
    identity before doing anything destructive.
    """

    VERSION = 1

    def __init__(self, root: Path):
        self.root = Path(root) / ".parity-staging"
        self.root.mkdir(parents=True, exist_ok=True)

    def _unit(self, logical_key: str) -> Path:
        return self.root / _key(logical_key)

    def _manifest(self, logical_key: str) -> Path:
        return self._unit(logical_key) / "active.json"

    def _lock(self, logical_key: str):
        return _DirectoryLock(self.root / ".locks" / _key(logical_key))

    @staticmethod
    def _body(identity: StagingIdentity) -> dict:
        return {
            "version": StageGenerationStore.VERSION,
            "logical_key": identity.logical_key,
            "transfer_id": identity.transfer_id,
            "staging_generation": identity.staging_generation,
            "source_path": identity.source_path,
        }

    @staticmethod
    def _identity(body: Optional[dict]) -> Optional[StagingIdentity]:
        if not body or body.get("version") != StageGenerationStore.VERSION:
            return None
        try:
            return StagingIdentity(
                logical_key=str(body["logical_key"]),
                transfer_id=str(body["transfer_id"]),
                staging_generation=int(body["staging_generation"]),
                source_path=str(body["source_path"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def active(self, logical_key: str) -> Optional[StagingIdentity]:
        return self._identity(_read_json(self._manifest(logical_key)))

    def begin(self, logical_key: str, source_name: str) -> StagingIdentity:
        source_name = Path(source_name).name
        if source_name in {"", ".", ".."}:
            raise ValueError("source_name must name one file")
        with self._lock(logical_key):
            previous = self.active(logical_key)
            generation = 1 if previous is None else previous.staging_generation + 1
            transfer_id = uuid.uuid4().hex
            directory = self._unit(logical_key) / "generations" / f"{generation:020d}-{transfer_id}"
            directory.mkdir(parents=True, exist_ok=False)
            source = directory / source_name
            source.touch(exist_ok=False)
            identity = StagingIdentity(
                logical_key=str(logical_key),
                transfer_id=transfer_id,
                staging_generation=generation,
                source_path=str(source),
            )
            _atomic_json(self._manifest(logical_key), self._body(identity))
            return identity

    @staticmethod
    def source(identity: StagingIdentity) -> Path:
        return Path(identity.source_path)

    def _is_active(self, identity: StagingIdentity) -> bool:
        return self.active(identity.logical_key) == identity

    def mark_durable(self, identity: StagingIdentity) -> bool:
        with self._lock(identity.logical_key):
            if not self._is_active(identity):
                return False
            source = self.source(identity)
            if not source.exists():
                raise FileNotFoundError(source)
            marker = source.parent / ".durable"
            marker.write_text(identity.transfer_id, encoding="ascii")
            return True

    def finish(self, identity: StagingIdentity) -> bool:
        """Close-time ownership check; stale handles are deliberately inert."""
        return self.mark_durable(identity)

    def cleanup(self, identity: StagingIdentity) -> bool:
        with self._lock(identity.logical_key):
            if not self._is_active(identity):
                return False
            shutil.rmtree(self.source(identity).parent, ignore_errors=True)
            try:
                self._manifest(identity.logical_key).unlink()
            except OSError:
                pass
            return True

    def recover(self) -> tuple[StagingIdentity, ...]:
        recovered = []
        for unit in self.root.iterdir():
            if not unit.is_dir() or unit.name == ".locks":
                continue
            manifest = unit / "active.json"
            identity = self._identity(_read_json(manifest))
            active_dir = self.source(identity).parent if identity is not None else None
            generations = unit / "generations"
            if identity is not None:
                source = self.source(identity)
                marker = source.parent / ".durable"
                if source.exists() and marker.exists():
                    recovered.append(identity)
                else:
                    if active_dir is not None:
                        shutil.rmtree(active_dir, ignore_errors=True)
                    try:
                        manifest.unlink()
                    except OSError:
                        pass
                    identity = None
                    active_dir = None
            if generations.exists():
                for directory in generations.iterdir():
                    if directory.is_dir() and directory != active_dir:
                        shutil.rmtree(directory, ignore_errors=True)
        return tuple(sorted(recovered, key=lambda item: item.logical_key))


class GroupBarrierStore:
    """Atomic per-group readiness visible to every bridge instance."""

    VERSION = 1

    def __init__(self, root: Path):
        self.root = Path(root) / ".parity-groups"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, group_id: str) -> Path:
        return self.root / f"{_key(group_id)}.json"

    def _lock(self, group_id: str):
        return _DirectoryLock(self.root / ".locks" / _key(group_id))

    def create(self, group_id: str, children: Iterable[str]) -> dict:
        expected = tuple(dict.fromkeys(str(child) for child in children))
        if not expected:
            raise ValueError("a group requires at least one child")
        with self._lock(group_id):
            existing = _read_json(self._path(group_id))
            if existing is not None:
                if tuple(existing.get("expected") or ()) != expected:
                    raise StateConflictError("group already exists with different children")
                return existing
            body = {
                "version": self.VERSION,
                "group_id": str(group_id),
                "expected": list(expected),
                "durable": [],
                "send_armed": False,
                "send_started": False,
            }
            _atomic_json(self._path(group_id), body)
            return body

    def _body(self, group_id: str) -> dict:
        body = _read_json(self._path(group_id))
        if body is None or body.get("version") != self.VERSION:
            raise KeyError(group_id)
        return body

    def mark_durable(self, group_id: str, child: str) -> bool:
        with self._lock(group_id):
            body = self._body(group_id)
            expected = tuple(body.get("expected") or ())
            child = str(child)
            if child in expected:
                durable = set(str(item) for item in body.get("durable") or ())
                durable.add(child)
                body["durable"] = [item for item in expected if item in durable]
                body["send_armed"] = set(expected).issubset(durable)
                _atomic_json(self._path(group_id), body)
            return bool(body.get("send_armed"))

    def durable_children(self, group_id: str) -> tuple[str, ...]:
        body = self._body(group_id)
        expected = tuple(body.get("expected") or ())
        durable = set(str(item) for item in body.get("durable") or ())
        return tuple(item for item in expected if item in durable)

    def ready(self, group_id: str) -> bool:
        return bool(self._body(group_id).get("send_armed"))

    def mark_send_started(self, group_id: str) -> bool:
        with self._lock(group_id):
            body = self._body(group_id)
            if not body.get("send_armed"):
                return False
            if body.get("send_started"):
                return False
            body["send_started"] = True
            _atomic_json(self._path(group_id), body)
            return True

    def remove(self, group_id: str) -> None:
        with self._lock(group_id):
            try:
                self._path(group_id).unlink()
            except OSError:
                pass
