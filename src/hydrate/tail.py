from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select
from sqlalchemy.engine import Engine

from discover.search_shards import RequestFailed
from hydrate.graphql_repo import RepoDetailsAdapter
from hydrate.repo_client import HydratedRepo, RepoNotFound, hydrate_repo
from lib.batching import chunked
from lib.deadlines import Deadline
from lib.gh_client import PartialResultsError, ThrottledError
from lib.graphql_batch import MAX_BATCH_SIZE, GraphQLAuthError, fetch_batch
from limiter.buckets import BucketLimiter
from store.lifecycle import apply_hydration
from store.models import Repo

_NAME_BATCH = 5000


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
    batch_size: int = MAX_BATCH_SIZE,
) -> RefreshStats:
    stats = RefreshStats()
    candidates = [(str(row["id"]), str(row["full_name"])) for row in rows]
    if not candidates:
        return stats
    etags = _stored_etags(engine, [full_name for _, full_name in candidates])
    full_name_by_key = dict(candidates)

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
            stats.tombstoned += 1
            return None
        except RequestFailed as exc:
            if exc.status == 401:
                raise GraphQLAuthError("github rejected the token (HTTP 401)") from exc
            stats.failed += 1
            raise RuntimeError(f"REST fallback failed with HTTP {exc.status}") from exc
        except ThrottledError as exc:
            stats.failed += 1
            raise RuntimeError(f"REST fallback throttled: {exc}") from exc
        if hydrated.not_modified:
            stats.not_modified += 1
            return None
        outcome = apply_hydration(engine, full_name, hydrated)
        stats.refreshed += 1
        stats.fallbacks += 1
        if outcome.renamed_from is not None:
            stats.renamed += 1
        return None

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
    )
    for key, details in outcome.values.items():
        hydrated = HydratedRepo(
            id=details.repo_id,
            node_id=details.node_id,
            full_name=details.full_name,
            payload=details.payload,
            etag=None,
            not_modified=False,
        )
        result = apply_hydration(engine, full_name_by_key[key], hydrated)
        stats.refreshed += 1
        if result.renamed_from is not None:
            stats.renamed += 1
    stats.unresolved = {full_name_by_key[key]: reason for key, reason in outcome.unresolved.items()}
    stats.batch = outcome.stats.as_dict()
    return stats
