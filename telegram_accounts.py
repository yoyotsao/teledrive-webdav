"""Configured Telegram accounts, lifecycle isolation, and exact routing."""

from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

from config import ConfigError
from tgio import TelegramWorker
from transfer_models import AccountSpec

log = logging.getLogger("tgaccounts")


class AccountUnavailableError(RuntimeError):
    """A requested account is not configured, online, or upload eligible."""


def load_account_specs(path: Path) -> list[AccountSpec]:
    """Read and validate the ordered account file without exposing secrets."""
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ConfigError(f"cannot read accounts file {path}: {exc}") from None
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid accounts file {path}: {exc}") from None

    rows = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ConfigError(f"accounts file {path} must contain a non-empty 'accounts' array")

    specs: list[AccountSpec] = []
    seen: set[int] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ConfigError(f"account {index + 1} in {path} must be an object")
        user_id = row.get("telegram_user_id")
        label = row.get("label")
        session = row.get("session")
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ConfigError(
                f"account {index + 1} in {path} has invalid telegram_user_id"
            )
        if user_id in seen:
            raise ConfigError(f"duplicate telegram_user_id {user_id} in accounts file {path}")
        if not isinstance(label, str) or not label.strip():
            raise ConfigError(f"account {user_id} in {path} has an empty label")
        if not isinstance(session, str) or not session:
            raise ConfigError(f"account {user_id} ({label.strip()}) in {path} has an empty session")
        seen.add(user_id)
        specs.append(AccountSpec(user_id, label.strip(), session))
    return specs


@dataclass
class AccountRuntime:
    spec: AccountSpec
    worker: TelegramWorker
    file_slots: threading.BoundedSemaphore
    online: bool = False
    linked: bool = False
    error: Optional[str] = None
    # Task 5 supplies both concrete implementations. Keeping these injectable
    # makes ownership explicit without creating a second temporary controller.
    chunk_limiter: Optional[object] = None
    message_limiter: Optional[object] = None
    _started: bool = field(default=False, init=False, repr=False)

    @property
    def telegram_user_id(self) -> int:
        actual = self.worker.user_id
        return int(actual) if actual is not None else self.spec.telegram_user_id

    @property
    def label(self) -> str:
        return self.spec.label


