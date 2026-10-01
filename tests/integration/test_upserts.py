from __future__ import annotations

import re
from copy import deepcopy
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

import store.upserts as upserts
from store.models import Owner, Repo
from store.upserts import UpsertStats, bootstrap_copy, normalize_repo, upsert_repos

REPO_FIELDS = (
    "id",
    "node_id",
    "full_name",
    "owner_id",
    "name",
    "description",
    "homepage",
    "language",
    "license_spdx",
    "topics",
    "visibility",
    "fork",
    "parent_full_name",
    "source_full_name",
    "archived",
    "disabled",
    "mirror_url",
    "is_template",
    "size_kb",
    "stargazers",
    "forks_count",
    "watchers",
    "open_issues",
    "default_branch",
    "has_wiki",
    "has_issues",
    "has_projects",
    "has_pages",
    "has_discussions",
    "has_pull_requests",
    "custom_properties",
    "created_at",
    "pushed_at",
    "updated_at",
)


def repo_item(
    repo_id: int = 1,
    *,
    full_name: str | None = None,
    owner_id: int = 100,
    owner_login: str = "octo",
    owner_type: str = "User",
    private: bool = False,
    **overrides,
) -> dict:
    name = full_name.rsplit("/", 1)[-1] if full_name else f"repo{repo_id}"
    item = {
        "id": repo_id,
        "node_id": f"R_{repo_id}",
        "name": name,
        "full_name": full_name or f"octo/repo{repo_id}",
        "description": f"desc {repo_id}",
        "homepage": None,
        "language": "Python",
        "license": {"spdx_id": "MIT", "name": "MIT License"},
        "topics": ["ai", "agents"],
        "private": private,
        "visibility": "private" if private else "public",
        "fork": False,
        "parent": None,
        "source": None,
        "archived": False,
        "disabled": False,
        "mirror_url": None,
        "is_template": False,
        "size": 1234,
        "stargazers_count": 42,
        "forks_count": 7,
        "watchers_count": 11,
        "open_issues_count": 3,
        "default_branch": "main",
        "has_wiki": True,
        "has_issues": True,
        "has_projects": False,
        "has_pages": True,
        "has_discussions": False,
        "has_pull_requests": True,
        "custom_properties": {"team": "core"},
        "created_at": "2024-01-02T03:04:05Z",
        "pushed_at": "2024-06-07T08:09:10Z",
        "updated_at": "2024-06-07T08:09:10Z",
        "owner": {"id": owner_id, "login": owner_login, "type": owner_type},
    }
    item.update(overrides)
    return item


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(alembic_engine: Engine) -> Engine:
    with alembic_engine.begin() as connection:
        connection.execute(
            text("TRUNCATE TABLE owners, repos, full_name_history RESTART IDENTITY CASCADE")
        )
    return alembic_engine


def dump_repos(engine: Engine) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(sa.select(Repo.__table__).order_by(Repo.id)).mappings()
        return [dict(row) for row in rows]


def dump_owners(engine: Engine) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(sa.select(Owner.__table__).order_by(Owner.id)).mappings()
        return [dict(row) for row in rows]


def dump_history(engine: Engine) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT repo_id, full_name FROM full_name_history ORDER BY id")
        ).mappings()
        return [dict(row) for row in rows]


def xmin_of(engine: Engine, repo_id: int) -> str:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT xmin::text FROM repos WHERE id = :id"), {"id": repo_id}
        ).scalar_one()


