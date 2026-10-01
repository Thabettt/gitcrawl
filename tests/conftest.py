from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url

ROOT = Path(__file__).resolve().parents[1]


class UnsafeTestDatabase(RuntimeError):
    pass


def assert_test_database(url: str) -> str:
    database = make_url(url).database or ""
    if not database.endswith("_test"):
        raise UnsafeTestDatabase(
            f"refusing destructive database tests against database {database!r}; "
            "the database name must end with '_test'"
        )
    return url


REQUIRE_TEST_DB_ENV = "GITCRAWL_REQUIRE_TEST_DB"


class MissingTestDatabase(RuntimeError):
    pass


def require_test_database() -> bool:
    return os.environ.get(REQUIRE_TEST_DB_ENV) == "1"


def resolve_test_database_url() -> str | None:
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        if require_test_database():
            raise MissingTestDatabase(
                "TEST_DATABASE_URL is required when GITCRAWL_REQUIRE_TEST_DB=1; "
                "refusing to skip database tests"
            )
        return None
    return assert_test_database(url)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    try:
        url = resolve_test_database_url()
    except MissingTestDatabase as exc:
        pytest.fail(str(exc), pytrace=False)
    if url is None:
        pytest.skip("TEST_DATABASE_URL is not set")
    return url


@pytest.fixture(scope="session")
def alembic_config(test_database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.attributes["database_url"] = test_database_url
    return config


@pytest.fixture(scope="session")
def alembic_engine(test_database_url: str) -> Iterator[Engine]:
    engine = create_engine(test_database_url)
    yield engine
    engine.dispose()
