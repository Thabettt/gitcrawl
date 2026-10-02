# Console UX Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the approved UX flow (`design/console-ux-redesign.md`) into reality: a job-first hub with plain-language naming, explained actions/counts/statuses, a System page with the status dot, a restructured New search page, a readable search workspace with a full results page, corpora freezing, and a runnable Library.

**Architecture:** Server-rendered Jinja2 + htmx, no new client framework. New pages follow the existing `register_*` pattern (`serve/system.py`, `serve/corpora.py`, error handlers in `serve/errors.py`); new data is one small `corpora` table (migration `0009`). Templates are restructured in place; a shared `describe_spec()` helper turns filter specs into plain sentences used everywhere. The design doc is normative for layout and copy; this plan is normative for files, routes, and tests.

**Tech Stack:** Python 3.12, FastAPI + Jinja2 + htmx, SQLAlchemy 2 + Alembic, pytest, ruff, black.

**Spec:** `design/console-ux-redesign.md` (normative for copy and structure). Depends on `2026-10-02-graphql-batch-engine.md` and `2026-10-02-console-settings.md` being executed first (System → Limits links to `/settings`; migration numbering below).

## Global Constraints

- Python `>=3.12`; line length 100; ruff (`E,F,I,UP,B`) + black clean; coverage floor `fail_under = 93`.
- DB tests require `TEST_DATABASE_URL` ending in `_test` (or `GITCRAWL_REQUIRE_TEST_DB=1`); use `tests/conftest.py` fixtures.
- Run tests with: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest <path> -q` (Windows PowerShell).
- Migration `0009` (down_revision `0008`) is the corpora table; agent detection renumbers to `0010` (record in the dev log).
- Copy is part of the contract: the exact strings in `design/console-ux-redesign.md` §6 (Copy deck) and §3.1 (Term map) are asserted by tests where listed. Never ship a raw internal name (`partial`, `shards`, `count_parity`, `PEL`, `p95`, `filter hash`) as a visible label.
- Every new GET page must be added to `tests/golden/conftest.py::PATH_PARAMS` and snapshots regenerated (`UPDATE_GOLDEN=1`); re-pin `tests/golden/snapshots/openapi.sha256` after new routes.
- All read paths and forms keep working without JS; htmx is enhancement only.
- All commits: conventional prefixes (`feat:`, `fix:`, `test:`, `docs:`).

---

## File Structure

**Create**

- `src/serve/errors.py` — HTML 404/500 handlers + CSRF failure page helper.
- `src/serve/system.py` — `/system` page and `/partials/status-dot`.
- `src/serve/corpora.py` — corpora list/detail/freeze/delete routes.
- `src/serve/templates/{404,500,system,results,corpora,corpus_detail}.html`
- `src/serve/templates/partials/{find_matches,results_table,status_dot}.html`
- `migrations/versions/0009_corpora.py`
- Tests: `tests/contract/test_error_pages.py`, `test_system_page.py`, `test_find_matches.py`, `test_results_page.py`, `test_corpora.py`, `test_library_run.py`

**Modify**

- `src/serve/templates/base.html` — nav (Searches / Corpora / Detections / Library / System), status dot include.
- `src/serve/templates/{dashboard,runs,run_detail,filters,library,diff}.html` — naming, structure, explanations per the design doc.
- `src/serve/templates/partials/{status,table,quality}.html` — plain statuses, preview/full table split, explained quality checks.
- `src/serve/pages.py` — labels, run-detail context, results route, matches partial, shared table helper.
- `src/serve/filter_spec.py` — `describe_spec(spec) -> str` plain-sentence helper.
- `src/serve/app.py` — register system/corpora/errors; library run-now route; `find_count_factory` injection for tests.
- `src/store/models.py` — `Corpus`.
- `tests/conftest.py` — `corpora` in `FULL_TRUNCATE_TABLES`.
- `tests/integration/test_models_migrations.py` — `corpora` table expectations.
- `tests/golden/conftest.py` + snapshots — new routes.
- `docs/environment.md`, `docs/development-log.md` — paths table and outcomes.

---

## Task 1: Shell, plain-language naming, and error pages

**Files:**
- Create: `src/serve/errors.py`, `src/serve/templates/404.html`, `src/serve/templates/500.html`
- Modify: `src/serve/templates/base.html`, `dashboard.html`, `runs.html`, `partials/status.html`, `partials/table.html`, `src/serve/pages.py` (status labels), `src/serve/app.py` (register error handlers)
- Test: `tests/contract/test_error_pages.py`, `tests/contract/test_pages.py`, `tests/contract/test_console_pages.py`

**Interfaces:**
- Produces: `register_error_pages(application)`; nav labels; plain status strings. Later tasks rely on the nav and the `csrf_error_page` helper.

- [ ] **Step 1: Write the failing tests**

Create `tests/contract/test_error_pages.py` (copy the `schema`/`clean`/`make_client` helpers from `tests/contract/test_pages.py:41-122`):

```python
def test_unknown_page_renders_html_for_browsers(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get("/definitely-not-a-page", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert "Back to Home" in response.text


def test_unknown_page_stays_json_for_api_clients(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get("/definitely-not-a-page", headers={"Accept": "application/json"})
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_nav_uses_plain_names(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    body = client.get("/").text
    for label in ("Searches", "Corpora", "Detections", "Library", "System"):
        assert f">{label}<" in body


def test_run_statuses_are_plain_words(clean, tmp_path, monkeypatch):
    run_id, _hash = seed_run(clean, tmp_path)
    body = healthy_client(clean, tmp_path, monkeypatch).get(f"/runs/{run_id}").text
    assert "Finished" in body
    assert ">partial<" not in body


def test_nav_marks_the_current_section(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    body = client.get("/runs").text
    assert 'href="/runs" aria-current="page"' in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/contract/test_error_pages.py -q`
Expected: FAIL (JSON 404, old nav).

- [ ] **Step 3: Implement `src/serve/errors.py`**

```python
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from serve import pages


def render_error(request: Request, status_code: int, title: str, message: str) -> HTMLResponse:
    return pages._templates.TemplateResponse(
        request,
        f"{status_code}.html",
        {"title": title, "message": message},
        status_code=status_code,
    )


def csrf_error_page(request: Request) -> HTMLResponse:
    return render_error(
        request,
        403,
        "That action expired",
        "For safety, forms time out after a while. Go back and submit again.",
    )


def register_error_pages(application: FastAPI) -> None:
    @application.exception_handler(StarletteHTTPException)
    async def http_exception(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404 and "text/html" in request.headers.get("accept", ""):
            return render_error(
                request, 404, "Page not found", "That page does not exist. Try Home or Searches."
            )
        return JSONResponse({"error": "not_found"}, status_code=exc.status_code)

    @application.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        if "text/html" in request.headers.get("accept", ""):
            return render_error(
                request,
                500,
                "Something broke",
                "The console hit an unexpected error. Nothing was lost — try again.",
            )
        return JSONResponse({"error": "internal_error"}, status_code=500)
```

Create `404.html`/`500.html` extending `base.html` with the message and a “Back to Home” link. Register `register_error_pages(application)` in `create_app` before `register_pages`.

- [ ] **Step 4: Apply the term map**

In `base.html` replace the nav with `Home · Searches · Corpora · Detections · Library · System`. Mark the current section with `aria-current="page"` (compare `request.url.path` against each href; `/runs` is “Searches”). `Detections` renders as a muted, non-clickable `<span title="Coming soon">` until the detection plan ships; `Corpora` links to `/corpora` (Task 6). Replace CSRF failure returns in `app.py`/`pages.py` (`"CSRF"`, `"invalid csrf token"`) with `errors.csrf_error_page(request)`.

In `pages.py` map status strings: `queued→Waiting`, `running→Running`, `done→Finished`, `partial→Finished — incomplete`, `failed→Failed`; use it in `partials/status.html`, `runs.html`, `dashboard.html`. Replace visible counter labels per the Term map (Found / Saved / Passed filters / Unavailable). Update `tests/contract/test_pages.py` and `test_console_pages.py` expectations that assert old strings (`partial`, `Fetched`, `Inserted`).

- [ ] **Step 5: Run tests**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/contract/test_error_pages.py tests/contract/test_pages.py tests/contract/test_console_pages.py -q`
Expected: PASS.

- [ ] **Step 6: Golden refresh**

Add any new GET routes to `PATH_PARAMS` (none new yet), run `$env:UPDATE_GOLDEN='1'` golden suite, re-pin OpenAPI if the route set changed.

- [ ] **Step 7: Commit**

```bash
git add src/serve/errors.py src/serve/templates/404.html src/serve/templates/500.html src/serve/templates/base.html src/serve/templates/dashboard.html src/serve/templates/runs.html src/serve/templates/partials/status.html src/serve/templates/partials/table.html src/serve/pages.py src/serve/app.py tests/contract/test_error_pages.py tests/contract/test_pages.py tests/contract/test_console_pages.py
git commit -m "feat: console shell, plain-language naming, and error pages"
```

---

## Task 2: System page and status dot

**Files:**
- Create: `src/serve/system.py`, `src/serve/templates/system.html`, `src/serve/templates/partials/status_dot.html`
- Modify: `src/serve/pages.py` (extract health snapshot), `src/serve/app.py`, `src/serve/templates/base.html`
- Test: `tests/contract/test_system_page.py`

**Interfaces:**
- Consumes: `pages` health probes, `serve.metrics.metrics_payload` + `LABELS`, the `/settings` page (Limits).
- Produces: `register_system(application, *, engine_factory, health_snapshot, metrics_redis)`; `GET /system`, `GET /partials/status-dot`.

- [ ] **Step 1: Extract the health snapshot**

In `pages.py`, move the inner `health_snapshot` construction (lines ~560-606) into a module-level factory:

```python
def build_health_snapshot(
    engine_factory, redis_ping=None, token_present=None
) -> Callable[[], dict[str, bool]]:
    ...  # exact existing body, unchanged behaviour
```

`register_pages` uses it for `/health`; `app.py` builds one and passes it to both `register_pages` and `register_system`.

- [ ] **Step 2: Write the failing tests**

Create `tests/contract/test_system_page.py` (helpers copied as in Task 1):

```python
def test_system_page_has_three_plain_sections(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get("/system")
    assert response.status_code == 200
    for text in ("Status", "Performance", "Limits"):
        assert text in response.text
    assert "Requests left this hour" in response.text
    assert "/settings" in response.text


def test_status_dot_reports_ok_when_all_probes_pass(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get("/partials/status-dot")
    assert response.status_code == 200
    assert 'aria-label="All systems ready"' in response.text


def test_status_dot_reports_degraded_without_token(clean, tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKENS", raising=False)
    client = make_client(clean, redis_ping=lambda: True, token_present=lambda: False)
    response = client.get("/partials/status-dot")
    assert "Degraded" in response.text
```

- [ ] **Step 3: Implement `src/serve/system.py`**

```python
from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from serve import pages
from serve.metrics import LABELS, metrics_payload

_PLAIN = {
    "search_remaining": "Requests left this hour",
    "incomplete_results_ratio": "Searches that came back incomplete",
    "rate_422": "Rejected filters",
    "rate_403_429": "Throttled responses",
    "p95_latency_ms": "Median response (ms)",
    "shard_coverage": "Search coverage",
    "geo_unmatched_rate": "Owners without a country",
}


def _dot(status: dict[str, bool]) -> tuple[str, str]:
    if not status.get("database", False):
        return "red", "Database is down"
    if not status.get("redis", True) or not status.get("github_token_present", False):
        return "amber", "Degraded — see System"
    return "green", "All systems ready"


def register_system(
    application: FastAPI,
    *,
    engine_factory: Callable,
    health_snapshot: Callable[[], dict[str, bool]],
    metrics_redis: Callable | None = None,
) -> None:
    @application.get("/partials/status-dot", response_class=HTMLResponse)
    def status_dot(request: Request):
        color, label = _dot(health_snapshot())
        return pages._templates.TemplateResponse(
            request, "partials/status_dot.html", {"color": color, "label": label}
        )

    @application.get("/system", response_class=HTMLResponse)
    def system_page(request: Request):
        status = health_snapshot()
        payload = metrics_payload(engine_factory(), redis_client=metrics_redis and metrics_redis())
        return pages._templates.TemplateResponse(
            request,
            "system.html",
            {
                "status": status,
                "metrics": payload,
                "plain": _PLAIN,
                "labels": LABELS,
                "csrf_token": request.state.csrf_token,
            },
        )
```

`system.html` renders the three sections with plain labels and a “Open limits” link to `/settings`. `status_dot.html` renders `<span class="dot dot-{{ color }}" aria-label="{{ label }}" title="{{ label }}">` and base.html includes `<span hx-get="/partials/status-dot" hx-trigger="load, every 30s" hx-swap="outerHTML"></span>` in the header.

- [ ] **Step 4: Register and test**

`app.py`: `register_system(application, engine_factory=engine_for, health_snapshot=health_snapshot, metrics_redis=metrics_redis_client)`.

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/contract/test_system_page.py -q`
Expected: PASS.

- [ ] **Step 5: Golden + commit**

Add `"/system": {}` and `"/partials/status-dot": {}` to `PATH_PARAMS`, regenerate snapshots, re-pin OpenAPI.

```bash
git add src/serve/system.py src/serve/templates/system.html src/serve/templates/partials/status_dot.html src/serve/pages.py src/serve/app.py src/serve/templates/base.html tests/contract/test_system_page.py tests/golden
git commit -m "feat: system page and header status dot"
```

---

## Task 3: New search — Common/Advanced, sticky summary, Check matches

**Files:**
- Create: `src/serve/templates/partials/find_matches.html`
- Modify: `src/serve/templates/filters.html`, `src/serve/pages.py` (matches partial route + count injection), `src/serve/app.py` (`find_count_factory`)
- Test: `tests/contract/test_find_matches.py`, `tests/contract/test_filter_form.py` (update)

**Interfaces:**
- Produces: `GET /partials/find/matches` (htmx fragment; standalone HTML without HX); `create_app(..., find_count_factory: Callable[[], Callable[[str], int]] | None = None)` for tests; all existing form fields keep their names.

- [ ] **Step 1: Write the failing tests**

```python
def test_search_page_has_common_advanced_and_explained_actions(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    body = client.get("/find").text
    assert "Common" in body and "Advanced" in body
    assert "uses your GitHub allowance" in body.lower()
    assert "No GitHub calls." in body


def test_check_matches_returns_count_and_caveat(clean, tmp_path, monkeypatch):
    def factory():
        return lambda query: 4200

    client = healthy_client(clean, tmp_path, monkeypatch, find_count_factory=factory)
    response = client.get("/partials/find/matches?q=language:rust&stars=100")
    assert response.status_code == 200
    assert "~4,200" in response.text
    assert "extra rules" in response.text.lower()


def test_check_matches_without_js_renders_a_page(clean, tmp_path, monkeypatch):
    def factory():
        return lambda query: 12

    client = healthy_client(clean, tmp_path, monkeypatch, find_count_factory=factory)
    response = client.get("/partials/find/matches?q=language:rust")
    assert response.status_code == 200
    assert "<html" in response.text.lower()  # full page for non-htmx clients
```

- [ ] **Step 2: Implement the route**

In `pages.py` `register_pages`, add:

```python
    @app.get("/partials/find/matches", response_class=HTMLResponse)
    def find_matches(request: Request):
        values = _flat_form_like(request.query_params)  # name -> value mapping
        try:
            spec = build_spec_from_form(values)
        except FilterSpecError as exc:
            context = {"errors": exc.errors, "hints": exc.hints}
        else:
            query = spec_to_query(spec)
            try:
                count = count_factory()(query)
                context = {"count": count, "sentence": describe_spec(spec)}
            except Exception:
                context = {"errors": ["Could not check matches right now."], "hints": []}
        template = "partials/find_matches.html"
        if request.headers.get("hx-request") != "true":
            return pages._templates.TemplateResponse(request, "find_matches_full.html", context)
        return pages._templates.TemplateResponse(request, template, context)
```

`count_factory` defaults to `lambda: (lambda q: pipeline.count_total(build_deps(engine_factory()), q))`; tests inject via `create_app(find_count_factory=...)` and `register_pages(..., find_count_factory=...)`.

`find_matches.html` renders the sentence, `~{{ "{:,}".format(count) }}`, and the caveat “Extra rules (files, country) are applied after fetching, so the final corpus will be smaller.” plus the “one search request” line. `find_matches_full.html` is a tiny base-extending page for the no-JS case.

- [ ] **Step 3: Restructure `filters.html`**

- Wrap the advanced groups (Owner, Counts, Dates, Meta, Flags, Custom properties, Virtual filters, Result) in `<details><summary>Advanced</summary>…</details>`; Common = Keywords + In scope, Language, Minimum stars, Updated since, Include forks.
- Add the sticky rail markup: plain summary (`{{ spec_sentence }}` from the route), the Check-matches button (`hx-get="/partials/find/matches" hx-include="closest form" hx-target="#match-result"`), `#match-result`, then Run (primary), Save, Download with the copy deck lines under each.
- Move upload into `<details><summary>Use a filter file</summary>` with its own submit.
- Route context adds `spec_sentence = describe_spec(parse_filter_spec(current spec))` when the form is prefilled; empty otherwise.

- [ ] **Step 4: Add `describe_spec` to `filter_spec.py`**

```python
def describe_spec(spec: FilterSpec) -> str:
    parts = []
    if spec.q:
        parts.append(spec.q.replace("language:", "").title())
    ...  # map stars/pushed/forks/license/topics/virtuals to plain fragments
    return " · ".join(parts) or "Everything"
```

Unit-test it in `tests/unit/test_filter_spec.py` with three representative specs.

Also enforce the `props` coupling server-side here (today the single-`org` rule exists only in the form UI, so uploaded JSON bypasses it). Append to `tests/unit/test_filter_spec.py`:

```python
def test_props_requires_exactly_one_org():
    with pytest.raises(FilterSpecError) as excinfo:
        parse_filter_spec({"gitcrawl_filter": 1, "q": "language:rust props.team:core"})
    assert any("org" in error for error in excinfo.value.errors)


def test_props_with_two_orgs_is_rejected():
    with pytest.raises(FilterSpecError):
        parse_filter_spec({"gitcrawl_filter": 1, "q": "org:a org:b props.team:core"})


def test_props_with_one_org_is_accepted():
    spec = parse_filter_spec({"gitcrawl_filter": 1, "q": "org:acme props.team:core"})
    assert spec.q == "org:acme props.team:core"
```

Implement in `parse_filter_spec` after the `q` allowlist check (adapt variable names to the function):

```python
    tokens = tokenize(query)
    has_props = any(token.startswith("props.") for token in tokens)
    orgs = [token for token in tokens if token.startswith("org:")]
    if has_props and len(orgs) != 1:
        errors.append("`props.` filters require exactly one `org:` in the query")
        hints.append("add a single `org:NAME` or remove the `props.` filter")
```

- [ ] **Step 5: Run the suites**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/contract/test_find_matches.py tests/contract/test_filter_form.py tests/unit/test_filter_spec.py -q`
Expected: PASS (update form-parity assertions if the restructuring moved fields — the emitted spec must stay byte-identical).

- [ ] **Step 6: Commit**

```bash
git add src/serve/templates/filters.html src/serve/templates/partials/find_matches.html src/serve/templates/find_matches_full.html src/serve/pages.py src/serve/filter_spec.py src/serve/app.py tests/contract/test_find_matches.py tests/contract/test_filter_form.py tests/unit/test_filter_spec.py
git commit -m "feat: new search page with common/advanced split and match check"
```

---

## Task 4: View-all results page

**Files:**
- Create: `src/serve/templates/results.html`, `src/serve/templates/partials/results_table.html`
- Modify: `src/serve/templates/partials/table.html` (include shared partial), `src/serve/pages.py` (results route + shared context helper)
- Test: `tests/contract/test_results_page.py`

**Interfaces:**
- Produces: `GET /runs/{run_id}/results?page=&per_page=&sort=&dir=`; allowed `per_page` {25, 50, 100, 200} (default 50), `sort` {stars, pushed, name}, `dir` {asc, desc}; invalid values render the page with an inline hint (HTTP 400); URL carries all state.

- [ ] **Step 1: Write the failing tests**

```python
def test_results_page_renders_with_page_size_selector(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    body = healthy_client(clean, tmp_path, monkeypatch).get(f"/runs/{run_id}/results").text
    assert "Per page" in body
    for size in ("25", "50", "100", "200"):
        assert f">{size}<" in body
    assert "View all results" not in body  # this IS the full view


def test_results_page_invalid_page_renders_hint(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    response = healthy_client(clean, tmp_path, monkeypatch).get(
        f"/runs/{run_id}/results?page=abc"
    )
    assert response.status_code == 400
    assert "whole number" in response.text
```

- [ ] **Step 2: Implement**

Extract the existing run-table query/sort/pagination logic from the `/partials/runs/{id}/table` handler into a shared `results_context(engine, run_id, *, page, per_page, sort, dir)` used by both routes. Create `partials/results_table.html` with the full columns; `partials/table.html` becomes the compact preview (5 columns) that includes `results_table.html` with `compact=true`. `/runs/{id}/results` renders `results.html` (header with the plain search sentence, action row, filter row placeholders for file/country/language, table, pagination, per-page selector). Change the partial-table handler’s invalid-page response from JSON 400 to the rendered hint so htmx swaps show it (update the existing test that asserts JSON).

- [ ] **Step 3: Run tests, golden, commit**

Add `"/runs/{run_id}/results": {"run_id": RUN_A}` to `PATH_PARAMS` (needs seeded run items — the golden seed already has them). Regenerate snapshots, re-pin OpenAPI.

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/contract/test_results_page.py tests/contract/test_pages.py -q`

```bash
git add src/serve/templates/results.html src/serve/templates/partials/results_table.html src/serve/templates/partials/table.html src/serve/pages.py tests/contract/test_results_page.py tests/golden
git commit -m "feat: full-width results page with page-size selector"
```

---

## Task 5: Search workspace

**Files:**
- Modify: `src/serve/templates/run_detail.html`, `src/serve/templates/partials/status.html`, `src/serve/templates/partials/quality.html`, `src/serve/templates/partials/clone_modal.html`, `src/serve/pages.py` (run-detail context), `src/serve/quality.py` (sentence labels), `src/serve/runs.py` (run-scoped export helper), `src/serve/app.py` (run-scoped export route)
- Test: `tests/contract/test_run_detail.py` (update + new)

**Interfaces:**
- Consumes: `describe_spec`, `results` route.
- Produces: run detail context keys `sentence`, `counts` (`found/saved/passed/with_file/unavailable` with `label` and `explain` strings), `preview_page_size = 20`, `clone_root`, action gating; `GET /runs/{run_id}/export?format=json|csv` (exports exactly the viewed run; the hash-scoped route stays for API clients); `serve.runs.export_run(engine, run_id, format, runs_root)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_run_detail_shows_filter_sentence_and_explained_counts(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    body = healthy_client(clean, tmp_path, monkeypatch).get(f"/runs/{run_id}").text
    assert "Rust" in body
    for text in ("Repos GitHub said matched your search.",
                 "We fetched each repo’s current details.",
                 "Deleted or private by the time we looked."):
        assert text in body
    assert "View all results" in body
    assert "Detect agent use" in body and "disabled" in body


def test_quality_panel_uses_sentences(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    body = healthy_client(clean, tmp_path, monkeypatch).get(
        f"/partials/runs/{run_id}/quality"
    ).text
    assert "count_parity" not in body
    assert "counts match" in body.lower()


def test_export_links_point_at_this_run(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    body = healthy_client(clean, tmp_path, monkeypatch).get(f"/runs/{run_id}").text
    assert f'href="/runs/{run_id}/export?format=json"' in body


def test_run_scoped_export_downloads_this_run(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    response = healthy_client(clean, tmp_path, monkeypatch).get(
        f"/runs/{run_id}/export?format=json"
    )
    assert response.status_code == 200
    assert response.json()["run_id"] == run_id


def test_clone_modal_shows_destination_and_tradeoffs(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    body = healthy_client(clean, tmp_path, monkeypatch).get(f"/runs/{run_id}").text
    assert "Destination:" in body
    assert "latest snapshot only" in body
```

- [ ] **Step 2: Implement**

- Header: h1 “Search #{{ id }} — {{ sentence }}”; status pill via plain words; “Finished · observation time {{ finished_at }}”.
- Action row per state: Detect (disabled with title/adjacent line from the copy deck), Export results, Clone code…, Compare with… (Task 8 adds the select), Search again, Resume (failed only). Back link to `/runs` labelled “All searches”.
- Side panel: counters from `counts` with permanent explanation lines; “With a Dockerfile” counted only when the stored filter requested it:

```sql
SELECT count(*) FROM run_items
WHERE run_id = :run_id AND virtuals->>'has_dockerfile' = 'true'
```

- `<details><summary>What this search asked for</summary>` shows the sentence and the raw `q`; `<details>Technical details</details>` keeps the old counters.
- Quality partial: map check names to sentences (`count_parity` → “Result counts match what was stored”, `duplicate_full_names` → “No duplicate repository names”, `missing_*` → “X% missing <field>”, `bundle` → “Export bundle readable”, `incomplete` → “Data completeness”); raw names move under Technical details. Implement the mapping in `quality.py` as `SENTENCES: dict[str, str]`.
- **Export the viewed search (data-integrity fix).** Extract the existing export serialization from `app.py` `/vsearch/runs/{hash}/export` into `serve/runs.py::export_run(engine, run_id, format, runs_root)`; add `GET /runs/{run_id}/export?format=json|csv` that exports exactly that run, and point the run-detail Export links at it. The hash-scoped route stays for API clients; the regression test above proves the viewed run is the one downloaded.
- **Clone modal copy.** Pass `clone_root` into the run-detail context; `partials/clone_modal.html` shows `Destination: {{ clone_root }}/<hash>` and one trade-off line per mode: shallow → “latest snapshot only”; file-only → “no history, smallest disk”; windowed → “recent history, largest disk”.

- [ ] **Step 3: Run tests, golden, commit**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/contract/test_run_detail.py tests/contract/test_quality_pages.py -q` (update old label assertions; never weaken behavior assertions). Regenerate goldens if run-detail HTML changed.

```bash
git add src/serve/templates/run_detail.html src/serve/templates/partials/status.html src/serve/templates/partials/quality.html src/serve/templates/partials/clone_modal.html src/serve/pages.py src/serve/quality.py src/serve/runs.py src/serve/app.py tests/contract/test_run_detail.py tests/contract/test_quality_pages.py tests/golden
git commit -m "feat: readable search workspace with explained counts"
```

---

## Task 6: Corpora — freeze, list, detail

**Files:**
- Create: `migrations/versions/0009_corpora.py`, `src/serve/corpora.py`, `src/serve/templates/corpora.html`, `src/serve/templates/corpus_detail.html`
- Modify: `src/store/models.py`, `src/serve/app.py`, `src/serve/templates/run_detail.html` (Freeze action), `tests/conftest.py`, `tests/integration/test_models_migrations.py`
- Test: `tests/contract/test_corpora.py`

**Interfaces:**
- Produces: `Corpus` model (`id`, `name` unique, `source_run_id` FK, `note`, `repo_count`, `frozen_at`); `GET /corpora`, `GET /corpora/{id}`, `POST /runs/{run_id}/corpus`, `POST /corpora/{id}/delete`; `register_corpora(application, *, engine_factory)`.

- [ ] **Step 1: Migration + model + tests**

```python
def upgrade() -> None:
    op.create_table(
        "corpora",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source_run_id", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("repo_count", sa.Integer(), nullable=False),
        sa.Column(
            "frozen_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["source_run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
```

Add `"corpora"` to `FULL_TRUNCATE_TABLES` and to `TABLES`/`EXPECTED_COLUMNS` in the migrations test.

- [ ] **Step 2: Contract tests**

```python
def test_freeze_creates_corpus_and_redirects(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    client = healthy_client(clean, tmp_path, monkeypatch)
    token = csrf_token(client)
    response = client.post(
        f"/runs/{run_id}/corpus",
        data={"name": "rust-2026-10", "note": "thesis frame"},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/corpora/")


def test_freeze_requires_a_finished_search(clean, tmp_path, monkeypatch):
    run_id = create_run(clean, {"gitcrawl_filter": 1, "q": "language:rust"}, api_version=API_VERSION)
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.post(
        f"/runs/{run_id}/corpus",
        data={"name": "x"},
        headers={"x-csrf-token": csrf_token(client)},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "finished" in response.text.lower()


def test_corpora_page_explains_what_a_corpus_is(clean, tmp_path, monkeypatch):
    body = healthy_client(clean, tmp_path, monkeypatch).get("/corpora").text
    assert "frozen snapshot of a finished search" in body
```

- [ ] **Step 3: Implement routes + templates**

`corpora.py` mirrors `settings.py` (CSRF via `pages.validate_csrf`, threadpool DB work, 303s, plain 400 hints). Freeze computes `repo_count` with `SELECT count(*) FROM run_items WHERE run_id=:id`; only `status in ('done','partial')`. Templates carry the copy deck lines for freezing and frozen semantics; greyed Detect buttons. Add the **Freeze as corpus…** action to run detail (form POST with name prompt in the existing modal pattern) and add Corpora to the nav link target (already in nav from Task 1).

- [ ] **Step 4: Run, golden, commit**

Add `"/corpora": {}` and `"/corpora/{corpus_id}": {"corpus_id": <seeded id>}` to `PATH_PARAMS` (seed one corpus in the golden SQL). Run the corpora + migrations suites; regenerate goldens; re-pin OpenAPI.

```bash
git add migrations/versions/0009_corpora.py src/store/models.py src/serve/corpora.py src/serve/templates/corpora.html src/serve/templates/corpus_detail.html src/serve/templates/run_detail.html src/serve/app.py tests/conftest.py tests/integration/test_models_migrations.py tests/contract/test_corpora.py tests/golden
git commit -m "feat: corpora freezing, list, and detail pages"
```

---

## Task 7: Library — run now and readable rows

**Files:**
- Modify: `src/serve/templates/library.html`, `src/serve/app.py` (`POST /filters/{id}/run`), `src/serve/pages.py` (renderer context)
- Test: `tests/contract/test_library_run.py`

**Interfaces:**
- Produces: `POST /filters/{id}/run` (CSRF; creates a run from the stored spec, enqueues, 303 to the run); library rows show `describe_spec` + last-used run; dialogs for rename/delete.

- [ ] **Step 1: Write the failing test**

```python
def test_library_run_now_creates_a_run_and_redirects(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    token = csrf_token(client)
    client.post(
        "/filters",
        data={"name": "rust picks", "gitcrawl_filter": "1", "q": "language:rust"},
        headers={"x-csrf-token": token},
    )
    filter_id = ...  # query saved_filters
    response = client.post(
        f"/filters/{filter_id}/run",
        headers={"x-csrf-token": csrf_token(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/runs/")


def test_library_row_shows_plain_sentence_and_explains_run_now(clean, tmp_path, monkeypatch):
    body = healthy_client(clean, tmp_path, monkeypatch).get("/filters").text
    assert "recipe" in body
    assert "Run now" in body
    assert "Searches and Corpora are untouched" in body
```

- [ ] **Step 2: Implement**

The run route reuses the `POST /find` creation path: parse the stored `filter_spec`, `create_run`, `executor_for().submit(run_id, runner_for())`, 303. The library renderer adds `sentence = describe_spec(parse_filter_spec(row["filter_spec"]))` and last-run info (latest run by `filter_hash`). Template: sentence column, actions Run now / Open in editor / Rename / Delete with the copy deck lines; delete uses the existing modal confirmation (replace `window.confirm`).

- [ ] **Step 3: Run, golden, commit**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/contract/test_library_run.py tests/integration/test_library.py -q`

```bash
git add src/serve/templates/library.html src/serve/app.py src/serve/pages.py tests/contract/test_library_run.py
git commit -m "feat: library run-now and readable saved-filter rows"
```

---

## Task 8: Remaining translations, empty states, verification

**Files:**
- Modify: `src/serve/templates/diff.html`, `run_detail.html` (Compare with), `partials/table.html`, `runs.html`, `dashboard.html`, `src/serve/pages.py`, `docs/environment.md`, `docs/development-log.md`
- Test: full suite

**Interfaces:**
- Produces: diff discoverability, explanatory empty states, documented routes, verified branch.

- [ ] **Step 1: Compare with…**

Add to run detail a small GET form: `<select name="against">` of the previous three same-hash searches + “Compare” → `/runs/{id}/diff`. `diff.html` header becomes “Compare search #A with #B”; summary words “new repos / gone / changed”; raw tables unchanged.

- [ ] **Step 2: Empty states**

Give each list an explanatory empty state with a next action: Searches (“No searches yet — start one”), Corpora (“Freeze a finished search to make a corpus”), Library (already), results table (“No repos passed your filters — try removing the file or country rule”), dashboard recent (“Nothing yet — start your first search”).

- [ ] **Step 3: Docs**

`docs/environment.md` path table gains `/system`, `/runs/{id}/results`, `/corpora`, `/settings` (Limits). Append the outcome to `docs/development-log.md`: naming model, corpora migration `0009` (detection → `0010`), coverage, test counts.

- [ ] **Step 4: Full verification**

Run: `$env:PYTHONPATH='src'; $env:GITCRAWL_REQUIRE_TEST_DB='1'; .\.venv\Scripts\python.exe -m pytest --cov=src --cov-report=term-missing -q`
Expected: all pass; coverage `>= 93%`.

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m ruff check src tests; .\.venv\Scripts\python.exe -m black --check src tests`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add src/serve/templates/diff.html src/serve/templates/run_detail.html src/serve/templates/partials/table.html src/serve/templates/runs.html src/serve/templates/dashboard.html src/serve/pages.py docs/environment.md docs/development-log.md
git commit -m "docs: log the console ux redesign outcome"
```

---

## Self-Review Checklist (run after implementation)

- [ ] Nav and term map match `design/console-ux-redesign.md` §3; no raw internal label is visible (Task 1 tests + copy grep).
- [ ] Every action button carries its cost line (search page, workspace, library, corpus freeze).
- [ ] Every counter and status carries its explanation (workspace, dashboard, progress).
- [ ] System page + status dot live and linked; `/settings` is System → Limits with plain labels.
- [ ] New search keeps all filters on one page, Common/Advanced split, Check matches spends one search request and states its limits.
- [ ] Workspace: filter sentence, preview + View all, action gating, greyed Detect.
- [ ] View-all: URL state, per-page selector, HTML hints for invalid params.
- [ ] Corpora: freeze → list → detail, frozen semantics explained, migration `0009`.
- [ ] Library: Run now creates a real run; delete semantics explained.
- [ ] No orphan pages; custom 404/500; CSRF failures explained.
- [ ] Full suite green, coverage ≥ 93, goldens re-pinned, dev log updated.
