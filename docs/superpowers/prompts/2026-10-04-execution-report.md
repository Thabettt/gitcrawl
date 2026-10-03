# Overnight Queue Execution Report — 2026-10-03/04

> Executed from `docs/superpowers/prompts/2026-10-03-overnight-queue-prompt.md` on branch `main`.
> No branch switch, no push, no amend; one commit per task plus fix-forward commits.

## Preconditions

- Tree clean at start; the queued plans were already committed (no `docs: add execution plans` commit needed).
- No competing OpenCode/pytest process; the local uvicorn console on port 8000 was ignored.
- Postgres reachable via `TEST_DATABASE_URL` (`gitcrawl_test` on `localhost:5433`); Redis reachable via `REDIS_URL` (`localhost:6379`).
- `.superpowers/sdd/queue-stop.md`: **not written** — no stop condition occurred.

## Phase status

| Phase | Plan | Status | Tasks |
|---|---|---|---|
| 1 | GraphQL batch engine | Done, reviewed clean | 9/9 done |
| 2 | Console settings | Done, reviewed clean | 5/5 done + 1 post-review fix |
| 3 | Console UX redesign | Done, reviewed clean | 8/8 done + final-review fix wave |

Every task followed TDD, passed an independent spec+quality review, and every finding was fixed or ruled on and recorded in the per-plan ledgers.

### Phase 1 — GraphQL batch engine (9/9 done)

1. GraphQL rate bucket and resource mapping
2. Generic batch core — parse first, attribute per repo
3. Failure isolation — splits, bounded retries, fallback, deadline, auth
4. Repo details adapter
5. Batched hydration with REST fallback
6. File presence adapter and batched Dockerfile checks
7. Owner location adapter and batched geo lookups
8. Enforce min/max commits from batched commit counts
9. Full verification, docs, status flip

Gate: 1237 passed, coverage 95.56% (floor 93); both isolation tests pass; ruff/black clean.

### Phase 2 — Console settings (5/5 done)

1. Settings persistence — migration 0008, model, resolution
2. Settings form validation
3. Settings page, save route, audit, navigation (amended copy: System → Limits)
4. Apply settings to new runs
5. Documentation, corpus profile, full verification

Gate: 1268 passed, coverage 95.40%; ruff/black clean. Post-review fix: quarantine-manifest coverage for `serve.settings`.

### Phase 3 — Console UX redesign (8/8 done)

1. Shell, plain-language naming, and error pages
2. System page and status dot
3. New search — Common/Advanced, sticky summary, Check matches
4. View-all results page
5. Search workspace (run-scoped export, explained counts, quality sentences, clone copy)
6. Corpora — freeze, list, detail (migration 0009)
7. Library — run now and readable rows
8. Remaining translations, empty states, verification

Gate: 1355 passed, coverage 95.22%; ruff/black clean. Final whole-branch review raised 5 Important findings; one fix wave addressed all 5 (1355 passed, 95.23%, OpenAPI pin unchanged).

## Commits created (oldest first)

```
0e28c40 feat: graphql rate bucket and resource mapping
726dca4 feat: generic graphql batch core with per-repo attribution
2bbf414 feat: graphql batch failure isolation and bounded retries
4772ad2 feat: graphql repo details adapter
1f88abc feat: batched hydration with rest fallback and outcome reporting
839025d feat: batched file presence checks with tree fallback
e88c917 fix: count each skipped repo once in the dockerfile report
16e20f1 feat: batched owner location lookups with rest fallback
4c2976a fix: persist confirmed no-location owners as unmatched
f705ff8 fix: count raising owner fallbacks against the geo budget
e15cbc3 feat: enforce min/max commits from batched commit counts
a19f5c1 fix: keep a hydrated repo when its commit count fetch fails
eaf2a3d docs: log the graphql batch engine outcome
1cc4d35 migration: add single-row app settings table
23742fd feat: settings form validation
03706de feat: settings page, audit trail, and navigation
7b4e72d feat: apply persisted settings to new runs
74a33b3 test: cover record_settings_change directly
c373ce3 docs: settings page, corpus profile, and outcome
ac7f225 feat: console shell, plain-language naming, and error pages
e250745 feat: system page and header status dot
db565ef fix: keep the system page usable when metrics are unavailable
db0e27f feat: new search page with common/advanced split and match check
4efe837 fix: keep unavailable loc filters and country confidence in the search round-trip
61f8b62 feat: full-width results page with page-size selector
c141eaf feat: readable search workspace with explained counts
90a9920 feat: corpora freezing, list, and detail pages
4956ba9 feat: library run-now and readable saved-filter rows
dbf793a docs: log the console ux redesign outcome
c9a87dd fix: align console copy and counters with the reviewed behavior
```

30 commits; one task per commit (fixes are fix-forward commits, never amendments).

## Verification

- Final full suite: **1355 passed**, coverage **95.23%** (`fail_under = 93`), ruff and black clean.
- Migrations: `0008` app settings, `0009` corpora; both reversible and round-trip tested; alembic head is `0009`. Agent detection remains renumbered to `0010`.
- Goldens: every new GET route is in `tests/golden/conftest.py::PATH_PARAMS`; snapshots regenerated per task; final `openapi.sha256` = `79a24d94d27e5066f6db058d831bab3fe4749ba265230bed6686e2726bfe1be4`.
- Security: CSRF on every new POST; tokens never stored/rendered/logged (tests assert the secret is absent from `/settings` and `/system`); settings saves write before/after audit rows.
- The two isolation contracts named in the queue prompt pass:
  - `tests/integration/test_hydrate_batch.py::test_one_bad_repo_falls_back_to_rest_without_touching_its_neighbours`
  - `tests/integration/test_runner.py::test_run_filter_batches_hydration_and_isolates_a_bad_repo`

