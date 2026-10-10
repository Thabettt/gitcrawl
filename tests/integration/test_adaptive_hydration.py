from __future__ import annotations

import json
import re

import httpx
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

from hydrate.tail import refresh_repos_batched
from limiter.adaptive import AdaptiveConfig, AdaptiveController

ALIAS_RE = re.compile(r'(n\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\)')


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


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


def graphql_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    data: dict[str, object] = {}
    for alias, _owner, name in ALIAS_RE.findall(body["query"]):
        data[alias] = graphql_node(int(name.removeprefix("repo")))
    return httpx.Response(200, json={"data": data})


def test_adaptive_hydration_shrinks_after_a_timeout_and_saves_every_repo(clean_db):
    engine = clean_db()
    seed_repos(engine, 25)
    clock = FakeClock()
    controller = AdaptiveController(
        AdaptiveConfig(initial_batch=20, short_pause_seconds=0.0, pause_jitter_seconds=0.0),
        now=clock,
        rng=lambda: 0.0,
    )
    state = {"failed": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if not state["failed"]:
            state["failed"] = True
            return httpx.Response(504, json={"message": "timeout"})
        return graphql_handler(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = refresh_repos_batched(
        engine, client, rows_for(25), adaptive=controller, now=clock, sleep=clock.advance
    )
    assert stats.refreshed == 25
    assert stats.unresolved == {}
    assert stats.batch["deferred"] == 0
    assert stats.batch["adaptive"]["window"] == 10  # 20 -> 10 on the first timeout
    assert stats.batch["adaptive"]["batch"] == 14  # 20 -> 14


def test_adaptive_hydration_defers_at_the_reserve_floor(clean_db):
    engine = clean_db()
    seed_repos(engine, 20)
    clock = FakeClock()
    controller = AdaptiveController(
        AdaptiveConfig(initial_window=1, min_window=1, max_window=1, initial_batch=10),
        now=clock,
        rng=lambda: 0.0,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        data: dict[str, object] = {}
        for alias, _owner, name in ALIAS_RE.findall(body["query"]):
            data[alias] = graphql_node(int(name.removeprefix("repo")))
        return httpx.Response(
            200,
            json={"data": data},
            headers={"x-ratelimit-remaining": "250", "x-ratelimit-reset": "1600"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = refresh_repos_batched(
        engine, client, rows_for(20), adaptive=controller, now=clock, sleep=clock.advance
    )
    assert stats.refreshed == 10
    assert len(stats.unresolved) == 10
    assert all(reason.startswith("deferred:") for reason in stats.unresolved.values())
    assert stats.batch["deferred"] == 10
    assert stats.batch["adaptive"]["pacer_remaining"] == 250
