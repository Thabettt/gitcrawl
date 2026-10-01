from __future__ import annotations

import re

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from lib.gh_client import API_VERSION
from serve.app import create_app
from serve.executor import RunPayload, RunPayloadItem, create_run, execute_run
from serve.pages import CSRF_COOKIE

FILTER_A = {"gitcrawl_filter": 1, "q": "language:rust"}
FILTER_B = {"gitcrawl_filter": 1, "q": "language:go"}
HTML = {"Accept": "text/html"}
SHORTCUTS = ("slash", "g-h", "g-f", "g-r", "g-l", "j", "k", "enter", "escape", "question")


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
                "INSERT INTO repos (id, node_id, full_name, owner_id, name, visibility) VALUES "
                "(1, 'R_1', 'octo/r1', 1, 'r1', 'public'), "
                "(2, 'R_2', 'octo/r2', 1, 'r2', 'public'), "
                "(3, 'R_3', 'octo/r3', 1, 'r3', 'public')"
            )
        )
    return alembic_engine


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


def seed_run(
    engine: Engine,
    tmp_path,
    *,
    spec: dict | None = None,
    items: list[RunPayloadItem] | None = None,
    incomplete: bool = False,
) -> int:
    run_id = create_run(engine, spec or FILTER_A, api_version=API_VERSION)
    payload_items = items if items is not None else [run_item(1)]

    def runner(_run_id: int, _spec: dict) -> RunPayload:
        return RunPayload(
            total_count=len(payload_items),
            fetched=len(payload_items),
            incomplete=incomplete,
            items=list(payload_items),
        )

    execute_run(engine, run_id, runner=runner, runs_root=str(tmp_path))
    return run_id


def filter_hash_of(engine: Engine, run_id: int) -> str:
    with engine.connect() as connection:
        return str(
            connection.scalar(text("SELECT filter_hash FROM runs WHERE id = :id"), {"id": run_id})
        )


def _forbidden_runner(_run_id: int, _spec: dict) -> RunPayload:
    raise AssertionError("this route must not execute a run")


@pytest.fixture()
def client(clean: Engine, tmp_path) -> TestClient:
    application = create_app(
        engine=clean,
        runs_root=str(tmp_path / "runs"),
        clone_root=str(tmp_path / "clones"),
        runner_factory=lambda _engine: _forbidden_runner,
    )
    return TestClient(application, raise_server_exceptions=False, follow_redirects=False)


def csrf_token(client: TestClient) -> str:
    client.get("/find")
    token = client.cookies.get(CSRF_COOKIE)
    assert token
    return token


def row_html(html: str, row_id: str) -> str:
    match = re.search(rf'<tr id="{row_id}".*?</tr>', html, re.S)
    assert match
    return match.group(0)


def table_html(html: str, table_id: str) -> str:
    match = re.search(rf'<table id="{table_id}".*?</table>', html, re.S)
    assert match
    return match.group(0)


def test_filters_page_negotiates_html_but_keeps_json_default(client: TestClient):
    default = client.get("/filters")
    page = client.get("/filters", headers=HTML)

    assert default.status_code == 200
    assert default.json() == {"filters": []}
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert 'id="library-table"' in page.text
    assert 'id="library-empty"' in page.text


def test_library_page_lists_saved_filters_with_actions(client: TestClient):
    created = client.post("/filters", json={"name": "Rust Finds", "spec": FILTER_A}).json()

    html = client.get("/filters", headers=HTML).text

    assert f'id="saved-filter-{created["id"]}"' in html
    assert "Rust Finds" in html
    assert created["filter_hash"][:8] in html
    assert f'href="/find?filter={created["id"]}"' in html
    assert f'action="/filters/{created["id"]}/rename"' in html
    assert f'action="/filters/{created["id"]}/delete"' in html
    assert html.count('name="csrf"') >= 2
    assert 'data-no-run="true"' in html


