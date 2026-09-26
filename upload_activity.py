"""Thread-safe byte-upload activity and speed accounting."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Condition, RLock
from typing import Callable, Deque, Optional

from transfer_models import AttemptLease, UploadRpcToken


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


@dataclass(frozen=True)
class AccountActivitySnapshot:
    account_id: int
    active_byte_upload_jobs: int
    in_flight_upload_rpcs: int
    reserved_task_id: Optional[str]
    idle_snapshot: Optional[IdleSpeedSnapshot]

    @property
    def idle(self) -> bool:
        return (
            self.active_byte_upload_jobs == 0
            and self.in_flight_upload_rpcs == 0
            and self.reserved_task_id is None
        )


class UploadSpeedTracker:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        window: float = 30.0,
        snapshot_ttl: float = 300.0,
    ) -> None:
        self._clock = clock
        self.window = float(window)
        self.snapshot_ttl = float(snapshot_ttl)
        self._lock = RLock()
        self._effective: dict[int, Deque[tuple[float, int]]] = defaultdict(deque)
        self._attempt_effective: dict[AttemptLease, Deque[tuple[float, int]]] = defaultdict(deque)
        self._physical: dict[int, Deque[tuple[float, int]]] = defaultdict(deque)
        self._effective_parts: set[tuple[str, int, int]] = set()
        self._physical_rpc_tokens: set[UploadRpcToken] = set()
        self._account_cycles: dict[int, MutableTotals] = defaultdict(MutableTotals)
        self._attempt_cycles: dict[AttemptLease, MutableTotals] = defaultdict(MutableTotals)
        self._physical_total_bytes: dict[int, int] = defaultdict(int)

    def _prune(self, samples: Deque[tuple[float, int]], now: Optional[float] = None) -> None:
        if now is None:
            now = self._clock()
        cutoff = now - self.window
        while samples and samples[0][0] < cutoff:
            samples.popleft()

    def _window_bytes(self, samples: Deque[tuple[float, int]]) -> int:
        now = self._clock()
        self._prune(samples, now)
        return sum(nbytes for _, nbytes in samples)

    def record_effective(self, lease: AttemptLease, part_index: int, nbytes: int) -> bool:
        key = (lease.task_id, lease.attempt_id, part_index)
        with self._lock:
            if key in self._effective_parts:
                return False
            self._effective_parts.add(key)
            now = self._clock()
            self._effective[lease.account_id].append((now, int(nbytes)))
            self._attempt_effective[lease].append((now, int(nbytes)))
            return True

    def live_speed(self, lease: AttemptLease) -> float:
        with self._lock:
            return self._window_bytes(self._attempt_effective[lease]) / self.window

    def account_live_speed(self, account_id: int) -> float:
        with self._lock:
            return self._window_bytes(self._effective[account_id]) / self.window

    def freeze_idle_snapshot(self, account_id: int) -> Optional[IdleSpeedSnapshot]:
        with self._lock:
            now = self._clock()
            samples = self._effective[account_id]
            self._prune(samples, now)
            total = sum(nbytes for _, nbytes in samples)
            if total <= 0:
                return None
            return IdleSpeedSnapshot(total / self.window, now, now + self.snapshot_ttl)

    def snapshot_is_fresh(self, snapshot: Optional[IdleSpeedSnapshot]) -> bool:
        return snapshot is not None and self._clock() < snapshot.expires_at

    def record_physical(self, token: UploadRpcToken, nbytes: int) -> bool:
        with self._lock:
            if token in self._physical_rpc_tokens:
                return False
            self._physical_rpc_tokens.add(token)
            now = self._clock()
            nbytes = int(nbytes)
            self._physical[token.account_id].append((now, nbytes))
            self._physical_total_bytes[token.account_id] += nbytes
            for cycle in (self._account_cycles[token.account_id], self._attempt_cycles[token.lease]):
                cycle.parts += 1
                cycle.bytes += nbytes
            return True

    def physical_speed(self, account_id: int) -> float:
        with self._lock:
            return self._window_bytes(self._physical[account_id]) / self.window

    def physical_bytes(self, account_id: int) -> int:
        with self._lock:
            return self._physical_total_bytes[account_id]

    def close_premium_flood_cycle(
        self, lease: AttemptLease, wait_seconds: float
    ) -> FloodCycleSnapshot:
        with self._lock:
            account = self._account_cycles.pop(lease.account_id, MutableTotals())
            attempt = self._attempt_cycles.pop(lease, MutableTotals())
            return FloodCycleSnapshot(
                lease,
                float(wait_seconds),
                account.parts,
                account.bytes,
                attempt.parts,
                attempt.bytes,
            )


@dataclass
class _MutableAccountActivity:
    jobs: set[str]
    requests: set[UploadRpcToken]
    reserved_task_id: Optional[str] = None
    idle_snapshot: Optional[IdleSpeedSnapshot] = None


class AccountActivityRegistry:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        speed_tracker: Optional[UploadSpeedTracker] = None,
    ) -> None:
        self._clock = clock
        self.speed_tracker = speed_tracker
        self._condition = Condition(RLock())
        self._accounts: dict[int, _MutableAccountActivity] = {}

    @property
    def condition(self) -> Condition:
        return self._condition

    def add_account(self, account_id: int) -> None:
        with self._condition:
            self._accounts.setdefault(account_id, _MutableAccountActivity(set(), set()))

    def _account(self, account_id: int) -> _MutableAccountActivity:
        try:
            return self._accounts[account_id]
        except KeyError:
            raise KeyError(f"unknown Telegram account {account_id}") from None

    def _is_idle(self, account: _MutableAccountActivity) -> bool:
        return not account.jobs and not account.requests and account.reserved_task_id is None

    def _expire_snapshot_locked(self, account: _MutableAccountActivity) -> bool:
        snap = account.idle_snapshot
        if snap is not None and self._clock() >= snap.expires_at:
            account.idle_snapshot = None
            return True
        return False

    def _change_locked(
        self,
        account_id: int,
        *,
        before_idle: bool,
        changed: bool,
        invalidate_snapshot: bool = False,
    ) -> ActivityChange:
        account = self._account(account_id)
        snapshot_changed = False
        if invalidate_snapshot and account.idle_snapshot is not None:
            account.idle_snapshot = None
            snapshot_changed = True
        after_idle = self._is_idle(account)
        became_idle = changed and not before_idle and after_idle
        if became_idle and self.speed_tracker is not None:
            new_snapshot = self.speed_tracker.freeze_idle_snapshot(account_id)
            if new_snapshot != account.idle_snapshot:
                account.idle_snapshot = new_snapshot
                snapshot_changed = True
        if changed or snapshot_changed:
            self._condition.notify_all()
        return ActivityChange(account_id, changed, became_idle, snapshot_changed)

    def snapshot(self, account_id: int) -> AccountActivitySnapshot:
        with self._condition:
            account = self._account(account_id)
            if self._expire_snapshot_locked(account):
                self._condition.notify_all()
            return AccountActivitySnapshot(
                account_id,
                len(account.jobs),
                len(account.requests),
                account.reserved_task_id,
                account.idle_snapshot,
            )

    def begin_job(self, account_id: int, work_id: str) -> ActivityChange:
        with self._condition:
            account = self._account(account_id)
            before_idle = self._is_idle(account)
            previous = len(account.jobs)
            account.jobs.add(work_id)
            changed = len(account.jobs) != previous
            return self._change_locked(
                account_id, before_idle=before_idle, changed=changed, invalidate_snapshot=changed
            )

    def end_job(self, account_id: int, work_id: str) -> ActivityChange:
        with self._condition:
            account = self._account(account_id)
            before_idle = self._is_idle(account)
            changed = work_id in account.jobs
            account.jobs.discard(work_id)
            return self._change_locked(account_id, before_idle=before_idle, changed=changed)

    def request_started(self, token: UploadRpcToken) -> ActivityChange:
        with self._condition:
            account = self._account(token.account_id)
            before_idle = self._is_idle(account)
            previous = len(account.requests)
            account.requests.add(token)
            changed = len(account.requests) != previous
            return self._change_locked(
                token.account_id,
                before_idle=before_idle,
                changed=changed,
                invalidate_snapshot=changed,
            )

    def request_settled(self, token: UploadRpcToken) -> ActivityChange:
        with self._condition:
            account = self._account(token.account_id)
            before_idle = self._is_idle(account)
            changed = token in account.requests
            account.requests.discard(token)
            return self._change_locked(token.account_id, before_idle=before_idle, changed=changed)

    def reserve_if_idle(self, account_id: int, task_id: str) -> bool:
        with self._condition:
            account = self._account(account_id)
            self._expire_snapshot_locked(account)
            if not self._is_idle(account):
                return False
            before_idle = True
            account.reserved_task_id = task_id
            self._change_locked(
                account_id, before_idle=before_idle, changed=True, invalidate_snapshot=True
            )
            return True

    def release_reservation(self, account_id: int, task_id: str) -> ActivityChange:
        with self._condition:
            account = self._account(account_id)
            before_idle = self._is_idle(account)
            changed = account.reserved_task_id == task_id
            if changed:
                account.reserved_task_id = None
            return self._change_locked(account_id, before_idle=before_idle, changed=changed)

    def activate_reservation(self, account_id: int, task_id: str, work_id: str) -> ActivityChange:
        with self._condition:
            account = self._account(account_id)
            before_idle = self._is_idle(account)
            if account.reserved_task_id != task_id:
                return ActivityChange(account_id, False, False, False)
            account.reserved_task_id = None
            account.jobs.add(work_id)
            return self._change_locked(account_id, before_idle=before_idle, changed=True)
