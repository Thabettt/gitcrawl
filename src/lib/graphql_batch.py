from __future__ import annotations

import contextvars
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from lib import cancellation
from lib.deadlines import Deadline, DeadlineExceededError
from lib.gh_client import PartialResultsError, RequestFailed, ThrottledError, request_with_retry
from limiter.buckets import BucketLimiter

GRAPHQL_URL = "https://api.github.com/graphql"
MAX_BATCH_SIZE = 20
DEFAULT_MAX_ATTEMPTS = 3
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
        }


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
    )
    if response.status_code == 401:
        raise GraphQLAuthError("github rejected the token (HTTP 401)")
    if response.status_code != 200:
        raise RequestFailed(int(response.status_code), "graphql request failed")
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
    return ParsedBatch(values=values, failures=failures), tuple(batch_errors)


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
    concurrency: int = 1,
) -> BatchOutcome:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    size = adapter.batch_size
    if not 1 <= size <= MAX_BATCH_SIZE:
        raise ValueError(f"adapter batch_size must be 1..{MAX_BATCH_SIZE}")
    unique = list(dict.fromkeys(keys))
    stats = BatchStats(keys=len(unique))
    values: dict[str, object] = {}
    unresolved: dict[str, str] = {}
    handled_keys: set[str] = set()
    if not unique:
        return BatchOutcome(values=values, unresolved=unresolved, stats=stats)
    attempts = dict.fromkeys(unique, 1)
    queue: deque[list[str]] = deque(_chunks(unique, size))

    def fall_back(key: str, reason: str) -> None:
        if fallback is None:
            unresolved[key] = reason
            return
        try:
            result = fallback(key)
        except GraphQLAuthError:
            raise
        except cancellation.RunCancelled:
            raise
        except Exception as exc:  # a fallback failure becomes a typed unresolved repo
            unresolved[key] = f"{reason}; fallback failed: {type(exc).__name__}: {exc}"
            return
        if result is None:
            stats.handled += 1
            handled_keys.add(key)
        else:
            values[key] = result
            stats.fallbacks += 1

    if not allow_requests:
        for key in unique:
            fall_back(key, "graphql batching disabled")
        stats.values = len(values)
        stats.unresolved = len(unresolved)
        return BatchOutcome(values=values, unresolved=unresolved, stats=stats)

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
            on_response=on_response,
            sleep=sleep,
            now=now,
            jitter=jitter,
        )

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        in_flight: dict[Future, list[str]] = {}
        while queue or in_flight:
            cancellation.check()
            while queue and len(in_flight) < concurrency:
                chunk = queue.popleft()
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
                in_flight[submit_chunk(pool, pending)] = pending
            if not in_flight:
                continue
            done, _pending_futures = wait(in_flight, return_when=FIRST_COMPLETED)
            for future in done:
                pending = in_flight.pop(future)
                try:
                    parsed, batch_errors = future.result()
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
                for key, value in parsed.values.items():
                    if key in pending:
                        values[key] = value
                failed = [key for key in pending if key not in values]
                if batch_errors:
                    reason = batch_errors[0]
                    if _is_transient(reason):
                        for key in requeue(failed):
                            fall_back(key, reason)
                    else:
                        for key in failed:
                            fall_back(key, reason)
                else:
                    for key in failed:
                        fall_back(key, parsed.failures.get(key, "missing result"))
                if on_progress is not None:
                    on_progress(
                        len(values) + len(unresolved) + len(handled_keys),
                        len(unique),
                    )
    stats.values = len(values)
    stats.unresolved = len(unresolved)
    return BatchOutcome(values=values, unresolved=unresolved, stats=stats)
