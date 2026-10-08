from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Engine

from scheduler.shard_planner import ShardSpec
from store.models import Shard


class ShardState(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    INCOMPLETE = "incomplete"


ALLOWED_TRANSITIONS: dict[ShardState, frozenset[ShardState]] = {
    ShardState.PENDING: frozenset({ShardState.ACTIVE}),
    ShardState.ACTIVE: frozenset({ShardState.DONE, ShardState.INCOMPLETE, ShardState.PENDING}),
    ShardState.DONE: frozenset(),
    ShardState.INCOMPLETE: frozenset(),
}

_SOURCES: dict[ShardState, frozenset[ShardState]] = {
    target: frozenset(
        source for source, targets in ALLOWED_TRANSITIONS.items() if target in targets
    )
    for target in ShardState
}


@dataclass(frozen=True)
class ShardRow:
    id: int
    kind: str
    query: str | None
    range_start: datetime | None
    range_end: datetime | None
    state: ShardState
    total_count: int | None
    fetched: int | None
    incomplete: bool


class ShardStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create(self, spec: ShardSpec, *, kind: str = "search-range") -> int:
        with self._engine.begin() as connection:
            shard_id = connection.execute(
                insert(Shard)
                .values(
                    kind=kind,
                    query=spec.query,
                    range_start=spec.range_start,
                    range_end=spec.range_end,
                    total_count=spec.total_count,
                )
                .returning(Shard.id)
            ).scalar_one()
        return int(shard_id)

    def get(self, shard_id: int) -> ShardRow:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        Shard.id,
                        Shard.kind,
                        Shard.query,
                        Shard.range_start,
                        Shard.range_end,
                        Shard.state,
                        Shard.total_count,
                        Shard.fetched,
                        Shard.incomplete,
                    ).where(Shard.id == shard_id)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise KeyError(shard_id)
        return ShardRow(
            id=row["id"],
            kind=row["kind"],
            query=row["query"],
            range_start=row["range_start"],
            range_end=row["range_end"],
            state=ShardState(row["state"]),
            total_count=row["total_count"],
            fetched=row["fetched"],
            incomplete=row["incomplete"],
        )

    def set_state(
        self,
        shard_id: int,
        new_state: ShardState,
        *,
        fetched: int | None = None,
        incomplete: bool | None = None,
        total_count: int | None = None,
    ) -> None:
        values: dict[str, object] = {"state": new_state.value, "updated_at": func.now()}
        if fetched is not None:
            values["fetched"] = fetched
        if incomplete is not None:
            values["incomplete"] = incomplete
        if total_count is not None:
            values["total_count"] = total_count
        allowed = [state.value for state in _SOURCES[new_state]]
        with self._engine.begin() as connection:
            updated = connection.execute(
                update(Shard)
                .where(Shard.id == shard_id, Shard.state.in_(allowed))
                .values(**values)
                .returning(Shard.id)
            ).scalar_one_or_none()
            if updated is None:
                current = connection.execute(
                    select(Shard.state).where(Shard.id == shard_id)
                ).scalar_one_or_none()
                if current is None:
                    raise KeyError(shard_id)
                raise ValueError(f"illegal shard transition {current} -> {new_state.value}")

    def counts(self) -> dict[str, int]:
        counts = {state.value: 0 for state in ShardState}
        with self._engine.connect() as connection:
            rows = connection.execute(select(Shard.state, func.count()).group_by(Shard.state)).all()
        for state, total in rows:
            counts[str(state)] = int(total)
        return counts
