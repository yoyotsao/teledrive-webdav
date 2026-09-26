"""Safe discovery and lifetime locking for Telegram SQLite session files."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from config import ConfigError
from transfer_models import AccountSpec

ACCOUNT_FILE = re.compile(r"^([1-9][0-9]*)\.session$")


class SessionLockError(RuntimeError):
    """The cooperating bridge/sessionctl lock could not be acquired or released."""


def _lock_one_byte_nonblocking(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        if stream.read(1) == b"":
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_one_byte(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class SessionDirectoryLock:
    """Exclusive lifetime lock shared by the bridge and sessionctl."""

    def __init__(self, session_dir: Path):
        self.path = Path(session_dir) / ".teledrive-session.lock"
        self._stream = None

    def acquire(self) -> "SessionDirectoryLock":
        if self._stream is not None:
            return self
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
            raise SessionLockError(
                "Telegram session directory is already in use or unavailable"
            ) from None
        self._stream = stream
        return self

    def release(self) -> None:
        if self._stream is None:
            return
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

    def __enter__(self) -> "SessionDirectoryLock":
        return self.acquire()

    def __exit__(self, *_exc) -> None:
        self.release()


def safe_resolve_existing(path: Path, *, kind: str) -> Path:
    """Resolve an existing path without allowing the path into public errors."""
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


def discover_account_specs(
    session_dir: Path, primary_user_id: int, app_root: Path,
) -> list[AccountSpec]:
    """Discover direct ``<positive user id>.session`` children deterministically."""
    directory = safe_resolve_existing(session_dir, kind="Telegram session directory")
    root = safe_resolve_existing(app_root, kind="application root")
    if not directory.is_dir() or directory == root or root in directory.parents:
        raise ConfigError(
            "session_dir must be an existing directory outside the application root"
        ) from None

    specs: list[AccountSpec] = []
    for candidate in safe_direct_children(directory):
        if not candidate.name.endswith(".session"):
            continue
        match = ACCOUNT_FILE.fullmatch(candidate.name)
        if match is None:
            digest = hashlib.sha256(
                candidate.name.encode("utf-8", "surrogatepass")
            ).hexdigest()[:12]
            raise ConfigError(
                f"invalid .session candidate sha256={digest} length={len(candidate.name)}"
            ) from None
        user_id = int(match.group(1))
        resolved = safe_resolve_existing(
            candidate, kind=f"Telegram session for account {user_id}",
        )
        if resolved.parent != directory or not resolved.is_file():
            raise ConfigError(
                f"Telegram session {user_id} must resolve to a direct regular file"
            ) from None
        specs.append(AccountSpec(user_id, resolved))

    if not specs:
        raise ConfigError("Telegram session directory contains no Telegram session files")
    by_id = {spec.telegram_user_id: spec for spec in specs}
    if primary_user_id not in by_id:
        raise ConfigError(
            f"primary Telegram account {primary_user_id} has no session file"
        ) from None
    return [
        by_id[primary_user_id],
        *sorted(
            (spec for spec in specs if spec.telegram_user_id != primary_user_id),
            key=lambda spec: spec.telegram_user_id,
        ),
    ]
