from __future__ import annotations

import threading
import time

import pytest

from serve.payload_cache import RunPayloadCache


def test_ttl_expiry_uses_injected_clock():
    now = {"value": 0.0}
    cache = RunPayloadCache(ttl_seconds=120, clock=lambda: now["value"])
    cache.set("k", "v1")
    now["value"] = 119.0
    assert cache.get("k") == "v1"
    now["value"] = 120.0
    assert cache.get("k") is None


def test_lru_eviction_at_capacity():
    cache = RunPayloadCache(max_entries=2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")
    cache.set("c", 3)
    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3


def test_run_once_collapses_concurrent_misses():
    cache = RunPayloadCache()
    calls = {"n": 0}
    barrier = threading.Barrier(4)

    def producer():
        calls["n"] += 1
        time.sleep(0.05)
        return "payload"

    results: list[str] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        value = cache.run_once("key", producer)
        with lock:
            results.append(value)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls["n"] == 1
    assert results == ["payload"] * 4


def test_run_once_propagates_producer_error_and_clears_in_flight():
    cache = RunPayloadCache()
    attempts = {"n": 0}

    def failing():
        attempts["n"] += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        cache.run_once("key", failing)

    assert cache.run_once("key", lambda: "recovered") == "recovered"
    assert attempts["n"] == 1
