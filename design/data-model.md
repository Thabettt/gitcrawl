# Data Model

**Date**: 2026-09-29 (updated 2026-10-04). Implements spec entities + `05` A3/A11 + efficiency review I5 + deep-research D10. Postgres 17+; Alembic migrations; partition by `id` range at scale.

**The one-paragraph version**: this is the schema of record. Everything is keyed on immutable GitHub IDs — never on names, because names change — with tombstones instead of deletes, ETags for cheap revalidation, and per-table vacuum/retention tuning so a large crawl stays healthy. Eleven tables are implemented today (migrations `0001`–`0009`): the core repo/owner/geo/shard/audit set, plus the console's runs/run_items/saved_filters, the single-row app_settings, and corpora. Two thesis tables (trace_packs, validation_samples) are designed here but not built — the thesis tracks are parked. Where this document and the migrations disagree, **the migrations are the authority**; this file explains the intent.

> **Status note (2026-10-04)**: the DDL below matches the implemented migrations for the core tables (minor column drift is possible — check `migrations/versions/`). The console tables were amended by migration `0004` (see the note there), and later migrations added `app_settings` (`0008`) and `corpora` (`0009`).

## Conventions

- PK is always the immutable GitHub `id BIGINT` for repos/owners. `full_name`/`login` are mutable → `UNIQUE` + history, never PK.
- Timestamps `timestamptz`. Counts `INTEGER` (widen to `BIGINT` if GitHub grows past 2B). JSONB for schemaless maps (`custom_properties`, SLO labels).
- Tombstones, never hard deletes (`deleted_at`). ETag columns for conditional hydrate.
- `repos` uses `fillfactor=80` (HOT-friendly: no-op upserts are gated in the app first — `ON CONFLICT ... WHERE` still writes WAL + takes locks); BRIN on `pushed_at` only if insert order correlates with it, else btree (PG16+ BRIN is HOT-safe).
- Per-table autovacuum: `autovacuum_vacuum_scale_factor=0.02–0.05`, `autovacuum_vacuum_insert_scale_factor=0.02` on `repos`/`audit_log`; autovacuum OFF during initial load + `VACUUM ANALYZE` after.
- Retention: `audit_log` monthly partitions via `pg_partman` + `pg_cron` (drop, never giant `DELETE`); staging tables per-batch `UNLOGGED` (visible cross-workers; TEMP is session-private) — never flip the main table (full rewrite + replica break).

## Tables

### repos (one row per GitHub repo id)

```sql
CREATE TABLE repos (
  id            BIGINT PRIMARY KEY,          -- immutable GitHub repo id
  node_id       TEXT NOT NULL,
  full_name     CITEXT NOT NULL UNIQUE,      -- owner/name, mutable → history table
  owner_id      BIGINT NOT NULL REFERENCES owners(id),
  name          TEXT NOT NULL,
  description   TEXT,
  homepage      TEXT,
  language      TEXT,                        -- primary, nullable
  license_spdx  TEXT,                        -- nullable; NOASSERTION/other preserved
  topics        TEXT[] NOT NULL DEFAULT '{}',
  visibility    TEXT NOT NULL,               -- public/private/internal
  fork          BOOLEAN NOT NULL DEFAULT FALSE,
  parent_full_name TEXT,                     -- fork chain (nullable)
  source_full_name TEXT,
  archived      BOOLEAN NOT NULL DEFAULT FALSE,
  disabled      BOOLEAN NOT NULL DEFAULT FALSE,
  mirror_url    TEXT,
  is_template   BOOLEAN NOT NULL DEFAULT FALSE,
  size_kb       INTEGER,
  stargazers    INTEGER NOT NULL DEFAULT 0,
  forks_count   INTEGER NOT NULL DEFAULT 0,
  watchers      INTEGER NOT NULL DEFAULT 0,
  open_issues   INTEGER NOT NULL DEFAULT 0,
  default_branch TEXT,
  has_wiki      BOOLEAN, has_issues BOOLEAN, has_projects BOOLEAN,
  has_pages     BOOLEAN, has_discussions BOOLEAN, has_pull_requests BOOLEAN,
  custom_properties JSONB NOT NULL DEFAULT '{}',
  created_at    TIMESTAMPTZ,
  pushed_at     TIMESTAMPTZ,                 -- current push state (ran_at snapshot)
  updated_at    TIMESTAMPTZ,
  etag          TEXT,                        -- hydrate conditional
  deleted_at    TIMESTAMPTZ,                 -- tombstone (404 → set, never delete)
  indexed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX repos_pushed_idx ON repos (pushed_at) WHERE deleted_at IS NULL;
CREATE INDEX repos_updated_idx ON repos (updated_at) WHERE deleted_at IS NULL;
CREATE INDEX repos_stars_idx ON repos (stargazers DESC) WHERE deleted_at IS NULL;
CREATE INDEX repos_owner_idx ON repos (owner_id) WHERE deleted_at IS NULL;
-- GIN(topics): build AFTER bootstrap COPY (see upserts), not during:
-- CREATE INDEX repos_topics_gin ON repos USING GIN (topics);
```

