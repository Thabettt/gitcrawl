# gitcrawl Efficiency Audit — Report, Savings Model, and Plan Index

**Date:** 2026-10-01
**Scope:** every tracked source file (61 Python files under `src/`, all migrations, templates, static assets, configs) plus docs/tests for semantics.
**Nature:** read-only audit; findings verified against source line-by-line. No code changed.

---

## 1. How to read this report

- Section 2 gives the savings model with explicit assumptions; every number is an
  order-of-magnitude estimate derived from the measured complexity and the rate limits
  documented in `design/plan.md:27-31`.
- Section 3 is the headline resource math.
- Section 4 is the complete finding inventory (critical → low) with plan mapping.
- Section 5 is the execution plan index, order, and risk register.
- The five companion plan files contain task-by-task TDD instructions.

Rate-limit context that bounds most claims (`design/plan.md:27`, `design/spec.md:129-132`):

| Resource | Limit | Implication |
|---|---|---|
| `search` | 30 req/min/token | ~0.5 req/s; serial latency (~300 ms) is NOT the bottleneck, the budget is |
| `core` | 5,000 req/hr/token | ~1.39 req/s; hydration/trees are budget-bound, not latency-bound |
| `code_search` | 10 req/min | untouched by this work |
| Console run caps | `max_shards=10`, `max_candidates=500`, `max_hydrate=200`, `max_enrich=100` (`src/serve/runner.py:45-48`) | bounds current-run severity |
| Corpus targets | 128k thesis buckets, 1M-repo file-existence target, ~200M full hydration out of v1 | bounds scale severity |

**Key consequence:** with a single token, network phases are rate-budget-bound, so the
highest-value fixes are the ones that (a) spend budget unnecessarily, (b) burn CPU/DB on
per-item work, or (c) break at scale. Parallelism only pays off for clone wall-clock and
multi-token mode.

---

## 2. Savings model (per fix)

### S1. Discovery ID accumulation is Θ(k²) — `src/discover/pipeline.py:57,261`

`stats.repo_ids += (repo_id,)` copies the whole tuple per repo. Total pointer copies =
k²/2 = 4·k² bytes.

| k (IDs in one run) | Bytes copied | Approx. CPU | Note |
|---|---|---|---|
| 566 (golden org) | 1.3 MB | <1 ms | invisible today |
| 128k (thesis bucket) | 65 GB | ~5–15 s | per run, pure waste |
| 1M | 4 TB | ~7 min | allocator/GC pressure, OOM churn |
| 5M (dense query) | 100 TB | ~2.8 hr | run-killer |

**Fix:** local `list.append` + one `tuple()` at the end. Saving = ~100% of the above.
Plan: `2026-10-01-discovery-scheduler-efficiency.md` T1.

### S2. Unbounded `IN (...)` — `src/serve/runner.py:151,522,537`, `src/hydrate/tail.py:33`

One bind parameter per ID/name. PostgreSQL's Bind parameter count is int16 → **hard
failure above 65,535**. Below that, statement parse/plan and payload grow O(N).

- At >65,535 discovered IDs: the entire run fails after spending its full search budget.
  Fix prevents 100% loss of runs larger than the golden org by ~2 orders of magnitude.
- Memory/plan: O(N) statement, O(N) merged dict either way; chunking makes it O(batch).
- **Fix cost:** trivial. Saving is correctness + bounded memory, not wall-clock.

Plan: discovery T2.

### S3. Eager shard planner burns the scarcest budget — `src/scheduler/shard_planner.py:49-72`, `pipeline.py:210-213`

`plan()` returns a fully materialized list, so every bisection probe runs before
`max_shards` can stop anything. Probes ≈ 2L−1 for L leaves. A broad query is bisected
until every leaf is under ~1,000–4,000 results.

| Query | Root count | Leaves | API probes before first shard | Search budget @30/min |
|---|---|---|---|---|
| `language:python` (docs live measurement 34,207,993) | 34.2M | ~8,550 | ~17,100 | **~9.5 hours** |
| Narrow query with 100 leaves | — | 100 | ~199 | ~6.6 min |
| After fix (stop at `max_shards=10`) | — | — | ~30–50 | ~1–2 min |

