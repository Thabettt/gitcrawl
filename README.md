# gitcrawl

**Live-only GitHub repository discovery for evidence-backed research corpora.**

gitcrawl takes a filter (language, stars, topics, dates, plus filters GitHub cannot express such as file presence, owner country, and commit counts), runs a *live* search at a chosen moment, follows it to completeness across GitHub's result caps, hydrates and enriches the matches, and freezes the result into a reproducible run bundle — filter spec, metadata, rows, and raw upstream evidence — that can be exported, replayed, compared, and cited.

Research prototype, not production software. CI runs on pushes to `main` and on pull requests:
[![CI](https://github.com/Thabettt/gitcrawl/actions/workflows/ci.yml/badge.svg)](https://github.com/Thabettt/gitcrawl/actions/workflows/ci.yml)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![PostgreSQL 17+](https://img.shields.io/badge/PostgreSQL-17%2B-336791?logo=postgresql&logoColor=white)
![Status: research prototype](https://img.shields.io/badge/status-research%20prototype-orange)

> **Name note:** this project is unrelated to [`openclaw/gitcrawl`](https://github.com/openclaw/gitcrawl), a Go issue/PR triage tool. Same name, different domain — see [Name note](#name-note) at the bottom.

**New here?** Read [The problem](#the-problem) and [Why not just script the GitHub API?](#why-not-just-script-the-github-api) first; they explain why this exists. For the human-readable tour of the pipeline, see [`design/how-the-data-flows.md`](design/how-the-data-flows.md).

## Table of contents

- [The problem](#the-problem)
- [What gitcrawl is](#what-gitcrawl-is)
- [How this relates to existing tools](#how-this-relates-to-existing-tools)
- [Why not just script the GitHub API?](#why-not-just-script-the-github-api)
- [Core concepts](#core-concepts)
- [How it works](#how-it-works)
- [Features](#features)
- [Architecture](#architecture)
- [Data model](#data-model)
- [Run artifacts](#run-artifacts)
- [Installation](#installation)
- [Usage](#usage)
- [JSON API](#json-api)
- [Testing, linting, and type checking](#testing-linting-and-type-checking)
- [Security and legal notes](#security-and-legal-notes)
- [Project status and roadmap](#project-status-and-roadmap)
- [Documentation map](#documentation-map)
- [Name note](#name-note)
- [License](#license)

---

## The problem

GitHub's repository search looks like a simple API call and hides a stack of constraints that break naive crawlers:

- **Only the first 1,000 results of any query are fetchable** (100 per page × 10 pages). Broader queries are not "many pages" — past result 1,000 you get a `422`. Ranking also considers at most ~4,000 matching repos, so query shape changes what you can even see.
- **Search is rate-limited to 30 requests/minute per token**, separate from the 5,000/hour core bucket. Broad discovery is therefore hours of paced calls, not seconds.
- **All filtering lives in one `q` string with a closed qualifier set.** Unknown qualifiers and typos are silently treated as plain text — you get a `200 OK` and wrong results. There is no server-side way to filter by file presence, owner location, or commit count.
- **The search index lags real pushes**, `total_count` is approximate, and `incomplete_results` can be true on a success response.
- **Repos rename, transfer, get deleted, or go private** after you record them; search never tells you which.

A one-off script answers "show me some Rust repos." It cannot answer "give me the complete set of Rust repos matching this frame as of 2026-10-03, with the Dockerfile/owner-country/commit filters applied, and the evidence to defend every row." That second question is what gitcrawl exists for.

## What gitcrawl is

gitcrawl is a **single-operator, live-only discovery engine with an operator console**:

1. You define a **filter spec** in the web console or as JSON.
2. gitcrawl **validates it locally** — qualifier allowlist, typo hints, `props.*` scope rules — and rejects bad input with a local `400` before any GitHub call.
3. It plans **shards** by bisecting `created:` ranges until every shard is under GitHub's 1,000-result cap, and pages each shard with `Link` headers followed verbatim.
4. Every response is **paced through per-resource Redis buckets** and triaged (`retry-after`, reset-wait, backoff, cap-split, fix, fail-loud).
5. Hits are **deduplicated by immutable repo `id`** and upserted into PostgreSQL; rename (`301`) and delete (`404`) are handled as lifecycle events within the run.
6. Matches are **hydrated** (REST with ETag, or aliased GraphQL batches of up to 20 with REST fallback) and then **enriched in cost order** — record-only filters first, zero-call mirrors next, single-call, batched, and deep only for survivors.
7. **Virtual filters** GitHub cannot express are applied locally: file presence, owner country with confidence tiers, commit counts, and more (see [Core concepts](#core-concepts)).
8. The result is frozen as a **run bundle** (`bundle.json` + `corpus.csv`) with per-request audit records and SLO metrics — exportable, replayable by filter hash, diffable against a previous run, and freezable into a named **corpus**.

**Live-only is a design decision, not a limitation.** Each run reflects GitHub as it is at `ran_at`. There are no background pollers, no persistent watermarks, no hot/cold tiers, and no GH Archive tail — so there is no stale index to trust and nothing to keep running between measurements. Historical sources (BigQuery, GH Archive) remain available as *fetch-time* inputs for specific tracks, not as a live tail.

**What it is not:** not a GitHub mirror, not a monitoring service, not a package index, not multi-tenant. It binds to localhost, has no authentication, and assumes one trusted operator.

## How this relates to existing tools

The landscape splits into adjacent families, each good at its slice:

| Family | Examples | What they provide | What they cannot do |
|---|---|---|---|
| Pre-crawled research catalogs | SEART GitHub Search + Data Hub | Queryable database of ~1.9M continuously crawled repos | Fixed columns, 10-star floor, crawl-time freshness (not yours), no file/geo filters, no per-run evidence |
| Offline archives at ecosystem scale | World of Code, GH Archive, BigQuery GitHub data | Raw historical objects/events at enormous scale | Monthly snapshots or raw events, not live filtered selection with today's metadata |
| Analytics dashboards | OSS Insight, DevStats | Aggregated trends and rankings | Aggregate answers, not frozen per-repo corpora |
| Thin API wrappers | `gh search repos`, MCP/assistant connectors | Ergonomic pass-through with JSON output | The same 1,000-result cap, no virtual filters, no persistence, no evidence bundle |

gitcrawl targets the intersection none of them covers: **live measurement at a chosen `ran_at`, arbitrary filters including file presence and owner location, coverage below the 10-star floor, frozen per-run evidence with raw upstream responses, audited pacing, and a path to coding-agent detection.** For a standard off-the-shelf sample, SEART is genuinely the better choice; for ecosystem-scale history, use World of Code. The full honest comparison, including a capability matrix and the case for building rather than adopting, lives in [`design/landscape-comparison.md`](design/landscape-comparison.md) and [`design/gitcrawl-vs-seart.md`](design/gitcrawl-vs-seart.md).

## Why not just script the GitHub API?

Fair question. For a one-off sample, you should:

```bash
gh search repos 'language:rust stars:>100' --limit 100 --json fullName,stargazersCount
```

That is the right tool for "show me some repos." Here is what changes when the output has to be a *complete, defensible corpus*:

| The script | What happens at scale | What gitcrawl adds |
|---|---|---|
| One query, first page(s) | Result 1,001 returns `422`; `total_count` is approximate | Recursive `created:` bisection until every shard is fetchable; cap/incomplete auto-split; coverage counters |
| Straight `requests.get` loop | 30 search req/min; 403/429/422 mean three different things | Per-bucket Redis pacing from response headers; `retry-after`/reset/backoff/cap-split/fix/fail-loud classifier; multi-token pool |
| No file/geo/commit filters server-side | You fetch thousands of repos to check for one file | Cost-ordered enrichment: GraphQL file-presence batches, trees API, owner geo resolver, commit counts — cheap screens first, survivors only |
| `owner/name` keys | Renames and transfers silently fork your data | Immutable `id` primary key, `full_name_history`, `301` follow, `404` tombstones |
| "It worked on my machine Tuesday" | You cannot reproduce last week's sample | Filter-spec hash, frozen bundles with raw upstream evidence, replay, run-to-run diff |
| Crash = start over | Long runs fail at hour three | Orphan recovery, resume (re-fetches from the start, safely), quality report per run |
| No telemetry | You cannot show what you spent or when | Per-request `audit_log` (rate-limit headers, status, latency, token fingerprint) and SLO dashboards |

The honest summary: **a script answers a query; gitcrawl produces a corpus you can defend.** If you only need the query, stay with the script.

## Core concepts

- **Search** — one live discovery run: validated filter → sharded search → hydrate → enrich → frozen results. Shown as runs in the console.
- **Corpus** — a named, immutable snapshot of a finished search. Freezing a run captures its repo set for later reference.
- **Detection** — planned feature: scanning a frozen corpus with the same file machinery (e.g. coding-agent config files). Designed but not implemented; the nav entry is greyed out on purpose.
- **`ran_at`** — the UTC moment a search measured GitHub. Every bundle records it.
- **Filter spec (v1)** — the JSON document that fully describes a search: `q`, `sort`, `order`, `virtual`, `page`, optional `frame`, optional `as_of`. Hashing the canonical spec yields a **filter hash** used for replay and run identity.
- **Virtual filter** — a condition GitHub search cannot express. Some translate to legal GitHub qualifiers (`min_stars` → `stars:>=N`, `team_topic` → `topic:x`); others are enforced locally after hydration/enrichment (`has_dockerfile`, `owner_country`, `min_commits`, …).
- **Run bundle** — the frozen artifact: normalized filter, `ran_at`, counts, item rows, per-field provenance (`field_stats`), and links to the raw upstream JSON captured in the audit log.
- **Geo confidence tiers** — owner country is resolved through flag emoji → gazetteer → alias → cached geocoder → weak signals, and recorded as `exact-iso`, `name`, `gazetteer-city`, `geocoder`, or `weak`; unresolved owners land in an explicit unmatched bucket rather than a forced guess.
- **Incompleteness is surfaced, never hidden** — budget caps, incomplete shards, and unenforceable filters set run warnings and a `partial` status instead of silently returning less.

## How it works

```
filter spec (console form or JSON upload)
  └─ validate locally (qualifier allowlist + typo hints; local 400, no GitHub call)
      └─ count_total (one query)
          └─ shard plan: bisect created: ranges until every shard < 1,000 fetchable
              └─ search shards (per-bucket Redis limiter; cap-422 → split; incomplete → mark)
                  └─ dedupe by immutable id → upsert into PostgreSQL
                      └─ hydrate (REST + ETag / GraphQL aliases ≤ 20, REST fallback)
                          └─ enrich in cost order:
                              record-only → zero-call mirrors → single-call (trees,
                              languages) → batched (GraphQL, deps.dev) → deep (survivors)
                              └─ virtual filters + owner-country geo resolution
                                  └─ sort → run_items snapshot
                                      └─ bundle.json + corpus.csv
                                          (+ audit_log rows, SLO metrics, warnings)
```

Rate-limit handling is a first-class subsystem, not a retry loop:

- Four Redis token buckets: `search` (30/min), `core` (5,000/hr), `code_search` (10/min), `graphql` (5,000 pts/hr), keyed by resource and token fingerprint.
- Response headers are authoritative — `X-RateLimit-*` are reconciled into the buckets; `remaining == 0` pauses only that bucket until reset.
- A classifier maps every failure to one action: `retry_after`, `wait_reset`, `backoff` (jittered, bounded), `shard` (cap-`422`), `fix` (validation-`422`), or `fail_loud` (SSO partial results, bad credentials).
- Without Redis, the runner falls back to `fakeredis` with a warning (`GITCRAWL_REDIS_STRICT=1` forbids that fallback). Unit tests are hermetic via `fakeredis[lua]`.

## Features

**Discovery**
- Sharded `GET /search/repositories` with recursive `created:` bisection, `per_page` up to 100, and verbatim `Link: rel="next"` following.
- Automatic shard splitting on the 1,000-result cap and on `incomplete_results`; every shard records `total_count`, fetched count, and state (`pending/active/done/incomplete`).
- Qualifier allowlist with typo hints; the "delta test" in the suite proves filtering actually narrows results (a silent `200` is never mistaken for filtering).
- Org/user enumeration (`GET /orgs/{org}/repos`, `GET /users/{u}/repos`) implemented and tested (not yet exposed as a console action); `GET /repositories?since=` cursor scanning exists as a quarantined surface.

**Storage and lifecycle**
- `id BIGINT` primary key, `full_name UNIQUE`, `full_name_history` rename log, `deleted_at` tombstones.
- Renames followed via `301`; deletions/privatizations tombstoned on `404` instead of vanishing.
- Batched `INSERT ... ON CONFLICT DO UPDATE` upserts; a COPY-based bulk path exists but is quarantined.

**Hydration and enrichment**
- REST `GET /repos/{full_name}` with ETag/`If-None-Match`; GraphQL aliased batches (≤ 20) with automatic per-repo REST fallback so one bad repo never poisons its neighbors.
- Commit counts from GraphQL `defaultBranchRef.target.history.totalCount` or REST `Link: rel=last`.
- File presence via GraphQL batch first, recursive Git Trees API fallback — never N× `contents` loops.
- Owner-location resolution through an offline gazetteer with confidence tiers, cached per location in `geo_cache`.
- Cost planner orders enrichment record → mirror → single → batched → deep, with per-field source and calls spent recorded in the run payload.

**Virtual filters** (all validated locally with actionable hints)

| Filter | Type | How it is enforced |
|---|---|---|
| `min_stars` | integer ≥ 0 | Translated to `stars:>=N` in the GitHub query |
| `team_topic` | topic slug (`^[a-z0-9][a-z0-9-]{0,49}$`) | Translated to `topic:x` |
| `has_dockerfile` | boolean | File-presence batch (GraphQL → trees fallback) |
| `owner_country` | ISO 3166-1 alpha-2 (e.g. `DE`) | Owner location → geo resolver → confidence tier |
| `min_geo_confidence` | `exact-iso` \| `name` \| `gazetteer-city` \| `geocoder` \| `weak` (default `gazetteer-city`) | Confidence floor for `owner_country` |
| `min_commits` / `max_commits` | integer ≥ 0 | Hydrated default-branch commit count |
| `min_loc` / `max_loc` | integer ≥ 0 | **Recorded only — not enforceable yet**; a run using them is flagged incomplete |

**Operator console** (server-rendered Jinja2 + vendored htmx; no Node or CDN at runtime)
- Dashboard, `/find` filter form (with "download as JSON", upload-and-run, save filter), run history, run detail with live status polling, full-width paginated results, run-to-run **diff**, **corpora** (freeze/list/detail), saved **filter library**, **quality report** per run, **system** page with metrics, **health**, and **settings** for run limits.
- Keyboard navigation (`/`, `g h/f/r/l`, `j`/`k`, `Enter`, `?`, `Esc`), dark mode, CSRF-protected forms, session-independent state (every page/sort/page-size lives in the URL).

**Runs, exports, and reproducibility**
- Filter-spec v1 JSON in, run bundle out: `bundle.json`, `corpus.csv`, persisted run metadata.
- Replay by filter hash (`GET /vsearch/runs/{filter_hash}`), run-scoped export (`GET /runs/{id}/export?format=json|csv`), and compare-with (`GET /api/runs/{id}/diff`).
- Quality report: count parity, duplicates, null ratios, incompleteness, bundle readability, field coverage.
- Audit log: one row per GitHub response (query hash, params, status, rate-limit headers, retry-after, Link, totals, token fingerprint, latency).
- SLO dashboard: limiter state, queue PEL, run counts, incomplete-results ratio, `422/403+429` rates, p95 latency, geo unmatched rate.

**Optional cloning**
- Clone the top-N of any search: `shallow` (`--depth 1`), `file_only` (`--depth 1 --no-checkout`), or `windowed` (`--filter=blob:none --no-checkout`).
- Disk estimate preview, low-disk warning under 2 GiB, progress persisted, completed repos skipped on retry, cancel supported, and a hardened git environment (no prompts, no global config).

**Safety and operational hygiene**
- Localhost binding by default, TrustedHost allowlist, Origin/CSRF middleware, request body caps (1 MiB JSON, 10 MiB uploads).
- Tokens live only in the environment; logs record SHA-256 fingerprints, filter-spec files reject token-like fields, and secrets are never rendered in the UI.
- Orphan recovery on startup (queued/running runs are marked failed and can be resumed), request and clone deadlines, graceful Redis degradation.

## Architecture

Everything runs from the repo source with `PYTHONPATH=src` (implicit namespace packages — there is no installed package or console script). The only runnable entry point is `python -m serve`.

| Path | Responsibility |
|---|---|
| `src/lib/` | GitHub client (`httpx`), token loading/fingerprinting, retry orchestration, generic GraphQL batch engine, deadlines, audit records, query tokenizing/qualifier validation |
| `src/discover/` | Search shard paging, `since` cursor scan (quarantined runner), org/user enumeration, discovery pipeline (`count_total`, shard execution, upserts, audit buffering) |
| `src/limiter/` | Redis Lua token-window buckets and the throttle classifier |
| `src/scheduler/` | `created:` shard planner, shard state machine + Redis Streams queue, survivor-first ordering |
| `src/store/` | SQLAlchemy models, upserts/dedupe, rename history, lifecycle (tombstones), app settings |
| `src/hydrate/` | REST repo hydration (ETag, redirects, commit counts), GraphQL repo adapter, batched refresh driver |
| `src/enrich/` | Geo resolver + gazetteer, trees-first file presence, GraphQL file/owner adapters, cost planner, segment executor, repo cloner |
| `src/serve/` | FastAPI app and middleware, run runner + background executor, filter-spec parsing/hashing, virtual params, console pages/templates/static, exports, clone registry, library, corpora, diff, quality, metrics, settings |
| `migrations/` | Alembic revisions `0001`–`0009` (11 tables) |
| `tests/` | `unit/`, `integration/`, `contract/`, `golden/`, `js/` suites |
| `design/`, `findings/`, `docs/` | Research, frozen specs, plans, environment and legal runbooks (not runtime code) |

Tech stack: Python 3.12+ (PEP 695 syntax is used, so 3.12 is mandatory), FastAPI + Uvicorn, Jinja2 + vendored htmx + hand-rolled CSS, SQLAlchemy 2.x + Alembic, PostgreSQL (with `citext`/`jsonb`/arrays), Redis 7 (or `fakeredis`), `httpx`, Pydantic. Pinned versions are in [`requirements.txt`](requirements.txt) and [`requirements-dev.txt`](requirements-dev.txt).

## Data model

Eleven tables created by nine Alembic revisions:

| Table | Purpose |
|---|---|
| `owners` | Owner identity (login, type) plus enrichment: location raw, `country_iso`, geo confidence, company/blog, ETag |
| `repos` | Repo metadata mirror keyed on immutable `id`: names, description, language, license, topics, flags, counts, `created/pushed/updated`, ETag, `deleted_at` |
| `full_name_history` | Rename/transfer log (`repo_id`, `full_name`, `seen_at`) |
| `geo_cache` | Normalized location → country, confidence, hit count |
| `shards` | Discovery shard state: kind, query, ranges, state, watermark, totals, fetched, incomplete |
| `audit_log` | One row per GitHub response: params, status, rate-limit headers, retry-after, Link, totals, token fingerprint, latency |
| `runs` | One row per search: filter hash + spec, status (`queued/running/done/partial/failed`), timestamps, counters, error, bundle dir |
| `run_items` | Frozen per-run result snapshot (repo id, stars, pushed, flags, geo, virtuals) |
| `saved_filters` | Named saved filter specs |
| `app_settings` | Single-row run limits and toggles (editable at `/settings`, pinnable by env) |
| `corpora` | Named frozen corpora referencing their source run |

## Run artifacts

```
runs/<filter_hash>/<run_id>/
├── bundle.json          # filter, ran_at, api_version, totals, items[], field_stats
├── corpus.csv           # id,full_name,stargazers,pushed_at,archived,language,license_spdx,country_iso,geo_confidence
└── clone-progress.json  # written only while cloning
clones/<filter_hash>/<run_id>/<owner__repo>/   # one dir per clone, with a done marker
```

`field_stats` in the bundle records `per_field_sources`, `calls_spent`, `requeues`, `warnings`, and GraphQL batch statistics, so every enriched field can be traced to how it was obtained. `runs/` and `clones/` are runtime artifacts and git-ignored.

## Installation

### Prerequisites

| Requirement | Notes |
|---|---|
| **Python 3.12+** | Mandatory (PEP 695 syntax). CI uses 3.12. |
| **PostgreSQL 17+** | The schema needs `citext`, `jsonb`, and arrays. `docker-compose.yml` provides 17. |
| **Redis 7** | Strongly recommended. Without it the app degrades to `fakeredis` (fine for tests, not for real pacing). |
| **GitHub token** | Required to crawl. A classic token or a fine-grained PAT with `metadata:read` over public repos is enough. Never commit it. |
| **`git` CLI** | Optional — only for the clone feature. |
| **Node.js 18+** | Optional — only for the JS unit tests (their pytest wrappers skip without it). |
| **Docker** | Optional — only if you use the provided Compose services. |

### Quickstart (Docker Compose)

```bash
git clone https://github.com/Thabettt/gitcrawl.git
cd gitcrawl
docker compose up -d          # PostgreSQL 17 on 5432, Redis 7 on 6379
```

Create and activate a virtual environment:

```powershell
# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1
```

```bash
# macOS / Linux
python -m venv .venv
source .venv/bin/activate
```

Install dependencies (all pinned):

```bash
pip install -r requirements.txt -r requirements-dev.txt
```

Set the required environment (swap in your own token):

```bash
# macOS / Linux
export DATABASE_URL='postgresql+psycopg://gitcrawl:gitcrawl@localhost:5432/gitcrawl'
export REDIS_URL='redis://localhost:6379/0'
export GITHUB_TOKEN='ghp_your_token_here'
```

```powershell
# Windows (PowerShell) — session only
$env:DATABASE_URL = 'postgresql+psycopg://gitcrawl:gitcrawl@localhost:5432/gitcrawl'
$env:REDIS_URL = 'redis://localhost:6379/0'
$env:GITHUB_TOKEN = 'ghp_your_token_here'
```

Apply migrations:

```bash
alembic upgrade head
```

Start the console:

```powershell
# Windows (PowerShell)
$env:PYTHONPATH = 'src'
python -m serve
```

```bash
# macOS / Linux
PYTHONPATH=src python -m serve
# equivalent explicit form
PYTHONPATH=src uvicorn serve.app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/> — the dashboard. `/health` shows database, Redis, and token presence. Ready to crawl.

### Manual / native services

If you already run PostgreSQL and Redis (or prefer not to use Docker), skip Compose and point the environment at your instances:

- `DATABASE_URL` — any PostgreSQL 17+ database; `alembic upgrade head` creates everything.
- `REDIS_URL` — any Redis 7 instance. If omitted or unreachable, the app logs a warning and falls back to `fakeredis`; set `GITCRAWL_REDIS_STRICT=1` to make an unreachable Redis a hard failure instead.

Everything else is identical to the quickstart.

### Windows local development (project-local services)

The development environment used a non-elevated Windows shell, so it avoids Windows services and Docker Desktop:

- A **project-local PostgreSQL cluster** (initialized with the same PG binaries, port `5433`, data under `%LOCALAPPDATA%\gitcrawl\pgdata`) started by `pg_ctl`, with databases `gitcrawl` and `gitcrawl_test`.
- **Redis 7 from WSL2 Ubuntu** via `wsl -u root -- service redis-server start`.
- User-level environment variables (`DATABASE_URL`, `TEST_DATABASE_URL`, `REDIS_URL`, `GITHUB_TOKEN`) — note that a process that is already running will not see newly set variables, so restart your terminal/editor after changing them.
- Per-user logon scheduled tasks (`gitcrawl-postgres`, `gitcrawl-redis`) start both services automatically.

Every command — creation, start/stop/status, password rotation, troubleshooting — is documented in [`docs/environment.md`](docs/environment.md). That document also notes the deliberate deviation from Compose: `docker-compose.yml` remains in the repo for environments that do have Docker.

### Environment variables

All variables are read from the environment only; secrets are never stored in the repo or database.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | — (required) | SQLAlchemy DSN, e.g. `postgresql+psycopg://user:pw@host:5432/gitcrawl`. The app refuses to start without it. |
| `GITHUB_TOKEN` | — (required to crawl) | Single GitHub token. Used for all API calls; audit logs store only its SHA-256 fingerprint. |
| `GITHUB_TOKENS` | — | Comma-separated token list; takes precedence over `GITHUB_TOKEN` when it parses to ≥ 1 token. |
| `REDIS_URL` | fakeredis fallback | Redis DSN for limiter buckets, queues, metrics. |
| `GITCRAWL_REDIS_STRICT` | unset | `1` disables the fakeredis fallback; an unreachable Redis fails loudly. |
| `GITCRAWL_HOST` / `GITCRAWL_PORT` | `127.0.0.1` / `8000` | Bind address for `python -m serve`. |
| `GITCRAWL_ALLOWED_HOSTS` | loopback only | Extra comma-separated hosts for TrustedHost (behind a proxy, set the public hostname and run uvicorn with `--proxy-headers`). |
| `GITCRAWL_REQUEST_DEADLINE_SECONDS` | `3600` | Deadline for a synchronous API/run request; pins the `/settings` field. |
| `GITCRAWL_CLONE_TIMEOUT_SECONDS` | `1800` | Per-clone timeout. |
| `GITCRAWL_MAX_SHARDS` | `10` | Shard cap per run (pins `/settings`). |
| `GITCRAWL_MAX_CANDIDATES` | `500` | Candidate cap per run. |
| `GITCRAWL_MAX_HYDRATE` | `200` | Hydration cap per run (must be ≤ candidates). |
| `GITCRAWL_MAX_ENRICH` | `100` | Enrichment-check cap per run. |
| `GITCRAWL_GRAPHQL_BATCH` | `true` | Toggle batched GraphQL (falls back to per-repo REST when off). |
| `GITCRAWL_GRAPHQL_BATCH_SIZE` | `20` | Aliases per GraphQL request (1–20). |
| `GITCRAWL_MAX_CONCURRENT` | `10` | Max in-flight requests per endpoint/token. |
| `TEST_DATABASE_URL` | — | Test database; its name must end in `_test` (destructive-safe). Suites skip without it. |
| `GITCRAWL_REQUIRE_TEST_DB` | unset | `1` turns a missing test database into a failure instead of a skip (used in CI). |
| `UPDATE_GOLDEN` | unset | `1` regenerates the golden snapshots. |

The default run caps are intentionally small (laptop-friendly). Raise them at **System → Limits** (`/settings`) for corpus builds, or pin them from the environment. The settings page shows env-pinned fields as read-only.

### Database migrations

```bash
alembic upgrade head        # run from the repo root, with DATABASE_URL set
```

Migrations `0005`–`0007` rewrite constraints and indexes on live tables and take write-blocking locks. On a large database, run them in a maintenance window or convert them to `CONCURRENTLY` builds as described in [`docs/environment.md`](docs/environment.md#migration-locking). A downgrade of `0004` can be lossy for rows with a `NULL` `repo_id`; see the migration's docstring.

## Usage

### A first search

1. Start the app and open <http://127.0.0.1:8000/>.
2. Go to **Find** (`/find`): enter a GitHub query (e.g. `language:rust stars:>100`) and optionally virtual filters such as `has_dockerfile` or `owner_country`.
3. Run it. The run page polls live status; when it finishes it shows counts (found / saved / passed / unavailable / with Dockerfile), warnings, and flags for partial results.
4. Inspect the full results page, **export** JSON/CSV, **compare** against the previous same-filter run, or **freeze** the result as a corpus.
5. Optional: clone the top-N with the Clone control.

A first run on default limits will intentionally stop after a few hundred candidates and mark itself partial — raise the caps on `/settings` for full-corpus runs.

### JSON API

Examples assume the app is on `http://127.0.0.1:8000`.

Synchronous search with virtual filters:

```bash
curl "http://127.0.0.1:8000/vsearch/repos?q=language:rust%20stars:%3E100&has_dockerfile=true&per_page=5"
```

Run a full filter spec and get the frozen result body:

```bash
curl -X POST http://127.0.0.1:8000/vsearch/run \
  -H 'Content-Type: application/json' \
  -d '{
    "gitcrawl_filter": 1,
    "q": "language:rust stars:>100",
    "sort": "stars",
    "order": "desc",
    "virtual": {"has_dockerfile": true, "min_commits": 100},
    "page": {"per_page": 100, "max_pages": 10}
  }'
```

The response includes a `filter_hash`. Replay the latest ready run for that filter, or export it:

```bash
curl "http://127.0.0.1:8000/vsearch/runs/<filter_hash>"
curl -OJ "http://127.0.0.1:8000/vsearch/runs/<filter_hash>/export?format=csv"
curl -OJ "http://127.0.0.1:8000/runs/<run_id>/export?format=json"
```

Unknown parameters are rejected locally with a `400` and a hint (for example a typo like `updated:>2024` never silently becomes a text search). Invalid virtual values are rejected the same way, before any GitHub call.

### Filter-spec v1

```json
{
  "gitcrawl_filter": 1,
  "q": "language:rust stars:>100",
  "sort": "stars",
  "order": "desc",
  "virtual": {
    "has_dockerfile": true,
    "owner_country": "DE",
    "min_commits": 100
  },
  "page": {"per_page": 100, "max_pages": 10}
}
```

- `q` — GitHub query string; validated against the qualifier allowlist (max 256 keyword characters and 5 boolean operators; `props.*` requires exactly one `org:` scope).
- `sort` — one of `stars`, `forks`, `help-wanted-issues`, `updated`; `order` — `desc` or `asc` (ignored without `sort`).
- `virtual` — see the [virtual filter table](#features); `owner_country` implicitly raises `min_geo_confidence` to its default tier.
- `page.per_page` — 1–100; `page.max_pages` — 1–10.
- `frame` — opaque corpus-frame config recorded verbatim (for study designs); `as_of` — ISO-8601 date/datetime recorded with the run.
- The same document is what "Download as JSON" produces on `/find` and what "Upload and run" accepts. Files are validated before execution and may not contain tokens or local state.
- Canonical serialization of the normalized spec is hashed (SHA-256) into the `filter_hash` that identifies runs.

### Operator console routes

| Path | What it does |
|---|---|
| `/` | Dashboard: health badges, quick find, recent runs |
| `/find` (`/vsearch/` alias) | Filter form: run, download as JSON, upload and run, save |
| `/runs` | Run history with status/hash filters and paging |
| `/runs/{id}` | Run detail: live status, counts, warnings, export/replay/resume/clone, compare-with |
| `/runs/{id}/results` | Full-width result table; page, sort, and page size live in the URL |
| `/runs/{id}/diff?against={baseline_id}` | Compare with an earlier run (defaults to the previous same-filter run) |
| `/corpora`, `/corpora/{id}` | Frozen corpora and their contents |
| `/filters` | Saved filter library (HTML for browsers, JSON for other clients) |
| `/system` | Status / Performance / Limits overview |
| `/settings` | Run limits for new runs (env-pinned fields read-only) |
| `/health` | DB/Redis/token-present booleans only |
| `/metrics`, `/api/metrics` | SLO dashboard and JSON payload |

### API routes

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/vsearch/repos` | Synchronous search (`q`, `sort`, `order`, `per_page`, `page`, virtuals) |
| `POST` | `/vsearch/run` | Execute a filter spec, return the result body |
| `GET` | `/vsearch/runs/{filter_hash}` | Replay the latest ready run for a filter hash |
| `GET` | `/vsearch/runs/{filter_hash}/export` | Export by filter hash (`format=json\|csv`) |
| `GET` | `/runs/{run_id}/export` | Run-scoped export |
| `GET` | `/api/runs/{run_id}/diff` | Compare a run with a baseline |
| `POST` | `/runs/{run_id}/resume` | Resume a failed run (resets artifacts, re-fetches) |
| `GET` | `/runs/{run_id}/quality` | Per-run quality report |
| `GET` | `/runs/{run_id}/clone-estimate` | Clone disk estimate preview |
| `POST` / `DELETE` | `/runs/{run_id}/clone` | Start / cancel cloning the top-N |
| `GET` | `/filters` | Saved filters (HTML or JSON) |
| `POST` | `/filters/{id}/run` | Run a saved filter |

### Cloning repos

From a finished run, `GET /runs/{id}/clone-estimate?limit=N&mode=MODE` previews disk usage, then `POST /runs/{id}/clone` starts the clone; `DELETE /runs/{id}/clone` cancels it. Modes:

- `shallow` — `git clone --depth 1` (working tree, last commit)
- `file_only` — `--depth 1 --no-checkout` (no working tree)
- `windowed` — `--filter=blob:none --no-checkout` (history without blobs)

Clones land in `clones/<filter_hash>/<run_id>/<owner__repo>/`; completed directories are skipped on retry.

## Testing, linting, and type checking

The suite collects **1,364 tests** across five suites; CI runs it on Ubuntu and Windows against real PostgreSQL and Redis (service containers on Linux, native services on Windows).

```bash
pytest                                   # all suites; DB-backed suites skip without TEST_DATABASE_URL
pytest tests/unit                        # hermetic unit tests (no services needed)
pytest tests/integration tests/contract  # DB-backed (set TEST_DATABASE_URL)
pytest -q --cov=src --cov-report=term-missing   # coverage gate: fail_under = 93%
node --test tests/js/*.test.mjs          # JS helper tests (optional; pytest wrappers skip without Node)
UPDATE_GOLDEN=1 pytest tests/golden      # regenerate snapshots after intended console/API changes
```

With a test database (its name must end in `_test`):

```bash
TEST_DATABASE_URL='postgresql+psycopg://gitcrawl:gitcrawl@localhost:5432/gitcrawl_test' \
GITCRAWL_REQUIRE_TEST_DB=1 pytest
```

```powershell
$env:TEST_DATABASE_URL = 'postgresql+psycopg://gitcrawl:gitcrawl@localhost:5432/gitcrawl_test'
$env:GITCRAWL_REQUIRE_TEST_DB = '1'
pytest
```

Lint, format, and types:

```bash
ruff check src tests        # rules E, F, I, UP, B; line length 100
black --check src tests     # formatting; black src tests to fix
mypy                        # gated packages: lib, limiter, store, scheduler (ratchet policy in pyproject.toml)
```

The `golden/` suite snapshots console pages and pins the OpenAPI schema hash, so any unintended API or markup change fails CI until regenerated deliberately. Coverage last measured ~95% against the 93% floor.

## Security and legal notes

- **Localhost by default, no authentication.** The console is a single-operator tool. It binds `127.0.0.1` and enforces a TrustedHost allowlist; do not expose it to a network without putting real auth in front and setting `GITCRAWL_ALLOWED_HOSTS` plus proxy headers.
- **CSRF** protection via an Origin/Referer check and a per-request token; state-changing bodies are size-capped.
- **Tokens** are read from the environment only. They are never written to the database, never rendered in the console, and never logged raw — audit records store a SHA-256 fingerprint. Filter-spec files explicitly reject token-like fields.
- **Upstream responses stay upstream.** API errors are mapped to defined local responses (`400`/`502`/`503`) instead of leaking raw GitHub bodies.
- **API-first and ToS-aware.** The default path is the official REST/GraphQL API. HTML scraping is off by default and gated on a `robots.txt` check and legal review. Do not share tokens across accounts to evade rate limits. Deletion/privatization handling and data-retention expectations are documented in [`docs/legal-gates.md`](docs/legal-gates.md) and [`findings/05-research-gaps.md`](findings/05-research-gaps.md) §A10.
- **Attribution and trademark.** This project is not affiliated with or endorsed by GitHub. Use a descriptive `User-Agent` (already set: `gitcrawl/0.0.1`) and avoid GitHub marks/logos in any published surface.

## Project status and roadmap

**Built and wired** (verifiable in `src/` and the test suite): sharded live discovery with cap handling, immutable-id storage and lifecycle, per-bucket rate limiting with the failure classifier, REST + GraphQL hydration, cost-ordered virtual enrichment (`has_dockerfile`, `owner_country` + confidence, `min_commits`/`max_commits`), the operator console (find/runs/results/diff/corpora/library/system/settings/health), filter-spec v1 with replay and hashes, bundles/CSV, quality reports, audit log, SLO metrics, and top-N cloning.

**Designed but not implemented** — the console and specs label these clearly rather than pretending:

- **Coding-agent detection** (Detection in the nav): scan a frozen corpus for agent configuration/trace files. Design: [`docs/superpowers/specs/2026-10-02-agent-detection-design.md`](docs/superpowers/specs/2026-10-02-agent-detection-design.md).
- **`min_loc`/`max_loc`**: accepted and recorded, but full-depth LOC enrichment is not wired; using them flags the run incomplete.
- **`GET /repositories?since=` bulk enumeration**: the cursor scanner and its tests exist, but the pipeline runner is quarantined (not exposed through the console or API).
- **Org/user enumeration** (`run_org_enum`): implemented and tested, not yet reachable from a route.
- **Multi-segment enrichment parallelism**: the executor supports segments, but the runner currently always uses one.
- **Mid-run checkpointing**: none — resume re-fetches from the start (upserts make that safe, only slower).

Intentional dead surfaces are tracked in [`tests/quarantine_manifest.txt`](tests/quarantine_manifest.txt) and enforced by tests, so unwired code cannot silently rot. Migration `0010` is reserved for the detection feature.

**Known operational constraints**: migrations `0005`–`0007` take write-blocking locks; runs occupy a single executor for their whole duration; the default caps are small; the console has no auth (see above).

## Documentation map

| Document | What it contains |
|---|---|
| [`design/spec.md`](design/spec.md) | Frozen feature specification (user stories, requirements, acceptance scenarios) |
| [`design/how-the-data-flows.md`](design/how-the-data-flows.md) | Beginner-friendly narrative of every pipeline stage |
| [`design/landscape-comparison.md`](design/landscape-comparison.md) | Honest tool-by-tool comparison and capability matrix |
| [`design/plan.md`](design/plan.md), [`design/tasks.md`](design/tasks.md) | Stack decisions and task breakdown |
| [`design/data-model.md`](design/data-model.md) | Schema rationale and indexing notes |
| [`findings/00–06`](findings/00-overview.md) | Deep research on GitHub search limits, parameters, existing solutions, gaps, and the exhaustive capability matrix |
| [`docs/development-log.md`](docs/development-log.md) | What was built, when, with which rulings and verification numbers |
| [`docs/environment.md`](docs/environment.md) | Windows/local environment: services, credentials, commands, troubleshooting, console manual pass |
| [`docs/legal-gates.md`](docs/legal-gates.md) | Compliance checklist for crawling, storing, and serving |
| [`docs/superpowers/`](docs/superpowers/) | Design specs, implementation plans, and execution reports for the hardening phases |

## Name note

There is an unrelated open-source project also called `gitcrawl` ([`openclaw/gitcrawl`](https://github.com/openclaw/gitcrawl), Go) that mirrors GitHub issues and pull requests into SQLite for maintainer triage. This project is a Python repository-corpus discovery engine for research; it shares only the name. If you cite this tool, cite it by repository URL to avoid ambiguity.

## License

No license has been chosen for this project yet. Until one is added, the code is "all rights reserved" by default: you may view it, but you may not redistribute or reuse it without permission. If you intend to reuse any of this work, open an issue to discuss licensing.