### full_name_history (rename/transfer chain)

```sql
CREATE TABLE full_name_history (
  id          BIGSERIAL PRIMARY KEY,
  repo_id     BIGINT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  full_name   CITEXT NOT NULL,
  seen_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX fnh_repo_idx ON full_name_history (repo_id, seen_at);
```

### owners (users + organizations, cached by id)

```sql
CREATE TABLE owners (
  id            BIGINT PRIMARY KEY,          -- GitHub user/org id
  login         CITEXT NOT NULL UNIQUE,      -- mutable → re-resolve, keep id
  type          TEXT NOT NULL,               -- User/Organization
  location_raw  TEXT,                        -- free-text, nullable
  country_iso   CHAR(2),                     -- resolver output, nullable
  geo_confidence TEXT,                       -- exact-iso/name/gazetteer-city/geocoder/weak; unmatched = NULL country
  company       TEXT, blog TEXT,
  etag          TEXT,
  synced_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### geo_cache (normalized location → resolution, shared across owners)

```sql
CREATE TABLE geo_cache (
  normalized  TEXT PRIMARY KEY,              -- lowercased, emoji-split, trimmed
  country_iso CHAR(2),
  confidence  TEXT NOT NULL,
  raw_sample  TEXT NOT NULL,
  hits        INTEGER NOT NULL DEFAULT 1,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### shards (discovery work units — within-run only, live-only)

```sql
CREATE TABLE shards (
  id              BIGSERIAL PRIMARY KEY,
  kind            TEXT NOT NULL,             -- search-range / since-range / org
  query           TEXT,                      -- q string for search-range (validated)
  range_start     TIMESTAMPTZ, range_end TIMESTAMPTZ,  -- created: window (search)
  since_id        BIGINT, since_max BIGINT,  -- ID window (since scan)
  org             TEXT,                      -- org/user scope (enum)
  tier            TEXT NOT NULL DEFAULT 'cold', -- within-run order hint only; NO cadences (live-only)
  state           TEXT NOT NULL DEFAULT 'pending', -- pending/active/done/incomplete (within-run)
  watermark       TIMESTAMPTZ,               -- ran_at snapshot (current max(pushed_at) seen; NOT a persistent cursor)
  total_count     INTEGER, fetched INTEGER DEFAULT 0,
  incomplete      BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX shards_state_idx ON shards (state, tier);
```

### audit_log (per-request, FR-010; partitioned monthly at scale)

```sql
CREATE TABLE audit_log (
  id              BIGSERIAL PRIMARY KEY,
  ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
  query_hash      TEXT,                      -- sha256 of normalized params
  params          JSONB NOT NULL,            -- q/sort/order/per_page/page (full)
  etag_sent       TEXT, status INTEGER NOT NULL,
  rl_limit INTEGER, rl_remaining INTEGER, rl_reset BIGINT, rl_resource TEXT,
  retry_after     INTEGER, link_next BOOLEAN,
  total_count     INTEGER, incomplete_results BOOLEAN,
  token_fp        TEXT NOT NULL,             -- hash, never raw token
  latency_ms      INTEGER NOT NULL
);
CREATE INDEX audit_ts_idx ON audit_log (ts);
```

## Console tables (US4, migration `0003_console.py` — ruling R38)

`runs`, `run_items`, `saved_filters` — exact DDL and lifecycle in `console-spec.md` §Backend additions. Run history is operator-triggered artifacts (no background jobs); `run_items` snapshots power history/diff.

**Amendment (migration `0004`, ruling R55)**: `run_items.repo_id` became nullable with `ON DELETE SET NULL` and a surrogate `id` was added, so a run snapshot survives a repo purge; the unique `(run_id, repo_id)` is retained. The `0004` downgrade is lossy for rows with a `NULL` `repo_id` (documented in the migration).

## Tables added after the first draft (implemented)

| Table | Migration | Purpose |
|---|---|---|
| `app_settings` | `0008` | Single-row run limits and toggles (`max_shards`, `max_candidates`, `max_hydrate`, `max_enrich`, `request_deadline_seconds`, `graphql_batch`, `graphql_batch_size` 1–20, `limiter_max_concurrent` 1–100); editable at `/settings`, env-pinnable |
| `corpora` | `0009` | Named frozen corpora: unique name, `source_run_id` FK → runs, note, `repo_count`, `frozen_at` |

## Planned tables (designed, not implemented — thesis tracks parked)

These are kept here as design intent. They do not exist in the database; the thesis tracks that would use them are deferred (see `tasks.md` Phase 6).

```sql
-- PLANNED (not implemented)
CREATE TABLE trace_packs (
  version     TEXT PRIMARY KEY,              -- e.g. 2026-10-01; frozen before runs
  authors     JSONB NOT NULL,                -- [{pattern, agent}]
  files       JSONB NOT NULL,
  branches    JSONB NOT NULL,
  labels      JSONB NOT NULL,
  bots        JSONB NOT NULL DEFAULT '[]',
  generic_weights JSONB NOT NULL DEFAULT '{}', -- e.g. AGENTS.md down-weighted
  exclusions  JSONB NOT NULL DEFAULT '["CONVENTIONS.md"]',
  frozen_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- PLANNED (not implemented)
CREATE TABLE validation_samples (
  id          BIGSERIAL PRIMARY KEY,
  repo_id     BIGINT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  channel     TEXT NOT NULL,                 -- commit/file/branch/pr
  stratum     TEXT NOT NULL,                 -- sampling stratum
  label_a     TEXT, label_b TEXT,            -- double labels
  adjudicated TEXT,                          -- final label after disagreements
  pack_version TEXT NOT NULL REFERENCES trace_packs(version),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX vsamp_repo_idx ON validation_samples (repo_id);
```

## Lifecycle rules (enforced in `store/lifecycle.py`)

1. Upsert on `id` only (`ON CONFLICT (id) DO UPDATE`); `full_name` conflicts insert history rows, never new repo rows.
2. `GET` 200 with `full_name` mismatch → update `repos.full_name` + history row. `301` → follow, same. `404` → set `deleted_at=now()` (quarantine from serve; purge after retention window).
3. Live-only snapshot (no persistent watermark): record current `max(pushed_at)` per shard at `ran_at`; `id`-dedupe absorbs overlap pages within the run. No 1h overlap reads across runs, no tier cadences.
4. Bootstrap: `COPY` (TEXT + `ON_ERROR ignore + REJECT_LIMIT` — PG18 forbids ON_ERROR in BINARY mode and binary silently truncates out-of-range ints, ruling R23) into per-batch `UNLOGGED` staging → merge → rebuild GIN; `INSERT...ON CONFLICT` for steady-state deltas only; `COPY ... ON_ERROR ignore` (PG17+) tolerates dirty payloads (`tuples_skipped` accounting), with `REJECT_LIMIT 100` added only on PG18+ where the server supports it; never row-by-row for >100k rows.

## Indexes recap

- `repos(pushed_at)`, `repos(updated_at)`, `repos(stargazers DESC)`, `repos(owner_id)` (all `WHERE deleted_at IS NULL`); `GIN(topics)` post-bootstrap.
- `owners(login)`, `geo_cache` PK lookup, `shards(state, tier)`, `audit_log(ts)`.

## Implemented table set (11)

`owners`, `repos`, `full_name_history`, `geo_cache`, `shards`, `audit_log`, `runs`, `run_items`, `saved_filters`, `app_settings`, `corpora` — asserted by `tests/integration/test_models_migrations.py`, which also exercises the downgrade chain.
