from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Mapping

import httpx
import pytest

from lib.deadlines import Deadline
from lib.graphql_batch import GraphQLAuthError, ParsedBatch, fetch_batch

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


class KeyAdapter(DictAdapter):
    def build_query(self, aliases: Mapping[str, str]) -> str:
        pairs = " ".join(f"{alias}: field_{key}" for alias, key in aliases.items())
        return f"query {{ {pairs} }}"


def keys_in(request: httpx.Request) -> list[str]:
    query = json.loads(request.content)["query"]
    return re.findall(r"n\d+: field_(\d+)", query)


def test_transient_batch_error_splits_and_only_failing_keys_are_resent():
    requests: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys = keys_in(request)
        requests.append(keys)
        if len(keys) > 1:
            return httpx.Response(200, json={"data": None, "errors": [{"message": "timeout"}]})
        if keys == ["2"]:
            return httpx.Response(200, json={"data": {"n0": "value-2"}})
        return httpx.Response(200, json={"data": None, "errors": [{"message": "timeout"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(KeyAdapter(), ["1", "2", "3", "4"], client=client)
    assert outcome.values == {"2": "value-2"}
    assert set(outcome.unresolved) == {"1", "3", "4"}
    assert all("timeout" in reason for reason in outcome.unresolved.values())
    assert outcome.stats.requeues >= 2
    assert requests[0] == ["1", "2", "3", "4"]
    assert all(len(batch) <= 2 for batch in requests[1:])
    assert ["2"] in requests


def test_single_key_transient_failure_goes_to_fallback_after_attempts():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"data": None, "errors": [{"message": "timeout"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(DictAdapter(), ["1"], client=client, fallback=lambda key: f"rest-{key}")
    assert outcome.values == {"1": "rest-1"}
    assert calls["n"] == 3  # max_attempts
    assert outcome.stats.fallbacks == 1


def test_non_transient_batch_error_skips_splitting_and_uses_fallback():
    requests: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(len(keys_in(request)))
        return httpx.Response(
            200, json={"data": None, "errors": [{"message": "Field 'x' doesn't exist"}]}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        KeyAdapter(), ["1", "2", "3"], client=client, fallback=lambda key: f"rest-{key}"
    )
    assert requests == [3]
    assert outcome.values == {"1": "rest-1", "2": "rest-2", "3": "rest-3"}


def test_fallback_failure_is_recorded_as_unresolved_with_reason():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": None, "errors": [{"message": "timeout"}]})

    def fallback(key: str) -> object:
        raise RuntimeError("rest is down")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(DictAdapter(), ["7"], client=client, fallback=fallback)
    assert outcome.values == {}
    assert "RuntimeError" in outcome.unresolved["7"]
    assert outcome.stats.unresolved == 1


def test_deadline_expiry_stops_requests_and_marks_remaining_unresolved():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected after deadline")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    deadline = Deadline(0.0)
    outcome = fetch_batch(DictAdapter(), ["1", "2"], client=client, deadline=deadline)
    assert outcome.values == {}
    assert outcome.unresolved == {"1": "run deadline exceeded", "2": "run deadline exceeded"}
    assert outcome.stats.deadline_hit is True
    assert outcome.stats.requests == 0


def test_http_401_aborts_loudly():
    client = client_from([httpx.Response(401, json={"message": "Bad credentials"})])
    with pytest.raises(GraphQLAuthError):
        fetch_batch(DictAdapter(), ["1"], client=client)


def test_malformed_json_is_treated_as_a_transient_batch_failure():
    client = client_from([httpx.Response(200, text="<html>nope</html>")])
    outcome = fetch_batch(DictAdapter(), ["1"], client=client, fallback=lambda key: f"rest-{key}")
    assert outcome.values == {"1": "rest-1"}
    assert outcome.stats.fallbacks == 1


def test_sso_partial_results_degrades_instead_of_aborting():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {}}, headers={"x-github-sso": "partial-results; ..."}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(DictAdapter(), ["1"], client=client, fallback=lambda key: f"rest-{key}")
    assert outcome.values == {"1": "rest-1"}
    assert outcome.stats.fallbacks == 1

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(DictAdapter(), ["1"], client=client)
    assert "1" in outcome.unresolved
    assert "PartialResultsError" in outcome.unresolved["1"]


def test_allow_requests_false_resolves_everything_through_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected when batching is disabled")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        KeyAdapter(),
        ["1", "2"],
        client=client,
        allow_requests=False,
        fallback=lambda key: f"rest-{key}",
    )
    assert outcome.values == {"1": "rest-1", "2": "rest-2"}
    assert outcome.stats.requests == 0
    assert outcome.stats.fallbacks == 2


def test_progress_counts_handled_fallback_keys():
    client = client_from([httpx.Response(200, json={"data": {}, "errors": [{"message": "boom"}]})])
    seen: list[tuple[int, int]] = []
    outcome = fetch_batch(
        DictAdapter(),
        ["1", "2"],
        client=client,
        fallback=lambda key: None,
        on_progress=lambda done, total: seen.append((done, total)),
    )
    assert outcome.stats.handled == 2
    assert seen[-1] == (2, 2)


def test_fetch_batch_runs_chunks_concurrently():
    import threading

    barrier = threading.Barrier(3, timeout=10)

    def handler(request: httpx.Request) -> httpx.Response:
        barrier.wait()
        return httpx.Response(200, json={"data": {}})

    adapter = DictAdapter()
    adapter.batch_size = 3
    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        adapter,
        [str(index) for index in range(9)],
        client=client,
        concurrency=3,
    )
    assert outcome.stats.requests == 3
    assert outcome.stats.unresolved == 9


def test_on_resolved_streams_chunks_as_they_complete():
    resolved: list[tuple[list[str], dict[str, object]]] = []
    issued = 0
    responses = iter(
        [
            httpx.Response(200, json=node_payload(["1", "2"])),
            httpx.Response(200, json=node_payload(["3", "4"])),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal issued
        assert len(resolved) == issued, "a completed chunk must resolve before the next request"
        issued += 1
        return next(responses)

    adapter = DictAdapter()
    adapter.batch_size = 2
    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        adapter,
        ["1", "2", "3", "4"],
        client=client,
        concurrency=1,
        on_resolved=lambda keys, values: resolved.append((list(keys), dict(values))),
    )
    assert outcome.values == {"1": "value-1", "2": "value-2", "3": "value-3", "4": "value-4"}
    assert resolved == [
        (["1", "2"], {"1": "value-1", "2": "value-2"}),
        (["3", "4"], {"3": "value-3", "4": "value-4"}),
    ]


def test_on_resolved_reports_only_values_resolved_by_the_batch():
    client = client_from([httpx.Response(200, json=node_payload(["1"]))])
    resolved: list[tuple[list[str], dict[str, object]]] = []
    outcome = fetch_batch(
        DictAdapter(),
        ["1", "2"],
        client=client,
        fallback=lambda key: f"rest-{key}",
        on_resolved=lambda keys, values: resolved.append((list(keys), dict(values))),
    )
    assert outcome.values == {"1": "value-1", "2": "rest-2"}
    assert resolved == [(["1"], {"1": "value-1"})]


@pytest.mark.parametrize("status", [499, 502, 504])
def test_http_timeout_status_splits_batches_before_falling_back(status):
    requests: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys = keys_in(request)
        requests.append(keys)
        if len(keys) > 2:
            return httpx.Response(status, json={"message": "timeout"})
        data = {f"n{index}": f"value-{key}" for index, key in enumerate(keys)}
        return httpx.Response(200, json={"data": data})

    adapter = KeyAdapter()
    adapter.batch_size = 4
    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        adapter,
        ["1", "2", "3", "4"],
        client=client,
        concurrency=1,
        fallback=lambda key: f"rest-{key}",
        sleep=lambda _seconds: None,
    )
    assert outcome.values == {"1": "value-1", "2": "value-2", "3": "value-3", "4": "value-4"}
    assert outcome.stats.fallbacks == 0
    assert outcome.stats.requeues >= 1
    assert requests[0] == ["1", "2", "3", "4"]
    assert {tuple(batch) for batch in requests[1:]} == {("1", "2"), ("3", "4")}


def test_fallbacks_run_in_the_background_without_stalling_dispatch():
    order: list[tuple[str, object]] = []
    guard = threading.Lock()

    def record(event: tuple[str, object]) -> None:
        with guard:
            order.append(event)

    def handler(request: httpx.Request) -> httpx.Response:
        keys = keys_in(request)
        record(("request", tuple(keys)))
        if keys == ["1", "2", "3", "4"]:
            return httpx.Response(400, json={"message": "bad request"})
        data = {f"n{index}": f"value-{key}" for index, key in enumerate(keys)}
        return httpx.Response(200, json={"data": data})

    def fallback(key: str) -> str:
        record(("fallback-start", key))
        time.sleep(0.2)
        record(("fallback-done", key))
        return f"rest-{key}"

    adapter = KeyAdapter()
    adapter.batch_size = 4
    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        adapter,
        ["1", "2", "3", "4", "5", "6", "7", "8"],
        client=client,
        concurrency=1,
        fallback=fallback,
        sleep=lambda _seconds: None,
    )
    assert outcome.values == {
        "1": "rest-1",
        "2": "rest-2",
        "3": "rest-3",
        "4": "rest-4",
        "5": "value-5",
        "6": "value-6",
        "7": "value-7",
        "8": "value-8",
    }
    dispatch_index = order.index(("request", ("5", "6", "7", "8")))
    first_fallback_done = order.index(("fallback-done", "1"))
    assert dispatch_index < first_fallback_done


def test_http_500_still_falls_back_without_splitting():
    requests: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(keys_in(request))
        return httpx.Response(500, json={"message": "server error"})

    adapter = KeyAdapter()
    adapter.batch_size = 4
    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        adapter,
        ["1", "2", "3", "4"],
        client=client,
        concurrency=1,
        fallback=lambda key: f"rest-{key}",
        sleep=lambda _seconds: None,
    )
    assert outcome.values == {"1": "rest-1", "2": "rest-2", "3": "rest-3", "4": "rest-4"}
    assert outcome.stats.fallbacks == 4
    assert all(batch == ["1", "2", "3", "4"] for batch in requests)
