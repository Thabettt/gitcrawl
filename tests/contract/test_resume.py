from __future__ import annotations

import time

import fakeredis
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from lib.gh_client import API_VERSION
from serve.app import create_app
from serve.executor import RunPayload, RunPayloadItem, create_run, execute_run
from serve.pages import CSRF_COOKIE


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
    runs_root="runs",
    *,
    redis_ping=None,
    token_present=None,
    runner_factory=None,
) -> TestClient:
    application = create_app(
        engine=engine,
        runs_root=str(runs_root),
        redis_ping=redis_ping,
        token_present=token_present,
        runner_factory=runner_factory,
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


def csrf_token(client) -> str:
    client.get("/")
    return client.cookies.get(CSRF_COOKIE)


def fake_runner_factory(engine):
    def runner(run_id: int, spec: dict):
        return RunPayload(
            total_count=1,
            fetched=1,
            items=[
                RunPayloadItem(repo_id=1296269, full_name="octo/hello", stargazers=10, virtuals={})
            ],
        )

    return runner


def test_resume_failed_run_requeues_and_clears_items(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    with clean.begin() as connection:
        connection.execute(text("UPDATE runs SET status='failed' WHERE id=:id"), {"id": run_id})
        connection.execute(
            text(
                "INSERT INTO run_items (run_id, repo_id, full_name) "
                "VALUES (:id, NULL, 'octo/world')"
            ),
            {"id": run_id},
        )
    observed: list[int] = []

    def recording_factory(engine):
        def runner(run_id: int, spec: dict) -> RunPayload:
            with engine.connect() as connection:
                count = connection.scalar(
                    text("SELECT count(*) FROM run_items WHERE run_id = :id"), {"id": run_id}
                )
            observed.append(int(count))
            return RunPayload(
                total_count=1,
                fetched=1,
                items=[
                    RunPayloadItem(
                        repo_id=1296269, full_name="octo/hello", stargazers=10, virtuals={}
                    )
                ],
            )

        return runner

    client = make_client(clean, tmp_path, runner_factory=recording_factory)
    token = csrf_token(client)
    response = client.post(
        f"/runs/{run_id}/resume", headers={"x-csrf-token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/runs/{run_id}"
    deadline = time.monotonic() + 5
    while True:
        with clean.connect() as connection:
            status = connection.scalar(text("SELECT status FROM runs WHERE id=:id"), {"id": run_id})
        if status in {"done", "failed", "partial"} or time.monotonic() > deadline:
            break
        time.sleep(0.05)
    assert observed == [0], f"runner saw {observed} run_items; resume did not reset first"
    with clean.connect() as connection:
        stale = connection.scalar(
            text("SELECT count(*) FROM run_items WHERE run_id=:id AND full_name='octo/world'"),
            {"id": run_id},
        )
    assert stale == 0
    assert status in {"queued", "running", "done"}


def test_resume_done_run_is_400(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    client = make_client(clean, tmp_path, runner_factory=fake_runner_factory)
    token = csrf_token(client)
    response = client.post(f"/runs/{run_id}/resume", headers={"x-csrf-token": token})
    assert response.status_code == 400
    assert "failed" in response.text


def test_resume_unknown_run_is_404(clean, tmp_path, monkeypatch):
    client = make_client(clean, tmp_path, runner_factory=fake_runner_factory)
    token = csrf_token(client)
    assert client.post("/runs/4242/resume", headers={"x-csrf-token": token}).status_code == 404


def test_resume_requires_csrf(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    client = make_client(clean, tmp_path, runner_factory=fake_runner_factory)
    assert client.post(f"/runs/{run_id}/resume").status_code == 403


def test_run_detail_shows_resume_only_for_failed(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    client = make_client(clean, tmp_path, runner_factory=fake_runner_factory)
    assert "Resume" not in client.get(f"/runs/{run_id}").text
    with clean.begin() as connection:
        connection.execute(
            text("UPDATE runs SET status='failed', error='boom' WHERE id=:id"), {"id": run_id}
        )
    assert "Resume" in client.get(f"/runs/{run_id}").text
