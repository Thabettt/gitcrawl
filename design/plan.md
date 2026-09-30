# Implementation Plan: gitcrawl

**Branch**: `001-gitcrawl` | **Date**: 2026-09-29 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `design/spec.md` (US1 discover P1 · US2 freshness P2 · US3 enrich+serve P3), grounded in `findings/00–06`.

**Note**: Filled per speckit plan workflow; user overrode default `specs/` location with `design/`.

## Summary

Build the SEART-pattern crawler hardened per the `05` gaps + efficiency review, live-only (entire job runs on the go at `ran_at`, no background polling): dual-path discovery (sharded REST search for filtered queries + `since`-enumeration/org-listing for bulk), Postgres on immutable `id`, Redis per-bucket limiter, Python FastAPI workers, within-run lifecycle (301 follow / 404 tombstone, no watermarks/tiers/GH Archive tail), cost-aware enrichment (trees-first, GraphQL-batch-only, ecosyste.ms zero-burn), owner-country resolver (`06 §6.1`), and a virtual-params serve API — with audit logging, SLOs, and legal gates from day one. SQLite for skeleton/laptop; Postgres 17 + Redis 7 for scale. New to the system? Read `how-the-data-flows.md` before this plan.

## Technical Context

**Language/Version**: Python 3.12+ (replication package requires 3.12+)

**Primary Dependencies**: FastAPI (serve + workers API), httpx (GitHub REST/GraphQL clients), SQLAlchemy 2 + Alembic (Postgres), psycopg3 (pipeline mode for OLTP writes, `COPY BINARY` for bootstrap; `stmt_cache_size=0` under PgBouncer), redis-py (token buckets + Streams queue), pydantic v2 (contracts/validation), pg_partman + pg_cron (partition retention), PyYAML + exact version pins to be locked at setup

**Storage**: SQLite for walking skeleton + laptop use (runs/bundles/cache); PostgreSQL 17 for scale (primary: `repos`, `full_name_history`, `owners`, `shards`, `audit_log`, `geo_cache`); Redis 7 (rate buckets, shard queue, ETag cache) at scale; PgBouncer transaction mode in front of Postgres; object/blob storage explicitly N/A for v1 (no clone corpus by default)

**Testing**: pytest (+ httpx MockTransport contract tests, golden-org integration tests, `id`-set diff harness) — OVERCOVERAGE MANDATE APPLIES (see Testing Overcoverage Mandate below; test:app ~2:1 accepted)

**Target Platform**: Linux server / Docker Compose (single-host MVP, horizontally scalable workers)

**Project Type**: service (scheduler + workers) with CLI runbook (`quickstart.md`)

**Performance Goals**: live-only serve (~10–20s first Find: bounded search + visible-page enrich; cached repeats fast); discovery at dense-search rate (≤1,800 search req/hr/token, 100/page); hydration ≤5,000 core req/hr/token; no background tails — all budget spent within the run

**Constraints**: search 30/min/token, core 5,000/hr/token (no token-sharing to evade); ~10 concurrent per endpoint per token (900 pts/min math); 1000-fetchable + ~4000-scan per query; `q` 256-char/5-op caps; API-only default (HTML fallback needs legal review + `robots.txt` check); GDPR deletion purge window; PII minimization (no commit emails stored)

**Scale/Scope**: v1 validates on golden org (`org:github`) to `id`-set parity; production target is filtered cross-GitHub coverage in single on-the-go runs (unfiltered 200M-repo full hydration explicitly out of v1 — bootstrap via ecosyste.ms/`since` sampling instead). Tokens via env (`GITHUB_TOKEN`/`GITHUB_TOKENS`, never hardcoded): 1 PAT for dev/golden-org, 2+ for dev loop, 5–10 for 100k+ corpus runs.

## Testing Overcoverage Mandate (NON-NEGOTIABLE)

- Every testable path MUST be tested: happy path, error paths, edge cases, boundary values, empty/null/malformed inputs, rate-limit/retry branches (`403/429/422/304/301/404`), pagination caps, tombstone/rename flows, cache hit/miss, allowlist `400` rejections. No slack.
- Each `src/` module MUST have a dedicated test file covering all branches (branch coverage 100% required, not just line coverage). Uncovered branch = build failure.
- Test:app ratio up to ~2:1 is explicitly accepted and expected. Prefer explicit duplicated tests over clever parametrization that hides paths.
- Contract tests MUST assert every row of `contracts/search-api.md` translation/error tables; unit tests MUST assert every classifier/watermark/validator branch; integration tests MUST assert golden-org parity + lifecycle fixtures.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- No existing constitution file in repo (checked `.specify/` templates only) — proceeding on `findings/` research as the governing constraints (rate limits, ToS §H, privacy, trademark).
- Efficiency mandates from review are normative: no daily search backfill, no global all-shard poll, no search-path ETag, no `total_count` validation, trees-first enrichment, per-bucket pausing — violations require written justification in PR.
- Re-check after Phase 1: data-model + contracts must preserve `id`-PK, tombstone, allowlist, and audit-log invariants (gates for tasks.md Phase 2).

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

