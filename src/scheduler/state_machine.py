from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from redis.exceptions import ResponseError
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

_GROUP = "workers"
_SHARD_FIELD = "shard_id"
_ATTEMPTS_FIELD = "attempts"


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


@dataclass(frozen=True)
class QueuedShard:
    stream_id: str
    shard_id: int
    attempts: int


def _as_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def _get_field(fields, name: str) -> str | None:
    for key, value in fields.items():
        if _as_text(key) == name:
            return _as_text(value)
    return None


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
        with self._engine.begin() as connection:
            row = connection.execute(select(Shard.state).where(Shard.id == shard_id)).one_or_none()
            if row is None:
                raise KeyError(shard_id)
            current = ShardState(row[0])
            if new_state not in ALLOWED_TRANSITIONS[current]:
                raise ValueError(f"illegal shard transition {current.value} -> {new_state.value}")
            values: dict[str, object] = {"state": new_state.value}
            if fetched is not None:
                values["fetched"] = fetched
            if incomplete is not None:
                values["incomplete"] = incomplete
            if total_count is not None:
                values["total_count"] = total_count
            connection.execute(update(Shard).where(Shard.id == shard_id).values(**values))

    def counts(self) -> dict[str, int]:
        counts = {state.value: 0 for state in ShardState}
        with self._engine.connect() as connection:
            rows = connection.execute(select(Shard.state, func.count()).group_by(Shard.state)).all()
        for state, total in rows:
            counts[str(state)] = int(total)
        return counts


class ShardQueue:
    def __init__(
        self,
        redis,
        *,
        lanes: int = 4,
        max_attempts: int = 3,
        prefix: str = "gitcrawl:shards",
    ) -> None:
        self._redis = redis
        self._lanes = lanes
        self._max_attempts = max_attempts
        self._prefix = prefix

    def lane_for(self, shard_id: int) -> int:
        return shard_id % self._lanes

    def _lane_key(self, lane: int) -> str:
        return f"{self._prefix}:lane:{lane}"

    @property
    def _dlq_key(self) -> str:
        return f"{self._prefix}:dlq"

    def _ensure_group(self, lane: int) -> None:
        try:
            self._redis.xgroup_create(self._lane_key(lane), _GROUP, id="0", mkstream=True)
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    def _read(self, lane: int, consumer: str, start: str, count: int, block_ms: int):
        options = {"count": count}
        if block_ms > 0:
            options["block"] = block_ms
        raw = self._redis.xreadgroup(_GROUP, consumer, {self._lane_key(lane): start}, **options)
        if not raw:
            return []
        entries = []
        for _stream, batch in raw:
            entries.extend(batch)
        return entries

    @staticmethod
    def _queued(stream_id, fields) -> QueuedShard:
        return QueuedShard(
            stream_id=_as_text(stream_id),
            shard_id=int(_get_field(fields, _SHARD_FIELD) or 0),
            attempts=int(_get_field(fields, _ATTEMPTS_FIELD) or 0),
        )

    def enqueue(self, shard_id: int) -> str:
        lane = self.lane_for(shard_id)
        self._ensure_group(lane)
        stream_id = self._redis.xadd(
            self._lane_key(lane), {_SHARD_FIELD: str(shard_id), _ATTEMPTS_FIELD: "0"}
        )
        return _as_text(stream_id)

    def claim(self, consumer: str, *, count: int = 1, block_ms: int = 0) -> list[QueuedShard]:
        claimed: list[QueuedShard] = []
        for lane in range(self._lanes):
            if len(claimed) >= count:
                break
            self._ensure_group(lane)
            for start in ("0", ">"):
                remaining = count - len(claimed)
                if remaining <= 0:
                    break
                for stream_id, fields in self._read(lane, consumer, start, remaining, block_ms):
                    claimed.append(self._queued(stream_id, fields))
                    if len(claimed) >= count:
                        break
        return claimed

    def ack(self, stream_id: str, shard_id: int) -> None:
        key = self._lane_key(self.lane_for(shard_id))
        self._redis.xack(key, _GROUP, stream_id)
        self._redis.xdel(key, stream_id)

    def retry_or_dlq(self, stream_id: str, shard_id: int, *, attempts: int) -> str:
        key = self._lane_key(self.lane_for(shard_id))
        self._redis.xack(key, _GROUP, stream_id)
        self._redis.xdel(key, stream_id)
        target = key if attempts < self._max_attempts else self._dlq_key
        new_id = self._redis.xadd(
            target, {_SHARD_FIELD: str(shard_id), _ATTEMPTS_FIELD: str(attempts)}
        )
        return _as_text(new_id)

    def reclaim_stale(
        self, consumer: str, *, min_idle_ms: int = 60000, count: int = 10
    ) -> list[QueuedShard]:
        claimed: list[QueuedShard] = []
        for lane in range(self._lanes):
            if len(claimed) >= count:
                break
            self._ensure_group(lane)
            _cursor, entries, _deleted = self._redis.xautoclaim(
                self._lane_key(lane),
                _GROUP,
                consumer,
                min_idle_ms,
                "0-0",
                count=count - len(claimed),
            )
            for stream_id, fields in entries:
                queued = self._queued(stream_id, fields)
                claimed.append(
                    QueuedShard(
                        stream_id=queued.stream_id,
                        shard_id=queued.shard_id,
                        attempts=queued.attempts + 1,
                    )
                )
        return claimed

    def pel_size(self, lane: int | None = None) -> int:
        lanes = range(self._lanes) if lane is None else (lane,)
        total = 0
        for index in lanes:
            self._ensure_group(index)
            info = self._redis.xpending(self._lane_key(index), _GROUP)
            if not info:
                continue
            if isinstance(info, dict):
                total += int(info.get("pending") or 0)
            else:
                total += int(info[0] or 0)
        return total
