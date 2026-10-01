from __future__ import annotations

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

from enrich.geo_resolver import (
    GeoCache,
    GeoResult,
    normalize_location,
    resolve_location,
    resolve_owner,
)


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(alembic_engine: Engine) -> Engine:
    with alembic_engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE geo_cache"))
    return alembic_engine


def test_flag_emoji_decodes_to_exact_iso():
    raw = "🇩🇪 Berlin"
    assert resolve_location(raw) == GeoResult("DE", "exact-iso", raw)


def test_flag_without_text_decodes():
    assert resolve_location("🇩🇪") == GeoResult("DE", "exact-iso", "🇩🇪")


def test_flag_appended_to_candidate_decodes():
    raw = "Berlin 🇩🇪"
    assert resolve_location(raw) == GeoResult("DE", "exact-iso", raw)


def test_multiple_flags_first_flag_wins():
    first = resolve_location("🇩🇪 Berlin / 🇫🇷 Paris")
    assert first == GeoResult("DE", "exact-iso", "🇩🇪 Berlin / 🇫🇷 Paris")
    assert resolve_location("🇫🇷 Paris / 🇩🇪 Berlin").country_iso == "FR"


def test_gazetteer_city_resolves():
    result = resolve_location("Lagos")
    assert result == GeoResult("NG", "gazetteer-city", "Lagos")


def test_unknown_city_without_geocoder_is_unmatched():
    assert resolve_location("Atlantis") == GeoResult(None, "unmatched", "Atlantis")


def test_remote_emoji_is_unmatched():
    assert resolve_location("🌍 remote") == GeoResult(None, "unmatched", "🌍 remote")


def test_multi_candidate_returns_first_confident_hit():
    assert resolve_location("Berlin / NYC").country_iso == "DE"
    assert resolve_location("NYC / Berlin").country_iso == "DE"
    assert resolve_location("Nowhereville, Lagos").country_iso == "NG"


def test_multiword_candidate_prefix_matches():
    assert resolve_location("New York City").country_iso == "US"
    assert resolve_location("Berlin Germany").country_iso == "DE"


@pytest.mark.parametrize(
    ("raw", "iso"),
    [
        ("USA", "US"),
        ("united states of america", "US"),
        ("Nederland", "NL"),
        ("Holland", "NL"),
        ("Dutch", "NL"),
        ("Deutschland", "DE"),
        ("German", "DE"),
        ("Nigerian", "NG"),
        ("United Kingdom", "GB"),
        ("UK", "GB"),
        ("UAE", "AE"),
        ("Emirates", "AE"),
        ("Brasil", "BR"),
        ("Japanese", "JP"),
        ("España", "ES"),
    ],
)
def test_country_aliases_use_name_tier(raw, iso):
    result = resolve_location(raw)
    assert result.country_iso == iso
    assert result.confidence == "name"
    assert result.raw_location == raw


def test_case_punctuation_and_emoji_noise():
    raw = "  bErLiN!!!  "
    assert resolve_location(raw) == GeoResult("DE", "gazetteer-city", raw)
    assert resolve_location("  🇩🇪  bErLiN!!!  ").country_iso == "DE"
    assert resolve_location("Deutschland!!").confidence == "name"
    assert resolve_location("Berlin • NYC").country_iso == "DE"
    assert resolve_location("Berlin, Germany").country_iso == "DE"


def test_normalize_location_is_stable_and_collapses_noise():
    assert normalize_location("  🇩🇪 Berlin!!! / NYC  ") == "🇩🇪 berlin nyc"
    assert normalize_location("Berlin / NYC") == normalize_location("Berlin / NYC")
    assert normalize_location("Berlin • NYC") == "berlin nyc"
    assert normalize_location(normalize_location("Berlin • NYC")) == normalize_location("Berlin • NYC")
    assert normalize_location("🌍 Remote") == "remote"
    assert normalize_location("") == ""
    assert normalize_location("!!!") == ""


def test_geocoder_resolves_residue():
    result = resolve_location("Atlantis", geocoder=lambda candidate: "FR")
    assert result == GeoResult("FR", "geocoder", "Atlantis")


