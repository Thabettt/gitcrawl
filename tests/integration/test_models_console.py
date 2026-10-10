from __future__ import annotations

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import CHAR, BigInteger, Integer, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.engine import Engine

from store.models import Base

CONSOLE_TABLES = {"runs", "run_items", "saved_filters"}

ALL_TABLES = {
    "repos",
    "owners",
    "full_name_history",
    "geo_cache",
    "shards",
    "audit_log",
    "runs",
    "run_items",
    "saved_filters",
    "app_settings",
    "corpora",
}

RUN_COLUMNS: dict[str, bool] = {
    "id": False,
    "filter_hash": False,
    "filter_spec": False,
    "status": False,
    "created_at": False,
    "started_at": True,
    "finished_at": True,
    "api_version": False,
    "total_count": True,
    "fetched": False,
    "inserted": False,
    "updated": False,
    "unchanged": False,
    "skipped": False,
    "incomplete_shards": False,
    "error": True,
    "bundle_dir": True,
    "progress_phase": True,
    "progress_done": True,
    "progress_total": True,
    "progress_updated_at": True,
    "progress_started_at": True,
}

RUN_ITEM_COLUMNS: dict[str, bool] = {
    "id": False,
    "run_id": False,
    "repo_id": True,
    "full_name": False,
    "stargazers": True,
    "pushed_at": True,
    "archived": True,
    "language": True,
    "license_spdx": True,
    "country_iso": True,
    "geo_confidence": True,
    "virtuals": False,
}

SAVED_FILTER_COLUMNS: dict[str, bool] = {
    "id": False,
    "name": False,
    "filter_spec": False,
    "created_at": False,
    "updated_at": False,
}

CORPUS_COLUMNS: dict[str, bool] = {
    "id": False,
    "name": False,
    "source_run_id": False,
    "note": True,
    "repo_count": False,
    "frozen_at": False,
}


@pytest.fixture(scope="module", autouse=True)
def migrated(alembic_config, alembic_engine: Engine) -> Engine:
    command.upgrade(alembic_config, "head")
    return alembic_engine


@pytest.fixture()
def clean(clean_db):
    return clean_db()


def _columns(engine: Engine, table: str) -> dict[str, dict[str, object]]:
    return {column["name"]: column for column in inspect(engine).get_columns(table)}


def _indexdefs(engine: Engine) -> dict[str, str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'")
        ).all()
    return dict(rows)


def test_single_alembic_head(alembic_config):
    assert ScriptDirectory.from_config(alembic_config).get_heads() == ["0015"]


def test_metadata_declares_exactly_the_expected_tables():
    assert set(Base.metadata.tables) == ALL_TABLES


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("runs", RUN_COLUMNS),
        ("run_items", RUN_ITEM_COLUMNS),
        ("saved_filters", SAVED_FILTER_COLUMNS),
        ("corpora", CORPUS_COLUMNS),
    ],
)
def test_console_columns_match_schema(migrated: Engine, table: str, expected: dict[str, bool]):
    actual = {name: column["nullable"] for name, column in _columns(migrated, table).items()}
    assert actual == expected


def test_runs_types_and_defaults(migrated: Engine):
    columns = _columns(migrated, "runs")
    assert isinstance(columns["id"]["type"], BigInteger)
    assert "nextval" in (columns["id"]["default"] or "")
    assert isinstance(columns["filter_spec"]["type"], JSONB)
    assert isinstance(columns["total_count"]["type"], Integer)
    for name in ("created_at", "started_at", "finished_at"):
        assert isinstance(columns[name]["type"], postgresql.TIMESTAMP)
        assert columns[name]["type"].timezone is True
    assert "'queued'" in (columns["status"]["default"] or "")
    assert "now()" in (columns["created_at"]["default"] or "")
    for name in ("fetched", "inserted", "updated", "unchanged", "skipped", "incomplete_shards"):
        assert "0" in (columns[name]["default"] or "")
    assert inspect(migrated).get_pk_constraint("runs")["constrained_columns"] == ["id"]


def test_run_items_types_primary_key_and_foreign_keys(migrated: Engine):
    columns = _columns(migrated, "run_items")
    assert isinstance(columns["full_name"]["type"], CITEXT)
    assert isinstance(columns["virtuals"]["type"], JSONB)
    assert isinstance(columns["stargazers"]["type"], Integer)
    assert isinstance(columns["country_iso"]["type"], CHAR)
    assert columns["country_iso"]["type"].length == 2
    assert isinstance(columns["pushed_at"]["type"], postgresql.TIMESTAMP)
    assert columns["pushed_at"]["type"].timezone is True
    assert "'{}'" in (columns["virtuals"]["default"] or "")
    inspector = inspect(migrated)
    assert inspector.get_pk_constraint("run_items")["constrained_columns"] == ["id"]
    foreign_keys = {
        fk["constrained_columns"][0]: fk for fk in inspector.get_foreign_keys("run_items")
    }
    assert foreign_keys["run_id"]["referred_table"] == "runs"
    assert foreign_keys["run_id"]["options"].get("ondelete") == "CASCADE"
    assert foreign_keys["repo_id"]["referred_table"] == "repos"
    assert foreign_keys["repo_id"]["options"].get("ondelete") == "SET NULL"
    unique = {tuple(item["column_names"]) for item in inspector.get_unique_constraints("run_items")}
    assert ("run_id", "repo_id") in unique


