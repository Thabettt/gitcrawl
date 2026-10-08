from __future__ import annotations

import contextvars
import re
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from lib import cancellation

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


def _fetchable(count: int, max_fetchable: int, scan_target: int) -> bool:
    return count < max_fetchable and count <= scan_target


def range_query(base: str, start: date, end: date) -> str:
    return f"{base} created:{start.isoformat()}..{end.isoformat()}"


def _leaf(base: str, start: date, end: date, count: int, *, oversized: bool = False) -> ShardSpec:
    return ShardSpec(
        query=range_query(base, start, end),
        range_start=datetime.combine(start, time.min, tzinfo=UTC),
        range_end=datetime.combine(end, time.max, tzinfo=UTC),
        total_count=count,
        oversized=oversized,
    )


def _probe_windows(
    probe_counts: Callable[[Sequence[str]], Sequence[int]],
    base: str,
    level: Sequence[tuple[date, date]],
    *,
    batch_size: int,
    probe_concurrency: int,
) -> list[int]:
    chunks = [
        [range_query(base, start, end) for start, end in level[offset : offset + batch_size]]
        for offset in range(0, len(level), batch_size)
    ]
    if probe_concurrency == 1 or len(chunks) == 1:
        return [count for chunk in chunks for count in probe_counts(chunk)]
    with ThreadPoolExecutor(max_workers=min(probe_concurrency, len(chunks))) as pool:
        futures = []
        for chunk in chunks:
            context = contextvars.copy_context()
            futures.append(pool.submit(context.run, probe_counts, chunk))
        try:
            return [count for future in futures for count in future.result()]
        except BaseException:
            pool.shutdown(wait=True, cancel_futures=True)
            raise


def plan_shards(
    query: str,
    probe_counts: Callable[[Sequence[str]], Sequence[int]],
    *,
    max_fetchable: int = 1000,
    scan_target: int = 4000,
    min_date: date = date(2008, 1, 1),
    root_count: int | None = None,
    max_shards: int = 10_000,
    batch_size: int = 20,
    probe_concurrency: int = 1,
    now: datetime | None = None,
) -> tuple[list[ShardSpec], bool]:
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    if probe_concurrency < 1:
        raise ValueError("probe_concurrency must be >= 1")
    current = now if now is not None else datetime.now(UTC)
    ceiling = current.date()
    if ceiling < min_date:
        raise ValueError("min_date is after now; there is no window to bisect")
    root = root_count if root_count is not None else probe_counts([query])[0]
    if root == 0:
        return [], False
    if _fetchable(root, max_fetchable, scan_target):
        return [ShardSpec(query=query, range_start=None, range_end=None, total_count=root)], False
    base, windows = split_created(query)
    if not windows:
        windows = [(None, None)]
    merged = _merge_windows(windows, floor=min_date, ceiling=ceiling)
    use_root_for_single = root_count is not None and len(merged) == 1
    pending: deque[tuple[date, date]] = deque(merged)
    leaves: list[ShardSpec] = []
    plan_capped = False
    while pending and not plan_capped:
        cancellation.check()
        level = [
            pending.popleft() for _ in range(min(batch_size * probe_concurrency, len(pending)))
        ]
        if (
            use_root_for_single
            and len(level) == 1
            and level[0] == merged[0]
            and level[0][0] != level[0][1]
        ):
            counts = [root]
        else:
            counts = _probe_windows(
                probe_counts,
                base,
                level,
                batch_size=batch_size,
                probe_concurrency=probe_concurrency,
            )
        for (start, end), count in zip(level, counts, strict=True):
            if count == 0:
                continue
            if _fetchable(count, max_fetchable, scan_target):
                leaves.append(_leaf(base, start, end, count, oversized=False))
            elif start == end:
                leaves.append(_leaf(base, start, end, count, oversized=True))
            else:
                middle = start + (end - start) // 2
                pending.append((start, middle))
                pending.append((middle + timedelta(days=1), end))
        if len(leaves) >= max_shards and (len(leaves) > max_shards or pending):
            plan_capped = True
    leaves.sort(key=lambda spec: spec.range_start or datetime.min.replace(tzinfo=UTC))
    if plan_capped:
        leaves = leaves[:max_shards]
    return leaves, plan_capped
