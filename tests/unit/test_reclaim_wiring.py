from __future__ import annotations

import fakeredis
import httpx
import pytest
from alembic import command

from discover.pipeline import Deps, run_search_discovery
from lib.deadlines import DeadlineExceededError
from scheduler.shard_planner import ShardSpec
from scheduler.state_machine import ShardQueue, ShardState, ShardStore


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


def handler(request: httpx.Request) -> httpx.Response:
    query = request.url.params.get("q", "")
    if query == "stars:>0":
        return httpx.Response(
            200, json={"total_count": 1, "incomplete_results": False, "items": []}
        )
    # planner probe for the outer query: no shards to plan
    return httpx.Response(200, json={"total_count": 0, "incomplete_results": False, "items": []})


@pytest.fixture()
def reclaim_immediately(monkeypatch):
    # reclaim immediately instead of waiting the production 60s idle window
    original = ShardQueue.reclaim_stale
    monkeypatch.setattr(
        ShardQueue,
        "reclaim_stale",
        lambda self, consumer, **kwargs: original(self, consumer, min_idle_ms=0, **kwargs),
    )


def make_deps(engine, redis, handler_fn=handler) -> Deps:
    return Deps(
        client=httpx.Client(transport=httpx.MockTransport(handler_fn)),
        engine=engine,
        redis=redis,
        limiter=None,
        token_fp="t",
        audit_buffer=None,
    )


def dead_consumer_delivery(store, queue, *, state=ShardState.PENDING, query="stars:>0"):
    shard_id = store.create(ShardSpec(query=query, range_start=None, range_end=None, total_count=1))
    if state is not ShardState.PENDING:
        store.set_state(shard_id, ShardState.ACTIVE)
        if state is not ShardState.ACTIVE:
            store.set_state(shard_id, state)
    queue.enqueue(shard_id)
    queue.claim("gitcrawl-dead", count=1)  # delivered to a consumer that died
    return shard_id


def test_discovery_reclaims_and_processes_a_dead_consumers_shard(clean_db, reclaim_immediately):
    engine = clean_db()
    redis = fakeredis.FakeRedis()
    store = ShardStore(engine)
    shard_id = dead_consumer_delivery(store, ShardQueue(redis))
    queue = ShardQueue(redis)
    assert queue.pel_size() == 1

    run_search_discovery(make_deps(engine, redis), "stars:>9999999", max_shards=1)

    assert queue.pel_size() == 0  # reclaimed, processed, and acked
    assert store.get(shard_id).state in {"done", "incomplete"}


def test_discovery_resumes_a_shard_left_active_by_a_dead_consumer(clean_db, reclaim_immediately):
    engine = clean_db()
    redis = fakeredis.FakeRedis()
    store = ShardStore(engine)
    shard_id = dead_consumer_delivery(store, ShardQueue(redis), state=ShardState.ACTIVE)
    queue = ShardQueue(redis)

    run_search_discovery(make_deps(engine, redis), "stars:>9999999", max_shards=1)

    assert queue.pel_size() == 0
    assert store.get(shard_id).state in {"done", "incomplete"}


@pytest.mark.parametrize("state", [ShardState.DONE, ShardState.INCOMPLETE])
def test_discovery_acks_a_redelivered_shard_already_finished(clean_db, reclaim_immediately, state):
    engine = clean_db()
    redis = fakeredis.FakeRedis()
    store = ShardStore(engine)
    shard_id = dead_consumer_delivery(store, ShardQueue(redis), state=state)
    queue = ShardQueue(redis)

    run_search_discovery(make_deps(engine, redis), "stars:>9999999", max_shards=1)

    assert queue.pel_size() == 0
    assert store.get(shard_id).state is state


def test_queue_prefix_scopes_discovery_to_its_own_messages(clean_db, reclaim_immediately):
    engine = clean_db()
    redis = fakeredis.FakeRedis()
    store = ShardStore(engine)
    other = dead_consumer_delivery(
        store, ShardQueue(redis, prefix="gitcrawl:shards:run:1"), state=ShardState.ACTIVE
    )
    own_queue = ShardQueue(redis, prefix="gitcrawl:shards:run:2")

    run_search_discovery(
        make_deps(engine, redis),
        "stars:>9999999",
        max_shards=1,
        queue_prefix="gitcrawl:shards:run:2",
    )

    assert own_queue.pel_size() == 0
    assert ShardQueue(redis, prefix="gitcrawl:shards:run:1").pel_size() == 1
    assert store.get(other).state is ShardState.ACTIVE


