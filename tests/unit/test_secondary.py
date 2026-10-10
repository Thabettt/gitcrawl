from __future__ import annotations

from limiter import secondary
from limiter.secondary import SecondaryTracker


def test_hits_inside_the_window_increment_the_streak():
    tracker = SecondaryTracker(window_seconds=600.0)
    assert tracker.hit(100.0) == 1
    assert tracker.hit(101.0) == 2
    assert tracker.hit(102.0) == 3
    assert tracker.total() == 3


def test_gap_longer_than_the_window_restarts_the_streak():
    tracker = SecondaryTracker(window_seconds=600.0)
    assert tracker.hit(100.0) == 1
    assert tracker.hit(200.0) == 2
    assert tracker.hit(801.0) == 1


def test_gap_equal_to_the_window_still_counts_as_consecutive():
    tracker = SecondaryTracker(window_seconds=600.0)
    assert tracker.hit(100.0) == 1
    assert tracker.hit(700.0) == 2


def test_success_clears_the_streak_but_not_the_total():
    tracker = SecondaryTracker()
    tracker.hit(0.0)
    tracker.hit(1.0)
    tracker.hit(2.0)
    tracker.success(3.0)
    assert tracker.total() == 3
    assert tracker.hit(4.0) == 1
    assert tracker.total() == 4


def test_reset_clears_streak_and_total():
    tracker = SecondaryTracker()
    tracker.hit(0.0)
    tracker.hit(1.0)
    tracker.reset()
    assert tracker.total() == 0
    assert tracker.hit(2.0) == 1


def test_module_functions_delegate_to_the_singleton():
    secondary.reset()
    assert secondary.hit(10.0) == 1
    assert secondary.hit(11.0) == 2
    assert secondary.total() == 2
    secondary.success(12.0)
    assert secondary.total() == 2
    assert secondary.hit(13.0) == 1
    secondary.reset()
    assert secondary.total() == 0