def test_normalize_repo_maps_a_full_object():
    item = repo_item(
        7,
        full_name="octo/proj",
        parent={"full_name": "up/stream"},
        source={"full_name": "src/origin"},
    )
    row = normalize_repo(item)
    assert set(row) == set(REPO_FIELDS) | {"owner_login", "owner_type"}
    assert row["id"] == 7
    assert row["node_id"] == "R_7"
    assert row["full_name"] == "octo/proj"
    assert row["owner_id"] == 100
    assert row["owner_login"] == "octo"
    assert row["owner_type"] == "User"
    assert row["name"] == "proj"
    assert row["description"] == "desc 7"
    assert row["homepage"] is None
    assert row["language"] == "Python"
    assert row["license_spdx"] == "MIT"
    assert row["topics"] == ["ai", "agents"]
    assert row["visibility"] == "public"
    assert row["fork"] is False
    assert row["parent_full_name"] == "up/stream"
    assert row["source_full_name"] == "src/origin"
    assert row["archived"] is False
    assert row["disabled"] is False
    assert row["mirror_url"] is None
    assert row["is_template"] is False
    assert row["size_kb"] == 1234
    assert row["stargazers"] == 42
    assert row["forks_count"] == 7
    assert row["watchers"] == 11
    assert row["open_issues"] == 3
    assert row["default_branch"] == "main"
    assert row["has_wiki"] is True
    assert row["has_issues"] is True
    assert row["has_projects"] is False
    assert row["has_pages"] is True
    assert row["has_discussions"] is False
    assert row["has_pull_requests"] is True
    assert row["custom_properties"] == {"team": "core"}
    assert row["created_at"] == datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert row["pushed_at"] == datetime(2024, 6, 7, 8, 9, 10, tzinfo=UTC)
    assert row["updated_at"] == datetime(2024, 6, 7, 8, 9, 10, tzinfo=UTC)


def test_normalize_repo_applies_defaults_for_a_minimal_object():
    row = normalize_repo(
        {
            "id": 9,
            "node_id": "R_9",
            "full_name": "tiny/repo",
            "owner": {"id": 900, "login": "tiny"},
            "private": True,
        }
    )
    assert row["name"] == "repo"
    assert row["description"] is None
    assert row["language"] is None
    assert row["license_spdx"] is None
    assert row["topics"] == []
    assert row["visibility"] == "private"
    assert row["fork"] is False
    assert row["parent_full_name"] is None
    assert row["source_full_name"] is None
    assert row["archived"] is False
    assert row["disabled"] is False
    assert row["is_template"] is False
    assert row["size_kb"] is None
    assert row["stargazers"] == 0
    assert row["forks_count"] == 0
    assert row["watchers"] == 0
    assert row["open_issues"] == 0
    assert row["default_branch"] is None
    assert row["has_wiki"] is None
    assert row["has_pull_requests"] is None
    assert row["custom_properties"] == {}
    assert row["created_at"] is None
    assert row["pushed_at"] is None
    assert row["updated_at"] is None
    assert row["owner_type"] == "User"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.pop("id"),
        lambda item: item.pop("node_id"),
        lambda item: item.pop("full_name"),
        lambda item: item.pop("owner"),
        lambda item: item["owner"].pop("id"),
        lambda item: item["owner"].pop("login"),
    ],
)
def test_normalize_repo_returns_none_when_a_required_field_is_missing(mutate):
    item = repo_item(1)
    mutate(item)
    assert normalize_repo(item) is None


def test_normalize_repo_returns_none_for_a_non_mapping():
    assert normalize_repo("not-a-dict") is None
    assert normalize_repo(None) is None


def test_normalize_repo_does_not_mutate_its_input():
    item = repo_item(1, parent={"full_name": "up/stream"})
    snapshot = deepcopy(item)
    normalize_repo(item)
    assert item == snapshot


@pytest.mark.parametrize(
    "overrides",
    [
        {"stargazers_count": "many"},
        {"stargazers_count": [1, 2]},
        {"stargazers_count": float("inf")},
        {"forks_count": float("1e400")},
        {"forks_count": "lots"},
        {"watchers_count": {"count": 1}},
        {"open_issues_count": "n/a"},
        {"topics": "ai"},
        {"topics": [1, 2]},
        {"topics": {"ai": True}},
        {"id": 1.5},
        {"id": "7"},
        {"owner": {"id": 100.5, "login": "octo"}},
        {"owner": {"id": "100", "login": "octo"}},
        {"node_id": 123},
    ],
)
def test_normalize_repo_rejects_dirty_counts_topics_and_ids(overrides):
    assert normalize_repo(repo_item(1, **overrides)) is None


def test_normalize_repo_coerces_coercible_numeric_counts():
    row = normalize_repo(repo_item(1, stargazers_count="42", forks_count=7.0))
    assert row["stargazers"] == 42
    assert row["forks_count"] == 7


