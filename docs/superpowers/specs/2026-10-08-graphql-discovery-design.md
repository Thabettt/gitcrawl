# GraphQL Discovery Engine — Design

**Date**: 2026-10-08 · **Status**: accepted and implemented (2026-10-08; live default-build validation: 109.3 s, 39,007 ids, 0/192 incomplete) · **Supersedes**: the REST `count_total` + `ShardPlanner` + `iter_shard_pages` search path only (`since` scan and org/user enumeration are untouched)

**Companion docs**: `design/corpus-building-efficient-engineering.md` §9.4 (the measurement), `design/run-limits.md` (limits derivation), `docs/environment.md` (settings), `docs/superpowers/specs/2026-10-02-graphql-batch-engine.md` (the batch engine this reuses)

**One-paragraph version**: A live spike on 2026-10-08 showed GitHub's GraphQL `search` connection bills the points meter (minimum 1 point per query), not the REST search meter (30 requests/minute). On that basis, discovery is rebuilt as two phases: (1) a **batched planner** that probes `repositoryCount` for up to 20 windows per GraphQL query and bisects until every window is fetchable; (2) an **executor** that fetches shards with one search connection per page from a pool of `discovery_concurrency` workers. The same 39k-repo shard-and-fetch measured 67.9 seconds end-to-end versus ~20 minutes on REST, and both phases ride the existing `graphql` limiter bucket, audit hook, and shard state machine. The Redis Streams queue and its deferral machinery are deleted (R58); everything else in the pipeline contract — shard rows, Stop/resume, page caps, warnings, bundles — is preserved.

---

## 1. Context and evidence

Measured live on 2026-10-08 with one personal token (see `design/corpus-building-efficient-engineering.md` §9.4 for the full ledger):

| What | Result |
|---|---|
| 16 aliased `repositoryCount` probes in one GraphQL query | works; ~1 s; **1 point total** |
| Planner (111 probes, 9 batched queries → 56 shards) | **8.9 s** |
| Fetch (418 pages via one search connection each, 32 in flight) | 38,969 unique repos in **59.0 s** |
| Whole shard-and-fetch | **67.9 s**, 0 retries, 0 failures, ~430–500 points |
| Aliased *pages* (multiple search connections per query) | **does not scale**: 4 × 100 nodes → 10 s timeout (`502`) |

Design consequences: probes are batched (counts return a single number each); pages are not (one connection per query, many queries in parallel). The 1,000-result cap and the `max_pages` contract do not move.

## 2. Architecture

**New module `src/discover/graphql_search.py`** — all GraphQL *search* traffic:

- `count_queries(client, queries, …) -> list[int]` — one POST with up to 20 aliases (`s0: search(first: 1, query: …) { repositoryCount }`), returns counts in order.
- `iter_pages(client, query, *, max_pages, …) -> Iterator[SearchPage]` — one search connection per request, cursor pagination (`pageInfo { hasNextPage endCursor }`), `per_page=100` fixed; yields `SearchPage(items, repository_count, has_next, end_cursor, incomplete, exhausted)`.
- `node_to_item(node) -> dict` — maps a GraphQL `Repository` node to the REST-shaped dict `normalize_repo` already accepts, keeping `upsert_repos` untouchable.
- GraphQL error handling and retry/backoff live here (see §4).

**`src/scheduler/shard_planner.py`** — the recursive `ShardPlanner` class is replaced by one batched function:

```python
plan_shards(query, probe_counts, *, max_fetchable, scan_target, min_date,
            root_count, max_shards, batch_size=20, now) -> tuple[list[ShardSpec], bool]
```

- BFS by level: probe up to 20 windows per round, split everything over target, drop empty windows, repeat.
- Identical target math (`min(1000, 100 × max_pages)`, fallback to 1000 when `total_count > max_shards × target`), identical `split_created`/`replace_created`/`_merge_windows` semantics, identical single-day `oversized` leaf.
- Stops once `max_shards` leaves exist (checked at level boundaries, truncated to the oldest windows, `plan_capped=True`) so interactive runs stay cheap.
- Returns leaves sorted by date for deterministic plans. `probe_counts` is a parameter, so planner tests need no HTTP.

**`src/discover/pipeline.py`**:

- `count_total` becomes one GraphQL count query (same signature; cost 1 point).
- `run_search_discovery` keeps its signature minus `queue_prefix`, plus `discovery_concurrency=32`: plan → create shard rows (PENDING) → execute via `ThreadPoolExecutor(discovery_concurrency)`.
- Each worker owns one shard: `PENDING → ACTIVE`, walk pages in cursor order, `normalize → dedupe → upsert_repos`, collect ids, fold stats; final state `DONE` or `INCOMPLETE`. DB writes and shared stats are guarded by a writer lock (audit buffering is already thread-safe).
- Stop/deadline checked per page: the shard returns to `PENDING` (resumable); in-flight workers drain; no row is ever left `ACTIVE`.
- Stats: `shards`, `pages`, `fetched`, `inserted`, `updated`, `unchanged`, `skipped`, `incomplete_shards`, `deadline_hit`, `page_capped_shards`, `plan_capped`, `repo_ids`. Removed: `deferred_shards`, `cap_splits` (no queue, no REST 422 cap-split).

