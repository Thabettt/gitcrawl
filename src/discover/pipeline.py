from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlparse

import httpx
import sqlalchemy

from discover.graphql_search import count_queries, iter_pages
from discover.org_enum import iter_org_repos, iter_user_repos
from discover.since_scan import iter_since_pages, save_checkpoint
from lib import audit, cancellation, progress
from lib.deadlines import DeadlineExceededError
from lib.gh_client import PartialResultsError, RequestFailed, ThrottledError
from lib.graphql_batch import MalformedResponse
from limiter.buckets import BucketLimiter
from scheduler.shard_planner import plan_shards
from scheduler.state_machine import ShardState, ShardStore
from store.upserts import UpsertStats, dedupe_items, upsert_repos

logger = logging.getLogger("gitcrawl.discover")

_INT_PARAMS = ("page", "per_page", "since")
_MAX_PROBE_CONCURRENCY = 8
_UPSERT_EVERY_PAGES = 10
_UPSERT_EVERY_ITEMS = 1000


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
    deadline_hit: bool = False
    page_capped_shards: int = 0
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
            token_fp=deps.token_fp,
            latency_ms=latency_ms,
        )
        if deps.audit_buffer is not None:
            deps.audit_buffer.add(record)
        else:
            audit.record_audit(deps.engine, record)

    return hook


def _graphql_body(response: httpx.Response) -> object | None:
    body = audit.cached_json(response)
    if body is None:
        try:
            body = response.json()
        except Exception:
            body = None
    return body


def _graphql_total_count(body: object) -> int | None:
    if not isinstance(body, Mapping):
        return None
    data = body.get("data")
    if not isinstance(data, Mapping):
        return None
    for alias in ("s", "s0"):
        node = data.get(alias)
        if not isinstance(node, Mapping):
            continue
        count = node.get("repositoryCount")
        if isinstance(count, int) and not isinstance(count, bool):
            return count
    return None


def _graphql_audit_hook(
    deps: Deps,
) -> Callable[[httpx.Response, float, Mapping[str, object]], None]:
    def hook(response: httpx.Response, latency_ms: float, params: Mapping[str, object]) -> None:
        body = _graphql_body(response)
        record = audit.record_from_response(
            params,
            response,
            token_fp=deps.token_fp,
            latency_ms=latency_ms,
            body=body,
            total_count=_graphql_total_count(body),
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
        "unchanged=%d skipped=%d incomplete_shards=%d",
        kind,
        stats.shards,
        stats.pages,
        stats.fetched,
        stats.inserted,
        stats.updated,
        stats.unchanged,
        stats.skipped,
        stats.incomplete_shards,
    )


