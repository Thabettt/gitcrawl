from __future__ import annotations

import json
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
HX = {"HX-Request": "true"}
RUN_MARKERS = (
    'id="run-header"',
    'id="run-hash"',
    'id="run-status-pill"',
    'id="run-counts"',
    'id="run-actions"',
    'id="run-table"',
    'id="run-items"',
    'id="clone-modal"',
    'id="clone-limit"',
    'id="clone-limit-input"',
    'id="clone-estimate"',
    'id="clone-start"',
    'id="clone-progress"',
)


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


def seed_run(
    engine: Engine,
    tmp_path,
    *,
    items: list[RunPayloadItem] | None = None,
    total: int | None = None,
    fetched: int | None = None,
    incomplete: bool = False,
    spec: dict | None = None,
    error: BaseException | None = None,
) -> int:
    run_id = create_run(engine, spec or FILTER, api_version=API_VERSION)
    payload_items = items if items is not None else [payload_item(1296269, "octo/hello", 80)]
    resolved_total = total if total is not None else len(payload_items)
    resolved_fetched = fetched if fetched is not None else len(payload_items)

    def runner(_run_id: int, _spec: dict) -> RunPayload:
        if error is not None:
            raise error
        return RunPayload(
            total_count=resolved_total,
            fetched=resolved_fetched,
            incomplete=incomplete,
            items=payload_items,
        )

    execute_run(engine, run_id, runner=runner, runs_root=str(tmp_path / "runs"))
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


def test_run_page_renders_full_detail(clean: Engine, tmp_path):
    run_id = seed_run(clean, tmp_path)
    client = make_client(clean, tmp_path)

    response = client.get(f"/runs/{run_id}")

    assert response.status_code == 200
    html = response.text
    for marker in RUN_MARKERS:
        assert marker in html
    assert re.search(r'<span id="run-hash"[^>]*>\w{8}</span>', html)
    assert 'id="copy-hash"' in html
    assert 'data-open="clone-modal"' in html
    assert f'action="/runs/{run_id}/replay"' in html
    assert 'name="csrf"' in html
    assert 'name="mode"' in html
    assert html.count('name="mode"') == 3
    assert 'id="run-duration"' in html
    assert 'id="run-created"' in html
    assert 'id="run-finished"' in html
    assert 'id="run-flags"' not in html
    assert f'hx-get="/runs/{run_id}/clone-estimate"' in html
    assert "every 2s [document.visibilityState === 'visible']" not in html
    assert "every 1s [document.visibilityState === 'visible']" not in html
    assert 'colspan="7"' not in html
    assert "octo/hello" in html


def test_run_page_reports_counts_and_export_links(clean: Engine, tmp_path):
    run_id = seed_run(clean, tmp_path, total=4, fetched=3, incomplete=True)
    client = make_client(clean, tmp_path)

    response = client.get(f"/runs/{run_id}")

    html = response.text
    assert 'data-status="partial"' in html
    assert 'id="run-counts"' in html
    assert 'data-count="fetched">fetched <strong>3</strong>' in html
    assert 'data-count="inserted">inserted <strong>1</strong>' in html
    assert 'data-count="incomplete">incomplete <strong>1</strong>' in html
    assert 'id="run-flags"' in html
    assert 'data-flag="incomplete"' in html
    assert 'data-flag="truncation"' in html


def test_run_page_reports_updated_unchanged_and_skipped(clean: Engine, tmp_path):
    run_id = seed_run(clean, tmp_path)
    with clean.begin() as connection:
        connection.execute(
            text("UPDATE runs SET updated = 2, unchanged = 3, skipped = 4 WHERE id = :id"),
            {"id": run_id},
        )
    client = make_client(clean, tmp_path)

    html = client.get(f"/runs/{run_id}").text

    assert 'data-count="updated">updated <strong>2</strong>' in html
    assert 'data-count="unchanged">unchanged <strong>3</strong>' in html
    assert 'data-count="skipped">skipped <strong>4</strong>' in html


def test_run_page_shows_r44_and_failed_flags(clean: Engine, tmp_path):
    r44_spec = {**FILTER, "virtual": {"min_commits": 10, "min_loc": 500}}
    r44_run = seed_run(clean, tmp_path, spec=r44_spec, incomplete=True)
    failed_run = seed_run(clean, tmp_path, error=RuntimeError("upstream exploded"))
    client = make_client(clean, tmp_path)

    r44_html = client.get(f"/runs/{r44_run}").text
    failed_html = client.get(f"/runs/{failed_run}").text

    assert "min_commits" in r44_html
    assert "min_loc" in r44_html
    assert "unenforceable" in r44_html
    assert 'data-flag="r44"' in r44_html
    assert 'data-flag="error"' in failed_html
    assert "RuntimeError: upstream exploded" in failed_html
    assert 'data-status="failed"' in failed_html


