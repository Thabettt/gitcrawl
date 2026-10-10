from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping

import fakeredis
import httpx

from lib.graphql_batch import ParsedBatch, fetch_batch
from limiter.adaptive import AdaptiveConfig, AdaptiveController
from limiter.buckets import BucketLimiter


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class KeyAdapter:
    name = "keys"

    def __init__(self, batch_size: int = 20) -> None:
        self.batch_size = batch_size

    def build_query(self, aliases: Mapping[str, str]) -> str:
        pairs = " ".join(f"{alias}: field_{key}" for alias, key in aliases.items())
        return f"query {{ {pairs} }}"

    def parse(self, payload: Mapping[str, object], aliases: Mapping[str, str]) -> ParsedBatch[str]:
        data = payload.get("data")
        values: dict[str, str] = {}
        if isinstance(data, dict):
            for alias, node in data.items():
                key = aliases.get(alias)
                if key is not None and isinstance(node, str):
                    values[key] = node
        return ParsedBatch(values=values)


KEYS_RE = re.compile(r"n\d+: field_(\d+)")


def keys_in(request: httpx.Request) -> list[str]:
    return KEYS_RE.findall(json.loads(request.content)["query"])


def ok_response(keys: list[str]) -> httpx.Response:
    data = {f"n{index}": f"v-{key}" for index, key in enumerate(keys)}
    return httpx.Response(200, json={"data": data})


def test_first_drop_halves_the_window_and_pauses_the_pool() -> None:
    clock = FakeClock()
    controller = AdaptiveController(AdaptiveConfig(), now=clock, rng=lambda: 0.0)
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] <= 5:
            return httpx.Response(
                403, json={"message": "secondary rate limit"}, headers={"retry-after": "30"}
            )
        return ok_response(keys_in(request))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        KeyAdapter(),
        ["1", "2", "3", "4"],
        client=client,
        adaptive=controller,
        fallback=lambda key: f"rest-{key}",
        max_attempts=1,
        sleep=clock.advance,
        now=clock,
    )
    assert controller.window == 10
    assert controller.snapshot()["drops"] == 1
    assert controller.snapshot()["pauses"] == 1
    assert outcome.values == {"1": "rest-1", "2": "rest-2", "3": "rest-3", "4": "rest-4"}
    assert outcome.unresolved == {}


def test_pause_defers_new_dispatch_until_it_expires() -> None:
    clock = FakeClock()
    controller = AdaptiveController(
        AdaptiveConfig(initial_window=2, min_window=1, max_window=2),
        now=clock,
        rng=lambda: 0.0,
    )
    starts: list[tuple[list[str], float]] = []
    lock = threading.Lock()
    state = {"failed": False}

    def handler(request: httpx.Request) -> httpx.Response:
        keys = keys_in(request)
        with lock:
            starts.append((keys, clock.value))
        if keys == ["1", "2"] and not state["failed"]:
            state["failed"] = True
            return httpx.Response(504, json={"message": "timeout"})
        return ok_response(keys)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        KeyAdapter(batch_size=2),
        ["1", "2", "3", "4", "5", "6"],
        client=client,
        adaptive=controller,
        sleep=clock.advance,
        now=clock,
    )
    assert set(outcome.values) == {"1", "2", "3", "4", "5", "6"}
    first = next(time for keys, time in starts if keys == ["1", "2"])
    tail = [time for keys, time in starts if keys and keys[0] in ("5", "6")]
    assert tail and min(tail) - first >= 60.0
    assert controller.window == 1


def test_timeout_shrinks_followup_chunks() -> None:
    clock = FakeClock()
    controller = AdaptiveController(
        AdaptiveConfig(
            initial_window=1,
            min_window=1,
            max_window=1,
            short_pause_seconds=0.0,
            pause_jitter_seconds=0.0,
        ),
        now=clock,
        rng=lambda: 0.0,
    )
    batches: list[list[str]] = []
    state = {"failed": False}

    def handler(request: httpx.Request) -> httpx.Response:
        keys = keys_in(request)
        batches.append(list(keys))
        if not state["failed"]:
            state["failed"] = True
            return httpx.Response(504, json={"message": "timeout"})
        return ok_response(keys)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        KeyAdapter(),
        [str(index) for index in range(1, 41)],
        client=client,
        adaptive=controller,
        sleep=clock.advance,
        now=clock,
    )
    assert batches[0] == [str(index) for index in range(1, 21)]
    assert controller.batch_size == 14
    assert all(len(batch) <= 14 for batch in batches[1:])
    assert any(len(batch) == 14 for batch in batches[1:])
    assert outcome.unresolved == {}


def test_reserve_floor_defers_queued_keys_without_dispatch() -> None:
    clock = FakeClock()
    controller = AdaptiveController(
        AdaptiveConfig(initial_window=2, min_window=1, max_window=2),
        now=clock,
        rng=lambda: 0.0,
    )
    batches: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys = keys_in(request)
        batches.append(list(keys))
        return httpx.Response(
            200,
            json={"data": {f"n{index}": f"v-{key}" for index, key in enumerate(keys)}},
            headers={"x-ratelimit-remaining": "350", "x-ratelimit-reset": "4600"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        KeyAdapter(batch_size=2),
        ["1", "2", "3", "4", "5", "6"],
        client=client,
        adaptive=controller,
        sleep=clock.advance,
        now=clock,
    )
    assert batches == [["1", "2"], ["3", "4"]]
    assert outcome.stats.deferred == 2
    assert outcome.unresolved == {
        "5": "deferred: github graphql point reserve reached",
        "6": "deferred: github graphql point reserve reached",
    }
    assert outcome.values == {"1": "v-1", "2": "v-2", "3": "v-3", "4": "v-4"}
    assert controller.snapshot()["deferred"] == 2


def test_local_throttled_error_counts_as_a_drop_and_pauses_the_bucket() -> None:
    clock = FakeClock(1000.0)
    redis = fakeredis.FakeRedis()
    limiter = BucketLimiter(redis, specs={"graphql": (0, 3600.0)}, max_concurrent=100)
    controller = AdaptiveController(
        AdaptiveConfig(short_pause_seconds=10.0, pause_jitter_seconds=0.0),
        now=clock,
        rng=lambda: 0.0,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP request expected while the limiter denies")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        KeyAdapter(),
        ["1"],
        client=client,
        limiter=limiter,
        token_id="token-a",
        adaptive=controller,
        sleep=clock.advance,
        now=clock,
    )
    assert controller.snapshot()["drops"] == 1
    assert controller.snapshot()["pauses"] == 1
    assert limiter.paused_until("graphql", "token-a") == clock.value + 10.0
    assert "1" in outcome.unresolved
    assert "ThrottledError" in outcome.unresolved["1"]


def test_controller_snapshot_lands_in_batch_stats() -> None:
    clock = FakeClock()
    controller = AdaptiveController(AdaptiveConfig(), now=clock, rng=lambda: 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return ok_response(keys_in(request))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(KeyAdapter(), ["1", "2"], client=client, adaptive=controller, now=clock)
    stats = outcome.stats.as_dict()
    assert stats["deferred"] == 0
    assert stats["adaptive"]["window"] == 20
    assert stats["adaptive"]["drops"] == 0
