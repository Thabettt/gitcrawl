from __future__ import annotations

import json
import threading
import time

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from enrich import cloner
from lib.gh_client import API_VERSION
from serve.app import create_app
from serve.executor import RunPayload, RunPayloadItem, create_run, execute_run

FILTER = {"gitcrawl_filter": 1, "q": "language:rust"}
MARKER_NAME = ".gitcrawl-clone-done"
IDLE_PROGRESS = {
    "status": "done",
    "total": 0,
    "completed": 0,
    "failed": 0,
    "current": None,
    "errors": [],
}


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
        connection.execute(text("INSERT INTO owners (id, login, type) VALUES (1, 'octo', 'User')"))
        connection.execute(
            text(
                "INSERT INTO repos (id, node_id, full_name, owner_id, name, visibility, size_kb) "
                "VALUES (1296269, 'R_1296269', 'octo/hello', 1, 'hello', 'public', 1024), "
                "(2, 'R_2', 'octo/world', 1, 'world', 'public', 2048)"
            )
        )
    return alembic_engine


def payload_item(repo_id: int, full_name: str, stargazers: int) -> RunPayloadItem:
    return RunPayloadItem(
        repo_id=repo_id,
        full_name=full_name,
        stargazers=stargazers,
        pushed_at="2026-09-30T12:00:00Z",
        archived=False,
        language="Ruby",
        license_spdx="MIT",
        country_iso="DE",
        geo_confidence="name",
        virtuals={},
    )


def seed_run(engine: Engine, tmp_path) -> tuple[int, str]:
    run_id = create_run(engine, FILTER, api_version=API_VERSION)
    payload = RunPayload(
        total_count=2,
        fetched=2,
        items=[payload_item(1296269, "octo/hello", 80), payload_item(2, "octo/world", 40)],
    )
    execute_run(
        engine, run_id, runner=lambda _rid, _spec: payload, runs_root=str(tmp_path / "runs")
    )
    with engine.connect() as connection:
        filter_hash = connection.scalar(
            text("SELECT filter_hash FROM runs WHERE id = :id"), {"id": run_id}
        )
    return run_id, str(filter_hash)


def make_client(engine: Engine, tmp_path) -> TestClient:
    application = create_app(
        engine=engine,
        runs_root=str(tmp_path / "runs"),
        clone_root=str(tmp_path / "clones"),
    )
    return TestClient(application, raise_server_exceptions=False)