def test_run_page_polls_only_while_non_terminal(clean: Engine, tmp_path):
    run_id = seed_run(clean, tmp_path)
    with clean.begin() as connection:
        connection.execute(
            text("UPDATE runs SET status = 'running' WHERE id = :id"), {"id": run_id}
        )
    client = make_client(clean, tmp_path)

    running = client.get(f"/runs/{run_id}")
    with clean.begin() as connection:
        connection.execute(text("UPDATE runs SET status = 'done' WHERE id = :id"), {"id": run_id})
    done = client.get(f"/runs/{run_id}")

    assert "every 2s [document.visibilityState === 'visible']" in running.text
    assert f'hx-get="/partials/runs/{run_id}/status"' in running.text
    assert 'hx-swap="outerHTML"' in running.text
    assert 'data-status="running"' in running.text
    assert "every 2s [document.visibilityState === 'visible']" not in done.text
    assert 'data-status="done"' in done.text


def test_run_page_unknown_is_404(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    response = client.get("/runs/424242")

    assert response.status_code == 404


def test_status_fragment_polls_while_running_and_stops_when_terminal(clean: Engine, tmp_path):
    run_id = seed_run(clean, tmp_path)
    with clean.begin() as connection:
        connection.execute(text("UPDATE runs SET status = 'queued' WHERE id = :id"), {"id": run_id})
    client = make_client(clean, tmp_path)

    queued = client.get(f"/partials/runs/{run_id}/status")
    with clean.begin() as connection:
        connection.execute(
            text("UPDATE runs SET status = 'done', finished_at = now() WHERE id = :id"),
            {"id": run_id},
        )
    done = client.get(f"/partials/runs/{run_id}/status")

    assert queued.status_code == done.status_code == 200
    assert 'id="run-status"' in queued.text
    assert 'data-status="queued"' in queued.text
    assert "every 2s [document.visibilityState === 'visible']" in queued.text
    assert 'hx-swap="outerHTML"' in queued.text
    assert "every 2s [document.visibilityState === 'visible']" not in done.text
    assert 'data-status="done"' in done.text
    assert 'id="run-counts"' in done.text


def test_status_fragment_unknown_run_is_404(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    response = client.get("/partials/runs/424242/status")

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("sort=stars&dir=desc", ["octo/alpha", "octo/beta", "octo/gamma"]),
        ("sort=stars&dir=asc", ["octo/gamma", "octo/beta", "octo/alpha"]),
        ("sort=pushed&dir=asc", ["octo/beta", "octo/gamma", "octo/alpha"]),
        ("sort=pushed&dir=desc", ["octo/alpha", "octo/gamma", "octo/beta"]),
        ("sort=name&dir=asc", ["octo/alpha", "octo/beta", "octo/gamma"]),
        ("sort=name&dir=desc", ["octo/gamma", "octo/beta", "octo/alpha"]),
    ],
)
def test_table_fragment_sort_keys_and_directions(clean: Engine, tmp_path, query, expected):
    run_id = seed_run(clean, tmp_path, items=ORDER_ITEMS)
    client = make_client(clean, tmp_path)

    response = client.get(f"/partials/runs/{run_id}/table?{query}")

    assert response.status_code == 200
    assert table_names(response.text) == expected


def test_table_fragment_default_sort_and_htmx_header_wiring(clean: Engine, tmp_path):
    run_id = seed_run(clean, tmp_path, items=ORDER_ITEMS)
    client = make_client(clean, tmp_path)

    response = client.get(f"/partials/runs/{run_id}/table")

    html = response.text
    assert table_names(html) == ["octo/alpha", "octo/beta", "octo/gamma"]
    for key in ("stars", "pushed", "name"):
        assert f'data-sort="{key}"' in html
    assert 'hx-target="#run-table"' in html
    assert 'hx-push-url="false"' in html
    assert f'hx-get="/partials/runs/{run_id}/table?sort=stars' in html
    assert 'data-page="1"' in html
    assert 'data-pages="1"' in html


def test_table_fragment_paginates_50_per_page(clean: Engine, tmp_path):
    run_id = seed_bulk_run(clean, 51)
    client = make_client(clean, tmp_path)

    first = client.get(f"/partials/runs/{run_id}/table?sort=stars&dir=desc&page=1")
    second = client.get(f"/partials/runs/{run_id}/table?sort=stars&dir=desc&page=2")

    first_names = table_names(first.text)
    second_names = table_names(second.text)
    assert len(first_names) == 50
    assert first_names[0] == "octo/repo-051"
    assert second_names == ["octo/repo-001"]
    assert 'data-page="1"' in first.text
    assert 'data-pages="2"' in first.text
    assert 'id="page-next"' in first.text
    assert 'id="page-prev"' not in first.text
    assert 'data-page="2"' in second.text
    assert 'id="page-prev"' in second.text
    assert 'id="page-next"' not in second.text


def test_table_fragment_empty_state(clean: Engine, tmp_path):
    run_id = create_run(clean, FILTER, api_version=API_VERSION)
    client = make_client(clean, tmp_path)

    response = client.get(f"/partials/runs/{run_id}/table")

    assert response.status_code == 200
    assert 'id="run-table-empty"' in response.text
    assert "No results" in response.text
    assert 'data-pages="1"' in response.text


