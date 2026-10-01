# gitcrawl Quality Hardening — Design Specification

**Date:** 2026-10-01
**Status:** Proposed (awaiting sign-off; plans written alongside)
**Scope:** All findings from the 2026-10-01 quality audit across architecture, web security, test suite, and operations/UX.
**Companion plans:**
- `docs/superpowers/plans/2026-10-01-ci-and-regression-harness.md` (Phase 0)
- `docs/superpowers/plans/2026-10-01-zero-behavior-fixes.md` (Phase 1a — runtime corrections)
- `docs/superpowers/plans/2026-10-01-test-and-tooling-hardening.md` (Phase 1b — tests, types, quarantine)
- `docs/superpowers/plans/2026-10-01-adversarial-hardening.md` (Phase 2)
- `docs/superpowers/plans/2026-10-01-ux-polish.md` (Phase 3)

---

## 1. Goal

Raise every audited quality dimension to A/A+ without changing what the application does
for a legitimate user. The repository is currently graded: core **B+**, web **B-**, tests
**A-**, ops/DX **C+**. The target state is: no Critical or Important audit finding open,
CI enforced, and UX additions that are strictly optional to the existing flows.

## 2. The Freeze Contract (normative)

The user has selected the **"normal use identical"** freeze. These invariants are binding
on every task in every plan. A task that cannot satisfy them is a plan defect, not a
licence to deviate.

**Definition — normal use:** a human on the same machine using the bundled UI in a
same-origin browser against a healthy PostgreSQL and Redis, with a valid CSRF cookie and
GitHub credentials, sending well-formed requests within the documented caps
(`max_shards=10`, `max_candidates=500`, `max_hydrate=200`, `max_enrich=100`).

- **INV-1 — Identical normal behavior.** For normal use, every observable output (HTTP
  status, body, headers except additive ones, files, DB rows, ordering, counts) is
  identical to today.
- **INV-2 — Additive-only UI.** No existing control moves, disappears, gains a required
  step, changes meaning, or changes its result. New affordances must be ignorable without
  cost (no mandatory fields, no new interrupts, no reordering of existing content).
- **INV-3 — Allowed behavior changes.** Behavior may differ only for:
  1. cross-site requests (`Origin`/`Referer` present and mismatched, or non-local `Host`),
  2. requests that hang beyond a generous deadline (today: hang forever),
  3. request bodies above a generous cap (today: unbounded),
  4. visibility of infrastructure outage (today: silent), and
  5. the single approved visible fix (run summary counters), which requires explicit
     sign-off before its task executes.
- **INV-4 — No runtime surface.** CI, formatting, typing, coverage, fixture
  centralization, annotation and documentation work change no runtime behavior.
- **INV-5 — Exception register.** Any discovered unavoidable deviation is recorded in
  §11 with an explicit approval checkbox before it ships.

**Pre-declared exception to snapshotting:** Phase 3 adds optional UI affordances, so HTML
snapshots will change during Phase 3. Those changes are permitted only when the diff is
pure insertion/addition (no existing bound value, attribute, order, or control changes).
The snapshot-update step in each Phase 3 task must show that diff and, at execution time,
a reviewer must confirm it is additive-only.

## 3. Baseline and Finding Map

| Dimension | Baseline grade | Plan that raises it |
|---|---|---|
| Core architecture | B+ | Phase 1 (zero-behavior fixes) + Phase 0 (gates) |
| Web security | B- | Phase 2 (adversarial hardening) |
| Test suite | A- | Phase 0 (CI, coverage) + Phase 1 (fixture/JS/flake work) |
| Ops/DX | C+ | Phase 0 (CI, format, types) |
| UX | not graded | Phase 3 (additive polish) |

Open findings this program closes (severity per the audit):

- **Critical:** token leak to `repos.ecosyste.ms` (`src/enrich/trees_first.py:176-188`);
  missing CSRF/origin control on state-changing JSON and clone routes
  (`src/serve/pages.py:916-950`, `src/serve/app.py:402-434,558-646`); unauthenticated
  unbounded blocking on the single-worker executor (`src/serve/app.py:377-380`).
