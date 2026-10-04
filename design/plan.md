# Implementation Plan: gitcrawl

**Branch**: `001-gitcrawl` | **Date**: 2026-09-29 (updated 2026-10-04) | **Spec**: [spec.md](./spec.md)

**Status: historical plan of record.** This is the plan the build followed. Where reality diverged, the deviations are recorded in `../docs/environment.md` (stack/environment) and `../docs/development-log.md` (outcomes and rulings R1–R58); the inline corrections below keep the plan from misleading a reader. Read `how-the-data-flows.md` before this document.

**The one-paragraph version**: build the SEART pattern — crawl → store → serve — hardened with the `05` gap fixes and the efficiency review, and locked to live-only: every job runs on the go at `ran_at`, with no background polling. Discovery is dual-path (sharded REST search for filtered queries; `since` enumeration and org listing for bulk). Storage is Postgres keyed on the immutable `id`. A Redis limiter paces each rate bucket separately. Enrichment runs cost-aware (trees first, GraphQL batches only where they beat REST, zero-token mirrors before paid calls). The serve layer exposes virtual parameters GitHub never built. Audit logging, SLOs, and legal gates exist from day one, not bolted on later.

**Input**: Feature specification from `design/spec.md` (US1 discover P1 · US2 freshness P2 · US3 enrich+serve P3), grounded in `findings/00–06`.

**Note**: filled per the speckit plan workflow; the user overrode the default `specs/` location with `design/`.

## Summary

Build the SEART-pattern crawler hardened per the `05` gaps + efficiency review, live-only (the entire job runs on the go at `ran_at`, no background polling): dual-path discovery (sharded REST search for filtered queries + `since`-enumeration/org-listing for bulk), Postgres on immutable `id`, Redis per-bucket limiter, Python FastAPI workers, within-run lifecycle (301 follow / 404 tombstone, no watermarks/tiers/GH Archive tail), cost-aware enrichment (trees-first, GraphQL-batch-only, ecosyste.ms zero-burn), owner-country resolver (`06 §6.1`), and a virtual-params serve API — with audit logging, SLOs, and legal gates from day one. PostgreSQL 17+ is the store at every stage (the original SQLite skeleton/laptop path was retired with `src/skeleton.py` under R58). New to the system? Read `how-the-data-flows.md` before this plan.

## Technical Context

**Language/Version**: Python 3.12+ (the replication package requires 3.12+).

**Primary Dependencies**: FastAPI (serve + workers API), httpx (GitHub REST/GraphQL clients), SQLAlchemy 2 + Alembic (Postgres), psycopg3 (pipeline mode for OLTP writes, `COPY` TEXT + `ON_ERROR ignore` for bootstrap — ruling R23; `stmt_cache_size=0` under PgBouncer), redis-py (token buckets + Streams queue), pydantic v2 (contracts/validation), pg_partman + pg_cron (partition retention), PyYAML + exact version pins locked at setup.

