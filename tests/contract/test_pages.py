from __future__ import annotations

import asyncio
import importlib
from urllib.parse import urlencode

import fakeredis
import pytest
import uvicorn
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from starlette.requests import Request

from lib.gh_client import API_VERSION
from serve.app import create_app
from serve.executor import RunPayload, RunPayloadItem, create_run, execute_run
from serve.pages import CSRF_COOKIE, validate_csrf

DASHBOARD_IDS = (
    'id="main-nav"',
    'id="theme-toggle"',
    'id="health-db"',
    'id="health-redis"',
    'id="health-token"',
    'id="quick-find"',
    'id="recent-runs"',
)


class BrokenEngine:
    def connect(self):
        raise RuntimeError("database is down")


def down_ping() -> bool:
    raise ConnectionError("redis is down")


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


def make_request(
    *,
    cookie: str | None = None,
    header: str | None = None,
    query: str = "",
    form: dict[str, str] | None = None,
) -> Request:
    headers = []
    if cookie is not None:
        headers.append((b"cookie", f"{CSRF_COOKIE}={cookie}".encode()))
    if header is not None:
        headers.append((b"x-csrf-token", header.encode()))
    body = b""
    if form is not None:
        body = urlencode(form).encode()
        headers.append((b"content-type", b"application/x-www-form-urlencoded"))
        headers.append((b"content-length", str(len(body)).encode()))
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "query_string": query.encode(),
            "headers": headers,
        },
        receive,
    )


