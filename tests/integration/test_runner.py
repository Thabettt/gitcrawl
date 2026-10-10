from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import fakeredis
import httpx
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

from discover import pipeline
from discover.pipeline import Deps, run_search_discovery
from lib.gh_client import ThrottledError, token_fingerprint
from limiter.buckets import BucketLimiter
from serve import runner as runner_module
from serve.executor import create_run, execute_run, run_status
from serve.filter_spec import parse_filter_spec
from serve.runner import RunnerConfig, build_deps, make_runner, run_filter


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(clean_db):
    return clean_db()


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


GRAPHQL_REPO_ALIAS_RE = re.compile(r'(n\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\)')
FILE_ALIAS_RE = re.compile(r'(n\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\)')


def rest_item_to_graphql_node(item: dict, *, commit_count: int | None = None) -> dict:
    owner = item.get("owner") or {}
    default_branch_ref: dict = {"name": item.get("default_branch") or "main"}
    if commit_count is not None:
        default_branch_ref["target"] = {"history": {"totalCount": commit_count}}
    return {
        "databaseId": item.get("id"),
        "id": item.get("node_id"),
        "name": item.get("name"),
        "nameWithOwner": item.get("full_name"),
        "stargazerCount": item.get("stargazers_count", 0),
        "forkCount": item.get("forks_count", 0),
        "watchers": {"totalCount": item.get("watchers_count", 0)},
        "issues": {"totalCount": item.get("open_issues_count", 0)},
        "diskUsage": item.get("size", 0),
        "isArchived": bool(item.get("archived")),
        "isDisabled": bool(item.get("disabled")),
        "isFork": bool(item.get("fork")),
        "isTemplate": bool(item.get("is_template")),
        "visibility": str(item.get("visibility") or "public").upper(),
        "description": item.get("description"),
        "homepageUrl": item.get("homepage"),
        "pushedAt": item.get("pushed_at"),
        "updatedAt": item.get("updated_at") or item.get("pushed_at"),
        "createdAt": item.get("created_at") or item.get("pushed_at"),
        "defaultBranchRef": default_branch_ref,
        "primaryLanguage": {"name": item.get("language")} if item.get("language") else None,
        "licenseInfo": (
            {"spdxId": (item.get("license") or {}).get("spdx_id")} if item.get("license") else None
        ),
        "repositoryTopics": {
            "nodes": [{"topic": {"name": topic}} for topic in item.get("topics", [])]
        },
        "hasIssuesEnabled": True,
        "hasWikiEnabled": False,
        "hasProjectsEnabled": False,
        "hasDiscussionsEnabled": False,
        "hasPullRequestsEnabled": True,
        "parent": None,
        "owner": {
            "databaseId": owner.get("id"),
            "login": owner.get("login"),
            "__typename": owner.get("type") or "User",
        },
    }


def graphql_batch_response(
    request: httpx.Request,
    items_by_full_name: dict[str, dict],
    *,
    commit_counts: dict[int, int] | None = None,
    rate_limit: dict[str, int] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    body = json.loads(request.content)
    counts = commit_counts or {}
    data: dict[str, object] = {}
    errors: list[dict] = []
    for alias, owner, name in GRAPHQL_REPO_ALIAS_RE.findall(body["query"]):
        item = items_by_full_name.get(f"{owner}/{name}")
        if item is None:
            data[alias] = None
            errors.append({"message": "Could not resolve to a Repository", "path": [alias]})
        else:
            item_id = item.get("id")
            count = counts.get(item_id) if isinstance(item_id, int) else None
            data[alias] = rest_item_to_graphql_node(item, commit_count=count)
    if rate_limit is not None:
        data["rateLimit"] = rate_limit
    payload: dict[str, object] = {"data": data}
    if errors:
        payload["errors"] = errors
    return httpx.Response(200, json=payload, headers=headers)


def graphql_file_response(
    request: httpx.Request, blob_paths: dict[str, list[str]], path: str
) -> httpx.Response:
    body = json.loads(request.content)
    data: dict[str, object] = {}
    for alias, owner, name in FILE_ALIAS_RE.findall(body["query"]):
        tree = blob_paths.get(f"{owner}/{name}", [])
        data[alias] = {"object": {"__typename": "Blob"}} if path in tree else {"object": None}
    return httpx.Response(200, json={"data": data})


def is_file_query(request: httpx.Request) -> bool:
    return 'object(expression: "HEAD:' in json.loads(request.content)["query"]


OWNER_ALIAS_RE = re.compile(r'(n\d+): (?:user|organization)\(login: "([^"]+)"\)')


def is_owner_query(request: httpx.Request) -> bool:
    query = json.loads(request.content)["query"]
    return "user(login:" in query or "organization(login:" in query


def owner_logins(request: httpx.Request) -> list[str]:
    return [login for _, login in OWNER_ALIAS_RE.findall(json.loads(request.content)["query"])]


def graphql_owner_response(request: httpx.Request, locations: dict[str, str | None]):
    body = json.loads(request.content)
    data: dict[str, object] = {}
    errors: list[dict] = []
    for alias, login in OWNER_ALIAS_RE.findall(body["query"]):
        if login not in locations:
            data[alias] = None
            errors.append({"message": "Could not resolve to a User", "path": [alias]})
        else:
            data[alias] = {"location": locations[login]}
    payload: dict[str, object] = {"data": data}
    if errors:
        payload["errors"] = errors
    return httpx.Response(200, json=payload)


SEARCH_ARG_RE = re.compile(r'query: ("(?:[^"\\]|\\.)*")')


def graphql_query_text(request: httpx.Request) -> str:
    return json.loads(request.content)["query"]


def count_response(request: httpx.Request, total: int) -> httpx.Response:
    aliases = re.findall(r"(s\d+): search\(first: 1", graphql_query_text(request))
    data: dict[str, object] = {alias: {"repositoryCount": total} for alias in aliases}
    data["rateLimit"] = {"cost": 1, "remaining": 5000}
    return httpx.Response(200, json={"data": data})


def page_response(
    items, *, incomplete: bool = False, next_url: str | None = None
) -> httpx.Response:
    nodes = [rest_item_to_graphql_node(item) for item in items]
    search = {
        "repositoryCount": len(nodes),
        "pageInfo": {"hasNextPage": next_url is not None, "endCursor": next_url},
        "nodes": nodes,
    }
    payload: dict[str, object] = {
        "data": {"s": search, "rateLimit": {"cost": 1, "remaining": 5000}}
    }
    if incomplete:
        payload["errors"] = [{"message": "Something went wrong"}]
    return httpx.Response(200, json=payload)


def scripted(handler):
    requests: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(wrapped)), requests


