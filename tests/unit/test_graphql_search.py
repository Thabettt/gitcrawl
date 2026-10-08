from __future__ import annotations

import json

import httpx
import pytest

from discover.graphql_search import count_queries, iter_pages, node_to_item
from lib.gh_client import RequestFailed
from lib.graphql_batch import GraphQLAuthError

GRAPHQL_URL = "https://api.github.com/graphql"


def graphql_client(payloads, captured=None):
    iterator = iter(payloads)

    def handler(request):
        if captured is not None:
            captured.append(request)
        return next(iterator)

    return httpx.Client(transport=httpx.MockTransport(handler))


def node(**overrides):
    base = {
        "databaseId": 1,
        "id": "R_1",
        "nameWithOwner": "o/r",
        "name": "r",
        "description": "d",
        "homepageUrl": None,
        "primaryLanguage": {"name": "Rust"},
        "licenseInfo": {"spdxId": "MIT"},
        "repositoryTopics": {"nodes": [{"topic": {"name": "cli"}}]},
        "visibility": "PUBLIC",
        "isFork": False,
        "parent": None,
        "isArchived": False,
        "isDisabled": False,
        "isTemplate": False,
        "mirrorUrl": None,
        "diskUsage": 12,
        "stargazerCount": 7,
        "forkCount": 1,
        "watchers": {"totalCount": 3},
        "issues": {"totalCount": 2},
        "defaultBranchRef": {"name": "main"},
        "hasIssuesEnabled": True,
        "hasWikiEnabled": False,
        "hasProjectsEnabled": True,
        "hasDiscussionsEnabled": False,
        "hasPullRequestsEnabled": True,
        "owner": {"login": "o", "__typename": "User", "databaseId": 9},
        "createdAt": "2025-01-01T00:00:00Z",
        "pushedAt": "2025-02-01T00:00:00Z",
        "updatedAt": "2025-02-02T00:00:00Z",
    }
    base.update(overrides)
    return base


def page_payload(nodes, *, count=1, has_next=False, cursor=None, errors=None):
    body = {
        "data": {
            "s": {
                "repositoryCount": count,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                "nodes": nodes,
            }
        }
    }
    if errors is not None:
        body["errors"] = errors
    return httpx.Response(200, json=body)


def test_node_to_item_maps_rest_shape():
    item = node_to_item(node())
    assert item["id"] == 1 and item["node_id"] == "R_1" and item["full_name"] == "o/r"
    assert item["owner"] == {"id": 9, "login": "o", "type": "User"}
    assert item["license"] == {"spdx_id": "MIT"}
    assert item["topics"] == ["cli"]
    assert item["stargazers_count"] == 7 and item["forks_count"] == 1
    assert item["watchers_count"] == 3 and item["open_issues_count"] == 2
    assert item["archived"] is False and item["visibility"] == "public"
    assert item["has_pages"] is None and item["custom_properties"] == {}


def test_page_query_requests_only_the_lean_node_fields():
    captured = []
    payloads = [page_payload([node()])]
    list(iter_pages(graphql_client(payloads, captured), "q", max_pages=1, now=lambda: 1000.0))
    query = json.loads(captured[0].content)["query"]
    for kept in (
        "databaseId",
        "nameWithOwner",
        "stargazerCount",
        "forkCount",
        "isArchived",
        "primaryLanguage { name }",
        "licenseInfo { spdxId }",
        "pushedAt",
        "createdAt",
        "owner {",
    ):
        assert kept in query
    assert "\n      id\n" in query
    assert "\n      name\n" in query
    for dropped in (
        "repositoryTopics",
        "watchers",
        "issues",
        "diskUsage",
        "defaultBranchRef",
        "description",
        "homepageUrl",
        "visibility",
        "isFork",
        "parent",
        "isDisabled",
        "isTemplate",
        "mirrorUrl",
        "hasIssuesEnabled",
        "hasWikiEnabled",
        "hasProjectsEnabled",
        "hasDiscussionsEnabled",
        "hasPullRequestsEnabled",
        "updatedAt",
    ):
        assert dropped not in query


def test_iter_pages_follows_cursor_and_caps_pages():
    captured = []
    payloads = [
        page_payload([node()], count=250, has_next=True, cursor="c1"),
        page_payload([node(databaseId=2)], count=250, has_next=True, cursor="c2"),
    ]
    pages = list(
        iter_pages(
            graphql_client(payloads, captured), "language:rust", max_pages=2, now=lambda: 1000.0
        )
    )
    assert [page.exhausted for page in pages] == [False, True]
    assert pages[0].items[0]["id"] == 1 and pages[1].items[0]["id"] == 2
    first, second = (json.loads(r.content) for r in captured)
    assert "after" not in first["query"] and 'after: "c1"' in second["query"]
    assert "first: 100" in first["query"]


def test_page_timeout_halves_size_and_replays_the_same_cursor():
    captured = []
    payloads = [
        page_payload([node()] * 100, count=500, has_next=True, cursor="c1"),
        httpx.Response(502),
        page_payload([node()] * 50, count=500),
    ]
    pages = list(
        iter_pages(
            graphql_client(payloads, captured),
            "q",
            sleep=lambda _: None,
            now=lambda: 1000.0,
            jitter=lambda: 0.0,
        )
    )
    assert [len(page.items) for page in pages] == [100, 50]
    queries = [json.loads(request.content)["query"] for request in captured]
    assert len(queries) == 3
    assert "s: search(first: 100," in queries[0] and "after" not in queries[0]
    assert "s: search(first: 100," in queries[1] and 'after: "c1"' in queries[1]
    assert "s: search(first: 50," in queries[2] and 'after: "c1"' in queries[2]


