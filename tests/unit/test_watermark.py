from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

from store.upserts import dedupe_items, upsert_repos


def item(
    repo_id: int,
    *,
    pushed_at: object = None,
    full_name: str | None = None,
    description: str | None = None,
) -> dict:
    name = full_name or f"octo/repo{repo_id}"
    return {
        "id": repo_id,
        "node_id": f"R_{repo_id}",
        "full_name": name,
        "owner": {"id": repo_id * 10, "login": f"owner{repo_id}", "type": "User"},
        "private": False,
        "description": description,
        "pushed_at": pushed_at,
    }


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(alembic_engine: Engine) -> Engine:
    with alembic_engine.begin() as connection:
        connection.execute(
            text("TRUNCATE TABLE owners, repos, full_name_history, shards RESTART IDENTITY CASCADE")
        )
    return alembic_engine


def test_dedupe_collapses_duplicate_ids_with_last_occurrence_fields():
    first = item(1, pushed_at="2024-01-01T00:00:00Z", description="first")
    last = item(1, pushed_at="2024-03-01T00:00:00Z", description="last")
    deduped = dedupe_items([first, item(2), last])
    assert [entry["id"] for entry in deduped] == [1, 2]
    assert deduped[0] == last
    assert deduped[0]["description"] == "last"
    assert deduped[0]["pushed_at"] == "2024-03-01T00:00:00Z"


def test_dedupe_keeps_maximum_pushed_at_when_the_last_occurrence_is_older():
    first = item(1, pushed_at="2024-05-05T00:00:00Z", description="first")
    last = item(1, pushed_at="2024-01-01T00:00:00Z", description="last")
    deduped = dedupe_items([first, last])
    assert len(deduped) == 1
    assert deduped[0]["description"] == "last"
    assert deduped[0]["pushed_at"] == "2024-05-05T00:00:00Z"


def test_dedupe_compares_datetime_values_as_well_as_strings():
    first = item(1, pushed_at=datetime(2024, 5, 5, tzinfo=UTC))
    last = item(1, pushed_at="2024-01-01T00:00:00Z")
    deduped = dedupe_items([first, last])
    assert deduped[0]["pushed_at"] == datetime(2024, 5, 5, tzinfo=UTC)


def test_dedupe_prefers_the_valid_timestamp_when_the_other_is_missing():
    assert (
        dedupe_items([item(1), item(1, pushed_at="2024-02-02T00:00:00Z")])[0]["pushed_at"]
        == "2024-02-02T00:00:00Z"
    )
    assert (
        dedupe_items([item(1, pushed_at="2024-02-02T00:00:00Z"), item(1)])[0]["pushed_at"]
        == "2024-02-02T00:00:00Z"
    )


def test_dedupe_preserves_first_seen_order():
    deduped = dedupe_items([item(3), item(1), item(2), item(1), item(3)])
    assert [entry["id"] for entry in deduped] == [3, 1, 2]


def test_dedupe_passes_through_items_without_a_valid_id():
    malformed = [{"full_name": "octo/no-id"}, {"id": "7"}, {"id": True}, "not-a-dict"]
    deduped = dedupe_items([*malformed, item(1), item(1, description="second")])
    assert deduped[:4] == malformed
    assert len(deduped) == 5
    assert deduped[4]["description"] == "second"


def test_dedupe_does_not_mutate_its_inputs():
    first = item(1, pushed_at="2024-05-05T00:00:00Z", description="first")
    last = item(1, pushed_at="2024-01-01T00:00:00Z", description="last")
    snapshot = deepcopy([first, last])
    dedupe_items([first, last])
    assert [first, last] == snapshot


def test_dedupe_is_deterministic_for_the_same_input():
    items = [
        item(2),
        item(1, pushed_at="2024-01-01T00:00:00Z"),
        item(1, pushed_at="2024-02-02T00:00:00Z"),
    ]
    assert dedupe_items(items) == dedupe_items(list(items))


def test_overlapping_batches_upsert_to_one_row_per_id(clean: Engine):
    page_one = [
        item(1, pushed_at="2024-01-01T00:00:00Z", description="page-one"),
        item(2, pushed_at="2024-01-01T00:00:00Z"),
    ]
    page_two = [
        item(2, pushed_at="2024-06-01T00:00:00Z", description="page-two"),
        item(3, pushed_at="2024-02-02T00:00:00Z"),
    ]
    stats = upsert_repos(clean, dedupe_items(page_one + page_two))
    assert stats.inserted == 3
    with clean.connect() as connection:
        rows = connection.execute(
            text("SELECT id, description, pushed_at FROM repos ORDER BY id")
        ).all()
    assert [row[0] for row in rows] == [1, 2, 3]
    assert rows[1][1] == "page-two"
    assert rows[1][2] == datetime(2024, 6, 1, tzinfo=UTC)


def test_overlap_dedupe_leaves_no_persistent_cursor_state(clean: Engine):
    page_one = [
        item(1, pushed_at="2024-01-01T00:00:00Z"),
        item(2, pushed_at="2024-01-01T00:00:00Z"),
    ]
    page_two = [
        item(2, pushed_at="2024-06-01T00:00:00Z"),
        item(1, pushed_at="2024-03-03T00:00:00Z"),
    ]
    upsert_repos(clean, dedupe_items(page_one + page_two))
    with clean.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM shards")) == 0
        snapshots = dict(connection.execute(text("SELECT id, pushed_at FROM repos")).all())
    assert snapshots[1] == datetime(2024, 3, 3, tzinfo=UTC)
    assert snapshots[2] == datetime(2024, 6, 1, tzinfo=UTC)
