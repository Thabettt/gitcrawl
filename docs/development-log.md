# gitcrawl Development Log

**Purpose**: durable, committed record of what has been done, decided, and is next — so nothing is lost when a session, tool, or the temporary SDD workspace disappears. Environment details live in `environment.md`.

**Updated**: 2026-10-01 (overnight console run complete) · **Branch**: `001-gitcrawl` · **HEAD**: `918339e`

## Objective (frozen 2026-09-30)

gitcrawl is a **live-only** GitHub repository discovery tool: one operator-triggered run validates a filter, searches GitHub live, hydrates matches, applies virtual filters, enriches survivors, resolves owner countries, serves/export a run bundle (JSON + CSV). No background crawl, no watermarks/tiers, no GH Archive tail; lifecycle (301 renames, 404 tombstones) resolves **within the run**. Thesis tracks are parked. Full authority: `design/spec.md` (Frozen v2) + `design/plan.md` + `design/tasks.md`.

## Environment provisioning (2026-10-01)

| What | Result | Details |
|---|---|---|
| Project-local PostgreSQL 18.1 cluster | ✅ running | `localhost:5433`, DBs `gitcrawl` + `gitcrawl_test`, logon task `gitcrawl-postgres` |
| WSL2 Redis 7 service | ✅ running | `redis://localhost:6379/0`, verified from Windows, logon task `gitcrawl-redis` |
| User env vars | ✅ set | `DATABASE_URL`, `TEST_DATABASE_URL`, `REDIS_URL`, `GITHUB_TOKEN` (from `gh` keyring; value never printed) |
| Docker Desktop | ⛔ skipped | WSL + local cluster chosen instead; `docker-compose.yml` kept for other hosts |

Full commands, credential handling, rotation, and troubleshooting: **`docs/environment.md`**.

## Cleanup note — stray Kilo Code worktree (2026-10-01)

While preparing the opencode restart, a `.kilo/` directory was found in the repo root. Investigation (read-only) established:

- It was a **registered git worktree** of this repo: `.kilo/worktrees/spiky-door`, detached HEAD at `784117b` (our Phase 2a fix commit), created 2026-09-30 23:33:30 by git identity `Thabettt <thabetology@gmail.com>`; admin metadata `.git/worktrees/spiky-door/kilo-agent-manager-metadata.json`.
- Content was **identical to our commit at the blob level** (sampled sha1s match; working-tree hash differences were CRLF-only reproductions of the same blobs). Working tree clean, no commits, no extra branches, no running process.
- Kilo Code had written 28 ignore lines into `.git/info/exclude` (`.kilo/worktrees/`, `.kilo/agent-manager.json`, `.kilo/setup-script*`, `.kilocode/*`), which is why `git status` never showed it.

Owner states Kilo Code was never used. **Removed**: `git worktree remove .kilo/worktrees/spiky-door --force`, `git worktree prune`, deleted `.kilo/`, restored `.git/info/exclude` to its default comments. Verified after: single worktree, branches only `main` + `001-gitcrawl`, `git status` clean, zero "kilo" mentions in git metadata.

Source note: VS Code extension `kilocode.kilo-code-7.8.1-win32-x64` is installed in `~/.vscode/extensions` (installed 2026-09-26 15:49); no Kilo task state (`globalStorage`/`workspaceStorage`) or logs retained. Uninstall offered to the owner. Separately, the design-doc v2 edits observed at 2026-09-30 22:45–22:50 predate this worktree and were not made by this session; they remain unexplained.

## Rulings record

