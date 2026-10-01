from __future__ import annotations

import csv
import io
import json

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from lib.gh_client import API_VERSION
from serve.app import create_app
from serve.executor import RunPayload, RunPayloadItem, create_run, execute_run
from serve.filter_spec import parse_filter_spec, spec_to_dict
from serve.runs import export_bundle, latest_run_for_hash, run_bundle_dir

FILTER = {"gitcrawl_filter": 1, "q": "language:rust"}
UNKNOWN_HASH = "0" * 64


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
        "raw": {"id": 1296269, "full_name": "octo/hello", "stargazers_count": 80},
    }
    values.update(overrides)
    return RunPayloadItem(**values)


def expected_snapshot() -> dict:
    return {
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


def make_client(engine: Engine, payload: RunPayload, runs_root) -> TestClient:
    def runner(run_id: int, filter_spec: dict) -> RunPayload:
        return payload

    application = create_app(
        engine=engine,
        runner_factory=lambda _engine: runner,
        runs_root=str(runs_root),
    )
    return TestClient(application, raise_server_exceptions=False)


def seed_run(client: TestClient, engine: Engine) -> tuple[str, int]:
    response = client.post("/vsearch/run", json=FILTER)
    assert response.status_code == 200
    filter_hash = response.json()["filter_hash"]
    with engine.connect() as connection:
        run_id = connection.scalar(
            text("SELECT id FROM runs WHERE filter_hash = :hash ORDER BY id DESC LIMIT 1"),
            {"hash": filter_hash},
        )
    return filter_hash, int(run_id)


def seed_failed_run(engine: Engine, filter_spec: dict | None = None) -> tuple[str, int]:
    normalized = spec_to_dict(parse_filter_spec(filter_spec or FILTER))
    run_id = create_run(engine, normalized, api_version=API_VERSION)

    def failing(_run_id: int, _spec: dict) -> RunPayload:
        raise RuntimeError("upstream exploded")

    execute_run(engine, run_id, runner=failing, runs_root="runs")
    with engine.connect() as connection:
        filter_hash = connection.scalar(
            text("SELECT filter_hash FROM runs WHERE id = :id"), {"id": run_id}
        )
    return str(filter_hash), int(run_id)


def stored_filter_spec(engine: Engine, run_id: int) -> dict:
    with engine.connect() as connection:
        return connection.scalar(
            text("SELECT filter_spec FROM runs WHERE id = :id"), {"id": run_id}
        )


def test_run_bundle_dir_matches_the_executor_layout(tmp_path):
    assert run_bundle_dir(str(tmp_path), "abc", 7) == tmp_path / "abc" / "7"


def test_latest_run_for_hash_returns_the_newest_run(clean: Engine, tmp_path):
    payload = RunPayload(total_count=1, fetched=1, items=[payload_item()])
    client = make_client(clean, payload, tmp_path)

    _, first_id = seed_run(client, clean)
    filter_hash, second_id = seed_run(client, clean)

    row = latest_run_for_hash(clean, filter_hash)
    assert row is not None
    assert row["id"] == second_id
    assert row["id"] != first_id
    assert latest_run_for_hash(clean, UNKNOWN_HASH) is None


def test_export_json_is_byte_identical_to_the_on_disk_bundle(clean: Engine, tmp_path):
    payload = RunPayload(total_count=1, fetched=1, items=[payload_item()])
    client = make_client(clean, payload, tmp_path)
    filter_hash, run_id = seed_run(client, clean)
    disk = (tmp_path / filter_hash / str(run_id) / "bundle.json").read_bytes()

    response = client.get(f"/vsearch/runs/{filter_hash}/export")

    assert response.status_code == 200
    assert response.content == disk
    assert response.headers["content-type"] == "application/json"
    assert "x-gitcrawl-regenerated" not in response.headers
    assert "regenerated" not in json.loads(disk.decode("utf-8"))
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="gitcrawl-{filter_hash}-{run_id}.json"'
    )


def test_export_csv_is_byte_identical_to_the_on_disk_corpus(clean: Engine, tmp_path):
    payload = RunPayload(total_count=1, fetched=1, items=[payload_item()])
    client = make_client(clean, payload, tmp_path)
    filter_hash, run_id = seed_run(client, clean)
    disk = (tmp_path / filter_hash / str(run_id) / "corpus.csv").read_bytes()

    response = client.get(f"/vsearch/runs/{filter_hash}/export", params={"format": "csv"})

    assert response.status_code == 200
    assert response.content == disk
    assert response.headers["content-type"].startswith("text/csv")
    assert "x-gitcrawl-regenerated" not in response.headers
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="gitcrawl-{filter_hash}-{run_id}.csv"'
    )


def test_export_uses_the_latest_run_for_the_hash(clean: Engine, tmp_path):
    payload = RunPayload(total_count=1, fetched=1, items=[payload_item()])
    client = make_client(clean, payload, tmp_path)
    filter_hash, first_id = seed_run(client, clean)
    _, second_id = seed_run(client, clean)
    assert first_id != second_id
    disk = (tmp_path / filter_hash / str(second_id) / "bundle.json").read_bytes()

    response = client.get(f"/vsearch/runs/{filter_hash}/export")

    assert response.status_code == 200
    assert response.content == disk
    assert f"gitcrawl-{filter_hash}-{second_id}.json" in response.headers["content-disposition"]


def test_export_uses_the_latest_completed_run_when_the_newest_run_failed(clean: Engine, tmp_path):
    payload = RunPayload(total_count=1, fetched=1, items=[payload_item()])
    client = make_client(clean, payload, tmp_path)
    filter_hash, done_id = seed_run(client, clean)
    failed_hash, failed_id = seed_failed_run(clean)
    assert failed_hash == filter_hash and failed_id > done_id
    disk = (tmp_path / filter_hash / str(done_id) / "bundle.json").read_bytes()

    response = client.get(f"/vsearch/runs/{filter_hash}/export")

    assert response.status_code == 200
    assert response.content == disk
    assert f"gitcrawl-{filter_hash}-{done_id}.json" in response.headers["content-disposition"]


