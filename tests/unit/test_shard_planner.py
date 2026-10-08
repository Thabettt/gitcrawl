from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import pytest

from scheduler.shard_planner import ShardSpec, plan_shards

MIN_DATE = date(2020, 1, 1)
NOW = datetime(2020, 1, 10, 12, 0, tzinfo=UTC)
WINDOW_DAYS = 10
RANGE_RE = re.compile(r"created:(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})")


def uniform_count(per_day):
    def count_fn(query):
        match = RANGE_RE.search(query)
        if match is None:
            return per_day * WINDOW_DAYS
        start = date.fromisoformat(match.group(1))
        end = date.fromisoformat(match.group(2))
        return per_day * ((end - start).days + 1)

    return count_fn


def probe_from(count_fn):
    calls = []

    def probe(queries):
        calls.append(list(queries))
        return [count_fn(query) for query in queries]

    return probe, calls


def all_probed(calls):
    return [query for batch in calls for query in batch]


def test_zero_root_count_returns_no_shards():
    probe, calls = probe_from(lambda query: 0)

    assert plan_shards("language:python", probe, now=NOW) == ([], False)
    assert calls == [["language:python"]]


def test_under_limit_root_returns_single_unbounded_shard():
    probe, calls = probe_from(uniform_count(per_day=99))

    specs, capped = plan_shards("language:go stars:>100", probe, now=NOW)
    assert specs == [
        ShardSpec(
            query="language:go stars:>100",
            range_start=None,
            range_end=None,
            total_count=990,
        )
    ]
    assert capped is False
    assert calls == [["language:go stars:>100"]]


def test_over_limit_root_bisects_into_fetchable_gap_free_leaves():
    probe, _ = probe_from(uniform_count(per_day=500))

    specs, capped = plan_shards("language:python", probe, min_date=MIN_DATE, now=NOW)
    assert capped is False
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
    probe_a, _ = probe_from(uniform_count(per_day=500))
    probe_b, _ = probe_from(uniform_count(per_day=500))

    assert plan_shards("q", probe_a, min_date=MIN_DATE, now=NOW) == plan_shards(
        "q", probe_b, min_date=MIN_DATE, now=NOW
    )


def test_root_count_override_skips_root_probe():
    probe, calls = probe_from(uniform_count(per_day=500))

    specs, capped = plan_shards("q", probe, min_date=MIN_DATE, root_count=5000, now=NOW)
    assert capped is False
    assert sum(spec.total_count for spec in specs) == 5000
    assert "q" not in all_probed(calls)


def test_scan_target_forces_split_below_max_fetchable():
    probe, _ = probe_from(uniform_count(per_day=50))

    specs, capped = plan_shards("q", probe, scan_target=300, min_date=MIN_DATE, now=NOW)
    assert capped is False
    assert len(specs) == 2
    assert sum(spec.total_count for spec in specs) == 500
    assert all(spec.total_count <= 300 for spec in specs)
    assert all(not spec.oversized for spec in specs)


def test_single_day_over_limit_is_marked_oversized():
    probe, _ = probe_from(lambda query: 1500)

    specs, capped = plan_shards(
        "q", probe, min_date=date(2020, 1, 1), now=datetime(2020, 1, 5, tzinfo=UTC)
    )
    assert capped is False
    assert len(specs) == 5
    assert [spec.range_start.date() for spec in specs] == [
        date(2020, 1, day) for day in range(1, 6)
    ]
    for spec in specs:
        assert spec.range_start.date() == spec.range_end.date()
        assert spec.total_count == 1500
        assert spec.oversized is True


def test_single_day_window_over_limit_is_one_oversized_leaf():
    probe, _ = probe_from(lambda query: 2000)

    specs, capped = plan_shards(
        "q", probe, min_date=date(2020, 1, 5), now=datetime(2020, 1, 5, tzinfo=UTC)
    )
    assert capped is False
    assert len(specs) == 1
    assert specs[0].oversized is True
    assert specs[0].range_start.date() == date(2020, 1, 5)
    assert specs[0].range_end.date() == date(2020, 1, 5)


def test_probes_are_clamped_to_the_configured_window():
    probe, calls = probe_from(lambda query: 1500)

    plan_shards("q", probe, min_date=date(2020, 1, 3), now=datetime(2020, 1, 5, tzinfo=UTC))
    probed = all_probed(calls)
    assert probed[0] == "q"
    ranged = probed[1:]
    assert ranged
    for query in ranged:
        match = RANGE_RE.search(query)
        assert match is not None
        start = date.fromisoformat(match.group(1))
        end = date.fromisoformat(match.group(2))
        assert date(2020, 1, 3) <= start <= end <= date(2020, 1, 5)


def test_count_fn_exception_propagates():
    def probe(queries):
        raise RuntimeError("probe failed")

    with pytest.raises(RuntimeError, match="probe failed"):
        plan_shards("q", probe, now=NOW)


