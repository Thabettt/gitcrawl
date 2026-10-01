from __future__ import annotations

import threading
import time

from enrich.cloner import CloneProgress
from serve.runs import CloneRegistry


def running() -> CloneProgress:
    return CloneProgress(status="running", total=2, completed=0, failed=0)


def done() -> CloneProgress:
    return CloneProgress(status="done", total=2, completed=2, failed=0)


def test_claim_constructs_once_and_returns_the_same_object_to_racers():
    registry = CloneRegistry()
    created: list[CloneProgress] = []
    created_lock = threading.Lock()

    def factory() -> CloneProgress:
        time.sleep(0.05)
        progress = running()
        with created_lock:
            created.append(progress)
        return progress

    results: list[tuple[CloneProgress, bool]] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait(5)
        result = registry.claim(7, factory)
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert len(created) == 1
    assert [progress for progress, _ in results] == [created[0]] * 8
    assert [claimed for _, claimed in results].count(True) == 1


def test_claim_starts_a_new_entry_once_the_previous_one_is_terminal():
    registry = CloneRegistry()
    first, claimed = registry.claim(1, done)
    assert claimed is True
    second, claimed_again = registry.claim(1, running)
    assert claimed_again is True
    assert second is not first


def test_registry_evicts_terminal_entries_by_count_and_keeps_running_ones():
    registry = CloneRegistry(max_entries=2)
    live = running()
    registry.set(1, live)
    registry.set(2, done())
    registry.set(3, done())

    assert registry.get(1) is live
    assert registry.get(2) is None
    assert registry.get(3) is not None


def test_registry_evicts_terminal_entries_by_idle_age():
    now = {"value": 0.0}
    registry = CloneRegistry(ttl_seconds=10.0, clock=lambda: now["value"])
    registry.set(1, done())

    assert registry.get(1) is not None
    now["value"] = 11.0
    assert registry.get(1) is None
