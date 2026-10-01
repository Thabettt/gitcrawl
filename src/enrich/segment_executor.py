from __future__ import annotations

import logging
from bisect import bisect_left
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from scheduler.tiering import order_repos

logger = logging.getLogger(__name__)

_SURVIVORS_FIRST = "survivors_first"


@dataclass
class SegmentStats:
    per_field_sources: dict[str, int] = field(default_factory=dict)
    calls_spent: dict[str, int] = field(default_factory=dict)
    requeues: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Segment:
    start: int
    end: int


def plan_segments(repo_ids: Sequence[int], *, segments: int) -> list[Segment]:
    if segments < 1:
        raise ValueError("segments must be at least 1")
    ids = sorted({repo_id for repo_id in repo_ids})
    if not ids:
        return []
    count = min(segments, len(ids))
    base, extra = divmod(len(ids), count)
    planned: list[Segment] = []
    start = ids[0] - 1
    index = 0
    for offset in range(count):
        index += base + (1 if offset < extra else 0)
        end = ids[index - 1]
        planned.append(Segment(start=start, end=end))
        start = end
    return planned


def _view(candidate) -> dict:
    if isinstance(candidate, int) and not isinstance(candidate, bool):
        return {"id": candidate, "stargazers": None, "pushed_at": None}
    repo_id, stargazers, pushed_at = candidate
    return {"id": repo_id, "stargazers": stargazers, "pushed_at": pushed_at}


def _normalized(candidates: Sequence) -> list[dict]:
    views: list[dict] = []
    seen: set[int] = set()
    for candidate in candidates:
        view = _view(candidate)
        if view["id"] in seen:
            continue
        seen.add(view["id"])
        views.append(view)
    return views


def execute_segments(
    candidates: Sequence,
    handlers: Mapping[str, Callable[[Sequence[int]], tuple[Sequence[int], int]]],
    *,
    segments: int = 1,
    max_requeues: int = 1,
    order: str = "survivors_first",
) -> tuple[list[int], SegmentStats]:
    if order != _SURVIVORS_FIRST:
        raise ValueError(f"unknown candidate order `{order}`; expected `{_SURVIVORS_FIRST}`")
    if segments < 1:
        raise ValueError("segments must be at least 1")
    if max_requeues < 0:
        raise ValueError("max_requeues must be non-negative")
    stats = SegmentStats()
    views = _normalized(candidates)
    if not views:
        return [], stats
    ranked = order_repos(views)
    ranked_ids = [view["id"] for view in ranked]
    rank = {repo_id: index for index, repo_id in enumerate(ranked_ids)}
    segments_plan = plan_segments(ranked_ids, segments=segments)
    ends = [segment.end for segment in segments_plan]
    buckets: list[list[int]] = [[] for _ in segments_plan]
    for repo_id in ranked_ids:
        buckets[bisect_left(ends, repo_id)].append(repo_id)
    work: list[tuple[Segment, list[int]]] = [
        (segment, ids)
        for segment, ids in zip(segments_plan, buckets, strict=True)
        if ids
    ]
    work.sort(key=lambda item: rank[item[1][0]])
    field_names = list(handlers)
    queue: deque[dict] = deque(
        {"segment": segment, "ids": list(ids), "field_index": 0, "requeues": 0}
        for segment, ids in work
    )
    survivors: list[int] = []
    while queue:
        item = queue.popleft()
        ids = item["ids"]
        failed: Exception | None = None
        try:
            while item["field_index"] < len(field_names):
                field = field_names[item["field_index"]]
                kept, calls = handlers[field](ids)
                ids = list(kept)
                stats.per_field_sources[field] = stats.per_field_sources.get(field, 0) + len(ids)
                stats.calls_spent[field] = stats.calls_spent.get(field, 0) + calls
                item["field_index"] += 1
                item["ids"] = ids
        except Exception as exc:
            failed = exc
        if failed is not None:
            field = field_names[item["field_index"]]
            if item["requeues"] < max_requeues:
                stats.requeues += 1
                item["requeues"] += 1
                queue.insert(1, item)
            else:
                message = (
                    f"segment ({item['segment'].start}, {item['segment'].end}] dropped "
                    f"{len(item['ids'])} repo(s) after {item['requeues']} requeue(s): "
                    f"field `{field}` failed: {type(failed).__name__}: {failed}"
                )
                stats.warnings.append(message)
                logger.warning(message)
            continue
        survivors.extend(ids)
    survivors.sort()
    return survivors, stats