def make_deps(engine: Engine, client: httpx.Client) -> Deps:
    redis = fakeredis.FakeRedis()
    return Deps(
        client=client, engine=engine, redis=redis, limiter=BucketLimiter(redis), token_fp="test-fp"
    )


def query_of(request: httpx.Request) -> str:
    match = SEARCH_ARG_RE.search(graphql_query_text(request))
    return json.loads(match.group(1)) if match else ""


def path_of(request: httpx.Request) -> str:
    return urlparse(str(request.url)).path


def is_count(request: httpx.Request) -> bool:
    return "search(first: 1," in graphql_query_text(request)


def is_search_page(request: httpx.Request) -> bool:
    return "search(first: 100," in graphql_query_text(request)


def is_search_request(request: httpx.Request) -> bool:
    return path_of(request) == "/graphql" and (is_count(request) or is_search_page(request))


def spec_for(**overrides):
    document = {"gitcrawl_filter": 1}
    document.update(overrides)
    return parse_filter_spec(document)


def test_run_search_discovery_returns_unique_repo_ids(clean: Engine):
    first = [repo_item(1), repo_item(2)]
    second = [repo_item(2), repo_item(3)]

    def handler(request: httpx.Request):
        if is_count(request):
            return count_response(request, 3)
        if "after:" in graphql_query_text(request):
            return page_response(second)
        return page_response(first, next_url="cursor-2")

    client, _ = scripted(handler)
    stats = run_search_discovery(make_deps(clean, client), "topic:ai")

    assert stats.repo_ids == (1, 2, 3)
    assert stats.fetched == 4


def test_run_filter_and_pipeline_share_count_total(clean: Engine, monkeypatch):
    calls: list[list[str]] = []

    def fake_count(client, queries, **kwargs) -> list[int]:
        calls.append(list(queries))
        return [0 for _ in queries]

    monkeypatch.setattr(pipeline, "count_queries", fake_count)
    client, _ = scripted(lambda request: httpx.Response(500))
    deps = make_deps(clean, client)

    payload = run_filter(deps, spec_for(q="topic:ai"), config=RunnerConfig(max_shards=1))
    assert calls == [["topic:ai"]]
    assert payload.total_count == 0
    assert payload.items == []

    stats = run_search_discovery(deps, "language:rust")
    assert calls == [["topic:ai"], ["language:rust"]]
    assert stats.shards == 0


def test_run_filter_honors_shard_candidate_and_hydrate_caps(clean: Engine):
    page = [repo_item(1, stars=1), repo_item(2, stars=100), repo_item(3, stars=50)]

    def handler(request: httpx.Request):
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 1600 if "created:" not in query_of(request) else 500)
            return page_response(page)
        if path == "/graphql":
            return graphql_batch_response(request, {"owner2/repo2": repo_item(2, stars=100)})
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
    assert payload.incomplete is True
    assert any("max_candidates=2" in warning for warning in payload.warnings)
    assert any("max_hydrate=1" in warning for warning in payload.warnings)
    assert len([request for request in requests if is_search_page(request)]) == 1
    hydration = [path_of(request) for request in requests if path_of(request).startswith("/repos/")]
    assert hydration == []
    assert any(request.url.path == "/graphql" for request in requests)


def test_run_filter_marks_incomplete_when_a_discovery_shard_is_incomplete(clean: Engine):
    def handler(request: httpx.Request):
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 1)
            return page_response([repo_item(1)], incomplete=True)
        if path == "/graphql":
            return graphql_batch_response(request, {"owner1/repo1": repo_item(1)})
        if path == "/repos/owner1/repo1":
            return httpx.Response(200, json=repo_item(1))
        return httpx.Response(404)

    client, _ = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="topic:ai"),
        config=RunnerConfig(max_shards=1),
    )

    assert [item.repo_id for item in payload.items] == [1]
    assert payload.incomplete is True
    assert any("shard" in warning and "incomplete" in warning for warning in payload.warnings)


def test_run_filter_marks_incomplete_when_the_page_cap_truncates(clean: Engine):
    page = [repo_item(1)]

    def handler(request: httpx.Request):
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 500)
            return page_response(page, next_url="cursor-2")
        if path == "/graphql":
            return graphql_batch_response(request, {"owner1/repo1": repo_item(1)})
        if path == "/repos/owner1/repo1":
            return httpx.Response(200, json=repo_item(1))
        return httpx.Response(404)

    client, _ = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="topic:ai", page={"per_page": 1, "max_pages": 1}),
        config=RunnerConfig(max_shards=1, max_enrich=0),
    )

    assert payload.fetched == 1
    assert payload.incomplete is True
    assert any("page cap" in warning and "incomplete" in warning for warning in payload.warnings)


