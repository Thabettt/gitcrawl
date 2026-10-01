from __future__ import annotations

import logging
import os
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as wall_time
from urllib.parse import parse_qsl, urlencode, urlparse

import httpx
import sqlalchemy

from discover.org_enum import iter_org_repos, iter_user_repos
from discover.search_shards import (
    RequestFailed,
    SearchCapExceeded,
    _short_message,
    iter_shard_pages,
)
from discover.since_scan import iter_since_pages, save_checkpoint
from lib.gh_client import API_BASE, request_with_retry
from limiter.buckets import BucketLimiter
from scheduler.shard_planner import ShardPlanner, ShardSpec
from scheduler.state_machine import ShardQueue, ShardRow, ShardState, ShardStore
from serve import audit
from store.upserts import UpsertStats, dedupe_items, upsert_repos

logger = logging.getLogger("gitcrawl.discover")

_INT_PARAMS = ("page", "per_page", "since")


@dataclass(frozen=True)
class Deps:
    client: httpx.Client
    engine: sqlalchemy.Engine
    redis: object | None = None
    limiter: BucketLimiter | None = None
    token_id: str = "anonymous"
    audit_buffer: audit.AuditBuffer | None = None


@dataclass
class DiscoveryStats:
    shards: int = 0
    pages: int = 0
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    incomplete_shards: int = 0
    page_capped_shards: int = 0
    cap_splits: int = 0
    plan_capped: bool = False
    repo_ids: tuple[int, ...] = ()


def _request_params(response: httpx.Response) -> dict[str, object]:
    request = response.request
    if request is None:
        return {}
    params: dict[str, object] = dict(parse_qsl(urlparse(str(request.url)).query))
    for key in _INT_PARAMS:
        value = params.get(key)
        if isinstance(value, str) and value.isdigit():
            params[key] = int(value)
    return params


def _audit_hook(deps: Deps) -> Callable[[httpx.Response, float], None]:
    def hook(response: httpx.Response, latency_ms: float) -> None:
        record = audit.record_from_response(
            _request_params(response),
            response,
            token_fp=deps.token_id,
            latency_ms=latency_ms,
        )
        if deps.audit_buffer is not None:
            deps.audit_buffer.add(record)
        else:
            audit.record_audit(deps.engine, record)

    return hook


def _flush_audit(deps: Deps) -> None:
    if deps.audit_buffer is not None:
        deps.audit_buffer.flush()


def _collect_ids(seen: set[int], collected: list[int], items: Iterable[Mapping]) -> None:
    for item in items:
        if not isinstance(item, Mapping):
            continue
        repo_id = item.get("id")
        if isinstance(repo_id, bool) or not isinstance(repo_id, int) or repo_id in seen:
            continue
        seen.add(repo_id)
        collected.append(repo_id)


def _fold(stats: DiscoveryStats, upserted: UpsertStats) -> None:
    stats.inserted += upserted.inserted
    stats.updated += upserted.updated
    stats.unchanged += upserted.unchanged
    stats.skipped += upserted.skipped


def _log_summary(kind: str, stats: DiscoveryStats) -> None:
    logger.info(
        "discovery run kind=%s shards=%d pages=%d fetched=%d inserted=%d updated=%d "
        "unchanged=%d skipped=%d incomplete_shards=%d cap_splits=%d",
        kind,
        stats.shards,
        stats.pages,
        stats.fetched,
        stats.inserted,
        stats.updated,
        stats.unchanged,
        stats.skipped,
        stats.incomplete_shards,
        stats.cap_splits,
    )


