# gitcrawl Development Log

**Purpose**: durable, committed record of what has been done, decided, and is next — so nothing is lost when a session, tool, or the temporary SDD workspace disappears. Environment details live in `environment.md`.

**Updated**: 2026-10-01 · **Branch**: `001-gitcrawl` · **HEAD**: `784117b`

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

Test status at `784117b`: **140 tests passing** (`tests/unit/`), `ruff` + `black` clean.

**Reviews conducted**: T000 (approved), Phase 1 batch (approved), Phase 2a (1 Important finding → 1 fix round → approved). Every batch passed a spec-compliance + code-quality gate before completion.

## Deferred Minor findings (open, for final review triage)

- `src/skeleton.py`: wire header casing via `urllib.add_header` (`X-github-api-version`); `Retry-After` HTTP-date form unparsed; `json.loads` of a 200 body unguarded. File is currently `extend-exclude`d from ruff/black — fix it and drop the excludes in a later task.
- `tests/unit/test_gh_client.py`: default-timeout test exercises the helper's explicit value, not `create_client`'s own default.
- `src/limiter/buckets.py`: `update_from_headers` reconciles when `remaining` parses even if `reset` is malformed; `KeyError` for resources outside the three specs; Lua identifier injection via `.replace` is fragile.
- `src/lib/qualify.py`: bare `props` (no colon) is treated as a keyword.
- Redundant `.gitkeep` files in populated dirs (`src/lib`, `tests/unit`, `docs`).
- Commit `f8cd570` message mentions a "fingerprint logging helper" that does not exist (helper is `token_fingerprint`).

## Current status & how to resume

- **Done**: T000; Phase 1 (T001–T004); Phase 2a (T005–T007 + fix round). **Next**: Phase 2b — T008 (models + Alembic) and T009 (audit + SLO), brief already written at `.superpowers/sdd/tasks/task-8-9-brief.md`.
- **Blocker resolved**: DB/Redis/token provisioning is complete; the only remaining step is an **opencode restart** so this session inherits the new User env vars. After restart, continue the session and dispatch Phase 2b, then US1 (T010–T018, golden-org parity).
- **Temporary workspace**: `.superpowers/sdd/tasks/` (gitignored) holds the SDD ledger, task briefs, reports, and review packages. It is scratch — this log is the durable mirror; the workspace is deleted after the final whole-branch review.
- **Execution process**: subagent-driven development — fresh implementer per task/batch, scripted task briefs, spec+quality review after each batch, scoped re-review per fix round, whole-branch review at the end. Deferred findings above are triaged at that final review.

## Repo/git facts

- `main` holds the frozen baseline (`717c9b3`); all work happens on `001-gitcrawl`.
- `.gitignore` excludes `.venv`, `runs/`, `clones/`, `.superpowers/`, `.env*`.
- `docs/legal-gates.md` (compliance checklist) and `docs/environment.md` (this setup) are tracked; **no secrets are tracked anywhere**.
