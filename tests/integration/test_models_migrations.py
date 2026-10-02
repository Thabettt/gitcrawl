from __future__ import annotations

import pytest
from alembic import command
from conftest import UnsafeTestDatabase, assert_test_database
from sqlalchemy import CHAR, BigInteger, Integer, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB
from sqlalchemy.engine import Engine

from store.models import Base

TABLES = {
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
}

EXPECTED_COLUMNS: dict[str, dict[str, bool]] = {
    "repos": {
        "id": False,
        "node_id": False,
        "full_name": False,
        "owner_id": False,
        "name": False,
        "description": True,
        "homepage": True,
        "language": True,
        "license_spdx": True,
        "topics": False,
        "visibility": False,
        "fork": False,
        "parent_full_name": True,
        "source_full_name": True,
        "archived": False,
        "disabled": False,
        "mirror_url": True,
        "is_template": False,
        "size_kb": True,
        "stargazers": False,
        "forks_count": False,
        "watchers": False,
        "open_issues": False,
        "default_branch": True,
        "has_wiki": True,
        "has_issues": True,
        "has_projects": True,
        "has_pages": True,
        "has_discussions": True,
        "has_pull_requests": True,
        "custom_properties": False,
        "created_at": True,
        "pushed_at": True,
        "updated_at": True,
        "etag": True,
        "deleted_at": True,
        "indexed_at": False,
    },
    "owners": {
        "id": False,
        "login": False,
        "type": False,
        "location_raw": True,
        "country_iso": True,
        "geo_confidence": True,
        "company": True,
        "blog": True,
        "etag": True,
        "synced_at": False,
    },
    "full_name_history": {
        "id": False,
        "repo_id": False,
        "full_name": False,
        "seen_at": False,
    },
    "geo_cache": {
        "normalized": False,
        "country_iso": True,
        "confidence": False,
        "raw_sample": True,
        "hits": False,
        "updated_at": False,
    },
    "shards": {
        "id": False,
        "kind": False,
        "query": True,
        "range_start": True,
        "range_end": True,
        "since_id": True,
        "since_max": True,
        "org": True,
        "tier": False,
        "state": False,
        "watermark": True,
        "total_count": True,
        "fetched": True,
        "incomplete": False,
        "updated_at": False,
    },
    "audit_log": {
        "id": False,
        "ts": False,
        "query_hash": True,
        "params": False,
        "etag_sent": True,
        "status": False,
        "rl_limit": True,
        "rl_remaining": True,
        "rl_reset": True,
        "rl_resource": True,
        "retry_after": True,
        "link_next": True,
        "total_count": True,
        "incomplete_results": True,
        "token_fp": False,
        "latency_ms": False,
    },
    "app_settings": {
        "id": False,
        "max_shards": False,
        "max_candidates": False,
        "max_hydrate": False,
        "max_enrich": False,
        "request_deadline_seconds": False,
        "graphql_batch": False,
        "graphql_batch_size": False,
        "limiter_max_concurrent": False,
        "updated_at": False,
    },
}

EXPECTED_INDEXES = {
    "repos_pushed_idx": "repos",
    "repos_updated_idx": "repos",
    "repos_stars_idx": "repos",
    "repos_owner_idx": "repos",
    "repos_deleted_idx": "repos",
    "fnh_repo_idx": "full_name_history",
    "shards_state_idx": "shards",
    "audit_ts_idx": "audit_log",
}


@pytest.fixture(scope="module")
def migrated(alembic_config, alembic_engine: Engine) -> Engine:
    command.upgrade(alembic_config, "head")
    return alembic_engine


def _indexdefs(engine: Engine) -> dict[str, str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'")
        ).all()
    return dict(rows)


def _columns(engine: Engine, table: str) -> dict[str, dict[str, object]]:
    return {column["name"]: column for column in inspect(engine).get_columns(table)}


def _reloptions(engine: Engine) -> dict[str, list[str] | None]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT relname, reloptions FROM pg_class "
                "WHERE relnamespace = 'public'::regnamespace AND relname IN ('repos', 'audit_log')"
            )
        ).all()
    return {name: options for name, options in rows}