**Fix:** `iter_plan()` generator consumed lazily by the pipeline, plus passing the
already-fetched `total_count` from `run_filter` into the planner (currently
`runner.py:501` counts, then `shard_planner.py:37` counts again — tests literally assert
the duplicate: `tests/integration/test_runner.py:164-182`).

**Saving:** up to ~9.5 CPU-hours of rate budget and ~17k requests for broad queries;
1 duplicate search request per ordinary run. This is the single largest recurring saving
because search budget is the app's limiting resource.

Plan: discovery T3.

### S4. Serve payload cache: unbounded + no single-flight — `src/serve/app.py:301,392-411`

- Memory: a `RunPayload` of ≤500 items ≈ 0.5–2 MB live Python objects. 1,000 distinct
  filter hashes ≈ 1–2 GB retained forever (TTL only gates reads).
- Thundering herd: each concurrent miss on the same `spec_hash` runs the full pipeline.
  A cold run costs ≥5 min wall-clock and ~300 rate-limited requests
  (≤100 search + 200 core + 100 trees). 5 concurrent duplicates ≈ 25 min / 1,500 requests.
- **Saving:** cap memory at a fixed LRU (e.g. 100 entries → ~200 MB), collapse
  N concurrent crawls to 1 → (N−1)×(5 min + ~300 requests) per herd.

Plan: serve T1.

### S5. Interactive requests queue behind bulk runs — `src/serve/executor.py:242,264-267`

Single FIFO worker shared by `submit` (bulk run) and `submit_call` (interactive search).
10 queued 5-minute runs ⇒ an interactive cache miss waits ~50 min. A priority lane
reduces that to at most the currently executing run (~5 min).
`_futures` (`:243,251`) also grows one entry per run forever.
**Saving:** interactive p95 latency under load from tens of minutes to one run; bounded
process memory. Plan: serve T2.

### S6. Audit: one commit + two JSON decodes per HTTP response — `serve/audit.py:88-101,120-122`

Per console run ≈ 400 responses (100 search + 200 hydrate + 100 tree; planner probes
dominate until S3 lands). Each response: 1 connection checkout + commit (≈0.5–2 ms) and
the body is decoded by the audit hook and again by the caller (`httpx` does not cache
`json()`); search pages are ~100–1,000 KB each.

- Console run: ~400 commits → 1 batch insert; ~50–200 MB duplicate decode → one parse.
  **~0.5–2 s CPU + ~0.2–0.8 s DB per run.**
- 100k-repo corpus: thousands of commits ⇒ minutes to hours of WAL/fsync saved.

Plan: serve T6 (with test updates for `test_audit_hook_exception_propagates`).

### S7. Hydration write amplification — `src/store/lifecycle.py:31-95`

Per refreshed repo: 3 transactions / 8–9 statements; the ETag is written in a **second**
UPDATE of the row just upserted; rename history does SELECT-then-INSERT.

- 200 repos/run: ~400 extra transactions and ~1,000 extra statements.
  **~0.3–1 s per run** locally; 3–5× that against a networked DB.
- 100k-repo full tail: ~300k fewer transactions ⇒ **5–15 minutes per pass**.
- Second row version per refresh ⇒ extra HOT/WAL and index churn.

Plans: store T1/T2.

### S8. Geo resolution N+1 + serial — `src/enrich/geo_resolver.py:729-790`, `serve/runner.py:256-320`

Per owner: 1 SELECT and (on miss) 1 upsert, each opening its own connection; no process
memo even though location skew is extreme (thousands of "London"/"San Francisco").
~150 unique owners per console run ⇒ ~300 round trips; with batching/memo ⇒ ~4.
**Saving:** ~0.2–1.5 s per run locally; at 100k hydrated owners, minutes to tens of minutes.
Plan: enrich T2.

### S9. Tree pattern matching O(patterns × paths) — `src/enrich/trees_first.py:30-33`

