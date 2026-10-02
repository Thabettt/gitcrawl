# Research: Decisions Distilled from findings/00–06

**Date**: 2026-09-29 (sources accessed 2026-09-29; numbers "as of" that date, re-verify quarterly). Each decision cites its finding; full URLs live in the finding cited. For the narrative behind these decisions, see `how-the-data-flows.md`.

## D1. Dual discovery: sharded search + `since` scan + org enum (not search-only)

- Sharded `GET /search/repositories` (`created:` bisect to <1000 fetchable and <<4000 scanned, `per_page=100`, `Link` verbatim) is the only path for *filtered* queries — but unfiltered cross-GitHub backfill via `created:`-bisect costs ~1,111 hr/PAT vs `GET /repositories?since=` at ~400 hr/PAT (2.8×), plus scan distortion (`findings/02 §6`, `05` A1/A6, efficiency review C2).
- Decision: `since`-enumeration (ID-range shards, `max(id)` checkpoint) for bulk coverage; org/user listing (`GET /orgs/{org}/repos`) for single scopes (core bucket, no 1000-cap); search shards only where `total << total GitHub`.
- Source: https://docs.github.com/en/rest/repos/repos (accessed 2026-09-29); full matrix `06 §4`.

## D2. Postgres on immutable `id`, never `full_name`

- `full_name` mutates on rename/transfer; search never pushes deletes. Store PK `id BIGINT` + `full_name UNIQUE` + history + `deleted_at`; follow `301`, tombstone on `404` (`05` A3, `06` §6 lifecycle row).
- Bulk load via batched `ON CONFLICT (id)` 500–1000/batch + `COPY` path; GIN(`topics`) rebuilt, partitioned by `id` (efficiency review I5).

## D3. Rate architecture: per-bucket Redis + 3-branch classifier

- `search` (30/min) vs `core` (5,000/hr) budgeted separately from response headers (never `GET /rate_limit` polling); per-bucket pausing only; `retry-after` → `reset` → `60s×2^n`+jitter max 5; spam-422 backoff vs cap-422 shard vs validation-422 fix (`05` A6/A11, `02` §5).
- No token-sharing to evade limits; SSO `partial-results` fails loudly; `GITHUB_TOKEN` forbidden for discovery (single-repo scope).

## D4. Freshness: live-only at `ran_at` (no background polling — locked)

- Live-only: the entire job runs on the go. No hourly/6h polling, no hot/cold tiers, no persistent watermarks, no GH Archive tail. Within-run `id`-dedupe only; record current `pushed_at` per repo; never mix `pushed_at` with `updated_at` ordering (`05` A6, review I3).
- Rationale: polling all shards hourly/6h saturates search (10k shards ≈ 5.5 hr/token/cycle — efficiency review C3), which is exactly why background polling is out of scope. History mirrors (GH Archive/BQ) remain fetch-time sources for thesis tracks only, not a live tail.

## D5. Enrichment economics: trees-first, GraphQL-batch-only, zero-burn mirrors

- File-existence via 1× `trees?recursive=1` (or zero-call ecosyste.ms metafiles/manifests), never N× `contents` loops (8× tax — review C4). `contents` only for bytes parsed.
- GraphQL only where it replaces ≥3 REST calls (`fundingLinks/discussions/sponsors`, `first≤50` to dodge 2025 resource-cap partials — review C5).
- Bootstrap R-fields from ecosyste.ms (343M repos as of 2026) + deps.dev (package→repo, Scorecard/OSV) before spending `core` (review C1); code-content via Sourcegraph/grep.app preselect, `clone+rg` as ground truth only (`06` §6).

## D6. No search-path ETag, no `total_count` validation

- Search `304` hit rate ≈ 0% (churn + `sort=updated` reorder); ETag kept for hydrate/trees/contents on stable URLs only (free if authed — review I1; `02` §4).
- `total_count` is approximate: validate by `id`-set diff + overlap windows; cap-`422` → auto-shard; `incomplete_results` → narrow once, mark shard (review I2; `05` A6).

## D7. Geo: `06 §6.1` pipeline as specified

