from __future__ import annotations

import pytest
from alembic import command

from store.settings import RunSettings, load_run_settings, update_run_settings


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(clean_db):
    return clean_db()


def test_defaults_are_seeded_by_the_migration(clean):
    assert load_run_settings(clean) == RunSettings()


def test_update_persists_and_returns_effective_settings(clean):
    updated = update_run_settings(clean, {"max_hydrate": 50, "graphql_batch": False})
    assert updated.max_hydrate == 50
    assert updated.graphql_batch is False
    assert load_run_settings(clean) == updated


def test_env_overrides_the_row(clean, monkeypatch):
    update_run_settings(clean, {"max_shards": 25})
    monkeypatch.setenv("GITCRAWL_MAX_SHARDS", "3")
    settings = load_run_settings(clean)
    assert settings.max_shards == 3


def test_invalid_env_value_falls_back_to_the_row(clean, monkeypatch):
    update_run_settings(clean, {"max_shards": 25})
    monkeypatch.setenv("GITCRAWL_MAX_SHARDS", "not-a-number")
    assert load_run_settings(clean).max_shards == 25


def test_env_graphql_batch_size_is_clamped_to_the_api_cap(clean, monkeypatch):
    monkeypatch.setenv("GITCRAWL_GRAPHQL_BATCH_SIZE", "50")
    assert load_run_settings(clean).graphql_batch_size == 20


def test_unknown_field_is_rejected(clean):
    with pytest.raises(KeyError):
        update_run_settings(clean, {"nope": 1})
