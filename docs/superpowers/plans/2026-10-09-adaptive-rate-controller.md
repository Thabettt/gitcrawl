# Adaptive Rate Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Add a default-off, env-gated three-loop adaptive controller (AIMD concurrency window + batch-size guard + hourly quota pacer + whole-pool pause) to the GraphQL batch engine so runs shrink on the first pushback, never drain the point meter to zero, and stop cleanly instead of sleeping to reset.

**Architecture:** A new pure-logic module `src/limiter/adaptive.py` owns all controller math behind an injectable clock/RNG. `lib/graphql_batch.fetch_batch` grows an optional `adaptive=` parameter: the fixed `ThreadPoolExecutor` is sized to the controller ceiling, while submissions are gated by the live window `W`; outcome samples (HTTP status, transient markers, `ThrottledError`, per-response latency from the existing hook) feed the controller; pending keys are re-sliced at dispatch time so batch-size changes apply going forward. `hydrate.tail.refresh_repos_batched` passes the controller through and raises the Redis slot cap to the ceiling; `serve.runner._hydrate` constructs the controller only when `GITCRAWL_ADAPTIVE=1`, clamped inside the hydration envelope that Theme 2 names (`min(HYDRATION_CONCURRENCY_CEILING=32, limiter_max_concurrent)`), so `W` starts at 20 and grows toward that envelope. State is emitted additively into `field_stats.graphql.hydration` (no DB changes).

**Tech Stack:** Python 3.12, httpx 0.28.1 (sync client, `MockTransport` in tests), threads + `ThreadPoolExecutor`, fakeredis, pytest. No new dependencies.

**Spec:** `design/runtime-audit-and-adaptive-control.md` (§7.3 recommended design, §8 recommendation 4, §9 risk verdict); guarantees preserved per `design/corpus-building-efficient-engineering.md` §3.5 and §10.2.

## Global Constraints

- Python 3.12; no new dependencies; the controller module imports only the standard library (no httpx, no Redis client).
- **No DB/schema changes, no settings rows, no alembic migration** (Theme 2 owns the single migration). The kill switch is the env flag `GITCRAWL_ADAPTIVE` only, read as `"1"`; default OFF.
- Adaptive OFF (no controller passed) must preserve existing behavior exactly: all tests in `tests/unit/test_graphql_batch_core.py`, `tests/integration/test_hydrate_batch.py`, and `tests/integration/test_runner.py` keep passing unmodified.
- Existing guarantees are non-negotiable with adaptive ON too: parse data first, per-key attribution, split only failures, REST fallback, explicit final states, deadline, cancellation.
- Coverage gate `fail_under = 93` (`pyproject.toml`); new code must be covered by its own unit/integration tests.
- Quality commands (Windows PowerShell 5.1, from the repo root):
  - tests `.venv\Scripts\python.exe -m pytest -q`
  - lint `.venv\Scripts\python.exe -m ruff check src tests`
  - format `.venv\Scripts\python.exe -m black --check src tests`
  - types `.venv\Scripts\python.exe -m mypy`
- Do **not** modify `src/limiter/buckets.py` (Theme 2 owns the window-anchor/limiter work); use only its public API (`acquire`, `release`, `pause`, `paused_until`, `bound_concurrency`, `deadline`).
- The controller clock must be the same callable as `fetch_batch`'s `now` (production default `time.time`); document this at every construction site.
- Base state: this plan is written against the working tree as of 2026-10-09 (it includes the in-flight fallback-pool changes in `graphql_batch.py`, `tail.py`, `runner.py`). Commit/stash those changes before starting; if a Theme 1/2 plan lands first, re-read the anchor functions before applying edits.

### Dependencies on the other two plans (do not implement here)

These interfaces were written against the sibling plans on disk (`2026-10-09-transport-pipeline-efficiency.md` = Theme 1, `2026-10-09-quota-limiter-utilization.md` = Theme 2).