def test_page_size_stays_halved_for_later_pages():
    captured = []
    payloads = [
        httpx.Response(502),
        page_payload([node()] * 50, count=500, has_next=True, cursor="c1"),
        page_payload([node()] * 50, count=500),
    ]
    pages = list(
        iter_pages(
            graphql_client(payloads, captured),
            "q",
            sleep=lambda _: None,
            now=lambda: 1000.0,
            jitter=lambda: 0.0,
        )
    )
    assert [len(page.items) for page in pages] == [50, 50]
    queries = [json.loads(request.content)["query"] for request in captured]
    assert "s: search(first: 100," in queries[0]
    assert "s: search(first: 50," in queries[1] and "after" not in queries[1]
    assert "s: search(first: 50," in queries[2] and 'after: "c1"' in queries[2]


def test_page_timeouts_at_the_floor_raise_request_failed():
    captured = []
    payloads = [httpx.Response(502), httpx.Response(502), httpx.Response(502)]
    with pytest.raises(RequestFailed) as excinfo:
        list(
            iter_pages(
                graphql_client(payloads, captured),
                "q",
                sleep=lambda _: None,
                now=lambda: 1000.0,
                jitter=lambda: 0.0,
            )
        )
    assert excinfo.value.status == 502
    assert "minimum page size" in excinfo.value.message
    queries = [json.loads(request.content)["query"] for request in captured]
    assert len(queries) == 3
    assert "s: search(first: 100," in queries[0]
    assert "s: search(first: 50," in queries[1]
    assert "s: search(first: 25," in queries[2]


def test_item_budget_marks_a_truncated_shard_exhausted():
    captured = []
    payloads = [
        page_payload([node()] * 100, count=500, has_next=True, cursor="c1"),
        page_payload([node()] * 50, count=500, has_next=True, cursor="c2"),
    ]
    pages = list(
        iter_pages(
            graphql_client(payloads, captured),
            "q",
            max_pages=2,
            sleep=lambda _: None,
            now=lambda: 1000.0,
            jitter=lambda: 0.0,
        )
    )
    assert [page.exhausted for page in pages] == [False, True]
    assert len(captured) == 2


def test_item_budget_is_preserved_when_pages_are_halved():
    captured = []
    payloads = [
        page_payload([node()] * 100, count=500, has_next=True, cursor="c1"),
        httpx.Response(504),
        page_payload([node()] * 50, count=500, has_next=True, cursor="c2"),
        page_payload([node()] * 50, count=500, has_next=True, cursor="c3"),
    ]
    pages = list(
        iter_pages(
            graphql_client(payloads, captured),
            "q",
            max_pages=2,
            sleep=lambda _: None,
            now=lambda: 1000.0,
            jitter=lambda: 0.0,
        )
    )
    assert [page.exhausted for page in pages] == [False, False, True]
    queries = [json.loads(request.content)["query"] for request in captured]
    sizes = [
        100 if "s: search(first: 100," in query else 50 if "s: search(first: 50," in query else 0
        for query in queries
    ]
    assert sizes == [100, 100, 50, 50]
    assert 'after: "c1"' in queries[1] and 'after: "c1"' in queries[2]
    assert 'after: "c2"' in queries[3]


def test_count_queries_batches_twenty_aliases_in_one_request():
    captured = []
    queries = [f"language:rust created:2025-01-{d:02d}" for d in range(1, 22)]
    payload = {"data": {f"s{i}": {"repositoryCount": i} for i in range(20)}}
    payload["data"]["s0"] = {"repositoryCount": 5}
    pages = [
        httpx.Response(200, json=payload),
        httpx.Response(200, json={"data": {"s0": {"repositoryCount": 9}}}),
    ]
    counts = count_queries(graphql_client(pages, captured), queries, now=lambda: 1000.0)
    assert len(captured) == 2 and counts[:20] == [5] + list(range(1, 20)) and counts[20] == 9


def test_count_queries_retries_missing_aliases():
    captured = []
    payloads = [
        httpx.Response(200, json={"data": {"s0": {"repositoryCount": 4}}}),
        httpx.Response(200, json={"data": {"s0": {"repositoryCount": 4}}}),
    ]
    counts = count_queries(
        graphql_client(payloads, captured), ["q"], sleep=lambda s: None, now=lambda: 1000.0
    )
    assert counts == [4] and len(captured) == 1


def test_page_transient_error_retries_then_succeeds():
    captured = []
    payloads = [
        httpx.Response(200, json={"errors": [{"message": "resource limits exceeded"}]}),
        page_payload([node()]),
    ]
    pages = list(
        iter_pages(
            graphql_client(payloads, captured),
            "q",
            sleep=lambda s: None,
            now=lambda: 1000.0,
            jitter=lambda: 0.0,
        )
    )
    assert len(pages) == 1 and len(captured) == 2


def test_page_partial_data_with_errors_is_incomplete_but_kept():
    payloads = [page_payload([node()], errors=[{"message": "resource limits exceeded"}])]
    page = next(iter(iter_pages(graphql_client(payloads), "q", sleep=lambda s: None)))
    assert page.incomplete is True and len(page.items) == 1


def test_unauthorized_raises_graphql_auth_error():
    payloads = [httpx.Response(401, json={"message": "Bad credentials"})]
    with pytest.raises(GraphQLAuthError):
        list(iter_pages(graphql_client(payloads), "q", now=lambda: 1000.0))


def test_on_response_receives_audit_params():
    recorded = []
    payloads = [page_payload([node()])]
    list(
        iter_pages(
            graphql_client(payloads),
            "q",
            max_pages=1,
            now=lambda: 1000.0,
            on_response=lambda response, ms, params: recorded.append(params),
        )
    )
    assert recorded[0]["q"] == "q" and recorded[0]["after"] is None
