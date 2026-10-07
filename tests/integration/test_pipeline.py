from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from datetime import date
from urllib.parse import parse_qs, urlencode, urlparse

import fakeredis
import httpx
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

import lib.audit as audit_module
from discover.pipeline import (
    Deps,
    DiscoveryStats,
    run_org_enum,
    run_search_discovery,
    run_since_scan,
)
from discover.search_shards import RequestFailed, iter_shard_pages
from lib.gh_client import PartialResultsError
from scheduler.state_machine import ShardQueue

SEARCH_HEADERS = {"x-ratelimit-resource": "search"}
NEXT_PAGE_BASE = "https://api.github.com/search/repositories"


def repo_item(repo_id: int, owner_login: str | None = None) -> dict:
    login = owner_login or f"owner{repo_id}"
    return {
        "id": repo_id,
        "node_id": f"R_{repo_id}",
        "name": f"repo{repo_id}",
        "full_name": f"{login}/repo{repo_id}",
        "owner": {"id": repo_id * 10, "login": login, "type": "User"},
        "private": False,
    }


def scalar(engine: Engine, sql: str):
    with engine.connect() as connection:
        return connection.scalar(text(sql))


def params_of(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(urlparse(str(request.url)).query)


def q_of(request: httpx.Request) -> str:
    return params_of(request).get("q", [""])[0]


def span_days(query: str) -> int | None:
    tokens = [token for token in query.split() if token.startswith("created:")]
    if not tokens:
        return None
    start_raw, _, end_raw = tokens[-1][len("created:") :].partition("..")
    return (date.fromisoformat(end_raw) - date.fromisoformat(start_raw)).days


def range_start(query: str) -> date | None:
    tokens = [token for token in query.split() if token.startswith("created:")]
    if not tokens:
        return None
    return date.fromisoformat(tokens[-1][len("created:") :].partition("..")[0])


def count_response(total: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={"total_count": total, "items": [], "incomplete_results": False},
        headers=SEARCH_HEADERS,
    )


def page_response(items, *, incomplete: bool = False, next_url: str | None = None):
    headers = dict(SEARCH_HEADERS)
    if next_url is not None:
        headers["Link"] = f'<{next_url}>; rel="next"'
    return httpx.Response(
        200,
        json={
            "total_count": len(items),
            "items": items,
            "incomplete_results": incomplete,
        },
        headers=headers,
    )


def cap_response() -> httpx.Response:
    return httpx.Response(
        422,
        json={"message": "Only the first 1000 search results are available"},
        headers=SEARCH_HEADERS,
    )


def next_page_url(query: str, page: int) -> str:
    return f"{NEXT_PAGE_BASE}?{urlencode({'q': query, 'per_page': 100, 'page': page})}"


def scripted_client(
    handler: Callable[[httpx.Request], httpx.Response], requests: list
) -> httpx.Client:
    def wrapped(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(wrapped))


def make_deps(engine: Engine, client: httpx.Client, redis=None, token_fp: str = "test-fp") -> Deps:
    return Deps(client=client, engine=engine, redis=redis, limiter=None, token_fp=token_fp)


def shard_state(engine: Engine, shard_id: int) -> tuple[str, bool]:
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT state, incomplete FROM shards WHERE id = :id"), {"id": shard_id}
        ).one()
    return str(row[0]), bool(row[1])


def queue_is_empty(redis) -> bool:
    queue = ShardQueue(redis)
    if queue.claim("probe", count=10):
        return False
    return not any(redis.xlen(key) > 0 for key in redis.scan_iter("gitcrawl:shards:*"))


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(clean_db):
    return clean_db()


@pytest.fixture()
def redis():
    return fakeredis.FakeRedis()