def test_insert_new_repos_and_owners(clean: Engine):
    stats = upsert_repos(
        clean,
        [
            repo_item(1),
            repo_item(2, owner_id=200, owner_login="org-x", owner_type="Organization"),
        ],
    )
    assert stats == UpsertStats(inserted=2, history_rows=2)
    repos = dump_repos(clean)
    assert [repo["id"] for repo in repos] == [1, 2]
    first = repos[0]
    assert first["full_name"] == "octo/repo1"
    assert first["topics"] == ["ai", "agents"]
    assert first["license_spdx"] == "MIT"
    assert first["visibility"] == "public"
    assert first["stargazers"] == 42
    assert first["created_at"] == datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert first["indexed_at"] is not None
    owners = dump_owners(clean)
    assert {owner["id"]: (owner["login"], owner["type"]) for owner in owners} == {
        100: ("octo", "User"),
        200: ("org-x", "Organization"),
    }
    assert [row["repo_id"] for row in dump_history(clean)] == [1, 2]


def test_identical_rerun_is_unchanged_and_leaves_xmin_stable(clean: Engine):
    items = [repo_item(1), repo_item(2, owner_id=200, owner_login="other")]
    upsert_repos(clean, items)
    before = {repo_id: xmin_of(clean, repo_id) for repo_id in (1, 2)}
    stats = upsert_repos(clean, [deepcopy(item) for item in items])
    assert stats == UpsertStats(unchanged=2)
    assert {repo_id: xmin_of(clean, repo_id) for repo_id in (1, 2)} == before
    assert len(dump_history(clean)) == 2


def test_owner_login_change_for_same_id_updates_in_place(clean: Engine):
    upsert_repos(clean, [repo_item(1, owner_id=100, owner_login="old-name")])
    stats = upsert_repos(clean, [repo_item(1, owner_id=100, owner_login="new-name")])
    assert stats == UpsertStats(unchanged=1)
    assert {owner["id"]: owner["login"] for owner in dump_owners(clean)} == {100: "new-name"}


def test_owner_login_held_by_another_id_is_stale_renamed(clean: Engine):
    upsert_repos(clean, [repo_item(1, owner_id=100, owner_login="shared")])
    stats = upsert_repos(clean, [repo_item(2, owner_id=200, owner_login="shared")])
    assert stats.conflicts == 1
    assert stats.inserted == 1
    owners = {owner["id"]: owner["login"] for owner in dump_owners(clean)}
    assert owners == {100: "shared~100", 200: "shared"}
    repos = {repo["id"]: repo for repo in dump_repos(clean)}
    assert repos[1]["owner_id"] == 100
    assert repos[2]["owner_id"] == 200


def test_changed_repo_is_updated(clean: Engine):
    upsert_repos(clean, [repo_item(1)])
    before = xmin_of(clean, 1)
    stats = upsert_repos(clean, [repo_item(1, description="changed")])
    assert stats == UpsertStats(updated=1)
    assert dump_repos(clean)[0]["description"] == "changed"
    assert xmin_of(clean, 1) != before


def test_full_name_change_records_previous_name_in_history(clean: Engine):
    upsert_repos(clean, [repo_item(1, full_name="octo/one")])
    assert [row["full_name"] for row in dump_history(clean)] == ["octo/one"]
    stats = upsert_repos(clean, [repo_item(1, full_name="octo/two")])
    assert stats == UpsertStats(updated=1, history_rows=1)
    assert [row["full_name"] for row in dump_history(clean)][-1] == "octo/one"
    assert dump_repos(clean)[0]["full_name"] == "octo/two"


def test_repo_full_name_held_by_another_id_is_stale_renamed(clean: Engine):
    upsert_repos(clean, [repo_item(1, full_name="octo/name")])
    stats = upsert_repos(clean, [repo_item(2, full_name="octo/name")])
    assert stats.conflicts == 1
    assert stats.inserted == 1
    assert stats.history_rows == 2
    repos = {repo["id"]: repo for repo in dump_repos(clean)}
    assert repos[1]["full_name"] == "octo/name~1"
    assert repos[2]["full_name"] == "octo/name"
    history = dump_history(clean)
    assert (1, "octo/name") in [(row["repo_id"], row["full_name"]) for row in history]