`match()` runs `fnmatchcase` over the full path frozenset per pattern. F ≤ GitHub's
100k-entry recursive-tree cap.

- Typical repo (F≈2k, P=5): ~10k fnmatch ≈ 10–20 ms/repo ⇒ 1–2 s per 100-repo enrich.
- Large monorepo (F≈50k, P=5): ~250k fnmatch ≈ 0.25–1 s/repo ⇒ 25–100 s per run.
- **Saving:** ~1–20 s typical, 25–100 s pathological; literal patterns (Dockerfile-style)
  become O(1) set lookups. Plan: enrich T1.

### S10. Clone phase: serial, no timeout, quadratic progress — `enrich/cloner.py:187-212`, `serve/runs.py:183-185`, `partials/clone_progress.html:2,13`

- Serial clones: 1,000 repos × 1–3 s = 17–50 min wall-clock; with 8 workers ≈ 2–7 min
  (GitHub/disk-bound; not rate-limited because clones do not consume API budget).
- Unguarded `progress.errors` + full-document rewrite per emit + 1 s polling of all
  errors: with n=1,000 failures ≈ **300 MB disk writes** and ~50–150 MB network/DOM churn;
  capped + throttled ⇒ <1 MB. Plan: enrich T4, frontend T1.

### S11. GraphQL split discards successful work — `enrich/graphql_batch.py:213-245`

A failing 20-repo batch can issue 1+2+4+8+16 = 31 POSTs (budget: core 5,000/hr) and if any
leaf raises, all partial results are dropped, then the caller requeues the whole set.
**Saving:** up to ~31× request amplification removed and paid-for results retained.
Plan: enrich T3.

### S12. Export / diff / replay materialize whole runs — `serve/runs.py:74-161`, `serve/diff.py:54-78`

At console scale (≤500 items) this is ~1–2 MB and milliseconds — negligible. At
100k-item runs (API/CLI/import path): diff holds 2 dicts + 3 sets + 3 sorted lists
(~200–400 MB transient); export peaks at 3–4× the bundle (150–300 MB) and ~1–3 s CPU.
Streaming/SQL-diff ⇒ constant KB memory.
Also fixes a real bug: `run_items.repo_id` is nullable (`0004`), and `diff_runs` keys on
`None` → silent collapse or `TypeError`. Plan: serve T5 (streaming) and report item L2.

### S13. Index mismatch and deep OFFSET — `migrations/0003_console.py:95`, `serve/pages.py:397-415`

`run_items_stars_idx` is `(run_id, stargazers DESC)`, but pages order
`stargazers DESC NULLS LAST` (Postgres DESC defaults to NULLS FIRST) ⇒ every table page
sorts the run; `runs.py:91` orders differently again. At 500 items this is 1–3 ms; at
100k items ~50–150 ms/page plus O(offset) scanning. **Saving:** ~1–2 ms index range reads
at scale, plus consistent ordering. Plan: serve T4.

### S14. Background polling — `partials/status.html:1`, `partials/clone_progress.html:2`

Each open run tab = 1 request + 2 queries every 2 s = **14,400 requests + 28,800 queries
per 8-hour abandoned tab**; a clone tab adds ~28,800 requests. Five forgotten tabs
overnight ≈ 70k–140k pointless HTTP requests and 140k–290k SQL queries.
**Saving:** essentially 100% of that with visibility-gated triggers. Plan: frontend T2.

### S15. Smaller recurring wins