- **Theme 1** (httpx pool limits, `_post` cached-JSON parse, discovery upsert buffering, audit writer thread): orthogonal. Theme 1 changes the parse *inside* `_post` but keeps `_post(adapter, keys, *, client, limiter, token_id, on_response, sleep, now, jitter) -> tuple[ParsedBatch, tuple[str, ...]]`; the adaptive hook wraps `on_response` and never touches that parse block.
- **Theme 2** (batch 29 defaults + migration `0014`, `HYDRATION_CONCURRENCY_CEILING = 32`, `rateLimit { cost used remaining }` telemetry, limiter window re-anchor, 200-body pause): the cross-plan contract in its Self-review says Theme 3 "build[s] the live window at `HYDRATION_CONCURRENCY_CEILING` by replacing the static `limit(concurrency)` submission gate in `fetch_batch`" and consumes `field_stats["points"]`, `BatchStats.points_cost/used/remaining`, `audit_log(run_id, phase, rl_used)` for validation only. Reconciliation in this plan: `_hydrate` clamps the controller envelope to `min(MAX_WINDOW=48, cfg.concurrency)` (Theme 2's `cfg.concurrency = min(limiter_max_concurrent, 32)`), starts `W` at `min(20, ceiling)`, and disables adaptive when the ceiling is below `MIN_WINDOW=8` so the window never exceeds the operator envelope. Theme 2's `ParsedBatch.rate_limit`/`BatchStats.points_*` stay optional: the pacer reads headers directly and does not import them.
- **Merge order:** Theme 2 and this plan both edit `src/lib/graphql_batch.py`, `src/hydrate/tail.py`, `src/serve/runner.py`; Theme 1 also edits `src/lib/graphql_batch.py`. Land Theme 1, then Theme 2, then rebase this plan onto the resulting `fetch_batch` loop (the anchor line numbers in Task 2 refer to the pre-Theme-2 tree; the *shape* of Edit A-D is what to re-apply).
- **Out of scope:** discovery-side adaptation (its page halving at `src/discover/graphql_search.py:395-399` stays as-is — follow-up note only), enrichment-side adaptation, DB settings for the flag.

### File structure

| File | Responsibility |
|---|---|
| `src/limiter/adaptive.py` (create) | Pure controller: AIMD window, batch guard, quota pacer, whole-pool pause state, telemetry snapshot, env flag helper |
| `src/lib/graphql_batch.py` (modify) | Live dispatch gate, adaptive response hook, re-slicing queue, drop/guard signal folding, reserve deferral, pool-wide pause wait loop, `BatchStats.deferred`/`.adaptive` |
| `src/hydrate/tail.py` (modify) | Pass the controller through; raise the Redis slot cap to the controller ceiling for the batch run |
| `src/serve/runner.py` (modify) | Build the controller when `GITCRAWL_ADAPTIVE=1`; add the deferred warning |
| `docs/environment.md` (modify) | Document the kill switch |
| `tests/unit/test_adaptive.py` (create), `tests/unit/test_graphql_batch_adaptive.py` (create), `tests/integration/test_adaptive_hydration.py` (create), `tests/integration/test_runner.py` (add cases) | Coverage |
| `docs/findings/2026-10-09-adaptive-soak.md` (create, Task 4) | Manual soak evidence |

---

### Task 1: Adaptive controller core (`src/limiter/adaptive.py`)

**Files:**
- Create: `src/limiter/adaptive.py`
- Test: `tests/unit/test_adaptive.py`

**Interfaces:**
- Consumes: standard library only (`math`, `os`, `random`, `threading`, `time`, `collections.deque`).
- Produces:
  - `limiter.adaptive.MIN_WINDOW = 8`, `MAX_WINDOW = 48`, `MIN_BATCH = 10`, `MAX_BATCH = 29`, `ADAPTIVE_ENV = "GITCRAWL_ADAPTIVE"`.
  - `limiter.adaptive.adaptive_enabled(env: Mapping[str, str] | None = None) -> bool` — true only when the flag is exactly `"1"`.
  - `limiter.adaptive.AdaptiveConfig` (frozen dataclass) fields with defaults: `initial_window=20`, `min_window=8`, `max_window=48`, `initial_batch=20`, `min_batch=10`, `max_batch=29`, `cooldown_seconds=120.0`, `dwell_seconds=300.0`, `grow_batches=50`, `shrink_factor=0.7`, `latency_p95_ms=7000.0`, `latency_samples=50`, `reserve_points=400`, `burst_points=4.0`, `short_pause_seconds=60.0`, `pause_jitter_seconds=60.0`, `max_pause_seconds=300.0`, `max_pacer_wait_seconds=30.0`, `retry_budget_ratio=0.10`.
  - `limiter.adaptive.AdaptiveController(config: AdaptiveConfig | None = None, *, now: Callable[[], float] = time.monotonic, rng: Callable[[], float] = random.random, initial_batch: int | None = None, initial_window: int | None = None)` with:
    - properties `window: int`, `batch_size: int`, `ceiling: int`, `p95_ms: float | None`
    - `observe_response(headers: Mapping[str, str], latency_ms: float | None = None) -> None` — thread-safe; parses `x-ratelimit-remaining`, `x-ratelimit-reset`, `retry-after`; records one latency sample; feeds the pacer
    - `record_batch(*, drop: bool = False, guard: bool = False, in_flight: int = 0) -> float` — folds one completed batch; returns whole-pool pause seconds (`0.0` when none)
    - `on_dispatch() -> None` (consume one pacer token), `pacer_delay() -> float`, `should_stop() -> bool`
    - `pause_remaining() -> float`, `note_deferred(count: int) -> None`
    - `snapshot() -> dict[str, object]` with keys `window`, `batch`, `drops`, `pauses`, `deferred`, `p95_latency_ms`, `pacer_remaining`, `pacer_rate`, `paused_for`

Semantics (locked): every `drop` halves `W` down to `min_window` and re-freezes growth during `cooldown_seconds`; `W` grows `+1` only when not in cooldown, at least `dwell_seconds` since the last drop/growth, and `in_flight * 2 >= W`, capped at `max_window`. `guard`/drop/p95-above-threshold shrinks `B` by `shrink_factor` floored at `min(min_batch, initial)`; 50 consecutive clean batches grow `B` by 1 capped at `max_batch`. The pacer spends `burst_points` then paces at `(remaining - reserve) / seconds-to-reset`; it stops cleanly when `remaining <= reserve` or when more than `max_pacer_wait_seconds` would be needed for the next point (never sleep to reset). Pauses honor `retry-after` exactly when present, else `60 + rng()*60`, capped at `max_pause_seconds`; pause count is bounded by `max(1, int(batches * retry_budget_ratio))`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_adaptive.py`:

```python
from __future__ import annotations

from collections.abc import Callable

import pytest

from limiter.adaptive import (
    ADAPTIVE_ENV,
    AdaptiveConfig,
    AdaptiveController,
    adaptive_enabled,
)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def make_controller(
    clock: FakeClock | None = None,
    rng: Callable[[], float] | None = None,
    **overrides: object,
) -> AdaptiveController:
    config = AdaptiveConfig(**overrides)  # type: ignore[arg-type]
    return AdaptiveController(config, now=clock or FakeClock(), rng=rng or (lambda: 0.0))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", True), ("0", False), ("true", False), ("", False)],
)
def test_adaptive_enabled_reads_the_kill_switch(raw: str, expected: bool) -> None:
    assert adaptive_enabled({ADAPTIVE_ENV: raw}) is expected


def test_adaptive_enabled_is_off_without_the_variable() -> None:
    assert adaptive_enabled({}) is False


def test_window_starts_at_twenty_and_batch_at_twenty() -> None:
    controller = make_controller()
    assert controller.window == 20
    assert controller.batch_size == 20
    assert controller.ceiling == 48


def test_each_drop_halves_the_window_down_to_the_floor() -> None:
    controller = make_controller()
    controller.record_batch(drop=True)
    assert controller.window == 10
    controller.record_batch(drop=True)
    assert controller.window == 8
    controller.record_batch(drop=True)
    assert controller.window == 8


def test_window_does_not_grow_while_in_cooldown() -> None:
    clock = FakeClock()
    controller = make_controller(clock, dwell_seconds=0.0)
    controller.record_batch(drop=True)  # window 10, cooldown until 120
    clock.advance(30.0)
    controller.record_batch(in_flight=10)
    assert controller.window == 10
    clock.advance(120.0)
    controller.record_batch(in_flight=10)
    assert controller.window == 11


def test_window_grows_only_after_a_clean_dwell_when_nearly_saturated() -> None:
    clock = FakeClock()
    controller = make_controller(clock, dwell_seconds=300.0)
    clock.advance(100.0)
    controller.record_batch(in_flight=20)  # dwell not reached
    assert controller.window == 20
    clock.advance(200.0)
    controller.record_batch(in_flight=1)  # t=300 but not saturated
    assert controller.window == 20
    clock.advance(1.0)
    controller.record_batch(in_flight=19)  # t=301 and saturated
    assert controller.window == 21


def test_window_growth_is_additive_and_capped() -> None:
    clock = FakeClock()
    controller = make_controller(clock, dwell_seconds=0.0)
    for _ in range(60):
        clock.advance(1.0)
        controller.record_batch(in_flight=48)
    assert controller.window == 48


def test_guard_signal_shrinks_the_batch_without_pausing() -> None:
    controller = make_controller()
    pause = controller.record_batch(guard=True)
    assert controller.batch_size == 14
    assert pause == 0.0


def test_high_p95_latency_shrinks_the_batch() -> None:
    controller = make_controller()
    for _ in range(10):
        controller.observe_response({}, latency_ms=8000.0)
    assert controller.record_batch() == 0.0
    assert controller.batch_size == 14


def test_batch_never_shrinks_below_ten() -> None:
    controller = make_controller()
    for _ in range(5):
        controller.record_batch(guard=True)
    assert controller.batch_size == 10


def test_batch_grows_one_after_fifty_clean_batches_and_caps_at_twenty_nine() -> None:
    controller = make_controller(initial_batch=28)
    for _ in range(50):
        controller.record_batch()
    assert controller.batch_size == 29
    for _ in range(150):
        controller.record_batch()
    assert controller.batch_size == 29


def test_reserve_floor_stops_dispatch_until_the_window_resets() -> None:
    clock = FakeClock(1000.0)
    controller = make_controller(clock)
    controller.observe_response({"x-ratelimit-remaining": "400", "x-ratelimit-reset": "1600"})
    assert controller.should_stop() is True
    controller.observe_response({"x-ratelimit-remaining": "900", "x-ratelimit-reset": "1600"})
    assert controller.should_stop() is False
    clock.advance(700.0)
    assert controller.should_stop() is False


def test_pacer_that_cannot_sustain_a_point_stops_the_run() -> None:
    clock = FakeClock(1000.0)
    controller = make_controller(clock)
    controller.observe_response({"x-ratelimit-remaining": "405", "x-ratelimit-reset": "1600"})
    for _ in range(4):
        controller.on_dispatch()
    assert controller.should_stop() is True


