# Consolidated Feasibility Review: Three Runtime Plans

> Review of `2026-10-09-transport-pipeline-efficiency.md` (Theme 1),
> `2026-10-09-quota-limiter-utilization.md` (Theme 2), and
> `2026-10-09-adaptive-rate-controller.md` (Theme 3) against the working tree and
> `design/runtime-audit-and-adaptive-control.md`. Verification date: 2026-10-09.
> No code was implemented; only surgical plan edits (9) plus this review were written.

**Verification method:** every file:line anchor was re-read in the working tree; the
migration chain was enumerated; `pyproject.toml` gates were read; test helpers and
fixtures named by the plans were confirmed; the audit↔graphql_batch import cycle and
the Alembic `alter_column` rendering were reproduced with the repo's `.venv`
(Python 3.12.10, httpx 0.28.1, alembic installed).

---

## 1. Verdict summary

| Plan | Verdict | One line |
|---|---|---|
| Theme 1 — transport/pipeline efficiency | **Feasible as-is** | All anchors correct; no conflicts that need plan changes; lands first because it edits the two files Themes 2/3 build on. |
| Theme 2 — quota/limiter utilization | **Feasible with revisions** | Refs correct and migration sound; 3 defects fixed in-plan (circular import, pause-writing conflict, missed docs/template sync); one open default decision (`RunnerConfig.graphql_batch_size`). |
| Theme 3 — adaptive controller | **Feasible with revisions** | Hard parts (live gate, re-slicing, pause, ceiling) verified sound; 2 fixes applied (403 test vs Theme 2 message preservation; phase-1 ceiling wording); requires a deliberate rebase over Themes 1+2. |

**Overall: yes, all three can be implemented.** Two defects were real blockers and are
now fixed in the plans (they would only bite when the plans land together, which is the
plan). One prerequisite: the six in-flight working-tree modifications must be
**committed** first — all three plans' line references are written against that exact
tree, and stash-and-drop would invalidate every anchor.

---

## 2. Conflict matrix (shared files → regions → merge order)

The operator brief overstates overlap in one place: **Theme 1 does not modify
`src/serve/runner.py`** (it only references it); runner.py is shared by Themes 2 and 3.
And Theme 3 does not modify `src/lib/audit.py`; audit.py is shared by Themes 1 and 2.