def test_split_created_strips_tokens_and_keeps_exclusions():
    from scheduler.shard_planner import split_created

    base, windows = split_created("language:rust created:>=2025-02-24 stars:>=4")
    assert base == "language:rust stars:>=4"
    assert windows == [(date(2025, 2, 24), None)]

    base, windows = split_created("language:rust -created:2024-01-01")
    assert base == "language:rust -created:2024-01-01"
    assert windows == []


def test_replace_created_swaps_the_token_instead_of_appending():
    from scheduler.shard_planner import replace_created

    replaced = replace_created(
        "language:rust created:>=2025-02-24", date(2025, 3, 1), date(2025, 3, 31)
    )
    assert replaced == "language:rust created:2025-03-01..2025-03-31"
    assert replaced.count("created:") == 1


def test_plan_replaces_a_user_created_bound_everywhere():
    probe, _ = probe_from(lambda query: 5000)

    specs, capped = plan_shards(
        "language:rust created:>=2025-02-24",
        probe,
        max_fetchable=1000,
        root_count=5000,
        now=datetime(2026, 10, 6, tzinfo=UTC),
    )
    assert specs
    assert capped is False
    for spec in specs:
        assert spec.query.count("created:") == 1
        assert "created:>=" not in spec.query
        assert spec.range_start is not None and spec.range_end is not None
        assert spec.range_start.date() >= date(2025, 2, 24)
        assert spec.range_end.date() <= date(2026, 10, 6)


def test_plan_keeps_the_original_query_when_the_root_is_fetchable():
    def probe(queries):
        raise AssertionError("a fetchable root must not be probed")

    specs, capped = plan_shards("language:rust created:>=2025-02-24", probe, root_count=50, now=NOW)
    assert specs == [
        ShardSpec(
            query="language:rust created:>=2025-02-24",
            range_start=None,
            range_end=None,
            total_count=50,
        )
    ]
    assert capped is False


def test_plan_stays_inside_a_closed_user_range():
    probe, _ = probe_from(lambda query: 5000)

    specs, _ = plan_shards(
        "language:rust created:2024-01-01..2024-01-31",
        probe,
        max_fetchable=1000,
        root_count=5000,
        now=datetime(2026, 10, 6, tzinfo=UTC),
    )
    assert specs
    for spec in specs:
        assert spec.range_start.date() >= date(2024, 1, 1)
        assert spec.range_end.date() <= date(2024, 1, 31)


def test_plan_covers_each_window_of_disjoint_created_constraints():
    probe, _ = probe_from(lambda query: 5000)

    specs, _ = plan_shards(
        "language:rust created:2024-01-01..2024-01-31 created:>=2026-01-01",
        probe,
        max_fetchable=1000,
        root_count=5000,
        now=datetime(2026, 10, 6, tzinfo=UTC),
    )
    assert specs
    for spec in specs:
        start = spec.range_start.date()
        end = spec.range_end.date()
        in_first = date(2024, 1, 1) <= start and end <= date(2024, 1, 31)
        in_second = date(2026, 1, 1) <= start and end <= date(2026, 10, 6)
        assert in_first or in_second


def test_plan_rejects_an_empty_created_range():
    probe, _ = probe_from(lambda query: 5000)

    with pytest.raises(ValueError):
        plan_shards(
            "language:rust created:2025-03-01..2025-01-01",
            probe,
            root_count=5000,
            now=datetime(2026, 10, 6, tzinfo=UTC),
        )


def test_probes_are_batched_by_the_batch_size():
    days = [MIN_DATE + timedelta(days=2 * index) for index in range(41)]
    query = "q " + " ".join(f"created:{day.isoformat()}" for day in days)
    probe, calls = probe_from(uniform_count(per_day=500))

    specs, capped = plan_shards(
        query, probe, min_date=MIN_DATE, now=datetime(2020, 6, 1, tzinfo=UTC)
    )
    assert capped is False
    assert len(specs) == 41
    assert all(spec.total_count == 500 for spec in specs)
    assert max(len(batch) for batch in calls) <= 20
    assert any(len(batch) == 20 for batch in calls)


def test_max_shards_caps_the_plan_to_the_oldest_leaves():
    probe, _ = probe_from(uniform_count(per_day=500))

    specs, capped = plan_shards(
        "q", probe, min_date=MIN_DATE, max_shards=3, now=datetime(2020, 1, 4, tzinfo=UTC)
    )
    assert capped is True
    assert [spec.range_start.date() for spec in specs] == [
        MIN_DATE,
        MIN_DATE + timedelta(days=1),
        MIN_DATE + timedelta(days=2),
    ]


def test_plan_capped_false_on_exact_fill():
    probe, _ = probe_from(uniform_count(per_day=500))

    specs, capped = plan_shards(
        "q", probe, min_date=MIN_DATE, max_shards=3, now=datetime(2020, 1, 3, tzinfo=UTC)
    )
    assert capped is False
    assert [spec.range_start.date() for spec in specs] == [
        MIN_DATE,
        MIN_DATE + timedelta(days=1),
        MIN_DATE + timedelta(days=2),
    ]
