from __future__ import annotations

import copy
import dataclasses
import hashlib
import json

import pytest

from serve.filter_spec import (
    ALLOWED_TOP_LEVEL,
    FILTER_SPEC_VERSION,
    FilterSpec,
    FilterSpecError,
    parse_filter_spec,
    spec_hash,
    spec_to_dict,
    spec_to_query,
)
from serve.virtual_params import (
    GEO_CONFIDENCE_ORDER,
    VIRTUAL_FILTERS,
    VirtualFilterError,
    translate_virtuals,
    validate_virtual,
)

FRAME = {
    "buckets": {"frozen_list_date": "2025-08-29", "new_cutoff": "2025-08-29"},
    "attrition": "count",
    "size_splits": ["small", "medium", "large"],
    "study_window": {"from": "2025-01-01", "to": None},
    "star_floor": 10,
}


def base(**overrides):
    doc = {"gitcrawl_filter": 1}
    doc.update(overrides)
    return doc


def test_allowed_top_level_is_the_documented_allowlist():
    assert ALLOWED_TOP_LEVEL == {
        "gitcrawl_filter",
        "q",
        "sort",
        "order",
        "virtual",
        "page",
        "frame",
        "as_of",
    }


def test_virtual_table_has_exactly_the_nine_virtuals():
    assert set(VIRTUAL_FILTERS) == {
        "min_stars",
        "team_topic",
        "has_dockerfile",
        "owner_country",
        "min_geo_confidence",
        "min_commits",
        "max_commits",
        "min_loc",
        "max_loc",
    }


def test_virtual_table_declares_the_documented_kinds():
    assert {name: rule.kind for name, rule in VIRTUAL_FILTERS.items()} == {
        "min_stars": "int",
        "team_topic": "topic",
        "has_dockerfile": "bool",
        "owner_country": "iso2",
        "min_geo_confidence": "enum",
        "min_commits": "int",
        "max_commits": "int",
        "min_loc": "int",
        "max_loc": "int",
    }


def test_rule_name_matches_its_table_key_and_has_a_post_description():
    for name, rule in VIRTUAL_FILTERS.items():
        assert rule.name == name
        assert rule.post


def test_only_min_stars_and_team_topic_translate_to_query_fragments():
    translating = {name for name, rule in VIRTUAL_FILTERS.items() if rule.to_query is not None}
    assert translating == {"min_stars", "team_topic"}


def test_geo_confidence_order_is_strongest_to_weakest():
    assert GEO_CONFIDENCE_ORDER == (
        "exact-iso",
        "name",
        "gazetteer-city",
        "geocoder",
        "weak",
    )


def test_geo_confidence_order_supports_threshold_comparisons():
    assert GEO_CONFIDENCE_ORDER.index("exact-iso") < GEO_CONFIDENCE_ORDER.index("name")
    assert GEO_CONFIDENCE_ORDER.index("name") < GEO_CONFIDENCE_ORDER.index("gazetteer-city")
    assert GEO_CONFIDENCE_ORDER.index("gazetteer-city") < GEO_CONFIDENCE_ORDER.index("geocoder")
    assert GEO_CONFIDENCE_ORDER.index("geocoder") < GEO_CONFIDENCE_ORDER.index("weak")


def test_min_geo_confidence_allows_the_order_and_defaults_to_gazetteer_city():
    rule = VIRTUAL_FILTERS["min_geo_confidence"]
    assert rule.allowed == GEO_CONFIDENCE_ORDER
    assert rule.default == "gazetteer-city"
    assert "unmatched" not in rule.allowed


def test_translate_virtuals_is_empty_without_translatable_values():
    assert translate_virtuals({}) == []
    assert translate_virtuals({"has_dockerfile": True, "owner_country": "DE"}) == []
    assert translate_virtuals({"min_stars": None, "team_topic": None}) == []


def test_translate_virtuals_min_stars_alone():
    assert translate_virtuals({"min_stars": 10}) == ["stars:>=10"]


def test_translate_virtuals_team_topic_alone():
    assert translate_virtuals({"team_topic": "rust"}) == ["topic:rust"]


