from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.engine import Engine

from discover.search_shards import RequestFailed
from hydrate.repo_client import RepoNotFound, hydrate_repo
from lib.gh_client import PartialResultsError, ThrottledError
from limiter.buckets import BucketLimiter
from store.lifecycle import apply_hydration
from store.models import Repo


@dataclass
class RefreshStats:
    refreshed: int = 0
    not_modified: int = 0
    renamed: int = 0
    tombstoned: int = 0
    failed: int = 0


def _stored_etags(engine: Engine, names: list[str]) -> dict[str, str | None]:
    if not names:
        return {}
    with engine.connect() as connection:
        rows = connection.execute(
            select(Repo.full_name, Repo.etag).where(Repo.full_name.in_(names))
        ).all()
    return {str(full_name).casefold(): etag for full_name, etag in rows}


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
