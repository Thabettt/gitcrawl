from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import pytest

from scheduler.shard_planner import ShardPlanner, ShardSpec

MIN_DATE = date(2020, 1, 1)
NOW = datetime(2020, 1, 10, 12, 0, tzinfo=UTC)
WINDOW_DAYS = 10
RANGE_RE = re.compile(r"created:(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})")


def uniform_count(per_day):
    calls = []

    def count_fn(query):
        calls.append(query)
        match = RANGE_RE.search(query)
        if match is None:
            return per_day * WINDOW_DAYS
        start = date.fromisoformat(match.group(1))
        end = date.fromisoformat(match.group(2))
        return per_day * ((end - start).days + 1)

    return count_fn, calls


def test_zero_root_count_returns_no_shards():
    calls = []

    def count_fn(query):
        calls.append(query)
        return 0

    assert ShardPlanner(count_fn).plan("language:python", now=NOW) == []
    assert calls == ["language:python"]


def test_under_limit_root_returns_single_unbounded_shard():
    count_fn, calls = uniform_count(per_day=99)
    specs = ShardPlanner(count_fn).plan("language:go stars:>100", now=NOW)
    assert specs == [
        ShardSpec(
            query="language:go stars:>100",
            range_start=None,
            range_end=None,
            total_count=990,
        )
    ]
    assert calls == ["language:go stars:>100"]


def test_over_limit_root_bisects_into_fetchable_gap_free_leaves():
    count_fn, _ = uniform_count(per_day=500)
    planner = ShardPlanner(count_fn, min_date=MIN_DATE)
    specs = planner.plan("language:python", now=NOW)
    assert sum(spec.total_count for spec in specs) == 5000
    assert all(spec.total_count < 1000 for spec in specs)
    assert all(not spec.oversized for spec in specs)
    spans = [(spec.range_start.date(), spec.range_end.date()) for spec in specs]
    assert spans == sorted(spans)
    assert spans[0][0] == MIN_DATE
    assert spans[-1][1] == NOW.date()
    for (_, previous_end), (next_start, _) in zip(spans[:-1], spans[1:], strict=True):
        assert next_start == previous_end + timedelta(days=1)
    for spec in specs:
        match = RANGE_RE.search(spec.query)
        assert match is not None
        assert date.fromisoformat(match.group(1)) == spec.range_start.date()
        assert date.fromisoformat(match.group(2)) == spec.range_end.date()
        assert spec.range_start.tzinfo is UTC
        assert spec.range_end.tzinfo is UTC


def test_plan_is_deterministic():
    count_a, _ = uniform_count(per_day=500)
    count_b, _ = uniform_count(per_day=500)
    assert ShardPlanner(count_a, min_date=MIN_DATE).plan("q", now=NOW) == ShardPlanner(
        count_b, min_date=MIN_DATE
    ).plan("q", now=NOW)


def test_scan_target_forces_split_below_max_fetchable():
    count_fn, _ = uniform_count(per_day=50)
    planner = ShardPlanner(count_fn, scan_target=300, min_date=MIN_DATE)
    specs = planner.plan("q", now=NOW)
    assert len(specs) == 2
    assert sum(spec.total_count for spec in specs) == 500
    assert all(spec.total_count <= 300 for spec in specs)
    assert all(not spec.oversized for spec in specs)


def test_single_day_over_limit_is_marked_oversized():
    def count_fn(query):
        return 1500

    planner = ShardPlanner(count_fn, min_date=date(2020, 1, 1))
    specs = planner.plan("q", now=datetime(2020, 1, 5, tzinfo=UTC))
    assert len(specs) == 5
    assert [spec.range_start.date() for spec in specs] == [
        date(2020, 1, day) for day in range(1, 6)
    ]
    for spec in specs:
        assert spec.range_start.date() == spec.range_end.date()
        assert spec.total_count == 1500
        assert spec.oversized is True


def test_single_day_window_over_limit_is_one_oversized_leaf():
    def count_fn(query):
        return 2000

    planner = ShardPlanner(count_fn, min_date=date(2020, 1, 5))
    specs = planner.plan("q", now=datetime(2020, 1, 5, tzinfo=UTC))
    assert len(specs) == 1
    assert specs[0].oversized is True
    assert specs[0].range_start.date() == date(2020, 1, 5)
    assert specs[0].range_end.date() == date(2020, 1, 5)


def test_probes_are_clamped_to_the_configured_window():
    calls = []

    def count_fn(query):
        calls.append(query)
        return 1500

    planner = ShardPlanner(count_fn, min_date=date(2020, 1, 3))
    planner.plan("q", now=datetime(2020, 1, 5, tzinfo=UTC))
    assert calls[0] == "q"
    ranged_calls = calls[1:]
    assert ranged_calls
    for query in ranged_calls:
        match = RANGE_RE.search(query)
        assert match is not None
        start = date.fromisoformat(match.group(1))
        end = date.fromisoformat(match.group(2))
        assert date(2020, 1, 3) <= start <= end <= date(2020, 1, 5)


def test_count_fn_exception_propagates():
    def count_fn(query):
        raise RuntimeError("probe failed")

    with pytest.raises(RuntimeError, match="probe failed"):
        ShardPlanner(count_fn).plan("q", now=NOW)