def test_translate_virtuals_both_follow_table_order_not_input_order():
    assert translate_virtuals({"team_topic": "rust", "min_stars": 10}) == [
        "stars:>=10",
        "topic:rust",
    ]


def test_translate_virtuals_normalizes_values():
    assert translate_virtuals({"team_topic": "Machine-Learning"}) == ["topic:machine-learning"]


@pytest.mark.parametrize("name", ["min_stars", "min_commits", "max_commits", "min_loc", "max_loc"])
def test_int_virtuals_accept_zero_and_positive_ints(name):
    assert validate_virtual(name, 0) == 0
    assert validate_virtual(name, 42) == 42


@pytest.mark.parametrize("name", ["min_stars", "min_commits", "max_commits", "min_loc", "max_loc"])
@pytest.mark.parametrize("value", [-1, 1.5, "5", True, False, [], {}])
def test_int_virtuals_reject_non_nonnegative_ints(name, value):
    with pytest.raises(VirtualFilterError) as excinfo:
        validate_virtual(name, value)
    assert excinfo.value.name == name
    assert excinfo.value.hint


def test_has_dockerfile_accepts_bools():
    assert validate_virtual("has_dockerfile", True) is True
    assert validate_virtual("has_dockerfile", False) is False


@pytest.mark.parametrize("value", ["true", "false", 1, 0, "yes"])
def test_has_dockerfile_rejects_non_bools(value):
    with pytest.raises(VirtualFilterError) as excinfo:
        validate_virtual("has_dockerfile", value)
    assert excinfo.value.name == "has_dockerfile"
    assert excinfo.value.hint


def test_team_topic_is_lowercased():
    assert validate_virtual("team_topic", "Machine-Learning") == "machine-learning"


def test_team_topic_charset_bounds():
    assert validate_virtual("team_topic", "a") == "a"
    assert validate_virtual("team_topic", "rust-") == "rust-"
    assert validate_virtual("team_topic", "a" * 50) == "a" * 50


@pytest.mark.parametrize("value", ["", "-rust", "rust lang", "rust!", "a" * 51, 5])
def test_team_topic_rejects_values_outside_the_charset(value):
    with pytest.raises(VirtualFilterError) as excinfo:
        validate_virtual("team_topic", value)
    assert excinfo.value.name == "team_topic"
    assert excinfo.value.hint


def test_owner_country_uppercases_and_validates_against_known_iso_codes():
    assert validate_virtual("owner_country", "de") == "DE"
    assert validate_virtual("owner_country", "DE") == "DE"
    assert validate_virtual("owner_country", " de ") == "DE"


@pytest.mark.parametrize("value", ["", "D", "DEN", "D1", "XX", "ZZ", 1, "not-a-country"])
def test_owner_country_rejects_unknown_or_malformed_codes(value):
    with pytest.raises(VirtualFilterError) as excinfo:
        validate_virtual("owner_country", value)
    assert excinfo.value.name == "owner_country"
    assert excinfo.value.hint


@pytest.mark.parametrize(
    "name",
    [
        "min_stars",
        "team_topic",
        "has_dockerfile",
        "owner_country",
        "min_commits",
        "max_commits",
        "min_loc",
        "max_loc",
    ],
)
def test_null_virtual_values_mean_unset(name):
    assert validate_virtual(name, None) is None


def test_min_geo_confidence_accepts_each_tier_and_defaults_to_gazetteer_city():
    for value in GEO_CONFIDENCE_ORDER:
        assert validate_virtual("min_geo_confidence", value) == value
    assert validate_virtual("min_geo_confidence", None) == "gazetteer-city"


def test_min_geo_confidence_rejects_unknown_tiers():
    with pytest.raises(VirtualFilterError) as excinfo:
        validate_virtual("min_geo_confidence", "sometimes")
    assert "exact-iso" in excinfo.value.hint


def test_validate_virtual_unknown_name_raises_with_a_hint():
    with pytest.raises(VirtualFilterError) as excinfo:
        validate_virtual("min_star", 5)
    assert excinfo.value.name == "min_star"
    assert "min_stars" in excinfo.value.hint