def test_export_is_404_when_only_non_terminal_runs_exist(clean: Engine, tmp_path):
    payload = RunPayload(total_count=1, fetched=1, items=[payload_item()])
    client = make_client(clean, payload, tmp_path)
    filter_hash, _ = seed_failed_run(clean)

    response = client.get(f"/vsearch/runs/{filter_hash}/export")

    assert response.status_code == 404
    assert response.json() == {"error": "run_not_ready", "filter_hash": filter_hash}


def test_export_unknown_hash_is_404(clean: Engine, tmp_path):
    payload = RunPayload(total_count=0, fetched=0, items=[])
    client = make_client(clean, payload, tmp_path)

    response = client.get(f"/vsearch/runs/{UNKNOWN_HASH}/export")

    assert response.status_code == 404
    assert response.json() == {"error": "run_not_found", "filter_hash": UNKNOWN_HASH}


@pytest.mark.parametrize("fmt", ["xml", "JSON", "csv2", ""])
def test_export_invalid_format_is_400(clean: Engine, tmp_path, fmt):
    payload = RunPayload(total_count=1, fetched=1, items=[payload_item()])
    client = make_client(clean, payload, tmp_path)
    filter_hash, _ = seed_run(client, clean)

    response = client.get(f"/vsearch/runs/{filter_hash}/export", params={"format": fmt})

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_param"
    assert body["param"] == "format"
    assert "format" in body["hint"]


def test_export_regenerates_json_and_csv_when_bundle_files_are_absent(clean: Engine, tmp_path):
    payload = RunPayload(total_count=1, fetched=1, items=[payload_item()])
    client = make_client(clean, payload, tmp_path)
    filter_hash, run_id = seed_run(client, clean)
    directory = tmp_path / filter_hash / str(run_id)
    (directory / "bundle.json").unlink()
    (directory / "corpus.csv").unlink()

    json_response = client.get(f"/vsearch/runs/{filter_hash}/export")
    csv_response = client.get(f"/vsearch/runs/{filter_hash}/export", params={"format": "csv"})

    assert json_response.status_code == 200
    assert csv_response.status_code == 200
    assert json_response.headers["x-gitcrawl-regenerated"] == "true"
    assert csv_response.headers["x-gitcrawl-regenerated"] == "true"
    document = json.loads(json_response.content.decode("utf-8"))
    assert set(document) == {
        "filter",
        "filter_hash",
        "run_id",
        "ran_at",
        "api_version",
        "total_count",
        "fetched",
        "incomplete",
        "regenerated",
        "items",
        "field_stats",
    }
    assert document["filter"] == stored_filter_spec(clean, run_id)
    assert document["filter_hash"] == filter_hash
    assert document["run_id"] == run_id
    assert document["api_version"]
    assert document["total_count"] == 1
    assert document["fetched"] == 1
    assert document["incomplete"] is False
    assert document["regenerated"] is True
    assert document["ran_at"].endswith("Z")
    assert document["field_stats"] == {}
    assert document["items"] == [expected_snapshot()]
    rows = list(csv.reader(io.StringIO(csv_response.content.decode("utf-8"))))
    assert rows == [
        [
            "id",
            "full_name",
            "stargazers",
            "pushed_at",
            "archived",
            "language",
            "license_spdx",
            "country_iso",
            "geo_confidence",
        ],
        [
            "1296269",
            "octo/hello",
            "80",
            "2011-01-26T19:06:43Z",
            "false",
            "Ruby",
            "MIT",
            "DE",
            "name",
        ],
    ]


def test_export_regeneration_orders_rows_by_stargazers_desc_then_repo_id(clean: Engine, tmp_path):
    with clean.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO repos (id, node_id, full_name, owner_id, name, visibility, "
                "stargazers) VALUES (2, 'R_2', 'octo/two', 1, 'two', 'public', 5), "
                "(3, 'R_3', 'octo/three', 1, 'three', 'public', 5)"
            )
        )
    items = [
        payload_item(repo_id=2, full_name="octo/two", stargazers=5),
        payload_item(),
        payload_item(repo_id=3, full_name="octo/three", stargazers=5),
    ]
    payload = RunPayload(total_count=3, fetched=3, items=items)
    client = make_client(clean, payload, tmp_path)
    filter_hash, run_id = seed_run(client, clean)
    directory = tmp_path / filter_hash / str(run_id)
    (directory / "bundle.json").unlink()
    (directory / "corpus.csv").unlink()

    json_response = client.get(f"/vsearch/runs/{filter_hash}/export")
    csv_response = client.get(f"/vsearch/runs/{filter_hash}/export", params={"format": "csv"})

    assert json_response.status_code == 200
    assert csv_response.status_code == 200
    document = json.loads(json_response.content.decode("utf-8"))
    assert [item["repo_id"] for item in document["items"]] == [1296269, 2, 3]
    rows = list(csv.reader(io.StringIO(csv_response.content.decode("utf-8"))))
    assert [row[0] for row in rows[1:]] == ["1296269", "2", "3"]


def test_export_bundle_rejects_unknown_format_without_touching_the_database(
    clean: Engine, tmp_path
):
    with pytest.raises(ValueError):
        export_bundle(clean, 1, format="xml", runs_root=str(tmp_path))


def test_export_bundle_raises_keyerror_for_unknown_run(clean: Engine, tmp_path):
    with pytest.raises(KeyError):
        export_bundle(clean, 424242, format="json", runs_root=str(tmp_path))
