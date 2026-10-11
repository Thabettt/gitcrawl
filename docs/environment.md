# Development Environment (Windows, local)

**Provisioned**: 2026-10-01 · **Host**: ZENBOOK (Windows, user `ZENBOOK\Abdul`, non-elevated shell)
Companion: `development-log.md` (what was built and why) · `design/plan.md` (stack decisions)

This document records every service, credential location, env var, and start/stop command for the local gitcrawl development environment, so nothing depends on session memory.

## At a glance

| Service | Endpoint | Started by | Auto-start | Credentials live in |
|---|---|---|---|---|
| PostgreSQL 18.1 (project-local cluster) | `localhost:5433` | `pg_ctl` (user process, no Windows service) | logon task `gitcrawl-postgres` | User env `DATABASE_URL` / `TEST_DATABASE_URL` |
| Redis 7 (WSL2 Ubuntu service) | `redis://localhost:6379/0` | WSL `service redis-server` | logon task `gitcrawl-redis` | none (localhost only) |
| GitHub API | `https://api.github.com` | app code (`httpx`) | — | User env `GITHUB_TOKEN` |

All env vars are set at **User** level (`HKCU\Environment`), never committed to the repo. A running opencode process does **not** see env vars set after it started — restart opencode after any change.

## Why a project-local PostgreSQL instead of the machine's PG service

