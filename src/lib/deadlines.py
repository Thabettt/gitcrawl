from __future__ import annotations

import os
import time
from collections.abc import Callable

DEFAULT_REQUEST_DEADLINE_SECONDS = 3600.0
DEFAULT_CLONE_TIMEOUT_SECONDS = 1800.0


class DeadlineExceededError(TimeoutError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"deadline exceeded; retry after {retry_after:.0f}s")
        self.retry_after = float(retry_after)


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def request_deadline_seconds() -> float:
    return _env_seconds("GITCRAWL_REQUEST_DEADLINE_SECONDS", DEFAULT_REQUEST_DEADLINE_SECONDS)


def clone_timeout_seconds() -> float:
    return _env_seconds("GITCRAWL_CLONE_TIMEOUT_SECONDS", DEFAULT_CLONE_TIMEOUT_SECONDS)


class Deadline:
    def __init__(self, seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._expires_at = clock() + seconds

    @property
    def remaining(self) -> float:
        return self._expires_at - self._clock()

    def bound_wait(self, seconds: float) -> None:
        if seconds > self.remaining:
            raise DeadlineExceededError(seconds)
