from __future__ import annotations

from datetime import datetime
from urllib.parse import parse_qs, urlparse

import fakeredis
import httpx
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from discover.search_shards import RequestFailed
from lib.gh_client import API_VERSION, PartialResultsError, ThrottledError
from serve.app import create_app
from serve.executor import RunPayload, RunPayloadItem, create_run, execute_run
from serve.filter_spec import parse_filter_spec, spec_hash, spec_to_dict
from serve.runner import RunnerConfig, build_deps, make_runner

SEARCH_PATH = "/search/repositories"
SEARCH_HEADERS = {"x-ratelimit-resource": "search"}
ITEM_KEYS = {
    "id",
    "full_name",
    "description",
    "language",
    "license_spdx",
    "topics",
    "stargazers",
    "forks_count",
    "open_issues",
    "pushed_at",
    "owner",
    "has_dockerfile",
    "virtuals",
}
OWNER_KEYS = {"login", "type", "country_iso", "geo_confidence", "raw_location"}


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
        connection.execute(
            text(
                "INSERT INTO owners (id, login, type, location_raw, country_iso, geo_confidence) "
                "VALUES (1, 'octo', 'User', 'Berlin, Germany', 'DE', 'name')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO repos (id, node_id, full_name, owner_id, name, visibility, "
                "description, language, license_spdx, topics, stargazers, forks_count, "
                "open_issues, pushed_at) VALUES (1296269, 'R_1296269', 'octo/hello', 1, 'hello', "
                "'public', 'My first repo', 'Ruby', 'MIT', ARRAY['octocat'], 80, 9, 0, "
                "'2011-01-26T19:06:43Z')"
            )
        )
    return alembic_engine


def payload_item(**overrides) -> RunPayloadItem:
    values: dict[str, object] = {
        "repo_id": 1296269,
        "full_name": "octo/hello",
        "stargazers": 80,
        "pushed_at": "2011-01-26T19:06:43Z",
        "archived": False,
        "language": "Ruby",
        "license_spdx": "MIT",
        "country_iso": "DE",
        "geo_confidence": "name",
        "virtuals": {"has_dockerfile": True},
    }
    values.update(overrides)
    return RunPayloadItem(**values)


def expected_item() -> dict:
    return {
        "id": 1296269,
        "full_name": "octo/hello",
        "description": "My first repo",
        "language": "Ruby",
        "license_spdx": "MIT",
        "topics": ["octocat"],
        "stargazers": 80,
        "forks_count": 9,
        "open_issues": 0,
        "pushed_at": "2011-01-26T19:06:43Z",
        "owner": {
            "login": "octo",
            "type": "User",
            "country_iso": "DE",
            "geo_confidence": "name",
            "raw_location": "Berlin, Germany",
        },
        "has_dockerfile": True,
        "virtuals": {"has_dockerfile": True},
    }


def make_client(
    engine: Engine, runner=None, *, factory=None, clock=lambda: 0.0, runs_root="runs"
) -> TestClient:
    if factory is None:

        def factory(_engine):
            return runner

    application = create_app(
        engine=engine,
        runner_factory=factory,
        runs_root=str(runs_root),
        clock=clock,
    )
    return TestClient(application, raise_server_exceptions=False)


def sso_partial_error() -> PartialResultsError:
    request = httpx.Request("GET", "https://api.github.com/users/alice")
    response = httpx.Response(
        200,
        headers={"x-github-sso": "required; partial-results"},
        request=request,
    )
    return PartialResultsError(response)


def counting_runner(payload: RunPayload, calls: list):
    def runner(run_id: int, filter_spec: dict) -> RunPayload:
        calls.append(filter_spec)
        return payload

    return runner


