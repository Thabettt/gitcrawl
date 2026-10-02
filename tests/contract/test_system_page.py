from __future__ import annotations

import fakeredis
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from serve.app import create_app
from serve.system import _dot


class BrokenEngine:
    def connect(self):
        raise RuntimeError("database is down")


def down_ping() -> bool:
    raise ConnectionError("redis is down")


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


def test_system_page_has_three_plain_sections(clean: Engine, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)

    response = client.get("/system")

    assert response.status_code == 200
    for text in ("Status", "Performance", "Limits"):
        assert text in response.text
    assert "Requests left this hour" in response.text
    assert "/settings" in response.text


def test_system_page_never_shows_raw_internal_metric_names(clean: Engine, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)

    response = client.get("/system")

    assert response.status_code == 200
    assert "p95" not in response.text
    assert "PEL" not in response.text
    assert "SLO" not in response.text


def test_system_page_lists_status_facts_without_secret_values(clean: Engine, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)

    response = client.get("/system")

    assert response.status_code == 200
    assert "Database" in response.text
    assert "Queue" in response.text
    assert "GitHub token" in response.text
    assert "present" in response.text
    assert "super-secret-token-value" not in response.text


def test_dot_maps_probes_to_exact_color_and_label():
    assert _dot({"database": False, "redis": True, "github_token_present": True}) == (
        "red",
        "Database is down",
    )
    assert _dot({"database": True, "redis": False, "github_token_present": True}) == (
        "amber",
        "Degraded — see System",
    )
    assert _dot({"database": True, "redis": True, "github_token_present": False}) == (
        "amber",
        "Degraded — see System",
    )
    assert _dot({"database": True, "redis": True, "github_token_present": True}) == (
        "green",
        "All systems ready",
    )


def test_system_page_still_renders_when_metrics_fail(clean: Engine, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr("serve.system.metrics_payload", boom)

    response = client.get("/system")

    assert response.status_code == 200
    assert "Performance metrics are unavailable right now" in response.text
    assert "Limits" in response.text
    assert "/settings" in response.text


def test_status_dot_reports_ok_when_all_probes_pass(clean: Engine, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)

    response = client.get("/partials/status-dot")

    assert response.status_code == 200
    assert 'aria-label="All systems ready"' in response.text


def test_status_dot_reports_degraded_without_token(clean: Engine, tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKENS", raising=False)
    client = make_client(clean, redis_ping=lambda: True, token_present=lambda: False)

    response = client.get("/partials/status-dot")

    assert response.status_code == 200
    assert 'aria-label="Degraded — see System"' in response.text


def test_status_dot_reports_degraded_when_redis_is_down(clean: Engine, tmp_path, monkeypatch):
    client = make_client(clean, redis_ping=down_ping, token_present=lambda: True)

    response = client.get("/partials/status-dot")

    assert response.status_code == 200
    assert 'aria-label="Degraded — see System"' in response.text


def test_status_dot_reports_red_when_database_is_down(clean: Engine, tmp_path, monkeypatch):
    client = make_client(BrokenEngine(), redis_ping=lambda: True, token_present=lambda: True)

    response = client.get("/partials/status-dot")

    assert response.status_code == 200
    assert 'aria-label="Database is down"' in response.text


def test_health_and_system_share_one_probe_cache(clean: Engine, tmp_path, monkeypatch):
    pings: list[int] = []

    def ping() -> bool:
        pings.append(1)
        return True

    client = make_client(clean, redis_ping=ping, token_present=lambda: True)

    assert client.get("/health").status_code == 200
    assert client.get("/system").status_code == 200

    assert len(pings) == 1
