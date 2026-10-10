from __future__ import annotations

import contextvars
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from lib import audit, cancellation
from lib.deadlines import Deadline, DeadlineExceededError
from lib.gh_client import (
    PartialResultsError,
    RequestFailed,
    ThrottledError,
    request_with_retry,
    short_message,
)
from limiter import secondary
from limiter.adaptive import AdaptiveController
from limiter.buckets import BucketLimiter
from limiter.classifier import rate_limit_wait

GRAPHQL_URL = "https://api.github.com/graphql"
MAX_BATCH_SIZE = 50
DEFAULT_BATCH_SIZE = 29
DEFAULT_MAX_ATTEMPTS = 3
_MAX_SECONDARY_HITS_PER_RUN = 15
_TIMEOUT_STATUSES = frozenset({499, 502, 504})
_MAX_FALLBACK_WORKERS = 16
_DEFAULT_RATE_LIMIT_PAUSE = 60.0
_RATE_LIMIT_MARKERS = ("rate limit", "secondary rate", "abuse")
_ADAPTIVE_SLEEP_SLICE = 0.5
_TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "resource limits",
    "something went wrong",
    "try again",
    "temporarily",
    "rate limit",
    "secondary rate",
    "abuse",
)


class GraphQLAuthError(RuntimeError):
    """GitHub rejected the credentials; the whole run must fail loudly."""


class MalformedResponse(ValueError):
    """The GraphQL endpoint returned something that is not a JSON object."""


@dataclass(frozen=True)
class ParsedBatch[T]:
    values: Mapping[str, T] = field(default_factory=dict)
    failures: Mapping[str, str] = field(default_factory=dict)
    rate_limit: RateLimitInfo | None = None


@dataclass(frozen=True)
class RateLimitInfo:
    cost: int | None = None
    used: int | None = None
    remaining: int | None = None


def _as_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def rate_limit_from_payload(payload: object) -> RateLimitInfo | None:
    """Read `data.rateLimit { cost used remaining }` from a GraphQL response body."""
    if not isinstance(payload, Mapping):
        return None
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None
    node = data.get("rateLimit")
    if not isinstance(node, Mapping):
        return None
    return RateLimitInfo(
        cost=_as_int(node.get("cost")),
        used=_as_int(node.get("used")),
        remaining=_as_int(node.get("remaining")),
    )


class GraphQLAdapter[T](Protocol):
    name: str
    batch_size: int

    def build_query(self, aliases: Mapping[str, str]) -> str: ...

    def parse(
        self, payload: Mapping[str, object], aliases: Mapping[str, str]
    ) -> ParsedBatch[T]: ...


@dataclass
class BatchStats:
    keys: int = 0
    requests: int = 0
    values: int = 0
    fallbacks: int = 0
    handled: int = 0
    unresolved: int = 0
    requeues: int = 0
    deadline_hit: bool = False
    deferred: int = 0
    adaptive: dict[str, object] = field(default_factory=dict)
    points_cost: int = 0
    points_used: int | None = None
    points_remaining: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "keys": self.keys,
            "requests": self.requests,
            "values": self.values,
            "fallbacks": self.fallbacks,
            "handled": self.handled,
            "unresolved": self.unresolved,
            "requeues": self.requeues,
            "deadline_hit": self.deadline_hit,
            "points_cost": self.points_cost,
            "points_used": self.points_used,
            "points_remaining": self.points_remaining,
            "deferred": self.deferred,
            "adaptive": self.adaptive,
        }


def _fold_rate_limit(stats: BatchStats, info: RateLimitInfo) -> None:
    if info.cost is not None:
        stats.points_cost += info.cost
    if info.used is not None:
        stats.points_used = (
            info.used if stats.points_used is None else max(stats.points_used, info.used)
        )
    if info.remaining is not None:
        stats.points_remaining = (
            info.remaining
            if stats.points_remaining is None
            else min(stats.points_remaining, info.remaining)
        )


@dataclass(frozen=True)
class BatchOutcome[T]:
    values: Mapping[str, T]
    unresolved: Mapping[str, str]
    stats: BatchStats


def _alias_map(keys: Sequence[str]) -> dict[str, str]:
    return {f"n{index}": key for index, key in enumerate(keys)}


def _chunks(keys: Sequence[str], size: int) -> list[list[str]]:
    return [list(keys[start : start + size]) for start in range(0, len(keys), size)]


def _error_entries(payload: Mapping[str, object]) -> list[tuple[str, tuple[str, ...]]]:
    raw = payload.get("errors")
    if not isinstance(raw, list):
        return []
    entries: list[tuple[str, tuple[str, ...]]] = []
    for error in raw:
        if not isinstance(error, dict):
            continue
        message = error.get("message")
        text = message if isinstance(message, str) else "unknown graphql error"
        raw_path = error.get("path")
        path = tuple(str(part) for part in raw_path) if isinstance(raw_path, list) else ()
        entries.append((text, path))
    return entries