def repo_item(repo_id: int, *, login: str = "alice", stars: int = 10, topics=()) -> dict:
    return {
        "id": repo_id,
        "node_id": f"R_{repo_id}",
        "name": f"repo{repo_id}",
        "full_name": f"{login}/repo{repo_id}",
        "owner": {"id": repo_id * 10, "login": login, "type": "User"},
        "private": False,
        "description": f"repo {repo_id}",
        "language": "Rust",
        "license": {"spdx_id": "MIT"},
        "topics": list(topics),
        "stargazers_count": stars,
        "forks_count": 1,
        "watchers_count": 1,
        "open_issues_count": 0,
        "default_branch": "main",
        "pushed_at": "2026-01-01T00:00:00Z",
    }


def test_get_unknown_param_is_a_400_and_never_runs(clean: Engine, tmp_path):
    calls: list = []
    client = make_client(clean, counting_runner(RunPayload(0, []), calls), runs_root=tmp_path)

    response = client.get("/vsearch/repos", params={"q": "language:rust", "watchers": "100"})

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_param"
    assert body["param"] == "watchers"
    assert body["hint"]
    assert calls == []


@pytest.mark.parametrize(
    ("params", "param"),
    [
        ({"sort": "watchers"}, "sort"),
        ({"order": "sideways"}, "order"),
        ({"per_page": "0"}, "per_page"),
        ({"per_page": "abc"}, "per_page"),
        ({"page": "0"}, "page"),
        ({"page": "nope"}, "page"),
        ({"min_stars": "abc"}, "min_stars"),
        ({"owner_country": "ZZ"}, "owner_country"),
        ({"has_dockerfile": "yes"}, "has_dockerfile"),
        ({"min_geo_confidence": "sometimes"}, "min_geo_confidence"),
        ({"q": "updated:>2024-01-01"}, "q"),
    ],
)
def test_get_known_params_with_invalid_values_are_400(clean: Engine, tmp_path, params, param):
    calls: list = []
    client = make_client(clean, counting_runner(RunPayload(0, []), calls), runs_root=tmp_path)

    response = client.get("/vsearch/repos", params=params)

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_param"
    assert body["param"] == param
    assert body["hint"]
    assert calls == []


@pytest.mark.parametrize("params", [{"q": ""}, {"q": "   "}, {}])
def test_get_without_a_keyword_or_filter_is_a_400_instead_of_an_upstream_call(
    clean: Engine, tmp_path, params
):
    calls: list = []
    client = make_client(clean, counting_runner(RunPayload(0, []), calls), runs_root=tmp_path)

    response = client.get("/vsearch/repos", params=params)

    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_param",
        "param": "q",
        "hint": "provide at least one keyword or filter",
    }
    assert calls == []


def test_get_happy_path_returns_the_exact_contract_shape(clean: Engine, tmp_path):
    calls: list = []
    payload = RunPayload(total_count=500, fetched=1, items=[payload_item()])
    client = make_client(clean, counting_runner(payload, calls), runs_root=tmp_path)

    response = client.get("/vsearch/repos", params={"q": "language:ruby", "min_stars": "5"})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"total_count", "incomplete", "items"}
    assert body["total_count"] == 1
    assert body["incomplete"] is False
    assert len(body["items"]) == 1
    assert set(body["items"][0]) == ITEM_KEYS
    assert set(body["items"][0]["owner"]) == OWNER_KEYS
    assert body["items"][0] == expected_item()
    assert calls[0]["q"] == "language:ruby"
    assert calls[0]["virtual"] == {"min_stars": 5}


