from __future__ import annotations

from store.settings import SETTINGS_ENV, RunSettings, env_pinned_fields


def test_settings_env_names_are_stable():
    assert SETTINGS_ENV["max_shards"] == "GITCRAWL_MAX_SHARDS"
    assert SETTINGS_ENV["request_deadline_seconds"] == "GITCRAWL_REQUEST_DEADLINE_SECONDS"
    assert SETTINGS_ENV["graphql_batch"] == "GITCRAWL_GRAPHQL_BATCH"
    assert SETTINGS_ENV["limiter_max_concurrent"] == "GITCRAWL_MAX_CONCURRENT"
    assert SETTINGS_ENV["discovery_concurrency"] == "GITCRAWL_DISCOVERY_CONCURRENCY"


def test_discovery_concurrency_defaults_to_32():
    assert RunSettings().discovery_concurrency == 32


def test_env_pinned_fields_lists_only_set_variables(monkeypatch):
    monkeypatch.delenv("GITCRAWL_MAX_SHARDS", raising=False)
    monkeypatch.setenv("GITCRAWL_GRAPHQL_BATCH", "0")
    assert env_pinned_fields() == frozenset({"graphql_batch"})


def test_as_dict_round_trips():
    settings = RunSettings()
    assert RunSettings(**settings.as_dict()) == settings
