from __future__ import annotations

import threading


class SecondaryTracker:
    """Consecutive secondary-limit streak plus a lifetime counter for the process."""

    def __init__(self, *, window_seconds: float = 600.0) -> None:
        self._window_seconds = float(window_seconds)
        self._lock = threading.Lock()
        self._streak = 0
        self._last_hit: float | None = None
        self._total = 0

    def hit(self, now: float) -> int:
        with self._lock:
            if (
                self._streak == 0
                or self._last_hit is None
                or now - self._last_hit > self._window_seconds
            ):
                self._streak = 1
            else:
                self._streak += 1
            self._last_hit = now
            self._total += 1
            return self._streak

    def success(self, now: float) -> None:
        with self._lock:
            self._streak = 0

    def total(self) -> int:
        with self._lock:
            return self._total

    def reset(self) -> None:
        with self._lock:
            self._streak = 0
            self._last_hit = None
            self._total = 0


_tracker = SecondaryTracker()


def hit(now: float) -> int:
    return _tracker.hit(now)


def success(now: float) -> None:
    _tracker.success(now)


def total() -> int:
    return _tracker.total()


def reset() -> None:
    _tracker.reset()