def test_get_slices_pages_from_the_same_cached_payload(clean: Engine, tmp_path):
    now = [1000.0]
    calls: list = []
    items = [payload_item(repo_id=index, full_name=f"octo/repo{index}") for index in (1, 2, 3)]
    client = make_client(
        clean,
        counting_runner(RunPayload(total_count=3, fetched=3, incomplete=True, items=items), calls),
        clock=lambda: now[0],
        runs_root=tmp_path,
    )

    first = client.get("/vsearch/repos", params={"q": "language:rust", "per_page": "2"})
    second = client.get("/vsearch/repos", params={"q": "language:rust", "per_page": "2"})
    third = client.get(
        "/vsearch/repos", params={"q": "language:rust", "per_page": "2", "page": "2"}
    )

    assert first.status_code == second.status_code == third.status_code == 200
    assert first.json()["total_count"] == 3
    assert first.json()["incomplete"] is True
    assert [item["id"] for item in first.json()["items"]] == [3, 2]
    assert [item["id"] for item in second.json()["items"]] == [3, 2]
    assert [item["id"] for item in third.json()["items"]] == [1]
    assert len(calls) == 1

    now[0] += 121.0
    fourth = client.get("/vsearch/repos", params={"q": "language:rust", "per_page": "2"})
    assert fourth.status_code == 200
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("exc", "retry_after_ms"),
    [
        (RequestFailed(500, "upstream body that must never leak"), 0),
        (ThrottledError(12.5), 12500),
        (sso_partial_error(), 0),
    ],
)
def test_get_maps_upstream_failures_to_a_sanitized_502(
    clean: Engine, tmp_path, exc, retry_after_ms
):
    def runner(run_id: int, filter_spec: dict) -> RunPayload:
        raise exc

    client = make_client(clean, runner, runs_root=tmp_path)
    response = client.get("/vsearch/repos", params={"q": "language:rust"})

    assert response.status_code == 502
    assert response.json() == {
        "error": "upstream_unavailable",
        "retry_after_ms": retry_after_ms,
    }
    assert "upstream body" not in response.text


def test_post_run_persists_and_returns_the_run_payload(clean: Engine, tmp_path):
    calls: list = []
    payload = RunPayload(total_count=9, fetched=1, items=[payload_item()])
    client = make_client(clean, counting_runner(payload, calls), runs_root=tmp_path)
    document = {
        "gitcrawl_filter": 1,
        "q": "language:rust",
        "sort": "stars",
        "order": "desc",
        "virtual": {"has_dockerfile": True},
        "page": {"per_page": 100, "max_pages": 10},
    }

    response = client.post("/vsearch/run", json=document)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "filter_hash",
        "ran_at",
        "api_version",
        "total_count",
        "incomplete",
        "items",
    }
    expected_hash = spec_hash(parse_filter_spec(document))
    assert body["filter_hash"] == expected_hash
    assert body["api_version"] == API_VERSION
    assert body["total_count"] == 1
    assert body["incomplete"] is False
    assert body["items"] == [expected_item()]
    assert datetime.fromisoformat(body["ran_at"].replace("Z", "+00:00")).tzinfo is not None
    assert calls[0]["q"] == "language:rust"
    with clean.connect() as connection:
        run = connection.execute(
            text("SELECT filter_hash, status FROM runs WHERE filter_hash = :hash"),
            {"hash": expected_hash},
        ).one()
        assert run == (expected_hash, "done")
        assert connection.scalar(text("SELECT count(*) FROM run_items")) == 1


@pytest.mark.parametrize(
    ("exc", "retry_after_ms"),
    [
        (RequestFailed(500, "upstream body that must never leak"), 0),
        (ThrottledError(2.0), 2000),
        (sso_partial_error(), 0),
    ],
)
def test_post_maps_upstream_failures_to_a_sanitized_502(
    clean: Engine, tmp_path, exc, retry_after_ms
):
    def runner(run_id: int, filter_spec: dict) -> RunPayload:
        raise exc

    client = make_client(clean, runner, runs_root=tmp_path)
    response = client.post("/vsearch/run", json={"gitcrawl_filter": 1, "q": "language:rust"})

    assert response.status_code == 502
    assert response.json() == {
        "error": "upstream_unavailable",
        "retry_after_ms": retry_after_ms,
    }
    assert "upstream body" not in response.text


def test_post_non_upstream_run_failure_is_500_and_never_200(clean: Engine, tmp_path):
    def runner(run_id: int, filter_spec: dict) -> RunPayload:
        raise RuntimeError("boom")

    client = make_client(clean, runner, runs_root=tmp_path)
    response = client.post("/vsearch/run", json={"gitcrawl_filter": 1, "q": "language:rust"})

    assert response.status_code == 500
    assert response.json() == {"error": "run_failed"}
    assert "boom" not in response.text
    with clean.connect() as connection:
        row = connection.execute(text("SELECT status, error FROM runs")).one()
    assert row[0] == "failed"
    assert "boom" in row[1]


