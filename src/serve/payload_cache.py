from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future
from typing import TypeVar

from lib.deadlines import DeadlineExceededError

CACHE_TTL_SECONDS = 120.0

T = TypeVar("T")


class RunPayloadCache:
    def __init__(
        self,
        *,
        max_entries: int = 100,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._entries: OrderedDict[str, tuple[float, T]] = OrderedDict()
        self._inflight: dict[str, Future] = {}
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._ttl = ttl_seconds
        self._clock = clock

    def get(self, key: str) -> T | None:
        with self._lock:
            return self._get_locked(key)

    def _get_locked(self, key: str) -> T | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() - entry[0] >= self._ttl:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry[1]

    def set(self, key: str, payload: T) -> None:
        with self._lock:
            self._entries[key] = (self._clock(), payload)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def run_once(self, key: str, producer: Callable[[], T], *, timeout: float | None = None) -> T:
        with self._lock:
            cached = self._get_locked(key)
            if cached is not None:
                return cached
            pending = self._inflight.get(key)
            if pending is None:
                pending = Future()
                self._inflight[key] = pending
                owner = True
            else:
                owner = False
        if not owner:
            try:
                return pending.result(timeout=timeout)
            except DeadlineExceededError:
                raise
            except TimeoutError as exc:
                raise DeadlineExceededError(timeout or 0.0) from exc
        try:
            value = producer()
        except BaseException as exc:
            with self._lock:
                self._inflight.pop(key, None)
            pending.set_exception(exc)
            raise
        self.set(key, value)
        with self._lock:
            self._inflight.pop(key, None)
        pending.set_result(value)
        return value
