from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from datetime import date
from urllib.parse import parse_qs, urlparse

import fakeredis
import httpx
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

import lib.audit as audit_module
from discover import pipeline
from discover.pipeline import (
    Deps,
    DiscoveryStats,
    count_total,
    run_org_enum,
    run_search_discovery,
    run_since_scan,
)
from lib import cancellation
from lib.deadlines import Deadline, DeadlineExceededError
from lib.gh_client import RequestFailed
from limiter.buckets import BucketLimiter

SEARCH_ARG_RE = re.compile(r'search\(first: (?P<first>\d+), query: (?P<query>"(?:[^"\\]|\\.)*")')


def repo_node(repo_id: int, owner_login: str | None = None) -> dict:
    login = owner_login or f"owner{repo_id}"
    return {
        "databaseId": repo_id,
        "id": f"R_{repo_id}",
        "name": f"repo{repo_id}",
        "nameWithOwner": f"{login}/repo{repo_id}",
        "owner": {"databaseId": repo_id * 10, "login": login, "__typename": "User"},
        "createdAt": "2020-01-01T00:00:00Z",
        "pushedAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
    }


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


def graphql_text(request: httpx.Request) -> str:
    return json.loads(request.content)["query"]


def search_query_of(request: httpx.Request) -> str:
    match = SEARCH_ARG_RE.search(graphql_text(request))
    return json.loads(match.group("query")) if match else ""


def is_count(request: httpx.Request) -> bool:
    return "search(first: 1," in graphql_text(request)


def count_payload(request: httpx.Request, total: int) -> httpx.Response:
    aliases = re.findall(r"(s\d+): search\(first: 1", graphql_text(request))
    data: dict[str, object] = {alias: {"repositoryCount": total} for alias in aliases}
    data["rateLimit"] = {"cost": 1, "remaining": 5000}
    return httpx.Response(200, json={"data": data})


def page_payload(
    items,
    *,
    total: int | None = None,
    has_next: bool = False,
    cursor: str | None = None,
    errors: list[dict] | None = None,
) -> httpx.Response:
    nodes = [repo_node(item) if isinstance(item, int) else item for item in items]
    search = {
        "repositoryCount": len(nodes) if total is None else total,
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": nodes,
    }
    payload: dict[str, object] = {
        "data": {"s": search, "rateLimit": {"cost": 1, "remaining": 5000}}
    }
    if errors:
        payload["errors"] = errors
    return httpx.Response(200, json=payload)


def transient_payload() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": {"rateLimit": {"cost": 1, "remaining": 5000}},
            "errors": [{"message": "Something went wrong while executing your query"}],
        },
    )


def span_days(query: str) -> int:
    match = re.search(r"created:(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", query)
    if match is None:
        return 10**9
    return (date.fromisoformat(match.group(2)) - date.fromisoformat(match.group(1))).days


def scalar(engine: Engine, sql: str):
    with engine.connect() as connection:
        return connection.scalar(text(sql))


def scripted_client(
    handler: Callable[[httpx.Request], httpx.Response], requests: list
) -> httpx.Client:
    def wrapped(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(wrapped))


def make_deps(
    engine: Engine,
    client: httpx.Client,
    *,
    limiter: BucketLimiter | None = None,
    token_fp: str = "test-fp",
) -> Deps:
    return Deps(client=client, engine=engine, redis=None, limiter=limiter, token_fp=token_fp)


def shard_state(engine: Engine, shard_id: int) -> tuple[str, bool]:
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT state, incomplete FROM shards WHERE id = :id"), {"id": shard_id}
        ).one()
    return str(row[0]), bool(row[1])


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(clean_db):
    return clean_db()


