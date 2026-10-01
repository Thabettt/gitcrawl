from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urlparse

import fakeredis
import httpx
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

from discover import pipeline
from discover.pipeline import Deps, run_search_discovery
from lib.gh_client import token_fingerprint
from limiter.buckets import BucketLimiter
from serve.filter_spec import parse_filter_spec
from serve.runner import RunnerConfig, build_deps, make_runner, run_filter

SEARCH_HEADERS = {"x-ratelimit-resource": "search"}
SEARCH_PATH = "/search/repositories"


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(alembic_engine: Engine) -> Engine:
    with alembic_engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE run_items, runs, saved_filters, audit_log, shards, geo_cache, "
                "owners, repos, full_name_history RESTART IDENTITY CASCADE"
            )
        )
    return alembic_engine


def repo_item(
    repo_id: int,
    *,
    login: str | None = None,
    stars: int = 10,
    topics: tuple[str, ...] = (),
    default_branch: str = "main",
    pushed_at: str = "2026-01-01T00:00:00Z",
    **overrides,
) -> dict:
    login = login or f"owner{repo_id}"
    item = {
        "id": repo_id,
        "node_id": f"R_{repo_id}",
        "name": f"repo{repo_id}",
        "full_name": f"{login}/repo{repo_id}",
        "owner": {"id": repo_id * 10, "login": login, "type": "User"},
        "private": False,
        "description": f"repo {repo_id}",
        "language": "Python",
        "license": {"spdx_id": "MIT"},
        "topics": list(topics),
        "stargazers_count": stars,
        "forks_count": 1,
        "watchers_count": 1,
        "open_issues_count": 0,
        "default_branch": default_branch,
        "pushed_at": pushed_at,
    }
    item.update(overrides)
    return item


def count_response(total: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={"total_count": total, "items": [], "incomplete_results": False},
        headers=SEARCH_HEADERS,
    )


def page_response(items, *, next_url: str | None = None) -> httpx.Response:
    headers = dict(SEARCH_HEADERS)
    if next_url is not None:
        headers["Link"] = f'<{next_url}>; rel="next"'
    return httpx.Response(
        200,
        json={"total_count": len(items), "items": items, "incomplete_results": False},
        headers=headers,
    )


def next_page_url(query: str, page: int) -> str:
    params = urlencode({"q": query, "per_page": 100, "page": page})
    return f"https://api.github.com{SEARCH_PATH}?{params}"


def scripted(handler):
    requests: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(wrapped)), requests


def make_deps(engine: Engine, client: httpx.Client) -> Deps:
    redis = fakeredis.FakeRedis()
    return Deps(
        client=client, engine=engine, redis=redis, limiter=BucketLimiter(redis), token_id="test-fp"
    )


def query_of(request: httpx.Request) -> str:
    return parse_qs(urlparse(str(request.url)).query).get("q", [""])[0]


