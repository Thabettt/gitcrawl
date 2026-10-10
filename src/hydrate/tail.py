from __future__ import annotations

import contextvars
import queue
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select
from sqlalchemy.engine import Engine

from hydrate.graphql_repo import RepoDetailsAdapter
from hydrate.repo_client import HydratedRepo, RepoNotFound, fetch_commit_count, hydrate_repo
from lib import cancellation
from lib.batching import chunked
from lib.deadlines import Deadline
from lib.gh_client import PartialResultsError, RequestFailed, ThrottledError
from lib.graphql_batch import DEFAULT_BATCH_SIZE, GraphQLAuthError, fetch_batch
from limiter.buckets import BucketLimiter
from store.lifecycle import LifecycleOutcome, apply_hydration, apply_hydration_batch
from store.models import Repo

_NAME_BATCH = 5000
_APPLY_BATCH = 500
_APPLY_QUEUE_DEPTH = 20
_QUEUE_POLL_SECONDS = 1.0


@dataclass
class RefreshStats:
    refreshed: int = 0
    not_modified: int = 0
    renamed: int = 0
    tombstoned: int = 0
    failed: int = 0
    fallbacks: int = 0
    unresolved: dict[str, str] = field(default_factory=dict)
    batch: dict = field(default_factory=dict)
    commit_counts: dict[str, int] = field(default_factory=dict)
    language_bytes: dict[str, dict[str, int]] = field(default_factory=dict)
    fetch_seconds: float = 0.0
    apply_seconds: float = 0.0


def _stored_etags(
    engine: Engine, names: list[str], *, batch_size: int = _NAME_BATCH
) -> dict[str, str | None]:
    if not names:
        return {}
    merged: dict[str, str | None] = {}
    with engine.connect() as connection:
        for batch in chunked(names, batch_size):
            rows = connection.execute(
                select(Repo.full_name, Repo.etag).where(Repo.full_name.in_(batch))
            ).all()
            merged.update({str(full_name).casefold(): etag for full_name, etag in rows})
    return merged


def refresh_repos(
    engine: Engine,
    client: httpx.Client,
    full_names: Iterable[str],
    *,
    limiter: BucketLimiter | None = None,
    token_id: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
    on_response: Callable[[httpx.Response, float], None] | None = None,
) -> RefreshStats:
    stats = RefreshStats()
    names = list(full_names)
    etags = _stored_etags(engine, names)
    for full_name in names:
        try:
            hydrated = hydrate_repo(
                client,
                full_name,
                etag=etags.get(full_name.casefold()),
                limiter=limiter,
                token_id=token_id,
                sleep=sleep,
                now=now,
                jitter=jitter,
                on_response=on_response,
            )
        except RepoNotFound:
            apply_hydration(engine, full_name, None, not_found=True)
            stats.tombstoned += 1
            continue
        except (RequestFailed, ThrottledError, PartialResultsError):
            stats.failed += 1
            continue
        if hydrated.not_modified:
            stats.not_modified += 1
            continue
        outcome = apply_hydration(engine, full_name, hydrated)
        stats.refreshed += 1
        if outcome.renamed_from is not None:
            stats.renamed += 1
    return stats


