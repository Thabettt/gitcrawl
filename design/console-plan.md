# gitcrawl Console Implementation Plan (overnight run)

> **For the executor (me):** execute the batch queue below with subagent-driven development — fresh implementer per batch, task review, fix loop, commit, ledger. Briefs quote exact task text/interfaces. Do not stop between batches; the operator is asleep and instructed a continuous run.

**Goal:** Complete US2 + US3 (backend) and add the US4 console (run persistence, operator extras, server-rendered UI) so gitcrawl is a startable local application.

**Architecture:** Live-only engine (already built through US1) + hydration/enrichment/geo layers + FastAPI serve layer (JSON contract + server-rendered Jinja2/htmx pages) + single-worker run executor with Postgres persistence.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, htmx (vendored), Tailwind CSS compiled once via standalone CLI (vendored output), PostgreSQL 18 (:5433), Redis (WSL), pytest.

**Spec:** `design/console-spec.md` (US4) + `design/spec.md` (v2) + `design/tasks.md` (T019–T037, T051) + `design/contracts/search-api.md`.

## Global Constraints

- Live-only: no background crawl/polling/freshness anywhere; runs are operator-triggered.
- Env-only tokens; fingerprint-only logging; never render or log raw tokens.
- Unknown params never forwarded upstream; client-side allowlist + hints (`lib/qualify.py`).
- Store PK immutable `id`; tombstones never hard deletes; run history is operator artifacts only.
- Tests-first; 100% branch coverage target; ruff + black clean; no code comments.
- No Node/toolchain at runtime; all front-end assets vendored under `src/serve/static/` and committed.
- Server binds `127.0.0.1`; CSRF double-submit on POSTs; `/health` shows present/absent only.

## Batch queue (execute in order)

| Batch | Tasks | Deliverable |
|---|---|---|
| B1 | T019, T020, T021, T022, T023 | US2: within-run dedupe tests, lifecycle test, refresh driver, hydrate client (ETag/301/404), lifecycle handler |
| B2 | T026, T028, T029 | trees-first enrichment (+equivalence test), GraphQL batch (funding/discussions/sponsors) |
| B3 | T031, T030, T027 | mirrors (ecosyste.ms/deps.dev/Scorecard), geo resolver (+fixture tests) |
| B4 | T052, T053 | runs/run_items/saved_filters models + migration 0002; single-worker run executor |
| B5 | T032, T033, T034, T025, T035, T056 | serve JSON API + virtual params, filter-spec validation, runs/replay/export, contract tests, filter form, UI shell (base/static/dashboard/health) |
| B6 | T036, T037, T051 | cost planner, segment executor, clone control (estimate/resume) |
| B7 | T054, T055 | saved-filter CRUD + library backend, run diff computation + JSON endpoint |
| B8 | T057 | run detail UI (polling status, sortable/paginated table, export/replay, clone modal) |
| B9 | T058, T059 | history/diff/library pages, keyboard-first nav, dark mode, polish/a11y/empty-error states |
| B10 | — | final whole-branch review + one fix wave + scoped re-review + docs/log/ledger + tests |

## New task definitions (T052–T059)

### T052 — console persistence models + migration 0002
- Modify: `src/store/models.py` (add `Runs`, `RunItem`, `SavedFilter` per console-spec DDL).
- Create: `migrations/versions/0002_console.py` (reversible; indexes; FKs).
- Test: `tests/integration/test_models_console.py` (tables/columns/indexes per spec; downgrade/upgrade round-trip on the guarded TEST DB).
- Produces: `Runs`, `RunItem`, `SavedFilter` ORM classes.

### T053 — run executor
- Create: `src/serve/executor.py`.
- Interfaces:
  ```python
  class RunExecutor:                     # one FIFO worker, in-process
      def __init__(self, engine, *, runner: Callable[[int], None] | None = None): ...
      def submit(self, run_id: int) -> None
      def wait(self, timeout: float | None = None) -> bool
  def execute_run(engine, run_id: int, *, deps_factory: Callable[[], Deps] | None = None) -> None
  def create_run(engine, filter_spec: dict, *, api_version: str) -> int   # status=queued
  def run_status(engine, run_id: int) -> dict
  ```