def test_run_filter_marks_incomplete_when_the_shard_plan_is_capped(clean: Engine):
    page = [repo_item(1)]

    def handler(request: httpx.Request):
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 1600 if "created:" not in query_of(request) else 500)
            return page_response(page)
        if path == "/graphql":
            return graphql_batch_response(request, {"owner1/repo1": repo_item(1)})
        if path == "/repos/owner1/repo1":
            return httpx.Response(200, json=repo_item(1))
        return httpx.Response(404)

    client, _ = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="topic:ai"),
        config=RunnerConfig(max_shards=1, max_enrich=0),
    )

    assert payload.incomplete is True
    assert any(
        "max_shards=1" in warning and "incomplete" in warning for warning in payload.warnings
    )


def test_run_filter_applies_db_filters_and_builds_payload(clean: Engine):
    page = [
        repo_item(1, stars=7, topics=("rust",)),
        repo_item(2, stars=3, topics=("rust",)),
        repo_item(3, stars=9, topics=("python",)),
    ]
    hydrated = {1: repo_item(1, stars=11, topics=("rust",), pushed_at="2026-02-02T00:00:00Z")}

    def handler(request: httpx.Request):
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 3)
            return page_response(page)
        if path == "/graphql":
            return graphql_batch_response(request, {"owner1/repo1": hydrated[1]})
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
    assert payload.incomplete is True
    assert any("were deleted or private" in warning for warning in payload.warnings)
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


def _sort_fixture_handler():
    page = [
        repo_item(1, stars=10, forks_count=5, pushed_at="2026-03-01T00:00:00Z"),
        repo_item(2, stars=30, forks_count=1, pushed_at="2026-01-01T00:00:00Z"),
        repo_item(3, stars=20, forks_count=9, pushed_at="2026-02-01T00:00:00Z"),
    ]

    def handler(request: httpx.Request):
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 3)
            return page_response(page)
        if path == "/graphql":
            return graphql_batch_response(request, {item["full_name"]: item for item in page})
        if path.startswith("/repos/"):
            repo_id = int(path.rsplit("repo", 1)[1])
            return httpx.Response(200, json=page[repo_id - 1])
        return httpx.Response(404)

    return handler


@pytest.mark.parametrize(
    ("sort", "order", "expected"),
    [
        ("stars", "desc", [2, 3, 1]),
        ("stars", "asc", [1, 3, 2]),
        ("forks", "desc", [3, 1, 2]),
        ("forks", "asc", [2, 1, 3]),
        ("updated", "desc", [1, 3, 2]),
        ("updated", "asc", [2, 3, 1]),
    ],
)
def test_run_filter_sorts_survivors_locally(clean: Engine, sort, order, expected):
    client, _ = scripted(_sort_fixture_handler())
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", sort=sort, order=order),
        config=RunnerConfig(max_shards=1),
    )

    assert [item.repo_id for item in payload.items] == expected
    assert payload.incomplete is False


def test_run_filter_keeps_survivor_order_for_help_wanted_issues(clean: Engine):
    client, _ = scripted(_sort_fixture_handler())
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", sort="help-wanted-issues", order="desc"),
        config=RunnerConfig(max_shards=1),
    )

    assert [item.repo_id for item in payload.items] == [2, 3, 1]
    assert payload.incomplete is True
    assert any("help-wanted-issues" in warning for warning in payload.warnings)


def test_run_filter_records_r44_warnings_and_marks_incomplete(clean: Engine):
    def handler(request: httpx.Request):
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 1)
            return page_response([repo_item(1)])
        if path == "/graphql":
            return graphql_batch_response(
                request, {"owner1/repo1": repo_item(1)}, commit_counts={1: 500}
            )
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
    assert payload.warnings == [
        "`min_loc` is recorded but unenforceable in this run; results are incomplete"
    ]
    assert not any("min_commits" in warning for warning in payload.warnings)
    assert [item.repo_id for item in payload.items] == [1]


def test_run_filter_enforces_min_and_max_commits(clean: Engine):
    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 3)
            return page_response([repo_item(index) for index in (1, 2, 3)])
        if path == "/graphql":
            return graphql_batch_response(
                request,
                {f"owner{i}/repo{i}": repo_item(i) for i in (1, 2, 3)},
                commit_counts={1: 500, 2: 50, 3: 5000},
            )
        raise AssertionError(f"unexpected path {path}")

    client, _requests = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", virtual={"min_commits": 100, "max_commits": 1000}),
    )
    assert [item.repo_id for item in payload.items] == [1]
    assert not any("min_commits" in warning for warning in payload.warnings)
    assert payload.items[0].virtuals["commit_count"] == 500


def test_run_filter_warns_when_commit_counts_are_missing(clean: Engine):
    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 1)
            return page_response([repo_item(1)])
        if path == "/graphql":
            return graphql_batch_response(request, {"owner1/repo1": repo_item(1)})
        raise AssertionError(f"unexpected path {path}")

    client, _ = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", virtual={"min_commits": 100}),
        config=RunnerConfig(max_shards=1),
    )

    assert payload.items == []
    assert (
        "1 repo(s) could not be checked for commit count; results are incomplete"
        in payload.warnings
    )


def test_run_filter_enforces_min_and_max_language_bytes(clean: Engine):
    sizes = {1: 50_000, 2: 100_000, 3: 200_000}

    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 3)
            return page_response([repo_item(index) for index in (1, 2, 3)])
        if path == "/graphql":
            return graphql_batch_response(
                request, {f"owner{i}/repo{i}": repo_item(i) for i in (1, 2, 3)}
            )
        if path.endswith("/languages"):
            repo_id = int(path.split("/")[3].removeprefix("repo"))
            return httpx.Response(200, json={"Python": sizes[repo_id]})
        raise AssertionError(f"unexpected path {path}")

    client, _requests = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(
            q="language:python",
            virtual={"min_language_bytes": 100_000, "max_language_bytes": 150_000},
        ),
    )
    assert [item.repo_id for item in payload.items] == [2]
    assert payload.items[0].virtuals["language_bytes"] == 100_000
    assert not any("language bytes" in warning for warning in payload.warnings)