The machine runs `postgresql-x64-18` on port **5432** (the user's own instance). We did not use it because:

- `pg_hba.conf` requires `scram-sha-256` for all local TCP connections and we do not have its superuser password;
- editing `pg_hba.conf` requires elevation and we deliberately did not modify the user's existing server;
- the shell is **not elevated** (`elevated: False`), so service installs / admin changes are out anyway.

Instead, a **second, project-local cluster** was initialized from the same PG 18 binaries into a user-writable directory. The user's 5432 service is untouched.

## PostgreSQL — project-local cluster

- **Data dir**: `%LOCALAPPDATA%\gitcrawl\pgdata`
- **Log**: `%LOCALAPPDATA%\gitcrawl\pg.log`
- **Port**: `5433` (set in `postgresql.conf`: `port = 5433`, `listen_addresses = 'localhost'`)
- **Role (superuser of this cluster)**: `gitcrawl`
- **Databases**: `gitcrawl` (dev), `gitcrawl_test` (tests — migration tests upgrade/downgrade it)
- **Auth**: `scram-sha-256` for `127.0.0.1`/`::1` (generated 28-char alphanumeric password)

### How it was created

```powershell
$bin = 'C:\Program Files\PostgreSQL\18\bin'
$data = "$env:LOCALAPPDATA\gitcrawl\pgdata"
# password generated in-memory, written to a temp --pwfile, deleted afterwards
& "$bin\initdb.exe" -D $data -U gitcrawl --auth-host=scram-sha-256 --auth-local=scram-sha-256 `
  --pwfile=<temp-file> --encoding=UTF8 --locale=C
Add-Content "$data\postgresql.conf" "`nport = 5433`nlisten_addresses = 'localhost'"
& "$bin\pg_ctl.exe" start -D $data -l "$env:LOCALAPPDATA\gitcrawl\pg.log"
& "$bin\createdb.exe" -h localhost -p 5433 -U gitcrawl gitcrawl
& "$bin\createdb.exe" -h localhost -p 5433 -U gitcrawl gitcrawl_test
```

The generated password exists **only** inside the two User-level env vars (below). It is not written to the repo, not in git, not in this doc.

### Start / stop / status

```powershell
$bin = 'C:\Program Files\PostgreSQL\18\bin'
$data = "$env:LOCALAPPDATA\gitcrawl\pgdata"

# start (detached — run via Start-Process in scripts so no console pipe is held)
Start-Process -FilePath "$bin\pg_ctl.exe" -ArgumentList @('start','-D',$data,'-l',"$env:LOCALAPPDATA\gitcrawl\pg.log") -WindowStyle Hidden

& "$bin\pg_ctl.exe" status -D $data          # status
& "$bin\pg_isready.exe" -h localhost -p 5433 # readiness
& "$bin\pg_ctl.exe" stop -D $data -m fast    # stop
```

### Connecting

```powershell
$dsn = [Environment]::GetEnvironmentVariable('DATABASE_URL','User')   # contains the password — do not paste into logs
$env:PGPASSWORD = [regex]::Match($dsn,'://[^:]+:([^@]+)@').Groups[1].Value
& "$bin\psql.exe" -h localhost -p 5433 -U gitcrawl -d gitcrawl -w -c 'select 1'
Remove-Item Env:PGPASSWORD
```

### Password rotation / rebuild

- **Rotate**: connect (as above) → `ALTER ROLE gitcrawl PASSWORD '<new>';` → update both User env vars with the new DSN → restart any process that had the old DSN.
- **Rebuild from scratch** (destroys data): `pg_ctl stop -D $data -m fast`; delete `%LOCALAPPDATA%\gitcrawl\pgdata`; repeat "How it was created".

## Redis — WSL2 Ubuntu

Redis was **already installed** in the WSL2 `Ubuntu` distro (`/usr/bin/redis-server`); no package install was needed. It runs as a distro service:

```powershell
wsl -u root -- service redis-server start     # start
wsl -u root -- service redis-server status    # status
wsl -u root -- redis-cli ping                 # expect: PONG
```

Verified from Windows (WSL2 localhost forwarding): `.venv\Scripts\python.exe -c "import redis; print(redis.Redis(host='localhost',port=6379).ping())"` → `True`.

- Config: `/etc/redis/redis.conf` (bind `127.0.0.1`), data `/var/lib/redis/`.
- Unit tests use **fakeredis** so they run without any server; real Redis is the integration/validation target.
- If WSL is shut down, `wsl -u root -- service redis-server start` brings both up.

## Auto-start at logon (non-elevated scheduled tasks)

Two per-user scheduled tasks start the services after login:

| Task | Action script | Effect |
|---|---|---|
| `gitcrawl-postgres` | `%LOCALAPPDATA%\gitcrawl\start-postgres.ps1` | `pg_ctl start` the 5433 cluster |
| `gitcrawl-redis` | `%LOCALAPPDATA%\gitcrawl\start-redis.ps1` | `wsl -u root -- service redis-server start` |

Both tasks have the power conditions **disabled** (`DisallowStartIfOnBatteries=false`, `StopIfGoingOnBatteries=false`) — set 2026-10-10 after a reboot on battery left the tasks stuck in `Queued` and `./start` timed out. On this laptop the services must start regardless of AC/battery; if a task ever sits `Queued`, check these two settings first:

```powershell
(Export-ScheduledTask -TaskName gitcrawl-postgres) -match 'Batteries'
$t = Get-ScheduledTask -TaskName gitcrawl-postgres
$t.Settings.DisallowStartIfOnBatteries = $false
$t.Settings.StopIfGoingOnBatteries = $false
Set-ScheduledTask -TaskName gitcrawl-postgres -Settings $t.Settings   # repeat for gitcrawl-redis
```

Manual control:

```powershell
Start-ScheduledTask -TaskName gitcrawl-postgres
Unregister-ScheduledTask -TaskName gitcrawl-postgres -Confirm:$false   # remove
Unregister-ScheduledTask -TaskName gitcrawl-redis -Confirm:$false
```

## Environment variables (User level, secrets never committed)

| Name | Value (shape) | Used by |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://gitcrawl:<pw>@localhost:5433/gitcrawl` | Alembic, app |
| `TEST_DATABASE_URL` | `postgresql+psycopg://gitcrawl:<pw>@localhost:5433/gitcrawl_test` | DB integration tests (destructive-safe) |
| `REDIS_URL` | `redis://localhost:6379/0` | limiter buckets at runtime |
| `GITHUB_TOKEN` | OAuth token from the authenticated `gh` CLI keyring | live GitHub calls (fingerprint-only logging) |
| `GITCRAWL_ADAPTIVE` | `1` force-enables the adaptive rate controller (normally toggled on `/settings`; default off) | hydration batching (`limiter/adaptive`) |

**Set/update pattern** (no secret echoed):

```powershell
[Environment]::SetEnvironmentVariable('NAME','value','User')
[Environment]::GetEnvironmentVariable('NAME','User')   # retrieve
```

### GitHub token sourcing

`gh auth status` shows the machine is logged in to `github.com` as `Thabettt`; `GITHUB_TOKEN` was set by piping `gh auth token` directly into the User env var (the value was never printed or written to disk). Notes:

- Current token is a broad OAuth token (scopes: `gist`, `read:org`, `repo`) — acceptable for read-only public discovery, broader than the plan's preferred fine-grained `metadata:read` PAT.
- To switch to a least-privilege PAT later: create it in the GitHub UI, then set `GITHUB_TOKEN` via the pattern above and restart opencode.
- Never log or store the raw token; audit records only its SHA-256 fingerprint (see `src/lib/gh_client.py`).

## Handling the opencode restart requirement

Windows processes inherit the environment of their parent; an already-running opencode does not see newly-set User env vars. Two options:

1. **Restart opencode** (clean; all tools/subagents inherit the vars) — the supported path.
2. Per-command injection (fallback): read the registry into the process env inside a command, e.g. `$env:DATABASE_URL = [Environment]::GetEnvironmentVariable('DATABASE_URL','User')`. Avoid for `GITHUB_TOKEN` unless piped from `gh` — never type a token into a command line.

## Running the app

The operator console runs from the repo root with `PYTHONPATH=src` so `serve.app` imports work; env vars come from the User-level table above (restart opencode first if they were just set).

```powershell
$env:PYTHONPATH = 'src'
.venv\Scripts\python.exe -m serve
# equivalent explicit form:
$env:PYTHONPATH = 'src'
.venv\Scripts\python.exe -m uvicorn serve.app:app --host 127.0.0.1 --port 8000
```

`GITCRAWL_HOST` / `GITCRAWL_PORT` override host/port for `python -m serve` (defaults `127.0.0.1:8000`). The console binds localhost only, has no auth (single operator), and never renders secrets.

### Start / restart from bash

Two root scripts bring the whole stack up from Git Bash (they orchestrate the Windows dev services,
so they do not apply to the Compose/macOS/Linux paths):

```bash
./start      # ensure Postgres + Redis, apply migrations, launch the server detached
./restart    # stop the running server, then run the full start flow
```

- The server runs detached; its PID lives in `%LOCALAPPDATA%\gitcrawl\serve.pid` and stdout/stderr go
  to `serve.log` / `serve.err.log` in the same folder as `pg.log`. `start` prints the exact log path
  and a `tail -f` command when it finishes.
- `start` is idempotent: if the recorded PID is alive it prints the URL and exits 0. It fails early
  with a clear message when `DATABASE_URL`, `REDIS_URL`, or `GITHUB_TOKEN` are missing, when the venv
  is absent, or when port 8000 is already taken.
- Down services are started through the existing `gitcrawl-postgres` / `gitcrawl-redis` scheduled
  tasks (Redis includes the WSL keeper session) and then polled for up to 30 s.
- `restart` stops only the app process; Postgres and Redis are checked but not bounced, because the
  test suites share them.

| Path | What it does |
|---|---|
| `/` | Dashboard: health badges, quick find, recent runs |
| `/find` | Full filter form (Find / Download as JSON / Upload and run / Save filter) |
| `/runs` | Run history (`?status=`, `?hash=` exact or prefix, `?page=`, 50/page) |
| `/runs/{id}` | Run detail: status polling, live phase/ETA, stop, save-filter, sortable results, flags, export/replay/clone, compare-with |
| `/runs/{id}/results` | Full-width result table; every page, sort, and page size lives in the URL (`?page=&per_page=&sort=&dir=`) |
| `/runs/{id}/diff?against={baseline_id}` | Compare this search with an earlier one; baseline defaults to the previous same-filter run |
| `/corpora` | Frozen corpora (`/corpora/{id}` detail): name, repos, frozen time, source search |
| `/filters` | Saved filter library: browsers (Accept `text/html`) get the page, other clients get the JSON API |
| `/system` | System: Status / Performance / Limits (the human face of `/health`; the header status dot links here) |
| `/settings` | System → Limits: run caps, request deadline, batching, concurrency |
| `/health` | DB/Redis/token-present booleans only |

- **No JS**: every read path (dashboard, history, diff, library, run table) is server-rendered, and forms POST normally with the hidden CSRF field; keyboard shortcuts, htmx polling/fragments, and the clone modal are enhancements.
- **No Node / no Tailwind at runtime**: styling is the hand-rolled `src/serve/static/app.css` (R46); `htmx.min.js` and `app.js` are vendored and committed — no CDN or build step.
- **Keyboard**: `/` focuses quick search, `g h`/`g f`/`g r`/`g l` navigate, `j`/`k` select table rows, `Enter` opens the selected row, `?` toggles the shortcut help, `Esc` closes dialogs.
- **Redis keeper session (R34)**: keep the hidden `wsl.exe -u root -- sleep infinity` session alive so WSL2 localhost forwarding stays up; the `gitcrawl-redis` logon task starts Redis plus that keeper. If `/health` shows Redis down, run `wsl -u root -- service redis-server status` and reopen the keeper.

## Tests and gates

```powershell
$env:PYTHONPATH = 'src'
.venv\Scripts\python.exe -m pytest -q                                       # full suite; DB tests need TEST_DATABASE_URL, JS wrappers skip without Node
.venv\Scripts\python.exe -m pytest tests/unit                              # hermetic unit tests (no services)
.venv\Scripts\python.exe -m pytest -q --cov=src --cov-report=term-missing  # coverage gate: fail_under = 93
.venv\Scripts\python.exe -m ruff check src tests                           # lint (rules E, F, I, UP, B; line length 100)
.venv\Scripts\python.exe -m black --check src tests                        # formatting
.venv\Scripts\python.exe -m mypy                                           # gated packages: lib, limiter, store, scheduler
node --test tests/js/*.test.mjs                                            # JS helper tests (optional)
```

`UPDATE_GOLDEN=1 pytest tests/golden` regenerates the golden snapshots after an intended console/API change (the OpenAPI schema hash is pinned there too). The full matrix and CI notes live in `README.md`.

## Corpus-build profile (System → Limits vs environment)

Open **System → Limits (the `/settings` page)** to raise the run limits for corpus builds. The page
persists to the database and applies to new runs only. Environment variables pin a field (the page
shows it read-only):

| Field | Environment variable | Default | Corpus-build example |
|---|---|---|---|
| Run shards | `GITCRAWL_MAX_SHARDS` | 10 | 1000 |
| Candidates | `GITCRAWL_MAX_CANDIDATES` | 500 | 100000 |
| Hydrations | `GITCRAWL_MAX_HYDRATE` | 200 | 100000 |
| Enrichment checks | `GITCRAWL_MAX_ENRICH` | 100 | 100000 |
| Request deadline (s) | `GITCRAWL_REQUEST_DEADLINE_SECONDS` | 3600 | 86400 |
| GraphQL batching | `GITCRAWL_GRAPHQL_BATCH` | on | on |
| Batch size | `GITCRAWL_GRAPHQL_BATCH_SIZE` | 29 | 29 |
| Adaptive pacing | `GITCRAWL_ADAPTIVE` | off | off |
| Concurrency | `GITCRAWL_MAX_CONCURRENT` | 10 | 32 |
| Discovery workers | `GITCRAWL_DISCOVERY_CONCURRENCY` | 32 | 32 |

These are the values the **Set to corpus-build limits** button applies; the ranges shown on the page are safety limits, and the preset sits well inside them. The derivation (and the multi-token scale-up) lives in `design/run-limits.md`.

- `GITCRAWL_GRAPHQL_BATCH_SIZE` env overrides are clamped to 1..50 at settings load (a value above the API cap clamps to 50 instead of failing every run); the default stays 29 (the largest batch that still costs one GraphQL point).
- `GITCRAWL_MAX_CONCURRENT` is the limiter's cap **and** the hydration driver: each new run snapshots `min(limiter_max_concurrent, 32)` as the GraphQL batch concurrency. The corpus preset is 32, raised from the profiled 10 (which ran ~2,800 repos/min live against ~300–400 sequential); the per-run point cost is unchanged, so watch for secondary-limit 403s — the classifier backoff already handles them. The 32-worker ceiling stays static; the adaptive toggle below makes the submission window dynamic inside it.
- `GITCRAWL_DISCOVERY_CONCURRENCY` is the discovery pool size: each run plans shards with batched GraphQL count probes, then fetches pages with one GraphQL search connection per worker. Default and corpus value 32 — the measured operating point (`design/corpus-building-efficient-engineering.md` §9.4).
- **Adaptive pacing** is the normal switch for the three-loop adaptive controller, a checkbox on the `/settings` page (System → Limits → Request pacing), default **off**; the corpus preset also sets it off. When a run starts with it on, the AIMD window starts at 20 (bounds 8-48), the batch guard starts at the configured batch size capped at 29 (bounds 10-29), the quota pacer paces to `(remaining - 400) / seconds-to-reset` with a burst of 4, and any 403/429/502/504/rate-limit signal pauses the pool. It emits `field_stats.graphql.hydration.adaptive` and `.deferred`. `GITCRAWL_ADAPTIVE=1` still force-enables it as an ops override (and pins the checkbox read-only, like the other env-pinned fields); the default stays off per the 2026-10-09 soak evidence (`docs/findings/2026-10-09-adaptive-soak.md`).

**Secondary limits (2026-10-11).** When GitHub answers with a secondary-rate-limit 403/429 (`retry-after: 60`, no rate-limit headers — the scraping-flavored flag), the client now pauses the whole token bucket for the wait instead of just the one worker, and repeats escalate the pool pause: 60 → 120 → 240 → 480 seconds (capped, ±30 s jitter) so workers wake desynchronized rather than in a synchronized re-knock wave. A batch pass (hydration or enrichment; discovery hits do not count) stops dispatching after 15 secondary hits: remaining queued repos are deferred, and the pass ends **partial and resumable** instead of accumulating more hits. If you see the scraping-flavored message, rest that token for **24 hours or more** — GitHub's secondary sensitivity escalates with repeated triggers and decays slowly.

A corpus run occupies the single executor for its whole duration; within it, discovery fetches
pages from the 32-worker pool before hydration begins. Run it overnight. The run page
shows live phase/done/total/ETA counters (migrations 0010/0011), but those are display-only, not
checkpoints — a resume still re-fetches from the start. A running search can be stopped (status
`cancelled`, shown as "Stopped") and resumed later; failed, partial, and cancelled runs are all
resumable.

## Migrations

Current head is **`0015`**: `0010` adds the nullable `runs.progress_*` live-progress columns, `0011` adds `progress_started_at` for the phase-relative ETA clock, `0012` adds `app_settings.discovery_concurrency` (default 32, checked 1–64), `0013` raises the `graphql_batch_size` check to 1–50, `0014` adds the three `audit_log` point-telemetry columns (`rl_used`, nullable `run_id`, nullable `phase`) plus the `audit_run_idx` index on `run_id`, and aligns `graphql_batch_size` to the new default 29 (server default plus a one-time `20 → 29` value update), and `0015` adds the `app_settings.adaptive` boolean (default false, no backfill) for the settings-page toggle. None of these rewrite a large table — the checks touch only the single-row `app_settings` — so the locking guidance below applies only to 0005–0007. Apply with `alembic upgrade head` from the repo root with `DATABASE_URL` set.

### Migration locking

Revisions 0005-0007 rewrite constraints/indexes on live tables and take write-blocking
locks on them. On a large table, run these in a maintenance window rather than during
active crawling. Suggested guards:

```sql
SET lock_timeout = '50ms';      -- fail fast instead of queueing behind readers
SET statement_timeout = '5s';
```

| Revision | Lock taken | Note |
|---|---|---|
| 0005 (`full_name_history` unique) | `ACCESS EXCLUSIVE` for the constraint build | dedupe `DELETE` is a self-join; cost scales with table size |
| 0006 (`repos_deleted_idx`) | `SHARE` on `repos` (blocks writes) | downgrade's `DROP INDEX` is `ACCESS EXCLUSIVE` |
| 0007 (`run_items_stars_idx` rewrite) | `ACCESS EXCLUSIVE` (drop) then `SHARE` (create) on `run_items` | writes stall for the whole revision |

For hot deployments, convert the index work to `CREATE UNIQUE INDEX CONCURRENTLY` /
`CREATE INDEX CONCURRENTLY` / `DROP INDEX CONCURRENTLY` in an autocommit block
(`op.get_bind().execution_options(isolation_level="AUTOCOMMIT")`) so no table-wide write
lock is held.

## Deviations from `design/plan.md`

| Plan | Here | Reason |
|---|---|---|
| PostgreSQL 17 via docker-compose | PostgreSQL 18.1, project-local cluster on 5433 | Docker Desktop deliberately skipped (WSL available); local PG 18 already installed; no admin needed |
| Redis 7 via docker-compose | WSL2 Redis 7 service | same |
| `REDIS_URL` integration | fakeredis in unit tests; real Redis for integration/validation | keeps unit tests hermetic |

`docker-compose.yml` (Postgres 17 + Redis 7) remains in the repo for environments that do have Docker; it is not used on this machine.

## Provisioning incident note

During provisioning, a combined setup command was killed by the tool's pipe handling because the freshly-started `postgres.exe` daemon inherited the tool's stdout pipe. Recovery: the never-used cluster was wiped, re-initialized, then started via `Start-Process -WindowStyle Hidden` (no pipe inheritance); DB creation and env wiring ran with `-w` (never-prompt) flags. No data was lost (nothing had been created in the cluster yet).

## Troubleshooting

- **`fe_sendauth: no password supplied`** → use the DSN extraction snippet above, or `-w` will fail fast instead of prompting.
- **`connection refused` on 5433** → cluster down: run the start command or `Start-ScheduledTask -TaskName gitcrawl-postgres`; check `%LOCALAPPDATA%\gitcrawl\pg.log`.
- **`./start` times out waiting for PostgreSQL/Redis** → check the task state (`Get-ScheduledTask -TaskName gitcrawl-postgres`); `Queued` means it never ran — on battery that is the power-condition setting (see Auto-start at logon above). A machine reboot can also kill the cluster uncleanly; the next start does automatic recovery (`pg.log` shows "automatic recovery in progress"), which may take longer than the 30 s poll.
- **Windows can't reach Redis** → `wsl -u root -- redis-cli ping` first (distro may be stopped); then the Python check. If localhost forwarding ever fails, use the WSL IP (`wsl hostname -I`) in `REDIS_URL` as a temporary fallback.
- **Env vars "missing" in a shell** → that shell predates the change; restart opencode or read them from the registry as shown above.

## Console manual pass (run before demos)
- [ ] `/`, `/find`, `/runs/{id}`, `/filters`, `/health` load without console errors
- [ ] keyboard rownav (j/k/enter/esc) works on the results table
- [ ] dark mode toggle persists
- [ ] clone modal estimate + start on a small run

## After a crash
1. Restart the app (`python -m serve`) — queued/running runs are marked `failed: orphaned` and their progress columns are cleared.
2. Open the run and click **Resume** (or `POST /runs/{id}/resume` from the console) — resume accepts failed, partial, and cancelled runs.
3. Resume re-fetches from the start; upserts make it safe. Detection runs clear their evidence first.
