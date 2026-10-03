# gitcrawl

**Live-only GitHub repository discovery for evidence-backed research corpora.**

[![CI](https://github.com/Thabettt/gitcrawl/actions/workflows/ci.yml/badge.svg)](https://github.com/Thabettt/gitcrawl/actions/workflows/ci.yml)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![PostgreSQL 17+](https://img.shields.io/badge/PostgreSQL-17%2B-336791?logo=postgresql&logoColor=white)
![Status: research prototype](https://img.shields.io/badge/status-research%20prototype-orange)

> This is a research prototype, not production software. CI runs on pushes to `main` and on pull requests.
>
> **Name note:** this project is unrelated to [`openclaw/gitcrawl`](https://github.com/openclaw/gitcrawl), a Go issue/PR triage tool. Same name, different domain — see [Name note](#name-note) at the bottom.

**New here?** Read [The one-paragraph version](#the-one-paragraph-version), then [Why this exists](#why-this-exists) and [Why not just script the GitHub API?](#why-not-just-script-the-github-api). Those three answer "what is this and do I need it." Everything after that is reference.

## Table of contents

- [The one-paragraph version](#the-one-paragraph-version)
- [Why this exists](#why-this-exists)
- [Cast of characters (the five things to know)](#cast-of-characters-the-five-things-to-know)
- [The journey, stage by stage](#the-journey-stage-by-stage)
- [Why not just script the GitHub API?](#why-not-just-script-the-github-api)
- [How this relates to existing tools](#how-this-relates-to-existing-tools)
- [What's in the box](#whats-in-the-box)
- [How it's built](#how-its-built)
- [Data model and run artifacts](#data-model-and-run-artifacts)
- [Getting it running](#getting-it-running)
- [Using gitcrawl](#using-gitcrawl)
- [Testing, linting, and type checking](#testing-linting-and-type-checking)
- [Being a good citizen](#being-a-good-citizen)
- [Where the project is today](#where-the-project-is-today)
- [Glossary](#glossary)
- [Documentation map](#documentation-map)
- [Name note](#name-note)
- [License](#license)

---

## The one-paragraph version

You describe what you want in a filter — an ordinary GitHub search query plus the filters GitHub never built, like "has a Dockerfile" or "owner is in Germany." gitcrawl checks that filter for mistakes *before spending a single API call*, asks GitHub's live search for matching repositories, follows that search past the 1,000-result wall by slicing the query until every slice fits, saves each candidate's full current details, drops the ones that fail your own filters, resolves owner countries, merges everything by immutable repo `id`, shows it to you with its provenance, and freezes the whole run into a bundle you can replay, compare, export, and cite. Every stage exists because of a specific GitHub limitation, and this README explains each one where it comes up.

The design principle behind all of it: **live-only**. A run measures GitHub as it is at that moment (`ran_at`). There are no background pollers, no stale indexes, no watermarks, and nothing to keep alive between measurements. When you want a corpus, you take a fresh measurement and you keep the evidence.

## Why this exists

GitHub's repository search looks like a friendly little API call. It is actually a front door with a very small lobby, and most crawlers find that out the hard way:

- **The lobby holds 1,000 people.** No single query can return more than 1,000 results, no matter how many pages you ask for. Past result 1,000, GitHub returns an error (`422`), not more pages. Ranking also considers at most ~4,000 matching repos, so a very broad query can't even *see* the whole crowd. The only way through is to slice the query into smaller pieces (by creation date, usually) until each piece fits — a technique called **sharding**.
- **There are two prepaid meters, and they are small.** Search queries and result pages spend the *search* meter: 30 requests per minute per token. Visiting a repo for its full details spends the *main* meter: 5,000 requests per hour. On top of those sit GitHub's anti-abuse ("secondary") limits, which punish bursting but never punish steady pacing. Details and the arithmetic live in [`design/corpus-building-efficient-engineering.md`](design/corpus-building-efficient-engineering.md).
- **All filtering lives inside one text string.** GitHub supports a closed set of `qualifier:value` filters inside `q`, and nothing else. There is no server-side "has a Dockerfile," no "owner located in Germany," no "at least 100 commits." Those questions have to be answered locally, one repo at a time — after you've paid to fetch the repo.
- **Typos fail silently.** Type `updated:>2024` (the real qualifier is `pushed:`) and GitHub answers `200 OK` with your filter treated as *plain search text*. You get wrong data and no error. This is why validation is the very first stage of a run.
- **The search index lags pushes, and `total_count` is an estimate.** GitHub says so in its docs. That's why gitcrawl labels incompleteness instead of hiding it.
- **Repos change underneath you.** They rename, transfer, get deleted, or go private. Search never tells you which. gitcrawl keys everything on the immutable `id`, follows renames, and records deletions as tombstones.

A one-off script can answer "show me some Rust repos." It cannot answer *"give me the complete set of Rust repos matching this frame as of today, with the Dockerfile, owner-country, and commit-count filters applied, plus the evidence to defend every row."* That second question is the one this project exists for.

## Cast of characters (the five things to know)

These five concepts recur everywhere, so here they are once, in plain words. No prior GitHub API knowledge is assumed.

- **GitHub search** (`GET /search/repositories`) — the front door. You send one text string (`q`) with filters inside it (e.g. `language:rust stars:>100`) and get back *thin* records: name, description, stars, language, dates, plus a `score`. It returns at most 100 per page and 1,000 per query, allows ~30 requests/minute per token, and silently treats typos as words. Reference: [`findings/01-search-repos-parameters.md`](findings/01-search-repos-parameters.md).
- **Hydration** (`GET /repos/{owner}/{repo}`) — asking for one repo's *full* current record: topics, license, exact counts, flags, dates. Costs one call per repo from the separate 5,000/hour main meter. Unchanged repos can be re-fetched almost free via caching headers (ETag). **This is not cloning** — no files are downloaded.
- **Virtual filters** — the filters GitHub never built (`has_dockerfile`, `min_commits`, `owner_country`, …). gitcrawl computes them itself, per repo, after hydration, cheapest first.
- **Enrichment** — extra detail fetched per repo beyond the full record: file trees, language byte breakdowns, releases, funding links, discussions. Each answer costs calls, so enrichment is spent only on repos that survived your filters.
- **Run bundle** — the saved artifact of one search: your filters, when it ran, what GitHub said, what survived, and the raw upstream responses behind it. It is how a second device replays your search and how a study proves what was measured.

## The journey, stage by stage

Here is the whole pipeline at a glance, then each stage with its reason for existing.

```
filters.json → Validate → Live search → Thin matches → Hydrate → Virtual filters
→ Enrich → Geo resolve → Merge + cache → Display → Bundle/export
```

**Stage 0 — Your filters (a file, not just a form).** Everything starts as a `filter-spec v1` JSON document: the `q` string GitHub understands, sort/order/page options, and `virtual` filters only gitcrawl understands. Because it's a file, it travels between devices; because it holds no tokens, it's safe to share. The console's "Download as JSON" produces exactly this document, and "Upload and run" accepts it.

**Stage 1 — Validation (mistakes die here, free).** Before any network call, every qualifier is checked against the documented allowlist. `updated:>2024-01-01` (doesn't exist — the real qualifier is `pushed:`) or `is:archive` (the real one is `archived:true`) is rejected with a hint, because GitHub would have returned `200 OK` with *unfiltered* results and no error at all. A passing filter set gets a `filter_hash` fingerprint used for caching and replay. *Why this stage exists:* GitHub's silent-`200` behavior makes typos the most expensive kind of bug — paid in wrong data, not errors.

**Stage 2 — Live search (thin matches arrive).** The validated `q` goes to GitHub search, pages of 100 followed via `Link` headers. Each match is thin; its `score` is discarded immediately, because that score is relevance *relative to that one query* and meaningless to store or sort by later. Two honest limits surface here: past 1,000 results GitHub errors instead of paging, so broad queries are narrowed by recursive `created:` bisection rather than forced; and `total_count` is approximate, so it is displayed as an estimate with an `incomplete` flag when a timeout cuts a page short.

**Stage 3 — Hydration (thin → full, candidates only).** Each surviving match gets its full current record, one call each from the main meter. This is the most expensive stage per repo, so two economies apply: unchanged repos revalidate almost free (ETag → `304`, which GitHub documents as not counting against the primary limit), and nothing downstream starts for repos that will fail a later filter. A repo renamed since discovery is followed (`301`); a deleted or privatized one becomes a recorded tombstone, not a crash.

**Stage 4 — Virtual filters (gitcrawl's own screening room).** Now the filters GitHub can't express run, cheapest first: counts already in the hydrated record (`min_stars`, `team_topic`), then commit counts (`min_commits`/`max_commits`), then file checks (`has_dockerfile` via one recursive file-tree call covering every path question — never one call per file), then `owner_country` via the geo pipeline. Failures drop with a recorded reason and are counted in the run metadata, so "47 repos matched search, 31 survived filters" is always explainable.

**Stage 5 — Enrichment (detail only for survivors).** Only repos that pass every filter get enriched: language byte breakdowns, releases, funding links, discussions. The rules that keep this cheap: one file-tree call answers all path questions; GraphQL batches are used only where one query replaces three or more REST calls; identical files across repos are fetched once by content hash; package data comes from zero-token mirrors (ecosyste.ms, deps.dev) before any GitHub call is spent. Nothing is fetched for a display you won't show.

**Stage 6 — Geo resolve (free text → country, honestly).** Owner `location` is unstructured human text: `"🇩🇪 Berlin"`, `"Lagos"`, `"🌍 remote"`, often empty. The pipeline decodes flag emojis directly (deterministic, free), normalizes and splits multi-place strings ("Berlin / NYC" → two candidates), resolves cities through an offline gazetteer at zero call cost, catches country-name variants ("USA", "Nederland") with an alias table, and only lets the residue touch a geocoder — cached forever by normalized string. Weak hints (domain endings, timezones) break ties, never lead. Every result stores `{country_iso, confidence, raw_location}` with an explicit unmatched bucket: an honest unknown beats a confident wrong answer.

**Stage 7 — Merge + cache (one result set, many devices).** Survivors merge by immutable repo `id` — never by name, because names change on rename or transfer — so the same filter file run on two laptops produces unionable, dedupe-safe sets. The merged set caches under its `filter_hash` briefly: an identical search minutes later costs zero GitHub calls.

**Stage 8 — Display (cards with provenance).** Each row shows its metadata, virtual-filter outcomes, owner country with confidence, and *when* it was measured (`ran_at`). Truncation is always declared: capped-by-1,000, timed-out pages, budget stops, and unenforceable filters appear as labeled flags, never as silent gaps.

**Stage 9 — Bundle and export (the replayable artifact).** The run persists its filter hash, `ran_at`, API version, uniform counts, and the raw upstream JSON per repo. Export downloads it as JSON or CSV. Byte-identical reproduction comes from this bundle — re-running the same file later may drift (stars move, repos vanish), and the bundle is what explains the difference. For research use, this bundle *is* the replication package's raw layer.

And a closing note on scale: none of the above changes when a run gets big. An exhaustive run is the *same* live flow with more shards; it is not a second, background pipeline.

## Why not just script the GitHub API?

Fair question, and the honest answer starts on your side of the table: **for a one-off sample, you should just script it.** This is the right tool for "show me some repos":

```bash
gh search repos 'language:rust stars:>100' --limit 100 --json fullName,stargazersCount
```

Here is what quietly changes when the output has to be a complete, defensible corpus:

| The script | What happens at scale | What gitcrawl adds |
|---|---|---|
| One query, first page(s) | Result 1,001 returns `422`; `total_count` is approximate | Recursive `created:` bisection until every shard is fetchable, cap/incomplete auto-split, coverage counters |
| A straight request loop | 30 search req/min; 403/429/422 mean three different things | Per-bucket Redis pacing from response headers; a classifier for retry-after / reset-wait / backoff / cap-split / fix / fail-loud; multi-token support |
| No file/geo/commit filters server-side | You fetch thousands of repos to answer one file question | Cost-ordered enrichment: GraphQL file-presence batches, trees API, owner geo resolver, commit counts — cheap screens first, survivors only |
| `owner/name` as the key | Renames and transfers silently fork your data | Immutable `id` primary key, `full_name_history`, `301` follow, `404` tombstones |
| "It worked on my machine Tuesday" | You cannot reproduce last week's sample | Filter-spec hash, frozen bundles with raw upstream evidence, replay, run-to-run diff |
| Crash = start over | A long run fails at hour three | Orphan recovery, resume (re-fetches from the start, safely), a quality report per run |
| No telemetry | You cannot show what you spent or when | Per-request audit log (rate-limit headers, status, latency, token fingerprint) and SLO dashboards |

The honest summary: **a script answers a query; gitcrawl produces a corpus you can defend.** If you only need the query, stay with the script — genuinely.

## How this relates to existing tools

The same question has another form: *"why not use the tool that already exists?"* The landscape splits into four families, and each is good at its slice:

| Family | Examples | What they provide | What they cannot do |
|---|---|---|---|
| Pre-crawled research catalogs | SEART GitHub Search + Data Hub | A queryable database of ~1.9M continuously crawled repos | Fixed columns, a 10-star floor, crawl-time freshness (not yours), no file/geo filters, no per-run evidence |
| Offline archives at ecosystem scale | World of Code, GH Archive, BigQuery GitHub data | Raw historical objects and events at enormous scale | Monthly snapshots or raw events, not live filtered selection with today's metadata |
| Analytics dashboards | OSS Insight, DevStats | Aggregated trends and rankings | Aggregate answers, not frozen per-repo corpora |
| Thin API wrappers | `gh search repos`, MCP/assistant connectors | Ergonomic pass-through with JSON output | The same 1,000-result cap, no virtual filters, no persistence, no evidence bundle |

gitcrawl targets the intersection none of them covers: **live measurement at a chosen `ran_at`, arbitrary filters including file presence and owner location, coverage below the 10-star floor, frozen per-run evidence with raw upstream responses, audited pacing, and a path to coding-agent detection.** For a standard off-the-shelf sample, SEART is genuinely the better choice; for ecosystem-scale history, use World of Code. The full honest comparison, including a capability matrix and the case for building rather than adopting, lives in [`design/landscape-comparison.md`](design/landscape-comparison.md) and [`design/gitcrawl-vs-seart.md`](design/gitcrawl-vs-seart.md).

## What's in the box

### Discovery

- **Sharded live search.** gitcrawl slices your query by `created:` date ranges until every shard is under GitHub's 1,000-result cap, then pages each shard with `per_page=100`, following `Link: rel="next"` verbatim. Shards that still come back over the cap or `incomplete` are split again automatically and every shard records its `total_count`, fetched count, and state (`pending/active/done/incomplete`).
- **A validation gate with teeth.** Every qualifier is checked against the documented allowlist with typo hints; a "delta test" in the test suite proves filtering actually narrows results, so a silent `200` can never masquerade as filtering.
- **Org and user enumeration** (`GET /orgs/{org}/repos`, `GET /users/{u}/repos`) is implemented and tested for single-scope jobs; `GET /repositories?since=` cursor scanning exists too. Both are currently quarantined from the console (see [Where the project is today](#where-the-project-is-today)).

### Storage and lifecycle

- Repos are keyed on the immutable `id BIGINT`, with `full_name` unique, a `full_name_history` log for renames, and `deleted_at` tombstones for deletions and privatizations.
- Revalidation follows `301`s and tombstones `404`s instead of letting rows vanish or fork.
- Upserts are batched `INSERT ... ON CONFLICT DO UPDATE`; a COPY-based bulk path exists in the code but is quarantined as unused.

### Hydration and enrichment

- **Two hydration paths:** REST `GET /repos/{full_name}` with ETag/`If-None-Match`, and aliased GraphQL batches of up to 20 repos with automatic per-repo REST fallback — so one bad repo never poisons its neighbours.
- Commit counts come from GraphQL `defaultBranchRef.target.history.totalCount` or REST `Link: rel=last`.
- File presence is answered by a GraphQL batch first, with the recursive Git Trees API as fallback — never N× `contents` loops.
- Owner locations resolve through the offline gazetteer described in [Stage 6](#the-journey-stage-by-stage) and cache per location in `geo_cache`.
- The cost planner orders all of this record → mirror → single → batched → deep, and the run records per-field source and calls spent.

### Virtual filters

All are validated locally with actionable hints; the table says where each is enforced.

| Filter | Type | How it is enforced |
|---|---|---|
| `min_stars` | integer ≥ 0 | Translated to `stars:>=N` in the GitHub query |
| `team_topic` | topic slug (`^[a-z0-9][a-z0-9-]{0,49}$`) | Translated to `topic:x` |
| `has_dockerfile` | boolean | File-presence batch (GraphQL → trees fallback) |
| `owner_country` | ISO 3166-1 alpha-2 (e.g. `DE`) | Owner location → geo resolver → confidence tier |
| `min_geo_confidence` | `exact-iso` \| `name` \| `gazetteer-city` \| `geocoder` \| `weak` (default `gazetteer-city`) | Confidence floor for `owner_country` |
| `min_commits` / `max_commits` | integer ≥ 0 | Hydrated default-branch commit count |
| `min_loc` / `max_loc` | integer ≥ 0 | **Recorded only — not enforceable yet**; a run using them is flagged incomplete |

### The operator console

Everything is server-rendered Jinja2 with vendored htmx and hand-rolled CSS — no Node, no CDN, no build step at runtime. The pages: dashboard, **Find** (the filter form, with download-as-JSON, upload-and-run, and save), **Searches** (run history, live status, full-width paginated results, replay, resume, export, clone, compare-with), **Corpora** (freeze a finished search into a named snapshot), **Library** (saved filters you can run again), **System** (status, performance, limits), **Settings** (run caps), **Metrics** (SLOs), and **Health**. Keyboard navigation (`/`, `g h/f/r/l`, `j`/`k`, `Enter`, `?`, `Esc`), dark mode, and CSRF-protected forms come standard; page, sort, and page size always live in the URL, so any view is linkable.

### Reproducibility

- Filter-spec v1 JSON in, run bundle out: `bundle.json`, `corpus.csv`, and persisted run metadata.
- Replay by filter hash, run-scoped export (`json`/`csv`), and run-to-run diff (`new repos / gone / changed`).
- A per-run quality report: count parity, duplicates, null ratios, incompleteness, bundle readability, field coverage.
- An audit log with one row per GitHub response (params, status, rate-limit headers, retry-after, Link, totals, token fingerprint, latency) and an SLO dashboard on top (limiter state, queue depth, run counts, incomplete ratio, `422/403+429` rates, p95 latency, geo unmatched rate).

### Optional cloning

Fetching never clones. If you want code on disk, the Clone control takes a count (top-N by current sort) and a mode — `shallow` (`--depth 1`), `file_only` (`--depth 1 --no-checkout`), or `windowed` (`--filter=blob:none --no-checkout`) — previews the disk estimate, warns below 2 GiB free, and clones into `clones/<filter_hash>/<run_id>/<owner__repo>/`. Retries skip finished repos, progress persists, and cancelling is supported. Zero clones is a complete, valid run.

### Guardrails

Localhost binding by default, TrustedHost allowlist, Origin/CSRF middleware, and request body caps (1 MiB JSON, 10 MiB uploads). Tokens live only in the environment; logs record fingerprints, not secrets; filter-spec files explicitly reject token-like fields; and startup recovers orphaned runs instead of leaving them spinning.

## How it's built

Everything runs from the repo source with `PYTHONPATH=src` (implicit namespace packages — there is no installed package or console script). The only runnable entry point is `python -m serve`, and the layout follows the pipeline:

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

The stack, in one sentence: Python 3.12+ (PEP 695 syntax is used, so 3.12 is mandatory), FastAPI + Uvicorn, Jinja2 + vendored htmx + hand-rolled CSS, SQLAlchemy 2.x + Alembic on PostgreSQL, Redis 7 (or `fakeredis`), `httpx`, and Pydantic. Pinned versions are in [`requirements.txt`](requirements.txt) and [`requirements-dev.txt`](requirements-dev.txt).

### Rate limiting, the short version

Pacing is a first-class subsystem here, not a retry loop bolted on:

- Four Redis token buckets — `search` (30/min), `core` (5,000/hr), `code_search` (10/min), `graphql` (5,000 pts/hr) — keyed by resource and token fingerprint.
- Response headers are authoritative. `X-RateLimit-*` values are reconciled into the buckets, and `remaining == 0` pauses only the exhausted bucket until its reset.
- Every failure maps to exactly one action: `retry_after`, `wait_reset`, `backoff` (jittered and bounded), `shard` (the cap-`422`), `fix` (a validation-`422`), or `fail_loud` (SSO partial results, bad credentials). No loop without a limit; no silent degradation.
- Without Redis the runner falls back to `fakeredis` with a warning (`GITCRAWL_REDIS_STRICT=1` forbids that fallback). Unit tests are hermetic via `fakeredis[lua]`.

## Data model and run artifacts

Eleven PostgreSQL tables, created by nine Alembic revisions:

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

One run leaves three things on disk, in a directory named by its filter hash and run id:

```
runs/<filter_hash>/<run_id>/
├── bundle.json          # filter, ran_at, api_version, totals, items[], field_stats
├── corpus.csv           # id,full_name,stargazers,pushed_at,archived,language,license_spdx,country_iso,geo_confidence
└── clone-progress.json  # written only while cloning
clones/<filter_hash>/<run_id>/<owner__repo>/   # one dir per clone, with a done marker
```

`field_stats` inside the bundle records `per_field_sources`, `calls_spent`, `requeues`, `warnings`, and GraphQL batch statistics — so every enriched field can be traced back to how it was obtained. `runs/` and `clones/` are runtime artifacts and git-ignored.

## Getting it running

### What you need

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

Install the pinned dependencies:

```bash
pip install -r requirements.txt -r requirements-dev.txt
```

Set the environment (swap in your own token):

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

Apply the migrations:

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

Open <http://127.0.0.1:8000/>. The dashboard greets you, and `/health` tells you plainly whether the database, Redis, and a GitHub token are all present. You're ready to crawl.

### Manual / native services

Already run PostgreSQL and Redis, or prefer not to use Docker? Skip Compose and point the environment at your instances:

- `DATABASE_URL` — any PostgreSQL 17+ database; `alembic upgrade head` creates everything.
- `REDIS_URL` — any Redis 7 instance. If it's omitted or unreachable, the app logs a warning and falls back to `fakeredis`; set `GITCRAWL_REDIS_STRICT=1` to make an unreachable Redis a hard failure instead.

Everything else is identical to the quickstart.

### Windows local development (project-local services)

The development environment ran from a non-elevated Windows shell, so it deliberately avoids Windows services and Docker Desktop:

- A **project-local PostgreSQL cluster** (created with the same PG binaries, port `5433`, data under `%LOCALAPPDATA%\gitcrawl\pgdata`) started by `pg_ctl`, with databases `gitcrawl` and `gitcrawl_test`.
- **Redis 7 from WSL2 Ubuntu** via `wsl -u root -- service redis-server start`.
- User-level environment variables (`DATABASE_URL`, `TEST_DATABASE_URL`, `REDIS_URL`, `GITHUB_TOKEN`). One gotcha worth internalising: a process that is already running does not see newly set variables, so restart your terminal or editor after changing them.
- Per-user logon scheduled tasks (`gitcrawl-postgres`, `gitcrawl-redis`) start both services automatically.

Every command — creation, start/stop/status, password rotation, troubleshooting — is documented in [`docs/environment.md`](docs/environment.md). That document also records the deliberate deviation from Compose: `docker-compose.yml` stays in the repo for environments that do have Docker.

### Environment variables

Everything is read from the environment only. Secrets are never stored in the repo or the database.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | — (required) | SQLAlchemy DSN, e.g. `postgresql+psycopg://user:pw@host:5432/gitcrawl`. The app refuses to start without it. |
| `GITHUB_TOKEN` | — (required to crawl) | Single GitHub token. Audit logs store only its SHA-256 fingerprint. |
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

The default run caps are intentionally small — laptop-friendly, interactive. Raise them at **System → Limits** (`/settings`) for corpus builds, or pin them from the environment; the settings page renders env-pinned fields as read-only.

### Database migrations

```bash
alembic upgrade head        # run from the repo root, with DATABASE_URL set
```

One warning worth reading before you point this at a large database: migrations `0005`–`0007` rewrite constraints and indexes on live tables and take write-blocking locks. On a big database, run them in a maintenance window or convert them to `CONCURRENTLY` builds as described in [`docs/environment.md`](docs/environment.md#migration-locking). A downgrade of `0004` can also be lossy for rows with a `NULL` `repo_id`; see the migration's docstring.

## Using gitcrawl

### A first search

1. Start the app and open <http://127.0.0.1:8000/>.
2. Go to **Find** (`/find`): enter a GitHub query (e.g. `language:rust stars:>100`) and optionally virtual filters such as `has_dockerfile` or `owner_country`.
3. Run it. The run page polls live status; when it finishes it shows counts (found / saved / passed / unavailable / with Dockerfile), warnings, and partial-result flags.
4. Inspect the full results page, **export** JSON/CSV, **compare** against the previous same-filter run, or **freeze** the result as a corpus.
5. Optional: clone the top-N with the Clone control.

A first run on default limits will intentionally stop after a few hundred candidates and mark itself partial — that's the laptop-friendly default working as designed. Raise the caps on `/settings` for full-corpus runs.

### Filter-spec v1, the document behind every search

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

- `q` — the GitHub query string; validated against the qualifier allowlist (max 256 keyword characters and 5 boolean operators; `props.*` requires exactly one `org:` scope).
- `sort` — one of `stars`, `forks`, `help-wanted-issues`, `updated`; `order` — `desc` or `asc` (ignored without `sort`).
- `virtual` — see the [virtual filter table](#virtual-filters); `owner_country` implicitly raises `min_geo_confidence` to its default tier.
- `page.per_page` — 1–100; `page.max_pages` — 1–10.
- `frame` — opaque corpus-frame config recorded verbatim (for study designs); `as_of` — an ISO-8601 date/datetime recorded with the run.
- The same document is what "Download as JSON" produces on `/find` and what "Upload and run" accepts. Files are validated before execution and may not contain tokens or local state.
- Canonical serialization of the normalized spec is hashed (SHA-256) into the `filter_hash` that identifies runs.

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

Unknown parameters are rejected locally with a `400` and a hint — a typo like `updated:>2024` never silently becomes a text search. Invalid virtual values are rejected the same way, before any GitHub call.

### Console routes, in one table

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

### API routes, in another

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

### Cloning, the optional epilogue

From a finished run, `GET /runs/{id}/clone-estimate?limit=N&mode=MODE` previews disk usage, `POST /runs/{id}/clone` starts the clone, and `DELETE /runs/{id}/clone` cancels it. The three modes:

- `shallow` — `git clone --depth 1` (working tree, last commit)
- `file_only` — `--depth 1 --no-checkout` (no working tree)
- `windowed` — `--filter=blob:none --no-checkout` (history without blobs)

Clones land in `clones/<filter_hash>/<run_id>/<owner__repo>/`, and completed directories are skipped on retry.

## Testing, linting, and type checking

The suite collects **1,364 tests** across five suites. CI runs it on Ubuntu and Windows against real PostgreSQL and Redis (service containers on Linux, native services on Windows).

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

Two pieces of test furniture deserve a sentence each. The `golden/` suite snapshots console pages and pins the OpenAPI schema hash, so any unintended API or markup change fails CI until regenerated deliberately. And the coverage gate (`fail_under = 93`) is a floor, not a target — coverage last measured around 95%.

## Being a good citizen

- **Localhost by default, no authentication.** The console is a single-operator tool. It binds `127.0.0.1` and enforces a TrustedHost allowlist; do not expose it to a network without putting real auth in front and setting `GITCRAWL_ALLOWED_HOSTS` plus proxy headers.
- **CSRF** protection via an Origin/Referer check and a per-request token; state-changing bodies are size-capped.
- **Tokens** are read from the environment only. They are never written to the database, never rendered in the console, and never logged raw — audit records store a SHA-256 fingerprint. Filter-spec files explicitly reject token-like fields.
- **Upstream responses stay upstream.** API errors are mapped to defined local responses (`400`/`502`/`503`) instead of leaking raw GitHub bodies.
- **API-first and ToS-aware.** The default path is the official REST/GraphQL API. HTML scraping is off by default and gated on a `robots.txt` check and legal review. Do not share tokens across accounts to evade rate limits. Deletion/privatization handling and data-retention expectations are documented in [`docs/legal-gates.md`](docs/legal-gates.md) and [`findings/05-research-gaps.md`](findings/05-research-gaps.md) §A10.
- **Attribution and trademark.** This project is not affiliated with or endorsed by GitHub. Use a descriptive `User-Agent` (already set: `gitcrawl/0.0.1`) and avoid GitHub marks or logos in any published surface.

## Where the project is today

**Built and wired** (verifiable in `src/` and in the test suite): sharded live discovery with cap handling; immutable-id storage and lifecycle; per-bucket rate limiting with the failure classifier; REST + GraphQL hydration; cost-ordered virtual enrichment (`has_dockerfile`, `owner_country` + confidence, `min_commits`/`max_commits`); the operator console (find/runs/results/diff/corpora/library/system/settings/health); filter-spec v1 with replay and hashes; bundles and CSV; quality reports; audit log; SLO metrics; and top-N cloning.

**Designed but not implemented** — the console and docs label these clearly rather than pretending:

- **Coding-agent detection** ("Detection" in the nav, greyed out): scanning a frozen corpus for agent configuration and trace files. Design: [`docs/superpowers/specs/2026-10-02-agent-detection-design.md`](docs/superpowers/specs/2026-10-02-agent-detection-design.md).
- **`min_loc`/`max_loc`**: accepted and recorded, but full-depth LOC enrichment is not wired; using them flags the run incomplete.
- **`GET /repositories?since=` bulk enumeration**: the cursor scanner and its tests exist, but the pipeline runner is quarantined (not exposed through the console or API).
- **Org/user enumeration** (`run_org_enum`): implemented and tested, not yet reachable from a route.
- **Multi-segment enrichment parallelism**: the executor supports segments, but the runner currently always uses one.
- **Mid-run checkpointing**: none — resume re-fetches from the start (upserts make that safe, only slower).

Intentional dead surfaces are tracked in [`tests/quarantine_manifest.txt`](tests/quarantine_manifest.txt) and enforced by tests, so unwired code cannot silently rot. Migration `0010` is reserved for the detection feature.

**Known operational constraints**: migrations `0005`–`0007` take write-blocking locks; a run occupies the single executor for its whole duration; the default caps are small; the console has no auth (see above).

## Glossary

- **Search** — one live discovery run. Shown as runs in the console; the noun for one measurement.
- **Corpus** — a named, immutable snapshot of a finished search.
- **Detection** — the planned feature that scans a frozen corpus for coding-agent config/trace files. Not implemented yet, by design.
- **`ran_at`** — the UTC moment a search measured GitHub. Every bundle records it.
- **Filter spec (v1)** — the JSON document that fully describes a search: `q`, `sort`, `order`, `virtual`, `page`, optional `frame`, optional `as_of`.
- **Filter hash** — the SHA-256 of the canonical normalized spec; the identity of a search for replay and caching.
- **Virtual filter** — a condition GitHub search cannot express; some translate to legal qualifiers, others are enforced locally after hydration.
- **Hydration** — fetching one repo's full current details. Not cloning.
- **Touch** — one request spent on one repo (to save it or check it); the term comes from the efficiency doc's metering model.
- **Sharding** — splitting one broad filter into many smaller queries, usually by creation date, so each stays under the 1,000-result cap.
- **ETag / `304`** — a stored fingerprint for a repo; if nothing changed, GitHub says so for free.
- **Run bundle** — the frozen artifact of a search: filters, `ran_at`, counts, item rows, provenance, raw upstream evidence.
- **Incomplete** — the honest flag on a run that hit a budget, a cap, or an unenforceable filter. Never silently hidden.
- **Geo confidence tiers** — `exact-iso`, `name`, `gazetteer-city`, `geocoder`, `weak`; the ladder behind every owner-country answer.

## Documentation map

| Document | What it contains |
|---|---|
| [`design/spec.md`](design/spec.md) | Frozen feature specification (user stories, requirements, acceptance scenarios) |
| [`design/how-the-data-flows.md`](design/how-the-data-flows.md) | The beginner-friendly narrative this README borrows its voice from |
| [`design/corpus-building-efficient-engineering.md`](design/corpus-building-efficient-engineering.md) | The meters, the math of what is findable vs. savable, the legal limits, and the batching engine |
| [`design/landscape-comparison.md`](design/landscape-comparison.md) | Honest tool-by-tool comparison and capability matrix |
| [`design/gitcrawl-vs-seart.md`](design/gitcrawl-vs-seart.md) | The closest-prior-art comparison, in depth |
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