def test_run_filter_warns_when_language_bytes_are_missing(clean: Engine):
    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 1)
            return page_response([repo_item(1)])
        if path == "/graphql":
            return graphql_batch_response(request, {"owner1/repo1": repo_item(1)})
        if path.endswith("/languages"):
            return httpx.Response(422, json={"message": "Validation Failed"})
        raise AssertionError(f"unexpected path {path}")

    client, _ = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", virtual={"min_language_bytes": 1}),
        config=RunnerConfig(max_shards=1),
    )

    assert payload.items == []
    assert (
        "1 repo(s) could not be checked for language bytes; results are incomplete"
        in payload.warnings
    )


def test_run_filter_language_bytes_budget_exhaustion_marks_incomplete(clean: Engine):
    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 1)
            return page_response([repo_item(1)])
        if path == "/graphql":
            return graphql_batch_response(request, {"owner1/repo1": repo_item(1)})
        raise AssertionError(f"unexpected path {path}")

    client, _ = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", virtual={"min_language_bytes": 1}),
        config=RunnerConfig(max_shards=1, max_enrich=0),
    )

    assert payload.items == []
    assert (
        "1 repo(s) could not be checked for language bytes; results are incomplete"
        in payload.warnings
    )


def test_run_filter_tolerates_hydration_failures(clean: Engine):
    page = [repo_item(1, stars=20), repo_item(2, stars=10)]

    def handler(request: httpx.Request):
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 2)
            return page_response(page)
        if path == "/graphql":
            return graphql_batch_response(request, {"owner2/repo2": repo_item(2, stars=42)})
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
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 3)
            return page_response(page)
        if path == "/graphql":
            if is_owner_query(request):
                return graphql_owner_response(request, locations)
            return graphql_batch_response(request, {item["full_name"]: item for item in page})
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
    assert payload.incomplete is True
    assert any("owner country could not be resolved" in warning for warning in payload.warnings)
    owner_requests = [
        request
        for request in requests
        if path_of(request) == "/graphql" and is_owner_query(request)
    ]
    assert len(owner_requests) == 1
    assert owner_logins(owner_requests[0]) == ["alice", "bob"]
    assert not any(path_of(request).startswith("/users/") for request in requests)
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
    blob_paths = {"owner1/repo1": ["Dockerfile"], "owner2/repo2": ["README.md"]}

    def handler(request: httpx.Request):
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 2)
            return page_response(page)
        if path == "/graphql":
            if is_file_query(request):
                return graphql_file_response(request, blob_paths, "Dockerfile")
            return graphql_batch_response(request, {item["full_name"]: item for item in page})
        if path.startswith("/repos/"):
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
    assert payload.incomplete is True
    assert any(
        "Dockerfile presence could not be checked" in warning for warning in payload.warnings
    )
    file_requests = [
        request for request in requests if request.url.path == "/graphql" and is_file_query(request)
    ]
    assert len(file_requests) == 1
    query = json.loads(file_requests[0].content)["query"]
    assert 'owner: "owner1", name: "repo1"' in query
    assert 'name: "repo2"' not in query
    assert not any("/git/trees/" in path_of(request) for request in requests)
    files = payload.field_stats["graphql"]["files"]
    assert files["keys"] == 1
    assert files["requests"] == 1
    assert files["values"] == 1
    assert files["fallbacks"] == 0


def test_run_filter_has_dockerfile_false_keeps_absent_repos(clean: Engine):
    page = [repo_item(1, stars=30), repo_item(2, stars=20)]
    blob_paths = {"owner1/repo1": ["Dockerfile"], "owner2/repo2": ["README.md"]}

    def handler(request: httpx.Request):
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 2)
            return page_response(page)
        if path == "/graphql":
            if is_file_query(request):
                return graphql_file_response(request, blob_paths, "Dockerfile")
            return graphql_batch_response(request, {item["full_name"]: item for item in page})
        if path.startswith("/repos/"):
            repo_id = int(path.rsplit("repo", 1)[1])
            return httpx.Response(200, json=repo_item(repo_id))
        return httpx.Response(404)

    client, requests = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", virtual={"has_dockerfile": False}),
        config=RunnerConfig(max_shards=1, max_enrich=5),
    )

    assert [item.repo_id for item in payload.items] == [2]
    assert payload.items[0].virtuals["has_dockerfile"] is False
    file_requests = [
        request for request in requests if request.url.path == "/graphql" and is_file_query(request)
    ]
    assert len(file_requests) == 1
    query = json.loads(file_requests[0].content)["query"]
    assert 'owner: "owner1", name: "repo1"' in query
    assert 'owner: "owner2", name: "repo2"' in query
    assert not any("/git/trees/" in path_of(request) for request in requests)


def test_run_filter_tolerates_owner_fetch_failure(clean: Engine):
    page = [repo_item(1, login="alice", stars=30), repo_item(2, login="bob", stars=20)]

    def handler(request: httpx.Request):
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 2)
            return page_response(page)
        if path == "/graphql":
            if is_owner_query(request):
                return graphql_owner_response(request, {"bob": "Germany"})
            return graphql_batch_response(request, {item["full_name"]: item for item in page})
        if path.startswith("/repos/"):
            login = path.split("/")[2]
            repo_id = int(path.rsplit("repo", 1)[1])
            return httpx.Response(200, json=repo_item(repo_id, login=login))
        if path == "/users/alice":
            return httpx.Response(404)
        if path == "/users/bob":
            return httpx.Response(200, json={"login": "bob", "location": "Germany"})
        return httpx.Response(404)

    client, requests = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", virtual={"owner_country": "DE"}),
        config=RunnerConfig(max_shards=1, max_enrich=5),
    )

    assert [item.repo_id for item in payload.items] == [2]
    assert any(path_of(request) == "/users/alice" for request in requests)
    assert not any(path_of(request) == "/users/bob" for request in requests)
    with clean.connect() as connection:
        stored = {
            row["login"]: (row["country_iso"], row["geo_confidence"])
            for row in connection.execute(
                text("SELECT login, country_iso, geo_confidence FROM owners")
            ).mappings()
        }
    assert stored == {"alice": (None, None), "bob": ("DE", "name")}