def test_search_discovery_bisects_upserts_audits_and_drains_queue(clean: Engine, redis):
    requests: list = []
    page_items: dict[str, list[dict]] = {}
    next_id = [1000]

    def count_for(query: str) -> int:
        span = span_days(query)
        if span is None or span >= 3000:
            return 2500
        return 600

    def handler(request: httpx.Request):
        query = q_of(request)
        if params_of(request).get("per_page") == ["1"]:
            return count_response(count_for(query))
        if query not in page_items:
            page_items[query] = [repo_item(next_id[0]), repo_item(next_id[0] + 1)]
            next_id[0] += 2
        return page_response(page_items[query])

    deps = make_deps(clean, scripted_client(handler, requests), redis)
    stats = run_search_discovery(deps, "language:python", jitter=lambda: 0.0)

    assert stats == DiscoveryStats(
        shards=4,
        pages=4,
        fetched=8,
        inserted=8,
        repo_ids=(1000, 1001, 1002, 1003, 1004, 1005, 1006, 1007),
    )
    with clean.connect() as connection:
        states = dict(
            connection.execute(text("SELECT state, count(*) FROM shards GROUP BY state")).all()
        )
        assert states == {"done": 4}
        assert connection.scalar(text("SELECT count(*) FROM repos")) == 8
        assert connection.scalar(text("SELECT count(*) FROM audit_log")) == len(requests) == 11
        audit = (
            connection.execute(
                text("SELECT params, token_fp, query_hash FROM audit_log ORDER BY id LIMIT 1")
            )
            .mappings()
            .one()
        )
    assert audit["token_fp"] == "test-fp"
    assert audit["query_hash"] == audit_module.query_hash(audit["params"])
    assert audit["params"]["q"] == "language:python"
    assert audit["params"]["per_page"] == 1
    page_queries = [
        q_of(request) for request in requests if params_of(request).get("per_page") == ["100"]
    ]
    assert all("created:" in query for query in page_queries)
    assert queue_is_empty(redis)


def test_cap_422_splits_shard_into_two_subshards(clean: Engine, redis):
    requests: list = []
    page_requests: list = []

    def count_for(query: str) -> int:
        if span_days(query) is None:
            return 1500
        if range_start(query) == date(2008, 1, 1):
            return 0
        return 500

    def handler(request: httpx.Request):
        query = q_of(request)
        if params_of(request).get("per_page") == ["1"]:
            return count_response(count_for(query))
        page_requests.append(query)
        if len(page_requests) == 1:
            return cap_response()
        return page_response([repo_item(2000 + len(page_requests))])

    deps = make_deps(clean, scripted_client(handler, requests), redis)
    stats = run_search_discovery(deps, "language:python", jitter=lambda: 0.0)

    assert stats == DiscoveryStats(
        shards=3,
        pages=2,
        fetched=2,
        inserted=2,
        cap_splits=1,
        repo_ids=(2002, 2003),
    )
    assert len(page_requests) == 3
    assert len(set(page_requests)) == 3
    with clean.connect() as connection:
        rows = connection.execute(text("SELECT id, state, query FROM shards ORDER BY id")).all()
        assert [row[1] for row in rows] == ["done", "done", "done"]
        original = str(rows[0][2])
        assert rows[1][2] != original and rows[2][2] != original
        assert connection.scalar(text("SELECT count(*) FROM repos")) == 2
        assert connection.scalar(text("SELECT count(*) FROM audit_log")) == len(requests) == 6
    assert page_requests[0] == original
    assert all("created:" in query for query in page_requests)
    assert queue_is_empty(redis)


def test_incomplete_results_narrow_once_then_mark_incomplete(clean: Engine, redis):
    requests: list = []
    page_requests: list[tuple[str, dict]] = []

    def count_for(query: str) -> int:
        if span_days(query) is None:
            return 1500
        if range_start(query) == date(2008, 1, 1):
            return 0
        return 500

    def handler(request: httpx.Request):
        query = q_of(request)
        params = params_of(request)
        if params.get("per_page") == ["1"]:
            return count_response(count_for(query))
        page_requests.append((query, params))
        if len(page_requests) == 1:
            return page_response(
                [repo_item(3000)], incomplete=True, next_url=next_page_url(query, 2)
            )
        if params.get("page") == ["2"]:
            return page_response([repo_item(3001)], incomplete=True)
        return page_response([repo_item(3000 + len(page_requests))])

    deps = make_deps(clean, scripted_client(handler, requests), redis)
    stats = run_search_discovery(deps, "language:python", jitter=lambda: 0.0)

    assert stats == DiscoveryStats(
        shards=3,
        pages=4,
        fetched=4,
        inserted=4,
        incomplete_shards=1,
        repo_ids=(3000, 3001, 3003, 3004),
    )
    with clean.connect() as connection:
        rows = connection.execute(
            text("SELECT id, state, incomplete FROM shards ORDER BY id")
        ).all()
    assert rows[0][1] == "incomplete" and rows[0][2] is True
    assert rows[1][1] == "done" and rows[2][1] == "done"
    initial_query = page_requests[0][0]
    assert [query for query, _ in page_requests].count(initial_query) == 2
    assert len({query for query, _ in page_requests}) == 3
    assert scalar(clean, "SELECT count(*) FROM audit_log") == len(requests) == 7
    assert queue_is_empty(redis)