def test_post_runner_factory_failure_is_500_without_orphan_run(clean: Engine, tmp_path):
    def factory(_engine):
        raise ValueError("no GitHub token configured")

    client = make_client(clean, factory=factory, runs_root=tmp_path)
    response = client.post("/vsearch/run", json={"gitcrawl_filter": 1, "q": "language:rust"})

    assert response.status_code == 500
    assert response.json() == {"error": "internal_error"}
    with clean.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM runs")) == 0


def test_post_invalid_spec_is_400_with_errors_and_hints(clean: Engine, tmp_path):
    calls: list = []
    client = make_client(clean, counting_runner(RunPayload(0, []), calls), runs_root=tmp_path)

    response = client.post("/vsearch/run", json={"gitcrawl_filter": 1, "q": "updated:>2024"})

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_param"
    assert body["errors"]
    assert body["hints"]
    assert calls == []
    with clean.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM runs")) == 0


def test_replay_returns_the_latest_run_and_404_when_unknown(clean: Engine, tmp_path):
    calls: list = []
    payload = RunPayload(total_count=9, fetched=1, items=[payload_item()])
    client = make_client(clean, counting_runner(payload, calls), runs_root=tmp_path)
    document = {"gitcrawl_filter": 1, "q": "language:rust"}

    created = client.post("/vsearch/run", json=document)
    assert created.status_code == 200
    filter_hash = created.json()["filter_hash"]

    replayed = client.get(f"/vsearch/runs/{filter_hash}")
    assert replayed.status_code == 200
    assert replayed.json() == created.json()

    missing = client.get("/vsearch/runs/" + "0" * 64)
    assert missing.status_code == 404
    assert missing.json()["error"] == "run_not_found"


def test_replay_uses_the_latest_completed_run_when_the_newest_run_failed(clean: Engine, tmp_path):
    calls: list = []
    payload = RunPayload(total_count=9, fetched=1, items=[payload_item()])
    client = make_client(clean, counting_runner(payload, calls), runs_root=tmp_path)
    document = {"gitcrawl_filter": 1, "q": "language:rust"}

    spec = parse_filter_spec(document)
    created = client.post("/vsearch/run", json=document)
    assert created.status_code == 200
    filter_hash = created.json()["filter_hash"]
    failed_id = create_run(clean, spec_to_dict(spec), api_version=API_VERSION)

    def failing(_run_id: int, _spec: dict) -> RunPayload:
        raise RuntimeError("upstream exploded")

    execute_run(clean, failed_id, runner=failing, runs_root=str(tmp_path))

    replayed = client.get(f"/vsearch/runs/{filter_hash}")

    assert replayed.status_code == 200
    assert replayed.json() == created.json()


def test_replay_is_409_when_only_non_terminal_runs_exist(clean: Engine, tmp_path):
    calls: list = []
    client = make_client(clean, counting_runner(RunPayload(0, []), calls), runs_root=tmp_path)
    document = {"gitcrawl_filter": 1, "q": "language:rust"}
    filter_hash = spec_hash(parse_filter_spec(document))
    create_run(clean, spec_to_dict(parse_filter_spec(document)), api_version=API_VERSION)

    response = client.get(f"/vsearch/runs/{filter_hash}")

    assert response.status_code == 409
    assert response.json() == {"error": "run_not_ready", "filter_hash": filter_hash}


