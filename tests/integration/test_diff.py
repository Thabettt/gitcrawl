from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

from lib.gh_client import API_VERSION
from serve.diff import CHANGED_FIELDS, RunDiff, diff_runs
from serve.executor import RunPayload, RunPayloadItem, create_run, execute_run

FILTER = {"gitcrawl_filter": 1, "q": "language:rust"}


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(alembic_engine: Engine) -> Engine:
    with alembic_engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE run_items, runs, saved_filters, audit_log, shards, geo_cache, "
                "owners, repos, full_name_history RESTART IDENTITY CASCADE"
            )
        )
        connection.execute(text("INSERT INTO owners (id, login, type) VALUES (1, 'octo', 'User')"))
        connection.execute(
            text(
                "INSERT INTO repos (id, node_id, full_name, owner_id, name, visibility) VALUES "
                "(1, 'R_1', 'octo/r1', 1, 'r1', 'public'), "
                "(2, 'R_2', 'octo/r2', 1, 'r2', 'public'), "
                "(3, 'R_3', 'octo/r3', 1, 'r3', 'public'), "
                "(4, 'R_4', 'octo/r4', 1, 'r4', 'public'), "
                "(5, 'R_5', 'octo/r5', 1, 'r5', 'public')"
            )
        )
    return alembic_engine


def item(repo_id: int, **overrides) -> RunPayloadItem:
    values: dict[str, object] = {
        "repo_id": repo_id,
        "full_name": f"octo/r{repo_id}",
        "stargazers": 10,
        "pushed_at": "2026-01-01T00:00:00Z",
        "archived": False,
        "language": "Rust",
        "license_spdx": "MIT",
        "country_iso": "DE",
        "geo_confidence": "name",
        "virtuals": {},
    }
    values.update(overrides)
    return RunPayloadItem(**values)


def seed_run(engine: Engine, tmp_path, items: list[RunPayloadItem]) -> int:
    run_id = create_run(engine, FILTER, api_version=API_VERSION)
    payload = RunPayload(total_count=len(items), fetched=len(items), items=list(items))
    execute_run(
        engine, run_id, runner=lambda _rid, _spec: payload, runs_root=str(tmp_path / "runs")
    )
    return run_id


def test_diff_reports_added_and_removed(clean: Engine, tmp_path):
    run_a = seed_run(clean, tmp_path, [item(1), item(2)])
    run_b = seed_run(clean, tmp_path, [item(2), item(3)])

    result = diff_runs(clean, run_a, run_b)

    assert isinstance(result, RunDiff)
    assert result.run_a == run_a
    assert result.run_b == run_b
    assert result.added == ({"repo_id": 3, "full_name": "octo/r3"},)
    assert result.removed == ({"repo_id": 1, "full_name": "octo/r1"},)
    assert result.changed == ()
    assert result.summary == {
        "added": 1,
        "removed": 1,
        "changed_repos": 0,
        "changed_fields": 0,
    }


def test_diff_reports_every_tracked_field_change(clean: Engine, tmp_path):
    run_a = seed_run(clean, tmp_path, [item(1)])
    run_b = seed_run(
        clean,
        tmp_path,
        [
            item(
                1,
                stargazers=12,
                pushed_at="2026-02-03T04:05:06Z",
                archived=True,
                language="Go",
                license_spdx="Apache-2.0",
                country_iso="FR",
            )
        ],
    )

    result = diff_runs(clean, run_a, run_b)

    assert result.changed == (
        {"repo_id": 1, "full_name": "octo/r1", "field": "stargazers", "from": 10, "to": 12},
        {
            "repo_id": 1,
            "full_name": "octo/r1",
            "field": "pushed_at",
            "from": "2026-01-01T00:00:00Z",
            "to": "2026-02-03T04:05:06Z",
        },
        {"repo_id": 1, "full_name": "octo/r1", "field": "archived", "from": False, "to": True},
        {"repo_id": 1, "full_name": "octo/r1", "field": "language", "from": "Rust", "to": "Go"},
        {
            "repo_id": 1,
            "full_name": "octo/r1",
            "field": "license_spdx",
            "from": "MIT",
            "to": "Apache-2.0",
        },
        {"repo_id": 1, "full_name": "octo/r1", "field": "country_iso", "from": "DE", "to": "FR"},
    )
    assert result.summary == {
        "added": 0,
        "removed": 0,
        "changed_repos": 1,
        "changed_fields": 6,
    }


