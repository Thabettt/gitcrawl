from __future__ import annotations

import logging

import pytest

from enrich.segment_executor import (
    Segment,
    SegmentStats,
    execute_segments,
    plan_segments,
)


def test_plan_segments_single_segment_covers_all_ids():
    assert plan_segments([3, 1, 2], segments=1) == [Segment(start=0, end=3)]


def test_plan_segments_partitions_sorted_ids_contiguously():
    segments = plan_segments(list(range(1, 10)), segments=3)

    assert segments == [Segment(0, 3), Segment(3, 6), Segment(6, 9)]
    assert all(
        segments[index].end == segments[index + 1].start for index in range(len(segments) - 1)
    )
    covered = [
        repo_id
        for repo_id in range(1, 10)
        if any(segment.start < repo_id <= segment.end for segment in segments)
    ]
    assert covered == list(range(1, 10))


def test_plan_segments_balances_remainder_toward_earlier_segments():
    assert plan_segments(list(range(1, 11)), segments=3) == [
        Segment(0, 4),
        Segment(4, 7),
        Segment(7, 10),
    ]


def test_plan_segments_handles_sparse_ids():
    assert plan_segments([10, 20, 30], segments=2) == [Segment(9, 20), Segment(20, 30)]


def test_plan_segments_caps_segments_at_unique_ids():
    assert plan_segments([5, 6], segments=5) == [Segment(4, 5), Segment(5, 6)]


def test_plan_segments_deduplicates_ids():
    assert plan_segments([2, 2, 1], segments=1) == [Segment(0, 2)]


def test_plan_segments_empty_ids_yield_no_segments():
    assert plan_segments([], segments=4) == []


def test_plan_segments_requires_at_least_one_segment():
    with pytest.raises(ValueError, match="segments"):
        plan_segments([1], segments=0)


def test_handlers_run_in_mapping_order():
    calls: list[tuple[str, list[int]]] = []

    def first(ids):
        calls.append(("first", list(ids)))
        return list(ids), 1

    def second(ids):
        calls.append(("second", list(ids)))
        return list(ids), 2

    survivors, stats = execute_segments([1, 2], {"first": first, "second": second})

    assert calls == [("first", [1, 2]), ("second", [1, 2])]
    assert survivors == [1, 2]
    assert stats.requeues == 0


def test_stats_count_survivors_and_calls_per_field():
    def first(ids):
        return [repo_id for repo_id in ids if repo_id != 1], 2

    def second(ids):
        return [repo_id for repo_id in ids if repo_id != 2], 3

    survivors, stats = execute_segments([1, 2, 3], {"first": first, "second": second})

    assert survivors == [3]
    assert stats.per_field_sources == {"first": 2, "second": 1}
    assert stats.calls_spent == {"first": 2, "second": 3}
    assert stats.requeues == 0
    assert stats.warnings == []


def test_survivors_first_orders_by_stars_then_pushed_at():
    seen: list[list[int]] = []

    def handler(ids):
        seen.append(list(ids))
        return list(ids), 0

    candidates = [
        (1, 5, "2024-01-01T00:00:00Z"),
        (2, 5, "2024-06-01T00:00:00Z"),
        (3, 9, "2024-01-01T00:00:00Z"),
        (4, 5, None),
    ]

    survivors, _ = execute_segments(candidates, {"x": handler})

    assert seen == [[3, 2, 1, 4]]
    assert survivors == [1, 2, 3, 4]


def test_integer_candidates_keep_input_order_when_metadata_ties():
    seen: list[list[int]] = []

    def handler(ids):
        seen.append(list(ids))
        return list(ids), 0

    survivors, _ = execute_segments([3, 1, 2], {"x": handler}, segments=3)

    assert seen == [[3], [1], [2]]
    assert survivors == [1, 2, 3]


def test_segments_are_processed_survivors_first():
    seen: list[list[int]] = []

    def handler(ids):
        seen.append(list(ids))
        return list(ids), 1

    candidates = [
        (1, 10, "2024-01-01T00:00:00Z"),
        (2, 10, "2024-01-01T00:00:00Z"),
        (3, 100, "2024-01-01T00:00:00Z"),
        (4, 100, "2024-01-01T00:00:00Z"),
    ]

    survivors, stats = execute_segments(candidates, {"x": handler}, segments=2)

    assert seen == [[3, 4], [1, 2]]
    assert survivors == [1, 2, 3, 4]
    assert stats.calls_spent == {"x": 2}


