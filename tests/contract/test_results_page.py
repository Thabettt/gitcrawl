from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from lib.gh_client import API_VERSION
from serve.app import create_app
from serve.executor import RunPayload, RunPayloadItem, create_run, execute_run

FILTER = {"gitcrawl_filter": 1, "q": "language:rust"}


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
                "size_kb": 1024,
                "stargazers": 80,
                "forks_count": 42,
                "pushed_at": "2026-01-03T00:00:00Z",
                "archived": False,
                "language": "Ruby",
                "license_spdx": "MIT",
            },
            {
                "id": 101,
                "node_id": "R_101",
                "full_name": "octo/alpha",
                "owner_id": 1,
                "name": "alpha",
                "visibility": "public",
                "size_kb": 1024,
                "stargazers": 30,
                "forks_count": 7,
                "pushed_at": "2026-01-03T00:00:00Z",
                "archived": False,
                "language": "Ruby",
                "license_spdx": "MIT",
            },
            {
                "id": 102,
                "node_id": "R_102",
                "full_name": "octo/beta",
                "owner_id": 1,
                "name": "beta",
                "visibility": "public",
                "size_kb": 1024,
                "stargazers": 20,
                "forks_count": 5,
                "pushed_at": "2026-01-01T00:00:00Z",
                "archived": False,
                "language": "Rust",
                "license_spdx": "Apache-2.0",
            },
            {
                "id": 103,
                "node_id": "R_103",
                "full_name": "octo/gamma",
                "owner_id": 1,
                "name": "gamma",
                "visibility": "public",
                "size_kb": 1024,
                "stargazers": 10,
                "forks_count": 3,
                "pushed_at": "2026-01-02T00:00:00Z",
                "archived": True,
                "language": "Go",
                "license_spdx": "MIT",
            },
        ],
    )


def make_client(engine: Engine, tmp_path) -> TestClient:
    application = create_app(
        engine=engine,
        runs_root=str(tmp_path / "runs"),
        clone_root=str(tmp_path / "clones"),
    )
    return TestClient(application, raise_server_exceptions=False)


def healthy_client(engine: Engine, tmp_path, monkeypatch, **overrides) -> TestClient:
    monkeypatch.setenv("GITHUB_TOKEN", "super-secret-token-value")
    monkeypatch.delenv("GITHUB_TOKENS", raising=False)
    return make_client(engine, tmp_path)


def payload_item(repo_id: int, full_name: str, stars: int, **overrides) -> RunPayloadItem:
    values: dict[str, object] = {
        "pushed_at": "2026-01-02T00:00:00Z",
        "archived": False,
        "language": "Ruby",
        "license_spdx": "MIT",
        "country_iso": "DE",
        "geo_confidence": "name",
        "virtuals": {},
    }
    values.update(overrides)
    return RunPayloadItem(repo_id=repo_id, full_name=full_name, stargazers=stars, **values)


ORDER_ITEMS = [
    payload_item(101, "octo/alpha", 30, pushed_at="2026-01-03T00:00:00Z"),
    payload_item(102, "octo/beta", 20, pushed_at="2026-01-01T00:00:00Z"),
    payload_item(103, "octo/gamma", 10, pushed_at="2026-01-02T00:00:00Z", archived=True),
]


def seed_run(engine: Engine, tmp_path, *, items: list[RunPayloadItem] | None = None) -> int:
    run_id = create_run(engine, FILTER, api_version=API_VERSION)
    payload_items = items if items is not None else [payload_item(1296269, "octo/hello", 80)]
    payload = RunPayload(
        total_count=len(payload_items), fetched=len(payload_items), items=payload_items
    )
    execute_run(
        engine, run_id, runner=lambda _rid, _spec: payload, runs_root=str(tmp_path / "runs")
    )
    return run_id


