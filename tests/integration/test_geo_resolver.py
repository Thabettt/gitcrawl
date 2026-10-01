from __future__ import annotations

from fractions import Fraction

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

from enrich.geo_resolver import (
    KNOWN_ISO_CODES,
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
def clean(clean_db):
    return clean_db()


def test_known_iso_codes_exports_all_official_alpha2_codes():
    assert isinstance(KNOWN_ISO_CODES, frozenset)
    assert len(KNOWN_ISO_CODES) == 249
    for code in ("AD", "DE", "GB", "US", "ZW"):
        assert code in KNOWN_ISO_CODES
    assert "XX" not in KNOWN_ISO_CODES


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
    assert normalize_location(normalize_location("Berlin • NYC")) == normalize_location(
        "Berlin • NYC"
    )
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
    assert resolve_location("Atlantis", tz_offset=5.75) == GeoResult("NP", "weak", "Atlantis")
    assert resolve_location("Atlantis", tz_offset=3.5) == GeoResult("IR", "weak", "Atlantis")


def test_weak_timezone_ambiguous_band_never_guesses():
    assert resolve_location("Atlantis", tz_offset=-8.0).confidence == "unmatched"
    assert resolve_location("Atlantis", tz_offset=9.0).confidence == "unmatched"
    assert resolve_location("Atlantis", tz_offset=5.5).confidence == "unmatched"
    assert resolve_location("Atlantis", tz_offset=6.5).confidence == "unmatched"


def test_complete_timezone_bands_never_guess():
    assert resolve_location("Atlantis", tz_offset=12.0).confidence == "unmatched"
    assert resolve_location("Atlantis", tz_offset=-10.0).confidence == "unmatched"
    assert resolve_location("Atlantis", tz_offset=-9.0).confidence == "unmatched"


def test_non_finite_timezone_never_raises():
    for value in (float("inf"), float("-inf"), float("nan"), "inf", "-inf", "not-a-number"):
        result = resolve_location("Atlantis", tz_offset=value)
        assert result == GeoResult(None, "unmatched", "Atlantis")


def test_huge_finite_timezone_never_raises():
    for value in (1e308, 1e308 * 4, Fraction(10**400), Fraction(10**400, 3)):
        result = resolve_location("Atlantis", tz_offset=value)
        assert result == GeoResult(None, "unmatched", "Atlantis")


def test_nfd_input_matches_nfc_gazetteer_and_aliases():
    assert normalize_location("Zu\u0308rich") == "zürich"
    assert resolve_location("Zu\u0308rich") == GeoResult("CH", "gazetteer-city", "Zu\u0308rich")
    assert resolve_location("Tu\u0308rkiye") == GeoResult("TR", "name", "Tu\u0308rkiye")


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


def test_geo_cache_round_trips_none_raw_location(clean: Engine):
    cache = GeoCache(clean)
    cache.put("", GeoResult(None, "unmatched", None))
    assert cache.get("") == GeoResult(None, "unmatched", None)
    with clean.connect() as connection:
        raw = connection.execute(
            sa.text("SELECT raw_sample FROM geo_cache WHERE normalized = ''")
        ).scalar_one()
    assert raw is None


def test_resolve_owner_round_trips_none_raw_location(clean: Engine):
    first = resolve_owner(clean, None)
    assert first == GeoResult(None, "unmatched", None)
    assert resolve_owner(clean, None) == first


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


def test_geo_cache_bulk_roundtrip(clean: Engine):
    from enrich.geo_resolver import GeoCache, GeoResult

    cache = GeoCache(clean)
    cache.put_many(
        {"london": GeoResult("GB", "name", "London"), "paris": GeoResult("FR", "name", "Paris")}
    )
    found = cache.get_many(["london", "paris", "missing"])
    assert found["london"].country_iso == "GB"
    assert found["paris"].country_iso == "FR"
    assert "missing" not in found
    cache.put_many({"london": GeoResult("GB", "name", "London")})
    with clean.connect() as connection:
        hits = connection.scalar(text("SELECT hits FROM geo_cache WHERE normalized = 'london'"))
    assert hits == 2


@pytest.fixture()
def fresh_memo():
    from enrich import geo_resolver

    geo_resolver._MEMO.clear()
    yield
    geo_resolver._MEMO.clear()


def test_resolve_many_keys_by_normalized_location(clean: Engine, fresh_memo):
    from enrich.geo_resolver import resolve_many

    results = resolve_many(clean, ["Lagos", "Lagos", "  lagos!!  ", None])
    assert set(results) == {"lagos", ""}
    assert results["lagos"] == GeoResult("NG", "gazetteer-city", "Lagos")
    assert results[""] == GeoResult(None, "unmatched", None)
    with clean.connect() as connection:
        stored = (
            connection.execute(sa.text("SELECT normalized FROM geo_cache ORDER BY normalized"))
            .scalars()
            .all()
        )
    assert stored == ["", "lagos"]


def test_resolve_many_memoizes_and_bounds_the_process_cache(clean: Engine, monkeypatch, fresh_memo):
    from enrich import geo_resolver

    calls: list[str | None] = []
    real_resolve = geo_resolver.resolve_location

    def spy(raw, **kwargs):
        calls.append(raw)
        return real_resolve(raw, **kwargs)

    monkeypatch.setattr(geo_resolver, "resolve_location", spy)
    first = geo_resolver.resolve_many(clean, ["Atlantis"])
    assert calls == ["Atlantis"]
    with clean.begin() as connection:
        connection.execute(sa.text("DELETE FROM geo_cache WHERE normalized = 'atlantis'"))
    assert geo_resolver.resolve_many(clean, ["Atlantis"]) == first
    assert calls == ["Atlantis"]

    monkeypatch.setattr(geo_resolver, "_MEMO_MAX", 2)
    geo_resolver._MEMO.clear()
    for raw in ("Atlantis", "Xanadu", "El Dorado", "Shangri-La", "Camelot"):
        geo_resolver.resolve_many(clean, [raw])
        assert len(geo_resolver._MEMO) <= 2


def test_geo_cache_put_many_chunks_statements(clean: Engine):
    statements: list[str] = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if "INTO GEO_CACHE" in statement.upper():
            statements.append(statement)

    cache = GeoCache(clean)
    results = {
        f"city-{index}": GeoResult("US", "gazetteer-city", f"City {index}") for index in range(5)
    }
    sa.event.listen(clean, "before_cursor_execute", listener)
    try:
        cache.put_many(results, batch_size=2)
    finally:
        sa.event.remove(clean, "before_cursor_execute", listener)

    assert len(statements) == 3
    found = cache.get_many(list(results))
    assert {key: result.country_iso for key, result in found.items()} == {
        key: "US" for key in results
    }