def partial_results_handler(request: httpx.Request) -> httpx.Response:
    if request.url.params.get("q") == "stars:>0":
        return httpx.Response(
            200,
            json={"total_count": 1, "incomplete_results": False, "items": []},
            headers={"x-github-sso": "required; partial-results"},
        )
    return httpx.Response(200, json={"total_count": 0, "incomplete_results": False, "items": []})


def test_transient_partial_results_is_retried_and_recovers(clean_db, reclaim_immediately):
    calls = {"n": 0}

    def flaky_handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("q") == "stars:>0":
            calls["n"] += 1
            headers = {"x-github-sso": "required; partial-results"} if calls["n"] == 1 else {}
            return httpx.Response(
                200,
                json={"total_count": 1, "incomplete_results": False, "items": []},
                headers=headers,
            )
        return httpx.Response(
            200, json={"total_count": 0, "incomplete_results": False, "items": []}
        )

    engine = clean_db()
    redis = fakeredis.FakeRedis()
    store = ShardStore(engine)
    shard_id = dead_consumer_delivery(store, ShardQueue(redis), state=ShardState.ACTIVE)
    queue = ShardQueue(redis)

    stats = run_search_discovery(
        make_deps(engine, redis, flaky_handler), "stars:>9999999", max_shards=1
    )

    assert stats.incomplete_shards == 0
    assert store.get(shard_id).state is ShardState.DONE
    assert queue.pel_size() == 0


def test_persistent_partial_results_marks_the_shard_incomplete(clean_db, reclaim_immediately):
    engine = clean_db()
    redis = fakeredis.FakeRedis()
    store = ShardStore(engine)
    shard_id = dead_consumer_delivery(store, ShardQueue(redis), state=ShardState.ACTIVE)

    stats = run_search_discovery(
        make_deps(engine, redis, partial_results_handler), "stars:>9999999", max_shards=1
    )

    assert stats.incomplete_shards == 1
    assert store.get(shard_id).state is ShardState.INCOMPLETE
    assert redis.xlen("gitcrawl:shards:dlq") == 1


def deferral_handler(request: httpx.Request) -> httpx.Response:
    query = request.url.params.get("q", "")
    if query == "stars:>9999999":
        return httpx.Response(
            200, json={"total_count": 0, "incomplete_results": False, "items": []}
        )
    if query == "boom:0":
        return httpx.Response(500, json={"message": "boom"})
    return httpx.Response(200, json={"total_count": 1, "incomplete_results": False, "items": []})


def test_transient_failure_does_not_strand_the_other_shards(clean_db):
    engine = clean_db()
    redis = fakeredis.FakeRedis()
    store = ShardStore(engine)
    failing = store.create(
        ShardSpec(query="boom:0", range_start=None, range_end=None, total_count=1)
    )
    good = store.create(
        ShardSpec(query="stars:>0", range_start=None, range_end=None, total_count=1)
    )
    queue = ShardQueue(redis)
    queue.enqueue(failing)
    queue.enqueue(good)

    stats = run_search_discovery(
        make_deps(engine, redis, deferral_handler),
        "stars:>9999999",
        max_shards=1,
        sleep=lambda _: None,
    )

    assert stats.deferred_shards == 1
    assert stats.incomplete_shards == 0
    assert store.get(good).state is ShardState.DONE
    assert store.get(failing).state is ShardState.PENDING
    claimed = queue.claim("probe", count=10)
    assert [(item.shard_id, item.attempts) for item in claimed] == [(failing, 2)]


def always_partial_handler(request: httpx.Request) -> httpx.Response:
    query = request.url.params.get("q", "")
    if query == "stars:>9999999":
        return httpx.Response(
            200, json={"total_count": 0, "incomplete_results": False, "items": []}
        )
    return httpx.Response(
        200,
        json={"total_count": 1, "incomplete_results": False, "items": []},
        headers={"x-github-sso": "required; partial-results"},
    )


