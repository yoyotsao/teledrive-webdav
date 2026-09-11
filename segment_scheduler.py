"""Lease-safe central scheduler for large Telegram upload segments."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from transfer_models import AttemptLease, UploadedPart, UploadRpcToken


@dataclass(frozen=True)
class SegmentDescriptor:
    index: int
    offset: int
    size: int


class SegmentState(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    MIGRATING = "migrating"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class SchedulerAction:
    task_id: str
    descriptor: SegmentDescriptor
    account_id: int
    attempt_id: int
    migrated: bool

    @property
    def lease(self) -> AttemptLease:
        return AttemptLease(self.task_id, self.attempt_id, self.account_id)


@dataclass(frozen=True)
class MigrationCommit:
    task_id: str
    old_attempt_id: int
    new_attempt_id: int
    from_account_id: int
    to_account_id: int
    score: float
    abandoned_logical_bytes: int
    target_snapshot_speed: float
    target_snapshot_age: float
    current_speed: float
    remaining_ratio: float


@dataclass
class SegmentTask:
    task_id: str
    file_job_id: str
    index: int
    offset: int
    size: int
    state: SegmentState = SegmentState.PENDING
    attempt_id: int = 0
    current_account_id: Optional[int] = None
    migration_count: int = 0
    attempted_account_ids: set[int] = field(default_factory=set)
    attempt_started_at: Optional[float] = None
    draining_attempt_id: Optional[int] = None
    logical_uploaded_bytes: int = 0
    completed_part_indices: set[int] = field(default_factory=set)
    attempt_in_flight_rpcs: dict[int, set[UploadRpcToken]] = field(default_factory=dict)
    last_premium_flood_at: Optional[float] = None
    qualification_deadline: Optional[float] = None
    reserved_account_id: Optional[int] = None
    active_upload_lease: Optional[object] = field(default=None, repr=False)
    active_upload_attempt_id: Optional[int] = None
    executor_quiescent: set[int] = field(default_factory=set, repr=False)
    revoke_handle: Optional[object] = field(default=None, repr=False)
    result: Optional[UploadedPart] = None
    error_category: Optional[str] = None

    @property
    def descriptor(self) -> SegmentDescriptor:
        return SegmentDescriptor(self.index, self.offset, self.size)


class SchedulerExecutionError(RuntimeError):
    pass


class SegmentScheduler:
    CANDIDATE_MIN_AGE = 30.0
    PREMIUM_RECENCY = 30.0
    SCORE_THRESHOLD = 2.0

    def __init__(
        self,
        file_job_id: str,
        descriptors,
        *,
        pool,
        activity,
        speed_tracker,
        clock: Callable[[], float] = time.monotonic,
        diagnostic_sink: Optional[Callable[[str, dict], None]] = None,
    ) -> None:
        self.file_job_id = str(file_job_id)
        self.pool = pool
        self.activity = activity
        self.speed_tracker = speed_tracker
        self._clock = clock
        self._diagnostic_sink = diagnostic_sink
        self._condition = threading.Condition(threading.RLock())
        self._tasks: dict[str, SegmentTask] = {}
        self._version = 0
        self._sequence = 0
        self._claimed_attempts: set[tuple[str, int]] = set()
        self._executor_completions = deque()
        self._scheduler_failure: Optional[str] = None
        self._physical_transferred_bytes = 0
        self._abandoned_logical_bytes = 0
        self._migration_count = 0
        for descriptor in descriptors:
            self.register_segment(descriptor)

    @property
    def version(self) -> int:
        with self._condition:
            return self._version

    def _bump_locked(self) -> None:
        self._version += 1
        self._condition.notify_all()

    def register_segment(self, descriptor: SegmentDescriptor, *, task_id: Optional[str] = None) -> str:
        with self._condition:
            task_id = task_id or f"{self.file_job_id}:{descriptor.index}"
            if task_id in self._tasks:
                raise ValueError(f"duplicate segment task {task_id}")
            self._tasks[task_id] = SegmentTask(
                task_id=task_id,
                file_job_id=self.file_job_id,
                index=descriptor.index,
                offset=descriptor.offset,
                size=descriptor.size,
            )
            self._bump_locked()
            return task_id

    def task(self, task_id: str) -> SegmentTask:
        with self._condition:
            return self._tasks[task_id]

    def _runtime_eligible_locked(self, account_id: int) -> bool:
        try:
            runtime = self.pool.runtime(account_id)
        except Exception:
            return False
        return bool(getattr(runtime, "online", False) and getattr(runtime, "linked", False))

    def _activity_snapshot_locked(self, account_id: int):
        try:
            return self.activity.snapshot(account_id)
        except Exception:
            return None

    def _valid_current(self, lease: AttemptLease, allowed_states) -> Optional[SegmentTask]:
        task = self._tasks.get(lease.task_id)
        if task is None:
            return None
        if task.state in (SegmentState.COMPLETED, SegmentState.FAILED):
            return None
        if task.attempt_id != lease.attempt_id:
            return None
        if task.current_account_id != lease.account_id:
            return None
        if task.state not in allowed_states:
            return None
        return task

    def _install_active_locked(self, task: SegmentTask, owned_lease, *, attempt_id: int, migrated: bool) -> SchedulerAction:
        account_id = int(owned_lease.runtime.telegram_user_id)
        task.state = SegmentState.ACTIVE
        task.attempt_id = attempt_id
        task.current_account_id = account_id
        task.attempt_started_at = self._clock()
        task.attempted_account_ids.add(account_id)
        task.active_upload_lease = owned_lease
        task.active_upload_attempt_id = attempt_id
        task.revoke_handle = None
        task.qualification_deadline = None
        task.last_premium_flood_at = None
        task.attempt_in_flight_rpcs.setdefault(attempt_id, set())
        self._claimed_attempts.add((task.task_id, attempt_id))
        self._bump_locked()
        return SchedulerAction(task.task_id, task.descriptor, account_id, attempt_id, migrated)

    def activate(self, task_id: str, account_id: int) -> AttemptLease:
        """Deterministically activate one pending task on an exact account."""
        with self._condition:
            task = self._tasks[task_id]
            if task.state is not SegmentState.PENDING:
                raise RuntimeError("task is not pending")
            owned = self.pool.acquire_exact_upload_lease(account_id, task_id)
            if owned is None:
                raise RuntimeError("account is not available")
            action = self._install_active_locked(task, owned, attempt_id=1, migrated=False)
            return action.lease

    def bind_revoke_handle(self, lease: AttemptLease, handle) -> bool:
        with self._condition:
            task = self._valid_current(lease, {SegmentState.ACTIVE})
            if task is None:
                return False
            task.revoke_handle = handle
            self._bump_locked()
            return True

    def borrow_runtime(self, lease: AttemptLease):
        """Return the runtime behind the scheduler-owned lease without transferring ownership."""
        with self._condition:
            task = self._valid_current(lease, {SegmentState.ACTIVE})
            if task is None:
                return None
            owned = task.active_upload_lease
            if owned is None or task.active_upload_attempt_id != lease.attempt_id:
                return None
            if int(owned.runtime.telegram_user_id) != lease.account_id:
                return None
            return owned.runtime

    def begin_request(self, lease: AttemptLease, part_index: int) -> UploadRpcToken:
        with self._condition:
            task = self._valid_current(lease, {SegmentState.ACTIVE})
            if task is None:
                raise RuntimeError("upload attempt is no longer current")
            self._sequence += 1
            token = UploadRpcToken(
                lease.task_id, lease.attempt_id, lease.account_id, int(part_index), self._sequence
            )
            task.attempt_in_flight_rpcs.setdefault(lease.attempt_id, set()).add(token)
            self.activity.request_started(token)
            self._bump_locked()
            return token

    def request_settled(self, token: UploadRpcToken) -> bool:
        with self._condition:
            task = self._tasks.get(token.task_id)
            changed = False
            if task is not None:
                tokens = task.attempt_in_flight_rpcs.get(token.attempt_id)
                if tokens is not None and token in tokens:
                    tokens.remove(token)
                    changed = True
            activity_change = self.activity.request_settled(token)
            changed = changed or activity_change.changed
            if task is not None:
                self._maybe_close_owned_locked(task, token.attempt_id)
            if changed:
                self._bump_locked()
            return changed

    def physical_success(self, token: UploadRpcToken, nbytes: int) -> bool:
        changed = self.speed_tracker.record_physical(token, nbytes)
        if changed:
            with self._condition:
                self._physical_transferred_bytes += int(nbytes)
                self._bump_locked()
        return changed

    def part_succeeded(self, lease: AttemptLease, part_index: int, nbytes: int) -> bool:
        with self._condition:
            task = self._valid_current(lease, {SegmentState.ACTIVE})
            if task is None or part_index in task.completed_part_indices:
                return False
            task.completed_part_indices.add(part_index)
            task.logical_uploaded_bytes = min(task.size, task.logical_uploaded_bytes + int(nbytes))
            self.speed_tracker.record_effective(lease, part_index, nbytes)
            self._bump_locked()
            return True

    def notify_premium_flood(self, lease: AttemptLease, seconds: float, pacer_snapshot):
        with self._condition:
            task = self._valid_current(lease, {SegmentState.ACTIVE})
            if task is None:
                return None
            now = self._clock()
            task.last_premium_flood_at = now
            if task.attempt_started_at is not None:
                task.qualification_deadline = max(now, task.attempt_started_at + self.CANDIDATE_MIN_AGE)
            cycle = self.speed_tracker.close_premium_flood_cycle(lease, seconds)
            self._bump_locked()
        if self._diagnostic_sink is not None:
            self._diagnostic_sink("premium_flood", {"cycle": cycle, "pacer": pacer_snapshot})
        return cycle

    def _candidate_locked(self, task: SegmentTask, now: float) -> bool:
        if task.state is not SegmentState.ACTIVE:
            return False
        if task.migration_count != 0:
            return False
        if task.logical_uploaded_bytes >= task.size:
            return False
        if task.attempt_started_at is None or now - task.attempt_started_at < self.CANDIDATE_MIN_AGE:
            return False
        if task.last_premium_flood_at is None or now - task.last_premium_flood_at > self.PREMIUM_RECENCY:
            return False
        return True

    def _score_locked(self, target_account_id: int, task: SegmentTask) -> Optional[float]:
        now = self._clock()
        if not self._candidate_locked(task, now):
            return None
        if target_account_id in task.attempted_account_ids:
            return None
        if not self._runtime_eligible_locked(target_account_id):
            return None
        snap = self._activity_snapshot_locked(target_account_id)
        if snap is None or not snap.idle or snap.idle_snapshot is None:
            return None
        if now >= snap.idle_snapshot.expires_at:
            return None
        current_lease = AttemptLease(task.task_id, task.attempt_id, task.current_account_id)
        current_speed = self.speed_tracker.live_speed(current_lease)
        remaining_ratio = (task.size - task.logical_uploaded_bytes) / task.size
        if current_speed == 0:
            return math.inf
        return (snap.idle_snapshot.bytes_per_second / current_speed) * remaining_ratio

    def score(self, target_account_id: int, task_id: str) -> Optional[float]:
        with self._condition:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            return self._score_locked(target_account_id, task)

    def commit_migration(self, target_account_id: int, task_id: str) -> Optional[MigrationCommit]:
        revoke = None
        with self._condition:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            score = self._score_locked(target_account_id, task)
            if score is None or score <= self.SCORE_THRESHOLD:
                return None
            now = self._clock()
            target_activity = self._activity_snapshot_locked(target_account_id)
            target_snapshot = None if target_activity is None else target_activity.idle_snapshot
            if target_snapshot is None:
                return None
            current_lease = AttemptLease(task.task_id, task.attempt_id, task.current_account_id)
            current_speed = self.speed_tracker.live_speed(current_lease)
            remaining_ratio = (task.size - task.logical_uploaded_bytes) / task.size
            target_snapshot_speed = float(target_snapshot.bytes_per_second)
            target_snapshot_age = max(0.0, now - target_snapshot.created_at)
            if not self.pool.try_reserve_idle(target_account_id, task_id):
                return None
            old_attempt = task.attempt_id
            old_account = task.current_account_id
            abandoned = task.logical_uploaded_bytes
            task.state = SegmentState.MIGRATING
            task.attempt_id += 1
            task.draining_attempt_id = old_attempt
            task.current_account_id = None
            task.logical_uploaded_bytes = 0
            task.completed_part_indices.clear()
            task.migration_count = 1
            self._migration_count += 1
            self._abandoned_logical_bytes += abandoned
            task.attempted_account_ids.add(target_account_id)
            task.reserved_account_id = target_account_id
            task.qualification_deadline = None
            revoke = task.revoke_handle
            task.revoke_handle = None
            commit = MigrationCommit(
                task.task_id, old_attempt, task.attempt_id, int(old_account),
                int(target_account_id), float(score), abandoned,
                target_snapshot_speed, target_snapshot_age, current_speed, remaining_ratio,
            )
            self._bump_locked()
        if revoke is not None:
            revoke.revoke()
        if self._diagnostic_sink is not None:
            self._diagnostic_sink("migration", {"commit": commit})
        return commit

    def drained(self, task_id: str, attempt_id: int) -> bool:
        with self._condition:
            task = self._tasks[task_id]
            return not task.attempt_in_flight_rpcs.get(attempt_id)

    def close_active_upload_lease(self, lease: AttemptLease) -> bool:
        with self._condition:
            task = self._tasks.get(lease.task_id)
            if task is None:
                return False
            owned = task.active_upload_lease
            if owned is None or task.active_upload_attempt_id != lease.attempt_id:
                return False
            if int(owned.runtime.telegram_user_id) != lease.account_id:
                return False
            task.active_upload_lease = None
            task.active_upload_attempt_id = None
            owned.close()
            self._bump_locked()
            return True

    def _maybe_close_owned_locked(self, task: SegmentTask, attempt_id: int) -> bool:
        if task.active_upload_attempt_id != attempt_id or task.active_upload_lease is None:
            return False
        tokens = task.attempt_in_flight_rpcs.get(attempt_id, set())
        if tokens:
            return False
        should_close = False
        if attempt_id in task.executor_quiescent:
            should_close = task.state in (SegmentState.MIGRATING, SegmentState.COMPLETED, SegmentState.FAILED)
            if task.draining_attempt_id == attempt_id:
                should_close = True
        if should_close:
            owned = task.active_upload_lease
            task.active_upload_lease = None
            task.active_upload_attempt_id = None
            owned.close()
            return True
        return False

    def attempt_quiesced(self, lease: AttemptLease) -> bool:
        with self._condition:
            task = self._tasks.get(lease.task_id)
            if task is None:
                return False
            before = lease.attempt_id in task.executor_quiescent
            task.executor_quiescent.add(lease.attempt_id)
            closed = self._maybe_close_owned_locked(task, lease.attempt_id)
            changed = not before or closed
            if changed:
                self._bump_locked()
            return changed

    def bytes_prepared(self, lease: AttemptLease) -> bool:
        with self._condition:
            task = self._valid_current(lease, {SegmentState.ACTIVE})
            if task is None:
                return False
            if task.logical_uploaded_bytes < task.size:
                return False
            if task.attempt_in_flight_rpcs.get(lease.attempt_id):
                return False
            owned = task.active_upload_lease
            if owned is None or task.active_upload_attempt_id != lease.attempt_id:
                return False
            task.active_upload_lease = None
            task.active_upload_attempt_id = None
            owned.close()
            self._bump_locked()
            return True

    def grant_finalize(self, lease: AttemptLease) -> bool:
        with self._condition:
            task = self._valid_current(lease, {SegmentState.ACTIVE})
            if task is None:
                return False
            task.state = SegmentState.FINALIZING
            task.qualification_deadline = None
            self._bump_locked()
            return True

    def _terminal_locked(self, task: SegmentTask, state: SegmentState, error_category: Optional[str] = None) -> bool:
        if task.state in (SegmentState.COMPLETED, SegmentState.FAILED):
            return False
        task.state = state
        task.current_account_id = None
        task.qualification_deadline = None
        task.error_category = error_category
        revoke = task.revoke_handle
        task.revoke_handle = None
        if task.reserved_account_id is not None:
            self.pool.release_reservation(task.reserved_account_id, task.task_id)
            task.reserved_account_id = None
        if revoke is not None:
            revoke.revoke()
        self._maybe_close_owned_locked(task, task.active_upload_attempt_id or -1)
        self._bump_locked()
        return True

    def fail_attempt(self, lease: AttemptLease, error_category: str) -> bool:
        with self._condition:
            task = self._valid_current(lease, {SegmentState.ACTIVE, SegmentState.FINALIZING})
            if task is None:
                return False
            return self._terminal_locked(task, SegmentState.FAILED, error_category)

    def fail_task(self, task_id: str, error_category: str) -> bool:
        with self._condition:
            task = self._tasks[task_id]
            return self._terminal_locked(task, SegmentState.FAILED, error_category)

    def complete_attempt(self, lease: AttemptLease, result: UploadedPart) -> bool:
        with self._condition:
            task = self._valid_current(lease, {SegmentState.FINALIZING})
            if task is None:
                return False
            task.result = result
            return self._terminal_locked(task, SegmentState.COMPLETED)

    def _replacement_ready_locked(self, task: SegmentTask) -> bool:
        if task.state is not SegmentState.MIGRATING:
            return False
        old = task.draining_attempt_id
        if old is None:
            return False
        if task.attempt_in_flight_rpcs.get(old):
            return False
        if old not in task.executor_quiescent:
            return False
        if task.active_upload_attempt_id == old and task.active_upload_lease is not None:
            self._maybe_close_owned_locked(task, old)
        return task.active_upload_lease is None and task.reserved_account_id is not None

    def _activate_reserved_replacement_locked(self, task: SegmentTask) -> Optional[SchedulerAction]:
        if not self._replacement_ready_locked(task):
            return None
        target = task.reserved_account_id
        try:
            owned = self.pool.activate_reservation(target, task.task_id)
        except Exception:
            self.pool.release_reservation(target, task.task_id)
            task.reserved_account_id = None
            self._terminal_locked(task, SegmentState.FAILED, "reservation activation failed")
            return None
        task.reserved_account_id = None
        task.draining_attempt_id = None
        return self._install_active_locked(task, owned, attempt_id=task.attempt_id, migrated=True)

    def select_next_action(self) -> Optional[SchedulerAction]:
        with self._condition:
            if self._scheduler_failure is not None:
                return None
            # First finish an already committed migration after its old attempt drains.
            for task in sorted(self._tasks.values(), key=lambda t: t.index):
                action = self._activate_reserved_replacement_locked(task)
                if action is not None:
                    return action

            # Migration selection has priority over normal pending work.
            best = None
            for account_id in getattr(self.pool, "eligible_upload_ids", ()):
                snap = self._activity_snapshot_locked(account_id)
                if snap is None or not snap.idle or snap.idle_snapshot is None:
                    continue
                for task in self._tasks.values():
                    score = self._score_locked(account_id, task)
                    if score is None or score <= self.SCORE_THRESHOLD:
                        continue
                    candidate = (score, -task.index, account_id, task.task_id)
                    if best is None or candidate > best:
                        best = candidate
            if best is not None:
                _, _, account_id, task_id = best
                self.commit_migration(account_id, task_id)
                return None

            for task in sorted(self._tasks.values(), key=lambda t: t.index):
                if task.state is not SegmentState.PENDING:
                    continue
                try:
                    owned = self.pool.acquire_upload_lease(task.task_id, timeout=0)
                except Exception:
                    owned = None
                if owned is None:
                    continue
                return self._install_active_locked(task, owned, attempt_id=1, migrated=False)
            return None

    def next_deadline(self) -> Optional[float]:
        with self._condition:
            now = self._clock()
            deadlines = []
            for task in self._tasks.values():
                if task.state is SegmentState.ACTIVE and task.migration_count == 0:
                    if task.last_premium_flood_at is not None:
                        age = (now if task.attempt_started_at is None else task.attempt_started_at) + self.CANDIDATE_MIN_AGE
                        expiry = task.last_premium_flood_at + self.PREMIUM_RECENCY
                        if age > now:
                            deadlines.append(age)
                        if expiry > now:
                            deadlines.append(expiry)
                        elif expiry == now and self._candidate_locked(task, now):
                            deadlines.append(math.nextafter(expiry, math.inf))
            for account_id in getattr(self.pool, "eligible_upload_ids", ()):
                snap = self._activity_snapshot_locked(account_id)
                idle_snapshot = None if snap is None else snap.idle_snapshot
                if idle_snapshot is not None and idle_snapshot.expires_at > now:
                    deadlines.append(idle_snapshot.expires_at)
            return min(deadlines) if deadlines else None

    def wait_for_change(self, observed_version: int, deadline: Optional[float]) -> int:
        with self._condition:
            if self._version != observed_version:
                return self._version
            timeout = None if deadline is None else max(0.0, deadline - self._clock())
            self._condition.wait(timeout=timeout)
            return self._version

    def notify_account_changed(self, _account_id: Optional[int] = None) -> None:
        with self._condition:
            self._bump_locked()

    def notify_speed_changed(self, _account_id: Optional[int] = None) -> None:
        with self._condition:
            self._bump_locked()

    def submission_failed(self, action: SchedulerAction, error_category: str) -> None:
        with self._condition:
            task = self._tasks.get(action.task_id)
            if task is not None and task.attempt_id == action.attempt_id:
                lease = action.lease
                self._terminal_locked(task, SegmentState.FAILED, error_category)
                task.executor_quiescent.add(lease.attempt_id)
                self._maybe_close_owned_locked(task, lease.attempt_id)

    def enqueue_executor_completion(self, future, action: SchedulerAction, error_category: Optional[str]) -> None:
        with self._condition:
            self._executor_completions.append((future, action, error_category))
            self._bump_locked()

    def take_executor_completions(self):
        with self._condition:
            items = list(self._executor_completions)
            self._executor_completions.clear()
            return items

    def executor_finished(self, action: SchedulerAction, error_category: Optional[str]) -> None:
        with self._condition:
            task = self._tasks.get(action.task_id)
            if task is None:
                return
            task.executor_quiescent.add(action.attempt_id)
            self._maybe_close_owned_locked(task, action.attempt_id)
            if error_category is not None:
                self._scheduler_failure = error_category
                if task.state not in (SegmentState.COMPLETED, SegmentState.FAILED):
                    self._terminal_locked(task, SegmentState.FAILED, error_category)
                for other in self._tasks.values():
                    if other.state not in (SegmentState.COMPLETED, SegmentState.FAILED):
                        self._terminal_locked(other, SegmentState.FAILED, error_category)
            elif task.state is SegmentState.ACTIVE and task.attempt_id == action.attempt_id:
                self._scheduler_failure = "executor returned without task outcome"
                self._terminal_locked(task, SegmentState.FAILED, self._scheduler_failure)
            self._bump_locked()

    def all_terminal(self) -> bool:
        with self._condition:
            return all(t.state in (SegmentState.COMPLETED, SegmentState.FAILED) for t in self._tasks.values())

    def completed_results_by_index(self) -> list[UploadedPart]:
        with self._condition:
            if self._scheduler_failure is not None:
                raise SchedulerExecutionError(self._scheduler_failure)
            failed = [t for t in self._tasks.values() if t.state is SegmentState.FAILED]
            if failed:
                raise SchedulerExecutionError(failed[0].error_category or "segment upload failed")
            if not all(t.state is SegmentState.COMPLETED and t.result is not None for t in self._tasks.values()):
                raise SchedulerExecutionError("segment scheduler is not complete")
            return [t.result for t in sorted(self._tasks.values(), key=lambda item: item.index)]

    def metrics_snapshot(self) -> dict:
        with self._condition:
            logical = sum(task.logical_uploaded_bytes for task in self._tasks.values())
            physical = self._physical_transferred_bytes
            return {
                "logical_uploaded_bytes": logical,
                "physical_transferred_bytes": physical,
                "migration_overhead_bytes": max(0, physical - logical),
                "migration_count": self._migration_count,
                "abandoned_logical_bytes": self._abandoned_logical_bytes,
            }

    def status(self) -> list[dict]:
        with self._condition:
            return [
                {
                    "task_id": task.task_id,
                    "segment_index": task.index,
                    "state": task.state.value,
                    "attempt_id": task.attempt_id,
                    "account_id": task.current_account_id,
                    "migration_count": task.migration_count,
                    "logical_uploaded_bytes": task.logical_uploaded_bytes,
                    "detail": (
                        "重新分派上傳帳號，該區段將從頭重傳"
                        if task.migration_count else ""
                    ),
                }
                for task in sorted(self._tasks.values(), key=lambda item: item.index)
            ]
