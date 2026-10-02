from __future__ import annotations

from serve.filter_spec import describe_spec, parse_filter_spec


def make_spec(q: str = "", virtual: dict | None = None):
    document: dict = {"gitcrawl_filter": 1, "q": q}
    if virtual:
        document["virtual"] = virtual
    return parse_filter_spec(document)


def test_describe_spec_of_an_empty_spec_is_everything():
    assert describe_spec(make_spec()) == "Everything"


def test_describe_spec_renders_query_filters_in_plain_words():
    described = describe_spec(make_spec("language:rust stars:>=100 pushed:>=2025-01-01 fork:false"))

    assert described == "Rust · 100+ stars · updated since 2025-01-01 · no forks"
    assert not described.startswith(" · ") and not described.endswith(" · ")


def test_describe_spec_appends_virtual_filters_deterministically():
    described = describe_spec(
        make_spec(
            "topic:rust license:mit stars:10..200 org:github",
            {"min_commits": 100, "has_dockerfile": True, "owner_country": "DE"},
        )
    )

    assert described == (
        "topic rust · license mit · 10–200 stars · org github · "
        "has Dockerfile · owner country DE · at least 100 commits"
    )
