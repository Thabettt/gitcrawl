from __future__ import annotations

import httpx

from enrich.graphql_file_presence import FilePresenceAdapter
from lib.graphql_batch import fetch_batch


def test_query_uses_head_expression_and_owner_name_aliases():
    adapter = FilePresenceAdapter("Dockerfile", {"911": "octo/alpha"})
    query = adapter.build_query({"n0": "911"})
    assert 'n0: repository(owner: "octo", name: "alpha")' in query
    assert 'object(expression: "HEAD:Dockerfile")' in query
    assert "__typename" in query


def test_parse_blob_is_true_and_missing_object_is_false():
    adapter = FilePresenceAdapter("Dockerfile", {"1": "octo/one", "2": "octo/two"})
    payload = {"data": {"n0": {"object": {"__typename": "Blob"}}, "n1": {"object": None}}}
    parsed = adapter.parse(payload, {"n0": "1", "n1": "2"})
    assert parsed.values == {"1": True, "2": False}


def test_parse_tree_object_is_false():
    adapter = FilePresenceAdapter("Dockerfile", {"1": "octo/one"})
    parsed = adapter.parse({"data": {"n0": {"object": {"__typename": "Tree"}}}}, {"n0": "1"})
    assert parsed.values == {"1": False}


def test_null_repository_is_missing_not_false():
    adapter = FilePresenceAdapter("Dockerfile", {"1": "octo/one"})
    parsed = adapter.parse({"data": {"n0": None}}, {"n0": "1"})
    assert parsed.values == {}
    assert parsed.failures == {}


def test_end_to_end_with_batch_core():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"n0": {"object": {"__typename": "Blob"}}}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = FilePresenceAdapter("Dockerfile", {"1": "octo/one"})
    outcome = fetch_batch(adapter, ["1"], client=client)
    assert outcome.values == {"1": True}