## Rulings made (controller decisions on plan conflicts)

Phase 1 (R1–R8):

- R1: REST fallback failures surface as `unresolved` with a reason; SSO `partial-results` propagates loudly. The isolation test's unresolved count was adapted to 1 (spec-faithful strengthening).
- R2: R44 "recorded but unenforceable" covers `min_loc` and `max_loc` only, in both `runner.py` and `pages.py`; `min_commits` is now enforced.
- R3: exact-set virtual-table tests updated to the nine virtuals.
- R4: cost-planner test table updated to the new costs (`min_commits`/`max_commits` = 2).
- R5: `max_attempts` means total POST tries (attempt counters initialized to 1), matching `request_with_retry` and Task 3's request-count test.
- R6: Task 5's tree-assertion bullets deferred to Task 6 (Task 5 does not change the Dockerfile path).
- R7: owners GitHub confirms have no location persist as `geo_confidence="unmatched"`, so later runs do not re-fetch them.
- R8: a commit-count fetch failure no longer marks an already-hydrated repo unresolved; the missing count is warned about by the runner.

Phase 2 (R9–R16):

- R9: amended Limits copy supersedes the plan's "applies to new runs" assertion.
- R10: nav link is `System` pointing at `/settings` until Phase 3 replaces it with `/system`.
- R11: migration numbering recorded as settings `0008`, UX corpora `0009`, agent detection `0010`.
- R12: `_enrich_handlers` keeps the Phase 1 `report`/`commit_counts` params and adds `cfg`; `_hydrate(deps, rows, cfg, hook)`; `batch_size` belongs to adapters, not `fetch_batch`.
- R13: no duplicate `build_deps` import in `app.py`.
- R14: settings template implemented to the amended plain-language copy.
- R15: `max_hydrate <= max_candidates` re-checked after merging pinned/default values before persisting.
- R16: quarantine-manifest gap for `serve.settings` fixed by directly testing `record_settings_change`.

Phase 3 (P1–P16):

- P1: every new GET route goes into `PATH_PARAMS`, snapshots regenerate, `openapi.sha256` re-pins after route changes.
- P2: kept the planned error-handler shape (only unknown-route 404s are affected today).
- P3: `build_health_snapshot` extraction preserves the probe single-flight cache; `/health` and `/system` share one snapshot.
- P4: `find_count_factory` threaded through `create_app`/`make_client`/`healthy_client`; a golden factory makes the matches snapshot deterministic.
- P5: the partial-table invalid-page response is a rendered 400 hint (contract test updated).
- P6: export serialization extracted to `serve.runs.export_run`; the hash-scoped API route is byte-identical.
- P7: `test_models_console.py` exact table set and alembic head updated for `0009`.
- P8: retired-string assertions replaced with the plain-language copy; no behavioural assertion weakened.
- P11: `describe_spec` is the cross-task sentence interface, deterministic and unit-pinned.
- P12: the settings page's CSRF branch uses the explained error page.
- P13: `/system` renders Status and Limits when metrics fail instead of returning 500.
- P14: hidden mirror inputs keep prefilled `min_loc`/`max_loc` values across a re-save while the visible inputs stay disabled.
- P15: an explicitly set `min_geo_confidence` appears in the filter sentence.
- P16: all visible run-scoped export links point at `/runs/{id}/export`; hash-scoped links remain only for API subjects.

Accepted adaptations (recorded in the ledgers): the non-transient batch test switched to `KeyAdapter` (the `DictAdapter` query made the count assertion vacuous); `RefreshStats.fallbacks` and `batch_size` added because the plan's own tests required them; `403.html`, the settings CSRF branch, and two contract-test files added to Phase 3 Task 1 as necessary; the library copy test seeds a saved filter (the clean fixture has none); the plan's `_R44_VIRTUALS` text overridden by R2.

## Residual risks / open items (accepted, not fixed)

- Dashboard "Saved" is `inserted` only while the run page shows `inserted + updated` (same class as the fixed "Found" mismatch; cosmetic).
- Run-scoped export and View-all results have no readiness gate; a mid-run export can look complete (the hash-scoped API route still gates).
- `runs.html` retains some legacy "Run/Find" vocabulary ("Run history", "New find").
- The REST commit-count fallback returns `None` for an empty repository (409) where GraphQL maps it to 0; those repos are warned/skipped rather than counted as 0.
- The htmx 2.x default drops 4xx bodies, so the partial-table inline hint is not painted on htmx-driven navigation without a `responseHandling` entry.
- The status dot and the new sticky rail have no CSS (`app.css` was out of scope: Phase 3 was restructure/re-copy only, no styling).
- Status dot is invisible without JS; with no `REDIS_URL` the queue reads "down" although the app intentionally runs on fakeredis.
- Minor test-strength gaps: the dashboard "Found" fix has no discriminating regression test; `count_parity`/`duplicate_full_names`/`field_coverage` warn sentences lack direct tests; `_commit_handler` assumes `skipped["commits"]` exists for direct callers.
- The dev-log "(R11)" tag in the Phase 2 entry collides with a historical R11 in the same file (cross-reference ambiguity).

## Explicitly NOT done

- Detection pages/routes (agent detection plan renumbered to migration `0010`) — never started.
- `min_loc`/`max_loc` enforcement (needs the full-history tier); they remain recorded-only, disabled in the UI, and covered by the R44 incomplete warning — the warning was never weakened.
- Remaining UX polish: dashboard Saved mismatch, `runs.html` vocabulary, status-dot/rail CSS, htmx 4xx swap handling, run-scoped export readiness gating.
- No push, no branch switch, no rebase/reset, no amend. All work is committed on `main` only.