| # | Ruling | Cost if wrong |
|---|---|---|
| R1 | T000 skeleton is stdlib-only (deps arrive in T002) | small import rewrite |
| R2 | `src/hydrate/tail.py` = within-run refresh driver, not a poller | rename/merge file |
| R3 | Every implementation task authors its own tests; conventional names where unspecified; 100% branch target | extra test churn |
| R4 | Dev DB is local PostgreSQL 18 (plan said 17) | test-env drift |
| R5 | T000 ran unauthenticated (no token then) | rerun with token |
| R6 | Pinned `requirements*.txt` + `pyproject.toml` tool config (no installable package) | repackage later |
| R7 | `load_tokens`: `GITHUB_TOKENS` wins when it parses to ≥1 token; empty list falls back to `GITHUB_TOKEN` | one-line change |
| R8 | Redis: fakeredis for unit tests now, real Redis before the validation pass | dialect drift caught at validation |
| R9 | (superseded by R14) user would set `DATABASE_URL` | — |
| R10 | (superseded by R16) user would set `GITHUB_TOKEN` | — |
| R11 | `-org:` exclusions do not satisfy `props.*`'s single-`org:` scope | validator stricter than brief |
| R12 | Alembic at repo-root `migrations/` + `alembic.ini`; URL from env | move dir |
| R13 | DB tests run against `TEST_DATABASE_URL` only (destructive-safe) | test-env misconfig |
| R14 | Provision a project-local PG 18 cluster on 5433 instead of the machine's 5432 service | port shift |
| R15 | Real Redis via the existing WSL Ubuntu service | service location change |
| R16 | `GITHUB_TOKEN` sourced from the authenticated `gh` keyring (broad OAuth token; fine-grained PAT can replace it) | scope breadth |
| R17 | Services auto-start via per-user logon scheduled tasks | manual start needed |
| R18 | US1 executed as 3 reviewable batches | review cadence |
| R19 | pipeline lives in `discover/pipeline.py` | move file |
| R20 | golden-org parity = cross-path + bounded since sample | adjust test |
| R21 | `request_with_retry` added to `lib/gh_client.py` | refactor later |
| R22 | bootstrap inserts first-insert history | missing/extra history rows |
| R23 | TEXT COPY (PG18 forbids binary `ON_ERROR`) | slower bulk load |
| R24 | multi-worker machinery built but unwired (documented) | hidden dead code until wired |
| R25 | live delta probe test added | two extra API calls |
| R26 | Console docs live in `design/console-spec.md` + `console-plan.md`; plan format compressed to interface-level (writing-plans adapted for scope) | implementer gaps caught at review |
| R27 | Overnight work continues on `001-gitcrawl` (no new branch); integration decision stays pending | branch semantics |
| R28 | AC standby/hibernate disabled for the overnight run (`powercfg`) | machine sleeps mid-run |
| R29 | Spec review deferred to morning per operator's continuous-run instruction | unreviewed spec assumptions |
| R30 | Single-worker in-process run executor (no Redis queue for runs) | run concurrency limited |
| R31 | Front-end assets vendored committed (Tailwind standalone output preferred, hand-rolled CSS fallback) | design quality varies by path |

## Task progress (commits on `001-gitcrawl`)

| Task | Scope | Commit(s) | Review |
|---|---|---|---|
| — | Baseline: research + frozen design (on `main`) | `717c9b3` | — |
| T000 | Walking skeleton (validate → live search ≤3 pages → display → bundle JSON+CSV); live unauth run fetched 300 repos / 3 pages, bundle in `runs/2bc6b774968a/` | `8947d1a` | ✅ clean (3 minor findings deferred) |
| T001 | Project structure + `docker-compose.yml` | `14ec36c` | ✅ batch approved |
| T002 | Pinned deps + `src/lib/gh_client.py` (env tokens, headers, fingerprint) + tests | `f8cd570` | ✅ batch approved |
| T003 | ruff + black + pytest config | `8174d2f` | ✅ batch approved |
| T004 | `docs/legal-gates.md` (FR-011 checklist) | `9097849` | ✅ batch approved |
| T005 | Per-bucket Redis limiter (`src/limiter/buckets.py`) + retry queue (`retry.py`) | `6ae3c32` | ✅ after fix round |
| T006 | Throttle classifier (`src/limiter/classifier.py`) | `01cfdc3` | ✅ after fix round |
| T007 | Qualifier allowlist + delta test (`src/lib/qualify.py`) | `f583db8` | ✅ after fix round |
| T006/T007 fix | 422 attempt-5 escalation, `props.*` org-scope (R11), test rename | `784117b` | ✅ re-review: all addressed |
| Phase 2b | T008 models + Alembic migration; T009 audit record + SLO snapshot | `dcba8e4`, `663196f` | ✅ approved |
| US1a | T010 pagination contract test; T012 shard planner; T013 shard state machine + Streams queue + ordering; T014 sharded search (`request_with_retry` extension) | `0d777d7`..`db5f69f` | ✅ after 1 fix round (limiter slot leak; retry cap; bonus queue fix) |
| US1b | T015 since cursor scan + ID-range sharding + checkpoint; T016 org/user enumeration; T017 upserts + COPY bootstrap (R22/R23) | `e26c300`..`33182f0` | ✅ after 1 fix round (array envelope; bootstrap stats O(N²); first-insert history) |
| US1c | T011 live golden-org parity test; T018 discovery pipeline + audit logging | `6d53778`..`9f0488d` | ✅ approved; live **A=566 B=566**, empty symmetric difference |
| Final whole-branch review | Senior review of `717c9b3..a0d3b92` (8,077-line package) + one fix wave: SSO `partial-results` loud-fail on 2xx, dirty-payload tolerance, bootstrap rename/staging, `order_repos` key fallback, live delta probe, non-dict body guard | `f64bd69` | ✅ all findings addressed; no new Critical/Important. Live delta probe `language:python` 34,207,993 → `+stars:>1000` 11,756 (`delta_narrows=True`) |

