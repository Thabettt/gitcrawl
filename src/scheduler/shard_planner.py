from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

_MAX_DEPTH = 40
_CREATED_TOKEN = re.compile(r"(?<!\S)created:(\S+)", re.IGNORECASE)


@dataclass(frozen=True)
class ShardSpec:
    query: str
    range_start: datetime | None
    range_end: datetime | None
    total_count: int
    oversized: bool = False


def _parse_created_date(text: str) -> date:
    head = text.split("T", 1)[0].split("t", 1)[0]
    return date.fromisoformat(head)


def _created_window(token: str) -> tuple[date | None, date | None]:
    value = token.strip()
    if not value:
        raise ValueError("created: requires a value")
    if ".." in value:
        left, _, right = value.partition("..")
        low = _parse_created_date(left) if left and left != "*" else None
        high = _parse_created_date(right) if right and right != "*" else None
    elif value.startswith(">="):
        low, high = _parse_created_date(value[2:]), None
    elif value.startswith(">"):
        low, high = _parse_created_date(value[1:]) + timedelta(days=1), None
    elif value.startswith("<="):
        low, high = None, _parse_created_date(value[2:])
    elif value.startswith("<"):
        low, high = None, _parse_created_date(value[1:]) - timedelta(days=1)
    else:
        low = high = _parse_created_date(value)
    if low is not None and high is not None and low > high:
        raise ValueError(f"created:{value} is an empty range")
    return low, high


def split_created(query: str) -> tuple[str, list[tuple[date | None, date | None]]]:
    """Split a query into (base without positive created tokens, inclusive windows).

    GitHub persists duplicates as a union (it does not intersect conflicting
    ``created:`` qualifiers), so callers that shard by date must replace the
    original qualifier(s) rather than append a second one.
    """
    windows: list[tuple[date | None, date | None]] = []
    for match in _CREATED_TOKEN.finditer(query):
        windows.append(_created_window(match.group(1)))
    if not windows:
        return query.strip(), []
    stripped = " ".join(_CREATED_TOKEN.sub(" ", query).split())
    return stripped, windows


def replace_created(query: str, start: date, end: date) -> str:
    """Return the query with every positive created token replaced by one range."""
    stripped, _ = split_created(query)
    token = f"created:{start.isoformat()}..{end.isoformat()}"
    return f"{stripped} {token}".strip()


def _merge_windows(
    windows: list[tuple[date | None, date | None]],
    *,
    floor: date,
    ceiling: date,
) -> list[tuple[date, date]]:
    clamped: list[tuple[date, date]] = []
    for low, high in windows:
        start = max(low or floor, floor)
        end = min(high or ceiling, ceiling)
        if start <= end:
            clamped.append((start, end))
    clamped.sort()
    merged: list[tuple[date, date]] = []
    for start, end in clamped:
        if merged and start <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


class ShardPlanner:
    def __init__(
        self,
        count_fn: Callable[[str], int],
        *,
        max_fetchable: int = 1000,
        scan_target: int = 4000,
        min_date: date = date(2008, 1, 1),
        root_count: int | None = None,
    ) -> None:
        self._count_fn = count_fn
        self._max_fetchable = max_fetchable
        self._scan_target = scan_target
        self._min_date = min_date
        self._root_count = root_count

    def iter_plan(self, query: str, *, now: datetime | None = None) -> Iterator[ShardSpec]:
        current = now if now is not None else datetime.now(UTC)
        ceiling = current.date()
        if ceiling < self._min_date:
            raise ValueError("min_date is after now; there is no window to bisect")
        root = self._root_count if self._root_count is not None else self._count_fn(query)
        if root == 0:
            return
        if self._fetchable(root):
            yield ShardSpec(query=query, range_start=None, range_end=None, total_count=root)
            return
        base, windows = split_created(query)
        if not windows:
            windows = [(None, None)]
        for start, end in _merge_windows(windows, floor=self._min_date, ceiling=ceiling):
            window_count = (
                root if start != end else self._count_fn(self._range_query(base, start, end))
            )
            if start == end:
                yield self._leaf(
                    base, start, end, window_count, oversized=not self._fetchable(window_count)
                )
            else:
                yield from self._bisect(base, start, end, window_count, 0)

    def plan(self, query: str, *, now: datetime | None = None) -> list[ShardSpec]:
        return list(self.iter_plan(query, now=now))

    def _fetchable(self, count: int) -> bool:
        return count < self._max_fetchable and count <= self._scan_target

    def _bisect(
        self, base_query: str, start: date, end: date, count: int, depth: int
    ) -> Iterator[ShardSpec]:
        if depth > _MAX_DEPTH:
            raise RuntimeError("shard planner exceeded the recursion depth cap")
        if start == end:
            yield self._leaf(base_query, start, end, count, oversized=not self._fetchable(count))
            return
        middle = start + (end - start) // 2
        for half_start, half_end in ((start, middle), (middle + timedelta(days=1), end)):
            if half_start > half_end:
                continue
            half_count = self._count_fn(self._range_query(base_query, half_start, half_end))
            if half_count == 0:
                continue
            if self._fetchable(half_count):
                yield self._leaf(base_query, half_start, half_end, half_count)
            else:
                yield from self._bisect(base_query, half_start, half_end, half_count, depth + 1)

    def _leaf(
        self, base_query: str, start: date, end: date, count: int, *, oversized: bool = False
    ) -> ShardSpec:
        return ShardSpec(
            query=self._range_query(base_query, start, end),
            range_start=datetime.combine(start, time.min, tzinfo=UTC),
            range_end=datetime.combine(end, time.max, tzinfo=UTC),
            total_count=count,
            oversized=oversized,
        )

    @staticmethod
    def _range_query(base_query: str, start: date, end: date) -> str:
        return f"{base_query} created:{start.isoformat()}..{end.isoformat()}"