- **Important:** poison-shard on discovery failure (`src/discover/pipeline.py:246-311`);
  raw-field upsert aborts (`src/store/upserts.py:130-161,302-313`); GraphQL partials
  swallowed (`src/enrich/graphql_batch.py:216-234`); clone hangs
  (`src/enrich/cloner.py:167-186`, `src/serve/runs.py:354-360`); silent Redis fallback
  (`src/serve/runner.py:53-69`); lazy singleton races
  (`src/serve/app.py:284-313`); clone guard race + unbounded registry
  (`src/serve/runs.py:189-200,337-348`); body/upload caps absent
  (`src/serve/pages.py:661-681`); health probe thread leak (`src/serve/pages.py:78-90`);
  layering inversion (`src/discover/pipeline.py:28` and peers); unbounded limiter sleep
  (`src/limiter/buckets.py:24-40`, `src/lib/gh_client.py:133-138`); dead unwired surface;
  no CI/coverage/mypy; 19 duplicated DB fixtures; wall-clock test assertions; JS coverage
  is one function.
- **Minor (selected):** unicode-digit 500 (`src/serve/forms.py:29-30`); malformed progress
  file 500 (`src/serve/runs.py:313-321`); `set_state` stale `updated_at`
  (`src/scheduler/state_machine.py:126-156`); `GeoCache.put_many` unchunked
  (`src/enrich/geo_resolver.py:795-820`); formatting drift (measured during Phase 0
  planning: 7 files under `black --check`, 10 under `ruff format --check`).

## 4. Phase 0 — Regression Harness and Gates

**Why first:** every later phase needs an automated veto. Today there is no CI, no
coverage measurement, and ~53% of the suite silently skips without `TEST_DATABASE_URL`.

Design:

1. **CI.** GitHub Actions workflow on `ubuntu-latest` and `windows-latest` with
   `postgres:17` and `redis:7` services. Create database `gitcrawl_test`, set
   `TEST_DATABASE_URL`; run `pytest -q`, `ruff check`, `black --check`. A missing
   `TEST_DATABASE_URL` in CI must fail, not skip.
2. **Endpoint golden harness.** A session-scoped fixture seeds a fixed corpus into a real
   migrated DB and captures, for every registered route, status + selected headers +
   full body. JSON bodies are compared byte-exact; HTML bodies are compared after a
   deterministic normalizer. Snapshots live under `tests/golden/`. Any later diff fails
   the suite until explicitly accepted by the owning task.
3. **OpenAPI pin.** Hash `app.openapi()` and pin it; any schema change fails.
4. **Coverage.** Add `pytest-cov`; record a baseline; set a floor and ratchet policy.
   Report untested modules (currently `src/skeleton.py`, `cloner.free_disk_mb`,
   `cloner._default_git_runner`, real-Redis semantics, most of `app.js`).
5. **Formatting authority.** `black` is the single formatter (already in dev deps); CI
   runs `black --check` and does **not** run `ruff format`. Ruff remains the linter.
   Reformat so the baseline is clean.
6. **Dependency audit.** Add `pip-audit` to CI as a non-blocking report initially,
   blocking after the first clean run.

Acceptance: workflow green on both OSes; golden suite fails deliberately when a route
response is altered; coverage floor enforced; `black --check` clean; OpenAPI hash stable.

## 5. Phase 1 — Zero-Behavior Corrections

All tasks here satisfy INV-1/INV-4 by construction.

1. **Token leak.** `fetch_metafiles` passes `auth=False`; add a test asserting no
   `Authorization` header reaches `METAFILES_BASE_URL`. Function is unwired, so no
   observable change.
2. **Singleton init races.** Replace check-then-create on `state` in `app.py` with a lock
   (or `functools.lru_cache` under a lock); dispose losing duplicates. Behavior under
   normal single-threaded first request is identical.
3. **Clone guard atomicity.** Move check-and-claim into `CloneRegistry` under its existing
   lock (`claim(run_id, factory)`), so concurrent starts return the same progress object.
4. **Clone registry bound.** Evict terminal entries by age/count on access; a poll for an
   evicted clone reads the identical persisted progress file, so responses are unchanged.
5. **Shard poison recovery.** On `RequestFailed`, roll the shard back to its pre-claim
   state (or `PENDING`) and move the message to the retry queue/DLQ path instead of leaving
   `ACTIVE`. Normal successful runs are untouched.
6. **Upsert input isolation.** Validate/coerce the raw fields (`size_kb`, `language`,
   `visibility`, boolean flags) in `normalize_repo` so one bad row is skipped/normalized
   rather than aborting the chunk. Well-formed rows behave identically.
7. **GraphQL partial surfacing.** `_fetch` returns successful partials plus an explicit
   incompleteness signal; callers log/accumulate it. Additive to internal return values.
8. **Layering.** Extract the audit/telemetry dependency used by `discover`/`enrich`/
   `hydrate` into `lib` (or inject it), and break the `store.lifecycle ↔ hydrate` cycle.
   No public behavior change; add an import-boundary test asserting core never imports
   `serve`.
