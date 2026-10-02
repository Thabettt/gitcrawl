# Overnight Queue Execution Report — 2026-10-02/03

**Status:** all four enabled phases completed. No stop condition fired; no `queue-stop.md` was written.
**Branch:** `quality-hardening` (never switched, rebased, or pushed). **Range:** `9f32d58` → `e21143e` (24 commits).
**Environment:** Postgres and Redis reachable; `TEST_DATABASE_URL` ends `_test`; tests run with `GITCRAWL_REQUIRE_TEST_DB=1` so DB tests cannot silently skip.

## Per-phase results

### Phase 1 — Unwired-debt triage (R58) — COMPLETE
Plan: `docs/superpowers/plans/2026-10-02-unwired-debt-triage.md`

- [x] Task 1 — R58 recorded in the dev-log ruling table (supersedes R24/R57 quarantine policy).
- [x] Task 2 — `reclaim_stale` wired before the discovery claim loop; regression test added; a schema-fixture isolation fix followed.
- [x] Task 3 — deleted `RetryQueue`, `order_shards`, `plan_id_ranges`, `mirrors`, `graphql_batch`, `fetch_metafiles`, `skeleton.py` and their tests (including `tests/unit/test_skeleton.py`); quarantine manifest updated and header now cites R58; both pyproject skeleton excludes removed.
- [x] Task 4 — run-error sanitizer (marker truncation), migration 0004 downgrade note, console manual checklist; lazy-loader lock recorded as already fixed in `def016b`.
- [x] Phase closeout — full suite 1140 passed, coverage 96.12%; dev-log outcome committed.
- Rulings: `Deps.token_id`→`token_fp` anchor; extra test skeleton deletion; manifest now exists (plan conditional); no duplicate lazy-loader test; staging includes tests.

### Phase 2 — QA reconciliation — COMPLETE
Plan: `docs/superpowers/plans/2026-10-02-qa-reconciliation.md`

- [x] Task 1 — read-only `src/serve/quality.py` (`Check`, `QualityReport`, `run_quality`), 6 tests.
- [x] Task 2 — `GET /runs/{run_id}/quality` JSON, `GET /partials/runs/{run_id}/quality` htmx partial, run-detail panel; contract tests; golden snapshots and OpenAPI pin updated.
- [x] Phase closeout — full suite 1150 passed, coverage 96.11%; dev-log outcome committed.
- Rulings: null-warn ratio 0.5; `now` injection for deterministic `generated_at`; golden `PATH_PARAMS` + snapshot set; contract helper copies; `HTMLResponse` import.

### Phase 3 — Run resume — COMPLETE (detect portion limited per queue §2)
Plan: `docs/superpowers/plans/2026-10-02-run-resume.md`

- [x] Task 1 — `recover_orphaned_runs` at startup; idempotent; test isolation fix for `serve.__main__` contract tests.
- [x] Task 2 — `reset_run_artifacts` (schema-existence guarded), `POST /runs/{run_id}/resume` for failed find runs, CSRF/404/400/303 guards; reset assertion strengthened and DB work offloaded to a threadpool in fix round 1.
- [x] Task 3 — Resume button on failed run-detail pages (whitespace-controlled, golden byte-identical), crash runbook, OpenAPI re-pin; detect branch present but intentionally unreachable until agent detection lands.
- [x] Phase closeout — full suite 1157 passed, coverage 95.79%; dev-log outcome committed.
- Rulings: row-mapping test fix; schema fixture; golden regen for the POST route; FK-safe test fixtures; `csrf_token` helper; `to_regclass` guard; template `run` variable; detect test omitted (detection not executed); Jinja whitespace control.

### Phase 4 — SLO dashboard — COMPLETE
Plan: `docs/superpowers/plans/2026-10-02-slo-dashboard.md`

- [x] Task 1 — `src/serve/metrics.py` payload (`lib.audit.slo_snapshot` + paused buckets + queue PEL + run counts), thresholds/labels; fix round added malformed-key tolerance and truthful `queue.degraded`.
- [x] Task 2 — `GET /api/metrics`, `GET /metrics`, `GET /partials/metrics`; `metrics_redis` injection for deterministic golden snapshots; OpenAPI re-pin and dev-log section.
- [x] Task 3 — `pel_size` wiring verified end-to-end (`enqueue → claim → pel == 1`).
- [x] Phase closeout — full suite 1166 passed, coverage 95.75%.
- Rulings: `slo_snapshot` moved to `lib.audit`; `search_remaining == 80` arithmetic correction; injectable metrics Redis; explicit `LABELS` map.

### Final whole-branch review and fix wave — COMPLETE

- Whole-branch review (`9f32d58..8560611`, 22 commits): **Ready to merge = Yes**; 0 Critical, 0 Important, 13 Minor with triage.
- One bounded fix wave landed the reviewer's top recommendations: quality bundle type guards + regression test, SLO key-set consistency test, guarded injected metrics Redis accessor, deterministic degradation test, duplicate import removal, sanitizer/metrics doc corrections, stale `pel_size` manifest entry removed.
- Scoped re-review: all findings addressed, no new Critical/Important breakage.

## Commits created (oldest → newest)