def test_max_shards_bound_creates_only_cap(clean: Engine, redis):
    requests: list = []
    page_requests: list = []

    def count_for(query: str) -> int:
        return 1600 if span_days(query) is None else 500

    def handler(request: httpx.Request):
        query = q_of(request)
        if params_of(request).get("per_page") == ["1"]:
            return count_response(count_for(query))
        page_requests.append(query)
        return page_response([repo_item(4000 + len(page_requests))])

    deps = make_deps(clean, scripted_client(handler, requests), redis)
    stats = run_search_discovery(deps, "topic:ai", max_shards=1, jitter=lambda: 0.0)

    assert stats == DiscoveryStats(
        shards=1, pages=1, fetched=1, inserted=1, repo_ids=(4001,), plan_capped=True
    )
    assert len(page_requests) == 1
    assert scalar(clean, "SELECT count(*) FROM shards") == 1
    assert scalar(clean, "SELECT state FROM shards") == "done"
    assert queue_is_empty(redis)


def test_page_cap_marks_the_shard_incomplete_and_counts_the_capped_page(clean: Engine, redis):
    requests: list = []

    def handler(request: httpx.Request):
        query = q_of(request)
        if params_of(request).get("per_page") == ["1"]:
            return count_response(50)
        return page_response([repo_item(6000)], next_url=next_page_url(query, 2))

    deps = make_deps(clean, scripted_client(handler, requests), redis)
    stats = run_search_discovery(deps, "topic:ai", max_pages=1, jitter=lambda: 0.0)

    assert stats == DiscoveryStats(
        shards=1,
        pages=1,
        fetched=1,
        inserted=1,
        incomplete_shards=1,
        page_capped_shards=1,
        repo_ids=(6000,),
    )
    assert scalar(clean, "SELECT state FROM shards") == "incomplete"


def test_non_object_count_payload_raises_request_failed(clean: Engine):
    def handler(request: httpx.Request):
        return httpx.Response(200, json=[1, 2, 3], headers=SEARCH_HEADERS)

    deps = make_deps(clean, scripted_client(handler, []))
    with pytest.raises(RequestFailed):
        run_search_discovery(deps, "language:python", jitter=lambda: 0.0)


def test_non_200_exhaustion_raises_request_failed(clean: Engine):
    requests: list = []
    sleeps: list[float] = []

    def handler(request: httpx.Request):
        return httpx.Response(500, json={"message": "boom"})

    deps = make_deps(clean, scripted_client(handler, requests))
    with pytest.raises(RequestFailed) as excinfo:
        run_search_discovery(
            deps,
            "language:python",
            sleep=sleeps.append,
            now=lambda: 1000.0,
            jitter=lambda: 0.0,
        )
    assert excinfo.value.status == 500
    assert excinfo.value.message == "boom"
    assert len(requests) == 5
    assert len(sleeps) == 4
    assert scalar(clean, "SELECT count(*) FROM audit_log") == 5


def test_planner_respects_page_budget_when_max_shards_allows(clean: Engine, redis):
    def handler(request: httpx.Request):
        if params_of(request).get("per_page") == ["1"]:
            query = q_of(request)
            return count_response(800 if "created:" not in query else 100)
        return page_response([])

    deps = make_deps(clean, scripted_client(handler, []), redis)
    stats = run_search_discovery(deps, "topic:ai", max_shards=10, max_pages=3, jitter=lambda: 0.0)

    assert stats.shards == 2
    assert stats.plan_capped is False


def test_planner_keeps_the_1000_target_when_shards_are_scarce(clean: Engine, redis):
    def handler(request: httpx.Request):
        if params_of(request).get("per_page") == ["1"]:
            return count_response(800)
        return page_response([])

    deps = make_deps(clean, scripted_client(handler, []), redis)
    stats = run_search_discovery(deps, "topic:ai", max_shards=1, max_pages=3, jitter=lambda: 0.0)

    assert stats.shards == 1


def test_zero_count_query_creates_no_shards(clean: Engine):
    requests: list = []

    def handler(request: httpx.Request):
        return count_response(0)

    deps = make_deps(clean, scripted_client(handler, requests))
    stats = run_search_discovery(deps, "language:python", jitter=lambda: 0.0)
    assert stats == DiscoveryStats()
    assert scalar(clean, "SELECT count(*) FROM shards") == 0
    assert len(requests) == 1