Test status at `f64bd69`: **350 tests passing** (including live golden-org parity and the live delta probe), `ruff` + `black` clean.

**Reviews conducted**: T000 (approved), Phase 1 batch (approved), Phase 2a (1 Important finding → 1 fix round → approved). Every batch passed a spec-compliance + code-quality gate before completion.

## Deferred Minor findings — triaged by the final review

The final whole-branch review triaged every prior deferred minor as **"Can ship"** (none block a merge); the only cross-cutting one — `order_repos` keyed on `stargazers` vs raw `stargazers_count` — was fixed in the wave. Residual minors from the fix-wave re-review (also "can ship"): `_coerce_count` catches only `TypeError`/`ValueError` so a JSON `Infinity`/`1e400` could still raise `OverflowError`; the `response.request is not None` guard in the SSO check is inert (httpx raises on unset request, real responses always carry one); the staging `finally` DROP could mask the original exception on a dead connection. Fix these opportunistically when touching those modules.

### Original deferred list (for reference)

- `src/skeleton.py`: wire header casing via `urllib.add_header` (`X-github-api-version`); `Retry-After` HTTP-date form unparsed; `json.loads` of a 200 body unguarded. File is currently `extend-exclude`d from ruff/black — fix it and drop the excludes in a later task.
- `tests/unit/test_gh_client.py`: default-timeout test exercises the helper's explicit value, not `create_client`'s own default.
- `src/limiter/buckets.py`: `update_from_headers` reconciles when `remaining` parses even if `reset` is malformed; `KeyError` for resources outside the three specs; Lua identifier injection via `.replace` is fragile.
- `src/lib/qualify.py`: bare `props` (no colon) is treated as a keyword.
- Redundant `.gitkeep` files in populated dirs (`src/lib`, `tests/unit`, `docs`).
- Commit `f8cd570` message mentions a "fingerprint logging helper" that does not exist (helper is `token_fingerprint`).

## Current status & how to resume

- **Done**: T000; Phase 1 (T001–T004); Phase 2 (T005–T009); **US1 complete** (T010–T018 + T011 live parity); **final whole-branch review clean after one fix wave** (`f64bd69`). 350 tests green incl. live parity (A=566/B=566) and live delta probe (34,207,993 → 11,756).
- **Overnight run (2026-10-01 → 02)**: operator approved completing **US2 + US3 + US4 console in one continuous run** (backend first, then UI), no check-ins, spec review deferred to morning, continuing on `001-gitcrawl`; machine kept awake (R28). Plan of record: `design/console-spec.md` + `design/console-plan.md` (batches B1–B10) executing outstanding tasks T019–T037/T051 plus new console tasks T052–T059.
- **Live progress ledger**: `.superpowers/sdd/tasks/progress.md` (recreated for part 2; scratch, points at this log). Completed batches and rulings R18–R31 accumulate there and in the tables above.
- **Known gap — built-but-unwired (ruling R24)**: `limiter/retry.py::RetryQueue`, `scheduler/state_machine.py::retry_or_dlq`/`reclaim_stale`/`pel_size`, `scheduler/tiering.py::order_shards`, `discover/since_scan.py::plan_id_ranges` remain deliberately unwired for the single-consumer path.
- **Morning leftovers**: branch integration decision (merge/PR/keep), the residual minors ledger, and any parked findings surfaced by the final console review.
- **Temporary workspace**: `.superpowers/sdd/tasks/` (gitignored) holds the SDD ledger, task briefs, reports, and review packages. It is scratch — this log is the durable mirror; the workspace is deleted after the final whole-branch review.
- **Execution process**: subagent-driven development — fresh implementer per task/batch, scripted task briefs, spec+quality review after each batch, scoped re-review per fix round, whole-branch review at the end. Deferred findings above are triaged at that final review.

