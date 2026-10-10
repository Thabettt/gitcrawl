from __future__ import annotations

import pytest

from serve.settings_spec import SettingsError, bounds_text, parse_settings_form


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
        "discovery_concurrency": "32",
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
        parse_settings_form(form(graphql_batch_size="51"))
    assert any("graphql_batch_size" in error for error in excinfo.value.errors)
    assert excinfo.value.hints


def test_batch_size_accepts_the_raised_cap():
    assert parse_settings_form(form(graphql_batch_size="50"))["graphql_batch_size"] == 50


def test_non_integer_is_rejected():
    with pytest.raises(SettingsError):
        parse_settings_form(form(max_shards="lots"))


@pytest.mark.parametrize("value", ["0", "65"])
def test_discovery_concurrency_out_of_bounds_is_rejected(value):
    with pytest.raises(SettingsError) as excinfo:
        parse_settings_form(form(discovery_concurrency=value))
    assert any("discovery_concurrency" in error for error in excinfo.value.errors)


def test_hydrate_above_candidates_is_rejected():
    with pytest.raises(SettingsError) as excinfo:
        parse_settings_form(form(max_candidates="100", max_hydrate="200"))
    assert any("max_hydrate" in error for error in excinfo.value.errors)


def test_pinned_fields_are_ignored():
    parsed = parse_settings_form(form(max_shards="999"), pinned=frozenset({"max_shards"}))
    assert "max_shards" not in parsed


def test_reset_returns_empty_mapping():
    assert parse_settings_form(form(reset="1")) == {}


def test_corpus_preset_returns_the_coherent_profile():
    assert parse_settings_form({"preset": "corpus"}) == {
        "max_shards": 1_000,
        "max_candidates": 100_000,
        "max_hydrate": 100_000,
        "max_enrich": 100_000,
        "request_deadline_seconds": 86_400,
        "graphql_batch_size": 29,
        "limiter_max_concurrent": 32,
        "discovery_concurrency": 32,
        "graphql_batch": True,
    }


def test_corpus_preset_respects_pinned_fields():
    parsed = parse_settings_form(
        {"preset": "corpus"}, pinned=frozenset({"max_shards", "graphql_batch"})
    )
    assert "max_shards" not in parsed
    assert "graphql_batch" not in parsed
    assert parsed["max_candidates"] == 100_000


def test_bounds_text_lists_every_bounded_field():
    text = bounds_text()
    assert text["max_shards"] == "1–10,000"
    assert text["max_candidates"] == "1–1,000,000"
    assert text["request_deadline_seconds"] == "60–86,400"
    assert text["graphql_batch_size"] == "1–50"
    assert text["limiter_max_concurrent"] == "1–100"
    assert text["discovery_concurrency"] == "1–64"
    assert "graphql_batch" not in text
