# Tasks: gitcrawl

**Input**: Design documents from `design/` (spec.md user stories US1–US3, plan.md structure, data-model.md entities, contracts/search-api.md)

**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, contracts/

**New here? Read `how-the-data-flows.md` first** — it explains what each phase below is doing and why, without assuming GitHub API knowledge.

**Tests**: Included per story (contract + integration first, must FAIL before implementation).

**Organization**: Grouped by user story for independent implementation, testing, and delivery.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story (US1, US2, US3)
- Exact file paths in every description

## Phase 0: Walking Skeleton (first running code)

**Purpose**: Thinnest live loop — one filter → live GitHub → display → bundle + CSV — before any depth. Locked first milestone.

- [ ] T000 Single hardcoded filter (`language:rust stars:>100`, no enrichment) through validate → live search (≤3 pages) → display → run bundle JSON + CSV export in `src/skeleton.py`; manual run + recorded output is the done proof

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project init, stack, gates that everything else assumes

- [ ] T001 Create project structure per plan.md (`src/scheduler,limiter,discover,store,hydrate,enrich,serve,lib`, `tests/contract,integration,unit`, `docker-compose.yml` for Postgres 17 + Redis 7)
- [ ] T002 Initialize Python 3.12+ project with pinned deps (FastAPI, httpx, SQLAlchemy 2, Alembic, redis-py, pydantic v2, PyYAML, pytest) + `X-GitHub-Api-Version` + `User-Agent: gitcrawl/…` defaults in `src/lib/gh_client.py`; tokens via env (`GITHUB_TOKEN`/`GITHUB_TOKENS`) only, never hardcoded, fingerprint-only logging (1 PAT dev/golden-org, 5–10 for 100k+ runs)
- [ ] T003 [P] Configure linting/formatting (ruff + black) and pytest layout per plan.md
- [ ] T004 [P] Write ToS/legal gates checklist (`robots.txt` check, token-ownership log, resale-review trigger, trademark naming) in `docs/legal-gates.md` per spec FR-011

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Rate limiter, qualifier validation, shared GitHub client, base models — MUST complete before ANY user story

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [ ] T005 Implement Redis per-bucket limiter in `src/limiter/buckets.py` (search/core/code_search HASH+Lua token buckets O(1) — never ZSET logs; `x-ratelimit-*` pacing, per-bucket pausing only, ~10 concurrent/endpoint/token ceiling, 0.5–1ms same-AZ budget) + delayed-retry via sorted sets in `src/limiter/retry.py`
- [ ] T006 Implement throttle classifier in `src/limiter/classifier.py` (retry-after → reset → `60s×2^n`+jitter max 5; spam-422 backoff vs cap-422 shard vs validation-422 fix; SSO `partial-results` loud-fail)
- [ ] T007 [P] Implement `q` allowlist + delta-test validation in `src/lib/qualify.py` (06 §2 set, `props.*`-only-with-`org:`, local `400`, CI delta test)
- [ ] T008 Create base models + Alembic migrations in `src/store/models.py` (repos PK `id`, owners, full_name_history, shards, audit_log, geo_cache per data-model.md)
- [ ] T009 Configure audit logging + SLO metric emit in `src/serve/audit.py` (FR-010 fields; `search_remaining`, `incomplete_results_ratio`, `422/403+429` rates, p95, coverage, geo-unmatched)

**Checkpoint**: Foundation ready — `pytest tests/unit/test_classifier.py tests/unit/test_watermark.py` green; limiter + qualify + models reviewable; user stories can now begin

---

## Phase 3: User Story 1 - Discover every matching public repo (Priority: P1) 🎯 MVP

**Goal**: Sharded search + `since` backfill + org enumeration into Postgres with `id`-dedupe and cap-422 auto-shard

**Independent Test**: Golden-org `org:github` run → stored `id`-set diff vs independent `since` sample shows parity; `tests/integration/test_golden_org.py` passes alone

### Tests for User Story 1 ⚠️

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [ ] T010 [P] [US1] Contract test for pagination/Link-follow + cap-422 auto-shard in `tests/contract/test_github_pagination.py`
- [ ] T011 [P] [US1] Integration test for golden-org `id`-set parity in `tests/integration/test_golden_org.py`

### Implementation for User Story 1