def test_malformed_rows_are_skipped_without_crashing(clean: Engine):
    bad = [
        {"id": 10, "node_id": "R", "full_name": "o/a", "owner": {"id": 1}},
        {"id": 11, "node_id": "R", "owner": {"id": 1, "login": "o"}},
        {"id": 12, "full_name": "o/b", "owner": {"id": 1, "login": "o"}},
        {"node_id": "R", "full_name": "o/c", "owner": {"id": 1, "login": "o"}},
        {"id": 13, "node_id": "R", "full_name": "o/d"},
        "not-a-dict",
    ]
    stats = upsert_repos(clean, [repo_item(1), *bad])
    assert stats == UpsertStats(inserted=1, skipped=6, history_rows=1)
    assert [repo["id"] for repo in dump_repos(clean)] == [1]


def test_dirty_page_rows_are_skipped_without_aborting_the_batch(clean: Engine):
    stats = upsert_repos(
        clean,
        [
            repo_item(1),
            repo_item(2, stargazers_count="many"),
            repo_item(3, topics="ai"),
            repo_item(4, id=4.5),
        ],
    )
    assert stats == UpsertStats(inserted=1, skipped=3, history_rows=1)
    assert [repo["id"] for repo in dump_repos(clean)] == [1]


def test_batch_size_splits_repo_writes(clean: Engine):
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO repos" in statement:
            statements.append(statement)

    sa.event.listen(clean, "before_cursor_execute", capture)
    try:
        stats = upsert_repos(clean, [repo_item(i) for i in range(1, 11)], batch_size=3)
    finally:
        sa.event.remove(clean, "before_cursor_execute", capture)
    assert stats.inserted == 10
    assert len(statements) == 4


def test_batch_size_must_be_positive(clean: Engine):
    with pytest.raises(ValueError):
        upsert_repos(clean, [], batch_size=0)


def test_bootstrap_copy_inserts_clean_rows(clean: Engine):
    stats = bootstrap_copy(clean, [repo_item(1), repo_item(2)])
    assert stats.inserted == 2
    assert stats.skipped == 0
    repos = dump_repos(clean)
    assert [repo["id"] for repo in repos] == [1, 2]
    assert repos[0]["topics"] == ["ai", "agents"]
    assert repos[0]["custom_properties"] == {"team": "core"}
    assert {owner["id"]: owner["login"] for owner in dump_owners(clean)} == {
        100: "octo",
    }


def test_bootstrap_copy_rejects_a_dirty_row_without_aborting(clean: Engine):
    stats = bootstrap_copy(clean, [repo_item(1), repo_item(2, size="not-a-number"), repo_item(3)])
    assert stats.inserted == 2
    assert stats.skipped == 1
    assert [repo["id"] for repo in dump_repos(clean)] == [1, 3]


def test_bootstrap_copy_skips_dirty_normalized_rows(clean: Engine):
    stats = bootstrap_copy(
        clean,
        [
            repo_item(1),
            repo_item(2, stargazers_count="many"),
            repo_item(3, topics="ai"),
            repo_item(4, id=4.5),
        ],
    )
    assert stats.inserted == 1
    assert stats.skipped == 3
    assert [repo["id"] for repo in dump_repos(clean)] == [1]


