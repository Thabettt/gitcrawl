from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from lib.batching import chunked
from store.models import FullNameHistory, Repo
from store.upserts import upsert_repos

if TYPE_CHECKING:
    from hydrate.repo_client import HydratedRepo


@dataclass(frozen=True)
class LifecycleOutcome:
    repo_id: int | None
    renamed_from: str | None
    history_added: bool
    tombstoned: bool


def _resolve_now(now: datetime | Callable[[], datetime] | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if callable(now):
        return now()
    return now


def _repo_id(engine: Engine, full_name: str) -> int | None:
    with engine.connect() as connection:
        return connection.execute(
            select(Repo.id).where(Repo.full_name == full_name)
        ).scalar_one_or_none()


def _record_history(engine: Engine, repo_id: int, full_name: str) -> None:
    statement = (
        pg_insert(FullNameHistory)
        .values(repo_id=repo_id, full_name=full_name)
        .on_conflict_do_nothing(index_elements=["repo_id", "full_name"])
    )
    with engine.begin() as connection:
        connection.execute(statement)


def _record_history_batch(engine: Engine, rows: list[dict], *, batch_size: int = 500) -> None:
    for batch in chunked(rows, batch_size):
        statement = (
            pg_insert(FullNameHistory)
            .values(batch)
            .on_conflict_do_nothing(index_elements=["repo_id", "full_name"])
        )
        with engine.begin() as connection:
            connection.execute(statement)


def apply_hydration_batch(
    engine: Engine,
    items: Sequence[tuple[str, HydratedRepo]],
    *,
    batch_size: int = 500,
) -> list[LifecycleOutcome]:
    resolved: list[tuple[str, HydratedRepo, dict]] = []
    for requested_full_name, hydrated in items:
        payload = hydrated.payload
        if payload is None:
            raise ValueError("hydrated payload is required for a 200 response")
        resolved.append((requested_full_name, hydrated, payload))

    chosen: dict[int, tuple[HydratedRepo, dict]] = {}
    for _requested, hydrated, payload in resolved:
        chosen[cast(int, payload["id"])] = (hydrated, payload)

    names: dict[str, int] = {}
    for repo_id, (_hydrated, payload) in chosen.items():
        key = str(payload["full_name"]).casefold()
        if names.setdefault(key, repo_id) != repo_id:
            return [
                apply_hydration(engine, requested, hydrated)
                for requested, hydrated, _payload in resolved
            ]

    plain: list[dict] = []
    with_etags: list[dict] = []
    etags: dict[int, str | None] = {}
    for repo_id, (hydrated, payload) in chosen.items():
        if hydrated.etag is not None:
            with_etags.append(payload)
            etags[repo_id] = hydrated.etag
        else:
            plain.append(payload)
    if plain:
        upsert_repos(engine, plain, batch_size=batch_size)
    if with_etags:
        upsert_repos(engine, with_etags, batch_size=batch_size, etags=etags)

    history: dict[tuple[int, str], None] = {}
    outcomes: list[LifecycleOutcome] = []
    for requested_full_name, _hydrated, payload in resolved:
        repo_id = cast(int, payload["id"])
        renamed_from = None
        history_added = False
        if payload["full_name"] != requested_full_name:
            renamed_from = requested_full_name
            history_added = True
            history[(repo_id, requested_full_name)] = None
        outcomes.append(
            LifecycleOutcome(
                repo_id=repo_id,
                renamed_from=renamed_from,
                history_added=history_added,
                tombstoned=False,
            )
        )
    if history:
        _record_history_batch(
            engine,
            [{"repo_id": repo_id, "full_name": name} for repo_id, name in history],
            batch_size=batch_size,
        )
    return outcomes


def apply_hydration(
    engine: Engine,
    requested_full_name: str,
    hydrated: HydratedRepo | None,
    *,
    not_found: bool = False,
    now: datetime | Callable[[], datetime] | None = None,
) -> LifecycleOutcome:
    if not_found:
        repo_id = _repo_id(engine, requested_full_name)
        tombstoned = tombstone(engine, requested_full_name, now=now)
        return LifecycleOutcome(
            repo_id=repo_id,
            renamed_from=None,
            history_added=False,
            tombstoned=tombstoned,
        )
    if hydrated is None:
        raise ValueError("hydrated is required when not_found is False")
    if hydrated.not_modified:
        return LifecycleOutcome(
            repo_id=_repo_id(engine, requested_full_name),
            renamed_from=None,
            history_added=False,
            tombstoned=False,
        )
    payload = hydrated.payload
    if payload is None:
        raise ValueError("hydrated payload is required for a 200 response")
    etags = {payload["id"]: hydrated.etag} if hydrated.etag is not None else None
    upsert_repos(engine, [payload], etags=etags)
    repo_id = cast(int, payload.get("id"))
    renamed_from = None
    history_added = False
    if payload.get("full_name") != requested_full_name:
        renamed_from = requested_full_name
        _record_history(engine, repo_id, requested_full_name)
        history_added = True
    return LifecycleOutcome(
        repo_id=repo_id,
        renamed_from=renamed_from,
        history_added=history_added,
        tombstoned=False,
    )


def tombstone(
    engine: Engine,
    full_name: str,
    *,
    now: datetime | Callable[[], datetime] | None = None,
) -> bool:
    when = _resolve_now(now)
    with engine.begin() as connection:
        result = connection.execute(
            update(Repo)
            .where(Repo.full_name == full_name)
            .where(Repo.deleted_at.is_(None))
            .values(deleted_at=when)
        )
    return result.rowcount > 0


def purge_tombstones(
    engine: Engine,
    *,
    retention_days: int = 30,
    now: datetime | Callable[[], datetime] | None = None,
    batch_size: int = 1000,
) -> int:
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    cutoff = _resolve_now(now) - timedelta(days=retention_days)
    total = 0
    while True:
        with engine.begin() as connection:
            ids = select(Repo.id).where(Repo.deleted_at < cutoff).limit(batch_size)
            result = connection.execute(delete(Repo).where(Repo.id.in_(ids)))
            removed = int(result.rowcount or 0)
        total += removed
        if removed < batch_size:
            return total