def test_run_filter_tolerates_tree_fetch_failure(clean: Engine, monkeypatch):
    real_fetch_tree = runner_module.fetch_tree

    def fast_fetch_tree(*args, **kwargs):
        kwargs["sleep"] = lambda _seconds: None
        return real_fetch_tree(*args, **kwargs)

    monkeypatch.setattr(runner_module, "fetch_tree", fast_fetch_tree)

    def handler(request: httpx.Request):
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 1)
            return page_response([repo_item(1)])
        if path == "/graphql":
            if is_file_query(request):
                return httpx.Response(
                    200,
                    json={
                        "errors": [{"message": "Something went wrong", "path": ["n0"]}],
                        "data": {"n0": None},
                    },
                )
            return graphql_batch_response(request, {"owner1/repo1": repo_item(1)})
        if "/git/trees/" in path:
            return httpx.Response(500)
        if path == "/repos/owner1/repo1":
            return httpx.Response(200, json=repo_item(1))
        return httpx.Response(404)

    client, requests = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", virtual={"has_dockerfile": True}),
        config=RunnerConfig(max_shards=1, max_enrich=5),
    )

    assert payload.items == []
    assert payload.incomplete is True
    assert any(
        "Dockerfile presence could not be checked" in warning for warning in payload.warnings
    )
    assert (
        "1 repo(s) skipped because Dockerfile presence could not be checked; "
        "results are incomplete" in payload.warnings
    )
    file_requests = [
        request for request in requests if request.url.path == "/graphql" and is_file_query(request)
    ]
    assert len(file_requests) == 1
    tree_requests = [path_of(request) for request in requests if "/git/trees/" in path_of(request)]
    assert tree_requests
    assert all(path == "/repos/owner1/repo1/git/trees/main" for path in tree_requests)


def test_run_filter_tolerates_owner_fetch_sso_partial_results(clean: Engine):
    page = [repo_item(1, login="alice", stars=30), repo_item(2, login="bob", stars=20)]

    def handler(request: httpx.Request):
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 2)
            return page_response(page)
        if path == "/graphql":
            if is_owner_query(request):
                return graphql_owner_response(request, {"bob": "Germany"})
            return graphql_batch_response(request, {item["full_name"]: item for item in page})
        if path.startswith("/repos/"):
            login = path.split("/")[2]
            repo_id = int(path.rsplit("repo", 1)[1])
            return httpx.Response(200, json=repo_item(repo_id, login=login))
        if path == "/users/alice":
            return httpx.Response(
                200,
                json={"login": "alice", "location": "Berlin, Germany"},
                headers={"x-github-sso": "required; partial-results"},
            )
        if path == "/users/bob":
            return httpx.Response(200, json={"login": "bob", "location": "Germany"})
        return httpx.Response(404)

    client, requests = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", virtual={"owner_country": "DE"}),
        config=RunnerConfig(max_shards=1, max_enrich=5),
    )

    assert [item.repo_id for item in payload.items] == [2]
    assert payload.incomplete is True
    assert any(path_of(request) == "/users/alice" for request in requests)
    assert not any(path_of(request) == "/users/bob" for request in requests)


def test_run_filter_geo_batches_and_falls_back_per_owner(clean: Engine):
    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 2)
            return page_response([repo_item(1, login="alice"), repo_item(2, login="bob")])
        if path == "/graphql":
            return graphql_owner_response(request, {"alice": "Berlin"})
        if path == "/users/bob":
            return httpx.Response(200, json={"location": "Lagos"})
        raise AssertionError(f"unexpected path {path}")

    client, requests = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", virtual={"owner_country": "DE"}),
    )
    assert [item.repo_id for item in payload.items] == [1]
    owner_requests = [
        request
        for request in requests
        if path_of(request) == "/graphql" and is_owner_query(request)
    ]
    assert len(owner_requests) == 1
    assert owner_logins(owner_requests[0]) == ["alice", "bob"]
    assert [path_of(request) for request in requests].count("/users/bob") == 1
    owners_stats = payload.field_stats["graphql"]["owners"]
    assert owners_stats["values"] == 2
    assert owners_stats["fallbacks"] == 1


@pytest.mark.parametrize("error", [ThrottledError(1.0), httpx.ConnectError("network down")])
def test_fetch_owner_location_returns_failure_when_request_raises(
    clean: Engine, monkeypatch, error
):
    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(runner_module, "request_with_retry", boom)
    client, _ = scripted(lambda request: httpx.Response(200))
    deps = make_deps(clean, client)

    assert runner_module._fetch_owner_location(deps, "alice", lambda *args: None) == (False, None)


def test_run_filter_persists_confirmed_no_location_owners_as_unmatched(clean: Engine):
    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 1)
            return page_response([repo_item(1, login="alice")])
        if path == "/graphql":
            if is_owner_query(request):
                return graphql_owner_response(request, {"alice": None})
            return graphql_batch_response(request, {"alice/repo1": repo_item(1, login="alice")})
        raise AssertionError(f"unexpected path {path}")

    client, requests = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", virtual={"owner_country": "DE"}),
    )
    assert payload.items == []
    owner_requests = [
        request
        for request in requests
        if path_of(request) == "/graphql" and is_owner_query(request)
    ]
    assert len(owner_requests) == 1
    with clean.connect() as connection:
        stored = (
            connection.execute(
                text(
                    "SELECT location_raw, country_iso, geo_confidence FROM owners "
                    "WHERE login = 'alice'"
                )
            )
            .mappings()
            .one()
        )
    assert stored["location_raw"] is None
    assert stored["country_iso"] is None
    assert stored["geo_confidence"] == "unmatched"

    def cached_handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 1)
            return page_response([repo_item(1, login="alice")])
        if path == "/graphql":
            if is_owner_query(request):
                raise AssertionError("cached unmatched owner must not be re-fetched")
            return graphql_batch_response(request, {"alice/repo1": repo_item(1, login="alice")})
        raise AssertionError(f"unexpected path {path}")

    cached_client, cached_requests = scripted(cached_handler)
    cached_payload = run_filter(
        make_deps(clean, cached_client),
        spec_for(q="language:python", virtual={"owner_country": "DE"}),
    )
    assert cached_payload.items == []
    assert not any(
        path_of(request) == "/graphql" and is_owner_query(request) for request in cached_requests
    )
    assert not any(path_of(request) == "/users/alice" for request in cached_requests)


