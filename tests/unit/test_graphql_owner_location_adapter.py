from __future__ import annotations

import httpx

from enrich.graphql_owner_location import OwnerLocationAdapter
from lib.graphql_batch import fetch_batch


def test_query_picks_user_or_organization_per_login():
    adapter = OwnerLocationAdapter({"alice": "User", "acme": "Organization"})
    query = adapter.build_query({"n0": "alice", "n1": "acme"})
    assert 'n0: user(login: "alice") { location }' in query
    assert 'n1: organization(login: "acme") { location }' in query


def test_parse_keeps_none_locations_as_present_values():
    adapter = OwnerLocationAdapter({"alice": "User", "bob": "User"})
    payload = {"data": {"n0": {"location": "Berlin"}, "n1": {"location": None}}}
    parsed = adapter.parse(payload, {"n0": "alice", "n1": "bob"})
    assert parsed.values == {"alice": "Berlin", "bob": None}


def test_null_account_is_missing_for_fallback():
    adapter = OwnerLocationAdapter({"ghost": "User"})
    parsed = adapter.parse({"data": {"n0": None}}, {"n0": "ghost"})
    assert parsed.values == {}
    assert parsed.failures == {}


def test_bot_type_uses_user_query():
    adapter = OwnerLocationAdapter({"dependabot[bot]": "Bot"})
    query = adapter.build_query({"n0": "dependabot[bot]"})
    assert 'n0: user(login: "dependabot[bot]") { location }' in query


def test_build_query_requests_rate_limit_telemetry():
    adapter = OwnerLocationAdapter({"alice": "User"})
    query = adapter.build_query({"n0": "alice"})
    assert "rateLimit { cost used remaining }" in query


def test_parse_reads_the_rate_limit_block():
    from lib.graphql_batch import RateLimitInfo

    adapter = OwnerLocationAdapter({"alice": "User"})
    payload = {
        "data": {
            "n0": {"location": "Berlin"},
            "rateLimit": {"cost": 1, "used": 412, "remaining": 4588},
        }
    }
    parsed = adapter.parse(payload, {"n0": "alice"})
    assert parsed.values == {"alice": "Berlin"}
    assert parsed.rate_limit == RateLimitInfo(cost=1, used=412, remaining=4588)


def test_parse_reads_rate_limit_when_node_data_is_missing():
    from lib.graphql_batch import RateLimitInfo

    adapter = OwnerLocationAdapter({"ghost": "User"})
    payload = {"data": {"rateLimit": {"cost": 1, "used": 412, "remaining": 4588}}}
    parsed = adapter.parse(payload, {"n0": "ghost"})
    assert parsed.values == {}
    assert parsed.rate_limit == RateLimitInfo(cost=1, used=412, remaining=4588)


def test_end_to_end_with_batch_core():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"n0": {"location": "Lagos"}}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OwnerLocationAdapter({"alice": "User"})
    outcome = fetch_batch(adapter, ["alice"], client=client)
    assert outcome.values == {"alice": "Lagos"}