def test_sequential_path_without_queue(clean: Engine):
    requests: list = []
    page_requests: list = []

    def count_for(query: str) -> int:
        return 1600 if span_days(query) is None else 500

    def handler(request: httpx.Request):
        query = q_of(request)
        if params_of(request).get("per_page") == ["1"]:
            return count_response(count_for(query))
        page_requests.append(query)
        return page_response([repo_item(5000 + len(page_requests))])

    deps = make_deps(clean, scripted_client(handler, requests), redis=None)
    stats = run_search_discovery(deps, "topic:ai", jitter=lambda: 0.0)

    assert stats == DiscoveryStats(shards=2, pages=2, fetched=2, inserted=2, repo_ids=(5001, 5002))
    assert len(page_requests) == 2
    with clean.connect() as connection:
        states = dict(
            connection.execute(text("SELECT state, count(*) FROM shards GROUP BY state")).all()
        )
    assert states == {"done": 2}


def test_since_scan_upserts_and_advances_checkpoint(clean: Engine):
    with clean.begin() as connection:
        shard_id = connection.execute(
            text("INSERT INTO shards (kind) VALUES ('since-range') RETURNING id")
        ).scalar_one()
    requests: list = []
    next_url = "https://api.github.com/repositories?since=20&per_page=100"

    def handler(request: httpx.Request):
        if params_of(request).get("since") == ["3"]:
            return httpx.Response(
                200,
                json=[repo_item(5), repo_item(15)],
                headers={"Link": f'<{next_url}>; rel="next"'},
            )
        return httpx.Response(200, json=[repo_item(20)])

    deps = make_deps(clean, scripted_client(handler, requests))
    stats = run_since_scan(
        deps,
        since=3,
        max_pages=5,
        checkpoint_shard_id=shard_id,
        jitter=lambda: 0.0,
    )

    assert stats == DiscoveryStats(pages=2, fetched=3, inserted=3)
    with clean.connect() as connection:
        assert (
            connection.execute(
                text("SELECT since_max FROM shards WHERE id = :id"), {"id": shard_id}
            ).scalar_one()
            == 20
        )
        assert connection.scalar(text("SELECT count(*) FROM repos")) == 3
        assert connection.scalar(text("SELECT count(*) FROM audit_log")) == 2


def test_org_enum_upserts_repos(clean: Engine):
    requests: list = []

    def handler(request: httpx.Request):
        assert urlparse(str(request.url)).path == "/orgs/acme/repos"
        assert params_of(request) == {"type": ["all"], "per_page": ["100"]}
        return httpx.Response(200, json=[repo_item(42, owner_login="acme")])

    deps = make_deps(clean, scripted_client(handler, requests))
    stats = run_org_enum(deps, org="acme", repo_type="all", jitter=lambda: 0.0)

    assert stats == DiscoveryStats(pages=1, fetched=1, inserted=1)
    assert scalar(clean, "SELECT count(*) FROM repos") == 1
    assert scalar(clean, "SELECT count(*) FROM shards") == 0
    assert scalar(clean, "SELECT count(*) FROM audit_log") == 1


def test_user_enum_upserts_repos(clean: Engine):
    requests: list = []

    def handler(request: httpx.Request):
        assert urlparse(str(request.url)).path == "/users/octocat/repos"
        assert params_of(request) == {"type": ["all"], "per_page": ["100"]}
        return httpx.Response(200, json=[repo_item(43, owner_login="octocat")])

    deps = make_deps(clean, scripted_client(handler, requests))
    stats = run_org_enum(deps, user="octocat", repo_type="all", jitter=lambda: 0.0)

    assert stats == DiscoveryStats(pages=1, fetched=1, inserted=1)
    assert scalar(clean, "SELECT count(*) FROM repos") == 1


def test_org_enum_requires_exactly_one_scope(clean: Engine):
    deps = make_deps(clean, scripted_client(lambda request: httpx.Response(500), []))
    with pytest.raises(ValueError):
        run_org_enum(deps)
    with pytest.raises(ValueError):
        run_org_enum(deps, org="acme", user="octocat")


def test_audit_hook_exception_propagates(clean: Engine, monkeypatch):
    requests: list = []

    def boom(engine, record):
        raise RuntimeError("audit sink down")

    monkeypatch.setattr(audit_module, "record_audit", boom)

    def handler(request: httpx.Request):
        return httpx.Response(200, json=[repo_item(44, owner_login="acme")])

    deps = make_deps(clean, scripted_client(handler, requests))
    with pytest.raises(RuntimeError, match="audit sink down"):
        run_org_enum(deps, org="acme", jitter=lambda: 0.0)
    assert len(requests) == 1


