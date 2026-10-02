from __future__ import annotations

import threading
import time

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from starlette.datastructures import URL

from serve.app import create_app
from serve.executor import RunPayload, RunPayloadItem
from serve.middleware import (
    JSON_BODY_LIMIT_BYTES,
    STATE_CHANGING_METHODS,
    UPLOAD_BODY_LIMIT_BYTES,
    _same_origin,
)

FILTER = {"gitcrawl_filter": 1, "q": "language:rust"}


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(clean_db):
    return clean_db()


def payload_item(repo_id: int = 1, full_name: str = "octo/hello") -> RunPayloadItem:
    return RunPayloadItem(repo_id=repo_id, full_name=full_name, stargazers=10, virtuals={})


def counting_runner(payload: RunPayload, calls: list):
    def runner(run_id: int, filter_spec: dict) -> RunPayload:
        calls.append(filter_spec)
        return payload

    return runner


def make_client(engine: Engine, tmp_path, *, runner=None, **kwargs) -> TestClient:
    if runner is None:
        runner = counting_runner(RunPayload(total_count=0, items=[]), [])
    application = create_app(
        engine=engine,
        runner_factory=lambda _engine: runner,
        runs_root=str(tmp_path / "runs"),
        clone_root=str(tmp_path / "clones"),
        **kwargs,
    )
    return TestClient(application, raise_server_exceptions=False)


def test_cross_origin_post_is_rejected(clean: Engine, tmp_path):
    calls: list = []
    client = make_client(clean, tmp_path, runner=counting_runner(RunPayload(0, []), calls))

    response = client.post("/vsearch/run", json=FILTER, headers={"Origin": "http://evil.example"})

    assert response.status_code == 403
    assert calls == []


def test_same_origin_post_passes(clean: Engine, tmp_path):
    calls: list = []
    client = make_client(
        clean, tmp_path, runner=counting_runner(RunPayload(total_count=0, items=[]), calls)
    )

    response = client.post("/vsearch/run", json=FILTER, headers={"Origin": "http://testserver"})

    assert response.status_code == 200
    assert len(calls) == 1


def test_default_port_origin_passes(clean: Engine, tmp_path):
    calls: list = []
    client = make_client(
        clean, tmp_path, runner=counting_runner(RunPayload(total_count=0, items=[]), calls)
    )

    response = client.post("/vsearch/run", json=FILTER, headers={"Origin": "http://testserver:80"})

    assert response.status_code == 200


def test_post_without_origin_passes(clean: Engine, tmp_path):
    calls: list = []
    client = make_client(
        clean, tmp_path, runner=counting_runner(RunPayload(total_count=0, items=[]), calls)
    )

    response = client.post("/vsearch/run", json=FILTER)

    assert response.status_code == 200
    assert len(calls) == 1


@pytest.mark.parametrize(
    "origin",
    ["https://testserver", "http://testserver:9999", "null", "http://testserver:notaport"],
)
def test_mismatched_origin_is_rejected(clean: Engine, tmp_path, origin):
    client = make_client(clean, tmp_path)

    response = client.post("/vsearch/run", json=FILTER, headers={"Origin": origin})

    assert response.status_code == 403