def test_virtual_filter_error_is_a_value_error():
    assert issubclass(VirtualFilterError, ValueError)


def test_filter_spec_version_constant():
    assert FILTER_SPEC_VERSION == 1


def test_minimal_spec_materializes_defaults():
    doc = base()
    spec = parse_filter_spec(doc)
    assert isinstance(spec, FilterSpec)
    assert spec.q == ""
    assert spec.sort is None
    assert spec.order is None
    assert spec.virtual == {}
    assert spec.per_page == 20
    assert spec.max_pages == 3
    assert spec.frame is None
    assert spec.as_of is None
    assert spec.raw == doc


def test_filter_spec_is_frozen():
    spec = parse_filter_spec(base())
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.q = "language:rust"


def test_missing_version_is_an_error_with_a_hint():
    with pytest.raises(FilterSpecError) as excinfo:
        parse_filter_spec({})
    assert any("gitcrawl_filter" in error for error in excinfo.value.errors)
    assert excinfo.value.hints


@pytest.mark.parametrize("value", [2, 0, "1", 1.0, True, None])
def test_wrong_version_is_an_error_with_a_hint(value):
    with pytest.raises(FilterSpecError) as excinfo:
        parse_filter_spec(base(gitcrawl_filter=value))
    assert any("gitcrawl_filter" in error for error in excinfo.value.errors)
    assert excinfo.value.hints


def test_every_unknown_top_level_key_is_named():
    with pytest.raises(FilterSpecError) as excinfo:
        parse_filter_spec(base(bogus=1, junk=2))
    assert any("bogus" in error for error in excinfo.value.errors)
    assert any("junk" in error for error in excinfo.value.errors)
    assert excinfo.value.hints


@pytest.mark.parametrize("key", ["token", "authorization", "github_token"])
def test_token_and_state_keys_are_rejected(key):
    with pytest.raises(FilterSpecError) as excinfo:
        parse_filter_spec(base(**{key: "secret-value"}))
    assert any(key in error for error in excinfo.value.errors)
    assert any("token" in hint or "state" in hint for hint in excinfo.value.hints)


def test_q_typo_hint_is_preserved_from_qualify():
    with pytest.raises(FilterSpecError) as excinfo:
        parse_filter_spec(base(q="updated:>2024-01-01"))
    assert any("updated" in error for error in excinfo.value.errors)
    assert "use `pushed:` + sort=updated" in excinfo.value.hints


def test_q_unknown_qualifier_is_an_error():
    with pytest.raises(FilterSpecError) as excinfo:
        parse_filter_spec(base(q="has_dockerfile:true"))
    assert any("has_dockerfile" in error for error in excinfo.value.errors)


def test_props_gating_is_delegated_to_qualify():
    with pytest.raises(FilterSpecError) as excinfo:
        parse_filter_spec(base(q="props.environment:production"))
    assert any("props.environment:production" in error for error in excinfo.value.errors)
    assert "add a single org: scope" in excinfo.value.hints


def test_props_with_a_single_org_is_accepted():
    spec = parse_filter_spec(base(q="org:github props.environment:production"))
    assert spec.q == "org:github props.environment:production"


def test_q_must_be_a_string():
    with pytest.raises(FilterSpecError) as excinfo:
        parse_filter_spec(base(q=7))
    assert any("q" in error for error in excinfo.value.errors)
    assert excinfo.value.hints


@pytest.mark.parametrize("value", ["stars", "forks", "help-wanted-issues", "updated"])
def test_sort_enum_values_are_accepted(value):
    assert parse_filter_spec(base(sort=value)).sort == value


@pytest.mark.parametrize("value", ["watchers", "best-match", "", 5])
def test_bad_sort_is_an_error_with_a_hint(value):
    with pytest.raises(FilterSpecError) as excinfo:
        parse_filter_spec(base(sort=value))
    assert any("sort" in error for error in excinfo.value.errors)
    assert excinfo.value.hints