def seed_bulk_run(engine: Engine, count: int = 51) -> int:
    run_id = create_run(engine, FILTER, api_version=API_VERSION)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    with engine.begin() as connection:
        for index in range(1, count + 1):
            repo_id = 9000 + index
            full_name = f"octo/repo-{index:03d}"
            pushed = base + timedelta(days=index)
            connection.execute(
                text(
                    "INSERT INTO repos (id, node_id, full_name, owner_id, name, visibility, "
                    "size_kb, stargazers, pushed_at, archived, language, license_spdx) VALUES "
                    "(:id, :node_id, :full_name, 1, :name, 'public', 1024, :stars, :pushed, "
                    "false, 'Ruby', 'MIT')"
                ),
                {
                    "id": repo_id,
                    "node_id": f"R_{repo_id}",
                    "full_name": full_name,
                    "name": f"repo-{index:03d}",
                    "stars": index,
                    "pushed": pushed,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO run_items (run_id, repo_id, full_name, stargazers, pushed_at, "
                    "archived, language, license_spdx, country_iso, geo_confidence, virtuals) "
                    "VALUES (:run_id, :repo_id, :full_name, :stars, :pushed, false, 'Ruby', "
                    "'MIT', 'DE', 'name', '{}'::jsonb)"
                ),
                {
                    "run_id": run_id,
                    "repo_id": repo_id,
                    "full_name": full_name,
                    "stars": index,
                    "pushed": pushed,
                },
            )
    return run_id


def table_names(html: str) -> list[str]:
    return re.findall(r'data-full-name="([^"]+)"', html)


def test_results_page_renders_with_page_size_selector(clean, tmp_path, monkeypatch):
    run_id = seed_run(clean, tmp_path)
    body = healthy_client(clean, tmp_path, monkeypatch).get(f"/runs/{run_id}/results").text
    assert "Per page" in body
    for size in ("25", "50", "100", "200"):
        assert f">{size}<" in body
    assert "View all results" not in body  # this IS the full view


def test_results_page_invalid_page_renders_hint(clean, tmp_path, monkeypatch):
    run_id = seed_run(clean, tmp_path)
    response = healthy_client(clean, tmp_path, monkeypatch).get(f"/runs/{run_id}/results?page=abc")
    assert response.status_code == 400
    assert "whole number" in response.text


def test_results_page_renders_every_full_column(clean, tmp_path, monkeypatch):
    run_id = seed_run(clean, tmp_path, items=ORDER_ITEMS)
    body = healthy_client(clean, tmp_path, monkeypatch).get(f"/runs/{run_id}/results").text

    for header in (
        "Repository",
        "Stars",
        "Forks",
        "Updated",
        "Language",
        "License",
        "Country",
        "Flags",
        "Saved time",
    ):
        assert f">{header}<" in body
    assert ">42<" in body or ">7<" in body  # forks come from the repo record
    assert "Rust" in body  # the plain filter sentence
    assert 'id="run-items"' in body


def test_results_preview_keeps_five_compact_columns(clean, tmp_path, monkeypatch):
    run_id = seed_run(clean, tmp_path, items=ORDER_ITEMS)
    client = healthy_client(clean, tmp_path, monkeypatch)

    preview = client.get(f"/partials/runs/{run_id}/table").text

    assert ">Repository<" in preview
    assert ">Stars<" in preview
    assert ">Updated<" in preview
    assert ">Country<" in preview
    assert ">Flags<" in preview
    assert ">Language<" not in preview
    assert ">License<" not in preview
    assert ">Forks<" not in preview
    assert ">Saved time<" not in preview
    assert not table_names(client.get(f"/runs/{run_id}/results").text) == []


def test_results_page_url_carries_pagination_state(clean, tmp_path, monkeypatch):
    run_id = seed_bulk_run(clean, 51)
    client = healthy_client(clean, tmp_path, monkeypatch)

    body = client.get(
        f"/runs/{run_id}/results", params={"page": 2, "per_page": 25, "sort": "stars", "dir": "asc"}
    ).text

    assert 'data-page="2"' in body
    assert 'data-pages="3"' in body
    assert 'data-shown="25"' in body
    assert f'href="/runs/{run_id}/results?page=1&per_page=25&sort=stars&dir=asc"' in body
    assert f'href="/runs/{run_id}/results?page=3&per_page=25&sort=stars&dir=asc"' in body
    assert f'href="/runs/{run_id}/results?sort=stars&dir=desc&page=1&per_page=25"' in body
    assert 'name="per_page"' in body


def test_results_page_honors_other_page_sizes(clean, tmp_path, monkeypatch):
    run_id = seed_bulk_run(clean, 51)
    client = healthy_client(clean, tmp_path, monkeypatch)

    body = client.get(f"/runs/{run_id}/results", params={"per_page": 100}).text

    assert 'data-shown="51"' in body
    assert 'data-pages="1"' in body


@pytest.mark.parametrize("value", ["7", "0", "big"])
def test_results_page_invalid_per_page_renders_hint(clean, tmp_path, monkeypatch, value):
    run_id = seed_run(clean, tmp_path)
    client = healthy_client(clean, tmp_path, monkeypatch)

    response = client.get(f"/runs/{run_id}/results", params={"per_page": value})

    assert response.status_code == 400
    assert "25, 50, 100, 200" in response.text
    assert 'id="run-items"' in response.text


def test_results_page_invalid_sort_and_direction_render_hints(clean, tmp_path, monkeypatch):
    run_id = seed_run(clean, tmp_path, items=ORDER_ITEMS)
    client = healthy_client(clean, tmp_path, monkeypatch)

    response = client.get(f"/runs/{run_id}/results", params={"sort": "bogus", "dir": "sideways"})

    assert response.status_code == 400
    assert "stars, pushed, name" in response.text
    assert "asc, desc" in response.text
    assert table_names(response.text) == ["octo/alpha", "octo/beta", "octo/gamma"]


def test_results_page_filter_row_is_a_placeholder(clean, tmp_path, monkeypatch):
    run_id = seed_run(clean, tmp_path)
    body = healthy_client(clean, tmp_path, monkeypatch).get(f"/runs/{run_id}/results").text

    assert 'id="results-filters"' in body
    for control in ("filter-dockerfile", "filter-country", "filter-language"):
        assert f'id="{control}"' in body
    assert body.count("disabled") >= 3
    assert "Not available yet" in body


def test_results_page_offers_export_with_explanation(clean, tmp_path, monkeypatch):
    run_id = seed_run(clean, tmp_path)
    with clean.connect() as connection:
        filter_hash = connection.scalar(
            text("SELECT filter_hash FROM runs WHERE id = :id"), {"id": run_id}
        )
    body = healthy_client(clean, tmp_path, monkeypatch).get(f"/runs/{run_id}/results").text

    assert f'href="/vsearch/runs/{filter_hash}/export?format=json"' in body
    assert f'href="/vsearch/runs/{filter_hash}/export?format=csv"' in body
    assert "Downloads the frozen result list as JSON or CSV." in body


def test_results_page_unknown_run_is_404(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)

    response = client.get("/runs/424242/results")

    assert response.status_code == 404
    assert 'id="results-not-found"' in response.text
