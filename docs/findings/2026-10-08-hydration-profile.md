# GraphQL discovery validation and hydration profile (measured 2026-10-08)

Live validation of the GraphQL discovery engine plus a profiled end-to-end hydration of the
run-#9 corpus, measured on the day the engine shipped. **Measured on 2026-10-08, run by the
SDD implementation of branch `graphql-discovery`** at commit `ce2e38c` (lean page fields +
malformed-response resume), on top of `52b6380` (adaptive page halving).

- Filter (run #9): `created:>=2025-02-24 language:rust`, `page {per_page: 20, max_pages: 3}`,
  `virtual {min_stars: 4, min_commits: 50, min_language_bytes: 175000}`, `gitcrawl_filter: 1`.
- Environment: one personal token (`GITHUB_TOKEN`, fingerprint `324662332ca6`), local dev
  Postgres (`localhost:5433`), local Redis (`localhost:6379`). Writes are local DB rows.
- Prior reference: the 2026-10-08 spike (`design/corpus-building-efficient-engineering.md` §9.4)
  measured 56 shards, 418 pages, 38,969 ids in 67.9 s, and run #9's REST-era hydration of
  38,833 repos at ~2,800 repos/min (`design/corpus-building-efficient-engineering.md` §11).

## Part A — shard-and-fetch validation

Command: `discover.pipeline.run_search_discovery(deps, "language:rust stars:>=4 created:>=2025-02-24", max_shards=1000, max_pages=3, discovery_concurrency=32)`
with `deps = serve.runner.build_deps(create_engine($DATABASE_URL))`. `plan_seconds` wraps
`discover.pipeline.count_queries`; halving is inferred from `pages > fetched / 100`.

| Run | Wall | Plan | Fetch | Shards | Pages | Ids | Avg page | Halving | Incomplete | Page-capped | Plan-capped | Overlap vs spike |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Prescribed (`build_deps` default 10 slots) | **58.0 s** | 27.2 s | 30.8 s | 192 | 59 | 5,276 | 89.4 | yes (1.118×) | **176** | 0 | false | 5,274/38,969 = **13.5%** |
| Conforming (`max_concurrent=32`) | **97.3 s** | 27.0 s | 70.3 s | 192 | 483 | **39,005** | 80.8 | yes (1.238×) | **0** | 0 | false | 38,965/38,969 = **99.99%** |

- The prescribed run "finished" fast by failing: `audit_log` shows only 83 HTTP responses
  (all 200) for 192 shards, and 176 shards were marked `incomplete` with no page ever fetched.
  The failure is the limiter, not GitHub: `build_deps` caps the `graphql` bucket at
  `max_concurrent=10` slots, the discovery pool runs 32 workers, and `request_with_retry`
  raises `ThrottledError` after 5 consecutive slot denials (~2.5 s) instead of waiting for a
  slot. A perfect slot storm at start-up starves the pool; 58 s is the sound of shards dying
  fast, not of fetching.
- The conforming run (32 slots = pool size) is the measurement the spike predicted: 39,005
  unique ids, 0 incomplete, 0 page-capped, 483 pages at an 80.8 average page size (halved
  from 100 when 502/504s hit; no fetch exceptions at all, 507 audit rows all HTTP 200).
  Overlap is 99.99% of the spike ids and 99.897% of this run's ids (40 ours-only, 4
  spike-only — search drift, same magnitude the project already documents for REST runs).
- Both runs took ~27 s to plan 192 shards (spike: 8.9 s / 56 shards). The planner is the
  slower phase relative to the spike because this filter is split into 192 shards, not 56;
  planning probes ride the same slow GraphQL endpoint measured below.
- The server wires discovery exactly like the prescribed run today
  (`serve/app.py:395` passes `settings.limiter_max_concurrent`, corpus preset 10), so the
  starvation is a production behavior, not a scratch-script artifact.

## Part B — end-to-end run with hydration profiling

Command: `serve.runner.run_filter(deps, parse_filter_spec(spec), config=RunnerConfig(max_shards=1000,
max_candidates=100000, max_hydrate=100000, max_enrich=100000, request_deadline_seconds=86400,
graphql_batch=True, graphql_batch_size=20, concurrency=10, discovery_concurrency=32))`.
`deps` was built with `max_concurrent=32` for the same reason as Part A's conforming run;
hydration itself is driven by `RunnerConfig.concurrency=10` and never contends for more
than 10 of those slots, so the hydration profile is unaffected by that choice.
Timers wrap `count_total`, `run_search_discovery`, `_hydrate`; `cProfile` is enabled only
around `_hydrate`.

| Stage | Wall | Detail |
|---|---|---|
| Count | 0.5 s | 1 query, `total_count=39,005` |
| Discovery | 93.5 s | 39,005 ids, 483 pages, 0 incomplete, 0 page-capped, plan-capped false |
| Hydration | **1,239.7 s (20.7 min)** | 39,005 candidates, 1,951 GraphQL requests, 39,005 values, 0 fallbacks, 0 unresolved, 0 requeues, deadline not hit |
| Enrichment (segments) | 0.2 s | all three virtuals served from hydration data (`calls_spent` all 0) |
| Writes / bookkeeping | 3.1 s | row loads, ordering, payload assembly |
| **Total** | **1,336.9 s (22.3 min)** | |