def test_saved_filters_unique_name_and_types(migrated: Engine):
    columns = _columns(migrated, "saved_filters")
    assert isinstance(columns["id"]["type"], BigInteger)
    assert isinstance(columns["filter_spec"]["type"], JSONB)
    for name in ("created_at", "updated_at"):
        assert isinstance(columns[name]["type"], postgresql.TIMESTAMP)
        assert columns[name]["type"].timezone is True
        assert "now()" in (columns[name]["default"] or "")
    unique = {
        tuple(item["column_names"])
        for item in inspect(migrated).get_unique_constraints("saved_filters")
    }
    assert ("name",) in unique


def test_corpora_foreign_key_unique_name_and_types(migrated: Engine):
    columns = _columns(migrated, "corpora")
    assert isinstance(columns["id"]["type"], BigInteger)
    assert "nextval" in (columns["id"]["default"] or "")
    assert isinstance(columns["repo_count"]["type"], Integer)
    assert isinstance(columns["frozen_at"]["type"], postgresql.TIMESTAMP)
    assert columns["frozen_at"]["type"].timezone is True
    assert "now()" in (columns["frozen_at"]["default"] or "")
    inspector = inspect(migrated)
    assert inspector.get_pk_constraint("corpora")["constrained_columns"] == ["id"]
    foreign_keys = inspector.get_foreign_keys("corpora")
    assert foreign_keys[0]["referred_table"] == "runs"
    assert foreign_keys[0]["constrained_columns"] == ["source_run_id"]
    unique = {tuple(item["column_names"]) for item in inspector.get_unique_constraints("corpora")}
    assert ("name",) in unique


def test_console_indexes_exist(migrated: Engine):
    defs = _indexdefs(migrated)
    assert defs["runs_created_idx"].endswith("(created_at DESC)")
    assert defs["runs_filter_hash_idx"].endswith("(filter_hash, created_at DESC)")
    assert defs["run_items_repo_idx"].endswith("(repo_id)")
    assert defs["run_items_stars_idx"].endswith("(run_id, stargazers DESC NULLS LAST, repo_id)")


def test_model_metadata_indexes_match_console_contract():
    runs = {index.name for index in Base.metadata.tables["runs"].indexes}
    run_items = {index.name for index in Base.metadata.tables["run_items"].indexes}
    assert runs == {"runs_created_idx", "runs_filter_hash_idx"}
    assert run_items == {"run_items_repo_idx", "run_items_stars_idx"}
    assert Base.metadata.tables["saved_filters"].indexes == set()


def test_deleting_run_cascades_to_run_items(clean: Engine):
    with clean.begin() as connection:
        connection.execute(text("INSERT INTO owners (id, login, type) VALUES (1, 'octo', 'User')"))
        connection.execute(
            text(
                "INSERT INTO repos (id, node_id, full_name, owner_id, name, visibility) "
                "VALUES (1, 'n1', 'octo/hello', 1, 'hello', 'public')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO runs (id, filter_hash, filter_spec, api_version) "
                "VALUES (1, 'hash', '{}'::jsonb, 'v1')"
            )
        )
        connection.execute(
            text("INSERT INTO run_items (run_id, repo_id, full_name) VALUES (1, 1, 'octo/hello')")
        )
        connection.execute(text("DELETE FROM runs WHERE id = 1"))
        remaining = connection.execute(text("SELECT count(*) FROM run_items")).scalar()
    assert remaining == 0


def test_repo_delete_nulls_snapshot_repo_ids(clean: Engine):
    with clean.begin() as connection:
        connection.execute(text("INSERT INTO owners (id, login, type) VALUES (1, 'octo', 'User')"))
        connection.execute(
            text(
                "INSERT INTO repos (id, node_id, full_name, owner_id, name, visibility) "
                "VALUES (1, 'n1', 'octo/hello', 1, 'hello', 'public')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO runs (id, filter_hash, filter_spec, api_version) "
                "VALUES (1, 'hash', '{}'::jsonb, 'v1')"
            )
        )
        connection.execute(
            text("INSERT INTO run_items (run_id, repo_id, full_name) VALUES (1, 1, 'octo/hello')")
        )
        connection.execute(text("DELETE FROM repos WHERE id = 1"))
        rows = connection.execute(
            text("SELECT repo_id, full_name FROM run_items WHERE run_id = 1")
        ).all()
    assert [(row[0], row[1]) for row in rows] == [(None, "octo/hello")]


def test_console_migration_round_trip(alembic_config, alembic_engine: Engine):
    command.downgrade(alembic_config, "0002")
    tables = set(inspect(alembic_engine).get_table_names())
    assert not (CONSOLE_TABLES & tables)
    assert "corpora" not in tables
    command.upgrade(alembic_config, "head")
    assert CONSOLE_TABLES <= set(inspect(alembic_engine).get_table_names())