9. **Dead-surface quarantine.** Do not delete unwired modules (`limiter/retry.py`,
   `scheduler.tiering.order_shards`, `since_scan.plan_id_ranges`, `upserts.bootstrap_copy`,
   `mirrors`, `graphql_batch`, `skeleton.py`). Instead add tests where missing and mark the
   surface as experimental in module docstrings; a CI test asserts every public module is
   either exercised or listed in a quarantine manifest.
10. **Types.** Add mypy to dev deps + CI with pragmatic settings (`ignore_missing_imports`,
    per-module overrides), starting from the core packages; fix the annotation lies noted
    in the audit (`Deps.token_id` naming, untyped helpers). Ratchet toward stricter.
11. **Test infrastructure.** Centralize the 19 duplicated `clean`/seed fixtures into
    `conftest.py` factories; make CWD-relative paths independent of launch directory;
    replace wall-clock assertions with injected clocks where deterministic; add Node tests
    for the extracted pure helpers in `app.js`.
12. **Geo chunking.** Chunk `GeoCache.put_many` binds like `get_many`; same results.
13. **Formatting drift.** Covered in Phase 0 task 5.
14. **Visible exception (gated).** Fix run summary counters (`updated/unchanged/skipped`)
    only after explicit sign-off (see §11).

Acceptance: every listed test passes; import-boundary and quarantine tests green; mypy
clean on its configured scope; full suite run with CI-parity environment and zero skips.

## 6. Phase 2 — Adversarial-Path Hardening

All changes are allowed by INV-3 and must be proven invisible to normal use by the golden
harness.

1. **Origin-aware CSRF.** Add central middleware for state-changing methods:
   - if `Origin` (fallback `Referer` on non-GET when `Origin` absent) is present, it must
     be same-origin (scheme, host, port) with the effective request base; otherwise 403;
   - if no origin header is present (curl, scripts, tests), pass — so the existing API
     clients are unchanged; existing per-route `validate_csrf` stays as defense in depth.
   This closes the JSON/clone endpoints without changing the browser UI (same-origin forms
   and htmx already send a matching Origin) or scripted clients.
2. **Host allowlist.** `TrustedHostMiddleware` with `localhost`, `127.0.0.1`, `[::1]`,
   extendable via `GITCRAWL_ALLOWED_HOSTS`. Default bind remains `127.0.0.1`
   (`src/serve/__main__.py:9`). Only non-local `Host` headers are rejected.
3. **Deadlines.** A single configurable deadline policy:
   - clone subprocess timeout (default generous, e.g. 1800 s per repo via
     `GITCRAWL_CLONE_TIMEOUT_SECONDS`), plus `GIT_TERMINAL_PROMPT=0`, stdin closed, and no
     credential helper — prevents interactive hangs;
   - bounded waits for `RunExecutor` results (`app.py:377-380,452`), payload-cache waiters
     (`payload_cache.py:66`), and the discovery limiter sleep (`gh_client.py:133-138`) —
     defaults chosen ≥ 4× the worst legitimate run so normal runs never trigger;
   - on expiry return HTTP 503 with a `retry_after` hint in the body, consistent with the
     existing structured error style (`invalid_param` envelopes use `error`/`details`;
     timeout responses add `error: "timeout"` plus `retry_after`).
4. **Body caps.** ASGI middleware enforcing `Content-Length` and streamed-byte caps:
   1 MiB for JSON endpoints, 10 MiB for the `/find` upload. Anything legitimate is far
   below; oversized requests get 413 (today they buffer unboundedly).
5. **Redis fail-loud.** Log a warning with the failure reason and surface a degraded
   indicator on `/health` when the real Redis URL is unreachable. Default behavior keeps
   the fake fallback (INV-3 only makes the outage visible); `GITCRAWL_REDIS_STRICT=1`
   refuses fallback for deployments.
6. **Clone lifecycle (backend).** Add `DELETE /runs/{id}/clone` cancellation backed by a
   cancel event the git runner honours. New capability, no existing flow altered; UI in
   Phase 3.
7. **Malformed input hardening.** `read_clone_progress` tolerates corrupt JSON/types
   (`runs.py:313-321`); `_int_or_raw` rejects non-ASCII digits (`forms.py:29-30`). Both
   only affect malformed input.
8. **Health probe.** Replace the abandoned-thread `_bounded` (`pages.py:78-90`) with a
   cached single-flight probe using a connect/statement timeout. Healthy responses are
   identical; the thread leak disappears.
9. **State freshness.** `set_state` updates `updated_at` (`state_machine.py:126-156`);
   `updated_at` is not part of any API response, so this is invisible.