- Hydration throughput: **1,887.8 repos/min** (prior run #9: ~2,800). Requests per repo are
  unchanged (0.050); per-batch latency at concurrency 10 rose from ~4.3 s to ~6.4 s
  (1,239.7 s / 1,951 requests = 0.636 s of slot time; ×10 slots). The gap is GitHub-side
  response latency today, not the engine.
- Payload `field_stats` (complete, no warnings, `incomplete=false`):
  - `per_field_sources`: `min_stars` 39,005 → `min_commits` 21,618 → `min_language_bytes` 17,913 survivors.
  - `calls_spent`: `min_stars` 0, `min_commits` 0, `min_language_bytes` 0.
  - `graphql.hydration`: keys 39,005 / requests 1,951 / values 39,005 / fallbacks 0 / handled 0 / unresolved 0 / requeues 0 / deadline_hit false.
  - Final items: 17,913.
- Points: GitHub `remaining` went 4,912 → 1,996 across the 22.3 min run (≈2.9k points;
  hydration 1,951 requests at ~1 point each, discovery ~515, the rest probes/retries). One
  full corpus run consumes ~3k of the 5,000/hour budget — at most one per hour.
- Re-run cost note: discovery upserted 112 updated / 38,893 unchanged rows between Part A
  and Part B in the same hour, yet Part B still paid the full 1,951-request hydration.

## cProfile — hydration phase only

`cProfile.Profile()` enabled around `serve.runner._hydrate`; sorted by cumulative time.
Multi-thread accounting makes cumulative sums exceed the 1,239.7 s wall, so read the ranking
relatively.

| Function (calls) | Cumulative | Interpretation |
|---|---|---|
| `concurrent.futures._base.wait` (1,951) | 8,214.9 s | The main loop blocks on batch completion once per request; hydration is entirely gated here. |
| `threading.Event.wait` (1,961) | 8,174.4 s | The condition waits inside the future machinery — same idle time, one layer down. |
| `_thread.lock.acquire` (25,491/7,903) | 1,643.7 s | Lock hand-offs between the batch loop and its 10 workers. |
| `Thread.join` / `_wait_for_tstate_lock` (10 each) | 1,643.6 s | Final pool drain waiting for the last straggler batches. |
| `serve.runner._hydrate` (1) | 1,239.7 s | Stage wall; matches the wall-clock timer exactly. |
| `hydrate.tail.refresh_repos_batched` (1) | 1,239.6 s | Same scope, including etag lookup, REST fallback wiring, and DB apply. |
| `SimpleQueue.get` (1,949) | 821.8 s | Result hand-off from workers to the batch loop. |
| `lib.graphql_batch.fetch_batch` (1) | 821.8 s | The batch engine's own share of the wall. |
| `_ssl._SSLSocket.read` (39,279) | 798.2 s tottime | Actual TLS body reads (~20 ms each); network bytes are a minor slice of the wait. |
| SQLAlchemy statement build (`coercions.expect` 11.9M calls 51.0 s cum, `schema.__init__` 1.4M calls 29.6 s cum, `cache_key` 15.0 s cum) | ~50–90 s combined | Per-row DB write path CPU; the only material non-network cost. |

## Ranked next bottleneck

1. **Discovery slot starvation (correctness first).** With the corpus preset (`limiter_max_concurrent=10`)
   and `discovery_concurrency=32`, 176/192 shards die before their first page (5,276 ids, 13.5%
   of spike), while the same code with 32 slots collects 39,005 ids, 0 incomplete, in 97.3 s.
   As shipped for the server, a corpus run cannot pass the discovery gate.
2. **Hydration batch throughput (time).** 1,951 sequential-ish batch calls at 10 concurrent
   × ~6.4 s = 20.7 min, 93% of end-to-end wall. Every additional slot of concurrency is
   roughly linear throughput, and the limiter already allows 32.
3. **DB write CPU and repeat-run waste (secondary).** SQLAlchemy statement construction is
   ~50–90 s per corpus; and because GraphQL hydration has no ETag equivalent, only 112 of
   39,005 rows changed between two runs minutes apart yet all 1,951 requests were spent.

## Fix proposals (no code changes in this task)

1. **Make discovery respect the limiter or vice versa.** Either pass
   `max_concurrent >= discovery_concurrency` when building discovery deps, have
   `request_with_retry` wait for a slot instead of raising `ThrottledError` after ~2.5 s of
   denials, or let the worker retry a throttled shard later instead of marking it
   `incomplete`. Measured upside: 97.3 s / 0 incomplete / 99.99% spike overlap.
2. **Raise hydration concurrency.** `runner_config_from` clamps to
   `min(limiter_max_concurrent, 20)`; the corpus preset sits at 10. Doubling to 20 should cut
   the 20.7 min toward ~10–11 min at unchanged point cost per run (~3k/hour), which one run
   per hour still fits.
3. **Skip recently hydrated rows.** Track a hydration watermark (or reuse the REST+ETag
   path) so unchanged repos cost nothing: 38,893 of 39,005 rows were byte-identical between
   two runs minutes apart. A freshness window would turn repeat corpus builds nearly free
   and free the point budget for larger corpora.
4. Lower-priority: batch the `apply_hydration` writes to cut the ~50–90 s of SQLAlchemy CPU
   per 39k-repo run.