def test_build_deps_requires_a_token(clean: Engine, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKENS", raising=False)
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    with pytest.raises(ValueError):
        build_deps(clean, redis_client=fakeredis.FakeRedis(), client=client)


def test_build_deps_fingerprints_the_token_and_wires_the_stack(clean: Engine):
    from lib.audit import AuditBuffer

    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    redis = fakeredis.FakeRedis()
    deps = build_deps(clean, token="sekret", redis_client=redis, client=client)

    assert deps.token_fp == token_fingerprint("sekret")
    assert deps.client is client
    assert deps.redis is redis
    assert deps.limiter is not None
    assert isinstance(deps.audit_buffer, AuditBuffer)


def test_run_filter_flushes_the_audit_buffer_at_run_end(clean: Engine, monkeypatch):
    from lib.audit import AuditBuffer

    def boom(engine, record):
        raise RuntimeError("record_audit should not run while a buffer is wired")

    monkeypatch.setattr(runner_module.audit, "record_audit", boom)

    client, _ = scripted(lambda request: count_response(request, 0))
    deps = dataclasses.replace(
        make_deps(clean, client), audit_buffer=AuditBuffer(clean, batch_size=100)
    )
    payload = run_filter(deps, spec_for(q="topic:ai"), config=RunnerConfig(max_shards=1))

    assert payload.total_count == 0
    with clean.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM audit_log")) == 1


def test_run_filter_flushes_the_audit_buffer_when_the_run_fails(clean: Engine, monkeypatch):
    from lib.audit import AuditBuffer

    def boom(engine, record):
        raise RuntimeError("record_audit should not run while a buffer is wired")

    monkeypatch.setattr(runner_module.audit, "record_audit", boom)

    client, _ = scripted(lambda request: httpx.Response(404))
    deps = dataclasses.replace(
        make_deps(clean, client), audit_buffer=AuditBuffer(clean, batch_size=100)
    )
    with pytest.raises(pipeline.RequestFailed):
        run_filter(deps, spec_for(q="topic:ai"), config=RunnerConfig(max_shards=1))
    with clean.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM audit_log")) == 1


def test_make_runner_parses_filter_spec_dicts(clean: Engine):
    def handler(request: httpx.Request):
        if is_count(request):
            return count_response(request, 0)
        return httpx.Response(404)

    client, requests = scripted(handler)
    runner = make_runner(make_deps(clean, client), config=RunnerConfig(max_shards=1))
    payload = runner(7, {"gitcrawl_filter": 1, "q": "language:rust"})

    assert payload.total_count == 0
    assert payload.items == []
    assert len(requests) == 1
    assert all(is_count(request) for request in requests)


def test_run_filter_uses_cost_plan_order_and_reports_field_stats(clean: Engine, monkeypatch):
    page = [repo_item(1, login="alice", stars=30), repo_item(2, login="bob", stars=20)]
    blob_paths = {"alice/repo1": ["Dockerfile"]}
    events: list[tuple[str, str]] = []

    def handler(request: httpx.Request):
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 2)
            return page_response(page)
        if path == "/graphql":
            if is_file_query(request):
                events.append(("file", json.loads(request.content)["query"]))
                return graphql_file_response(request, blob_paths, "Dockerfile")
            return graphql_batch_response(request, {item["full_name"]: item for item in page})
        if path.startswith("/repos/"):
            login = path.split("/")[2]
            repo_id = int(path.rsplit("repo", 1)[1])
            return httpx.Response(200, json=repo_item(repo_id, login=login))
        if path.startswith("/users/"):
            login = path.rsplit("/", 1)[1]
            events.append(("user", login))
            location = "Germany" if login == "alice" else "Berlin"
            return httpx.Response(200, json={"login": login, "location": location})
        return httpx.Response(404)

    real_plan = runner_module.plan_enrichment
    plans: list[tuple[tuple[str, ...], str]] = []

    def spy(requested, *, depth="page"):
        plans.append((tuple(requested), depth))
        return real_plan(requested, depth=depth)

    monkeypatch.setattr(runner_module, "plan_enrichment", spy)
    client, _ = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(
            q="language:python",
            virtual={"has_dockerfile": True, "owner_country": "DE", "min_geo_confidence": "name"},
        ),
        config=RunnerConfig(max_shards=1, max_enrich=5),
    )

    assert plans == [(("has_dockerfile", "owner_country", "min_geo_confidence"), "page")]
    assert [kind for kind, _ in events] == ["user", "user", "file"]
    assert events[0] == ("user", "alice")
    assert events[1] == ("user", "bob")
    file_events = [query for kind, query in events if kind == "file"]
    assert 'owner: "alice", name: "repo1"' in file_events[0]
    assert 'name: "repo2"' not in file_events[0]
    assert [item.repo_id for item in payload.items] == [1]
    field_stats = dict(payload.field_stats)
    field_stats.pop("graphql")
    field_stats.pop("points")
    assert field_stats == {
        "per_field_sources": {"owner_country": 1, "has_dockerfile": 1},
        "calls_spent": {"owner_country": 2, "has_dockerfile": 1},
        "requeues": 0,
        "warnings": [],
    }
    assert "graphql" in payload.field_stats


