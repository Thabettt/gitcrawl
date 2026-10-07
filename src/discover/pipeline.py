from __future__ import annotations

import logging
import os
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as wall_time
from enum import StrEnum
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
from lib import audit, cancellation, progress
from lib.deadlines import DeadlineExceededError
from lib.gh_client import API_BASE, PartialResultsError, ThrottledError, request_with_retry
from limiter.buckets import BucketLimiter
from scheduler.shard_planner import ShardPlanner, ShardSpec, replace_created
from scheduler.state_machine import QueuedShard, ShardQueue, ShardRow, ShardState, ShardStore
from store.upserts import UpsertStats, dedupe_items, upsert_repos

logger = logging.getLogger("gitcrawl.discover")

_INT_PARAMS = ("page", "per_page", "since")


@dataclass(frozen=True)
class Deps:
    client: httpx.Client
    engine: sqlalchemy.Engine
    redis: object | None = None
    limiter: BucketLimiter | None = None
    token_fp: str = "anonymous"
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
    deferred_shards: int = 0
    deadline_hit: bool = False
    page_capped_shards: int = 0
    cap_splits: int = 0
    plan_capped: bool = False
    repo_ids: tuple[int, ...] = ()


class _ProcessResult(StrEnum):
    DONE = "done"
    DEFERRED = "deferred"
    STOP = "stop"


@dataclass(frozen=True)
class _ProcessOutcome:
    result: _ProcessResult
    next_attempts: int = 0


_MAX_CONSECUTIVE_DEFERRALS = 3


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
            token_fp=deps.token_fp,
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


def _progress_counters(stats: DiscoveryStats, total_count: int | None) -> dict[str, int]:
    counters = {
        "fetched": stats.fetched,
        "updated": stats.updated,
        "unchanged": stats.unchanged,
        "skipped": stats.skipped,
    }
    if total_count is not None:
        counters["total_count"] = total_count
    return counters


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
        token_id=deps.token_fp,
        on_response=on_response,
        sleep=sleep,
        now=now,
        jitter=jitter,
    )
    if response.status_code != 200:
        raise RequestFailed(int(response.status_code), _short_message(response))
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise RequestFailed(200, "search payload is not an object")
    return int(payload.get("total_count") or 0)


def _splittable(row: ShardRow) -> bool:
    if row.range_start is None or row.range_end is None:
        return False
    return (row.range_end.date() - row.range_start.date()).days >= 1