def test_metadata_declares_exactly_the_expected_tables():
    assert set(Base.metadata.tables) == TABLES


@pytest.mark.parametrize("table", sorted(EXPECTED_COLUMNS))
def test_columns_match_schema(migrated: Engine, table: str):
    actual = {name: column["nullable"] for name, column in _columns(migrated, table).items()}
    assert actual == EXPECTED_COLUMNS[table]


def test_primary_keys(migrated: Engine):
    inspector = inspect(migrated)
    expected = {
        "repos": ["id"],
        "owners": ["id"],
        "full_name_history": ["id"],
        "geo_cache": ["normalized"],
        "shards": ["id"],
        "audit_log": ["id"],
    }
    for table, columns in expected.items():
        assert inspector.get_pk_constraint(table)["constrained_columns"] == columns


def test_repos_postgres_types(migrated: Engine):
    columns = _columns(migrated, "repos")
    assert isinstance(columns["full_name"]["type"], CITEXT)
    assert isinstance(columns["topics"]["type"], ARRAY)
    assert isinstance(columns["custom_properties"]["type"], JSONB)
    for name in ("created_at", "pushed_at", "updated_at", "deleted_at", "indexed_at"):
        assert isinstance(columns[name]["type"], postgresql.TIMESTAMP)
        assert columns[name]["type"].timezone is True


def test_owners_and_geo_cache_types(migrated: Engine):
    owners = _columns(migrated, "owners")
    assert isinstance(owners["login"]["type"], CITEXT)
    assert isinstance(owners["country_iso"]["type"], CHAR)
    assert owners["country_iso"]["type"].length == 2
    assert isinstance(owners["synced_at"]["type"], postgresql.TIMESTAMP)
    geo = _columns(migrated, "geo_cache")
    assert isinstance(geo["country_iso"]["type"], CHAR)
    assert geo["country_iso"]["type"].length == 2
    assert isinstance(geo["hits"]["type"], Integer)


def test_history_and_shard_types(migrated: Engine):
    history = _columns(migrated, "full_name_history")
    assert isinstance(history["full_name"]["type"], CITEXT)
    assert isinstance(history["seen_at"]["type"], postgresql.TIMESTAMP)
    assert history["seen_at"]["type"].timezone is True
    shards = _columns(migrated, "shards")
    assert isinstance(shards["since_id"]["type"], BigInteger)
    assert isinstance(shards["since_max"]["type"], BigInteger)
    assert isinstance(shards["range_start"]["type"], postgresql.TIMESTAMP)
    assert shards["range_start"]["type"].timezone is True
    assert isinstance(shards["fetched"]["type"], Integer)


def test_audit_log_types(migrated: Engine):
    audit = _columns(migrated, "audit_log")
    assert isinstance(audit["params"]["type"], JSONB)
    assert isinstance(audit["rl_reset"]["type"], BigInteger)
    assert isinstance(audit["latency_ms"]["type"], Integer)
    assert isinstance(audit["ts"]["type"], postgresql.TIMESTAMP)
    assert audit["ts"]["type"].timezone is True


def test_server_defaults(migrated: Engine):
    expected = {
        ("repos", "topics"): "'{}'",
        ("repos", "custom_properties"): "'{}'",
        ("repos", "fork"): "false",
        ("repos", "stargazers"): "0",
        ("repos", "indexed_at"): "now()",
        ("owners", "synced_at"): "now()",
        ("full_name_history", "seen_at"): "now()",
        ("geo_cache", "hits"): "1",
        ("geo_cache", "updated_at"): "now()",
        ("shards", "tier"): "'cold'",
        ("shards", "state"): "'pending'",
        ("shards", "incomplete"): "false",
        ("shards", "updated_at"): "now()",
        ("audit_log", "ts"): "now()",
    }
    for (table, column), fragment in expected.items():
        assert fragment in (_columns(migrated, table)[column]["default"] or "")


def test_foreign_keys(migrated: Engine):
    inspector = inspect(migrated)
    owner_fk = inspector.get_foreign_keys("repos")[0]
    assert owner_fk["referred_table"] == "owners"
    assert owner_fk["constrained_columns"] == ["owner_id"]
    history_fk = inspector.get_foreign_keys("full_name_history")[0]
    assert history_fk["referred_table"] == "repos"
    assert history_fk["constrained_columns"] == ["repo_id"]
    assert history_fk["options"].get("ondelete") == "CASCADE"


