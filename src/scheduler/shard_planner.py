from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

_MAX_DEPTH = 40


@dataclass(frozen=True)
class ShardSpec:
    query: str
    range_start: datetime | None
    range_end: datetime | None
    total_count: int
    oversized: bool = False


class ShardPlanner:
    def __init__(
        self,
        count_fn: Callable[[str], int],
        *,
        max_fetchable: int = 1000,
        scan_target: int = 4000,
        min_date: date = date(2008, 1, 1),
    ) -> None:
        self._count_fn = count_fn
        self._max_fetchable = max_fetchable
        self._scan_target = scan_target
        self._min_date = min_date
        self._window_end = min_date

    def plan(self, query: str, *, now: datetime | None = None) -> list[ShardSpec]:
        current = now if now is not None else datetime.now(UTC)
        self._window_end = current.date()
        root = self._count_fn(query)
        if root == 0:
            return []
        if self._fetchable(root):
            return [ShardSpec(query=query, range_start=None, range_end=None, total_count=root)]
        if self._window_end < self._min_date:
            raise ValueError("min_date is after now; there is no window to bisect")
        return self._bisect(query, self._min_date, self._window_end, root, 0)

    def _fetchable(self, count: int) -> bool:
        return count < self._max_fetchable and count <= self._scan_target

    def _bisect(
        self, query: str, start: date, end: date, count: int, depth: int
    ) -> list[ShardSpec]:
        if depth > _MAX_DEPTH:
            raise RuntimeError("shard planner exceeded the recursion depth cap")
        if start == end:
            return [self._leaf(query, start, end, count, oversized=not self._fetchable(count))]
        middle = start + (end - start) // 2
        leaves: list[ShardSpec] = []
        for half_start, half_end in ((start, middle), (middle + timedelta(days=1), end)):
            bounded_start = max(half_start, self._min_date)
            bounded_end = min(half_end, self._window_end)
            if bounded_start > bounded_end:
                continue
            half_count = self._count_fn(self._range_query(query, bounded_start, bounded_end))
            if half_count == 0:
                continue
            if self._fetchable(half_count):
                leaves.append(self._leaf(query, bounded_start, bounded_end, half_count))
            else:
                leaves.extend(
                    self._bisect(query, bounded_start, bounded_end, half_count, depth + 1)
                )
        return leaves

    def _leaf(
        self, query: str, start: date, end: date, count: int, *, oversized: bool = False
    ) -> ShardSpec:
        return ShardSpec(
            query=self._range_query(query, start, end),
            range_start=datetime.combine(start, time.min, tzinfo=UTC),
            range_end=datetime.combine(end, time.max, tzinfo=UTC),
            total_count=count,
            oversized=oversized,
        )

    @staticmethod
    def _range_query(query: str, start: date, end: date) -> str:
        return f"{query} created:{start.isoformat()}..{end.isoformat()}"