def test_bootstrap_copy_rebootstrap_after_rename_matches_upsert_semantics(clean: Engine):
    initial = [repo_item(1, full_name="octo/old")]
    second = [repo_item(2, full_name="octo/old"), repo_item(1, full_name="octo/new")]
    bootstrap_copy(clean, initial)
    bootstrap_stats = bootstrap_copy(clean, second)
    bootstrap_repos = [
        {key: value for key, value in repo.items() if key != "indexed_at"}
        for repo in dump_repos(clean)
    ]
    bootstrap_history = sorted((row["repo_id"], row["full_name"]) for row in dump_history(clean))
    with clean.begin() as connection:
        connection.execute(
            text("TRUNCATE TABLE owners, repos, full_name_history RESTART IDENTITY CASCADE")
        )
    bootstrap_copy(clean, initial)
    upsert_stats = upsert_repos(clean, second)
    upsert_repos_dump = [
        {key: value for key, value in repo.items() if key != "indexed_at"}
        for repo in dump_repos(clean)
    ]
    upsert_history = sorted((row["repo_id"], row["full_name"]) for row in dump_history(clean))
    assert {repo["id"]: repo["full_name"] for repo in bootstrap_repos} == {
        1: "octo/new",
        2: "octo/old",
    }
    assert bootstrap_repos == upsert_repos_dump
    assert bootstrap_history == upsert_history
    assert bootstrap_stats.conflicts == upsert_stats.conflicts == 1
    assert bootstrap_stats.history_rows == upsert_stats.history_rows == 3


def test_bootstrap_copy_failure_drops_staging_table(clean: Engine, monkeypatch):
    def boom(connection, table_name):
        raise RuntimeError("merge exploded")

    monkeypatch.setattr(upserts, "_merge_staging", boom)
    with pytest.raises(RuntimeError, match="merge exploded"):
        bootstrap_copy(clean, [repo_item(1)])
    with clean.connect() as connection:
        leftovers = connection.scalar(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name LIKE 'repos_staging_%'"
            )
        )
    assert leftovers == 0


def test_bootstrap_copy_writes_first_insert_history_rows(clean: Engine):
    stats = bootstrap_copy(
        clean, [repo_item(1, full_name="octo/one"), repo_item(2, full_name="octo/two")]
    )
    assert stats.history_rows == 2
    assert sorted((row["repo_id"], row["full_name"]) for row in dump_history(clean)) == [
        (1, "octo/one"),
        (2, "octo/two"),
    ]


def test_bootstrap_copy_accounts_stats_without_full_repo_scans(clean: Engine, monkeypatch):
    monkeypatch.setattr(upserts, "_COPY_BATCH", 2)
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.lower().split()))

    sa.event.listen(clean, "before_cursor_execute", capture)
    try:
        stats = bootstrap_copy(clean, [repo_item(i) for i in range(1, 6)])
    finally:
        sa.event.remove(clean, "before_cursor_execute", capture)
    assert stats.inserted == 5
    assert stats.history_rows == 5
    assert stats.skipped == 0
    assert not any(
        re.search(r"count\(\*\) from repos(?![_a-z0-9])", statement) for statement in statements
    )


def test_bootstrap_copy_matches_upsert_repos_for_the_same_input(clean: Engine):
    items = [
        repo_item(1),
        repo_item(
            2,
            owner_id=200,
            owner_login="org-x",
            owner_type="Organization",
            parent={"full_name": "up/stream"},
        ),
        repo_item(3, private=True),
    ]
    bootstrap_stats = bootstrap_copy(clean, items)
    assert bootstrap_stats.inserted == 3
    assert bootstrap_stats.history_rows == 3
    bootstrap_repos = [
        {key: value for key, value in repo.items() if key != "indexed_at"}
        for repo in dump_repos(clean)
    ]
    bootstrap_owners = [
        {key: value for key, value in owner.items() if key != "synced_at"}
        for owner in dump_owners(clean)
    ]
    bootstrap_history = sorted((row["repo_id"], row["full_name"]) for row in dump_history(clean))
    with clean.begin() as connection:
        connection.execute(
            text("TRUNCATE TABLE owners, repos, full_name_history RESTART IDENTITY CASCADE")
        )
    upsert_stats = upsert_repos(clean, items)
    assert upsert_stats.inserted == 3
    upsert_repos_dump = [
        {key: value for key, value in repo.items() if key != "indexed_at"}
        for repo in dump_repos(clean)
    ]
    upsert_owners = [
        {key: value for key, value in owner.items() if key != "synced_at"}
        for owner in dump_owners(clean)
    ]
    assert upsert_repos_dump == bootstrap_repos
    assert upsert_owners == bootstrap_owners
    assert upsert_stats.history_rows == 3
    assert (
        sorted((row["repo_id"], row["full_name"]) for row in dump_history(clean))
        == bootstrap_history
    )
