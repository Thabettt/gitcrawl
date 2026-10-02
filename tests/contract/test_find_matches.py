from __future__ import annotations

import re
import types

import fakeredis
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from serve.app import create_app
from serve.executor import RunPayloadItem


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
    find_count_factory=None,
) -> TestClient:
    application = create_app(
        engine=engine,
        runs_root=str(runs_root),
        redis_ping=redis_ping,
        token_present=token_present,
        find_count_factory=find_count_factory,
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


def test_search_page_has_common_advanced_and_explained_actions(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    body = client.get("/find").text
    assert "Common" in body and "Advanced" in body
    assert "uses your github allowance" in body.lower()
    assert "No GitHub calls." in body


def test_check_matches_returns_count_and_caveat(clean, tmp_path, monkeypatch):
    def factory():
        return lambda query: 4200

    client = healthy_client(clean, tmp_path, monkeypatch, find_count_factory=factory)
    response = client.get("/partials/find/matches?q=language:rust&stars=100")
    assert response.status_code == 200
    assert "~4,200" in response.text
    assert "extra rules" in response.text.lower()


def test_check_matches_without_js_renders_a_page(clean, tmp_path, monkeypatch):
    def factory():
        return lambda query: 12

    client = healthy_client(clean, tmp_path, monkeypatch, find_count_factory=factory)
    response = client.get("/partials/find/matches?q=language:rust")
    assert response.status_code == 200
    assert "<html" in response.text.lower()  # full page for non-htmx clients


def test_check_matches_with_htmx_returns_a_fragment(clean, tmp_path, monkeypatch):
    def factory():
        return lambda query: 7

    client = healthy_client(clean, tmp_path, monkeypatch, find_count_factory=factory)
    response = client.get("/partials/find/matches?q=language:rust", headers={"hx-request": "true"})
    assert response.status_code == 200
    assert "~7" in response.text
    assert "<html" not in response.text.lower()


def test_check_matches_uses_the_default_count_factory(clean, tmp_path, monkeypatch):
    closed: list[bool] = []

    class FakeClient:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        "serve.runner.build_deps", lambda engine: types.SimpleNamespace(client=FakeClient())
    )
    monkeypatch.setattr("discover.pipeline.count_total", lambda deps, query: 321)

    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get("/partials/find/matches", params={"keywords": "language:rust"})

    assert response.status_code == 200
    assert "~321" in response.text
    assert closed == [True]


def test_search_page_with_invalid_prefill_still_renders(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)

    response = client.get("/find", params={"keywords": "updated:>2024"})

    assert response.status_code == 200
    assert "Everything" in response.text


def test_check_matches_reports_a_failed_count(clean, tmp_path, monkeypatch):
    def factory():
        def boom(query):
            raise RuntimeError("upstream is down")

        return boom

    client = healthy_client(clean, tmp_path, monkeypatch, find_count_factory=factory)
    response = client.get("/partials/find/matches?q=language:rust")
    assert response.status_code == 200
    assert "Could not check matches right now." in response.text


def test_check_matches_reports_invalid_filters(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get("/partials/find/matches", params={"keywords": "updated:>2024"})
    assert response.status_code == 200
    assert "updated" in response.text
    assert "pushed" in response.text


def test_unavailable_filters_are_greyed_out_with_a_reason(clean, tmp_path, monkeypatch):
    body = healthy_client(clean, tmp_path, monkeypatch).get("/find").text
    assert re.search(r'<input[^>]*name="min_loc"[^>]*disabled', body)
    assert re.search(r'<input[^>]*name="max_loc"[^>]*disabled', body)
    assert "Not available yet" in body
    assert 'name="max_commits"' in body  # available once Phase 1 lands


def test_numeric_count_rows_expose_min_and_max(clean, tmp_path, monkeypatch):
    body = healthy_client(clean, tmp_path, monkeypatch).get("/find").text
    assert 'name="range_stars_min"' in body
    assert 'name="range_stars_max"' in body
    assert 'name="range_size_min"' in body and 'name="range_size_max"' in body