def test_table_fragment_badges_and_geo(clean: Engine, tmp_path):
    items = [
        payload_item(101, "octo/alpha", 30, virtuals={"min_commits": 5}),
        payload_item(103, "octo/gamma", 10, archived=True),
    ]
    run_id = seed_run(clean, tmp_path, items=items)
    client = make_client(clean, tmp_path)

    response = client.get(f"/partials/runs/{run_id}/table")

    html = response.text
    assert html.count('data-flag="archived"') == 1
    assert html.count('data-flag="incomplete"') == 1
    assert 'data-confidence="name"' in html
    assert 'data-country="DE"' in html


def test_table_fragment_non_integer_page_is_a_friendly_400(clean: Engine, tmp_path):
    run_id = seed_run(clean, tmp_path)
    client = make_client(clean, tmp_path)

    response = client.get(f"/partials/runs/{run_id}/table", params={"page": "abc"})

    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_param",
        "param": "page",
        "hint": "page must be an integer >= 1",
    }


def test_status_fragment_refreshes_flags_when_the_run_becomes_terminal(clean: Engine, tmp_path):
    run_id = seed_run(clean, tmp_path)
    with clean.begin() as connection:
        connection.execute(
            text("UPDATE runs SET status = 'running' WHERE id = :id"), {"id": run_id}
        )
    client = make_client(clean, tmp_path)

    running = client.get(f"/partials/runs/{run_id}/status")
    assert 'id="run-flags"' not in running.text

    with clean.begin() as connection:
        connection.execute(
            text(
                "UPDATE runs SET status = 'partial', incomplete_shards = 1, finished_at = now() "
                "WHERE id = :id"
            ),
            {"id": run_id},
        )
    terminal = client.get(f"/partials/runs/{run_id}/status")

    assert 'id="run-flags"' in terminal.text
    assert 'data-flag="incomplete"' in terminal.text


def test_table_fragment_unknown_run_is_404(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    response = client.get("/partials/runs/424242/table")

    assert response.status_code == 404


def test_clone_modal_renders_estimate_and_low_disk_warning(clean: Engine, tmp_path, monkeypatch):
    monkeypatch.setattr("serve.runs.free_disk_mb", lambda path: 0.0)
    run_id = seed_run(clean, tmp_path)
    client = make_client(clean, tmp_path)

    response = client.get(f"/runs/{run_id}")

    html = response.text
    assert 'data-repos="1"' in html
    assert 'data-estimated-mb="1.0"' in html
    assert 'id="clone-warnings"' in html
    assert "low disk" in html


def test_clone_estimate_fragment_negotiates_html_for_htmx(clean: Engine, tmp_path, monkeypatch):
    monkeypatch.setattr("serve.runs.free_disk_mb", lambda path: 10_000.0)
    run_id = seed_run(clean, tmp_path, items=ORDER_ITEMS)
    client = make_client(clean, tmp_path)

    response = client.get(
        f"/runs/{run_id}/clone-estimate",
        params={"limit": 2, "mode": "shallow"},
        headers=HX,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="clone-estimate"' in response.text
    assert 'data-repos="2"' in response.text
    assert 'id="clone-modal"' not in response.text


def test_clone_progress_fragment_idle_and_running(clean: Engine, tmp_path):
    run_id = seed_run(clean, tmp_path)
    client = make_client(clean, tmp_path)

    idle = client.get(f"/partials/runs/{run_id}/clone-progress", headers=HX)

    assert idle.status_code == 200
    assert 'id="clone-progress"' in idle.text
    assert 'data-status="done"' in idle.text
    assert "No clone in progress" in idle.text
    assert "every 1s [document.visibilityState === 'visible']" not in idle.text

    with clean.connect() as connection:
        filter_hash = connection.scalar(
            text("SELECT filter_hash FROM runs WHERE id = :id"), {"id": run_id}
        )
    progress_path = tmp_path / "runs" / str(filter_hash) / str(run_id) / "clone-progress.json"
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path.write_text(
        json.dumps(
            {
                "status": "running",
                "total": 5,
                "completed": 2,
                "failed": 1,
                "current": "octo/hello",
                "errors": ["octo/bad: RuntimeError: boom"],
            }
        ),
        encoding="utf-8",
    )
    fresh = make_client(clean, tmp_path)

    running = fresh.get(f"/partials/runs/{run_id}/clone-progress", headers=HX)

    html = running.text
    assert 'data-status="running"' in html
    assert "every 1s [document.visibilityState === 'visible']" in html
    assert f'hx-get="/partials/runs/{run_id}/clone-progress"' in html
    assert 'hx-swap="outerHTML"' in html
    assert "2 / 5" in html
    assert "octo/hello" in html
    assert "octo/bad: RuntimeError: boom" in html


def test_clone_progress_fragment_unknown_run_is_404(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    response = client.get("/partials/runs/424242/clone-progress", headers=HX)

    assert response.status_code == 404


def test_app_js_wires_detail_controls(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    response = client.get("/static/app.js")

    assert response.status_code == 200
    for marker in (
        "clone-limit-input",
        "copy-hash",
        "clone-start",
        "clone-progress",
        "data-open",
        "data-close",
    ):
        assert marker in response.text