| Fix | Current | Saving |
|---|---|---|
| Static assets revalidate every navigation (`base.html:21-23`) | 3× 304 per nav | ~90 ms/nav, more on high RTT |
| `j`/`k` rescans all rows per keypress (`app.js:89-120`) | O(rows) + layout | 10–30 ms → ~1 ms on 8k-row diff pages |
| Clone estimate full scan per run page (`runs.py:205-216`, `cloner.py:124`) | O(run) rows + join + sort | 5–20 ms at console, 1–3 s + ~50 MB at 100k |
| Run page duplicate queries (`pages.py:746-772`) | ~10 statements | ~4–5 statements |
| Registry checked after DB (`runs.py:230-233`) | 1 query/poll | 0 for active clones |
| Health check spawns threads + new Redis client (`pages.py:59-88`) | 2 threads + TCP, ≤0.5 s | cached, connection-reused |
| Purge tombstones seq-scans `repos` (`lifecycle.py:123`) | O(table) delete + FK work | index-batched delete |
| Bootstrap COPY does CREATE/DROP + 2 counts per 1k rows (`upserts.py:398-425`) | 10k DDL pairs at 10M rows | catalog churn removed |
| TransportError aborts tail refresh (`gh_client.py:140-147`) | 1 blip kills a run | jittered retry |
| Owner/full-name conflict loops (`upserts.py:250-273`) | per-row statements | batched VALUES update |
| Owner login collision in one batch (`upserts.py:215-249`) | unique violation | tokenized loser |

### S16. Explicit non-savings / honest caveats

- **Single-token rate limits dominate wall-clock.** Serial hydration/enrich phases are
  trading latency for budget; with `core=1.39 req/s` a 200-repo hydrate floor is ~144 s
  even with perfect parallelism. Parallelism is only planned where it pays (clones,
  mirrors, multi-token future).
- **Estimates are analytical, not measured.** The repo has no performance harness; plans
  add operation-count/perf-guard tests where they are deterministic.
- **Latent findings:** GraphQL batching, mirrors, tiering and the retry queue are built
  but unwired (`docs/development-log.md` R24/R57). Their fixes are still planned because
  wiring is roadmap, but they save nothing until wired.
- **Per-process state:** cache, executor and clone registry are per-process; multi-worker
  deployments multiply work until S4/S5 land.

---

## 3. Headline resource math

**Per console cold run (current caps, single token):**
~5–20 s CPU+DB saved (audit S6, hydration S7, geo S8, trees S9, duplicate count S3,
cache single-flight S4), plus one ~9.5-hour search-budget disaster removed for broad
queries (S3), plus interactive p95 waits cut from tens of minutes to one run length (S5).

**Per 100k-repo corpus run:** ~10–20 s from ID accumulation (S1), minutes from hydration
transactions (S7), minutes from audit commits (S6), bounded memory in export/diff (S12),
and the planner fix protects the entire search budget (S3).

**Per 1M-repo run:** ~7 minutes of pure tuple copying removed (S1); the run no longer
hard-fails on the 65,535-parameter limit (S2); no 150–300 MB peaks per export/diff (S12).

**Per clone run:** 17–50 min → 2–7 min with 8 workers (S10); failure storms lose
~300 MB of disk writes and ~100 MB of transfer, and the browser stops re-rendering a
growing error list every second.

**Per abandoned browser tab per day:** ~43k HTTP requests + ~86k SQL queries removed (S14).

---

## 4. Finding inventory → plan map

Severity: **C** = superlinear/unbounded/hard failure at realistic scale; **H** = significant
hot-path waste; **M/L** = bounded or micro.