def test_library_page_shows_last_run_time(client: TestClient, clean: Engine):
    created = client.post("/filters", json={"name": "Rust Finds", "spec": FILTER_A}).json()
    with clean.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runs (filter_hash, filter_spec, status, api_version, total_count, "
                "fetched, inserted, started_at, finished_at) VALUES (:filter_hash, '{}'::jsonb, "
                "'done', '2022-11-28', 1, 1, 1, now(), now())"
            ),
            {"filter_hash": created["filter_hash"]},
        )

    html = client.get("/filters", headers=HTML).text
    row = row_html(html, f'saved-filter-{created["id"]}')

    assert f'data-filter-hash="{created["filter_hash"]}"' in row
    assert 'data-no-run="true"' not in row
    assert "never" not in row
    assert "just now" in row or "ago" in row


def test_find_save_creates_the_filter_without_running(client: TestClient, clean: Engine):
    token = csrf_token(client)

    response = client.post(
        "/find",
        data={"csrf": token, "action": "save", "name": "  My Rust  ", "keywords": "language:rust"},
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/filters"
    filters = client.get("/filters").json()["filters"]
    assert [item["name"] for item in filters] == ["My Rust"]
    with clean.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM runs")) == 0


def test_find_save_errors_rerender_the_find_form(client: TestClient):
    token = csrf_token(client)

    blank = client.post(
        "/find",
        data={"csrf": token, "action": "save", "name": "  ", "keywords": "language:rust"},
    )
    assert blank.status_code == 400
    assert 'id="filter-errors"' in blank.text
    assert 'value="language:rust"' in blank.text

    client.post(
        "/find",
        data={"csrf": token, "action": "save", "name": "dup", "keywords": "language:rust"},
    )
    duplicate = client.post(
        "/find",
        data={"csrf": token, "action": "save", "name": "dup", "keywords": "language:rust"},
    )

    assert duplicate.status_code == 409
    assert 'id="filter-errors"' in duplicate.text
    assert len(client.get("/filters").json()["filters"]) == 1


def test_library_rename_form_redirects_and_persists(client: TestClient):
    created = client.post("/filters", json={"name": "before", "spec": FILTER_A}).json()
    token = csrf_token(client)

    response = client.post(
        f'/filters/{created["id"]}/rename', data={"csrf": token, "name": "after"}
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/filters"
    assert [item["name"] for item in client.get("/filters").json()["filters"]] == ["after"]
    assert "after" in client.get("/filters", headers=HTML).text


def test_library_rename_form_requires_csrf_and_reports_conflicts(client: TestClient):
    first = client.post("/filters", json={"name": "first", "spec": FILTER_A}).json()
    client.post("/filters", json={"name": "second", "spec": FILTER_B})
    token = client.cookies.get(CSRF_COOKIE)

    missing = client.post(f'/filters/{first["id"]}/rename', data={"name": "third"})
    assert missing.status_code == 403

    conflict = client.post(f'/filters/{first["id"]}/rename', data={"csrf": token, "name": "second"})
    assert conflict.status_code == 409
    assert 'id="library-error"' in conflict.text
    assert [item["name"] for item in client.get("/filters").json()["filters"]] == [
        "first",
        "second",
    ]


def test_library_delete_form_redirects_and_requires_csrf(client: TestClient):
    created = client.post("/filters", json={"name": "gone", "spec": FILTER_A}).json()
    token = client.cookies.get(CSRF_COOKIE)

    missing = client.post(f'/filters/{created["id"]}/delete', data={"other": "x"})
    assert missing.status_code == 403
    assert len(client.get("/filters").json()["filters"]) == 1

    deleted = client.post(f'/filters/{created["id"]}/delete', data={"csrf": token})
    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/filters"
    assert 'id="library-empty"' in client.get("/filters", headers=HTML).text


def test_library_delete_form_has_no_inline_handler(client: TestClient):
    name = 'Rust "quoted" <b>bold</b>'
    created = client.post("/filters", json={"name": name, "spec": FILTER_A}).json()

    html = client.get("/filters", headers=HTML).text

    assert created["id"]
    assert "onsubmit" not in html
    assert "data-delete-filter" in html
    assert "data-name=" in html

    script = client.get("/static/app.js").text
    assert "data-delete-filter" in script
    assert '"submit"' in script or "'submit'" in script


def test_find_page_loads_saved_filter_into_the_form(client: TestClient):
    spec = {
        **FILTER_A,
        "sort": "stars",
        "virtual": {"min_stars": 50},
        "page": {"per_page": 50, "max_pages": 2},
    }
    created = client.post("/filters", json={"name": "Rust", "spec": spec}).json()

    html = client.get("/find", params={"filter": created["id"]}).text

    assert 'value="language:rust"' in html
    assert '<option value="stars" selected' in html
    assert 'name="min_stars" value="50"' in html
    assert 'name="per_page" value="50"' in html

    unknown = client.get("/find", params={"filter": 424242})
    assert unknown.status_code == 404
    assert 'id="filter-errors"' in unknown.text


def test_runs_page_renders_history_rows_and_replay_action(client: TestClient, clean, tmp_path):
    done = seed_run(clean, tmp_path, spec=FILTER_A, items=[run_item(1), run_item(2)])
    partial = seed_run(clean, tmp_path, spec=FILTER_B, items=[run_item(3)], incomplete=True)
    queued = create_run(clean, {"gitcrawl_filter": 1, "q": "language:c"}, api_version=API_VERSION)

    html = client.get("/runs").text

    assert 'id="run-history"' in html
    assert f'id="history-run-{done}"' in html
    assert f'id="history-run-{partial}"' in html
    assert f'href="/runs/{done}"' in html
    assert 'data-status="done"' in html
    assert 'data-status="partial"' in html
    assert 'data-flag="incomplete"' in html
    done_row = row_html(html, f"history-run-{done}")
    queued_row = row_html(html, f"history-run-{queued}")
    assert 'data-count="fetched">2<' in done_row
    assert 'data-count="inserted">2<' in done_row
    assert 'data-field="duration"' in done_row
    assert 'data-replay="true"' in done_row
    assert 'data-replay="true"' not in queued_row


def test_runs_page_filters_by_status_and_hash(client: TestClient, clean, tmp_path):
    done = seed_run(clean, tmp_path, spec=FILTER_A)
    partial = seed_run(clean, tmp_path, spec=FILTER_B, incomplete=True)
    hash_a = filter_hash_of(clean, done)

    by_status = client.get("/runs", params={"status": "partial"}).text
    by_hash = client.get("/runs", params={"hash": hash_a[:6]}).text
    no_match = client.get("/runs", params={"status": "done", "hash": "zzzz"}).text

    assert f'id="history-run-{partial}"' in by_status
    assert f'id="history-run-{done}"' not in by_status
    assert f'id="history-run-{done}"' in by_hash
    assert f'id="history-run-{partial}"' not in by_hash
    assert 'id="runs-empty"' in no_match
    assert 'id="run-history"' in no_match


@pytest.mark.parametrize("value", ["abc", "0", "-1"])
def test_runs_page_non_integer_page_is_a_friendly_400(client: TestClient, value):
    response = client.get("/runs", params={"page": value})

    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_param",
        "param": "page",
        "hint": "page must be an integer >= 1",
    }


def test_runs_page_empty_state(client: TestClient):
    html = client.get("/runs").text

    assert 'id="runs-empty"' in html
    assert "No runs" in html
    assert 'id="run-history"' in html


def test_runs_page_paginates_50_per_page(client: TestClient, clean: Engine):
    for index in range(51):
        create_run(
            clean,
            {"gitcrawl_filter": 1, "q": f"language:x{index}"},
            api_version=API_VERSION,
        )

    first = client.get("/runs").text
    second = client.get("/runs", params={"page": 2}).text

    assert first.count('id="history-run-') == 50
    assert 'data-page="1"' in first
    assert 'data-pages="2"' in first
    assert 'id="runs-page-next"' in first
    assert 'id="runs-page-prev"' not in first
    assert second.count('id="history-run-') == 1
    assert 'data-page="2"' in second
    assert 'id="runs-page-prev"' in second
    assert 'id="runs-page-next"' not in second


def diff_seed(clean: Engine, tmp_path) -> tuple[int, int, int]:
    baseline = seed_run(
        clean, tmp_path, spec=FILTER_A, items=[run_item(1, stargazers=10), run_item(2)]
    )
    viewed = seed_run(
        clean, tmp_path, spec=FILTER_A, items=[run_item(1, stargazers=20), run_item(3)]
    )
    other = seed_run(clean, tmp_path, spec=FILTER_B, items=[run_item(1)])
    return baseline, viewed, other


def test_diff_page_labels_direction_and_same_hash_selector(client: TestClient, clean, tmp_path):
    baseline, viewed, other = diff_seed(clean, tmp_path)

    html = client.get(f"/runs/{viewed}/diff", params={"against": baseline}).text

    assert f'id="diff-viewed-label" data-run-id="{viewed}"' in html
    assert f'id="diff-baseline-label" data-run-id="{baseline}"' in html
    assert "Added in" in html and "not in baseline" in html
    assert "Removed from" in html and "not in viewed" in html
    added = table_html(html, "diff-added")
    removed = table_html(html, "diff-removed")
    assert "octo/r3" in added and "octo/r2" not in added
    assert "octo/r2" in removed and "octo/r3" not in removed
    changed = table_html(html, "diff-changed")
    assert "octo/r1" in changed
    assert "stargazers" in changed
    assert ">10<" in changed and ">20<" in changed
    assert 'data-count="added">1<' in html
    assert 'data-count="removed">1<' in html
    assert 'data-count="changed">1<' in html
    selector = re.search(r'<select id="diff-baseline".*?</select>', html, re.S).group(0)
    assert f'value="{baseline}"' in selector
    assert f'value="{viewed}"' not in selector
    assert f'value="{other}"' not in selector


def test_diff_page_defaults_to_previous_same_hash_run(client: TestClient, clean, tmp_path):
    baseline = seed_run(clean, tmp_path, spec=FILTER_A, items=[run_item(1, stargazers=10)])
    viewed = seed_run(clean, tmp_path, spec=FILTER_A, items=[run_item(1, stargazers=20)])
    other = seed_run(clean, tmp_path, spec=FILTER_B, items=[run_item(1)])

    html = client.get(f"/runs/{viewed}/diff").text

    assert f'id="diff-header" data-run-id="{viewed}" data-against="{baseline}"' in html
    assert f'id="diff-baseline-label" data-run-id="{baseline}"' in html
    assert f'id="diff-baseline-label" data-run-id="{other}"' not in html


def test_diff_page_empty_state(client: TestClient, clean, tmp_path):
    baseline = seed_run(clean, tmp_path, spec=FILTER_A, items=[run_item(1)])
    viewed = seed_run(clean, tmp_path, spec=FILTER_A, items=[run_item(1)])

    html = client.get(f"/runs/{viewed}/diff", params={"against": baseline}).text

    assert 'id="diff-empty"' in html
    assert 'data-count="added">0<' in html
    assert 'id="diff-added"' not in html


def test_diff_page_reports_missing_baseline(client: TestClient, clean, tmp_path):
    only = seed_run(clean, tmp_path, spec=FILTER_A, items=[run_item(1)])

    html = client.get(f"/runs/{only}/diff").text

    assert 'id="diff-no-baseline"' in html
    assert 'id="diff-summary"' not in html


def test_diff_page_renders_null_changed_values(client: TestClient, clean, tmp_path):
    baseline = seed_run(clean, tmp_path, spec=FILTER_A, items=[run_item(1, language=None)])
    viewed = seed_run(clean, tmp_path, spec=FILTER_A, items=[run_item(1, language="Rust")])

    html = client.get(f"/runs/{viewed}/diff", params={"against": baseline}).text
    changed = table_html(html, "diff-changed")

    assert 'data-field="language"' in changed
    assert "—" in changed


def test_diff_page_empty_against_falls_back_to_default_baseline(
    client: TestClient, clean, tmp_path
):
    baseline = seed_run(clean, tmp_path, spec=FILTER_A, items=[run_item(1, stargazers=10)])
    viewed = seed_run(clean, tmp_path, spec=FILTER_A, items=[run_item(1, stargazers=20)])

    response = client.get(f"/runs/{viewed}/diff", params={"against": ""})

    assert response.status_code == 200
    assert f'id="diff-header" data-run-id="{viewed}" data-against="{baseline}"' in response.text
    assert 'id="diff-not-found"' not in response.text


def test_diff_page_empty_against_without_earlier_run_shows_no_baseline(
    client: TestClient, clean, tmp_path
):
    only = seed_run(clean, tmp_path, spec=FILTER_A, items=[run_item(1)])

    response = client.get(f"/runs/{only}/diff", params={"against": ""})

    assert response.status_code == 200
    assert 'id="diff-no-baseline"' in response.text
    assert 'id="diff-not-found"' not in response.text


def test_diff_page_unknown_is_404(client: TestClient, clean, tmp_path):
    baseline = seed_run(clean, tmp_path)

    unknown_path = client.get("/runs/424242/diff")
    unknown_against = client.get(f"/runs/{baseline}/diff", params={"against": 424242})
    malformed = client.get(f"/runs/{baseline}/diff", params={"against": "abc"})

    assert unknown_path.status_code == 404
    assert 'id="diff-not-found"' in unknown_path.text
    assert unknown_against.status_code == 404
    assert 'id="diff-not-found"' in unknown_against.text
    assert malformed.status_code == 404


def test_status_and_clone_fragments_announce_updates(client: TestClient, clean, tmp_path):
    run_id = seed_run(clean, tmp_path)

    status = client.get(f"/partials/runs/{run_id}/status")
    progress = client.get(f"/partials/runs/{run_id}/clone-progress", headers={"HX-Request": "true"})

    assert 'aria-live="polite"' in status.text
    assert 'id="status-loading"' in status.text
    assert 'class="htmx-indicator' in status.text
    assert 'aria-live="polite"' in progress.text
    assert 'id="clone-progress-loading"' in progress.text


def test_run_table_has_a_loading_indicator(client: TestClient, clean, tmp_path):
    run_id = seed_run(clean, tmp_path)

    html = client.get(f"/runs/{run_id}").text

    assert 'id="table-loading"' in html
    assert html.index('id="table-loading"') < html.index('id="run-table"')
    assert 'hx-indicator="#table-loading"' in html


def test_form_controls_have_labels_or_aria_labels(client: TestClient):
    created = client.post("/filters", json={"name": "Rust", "spec": FILTER_A}).json()

    dashboard = client.get("/").text
    find = client.get("/find").text
    runs = client.get("/runs").text
    library = client.get("/filters", headers=HTML).text

    assert 'for="quick-find-q"' in dashboard
    assert 'for="field-keywords"' in find
    assert 'for="field-name"' in find
    assert 'for="run-filter-status"' in runs
    assert 'for="run-filter-hash"' in runs
    assert f'for="rename-{created["id"]}"' in library
    assert 'aria-label="Delete Rust"' in library
    assert 'aria-label="Toggle dark mode"' in dashboard


def test_skip_link_scope_attributes_and_focus_styles(client: TestClient, clean, tmp_path):
    baseline, viewed, _other = diff_seed(clean, tmp_path)

    dashboard = client.get("/").text
    assert 'class="skip-link"' in dashboard
    assert 'href="#content"' in dashboard
    assert 'id="content"' in dashboard

    for html in (
        client.get("/runs").text,
        client.get("/filters", headers=HTML).text,
        client.get(f"/runs/{viewed}/diff", params={"against": baseline}).text,
    ):
        assert 'scope="col"' in html

    css = client.get("/static/app.css").text
    assert ":focus-visible" in css


def test_error_toasts_are_wired(client: TestClient):
    script = client.get("/static/app.js").text

    assert "htmx:responseError" in script
    assert "htmx:sendError" in script
    assert 'id="toast"' in client.get("/").text


def test_app_css_uses_compositor_progress_and_row_containment():
    from pathlib import Path

    css = Path("src/serve/static/app.css").read_text(encoding="utf-8")
    assert "transform: scaleX(var(--progress, 0))" in css
    assert "content-visibility: auto" in css


def test_shortcuts_modal_and_theme_toggle_markup(client: TestClient):
    html = client.get("/").text

    assert 'id="shortcuts-modal"' in html
    assert 'id="shortcuts-title"' in html
    for key in SHORTCUTS:
        assert f'data-shortcut="{key}"' in html
    assert html.count('id="theme-toggle"') == 1
    assert 'aria-label="Toggle dark mode"' in html
    assert "gc-theme" in html
    assert "data-theme" in html

    script = client.get("/static/app.js").text
    for marker in ("shortcuts-modal", "row-selected", "quick-find-q", "keydown", "textarea"):
        assert marker in script
