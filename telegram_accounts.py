"""Configured Telegram accounts, lifecycle isolation, and exact routing."""

from __future__ import annotations

import inspect
import logging
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

from config import ConfigError, HERE
from telegram_sessions import discover_account_specs
from tgio import SessionClientError, SessionIdentityError, TelegramWorker
from transfer_models import AccountSpec
from upload_activity import AccountActivityRegistry, ActivityChange, UploadSpeedTracker

log = logging.getLogger("tgaccounts")


class AccountUnavailableError(RuntimeError):
    """A requested account is not configured, online, or upload eligible."""


@dataclass
class AccountRuntime:
    spec: AccountSpec
    worker: TelegramWorker
    file_slots: threading.BoundedSemaphore
    online: bool = False
    linked: bool = False
    error: Optional[str] = None
    chunk_limiter: Optional[object] = None
    message_limiter: Optional[object] = None
    _started: bool = field(default=False, init=False, repr=False)

    @property
    def telegram_user_id(self) -> int:
        actual = self.worker.user_id
        return int(actual) if actual is not None else self.spec.telegram_user_id


class UploadLease:
    """Own exactly one account file slot and its byte-upload activity job."""

    def __init__(self, pool: "TelegramAccountPool", runtime: AccountRuntime, work_id: str):
        self.pool = pool
        self.runtime = runtime
        self.work_id = work_id
        self._closed = False

    def __enter__(self):
        return self.runtime

    def close(self) -> ActivityChange:
        with self.pool._lock:
            if self._closed:
                return ActivityChange(self.runtime.telegram_user_id, False, False, False)
            self._closed = True
            return self.pool._release_upload_locked(self.runtime, self.work_id)

    def __exit__(self, *_exc):
        self.close()



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
        chunk_limiter_factory: Optional[Callable[..., object]] = None,
        message_limiter_factory: Optional[Callable[[], object]] = None,
        speed_tracker: Optional[UploadSpeedTracker] = None,
        activity_registry: Optional[AccountActivityRegistry] = None,
    ) -> None:
        if not specs:
            raise ConfigError("at least one Telegram account is required")
        ids = [spec.telegram_user_id for spec in specs]
        duplicate = next((user_id for user_id in ids if ids.count(user_id) > 1), None)
        if duplicate is not None:
            raise ConfigError(f"duplicate telegram_user_id {duplicate}")

        self._runtimes: list[AccountRuntime] = []
        for spec in specs:
            chunk_limiter = self._make_runtime_value(chunk_limiter_factory, spec)
            message_limiter = message_limiter_factory() if message_limiter_factory else None
            worker = worker_factory(
                api_id,
                api_hash,
                spec.telegram_user_id,
                spec.session_path,
                download_connections,
                upload_parts=upload_parts,
            )
            bind_limiter = getattr(worker, "set_upload_limiter", None)
            if chunk_limiter is not None and callable(bind_limiter):
                bind_limiter(chunk_limiter)
            self._runtimes.append(
                AccountRuntime(
                    spec=spec,
                    worker=worker,
                    file_slots=threading.BoundedSemaphore(upload_files),
                    chunk_limiter=chunk_limiter,
                    message_limiter=message_limiter,
                )
            )
        self._by_id = {
            runtime.spec.telegram_user_id: runtime for runtime in self._runtimes
        }
        self._rr = 0
        self._lock = threading.RLock()
        self.speed_tracker = speed_tracker or UploadSpeedTracker()
        self.activity = activity_registry or AccountActivityRegistry(
            speed_tracker=self.speed_tracker
        )
        for runtime in self._runtimes:
            self.activity.add_account(runtime.spec.telegram_user_id)
        self._work_sequence = 0
        self._started = False

    @staticmethod
    def _make_runtime_value(factory, spec: AccountSpec):
        if factory is None:
            return None
        try:
            inspect.signature(factory).bind(spec)
        except (TypeError, ValueError):
            return factory()
        return factory(spec)

    @staticmethod
    def _application_root() -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return HERE.resolve()

    @classmethod
    def from_config(
        cls,
        cfg,
        *,
        worker_factory: Callable[..., TelegramWorker] = TelegramWorker,
        chunk_limiter_factory: Optional[Callable[..., object]] = None,
        message_limiter_factory: Optional[Callable[[], object]] = None,
    ) -> "TelegramAccountPool":
        specs = discover_account_specs(
            cfg.session_dir, cfg.primary_user_id, cls._application_root()
        )
        if chunk_limiter_factory is None:
            from upload_limiter import AdaptiveUploadLimiter

            def chunk_limiter_factory(spec: AccountSpec):
                return AdaptiveUploadLimiter(
                    max_window=cfg.upload_parts,
                    account_id=spec.telegram_user_id,
                    cache_dir=getattr(cfg, "cache_dir", None),
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
            raise AccountUnavailableError(
                f"Telegram account {telegram_user_id} is not configured"
            )
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
            raise AccountUnavailableError(
                f"primary account authentication failed: {detail}"
            ) from None

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
                raise SessionClientError(
                    f"Telegram session for account {runtime.spec.telegram_user_id} returned no user ID"
                )
            actual = int(actual)
            expected = runtime.spec.telegram_user_id
            if actual != expected:
                raise SessionIdentityError(
                    f"Telegram session user ID mismatch: expected {expected}, got {actual}"
                )
            other = self._by_id.get(actual)
            if other is not None and other is not runtime:
                raise SessionIdentityError(
                    f"Telegram session user ID {actual} conflicts with another account"
                )
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
                except Exception as exc:
                    log.warning(
                        "Telegram account shutdown failed: %s",
                        self._safe_detail(runtime, exc),
                    )
                runtime._started = False
            runtime.online = False
            runtime.linked = False
        self._started = False

    def for_read(self, telegram_user_id: int) -> AccountRuntime:
        runtime = self.runtime(telegram_user_id)
        if not runtime.online:
            raise AccountUnavailableError(self._unavailable_message(runtime))
        return runtime

    def _next_work_id_locked(self) -> str:
        self._work_sequence += 1
        return f"upload:{self._work_sequence}"

    def _release_upload_locked(self, runtime: AccountRuntime, work_id: str) -> ActivityChange:
        change = self.activity.end_job(runtime.telegram_user_id, work_id)
        runtime.file_slots.release()
        return change

    def _try_acquire_exact_locked(self, runtime: AccountRuntime, work_id: str) -> Optional[UploadLease]:
        if not runtime.online or not runtime.linked:
            return None
        activity = self.activity.snapshot(runtime.telegram_user_id)
        if activity.reserved_task_id is not None:
            return None
        if not runtime.file_slots.acquire(blocking=False):
            return None
        change = self.activity.begin_job(runtime.telegram_user_id, work_id)
        if not change.changed:
            runtime.file_slots.release()
            return None
        return UploadLease(self, runtime, work_id)

    def acquire_exact_upload_lease(self, account_id: int, work_id: str) -> Optional[UploadLease]:
        with self._lock:
            try:
                runtime = self.runtime(account_id)
            except AccountUnavailableError:
                return None
            return self._try_acquire_exact_locked(runtime, work_id)

    def acquire_upload_lease(
        self, work_id: Optional[str] = None, timeout: Optional[float] = None
    ) -> UploadLease:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative or None")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                if work_id is None:
                    work_id = self._next_work_id_locked()
                count = len(self._runtimes)
                order = [(self._rr + offset) % count for offset in range(count)]
                for index in order:
                    runtime = self._runtimes[index]
                    lease = self._try_acquire_exact_locked(runtime, work_id)
                    if lease is not None:
                        self._rr = (index + 1) % count
                        return lease
                any_eligible = any(r.online and r.linked for r in self._runtimes)
            if not any_eligible:
                raise AccountUnavailableError(
                    "no online linked Telegram account is available for upload"
                )
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AccountUnavailableError(
                        "timed out waiting for a Telegram upload file slot"
                    )
                time.sleep(min(0.01, remaining))
            else:
                time.sleep(0.01)

    @contextmanager
    def acquire_upload(
        self, timeout: Optional[float] = None, *, work_id: Optional[str] = None
    ) -> Iterator[AccountRuntime]:
        lease = self.acquire_upload_lease(work_id=work_id, timeout=timeout)
        try:
            yield lease.runtime
        finally:
            lease.close()

    def try_reserve_idle(self, account_id: int, task_id: str) -> bool:
        with self._lock:
            try:
                runtime = self.runtime(account_id)
            except AccountUnavailableError:
                return False
            if not runtime.online or not runtime.linked:
                return False
            snapshot = self.activity.snapshot(account_id)
            if not snapshot.idle or snapshot.idle_snapshot is None:
                return False
            return self.activity.reserve_if_idle(account_id, task_id)

    def activate_reservation(self, account_id: int, task_id: str) -> UploadLease:
        with self._lock:
            runtime = self.runtime(account_id)
            if not runtime.online or not runtime.linked:
                raise AccountUnavailableError(
                    f"Telegram account {account_id} is not upload eligible"
                )
            snapshot = self.activity.snapshot(account_id)
            if snapshot.reserved_task_id != task_id:
                raise AccountUnavailableError(
                    f"Telegram account {account_id} is not reserved for this upload"
                )
            if not runtime.file_slots.acquire(blocking=False):
                raise AccountUnavailableError(
                    f"Telegram account {account_id} has no upload file slot"
                )
            change = self.activity.activate_reservation(account_id, task_id, task_id)
            if not change.changed:
                runtime.file_slots.release()
                raise AccountUnavailableError(
                    f"Telegram account {account_id} reservation changed"
                )
            return UploadLease(self, runtime, task_id)

    def release_reservation(self, account_id: int, task_id: str) -> ActivityChange:
        with self._lock:
            return self.activity.release_reservation(account_id, task_id)

    def status(self) -> dict:
        accounts = []
        for runtime in self._runtimes:
            account_id = runtime.spec.telegram_user_id
            activity = self.activity.snapshot(account_id)
            idle_speed = (
                None if activity.idle_snapshot is None
                else activity.idle_snapshot.bytes_per_second
            )
            accounts.append({
                "telegram_user_id": account_id,
                "primary": runtime is self.primary,
                "online": runtime.online,
                "linked": runtime.linked,
                "error": runtime.error,
                "limiter": self._limiter_status(runtime.chunk_limiter),
                "idle": activity.idle,
                "active_byte_upload_jobs": activity.active_byte_upload_jobs,
                "in_flight_upload_rpcs": activity.in_flight_upload_rpcs,
                "reserved_task_id": activity.reserved_task_id,
                "idle_speed_bytes_per_second": idle_speed,
            })
        return {
            "accounts": accounts,
            "eligible_upload_ids": list(self.eligible_upload_ids),
        }

    @staticmethod
    def _limiter_status(limiter: Optional[object]) -> dict:
        stats = getattr(limiter, "stats", None)
        return dict(stats()) if callable(stats) else {}

    @staticmethod
    def _safe_detail(runtime: AccountRuntime, exc: BaseException) -> str:
        if isinstance(exc, (SessionClientError, SessionIdentityError)):
            detail = str(exc)
        else:
            detail = type(exc).__name__
        return f"account {runtime.spec.telegram_user_id}: {detail}"

    @staticmethod
    def _unavailable_message(runtime: AccountRuntime) -> str:
        return runtime.error or f"account {runtime.spec.telegram_user_id} is offline"
