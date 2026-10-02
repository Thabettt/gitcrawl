from __future__ import annotations

import fakeredis
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from starlette.requests import Request

from lib.gh_client import API_VERSION
from serve.app import create_app
from serve.errors import csrf_error_page
from serve.executor import RunPayload, RunPayloadItem, create_run, execute_run

REQUEST_SCOPE = {
    "type": "http",
    "method": "GET",
    "path": "/settings",
    "headers": [],
    "query_string": b"",
    "scheme": "http",
    "server": ("testserver", 80),
    "root_path": "",
}


class BrokenEngine:
    def connect(self):
        raise RuntimeError("database is down")


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(clean_db):
    return clean_db(
        owners=[
            {
                "id": 1,
                "login": "octo",
                "type": "User",
                "location_raw": "Berlin, Germany",
                "country_iso": "DE",
                "geo_confidence": "name",
            }
        ],
        repos=[
            {
                "id": 1296269,
                "node_id": "R_1296269",
                "full_name": "octo/hello",
                "owner_id": 1,
                "name": "hello",
                "visibility": "public",
                "description": "My first repo",
                "language": "Ruby",
                "license_spdx": "MIT",
                "topics": ["octocat"],
                "stargazers": 80,
                "forks_count": 9,
                "open_issues": 0,
                "pushed_at": "2011-01-26T19:06:43Z",
            }
        ],
    )


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


def make_client(
    engine,
    *,
    redis_ping=None,
    token_present=None,
    runs_root="runs",
) -> TestClient:
    application = create_app(
        engine=engine,
        runs_root=str(runs_root),
        redis_ping=redis_ping,
        token_present=token_present,
    )
    return TestClient(application, raise_server_exceptions=False)


def healthy_client(engine: Engine, tmp_path, monkeypatch, **overrides) -> TestClient:
    monkeypatch.setenv("GITHUB_TOKEN", "super-secret-token-value")
    monkeypatch.delenv("GITHUB_TOKENS", raising=False)
    options = {
        "redis_ping": fakeredis.FakeRedis().ping,
        "token_present": lambda: True,
        "runs_root": tmp_path,
    }
    options.update(overrides)
    return make_client(engine, **options)


def seed_run(engine: Engine, tmp_path) -> tuple[int, str]:
    filter_spec = {"gitcrawl_filter": 1, "q": "language:rust"}
    run_id = create_run(engine, filter_spec, api_version=API_VERSION)
    payload = RunPayload(total_count=1, fetched=1, items=[payload_item()])
    execute_run(engine, run_id, runner=lambda _rid, _spec: payload, runs_root=str(tmp_path))
    with engine.connect() as connection:
        filter_hash = connection.scalar(
            text("SELECT filter_hash FROM runs WHERE id = :id"), {"id": run_id}
        )
    return run_id, str(filter_hash)


def test_unknown_page_renders_html_for_browsers(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get("/definitely-not-a-page", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert "Back to Home" in response.text


def test_unknown_page_stays_json_for_api_clients(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get("/definitely-not-a-page", headers={"Accept": "application/json"})
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_server_error_renders_html_for_browsers(clean, tmp_path, monkeypatch):
    client = healthy_client(BrokenEngine(), tmp_path, monkeypatch)
    response = client.get("/partials/runs/1/quality", headers={"Accept": "text/html"})
    assert response.status_code == 500
    assert "Back to Home" in response.text


def test_server_error_stays_json_for_api_clients(clean, tmp_path, monkeypatch):
    client = healthy_client(BrokenEngine(), tmp_path, monkeypatch)
    response = client.get("/partials/runs/1/quality", headers={"Accept": "application/json"})
    assert response.status_code == 500
    assert response.json()["error"] == "internal_error"


def test_csrf_failure_renders_the_explained_page(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.post("/settings", data={"max_shards": "5"})
    assert response.status_code == 403
    assert "That action expired" in response.text
    assert "Go back and submit again" in response.text


def test_csrf_error_page_helper_explains_the_timeout():
    response = csrf_error_page(Request(REQUEST_SCOPE))
    assert response.status_code == 403
    assert b"That action expired" in response.body
    assert b"Back to Home" in response.body


def test_nav_uses_plain_names(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    body = client.get("/").text
    for label in ("Searches", "Corpora", "Detections", "Library", "System"):
        assert f">{label}<" in body


def test_run_statuses_are_plain_words(clean, tmp_path, monkeypatch):
    run_id, _hash = seed_run(clean, tmp_path)
    body = healthy_client(clean, tmp_path, monkeypatch).get(f"/runs/{run_id}").text
    assert "Finished" in body
    assert ">partial<" not in body


def test_nav_marks_the_current_section(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    body = client.get("/runs").text
    assert 'href="/runs" aria-current="page"' in body
