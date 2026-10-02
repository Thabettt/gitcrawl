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
    "error_count": 0,
}


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(clean_db):
    return clean_db(
        owners=[{"id": 1, "login": "octo", "type": "User"}],
        repos=[
            {
                "id": 1296269,
                "node_id": "R_1296269",
                "full_name": "octo/hello",
                "owner_id": 1,
                "name": "hello",
                "visibility": "public",
                "size_kb": 1024,
            },
            {
                "id": 2,
                "node_id": "R_2",
                "full_name": "octo/world",
                "owner_id": 1,
                "name": "world",
                "visibility": "public",
                "size_kb": 2048,
            },
        ],
    )


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
        lambda argv, cwd, **kwargs: calls.append((list(argv), cwd)),
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
        "error_count": 0,
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


def test_progress_is_live_in_flight_and_persisted_when_done(clean: Engine, tmp_path, monkeypatch):
    run_id, filter_hash = seed_run(clean, tmp_path)
    release = threading.Event()
    seen: list[list[str]] = []

    def runner(argv: list[str], cwd: str, **kwargs) -> None:
        seen.append(list(argv))
        if len(seen) == 2:
            assert release.wait(10.0)

    monkeypatch.setattr(cloner, "_default_git_runner", runner)
    client = make_client(clean, tmp_path)

    response = client.post(f"/runs/{run_id}/clone", json={"limit": 2, "mode": "shallow"})

    assert response.json()["status"] == "running"
    live = None
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        live = client.get(f"/partials/runs/{run_id}/clone-progress").json()
        if live["completed"] == 1 and live["current"] == "octo/world":
            break
        time.sleep(0.01)
    assert live is not None
    assert live["status"] == "running"
    assert live["completed"] == 1
    assert live["current"] == "octo/world"
    release.set()
    done = wait_for_status(client, run_id)
    assert done["completed"] == 2
    progress_path = tmp_path / "runs" / filter_hash / str(run_id) / "clone-progress.json"
    persisted = None
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            persisted = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            persisted = None
        if persisted == done:
            break
        time.sleep(0.01)
    assert persisted == done


def test_clone_progress_renders_worker_level_failures(clean: Engine, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("worker exploded")

    monkeypatch.setattr("serve.runs.clone_repos", boom)
    client = make_client(clean, tmp_path)

    response = client.post(f"/runs/{run_id}/clone", json={"limit": 1, "mode": "shallow"})

    assert response.status_code == 200
    partial = None
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        partial = client.get(
            f"/partials/runs/{run_id}/clone-progress", headers={"HX-Request": "true"}
        )
        if 'data-status="failed"' in partial.text:
            break
        time.sleep(0.01)
    assert partial is not None
    assert 'data-status="failed"' in partial.text
    assert "RuntimeError: worker exploded" in partial.text


def test_clone_progress_caps_rendered_errors(clean: Engine, tmp_path):
    run_id, filter_hash = seed_run(clean, tmp_path)
    progress_path = tmp_path / "runs" / filter_hash / str(run_id) / "clone-progress.json"
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path.write_text(
        json.dumps(
            {
                "status": "running",
                "total": 25,
                "completed": 5,
                "failed": 25,
                "current": None,
                "errors": [f"octo/r{index}: RuntimeError: boom" for index in range(20)],
                "error_count": 25,
            }
        ),
        encoding="utf-8",
    )
    client = make_client(clean, tmp_path)

    response = client.get(f"/partials/runs/{run_id}/clone-progress", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert response.text.count("<li>") <= 20
    assert "5 more failure" in response.text
    assert 'style="--progress: 0.2"' in response.text


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


def test_clone_wires_env_timeout_and_cancel_event(clean: Engine, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    captured: dict = {}
    called = threading.Event()

    def fake_clone_repos(engine, run_id, **kwargs):
        captured.update(kwargs)
        called.set()
        return cloner.CloneStats(requested=0, completed=0, skipped=0, failed=0, dest_root="clones")

    monkeypatch.setattr("serve.runs.clone_repos", fake_clone_repos)
    monkeypatch.setenv("GITCRAWL_CLONE_TIMEOUT_SECONDS", "42")
    client = make_client(clean, tmp_path)

    response = client.post(f"/runs/{run_id}/clone", json={"limit": 1, "mode": "shallow"})

    assert response.status_code == 200
    assert called.wait(10) is True
    assert captured["clone_timeout"] == 42.0
    assert isinstance(captured["cancel_event"], threading.Event)


def test_clone_cancel_stops_remaining_repos(clean: Engine, tmp_path, monkeypatch):
    run_id, filter_hash = seed_run(clean, tmp_path)
    started = threading.Event()
    release = threading.Event()
    calls: list[list[str]] = []

    def runner(argv: list[str], cwd: str, **kwargs) -> None:
        calls.append(list(argv))
        started.set()
        release.wait(10)

    monkeypatch.setattr(cloner, "_default_git_runner", runner)
    client = make_client(clean, tmp_path)

    started_response = client.post(f"/runs/{run_id}/clone", json={"limit": 2, "mode": "shallow"})
    assert started_response.json()["status"] == "running"
    assert started.wait(10) is True

    cancelled = client.delete(f"/runs/{run_id}/clone")
    assert cancelled.status_code == 200
    release.set()

    done = wait_for_status(client, run_id)
    assert done["status"] == "cancelled"
    assert len(calls) == 1
    assert done["failed"] == 0


def test_clone_cancel_on_an_idle_run_returns_the_idle_progress(clean: Engine, tmp_path):
    run_id, _ = seed_run(clean, tmp_path)
    client = make_client(clean, tmp_path)

    response = client.delete(f"/runs/{run_id}/clone")

    assert response.status_code == 200
    assert response.json() == IDLE_PROGRESS


def test_clone_cancel_unknown_run_is_404(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    response = client.delete("/runs/424242/clone")

    assert response.status_code == 404
    assert response.json()["error"] == "run_not_found"