def test_diff_null_transitions_count_as_changed(clean: Engine, tmp_path):
    run_a = seed_run(clean, tmp_path, [item(1, stargazers=None, language="Rust", country_iso=None)])
    run_b = seed_run(clean, tmp_path, [item(1, stargazers=5, language=None, country_iso="DE")])

    result = diff_runs(clean, run_a, run_b)

    assert result.changed == (
        {"repo_id": 1, "full_name": "octo/r1", "field": "stargazers", "from": None, "to": 5},
        {"repo_id": 1, "full_name": "octo/r1", "field": "language", "from": "Rust", "to": None},
        {"repo_id": 1, "full_name": "octo/r1", "field": "country_iso", "from": None, "to": "DE"},
    )
    assert result.summary["changed_fields"] == 3
    assert result.summary["changed_repos"] == 1


def test_diff_identical_runs_is_empty(clean: Engine, tmp_path):
    items = [item(1), item(2, stargazers=40)]
    run_a = seed_run(clean, tmp_path, items)
    run_b = seed_run(clean, tmp_path, items)

    result = diff_runs(clean, run_a, run_b)

    assert result.added == ()
    assert result.removed == ()
    assert result.changed == ()
    assert result.summary == {
        "added": 0,
        "removed": 0,
        "changed_repos": 0,
        "changed_fields": 0,
    }


def test_diff_orders_entries_by_repo_then_field(clean: Engine, tmp_path):
    run_a = seed_run(clean, tmp_path, [item(3), item(1), item(2, stargazers=10, language="Rust")])
    run_b = seed_run(
        clean,
        tmp_path,
        [
            item(3, language="Go"),
            item(1, archived=True),
            item(2, stargazers=11, language="Go"),
        ],
    )

    result = diff_runs(clean, run_a, run_b)

    assert [(entry["repo_id"], entry["field"]) for entry in result.changed] == [
        (1, "archived"),
        (2, "stargazers"),
        (2, "language"),
        (3, "language"),
    ]
    assert result.summary == {
        "added": 0,
        "removed": 0,
        "changed_repos": 3,
        "changed_fields": 4,
    }


def test_diff_orders_added_and_removed_by_repo_id(clean: Engine, tmp_path):
    run_a = seed_run(clean, tmp_path, [item(3), item(1)])
    run_b = seed_run(clean, tmp_path, [item(4), item(2)])

    result = diff_runs(clean, run_a, run_b)

    assert [entry["repo_id"] for entry in result.added] == [2, 4]
    assert [entry["repo_id"] for entry in result.removed] == [1, 3]


def test_diff_unknown_runs_raise_key_error(clean: Engine, tmp_path):
    run_a = seed_run(clean, tmp_path, [item(1)])

    with pytest.raises(KeyError):
        diff_runs(clean, run_a, 424242)
    with pytest.raises(KeyError):
        diff_runs(clean, 424242, run_a)


def test_diff_handles_null_repo_ids(clean: Engine):
    base_id = create_run(clean, FILTER, api_version="v1")
    viewed_id = create_run(clean, FILTER, api_version="v1")
    with clean.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO run_items (run_id, repo_id, full_name, stargazers)"
                " VALUES (:run_id, NULL, 'ghost/one', 1)"
            ),
            {"run_id": base_id},
        )
        connection.execute(
            text(
                "INSERT INTO run_items (run_id, repo_id, full_name, stargazers)"
                " VALUES (:run_id, NULL, 'ghost/two', 2)"
            ),
            {"run_id": viewed_id},
        )
        connection.execute(
            text(
                "INSERT INTO run_items (run_id, repo_id, full_name, stargazers)"
                " VALUES (:run_id, NULL, 'ghost/one', 1)"
            ),
            {"run_id": viewed_id},
        )
    result = diff_runs(clean, base_id, viewed_id)
    assert result.summary == {
        "added": 1,
        "removed": 0,
        "changed_repos": 0,
        "changed_fields": 0,
    }


def test_same_hash_candidates_are_capped(clean: Engine, tmp_path):
    from serve.pages import _same_hash_runs

    run_id = seed_run(clean, tmp_path, [item(1)])
    for _ in range(55):
        create_run(clean, FILTER, api_version=API_VERSION)
    with clean.connect() as connection:
        row = (
            connection.execute(text("SELECT * FROM runs WHERE id = :id"), {"id": run_id})
            .mappings()
            .one()
        )
    assert len(_same_hash_runs(clean, row)) == 50


def test_changed_fields_constant_is_exact():
    assert CHANGED_FIELDS == (
        "stargazers",
        "pushed_at",
        "archived",
        "language",
        "license_spdx",
        "country_iso",
    )