| # | Sev | Finding | Location | Plan |
|---|---|---|---|---|
| 1 | C | Θ(k²) tuple accumulation | `discover/pipeline.py:57,261` | discovery T1 |
| 2 | C | Unbounded `IN` over discovered IDs (65,535 hard fail) | `serve/runner.py:151,522,537` | discovery T2 |
| 3 | C | Unbounded `IN` for ETags | `hydrate/tail.py:33` | discovery T2 |
| 4 | C | Payload cache unbounded, no single-flight | `serve/app.py:301,392-411` | serve T1 |
| 5 | C | One FIFO worker for interactive + bulk; `_futures` leak | `serve/executor.py:242,243,264-267` | serve T2 |
| 6 | C | Export 3–4× buffering | `serve/runs.py:111-161` | serve T5 |
| 7 | C | Diff full materialization + NULL `repo_id` bug | `serve/diff.py:54-78` | serve T5 (bug), report L2 |
| 8 | C | Clone progress quadratic disk/network/DOM | `enrich/cloner.py:202`, `serve/runs.py:185`, `clone_progress.html:13` | enrich T4 / frontend T1 |
| 9 | C | Geo N+1, no memo/batch | `enrich/geo_resolver.py:729-790` | enrich T2 |
| 10 | C | Tree match O(P×F) | `enrich/trees_first.py:30-33` | enrich T1 |
| 11 | C | Hydration 3 txns/repo, ETag double-write | `store/lifecycle.py:31-95` | store T1 |
| 12 | C | Cross-connection per-owner writes | `serve/runner.py:256-274` | enrich T2 |
| 13 | H | Eager planner spends all probes | `scheduler/shard_planner.py:49-72` | discovery T3 |
| 14 | H | Duplicate root `count_total` per run | `serve/runner.py:501` → `shard_planner.py:37` | discovery T3 |
| 15 | H | Audit commit per response + double JSON decode | `serve/audit.py:88-101,120-122` | serve T6 |
| 16 | H | GraphQL split discards partials, 31 POSTs | `enrich/graphql_batch.py:213-245` | enrich T3 |
| 17 | H | Mirrors 3 serial GETs, no conditional cache | `enrich/mirrors.py:107-181` | enrich T5 |
| 18 | H | Segment bucket O(N·S), list shifts | `enrich/segment_executor.py:88-91,100,119` | enrich T5 |
| 19 | H | Serial clones, no timeout | `enrich/cloner.py:187-198` | enrich T4 |
| 20 | H | Run page ~10 queries, estimate full scan | `serve/pages.py:746-772`, `runs.py:196-216` | serve T3 |
| 21 | H | `_same_hash_runs` unbounded | `serve/pages.py:210-231` | serve T4 |
| 22 | H | Registry checked after DB per poll | `serve/runs.py:223-250` | serve T3 |
| 23 | H | Index/order mismatch; deep OFFSET | `0003_console.py:95`, `pages.py:397-415` | serve T4 |
| 24 | H | Bootstrap staging DDL per chunk | `store/upserts.py:398-425` | store T3 |
| 25 | H | Purge tombstones seq scan | `store/lifecycle.py:115-123` | store T2 |
| 26 | H | TransportError never retried | `lib/gh_client.py:140-147` | store T4 |
| 27 | H | 3 Redis EVALs per request; blind denial writes | `limiter/buckets.py:133,150,186` | report L1 (deferred) |
| 28 | H | Shard queue re-creates groups; 7–16 Redis trips/shard | `scheduler/state_machine.py:212-226` | discovery T5 |
| 29 | M | `set_state` read-modify-write | `scheduler/state_machine.py:119-142` | discovery T5 |
| 30 | M | `pending.pop(0)` Θ(n) | `discover/pipeline.py:291` | discovery T4 |
| 31 | M | In-band `sleep` on limiter denial; wake stampede | `lib/gh_client.py`, `limiter/classifier.py` | report L1 (deferred) |
| 32 | M | Owner/rename per-row conflict loops | `store/upserts.py:250-273` | store T3 |
| 33 | M | Owner login collision in batch | `store/upserts.py:215-249` | store T3 |
| 34 | M | `_record_history` SELECT-then-INSERT + duplicates | `store/lifecycle.py:38-47` | store T1 |
| 35 | M | Replay/`_run_items` unbounded per request | `serve/app.py:225-271` | report L2 (bounded by caps) |
| 36 | M | Static assets revalidate every nav | `base.html:21-23` | frontend T3 |
| 37 | M | Diff page unbounded rows/options, nowrap | `diff.html:72-120`, `app.css:334-340` | frontend T3 |
| 38 | M | `j`/`k` O(rows) DOM churn | `static/app.js:89-120` | frontend T4 |
| 39 | M | Polling ignores tab visibility | `partials/status.html:1`, `clone_progress.html:2` | frontend T2 |
| 40 | M | Progress bar animates `width` (layout) | `app.css:390-394` | frontend T3 |
| 41 | M | Clone start double-submit | `static/app.js:255-292` | frontend T4 |
| 42 | M | Library re-parses/re-hashes every filter per view | `serve/library.py:43-52` | report L3 (deferred) |
| 43 | M | Health check threads + new Redis client | `serve/pages.py:59-88` | serve T3 |
| 44 | L | `_get_field` linear scan | `scheduler/state_machine.py:60-64` | discovery T4 |
| 45 | L | `response.text[:300]` decodes whole body | `lib/gh_client.py:102` | store T4 |
| 46 | L | Header linear scans | `serve/audit.py:51-55`, `limiter/classifier.py:27-31` | store T4 |
| 47 | L | `dedupe_items` unwired; within-page duplicates overcount | `store/upserts.py:171` | discovery T1 |
| 48 | L | `tiering.order_repos` double sort | `scheduler/tiering.py:30-32` | discovery T4 |
| 49 | L | `Jinja2Templates(auto_reload=True)` in prod | `serve/pages.py:56` | frontend T3 |
| 50 | L | `audit_ts_idx` missing `id DESC` tiebreak | `store/models.py:221` | report L3 (deferred) |
| 51 | L | `happy path` JSON parse in error path twice | `lib/gh_client.py:87-102` | store T4 |

