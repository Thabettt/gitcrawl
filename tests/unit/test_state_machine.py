from __future__ import annotations

import time
from datetime import UTC, datetime

import fakeredis
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

from scheduler.shard_planner import ShardSpec
from scheduler.state_machine import (
    ALLOWED_TRANSITIONS,
    QueuedShard,
    ShardQueue,
    ShardRow,
    ShardState,
    ShardStore,
)

START = datetime(2024, 1, 1, tzinfo=UTC)
END = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=UTC)
QUERY = "language:python created:2024-01-01..2024-01-31"


def spec(query=QUERY, total_count=42):
    return ShardSpec(query=query, range_start=START, range_end=END, total_count=total_count)


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def store(alembic_engine: Engine):
    with alembic_engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE shards RESTART IDENTITY CASCADE"))
    return ShardStore(alembic_engine)


@pytest.fixture()
def redis():
    return fakeredis.FakeRedis()


def test_state_values_are_exact():
    assert {state.value for state in ShardState} == {"pending", "active", "done", "incomplete"}


def test_allowed_transitions_table_is_exact():
    assert ALLOWED_TRANSITIONS == {
        ShardState.PENDING: frozenset({ShardState.ACTIVE}),
        ShardState.ACTIVE: frozenset({ShardState.DONE, ShardState.INCOMPLETE, ShardState.PENDING}),
        ShardState.DONE: frozenset(),
        ShardState.INCOMPLETE: frozenset(),
    }


def test_get_field_prefers_direct_key_access():
    from scheduler.state_machine import _get_field

    assert _get_field({"shard_id": "7"}, "shard_id") == "7"
    assert _get_field({b"shard_id": b"8"}, "shard_id") == "8"
    assert _get_field({}, "shard_id") is None


def test_create_and_get_roundtrip(store):
    shard_id = store.create(spec())
    row = store.get(shard_id)
    assert isinstance(row, ShardRow)
    assert row.id == shard_id
    assert row.kind == "search-range"
    assert row.query == QUERY
    assert row.range_start == START
    assert row.range_end == END
    assert row.total_count == 42
    assert row.state is ShardState.PENDING
    assert row.fetched == 0
    assert row.incomplete is False


def test_create_honors_kind(store):
    shard_id = store.create(spec(), kind="org")
    assert store.get(shard_id).kind == "org"


def test_get_missing_shard_raises_key_error(store):
    with pytest.raises(KeyError):
        store.get(999)


@pytest.mark.parametrize(
    "path",
    [
        [ShardState.ACTIVE],
        [ShardState.ACTIVE, ShardState.PENDING, ShardState.ACTIVE],
        [ShardState.ACTIVE, ShardState.DONE],
        [ShardState.ACTIVE, ShardState.INCOMPLETE],
    ],
)
def test_legal_transitions_are_allowed(store, path):
    shard_id = store.create(spec())
    for state in path:
        store.set_state(shard_id, state)
        assert store.get(shard_id).state is state


def _shard_in_state(store, state):
    shard_id = store.create(spec())
    if state is ShardState.ACTIVE:
        store.set_state(shard_id, ShardState.ACTIVE)
    elif state in (ShardState.DONE, ShardState.INCOMPLETE):
        store.set_state(shard_id, ShardState.ACTIVE)
        store.set_state(shard_id, state)
    return shard_id


def test_every_illegal_transition_is_rejected(store):
    for source in ShardState:
        for target in ShardState:
            if target in ALLOWED_TRANSITIONS[source]:
                continue
            shard_id = _shard_in_state(store, source)
            with pytest.raises(ValueError):
                store.set_state(shard_id, target)
            assert store.get(shard_id).state is source


def test_set_state_updates_metrics(store):
    shard_id = store.create(spec())
    store.set_state(shard_id, ShardState.ACTIVE)
    store.set_state(shard_id, ShardState.DONE, fetched=40, incomplete=True, total_count=41)
    row = store.get(shard_id)
    assert row.state is ShardState.DONE
    assert row.fetched == 40
    assert row.incomplete is True
    assert row.total_count == 41


def test_set_state_missing_shard_raises_key_error(store):
    with pytest.raises(KeyError):
        store.set_state(1234, ShardState.ACTIVE)


def test_counts_returns_every_state(store):
    first = store.create(spec())
    second = store.create(spec())
    third = store.create(spec())
    store.set_state(first, ShardState.ACTIVE)
    store.set_state(second, ShardState.ACTIVE)
    store.set_state(second, ShardState.DONE)
    store.set_state(third, ShardState.ACTIVE)
    store.set_state(third, ShardState.INCOMPLETE)
    assert store.counts() == {
        "pending": 0,
        "active": 1,
        "done": 1,
        "incomplete": 1,
    }


def test_lane_for_maps_shard_id_modulo_lanes():
    queue = ShardQueue(fakeredis.FakeRedis(), lanes=4)
    assert queue.lane_for(0) == 0
    assert queue.lane_for(3) == 3
    assert queue.lane_for(4) == 0
    assert queue.lane_for(7) == 3


def test_enqueue_claim_ack_roundtrip(redis):
    queue = ShardQueue(redis, lanes=4)
    stream_id = queue.enqueue(9)
    assert isinstance(stream_id, str)
    claimed = queue.claim("worker-1")
    assert claimed == [QueuedShard(stream_id=stream_id, shard_id=9, attempts=0)]
    queue.ack(stream_id, 9)
    assert queue.pel_size() == 0
    assert queue.claim("worker-1") == []


