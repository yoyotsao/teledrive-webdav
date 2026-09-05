"""Per-account Web upload pacing, ceiling learning, and message admission.

Transitions follow frontend/src/lib/adaptiveRateLimiter.ts, in seconds. Each
instance belongs to one account's event loop; AccountRuntime owns sharing.
Only rate and ceiling survive restart, as in the Web client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LimiterConfig:
    initial: float = 4.0
    minimum: float = 0.5
    maximum: float = 12.0
    burst: int = 2
    decrease_factor: float = 0.5
    increase_step: float = 0.5
    increase_interval: float = 10.0
    clean_window: float = 20.0
    first_backoff: float = 0.5
    backoff: float = 0.95
    ceiling_floor: float = 1.0
    slow_zone: float = 0.8
    slow_step: float = 0.1
    slow_interval: float = 30.0
    probe_cooldown: float = 300.0
    probe_cooldown_max: float = 1800.0
    probe_step: float = 0.2
    probe_confirm: float = 60.0
    escalation_count: int = 3
    escalation_window: float = 120.0
    escalated_decrease_factor: float = 0.3
    escalated_ceiling_factor: float = 0.9
    escalated_clean_window: float = 60.0
    escalation_reset: float = 600.0

    @classmethod
    def web_defaults(cls) -> LimiterConfig:
        return cls()


@dataclass(frozen=True)
class LimiterSnapshot:
    rate: float
    ceiling: Optional[float]
    floods: int
    window: int
    paused_until: float
    escalated: bool
    probe_started_at: Optional[float]
    probe_cooldown: float


def _finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _wait_seconds(seconds: Optional[float]) -> float:
    return float(seconds) if _finite_number(seconds) and seconds > 0 else 10.0


class _RateStore:
    """Best-effort versioned learning, replaced atomically in the metadata dir."""

    def __init__(self, cache_dir, account_id: int, wall_clock: Callable[[], float]):
        self.path = Path(cache_dir) / "meta" / f"upload-rate-{int(account_id)}.json"
        self._wall_clock = wall_clock

    def load(self, config: LimiterConfig):
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                return None
            rate, updated_at = payload.get("rate"), payload.get("updated_at")
            if not _finite_number(rate) or not _finite_number(updated_at):
                return None
            if self._wall_clock() - updated_at >= 86400:
                return None
            ceiling = payload.get("ceiling")
            if _finite_number(ceiling) and ceiling > 0:
                ceiling = min(config.maximum, max(config.ceiling_floor, ceiling))
            else:
                ceiling = None
            cap = ceiling if ceiling is not None else config.maximum
            rate = min(config.maximum, max(config.minimum, min(rate, cap) * 0.8))
            return rate, ceiling
        except (OSError, ValueError):
            return None

    def save(self, rate: float, ceiling: Optional[float]) -> None:
        partial = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=self.path.name + ".", suffix=".part", delete=False,
            ) as stream:
                partial = Path(stream.name)
                json.dump({"version": 1, "rate": rate, "ceiling": ceiling,
                           "updated_at": self._wall_clock()}, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(partial, self.path)
        except (OSError, ValueError):
            log.warning("Could not persist upload rate to %s", self.path)
        finally:
            if partial is not None:
                try:
                    partial.unlink(missing_ok=True)
                except OSError:
                    pass


class AdaptiveUploadLimiter:
    """Shared chunk admission plus RTT-independent adaptive rate control.

    ``acquire`` is held around one part RPC. ``slot`` and ``pace`` are exposed
    separately for compatibility with the existing part-buffering uploader.
    Persistence writes only when learning changes, never for every part.
    """

    def __init__(
        self, max_window: int = 12, *, account_id: int = 0, cache_dir=None,
        config: Optional[LimiterConfig] = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Optional[Callable[[float], Awaitable[None]]] = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config or LimiterConfig.web_defaults()
        self.account_id = int(account_id)
        self._clock = clock
        self._sleep = sleeper or asyncio.sleep
        self._window = max(1, int(max_window))
        self._slots = asyncio.Semaphore(self._window)
        self._rate = self.config.initial
        self._ceiling: Optional[float] = None
        self._next_slot_at = 0.0
        self._penalty_until = 0.0
        self._last_flood_at: Optional[float] = None
        self._last_increase_at = 0.0
        self._floods = 0
        self._flood_events: list[float] = []
        self._escalated_until = 0.0
        self._probe_started_at: Optional[float] = None
        self._probe_cooldown = self.config.probe_cooldown
        self._last_probe_ended_at = self.now()
        self._store = None if cache_dir is None else _RateStore(cache_dir, self.account_id, wall_clock)
        if self._store is not None:
            restored = self._store.load(self.config)
            if restored is not None:
                self._rate, self._ceiling = restored

    def now(self) -> float:
        return self._clock()

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await self._sleep(seconds)

    @asynccontextmanager
    async def slot(self):
        async with self._slots:
            yield

    @asynccontextmanager
    async def acquire(self):
        async with self.slot():
            await self.pace()
            yield

    async def pace(self) -> None:
        while True:
            now = self.now()
            interval = 1.0 / self._rate
            earliest = max(now, self._penalty_until)
            scheduled = max(self._next_slot_at, earliest - self.config.burst * interval)
            self._next_slot_at = scheduled + interval
            await self.sleep(scheduled - now)
            if self.now() >= self._penalty_until:
                return

    def _persist(self) -> None:
        if self._store is not None:
            self._store.save(self._rate, self._ceiling)

    def flood(self, seconds: Optional[float], *, premium: bool = False) -> None:
        now, cfg = self.now(), self.config
        wait = _wait_seconds(seconds)
        if not premium:
            self._floods += 1
            self._last_flood_at = now
            if now >= self._penalty_until:
                previous = self._rate
                self._flood_events.append(now)
                self._flood_events = [t for t in self._flood_events if now - t < cfg.escalation_window]
                escalate = len(self._flood_events) >= cfg.escalation_count
                if escalate:
                    self._escalated_until = now + cfg.escalation_reset
                ceiling = (
                    previous * cfg.first_backoff if self._ceiling is None
                    else min(previous * cfg.backoff, (self._ceiling + previous) / 2)
                )
                if escalate:
                    ceiling *= cfg.escalated_ceiling_factor
                self._ceiling = min(cfg.maximum, max(cfg.ceiling_floor, ceiling))
                if self._probe_started_at is not None:
                    self._rate = max(cfg.minimum, self._ceiling * cfg.slow_zone)
                    self._probe_cooldown = min(self._probe_cooldown * 2, cfg.probe_cooldown_max)
                    self._last_probe_ended_at = now
                else:
                    factor = cfg.escalated_decrease_factor if escalate else cfg.decrease_factor
                    self._rate = max(cfg.minimum, previous * factor)
                self._probe_started_at = None
                self._persist()
                log.warning("account %s FLOOD_WAIT #%d: wait=%.1fs rate=%.2f ceiling=%.2f",
                            self.account_id, self._floods, wait, self._rate, self._ceiling)
        # Web applies the same one-second guard to ordinary and premium waits.
        self._penalty_until = max(self._penalty_until, now + wait + 1.0)
        self._next_slot_at = max(self._next_slot_at, self._penalty_until)

    def success(self, duration: float) -> None:
        # duration is accepted for uploader compatibility; Web pacing ignores RTT.
        now, cfg = self.now(), self.config
        clean = cfg.escalated_clean_window if now < self._escalated_until else cfg.clean_window
        if self._last_flood_at is not None and now - self._last_flood_at < clean:
            return
        if self._rate >= cfg.maximum:
            return
        if self._ceiling is None:
            if now - self._last_increase_at < cfg.increase_interval:
                return
            self._last_increase_at = now
            self._rate = min(cfg.maximum, self._rate + cfg.increase_step)
        elif self._probe_started_at is not None and now - self._probe_started_at >= cfg.probe_confirm:
            self._ceiling = min(cfg.maximum, self._rate)
            self._probe_started_at = None
            self._last_probe_ended_at = now
            self._probe_cooldown = cfg.probe_cooldown
        elif self._rate < self._ceiling * cfg.slow_zone:
            if now - self._last_increase_at < cfg.increase_interval:
                return
            self._last_increase_at = now
            self._rate = min(self._ceiling * cfg.slow_zone, self._rate + cfg.increase_step)
        elif self._rate < self._ceiling:
            if now - self._last_increase_at < cfg.slow_interval:
                return
            self._last_increase_at = now
            self._rate = min(self._ceiling, self._rate + cfg.slow_step)
        else:
            if self._probe_started_at is not None:
                return
            if self._last_flood_at is not None and now - self._last_flood_at < self._probe_cooldown:
                return
            if now - self._last_probe_ended_at < self._probe_cooldown:
                return
            self._rate = min(cfg.maximum, self._rate + cfg.probe_step)
            self._probe_started_at = now
            self._last_increase_at = now
        self._persist()

    def snapshot(self) -> LimiterSnapshot:
        return LimiterSnapshot(
            self._rate, self._ceiling, self._floods, self._window, self._penalty_until,
            self.now() < self._escalated_until, self._probe_started_at, self._probe_cooldown,
        )

    def stats(self) -> dict:
        return asdict(self.snapshot())


class MessageTokenBucket:
    """Independent per-account message budget; floods empty and pause refills."""

    def __init__(
        self, rate: float = 3.0, burst: int = 6, *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Optional[Callable[[float], Awaitable[None]]] = None,
    ) -> None:
        if not _finite_number(rate) or rate <= 0 or not _finite_number(burst) or burst < 1:
            raise ValueError("message rate must be positive and burst must be at least one")
        self._rate, self._burst = float(rate), float(burst)
        self._tokens = self._burst
        self._clock, self._sleep = clock, sleeper or asyncio.sleep
        self._last_refill = clock()

    async def acquire(self) -> None:
        while True:
            now = self._clock()
            if now >= self._last_refill:
                self._tokens = min(self._burst, self._tokens + (now - self._last_refill) * self._rate)
                self._last_refill = now
            # Tolerate sub-nanosecond floating-point rounding at refill boundaries.
            if self._tokens >= 1 - 1e-9:
                self._tokens = max(0.0, self._tokens - 1)
                return
            delay = max(0.0, self._last_refill - now) + (1 - self._tokens) / self._rate
            await self._sleep(delay)

    def flood(self, seconds: Optional[float]) -> None:
        self._tokens = 0.0
        self._last_refill = max(self._last_refill, self._clock() + _wait_seconds(seconds))