- `execute_run`: queued→running→(done|partial|failed); calls the US1 pipeline (search/since/org as the filter-spec dictates), then hydration + enrichment + virtual filters (whatever batches B1–B3 delivered), snapshots `run_items`, writes `runs/{filter_hash}/{run_id}/bundle.json` + `corpus.csv`; sanitized `error` on failure; partial when any incomplete shard.
- Tests: `tests/integration/test_executor.py` with MockTransport + TEST DB (status transitions, FIFO order, snapshot rows, bundle files, failure path, partial path).

### T054 — saved filter library (backend)
- Create: `src/serve/library.py`.
- Interfaces: `list_filters(engine)`, `get_filter(engine, id)`, `create_filter(engine, name, spec)`, `delete_filter(engine, id)`, `rename_filter(engine, id, name)`; name validation non-empty/unique (409 on duplicate).
- Tests: `tests/integration/test_library.py`.

### T055 — run diff
- Create: `src/serve/diff.py`.
- Interfaces:
  ```python
  @dataclass(frozen=True)
  class RunDiff:
      added: tuple[dict, ...]; removed: tuple[dict, ...]
      changed: tuple[dict, ...]            # {repo_id, full_name, field, from, to}
      summary: dict[str, int]
  def diff_runs(engine, run_a: int, run_b: int) -> RunDiff
  ```
- Changed fields: `stargazers`, `pushed_at`, `archived`, `language`, `license_spdx`, `country_iso`.
- Tests: `tests/integration/test_diff.py` on fixture runs.

### T056 — UI shell + dashboard
- Create: `src/serve/templates/{base.html,dashboard.html}`, `src/serve/static/{htmx.min.js,app.css,app.js}` (htmx vendored; Tailwind standalone CLI compiled once into `app.css`, committed), `src/serve/pages.py` (HTML routes), `src/serve/__main__.py` (dev server convenience).
- Modify: `src/serve/app.py` (app factory wiring, static mount, health).
- Tests: `tests/contract/test_pages.py` (dashboard/health render, static assets 200, CSRF cookie set).
- `/health` returns DB/Redis/token-present booleans only.

### T057 — run detail UI
- Create: `src/serve/templates/run_detail.html` + `src/serve/templates/partials/{status.html,table.html,clone_modal.html,clone_progress.html}`; routes in `pages.py`; partial routes `/partials/runs/{id}/status|table|clone-estimate|clone-progress`.
- Table: sort (stars/pushed/name), 50/page, badges for incomplete/truncation, owner country + confidence, export/replay buttons.
- Tests: `tests/contract/test_run_detail.py` (fragments, polling stops at terminal, sort/paging, badges).

### T058 — history/diff/library pages + keyboard + dark mode
- Create: `src/serve/templates/{runs.html,diff.html,filters.html,shortcuts.html}`; wire routes; expand `static/app.js` (keyboard map `/`, `g h`, `g f`, `g r`, `j/k`, `Enter`, `?`; dark-mode toggle + no-flash inline script; htmx error toasts).
- Tests: `tests/contract/test_console_pages.py` (pages render, diff page shows added/removed/changed, library CRUD via forms, shortcut markup present, CSRF enforcement).

### T059 — polish pass
- Empty/loading/error states on every page; a11y labels/roles; form parity test `tests/contract/test_filter_form.py` (form ≡ JSON upload spec); README/start section appended to `docs/environment.md`; final `ruff/black/pytest` sweep including live tests.

## Notes for briefs

- Each batch brief quotes the relevant task text from `design/tasks.md` + this file, the exact interfaces above, tests required, and done-proof.
- Tests use MockTransport/fakeredis/TEST DB; live only where a task already requires it.
- Every batch commits per task ID and updates the ledger at `.superpowers/sdd/tasks/progress.md`.
