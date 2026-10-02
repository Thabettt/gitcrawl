from __future__ import annotations

import json
import re
from collections.abc import Mapping

import httpx

from lib.graphql_batch import ParsedBatch, fetch_batch

ALIAS_RE = re.compile(r"(n\d+): field")


class DictAdapter:
    name = "dict"
    batch_size = 20

    def build_query(self, aliases: Mapping[str, str]) -> str:
        return "query { " + " ".join(f"{alias}: field" for alias in aliases) + " }"

    def parse(self, payload: Mapping[str, object], aliases: Mapping[str, str]) -> ParsedBatch[str]:
        data = payload.get("data")
        values: dict[str, str] = {}
        if isinstance(data, dict):
            for alias, node in data.items():
                key = aliases.get(alias)
                if key is None or not isinstance(node, str):
                    continue
                values[key] = node
        return ParsedBatch(values=values)


def client_from(responses, recorder=None):
    iterator = iter(responses)

    def handler(request):
        if recorder is not None:
            recorder.append(request)
        return next(iterator)

    return httpx.Client(transport=httpx.MockTransport(handler))


def node_payload(keys, *, prefix="value-"):
    data = {"rateLimit": {"cost": 1, "remaining": 4999}}
    for index, key in enumerate(keys):
        data[f"n{index}"] = f"{prefix}{key}"
    return {"data": data}


def test_fetch_batch_single_request_maps_all_values():
    captured = []
    client = client_from([httpx.Response(200, json=node_payload(["1", "2", "3"]))], captured)
    outcome = fetch_batch(DictAdapter(), ["1", "2", "3"], client=client)
    assert outcome.values == {"1": "value-1", "2": "value-2", "3": "value-3"}
    assert outcome.unresolved == {}
    assert outcome.stats.as_dict()["requests"] == 1
    assert len(captured) == 1
    query = json.loads(captured[0].content)["query"]
    assert ALIAS_RE.findall(query) == ["n0", "n1", "n2"]


def test_fetch_batch_keeps_good_results_when_one_alias_errors():
    payload = node_payload(["1", "2"])
    payload["data"]["n1"] = None
    payload["errors"] = [
        {"message": "Could not resolve to a node", "path": ["n1"]},
    ]
    captured = []
    client = client_from([httpx.Response(200, json=payload)], captured)
    outcome = fetch_batch(
        DictAdapter(),
        ["1", "2"],
        client=client,
        fallback=lambda key: f"fallback-{key}",
    )
    assert outcome.values == {"1": "value-1", "2": "fallback-2"}
    assert outcome.unresolved == {}
    assert outcome.stats.fallbacks == 1
    assert len(captured) == 1  # per-repo errors never trigger a split


def test_fetch_batch_missing_alias_goes_to_fallback():
    client = client_from([httpx.Response(200, json=node_payload(["1"]))])
    outcome = fetch_batch(
        DictAdapter(),
        ["1", "2"],
        client=client,
        fallback=lambda key: f"fallback-{key}",
    )
    assert outcome.values == {"1": "value-1", "2": "fallback-2"}
    assert outcome.stats.fallbacks == 1


def test_fetch_batch_never_discards_good_data_when_errors_and_data_share_a_reply():
    payload = node_payload(["1", "2", "3"])
    payload["data"]["n1"] = None
    payload["errors"] = [
        {"message": "Could not resolve to a node", "path": ["n1"]},
    ]
    client = client_from([httpx.Response(200, json=payload)])
    seen_fallbacks = []

    def fallback(key):
        seen_fallbacks.append(key)
        return None

    outcome = fetch_batch(DictAdapter(), ["1", "2", "3"], client=client, fallback=fallback)
    assert outcome.values == {"1": "value-1", "3": "value-3"}
    assert seen_fallbacks == ["2"]
    assert outcome.stats.values == 2
    assert outcome.stats.handled == 1


def test_fetch_batch_without_keys_makes_no_request():
    def handler(request):
        raise AssertionError("no request expected")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(DictAdapter(), [], client=client)
    assert outcome.values == {}
    assert outcome.stats.as_dict()["keys"] == 0
