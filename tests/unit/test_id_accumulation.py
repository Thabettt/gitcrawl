from __future__ import annotations

import time

from discover.pipeline import _collect_ids


def test_collect_ids_appends_in_order_and_deduplicates():
    seen: set[int] = set()
    collected: list[int] = []
    _collect_ids(seen, collected, [{"id": 3}, {"id": 1}, {"id": 3}, {"nope": 1}, {"id": True}])
    assert collected == [3, 1]
    assert seen == {3, 1}


def test_collect_ids_stays_linear_at_100k_items():
    seen: set[int] = set()
    collected: list[int] = []
    items = [{"id": value} for value in range(100_000)]
    started = time.perf_counter()
    _collect_ids(seen, collected, items)
    elapsed = time.perf_counter() - started
    assert len(collected) == 100_000
    # tuple concatenation at this size copies ~40 GB and takes seconds-to-minutes.
    assert elapsed < 1.0