Out of scope deliberately: adding authentication (not requested; loopback + Host allowlist
is the model), changing error codes for consistency (visible; see §11), enabling clone
worker parallelism or `/vsearch` concurrency caps (changes timing under normal use).

## 7. Phase 3 — UX Polish (additive only)

Governing rule: **add only things a user can ignore**. Nothing existing moves, reorders,
or becomes required. New capabilities may be added so long as current paths keep working
unchanged. Concretely planned candidates (final list refined in the plan):

- **Busy feedback:** htmx indicators/busy states on navigation, sorting, paging, and
  actions, so slow requests show progress without changing results.
- **Actionable errors:** replace the generic clone failure toast (`app.js:287-301`) with
  the server's structured `invalid_param`/error text; add dismiss/retry affordances.
- **Recent filters:** a localStorage-backed "recent filters" quick-pick on the filter page.
  Nothing is auto-applied; defaults are unchanged for a first-time or cleared browser.
- **Empty states:** guidance plus one-click example queries when a list has no rows.
- **Keyboard help:** a `?` help panel documenting existing shortcuts (additive; must not
  shadow an existing binding).
- **Share/copy:** copy the current filter hash / URL to clipboard.
- **Clone cancel:** expose the Phase 2 backend cancel as a button in the clone modal.
- **Modal focus UX:** Escape closes, focus is trapped while open and restored on close
  (`app.js:53-70`). Mouse behavior unchanged; keyboard behavior improved.
- **Long-list ergonomics:** sticky table header and a visible row count on run pages.

Every UX task must: keep existing contract tests green, update golden HTML only with an
insertion-only diff, and add a DOM-level test where the project's existing test style
allows it.

## 8. Acceptance Criteria (A/A+ definition)

| Dimension | A+ evidence |
|---|---|
| Security | Zero open Critical/Important; origin/CSRF/Host/deadline/body-cap tests present; `pip-audit` clean; loopback default documented |
| Architecture | Import-boundary test ensures core never imports `serve`; unwired surface quarantined and tested; mypy green on scoped modules; no formatting drift |
| Tests | CI green on Linux + Windows with a real DB and zero silent skips; golden + OpenAPI pins active; coverage floor ratcheted; no wall-clock flake assertions; JS helpers tested |
| Ops/DX | CI, coverage, typecheck, format gate, dependency audit all enforced; development log updated |
| UX | All additive items shipped; existing contract/UX tests green; golden HTML diffs insertion-only; manual smoke of every changed screen in light/dark, keyboard-only |

## 9. Non-Goals

- No authentication/login, multi-user, or remote-exposure hardening beyond the allowlist.
- No API contract redesign (status/error consistency stays as-is, documented).
- No new crawling features; no clone worker parallelism or executor re-architecture.
- No database migrations required by this program (no schema changes).
- No deletion of unwired code (quarantine, don't destroy).

## 10. Risks

| Risk | Mitigation |
|---|---|
| Golden snapshots block intended Phase 3 additions | Pre-declared insertion-only review process (INV-2 note); JSON stays byte-exact |
| Deadlines too tight for large legitimate runs | Defaults ≥ 4× worst case; env override; load-test task before merge |
| Origin check breaks a proxy/deployment | Origin-absent requests pass; allowlist env for hosts; docs updated |
| Fixture centralization churn across 19 files | One task, golden suite proves equivalence, no behavior assertions touched |
| mypy on an untyped codebase stalls | Scoped start + per-module overrides + ratchet, not big-bang strict |
| Quarantine manifest becomes a loophole | Every quarantined module must have at least one test |

## 11. Exception Register (requires explicit approval)

| # | Exception | Why | Approved? |
|---|---|---|---|
| E1 | Run summary shows correct `updated/unchanged/skipped` counts (`executor.py:208-221`) | This is the only **normal-use-visible** change; today the numbers are wrong. Task ships only if approved here. | [x] approved by operator 2026-10-01 |
| E2 | Error-status inconsistency (replay 409 vs export 404) remains | Fixing is API-visible; out of scope by choice | [ ] keep as-is |
| E3 | Phase 3 HTML snapshots change by insertion | Inherent to additive UI; covered by INV-2 review rule | [x] pre-approved |

## 12. Definition of Done

This program is complete when: CI is enforced on both OSes with real services and zero
silent skips; every Phase 1/2 task merged and its tests green; the exception register
decided; the audit's Critical/Important findings all closed or explicitly excepted;
coverage/type/format/dependency gates active; and a final whole-branch review confirms
INV-1..INV-5 were upheld (golden suite unchanged except E3 insertions and any approved
E1).
