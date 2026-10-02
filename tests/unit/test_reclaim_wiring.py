from __future__ import annotations

import fakeredis
import httpx

from discover.pipeline import Deps, run_search_discovery
from scheduler.shard_planner import ShardSpec
from scheduler.state_machine import ShardQueue, ShardStore


def handler(request: httpx.Request) -> httpx.Response:
    query = request.url.params.get("q", "")
    if query == "stars:>0":
        return httpx.Response(
            200, json={"total_count": 1, "incomplete_results": False, "items": []}
        )
    # planner probe for the outer query: no shards to plan
    return httpx.Response(200, json={"total_count": 0, "incomplete_results": False, "items": []})


def test_discovery_reclaims_and_processes_a_dead_consumers_shard(clean_db, monkeypatch):
    engine = clean_db()
    redis = fakeredis.FakeRedis()
    store = ShardStore(engine)
    shard_id = store.create(
        ShardSpec(query="stars:>0", range_start=None, range_end=None, total_count=1)
    )
    queue = ShardQueue(redis)
    queue.enqueue(shard_id)
    queue.claim("gitcrawl-dead", count=1)  # delivered to a consumer that died
    assert queue.pel_size() == 1

    # reclaim immediately in the test instead of waiting the production 60s idle window
    original = ShardQueue.reclaim_stale
    monkeypatch.setattr(
        ShardQueue,
        "reclaim_stale",
        lambda self, consumer, **kwargs: original(self, consumer, min_idle_ms=0, **kwargs),
    )

    deps = Deps(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        engine=engine,
        redis=redis,
        limiter=None,
        token_fp="t",
        audit_buffer=None,
    )
    run_search_discovery(deps, "stars:>9999999", max_shards=1)

    assert queue.pel_size() == 0  # reclaimed, processed, and acked
    assert store.get(shard_id).state in {"done", "incomplete"}