def test_sort_order_is_shared_by_get_post_and_replay(clean: Engine, tmp_path):
    with clean.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO owners (id, login, type) VALUES "
                "(2, 'octo2', 'User'), (3, 'octo3', 'User')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO repos (id, node_id, full_name, owner_id, name, visibility, "
                "stargazers, forks_count, pushed_at) VALUES "
                "(2, 'R_2', 'octo2/a', 2, 'a', 'public', 5, 50, '2020-01-01T00:00:00Z'), "
                "(3, 'R_3', 'octo3/b', 3, 'b', 'public', 1, 1, '2021-01-01T00:00:00Z')"
            )
        )
    calls: list = []
    items = [
        payload_item(),
        payload_item(
            repo_id=3, full_name="octo3/b", stargazers=1, pushed_at="2021-01-01T00:00:00Z"
        ),
        payload_item(
            repo_id=2, full_name="octo2/a", stargazers=5, pushed_at="2020-01-01T00:00:00Z"
        ),
    ]
    client = make_client(
        clean,
        counting_runner(RunPayload(total_count=3, fetched=3, items=items), calls),
        runs_root=tmp_path,
    )

    desc = client.get("/vsearch/repos", params={"q": "language:rust", "sort": "forks"})
    asc = client.get(
        "/vsearch/repos", params={"q": "language:rust", "sort": "forks", "order": "asc"}
    )

    assert [item["id"] for item in desc.json()["items"]] == [2, 1296269, 3]
    assert [item["id"] for item in asc.json()["items"]] == [3, 1296269, 2]

    document = {"gitcrawl_filter": 1, "q": "language:rust", "sort": "forks", "order": "desc"}
    created = client.post("/vsearch/run", json=document)
    assert created.status_code == 200
    post_ids = [item["id"] for item in created.json()["items"]]
    assert post_ids == [2, 1296269, 3]

    replayed = client.get(f"/vsearch/runs/{created.json()['filter_hash']}")
    assert replayed.status_code == 200
    assert [item["id"] for item in replayed.json()["items"]] == post_ids


def test_get_never_forwards_unknown_params_upstream(clean: Engine, tmp_path):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = urlparse(str(request.url)).path
        params = parse_qs(urlparse(str(request.url)).query)
        if path == SEARCH_PATH:
            if params.get("per_page") == ["1"]:
                return httpx.Response(
                    200,
                    json={"total_count": 1, "items": [], "incomplete_results": False},
                    headers=SEARCH_HEADERS,
                )
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "items": [repo_item(7001, topics=("rust",))],
                    "incomplete_results": False,
                },
                headers=SEARCH_HEADERS,
            )
        if path == "/repos/alice/repo7001":
            return httpx.Response(200, json=repo_item(7001, topics=("rust",)))
        if path == "/repos/alice/repo7001/git/trees/main":
            return httpx.Response(
                200,
                json={"tree": [{"path": "Dockerfile", "type": "blob"}], "truncated": False},
            )
        if path == "/users/alice":
            return httpx.Response(200, json={"login": "alice", "location": "Berlin, Germany"})
        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    deps = build_deps(clean, token="tok", redis_client=fakeredis.FakeRedis(), client=mock_client)
    runner = make_runner(deps, config=RunnerConfig(max_shards=2, max_candidates=10, max_enrich=5))
    client = make_client(clean, runner, runs_root=tmp_path)

    response = client.get(
        "/vsearch/repos",
        params={
            "q": "language:rust",
            "min_stars": "5",
            "team_topic": "Rust",
            "has_dockerfile": "true",
            "owner_country": "DE",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total_count"] == 1
    assert body["items"][0]["has_dockerfile"] is True
    assert body["items"][0]["owner"]["country_iso"] == "DE"
    for request in requests:
        parsed = urlparse(str(request.url))
        raw = str(request.url)
        assert "min_stars" not in raw
        assert "team_topic" not in raw
        assert "owner_country" not in raw
        assert "has_dockerfile" not in raw
        if parsed.path != SEARCH_PATH:
            continue
        assert set(parse_qs(parsed.query)) <= {"q", "sort", "order", "per_page", "page"}
    search_queries = [
        parse_qs(urlparse(str(request.url)).query).get("q", [""])[0]
        for request in requests
        if urlparse(str(request.url)).path == SEARCH_PATH
    ]
    assert any("stars:>=5" in query and "topic:rust" in query for query in search_queries)