def test_pacer_allows_a_small_burst_then_waits() -> None:
    clock = FakeClock(1000.0)
    controller = make_controller(clock)
    controller.observe_response({"x-ratelimit-remaining": "1000", "x-ratelimit-reset": "1600"})
    for _ in range(4):  # (1000 - 400) / 600 = 1 point/s; burst of 4
        assert controller.pacer_delay() == 0.0
        controller.on_dispatch()
    assert controller.pacer_delay() == pytest.approx(1.0)
    clock.advance(1.0)
    assert controller.pacer_delay() == 0.0


def test_pacer_does_nothing_without_headers() -> None:
    controller = make_controller(FakeClock(1000.0))
    assert controller.pacer_delay() == 0.0
    assert controller.should_stop() is False


def test_drop_uses_retry_after_for_the_pool_pause() -> None:
    clock = FakeClock(1000.0)
    controller = make_controller(clock)
    controller.observe_response({"retry-after": "75"})
    pause = controller.record_batch(drop=True)
    assert pause == 75.0
    assert controller.pause_remaining() == 75.0
    clock.advance(75.0)
    assert controller.pause_remaining() == 0.0


def test_pause_without_retry_after_is_at_least_sixty_seconds() -> None:
    controller = make_controller(FakeClock(1000.0), rng=lambda: 0.5)
    assert controller.record_batch(drop=True) == 90.0


def test_pause_is_capped() -> None:
    controller = make_controller(FakeClock(1000.0), max_pause_seconds=300.0)
    controller.observe_response({"retry-after": "900"})
    assert controller.record_batch(drop=True) == 300.0


def test_pause_respects_the_retry_budget() -> None:
    controller = make_controller(FakeClock(1000.0))
    assert controller.record_batch(drop=True) > 0.0  # 1 batch -> budget max(1, 0) = 1
    assert controller.record_batch(drop=True) == 0.0


def test_snapshot_reports_the_live_state() -> None:
    controller = make_controller(FakeClock(1000.0))
    controller.record_batch()
    snapshot = controller.snapshot()
    assert snapshot["window"] == 20
    assert snapshot["batch"] == 20
    assert snapshot["drops"] == 0
    assert snapshot["pauses"] == 0
    assert snapshot["deferred"] == 0
    assert snapshot["p95_latency_ms"] is None


def test_note_deferred_is_reported() -> None:
    controller = make_controller()
    controller.note_deferred(3)
    assert controller.snapshot()["deferred"] == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_adaptive.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'limiter.adaptive'`.

- [ ] **Step 3: Implement `src/limiter/adaptive.py`**

```python
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
        self._window = max(
            self._config.min_window, min(int(start_window), self._config.max_window)
        )
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

    def observe_response(
        self, headers: Mapping[str, str], latency_ms: float | None = None
    ) -> None:
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

    def record_batch(
        self, *, drop: bool = False, guard: bool = False, in_flight: int = 0
    ) -> float:
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
                self._batch = max(
                    self._batch_floor, int(self._batch * self._config.shrink_factor)
                )
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_adaptive.py -q`
Expected: PASS (20 tests).

- [ ] **Step 5: Lint, format, types, then commit**

```bash
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m black --check src tests
.venv\Scripts\python.exe -m mypy
git add src/limiter/adaptive.py tests/unit/test_adaptive.py
git commit -m "feat(limiter): adaptive rate controller core (aimd window, batch guard, pacer, pool pause)"
```

---

### Task 2: Wire the controller into `fetch_batch`

**Files:**
- Modify: `src/lib/graphql_batch.py` (imports :1-16, `BatchStats` :62-83, `fetch_batch` :178-386)
- Test: `tests/unit/test_graphql_batch_adaptive.py`

**Interfaces:**
- Consumes: `limiter.adaptive.AdaptiveController` (Task 1); `limiter.buckets.BucketLimiter.pause`/`paused_until`.
- Produces:
  - `lib.graphql_batch.fetch_batch(..., adaptive: AdaptiveController | None = None)` — new keyword-only parameter; `None` keeps today's exact behavior (queue seeded by `_chunks`, gate = `concurrency`, pool = `concurrency`).
  - `lib.graphql_batch.BatchStats` gains `deferred: int = 0` and `adaptive: dict[str, object] = field(default_factory=dict)`; `as_dict()` includes both keys (`adaptive` is `{}` when no controller ran).
  - With a controller: pool workers = `adaptive.ceiling` (the caller-clamped ceiling, `<= 48`; `_hydrate` clamps it into Theme 2's hydration envelope); dispatch gate = `adaptive.window`; chunk budget = `min(adaptive.batch_size, adapter.batch_size)`; queue is one contiguous key run, sliced live at dispatch and re-sliced after requeues; `adaptive.on_dispatch()` per submission; every completed batch folds `drop` (403/429, 499/502/504, rate-limit/abuse markers, `ThrottledError`) and `guard` (timeout status or any `_is_transient` reason) plus the response hook feeds `observe_response` for latency and pacer headers; when `record_batch` returns `pause > 0`, every worker gate opens only after `pause_remaining()` reaches 0, and the shared Redis bucket is paused when the new deadline extends the stored one.
  - When `should_stop()`: queued keys become `unresolved[key] = "deferred: github graphql point reserve reached"`, `stats.deferred` counts them, `adaptive.note_deferred` mirrors the count; in-flight work still folds; the run returns a partial outcome.
  - Fixed pool, variable gate: the pool is created once at `adaptive.ceiling`; only the gate changes. Immediately after a drop, already-issued calls can outnumber the new `W` until they return; the loop simply stops submitting until `len(in_flight) < W`, so concurrency never exceeds the ceiling and never grows by more than one per dwell.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_graphql_batch_adaptive.py`:

