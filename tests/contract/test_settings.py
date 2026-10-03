from __future__ import annotations

import re

import fakeredis
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from serve.app import create_app
from store.settings import RunSettings, load_run_settings, update_run_settings


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


def csrf_token(client) -> str:
    response = client.get("/")
    marker = 'name="csrf-token" content="'
    start = response.text.index(marker) + len(marker)
    return response.text[start : response.text.index('"', start)]


def valid_form(**overrides: str) -> dict[str, str]:
    data = {
        "max_shards": "50",
        "max_candidates": "5000",
        "max_hydrate": "4000",
        "max_enrich": "1000",
        "request_deadline_seconds": "7200",
        "graphql_batch": "on",
        "graphql_batch_size": "10",
        "limiter_max_concurrent": "4",
    }
    data.update(overrides)
    return data


def test_settings_page_renders_current_values(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get("/settings")
    assert response.status_code == 200
    assert "max_hydrate" in response.text
    assert "limiter_max_concurrent" in response.text
    assert "Run limits for new searches" in response.text
    assert "Part of System" in response.text
    assert "Search breadth (slices)" in response.text
    assert "Repos to save details for" in response.text
    assert re.search(r"keep well under GitHub’s ceiling", response.text, re.IGNORECASE)
    assert '<a href="/system" aria-current="page">System</a>' in response.text


def test_settings_page_never_renders_the_token(clean, tmp_path, monkeypatch):
    # healthy_client sets GITHUB_TOKEN=super-secret-token-value
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get("/settings")
    assert "super-secret-token-value" not in response.text


def test_post_saves_values_and_redirects(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    token = csrf_token(client)
    response = client.post(
        "/settings",
        data=valid_form(),
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1"
    saved = load_run_settings(clean)
    assert saved.max_shards == 50
    assert saved.max_hydrate == 4000
    assert saved.limiter_max_concurrent == 4


def test_pinned_fields_render_read_only_and_are_not_written(clean, tmp_path, monkeypatch):
    monkeypatch.setenv("GITCRAWL_MAX_SHARDS", "3")
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get("/settings")
    assert response.status_code == 200
    assert re.search(r'name="max_shards"[^>]*readonly', response.text)
    assert "set by environment" in response.text
    token = csrf_token(client)
    response = client.post(
        "/settings",
        data=valid_form(max_shards="999"),
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with clean.connect() as connection:
        stored = connection.scalar(text("SELECT max_shards FROM app_settings WHERE id = 1"))
    assert stored == 10


def test_invalid_settings_are_a_local_400(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    token = csrf_token(client)
    response = client.post(
        "/settings",
        data={"max_shards": "0", "graphql_batch_size": "99"},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "max_shards" in response.text


def test_missing_csrf_is_403(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.post("/settings", data={"max_shards": "5"}, follow_redirects=False)
    assert response.status_code == 403


def test_save_writes_an_audit_row(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    token = csrf_token(client)
    response = client.post(
        "/settings",
        data=valid_form(
            max_shards="10",
            max_candidates="500",
            max_hydrate="200",
            max_enrich="100",
            request_deadline_seconds="3600",
            graphql_batch_size="20",
            limiter_max_concurrent="10",
        ),
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with clean.connect() as connection:
        params = connection.scalar(
            text(
                "SELECT params FROM audit_log "
                "WHERE token_fp = 'settings' ORDER BY id DESC LIMIT 1"
            )
        )
    assert params["app_settings"]["after"]["max_shards"] == 10
    assert params["app_settings"]["before"]["max_shards"] == 10


def test_record_settings_change_writes_before_and_after(clean):
    from serve.settings import record_settings_change

    record_settings_change(clean, {"max_shards": 10}, {"max_shards": 25})
    with clean.connect() as connection:
        params = connection.scalar(
            text(
                "SELECT params FROM audit_log WHERE token_fp = 'settings' "
                "ORDER BY id DESC LIMIT 1"
            )
        )
    assert params["app_settings"] == {
        "before": {"max_shards": 10},
        "after": {"max_shards": 25},
    }


def test_reset_restores_defaults(clean, tmp_path, monkeypatch):
    update_run_settings(clean, {"max_shards": 50, "max_candidates": 5000, "max_hydrate": 4000})
    client = healthy_client(clean, tmp_path, monkeypatch)
    token = csrf_token(client)
    response = client.post(
        "/settings",
        data={"reset": "1"},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert load_run_settings(clean) == RunSettings()


def test_pinned_candidates_conflict_with_submitted_hydrate_is_rejected(
    clean, tmp_path, monkeypatch
):
    monkeypatch.setenv("GITCRAWL_MAX_CANDIDATES", "100")
    client = healthy_client(clean, tmp_path, monkeypatch)
    token = csrf_token(client)
    response = client.post(
        "/settings",
        data=valid_form(max_shards="7", max_candidates="5000", max_hydrate="200"),
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "max_hydrate" in response.text
    with clean.connect() as connection:
        stored = connection.scalar(text("SELECT max_shards FROM app_settings WHERE id = 1"))
        audits = connection.scalar(
            text("SELECT count(*) FROM audit_log WHERE token_fp = 'settings'")
        )
    assert stored == 10
    assert audits == 0


def test_settings_page_shows_every_allowed_range(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    body = client.get("/settings").text
    for needle in ("1–10,000", "1–1,000,000", "60–86,400", "1–20", "1–100"):
        assert needle in body


def test_set_to_maximum_limits_saves_the_bounds(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    token = csrf_token(client)
    response = client.post(
        "/settings",
        data={"preset": "max"},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert load_run_settings(clean) == RunSettings(
        max_shards=10_000,
        max_candidates=1_000_000,
        max_hydrate=1_000_000,
        max_enrich=1_000_000,
        request_deadline_seconds=86_400,
        graphql_batch=True,
        graphql_batch_size=20,
        limiter_max_concurrent=100,
    )
    with clean.connect() as connection:
        params = connection.scalar(
            text(
                "SELECT params FROM audit_log "
                "WHERE token_fp = 'settings' ORDER BY id DESC LIMIT 1"
            )
        )
    assert params["app_settings"]["after"]["max_shards"] == 10_000


def test_max_preset_respects_pinned_fields(clean, tmp_path, monkeypatch):
    monkeypatch.setenv("GITCRAWL_MAX_SHARDS", "3")
    client = healthy_client(clean, tmp_path, monkeypatch)
    token = csrf_token(client)
    response = client.post(
        "/settings",
        data={"preset": "max"},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with clean.connect() as connection:
        stored = connection.scalar(text("SELECT max_shards FROM app_settings WHERE id = 1"))
    assert stored == 10
    assert load_run_settings(clean).max_shards == 3


def test_max_preset_with_pinned_candidates_conflict_is_rejected(clean, tmp_path, monkeypatch):
    monkeypatch.setenv("GITCRAWL_MAX_CANDIDATES", "100")
    client = healthy_client(clean, tmp_path, monkeypatch)
    token = csrf_token(client)
    response = client.post(
        "/settings",
        data={"preset": "max"},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "max_hydrate" in response.text


def test_reset_with_pinned_candidates_conflict_is_rejected(clean, tmp_path, monkeypatch):
    monkeypatch.setenv("GITCRAWL_MAX_CANDIDATES", "100")
    client = healthy_client(clean, tmp_path, monkeypatch)
    token = csrf_token(client)
    response = client.post(
        "/settings",
        data={"reset": "1"},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "max_hydrate" in response.text
    with clean.connect() as connection:
        audits = connection.scalar(
            text("SELECT count(*) FROM audit_log WHERE token_fp = 'settings'")
        )
    assert audits == 0
