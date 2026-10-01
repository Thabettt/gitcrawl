from __future__ import annotations

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from lib.gh_client import API_VERSION
from serve.app import create_app
from serve.executor import RunPayload, RunPayloadItem, create_run, execute_run
from serve.filter_spec import parse_filter_spec, spec_hash

SPEC = {"gitcrawl_filter": 1, "q": "language:rust", "sort": "stars"}
ITEM_KEYS = {"id", "name", "filter_hash", "created_at", "updated_at"}


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(clean_db):
    return clean_db(
        owners=[{"id": 1, "login": "octo", "type": "User"}],
        repos=[
            {
                "id": 1,
                "node_id": "R_1",
                "full_name": "octo/r1",
                "owner_id": 1,
                "name": "r1",
                "visibility": "public",
            },
            {
                "id": 2,
                "node_id": "R_2",
                "full_name": "octo/r2",
                "owner_id": 1,
                "name": "r2",
                "visibility": "public",
            },
            {
                "id": 3,
                "node_id": "R_3",
                "full_name": "octo/r3",
                "owner_id": 1,
                "name": "r3",
                "visibility": "public",
            },
        ],
    )


def run_item(repo_id: int, **overrides) -> RunPayloadItem:
    values: dict[str, object] = {
        "repo_id": repo_id,
        "full_name": f"octo/r{repo_id}",
        "stargazers": 10,
        "pushed_at": "2026-01-01T00:00:00Z",
        "archived": False,
        "language": "Rust",
        "license_spdx": "MIT",
        "country_iso": "DE",
        "geo_confidence": "name",
        "virtuals": {},
    }
    values.update(overrides)
    return RunPayloadItem(**values)


def seed_run(engine: Engine, tmp_path, items: list[RunPayloadItem]) -> int:
    run_id = create_run(engine, SPEC, api_version=API_VERSION)
    payload = RunPayload(total_count=len(items), fetched=len(items), items=list(items))
    execute_run(
        engine, run_id, runner=lambda _rid, _spec: payload, runs_root=str(tmp_path / "runs")
    )
    return run_id


@pytest.fixture()
def client(clean: Engine, tmp_path) -> TestClient:
    application = create_app(
        engine=clean,
        runs_root=str(tmp_path / "runs"),
        clone_root=str(tmp_path / "clones"),
    )
    return TestClient(application, raise_server_exceptions=False)


def test_list_filters_is_empty_by_default(client: TestClient):
    response = client.get("/filters")

    assert response.status_code == 200
    assert response.json() == {"filters": []}


def test_create_filter_returns_201_and_list_view_without_spec(client: TestClient):
    created = client.post("/filters", json={"name": "  Rust Finds  ", "spec": SPEC})

    assert created.status_code == 201
    body = created.json()
    assert set(body) == {"id", "name", "filter_hash"}
    assert body["name"] == "Rust Finds"
    assert body["filter_hash"] == spec_hash(parse_filter_spec(SPEC))

    listed = client.get("/filters")
    assert listed.status_code == 200
    filters = listed.json()["filters"]
    assert len(filters) == 1
    item = filters[0]
    assert set(item) == ITEM_KEYS
    assert item["id"] == body["id"]
    assert item["name"] == "Rust Finds"
    assert item["filter_hash"] == body["filter_hash"]
    assert item["created_at"].endswith("Z")
    assert item["updated_at"].endswith("Z")
    assert "language:rust" not in listed.text
    assert "spec" not in item