def test_run_filter_field_stats_flow_into_the_bundle(clean: Engine, tmp_path):
    page = [repo_item(1, stars=11, topics=("rust",)), repo_item(2, stars=3, topics=("rust",))]

    def handler(request: httpx.Request):
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 2)
            return page_response(page)
        if path == "/graphql":
            return graphql_batch_response(
                request, {"owner1/repo1": repo_item(1, stars=11, topics=("rust",))}
            )
        if path == "/repos/owner1/repo1":
            return httpx.Response(200, json=repo_item(1, stars=11, topics=("rust",)))
        return httpx.Response(404)

    client, _ = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", virtual={"min_stars": 5, "team_topic": "rust"}),
        config=RunnerConfig(max_shards=1),
    )

    field_stats = dict(payload.field_stats)
    field_stats.pop("graphql")
    field_stats.pop("points")
    assert field_stats == {
        "per_field_sources": {"min_stars": 1, "team_topic": 1},
        "calls_spent": {"min_stars": 0, "team_topic": 0},
        "requeues": 0,
        "warnings": [],
    }
    assert "graphql" in payload.field_stats

    run_id = create_run(clean, {"gitcrawl_filter": 1, "q": "language:python"}, api_version="v1")
    execute_run(clean, run_id, runner=lambda rid, spec: payload, runs_root=str(tmp_path))
    status = run_status(clean, run_id)
    bundle = json.loads((Path(status["bundle_dir"]) / "bundle.json").read_text(encoding="utf-8"))
    assert bundle["field_stats"] == payload.field_stats


def test_run_filter_persists_per_phase_points_and_audit_attribution(clean: Engine):
    item = repo_item(1)

    def handler(request: httpx.Request):
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 1)
            return page_response([item])
        if path == "/graphql":
            return graphql_batch_response(
                request,
                {item["full_name"]: item},
                rate_limit={"cost": 1, "used": 412, "remaining": 4588},
                headers={"x-ratelimit-used": "412"},
            )
        raise AssertionError(f"unexpected path {path}")

    client, _ = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python"),
        config=RunnerConfig(max_shards=1, max_enrich=0),
        run_id=42,
    )
    points = payload.field_stats["points"]
    assert points["count"]["points"] == 1
    assert points["discovery"]["points"] == 1
    assert points["hydration"]["points"] == 1
    with clean.connect() as connection:
        phases = set(connection.execute(text("SELECT DISTINCT phase FROM audit_log")).scalars())
        run_ids = set(connection.execute(text("SELECT DISTINCT run_id FROM audit_log")).scalars())
        used = connection.execute(
            text("SELECT rl_used FROM audit_log WHERE phase = 'hydration'")
        ).scalar()
    assert {"count", "discovery", "hydration"} <= phases
    assert run_ids == {42}
    assert used == 412


def test_run_filter_reports_discovery_upsert_counters(clean: Engine, monkeypatch):
    from discover.pipeline import DiscoveryStats

    stats = DiscoveryStats(
        shards=1,
        pages=1,
        fetched=2,
        inserted=1,
        updated=2,
        unchanged=3,
        skipped=4,
    )
    monkeypatch.setattr(runner_module.pipeline, "count_total", lambda *args, **kwargs: 10)
    monkeypatch.setattr(
        runner_module.pipeline, "run_search_discovery", lambda *args, **kwargs: stats
    )
    client, _ = scripted(lambda request: httpx.Response(500))
    payload = run_filter(make_deps(clean, client), spec_for(q="language:python"))

    assert payload.updated == 2
    assert payload.unchanged == 3
    assert payload.skipped == 4


def test_enrich_handlers_clamp_unsupported_full_depth_fields_without_raising(clean: Engine):
    handlers, unsupported = runner_module._enrich_handlers(
        None,
        [],
        {"min_stars": 5, "funding": True},
        {"remaining": 0},
        None,
        {"geo": 0, "dockerfile": 0},
        {},
        depth="full",
    )

    assert list(handlers) == ["min_stars"]
    assert unsupported == ["funding"]


def test_run_filter_surfaces_dropped_segments_as_incomplete(clean: Engine, monkeypatch):
    page = [repo_item(1, stars=30)]

    def handler(request: httpx.Request):
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 1)
            return page_response(page)
        if path == "/graphql":
            return graphql_batch_response(request, {"owner1/repo1": repo_item(1)})
        if path == "/repos/owner1/repo1":
            return httpx.Response(200, json=repo_item(1))
        return httpx.Response(404)

    def always_fail(*args, **kwargs):
        raise RuntimeError("lane down")

    monkeypatch.setattr(runner_module, "_apply_dockerfile", always_fail)
    client, _ = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", virtual={"has_dockerfile": True}),
        config=RunnerConfig(max_shards=1),
    )

    assert payload.items == []
    assert payload.incomplete is True
    assert payload.field_stats["requeues"] == 1
    assert len(payload.field_stats["warnings"]) == 1
    dropped = payload.field_stats["warnings"][0]
    assert "dropped" in dropped
    assert "`has_dockerfile`" in dropped
    assert dropped in payload.warnings