def count_total(
    deps: Deps,
    query: str,
    *,
    on_response: Callable[[httpx.Response, float, Mapping[str, object]], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
) -> int:
    hook = _graphql_audit_hook(deps) if on_response is None else on_response
    return count_queries(
        deps.client,
        [query],
        limiter=deps.limiter,
        token_id=deps.token_fp,
        on_response=hook,
        sleep=sleep,
        now=now,
        jitter=jitter,
    )[0]


class _Worker:
    def __init__(
        self,
        deps: Deps,
        stats: DiscoveryStats,
        lock: threading.Lock,
        stop: threading.Event,
        seen: set[int],
        collected: list[int],
        hook: Callable[[httpx.Response, float, Mapping[str, object]], None],
        *,
        max_pages: int,
        sleep: Callable[[float], None],
        now: Callable[[], float],
        jitter: Callable[[], float] | None,
        upsert_every_pages: int = _UPSERT_EVERY_PAGES,
        upsert_every_items: int = _UPSERT_EVERY_ITEMS,
    ) -> None:
        self._deps = deps
        self._stats = stats
        self._lock = lock
        self._stop = stop
        self._seen = seen
        self._collected = collected
        self._hook = hook
        self._max_pages = max_pages
        self._sleep = sleep
        self._now = now
        self._jitter = jitter
        self._upsert_every_pages = upsert_every_pages
        self._upsert_every_items = upsert_every_items
        self._store = ShardStore(deps.engine)

    def _deadline_expired(self) -> bool:
        deadline = self._deps.limiter.deadline if self._deps.limiter is not None else None
        return deadline is not None and deadline.remaining <= 0

    def _stop_for_deadline(self) -> None:
        with self._lock:
            self._stats.deadline_hit = True
        self._stop.set()

    def process(self, shard_id: int) -> None:
        row = self._store.get(shard_id)
        if row.query is None:
            raise ValueError(f"shard {shard_id} has no query")
        if row.state in (ShardState.DONE, ShardState.INCOMPLETE):
            return
        if self._stop.is_set():
            return
        cancellation.check()
        if self._deadline_expired():
            self._stop_for_deadline()
            if row.state is ShardState.ACTIVE:
                self._store.set_state(shard_id, ShardState.PENDING)
            return
        if row.state is ShardState.PENDING:
            self._store.set_state(shard_id, ShardState.ACTIVE)
        fetched = 0
        last_total = row.total_count
        incomplete = False
        pending: list[dict] = []
        pending_pages = 0

        def flush_pending() -> None:
            nonlocal pending_pages
            if not pending:
                return
            upserted = upsert_repos(self._deps.engine, dedupe_items(pending))
            with self._lock:
                _fold(self._stats, upserted)
            pending.clear()
            pending_pages = 0

        def flush_pending_best_effort() -> None:
            try:
                flush_pending()
            except Exception:
                logger.exception("could not flush buffered discovery pages")

        try:
            for page in iter_pages(
                self._deps.client,
                row.query,
                max_pages=self._max_pages,
                limiter=self._deps.limiter,
                token_id=self._deps.token_fp,
                on_response=self._hook,
                sleep=self._sleep,
                now=self._now,
                jitter=self._jitter,
            ):
                cancellation.check()
                if self._deadline_expired():
                    self._stop_for_deadline()
                    flush_pending()
                    self._store.set_state(shard_id, ShardState.PENDING)
                    return
                pending.extend(page.items)
                pending_pages += 1
                with self._lock:
                    self._stats.pages += 1
                    self._stats.fetched += len(page.items)
                    fetched += len(page.items)
                    last_total = page.repository_count
                    _collect_ids(self._seen, self._collected, page.items)
                    if page.exhausted:
                        self._stats.page_capped_shards += 1
                        incomplete = True
                    if page.incomplete:
                        incomplete = True
                    if page.repository_count > fetched and not page.has_next:
                        incomplete = True
                if (
                    pending_pages >= self._upsert_every_pages
                    or len(pending) >= self._upsert_every_items
                ):
                    flush_pending()
            flush_pending()
        except (
            RequestFailed,
            ThrottledError,
            PartialResultsError,
            MalformedResponse,
            httpx.HTTPError,
        ):
            flush_pending_best_effort()
            with self._lock:
                self._stats.incomplete_shards += 1
            self._store.set_state(
                shard_id,
                ShardState.INCOMPLETE,
                fetched=fetched,
                total_count=last_total,
                incomplete=True,
            )
            return
        except cancellation.RunCancelled:
            flush_pending_best_effort()
            self._store.set_state(shard_id, ShardState.PENDING)
            raise
        except DeadlineExceededError:
            flush_pending_best_effort()
            self._stop_for_deadline()
            self._store.set_state(shard_id, ShardState.PENDING)
            return
        except BaseException:
            flush_pending_best_effort()
            try:
                self._store.set_state(
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
            with self._lock:
                self._stats.incomplete_shards += 1
        self._store.set_state(
            shard_id,
            ShardState.INCOMPLETE if incomplete else ShardState.DONE,
            fetched=fetched,
            total_count=last_total,
            incomplete=True if incomplete else None,
        )


def run_search_discovery(
    deps: Deps,
    query: str,
    *,
    per_page: int = 100,
    max_pages: int = 10,
    max_shards: int = 100,
    total_count: int | None = None,
    discovery_concurrency: int = 32,
    upsert_every_pages: int = _UPSERT_EVERY_PAGES,
    upsert_every_items: int = _UPSERT_EVERY_ITEMS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
) -> DiscoveryStats:
    stats = DiscoveryStats()
    hook = _graphql_audit_hook(deps)
    store = ShardStore(deps.engine)

    def probe_counts(queries: Sequence[str]) -> list[int]:
        return count_queries(
            deps.client,
            queries,
            limiter=deps.limiter,
            token_id=deps.token_fp,
            on_response=hook,
            sleep=sleep,
            now=now,
            jitter=jitter,
        )

    target = min(1000, max(1, per_page * max_pages))
    if total_count is not None and total_count > max_shards * target:
        target = 1000
    cancellation.check()
    limiter = deps.limiter
    binding = (
        limiter.bound_concurrency(discovery_concurrency) if limiter is not None else nullcontext()
    )
    with binding:
        try:
            leaves, plan_capped = plan_shards(
                query,
                probe_counts,
                max_fetchable=target,
                root_count=total_count,
                max_shards=max_shards,
                probe_concurrency=min(discovery_concurrency, _MAX_PROBE_CONCURRENCY),
            )
        except DeadlineExceededError:
            stats.deadline_hit = True
            _flush_audit(deps)
            _log_summary("search", stats)
            return stats
        stats.plan_capped = plan_capped
        shard_ids = [store.create(spec) for spec in leaves]
        stats.shards = len(shard_ids)
        progress.report("discovering", 0, stats.shards, **_progress_counters(stats, total_count))

        lock = threading.Lock()
        stop = threading.Event()
        seen_ids: set[int] = set()
        collected_ids: list[int] = []
        worker = _Worker(
            deps,
            stats,
            lock,
            stop,
            seen_ids,
            collected_ids,
            hook,
            max_pages=max_pages,
            sleep=sleep,
            now=now,
            jitter=jitter,
            upsert_every_pages=upsert_every_pages,
            upsert_every_items=upsert_every_items,
        )
        completed = 0
        with ThreadPoolExecutor(max_workers=discovery_concurrency) as pool:
            futures = {}
            for shard_id in shard_ids:
                if stop.is_set():
                    break
                context = contextvars.copy_context()
                futures[pool.submit(context.run, worker.process, shard_id)] = shard_id
            error: BaseException | None = None
            for future in as_completed(futures):
                exc = future.exception()
                if exc is not None:
                    error = exc
                    pool.shutdown(wait=True, cancel_futures=True)
                    break
                with lock:
                    completed += 1
                    counters = _progress_counters(stats, total_count)
                progress.report("discovering", completed, stats.shards, **counters)
            if error is not None:
                raise error
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