def wait_for_status(client: TestClient, run_id: int, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/partials/runs/{run_id}/clone-progress").json()
        if body["status"] != "running":
            return body
        time.sleep(0.01)
    raise AssertionError("clone did not reach a terminal status")


def test_clone_estimate_reports_top_repos_math_and_warnings(clean: Engine, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    monkeypatch.setattr("serve.runs.free_disk_mb", lambda path: 10_000.0)
    client = make_client(clean, tmp_path)

    response = client.get(f"/runs/{run_id}/clone-estimate", params={"limit": 1, "mode": "shallow"})

    assert response.status_code == 200
    assert response.json() == {"repos": 1, "estimated_mb": pytest.approx(1.0), "warnings": []}


def test_clone_estimate_defaults_to_all_repos_and_shallow(clean: Engine, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    monkeypatch.setattr("serve.runs.free_disk_mb", lambda path: 10_000.0)
    client = make_client(clean, tmp_path)

    response = client.get(f"/runs/{run_id}/clone-estimate")

    assert response.status_code == 200
    body = response.json()
    assert body["repos"] == 2
    assert body["estimated_mb"] == pytest.approx(3.0)


def test_clone_estimate_warns_when_close_to_the_disk_reserve(clean: Engine, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    monkeypatch.setattr("serve.runs.free_disk_mb", lambda path: 2048.0)
    client = make_client(clean, tmp_path)

    response = client.get(f"/runs/{run_id}/clone-estimate", params={"limit": 2})

    assert response.status_code == 200
    warnings = response.json()["warnings"]
    assert len(warnings) == 1
    assert "low disk" in warnings[0]


def test_clone_estimate_unknown_run_is_404(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    response = client.get("/runs/424242/clone-estimate", params={"limit": 1, "mode": "shallow"})

    assert response.status_code == 404
    assert response.json()["error"] == "run_not_found"


@pytest.mark.parametrize(
    ("params", "param"),
    [
        ({"limit": 1, "mode": "deep"}, "mode"),
        ({"limit": -1, "mode": "shallow"}, "limit"),
        ({"limit": "abc", "mode": "shallow"}, "limit"),
    ],
)
def test_clone_estimate_invalid_params_are_400(clean: Engine, tmp_path, params, param):
    run_id, _ = seed_run(clean, tmp_path)
    client = make_client(clean, tmp_path)

    response = client.get(f"/runs/{run_id}/clone-estimate", params=params)

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_param"
    assert body["param"] == param


def test_zero_limit_clone_is_a_synchronous_noop(clean: Engine, tmp_path):
    run_id, _ = seed_run(clean, tmp_path)
    client = make_client(clean, tmp_path)

    response = client.post(f"/runs/{run_id}/clone", json={"limit": 0, "mode": "shallow"})

    assert response.status_code == 200
    assert response.json() == IDLE_PROGRESS
    progress = client.get(f"/partials/runs/{run_id}/clone-progress")
    assert progress.status_code == 200
    assert progress.json() == IDLE_PROGRESS


def test_clone_start_returns_running_then_progress_completes(clean: Engine, tmp_path, monkeypatch):
    run_id, filter_hash = seed_run(clean, tmp_path)
    calls: list[tuple[list[str], str]] = []
    monkeypatch.setattr(
        cloner,
        "_default_git_runner",
        lambda argv, cwd: calls.append((list(argv), cwd)),
    )
    client = make_client(clean, tmp_path)

    response = client.post(f"/runs/{run_id}/clone", json={"limit": 2, "mode": "windowed"})

    assert response.status_code == 200
    started = response.json()
    assert started["status"] == "running"
    assert started["total"] == 2
    done = wait_for_status(client, run_id)
    assert done == {
        "status": "done",
        "total": 2,
        "completed": 2,
        "failed": 0,
        "current": None,
        "errors": [],
    }
    run_dir = tmp_path / "clones" / filter_hash / str(run_id)
    assert len(calls) == 2
    assert calls[0][0] == [
        "git",
        "clone",
        "--filter=blob:none",
        "--no-checkout",
        "https://github.com/octo/hello.git",
        str(run_dir / "octo__hello"),
    ]
    assert (run_dir / "octo__hello" / MARKER_NAME).is_file()
    progress_path = tmp_path / "runs" / filter_hash / str(run_id) / "clone-progress.json"
    assert json.loads(progress_path.read_text(encoding="utf-8")) == done


def test_progress_is_persisted_after_each_repo(clean: Engine, tmp_path, monkeypatch):
    run_id, filter_hash = seed_run(clean, tmp_path)
    release = threading.Event()
    seen: list[list[str]] = []

    def runner(argv: list[str], cwd: str) -> None:
        seen.append(list(argv))
        if len(seen) == 2:
            assert release.wait(10.0)

    monkeypatch.setattr(cloner, "_default_git_runner", runner)
    client = make_client(clean, tmp_path)

    response = client.post(f"/runs/{run_id}/clone", json={"limit": 2, "mode": "shallow"})

    assert response.json()["status"] == "running"
    progress_path = tmp_path / "runs" / filter_hash / str(run_id) / "clone-progress.json"
    data = None
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            data = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if data is not None and data["completed"] == 1 and data["current"] == "octo/world":
            break
        time.sleep(0.01)
    assert data is not None
    assert data["status"] == "running"
    assert data["completed"] == 1
    assert data["current"] == "octo/world"
    release.set()
    done = wait_for_status(client, run_id)
    assert done["completed"] == 2


def test_clone_unknown_run_is_404(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    response = client.post("/runs/424242/clone", json={"limit": 1, "mode": "shallow"})

    assert response.status_code == 404
    assert response.json()["error"] == "run_not_found"


@pytest.mark.parametrize(
    ("document", "param"),
    [
        ({"limit": 1, "mode": "deep"}, "mode"),
        ({"limit": -1, "mode": "shallow"}, "limit"),
        ({"limit": "one", "mode": "shallow"}, "limit"),
        ({"limit": True, "mode": "shallow"}, "limit"),
        ({"limit": 1, "mode": 7}, "mode"),
    ],
)
def test_clone_invalid_body_is_400(clean: Engine, tmp_path, document, param):
    run_id, _ = seed_run(clean, tmp_path)
    client = make_client(clean, tmp_path)

    response = client.post(f"/runs/{run_id}/clone", json=document)

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_param"
    assert body["param"] == param


def test_clone_non_json_body_is_400(clean: Engine, tmp_path):
    run_id, _ = seed_run(clean, tmp_path)
    client = make_client(clean, tmp_path)

    response = client.post(
        f"/runs/{run_id}/clone",
        content="not-json",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_param"


def test_progress_unknown_run_is_404(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    response = client.get("/partials/runs/424242/clone-progress")

    assert response.status_code == 404
    assert response.json()["error"] == "run_not_found"


def test_progress_for_a_run_that_never_cloned_is_idle(clean: Engine, tmp_path):
    run_id, _ = seed_run(clean, tmp_path)
    client = make_client(clean, tmp_path)

    response = client.get(f"/partials/runs/{run_id}/clone-progress")

    assert response.status_code == 200
    assert response.json() == IDLE_PROGRESS