def _sub_specs(row: ShardRow) -> tuple[ShardSpec, ShardSpec] | None:
    if not _splittable(row):
        return None
    start = row.range_start.date()
    end = row.range_end.date()
    middle = start + (end - start) // 2
    right_start = middle + timedelta(days=1)
    left = ShardSpec(
        query=replace_created(row.query, start, middle),
        range_start=datetime.combine(start, wall_time.min, tzinfo=UTC),
        range_end=datetime.combine(middle, wall_time.max, tzinfo=UTC),
        total_count=0,
    )
    right = ShardSpec(
        query=replace_created(row.query, right_start, end),
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
    queue_prefix: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
) -> DiscoveryStats:
    stats = DiscoveryStats()
    on_response = _audit_hook(deps)
    store = ShardStore(deps.engine)
    queue: ShardQueue | None = None
    if deps.redis is not None:
        queue = (
            ShardQueue(deps.redis, prefix=queue_prefix) if queue_prefix else ShardQueue(deps.redis)
        )
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

    deadline_reached = False
    planner_target = min(1000, max(1, per_page * max_pages))
    if total_count is not None and total_count > max_shards * planner_target:
        # Not enough shard allowance for a finer plan; fall back to the 1000 cap
        # (the plan will hit max_shards and report plan_capped).
        planner_target = 1000
    try:
        for spec in ShardPlanner(
            count_fn, max_fetchable=planner_target, root_count=total_count
        ).iter_plan(query):
            cancellation.check()
            if create_shard(spec) is None:
                stats.plan_capped = True
                break
    except DeadlineExceededError:
        stats.deadline_hit = True
        deadline_reached = True
        logger.warning("run deadline reached during discovery planning")
    progress.report("discovering", 0, stats.shards, **_progress_counters(stats, total_count))

    def spawn_narrower(row: ShardRow) -> bool:
        specs = _sub_specs(row)
        if specs is None or created + len(specs) > max_shards:
            return False
        for spec in specs:
            create_shard(spec)
        return True

    def process(shard_id: int, queued: QueuedShard | None = None) -> _ProcessOutcome:
        row = store.get(shard_id)
        if row.query is None:
            raise ValueError(f"shard {shard_id} has no query")
        if row.state in (ShardState.DONE, ShardState.INCOMPLETE):
            # At-least-once delivery: the shard finished but its ack was lost.
            # The caller acks this stale delivery and moves on.
            return _ProcessOutcome(_ProcessResult.DONE)
        if row.state is ShardState.PENDING:
            store.set_state(shard_id, ShardState.ACTIVE)
        # An ACTIVE shard here was reclaimed from a consumer that died mid-shard;
        # reprocess it. Page upserts are idempotent, so a partial first attempt is safe.
        fetched = 0
        last_total = row.total_count
        narrowed = False
        incomplete = False
        try:
            for page in iter_shard_pages(
                deps.client,
                row.query,
                limiter=deps.limiter,
                token_id=deps.token_fp,
                per_page=per_page,
                max_pages=max_pages,
                sleep=sleep,
                now=now,
                jitter=jitter,
                on_response=on_response,
            ):
                cancellation.check()
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
                return _ProcessOutcome(_ProcessResult.DONE)
            incomplete = True
        except (RequestFailed, ThrottledError, PartialResultsError, httpx.HTTPError):
            if queued is None or queue is None:
                store.set_state(shard_id, ShardState.PENDING)
                raise
            next_attempts = queued.attempts + 1
            if next_attempts >= queue.max_attempts:
                queue.dead_letter(shard_id, next_attempts)
                stats.incomplete_shards += 1
                store.set_state(
                    shard_id,
                    ShardState.INCOMPLETE,
                    fetched=fetched,
                    total_count=last_total,
                    incomplete=True,
                )
                return _ProcessOutcome(_ProcessResult.DONE)
            store.set_state(shard_id, ShardState.PENDING)
            return _ProcessOutcome(_ProcessResult.DEFERRED, next_attempts)
        except DeadlineExceededError:
            # Out of time: keep the delivery for a later resume instead of failing.
            stats.deadline_hit = True
            store.set_state(shard_id, ShardState.PENDING)
            return _ProcessOutcome(_ProcessResult.STOP)
        except cancellation.RunCancelled:
            # Aborted: keep the shard resumable.
            store.set_state(shard_id, ShardState.PENDING)
            raise
        except Exception:
            # Never strand a shard in ACTIVE: an unexpected failure marks it
            # incomplete so a crash cannot poison every later discovery run.
            try:
                store.set_state(
                    shard_id,
                    ShardState.INCOMPLETE,
                    fetched=fetched,
                    total_count=last_total,
                    incomplete=True,
                )
            except Exception:
                logger.exception("could not mark shard %s incomplete", shard_id)
            raise
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
        return _ProcessOutcome(_ProcessResult.DONE)

    def deadline_expired() -> bool:
        deadline = deps.limiter.deadline if deps.limiter is not None else None
        return deadline is not None and deadline.remaining <= 0

    deferred: list[QueuedShard] = []
    leftover: list[QueuedShard] = []
    consecutive_deferrals = 0
    aborted = False

    def consume(queued: QueuedShard) -> _ProcessResult | None:
        """Process one delivery; None means stop claiming, DONE means finished."""
        nonlocal consecutive_deferrals
        outcome = process(queued.shard_id, queued)
        if outcome.result is _ProcessResult.STOP:
            return None
        if outcome.result is _ProcessResult.DEFERRED:
            # Drop this delivery now; the retry pass republishes it after the
            # remaining shards (lane ordering would otherwise starve them).
            queue.ack(queued.stream_id, queued.shard_id)
            deferred.append(
                QueuedShard(
                    stream_id=queued.stream_id,
                    shard_id=queued.shard_id,
                    attempts=outcome.next_attempts,
                )
            )
            consecutive_deferrals += 1
            return None if consecutive_deferrals >= _MAX_CONSECUTIVE_DEFERRALS else outcome.result
        consecutive_deferrals = 0
        queue.ack(queued.stream_id, queued.shard_id)
        return _ProcessResult.DONE

    consumer = f"gitcrawl-{os.getpid()}"
    completed = 0

    def mark_completed() -> None:
        nonlocal completed
        completed += 1
        progress.report(
            "discovering", completed, stats.shards, **_progress_counters(stats, total_count)
        )

    if queue is None:
        while pending:
            cancellation.check()
            outcome = process(pending.popleft())
            if outcome.result is _ProcessResult.STOP:
                break
            mark_completed()
    elif not deadline_reached:
        for queued in queue.reclaim_stale(consumer):
            cancellation.check()
            if deadline_expired():
                break
            result = consume(queued)
            if result is None:
                break
            if result is _ProcessResult.DONE:
                mark_completed()
        while not deadline_expired():
            cancellation.check()
            claimed = queue.claim(consumer, count=1)
            if not claimed:
                break
            stop = False
            for queued in claimed:
                result = consume(queued)
                if result is _ProcessResult.DONE:
                    mark_completed()
                if result is None:
                    stop = True
                    break
            if stop:
                break
        aborted = consecutive_deferrals >= _MAX_CONSECUTIVE_DEFERRALS
        if not aborted and not deadline_expired():
            for old in deferred:
                cancellation.check()
                if deadline_expired():
                    queue.requeue(old.shard_id, old.attempts)
                    leftover.append(old)
                    continue
                stream_id = queue.requeue(old.shard_id, old.attempts)
                item = QueuedShard(
                    stream_id=stream_id, shard_id=old.shard_id, attempts=old.attempts
                )
                outcome = process(item.shard_id, item)
                if outcome.result is _ProcessResult.DEFERRED:
                    queue.retry_or_dlq(stream_id, item.shard_id, attempts=outcome.next_attempts)
                    leftover.append(item)
                elif outcome.result is _ProcessResult.STOP:
                    leftover.append(item)
                else:
                    queue.ack(stream_id, item.shard_id)
                    mark_completed()
        else:
            for old in deferred:
                queue.requeue(old.shard_id, old.attempts)
                leftover.append(old)

    stats.deferred_shards = len(leftover)

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
        token_id=deps.token_fp,
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
            token_id=deps.token_fp,
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
            token_id=deps.token_fp,
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