def test_sso_partial_results_aborts_shard_pagination():
    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={"total_count": 1, "items": [repo_item(1)], "incomplete_results": False},
            headers={"x-github-sso": "required; partial-results"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(PartialResultsError):
        list(iter_shard_pages(client, "language:python", now=lambda: 1000.0))


def test_run_emits_single_info_summary(clean: Engine, caplog):
    requests: list = []

    def handler(request: httpx.Request):
        return httpx.Response(200, json=[repo_item(45, owner_login="acme")])

    deps = make_deps(clean, scripted_client(handler, requests))
    with caplog.at_level(logging.INFO, logger="gitcrawl.discover"):
        run_org_enum(deps, org="acme", jitter=lambda: 0.0)
    records = [record for record in caplog.records if record.name == "gitcrawl.discover"]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert records[0].getMessage().startswith("discovery run kind=org")
    assert "inserted=1" in records[0].getMessage()


def test_deps_defaults(clean: Engine):
    client = scripted_client(lambda request: httpx.Response(500), [])
    deps = Deps(client=client, engine=clean)
    assert deps.redis is None
    assert deps.limiter is None
    assert deps.token_fp == "anonymous"
    assert deps.audit_buffer is None


def test_audit_buffer_defers_record_audit_until_run_end(clean: Engine, monkeypatch):
    from lib.audit import AuditBuffer

    def boom(engine, record):
        raise RuntimeError("record_audit should not run while a buffer is wired")

    monkeypatch.setattr(audit_module, "record_audit", boom)

    def handler(request: httpx.Request):
        return httpx.Response(200, json=[repo_item(46, owner_login="acme")])

    deps = dataclasses.replace(
        make_deps(clean, scripted_client(handler, [])),
        audit_buffer=AuditBuffer(clean, batch_size=100),
    )
    stats = run_org_enum(deps, org="acme", jitter=lambda: 0.0)

    assert stats.fetched == 1
    assert scalar(clean, "SELECT count(*) FROM audit_log") == 1


def test_request_failed_shard_is_rolled_back_and_queued_for_retry(clean: Engine, redis):
    def handler(request: httpx.Request):
        if params_of(request).get("per_page") == ["1"]:
            return count_response(500)
        return httpx.Response(500, json={"message": "boom"})

    deps = make_deps(clean, scripted_client(handler, []), redis)
    stats = run_search_discovery(
        deps,
        "language:python",
        sleep=lambda _: None,
        now=lambda: 1000.0,
        jitter=lambda: 0.0,
    )

    assert stats.fetched == 0
    assert stats.deferred_shards == 1
    assert shard_state(clean, 1) == ("pending", False)
    claimed = ShardQueue(redis, lanes=4).claim("probe", count=10)
    assert [(item.shard_id, item.attempts) for item in claimed] == [(1, 2)]


def test_poison_shard_reaches_the_dlq_after_max_attempts(clean: Engine, redis):
    def handler(request: httpx.Request):
        if params_of(request).get("per_page") == ["1"]:
            return count_response(500)
        return httpx.Response(500, json={"message": "boom"})

    deps = make_deps(clean, scripted_client(handler, []), redis)
    stats = None
    for _ in range(3):
        stats = run_search_discovery(
            deps,
            "language:python",
            sleep=lambda _: None,
            now=lambda: 1000.0,
            jitter=lambda: 0.0,
        )

    assert redis.xlen("gitcrawl:shards:dlq") >= 1
    dlq_shards = {entry[1][b"shard_id"] for entry in redis.xrange("gitcrawl:shards:dlq")}
    assert b"1" in dlq_shards
    assert shard_state(clean, 1) == ("incomplete", True)
    assert stats is not None and stats.incomplete_shards == 1


def test_discovery_honours_cancellation(clean: Engine, redis):
    from lib import cancellation

    def handler(request: httpx.Request):
        return count_response(1600)

    deps = make_deps(clean, scripted_client(handler, []), redis)
    token = cancellation.bind(lambda: True)
    try:
        with pytest.raises(cancellation.RunCancelled):
            run_search_discovery(deps, "topic:ai", jitter=lambda: 0.0)
    finally:
        cancellation.reset(token)