@pytest.mark.parametrize("value", ["desc", "asc"])
def test_order_enum_values_are_accepted(value):
    assert parse_filter_spec(base(sort="stars", order=value)).order == value


@pytest.mark.parametrize("value", ["descending", "", 1])
def test_bad_order_is_an_error_with_a_hint(value):
    with pytest.raises(FilterSpecError) as excinfo:
        parse_filter_spec(base(order=value))
    assert any("order" in error for error in excinfo.value.errors)
    assert excinfo.value.hints


def test_order_without_sort_is_accepted_and_recorded():
    spec = parse_filter_spec(base(order="asc"))
    assert spec.sort is None
    assert spec.order == "asc"


def test_virtual_values_are_normalized_in_the_spec():
    spec = parse_filter_spec(
        base(
            virtual={
                "min_stars": 0,
                "team_topic": "Rust",
                "has_dockerfile": False,
                "min_commits": 100,
                "min_loc": 5000,
            }
        )
    )
    assert spec.virtual == {
        "min_stars": 0,
        "team_topic": "rust",
        "has_dockerfile": False,
        "min_commits": 100,
        "min_loc": 5000,
    }


def test_unknown_virtual_name_is_an_error_with_a_hint():
    with pytest.raises(FilterSpecError) as excinfo:
        parse_filter_spec(base(virtual={"min_star": 5}))
    assert any("min_star" in error for error in excinfo.value.errors)
    assert any("min_stars" in hint for hint in excinfo.value.hints)


def test_invalid_virtual_value_is_an_error_with_the_virtual_hint():
    with pytest.raises(FilterSpecError) as excinfo:
        parse_filter_spec(base(virtual={"min_stars": -1}))
    assert any("min_stars" in error for error in excinfo.value.errors)
    assert any("non-negative" in hint for hint in excinfo.value.hints)


def test_virtual_must_be_an_object():
    with pytest.raises(FilterSpecError) as excinfo:
        parse_filter_spec(base(virtual=[1, 2]))
    assert any("virtual" in error for error in excinfo.value.errors)
    assert excinfo.value.hints


def test_owner_country_null_is_recorded_as_unset():
    spec = parse_filter_spec(
        base(
            virtual={
                "min_commits": 100,
                "min_loc": 5000,
                "has_dockerfile": False,
                "owner_country": None,
            }
        )
    )
    assert spec.virtual == {
        "has_dockerfile": False,
        "owner_country": None,
        "min_commits": 100,
        "min_loc": 5000,
    }


def test_min_geo_confidence_default_is_recorded_with_owner_country():
    spec = parse_filter_spec(base(virtual={"owner_country": "de"}))
    assert spec.virtual == {
        "owner_country": "DE",
        "min_geo_confidence": "gazetteer-city",
    }


def test_explicit_min_geo_confidence_is_kept():
    spec = parse_filter_spec(base(virtual={"owner_country": "DE", "min_geo_confidence": "name"}))
    assert spec.virtual == {"owner_country": "DE", "min_geo_confidence": "name"}


def test_min_geo_confidence_default_is_not_added_without_owner_country():
    spec = parse_filter_spec(base(virtual={"min_stars": 5}))
    assert spec.virtual == {"min_stars": 5}


def test_page_bounds_accept_the_edges():
    spec = parse_filter_spec(base(page={"per_page": 1, "max_pages": 1}))
    assert (spec.per_page, spec.max_pages) == (1, 1)
    spec = parse_filter_spec(base(page={"per_page": 100, "max_pages": 10}))
    assert (spec.per_page, spec.max_pages) == (100, 10)


def test_page_partial_overrides_keep_the_other_default():
    spec = parse_filter_spec(base(page={"per_page": 50}))
    assert (spec.per_page, spec.max_pages) == (50, 3)
    spec = parse_filter_spec(base(page={"max_pages": 5}))
    assert (spec.per_page, spec.max_pages) == (20, 5)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("per_page", 0),
        ("per_page", 101),
        ("per_page", "20"),
        ("per_page", 1.5),
        ("per_page", True),
        ("max_pages", 0),
        ("max_pages", 11),
        ("max_pages", "3"),
        ("max_pages", False),
    ],
)
def test_page_bounds_reject_out_of_range_and_non_int_values(key, value):
    with pytest.raises(FilterSpecError) as excinfo:
        parse_filter_spec(base(page={key: value}))
    assert any(key in error for error in excinfo.value.errors)
    assert excinfo.value.hints