def test_run_filter_batches_hydration_and_isolates_a_bad_repo(clean: Engine, monkeypatch):
    real_refresh = runner_module.refresh_repos_batched

    def fast_refresh(*args, **kwargs):
        kwargs.setdefault("sleep", lambda _seconds: None)
        return real_refresh(*args, **kwargs)

    monkeypatch.setattr(runner_module, "refresh_repos_batched", fast_refresh)

    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 3)
            return page_response([repo_item(index) for index in (1, 2, 3)])
        if path == "/graphql":
            return graphql_batch_response(
                request,
                {"owner1/repo1": repo_item(1), "owner3/repo3": repo_item(3)},
            )
        if path == "/repos/owner2/repo2":
            return httpx.Response(500, json={"message": "boom"})
        raise AssertionError(f"unexpected path {path}")

    client, requests = scripted(handler)
    deps = make_deps(clean, client)
    payload = run_filter(deps, spec_for(q="language:python"))
    assert payload.fetched == 3
    assert payload.field_stats["graphql"]["hydration"]["unresolved"] == 1
    assert any("could not be hydrated" in warning for warning in payload.warnings)
    graphql_requests = [
        request
        for request in requests
        if path_of(request) == "/graphql" and not is_search_request(request)
    ]
    assert len(graphql_requests) == 1


def test_run_filter_reports_batch_counts_in_field_stats(clean: Engine):
    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 1)
            return page_response([repo_item(1)])
        if path == "/graphql":
            return graphql_batch_response(request, {"owner1/repo1": repo_item(1)})
        raise AssertionError(f"unexpected path {path}")

    client, _requests = scripted(handler)
    payload = run_filter(make_deps(clean, client), spec_for(q="language:python"))
    hydration = payload.field_stats["graphql"]["hydration"]
    assert hydration["keys"] == 1
    assert hydration["values"] == 1
    assert hydration["requests"] == 1
    assert hydration["deadline_hit"] is False


def test_run_filter_records_stage_timings(clean: Engine):
    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 1)
            return page_response([repo_item(1)])
        if path == "/graphql":
            return graphql_batch_response(request, {"owner1/repo1": repo_item(1)})
        raise AssertionError(f"unexpected path {path}")

    client, _requests = scripted(handler)
    payload = run_filter(make_deps(clean, client), spec_for(q="language:python"))
    stages = {
        "count",
        "discovery",
        "candidates",
        "hydration",
        "hydration_fetch",
        "hydration_apply",
        "enrich",
        "sort_payload",
    }
    assert stages <= set(payload.timings)
    assert all(isinstance(value, float) and value >= 0.0 for value in payload.timings.values())


def test_run_filter_reports_the_saving_phase(clean: Engine):
    from lib import progress as progress_module

    reports: list[tuple[str, int, int | None]] = []
    token = progress_module.bind(
        lambda phase, done, total, counters: reports.append((phase, done, total))
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 1)
            return page_response([repo_item(1)])
        if path == "/graphql":
            return graphql_batch_response(request, {"owner1/repo1": repo_item(1)})
        raise AssertionError(f"unexpected path {path}")

    try:
        client, _requests = scripted(handler)
        run_filter(make_deps(clean, client), spec_for(q="language:python"))
    finally:
        progress_module.reset(token)

    phases = [phase for phase, _done, _total in reports]
    assert "saving" in phases
    assert phases.index("saving") < phases.index("enriching")
    assert ("saving", 1, 1) in reports


def test_runner_config_from_maps_settings(clean: Engine):
    from serve.runner import runner_config_from
    from store.settings import update_run_settings

    settings = update_run_settings(
        clean,
        {
            "max_shards": 1,
            "max_candidates": 2,
            "max_hydrate": 1,
            "graphql_batch": False,
            "discovery_concurrency": 48,
        },
    )
    config = runner_config_from(settings)
    assert config.max_shards == 1
    assert config.max_candidates == 2
    assert config.max_hydrate == 1
    assert config.graphql_batch is False
    assert config.discovery_concurrency == 48


def test_corpus_profile_hydrates_at_twenty_with_matching_limiter_cap(clean: Engine):
    from serve.runner import build_deps, runner_config_from
    from serve.settings_spec import parse_settings_form
    from store.settings import update_run_settings

    settings = update_run_settings(clean, parse_settings_form({"preset": "corpus"}))
    config = runner_config_from(settings)
    deps = build_deps(
        clean,
        token="t",
        redis_client=fakeredis.FakeRedis(),
        max_concurrent=settings.limiter_max_concurrent,
    )
    assert settings.limiter_max_concurrent == 20
    assert config.concurrency == 20
    assert deps.limiter is not None
    assert config.concurrency <= deps.limiter.max_concurrent

    raised = update_run_settings(clean, {"limiter_max_concurrent": 40})
    assert runner_config_from(raised).concurrency == 20


def test_build_deps_honors_max_concurrent(clean: Engine):
    deps = build_deps(clean, token="t", redis_client=fakeredis.FakeRedis(), max_concurrent=2)
    assert isinstance(deps.limiter, BucketLimiter)
    assert deps.limiter.max_concurrent == 2


def test_run_filter_with_batching_disabled_hydrates_via_rest(clean: Engine):
    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 1)
            return page_response([repo_item(1)])
        if path == "/graphql":
            raise AssertionError("graphql batching disabled must skip /graphql")
        if path.startswith("/repos/owner1/repo1"):
            return httpx.Response(200, json=repo_item(1))
        raise AssertionError(f"unexpected path {path}")

    client, requests = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python"),
        config=RunnerConfig(max_shards=1, graphql_batch=False),
    )

    assert [item.repo_id for item in payload.items] == [1]
    assert all(request.url.path != "/graphql" or is_search_request(request) for request in requests)
    hydration = payload.field_stats["graphql"]["hydration"]
    assert hydration["requests"] == 0
    assert hydration["handled"] == 1


def test_load_owners_chunks_large_id_lists(clean_db, monkeypatch):
    import serve.runner as runner_module

    owners = [{"id": index, "login": f"o{index}", "type": "User"} for index in range(1, 6)]
    engine = clean_db(owners=owners)
    monkeypatch.setattr(runner_module, "_ID_BATCH", 2)

    loaded = runner_module._load_owners(engine, [1, 2, 3, 4, 5])

    assert set(loaded) == {1, 2, 3, 4, 5}
