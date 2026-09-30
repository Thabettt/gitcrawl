# gitcrawl Development Log

**Purpose**: durable, committed record of what has been done, decided, and is next — so nothing is lost when a session, tool, or the temporary SDD workspace disappears. Environment details live in `environment.md`.

**Updated**: 2026-10-01 · **Branch**: `001-gitcrawl` · **HEAD**: `f64bd69`

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

## Repo/git facts

- `main` holds the frozen baseline (`717c9b3`); all work happens on `001-gitcrawl`.
- `.gitignore` excludes `.venv`, `runs/`, `clones/`, `.superpowers/`, `.env*`.
- `docs/legal-gates.md` (compliance checklist) and `docs/environment.md` (this setup) are tracked; **no secrets are tracked anywhere**.
