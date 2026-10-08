from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

from scheduler.shard_planner import ShardSpec
from scheduler.state_machine import (
    ALLOWED_TRANSITIONS,
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


def test_state_values_are_exact():
    assert {state.value for state in ShardState} == {"pending", "active", "done", "incomplete"}


def test_allowed_transitions_table_is_exact():
    assert ALLOWED_TRANSITIONS == {
        ShardState.PENDING: frozenset({ShardState.ACTIVE}),
        ShardState.ACTIVE: frozenset({ShardState.DONE, ShardState.INCOMPLETE, ShardState.PENDING}),
        ShardState.DONE: frozenset(),
        ShardState.INCOMPLETE: frozenset(),
    }


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