## Console build run — completion record (2026-10-01 overnight)

**Scope executed:** US2 within-run lifecycle (T019–T023), US3 enrichment + serve (T028–T037, T051), and US4 operator console (T052–T059: persistence, executor, library, diff, UI shell/detail/history/library, keyboard/dark, polish). Batches B1–B9 + final review + one fix wave, all on `001-gitcrawl`.

**Result:** **1020 tests passing first-hand (live golden-org + delta included)**, ruff/black clean, working tree clean. Final layered whole-branch review (3 reviewers: US2+enrichment / backend serve / clone+UI) → one fix wave (`918339e`) → scoped re-review: all items addressed; one latent residual parked (below). Head: `918339e`.

**How to run the app** (details in `environment.md` → "Running the app"):
```powershell
$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m serve
# then open http://127.0.0.1:8000/  (dashboard; /find, /runs, /filters, /health)
```

**Rulings R32–R57 (append to the table above):**

| # | Ruling | Cost if wrong |
|---|---|---|
| R32 | No GraphQL pre-flight dryRun (not a query feature); cost from `rateLimit` + guardrails | cost gate less precise |
| R33 | `request_with_retry` gains `json_body`/`extra_headers` | refactor needed |
| R34 | Persistent WSL session keeps the localhost relay alive for Redis | live calls flake |
| R35 | T031 scope = zero-auth mirrors; GitHub-REST enrichments deferred | fewer fields |
| R36 | Gazetter collisions by table order (injectable gazetteer later) | rare wrong country |
| R37 | Weak tz tiebreak only for guaranteed-unique bands | fewer resolutions |
| R38 | Console migration is `0003_console` (0002 was consumed) | doc mismatch |
| R39 | Executor mechanics use an injectable runner; production runner in B5b | small refactor |
| R40 | B5 split into three reviewable batches | review cadence |
| R41 | Filter-spec `frame` opaque/verbatim until T038 | unvalidated frame |
| R42 | ISO validity via `KNOWN_ISO_CODES` export | move helper |
| R43 | `DiscoveryStats.repo_ids` collected by the pipeline | small refactor |
| R44 | `min_commits`/`min_loc` warn + mark incomplete (no data source) | partial flagged |
| R45 | `count_total` exposed and reused | duplication |
| R46 | Hand-rolled CSS (R31 fallback), htmx vendored (sha256 recorded) | style differs from Tailwind |
| R47 | Regenerated exports marked; raw stays file-only | degraded fallback marked |
| R48 | Form at `/find` with `/vsearch/` alias | route naming |
| R49 | Minimal run page in B5d; B8 upgraded it | placeholder churn |
| R50 | Download-as-JSON = server-built spec action | parity drift |
| R51 | Planner/segments wired into the runner | runner churn |
| R52 | `field_stats` on payload + bundle (no DB column) | less provenance |
| R53 | Clone progress in registry + bundle JSON file | lost on restart |
| R54 | Canonical clone routes (spec amended) | B8 wiring mismatch |
| R55 | Final fix-wave scope (C1 truncation, I1 executor, I2 replay status, auth strip, FK migration 0004, minors) | larger wave |
| R56 | API paths execute via FIFO executor with per-run wait; browser POSTs enqueue+303 | latency/loop semantics |
| R57 | GraphQL/mirrors/full-depth remain built-but-unwired (like R24); clamp + warning | missing enrichment fields |
| R58 | Supersedes the R24/R57 quarantine policy: unwired surface is triaged, not preserved. Wiring: `reclaim_stale` (crash reclaim), `pel_size` (SLO metric). Deletions: `RetryQueue`, `order_shards`, `plan_id_ranges`, `mirrors`, `graphql_batch`, `fetch_metafiles`, `skeleton.py`. Parked minors closed: metafiles auth (fixed), executor future eviction (fixed). Rationale: dead code is a trap; deleted work is recoverable from git. | lost work if an exploration later claims a deleted module (re-add is a fresh request) |