def test_page_must_be_an_object():
    with pytest.raises(FilterSpecError) as excinfo:
        parse_filter_spec(base(page=3))
    assert any("page" in error for error in excinfo.value.errors)


def test_unknown_page_key_is_an_error():
    with pytest.raises(FilterSpecError) as excinfo:
        parse_filter_spec(base(page={"per_page": 10, "page": 2}))
    assert any("page" in error for error in excinfo.value.errors)


def test_frame_is_recorded_verbatim():
    spec = parse_filter_spec(base(frame=FRAME))
    assert spec.frame == FRAME
    assert spec_to_dict(spec)["frame"] == FRAME


def test_frame_must_be_an_object():
    with pytest.raises(FilterSpecError) as excinfo:
        parse_filter_spec(base(frame=["buckets"]))
    assert any("frame" in error for error in excinfo.value.errors)
    assert excinfo.value.hints


@pytest.mark.parametrize(
    "value",
    ["2025-01-01", "2025-01-01T12:30:00Z", "2025-01-01T12:30:00+02:00"],
)
def test_as_of_accepts_iso_dates_and_datetimes(value):
    assert parse_filter_spec(base(as_of=value)).as_of == value


@pytest.mark.parametrize("value", ["", "not-a-date", "2025-13-01", 5, ["2025-01-01"]])
def test_as_of_rejects_non_iso_values(value):
    with pytest.raises(FilterSpecError) as excinfo:
        parse_filter_spec(base(as_of=value))
    assert any("as_of" in error for error in excinfo.value.errors)
    assert excinfo.value.hints


def test_spec_to_query_of_an_empty_spec_is_empty():
    assert spec_to_query(parse_filter_spec(base())) == ""


def test_spec_to_query_collapses_whitespace():
    spec = parse_filter_spec(base(q="  language:rust   fork:false  "))
    assert spec_to_query(spec) == "language:rust fork:false"


def test_spec_to_query_appends_translated_virtuals_in_table_order():
    spec = parse_filter_spec(
        base(q="language:rust", virtual={"team_topic": "rust", "min_stars": 10})
    )
    assert spec_to_query(spec) == "language:rust stars:>=10 topic:rust"


def test_spec_to_query_virtuals_only_has_no_leading_space():
    spec = parse_filter_spec(base(virtual={"min_stars": 10}))
    assert spec_to_query(spec) == "stars:>=10"


def test_spec_to_query_ignores_post_filter_only_virtuals():
    spec = parse_filter_spec(
        base(
            q="language:rust",
            virtual={"has_dockerfile": True, "owner_country": "DE", "min_geo_confidence": "name"},
        )
    )
    assert spec_to_query(spec) == "language:rust"


def test_spec_to_dict_is_normalized_in_stable_order():
    spec = parse_filter_spec(
        base(
            q="language:rust",
            sort="stars",
            order="desc",
            virtual={"min_stars": 10},
            frame={"star_floor": 10},
            as_of="2025-01-01",
        )
    )
    normalized = spec_to_dict(spec)
    assert list(normalized) == [
        "gitcrawl_filter",
        "q",
        "sort",
        "order",
        "virtual",
        "page",
        "frame",
        "as_of",
    ]
    assert normalized["gitcrawl_filter"] == 1
    assert normalized["virtual"] == {"min_stars": 10}
    assert normalized["page"] == {"per_page": 20, "max_pages": 3}
    assert normalized["frame"] == {"star_floor": 10}


def test_spec_to_dict_omits_unset_optionals():
    assert list(spec_to_dict(parse_filter_spec(base()))) == ["gitcrawl_filter", "q", "page"]