def refresh_repos_batched(
    engine: Engine,
    client: httpx.Client,
    rows: Sequence[Mapping[str, object]],
    *,
    limiter: BucketLimiter | None = None,
    token_id: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
    on_response: Callable[[httpx.Response, float], None] | None = None,
    deadline: Deadline | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    allow_requests: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
    concurrency: int = 1,
    clock: Callable[[], float] = time.perf_counter,
    apply_batch_size: int = _APPLY_BATCH,
    on_apply_progress: Callable[[int, int], None] | None = None,
) -> RefreshStats:
    if apply_batch_size < 1:
        raise ValueError("apply_batch_size must be >= 1")
    stats = RefreshStats()
    candidates = [(str(row["id"]), str(row["full_name"])) for row in rows]
    if not candidates:
        return stats
    etags = _stored_etags(engine, [full_name for _, full_name in candidates])
    full_name_by_key = dict(candidates)
    key_by_full_name = {full_name: key for key, full_name in candidates}
    stats_lock = threading.Lock()

    def fallback(key: str) -> object | None:
        full_name = full_name_by_key[key]
        try:
            hydrated = hydrate_repo(
                client,
                full_name,
                etag=etags.get(full_name.casefold()),
                limiter=limiter,
                token_id=token_id,
                sleep=sleep,
                now=now,
                jitter=jitter,
                on_response=on_response,
            )
        except RepoNotFound:
            apply_hydration(engine, full_name, None, not_found=True)
            with stats_lock:
                stats.tombstoned += 1
            return None
        except RequestFailed as exc:
            if exc.status == 401:
                raise GraphQLAuthError("github rejected the token (HTTP 401)") from exc
            with stats_lock:
                stats.failed += 1
            raise RuntimeError(f"REST fallback failed with HTTP {exc.status}") from exc
        except ThrottledError as exc:
            with stats_lock:
                stats.failed += 1
            raise RuntimeError(f"REST fallback throttled: {exc}") from exc
        if hydrated.not_modified:
            with stats_lock:
                stats.not_modified += 1
            return None
        outcome = apply_hydration(engine, full_name, hydrated)
        with stats_lock:
            stats.refreshed += 1
            stats.fallbacks += 1
            if outcome.renamed_from is not None:
                stats.renamed += 1
        try:
            count = fetch_commit_count(
                client,
                full_name,
                limiter=limiter,
                token_id=token_id,
                sleep=sleep,
                now=now,
                jitter=jitter,
                on_response=on_response,
            )
        except (PartialResultsError, ThrottledError, httpx.HTTPError):
            count = None
        if count is not None:
            with stats_lock:
                stats.commit_counts[key] = count
        return None

    fetch_started = clock()
    applied: list[tuple[str, LifecycleOutcome]] = []
    apply_events: list[tuple[int, int]] = []
    details_by_key: dict[str, object] = {}
    errors: list[BaseException] = []
    busy_seconds = 0.0
    total = len(candidates)
    buffer: list[tuple[str, HydratedRepo]] = []
    work: queue.Queue[list[tuple[str, HydratedRepo]] | None] = queue.Queue(
        maxsize=_APPLY_QUEUE_DEPTH
    )
    producer_done = threading.Event()

    def consume() -> None:
        nonlocal busy_seconds
        if on_apply_progress is not None:
            apply_events.append((0, total))
        done = 0
        try:
            while True:
                try:
                    chunk = work.get(timeout=_QUEUE_POLL_SECONDS)
                except queue.Empty:
                    if producer_done.is_set():
                        break
                    continue
                if chunk is None:
                    break
                cancellation.check()
                started = time.perf_counter()
                try:
                    outcomes = apply_hydration_batch(engine, chunk, batch_size=apply_batch_size)
                finally:
                    busy_seconds += time.perf_counter() - started
                applied.extend(
                    (key, outcome) for (key, _details), outcome in zip(chunk, outcomes, strict=True)
                )
                done += len(chunk)
                if on_apply_progress is not None:
                    apply_events.append((done, total))
        except BaseException as exc:
            errors.append(exc)

    def enqueue(chunk: list[tuple[str, HydratedRepo]]) -> None:
        while True:
            if errors:
                raise errors[0]
            try:
                work.put(chunk, timeout=_QUEUE_POLL_SECONDS)
                return
            except queue.Full:
                cancellation.check()

    def on_resolved(keys: Sequence[str], values: Mapping[str, object]) -> None:
        for key in keys:
            details = values.get(key)
            if details is None:
                continue
            details_by_key[full_name_by_key[key]] = details
            buffer.append(
                (
                    full_name_by_key[key],
                    HydratedRepo(
                        id=details.repo_id,
                        node_id=details.node_id,
                        full_name=details.full_name,
                        payload=details.payload,
                        etag=None,
                        not_modified=False,
                    ),
                )
            )
        while len(buffer) >= apply_batch_size:
            enqueue(buffer[:apply_batch_size])
            del buffer[:apply_batch_size]

    context = contextvars.copy_context()
    worker = threading.Thread(
        target=context.run, args=(consume,), name="hydrate-apply", daemon=True
    )
    worker.start()
    try:
        outcome = fetch_batch(
            RepoDetailsAdapter(dict(candidates), batch_size=batch_size),
            [key for key, _ in candidates],
            client=client,
            limiter=limiter,
            token_id=token_id,
            fallback=fallback,
            deadline=deadline,
            on_response=on_response,
            sleep=sleep,
            now=now,
            jitter=jitter,
            allow_requests=allow_requests,
            on_progress=on_progress,
            on_resolved=on_resolved,
            concurrency=concurrency,
        )
        stats.fetch_seconds = clock() - fetch_started
        if buffer:
            enqueue(buffer[:])
            buffer.clear()
    finally:
        producer_done.set()
        if not errors:
            while True:
                try:
                    work.put(None, timeout=_QUEUE_POLL_SECONDS)
                    break
                except queue.Full:
                    if errors:
                        break
        worker.join()
    if errors:
        raise errors[0]
    stats.apply_seconds = busy_seconds
    for full_name, result in applied:
        details = details_by_key[full_name]
        repo_key = key_by_full_name[full_name]
        stats.refreshed += 1
        if result.renamed_from is not None:
            stats.renamed += 1
        if details.commit_count is not None:
            stats.commit_counts[repo_key] = details.commit_count
        if details.language_bytes:
            stats.language_bytes[repo_key] = details.language_bytes
    stats.unresolved = {full_name_by_key[key]: reason for key, reason in outcome.unresolved.items()}
    stats.batch = outcome.stats.as_dict()
    if on_apply_progress is not None:
        for done, total_count in apply_events:
            on_apply_progress(done, total_count)
    return stats