| File | Plans | Exact regions | Merge order / notes |
|---|---|---|---|
| `src/lib/graphql_batch.py` | T1, T2, T3 | T1: import line 13, `_post` parse 157-160. T2: `ParsedBatch`/`BatchStats` 45-83, `_post` 155-175 + return, fold 357-374, imports 15. T3: imports 1-16, `BatchStats` 62-83, dispatch loop 309-383 (whole-block replacement). | **T1 → T2 → T3.** T3 must re-apply Edit C's *shape* onto the merged loop and keep T2's `_fold_rate_limit` call after `future.result()` (T3's plan already says this; treat it as a hard checklist item). |
| `src/lib/audit.py` | T1, T2 | T1: `AuditBuffer` 136-158 + `import queue`. T2: `AuditRecord` 17-33, `record_from_response` 87-128, new `PointsLedger`, imports. | **T1 → T2.** Non-overlapping regions. T2's `PointsLedger.add` must import `rate_limit_from_payload` lazily (see §3.1). |
| `src/discover/pipeline.py` | T1, T2 | T1: constants 31, `_Worker.__init__` 208-235, `_Worker.process` 246-342, `run_search_discovery` signature 345-357 + `_Worker(...)` construction 406-418. T2: `_graphql_audit_hook` 114-132, `count_total` 186-205, `run_search_discovery` signature + hook line 359. | **T1 → T2.** Both edit the `run_search_discovery` signature (T1 adds `upsert_every_*`, T2 adds `run_id`/`ledger`); combine keyword-only params. |
| `src/serve/runner.py` | T2, T3 | T2: ceiling constant near 59-83, `_audit_hook` 152-173, `run_filter` 771-792, `_run_filter` 794-969, `make_runner` 972-978. T3: `_hydrate` 221-245, deferred warning 886-893, imports. | **T2 → T3.** Regions disjoint; T3's `_hydrate` edit assumes T2's ceiling constant exists. |
| `src/hydrate/tail.py` | T3 only | `refresh_repos_batched` 107-327. | — (T2 edits `graphql_repo.py`, not this file.) |
| `docs/environment.md` | T2, T3 | T2: 228-247. T3: row after 121, bullet near 235. | **T2 → T3**; same-region doc conflict, resolve by keeping both texts. |
| `tests/unit/test_graphql_batch_core.py` | T1, T2 | T1: append near 301. T2: import line 13, `DictAdapter.parse`, new tests, `redis` fixture + `BucketLimiter` import. | **T1 → T2.** |
| `tests/integration/test_runner.py` | T2, T3 | T2: 120-142 helper, 1238-1246, 1273-1281, 1503-1522. T3: imports + appended tests. | **T2 → T3.** |
| `tests/integration/test_audit_db.py` | T1, T2 | T1: imports, 109-128 rewrite, append after 147. T2: `_record` 25-52 extension, append. | **T1 → T2.** Non-overlapping. |
| `src/limiter/buckets.py`, `classifier.py` | T2 only | — | — |
| `src/lib/gh_client.py` | T1 only | 51-52. | — |
| `src/store/models.py`, `settings.py`, `settings_spec.py`, `serve/app.py`, `hydrate/graphql_repo.py` | T2 only | — | — |
| `src/limiter/adaptive.py` | T3 create | — | mypy-gated package. |
| `tests/golden/snapshots/settings.json` | T2 only | regenerate via `UPDATE_GOLDEN=1`. | — |

---

## 3. Interface consistency findings

**Verified as matching across plans** (all names/signatures re-read in the tree):
`HYDRATION_CONCURRENCY_CEILING` (produced T2 Task 5, consumed T3), `run_id` on
`run_filter`/`make_runner`/`app.py`, `BatchStats.points_cost/used/remaining`,
`field_stats["points"]`, `RateLimitInfo`, `rate_limit_from_payload`,
`AuditRecord.rl_used/run_id/phase`, `PointsLedger.add(phase, body)`,
`record_from_response(..., run_id=, phase=)`, `_post` signature (unchanged in T1, T2,
and consumed unchanged by T3's hook wrapper), phase names
`"count"|"discovery"|"hydration"|"enrich"`, `fetch_batch(..., adaptive=)`,
`refresh_repos_batched(..., adaptive=)`, `adaptive_enabled()`, deferral copy
`"deferred: github graphql point reserve reached"`.

### 3.1 BLOCKER (fixed) — circular import `lib.audit` ↔ `lib.graphql_batch`

- After Theme 1 Task 2, `graphql_batch.py` imports `lib.audit` at module level (line 13:
  `from lib import audit, cancellation`).
- Theme 2 Task 3 as originally written added `from lib.graphql_batch import
  rate_limit_from_payload` at the top of `audit.py`.
- Reproduced in this Python 3.12: when `lib.graphql_batch` initializes first — which is
  what `import store.settings` does (`store/settings.py:11` imports `MAX_BATCH_SIZE`
  before `store/models` on line 12) — the cycle fails with
  `ImportError: cannot import name 'rate_limit_from_payload' from partially initialized
  module 'lib.graphql_batch' (most likely due to a circular import)`.
- **Resolution (applied):** audit.py keeps only `from dataclasses import asdict,
  dataclass, field`; `rate_limit_from_payload` is imported inside `PointsLedger.add`
  (one import per call, negligible on the audit path).

### 3.2 Conflict (fixed) — two pausers on the same Redis bucket with no max semantics

- Theme 2 Task 7 pauses the `graphql` bucket directly (`pause()` is an HSET of
  `paused_until`), with a 60 s default on silent headers.
- Theme 3 pauses the same bucket via `_pause_bucket`, which is extend-only.
- When both land, a Theme 2 default pause could *shorten* a longer Theme 3 controller
  pause on the shared key.
- **Resolution (applied):** Theme 2's `_pause_if_rate_limited` now checks
  `paused_until` and only pauses when it extends. Both pausers are now max-preserving.

### 3.3 Test vs merged behavior (fixed) — Theme 3's first-403 test

- Theme 3 Task 2's `test_first_drop_halves_the_window_and_pauses_the_pool` asserts the
  four keys recover via `fallback` (`rest-*`).
- Theme 2 Task 7 changes `_post` to `RequestFailed(403, short_message(response))`; the
  test's body says "secondary rate limit", `_is_transient` matches it, so the keys are
  **requeued** (not fallen back) and succeed on a later HTTP call → assertion fails once
  Theme 2 is merged (which the declared merge order guarantees).
- **Resolution (applied):** `max_attempts=1` on that `fetch_batch` call. Keys exhaust
  immediately and take the fallback under both pre- and post-Theme-2 behavior; the
  window/pause assertions are unchanged. Note this was caught by verification, not by
  either plan's self-review.

### 3.4 Window ceiling (fixed wording) — phase 1 is 32, not 48

- Theme 2: `cfg.concurrency = min(settings.limiter_max_concurrent,
  HYDRATION_CONCURRENCY_CEILING=32)` — always ≤ 32.
- Theme 3: `ceiling = min(MAX_WINDOW=48, cfg.concurrency)` — therefore always ≤ 32.
- The original Theme 3 self-review claimed 48 stays reachable "if the corpus preset is
  later raised" — false, because the preset cannot exceed Theme 2's constant.
- **Decision (applied):** Phase 1 ceiling = 32 (starting W = min(20, ceiling)); 40–48 is
  a later lift that requires raising `HYDRATION_CONCURRENCY_CEILING` in Theme 2, backed
  by soak evidence. `MAX_WINDOW=48` stays in the controller as the eventual bound. Both
  plans now say this.

### 3.5 Open default (not fixed) — `RunnerConfig.graphql_batch_size = 20`

- Theme 2 moves the default to 29 in `DEFAULT_BATCH_SIZE`, `RunSettings`, the corpus
  preset, and the DB server default, but `src/serve/runner.py:67` keeps
  `graphql_batch_size: int = 20` in `RunnerConfig`.
- Production always feeds it from settings (`runner_config_from`), so live runs get 29;
  direct `RunnerConfig()`/`run_filter(deps, spec)` constructions get 20. Theme 3's
  `test_run_filter_adaptive_flag_reports_controller_state` deliberately pins
  `adaptive["batch"] == 20` from a bare `RunnerConfig(concurrency=32)`.
- Changing it now would falsify that (frozen) Theme 3 test, so it is recorded as an
  operator decision (§5, low).

### 3.6 Other cross-checks that passed

- Theme 2 Task 4's end-to-end test math is sound: `count_response`/`page_response` in
  `tests/integration/test_runner.py` already embed `rateLimit { cost: 1 }`, and
  `plan_shards` with `root_count=1` makes no probes (`_fetchable` short-circuit at
  `shard_planner.py:163-167`), so `points["count"] == 1` and `points["discovery"] == 1`
  are achievable exactly.
- Theme 3's pool-wide barrier test works only because `refresh_repos_batched` wraps the
  fetch in `limiter.bound_concurrency(adaptive.ceiling)` (the test's `make_deps` cap is
  10; 20 simultaneous handler calls need the raised slot cap). The plan includes the
  wrap — keep it.
- BatchStats field merging is additive from both plans (`points_*` and
  `deferred`/`adaptive` appended after `deadline_hit`); no positional construction
  exists; `as_dict()` keys also append cleanly.
- Theme 1's `AuditBuffer` writer uses `dataclasses.asdict`, so Theme 2's new nullable
  `AuditRecord` fields flow to the (nullable) DB columns with no writer changes.
- Theme 1's new tests use only helpers that exist: `node_payload`, `client_from`,
  `DictAdapter` (core), `page_payload`, `count_payload`, `is_count`, `make_deps`,
  `scripted_client`, `shard_state`, `scalar`, `repo_node` (pipeline), and the
  `response.json` monkeypatch was reproduced successfully on httpx 0.28.1.

---

## 4. Revisions applied (9 edits, 2 files)

All are surgical; no tasks added or removed.

| # | File / section | What changed | Why |
|---|---|---|---|
| 1 | Theme 2, Task 3 Step 3 (imports) | Removed the top-level `from lib.graphql_batch import rate_limit_from_payload`; replaced with a deferred-import instruction and a note documenting the Theme 1 cycle. | §3.1 — would crash `import store.settings` in graphql_batch-first order; reproduced. |
| 2 | Theme 2, Task 3 `PointsLedger.add` code | Added `from lib.graphql_batch import rate_limit_from_payload` inside `add`. | §3.1 — makes the cycle impossible in every import order. |
| 3 | Theme 2, Task 7 `_pause_if_rate_limited` code | Pause is now conditional on `paused_until` being absent/short (extend-only). | §3.2 — prevents a 60 s default from shortening Theme 3's longer controller pause. |
| 4 | Theme 2, Task 7 note paragraph | Documented the extend-only parity with Theme 3's `_pause_bucket`. | Same as #3. |
| 5 | Theme 2, Task 8 Files line | Added `docs/environment.md:228-229`, `:234` and `src/serve/templates/settings.html:125-128`. | The plan's docs sync missed the corpus-build example table, the "default stays 20" prose, and the static corpus-note copy ("20 simultaneous requests"). |
| 6 | Theme 2, Task 8 bullets | Added bullets for `environment.md:228-229`/`:234` (29 / 32) and `settings.html:125-128` (copy → "29 repos per call, 32 simultaneous requests"). | Same as #5. |
| 7 | Theme 3, Task 2 first 403 test | Added `max_attempts=1` to the `fetch_batch` call. | §3.3 — restores the fallback pin after Theme 2 preserves the GitHub message. |
| 8 | Theme 3, Task 2 prose after the test block | Added a paragraph explaining the `max_attempts=1` pin. | Executor context; prevents someone "cleaning it up". |
| 9 | Theme 3, self-review note (f) | Corrected the claim about 48 reachability: phase 1 ceiling is 32; 40–48 requires raising Theme 2's `HYDRATION_CONCURRENCY_CEILING`. | §3.4 — the original claim was factually wrong. |

**Line-reference audit result:** every material anchor in all three plans matches the
working tree (including `runner.py:81`, `buckets.py:32/75/212`, `audit.py:99-104`,
`graphql_batch.py:13/155-156/158/212/309-380`, `pipeline.py:282/345-443`, migration
`0013`, `tests/unit/test_buckets.py:34-40/51-56/131-142/260-267`,
`test_graphql_batch_core.py:301`, `test_pipeline.py:229`, `test_audit_db.py:109-128/147`,
`test_models_migrations.py:111-128/152-161/279-299/396`, `test_runner.py:1118/1503-1522`,
`docs/environment.md:235/247`, `design/run-limits.md:11/25/34/35/48/54`,
`design/data-model.md:157`, `design/contracts/search-api.md:87`). Only ±1-line boundary
imprecision was found, none material: `pipeline.py:345-356` (signature ends 357),
`runner.py:771-793`/`:972-977`, `test_runner.py:120-153` (the batch helper is 120-142),
Theme 3's warning region `:885-893` (the warning block starts 886).

---

## 5. Required revisions not applied

| # | Severity | Issue | Recommendation |
|---|---|---|---|
| 1 | Low (decision) | `RunnerConfig.graphql_batch_size = 20` remains a third default while the system default becomes 29 (§3.5). | Leave for phase 2 review, or align it to `DEFAULT_BATCH_SIZE` and update Theme 3's `adaptive["batch"] == 20` expectation to 29 in the same phase. Do not do both halves separately. |
| 2 | Low | Theme 3's `AdaptiveController` defaults to `time.monotonic` while `fetch_batch` defaults to `time.time`; every construction site currently passes the matching clock. | Keep the global constraint "same callable as `now`" prominent at review; a future caller forgetting `now=` gets a controller whose pacer/pause math silently misreads. |
| 3 | Low | Theme 1's `AuditBuffer` writer has branches (bounded-queue backpressure, writer restart after `flush`) with no direct tests; coverage floor is global 93, so this is absorbed unless other regressions eat margin. | Add one backpressure/restart test if coverage drops near 93 in Phase 1. |
| 4 | Low | Theme 3's hard parts are only partially exercisable in unit tests: in-flight httpx overshoot after a drop, and the non-cancellable in-flight wave, are modeled only in mocked tests. | Covered by Task 4 soak watch table; do not flip the default before that evidence exists. |

No high-severity open items remain.

---

## 6. Final execution order

### Phase 0 — Baseline freeze (prerequisite; no plan tasks)

1. Commit the six in-flight working-tree modifications (`src/discover/graphql_search.py`,
   `src/hydrate/tail.py`, `src/lib/graphql_batch.py`, `src/serve/runner.py`,
   `tests/unit/test_graphql_batch_core.py`, `tests/unit/test_graphql_search.py`) as one
   commit. These are the fallback-pool/thread-safety changes every plan's line numbers
   assume. **Commit, do not stash-and-drop** — reverting them invalidates all anchors.
2. Capture baseline evidence: `pytest -q --cov=src --cov-report=term-missing`,
   `ruff check src tests`, `black --check src tests`, `mypy`.
3. Create a tag/branch at this commit.

**Gate:** working tree clean, baseline suite green; record coverage number.
**Rollback:** `43fc69a` (current HEAD) + the new baseline commit.

### Phase 1 — Theme 1, Tasks 1→5 (no schema, no settings)

Task 1 pool → Task 2 cached JSON → Task 3 discovery buffering → Task 4 audit writer →
Task 5 evidence/findings.

**Gate:** all focused tests in the plan pass; full suite with coverage ≥ 93; ruff/black/mypy
clean (`src/discover` is outside the mypy gate — note it explicitly in the evidence);
findings note written.
**Rollback:** revert the five commits (or reset to the Phase 0 tag). No DB state touched.

### Phase 2 — Theme 2, Tasks 1→8 (the speed win + one migration)

Order as written: migration/defaults → telemetry → audit fields/ledger → runner wiring →
ceiling 32 → limiter window → messages/pause → docs/full verify.
Rebase note: apply Tasks 2/3 on top of Theme 1's `_post` parse change; the ledger import
is already relocated (revision §4 #1/#2).

**Gate:** `alembic heads` shows exactly one head (`0014`); full suite + coverage; golden
snapshot regenerated (`UPDATE_GOLDEN=1 pytest tests/golden`); lint/format/mypy; docs and
template copy updated.
**Rollback:** `alembic downgrade 0013` (restores default 20) + revert the phase commits.
The 20→29 data alignment is intentionally reversed by the downgrade.

### Phase 3 — Theme 3, Tasks 1→3, then soak Task 4 (default OFF)

Rebase onto merged Themes 1+2 and re-derive line anchors (the plans' numbers are
pre-merge). In Task 2's Edit C, keep Theme 2's `_fold_rate_limit` lines and
`points_*` keys in the new loop — this is the single riskiest merge point.

**Gate 1 (flag off):** full suite green; a run's
`field_stats.graphql.hydration.adaptive == {}` and all Theme 1/2 tests unchanged.
**Gate 2 (soak):** bounded live run with `GITCRAWL_ADAPTIVE=1` against the Task 4 watch
table; findings note with keep-off/flip decision.
**Rollback:** unset `GITCRAWL_ADAPTIVE` — production returns to the Phase 2 baseline with
zero code revert. Code revert only if Gate 1 regresses.

### Later lift (optional, after clean soak)

Raise `HYDRATION_CONCURRENCY_CEILING` from 32 → 40 (one constant in Theme 2's code) to
make the controller's 40–48 range reachable; `MAX_WINDOW=48` already exists. One clean
run at each step, per design §5.1.

---

## 7. Can all three be implemented? Minimal critical path

**Yes.** No hard blockers remain: the migration chain has a single head (`0013`) and
Theme 2's `0014` slots cleanly under it; no new dependencies; the two true cross-plan
defects (circular import, message-preservation test conflict) are fixed in the plans;
and the batch/queue/import interactions were checked against the actual source.

**Minimal critical path to "faster + safe + adaptive"** (if schedule pressure forces a
subset):

1. Phase 0 (commit in-flight work).
2. Theme 2 Tasks 1 + 5 — batch 29 + concurrency 32. This is the static speed win that
   takes the profiled 39k run from ~10 min toward the design's 4.5–6 min.
3. Theme 2 Tasks 2/3/4/6/7 — telemetry, quota ledger, window re-anchor, message
   preservation, 200-body pause (safety at the edge + the data Theme 3 needs).
4. Theme 3 Tasks 1–3 with the flag off — adaptive capability, zero behavior change
   until enabled.
5. Theme 3 Task 4 soak, then decide on the flag and the 40–48 lift.

Theme 1 is **not** on the speed critical path (spec §3.1: network is 5–10% of a run) but
it is low-risk, request-scoped, and should land first anyway so the later plans rebase
once, not twice.

---

## 8. Residual risks / uncertainties the executor must watch

1. **Base-tree drift.** If the Phase 0 commit is skipped, every line anchor in the three
   plans is suspect. Re-check anchors after each rebase, especially Theme 3's Edit C.
2. **Live-only truths.** `x-ratelimit-used` is not guaranteed on GraphQL responses;
   real 200-body rate-limit texts and pooling behavior are not unit-testable
   (`MockTransport` bypasses pooling). The Theme 3 soak and Theme 1 measurement step are
   the only real evidence.
3. **Batch 29 narrows the 10 s timeout margin** (design §5.4). Watch 502/504 counts in the
   first live run; the documented fallback is 25. The controller only exists from Phase 3.
4. **Theme 2+3 merge point.** `_fold_rate_limit`, `points_*` keys, and the adaptive gate
   live in the same loop; Gate 1 of Phase 3 must assert Theme 2's telemetry still folds
   (`outcome.stats.points_cost` present) — not just that the suite passes.
5. **Redis window re-key.** Existing hashes holding `window` are ignored after Task 6;
   the count restarts at first use until a response re-anchors to `x-ratelimit-reset`.
   Safety-net behavior only, but expect one loose window at deploy.
6. **Pool-wide pause semantics.** In-flight `httpx` calls cannot be cancelled; the
   guarantee is "no new knocks", and pause sleeps are cancellation-checked. Do not
   claim more in the findings note.
7. **Re-slicing edge.** The adaptive dispatch queue holds a single contiguous run and
   slices at dispatch; empty-after-filter slices are consumed (no infinite loop), but
   requeues are split in halves (existing precedent) — verify `test_timeout_shrinks_...`
   covers the split you actually get.
8. **Coverage floor 93** is global. Phase 1's writer-thread branches and Phase 3's
   adaptive failure paths are the likeliest margin eaters; re-run the coverage gate at
   every phase boundary, not just at the end.
9. **Open default** `RunnerConfig.graphql_batch_size` (20 vs 29) — decide once, in
   Phase 2 review, and update Theme 3's frozen expectation together if changed.
10. **Doc/template sync** now includes `settings.html` and the corpus table; the golden
    snapshot regeneration will not protect the static note copy (it is a template
    string, not a settings value).