def params_of(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(urlparse(str(request.url)).query)


def path_of(request: httpx.Request) -> str:
    return urlparse(str(request.url)).path


def is_count(request: httpx.Request) -> bool:
    return params_of(request).get("per_page") == ["1"]


def is_search_page(request: httpx.Request) -> bool:
    return path_of(request) == SEARCH_PATH and not is_count(request)


def spec_for(**overrides):
    document = {"gitcrawl_filter": 1}
    document.update(overrides)
    return parse_filter_spec(document)


def test_run_search_discovery_returns_unique_repo_ids(clean: Engine):
    first = [repo_item(1), repo_item(2)]
    second = [repo_item(2), repo_item(3)]

    def handler(request: httpx.Request):
        if is_count(request):
            return count_response(3)
        if params_of(request).get("page") == ["2"]:
            return page_response(second)
        return page_response(first, next_url=next_page_url(query_of(request), 2))

    client, _ = scripted(handler)
    stats = run_search_discovery(make_deps(clean, client), "topic:ai")

    assert stats.repo_ids == (1, 2, 3)
    assert stats.fetched == 4


def test_run_filter_and_pipeline_share_count_total(clean: Engine, monkeypatch):
    calls: list[str] = []

    def fake_count(deps: Deps, query: str, **kwargs) -> int:
        calls.append(query)
        return 0

    monkeypatch.setattr(pipeline, "count_total", fake_count)
    client, _ = scripted(lambda request: httpx.Response(500))
    deps = make_deps(clean, client)

    payload = run_filter(deps, spec_for(q="topic:ai"), config=RunnerConfig(max_shards=1))
    assert calls == ["topic:ai", "topic:ai"]
    assert payload.total_count == 0
    assert payload.items == []

    stats = run_search_discovery(deps, "language:rust")
    assert calls == ["topic:ai", "topic:ai", "language:rust"]
    assert stats.shards == 0


def test_run_filter_honors_shard_candidate_and_hydrate_caps(clean: Engine):
    page = [repo_item(1, stars=1), repo_item(2, stars=100), repo_item(3, stars=50)]

    def handler(request: httpx.Request):
        path = path_of(request)
        if path == SEARCH_PATH:
            if is_count(request):
                return count_response(1600 if "created:" not in query_of(request) else 500)
            return page_response(page)
        if path.startswith("/repos/"):
            return httpx.Response(200, json=repo_item(2, stars=100))
        return httpx.Response(404)

    client, requests = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="topic:ai"),
        config=RunnerConfig(max_shards=1, max_candidates=2, max_hydrate=1, max_enrich=0),
    )

    assert [item.repo_id for item in payload.items] == [2, 3]
    assert payload.fetched == 3
    assert len([request for request in requests if is_search_page(request)]) == 1
    hydration = [path_of(request) for request in requests if path_of(request).startswith("/repos/")]
    assert hydration == ["/repos/owner2/repo2"]


def test_run_filter_applies_db_filters_and_builds_payload(clean: Engine):
    page = [
        repo_item(1, stars=7, topics=("rust",)),
        repo_item(2, stars=3, topics=("rust",)),
        repo_item(3, stars=9, topics=("python",)),
    ]
    hydrated = {1: repo_item(1, stars=11, topics=("rust",), pushed_at="2026-02-02T00:00:00Z")}

    def handler(request: httpx.Request):
        path = path_of(request)
        if path == SEARCH_PATH:
            if is_count(request):
                return count_response(3)
            return page_response(page)
        if path == "/repos/owner1/repo1":
            return httpx.Response(200, json=hydrated[1])
        return httpx.Response(404)

    client, _ = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", virtual={"min_stars": 5, "team_topic": "rust"}),
        config=RunnerConfig(max_shards=1),
    )

    assert payload.total_count == 3
    assert payload.fetched == 3
    assert payload.incomplete is False
    assert payload.warnings == []
    assert [item.repo_id for item in payload.items] == [1]
    item = payload.items[0]
    assert item.full_name == "owner1/repo1"
    assert item.stargazers == 11
    assert item.pushed_at == "2026-02-02T00:00:00Z"
    assert item.archived is False
    assert item.language == "Python"
    assert item.license_spdx == "MIT"
    assert item.country_iso is None
    assert item.geo_confidence is None
    assert item.virtuals == {"min_stars": 5, "team_topic": "rust"}


def test_run_filter_records_r44_warnings_and_marks_incomplete(clean: Engine):
    def handler(request: httpx.Request):
        path = path_of(request)
        if path == SEARCH_PATH:
            if is_count(request):
                return count_response(1)
            return page_response([repo_item(1)])
        if path == "/repos/owner1/repo1":
            return httpx.Response(200, json=repo_item(1))
        return httpx.Response(404)

    client, _ = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", virtual={"min_commits": 100, "min_loc": 5000}),
        config=RunnerConfig(max_shards=1),
    )

    assert payload.incomplete is True
    assert len(payload.warnings) == 2
    assert any("min_commits" in warning for warning in payload.warnings)
    assert any("min_loc" in warning for warning in payload.warnings)
    assert [item.repo_id for item in payload.items] == [1]