def test_spec_to_dict_round_trips_through_parse():
    spec = parse_filter_spec(
        base(
            q="language:rust",
            sort="stars",
            virtual={"min_stars": 10},
            frame={"star_floor": 10},
            as_of="2025-01-01",
        )
    )
    reparsed = parse_filter_spec(spec_to_dict(spec))
    assert reparsed.q == spec.q
    assert reparsed.sort == spec.sort
    assert reparsed.order == spec.order
    assert reparsed.virtual == spec.virtual
    assert (reparsed.per_page, reparsed.max_pages) == (spec.per_page, spec.max_pages)
    assert reparsed.frame == spec.frame
    assert reparsed.as_of == spec.as_of


def test_spec_hash_matches_canonical_sha256():
    spec = parse_filter_spec(base(q="language:rust", virtual={"min_stars": 10}))
    canonical = json.dumps(spec_to_dict(spec), sort_keys=True, separators=(",", ":"))
    assert spec_hash(spec) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_spec_hash_is_stable_across_dict_key_order_and_whitespace():
    first = parse_filter_spec(
        base(
            q="language:rust fork:false",
            sort="stars",
            virtual={"min_stars": 10, "team_topic": "rust"},
        )
    )
    second = parse_filter_spec(
        {
            "virtual": {"team_topic": "rust", "min_stars": 10},
            "sort": "stars",
            "q": "  language:rust   fork:false ",
            "gitcrawl_filter": 1,
        }
    )
    assert spec_hash(first) == spec_hash(second)


def test_spec_hash_is_stable_between_explicit_and_default_page():
    first = parse_filter_spec(base(page={"per_page": 20, "max_pages": 3}))
    second = parse_filter_spec(base())
    assert spec_hash(first) == spec_hash(second)


@pytest.mark.parametrize(
    "changed",
    [
        {"q": "language:python"},
        {"sort": "forks"},
        {"order": "asc"},
        {"virtual": {"min_stars": 11}},
        {"virtual": {"team_topic": "rust"}},
        {"page": {"per_page": 21}},
        {"frame": {"star_floor": 10}},
        {"as_of": "2025-01-01"},
    ],
)
def test_spec_hash_changes_when_any_normalized_field_changes(changed):
    base_doc = base(q="language:rust", sort="stars", virtual={"min_stars": 10})
    changed_doc = dict(base_doc)
    changed_doc.update(changed)
    assert spec_hash(parse_filter_spec(base_doc)) != spec_hash(parse_filter_spec(changed_doc))


def test_parse_filter_spec_does_not_mutate_the_input_document():
    doc = base(
        q="language:rust",
        sort="stars",
        order="desc",
        virtual={"min_stars": 10, "team_topic": "Rust"},
        page={"per_page": 50, "max_pages": 4},
        frame={"buckets": {"new_cutoff": "2025-08-29"}},
        as_of="2025-01-01",
    )
    before = copy.deepcopy(doc)
    parse_filter_spec(doc)
    assert doc == before


def test_contract_example_filter_spec_parses():
    doc = {
        "gitcrawl_filter": 1,
        "q": "language:rust fork:false pushed:>2024-09-29",
        "sort": "stars",
        "order": "desc",
        "virtual": {
            "min_commits": 100,
            "min_loc": 5000,
            "has_dockerfile": False,
            "owner_country": None,
        },
        "page": {"per_page": 100, "max_pages": 10},
    }
    spec = parse_filter_spec(doc)
    assert spec.virtual == {
        "has_dockerfile": False,
        "owner_country": None,
        "min_commits": 100,
        "min_loc": 5000,
    }
    assert (spec.per_page, spec.max_pages) == (100, 10)
    assert spec_to_query(spec) == doc["q"]


def test_filter_spec_error_carries_errors_and_hints():
    with pytest.raises(FilterSpecError) as excinfo:
        parse_filter_spec({})
    assert isinstance(excinfo.value.errors, tuple)
    assert isinstance(excinfo.value.hints, tuple)
    assert excinfo.value.errors
    assert isinstance(excinfo.value, ValueError)
