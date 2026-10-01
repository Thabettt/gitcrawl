# Console Spec (US4) — gitcrawl Operator Console

**Status**: Approved-for-overnight v1 (scope approved in conversation 2026-10-01; written review deferred per operator's overnight instruction). Parents: `spec.md` (Frozen v2), `tasks.md` (T019–T037, T051), `contracts/search-api.md`, `data-model.md`. Plan of record: `console-plan.md`.

## Goal

A local single-operator web console for gitcrawl: run live Finds, watch progress, explore/sort/export results, browse run history, diff runs, manage saved filters, optionally clone top-N repos. One FastAPI process, Jinja2 + htmx + one local CSS file — **no Node/toolchain at runtime**.

## Scope

- Completes **US2** (within-run lifecycle: hydrate + ETag, 301 follow, 404 tombstone) and **US3** (enrichment + serve API + filter-spec + runs/replay/export + form + cost planner + clone) per the frozen contract.
- Adds **US4**: run persistence (`runs`, `run_items`, `saved_filters`), console pages, and operator extras (history browser, run-to-run diff, saved filter library, keyboard-first nav, dark mode).
- **Run history is operator-triggered artifacts, not a background index** — the live-only freshness lock stands (no schedulers, no polling of GitHub between runs).
- Non-goals: auth/multi-user, public hosting/ops, SPA-level animation, thesis tracks.

## Backend additions

### Data model (migration `0003_console.py`, down_revision `"0002"` — ruling R38)

```sql
CREATE TABLE runs (
  id            BIGSERIAL PRIMARY KEY,
  filter_hash   TEXT NOT NULL,              -- sha256 of canonical filter-spec
  filter_spec   JSONB NOT NULL,             -- full validated filter-spec v1 (+frame if any)
  status        TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|failed|partial
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at    TIMESTAMPTZ, finished_at TIMESTAMPTZ,
  api_version   TEXT NOT NULL,
  total_count   INTEGER, fetched INTEGER NOT NULL DEFAULT 0,
  inserted INTEGER NOT NULL DEFAULT 0, updated INTEGER NOT NULL DEFAULT 0,
  unchanged INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
  incomplete_shards INTEGER NOT NULL DEFAULT 0,
  error         TEXT,                        -- sanitized message, never an upstream body dump
  bundle_dir    TEXT                         -- runs/{filter_hash}/{run_id}/
);
CREATE INDEX runs_created_idx ON runs (created_at DESC);
CREATE INDEX runs_filter_hash_idx ON runs (filter_hash, created_at DESC);

CREATE TABLE run_items (
  run_id        BIGINT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  repo_id       BIGINT NOT NULL REFERENCES repos(id),
  full_name     CITEXT NOT NULL,
  stargazers    INTEGER, pushed_at TIMESTAMPTZ, archived BOOLEAN,
  language      TEXT, license_spdx TEXT,
  country_iso   CHAR(2), geo_confidence TEXT,
  virtuals      JSONB NOT NULL DEFAULT '{}', -- badge flags (has_dockerfile, team_topic, ...)
  PRIMARY KEY (run_id, repo_id)
);
CREATE INDEX run_items_repo_idx ON run_items (repo_id);
CREATE INDEX run_items_stars_idx ON run_items (run_id, stargazers DESC);

CREATE TABLE saved_filters (
  id            BIGSERIAL PRIMARY KEY,
  name          TEXT NOT NULL UNIQUE,
  filter_spec   JSONB NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### Run executor

- Single background worker (in-process thread), FIFO queue; one Find at a time; `POST /find` enqueues and redirects.
- Lifecycle: `queued` → `running` → (`done` | `partial` when any shard/page incomplete | `failed`).
- Uses the existing pipeline (`run_search_discovery` / `run_since_scan` / `run_org_enum`) for discovery, then hydration/enrichment/virtual filters for the result set, then snapshots `run_items` and writes the bundle (`bundle.json` + `corpus.csv`) under `runs/{filter_hash}/{run_id}/`.
- Audit rows continue to be written per response (FR-010).

### Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | DB/Redis/token-present badges (never values) |
| GET | `/` | Dashboard: recent runs, health, quick find |
| GET | `/find` | Filter form (all 06-matrix groups + virtuals; `props.*` enabled only with a single `org:`) |
| POST | `/find` | Validates, creates a run, enqueues, 303 → `/runs/{id}` |
| GET | `/vsearch/repos` | Frozen JSON contract (`contracts/search-api.md`) — synchronous/small queries |
| POST | `/vsearch/run` | Filter-spec JSON upload (contract) |
| GET | `/vsearch/runs/{filter_hash}` | Replay contract (latest run for hash) |
| GET | `/runs` | History list (filters: status, hash; paginated) |
| GET | `/runs/{id}` | Run detail page |
| GET | `/runs/{id}/export?format=json\|csv` | Bundle download |
| POST | `/runs/{id}/replay` | New run from stored filter-spec |
| GET | `/runs/{id}/diff?against={id2}` | Diff page |
| GET | `/api/runs/{id}/diff?against={id2}` | Diff JSON (`added`, `removed`, `changed[{field,from,to}]`) |
| GET | `/filters` | Saved filter library |
| POST | `/filters` / POST `/filters/{id}/delete` | Save / delete (rename optional) |
| GET | `/partials/runs/{id}/status` | htmx poll: status banner + counts (stops at terminal state) |
| GET | `/partials/runs/{id}/table?sort=&dir=&page=` | htmx results table fragment |
| GET | `/runs/{id}/clone-estimate?limit=&mode=` | disk estimate preview (R54) |
| POST | `/runs/{id}/clone` / GET `/partials/runs/{id}/clone-progress` | clone start / progress (R54) |

## UX requirements

- **Dashboard**: recent runs table (id, hash short, status pill, counts, duration, relative time), health badges, quick filter box.
- **Filter form**: every `06 §2` qualifier group with comparator/range widgets; virtual section (`min_stars`, `team_topic`, `has_dockerfile`, `min_commits`, `min_loc`, `owner_country` + confidence threshold); `props.*` gated on single `org:`; live client-side typo hints mirrored from `lib/qualify.py`; "Download as JSON" button producing the exact filter-spec the form would POST.
- **Run detail**: status banner auto-polls via htmx until terminal; results table sortable (stars, pushed, name) and paginated 50/page via htmx fragments; badges for incomplete/truncation; per-row provenance tooltip; owner country + confidence; export/replay/clone actions.
- **Clone control**: modal with slider + numeric input (top-N by current sort), mode radio (shallow / file-only / windowed), disk estimate with low-disk warning, resumable progress view.
- **Diff**: select a second run for the same filter hash; table of added/removed/changed (stars, pushed_at, archived, status flags) with counts summary.
- **Saved filters**: list with name, hash, last run; one-click load into the form; save-from-form; delete with confirmation.
- **Keyboard-first**: `/` focuses search, `g h` home, `g f` find, `g r` runs, `j/k` row navigation, `Enter` open, `?` shortcut help modal.
- **Dark mode**: `prefers-color-scheme` default + toggle persisted in `localStorage`; no flash-of-wrong-theme (inline script).
- **States everywhere**: loading (htmx indicator), empty (explanatory), error (actionable), disabled (during active runs).

## Error handling

- `400` invalid params render inline with the qualifier hint (never a raw traceback); unknown params never forwarded upstream.
- `502` upstream throttling/outage: banner with retry-after; failed run keeps partial results and a sanitized `error`.
- Incomplete/truncated results are always labeled, never silent; `total_count` displayed as approximate.
- Template-level: every page works with JS disabled for its core read paths (forms still POST).

## Security & ops

- Binds `127.0.0.1:8000` only. No auth (local single-operator) — documented.
- Minimal CSRF protection on POSTs (double-submit token cookie + hidden field).
- Secrets never rendered; `/health` shows only present/absent.
- Start: `.venv\Scripts\python.exe -m uvicorn serve.app:app --host 127.0.0.1 --port 8000` from a shell with `PYTHONPATH=src`, plus a `src/serve/__main__.py` convenience (`PYTHONPATH=src .venv\Scripts\python.exe -m serve`).
- htmx, Tailwind CSS output, and the small JS file are **vendored locally** (`src/serve/static/`), committed; no CDN at runtime.

## Testing

- Contract tests for every route (status codes, validation hints, allowlist, export headers/bodies, diff JSON shape, saved-filter CRUD, CSRF reject).
- Template tests (TestClient): key elements/ids present per page, empty/error states render, form produces the exact filter-spec (form ≡ JSON parity test).
- Executor tests: queued→running→done/partial/failed with MockTransport + TEST DB; snapshot rows; bundle files written; single-worker FIFO.
- Diff unit/integration tests on fixture runs.
- Existing live tests stay; new UI tests are offline. Optional token-guarded live smoke for `POST /find` end-to-end.

## Success criteria

- **SC-005**: From the documented start command, an operator can submit a Find in a browser and see live results with export downloads that parse as valid JSON/CSV.
- **SC-006**: History and diff correctly show added/removed/changed repos on fixture runs.
- **SC-007**: Invalid filters render local hints; zero unknown params reach upstream.
- **SC-008**: No Node/toolchain at runtime; keyboard navigation and dark mode work; every page's read path degrades gracefully without JS.