def test_cross_origin_referer_is_rejected_when_origin_is_absent(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    response = client.post(
        "/vsearch/run", json=FILTER, headers={"Referer": "http://evil.example/find"}
    )

    assert response.status_code == 403


def test_same_origin_referer_passes_when_origin_is_absent(clean: Engine, tmp_path):
    calls: list = []
    client = make_client(
        clean, tmp_path, runner=counting_runner(RunPayload(total_count=0, items=[]), calls)
    )

    response = client.post(
        "/vsearch/run", json=FILTER, headers={"Referer": "http://testserver/runs/1"}
    )

    assert response.status_code == 200
    assert len(calls) == 1


def test_cross_origin_delete_clone_is_rejected_before_routing(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    response = client.delete("/runs/999999/clone", headers={"Origin": "http://evil.example"})

    assert response.status_code == 403


def test_state_changing_methods_cover_write_verbs():
    assert STATE_CHANGING_METHODS == frozenset({"POST", "PUT", "PATCH", "DELETE"})


def test_same_origin_matches_host_scheme_and_effective_port():
    base = URL("http://testserver")

    assert _same_origin("http://testserver", base) is True
    assert _same_origin("http://testserver:80", base) is True
    assert _same_origin("https://testserver", base) is False
    assert _same_origin("http://testserver:9999", base) is False
    assert _same_origin("null", base) is False
    assert _same_origin("http://evil.example", base) is False


@pytest.fixture(autouse=True)
def _allow_testclient_host(monkeypatch):
    monkeypatch.setenv("GITCRAWL_ALLOWED_HOSTS", "testserver")


@pytest.mark.parametrize(
    "base_url",
    ["http://localhost", "http://127.0.0.1:8000", "http://[::1]:8000"],
)
def test_loopback_hosts_are_allowed(clean: Engine, tmp_path, base_url):
    application = create_app(engine=clean, runs_root=str(tmp_path / "runs"))
    client = TestClient(application, base_url=base_url, raise_server_exceptions=False)

    response = client.get("/health")

    assert response.status_code == 200


def test_non_local_host_is_rejected(clean: Engine, tmp_path):
    application = create_app(engine=clean, runs_root=str(tmp_path / "runs"))
    client = TestClient(application, base_url="http://evil.example", raise_server_exceptions=False)

    response = client.get("/health")

    assert response.status_code == 400
    assert "Invalid host header" in response.text


def test_allowed_hosts_env_extends_the_allowlist(clean: Engine, tmp_path, monkeypatch):
    monkeypatch.setenv("GITCRAWL_ALLOWED_HOSTS", "gitcrawl.internal")
    application = create_app(engine=clean, runs_root=str(tmp_path / "runs"))

    allowed = TestClient(
        application, base_url="http://gitcrawl.internal", raise_server_exceptions=False
    )
    replaced = TestClient(application, base_url="http://testserver", raise_server_exceptions=False)

    assert allowed.get("/health").status_code == 200
    assert replaced.get("/health").status_code == 400


@pytest.mark.parametrize("size", [JSON_BODY_LIMIT_BYTES - 1, JSON_BODY_LIMIT_BYTES])
def test_json_body_at_or_below_the_cap_is_parsed_not_rejected(clean: Engine, tmp_path, size):
    client = make_client(clean, tmp_path)

    response = client.post(
        "/vsearch/run",
        content=b"x" * size,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_param"


def test_json_body_above_the_cap_is_413(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    response = client.post(
        "/vsearch/run",
        content=b"x" * (JSON_BODY_LIMIT_BYTES + 1),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {
        "error": "payload_too_large",
        "limit": JSON_BODY_LIMIT_BYTES,
    }


@pytest.mark.parametrize("size", [UPLOAD_BODY_LIMIT_BYTES - 1, UPLOAD_BODY_LIMIT_BYTES])
def test_find_upload_at_or_below_the_cap_is_not_413(clean: Engine, tmp_path, size):
    client = make_client(clean, tmp_path)

    response = client.post("/find", content=b"x" * size)

    assert response.status_code == 403  # CSRF check, i.e. the cap did not trigger


def test_find_upload_above_the_cap_is_413(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    response = client.post("/find", content=b"x" * (UPLOAD_BODY_LIMIT_BYTES + 1))

    assert response.status_code == 413


def test_chunked_body_without_content_length_is_capped(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    def chunks():
        yield b"x" * (JSON_BODY_LIMIT_BYTES // 2)
        yield b"x" * (JSON_BODY_LIMIT_BYTES // 2)
        yield b"x"

    response = client.post(
        "/vsearch/run", content=chunks(), headers={"content-type": "application/json"}
    )

    assert response.status_code == 413


def test_get_repos_timeout_returns_503(clean: Engine, tmp_path, monkeypatch):
    monkeypatch.setenv("GITCRAWL_REQUEST_DEADLINE_SECONDS", "0.05")
    release = threading.Event()
    started = threading.Event()

    def blocking(run_id: int, filter_spec: dict) -> RunPayload:
        started.set()
        release.wait(10)
        return RunPayload(total_count=0, items=[])

    client = make_client(clean, tmp_path, runner=blocking)
    try:
        response = client.get("/vsearch/repos", params={"q": "language:rust"})
    finally:
        release.set()

    assert response.status_code == 503
    assert response.json() == {"error": "timeout", "retry_after": 1}


def test_post_run_timeout_returns_503(clean: Engine, tmp_path, monkeypatch):
    monkeypatch.setenv("GITCRAWL_REQUEST_DEADLINE_SECONDS", "0.05")
    release = threading.Event()

    def blocking(run_id: int, filter_spec: dict) -> RunPayload:
        release.wait(10)
        return RunPayload(total_count=0, items=[])

    client = make_client(clean, tmp_path, runner=blocking)
    try:
        response = client.post("/vsearch/run", json=FILTER)
    finally:
        release.set()

    assert response.status_code == 503
    assert response.json() == {"error": "timeout", "retry_after": 1}


def test_deadline_env_does_not_affect_a_fast_run(clean: Engine, tmp_path):
    calls: list = []
    client = make_client(
        clean, tmp_path, runner=counting_runner(RunPayload(total_count=0, items=[]), calls)
    )

    response = client.post("/vsearch/run", json=FILTER)

    assert response.status_code == 200
    assert len(calls) == 1


def test_health_reports_degraded_redis_when_the_url_is_unreachable(
    clean: Engine, tmp_path, monkeypatch
):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6390/0")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKENS", raising=False)
    client = make_client(clean, tmp_path, redis_ping=lambda: False)

    body = client.get("/health").json()

    assert body["redis"] is False
    assert body["redis_degraded"] is True


def test_health_omits_degraded_redis_when_redis_is_healthy(clean: Engine, tmp_path, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6390/0")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKENS", raising=False)
    client = make_client(clean, tmp_path, redis_ping=lambda: True)

    body = client.get("/health").json()

    assert body == {"database": True, "redis": True, "github_token_present": False}


def test_health_omits_degraded_redis_without_a_configured_url(clean: Engine, tmp_path, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKENS", raising=False)
    client = make_client(clean, tmp_path, redis_ping=lambda: False)

    body = client.get("/health").json()

    assert body == {"database": True, "redis": False, "github_token_present": False}


def test_health_returns_false_quickly_for_a_hung_database(clean: Engine, tmp_path):
    release = threading.Event()

    class HangingEngine:
        def connect(self):
            release.wait(30)
            raise RuntimeError("unreachable")

    client = make_client(HangingEngine(), tmp_path)
    started = time.monotonic()
    try:
        response = client.get("/health")
    finally:
        release.set()

    assert response.status_code == 200
    assert response.json()["database"] is False
    assert time.monotonic() - started < 2.0
