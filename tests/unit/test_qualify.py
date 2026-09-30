import dataclasses

import pytest

from lib.qualify import (
    ALLOWED_QUALIFIERS,
    TYPO_HINTS,
    ValidationResult,
    delta_narrows,
    tokenize,
    validate,
)


def test_allowed_qualifiers_are_the_exact_six_section_two_set():
    assert isinstance(ALLOWED_QUALIFIERS, frozenset)
    assert ALLOWED_QUALIFIERS == frozenset(
        {
            "in",
            "repo",
            "user",
            "org",
            "size",
            "followers",
            "forks",
            "stars",
            "created",
            "pushed",
            "language",
            "topic",
            "topics",
            "license",
            "is",
            "mirror",
            "template",
            "archived",
            "good-first-issues",
            "help-wanted-issues",
            "has",
            "props",
            "fork",
            "deployable",
            "deployed",
        }
    )


def test_typo_hints_are_the_exact_mapping():
    assert TYPO_HINTS == {
        "updated": "use `pushed:` + sort=updated",
        "push": "use `pushed:`",
        "is:archive": "use `archived:true`",
        "is:fork": "use `fork:true`",
        "is:forks": "use `fork:true`",
        "is:sponsor": "use `is:sponsorable`",
        "has:funding": "use `has:funding-file`",
        "is:mirror": "use `mirror:`",
        "is:template": "use `template:`",
    }


def test_tokenize_keeps_quoted_span_whole():
    assert tokenize('"machine learning" language:python') == [
        '"machine learning"',
        "language:python",
    ]


def test_tokenize_handles_empty_and_repeated_whitespace():
    assert tokenize("") == []
    assert tokenize("   ") == []
    assert tokenize("a   b") == ["a", "b"]


def test_tokenize_multiple_quoted_spans():
    assert tokenize('"a b" "c d"') == ['"a b"', '"c d"']


def test_valid_query_passes():
    query = (
        "org:github language:python stars:>=500 pushed:>2024-01-01 "
        'archived:false -fork:true "machine learning"'
    )
    result = validate(query)
    assert result.ok is True
    assert result.errors == ()
    assert result.hints == ()


def test_empty_query_passes():
    assert validate("").ok is True
    assert validate("   ").ok is True


def test_qualifier_names_are_case_insensitive():
    assert validate("STARS:>=500 Language:Python").ok is True


def test_exclusion_prefix_is_allowed():
    assert validate("-org:github -stars:>=5").ok is True


def test_empty_value_is_an_error():
    result = validate("language:")
    assert result.ok is False
    assert any("language:" in error for error in result.errors)


def test_hyphenated_names_are_single_names():
    assert validate("good-first-issues:>2 help-wanted-issues:>1").ok is True


def test_unknown_qualifier_errors_with_token_name():
    result = validate("has_dockerfile:true")
    assert result.ok is False
    assert any("has_dockerfile" in error for error in result.errors)


def test_unknown_qualifier_without_hint_has_no_hints():
    result = validate("has_dockerfile:true")
    assert result.hints == ()


def test_updated_typo_hint():
    result = validate("updated:>2024-01-01")
    assert result.ok is False
    assert result.hints == ("use `pushed:` + sort=updated",)


def test_push_typo_hint():
    result = validate("push:>2024-01-01")
    assert result.hints == ("use `pushed:`",)


def test_is_archive_typo_hint():
    result = validate("is:archive")
    assert result.ok is False
    assert result.hints == ("use `archived:true`",)


def test_is_fork_typo_hint():
    result = validate("is:fork")
    assert result.hints == ("use `fork:true`",)


def test_is_forks_typo_hint():
    result = validate("is:forks:true")
    assert result.hints == ("use `fork:true`",)


def test_is_sponsor_typo_hint():
    result = validate("is:sponsor")
    assert result.hints == ("use `is:sponsorable`",)


def test_has_funding_typo_hint():
    result = validate("has:funding")
    assert result.hints == ("use `has:funding-file`",)


def test_is_mirror_typo_hint():
    result = validate("is:mirror")
    assert result.hints == ("use `mirror:`",)


def test_is_template_typo_hint():
    result = validate("is:template")
    assert result.hints == ("use `template:`",)


def test_props_requires_a_single_org_scope():
    result = validate("props.environment:production")
    assert result.ok is False
    assert "add a single org: scope" in result.hints


def test_props_allowed_with_single_org():
    assert validate("org:github props.environment:production").ok is True


def test_props_rejected_with_two_distinct_orgs():
    result = validate("org:github org:microsoft props.environment:production")
    assert result.ok is False
    assert "add a single org: scope" in result.hints


def test_props_allowed_with_repeated_same_org():
    assert validate("org:github org:github props.environment:production").ok is True


def test_props_rejected_when_org_is_only_excluded():
    result = validate("props.environment:production -org:github")
    assert result.ok is False
    assert "add a single org: scope" in result.hints


def test_props_allowed_with_one_positive_org_and_an_exclusion():
    assert validate("org:github -org:microsoft props.environment:production").ok is True


def test_props_without_dot_is_invalid():
    result = validate("org:github props:production")
    assert result.ok is False
    assert any("props" in error for error in result.errors)


def test_props_with_empty_name_is_invalid():
    result = validate("org:github props.:production")
    assert result.ok is False


def test_props_with_empty_value_is_invalid():
    result = validate("org:github props.environment:")
    assert result.ok is False


def test_quoted_span_is_never_parsed_as_a_qualifier():
    assert validate('"props.environment:production"').ok is True
    assert validate('"updated:>2024-01-01"').ok is True


def test_keyword_length_allows_256_characters():
    assert validate("a" * 256).ok is True


def test_keyword_length_rejects_257_characters():
    result = validate("a" * 257)
    assert result.ok is False
    assert any("256" in error for error in result.errors)


def test_keyword_length_sums_all_keyword_tokens():
    result = validate(("a" * 128) + " " + ("b" * 129))
    assert result.ok is False


def test_qualifier_text_does_not_count_toward_keyword_length():
    assert validate("language:" + "a" * 300).ok is True


def test_quoted_keyword_length_strips_quotes():
    assert validate('"' + ("a" * 256) + '"').ok is True
    assert validate('"' + ("a" * 257) + '"').ok is False


def test_five_operators_are_allowed():
    assert validate("a AND b AND c OR d OR e NOT f").ok is True


def test_six_operators_are_rejected():
    result = validate("a AND b AND c OR d OR e NOT f NOT g")
    assert result.ok is False
    assert any("AND/OR/NOT" in error for error in result.errors)


def test_lowercase_operators_are_keywords():
    assert validate("a and b or c not d").ok is True


def test_delta_narrows_requires_a_strictly_smaller_candidate():
    assert delta_narrows(1000, 999) is True
    assert delta_narrows(1000, 1000) is False
    assert delta_narrows(1000, 1001) is False


def test_validation_result_is_frozen():
    result = validate("")
    assert result == ValidationResult(True, (), ())
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.ok = False