- [ ] T012 [P] [US1] Implement shard planner (created:/stars: bisect to <1000 fetchable and <<4000 scanned) in `src/scheduler/shard_planner.py`
- [ ] T013 [P] [US1] Implement shard state machine (pending/active/done/incomplete, within-run only) + within-run ordering + Redis Streams queue (sharded lanes `hash%N` for per-repo order, PEL-history drain before `>`, `XAUTOCLAIM` reaper + DLQ after 3 tries, PEL-size monitor; NO hot/cold tiers) in `src/scheduler/state_machine.py` and `src/scheduler/tiering.py`
- [ ] T014 [US1] Implement sharded search discovery (`per_page=100`, `Link: rel="next"` verbatim, `incomplete_results` → narrow-once + mark) in `src/discover/search_shards.py` (depends on T012)
- [ ] T015 [US1] Implement `since` cursor scan + ID-range sharding + `max(id)` checkpoint in `src/discover/since_scan.py`
- [ ] T016 [US1] Implement org/user enumeration (`/orgs/{org}/repos`, `/users/{u}/repos`) in `src/discover/org_enum.py`
- [ ] T017 [US1] Implement batched upserts (`INSERT ... ON CONFLICT (id)` with app-side no-op gating first — `WHERE` still writes WAL+locks — 500–1000/batch, `COPY` bootstrap path (TEXT + `ON_ERROR ignore + REJECT_LIMIT`; PG18 forbids ON_ERROR in BINARY and binary silently truncates out-of-range ints — ruling R23) per-batch `UNLOGGED` staging) in `src/store/upserts.py`
- [ ] T018 [US1] Wire US1 pipeline + logging (query hash, `total_count`/`incomplete_results`, token fingerprint, latency)

**Checkpoint**: US1 fully functional and testable independently — golden-org parity green

---

## Phase 4: User Story 2 - Live current-state with lifecycle (Priority: P2) [LIVE-ONLY]

**Goal**: Within-run current-state at `ran_at` + rename/delete handling. No watermarks, tiers, backfills, or tails.

**Independent Test**: Seeded fixtures, single run, no clock advance: current `pushed_at` present, renames collapse to one `id`, deletes become tombstones — `tests/integration/test_lifecycle.py` passes alone

### Tests for User Story 2 ⚠️

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [ ] T019 [P] [US2] Within-run dedupe unit tests (`id`-dedupe across overlap pages, current `pushed_at` recorded, no pushed/updated mixing) in `tests/unit/test_watermark.py` (file name kept; watermark = `ran_at` snapshot, not a persistent cursor)
- [ ] T020 [P] [US2] Lifecycle integration test (301 rename, 404 tombstone, retention purge) in `tests/integration/test_lifecycle.py`

### Implementation for User Story 2

- [ ] T021 [US2] Implement live within-run refresh in `src/hydrate/tail.py` (current state at `ran_at`; NO `pushed:>watermark-1h` poller, NO tier cadences)
- [ ] T022 [US2] Implement hydrate client (`GET /repos/{o}/{r}` + ETag on stable URLs only, never search path) in `src/hydrate/repo_client.py`
- [ ] T023 [US2] Implement lifecycle handler (301 follow + history row, 404 tombstone + quarantine, retention purge) in `src/store/lifecycle.py`
- [ ] T024 [US2] CUT (live-only) — GH Archive hourly tail complement REMOVED; `src/enrich/mirrors.py` keeps ecosyste.ms/deps.dev bootstrap only

**Checkpoint**: US1 AND US2 work independently — live run reflects current state with lifecycle proven on fixtures

---

## Phase 5: User Story 3 - Enrich + serve with virtual params + owner country (Priority: P3)

**Goal**: Cost-aware enrichment, geo resolver, and `GET /vsearch/repos` serve contract

**Independent Test**: 3 fixture repos → `GET /vsearch/repos?has_dockerfile=true&owner_country=DE` filters correctly with geo tiers — contract tests pass alone

### Tests for User Story 3 ⚠️

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [ ] T025 [P] [US3] Contract tests for allowlist/`400` + virtual-param translation in `tests/contract/test_vsearch_params.py`
- [ ] T026 [P] [US3] Trees-first equivalence test in `tests/unit/test_trees_first.py`
- [ ] T027 [P] [US3] Geo resolver fixture tests (flag/city/multi/unmatched) in `tests/integration/test_geo_resolver.py`

### Implementation for User Story 3