def test_search_discovery_fetches_shards_in_parallel_and_dedupes(clean: Engine):
    requests: list = []
    issued: list[int] = []
    page_lock = threading.Lock()

    def handler(request: httpx.Request):
        if is_count(request):
            return count_payload(
                request, 1500 if span_days(search_query_of(request)) > 4000 else 500
            )
        with page_lock:
            issued.append(len(issued) + 1)
            index = issued[-1]
        items = (1000, 1001) if index == 1 else (1001, 1002)
        return page_payload(items)

    deps = make_deps(clean, scripted_client(handler, requests))
    stats = run_search_discovery(deps, "language:python", jitter=lambda: 0.0)

    assert stats.shards == 2
    assert stats.pages == 2
    assert stats.fetched == 4
    assert stats.inserted == 3
    assert stats.incomplete_shards == 0
    assert sorted(stats.repo_ids) == [1000, 1001, 1002]
    with clean.connect() as connection:
        states = dict(
            connection.execute(text("SELECT state, count(*) FROM shards GROUP BY state")).all()
        )
        assert states == {"done": 2}
        assert connection.scalar(text("SELECT count(*) FROM repos")) == 3
        assert connection.scalar(text("SELECT count(*) FROM audit_log")) == len(requests) == 5


def test_planner_targets_created_windows_from_the_page_budget(clean: Engine):
    requests: list = []

    def handler(request: httpx.Request):
        if is_count(request):
            query = search_query_of(request)
            return count_payload(request, 800 if span_days(query) > 4000 else 100)
        return page_payload(range(2000, 2100), total=100)

    deps = make_deps(clean, scripted_client(handler, requests))
    stats = run_search_discovery(deps, "topic:ai", max_pages=3, max_shards=10, jitter=lambda: 0.0)

    assert stats.shards == 2
    assert stats.plan_capped is False
    assert stats.incomplete_shards == 0
    with clean.connect() as connection:
        rows = connection.execute(
            text("SELECT query, total_count, state FROM shards ORDER BY id")
        ).all()
    assert len(rows) == 2
    for query, total_count, state in rows:
        assert "created:" in query
        assert total_count == 100
        assert state == "done"
    probed = [search_query_of(request) for request in requests if is_count(request)]
    assert probed and any("created:" in query for query in probed)


def test_page_cap_marks_the_shard_incomplete(clean: Engine):
    def handler(request: httpx.Request):
        if is_count(request):
            return count_payload(request, 50)
        return page_payload([6000], total=50, has_next=True, cursor="c1")

    deps = make_deps(clean, scripted_client(handler, []))
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
    assert shard_state(clean, 1) == ("incomplete", True)


def test_transient_page_error_is_retried_then_succeeds(clean: Engine):
    attempts = {"n": 0}

    def handler(request: httpx.Request):
        if is_count(request):
            return count_payload(request, 1)
        attempts["n"] += 1
        if attempts["n"] == 1:
            return transient_payload()
        return page_payload([7000])

    deps = make_deps(clean, scripted_client(handler, []))
    stats = run_search_discovery(
        deps, "topic:ai", sleep=lambda _: None, now=lambda: 1000.0, jitter=lambda: 0.0
    )

    assert attempts["n"] == 2
    assert stats.pages == 1
    assert stats.fetched == 1
    assert stats.incomplete_shards == 0
    assert shard_state(clean, 1) == ("done", False)


def test_transient_page_errors_exhausted_mark_the_shard_incomplete(clean: Engine):
    attempts = {"n": 0}

    def handler(request: httpx.Request):
        if is_count(request):
            return count_payload(request, 1)
        attempts["n"] += 1
        return transient_payload()

    deps = make_deps(clean, scripted_client(handler, []))
    stats = run_search_discovery(
        deps, "topic:ai", sleep=lambda _: None, now=lambda: 1000.0, jitter=lambda: 0.0
    )

    assert attempts["n"] == 3
    assert stats.fetched == 0
    assert stats.incomplete_shards == 1
    assert shard_state(clean, 1) == ("incomplete", True)


def test_expired_deadline_leaves_shards_pending(clean: Engine):
    def handler(request: httpx.Request):
        if is_count(request):
            return count_payload(request, 50)
        return page_payload([8000])

    limiter = BucketLimiter(fakeredis.FakeRedis())
    limiter.bind_deadline(Deadline(0.0))
    deps = make_deps(clean, scripted_client(handler, []), limiter=limiter)
    stats = run_search_discovery(deps, "topic:ai", jitter=lambda: 0.0)

    assert stats.shards == 1
    assert stats.fetched == 0
    assert stats.deadline_hit is True
    assert shard_state(clean, 1) == ("pending", False)


