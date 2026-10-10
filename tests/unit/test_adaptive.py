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


def test_note_secondary_halves_the_window_down_to_the_floor() -> None:
    controller = make_controller()
    controller.note_secondary()
    assert controller.window == 10
    controller.note_secondary()
    assert controller.window == 8
    controller.note_secondary()
    assert controller.window == 8


def test_note_secondary_blocks_window_growth_during_the_cooldown() -> None:
    clock = FakeClock()
    controller = make_controller(clock, dwell_seconds=0.0)
    controller.note_secondary()
    clock.advance(30.0)
    controller.record_batch(in_flight=10)
    assert controller.window == 10
    clock.advance(120.0)
    controller.record_batch(in_flight=10)
    assert controller.window == 11


def test_snapshot_carries_the_secondary_hit_count() -> None:
    controller = make_controller()
    assert controller.snapshot()["secondary"] == 0
    controller.note_secondary()
    controller.note_secondary()
    assert controller.snapshot()["secondary"] == 2