- [ ] T028 [P] [US3] Implement trees-first enrichment (1× `trees?recursive=1` / ecosyste.ms metafiles; `contents` for bytes only) in `src/enrich/trees_first.py`
- [ ] T029 [P] [US3] Implement GraphQL batch (funding/discussions/sponsors, `first≤50`, ≤10–20 aliases/query shallow-only, `dryRun` cost gate, split-on-timeout never retry-same-shape) in `src/enrich/graphql_batch.py`
- [ ] T030 [P] [US3] Implement geo resolver (flag decode → normalize/split → gazetteer → aliases → cached geocoder → tiebreakers → `{country_iso, confidence, raw}`) in `src/enrich/geo_resolver.py` per `06 §6.1`
- [ ] T031 [P] [US3] Implement mirror enrichers (ecosyste.ms polite-pool `?mailto=` + `POST /packages/bulk_lookup`, deps.dev batch endpoints first, releases/commits/issues/SBOM/OSV/Scorecard-weekly — `criticality_score` bulk is dead — per `06 §6` cost notes) in `src/enrich/mirrors.py`
- [ ] T032 [US3] Implement FastAPI `GET /vsearch/repos` + virtual-param translation + upstream allowlist in `src/serve/app.py` and `src/serve/virtual_params.py` per `contracts/search-api.md` (depends on T028–T031)
- [ ] T033 [P] [US3] Implement filter-spec v1 schema validation (version gate, qualifier allowlist + typo hints, virtual table check, no tokens/state accepted) in `src/serve/filter_spec.py`
- [ ] T034 [P] [US3] Implement run/replay/export (`POST /vsearch/run` → `{filter_hash, ran_at, api_version, ...}`, `GET /vsearch/runs/{filter_hash}`, `GET .../export` run bundle with raw upstream JSON) in `src/serve/runs.py` (depends on T033)
- [ ] T035 [P] [US3] Implement filter form page (`GET /vsearch/`: every `06`-matrix group incl. virtual section, `props.*` gated on single-`org:`, builds identical filter-spec on submit, upload-JSON control, download-as-JSON button) in `src/serve/templates/filters.html` + form handler in `src/serve/app.py`; parity test (form ≡ equivalent JSON upload) in `tests/contract/test_filter_form.py`
- [ ] T036 [P] [US3] Implement cost-ordered filter planner (cheap-first ordering, per-field source priority table per research D12, lazy depth: visible-page vs full-export) in `src/enrich/cost_planner.py`
- [ ] T037 [P] [US3] Implement segment executor (id-range segments × tokens, survivors-first ordering, dead-lane re-queue, per-field source + calls-spent reporting into run metadata) in `src/enrich/segment_executor.py` (depends on T036)
- [ ] T051 [P] [US3] Implement optional clone action (count selector top-N, shallow/file-only/windowed modes into `clones/{run_hash}/`, pre-confirm disk estimate + low-disk warning, resumable skip-completed, progress in run metadata; zero clones valid) in `src/enrich/cloner.py` + estimate/progress endpoints in `src/serve/runs.py` (numbered out of sequence to avoid renumbering T038–T050)

**Checkpoint**: All user stories independently functional — serve returns enriched + geo-tiered results from seeded data

---

## Phase 6: Thesis Tracks (corpus, traces, validation) [DEFERRED — do not build until US1–US3 live slice is green]

**Purpose**: Thesis-grade depth on top of US1–US3 (FR-015–FR-022). Each track independently testable on fixtures; build in listed order (corpus first per timeline pressure). Deferred per live-only decision; needs external `thisisjustthebeginning` thesis repo (not in this repo) before T039–T045.

