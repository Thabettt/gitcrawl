from __future__ import annotations

import fakeredis
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from serve.app import create_app


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


def test_api_metrics_json_shape(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get("/api/metrics")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"slo", "runs", "limiter", "queue"}


def test_metrics_page_renders_cards(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get("/metrics")
    assert response.status_code == 200
    for label in (
        "Search remaining",
        "Incomplete ratio",
        "422 rate",
        "403/429 rate",
        "p95 latency",
        "Shard coverage",
        "Geo unmatched",
        "Queue PEL",
    ):
        assert label in response.text
    assert 'hx-get="/partials/metrics"' in response.text


def test_metrics_partial_degrades_without_redis(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get("/partials/metrics")
    assert response.status_code == 200
    assert "degraded" in response.text.lower() or "n/a" in response.text.lower()