def test_mid_fetch_deadline_leaves_the_shard_pending(clean: Engine):
    clock = [0.0]

    def handler(request: httpx.Request):
        if is_count(request):
            return count_payload(request, 50)
        clock[0] = 6.0
        return httpx.Response(503, json={"message": "server error"})

    limiter = BucketLimiter(fakeredis.FakeRedis())
    limiter.bind_deadline(Deadline(10.0, clock=lambda: clock[0]))
    deps = make_deps(clean, scripted_client(handler, []), limiter=limiter)
    stats = run_search_discovery(deps, "topic:ai", sleep=lambda _: None, jitter=lambda: 0.0)

    assert stats.deadline_hit is True
    assert stats.fetched == 0
    assert stats.incomplete_shards == 0
    assert shard_state(clean, 1) == ("pending", False)


def test_deadline_during_planning_returns_partial_stats(clean: Engine, monkeypatch):
    def raise_deadline(*args, **kwargs):
        raise DeadlineExceededError(5.0)

    monkeypatch.setattr(pipeline, "count_queries", raise_deadline)
    deps = make_deps(clean, scripted_client(lambda request: httpx.Response(500), []))
    stats = run_search_discovery(deps, "topic:ai", jitter=lambda: 0.0)

    assert stats == DiscoveryStats(deadline_hit=True)
    assert scalar(clean, "SELECT count(*) FROM shards") == 0


def test_cancellation_mid_page_leaves_the_shard_pending(clean: Engine):
    state = {"cancel": False}

    def handler(request: httpx.Request):
        if is_count(request):
            return count_payload(request, 50)
        state["cancel"] = True
        return page_payload([9000])

    deps = make_deps(clean, scripted_client(handler, []))
    token = cancellation.bind(lambda: state["cancel"])
    try:
        with pytest.raises(cancellation.RunCancelled):
            run_search_discovery(deps, "topic:ai", jitter=lambda: 0.0)
    finally:
        cancellation.reset(token)

    assert shard_state(clean, 1) == ("pending", False)


def test_plan_capped_when_max_shards_is_small(clean: Engine):
    def handler(request: httpx.Request):
        if is_count(request):
            return count_payload(
                request, 1500 if span_days(search_query_of(request)) > 4000 else 500
            )
        return page_payload([])

    deps = make_deps(clean, scripted_client(handler, []))
    stats = run_search_discovery(deps, "topic:ai", max_shards=1, jitter=lambda: 0.0)

    assert stats.plan_capped is True
    assert stats.shards == 1


def test_count_total_returns_the_graphql_repository_count(clean: Engine):
    requests: list = []

    def handler(request: httpx.Request):
        return count_payload(request, 321)

    deps = make_deps(clean, scripted_client(handler, requests))
    assert count_total(deps, "language:python") == 321

    assert len(requests) == 1
    assert urlparse(str(requests[0].url)).path == "/graphql"
    with clean.connect() as connection:
        row = connection.execute(text("SELECT params, token_fp FROM audit_log")).mappings().one()
    assert row["params"]["q"] == "language:python"
    assert row["token_fp"] == "test-fp"


def test_graphql_audit_hook_records_page_repository_count(clean: Engine):
    deps = make_deps(clean, scripted_client(lambda request: httpx.Response(500), []))
    hook = pipeline._graphql_audit_hook(deps)

    hook(page_payload([], total=250), 12.0, {"q": "topic:ai", "after": None})

    with clean.connect() as connection:
        row = connection.execute(text("SELECT params, total_count FROM audit_log")).mappings().one()
    assert row["params"] == {"q": "topic:ai", "after": None}
    assert row["total_count"] == 250