def count_total(
    deps: Deps,
    query: str,
    *,
    on_response: Callable[[httpx.Response, float], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
) -> int:
    if on_response is None:
        on_response = _audit_hook(deps)
    url = f"{API_BASE}/search/repositories?{urlencode({'q': query, 'per_page': 1, 'page': 1})}"
    response = request_with_retry(
        deps.client,
        "GET",
        url,
        limiter=deps.limiter,
        token_id=deps.token_id,
        on_response=on_response,
        sleep=sleep,
        now=now,
        jitter=jitter,
    )
    if response.status_code != 200:
        raise RequestFailed(int(response.status_code), _short_message(response))
    payload = response.json()
    if not isinstance(payload, Mapping):
        return 0
    return int(payload.get("total_count") or 0)


def _splittable(row: ShardRow) -> bool:
    if row.range_start is None or row.range_end is None:
        return False
    return (row.range_end.date() - row.range_start.date()).days >= 1


def _sub_specs(base_query: str, row: ShardRow) -> tuple[ShardSpec, ShardSpec] | None:
    if not _splittable(row):
        return None
    start = row.range_start.date()
    end = row.range_end.date()
    middle = start + (end - start) // 2
    right_start = middle + timedelta(days=1)
    left = ShardSpec(
        query=f"{base_query} created:{start.isoformat()}..{middle.isoformat()}",
        range_start=datetime.combine(start, wall_time.min, tzinfo=UTC),
        range_end=datetime.combine(middle, wall_time.max, tzinfo=UTC),
        total_count=0,
    )
    right = ShardSpec(
        query=f"{base_query} created:{right_start.isoformat()}..{end.isoformat()}",
        range_start=datetime.combine(right_start, wall_time.min, tzinfo=UTC),
        range_end=datetime.combine(end, wall_time.max, tzinfo=UTC),
        total_count=0,
    )
    return left, right


def run_search_discovery(
    deps: Deps,
    query: str,
    *,
    per_page: int = 100,
    max_pages: int = 10,
    max_shards: int = 100,
    total_count: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
) -> DiscoveryStats:
    stats = DiscoveryStats()
    on_response = _audit_hook(deps)
    store = ShardStore(deps.engine)
    queue = ShardQueue(deps.redis) if deps.redis is not None else None
    pending: deque[int] = deque()
    created = 0
    seen_ids: set[int] = set()
    collected_ids: list[int] = []

    def count_fn(candidate: str) -> int:
        return count_total(
            deps,
            candidate,
            on_response=on_response,
            sleep=sleep,
            now=now,
            jitter=jitter,
        )

    def create_shard(spec: ShardSpec) -> int | None:
        nonlocal created
        if created >= max_shards:
            return None
        shard_id = store.create(spec)
        created += 1
        stats.shards += 1
        if queue is not None:
            queue.enqueue(shard_id)
        else:
            pending.append(shard_id)
        return shard_id

    for spec in ShardPlanner(count_fn, root_count=total_count).iter_plan(query):
        if create_shard(spec) is None:
            stats.plan_capped = True
            break

    def spawn_narrower(row: ShardRow) -> bool:
        specs = _sub_specs(query, row)
        if specs is None or created + len(specs) > max_shards:
            return False
        for spec in specs:
            create_shard(spec)
        return True

    def process(shard_id: int) -> None:
        row = store.get(shard_id)
        if row.query is None:
            raise ValueError(f"shard {shard_id} has no query")
        store.set_state(shard_id, ShardState.ACTIVE)
        fetched = 0
        last_total = row.total_count
        narrowed = False
        incomplete = False
        try:
            for page in iter_shard_pages(
                deps.client,
                row.query,
                limiter=deps.limiter,
                token_id=deps.token_id,
                per_page=per_page,
                max_pages=max_pages,
                sleep=sleep,
                now=now,
                jitter=jitter,
                on_response=on_response,
            ):
                stats.pages += 1
                fetched += len(page.items)
                stats.fetched += len(page.items)
                last_total = page.total_count
                _fold(stats, upsert_repos(deps.engine, dedupe_items(page.items)))
                _collect_ids(seen_ids, collected_ids, page.items)
                if page.exhausted:
                    stats.page_capped_shards += 1
                    incomplete = True
                if page.incomplete:
                    if not narrowed and spawn_narrower(row):
                        narrowed = True
                    else:
                        incomplete = True
        except SearchCapExceeded:
            if spawn_narrower(row):
                stats.cap_splits += 1
                store.set_state(shard_id, ShardState.DONE, fetched=fetched, total_count=last_total)
                return
            incomplete = True
        if incomplete:
            stats.incomplete_shards += 1
            store.set_state(
                shard_id,
                ShardState.INCOMPLETE,
                fetched=fetched,
                total_count=last_total,
                incomplete=True,
            )
        else:
            store.set_state(shard_id, ShardState.DONE, fetched=fetched, total_count=last_total)

    consumer = f"gitcrawl-{os.getpid()}"
    if queue is None:
        while pending:
            process(pending.popleft())
    else:
        while True:
            claimed = queue.claim(consumer, count=1)
            if not claimed:
                break
            for queued in claimed:
                process(queued.shard_id)
                queue.ack(queued.stream_id, queued.shard_id)

    stats.repo_ids = tuple(collected_ids)
    _flush_audit(deps)
    _log_summary("search", stats)
    return stats


def run_since_scan(
    deps: Deps,
    *,
    since: int = 0,
    per_page: int = 100,
    max_pages: int | None = None,
    stop_after_id: int | None = None,
    checkpoint_shard_id: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
) -> DiscoveryStats:
    stats = DiscoveryStats()
    on_response = _audit_hook(deps)
    for page in iter_since_pages(
        deps.client,
        since=since,
        stop_after_id=stop_after_id,
        per_page=per_page,
        max_pages=max_pages,
        limiter=deps.limiter,
        token_id=deps.token_id,
        sleep=sleep,
        now=now,
        jitter=jitter,
        on_response=on_response,
    ):
        stats.pages += 1
        stats.fetched += len(page.items)
        _fold(stats, upsert_repos(deps.engine, page.items))
        if checkpoint_shard_id is not None and page.max_id is not None:
            save_checkpoint(deps.engine, checkpoint_shard_id, page.max_id)
    _flush_audit(deps)
    _log_summary("since", stats)
    return stats


def run_org_enum(
    deps: Deps,
    *,
    org: str | None = None,
    user: str | None = None,
    repo_type: str = "all",
    per_page: int = 100,
    max_pages: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
) -> DiscoveryStats:
    if (org is None) == (user is None):
        raise ValueError("run_org_enum requires exactly one of org or user")
    stats = DiscoveryStats()
    on_response = _audit_hook(deps)
    pages = (
        iter_org_repos(
            deps.client,
            org,
            repo_type=repo_type,
            per_page=per_page,
            max_pages=max_pages,
            limiter=deps.limiter,
            token_id=deps.token_id,
            sleep=sleep,
            now=now,
            jitter=jitter,
            on_response=on_response,
        )
        if org is not None
        else iter_user_repos(
            deps.client,
            user,
            repo_type=repo_type,
            per_page=per_page,
            max_pages=max_pages,
            limiter=deps.limiter,
            token_id=deps.token_id,
            sleep=sleep,
            now=now,
            jitter=jitter,
            on_response=on_response,
        )
    )
    for page in pages:
        stats.pages += 1
        stats.fetched += len(page.items)
        _fold(stats, upsert_repos(deps.engine, page.items))
    _flush_audit(deps)
    _log_summary("org" if org is not None else "user", stats)
    return stats