**Storage**: PostgreSQL 17+ (primary: `repos`, `full_name_history`, `owners`, `shards`, `audit_log`, `geo_cache`, plus the console's `runs`/`run_items`/`saved_filters`, `app_settings`, `corpora`); Redis 7 (rate buckets, shard queue, ETag cache) at scale; PgBouncer transaction mode in front of Postgres; object/blob storage explicitly N/A for v1 (no clone corpus by default).

**Testing**: pytest (+ httpx MockTransport contract tests, golden-org integration tests, `id`-set diff harness) — OVERCOVERAGE MANDATE APPLIES (see the Testing Overcoverage Mandate below; test:app ~2:1 accepted).

**Target Platform**: Linux server / Docker Compose (single-host MVP, horizontally scalable workers). The Windows project-local setup used during development is documented in `../docs/environment.md` as a deliberate deviation.

**Project Type**: service (scheduler + workers) with a local operator console (US4) and a CLI runbook (`quickstart.md`).

**Performance Goals**: live-only serve (~10–20s first Find: bounded search + visible-page enrich; cached repeats fast); discovery at dense-search rate (≤1,800 search req/hr/token, 100/page); hydration ≤5,000 core req/hr/token; no background tails — all budget spent within the run.

**Constraints**: search 30/min/token, core 5,000/hr/token (no token-sharing to evade); ~10 concurrent per endpoint per token (900 pts/min math); 1000-fetchable + ~4000-scan per query; `q` 256-char/5-op caps; API-only default (HTML fallback needs legal review + `robots.txt` check); GDPR deletion purge window; PII minimization (no commit emails stored).

**Scale/Scope**: v1 validates on the golden org (`org:github`) to `id`-set parity; the production target is filtered cross-GitHub coverage in single on-the-go runs (unfiltered 200M-repo full hydration is explicitly out of v1 — bootstrap via ecosyste.ms/`since` sampling instead). Tokens via env (`GITHUB_TOKEN`/`GITHUB_TOKENS`, never hardcoded): 1 PAT for dev/golden-org, 2+ for the dev loop, 5–10 for 100k+ corpus runs.

## Testing Overcoverage Mandate (NON-NEGOTIABLE)

- Every testable path MUST be tested: happy path, error paths, edge cases, boundary values, empty/null/malformed inputs, rate-limit/retry branches (`403/429/422/304/301/404`), pagination caps, tombstone/rename flows, cache hit/miss, allowlist `400` rejections. No slack.
- Each `src/` module MUST have a dedicated test file covering all branches (branch coverage 100% required, not just line coverage). Uncovered branch = build failure.
- Test:app ratio up to ~2:1 is explicitly accepted and expected. Prefer explicit duplicated tests over clever parametrization that hides paths.
- Contract tests MUST assert every row of `contracts/search-api.md` translation/error tables; unit tests MUST assert every classifier/watermark/validator branch; integration tests MUST assert golden-org parity + lifecycle fixtures.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- No existing constitution file in the repo (checked `.specify/` templates only) — proceeding on `findings/` research as the governing constraints (rate limits, ToS §H, privacy, trademark).
- Efficiency mandates from review are normative: no daily search backfill, no global all-shard poll, no search-path ETag, no `total_count` validation, trees-first enrichment, per-bucket pausing — violations require written justification in the PR.
- Re-check after Phase 1: data-model + contracts must preserve `id`-PK, tombstone, allowlist, and audit-log invariants (gates for `tasks.md` Phase 2).

## Project Structure

### Documentation (this feature)

```text
design/
├── spec.md               # Feature spec (user stories, FR/SC, entities)
├── plan.md               # This file
├── research.md           # Phase 0 decisions distilled from findings/00–06
├── data-model.md         # Phase 1 schema (DDL + indexes + lifecycle rules)
├── quickstart.md         # Phase 1 golden-org validation runbook
├── contracts/
│   └── search-api.md     # Phase 1 serve contract (virtual params, errors)
└── tasks.md              # Phase 2 task list by user story (separate command output)
```

### Source Code (repository root)

The list below is the **current** layout, with the original plan's entries corrected where rulings moved or removed them.

```text
src/
├── scheduler/
│   ├── shard_planner.py      # created:/stars: bisect to <1000 and <<4000 scanned
│   ├── tiering.py            # within-run shard ordering (cheap-first, survivors-first; no cadences)
│   └── state_machine.py      # pending/active/done/incomplete per shard (within-run only)
├── limiter/
│   ├── buckets.py            # search/core/code_search/graphql Redis token buckets
│   └── classifier.py         # retry-after → reset → exp-backoff; 422 triage
├── discover/
│   ├── search_shards.py      # sharded GET /search/repositories, Link follower
│   ├── since_scan.py         # GET /repositories?since= cursor + ID-range shards (quarantined runner)
│   ├── org_enum.py           # GET /orgs+users repos enumeration (tested; not exposed yet)
│   └── pipeline.py           # discovery orchestration (count_total, shard execution, upserts)
├── store/
│   ├── models.py             # repos/owners/history/shards/audit/geo_cache + console/settings/corpora
│   ├── upserts.py            # batched ON CONFLICT (id) w/ no-op gating, COPY TEXT bootstrap (R23)
│   ├── lifecycle.py          # 301 follow, 404 tombstone, retention purge
│   └── settings.py           # RunSettings + app_settings load/update + env pinning
├── hydrate/
│   ├── repo_client.py        # GET /repos + ETag (stable URLs only)
│   ├── graphql_repo.py       # GraphQL repo-details batch adapter (REST fallback)
│   └── tail.py               # live within-run refresh (current pushed_at at ran_at; no watermark poller)
├── enrich/
│   ├── cost_planner.py       # cost-ordered filter plan + per-field source priority (D12)
│   ├── segment_executor.py   # id-range segments × tokens, survivors-first, lazy depth
│   ├── trees_first.py        # trees?recursive=1 / metafiles over contents loops
│   ├── graphql_file_presence.py / graphql_owner_location.py  # batch adapters (file/geo)
│   ├── geo_resolver.py       # 06 §6.1 flag→gazetteer→alias→cached-geocoder
│   ├── cloner.py             # optional post-fetch clone (top-N, modes, estimate, resume) — T051
│   └── (retired under R58: mirrors.py, graphql_batch.py — the batch core now lives in src/lib/)
│   └── (deferred, thesis tracks — not built: trace_packs, commit_history, pr_channel,
│        file_inventory, crates_link, snowball, validation; see tasks.md Phase 6)
├── serve/
│   ├── app.py                # FastAPI JSON API + app factory
│   ├── pages.py              # server-rendered console routes
│   ├── executor.py           # single-worker run executor
│   ├── virtual_params.py     # translation table + upstream allowlist + own 400
│   ├── filter_spec.py        # filter-spec v1 schema + corpus-frame validation (T033/T038)
│   ├── runs.py               # run/replay/export + clone estimate/progress (T034/T051)
│   ├── library.py / diff.py / corpora.py / quality.py / metrics.py / settings.py / system.py
│   ├── templates/            # base/dashboard/filters/run detail/history/diff/library/corpora/...
│   └── static/               # vendored htmx + hand-rolled app.css/app.js (R46)
└── lib/
    ├── gh_client.py          # shared httpx + headers + User-Agent + version pin
    ├── graphql_batch.py      # generic aliased GraphQL batch core (moved from enrich/)
    ├── audit.py              # per-request audit + SLO emit (moved from serve/)
    ├── deadlines.py          # request/clone deadlines
    └── qualify.py            # q allowlist + delta-test validation

tests/
├── contract/                 # vsearch params, pagination, pages, run detail, corpora, settings...
├── integration/              # golden-org, lifecycle, geo, executor, library, diff, models...
├── unit/                     # classifier, watermark, trees-first, planner, geo, cost, cloner...
├── golden/                   # route snapshots + OpenAPI pin
└── js/                       # Node helper tests (skipped without Node)
```

**Structure Decision**: single-project service layout (scheduler/limiter/discover/store/hydrate/enrich/serve/lib) under `src/` with contract/integration/unit tests — one bounded unit per pipeline stage with DB/queue interfaces, matching the FR-001–FR-011 decomposition so each user story maps to whole vertical slices.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| Dual discovery paths (search shards + `since` scan + org enum) | No single path covers filtered queries (search), bulk coverage (`since`), and org scopes (enum) within budget | Search-only backfill is 2.8× slower with scan distortion (efficiency review C2); `since`-only can't filter |
| Redis alongside Postgres | Per-bucket distributed rate coordination across workers can't live in Postgres row locks at poll rates | In-process limiter breaks with >1 worker; PG advisory locks add contention vs Lua buckets |
| GraphQL client alongside REST | funding/discussions/sponsors/tiers have no REST equivalent; batch replaces ≥3 REST calls | REST-only leaves US3 acceptance (funding+discussions) unimplementable |

## Console additions (US4 — see `console-spec.md` / `console-plan.md`)

Additional source files, as actually built: `src/serve/pages.py` (server-rendered routes), `src/serve/executor.py` (single-worker run executor), `src/serve/library.py` (saved filters), `src/serve/diff.py` (run diff), `src/serve/corpora.py` (frozen corpora), `src/serve/quality.py` (per-run quality report), `src/serve/metrics.py` (SLO payload), `src/serve/settings.py` + `settings_spec.py` (run limits), `src/serve/system.py` (status page), `src/serve/__main__.py` (dev server), `src/serve/templates/` (base/dashboard/find/run detail/history/diff/library/corpora/settings/system + partials), `src/serve/static/` (vendored htmx + hand-rolled CSS + app.js, R46), and `migrations/versions/0003_console.py` (runs/run_items/saved_filters — ruling R38; later migrations `0004`, `0008`, `0009` amend/extend). The console completes US2 (T019–T023) and US3 (T025–T037, T051) per `console-plan.md` batches B1–B10; the thesis tracks (FR-015–FR-022) remain deferred and parked.