def _is_transient(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


def is_transient_error(message: str) -> bool:
    """Public form of the batch engine's transient-error classifier."""
    return _is_transient(message)


def _adaptive_hook(
    adaptive: AdaptiveController,
    on_response: Callable[[httpx.Response, float], None] | None,
) -> Callable[[httpx.Response, float], None]:
    def hook(response: httpx.Response, latency_ms: float) -> None:
        adaptive.observe_response(response.headers, latency_ms)
        if response.status_code in (403, 429):
            adaptive.note_secondary()
        if on_response is not None:
            on_response(response, latency_ms)

    return hook


def _adaptive_sleep(seconds: float, sleep: Callable[[float], None]) -> None:
    remaining = max(0.0, seconds)
    while remaining > 0.0:
        cancellation.check()
        step = min(remaining, _ADAPTIVE_SLEEP_SLICE)
        sleep(step)
        remaining -= step


def _pause_bucket(
    limiter: BucketLimiter, token_id: str | None, seconds: float, now_value: float
) -> None:
    limiter.pause_at_least("graphql", token_id or "", seconds, now=now_value)


def _is_rate_limit(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


def _pause_if_rate_limited(
    limiter: BucketLimiter | None,
    token_id: str | None,
    headers: Mapping[str, str],
    batch_errors: Sequence[str],
    *,
    now: Callable[[], float],
    jitter: Callable[[], float] | None,
) -> None:
    if limiter is None or token_id is None:
        return
    if not any(_is_rate_limit(message) for message in batch_errors):
        return
    wait = rate_limit_wait(headers, now=now())
    if wait is not None:
        seconds = wait[1]
    else:
        seconds = _DEFAULT_RATE_LIMIT_PAUSE + (jitter() if jitter is not None else 0.0)
    if seconds > 0:
        limiter.pause_at_least("graphql", token_id, seconds, now=now())


def _post(
    adapter: GraphQLAdapter,
    keys: Sequence[str],
    *,
    client: httpx.Client,
    limiter: BucketLimiter | None,
    token_id: str | None,
    on_response: Callable[[httpx.Response, float], None] | None,
    sleep: Callable[[float], None],
    now: Callable[[], float],
    jitter: Callable[[], float] | None,
) -> tuple[ParsedBatch, tuple[str, ...]]:
    aliases = _alias_map(keys)
    response = request_with_retry(
        client,
        "POST",
        GRAPHQL_URL,
        json_body={"query": adapter.build_query(aliases)},
        limiter=limiter,
        token_id=token_id,
        on_response=on_response,
        sleep=sleep,
        now=now,
        jitter=jitter,
        retry_5xx=False,
    )
    if response.status_code == 401:
        raise GraphQLAuthError("github rejected the token (HTTP 401)")
    if response.status_code != 200:
        raise RequestFailed(int(response.status_code), short_message(response))
    payload = audit.cached_json(response)
    if payload is None:
        try:
            payload = response.json()
        except ValueError:
            raise MalformedResponse("graphql response is not JSON") from None
    if not isinstance(payload, dict):
        raise MalformedResponse("graphql response is not an object")
    parsed = adapter.parse(payload, aliases)
    values = dict(parsed.values)
    failures = dict(parsed.failures)
    batch_errors: list[str] = []
    aliases_by_name = {alias: key for alias, key in aliases.items()}
    for message, path in _error_entries(payload):
        if path and path[0] in aliases_by_name:
            key = aliases_by_name[path[0]]
            if key not in values and key not in failures:
                failures[key] = message
        else:
            batch_errors.append(message)
    _pause_if_rate_limited(
        limiter, token_id, response.headers, batch_errors, now=now, jitter=jitter
    )
    return (
        ParsedBatch(values=values, failures=failures, rate_limit=parsed.rate_limit),
        tuple(batch_errors),
    )


def fetch_batch(
    adapter: GraphQLAdapter,
    keys: Sequence[str],
    *,
    client: httpx.Client,
    limiter: BucketLimiter | None = None,
    token_id: str | None = None,
    fallback: Callable[[str], object | None] | None = None,
    allow_requests: bool = True,
    deadline: Deadline | None = None,
    on_response: Callable[[httpx.Response, float], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    on_progress: Callable[[int, int], None] | None = None,
    on_resolved: Callable[[Sequence[str], Mapping[str, object]], None] | None = None,
    concurrency: int = 1,
    adaptive: AdaptiveController | None = None,
) -> BatchOutcome:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    size = adapter.batch_size
    if not 1 <= size <= MAX_BATCH_SIZE:
        raise ValueError(f"adapter batch_size must be 1..{MAX_BATCH_SIZE}")
    pool_workers = adaptive.ceiling if adaptive is not None else concurrency
    unique = list(dict.fromkeys(keys))
    stats = BatchStats(keys=len(unique))
    if adaptive is not None:
        stats.adaptive = adaptive.snapshot()
    values: dict[str, object] = {}
    unresolved: dict[str, str] = {}
    handled_keys: set[str] = set()
    if not unique:
        if adaptive is not None:
            stats.adaptive = adaptive.snapshot()
        return BatchOutcome(values=values, unresolved=unresolved, stats=stats)
    attempts = dict.fromkeys(unique, 1)
    if adaptive is None:
        queue: deque[list[str]] = deque(_chunks(unique, size))
    else:
        queue = deque([unique])  # one contiguous run; sliced live at dispatch

    fallback_pool = (
        ThreadPoolExecutor(max_workers=min(pool_workers, _MAX_FALLBACK_WORKERS))
        if fallback is not None
        else None
    )
    fallback_futures: dict[Future[tuple[str, str, object]], str] = {}

    def run_fallback(key: str, reason: str) -> tuple[str, str, object]:
        assert fallback is not None
        try:
            result = fallback(key)
        except (GraphQLAuthError, cancellation.RunCancelled):
            raise
        except Exception as exc:  # a fallback failure becomes a typed unresolved repo
            return key, "error", f"{reason}; fallback failed: {type(exc).__name__}: {exc}"
        if result is None:
            return key, "handled", None
        return key, "value", result

    def fold_fallback(future: Future[tuple[str, str, object]]) -> None:
        key, kind, payload = future.result()
        if kind == "error":
            unresolved[key] = str(payload)
        elif kind == "handled":
            stats.handled += 1
            handled_keys.add(key)
        else:
            values[key] = payload
            stats.fallbacks += 1

    def drain_fallbacks(*, block: bool) -> None:
        if not fallback_futures:
            return
        if block:
            wait(list(fallback_futures))
        folded = [item for item in fallback_futures if item.done()]
        for future in folded:
            del fallback_futures[future]
            fold_fallback(future)
        if folded and on_progress is not None:
            on_progress(
                len(values) + len(unresolved) + len(handled_keys),
                len(unique),
            )

    def fall_back(key: str, reason: str) -> None:
        if fallback_pool is None:
            unresolved[key] = reason
            return
        fallback_futures[fallback_pool.submit(run_fallback, key, reason)] = key

    try:
        if not allow_requests:
            for key in unique:
                fall_back(key, "graphql batching disabled")
            drain_fallbacks(block=True)
            stats.values = len(values)
            stats.unresolved = len(unresolved)
            if adaptive is not None:
                stats.adaptive = adaptive.snapshot()
            return BatchOutcome(values=values, unresolved=unresolved, stats=stats)

        hook = on_response if adaptive is None else _adaptive_hook(adaptive, on_response)

        def requeue(failed: list[str]) -> list[str]:
            fresh: list[str] = []
            exhausted: list[str] = []
            for key in failed:
                if attempts[key] >= max_attempts:
                    exhausted.append(key)
                else:
                    attempts[key] += 1
                    fresh.append(key)
            if fresh:
                stats.requeues += 1
                if len(fresh) > 1:
                    middle = len(fresh) // 2
                    queue.appendleft(fresh[:middle])
                    queue.appendleft(fresh[middle:])
                else:
                    queue.appendleft(fresh)
            return exhausted

        def submit_chunk(pool: ThreadPoolExecutor, pending: list[str]):
            context = contextvars.copy_context()
            return pool.submit(
                context.run,
                _post,
                adapter,
                pending,
                client=client,
                limiter=limiter,
                token_id=token_id,
                on_response=hook,
                sleep=sleep,
                now=now,
                jitter=jitter,
            )

        with ThreadPoolExecutor(max_workers=pool_workers) as pool:
            in_flight: dict[Future, tuple[list[str], float]] = {}
            secondary_baseline = secondary.total()
            secondary_budget_stopped = False
            while queue or in_flight or fallback_futures:
                cancellation.check()
                throttle = 0.0
                if adaptive is not None:
                    if adaptive.should_stop():
                        marked = 0
                        while queue:
                            for key in queue.popleft():
                                if (
                                    key not in values
                                    and key not in unresolved
                                    and key not in handled_keys
                                ):
                                    unresolved[key] = (
                                        "deferred: github graphql point reserve reached"
                                    )
                                    marked += 1
                        stats.deferred += marked
                        adaptive.note_deferred(marked)
                    throttle = max(adaptive.pause_remaining(), adaptive.pacer_delay())
                if (
                    not secondary_budget_stopped
                    and secondary.total() - secondary_baseline >= _MAX_SECONDARY_HITS_PER_RUN
                ):
                    secondary_budget_stopped = True
                    marked = 0
                    while queue:
                        for key in queue.popleft():
                            if (
                                key not in values
                                and key not in unresolved
                                and key not in handled_keys
                            ):
                                unresolved[key] = "secondary rate limit budget exhausted"
                                marked += 1
                    stats.deferred += marked
                    if adaptive is not None:
                        adaptive.note_deferred(marked)
                gate = adaptive.window if adaptive is not None else concurrency
                while queue and len(in_flight) < gate and throttle <= 0.0:
                    chunk = queue.popleft()
                    budget = min(adaptive.batch_size, size) if adaptive is not None else size
                    if len(chunk) > budget:
                        queue.appendleft(chunk[budget:])
                        chunk = chunk[:budget]
                    pending = [
                        key
                        for key in chunk
                        if key not in values and key not in unresolved and key not in handled_keys
                    ]
                    if not pending:
                        continue
                    if deadline is not None and deadline.remaining <= 0:
                        stats.deadline_hit = True
                        for key in pending:
                            unresolved[key] = "run deadline exceeded"
                        continue
                    stats.requests += 1
                    if adaptive is not None:
                        adaptive.on_dispatch()
                    in_flight[submit_chunk(pool, pending)] = (pending, now())
                    if adaptive is not None:
                        throttle = max(adaptive.pause_remaining(), adaptive.pacer_delay())
                if not in_flight:
                    if throttle > 0.0:
                        drain_fallbacks(block=False)
                        _adaptive_sleep(min(throttle, _ADAPTIVE_SLEEP_SLICE), sleep)
                        continue
                    drain_fallbacks(block=True)
                    continue
                done, _pending_futures = wait(
                    in_flight,
                    return_when=FIRST_COMPLETED,
                    timeout=None if throttle <= 0.0 else min(throttle, _ADAPTIVE_SLEEP_SLICE),
                )
                for future in done:
                    pending, _submitted = in_flight.pop(future)
                    transient = False
                    status: int | None = None
                    throttled = False
                    try:
                        parsed, batch_errors = future.result()
                        if parsed.rate_limit is not None:
                            _fold_rate_limit(stats, parsed.rate_limit)
                    except GraphQLAuthError:
                        raise
                    except DeadlineExceededError:
                        stats.deadline_hit = True
                        for key in pending:
                            unresolved[key] = "run deadline exceeded"
                        continue
                    except (
                        PartialResultsError,
                        RequestFailed,
                        MalformedResponse,
                        ThrottledError,
                        httpx.HTTPError,
                    ) as exc:
                        parsed = ParsedBatch()
                        batch_errors = (f"{type(exc).__name__}: {exc}",)
                        if isinstance(exc, RequestFailed):
                            status = exc.status
                        throttled = isinstance(exc, ThrottledError)
                        transient = (
                            isinstance(exc, RequestFailed) and exc.status in _TIMEOUT_STATUSES
                        )
                    resolved = {
                        key: value for key, value in parsed.values.items() if key in pending
                    }
                    values.update(resolved)
                    if on_resolved is not None and resolved:
                        on_resolved(tuple(resolved), resolved)
                    failed = [key for key in pending if key not in values]
                    reason = batch_errors[0] if batch_errors else ""
                    if batch_errors:
                        if transient or _is_transient(reason):
                            for key in requeue(failed):
                                fall_back(key, reason)
                        else:
                            for key in failed:
                                fall_back(key, reason)
                    else:
                        for key in failed:
                            fall_back(key, parsed.failures.get(key, "missing result"))
                    if adaptive is not None:
                        pause = adaptive.record_batch(
                            drop=(
                                throttled
                                or status in (403, 429)
                                or status in _TIMEOUT_STATUSES
                                or _is_rate_limit(reason)
                            ),
                            guard=transient or _is_transient(reason),
                            in_flight=len(in_flight),
                        )
                        if pause > 0.0 and limiter is not None:
                            _pause_bucket(limiter, token_id, pause, now())
                    if on_progress is not None:
                        on_progress(
                            len(values) + len(unresolved) + len(handled_keys),
                            len(unique),
                        )
                drain_fallbacks(block=False)
        stats.values = len(values)
        stats.unresolved = len(unresolved)
        if adaptive is not None:
            stats.adaptive = adaptive.snapshot()
        return BatchOutcome(values=values, unresolved=unresolved, stats=stats)
    finally:
        if fallback_pool is not None:
            fallback_pool.shutdown(wait=True, cancel_futures=True)