def test_run_filter_tolerates_hydration_failures(clean: Engine):
    page = [repo_item(1, stars=20), repo_item(2, stars=10)]

    def handler(request: httpx.Request):
        path = path_of(request)
        if path == SEARCH_PATH:
            if is_count(request):
                return count_response(2)
            return page_response(page)
        if path == "/repos/owner1/repo1":
            return httpx.Response(422, json={"message": "Validation Failed"})
        if path == "/repos/owner2/repo2":
            return httpx.Response(200, json=repo_item(2, stars=42))
        return httpx.Response(404)

    client, _ = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python"),
        config=RunnerConfig(max_shards=1),
    )

    assert [item.repo_id for item in payload.items] == [2, 1]
    refreshed = {item.repo_id: item.stargazers for item in payload.items}
    assert refreshed == {1: 20, 2: 42}


def test_run_filter_owner_country_filters_and_bounds_owner_fetches(clean: Engine):
    page = [
        repo_item(1, login="alice", stars=30),
        repo_item(2, login="bob", stars=20),
        repo_item(3, login="carol", stars=10),
    ]
    locations = {"alice": "Germany", "bob": "Berlin"}

    def handler(request: httpx.Request):
        path = path_of(request)
        if path == SEARCH_PATH:
            if is_count(request):
                return count_response(3)
            return page_response(page)
        if path.startswith("/repos/"):
            login = path.split("/")[2]
            repo_id = int(path.rsplit("repo", 1)[1])
            return httpx.Response(200, json=repo_item(repo_id, login=login))
        if path.startswith("/users/"):
            login = path.rsplit("/", 1)[1]
            if login in locations:
                return httpx.Response(200, json={"login": login, "location": locations[login]})
            return httpx.Response(404)
        return httpx.Response(404)

    client, requests = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(
            q="language:python",
            virtual={"owner_country": "DE", "min_geo_confidence": "name"},
        ),
        config=RunnerConfig(max_shards=1, max_enrich=2),
    )

    assert [item.repo_id for item in payload.items] == [1]
    assert payload.items[0].country_iso == "DE"
    assert payload.items[0].geo_confidence == "name"
    fetched_owners = [
        path_of(request).rsplit("/", 1)[1]
        for request in requests
        if path_of(request).startswith("/users/")
    ]
    assert fetched_owners == ["alice", "bob"]
    with clean.connect() as connection:
        stored = {
            row["login"]: (row["country_iso"], row["geo_confidence"])
            for row in connection.execute(
                text("SELECT login, country_iso, geo_confidence FROM owners")
            ).mappings()
        }
    assert stored == {
        "alice": ("DE", "name"),
        "bob": ("DE", "gazetteer-city"),
        "carol": (None, None),
    }


def test_run_filter_has_dockerfile_true_enforces_presence_and_budget(clean: Engine):
    page = [repo_item(1, stars=30), repo_item(2, stars=20)]
    trees = {
        "/repos/owner1/repo1/git/trees/main": {"tree": [{"path": "Dockerfile", "type": "blob"}]},
        "/repos/owner2/repo2/git/trees/main": {"tree": [{"path": "README.md", "type": "blob"}]},
    }

    def handler(request: httpx.Request):
        path = path_of(request)
        if path == SEARCH_PATH:
            if is_count(request):
                return count_response(2)
            return page_response(page)
        if path.startswith("/repos/"):
            if path in trees:
                return httpx.Response(200, json={**trees[path], "truncated": False})
            repo_id = int(path.rsplit("repo", 1)[1]) if path.rsplit("repo", 1)[1].isdigit() else 0
            return httpx.Response(200, json=repo_item(repo_id))
        return httpx.Response(404)

    client, requests = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", virtual={"has_dockerfile": True}),
        config=RunnerConfig(max_shards=1, max_enrich=1),
    )

    assert [item.repo_id for item in payload.items] == [1]
    assert payload.items[0].virtuals["has_dockerfile"] is True
    tree_requests = [path_of(request) for request in requests if "/git/trees/" in path_of(request)]
    assert tree_requests == ["/repos/owner1/repo1/git/trees/main"]