```python
from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping

import fakeredis
import httpx

from lib.graphql_batch import ParsedBatch, fetch_batch
from limiter.adaptive import AdaptiveConfig, AdaptiveController
from limiter.buckets import BucketLimiter


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class KeyAdapter:
    name = "keys"

    def __init__(self, batch_size: int = 20) -> None:
        self.batch_size = batch_size

    def build_query(self, aliases: Mapping[str, str]) -> str:
        pairs = " ".join(f"{alias}: field_{key}" for alias, key in aliases.items())
        return f"query {{ {pairs} }}"

    def parse(self, payload: Mapping[str, object], aliases: Mapping[str, str]) -> ParsedBatch[str]:
        data = payload.get("data")
        values: dict[str, str] = {}
        if isinstance(data, dict):
            for alias, node in data.items():
                key = aliases.get(alias)
                if key is not None and isinstance(node, str):
                    values[key] = node
        return ParsedBatch(values=values)


KEYS_RE = re.compile(r"n\d+: field_(\d+)")


def keys_in(request: httpx.Request) -> list[str]:
    return KEYS_RE.findall(json.loads(request.content)["query"])


def ok_response(keys: list[str]) -> httpx.Response:
    data = {f"n{index}": f"v-{key}" for index, key in enumerate(keys)}
    return httpx.Response(200, json={"data": data})


def test_first_drop_halves_the_window_and_pauses_the_pool() -> None:
    clock = FakeClock()
    controller = AdaptiveController(AdaptiveConfig(), now=clock, rng=lambda: 0.0)
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] <= 5:
            return httpx.Response(
                403, json={"message": "secondary rate limit"}, headers={"retry-after": "30"}
            )
        return ok_response(keys_in(request))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        KeyAdapter(),
        ["1", "2", "3", "4"],
        client=client,
        adaptive=controller,
        fallback=lambda key: f"rest-{key}",
        max_attempts=1,
        sleep=clock.advance,
        now=clock,
    )
    assert controller.window == 10
    assert controller.snapshot()["drops"] == 1
    assert controller.snapshot()["pauses"] == 1
    assert outcome.values == {"1": "rest-1", "2": "rest-2", "3": "rest-3", "4": "rest-4"}
    assert outcome.unresolved == {}


def test_pause_defers_new_dispatch_until_it_expires() -> None:
    clock = FakeClock()
    controller = AdaptiveController(
        AdaptiveConfig(initial_window=2, min_window=1, max_window=2),
        now=clock,
        rng=lambda: 0.0,
    )
    starts: list[tuple[list[str], float]] = []
    lock = threading.Lock()
    state = {"failed": False}

    def handler(request: httpx.Request) -> httpx.Response:
        keys = keys_in(request)
        with lock:
            starts.append((keys, clock.value))
        if keys == ["1", "2"] and not state["failed"]:
            state["failed"] = True
            return httpx.Response(504, json={"message": "timeout"})
        return ok_response(keys)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        KeyAdapter(batch_size=2),
        ["1", "2", "3", "4", "5", "6"],
        client=client,
        adaptive=controller,
        sleep=clock.advance,
        now=clock,
    )
    assert set(outcome.values) == {"1", "2", "3", "4", "5", "6"}
    first = next(time for keys, time in starts if keys == ["1", "2"])
    tail = [time for keys, time in starts if keys and keys[0] in ("5", "6")]
    assert tail and min(tail) - first >= 60.0
    assert controller.window == 1


def test_timeout_shrinks_followup_chunks() -> None:
    clock = FakeClock()
    controller = AdaptiveController(
        AdaptiveConfig(short_pause_seconds=0.0, pause_jitter_seconds=0.0),
        now=clock,
        rng=lambda: 0.0,
    )
    batches: list[list[str]] = []
    state = {"failed": False}

    def handler(request: httpx.Request) -> httpx.Response:
        keys = keys_in(request)
        batches.append(list(keys))
        if not state["failed"]:
            state["failed"] = True
            return httpx.Response(504, json={"message": "timeout"})
        return ok_response(keys)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        KeyAdapter(),
        [str(index) for index in range(1, 41)],
        client=client,
        adaptive=controller,
        sleep=clock.advance,
        now=clock,
    )
    assert batches[0] == [str(index) for index in range(1, 21)]
    assert controller.batch_size == 14
    assert all(len(batch) <= 14 for batch in batches[1:])
    assert any(len(batch) == 14 for batch in batches[1:])
    assert outcome.unresolved == {}


def test_reserve_floor_defers_queued_keys_without_dispatch() -> None:
    clock = FakeClock()
    controller = AdaptiveController(
        AdaptiveConfig(initial_window=2, min_window=1, max_window=2),
        now=clock,
        rng=lambda: 0.0,
    )
    batches: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys = keys_in(request)
        batches.append(list(keys))
        return httpx.Response(
            200,
            json={"data": {f"n{index}": f"v-{key}" for index, key in enumerate(keys)}},
            headers={"x-ratelimit-remaining": "350", "x-ratelimit-reset": "4600"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        KeyAdapter(batch_size=2),
        ["1", "2", "3", "4", "5", "6"],
        client=client,
        adaptive=controller,
        sleep=clock.advance,
        now=clock,
    )
    assert batches == [["1", "2"], ["3", "4"]]
    assert outcome.stats.deferred == 2
    assert outcome.unresolved == {
        "5": "deferred: github graphql point reserve reached",
        "6": "deferred: github graphql point reserve reached",
    }
    assert outcome.values == {"1": "v-1", "2": "v-2", "3": "v-3", "4": "v-4"}
    assert controller.snapshot()["deferred"] == 2


def test_local_throttled_error_counts_as_a_drop_and_pauses_the_bucket() -> None:
    clock = FakeClock(1000.0)
    redis = fakeredis.FakeRedis()
    limiter = BucketLimiter(redis, specs={"graphql": (0, 3600.0)}, max_concurrent=100)
    controller = AdaptiveController(
        AdaptiveConfig(short_pause_seconds=10.0, pause_jitter_seconds=0.0),
        now=clock,
        rng=lambda: 0.0,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP request expected while the limiter denies")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        KeyAdapter(),
        ["1"],
        client=client,
        limiter=limiter,
        token_id="token-a",
        adaptive=controller,
        sleep=clock.advance,
        now=clock,
    )
    assert controller.snapshot()["drops"] == 1
    assert controller.snapshot()["pauses"] == 1
    assert limiter.paused_until("graphql", "token-a") == clock.value + 10.0
    assert "1" in outcome.unresolved
    assert "ThrottledError" in outcome.unresolved["1"]


def test_controller_snapshot_lands_in_batch_stats() -> None:
    clock = FakeClock()
    controller = AdaptiveController(AdaptiveConfig(), now=clock, rng=lambda: 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return ok_response(keys_in(request))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(KeyAdapter(), ["1", "2"], client=client, adaptive=controller, now=clock)
    stats = outcome.stats.as_dict()
    assert stats["deferred"] == 0
    assert stats["adaptive"]["window"] == 20
    assert stats["adaptive"]["drops"] == 0
```

`max_attempts=1` on the first 403 test is deliberate: once Theme 2 lands, `_post` preserves GitHub's message ("secondary rate limit"), which `_is_transient` treats as retryable, so without the pin the failed keys would be requeued and could succeed on a later HTTP call instead of taking the REST fallback this test pins. With `max_attempts=1` the keys exhaust immediately and go to `fallback` under both pre- and post-Theme-2 behavior.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_batch_adaptive.py -q`
Expected: FAIL — `TypeError: fetch_batch() got an unexpected keyword argument 'adaptive'`.

- [ ] **Step 3: Implement the batch-engine wiring**

**Edit A — imports, module helpers, `BatchStats`, signature.** Replace the import block and add three module-level helpers after `_is_transient` / `is_transient_error`:

```python
from limiter.adaptive import AdaptiveController
```

```python
_RATE_LIMIT_MARKERS = ("rate limit", "secondary rate", "abuse")
_ADAPTIVE_SLEEP_SLICE = 0.5


def _is_rate_limit_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


def _adaptive_hook(
    adaptive: AdaptiveController,
    on_response: Callable[[httpx.Response, float], None] | None,
) -> Callable[[httpx.Response, float], None]:
    def hook(response: httpx.Response, latency_ms: float) -> None:
        adaptive.observe_response(response.headers, latency_ms)
        if on_response is not None:
            on_response(response, latency_ms)

    return hook


def _adaptive_sleep(seconds: float, sleep: Callable[[float], None]) -> None:
    remaining = max(0.0, seconds)
    while remaining > 0.0:
        cancellation.check()
        step = min(remaining, _ADAPTIVE_SLEEP_SLICE)
        sleep(step)
        remaining -= step


def _pause_bucket(
    limiter: BucketLimiter, token_id: str | None, seconds: float, now_value: float
) -> None:
    key_id = token_id or ""
    existing = limiter.paused_until("graphql", key_id)
    if existing is None or existing < now_value + seconds:
        limiter.pause("graphql", key_id, seconds, now=now_value)
```

