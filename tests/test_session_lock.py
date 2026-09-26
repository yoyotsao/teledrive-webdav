from __future__ import annotations

import multiprocessing
import traceback
from pathlib import Path

import pytest

from telegram_sessions import SessionDirectoryLock, SessionLockError


def _try_lock(directory: str, queue) -> None:
    try:
        lock = SessionDirectoryLock(Path(directory)).acquire()
    except SessionLockError:
        queue.put(False)
        return
    try:
        queue.put(True)
    finally:
        lock.release()


def test_second_lock_fails_without_exposing_directory(tmp_path):
    secret_dir = tmp_path / "credential-vault-private"
    secret_dir.mkdir()
    first = SessionDirectoryLock(secret_dir).acquire()
    try:
        with pytest.raises(SessionLockError, match="already in use") as raised:
            SessionDirectoryLock(secret_dir).acquire()
        rendered = "".join(traceback.format_exception(raised.value))
        assert str(secret_dir) not in rendered
        assert secret_dir.name not in rendered
        assert raised.value.__cause__ is None
    finally:
        first.release()


def test_second_process_contends_and_release_allows_reacquire(tmp_path):
    directory = tmp_path / "sessions"
    directory.mkdir()
    first = SessionDirectoryLock(directory).acquire()
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    process = ctx.Process(target=_try_lock, args=(str(directory), queue))
    process.start()
    process.join(5)
    try:
        assert process.exitcode == 0
        assert queue.get(timeout=1) is False
    finally:
        first.release()
    second = SessionDirectoryLock(directory).acquire()
    second.release()


def test_release_seeks_back_to_locked_byte_before_unlock(tmp_path):
    directory = tmp_path / "sessions"
    directory.mkdir()
    lock = SessionDirectoryLock(directory).acquire()
    lock._stream.seek(0, 2)
    lock.release()
    SessionDirectoryLock(directory).acquire().release()


def test_lock_release_is_idempotent(tmp_path):
    directory = tmp_path / "sessions"
    directory.mkdir()
    lock = SessionDirectoryLock(directory).acquire()
    lock.release()
    lock.release()