| Commit | Message |
|---|---|
| `e57705a` | chore: ignore operator-only overnight queue prompt |
| `bde48de` | docs: add queued execution plans |
| `35dbd5a` | docs: ruling R58 triages the unwired surface |
| `225d169` | fix: reclaim stale shard deliveries before claiming |
| `c8e89c3` | test: isolate reclaim wiring test with schema fixture |
| `5fafdca` | chore: delete superseded unwired surfaces (R58) |
| `7951b7e` | fix: lazy-init lock, sanitized run errors, migration downgrade note |
| `6107be7` | docs: log Phase 1 unwired-debt triage outcome |
| `acb427f` | feat: computed run quality report |
| `8649b96` | feat: run quality panel and JSON route |
| `e40f4d4` | docs: log Phase 2 QA reconciliation outcome |
| `bfa6c42` | fix: recover runs orphaned by a process restart |
| `2bc9216` | feat: resume failed find runs from the stored filter spec |
| `7f68e73` | test: isolate serve main tests from startup recovery |
| `aaac0eb` | fix: assert resume reset and offload route db work |
| `c668b5b` | feat: resume detect runs, console button, and crash runbook |
| `a2625be` | docs: log Phase 3 run resume outcome |
| `e8928c9` | feat: ops metrics payload |
| `9c43d82` | fix: degrade metrics on malformed rl keys and pel errors |
| `a045ffe` | feat: SLO dashboard page, partial, and JSON API |
| `be0d216` | test: verify queue PEL wiring in the metrics payload |
| `8560611` | docs: log Phase 4 SLO dashboard outcome |
| `8de9bb5` | fix: harden quality bundle and metrics degradation paths |
| `e21143e` | docs: correct sanitizer/metrics guarantees and manifest |

## Verification (controller-run, final)

- `pytest --cov=src --cov-report=term-missing -q`: **exit 0**, **1168 tests collected/passed**, **coverage 95.72%** (`fail_under = 93`).
- Coverage by phase: 96.12% → 96.11% → 95.79% → 95.75% → 95.72% (decline is new code plus the intentionally unreachable detect branch).
- `ruff check src tests` and `black --check src tests`: clean at every commit.
- Golden endpoint suite green; `openapi.sha256` re-pinned in Phases 2, 3, and 4; `runs_run_id.json` changed only by the insertion-only quality panel (resume form emits zero bytes for non-failed runs).
- Fresh-collection count verified independently of run output: 1168.

## Residual risks / open items

1. **Run-error sanitizer is marker-based** (`{`, `Validation Failed`, `upstream`, 300-char cap). Text before the first marker or marker-free text can survive; documented in the dev log. Plan-mandated shape; the kept truncation test constrains it.
2. **Resume TOCTOU**: status is read and updated in separate transactions, so two concurrent POSTs can both queue a run. The FIFO executor and idempotent upserts prevent corruption; a conditional `UPDATE ... WHERE status='failed'` would close it.
3. **`recover_orphaned_runs` is table-wide**: a second live instance against the same DB would mark the first instance's active runs failed. Consistent with the documented single-operator/local model; needs an instance heartbeat before multi-process use.
4. **Detect path deferred**: the detect-resume branch exists behind `row.get("kind") == "detect"` but is unreachable and untested until the agent-detection plan lands; it will then need `detect_runner_factory` injection, `kind`-gated quality panel wiring, and the detect-resume contract test.
5. **Reclaim limits**: stale reclaim drains at most 10 entries per discovery run; a reclaimed shard that fails again consumes an extra attempt (reclaim + process both increment).
6. **Metrics**: only `socket_connect_timeout=0.5` is set (no read timeout); `pel_size` can create missing Redis stream groups on a fresh Redis (documented, pre-existing triage behavior); the SLO key-set consistency test now guards `THRESHOLDS`/`LABELS` drift.
7. **Test coverage gaps**: partial-route unknown-run 404, `main()` recovery invocation, recovered-row `finished_at`, and the raising-accessor metrics branch are not directly asserted.
8. **Ratchet policy not applied**: the dev log says raise `fail_under` by 1 whenever coverage exceeds the floor by ≥2 points; the queue prompt fixed the floor at 93 for this run, so it was left at 93. 95.72% would support raising it to 94.
9. **External process note**: a non-session pytest process briefly ran against the shared `gitcrawl_test` DB at 12:03–12:06 during Phase 1 Task 2 and exited; no CLI `opencode.exe` or pytest process was present at every subsequent precondition check. Its interference was diagnosed and the affected runs were repeated clean.
10. **Manual browser/keyboard checklist** was recorded in `docs/environment.md` but not executed (no browser pass in this run).

Remaining final-review minors deliberately deferred: retry-path asymmetry test, quality partial-route 404 test, resume poll-loop style, `pages._templates` private access, metrics read timeout, duplicate-import cleanup (fixed), recovered-row `finished_at` assertion.

## What is NOT done

- **Agent detection was not executed** (deliberate per queue §2): `docs/superpowers/plans/2026-10-02-agent-detection.md` and `docs/superpowers/specs/2026-10-02-agent-detection-design.md` are committed as reference only. All detect-dependent behavior (detect-run resume, detection evidence reset, detect UI, detect tests) remains pending.
- No Phase 5 (none exists).
- No authentication was added; the console remains local single-operator by design.
- Deferred minors above were not fixed; no coverage-floor change was made.
- No merge, push, branch switch, rebase, or reset was performed; the work remains on `quality-hardening`.
- The operator-only queue prompt was never committed; SDD scratch workspaces were cleaned after this report was committed.

## Stop conditions

None occurred. `.superpowers/sdd/queue-stop.md` was not created.