def test_create_filter_invalid_spec_is_400_with_hints(client: TestClient):
    response = client.post(
        "/filters", json={"name": "broken", "spec": {"gitcrawl_filter": 1, "bogus": 1}}
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_spec"
    assert body["hints"]
    assert "bogus" in body["message"]


def test_create_filter_invalid_name_is_400_with_hints(client: TestClient):
    response = client.post("/filters", json={"name": "   ", "spec": SPEC})

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_name"
    assert body["hints"]


def test_create_filter_duplicate_name_is_409(client: TestClient):
    assert client.post("/filters", json={"name": "dup", "spec": SPEC}).status_code == 201

    response = client.post("/filters", json={"name": "dup", "spec": SPEC})

    assert response.status_code == 409
    assert response.json()["error"] == "duplicate_name"


@pytest.mark.parametrize(
    "document",
    [
        {"name": "no-spec"},
        {"name": "bad-spec", "spec": "nope"},
        {"name": "null-spec", "spec": None},
    ],
)
def test_create_filter_missing_or_non_object_spec_is_400(client: TestClient, document):
    response = client.post("/filters", json=document)

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_spec"
    assert body["hints"]


def test_create_filter_non_json_body_is_400(client: TestClient):
    response = client.post(
        "/filters", content="not-json", headers={"content-type": "application/json"}
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_spec"


def test_create_filter_non_object_body_is_400(client: TestClient):
    response = client.post("/filters", json=[1, 2, 3])

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_spec"


def test_rename_filter_returns_200_and_persists(client: TestClient):
    created = client.post("/filters", json={"name": "before", "spec": SPEC}).json()

    response = client.post(f"/filters/{created['id']}/rename", json={"name": "after"})

    assert response.status_code == 200
    assert response.json() == {
        "id": created["id"],
        "name": "after",
        "filter_hash": created["filter_hash"],
    }
    listed = client.get("/filters").json()["filters"]
    assert [item["name"] for item in listed] == ["after"]


def test_rename_filter_unknown_is_404(client: TestClient):
    response = client.post("/filters/424242/rename", json={"name": "nope"})

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_rename_filter_invalid_name_is_400(client: TestClient):
    created = client.post("/filters", json={"name": "valid", "spec": SPEC}).json()

    response = client.post(f"/filters/{created['id']}/rename", json={"name": ""})

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_name"


def test_rename_filter_collision_is_409(client: TestClient):
    first = client.post("/filters", json={"name": "first", "spec": SPEC}).json()
    client.post("/filters", json={"name": "second", "spec": SPEC})

    response = client.post(f"/filters/{first['id']}/rename", json={"name": "second"})

    assert response.status_code == 409
    assert response.json()["error"] == "duplicate_name"


def test_delete_filter_is_204_then_404(client: TestClient):
    created = client.post("/filters", json={"name": "gone", "spec": SPEC}).json()

    deleted = client.post(f"/filters/{created['id']}/delete")

    assert deleted.status_code == 204
    assert deleted.content == b""
    assert client.get("/filters").json() == {"filters": []}
    again = client.post(f"/filters/{created['id']}/delete")
    assert again.status_code == 404
    assert again.json()["error"] == "not_found"


def test_delete_filter_unknown_is_404(client: TestClient):
    response = client.post("/filters/424242/delete")

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_filter_json_api_does_not_require_csrf(clean: Engine, tmp_path):
    application = create_app(engine=clean, runs_root=str(tmp_path / "runs"))
    bare = TestClient(application, raise_server_exceptions=False)

    response = bare.post("/filters", json={"name": "local-only", "spec": SPEC})

    assert response.status_code == 201


def test_diff_endpoint_happy_path(client: TestClient, clean: Engine, tmp_path):
    run_a = seed_run(clean, tmp_path, [run_item(1), run_item(2)])
    run_b = seed_run(clean, tmp_path, [run_item(1, stargazers=20), run_item(3)])

    response = client.get(f"/api/runs/{run_b}/diff", params={"against": run_a})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"run_a", "run_b", "added", "removed", "changed", "summary"}
    assert body["run_a"] == run_a
    assert body["run_b"] == run_b
    assert body["added"] == [{"repo_id": 3, "full_name": "octo/r3"}]
    assert body["removed"] == [{"repo_id": 2, "full_name": "octo/r2"}]
    assert body["changed"] == [
        {"repo_id": 1, "full_name": "octo/r1", "field": "stargazers", "from": 10, "to": 20}
    ]
    assert body["summary"] == {
        "added": 1,
        "removed": 1,
        "changed_repos": 1,
        "changed_fields": 1,
    }


def test_diff_endpoint_identical_runs_is_empty(client: TestClient, clean: Engine, tmp_path):
    run_a = seed_run(clean, tmp_path, [run_item(1)])
    run_b = seed_run(clean, tmp_path, [run_item(1)])

    response = client.get(f"/api/runs/{run_b}/diff", params={"against": run_a})

    assert response.status_code == 200
    body = response.json()
    assert body["added"] == []
    assert body["removed"] == []
    assert body["changed"] == []
    assert body["summary"] == {
        "added": 0,
        "removed": 0,
        "changed_repos": 0,
        "changed_fields": 0,
    }


def test_diff_endpoint_unknown_runs_are_404(client: TestClient):
    path_unknown = client.get("/api/runs/424242/diff", params={"against": 1})
    against_unknown = client.get("/api/runs/1/diff", params={"against": 424242})

    assert path_unknown.status_code == 404
    assert path_unknown.json()["error"] == "run_not_found"
    assert against_unknown.status_code == 404
    assert against_unknown.json()["error"] == "run_not_found"


def test_diff_endpoint_requires_against(client: TestClient):
    missing = client.get("/api/runs/1/diff")
    malformed = client.get("/api/runs/1/diff", params={"against": "abc"})

    assert missing.status_code == 400
    assert missing.json()["param"] == "against"
    assert malformed.status_code == 400
    assert malformed.json()["param"] == "against"
