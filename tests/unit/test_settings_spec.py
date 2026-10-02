from __future__ import annotations

import pytest

from serve.settings_spec import SettingsError, parse_settings_form


def form(**overrides: str) -> dict[str, str]:
    values = {
        "max_shards": "10",
        "max_candidates": "500",
        "max_hydrate": "200",
        "max_enrich": "100",
        "request_deadline_seconds": "3600",
        "graphql_batch": "on",
        "graphql_batch_size": "20",
        "limiter_max_concurrent": "10",
    }
    values.update(overrides)
    return values


def test_parse_accepts_valid_form():
    parsed = parse_settings_form(form())
    assert parsed["max_hydrate"] == 200
    assert parsed["graphql_batch"] is True


def test_unchecked_checkbox_is_false():
    values = form()
    values.pop("graphql_batch")
    assert parse_settings_form(values)["graphql_batch"] is False


def test_out_of_range_value_is_rejected_with_a_hint():
    with pytest.raises(SettingsError) as excinfo:
        parse_settings_form(form(graphql_batch_size="21"))
    assert any("graphql_batch_size" in error for error in excinfo.value.errors)
    assert excinfo.value.hints


def test_non_integer_is_rejected():
    with pytest.raises(SettingsError):
        parse_settings_form(form(max_shards="lots"))


def test_hydrate_above_candidates_is_rejected():
    with pytest.raises(SettingsError) as excinfo:
        parse_settings_form(form(max_candidates="100", max_hydrate="200"))
    assert any("max_hydrate" in error for error in excinfo.value.errors)


def test_pinned_fields_are_ignored():
    parsed = parse_settings_form(form(max_shards="999"), pinned=frozenset({"max_shards"}))
    assert "max_shards" not in parsed


def test_reset_returns_empty_mapping():
    assert parse_settings_form(form(reset="1")) == {}