**Parked residuals (closed 2026-10-02; kept as a closure record):**
- `trees_first.fetch_metafiles` auth: fixed in `00692a0` (token kept off the metafiles host); the module itself was deleted under R58 in `5fafdca`.
- Executor `_futures` eviction: fixed — done futures are pruned on `submit` (`src/serve/executor.py`). `/vsearch/repos` cache-misses still share the FIFO queue (unchanged, local/minor).
- Lazy executor/runner init: serialized in `def016b` (row 13); concurrency regression at `tests/unit/test_app_singletons.py::test_lazy_loader_constructs_once_under_concurrency`.
- 0004 downgrade lossiness for NULL `repo_id` rows: documented in the migration's `downgrade` docstring (export those rows before downgrading).
- Keyboard/JS behavior: markup-tested plus a manual console pass checklist in `docs/environment.md`.
- `runs.error`: sanitized to exception type + status text (≤300 chars, no upstream bodies) in `src/serve/executor.py::_error_message`.

## Quality hardening program (2026-10-01)

**Entry points:** `docs/superpowers/specs/2026-10-01-quality-hardening-design.md` (freeze contract, phases, acceptance), with plans `2026-10-01-ci-and-regression-harness.md` (Phase 0), `2026-10-01-zero-behavior-fixes.md` (Phase 1a), `2026-10-01-test-and-tooling-hardening.md` (Phase 1b), `2026-10-01-adversarial-hardening.md` (Phase 2), `2026-10-01-ux-polish.md` (Phase 3).

**Gates added by Phase 1b:**

- Fixtures: 21 `clean` fixtures plus `test_executor.py::db` (22 fixtures across 22 files, including `test_clone_registry.py` and the Phase 2 `tests/contract/test_adversarial_hardening.py`) now call the `clean_db` factory in `tests/conftest.py`; two in-test truncates in `test_upserts.py` were adapted to call `clean_db()` directly. `tests/unit/test_fixture_centralization.py` rejects raw `TRUNCATE TABLE` outside the factory and three allowlisted single-purpose fixtures.
- CWD: `tests/conftest.py::repo_root` anchors asset paths; `tests/unit/test_cwd_independence.py` runs representative tests from a foreign directory and rejects relative `Path("…")` literals.
- Flakes: no wall-clock assertions remain; ordering tests use threads events/barriers and an append-operation counter (`test_id_accumulation.py`, `test_executor.py`, `test_cloner.py`, `test_filter_form.py`).
- JS: pure helpers live in `src/serve/static/applib.js`; `tests/js/applib.test.mjs` + `tests/unit/test_applib_js.py` cover the row cache, clone-start guard, clone limit parsing, editable-target detection, and toast messages.
- Quarantine: `tests/quarantine_manifest.txt` lists every intentionally-unwired module/function (13 entries); `tests/unit/test_quarantine_manifest.py` fails when a `src` module loses test coverage or a manifest entry loses its test.
- Types: `mypy` (pinned in `requirements-dev.txt`; config in `pyproject.toml`) gates `src/lib`, `src/limiter`, `src/store`, `src/scheduler` with `disallow_untyped_defs` for `limiter.buckets`/`limiter.classifier`; ratchet policy in the config comments; `Deps.token_id` renamed to `Deps.token_fp`.
- Layering: `src/lib/audit.py` (was `src/serve/audit.py`) is core's telemetry dependency; `tests/unit/test_import_boundaries.py` enforces core-never-imports-`serve` and breaks the `store`↔`hydrate` runtime cycle.