def test_consecutive_deferrals_trip_the_circuit_breaker(clean_db):
    engine = clean_db()
    redis = fakeredis.FakeRedis()
    store = ShardStore(engine)
    queue = ShardQueue(redis)
    shards = [
        store.create(ShardSpec(query="boom:0", range_start=None, range_end=None, total_count=1))
        for _ in range(4)
    ]
    for shard_id in shards:
        queue.enqueue(shard_id)

    stats = run_search_discovery(
        make_deps(engine, redis, always_partial_handler), "stars:>9999999", max_shards=1
    )

    assert stats.deferred_shards == 3
    assert store.get(shards[3]).state is ShardState.PENDING
    claimed = queue.claim("probe", count=10)
    assert {item.shard_id for item in claimed} == set(shards)


def test_deadline_during_a_shard_defers_instead_of_failing(clean_db, monkeypatch):
    from discover import pipeline

    def raise_deadline(*_args, **_kwargs):
        raise DeadlineExceededError(5.0)

    monkeypatch.setattr(pipeline, "iter_shard_pages", raise_deadline)
    engine = clean_db()
    redis = fakeredis.FakeRedis()
    store = ShardStore(engine)
    shard_id = store.create(
        ShardSpec(query="stars:>0", range_start=None, range_end=None, total_count=1)
    )
    queue = ShardQueue(redis)
    queue.enqueue(shard_id)

    stats = run_search_discovery(
        make_deps(engine, redis, deferral_handler), "stars:>9999999", max_shards=1
    )

    assert stats.deadline_hit is True
    assert store.get(shard_id).state is ShardState.PENDING
    assert queue.pel_size() == 1


def test_deadline_during_planning_returns_partial_without_failing(clean_db, monkeypatch):
    from discover import pipeline

    def raise_deadline(*_args, **_kwargs):
        raise DeadlineExceededError(5.0)

    monkeypatch.setattr(pipeline, "count_total", raise_deadline)
    engine = clean_db()
    redis = fakeredis.FakeRedis()

    stats = run_search_discovery(
        make_deps(engine, redis, deferral_handler), "stars:>9999999", max_shards=1
    )

    assert stats.deadline_hit is True
    assert stats.shards == 0


def test_run_filter_surfaces_deferred_and_deadline_discovery(clean_db, monkeypatch):
    from discover import pipeline
    from discover.pipeline import DiscoveryStats
    from serve.filter_spec import parse_filter_spec
    from serve.runner import run_filter

    engine = clean_db()
    monkeypatch.setattr(
        pipeline,
        "run_search_discovery",
        lambda *_args, **_kwargs: DiscoveryStats(deferred_shards=2, deadline_hit=True, skipped=3),
    )
    monkeypatch.setattr(pipeline, "count_total", lambda deps, query, **kwargs: 40)
    deps = Deps(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        engine=engine,
        redis=None,
        limiter=None,
        token_fp="t",
        audit_buffer=None,
    )

    payload = run_filter(deps, parse_filter_spec({"gitcrawl_filter": 1, "q": "language:rust"}))

    assert payload.incomplete is True
    joined = " ".join(payload.warnings)
    assert "deferred for retry" in joined
    assert "deadline was reached" in joined
    assert "could not be saved" in joined
    assert "collected 0 unique repos of ~40" in joined


def test_run_filter_forwards_the_queue_prefix_to_discovery(clean_db, monkeypatch):
    from discover import pipeline
    from serve.filter_spec import parse_filter_spec
    from serve.runner import run_filter

    engine = clean_db()
    captured: dict = {}
    real = pipeline.run_search_discovery

    def recording(deps, query, **kwargs):
        captured.update(kwargs)
        return real(deps, query, **kwargs)

    monkeypatch.setattr(pipeline, "run_search_discovery", recording)
    monkeypatch.setattr(pipeline, "count_total", lambda deps, query, **kwargs: 0)
    deps = Deps(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        engine=engine,
        redis=None,
        limiter=None,
        token_fp="t",
        audit_buffer=None,
    )

    run_filter(
        deps,
        parse_filter_spec({"gitcrawl_filter": 1, "q": "language:rust"}),
        queue_prefix="gitcrawl:shards:run:5",
    )

    assert captured["queue_prefix"] == "gitcrawl:shards:run:5"
