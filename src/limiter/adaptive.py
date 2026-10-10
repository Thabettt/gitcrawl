from __future__ import annotations

import math
import os
import random
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass

MIN_WINDOW = 8
MAX_WINDOW = 48
MIN_BATCH = 10
MAX_BATCH = 29
ADAPTIVE_ENV = "GITCRAWL_ADAPTIVE"


def adaptive_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return source.get(ADAPTIVE_ENV, "").strip() == "1"


@dataclass(frozen=True)
class AdaptiveConfig:
    initial_window: int = 20
    min_window: int = MIN_WINDOW
    max_window: int = MAX_WINDOW
    initial_batch: int = 20
    min_batch: int = MIN_BATCH
    max_batch: int = MAX_BATCH
    cooldown_seconds: float = 120.0
    dwell_seconds: float = 300.0
    grow_batches: int = 50
    shrink_factor: float = 0.7
    latency_p95_ms: float = 7000.0
    latency_samples: int = 50
    reserve_points: int = 400
    burst_points: float = 4.0
    short_pause_seconds: float = 60.0
    pause_jitter_seconds: float = 60.0
    max_pause_seconds: float = 300.0
    max_pacer_wait_seconds: float = 30.0
    retry_budget_ratio: float = 0.10


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if str(key).lower() == name:
            return str(value)
    return None


def _parse_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


class AdaptiveController:
    """Pure AIMD window + batch guard + quota pacer + whole-pool pause state."""

    def __init__(
        self,
        config: AdaptiveConfig | None = None,
        *,
        now: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
        initial_batch: int | None = None,
        initial_window: int | None = None,
    ) -> None:
        self._config = config or AdaptiveConfig()
        self._now = now
        self._rng = rng
        self._lock = threading.Lock()
        start_window = self._config.initial_window if initial_window is None else initial_window
        self._window = max(self._config.min_window, min(int(start_window), self._config.max_window))
        start_batch = self._config.initial_batch if initial_batch is None else initial_batch
        self._batch = max(1, min(int(start_batch), self._config.max_batch))
        self._batch_floor = min(self._config.min_batch, self._batch)
        self._clean_batches = 0
        self._batches = 0
        self._drops = 0
        self._pauses = 0
        self._deferred = 0
        self._cooldown_until = 0.0
        self._pause_until = 0.0
        self._last_growth = self._now()
        self._latencies: deque[float] = deque(maxlen=self._config.latency_samples)
        self._remaining: int | None = None
        self._reset_at: float | None = None
        self._rate = 0.0
        self._tokens = self._config.burst_points
        self._last_refill = self._now()
        self._pending_retry_after: float | None = None

    @property
    def window(self) -> int:
        with self._lock:
            return self._window

    @property
    def batch_size(self) -> int:
        with self._lock:
            return self._batch

    @property
    def ceiling(self) -> int:
        return self._config.max_window

    @property
    def p95_ms(self) -> float | None:
        with self._lock:
            return self._p95_locked()

    def _p95_locked(self) -> float | None:
        if not self._latencies:
            return None
        ordered = sorted(self._latencies)
        index = max(0, math.ceil(0.95 * len(ordered)) - 1)
        return ordered[index]

    def observe_response(self, headers: Mapping[str, str], latency_ms: float | None = None) -> None:
        with self._lock:
            if latency_ms is not None:
                self._latencies.append(float(latency_ms))
            now = self._now()
            retry_after = _parse_float(_header(headers, "retry-after"))
            if retry_after is not None:
                self._pending_retry_after = retry_after
            remaining = _parse_int(_header(headers, "x-ratelimit-remaining"))
            if remaining is None or remaining < 0:
                return
            self._refill_locked(now)
            self._remaining = remaining
            reset = _parse_float(_header(headers, "x-ratelimit-reset"))
            if reset is None:
                return
            self._reset_at = reset
            seconds = reset - now
            if seconds > 0:
                self._rate = max(0.0, (remaining - self._config.reserve_points) / seconds)
            else:
                self._rate = 0.0
            self._tokens = min(self._tokens, self._config.burst_points)

    def _refill_locked(self, now: float) -> None:
        elapsed = max(0.0, now - self._last_refill)
        self._last_refill = now
        if self._rate <= 0.0:
            return
        self._tokens = min(self._config.burst_points, self._tokens + elapsed * self._rate)

    def record_batch(self, *, drop: bool = False, guard: bool = False, in_flight: int = 0) -> float:
        with self._lock:
            now = self._now()
            self._batches += 1
            slow = (self._p95_locked() or 0.0) > self._config.latency_p95_ms
            if drop:
                self._drops += 1
                self._window = max(self._config.min_window, self._window // 2)
                self._cooldown_until = max(
                    self._cooldown_until, now + self._config.cooldown_seconds
                )
                self._last_growth = now
            if drop or guard or slow:
                self._batch = max(self._batch_floor, int(self._batch * self._config.shrink_factor))
                self._clean_batches = 0
            else:
                self._clean_batches += 1
                if self._clean_batches >= self._config.grow_batches:
                    self._batch = min(self._config.max_batch, self._batch + 1)
                    self._clean_batches = 0
            if (
                not drop
                and now >= self._cooldown_until
                and now - self._last_growth >= self._config.dwell_seconds
                and in_flight * 2 >= self._window
            ):
                self._window = min(self._config.max_window, self._window + 1)
                self._last_growth = now
            return self._pause_locked(now) if drop else 0.0

    def _pause_locked(self, now: float) -> float:
        budget = max(1, int(self._batches * self._config.retry_budget_ratio))
        if self._pauses >= budget:
            return 0.0
        retry_after = self._pending_retry_after
        self._pending_retry_after = None
        if retry_after is not None and retry_after > 0:
            seconds = retry_after
        else:
            seconds = self._config.short_pause_seconds + (
                self._rng() * self._config.pause_jitter_seconds
            )
        seconds = min(seconds, self._config.max_pause_seconds)
        if seconds <= 0:
            return 0.0
        self._pauses += 1
        self._pause_until = max(self._pause_until, now + seconds)
        return seconds

    def pacer_delay(self) -> float:
        with self._lock:
            return self._pacer_delay_locked(self._now())

    def _pacer_delay_locked(self, now: float) -> float:
        if self._remaining is None or self._rate <= 0.0:
            return 0.0
        if self._reset_at is not None and now >= self._reset_at:
            return 0.0
        self._refill_locked(now)
        if self._tokens >= 1.0:
            return 0.0
        return (1.0 - self._tokens) / self._rate

    def on_dispatch(self) -> None:
        with self._lock:
            self._refill_locked(self._now())
            self._tokens = max(0.0, self._tokens - 1.0)

    def should_stop(self) -> bool:
        with self._lock:
            if self._remaining is None:
                return False
            now = self._now()
            if self._reset_at is not None and now >= self._reset_at:
                return False
            if self._remaining <= self._config.reserve_points:
                return True
            return self._pacer_delay_locked(now) > self._config.max_pacer_wait_seconds

    def pause_remaining(self) -> float:
        with self._lock:
            return max(0.0, self._pause_until - self._now())

    def note_deferred(self, count: int) -> None:
        if count <= 0:
            return
        with self._lock:
            self._deferred += int(count)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            now = self._now()
            return {
                "window": self._window,
                "batch": self._batch,
                "drops": self._drops,
                "pauses": self._pauses,
                "deferred": self._deferred,
                "p95_latency_ms": self._p95_locked(),
                "pacer_remaining": self._remaining,
                "pacer_rate": round(self._rate, 4),
                "paused_for": round(max(0.0, self._pause_until - now), 1),
            }