**Run the gates:**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m mypy
node --test tests/js/rownav.test.mjs tests/js/applib.test.mjs
```

## Repo/git facts

- `main` holds the frozen baseline (`717c9b3`); all work happens on `001-gitcrawl`.
- `.gitignore` excludes `.venv`, `runs/`, `clones/`, `.superpowers/`, `.env*`.
- `docs/legal-gates.md` (compliance checklist) and `docs/environment.md` (this setup) are tracked; **no secrets are tracked anywhere**.

## Quality hardening Phase 0 — regression harness (2026-10-02)

| Item | Result |
|---|---|
| Coverage baseline | 93.41% total (4965 statements, 327 missed), Windows/Python 3.12.10, 1093 tests with `TEST_DATABASE_URL` set; `pytest -q --cov=src --cov-report=term-missing` exit 0 |
| Floor | `fail_under = 93` in `pyproject.toml`; `--cov-fail-under=94` probe fails with `Coverage failure: total of 93 is less than fail-under=94` |
| Ratchet policy | Raise `fail_under` by 1 whenever a phase's measured total exceeds the floor by at least 2 points; never lower it; record each change here |
| Lowest modules (measured) | `src/skeleton.py` 0% (135 statements, missing lines 1-189); `src/serve/runner.py` 89% (39 missed); `src/serve/__main__.py` 89% (1 missed, line 15); `src/serve/app.py` 92% (27 missed); `src/discover/org_enum.py` 92% (3 missed, lines 53-56); `src/serve/pages.py` 93% (36 missed); everything else 93-100% |
| Untested at baseline | `src/skeleton.py` 0% (excluded from black/ruff; quarantined in Phase 1b); `static/app.js` has no pytest coverage (`tests/js/rownav.test.mjs` covers one helper via Node); real-Redis client paths run against `fakeredis` only. Spec §4.4's `cloner.free_disk_mb`/`cloner._default_git_runner` entry is stale: measured `src/enrich/cloner.py` is 99% with exactly 1 missed statement — line 231, the `clone_timeout`-specified `_default_git_runner` call |
| CI | `.github/workflows/ci.yml`: ubuntu-latest `postgres:17` + `redis:7` services; windows-latest native PostgreSQL 17 + Memurai 4.1.8; `GITCRAWL_REQUIRE_TEST_DB=1`; both `Tests` steps now run `pytest -q -rs --cov=src --cov-report=term-missing` |
| Golden harness | `tests/golden/` snapshots 23 GET-route cases; JSON byte-exact, HTML normalized (CSRF, relative time, disk warning); regenerate with `UPDATE_GOLDEN=1 pytest tests/golden -q` |
| OpenAPI pin | `tests/golden/snapshots/openapi.sha256` pins canonical `app.openapi()`; a schema change fails CI |

## Quality hardening operations (2026-10-02)

- **Host allowlist** (`src/serve/app.py::_allowed_hosts`): loopback defaults `localhost`, `127.0.0.1`, `[::1]`; `GITCRAWL_ALLOWED_HOSTS` adds comma-separated extras. Behind a TLS-terminating proxy set it to the public hostname and run uvicorn with `--proxy-headers`/`--forwarded-allow-ips` so Origin/scheme checks see the effective base URL.
- **Deadlines** (`src/lib/deadlines.py`): `GITCRAWL_REQUEST_DEADLINE_SECONDS` (default 3600) bounds API requests; `GITCRAWL_CLONE_TIMEOUT_SECONDS` (default 1800) bounds each clone. A hung request fails with 503 `{"error":"timeout",...}`; a clone that exceeds its timeout is killed and recorded as failed instead of hanging forever. Missing, non-numeric, or non-positive values fall back to the defaults.
- **Redis strictness** (`src/serve/runner.py::_redis_or_fake`): with `GITCRAWL_REDIS_STRICT=1`, an unreachable `REDIS_URL` refuses the fakeredis fallback; otherwise the outage is logged and `/health` reports `redis_degraded`.
- **Body caps** (`src/serve/middleware.py`): state-changing request bodies default to 1 MiB; `/find` uploads allow 10 MiB. Oversized requests get 413 `{"error":"payload_too_large",...}`.
- **Clone git environment** (`src/enrich/cloner.py::_git_env`): clones run with `GIT_TERMINAL_PROMPT=0`, stdin closed, and global/system git config disabled (`GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL` pointed at the null device). A machine that relies on global `http.proxy`/custom CA/`url.*.insteadOf` config must set it per-repo or via environment instead.
- **Run counters (E1)**: run pages now show real `updated`/`unchanged`/`skipped` counters (`src/serve/templates/partials/status.html`).

## Unwired-debt triage Phase 1 (2026-10-02)

- Executed tasks 1–4 of `docs/superpowers/plans/2026-10-02-unwired-debt-triage.md`; ruling R58 recorded (commit 35dbd5a).
- Wiring: `reclaim_stale` reclaimed before the claim loop in `src/discover/pipeline.py` (225d169, test isolation c8e89c3).
- Deletions per R58: `RetryQueue`, `order_shards`, `plan_id_ranges`, `mirrors`, `graphql_batch`, `fetch_metafiles`, `skeleton.py` and their tests; quarantine manifest updated (5fafdca).
- Parked-minor fixes: `_error_message` sanitizes run errors (no upstream bodies), migration 0004 downgrade note, console manual checklist; lazy-loader lock was already fixed in def016b (7951b7e).
- Verification: full suite 1140 tests collected/passed, coverage 96.12% (floor 93); ruff + black clean.

## QA reconciliation (2026-10-02)

- Task 1: computed run quality report in `src/serve/quality.py` (`acb427f`).
- Task 2: `GET /runs/{run_id}/quality` (JSON) and `GET /partials/runs/{run_id}/quality` (htmx fragment); the panel loads on page load for every run-detail page because the `runs` table has no kind column (find-only gating is deferred). Both routes pass the app's injectable clock into `run_quality`, so `generated_at` is deterministic in tests/snapshots.
- OpenAPI pin re-pinned for the two new routes: `5d46f629b1aa80164f3cada46de8948763bb8fc6173dc6e620c1582dea324e05` → `3a025995ea6f8263faa1f68595c6b986088f639de71309c63faeb86ec6b9ad3c`; golden snapshots regenerated (new `runs_run_id_quality.json`, `partials_runs_run_id_quality.json`; `runs_run_id.json` gains the loading section).

## QA reconciliation Phase 2 (2026-10-02)

- Executed tasks 1–2 of `docs/superpowers/plans/2026-10-02-qa-reconciliation.md`: quality module `src/serve/quality.py` (acb427f), JSON + partial routes and run-detail panel (8649b96).
- Execution rulings: null-warn ratio 0.5 (clean-run seed tolerance), injectable clock for deterministic `generated_at`, golden `PATH_PARAMS` + snapshots regenerated, unknown-run 404s, read-only module.
- Verification: full suite 1150 passed, coverage 96.11% (floor 93); ruff + black clean; golden suite green with the run-detail change insertion-only.

## Run resume Phase 3 (2026-10-02)

- Orphan recovery: startup marks queued/running runs `failed: orphaned`, idempotently (`bfa6c42`, isolation `7f68e73`).
- Resume route: `POST /runs/{run_id}/resume` re-queues only failed runs, resets items/artifacts first, and reuses the stored `filter_spec`/`filter_hash`; detect-kind branch is present behind `row.get("kind") == "detect"` but unreachable until the detection plan lands (`2bc9216`, `aaac0eb`).
- Console: Resume form renders on failed run-detail pages only; Jinja whitespace control keeps done/partial output byte-identical (`src/serve/templates/run_detail.html`).
- OpenAPI re-pin for the resume POST route: `3a025995ea6f8263faa1f68595c6b986088f639de71309c63faeb86ec6b9ad3c` → `7ed80e2ee95d9113cf040f05e5c574f774fdc705404b6cf828abd2f22fbf3db1`; `runs_run_id.json` unchanged.
- Runbook: `docs/environment.md` "After a crash" (restart marks orphans failed; resume from the run page or console; upserts make re-fetch safe).
- Verification: full suite 1157 collected/passed, coverage 95.79% (floor 93); ruff + black clean; golden suite green; the detect branch is present but unreachable/untested until the detection plan lands.

## SLO dashboard Phase 4 (2026-10-02)

- Metrics payload (`src/serve/metrics.py::metrics_payload`, Task 1) wraps `lib.audit.slo_snapshot` and adds limiter pause state, queue PEL, and run counts by status.
- Routes: `GET /api/metrics` (JSON), `GET /metrics` (cards page), `GET /partials/metrics` (htmx fragment, refreshes `every 10s`). Redis access is injectable via `create_app(metrics_redis=...)`; when Redis is unavailable the payload degrades (`limiter.degraded`/`queue.degraded`) and cards render `n/a` for missing values instead of failing.
- Thresholds (`src/serve/metrics.py::THRESHOLDS`) map each SLO to `warn`/`fail` boundaries and card classes; an explicit `LABELS` map supplies display names. The dashboard is read-only: no DB or Redis writes in the metrics module or routes (the `pel_size` group creation is pre-existing triage behavior).
- OpenAPI re-pin for the three GET routes: `7ed80e2ee95d9113cf040f05e5c574f774fdc705404b6cf828abd2f22fbf3db1` → `1c5bdb6423fff93eeb2f4f93ff40548ae4d63970d3cdb8844f8109f2dab97f64`; golden snapshots regenerated (new `metrics.json`, `partials_metrics.json`, `api_metrics.json`; `openapi_json.json` changed; `runs_run_id.json` and all other snapshots unchanged). `build_golden_app` injects `metrics_redis=lambda: None` so the new snapshots are environment-independent.
- Verification: full suite 1165 passed, coverage 95.75% (floor 93); ruff + black clean; golden suite green.
