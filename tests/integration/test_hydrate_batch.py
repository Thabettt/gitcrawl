from __future__ import annotations

import json
import re

import httpx
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

from hydrate.tail import refresh_repos_batched
from lib.graphql_batch import GraphQLAuthError

ALIAS_RE = re.compile(r'(n\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\)')


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


def seed_repos(engine: Engine, count: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO owners (id, login, type) VALUES (901, 'octo', 'User')")
        )
        for repo_id in range(1, count + 1):
            connection.execute(
                text(
                    "INSERT INTO repos (id, node_id, full_name, owner_id, name, visibility)"
                    " VALUES (:id, :node, :full, 901, :name, 'public')"
                ),
                {
                    "id": repo_id,
                    "node": f"R_{repo_id}",
                    "full": f"octo/repo{repo_id}",
                    "name": f"repo{repo_id}",
                },
            )


def rows_for(count: int) -> list[dict]:
    return [{"id": repo_id, "full_name": f"octo/repo{repo_id}"} for repo_id in range(1, count + 1)]


def graphql_node(repo_id: int) -> dict:
    return {
        "databaseId": repo_id,
        "id": f"R_{repo_id}",
        "name": f"repo{repo_id}",
        "nameWithOwner": f"octo/repo{repo_id}",
        "stargazerCount": repo_id,
        "forkCount": 0,
        "watchers": {"totalCount": 0},
        "issues": {"totalCount": 0},
        "diskUsage": 1,
        "isArchived": False,
        "isDisabled": False,
        "isFork": False,
        "isTemplate": False,
        "visibility": "PUBLIC",
        "description": None,
        "homepageUrl": None,
        "pushedAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
        "createdAt": "2024-01-01T00:00:00Z",
        "defaultBranchRef": {
            "name": "main",
            "target": {"history": {"totalCount": repo_id * 10}},
        },
        "primaryLanguage": {"name": "Rust"},
        "licenseInfo": None,
        "repositoryTopics": {"nodes": []},
        "hasIssuesEnabled": True,
        "hasWikiEnabled": False,
        "hasProjectsEnabled": False,
        "hasDiscussionsEnabled": False,
        "hasPullRequestsEnabled": True,
        "parent": None,
        "owner": {"databaseId": 901, "login": "octo", "__typename": "User"},
    }


def graphql_handler(seen: list[str], *, bad_repos: frozenset[int] = frozenset()):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        body = json.loads(request.content)
        matches = ALIAS_RE.findall(body["query"])
        data: dict[str, object] = {}
        errors: list[dict] = []
        for alias, _owner, name in matches:
            repo_id = int(name.removeprefix("repo"))
            if repo_id in bad_repos:
                data[alias] = None
                errors.append({"message": "Could not resolve to a Repository", "path": [alias]})
            else:
                data[alias] = graphql_node(repo_id)
        payload: dict = {"data": data}
        if errors:
            payload["errors"] = errors
        return httpx.Response(200, json=payload)

    return handler


def test_batched_hydration_saves_every_repo_in_one_request(clean_db):
    engine = clean_db()
    seed_repos(engine, 25)
    seen: list[str] = []
    client = httpx.Client(transport=httpx.MockTransport(graphql_handler(seen)))
    stats = refresh_repos_batched(engine, client, rows_for(25))
    assert stats.refreshed == 25
    assert stats.unresolved == {}
    assert stats.commit_counts == {str(repo_id): repo_id * 10 for repo_id in range(1, 26)}
    assert seen == ["/graphql", "/graphql"]  # 20 + 5
    with engine.connect() as connection:
        count = connection.scalar(text("SELECT count(*) FROM repos WHERE stargazers IS NOT NULL"))
    assert count == 25


def test_batched_hydration_counts_renames(clean_db):
    engine = clean_db()
    seed_repos(engine, 1)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        data: dict[str, object] = {}
        for alias, _owner, _name in ALIAS_RE.findall(body["query"]):
            node = graphql_node(1)
            node["name"] = "renamed"
            node["nameWithOwner"] = "octo/renamed"
            data[alias] = node
        return httpx.Response(200, json={"data": data})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = refresh_repos_batched(engine, client, rows_for(1))
    assert stats.refreshed == 1
    assert stats.renamed == 1
    with engine.connect() as connection:
        history = (
            connection.execute(text("SELECT full_name FROM full_name_history WHERE repo_id = 1"))
            .scalars()
            .all()
        )
    assert history == ["octo/repo1"]


def test_batched_hydration_reports_apply_progress(clean_db):
    engine = clean_db()
    seed_repos(engine, 25)
    seen: list[str] = []
    reports: list[tuple[int, int]] = []
    client = httpx.Client(transport=httpx.MockTransport(graphql_handler(seen)))
    stats = refresh_repos_batched(
        engine,
        client,
        rows_for(25),
        apply_batch_size=10,
        on_apply_progress=lambda done, total: reports.append((done, total)),
    )
    assert stats.refreshed == 25
    assert reports == [(0, 25), (10, 25), (20, 25), (25, 25)]


def test_apply_batch_size_must_be_positive(clean_db):
    engine = clean_db()
    client = httpx.Client(transport=httpx.MockTransport(graphql_handler([])))
    with pytest.raises(ValueError):
        refresh_repos_batched(engine, client, [], apply_batch_size=0)