def test_dashboard_renders_empty_state_and_key_elements(clean: Engine, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    for element_id in DASHBOARD_IDS:
        assert element_id in html
    assert 'id="empty-state"' in html
    assert "No runs yet" in html
    assert "super-secret-token-value" not in html


def test_dashboard_lists_recent_runs_with_status_and_counts(clean: Engine, tmp_path, monkeypatch):
    run_id, filter_hash = seed_run(clean, tmp_path)
    client = healthy_client(clean, tmp_path, monkeypatch)

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert f'id="run-{run_id}"' in html
    assert filter_hash[:8] in html
    assert ">done<" in html
    assert 'id="empty-state"' not in html
    assert 'data-state="ok"' in html


def test_navigation_links_to_the_filter_form(clean: Engine, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)

    response = client.get("/")

    assert response.status_code == 200
    assert 'href="/find"' in response.text


def test_dashboard_run_links_resolve_to_the_run_page(clean: Engine, tmp_path, monkeypatch):
    run_id, _filter_hash = seed_run(clean, tmp_path)
    client = healthy_client(clean, tmp_path, monkeypatch)

    dashboard = client.get("/")
    run_page = client.get(f"/runs/{run_id}")

    assert f'href="/runs/{run_id}"' in dashboard.text
    assert run_page.status_code == 200
    assert 'id="run-header"' in run_page.text


def test_dashboard_renders_error_state_when_database_is_down(clean: Engine, tmp_path, monkeypatch):
    client = healthy_client(BrokenEngine(), tmp_path, monkeypatch)

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert 'id="runs-error"' in html
    assert 'id="empty-state"' not in html
    assert 'id="recent-runs"' in html
    assert 'id="health-db"' in html
    assert 'data-state="down"' in html


def test_health_reports_booleans_for_healthy_clients(clean: Engine, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "super-secret-token-value")
    monkeypatch.delenv("GITHUB_TOKENS", raising=False)
    client = make_client(clean, redis_ping=fakeredis.FakeRedis().ping, runs_root=tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"database": True, "redis": True, "github_token_present": True}
    assert "super-secret-token-value" not in response.text


def test_dashboard_memoizes_health_checks_between_requests(clean: Engine, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "super-secret-token-value")
    monkeypatch.delenv("GITHUB_TOKENS", raising=False)
    pings: list[int] = []

    def ping() -> bool:
        pings.append(1)
        return True

    client = make_client(clean, redis_ping=ping, runs_root=tmp_path)

    first = client.get("/")
    second = client.get("/")

    assert first.status_code == second.status_code == 200
    assert len(pings) == 1


def test_health_reports_booleans_for_unhealthy_clients(clean: Engine, tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKENS", raising=False)
    client = make_client(
        BrokenEngine(),
        redis_ping=down_ping,
        runs_root=tmp_path,
    )

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "database": False,
        "redis": False,
        "github_token_present": False,
    }


def test_csrf_cookie_is_issued_and_rendered_into_the_meta_tag(clean: Engine, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)

    first = client.get("/")
    token = client.cookies.get(CSRF_COOKIE)
    second = client.get("/")

    assert first.status_code == second.status_code == 200
    assert token
    assert len(token) >= 32
    assert f'name="csrf-token" content="{token}"' in first.text
    assert client.cookies.get(CSRF_COOKIE) == token


def test_validate_csrf_honors_header_match_and_mismatch(clean: Engine, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    client.get("/")
    token = client.cookies.get(CSRF_COOKIE)

    assert asyncio.run(validate_csrf(make_request(cookie=token, header=token))) is True
    assert asyncio.run(validate_csrf(make_request(cookie=token, header="wrong-token"))) is False
    assert asyncio.run(validate_csrf(make_request(cookie=token, header="tökén"))) is False
    assert asyncio.run(validate_csrf(make_request(cookie=token))) is False
    assert asyncio.run(validate_csrf(make_request(header=token))) is False
    assert asyncio.run(validate_csrf(make_request())) is False


def test_validate_csrf_accepts_hidden_form_field_without_javascript(
    clean: Engine, tmp_path, monkeypatch
):
    client = healthy_client(clean, tmp_path, monkeypatch)
    client.get("/")
    token = client.cookies.get(CSRF_COOKIE)

    assert asyncio.run(validate_csrf(make_request(cookie=token, form={"csrf": token}))) is True
    assert asyncio.run(validate_csrf(make_request(cookie=token, form={"csrf": "wrong"}))) is False
    assert asyncio.run(validate_csrf(make_request(cookie=token, form={}))) is False
    assert asyncio.run(validate_csrf(make_request(form={"csrf": token}))) is False


def test_validate_csrf_rejects_query_string_tokens(clean: Engine, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    client.get("/")
    token = client.cookies.get(CSRF_COOKIE)

    assert asyncio.run(validate_csrf(make_request(cookie=token, query=f"csrf={token}"))) is False
    assert asyncio.run(validate_csrf(make_request(cookie=token, query="csrf=wrong"))) is False


@pytest.mark.parametrize(
    ("path", "content_type", "marker"),
    [
        ("/static/app.css", "text/css", b":root"),
        ("/static/app.js", "javascript", b"gc-theme"),
        ("/static/htmx.min.js", "javascript", b"htmx"),
    ],
)
def test_static_assets_are_served_with_correct_content_types(
    clean: Engine, tmp_path, monkeypatch, path, content_type, marker
):
    client = healthy_client(clean, tmp_path, monkeypatch)

    response = client.get(path)

    assert response.status_code == 200
    assert content_type in response.headers["content-type"]
    assert marker in response.content


def test_main_module_reads_env_overrides_without_starting_a_server(monkeypatch):
    entry = importlib.import_module("serve.__main__")
    captured: dict[str, object] = {}

    def fake_run(app_path: str, *, host: str, port: int) -> None:
        captured.update(app=app_path, host=host, port=port)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setenv("GITCRAWL_HOST", "0.0.0.0")
    monkeypatch.setenv("GITCRAWL_PORT", "9123")

    entry.main()

    assert captured == {"app": "serve.app:app", "host": "0.0.0.0", "port": 9123}


def test_main_module_defaults_to_localhost_8000(monkeypatch):
    entry = importlib.import_module("serve.__main__")
    captured: dict[str, object] = {}

    def fake_run(app_path: str, *, host: str, port: int) -> None:
        captured.update(app=app_path, host=host, port=port)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.delenv("GITCRAWL_HOST", raising=False)
    monkeypatch.delenv("GITCRAWL_PORT", raising=False)

    entry.main()

    assert captured == {"app": "serve.app:app", "host": "127.0.0.1", "port": 8000}
