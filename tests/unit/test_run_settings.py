from __future__ import annotations

import pytest

from store.settings import SETTINGS_ENV, RunSettings, env_pinned_fields, load_run_settings


class _EmptyResult:
    def mappings(self):
        return self

    def one_or_none(self):
        return None


class _EmptyConnection:
    def execute(self, statement):
        return _EmptyResult()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _EmptyEngine:
    def connect(self):
        return _EmptyConnection()


def test_settings_env_names_are_stable():
    assert SETTINGS_ENV["max_shards"] == "GITCRAWL_MAX_SHARDS"
    assert SETTINGS_ENV["request_deadline_seconds"] == "GITCRAWL_REQUEST_DEADLINE_SECONDS"
    assert SETTINGS_ENV["graphql_batch"] == "GITCRAWL_GRAPHQL_BATCH"
    assert SETTINGS_ENV["adaptive"] == "GITCRAWL_ADAPTIVE"
    assert SETTINGS_ENV["limiter_max_concurrent"] == "GITCRAWL_MAX_CONCURRENT"
    assert SETTINGS_ENV["discovery_concurrency"] == "GITCRAWL_DISCOVERY_CONCURRENCY"


def test_discovery_concurrency_defaults_to_32():
    assert RunSettings().discovery_concurrency == 32


def test_graphql_batch_size_defaults_to_29():
    assert RunSettings().graphql_batch_size == 29


def test_adaptive_defaults_to_false():
    assert RunSettings().adaptive is False


def test_env_pinned_fields_lists_only_set_variables(monkeypatch):
    monkeypatch.delenv("GITCRAWL_MAX_SHARDS", raising=False)
    monkeypatch.setenv("GITCRAWL_GRAPHQL_BATCH", "0")
    assert env_pinned_fields() == frozenset({"graphql_batch"})


def test_as_dict_round_trips():
    settings = RunSettings()
    assert RunSettings(**settings.as_dict()) == settings


@pytest.mark.parametrize("raw", ["65", "999"])
def test_env_discovery_concurrency_is_clamped_to_the_form_bound(monkeypatch, raw):
    monkeypatch.setenv("GITCRAWL_DISCOVERY_CONCURRENCY", raw)
    assert load_run_settings(_EmptyEngine()).discovery_concurrency == 64


def test_env_discovery_concurrency_within_bounds_is_kept(monkeypatch):
    monkeypatch.setenv("GITCRAWL_DISCOVERY_CONCURRENCY", "48")
    assert load_run_settings(_EmptyEngine()).discovery_concurrency == 48
