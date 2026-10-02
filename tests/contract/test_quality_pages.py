from __future__ import annotations

import fakeredis
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from lib.gh_client import API_VERSION
from serve.app import create_app
from serve.executor import RunPayload, RunPayloadItem, create_run, execute_run


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


def test_quality_json_route_reports_status(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get(f"/runs/{run_id}/quality")
    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == run_id
    assert payload["status"] in {"ok", "warn", "fail"}
    assert any(check["name"] == "count_parity" for check in payload["checks"])


def test_quality_json_unknown_run_is_404(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    assert client.get("/runs/9999/quality").status_code == 404


def test_run_detail_renders_quality_panel(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get(f"/runs/{run_id}")
    assert "Run quality" in response.text
    assert f'hx-get="/partials/runs/{run_id}/quality"' in response.text


def test_quality_partial_renders_checks(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get(f"/partials/runs/{run_id}/quality")
    assert response.status_code == 200
    assert "count_parity" in response.text
    assert "bundle" in response.text