def test_batched_hydration_reports_fetch_and_apply_seconds(clean_db):
    engine = clean_db()
    seed_repos(engine, 1)
    ticks = iter([0.0, 3.0, 5.0, 9.0])
    client = httpx.Client(transport=httpx.MockTransport(graphql_handler([])))
    stats = refresh_repos_batched(engine, client, rows_for(1), clock=lambda: next(ticks))
    assert stats.fetch_seconds == 3.0
    assert stats.apply_seconds == 4.0


def test_one_bad_repo_falls_back_to_rest_without_touching_its_neighbours(clean_db):
    engine = clean_db()
    seed_repos(engine, 3)
    graphql_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/graphql":
            return graphql_handler(graphql_seen, bad_repos=frozenset({2}))(request)
        if path == "/repos/octo/repo2":
            return httpx.Response(
                200,
                json={
                    "id": 2,
                    "node_id": "R_2",
                    "full_name": "octo/repo2",
                    "name": "repo2",
                    "owner": {"id": 901, "login": "octo", "type": "User"},
                    "private": False,
                    "topics": [],
                    "stargazers_count": 2,
                    "forks_count": 0,
                    "watchers_count": 0,
                    "open_issues_count": 0,
                    "default_branch": "main",
                    "pushed_at": "2026-01-01T00:00:00Z",
                },
                headers={"etag": 'W/"abc"'},
            )
        if path == "/repos/octo/repo2/commits":
            return httpx.Response(200, json=[{}])
        raise AssertionError(f"unexpected path {path}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = refresh_repos_batched(engine, client, rows_for(3))
    assert stats.refreshed == 3  # 2 batched + 1 fallback
    assert stats.fallbacks == 1
    assert stats.unresolved == {}
    with engine.connect() as connection:
        row = connection.execute(text("SELECT stargazers, etag FROM repos WHERE id = 2")).one()
    assert row.stargazers == 2
    assert row.etag == 'W/"abc"'


def rest_repo_payload(repo_id: int) -> dict:
    return {
        "id": repo_id,
        "node_id": f"R_{repo_id}",
        "full_name": f"octo/repo{repo_id}",
        "name": f"repo{repo_id}",
        "owner": {"id": 901, "login": "octo", "type": "User"},
        "private": False,
        "topics": [],
        "stargazers_count": repo_id,
        "forks_count": 0,
        "watchers_count": 0,
        "open_issues_count": 0,
        "default_branch": "main",
        "pushed_at": "2026-01-01T00:00:00Z",
    }


def test_rest_fallback_reads_commit_count_from_the_last_page_link(clean_db):
    engine = clean_db()
    seed_repos(engine, 2)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/graphql":
            return graphql_handler([], bad_repos=frozenset({2}))(request)
        if path == "/repos/octo/repo2":
            return httpx.Response(200, json=rest_repo_payload(2))
        if path == "/repos/octo/repo2/commits":
            return httpx.Response(
                200,
                json=[{}],
                headers={
                    "Link": (
                        "<https://api.github.com/repositories/2/commits?"
                        'per_page=1&page=77>; rel="last"'
                    )
                },
            )
        raise AssertionError(f"unexpected path {path}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = refresh_repos_batched(engine, client, rows_for(2))
    assert stats.fallbacks == 1
    assert stats.commit_counts["2"] == 77


def test_rest_fallback_counts_returned_items_without_a_link_header(clean_db):
    engine = clean_db()
    seed_repos(engine, 2)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/graphql":
            return graphql_handler([], bad_repos=frozenset({2}))(request)
        if path == "/repos/octo/repo2":
            return httpx.Response(200, json=rest_repo_payload(2))
        if path == "/repos/octo/repo2/commits":
            return httpx.Response(200, json=[{}, {}])
        raise AssertionError(f"unexpected path {path}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = refresh_repos_batched(engine, client, rows_for(2))
    assert stats.fallbacks == 1
    assert stats.commit_counts["2"] == 2


def test_rest_fallback_commit_count_failure_keeps_the_repo_hydrated(clean_db):
    engine = clean_db()
    seed_repos(engine, 1)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/graphql":
            return graphql_handler([], bad_repos=frozenset({1}))(request)
        if path == "/repos/octo/repo1":
            return httpx.Response(200, json=rest_repo_payload(1))
        if path == "/repos/octo/repo1/commits":
            raise httpx.ConnectError("boom")
        raise AssertionError(f"unexpected path {path}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = refresh_repos_batched(engine, client, rows_for(1), sleep=lambda _seconds: None)
    assert stats.refreshed == 1
    assert stats.fallbacks == 1
    assert stats.unresolved == {}
    assert stats.commit_counts == {}


def test_unresolved_repo_is_reported_not_swallowed(clean_db):
    engine = clean_db()
    seed_repos(engine, 1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/graphql":
            return graphql_handler([], bad_repos=frozenset({1}))(request)
        return httpx.Response(500, json={"message": "boom"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = refresh_repos_batched(engine, client, rows_for(1), sleep=lambda _seconds: None)
    assert stats.refreshed == 0
    assert "octo/repo1" in stats.unresolved
    assert stats.batch["unresolved"] == 1


def test_graphql_401_aborts_the_whole_hydration(clean_db):
    engine = clean_db()
    seed_repos(engine, 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GraphQLAuthError):
        refresh_repos_batched(engine, client, rows_for(1))