def test_injectable_gazetteer_is_preferred():
    assert resolve_location("Gotham", gazetteer={"gotham": "US"}) == GeoResult(
        "US", "gazetteer-city", "Gotham"
    )
    assert resolve_location("Lagos", gazetteer={"gotham": "US"}).country_iso == "NG"


def test_injectable_gazetteer_can_override_embedded():
    assert resolve_location("Lagos", gazetteer={"lagos": "GH"}).country_iso == "GH"


def test_weak_blog_tld():
    result = resolve_location("Atlantis", blog="https://atlantis.example.de/about")
    assert result == GeoResult("DE", "weak", "Atlantis")
    assert resolve_location("Atlantis", blog="https://example.co.uk").country_iso == "GB"


def test_weak_blog_unmapped_tld_is_unmatched():
    assert resolve_location("Atlantis", blog="https://example.com").confidence == "unmatched"
    assert resolve_location("Atlantis", blog=None).confidence == "unmatched"


def test_weak_timezone_single_country_band():
    assert resolve_location("Atlantis", tz_offset=5.5) == GeoResult("IN", "weak", "Atlantis")
    assert resolve_location("Atlantis", tz_offset=5.75).country_iso == "NP"


def test_weak_timezone_ambiguous_band_never_guesses():
    assert resolve_location("Atlantis", tz_offset=-8.0).confidence == "unmatched"
    assert resolve_location("Atlantis", tz_offset=9.0).confidence == "unmatched"


def test_weak_never_overrides_stronger_tiers():
    strong = resolve_location("Lagos", blog="https://example.de", tz_offset=-8.0)
    assert strong == GeoResult("NG", "gazetteer-city", "Lagos")
    assert resolve_location("USA", blog="https://example.fr").country_iso == "US"


def test_none_raw_keeps_none():
    assert resolve_location(None) == GeoResult(None, "unmatched", None)


def test_empty_and_weird_raw_never_raise():
    assert resolve_location("   ") == GeoResult(None, "unmatched", "   ")
    assert resolve_location("!!!").confidence == "unmatched"
    assert resolve_location(42) == GeoResult(None, "unmatched", "42")


def test_geo_cache_round_trip_and_hit_counter(clean: Engine):
    cache = GeoCache(clean)
    assert cache.get("lagos") is None
    result = GeoResult("NG", "gazetteer-city", "Lagos")
    cache.put("lagos", result)
    assert cache.get("lagos") == result
    cache.put("lagos", GeoResult("NG", "gazetteer-city", "Lagos, Nigeria"))
    stored = cache.get("lagos")
    assert stored is not None
    assert stored.raw_location == "Lagos, Nigeria"
    with clean.connect() as connection:
        hits = connection.execute(
            sa.text("SELECT hits FROM geo_cache WHERE normalized = 'lagos'")
        ).scalar_one()
    assert hits == 2


def test_resolve_owner_calls_geocoder_once_across_calls(clean: Engine):
    calls: list[str] = []

    def geocoder(candidate: str) -> str | None:
        calls.append(candidate)
        return "FR"

    first = resolve_owner(clean, "Atlantis", geocoder=geocoder)
    second = resolve_owner(clean, "atlantis!", geocoder=geocoder)
    assert first == GeoResult("FR", "geocoder", "Atlantis")
    assert second == first
    assert calls == ["atlantis"]


def test_resolve_owner_skips_geocoder_for_known_tiers(clean: Engine):
    def geocoder(candidate: str) -> str | None:
        raise AssertionError(f"geocoder should not be called for {candidate}")

    assert resolve_owner(clean, "Lagos", geocoder=geocoder).country_iso == "NG"
    assert resolve_owner(clean, "🇩🇪 Berlin", geocoder=geocoder).country_iso == "DE"
    assert resolve_owner(clean, "USA", geocoder=geocoder).country_iso == "US"


def test_resolve_owner_rejects_invalid_geocoder_output(clean: Engine):
    result = resolve_owner(clean, "Atlantis", geocoder=lambda candidate: "not-a-country")
    assert result == GeoResult(None, "unmatched", "Atlantis")
