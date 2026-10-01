from __future__ import annotations

import json
import re
from collections import Counter

import httpx
import pytest

from discover.search_shards import RequestFailed
from enrich.graphql_batch import (
    _MAX_SPLIT_DEPTH,
    RepoGraphQL,
    build_batch_query,
    fetch_graphql_batch,
    parse_batch_response,
)

GRAPHQL_URL = "https://api.github.com/graphql"

SPLIT_ERRORS = (
    {"message": "timeout while executing query"},
    {"message": "Resource limits exceeded for this query"},
    {"message": "Something went wrong while executing your query"},
)


def client_from(responses, recorder=None):
    iterator = iter(responses)

    def handler(request):
        if recorder is not None:
            recorder.append(request)
        return next(iterator)

    return httpx.Client(transport=httpx.MockTransport(handler))


def payload_for(repo_ids, *, cost=1):
    data = {"rateLimit": {"cost": cost, "remaining": 10, "resetAt": "2026-10-01T00:00:00Z"}}
    for repo_id in repo_ids:
        data[f"c{repo_id}"] = {}
    return {"data": data}


def test_build_batch_query_requires_node_ids():
    with pytest.raises(ValueError):
        build_batch_query([1, 2])
    with pytest.raises(ValueError):
        build_batch_query([1, 2], node_ids=["only-one"])


def test_build_batch_query_rejects_more_than_max_aliases():
    repo_ids = list(range(1, 22))
    node_ids = [f"NODE_{repo_id}" for repo_id in repo_ids]
    with pytest.raises(ValueError):
        build_batch_query(repo_ids, node_ids=node_ids, max_aliases=20)
    query = build_batch_query(repo_ids[:-1], node_ids=node_ids[:-1], max_aliases=20)
    assert query.count("node(id:") == 20


def test_build_batch_query_is_shallow_node_form_with_ratelimit_and_first_cap():
    query = build_batch_query([11, 22], node_ids=["NODE_A", "NODE_B"])
    assert "rateLimit { cost remaining resetAt }" in query
    assert 'c11: node(id: "NODE_A")' in query
    assert 'c22: node(id: "NODE_B")' in query
    for field in (
        "fundingLinks { url platform }",
        "hasDiscussionsEnabled",
        "discussions(first: 1) { totalCount }",
        "sponsorsListing { tiers(first: 1) { monthlyPriceInDollars } }",
    ):
        assert query.count(field) == 2
    assert re.findall(r"first: (\d+)", query) == ["1", "1", "1", "1"]
    assert "issues" not in query
    assert "pullRequests" not in query


def test_parse_batch_response_happy_path_with_null_node():
    payload = {
        "data": {
            "rateLimit": {"cost": 2, "remaining": 4999, "resetAt": "2026-10-01T00:00:00Z"},
            "c1": {
                "fundingLinks": [{"url": "https://x.test", "platform": "GITHUB"}],
                "hasDiscussionsEnabled": True,
                "discussions": {"totalCount": 4},
                "sponsorsListing": {"tiers": [{"monthlyPriceInDollars": 5}]},
            },
            "c2": None,
        }
    }
    assert parse_batch_response(payload) == {
        1: RepoGraphQL(
            repo_id=1,
            funding_links=({"url": "https://x.test", "platform": "GITHUB"},),
            has_discussions=True,
            discussions_count=4,
            sponsors_tiers=({"monthlyPriceInDollars": 5},),
            rate_limit_cost=2,
        )
    }


def test_parse_batch_response_defaults_partial_fields():
    payload = {
        "data": {
            "rateLimit": {"cost": 1},
            "c7": {},
            "c8": {
                "hasDiscussionsEnabled": False,
                "discussions": None,
                "sponsorsListing": None,
                "fundingLinks": [],
            },
        }
    }
    assert parse_batch_response(payload) == {
        7: RepoGraphQL(repo_id=7, rate_limit_cost=1),
        8: RepoGraphQL(repo_id=8, has_discussions=False, rate_limit_cost=1),
    }


def test_parse_batch_response_without_data_returns_empty_map():
    assert parse_batch_response({}) == {}
    assert parse_batch_response({"data": None}) == {}
    assert parse_batch_response({"data": {"rateLimit": {"cost": 1}}}) == {}


def test_parse_batch_response_ignores_ratelimit_and_unknown_aliases():
    payload = {
        "data": {
            "rateLimit": {"cost": 3},
            "weird": {"fundingLinks": []},
            "c5": {},
        }
    }
    result = parse_batch_response(payload)
    assert set(result) == {5}
    assert result[5].rate_limit_cost == 3