**`src/scheduler/state_machine.py`** — keep `ShardStore`, `ShardState`, `ShardRow`, `ALLOWED_TRANSITIONS`. Delete `ShardQueue`, `QueuedShard`, `RetryOutcome`, and their Redis plumbing.

**Shared REST helpers move to `src/lib/gh_client.py`** — `RequestFailed`, `short_message` (was `_short_message`), `next_link` (was `_next_link`) are used by org-enum, since-scan, hydration, enrichment, and the console; `discover/search_shards.py` is deleted entirely. All imports are updated mechanically.

## 3. GraphQL search contract

Page query (one connection):

```graphql
query {
  s: search(first: 100, after: $cursor, query: "…", type: REPOSITORY) {
    repositoryCount
    pageInfo { hasNextPage endCursor }
    nodes { ... on Repository { databaseId id nameWithOwner name owner { login __typename
      ... on User { databaseId } ... on Organization { databaseId } } stargazerCount
      forkCount isArchived isFork visibility primaryLanguage { name } licenseInfo { spdxId }
      pushedAt createdAt } }
  }
  rateLimit { cost remaining }
}
```

- Lean page set (measured 2026-10-08): the page query requests exactly the 14 fields above — identity (`databaseId`, `id`, `nameWithOwner`, `name`, `owner`), counters (`stargazerCount`, `forkCount`), archive/fork status (`isArchived`, `isFork`), `visibility`, `primaryLanguage`, `licenseInfo`, `pushedAt`, `createdAt`. The original 35-field query (nested `repositoryTopics`/`watchers`/`issues` connections) consistently hit GitHub's ~10 s query timeout at `first: 100` (502 at 10.6–10.9 s) and timeout-stormed under 32-way concurrency (133/192 shards incomplete); the lean set measures 3.9–6.0 s serial and 4.1–4.7 s under 6-way parallel load at `first: 100` with 0 failures.
- Parity gap: `description`, `homepageUrl`, `repositoryTopics`, `watchers`, `issues`, `diskUsage`, `defaultBranchRef`, `parent`, `isDisabled`, `isTemplate`, `mirrorUrl`, the `has*` flags, and `updatedAt` are no longer requested by discovery, so un-hydrated rows carry `None`/`0`/`[]` until hydration; corpus runs hydrate all candidates and interactive runs hydrate up to `max_hydrate`, so the gap closes for every candidate either path touches. Discovery upserts write the full row, so for interactive runs where `max_hydrate < max_candidates`, rows outside the hydrate window carry discovery-thin values (`description`/`topics`/`watchers`/`open_issues`/`size`/`default branch`/`updated_at` absent) and can replace previously hydrated detail on a re-run; corpus runs hydrate every candidate so the final state is full. `has_pages`, `custom_properties`, and `source_full_name` (fork-network source) remain unexposed by the GraphQL schema and stay `None`/`{}` exactly as the hydration adapter already leaves them (the pre-existing documented gap).
- Count probe replaces only the search body with `first: 1` + `repositoryCount`; the point floor keeps each 20-probe batch at 1 point.
- Measured cost: single-connection lean 100-node page 3.9–6.0 s serial and 4.1–4.7 s under 6-way parallel load; the mapper and query builder are unit-tested against captured payloads, and `node_to_item` still parses every legacy field if one reappears in a payload.

## 4. Errors, retries, backoff

- Transport: `request_with_retry` (existing) — classifier, `retry-after`/reset waits, limiter reconciliation, SSO `partial-results`, 401 loud-fail. `GraphQLAuthError` (from the batch engine) is raised on 401.
- GraphQL-shaped failures (HTTP 200 with `errors`, missing aliases): classified with the batch engine's transient markers; retried up to 3 attempts with `2^n + jitter` (bounded by the run deadline). Batch count probes retry only the failing windows; a still-failing probe raises `RequestFailed` and the run fails loudly (planning cannot proceed on unknown counts).
- Page fetch after exhausted retries: the shard is marked `INCOMPLETE` (`stats.incomplete_shards`), the run continues, and the existing partial-run warnings fire. Unexpected exceptions also mark the shard incomplete before propagating, so no crash can strand a shard `ACTIVE`.
- GraphQL partial data with errors: items present are kept; the page is flagged `incomplete` and the shard is marked incomplete at the end (no silent gaps).

## 5. Settings and limits