- [ ] T038 Implement corpus-frame config in filter-spec (buckets, attrition rules, size splits, study window, ≥10-star option) in `src/serve/filter_spec.py` + validation tests in `tests/contract/test_corpus_frame.py`
- [ ] T039 [P] Implement versioned trace-pattern packs (authors/files/branches/labels/bots, freeze command, generic weights, CONVENTIONS exclusion, per-agent attribution) in `src/enrich/trace_packs.py`; pack-freeze test in `tests/unit/test_trace_packs.py`
- [ ] T040 [P] Implement windowed commit-history builder (messages/authors/trailers/diffstat, merge-revert-bump + bot filters, adoption-date + ratios; BigQuery primary, API top-up) in `src/enrich/commit_history.py`
- [ ] T041 [P] Implement PR channel hydration (branch/label match, merge-type mapping, outcome/iterations/reverts, Codex rule; GraphQL bulk honoring 10k cap) in `src/enrich/pr_channel.py`
- [ ] T042 [P] Implement file inventory (file list + .gitignore scan, one-path-per-line exports, per-file stats, matched-file checkout, two-clone strategy) in `src/enrich/file_inventory.py`
- [ ] T043 [P] Implement crates.io linkage (package cross-ref, downloads/yanked/versions, Cargo/workspace parse, hallucination check) in `src/enrich/crates_link.py`
- [ ] T044 [P] Implement snowball expansion (user-search seeding, followers/following frontier + dedup, org-member expansion with exclusions, bulk import, fallback run config) in `src/enrich/snowball.py`
- [ ] T045 Implement validation harness (stratified sampler default n=400, double-label queue + Cohen's κ, Wilson CIs, Chapman capture-recapture matrix, pre-registration freeze) in `src/enrich/validation.py`

**Checkpoint**: Thesis tracks functional on fixtures — frozen pack + windowed history + PR/file channels + crates + snowball + validation all independently testable

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Hardening that touches multiple stories

- [ ] T046 Documentation updates (README runbook pointers, `docs/legal-gates.md` review notes)
- [ ] T047 [P] Additional unit tests for classifier/watermark/trees-first edge cases in `tests/unit/`
- [ ] T048 Performance pass: batch sizes, per-table autovacuum tuning (OFF during load + `VACUUM ANALYZE`), `CONCURRENTLY` index builds (+ per-partition attach), `pg_cron`+`pg_partman` retention, pool sizing (`(cores×2)+1`, PgBouncer txn, `stmt_cache_size=0`), migration `lock_timeout=50ms`/`statement_timeout=5s`, `COPY` bootstrap validation
- [ ] T049 Security hardening: token vault/OIDC, secret-scanning, least-privilege fine-grained PAT review, Dependabot
- [ ] T050 Run `quickstart.md` validation end-to-end (golden org → lifecycle → serve → geo) and record evidence

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Setup — BLOCKS all user stories
- **User Stories (Phase 3+)**: All depend on Foundational; then parallelizable (or P1 → P2 → P3 sequentially)
- **Thesis Tracks (Phase 6)**: Depend on Foundational + US1 data shape; build in listed order (corpus frame first per timeline pressure); testable on fixtures independently
- **Polish (Phase 7)**: Depends on all desired stories complete

### User Story Dependencies

- **US1 (P1)**: After Foundational — no story dependencies
- **US2 (P2)**: After Foundational — needs US1 data to hydrate, but lifecycle logic is independently testable on fixtures (no tail, no background jobs)
- **US3 (P3)**: After Foundational — needs stored rows to enrich/serve, but translation/geo/trees-first independently testable on fixtures

### Within Each User Story

- Tests MUST be written and FAIL before implementation
- Planner/state before discovery; discovery before upserts
- Enrichment translators before serve endpoints
- Story complete (checkpoint green) before next priority

### Parallel Opportunities

- All `[P]` setup/foundational tasks run in parallel (different files)
- After Foundational, US1/US2/US3 can proceed in parallel if staffed
- All tests within a story marked `[P]` run in parallel
- T012/T013, T028–T031 model/translator tasks run in parallel

## Implementation Strategy

### MVP First (locked order)

0. Complete Phase 0: Walking Skeleton (first running code — locked first milestone)
1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL)
3. Complete Phase 3: US1
4. **STOP and VALIDATE**: golden-org `id`-set parity
5. Deploy/demo if ready

### Incremental Delivery

1. Setup + Foundational → foundation ready
2. + US1 → Test independently → Deploy/Demo (MVP!)
3. + US2 → Test independently → Deploy/Demo
4. + US3 → Test independently → Deploy/Demo

### Parallel Team Strategy

1. Team completes Setup + Foundational together
2. Then Developer A: US1 · Developer B: US2 · Developer C: US3
3. Stories integrate via DB/queue interfaces only

## Notes

- Never forward unknown params upstream; never treat `total_count` as ground truth; never ETag the search path
- Commit after each task or logical group; stop at any checkpoint to validate
- Re-verify quarterly: API version pin, star/pricing/version numbers, `robots.txt`