def test_fetch_graphql_batch_posts_single_query_and_maps_results():
    captured = []
    recorded = []
    payload = {
        "data": {
            "rateLimit": {"cost": 2, "remaining": 4999, "resetAt": "2026-10-01T00:00:00Z"},
            "c1": {"hasDiscussionsEnabled": True},
        }
    }
    client = client_from([httpx.Response(200, json=payload)], captured)
    result = fetch_graphql_batch(
        client,
        token="tok",
        repo_ids=[1],
        node_ids=["NODE_1"],
        on_response=lambda response, latency_ms: recorded.append(response.status_code),
    )
    assert result == {1: RepoGraphQL(repo_id=1, has_discussions=True, rate_limit_cost=2)}
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == GRAPHQL_URL
    assert request.headers["Authorization"] == "Bearer tok"
    assert json.loads(request.content) == {"query": build_batch_query([1], node_ids=["NODE_1"])}
    assert recorded == [200]


def test_fetch_graphql_batch_requires_node_ids_before_any_request():
    def handler(request):
        raise AssertionError("no HTTP request expected without node ids")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError):
        fetch_graphql_batch(client, token="tok", repo_ids=[1])


def test_fetch_graphql_batch_deviation_from_max_aliases_is_value_error():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    with pytest.raises(ValueError):
        fetch_graphql_batch(
            client,
            token="tok",
            repo_ids=list(range(1, 22)),
            node_ids=[f"NODE_{repo_id}" for repo_id in range(1, 22)],
        )


def test_fetch_graphql_batch_maps_http_error_to_request_failed():
    captured = []
    client = client_from([httpx.Response(401, json={"message": "Bad credentials"})], captured)
    with pytest.raises(RequestFailed) as excinfo:
        fetch_graphql_batch(client, token="tok", repo_ids=[1], node_ids=["NODE_1"])
    assert excinfo.value.status == 401
    assert excinfo.value.message == "Bad credentials"
    assert len(captured) == 1


def test_fetch_graphql_batch_maps_malformed_body_to_request_failed():
    client = client_from([httpx.Response(200, text="<html>not json</html>")])
    with pytest.raises(RequestFailed) as excinfo:
        fetch_graphql_batch(client, token="tok", repo_ids=[1], node_ids=["NODE_1"])
    assert excinfo.value.status == 200


def test_fetch_graphql_batch_raises_on_non_split_errors():
    captured = []
    payload = {"data": None, "errors": [{"message": "Could not resolve to a node"}]}
    client = client_from([httpx.Response(200, json=payload)], captured)
    with pytest.raises(RequestFailed) as excinfo:
        fetch_graphql_batch(client, token="tok", repo_ids=[1], node_ids=["NODE_1"])
    assert excinfo.value.message == "Could not resolve to a node"
    assert len(captured) == 1


@pytest.mark.parametrize("error", SPLIT_ERRORS)
def test_split_returns_partial_results_and_retries_only_failures(error):
    posts = []

    def handler(request):
        query = json.loads(request.content)["query"]
        ids = [int(match) for match in re.findall(r"c(\d+): node", query)]
        posts.append(ids)
        if any(repo_id in {1, 2} for repo_id in ids):
            return httpx.Response(200, json={"data": None, "errors": [error]})
        return httpx.Response(200, json=payload_for(ids, cost=1))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = fetch_graphql_batch(
        client,
        token="tok",
        repo_ids=list(range(1, 5)),
        node_ids=[f"NODE_{repo_id}" for repo_id in range(1, 5)],
    )
    assert set(result) == {3, 4}
    assert result[3].rate_limit_cost == 1
    counts = Counter(frozenset(ids) for ids in posts)
    assert counts[frozenset({3, 4})] == 1
    assert all(3 not in ids and 4 not in ids for ids in posts[posts.index([3, 4]) + 1 :])
    failing = sum(count for ids, count in counts.items() if ids <= {1, 2})
    assert 0 < failing <= _MAX_SPLIT_DEPTH


def test_fetch_graphql_batch_depth_cap_stops_recursion_with_request_failed():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"data": None, "errors": [{"message": "timeout"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    repo_ids = list(range(1, 17))
    with pytest.raises(RequestFailed):
        fetch_graphql_batch(
            client,
            token="tok",
            repo_ids=repo_ids,
            node_ids=[f"NODE_{repo_id}" for repo_id in repo_ids],
        )
    assert len(captured) == 2 ** (_MAX_SPLIT_DEPTH + 1) - 1


def test_fetch_graphql_batch_returns_empty_map_without_requests_for_no_ids():
    def handler(request):
        raise AssertionError("no HTTP request expected for empty ids")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert fetch_graphql_batch(client, token="tok", repo_ids=[], node_ids=[]) == {}