**Deferred (L1–L3) rationale:** limiter EVAL folding and classifier jitter only pay at
millions of requests or multi-process herds; library re-hashing only matters with large
saved-filter catalogs; audit index tiebreak is a PG planner micro. These are listed in
the report so nothing is lost; they can become Plan 6 once measured.

---

## 5. Execution plan index

| Order | Plan | Tasks | Depends on | Why this order |
|---|---|---|---|---|
| 1 | `2026-10-01-discovery-scheduler-efficiency.md` | 5 | — | Removes Θ(k²), the 65,535 hard fail, and the budget blowup; highest value, lowest risk |
| 2 | `2026-10-01-store-hydrate-efficiency.md` | 4 | — | Write-path round trips; migration 0005 introduced here |
| 3 | `2026-10-01-enrich-efficiency.md` | 5 | — | CPU + network; independent of 1/2 |
| 4 | `2026-10-01-serve-runtime-efficiency.md` | 6 | store migration 0005 (indexes can share 0006) | Depends on audit/write-path shape; biggest surface |
| 5 | `2026-10-01-frontend-efficiency.md` | 4 | enrich T4 (error fields), serve T3 (headers) | Consumes server-side fields added earlier |

Parallelizable: 1, 2, 3 can run in separate worktrees simultaneously. 4 after 2 (shared
migrations), 5 after 3/4.

### Verification strategy

- TDD per task; each plan names the exact test file and command.
- Query/round-trip reductions are verified with SQLAlchemy `before_cursor_execute` spies
  or Redis/fake-transport call counters — not wall-clock timers (deterministic in CI).
- Two perf guards are process-time bound with 10–100× separation from the failing
  behavior (ID accumulation at 100k; tree matching at 50k paths), so they are not flaky.
- No behavior changes except explicit bug fixes: diff NULL key, duplicate history rows,
  owner-login collision, transport retry. Each has a dedicated test.

### Risk register

| Risk | Mitigation |
|---|---|
| Migration 0005/0006 on populated DBs (unique constraint, index rewrite, ACCESS EXCLUSIVE) | Dedupe before constraint; `CREATE INDEX CONCURRENTLY` outside a transaction; document lock timeouts from `design/research.md:68-71` |
| Planner generator changes probe order/plug-point | `plan()` stays list-returning; new `iter_plan()` is additive; existing tests untouched |
| Audit batching breaks `test_audit_hook_exception_propagates` | Plan updates that test to assert propagation at flush time; explicit step |
| Clone worker concurrency changes progress/emit timing | `workers` defaults to 1 (current behavior); lock-guarded emit; tests inject fake runner |
| Frontend has no JS test runner | Pure helpers extracted and tested with `node --test` (node is guaranteed in this environment); pytest wrapper skips if node is absent |
| Index order changes pinned SQL | Existing pinned tests updated in the same task; migration parity test enforces model/migration agreement |