```text
src/
├── scheduler/
│   ├── shard_planner.py      # created:/stars: bisect to <1000 and <<4000 scanned
│   ├── tiering.py            # within-run shard ordering (cheap-first, survivors-first; no cadences)
│   └── state_machine.py      # pending/active/done/incomplete per shard (within-run only)
├── limiter/
│   ├── buckets.py            # search/core/code_search Redis token buckets
│   └── classifier.py         # retry-after → reset → exp-backoff; 422 triage
├── discover/
│   ├── search_shards.py      # sharded GET /search/repositories, Link follower
│   ├── since_scan.py         # GET /repositories?since= cursor + ID-range shards
│   └── org_enum.py           # GET /orgs+users repos enumeration
├── store/
│   ├── models.py             # repos/owners/history/shards/audit/geo_cache
│   ├── upserts.py            # batched ON CONFLICT (id) w/ no-op gating, COPY BINARY bootstrap
│   └── lifecycle.py          # 301 follow, 404 tombstone, retention purge
├── hydrate/
│   ├── repo_client.py        # GET /repos + ETag (stable URLs only)
│   └── tail.py               # live within-run refresh (current pushed_at at ran_at; no watermark poller)
├── enrich/
│   ├── cost_planner.py       # cost-ordered filter plan + per-field source priority (D12)
│   ├── segment_executor.py   # id-range segments × tokens, survivors-first, lazy depth
│   ├── trees_first.py        # trees?recursive=1 / metafiles over contents loops
│   ├── graphql_batch.py      # funding/discussions/sponsors batch (first≤50)
│   ├── geo_resolver.py       # 06 §6.1 flag→gazetteer→alias→cached-geocoder
│   ├── mirrors.py            # ecosyste.ms/deps.dev bootstrap (zero-burn; no GH Archive tail in live-only)
│   ├── trace_packs.py        # versioned author/file/branch/label patterns + generic weights
│   ├── commit_history.py     # windowed log + adoption-date + ratios (BQ primary, API top-up)
│   ├── pr_channel.py         # branch/label/outcome/iterations/reverts + Codex rule
│   ├── file_inventory.py     # file list + gitignore scan + per-file stats + two-clone strategy
│   ├── crates_link.py        # crates.io cross-ref + Cargo/workspace parse + hallucination check
│   ├── snowball.py           # seed → frontier queue → org expansion → geographic fallback
│   ├── validation.py         # stratified sampler + kappa + Chapman capture-recapture (thesis tracks end here)
│   └── cloner.py             # optional post-fetch clone (top-N, modes, estimate, resume) — T051, general tool
├── serve/
│   ├── app.py                # FastAPI GET /vsearch/repos + form + upload
│   ├── templates/
│   │   └── filters.html      # server-rendered filter form (all 06-matrix groups)
│   ├── virtual_params.py     # translation table + upstream allowlist + own 400
│   ├── filter_spec.py        # filter-spec v1 schema + corpus-frame validation (T033/T038)
│   ├── runs.py               # run/replay/export + clone estimate/progress (T034/T051)
│   └── audit.py              # per-request log + SLO metrics emit
└── lib/
    ├── gh_client.py          # shared httpx + headers + User-Agent + version pin
    └── qualify.py            # q allowlist + delta-test validation

tests/
├── contract/
│   ├── test_vsearch_params.py   # allowlist/400, virtual-param translation
│   └── test_github_pagination.py # Link-follow, cap-422 auto-shard
├── integration/
│   ├── test_golden_org.py       # org:github id-set parity vs since sample
│   ├── test_lifecycle.py        # rename 301, delete 404 tombstone
│   └── test_geo_resolver.py     # flag/city/multi/unmatched fixtures
└── unit/
    ├── test_classifier.py       # throttle 3-branch + 422 triage
    ├── test_watermark.py        # overlap + id-dedupe, no pushed/updated mix
    └── test_trees_first.py      # contents-loop → tree equivalence
```

**Structure Decision**: Single-project service layout (scheduler/limiter/discover/store/hydrate/enrich/serve/lib) under `src/` with contract/integration/unit tests — one bounded unit per pipeline stage with DB/queue interfaces, matching the FR-001–FR-011 decomposition so each user story maps to whole vertical slices.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| Dual discovery paths (search shards + `since` scan + org enum) | No single path covers filtered queries (search), bulk coverage (`since`), and org scopes (enum) within budget | Search-only backfill is 2.8× slower with scan distortion (efficiency review C2); `since`-only can't filter |
| Redis alongside Postgres | Per-bucket distributed rate coordination across workers can't live in Postgres row locks at poll rates | In-process limiter breaks with >1 worker; PG advisory locks add contention vs Lua buckets |
| GraphQL client alongside REST | funding/discussions/sponsors/tiers have no REST equivalent; batch replaces ≥3 REST calls | REST-only leaves US3 acceptance (funding+discussions) unimplementable |