def test_enqueue_routes_to_lane_stream(redis):
    queue = ShardQueue(redis, lanes=4)
    first = queue.enqueue(4)
    second = queue.enqueue(8)
    queue.enqueue(5)
    assert first != second
    assert redis.xlen("gitcrawl:shards:lane:0") == 2
    assert redis.xlen("gitcrawl:shards:lane:1") == 1


def test_ensure_group_runs_once_per_lane(redis):
    class CountingRedis:
        def __init__(self, inner):
            self._inner = inner
            self.group_creates = 0

        def xgroup_create(self, *args, **kwargs):
            self.group_creates += 1
            return self._inner.xgroup_create(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    counting = CountingRedis(redis)
    queue = ShardQueue(counting)
    queue.enqueue(1)
    first = counting.group_creates
    assert first >= 1
    queue.claim("consumer", count=10)
    after_claim = counting.group_creates
    queue.claim("consumer", count=10)
    assert counting.group_creates == after_claim


def test_claim_drains_pel_history_before_new_messages():
    queue = ShardQueue(fakeredis.FakeRedis(), lanes=1)
    first = queue.enqueue(1)
    second = queue.enqueue(2)
    assert [item.shard_id for item in queue.claim("worker-1")] == [1]
    claimed = queue.claim("worker-1", count=2)
    assert [item.shard_id for item in claimed] == [1, 2]
    assert claimed[0].stream_id == first
    assert claimed[1].stream_id == second
    assert len({item.stream_id for item in claimed}) == 2


def test_retry_reenqueue_preserves_single_live_copy(redis):
    queue = ShardQueue(redis, lanes=1)
    stream_id = queue.enqueue(7)
    assert [item.shard_id for item in queue.claim("worker-1")] == [7]
    outcome = queue.retry_or_dlq(stream_id, 7, attempts=1)
    assert outcome.dead_lettered is False
    assert queue.pel_size() == 0
    assert redis.xlen("gitcrawl:shards:lane:0") == 1
    assert queue.claim("worker-1") == [
        QueuedShard(stream_id=outcome.stream_id, shard_id=7, attempts=1)
    ]


def test_retry_reenqueue_redelivers_within_the_same_millisecond(redis, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    queue = ShardQueue(redis, lanes=1)
    stream_id = queue.enqueue(7)
    assert queue.claim("worker-1") == [QueuedShard(stream_id=stream_id, shard_id=7, attempts=0)]
    outcome = queue.retry_or_dlq(stream_id, 7, attempts=1)
    assert queue.claim("worker-1") == [
        QueuedShard(stream_id=outcome.stream_id, shard_id=7, attempts=1)
    ]


def test_failure_after_max_attempts_lands_in_dlq(redis):
    queue = ShardQueue(redis, lanes=1)
    first = queue.enqueue(3)
    queue.claim("worker-1")
    second = queue.retry_or_dlq(first, 3, attempts=1).stream_id
    queue.claim("worker-1")
    third = queue.retry_or_dlq(second, 3, attempts=2).stream_id
    queue.claim("worker-1")
    outcome = queue.retry_or_dlq(third, 3, attempts=3)
    assert outcome.dead_lettered is True
    assert redis.xlen("gitcrawl:shards:lane:0") == 0
    assert redis.xlen("gitcrawl:shards:dlq") == 1
    assert queue.pel_size() == 0
    entry = redis.xrange("gitcrawl:shards:dlq")[0]
    assert entry[1][b"shard_id"] == b"3"


def test_reclaim_stale_redelivers_with_incremented_attempts(redis):
    queue = ShardQueue(redis, lanes=1)
    stream_id = queue.enqueue(5)
    assert queue.claim("worker-1")[0].stream_id == stream_id
    assert queue.reclaim_stale("worker-2", min_idle_ms=60000) == []
    assert queue.reclaim_stale("worker-2", min_idle_ms=0) == [
        QueuedShard(stream_id=stream_id, shard_id=5, attempts=1)
    ]
    assert queue.pel_size() == 1


def test_set_state_refreshes_updated_at(store, alembic_engine):
    shard_id = store.create(spec())
    with alembic_engine.begin() as connection:
        connection.execute(
            text("UPDATE shards SET updated_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": shard_id},
        )
        before = connection.scalar(
            text("SELECT updated_at FROM shards WHERE id = :id"), {"id": shard_id}
        )

    store.set_state(shard_id, ShardState.ACTIVE)

    with alembic_engine.connect() as connection:
        after = connection.scalar(
            text("SELECT updated_at FROM shards WHERE id = :id"), {"id": shard_id}
        )
    assert after > before


def test_pel_size_accounts_per_lane_and_total(redis):
    queue = ShardQueue(redis, lanes=4)
    queue.enqueue(0)
    queue.enqueue(4)
    queue.enqueue(5)
    claimed = queue.claim("worker-1", count=3)
    assert len(claimed) == 3
    assert queue.pel_size(0) == 2
    assert queue.pel_size(1) == 1
    assert queue.pel_size(2) == 0
    assert queue.pel_size(3) == 0
    assert queue.pel_size() == 3
    queue.ack(claimed[0].stream_id, claimed[0].shard_id)
    assert queue.pel_size(0) == 1
    assert queue.pel_size() == 2


def test_total_pel_sums_across_prefixed_streams_without_creating_groups(redis):
    base = ShardQueue(redis, lanes=2)
    base.enqueue(0)
    base.enqueue(1)
    assert len(base.claim("worker-1", count=2)) == 2
    scoped = ShardQueue(redis, prefix="gitcrawl:shards:run:9", lanes=2)
    scoped.enqueue(0)
    scoped.claim("worker-1", count=1)

    before = set(redis.keys("gitcrawl:*"))
    assert ShardQueue(redis).total_pel() == 3
    assert set(redis.keys("gitcrawl:*")) == before