def test_graphql_audit_hook_records_count_batch_alias_zero(clean: Engine):
    deps = make_deps(clean, scripted_client(lambda request: httpx.Response(500), []))
    hook = pipeline._graphql_audit_hook(deps)
    response = httpx.Response(
        200,
        json={
            "data": {
                "s0": {"repositoryCount": 321},
                "s1": {"repositoryCount": 5},
                "rateLimit": {"cost": 1, "remaining": 5000},
            }
        },
    )

    hook(response, 8.0, {"q": "language:python", "after": None})

    assert scalar(clean, "SELECT total_count FROM audit_log") == 321


def test_graphql_audit_hook_leaves_total_count_none_without_counts(clean: Engine):
    deps = make_deps(clean, scripted_client(lambda request: httpx.Response(500), []))
    hook = pipeline._graphql_audit_hook(deps)
    response = httpx.Response(200, json={"data": {"rateLimit": {"cost": 1, "remaining": 5000}}})

    hook(response, 3.0, {"q": "topic:ai", "after": None})

    assert scalar(clean, "SELECT total_count FROM audit_log") is None


def test_graphql_audit_hook_parses_the_body_once(clean: Engine, monkeypatch):
    deps = make_deps(clean, scripted_client(lambda request: httpx.Response(500), []))
    hook = pipeline._graphql_audit_hook(deps)
    response = page_payload([], total=250)
    calls = {"n": 0}
    original_json = response.json

    def counting_json():
        calls["n"] += 1
        return original_json()

    monkeypatch.setattr(response, "json", counting_json)

    hook(response, 12.0, {"q": "topic:ai", "after": None})

    assert calls["n"] == 1
    cached = audit_module.cached_json(response)
    assert cached is not None
    assert cached["data"]["s"]["repositoryCount"] == 250


def test_graphql_audit_hook_falls_back_to_alias_zero_when_page_count_invalid(clean: Engine):
    deps = make_deps(clean, scripted_client(lambda request: httpx.Response(500), []))
    hook = pipeline._graphql_audit_hook(deps)
    response = httpx.Response(
        200,
        json={"data": {"s": {"repositoryCount": None}, "s0": {"repositoryCount": 42}}},
    )

    hook(response, 8.0, {"q": "topic:ai", "after": None})

    assert scalar(clean, "SELECT total_count FROM audit_log") == 42


def test_non_200_count_exhaustion_raises_request_failed(clean: Engine):
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
    assert len(requests) == 5
    assert len(sleeps) == 4
    assert scalar(clean, "SELECT count(*) FROM audit_log") == 5


def test_zero_count_query_creates_no_shards(clean: Engine):
    requests: list = []

    def handler(request: httpx.Request):
        return count_payload(request, 0)

    deps = make_deps(clean, scripted_client(handler, requests))
    stats = run_search_discovery(deps, "language:python", jitter=lambda: 0.0)
    assert stats == DiscoveryStats()
    assert scalar(clean, "SELECT count(*) FROM shards") == 0
    assert len(requests) == 1


def test_since_scan_upserts_and_advances_checkpoint(clean: Engine):
    with clean.begin() as connection:
        shard_id = connection.execute(
            text("INSERT INTO shards (kind) VALUES ('since-range') RETURNING id")
        ).scalar_one()
    requests: list = []
    next_url = "https://api.github.com/repositories?since=20&per_page=100"

    def handler(request: httpx.Request):
        if request.url.params.get("since") == "3":
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
        assert parse_qs(request.url.query.decode()) == {"type": ["all"], "per_page": ["100"]}
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
        assert parse_qs(request.url.query.decode()) == {"type": ["all"], "per_page": ["100"]}
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

    deps = Deps(
        client=scripted_client(handler, []),
        engine=clean,
        redis=None,
        limiter=None,
        token_fp="test-fp",
        audit_buffer=AuditBuffer(clean, batch_size=100),
    )
    stats = run_org_enum(deps, org="acme", jitter=lambda: 0.0)

    assert stats.fetched == 1
    assert scalar(clean, "SELECT count(*) FROM audit_log") == 1