- Free-text `location` only (`GET /users`, `GET /orgs`; `location:` qualifier users-search-only — https://docs.github.com/en/rest/users/users#get-a-user, https://docs.github.com/en/search-github/searching-on-github/searching-users, both accessed 2026-09-29).
- Order: flag-emoji decode → normalize/split → gazetteer (population-threshold) → alias table → cached residue geocoder → weak tiebreakers (TLD/timezone) → `{country_iso, confidence, raw}` tiers + unmatched bucket + dashboard.

## D8. Serve + legal invariants

- Serve contract (`contracts/search-api.md`): upstream allowlist `q,sort,order,per_page,page`, own `400` on customs, virtual-param translation table, defined `400/502` mappings (never leaked upstream bodies).
- Legal gates (`05` A10–A11): API-only default, `robots.txt` check (https://github.com/robots.txt), deletion/GDPR purge + email minimization, no spam/resale-of-PII, resale/high-throughput subscription review, trademark-safe naming, per-request audit log + SLOs.

## Alternatives rejected

- Search-only backfill (2.8× slower + distortion — C2). Global all-shard polling (22 hr/token/day — C3). Per-file `contents` loops (8× — C4). Naive per-repo GraphQL (2× slower unless batching — C5). Vendor-managed discovery at scale ($0.90–$3/1k, no cap escape — `04` §5). HTML scraping to dodge limits (ToS `05` A10).

## D9. API frontier deltas (deep research 2026-09-29 — new rules beyond D3/D5)

- Star-history replaces stargazer-timestamp crawling: `GET .../stargazers/history` + `/count` (2026-09-04) costs ~18 reqs for 10yr history vs ~500 before (~25×); per-user timestamps are collab-only since 2026-06-30 — use the history endpoint everywhere.
- Install math: 5,000 + 50/repo + 50/user past 20, cap 12,500/hr (≈170 repos to cap); user-to-server tokens share the 5k user bucket — never for bulk crawl; per-install isolation = N×12.5k scaling.
- Token format: stateless `ghs_` JWT (~520 chars, 2 dots, from Apr–May 2026) breaks `len==40` validation — accept both forms; enterprise install lookup endpoint saves `ceil(installs/100)` reqs per rotation.
- GraphQL guardrails: timeouts burn primary points (2025-07-21 — split on timeout, never retry same shape); resource caps return partials (2025-09-01 — 1 connection level per query, aliases ≤10–20, shallow fields); pre-flight with `dryRun` cost, gate batch iff cost ≤ repos replaced.
- Concurrency ceiling: 900 pts/min/endpoint with ~700ms p50 → cap ~10 concurrent/endpoint/token; 3-way buckets (`search` 30/min, `code_search` 10/min, semantic 10/min — never semantic for bulk).
- `GET /rate_limit`: read `resources.core` (`rate` + `code_scanning_upload` removed); endpoint is primary-free but secondary-counted — pace from response headers only.
- ETag generalizes: any GET returning `etag` (pulls, contents) → free `304` if authed — extend beyond hydrate, byte-stable URLs required.
- No `?fields=` sparse fieldsets on GitHub REST (media-types only — don't build field filters); skip deployment-status history >90d (auto-deleted July 2026); every request authed (unauth hardened May 2025); DEV anchor for budgeting: 1k repos × 5 calls = 1 token-hr.

## D10. Postgres/Redis hardening (deep research 2026-09-29 — extends data-model.md)

- Gate no-op upserts in app/Redis first (bloom/dirty-flag/`pushed_at` compare): `ON CONFLICT ... WHERE` still writes WAL + takes the lock (2× disk, 4× syncs at scale); `fillfactor=80`; monitor `n_tup_hot_upd`.
- Bootstrap via `COPY` (TEXT + `ON_ERROR ignore` on PG17+, plus `REJECT_LIMIT` only when the server is PG18+; binary mode forbids ON_ERROR and silently truncates out-of-range ints, so TEXT is used for dirty-payload tolerance — ruling R23); `INSERT...ON CONFLICT` for deltas only. The original ~72% binary speedup no longer applies.
- Staging: per-batch `UNLOGGED` (visible cross-workers; TEMP is session-private), never flip main table (full rewrite + replica break); `COPY` → `INSERT...SELECT` → `DROP`.
- Autovacuum per table (`scale_factor 0.02–0.05`, OFF during load + `VACUUM ANALYZE` after); PG16+ BRIN is HOT-safe — use for `pushed_at` if insert order correlates, else btree.
- Driver: `psycopg3` pipeline mode for OLTP writes (coalesced round-trips; `COPY` unsupported in pipeline); prepared statements break under PgBouncer txn mode (`stmt_cache_size=0`); pools: `(cores×2)+1` backends, 1–2 conns per network-bound worker, PgBouncer txn mode; migrations with `lock_timeout=50ms`/`statement_timeout=5s`.
- Queue: Redis Streams sharded lanes (`hash%N`, per-repo order), drain PEL-history before `>`, `XAUTOCLAIM` reaper + DLQ after 3 tries, watch PEL size; delayed-retry via sorted sets; rate-limit via HASH+Lua O(1) (never ZSET logs); budget 0.5–1ms same-AZ.
- Serve p95: read replica (indexes must exist on primary), covering `INCLUDE (stars, forks)` indexes, `REFRESH ... CONCURRENTLY` or `pg_ivm`/trigger table + Redis watermark-keyed cache.
- Indexes: `CONCURRENTLY` builds (+ per-partition attach workaround), `max_parallel_maintenance_workers=4`; retention via `pg_cron` + `pg_partman` (drop old `audit_log` partitions); PITR via PG17 incremental basebackup + watermark-table resume (idempotent `ON CONFLICT DO NOTHING` replay).
- `MERGE` stays rejected (~28% slower, different concurrency) — `ON CONFLICT` stands.

## D11. Bulk/enrich deltas (deep research 2026-09-29 — extends D4/D5)

- Cliff exacts: brownout 2025-09-08 → permanent 2025-10-07; latency upside 8h → near-realtime; IDs survive, rehydrate via REST.
- ClickHouse: steal `github.events` ordering (`ORDER BY (event_type, repo_name, date)` — filter `event_type` first); refreshable MVs for top-N (~0.01s).
- BQ cost controls: $6.25/TiB, 10MB minimum, `LIMIT` doesn't prune — dry-run + bare `WHERE event_date=` pruning (1000× spread by column choice).
- DuckDB hourly-poller (land raw → Parquet by date/hour, T-3h safety) as the lightweight GH Archive alternative.
- `criticality_score` bulk DEAD (2026-08-29) — Scorecard weekly feed instead; patch `04`/`06` references done.
- deps.dev batch-first (`GetVersionBatch`/`GetProjectBatch`, hash→≤1000 versions); ecosyste.ms polite-pool (`?mailto=`, 5000/hr/IP, `POST /packages/bulk_lookup`, zero-token MCP path).
- Blob-SHA dedupe across repos (`git/blobs/{sha}` once); sparse blobless clones for existence sweeps (up to 180× on monorepos).
- Freshness tiers (background options — REJECTED for live-only; listed for context only): webhook push → events 60–300s ETag → GH Archive hourly-3h → BQ daily. Live-only uses none of these as a tail; BQ/GH Archive remain opt-in fetch-time sources for windowed history.

## D13. Thesis tracks: corpus, traces, validation (FR-015–FR-022)

Source: the `thisisjustthebeginning` thesis repo (proposal §3, research-notes methodology + replication-package deep dive, meeting-script paper map). Rule: gitcrawl feeds the thesis pipeline evidence; analysis (metrics, models, writing) lives downstream.

- **Corpus frame first (timeline pressure: draft due 10.12.2026, month 1–2 pipelines + basic rates).** Filter-spec carries buckets (frozen-list date for old/new), attrition accounting (~2k drops at 128k scale: private/deleted/dotfiles), size splits (small/med/large), study window (drop pre-2025), ≥10-star Dabic option. Raw upstream JSON stored per repo (replication package requirement).
- **Trace packs versioned + frozen pre-run** (authors/files/branches/labels/bots for 63 agents): content fetch for guidance files (not existence), generic-`AGENTS.md` weighted down, `CONVENTIONS.md` excluded, per-agent attribution for preference ranking; disabled-trace agents (Claude opt-out, Pi/OpenCode unsigned) mean measured adoption is always an undercount — reported, never silently corrected.
- **Commit history windowed** (messages/authors/trailers/diffstat; merge-revert-bump + bot filtered; adoption-date + ratios), BigQuery primary + API top-up; **PR channel** (branch/label, merge-type mapping with squash-imprecision rule, outcome/iterations/reverts, Codex rule, 10k cap); **file inventory** (list + `.gitignore` scan — 2–11.8% visible-only-via-ignore — per-file stats, two-clone strategy).
- **crates.io linkage** (metadata/downloads/yanked/versions, Cargo/workspace parse, hallucination check); **snowball expansion** (user-search seed → frontier queue → org expansion with exclusions → bulk import → geographic fallback); **validation harness** (n=400 stratified samples, double-label κ, Wilson CIs, Chapman matrix, pre-registration freeze).
- **Known numbers to respect**: ~128k old-bucket / ~13k new-bucket reference scale; 0.5–1% FP + ~3% borderline baseline; 50–100GB/stack storage; Python 3.12+ (plan aligned).

## D12. Enrichment scheduler: cost order + batching + segmentation (FR-014)

Savings come from calls never made, not corners cut (~3–4× under naive per-repo enrichment, zero fields dropped):

| Priority | Source | Fields | Cost |
|---|---|---|---|
| 0 (free) | Search/hydrated record | stars/topics/pushed, counts, flags | 0 extra calls |
| 1 (cheap) | Zero-call mirrors | ecosyste.ms metafiles/manifests/scorecard, deps.dev linkage/vulns, geo-cache by owner id, blob-SHA cache | 0 GitHub calls |
| 2 (single) | One call, many answers | `trees?recursive=1` (all path checks), `languages` bytes, releases page | 1 call/repo |
| 3 (batched) | One round-trip, N repos | GraphQL aliases ≤10–20 (funding/discussions/sponsors/tiers), deps.dev `GetVersionBatch`, ecosyste.ms `bulk_lookup` | 1/N calls each |
| 4 (deep) | Full price, survivors only | per-SHA CI, coverage artifacts, sparse blobless clones, raw file bytes | full cost, few repos |

Execution: filters sorted by cost-per-repo (cheap screens first so expensive stages see only survivors); candidates segmented by id range across tokens (≈10 concurrent/endpoint/token, stateless shard-scoped workers, dead lanes re-queue); segments ordered survivors-first (high stars/recent push) so early stops stay useful; interactive Finds enrich the visible page only, corpus exports run full depth; ETag revalidation skips unchanged repos; every run reports per-field source + calls spent.