def test_unique_constraints(migrated: Engine):
    inspector = inspect(migrated)
    repos = {tuple(item["column_names"]) for item in inspector.get_unique_constraints("repos")}
    owners = {tuple(item["column_names"]) for item in inspector.get_unique_constraints("owners")}
    assert ("full_name",) in repos
    assert ("login",) in owners


def test_repos_id_is_not_serial_and_generated_ids_are(migrated: Engine):
    assert _columns(migrated, "repos")["id"]["default"] is None
    for table in ("full_name_history", "shards", "audit_log"):
        assert "nextval" in (_columns(migrated, table)["id"]["default"] or "")


def test_citext_extension_installed(migrated: Engine):
    with migrated.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM pg_extension WHERE extname = 'citext'")
        ).scalar()
    assert count == 1


def test_indexes_exist_with_partial_predicates(migrated: Engine):
    defs = _indexdefs(migrated)
    assert set(EXPECTED_INDEXES) <= set(defs)
    assert "repos_topics_gin" not in defs
    for name in ("repos_pushed_idx", "repos_updated_idx", "repos_owner_idx"):
        assert "WHERE (deleted_at IS NULL)" in defs[name]
    assert "stargazers DESC" in defs["repos_stars_idx"]
    assert "WHERE (deleted_at IS NULL)" in defs["repos_stars_idx"]
    assert "(deleted_at) WHERE (deleted_at IS NOT NULL)" in defs["repos_deleted_idx"]
    assert defs["fnh_repo_idx"].endswith("(repo_id, seen_at)")
    assert defs["shards_state_idx"].endswith("(state, tier)")
    assert defs["audit_ts_idx"].endswith("(ts)")


def test_storage_parameters(migrated: Engine):
    options = _reloptions(migrated)
    repos = set(options["repos"] or [])
    audit = set(options["audit_log"] or [])
    assert {
        "fillfactor=80",
        "autovacuum_vacuum_scale_factor=0.02",
        "autovacuum_vacuum_insert_scale_factor=0.02",
    } <= repos
    assert {
        "autovacuum_vacuum_scale_factor=0.02",
        "autovacuum_vacuum_insert_scale_factor=0.02",
    } <= audit


def test_model_metadata_indexes_match_contract():
    repos = {index.name for index in Base.metadata.tables["repos"].indexes}
    assert repos == {
        "repos_pushed_idx",
        "repos_updated_idx",
        "repos_stars_idx",
        "repos_owner_idx",
        "repos_deleted_idx",
    }
    assert {index.name for index in Base.metadata.tables["full_name_history"].indexes} == {
        "fnh_repo_idx"
    }
    assert {index.name for index in Base.metadata.tables["shards"].indexes} == {"shards_state_idx"}
    assert {index.name for index in Base.metadata.tables["audit_log"].indexes} == {"audit_ts_idx"}
    predicates = {
        str(index.dialect_options["postgresql"]["where"])
        for index in Base.metadata.tables["repos"].indexes
    }
    assert predicates == {"deleted_at IS NULL", "deleted_at IS NOT NULL"}


def test_guard_refuses_non_test_database():
    with pytest.raises(UnsafeTestDatabase):
        assert_test_database("postgresql+psycopg://gitcrawl:pw@localhost:5432/gitcrawl")
    with pytest.raises(UnsafeTestDatabase):
        assert_test_database("postgresql+psycopg://gitcrawl:pw@localhost:5432/gitcrawl_dev")
    url = "postgresql+psycopg://gitcrawl:pw@localhost:5432/gitcrawl_test"
    assert assert_test_database(url).endswith("/gitcrawl_test")


def test_downgrade_and_upgrade_round_trip(alembic_config, alembic_engine: Engine):
    command.downgrade(alembic_config, "base")
    assert set(inspect(alembic_engine).get_table_names()) == {"alembic_version"}
    command.upgrade(alembic_config, "head")
    assert TABLES <= set(inspect(alembic_engine).get_table_names())