Extend `BatchStats` **additively** (do not delete Theme 2's `points_cost`/`points_used`/`points_remaining` fields or keys if they are already present). Add the two fields after `deadline_hit`:

```python
    deferred: int = 0
    adaptive: dict[str, object] = field(default_factory=dict)
```

and add the two keys at the end of `as_dict()`:

```python
            "deferred": self.deferred,
            "adaptive": self.adaptive,
```

Add the parameter to `fetch_batch` (keyword-only, after `concurrency`):

```python
    concurrency: int = 1,
    adaptive: AdaptiveController | None = None,
) -> BatchOutcome:
```

After the existing validations add:

```python
    pool_workers = adaptive.ceiling if adaptive is not None else concurrency
```

Set `stats.adaptive` on every return path. Immediately after `stats = BatchStats(keys=len(unique))` add:

```python
    if adaptive is not None:
        stats.adaptive = adaptive.snapshot()
```

Do the same just before the early `return BatchOutcome(...)` for `if not unique:` and inside the `if not allow_requests:` block, and refresh it before the final return (see Edit C).

**Edit B — queue seeding, fallback pool, hook, `submit_chunk`.** Replace line 212 and the fallback-pool construction:

```python
    if adaptive is None:
        queue: deque[list[str]] = deque(_chunks(unique, size))
    else:
        queue = deque([unique])  # one contiguous run; sliced live at dispatch

    fallback_pool = (
        ThreadPoolExecutor(max_workers=min(pool_workers, _MAX_FALLBACK_WORKERS))
        if fallback is not None
        else None
    )
```

Then, inside `try:` after the `if not allow_requests:` block, before the `def requeue` definition, add:

```python
        hook = on_response if adaptive is None else _adaptive_hook(adaptive, on_response)
```

and change `submit_chunk` to pass `on_response=hook`.

**Edit C — the dispatch/wait loop.** Replace the whole block from `with ThreadPoolExecutor(max_workers=concurrency) as pool:` through the final `drain_fallbacks(block=False)` (currently lines 309-380) with the code below. If Theme 2 landed first, re-apply this shape while keeping its `rate_limit` telemetry-aggregation lines in the fold (the `resolved`/`values.update`/`fall_back` logic is unchanged; only insert the adaptive signal computation and the `record_batch` call after it).

```python
        with ThreadPoolExecutor(max_workers=pool_workers) as pool:
            in_flight: dict[Future, tuple[list[str], float]] = {}
            while queue or in_flight or fallback_futures:
                cancellation.check()
                throttle = 0.0
                if adaptive is not None:
                    if adaptive.should_stop():
                        marked = 0
                        while queue:
                            for key in queue.popleft():
                                if (
                                    key not in values
                                    and key not in unresolved
                                    and key not in handled_keys
                                ):
                                    unresolved[key] = (
                                        "deferred: github graphql point reserve reached"
                                    )
                                    marked += 1
                        stats.deferred += marked
                        adaptive.note_deferred(marked)
                    throttle = max(adaptive.pause_remaining(), adaptive.pacer_delay())
                gate = adaptive.window if adaptive is not None else concurrency
                while queue and len(in_flight) < gate and throttle <= 0.0:
                    chunk = queue.popleft()
                    budget = min(adaptive.batch_size, size) if adaptive is not None else size
                    if len(chunk) > budget:
                        queue.appendleft(chunk[budget:])
                        chunk = chunk[:budget]
                    pending = [
                        key
                        for key in chunk
                        if key not in values and key not in unresolved and key not in handled_keys
                    ]
                    if not pending:
                        continue
                    if deadline is not None and deadline.remaining <= 0:
                        stats.deadline_hit = True
                        for key in pending:
                            unresolved[key] = "run deadline exceeded"
                        continue
                    stats.requests += 1
                    if adaptive is not None:
                        adaptive.on_dispatch()
                    in_flight[submit_chunk(pool, pending)] = (pending, now())
                    if adaptive is not None:
                        throttle = max(adaptive.pause_remaining(), adaptive.pacer_delay())
                if not in_flight:
                    if throttle > 0.0:
                        drain_fallbacks(block=False)
                        _adaptive_sleep(min(throttle, _ADAPTIVE_SLEEP_SLICE), sleep)
                        continue
                    drain_fallbacks(block=True)
                    continue
                done, _pending_futures = wait(
                    in_flight,
                    return_when=FIRST_COMPLETED,
                    timeout=None if throttle <= 0.0 else min(throttle, _ADAPTIVE_SLEEP_SLICE),
                )
                for future in done:
                    pending, _submitted = in_flight.pop(future)
                    transient = False
                    status: int | None = None
                    throttled = False
                    try:
                        parsed, batch_errors = future.result()
                    except GraphQLAuthError:
                        raise
                    except DeadlineExceededError:
                        stats.deadline_hit = True
                        for key in pending:
                            unresolved[key] = "run deadline exceeded"
                        continue
                    except (
                        PartialResultsError,
                        RequestFailed,
                        MalformedResponse,
                        ThrottledError,
                        httpx.HTTPError,
                    ) as exc:
                        parsed = ParsedBatch()
                        batch_errors = (f"{type(exc).__name__}: {exc}",)
                        if isinstance(exc, RequestFailed):
                            status = exc.status
                        throttled = isinstance(exc, ThrottledError)
                        transient = (
                            isinstance(exc, RequestFailed) and exc.status in _TIMEOUT_STATUSES
                        )
                    resolved = {
                        key: value for key, value in parsed.values.items() if key in pending
                    }
                    values.update(resolved)
                    if on_resolved is not None and resolved:
                        on_resolved(tuple(resolved), resolved)
                    failed = [key for key in pending if key not in values]
                    reason = batch_errors[0] if batch_errors else ""
                    if batch_errors:
                        if transient or _is_transient(reason):
                            for key in requeue(failed):
                                fall_back(key, reason)
                        else:
                            for key in failed:
                                fall_back(key, reason)
                    else:
                        for key in failed:
                            fall_back(key, parsed.failures.get(key, "missing result"))
                    if adaptive is not None:
                        pause = adaptive.record_batch(
                            drop=(
                                throttled
                                or status in (403, 429)
                                or status in _TIMEOUT_STATUSES
                                or _is_rate_limit_error(reason)
                            ),
                            guard=transient or _is_transient(reason),
                            in_flight=len(in_flight),
                        )
                        if pause > 0.0 and limiter is not None:
                            _pause_bucket(limiter, token_id, pause, now())
                    if on_progress is not None:
                        on_progress(
                            len(values) + len(unresolved) + len(handled_keys),
                            len(unique),
                        )
                drain_fallbacks(block=False)
```

**Edit D — final stats.** Before the last `return BatchOutcome(...)` (currently lines 381-383) add:

```python
        stats.values = len(values)
        stats.unresolved = len(unresolved)
        if adaptive is not None:
            stats.adaptive = adaptive.snapshot()
        return BatchOutcome(values=values, unresolved=unresolved, stats=stats)
```

- [ ] **Step 4: Run the focused and existing batch suites**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_batch_adaptive.py tests/unit/test_graphql_batch_core.py -q`
Expected: PASS. If any `test_graphql_batch_core.py` case fails, the adaptive-OFF path changed — fix before continuing.

- [ ] **Step 5: Lint, format, types, then commit**

```bash
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m black --check src tests
.venv\Scripts\python.exe -m mypy
git add src/lib/graphql_batch.py tests/unit/test_graphql_batch_adaptive.py
git commit -m "feat(graphql): adaptive live gate, batch guard, reserve deferral, pool pause"
```

---

### Task 3: Hydration + runner wiring, telemetry, kill switch

**Files:**
- Modify: `src/hydrate/tail.py` (`refresh_repos_batched` :107-327)
- Modify: `src/serve/runner.py` (`_hydrate` :221-245, deferred warning :885-893)
- Modify: `docs/environment.md` (env-var table :116-124, corpus-profile bullets :234-235)
- Test: `tests/integration/test_adaptive_hydration.py` (create), `tests/integration/test_runner.py` (append three cases; add `import threading` and `import time`)

**Interfaces:**
- Consumes: `AdaptiveController` (Task 1), `fetch_batch(..., adaptive=...)` and `BatchStats.deferred`/`.adaptive` (Task 2).
- Produces:
  - `hydrate.tail.refresh_repos_batched(..., adaptive: AdaptiveController | None = None) -> RefreshStats` — passes the controller to `fetch_batch`; when both `adaptive` and `limiter` are present, wraps the fetch in `limiter.bound_concurrency(adaptive.ceiling)` so the Redis slot cap stays the hard backstop (it only raises; never lowers an operator value).
  - `RefreshStats.batch` now carries `deferred` and `adaptive` (via `BatchStats.as_dict`), surfacing at `payload.field_stats["graphql"]["hydration"]["adaptive"]` and `[...]["deferred"]`.
  - `serve.runner._hydrate` builds the controller only when `adaptive_enabled()` and `min(MAX_WINDOW, cfg.concurrency) >= MIN_WINDOW`, using `AdaptiveConfig(initial_window=min(20, ceiling), max_window=ceiling)` with `ceiling = min(MAX_WINDOW, cfg.concurrency)` and `initial_batch=min(cfg.graphql_batch_size, MAX_BATCH)`; adaptive OFF (or a too-small ceiling) constructs nothing and produces `adaptive == {}`.
  - New warning copy when deferrals happen: `"{n} repo(s) deferred: the GraphQL point reserve was reached; results are incomplete"`.

- [ ] **Step 1: Write the failing integration tests**

Create `tests/integration/test_adaptive_hydration.py`:

```python
from __future__ import annotations

import json
import re

import httpx
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

from hydrate.tail import refresh_repos_batched
from limiter.adaptive import AdaptiveConfig, AdaptiveController

ALIAS_RE = re.compile(r'(n\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\)')


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def seed_repos(engine: Engine, count: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO owners (id, login, type) VALUES (901, 'octo', 'User')")
        )
        for repo_id in range(1, count + 1):
            connection.execute(
                text(
                    "INSERT INTO repos (id, node_id, full_name, owner_id, name, visibility)"
                    " VALUES (:id, :node, :full, 901, :name, 'public')"
                ),
                {
                    "id": repo_id,
                    "node": f"R_{repo_id}",
                    "full": f"octo/repo{repo_id}",
                    "name": f"repo{repo_id}",
                },
            )


def rows_for(count: int) -> list[dict]:
    return [
        {"id": repo_id, "full_name": f"octo/repo{repo_id}"}
        for repo_id in range(1, count + 1)
    ]


def graphql_node(repo_id: int) -> dict:
    return {
        "databaseId": repo_id,
        "id": f"R_{repo_id}",
        "name": f"repo{repo_id}",
        "nameWithOwner": f"octo/repo{repo_id}",
        "stargazerCount": repo_id,
        "forkCount": 0,
        "watchers": {"totalCount": 0},
        "issues": {"totalCount": 0},
        "diskUsage": 1,
        "isArchived": False,
        "isDisabled": False,
        "isFork": False,
        "isTemplate": False,
        "visibility": "PUBLIC",
        "description": None,
        "homepageUrl": None,
        "pushedAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
        "createdAt": "2024-01-01T00:00:00Z",
        "defaultBranchRef": {
            "name": "main",
            "target": {"history": {"totalCount": repo_id * 10}},
        },
        "primaryLanguage": {"name": "Rust"},
        "licenseInfo": None,
        "repositoryTopics": {"nodes": []},
        "hasIssuesEnabled": True,
        "hasWikiEnabled": False,
        "hasProjectsEnabled": False,
        "hasDiscussionsEnabled": False,
        "hasPullRequestsEnabled": True,
        "parent": None,
        "owner": {"databaseId": 901, "login": "octo", "__typename": "User"},
    }


def graphql_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    data: dict[str, object] = {}
    for alias, _owner, name in ALIAS_RE.findall(body["query"]):
        data[alias] = graphql_node(int(name.removeprefix("repo")))
    return httpx.Response(200, json={"data": data})


def test_adaptive_hydration_shrinks_after_a_timeout_and_saves_every_repo(clean_db):
    engine = clean_db()
    seed_repos(engine, 25)
    clock = FakeClock()
    controller = AdaptiveController(
        AdaptiveConfig(initial_batch=20, short_pause_seconds=0.0, pause_jitter_seconds=0.0),
        now=clock,
        rng=lambda: 0.0,
    )
    state = {"failed": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if not state["failed"]:
            state["failed"] = True
            return httpx.Response(504, json={"message": "timeout"})
        return graphql_handler(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = refresh_repos_batched(
        engine, client, rows_for(25), adaptive=controller, now=clock, sleep=clock.advance
    )
    assert stats.refreshed == 25
    assert stats.unresolved == {}
    assert stats.batch["deferred"] == 0
    assert stats.batch["adaptive"]["window"] == 10  # 20 -> 10 on the first timeout
    assert stats.batch["adaptive"]["batch"] == 14  # 20 -> 14


def test_adaptive_hydration_defers_at_the_reserve_floor(clean_db):
    engine = clean_db()
    seed_repos(engine, 20)
    clock = FakeClock()
    controller = AdaptiveController(
        AdaptiveConfig(initial_window=1, min_window=1, max_window=1, initial_batch=10),
        now=clock,
        rng=lambda: 0.0,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        data: dict[str, object] = {}
        for alias, _owner, name in ALIAS_RE.findall(body["query"]):
            data[alias] = graphql_node(int(name.removeprefix("repo")))
        return httpx.Response(
            200,
            json={"data": data},
            headers={"x-ratelimit-remaining": "250", "x-ratelimit-reset": "1600"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = refresh_repos_batched(
        engine, client, rows_for(20), adaptive=controller, now=clock, sleep=clock.advance
    )
    assert stats.refreshed == 10
    assert len(stats.unresolved) == 10
    assert all(reason.startswith("deferred:") for reason in stats.unresolved.values())
    assert stats.batch["deferred"] == 10
    assert stats.batch["adaptive"]["pacer_remaining"] == 250
```

- [ ] **Step 2: Run to fail**

Run: `.venv\Scripts\python.exe -m pytest tests/integration/test_adaptive_hydration.py -q`
Expected: FAIL — `TypeError: refresh_repos_batched() got an unexpected keyword argument 'adaptive'`.

Append to `tests/integration/test_runner.py` (add `import threading` and `import time` to its import block first):

```python
def test_run_filter_adaptive_is_off_by_default(clean: Engine):
    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 1)
            return page_response([repo_item(1)])
        if path == "/graphql":
            return graphql_batch_response(request, {"owner1/repo1": repo_item(1)})
        raise AssertionError(f"unexpected path {path}")

    client, requests = scripted(handler)
    payload = run_filter(make_deps(clean, client), spec_for(q="language:python"))
    hydration = payload.field_stats["graphql"]["hydration"]
    assert hydration["adaptive"] == {}
    assert hydration["deferred"] == 0
    assert len(requests) == 3  # count + page + hydration batch, unchanged


def test_run_filter_adaptive_flag_reports_controller_state(clean: Engine, monkeypatch):
    monkeypatch.setenv("GITCRAWL_ADAPTIVE", "1")

    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 1)
            return page_response([repo_item(1)])
        if path == "/graphql":
            body = graphql_batch_response(request, {"owner1/repo1": repo_item(1)})
            return httpx.Response(
                200,
                json=json.loads(body.content),
                headers={
                    "x-ratelimit-remaining": "4999",
                    "x-ratelimit-reset": str(int(time.time()) + 600),
                },
            )
        raise AssertionError(f"unexpected path {path}")

    client, _requests = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python"),
        config=RunnerConfig(concurrency=32),  # the hydration envelope must exceed MIN_WINDOW
    )
    adaptive = payload.field_stats["graphql"]["hydration"]["adaptive"]
    assert adaptive["window"] == 20
    assert adaptive["batch"] == 20
    assert adaptive["drops"] == 0
    assert adaptive["deferred"] == 0


def test_run_filter_warns_when_the_reserve_defers_hydration(clean: Engine, monkeypatch):
    monkeypatch.setenv("GITCRAWL_ADAPTIVE", "1")
    barrier = threading.Barrier(20, timeout=10)

    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 25)
            return page_response([repo_item(index) for index in range(1, 26)])
        if path == "/graphql":
            body = json.loads(request.content)
            matches = GRAPHQL_REPO_ALIAS_RE.findall(body["query"])
            barrier.wait()
            data: dict[str, object] = {}
            for alias, _login, name in matches:
                repo_id = int(name.removeprefix("repo"))
                data[alias] = rest_item_to_graphql_node(repo_item(repo_id))
            return httpx.Response(
                200,
                json={"data": data},
                headers={
                    "x-ratelimit-remaining": "100",
                    "x-ratelimit-reset": str(int(time.time()) + 600),
                },
            )
        raise AssertionError(f"unexpected path {path}")

    client, requests = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python"),
        config=RunnerConfig(graphql_batch_size=1, max_hydrate=25, concurrency=32),
    )
    hydration = payload.field_stats["graphql"]["hydration"]
    hydration_requests = [
        request
        for request in requests
        if path_of(request) == "/graphql" and not is_search_request(request)
    ]
    assert len(hydration_requests) == 20  # the window sized the first wave
    assert hydration["deferred"] == 5
    assert any("deferred" in warning for warning in payload.warnings)
    assert payload.incomplete is True
```

- [ ] **Step 3: Implement the wiring**

`src/hydrate/tail.py` — add imports (`from contextlib import nullcontext`, `from limiter.adaptive import AdaptiveController`), add the parameter and the ceiling wrap around the `fetch_batch` call:

```python
def refresh_repos_batched(
    engine: Engine,
    client: httpx.Client,
    rows: Sequence[Mapping[str, object]],
    *,
    limiter: BucketLimiter | None = None,
    token_id: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
    on_response: Callable[[httpx.Response, float], None] | None = None,
    deadline: Deadline | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    allow_requests: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
    concurrency: int = 1,
    clock: Callable[[], float] = time.perf_counter,
    apply_batch_size: int = _APPLY_BATCH,
    on_apply_progress: Callable[[int, int], None] | None = None,
    adaptive: AdaptiveController | None = None,
) -> RefreshStats:
```

Just before the existing `outcome = fetch_batch(` call (inside the `try:`), add:

```python
        ceiling = (
            limiter.bound_concurrency(adaptive.ceiling)
            if adaptive is not None and limiter is not None
            else nullcontext()
        )
        with ceiling:
            outcome = fetch_batch(
                RepoDetailsAdapter(dict(candidates), batch_size=batch_size),
                [key for key, _ in candidates],
                client=client,
                limiter=limiter,
                token_id=token_id,
                fallback=fallback,
                deadline=deadline,
                on_response=on_response,
                sleep=sleep,
                now=now,
                jitter=jitter,
                allow_requests=allow_requests,
                on_progress=on_progress,
                on_resolved=on_resolved,
                concurrency=concurrency,
                adaptive=adaptive,
            )
```

indent the previous `outcome = fetch_batch(...)` body into the `with` (do not duplicate it).

`src/serve/runner.py` — add `from limiter.adaptive import (MAX_BATCH, MAX_WINDOW, MIN_WINDOW, AdaptiveConfig, AdaptiveController, adaptive_enabled)` and change `_hydrate`:

```python
def _hydrate(
    deps: Deps,
    rows: list[dict],
    cfg: RunnerConfig,
    hook: Callable[[httpx.Response, float], None],
    on_progress: Callable[[int, int], None] | None = None,
    on_apply_progress: Callable[[int, int], None] | None = None,
) -> RefreshStats:
    candidates = rows[: cfg.max_hydrate]
    if not candidates:
        return RefreshStats()
    adaptive = None
    if adaptive_enabled():
        ceiling = min(MAX_WINDOW, cfg.concurrency)
        if ceiling >= MIN_WINDOW:
            adaptive = AdaptiveController(
                AdaptiveConfig(initial_window=min(20, ceiling), max_window=ceiling),
                now=time.time,
                initial_batch=min(cfg.graphql_batch_size, MAX_BATCH),
            )
    return refresh_repos_batched(
        deps.engine,
        deps.client,
        candidates,
        limiter=deps.limiter,
        token_id=deps.token_fp,
        on_response=hook,
        deadline=deps.limiter.deadline if deps.limiter is not None else None,
        batch_size=cfg.graphql_batch_size,
        allow_requests=cfg.graphql_batch,
        on_progress=on_progress,
        on_apply_progress=on_apply_progress,
        concurrency=cfg.concurrency,
        adaptive=adaptive,
    )
```

`src/serve/runner.py` — after the existing unresolved warning block (currently `if hydration.unresolved: ...`, around line 886-893), add:

```python
    deferred = hydration.batch.get("deferred", 0)
    if isinstance(deferred, int) and deferred > 0:
        warnings.append(
            f"{deferred} repo(s) deferred: the GraphQL point reserve was reached; "
            "results are incomplete"
        )
```

`docs/environment.md` — add a row to the environment-variable table (after the `GITHUB_TOKEN` row, around line 121) and a bullet under the corpus-profile notes (around line 235):

```markdown
| `GITCRAWL_ADAPTIVE` | `1` enables the adaptive rate controller (default off) | hydration batching (`limiter/adaptive`) |
```

```markdown
- `GITCRAWL_ADAPTIVE=1` turns on the three-loop adaptive controller for hydration: the AIMD window starts at 20 (bounds 8-48), the batch guard starts at the configured batch size capped at 29 (bounds 10-29), the quota pacer paces to `(remaining - 400) / seconds-to-reset` with a burst of 4, and any 403/429/502/504/rate-limit signal pauses the pool. It emits `field_stats.graphql.hydration.adaptive` and `.deferred`; keep it off until a soak run validates it.
```

- [ ] **Step 4: Run the hydration, runner, and batch suites**

Run: `.venv\Scripts\python.exe -m pytest tests/integration/test_adaptive_hydration.py tests/integration/test_hydrate_batch.py tests/integration/test_runner.py tests/unit/test_graphql_batch_adaptive.py tests/unit/test_graphql_batch_core.py tests/unit/test_adaptive.py -q`
Expected: PASS.

- [ ] **Step 5: Full gates, then commit**

```bash
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m black --check src tests
.venv\Scripts\python.exe -m mypy
git add src/hydrate/tail.py src/serve/runner.py docs/environment.md tests/integration/test_adaptive_hydration.py tests/integration/test_runner.py
git commit -m "feat(runner): env-gated adaptive hydration wiring and telemetry"
```

---

### Task 4: Soak-style live validation (manual, network-gated)

**Files:**
- Create: `docs/findings/2026-10-09-adaptive-soak.md`
- No code changes.

**Interfaces:** Consumes the `field_stats.graphql.hydration.adaptive` / `.deferred` keys and the run bundle from Tasks 1-3.

- [ ] **Step 1: Preconditions**

Full suite green (Task 3 Step 5), DB + Redis up per `docs/environment.md`, one token configured, corpus preset loaded at `/settings` (`max_shards=1000`, `max_candidates=100000`, `max_hydrate=100000`, `max_enrich=100000`, `request_deadline_seconds=86400`, `graphql_batch_size` at its current default, `limiter_max_concurrent` at its current default). Set the kill switch only for this run:

```powershell
$env:GITCRAWL_ADAPTIVE = "1"
```

Restart the server process so the env var is picked up (the flag is read per `_hydrate` call, but restart avoids stale env in the scheduled task).

- [ ] **Step 2: Run a bounded soak**

Trigger the corpus filter used by the profiled run (`language:rust stars:>=4 created:>=2025-02-24`, roughly 39k repos) once, and keep the run bundle. Then trigger one repeat run to check refresh behavior. Do not change settings between the two runs.

- [ ] **Step 3: Watch the scenario table live** (this is the manual verification)

| # | Signal | Read from | Healthy | Stop and investigate if |
|---|---|---|---|---|
| 1 | Window `W` | `field_stats.graphql.hydration.adaptive.window` | starts 20; climbs toward the envelope (32 with Theme 2's corpus preset) on a clean run | any `drops > 0`, or `W` pinned at 8 |
| 2 | Batch `B` | `...adaptive.batch` | stable at the configured size (20 today, 29 after Theme 2) | `B` at the floor 10 for more than ~2 minutes |
| 3 | Drops | `...adaptive.drops` | 0 | any drop; capture the bundle and stop |
| 4 | Pauses | `...adaptive.pauses`, `paused_for` | 0 | any pause, or total pause time > 5 minutes |
| 5 | Pacer | `...adaptive.pacer_remaining`, `pacer_rate` | `remaining` stays above the 400 reserve; rate ~1.4 points/s early in the window | `remaining` pinned at/below 400, or rate 0 before 80% of the run |
| 6 | Deferred | `...deferred` plus the "deferred: the GraphQL point reserve was reached" warning | 0 | any deferral |
| 7 | Per-repo outcomes | `...hydration.unresolved` and run warnings | `{}` / no unresolved warning | any repo unresolved without an explicit reason |
| 8 | Wall time | run bundle `timings` | at or below the recorded 10-minute baseline for 39k | > 12 minutes with zero drops (pacer/gate cost too high) |
| 9 | Audit 403/429 | `audit_log` / `/system` performance page | 0 | any 403/429 row |

- [ ] **Step 4: Write `docs/findings/2026-10-09-adaptive-soak.md`**

Record: date, filter, measured wall time and stage timings, points spent (from the bundle/audit), the final `adaptive` snapshot, drops/pauses/deferred, every warning, and the watch-table readings for both runs. State the decision explicitly (keep the flag off, or propose flipping the default in a follow-up with the evidence).

- [ ] **Step 5: Commit the findings**

```bash
git add docs/findings/2026-10-09-adaptive-soak.md
git commit -m "docs(findings): adaptive controller soak validation"
```

## Follow-ups (deliberately out of scope)

- Discovery-side adaptation: `src/discover/graphql_search.py` keeps its current page halving (`100 -> 50 -> 25`) and fixed concurrency; a later plan can share the controller's pacer there.
- Enrichment adapters (files/owners) still use the fixed Redis slot cap; no controller passes through them.
- Flipping `GITCRAWL_ADAPTIVE` on by default requires the Task 4 evidence plus the Theme 2 telemetry (`rl_used`, run id) to validate the 1-point-per-batch assumption.

## Self-review notes

- **Spec coverage:** §7.3 Loop 1 -> Task 1 `batch` + Task 2 `guard`; Loop 2 -> Task 1 `window` + Task 2 gate; Loop 3 -> Task 1 pacer + Task 2 reserve deferral; Pause rule -> Task 1 `record_batch`/`pause_remaining` + Task 2 wait loop + `_pause_bucket`; §8 rec 4 wiring -> Task 3; §9 "stops climbing after the first no" -> cooldown/dwell; telemetry and kill switch -> Task 3; manual soak -> Task 4.
- **Guarantees:** Edit C preserves the existing fold verbatim (values first, per-key failures, split-only-failures, REST fallback, deadline/cancellation); adaptive only adds signals, a gate, re-slicing, and the deferral path.
- **Type consistency:** `AdaptiveController` names/signatures are identical in Tasks 1-4; `fetch_batch(..., adaptive=...)`, `refresh_repos_batched(..., adaptive=...)`, `BatchStats.deferred`/`.adaptive`, and `adaptive_enabled()` are used exactly as produced. The controller clock is the same callable as `fetch_batch`'s `now` at every construction site (`_hydrate` uses `time.time`; tests share a `FakeClock`).
- **Interpretations to flag at review:** (a) "halve on any drop" is implemented literally, so a wave of sibling 403s can drive `W` to 8 quickly; growth only after a clean 300 s dwell at near-saturation. (b) The pool pause gates new dispatch only; requests already inside `httpx` cannot be cancelled, so the anti-octokit guarantee is "no new knocks", not "abort the wave". (c) The pacer assumes one point per batch, so it is only valid while `B <= 29`; Theme 2 telemetry (`field_stats["points"]`, `BatchStats.points_remaining`) should confirm without any code dependency. (d) `_pause_bucket`'s read-then-write is not atomic (single-process runs only). (e) A run shorter than `dwell_seconds` never grows `W`; a 10-minute run expects about two growth steps. (f) The task brief's hard bound is 48; this plan narrows the *effective* envelope to Theme 2's `cfg.concurrency` (`min(limiter_max_concurrent, HYDRATION_CONCURRENCY_CEILING=32)`) so the controller only reduces within the operator's setting, matching Theme 2's cross-plan contract. Phase 1's ceiling is therefore 32; `MAX_WINDOW=48` is unreachable until Theme 2's `HYDRATION_CONCURRENCY_CEILING` is itself raised (40–48 is a later lift backed by soak evidence) — raising the corpus preset alone cannot exceed 32.
- **Merge check against Theme 2:** its Self-review contract expects the live window built "at `HYDRATION_CONCURRENCY_CEILING` by replacing the static `limit(concurrency)` submission gate in `fetch_batch`". Edit C does exactly that (`gate = adaptive.window`, pool = `adaptive.ceiling`), and `_hydrate` derives the ceiling from `cfg.concurrency`, which Theme 2 sets to `min(limiter_max_concurrent, 32)`.
- **Placeholder scan:** no TBD/TODO; every test and implementation step shows real code.

