from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import text

from serve.executor import recover_orphaned_runs


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


def seed_run_with_status(engine, status: str) -> int:
    with engine.begin() as connection:
        return int(
            connection.execute(
                text(
                    "INSERT INTO runs (filter_hash, filter_spec, api_version, status)"
                    " VALUES (gen_random_uuid()::text, '{}'::jsonb, '2022-11-28', :status)"
                    " RETURNING id"
                ),
                {"status": status},
            ).scalar_one()
        )


def test_recover_marks_queued_and_running_as_orphaned(clean_db):
    engine = clean_db()
    queued = seed_run_with_status(engine, "queued")
    running = seed_run_with_status(engine, "running")
    done = seed_run_with_status(engine, "done")
    failed = seed_run_with_status(engine, "failed")

    count = recover_orphaned_runs(engine)
    assert count == 2
    with engine.connect() as connection:
        rows = {
            row["id"]: row
            for row in connection.execute(
                text("SELECT id, status, error FROM runs WHERE id = ANY(:ids)"),
                {"ids": [queued, running, done, failed]},
            )
            .mappings()
            .all()
        }
    assert rows[queued]["status"] == "failed"
    assert rows[running]["status"] == "failed"
    assert "orphaned" in rows[running]["error"]
    assert rows[done]["status"] == "done"  # untouched
    assert rows[failed]["status"] == "failed"  # untouched


def test_recover_is_idempotent(clean_db):
    engine = clean_db()
    seed_run_with_status(engine, "running")
    assert recover_orphaned_runs(engine) == 1
    assert recover_orphaned_runs(engine) == 0