def test_run_filter_has_dockerfile_false_keeps_absent_repos(clean: Engine):
    page = [repo_item(1, stars=30), repo_item(2, stars=20)]
    trees = {
        "/repos/owner1/repo1/git/trees/main": {"tree": [{"path": "Dockerfile", "type": "blob"}]},
        "/repos/owner2/repo2/git/trees/main": {"tree": [{"path": "README.md", "type": "blob"}]},
    }

    def handler(request: httpx.Request):
        path = path_of(request)
        if path == SEARCH_PATH:
            if is_count(request):
                return count_response(2)
            return page_response(page)
        if path in trees:
            return httpx.Response(200, json={**trees[path], "truncated": False})
        if path.startswith("/repos/"):
            repo_id = int(path.rsplit("repo", 1)[1])
            return httpx.Response(200, json=repo_item(repo_id))
        return httpx.Response(404)

    client, _ = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", virtual={"has_dockerfile": False}),
        config=RunnerConfig(max_shards=1, max_enrich=5),
    )

    assert [item.repo_id for item in payload.items] == [2]
    assert payload.items[0].virtuals["has_dockerfile"] is False


def test_run_filter_tolerates_owner_fetch_failure(clean: Engine):
    page = [repo_item(1, login="alice", stars=30), repo_item(2, login="bob", stars=20)]

    def handler(request: httpx.Request):
        path = path_of(request)
        if path == SEARCH_PATH:
            if is_count(request):
                return count_response(2)
            return page_response(page)
        if path.startswith("/repos/"):
            login = path.split("/")[2]
            repo_id = int(path.rsplit("repo", 1)[1])
            return httpx.Response(200, json=repo_item(repo_id, login=login))
        if path == "/users/alice":
            return httpx.Response(404)
        if path == "/users/bob":
            return httpx.Response(200, json={"login": "bob", "location": "Germany"})
        return httpx.Response(404)

    client, _ = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", virtual={"owner_country": "DE"}),
        config=RunnerConfig(max_shards=1, max_enrich=5),
    )

    assert [item.repo_id for item in payload.items] == [2]
    with clean.connect() as connection:
        stored = {
            row["login"]: (row["country_iso"], row["geo_confidence"])
            for row in connection.execute(
                text("SELECT login, country_iso, geo_confidence FROM owners")
            ).mappings()
        }
    assert stored == {"alice": (None, None), "bob": ("DE", "name")}


def test_run_filter_tolerates_tree_fetch_failure(clean: Engine):
    def handler(request: httpx.Request):
        path = path_of(request)
        if path == SEARCH_PATH:
            if is_count(request):
                return count_response(1)
            return page_response([repo_item(1)])
        if "/git/trees/" in path:
            return httpx.Response(404)
        if path == "/repos/owner1/repo1":
            return httpx.Response(200, json=repo_item(1))
        return httpx.Response(404)

    client, _ = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", virtual={"has_dockerfile": True}),
        config=RunnerConfig(max_shards=1, max_enrich=5),
    )

    assert payload.items == []
    assert payload.incomplete is False


def test_build_deps_requires_a_token(clean: Engine, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKENS", raising=False)
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    with pytest.raises(ValueError):
        build_deps(clean, redis_client=fakeredis.FakeRedis(), client=client)


def test_build_deps_fingerprints_the_token_and_wires_the_stack(clean: Engine):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    redis = fakeredis.FakeRedis()
    deps = build_deps(clean, token="sekret", redis_client=redis, client=client)

    assert deps.token_id == token_fingerprint("sekret")
    assert deps.client is client
    assert deps.redis is redis
    assert deps.limiter is not None


def test_make_runner_parses_filter_spec_dicts(clean: Engine):
    def handler(request: httpx.Request):
        if is_count(request):
            return count_response(0)
        return httpx.Response(404)

    client, requests = scripted(handler)
    runner = make_runner(make_deps(clean, client), config=RunnerConfig(max_shards=1))
    payload = runner(7, {"gitcrawl_filter": 1, "q": "language:rust"})

    assert payload.total_count == 0
    assert payload.items == []
    assert len(requests) == 2
    assert all(is_count(request) for request in requests)