def test_failed_handler_requeues_segment_behind_the_next():
    attempts = {"failures": 0}
    calls: list[list[int]] = []

    def handler(ids):
        calls.append(list(ids))
        if ids == [3, 4] and attempts["failures"] == 0:
            attempts["failures"] += 1
            raise RuntimeError("lane down")
        return list(ids), 1

    candidates = [
        (1, 10, "2024-01-01T00:00:00Z"),
        (2, 10, "2024-01-01T00:00:00Z"),
        (3, 100, "2024-01-01T00:00:00Z"),
        (4, 100, "2024-01-01T00:00:00Z"),
    ]

    survivors, stats = execute_segments(candidates, {"x": handler}, segments=2)

    assert calls == [[3, 4], [1, 2], [3, 4]]
    assert survivors == [1, 2, 3, 4]
    assert stats.requeues == 1
    assert stats.per_field_sources == {"x": 4}
    assert stats.calls_spent == {"x": 2}


def test_exhausted_requeues_drop_the_segment_with_a_warning(caplog):
    def handler(ids):
        if ids == [3, 4]:
            raise RuntimeError("lane down")
        return list(ids), 1

    candidates = [
        (1, 10, "2024-01-01T00:00:00Z"),
        (2, 10, "2024-01-01T00:00:00Z"),
        (3, 100, "2024-01-01T00:00:00Z"),
        (4, 100, "2024-01-01T00:00:00Z"),
    ]

    with caplog.at_level(logging.WARNING, logger="enrich.segment_executor"):
        survivors, stats = execute_segments(candidates, {"x": handler}, segments=2)

    assert survivors == [1, 2]
    assert stats.requeues == 1
    assert stats.per_field_sources == {"x": 2}
    assert stats.calls_spent == {"x": 1}
    assert len(stats.warnings) == 1
    warning = stats.warnings[0]
    assert "dropped" in warning
    assert "`x`" in warning
    assert "(2, 4]" in warning
    assert "2 repo(s)" in warning
    assert any(record.getMessage() == warning for record in caplog.records)


def test_requeue_resumes_at_the_failed_handler():
    calls: list[tuple[str, list[int]]] = []
    state = {"failed": False}

    def first(ids):
        calls.append(("first", list(ids)))
        return list(ids), 0

    def second(ids):
        calls.append(("second", list(ids)))
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("boom")
        return list(ids), 5

    survivors, stats = execute_segments([1], {"first": first, "second": second})

    assert calls == [("first", [1]), ("second", [1]), ("second", [1])]
    assert survivors == [1]
    assert stats.requeues == 1
    assert stats.per_field_sources == {"first": 1, "second": 1}
    assert stats.calls_spent == {"first": 0, "second": 5}


def test_max_requeues_zero_drops_immediately():
    def handler(ids):
        raise RuntimeError("always down")

    survivors, stats = execute_segments([1, 2], {"x": handler}, max_requeues=0)

    assert survivors == []
    assert stats.requeues == 0


def test_empty_candidates_return_empty_results_and_stats():
    survivors, stats = execute_segments([], {"x": lambda ids: (ids, 1)})

    assert survivors == []
    assert stats == SegmentStats(per_field_sources={}, calls_spent={}, requeues=0)


def test_execution_is_deterministic():
    def handler(ids):
        return [repo_id for repo_id in ids if repo_id % 2 == 0], 1

    first = execute_segments([1, 2, 3, 4], {"x": handler}, segments=2)
    second = execute_segments([1, 2, 3, 4], {"x": handler}, segments=2)

    assert first == second


def test_survivors_return_in_ascending_id_order():
    def handler(ids):
        return list(ids), 0

    survivors, _ = execute_segments(
        [(3, 1, None), (1, 3, None), (2, 2, None)],
        {"x": handler},
    )

    assert survivors == [1, 2, 3]


def test_execute_segments_rejects_unknown_order():
    with pytest.raises(ValueError, match="random"):
        execute_segments([1], {}, order="random")


def test_execute_segments_requires_at_least_one_segment():
    with pytest.raises(ValueError, match="segments"):
        execute_segments([1], {}, segments=0)
