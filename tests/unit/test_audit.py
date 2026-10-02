from __future__ import annotations

import dataclasses
import hashlib
from datetime import UTC, datetime

import httpx
import pytest

from lib.audit import AuditRecord, query_hash, record_from_response

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

FULL_HEADERS = {
    "x-ratelimit-limit": "30",
    "x-ratelimit-remaining": "17",
    "x-ratelimit-reset": "1770000000",
    "x-ratelimit-resource": "search",
    "retry-after": "12",
    "link": (
        '<https://api.github.com/search/repositories?page=2>; rel="next", '
        '<https://api.github.com/search/repositories?page=5>; rel="last"'
    ),
}


def _response(status: int = 200, headers: dict[str, str] | None = None, **kwargs) -> httpx.Response:
    headers = headers or {}
    if "json" in kwargs:
        return httpx.Response(status, headers=headers, json=kwargs["json"])
    return httpx.Response(status, headers=headers, text=kwargs.get("text", ""))


def test_query_hash_matches_sha256_of_canonical_json():
    params = {"q": "org:github", "per_page": 100, "page": 2}
    canonical = b'{"page":2,"per_page":100,"q":"org:github"}'
    assert query_hash(params) == hashlib.sha256(canonical).hexdigest()


def test_query_hash_is_order_insensitive_at_the_top_level():
    first = {"q": "ai", "sort": "stars", "order": "desc"}
    second = {"order": "desc", "sort": "stars", "q": "ai"}
    assert query_hash(first) == query_hash(second)


def test_query_hash_is_order_insensitive_for_nested_mappings():
    first = {"q": "ai", "props": {"a": 1, "b": 2}}
    second = {"props": {"b": 2, "a": 1}, "q": "ai"}
    assert query_hash(first) == query_hash(second)


def test_query_hash_changes_when_params_change():
    assert query_hash({"q": "ai"}) != query_hash({"q": "ml"})


def test_query_hash_serializes_none_as_null():
    assert query_hash({"q": None}) == hashlib.sha256(b'{"q":null}').hexdigest()


def test_record_from_response_parses_full_headers_and_body():
    response = _response(200, FULL_HEADERS, json={"total_count": 1234, "incomplete_results": True})
    record = record_from_response(
        {"q": "ai"},
        response,
        token_fp="fp",
        latency_ms=250,
        etag_sent='W/"abc"',
        now=NOW,
    )
    assert record == AuditRecord(
        ts=NOW,
        query_hash=query_hash({"q": "ai"}),
        params={"q": "ai"},
        etag_sent='W/"abc"',
        status=200,
        rl_limit=30,
        rl_remaining=17,
        rl_reset=1770000000,
        rl_resource="search",
        retry_after=12,
        link_next=True,
        total_count=1234,
        incomplete_results=True,
        token_fp="fp",
        latency_ms=250,
    )


def test_record_from_response_defaults_timestamp_to_utc_now():
    before = datetime.now(UTC)
    record = record_from_response({}, _response(204), token_fp="fp", latency_ms=1)
    after = datetime.now(UTC)
    assert before <= record.ts <= after
    assert record.ts.tzinfo is UTC


def test_record_from_response_absent_headers_and_body_yield_none_fields():
    record = record_from_response({}, _response(200), token_fp="fp", latency_ms=5, now=NOW)
    assert record.etag_sent is None
    assert record.rl_limit is None
    assert record.rl_remaining is None
    assert record.rl_reset is None
    assert record.rl_resource is None
    assert record.retry_after is None
    assert record.link_next is False
    assert record.total_count is None
    assert record.incomplete_results is None
    assert record.status == 200
    assert record.token_fp == "fp"
    assert record.latency_ms == 5


def test_record_from_response_malformed_numeric_headers_become_none():
    headers = {
        "x-ratelimit-limit": "lots",
        "x-ratelimit-remaining": "",
        "x-ratelimit-reset": "n/a",
        "retry-after": "soon",
    }
    record = record_from_response({}, _response(200, headers), token_fp="fp", latency_ms=1, now=NOW)
    assert record.rl_limit is None
    assert record.rl_remaining is None
    assert record.rl_reset is None
    assert record.retry_after is None


def test_record_from_response_malformed_body_is_tolerated():
    record = record_from_response(
        {}, _response(200, text="<html>nope</html>"), token_fp="fp", latency_ms=1, now=NOW
    )
    assert record.total_count is None
    assert record.incomplete_results is None


def test_record_from_response_non_object_body_is_tolerated():
    record = record_from_response(
        {}, _response(200, json=[1, 2, 3]), token_fp="fp", latency_ms=1, now=NOW
    )
    assert record.total_count is None
    assert record.incomplete_results is None


def test_record_from_response_non_numeric_counters_become_none():
    body = {"total_count": "many", "incomplete_results": "yes"}
    record = record_from_response(
        {}, _response(200, json=body), token_fp="fp", latency_ms=1, now=NOW
    )
    assert record.total_count is None
    assert record.incomplete_results is None


def test_record_from_response_link_without_next_is_false():
    headers = {"link": '<https://api.github.com/x?page=5>; rel="last"'}
    record = record_from_response({}, _response(200, headers), token_fp="fp", latency_ms=1, now=NOW)
    assert record.link_next is False


def test_record_from_response_link_rel_is_case_insensitive():
    headers = {"link": '<https://api.github.com/x?page=2>; rel="Next"'}
    record = record_from_response({}, _response(200, headers), token_fp="fp", latency_ms=1, now=NOW)
    assert record.link_next is True


def test_record_from_response_header_lookup_is_case_insensitive():
    headers = {"X-RateLimit-Limit": "30", "Retry-After": "4"}
    record = record_from_response({}, _response(200, headers), token_fp="fp", latency_ms=1, now=NOW)
    assert record.rl_limit == 30
    assert record.retry_after == 4


def test_record_from_response_not_modified_has_no_body_fields():
    record = record_from_response({}, _response(304), token_fp="fp", latency_ms=3, now=NOW)
    assert record.status == 304
    assert record.total_count is None
    assert record.incomplete_results is None


def test_audit_record_is_frozen():
    record = record_from_response({}, _response(200), token_fp="fp", latency_ms=1, now=NOW)
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.status = 500


def test_record_from_response_caches_parsed_body():
    from lib.audit import cached_json

    response = _response(200, json={"total_count": 7, "incomplete_results": False})
    body = response.json()
    # emulate the hook having cached it
    response.extensions["gitcrawl.json"] = body
    assert cached_json(response) is body


def test_record_from_response_parses_and_caches_the_body():
    from lib.audit import cached_json

    body = {"total_count": 7, "incomplete_results": False}
    response = _response(200, json=body)
    record = record_from_response({}, response, token_fp="fp", latency_ms=1, now=NOW)
    assert record.total_count == 7
    assert cached_json(response) == body


def test_record_from_response_caches_a_malformed_body_as_none():
    from lib.audit import cached_json

    response = _response(200, text="<html>nope</html>")
    record = record_from_response({}, response, token_fp="fp", latency_ms=1, now=NOW)
    assert record.total_count is None
    assert cached_json(response) is None


def test_record_from_response_accepts_preparsed_body():
    from lib.audit import cached_json, record_from_response

    body = {"total_count": 9}
    response = _response(200)
    record = record_from_response({}, response, token_fp="fp", latency_ms=1, now=NOW, body=body)
    assert record.total_count == 9
    assert cached_json(response) is body