- New `discovery_concurrency` setting: default **32**, bounds **1–64**, env `GITCRAWL_DISCOVERY_CONCURRENCY`, corpus preset 32, migration `0012` (`ADD COLUMN … NOT NULL DEFAULT 32` + CHECK, no table rewrite beyond the new check), `AppSettings` column, settings page row, `RunSettings`/`RunnerConfig` fields.
- `limiter_max_concurrent` (default 10) keeps governing hydration; the discovery pool is the only new concurrency knob, which matches the measured operating point (32 of GitHub's documented 100 concurrent-request ceiling). During discovery the `graphql` bucket's slot ceiling is raised to `discovery_concurrency` for the phase (a cap, not a target); hydration is unchanged.
- The `graphql` bucket (5,000 points/hour) meters discovery exactly as it meters hydration; the REST `search` bucket is no longer consumed by discovery.

## 6. Audit, SLO, metrics

- Audit: a GraphQL-aware hook records `params={"q": query, "after": cursor}`, `total_count=repositoryCount`, `rl_resource=graphql`, latency and headers — so FR-010 keeps per-request records. `audit.record_from_response` gains an optional explicit `total_count` override used only by this path.
- SLO: `SloSnapshot.search_remaining` becomes `graphql_remaining` (latest `rl_resource='graphql'` audit row), with matching thresholds, labels, system-page and metrics-card copy. Golden snapshots are regenerated with `UPDATE_GOLDEN=1`.
- Metrics payload loses the `queue.pel` gauge (queue deleted); the `/metrics` contract keeps `limiter.paused` and `runs`.

## 7. Deletions and refactors

| Delete | Replace with |
|---|---|
| `discover/search_shards.py` (paging, `SearchCapExceeded`, `ShardPage`) | `discover/graphql_search.py` + helpers moved to `lib/gh_client.py` |
| `ShardPlanner` class | `plan_shards` batched planner |
| `ShardQueue`, `QueuedShard`, `RetryOutcome`, `reclaim_stale`, `retry_or_dlq`, `pel_size`, `total_pel` | inline retry/backoff in the executor |
| `_shard_queue_prefix`, `queue_prefix` plumbing (`app.py`, `runner.py`) | nothing (single-process pool) |
| `_queue_pel` and the queue card/row | GraphQL-remaining SLO |
| `deferred_shards`, `cap_splits` stats + warnings | `incomplete_shards` + existing partials |
| `tests/contract/test_github_pagination.py`, `tests/unit/test_reclaim_wiring.py`, queue cases in `test_state_machine.py` / `test_metrics.py` / `test_app_singletons.py` | `tests/unit/test_graphql_search.py`, rewritten planner/pipeline/metrics tests |

## 8. Testing

- Unit: `graphql_search` (query build, alias batching, mapping against captured payloads, pagination, max_pages, 401/200-with-errors/missing-alias retries, audit params); batched planner (all old planner cases adapted, batching ≤20, `plan_capped`, deterministic sorted leaves).
- Integration: `test_pipeline.py` discovery rewrite through `httpx.MockTransport` GraphQL responses — multi-shard parallel fetch, id dedupe, page caps, incomplete shards, deadline/Stop leaving `PENDING`, upsert parity.
- Contract: settings (bounds/env/preset/migration 0012), console warnings copy, metrics/system pages, golden snapshots regenerated.
- Gates: full `pytest -q` (with `TEST_DATABASE_URL`), `ruff`, `black --check`, `mypy`; then a live validation run: the run-#9 filter shard-and-fetch must land near the measured ~70 s, and its id set must match the spike's measured id set (`gql_spike_ids.txt`, 38,969 ids) within search drift.

## 9. Hydration profiling deliverable

After the engine ships: one real corpus-profile run of the run-#9 filter with per-stage wall timers (plan / fetch / hydrate / enrich / writes) and a `cProfile` of the hydration phase. Deliverable is a dated findings note (`docs/findings/2026-10-08-hydration-profile.md`) ranking the next bottleneck with evidence — no hydration code changes in this effort.

## 10. Risks and mitigations

| Risk | Mitigation |
|---|---|
| **Resolved 2026-10-08**: the rich 35-field page query (nested topics/watchers/issues connections) hit GraphQL resource limits / the ~10 s timeout at `first: 100` (502 at 10.6–10.9 s; 133/192 shards incomplete under 32-way load) | Page query cut to the measured lean 14-field set (§3, plus `isFork`/`visibility` in the final review): 3.9–6.0 s serial and 4.1–4.7 s under 6-way parallel load at `first: 100`, 0 failures; adaptive halving (`100 → 50 → 25`) remains as the safety net |
| Undisclosed secondary limits under sustained multi-minute bursts | 32 of 100 concurrent; per-page backoff; run deadline; a single watchpoint to lower `discovery_concurrency` without code changes |
| Points meter exhaustion mid-run (5,000/hr shared with hydration) | ~10% of budget per 39k run measured; limiter reconciles from headers and pauses |
| Search drift/nondeterministic pagination (D6) | Unchanged: `id` dedupe, bundle as arbiter, warnings |

## 11. Out of scope

- `since`-scan and org enumeration stay REST (core bucket).
- Hydration/enrichment code unchanged; only profiled.
- No multi-worker/Redis queue resurrection; single-process pool only (R58).
- Filter-spec v2; `max_pages` semantics unchanged (kept exactly).