class TelegramAccountPool:
    """Own one worker and independent admission state per Telegram account."""

    def __init__(
        self,
        specs: Sequence[AccountSpec],
        *,
        api_id: int,
        api_hash: str,
        download_connections: int = 8,
        upload_files: int = 3,
        upload_parts: int = 12,
        worker_factory: Callable[..., TelegramWorker] = TelegramWorker,
        chunk_limiter_factory: Optional[Callable[[], object]] = None,
        message_limiter_factory: Optional[Callable[[], object]] = None,
    ) -> None:
        if not specs:
            raise ConfigError("at least one Telegram account is required")
        ids = [spec.telegram_user_id for spec in specs if spec.telegram_user_id != 0]
        duplicate = next((user_id for user_id in ids if ids.count(user_id) > 1), None)
        if duplicate is not None:
            raise ConfigError(f"duplicate telegram_user_id {duplicate}")

        self._runtimes: list[AccountRuntime] = []
        for spec in specs:
            worker = worker_factory(
                api_id,
                api_hash,
                spec.session,
                download_connections,
                upload_parts=upload_parts,
            )
            self._runtimes.append(
                AccountRuntime(
                    spec=spec,
                    worker=worker,
                    file_slots=threading.BoundedSemaphore(upload_files),
                    chunk_limiter=(chunk_limiter_factory() if chunk_limiter_factory else None),
                    message_limiter=(message_limiter_factory() if message_limiter_factory else None),
                )
            )
        self._by_id = {
            runtime.spec.telegram_user_id: runtime
            for runtime in self._runtimes
            if runtime.spec.telegram_user_id != 0
        }
        self._rr = 0
        self._lock = threading.Lock()
        self._started = False

    @classmethod
    def from_config(
        cls,
        cfg,
        *,
        worker_factory: Callable[..., TelegramWorker] = TelegramWorker,
        chunk_limiter_factory: Optional[Callable[[], object]] = None,
        message_limiter_factory: Optional[Callable[[], object]] = None,
    ) -> "TelegramAccountPool":
        specs = (
            load_account_specs(cfg.accounts_file)
            if cfg.accounts_file is not None
            else [AccountSpec(0, "primary", cfg.session)]
        )
        return cls(
            specs,
            api_id=cfg.api_id,
            api_hash=cfg.api_hash,
            download_connections=cfg.download_connections,
            upload_files=cfg.upload_files,
            upload_parts=cfg.upload_parts,
            worker_factory=worker_factory,
            chunk_limiter_factory=chunk_limiter_factory,
            message_limiter_factory=message_limiter_factory,
        )

    @property
    def primary(self) -> AccountRuntime:
        return self._runtimes[0]

    @property
    def eligible_upload_ids(self) -> tuple[int, ...]:
        return tuple(
            runtime.telegram_user_id
            for runtime in self._runtimes
            if runtime.online and runtime.linked
        )

    def runtime(self, telegram_user_id: int) -> AccountRuntime:
        if telegram_user_id == 0:
            return self.primary
        runtime = self._by_id.get(int(telegram_user_id))
        if runtime is None:
            raise AccountUnavailableError(f"Telegram account {telegram_user_id} is not configured")
        return runtime

    def start(self, api) -> None:
        if self._started:
            return
        for runtime in self._runtimes:
            self._start_runtime(runtime)

        primary = self.primary
        if not primary.online:
            self.stop()
            raise AccountUnavailableError(self._unavailable_message(primary))

        try:
            api.set_dm_sender(primary.worker.send_dm)
            api.login()
            linked_ids = api.linked_account_ids()
        except Exception as exc:
            self.stop()
            detail = self._safe_detail(primary, exc)
            raise AccountUnavailableError(f"primary account authentication failed: {detail}") from exc

        for index, runtime in enumerate(self._runtimes):
            runtime.linked = runtime.online and (
                index == 0 or runtime.telegram_user_id in linked_ids
            )
        self._started = True

    def _start_runtime(self, runtime: AccountRuntime) -> None:
        try:
            runtime.worker.start()
            runtime._started = True
            actual = runtime.worker.user_id
            if actual is None:
                raise RuntimeError("connected session did not return a Telegram user ID")
            actual = int(actual)
            expected = runtime.spec.telegram_user_id
            if expected != 0 and actual != expected:
                raise RuntimeError(f"session user ID mismatch: expected {expected}, got {actual}")
            other = self._by_id.get(actual)
            if other is not None and other is not runtime:
                raise RuntimeError(f"session user ID {actual} conflicts with another account")
            self._by_id[actual] = runtime
            runtime.online = True
            runtime.error = None
        except Exception as exc:
            runtime.error = self._safe_detail(runtime, exc)
            runtime.online = False
            runtime.linked = False
            try:
                runtime.worker.stop()
            except Exception:
                pass
            runtime._started = False
            log.error("Telegram account unavailable: %s", runtime.error)

    def stop(self) -> None:
        for runtime in reversed(self._runtimes):
            if runtime._started:
                try:
                    runtime.worker.stop()
                except Exception as exc:  # best effort: every other account still stops
                    log.warning("Telegram account shutdown failed: %s", self._safe_detail(runtime, exc))
                runtime._started = False
            runtime.online = False
            runtime.linked = False
        self._started = False

    def for_read(self, telegram_user_id: int) -> AccountRuntime:
        runtime = self.runtime(telegram_user_id)
        if not runtime.online:
            raise AccountUnavailableError(self._unavailable_message(runtime))
        return runtime

    @contextmanager
    def acquire_upload(self, timeout: Optional[float] = None) -> Iterator[AccountRuntime]:
        runtime = self._choose_free_runtime(timeout)
        try:
            yield runtime
        finally:
            runtime.file_slots.release()

    def _choose_free_runtime(self, timeout: Optional[float]) -> AccountRuntime:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative or None")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                count = len(self._runtimes)
                order = [(self._rr + offset) % count for offset in range(count)]
                for index in order:
                    runtime = self._runtimes[index]
                    if runtime.online and runtime.linked and runtime.file_slots.acquire(blocking=False):
                        self._rr = (index + 1) % count
                        return runtime
                any_eligible = any(r.online and r.linked for r in self._runtimes)
            if not any_eligible:
                raise AccountUnavailableError("no online linked Telegram account is available for upload")
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AccountUnavailableError("timed out waiting for a Telegram upload file slot")
                time.sleep(min(0.01, remaining))
            else:
                time.sleep(0.01)

    def status(self) -> dict:
        return {
            "accounts": [
                {
                    "telegram_user_id": runtime.telegram_user_id,
                    "label": runtime.label,
                    "online": runtime.online,
                    "linked": runtime.linked,
                    "error": runtime.error,
                    "limiter": self._limiter_status(runtime.chunk_limiter),
                }
                for runtime in self._runtimes
            ],
            "eligible_upload_ids": list(self.eligible_upload_ids),
        }

    @staticmethod
    def _limiter_status(limiter: Optional[object]) -> dict:
        stats = getattr(limiter, "stats", None)
        return dict(stats()) if callable(stats) else {}

    @staticmethod
    def _safe_detail(runtime: AccountRuntime, exc: BaseException) -> str:
        detail = str(exc).replace(runtime.spec.session, "[redacted]")
        prefix = f"account {runtime.spec.telegram_user_id} ({runtime.spec.label})"
        return f"{prefix}: {detail or type(exc).__name__}"

    @staticmethod
    def _unavailable_message(runtime: AccountRuntime) -> str:
        return runtime.error or (
            f"account {runtime.spec.telegram_user_id} ({runtime.spec.label}) is offline"
        )
