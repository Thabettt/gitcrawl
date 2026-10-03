from __future__ import annotations

import re
import time

import fakeredis
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from serve.app import create_app
from serve.executor import RunPayload, RunPayloadItem
from serve.filter_spec import describe_spec, parse_filter_spec, spec_hash, spec_to_dict
from serve.pages import CSRF_COOKIE

HTML = {"Accept": "text/html"}
SPEC = {"gitcrawl_filter": 1, "q": "language:rust"}
RICH_SPEC = {
    "gitcrawl_filter": 1,
    "q": "language:rust",
    "virtual": {"min_stars": 50},
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


def healthy_client(engine: Engine, tmp_path, *, calls: list | None = None) -> TestClient:
    captured = calls if calls is not None else []

    def runner(_run_id: int, filter_spec: dict) -> RunPayload:
        captured.append(filter_spec)
        return RunPayload(total_count=1, fetched=1, items=[payload_item()])

    application = create_app(
        engine=engine,
        runner_factory=lambda _engine: runner,
        runs_root=str(tmp_path),
        redis_ping=fakeredis.FakeRedis().ping,
        token_present=lambda: True,
    )
    return TestClient(application, raise_server_exceptions=False, follow_redirects=False)


def wait_for_run(engine: Engine, run_id: int, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            status = connection.scalar(
                text("SELECT status FROM runs WHERE id = :id"), {"id": run_id}
            )
        if status in ("done", "partial", "failed"):
            return str(status)
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} did not reach a terminal status in {timeout}s")


def csrf_token(client: TestClient) -> str:
    client.get("/find")
    token = client.cookies.get(CSRF_COOKIE)
    assert token
    return token


def save_filter(client: TestClient, name: str, spec: dict) -> int:
    response = client.post("/filters", json={"name": name, "spec": spec})
    assert response.status_code == 201
    return int(response.json()["id"])


def row_html(html: str, filter_id: int) -> str:
    match = re.search(rf'<tr id="saved-filter-{filter_id}".*?</tr>', html, re.S)
    assert match, f"no library row for filter {filter_id}"
    return match.group(0)


def test_library_run_now_creates_a_run_and_redirects(clean: Engine, tmp_path):
    calls: list = []
    client = healthy_client(clean, tmp_path, calls=calls)
    filter_id = save_filter(client, "rust picks", SPEC)
    token = csrf_token(client)

    response = client.post(f"/filters/{filter_id}/run", headers={"x-csrf-token": token})

    assert response.status_code == 303
    location = response.headers["location"]
    assert re.fullmatch(r"/runs/\d+", location)
    run_id = int(location.rsplit("/", 1)[1])
    assert wait_for_run(clean, run_id) == "done"
    assert client.get(location).status_code == 200
    expected = spec_to_dict(parse_filter_spec(SPEC))
    with clean.connect() as connection:
        row = connection.execute(
            text("SELECT filter_hash, filter_spec, status FROM runs WHERE id = :id"),
            {"id": run_id},
        ).one()
    assert row[0] == spec_hash(parse_filter_spec(SPEC))
    assert row[1] == expected
    assert row[2] == "done"
    assert calls == [expected]


def test_library_run_now_requires_csrf(clean: Engine, tmp_path):
    client = healthy_client(clean, tmp_path)
    filter_id = save_filter(client, "guarded", SPEC)

    response = client.post(f"/filters/{filter_id}/run")

    assert response.status_code == 403
    with clean.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM runs")) == 0


def test_library_run_now_unknown_filter_renders_a_library_error(clean: Engine, tmp_path):
    client = healthy_client(clean, tmp_path)
    token = csrf_token(client)

    response = client.post("/filters/424242/run", headers={"x-csrf-token": token})

    assert response.status_code == 404
    assert 'id="library-error"' in response.text
    with clean.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM runs")) == 0


def test_library_run_now_bad_stored_spec_renders_a_hint(clean: Engine, tmp_path):
    client = healthy_client(clean, tmp_path)
    token = csrf_token(client)
    with clean.begin() as connection:
        filter_id = connection.execute(
            text(
                "INSERT INTO saved_filters (name, filter_spec) "
                "VALUES ('broken', CAST(:spec AS jsonb)) RETURNING id"
            ),
            {"spec": '{"gitcrawl_filter": 1, "bogus": true}'},
        ).scalar_one()

    response = client.post(f"/filters/{filter_id}/run", headers={"x-csrf-token": token})

    assert response.status_code == 400
    assert 'id="library-error"' in response.text
    assert "bogus" in response.text
    with clean.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM runs")) == 0


def test_library_row_shows_plain_sentence_and_explains_run_now(clean: Engine, tmp_path):
    client = healthy_client(clean, tmp_path)
    filter_id = save_filter(client, "rust picks", RICH_SPEC)

    body = client.get("/filters", headers=HTML).text
    row = row_html(body, filter_id)

    expected_sentence = describe_spec(parse_filter_spec(RICH_SPEC))
    assert expected_sentence in row
    assert "recipe" in body
    assert "Run now" in body
    assert "Searches and Corpora are untouched" in body
    assert f'href="/find?filter={filter_id}"' in row


def test_library_row_links_to_last_used_run_with_a_count(clean: Engine, tmp_path):
    client = healthy_client(clean, tmp_path)
    filter_id = save_filter(client, "rust picks", SPEC)
    token = csrf_token(client)

    response = client.post(f"/filters/{filter_id}/run", headers={"x-csrf-token": token})
    run_id = int(response.headers["location"].rsplit("/", 1)[1])
    assert wait_for_run(clean, run_id) == "done"

    body = client.get("/filters", headers=HTML).text
    row = row_html(body, filter_id)

    assert f'href="/runs/{run_id}"' in row
    assert "1 repo" in row
    assert 'data-no-run="true"' not in row
    assert "never" not in row


def test_library_delete_confirmation_uses_a_modal(clean: Engine, tmp_path):
    client = healthy_client(clean, tmp_path)
    filter_id = save_filter(client, "rust picks", SPEC)

    body = client.get("/filters", headers=HTML).text

    assert "onsubmit" not in body
    assert f'id="delete-filter-{filter_id}"' in body
    assert f'data-open="delete-filter-{filter_id}"' in body
    assert f'action="/filters/{filter_id}/delete"' in body
    opener = re.search(rf'<button[^>]*data-open="delete-filter-{filter_id}"[^>]*>', body)
    assert opener and 'type="button"' in opener.group(0)
