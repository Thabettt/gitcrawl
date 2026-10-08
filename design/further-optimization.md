# Further Optimization: what to do after the GraphQL discovery engine

**Date**: 2026-10-08. Companions: `corpus-building-efficient-engineering.md` §9.4 (the original measurement), `run-limits.md` (the meters and presets), `../docs/findings/2026-10-08-hydration-profile.md` (the live profile this builds on), `../docs/superpowers/specs/2026-10-08-graphql-discovery-design.md` (the engine's design), and `how-the-data-flows.md` (the stage narrative).

**The one-paragraph version**: the search phase is solved — the same 39,000-repo filter that used to spend ~20 minutes sharding now finishes its shard-and-fetch in about 100 seconds, and a full corpus run dropped from 77m56s to 28m33s. The bottleneck has moved downstream to **hydration**, which is 20–25 minutes of the run and is gated by GitHub response latency at a concurrency of 10. The biggest available win is not a cleverer crawl; it is letting hydration use the concurrency the account already allows, and then not paying twice for rows that did not change. This document records the measured evidence, the ranked opportunities, and the small deferred items that should not get lost.

---

## 1. What changed (for context)

Discovery was rebuilt as two phases: a **planner** that batches up to 20 `repositoryCount` probes per GraphQL query, and a **fetcher** that pulls pages with one search connection per request from a pool of `discovery_concurrency` workers (default 32), halving the page size when GitHub's ~10-second query timeout hits. The Redis shard queue and the REST paging path were deleted; shard rows, Stop/resume, `max_pages`, audit, and warnings were kept. The GraphQL points meter (5,000/hour) now pays for discovery; the REST search meter is no longer used by any shipped path.

Measured on the day it shipped:

| What | Result |
|---|---|
| Spike (isolated proof) | 418 pages, 38,969 ids in **67.9 s**, ~430–500 points |
| Live, engine + binding fix | **97.3 s / 109.3 s**, 39,005–39,007 ids, **0 incomplete**, 99.99% overlap with the spike |
| Old REST-era run #9 (reference) | ~20 min of sharding alone; 77m56s end-to-end (reported by the operator, 2026-10-08) |

## 2. The operator's re-run (2026-10-08)

A real corpus run of run-#9's filter through the console, after the merge:

| | Old | New |
|---|---|---|
| End-to-end | **77m56s** | **28m33s** |
| Search phase | ~20 min | ~1.7–2 min (the 97–109 s bound-path measurements plus count/startup) |
| Hydration | ~14–20 min | ~24–25 min (implied: total minus search and overhead) |

Plain effect: the run is **~2.7× faster end-to-end**, and essentially all of the remaining time is hydration. The old number was dominated by two slow phases; the new number has one.

## 3. Where the time goes now

From the profiled run (`docs/findings/2026-10-08-hydration-profile.md`, 2026-10-08; timers around each stage, cProfile around hydration):

| Stage | Wall | Detail |
|---|---|---|
| Count | 0.5 s | one GraphQL count query |
| Discovery | 93.5 s | 483 pages, 0 incomplete; ~27 s of it is planning probes |
| **Hydration** | **1,239.7 s (20.7 min)** | 39,005 repos in 1,951 GraphQL batch requests, 0 fallbacks, 0 unresolved |
| Enrichment | 0.2 s | all three virtual filters answered from hydration data; 0 extra calls |
| Writes / bookkeeping | 3.1 s | row loads, ordering, payload assembly |
| **Total** | **22.3 min** | |

- Hydration throughput was **1,888 repos/min** that day, versus ~2,800 repos/min in the REST-era run #9. Requests per repo did not change (0.050); per-batch latency at concurrency 10 rose from ~4.3 s to ~6.4 s. That gap is GitHub-side response latency, not the engine — the same engine at another hour measured 2,800/min.
- cProfile confirms the shape: the run is **blocked on batch completion** (`concurrent.futures.wait` / `Event.wait` dominate cumulative time), with real network reads a minor slice (~800 s of TLS reads spread across the wall) and SQLAlchemy row building the only material CPU cost (~50–90 s per 39k-repo corpus).
- Points: one corpus run costs **~3,000 of the 5,000/hour** budget (1,951 hydration + ~515 discovery + probes/retries). That is roughly **one full corpus run per hour per account** — a planning constraint, not a throttle to fight.

## 4. Opportunities, ranked by measured upside

### 4.1 Raise hydration concurrency (biggest single win, smallest change)

Hydration runs `min(limiter_max_concurrent, 20)` workers and the corpus preset sets `limiter_max_concurrent=10`. The profile shows latency-bound batches at concurrency 10; every additional slot is close to linear throughput up to GitHub's documented 100-concurrent ceiling, and the limiter's phase binding for discovery proves the machinery already exists. Raising the corpus preset (and the clamp) to 20 is one settings/code line and should cut the 20.7-minute hydration toward **10–12 minutes** at unchanged point cost. Watch for secondary-limit 403s at the higher concurrency; the backoff already exists.

### 4.2 Stop paying for unchanged rows (biggest win for repeat runs)

Between two runs minutes apart, **38,893 of 39,005 rows were byte-identical**, yet the second run paid all 1,951 hydration requests. Two possible fixes, both already sketched in the project's own research:

- a **freshness window** (reuse the previous hydration timestamp and skip rows inside it), or
- the **REST + ETag path** for refresh runs (a `304` is free), while GraphQL batching stays for first builds.

Upside: repeat corpus builds become nearly free in time and points, which in turn makes multi-corpus days feasible on one token. This is a product decision (it softens the "live-only at `ran_at`" rule), not a technical one.

### 4.3 Cut the DB write CPU

SQLAlchemy statement construction and row coercion cost ~50–90 s per corpus, serialized behind the discovery writer lock and inside hydration's apply step. Batching the hydration `apply` writes (multi-row statements, `executemany`, or the existing `COPY` staging path) is straightforward and removes the only material CPU cost in the run.

### 4.4 Discovery polish (diminishing returns)

Discovery's own 100 s is now ~27 s of planning probes and ~70–83 s of fetching. If it ever matters again: raise `discovery_concurrency` toward 48 (measured fast), batch planner probes larger, and reuse the root count across more of the plan. The 1,000-result cap and GitHub's 10-second query timeout are hard walls; the adaptive page halving (100→50→25) is the safety net.

### 4.5 Make each run self-reporting

The stage timers above lived in a scratch script. Persisting per-stage wall times (and GraphQL points spent) to the run payload would let every future run produce this document's evidence automatically, and would make regressions visible from the console instead of by profiling.

## 5. Deferred items (small, recorded so they are not lost)

| Item | Why it matters | Size |
|---|---|---|
| Startup sweep for orphaned `ACTIVE` shard rows | A crashed run leaves rows `active` forever; `shard_coverage` counts them (the dashboard currently reads ~0.55 because of historical rows) | one startup statement |
| Count-batch audit params record only the first query | The other 19 probes of a batch are not represented in the audit row (one row per request is still true to FR-010) | one field |
| Floor-timeout error reports 502 even for 504 | Log copy only | one line |
| `plan_capped` can be true at an exact fill | The warning is conservative; no silent truncation | planner tweak |
| Item budget counts requested page size | Documented; conservative | doc note |
| `mypy` ratchet does not cover `src/discover/` | The most-changed package is outside the gate; the new module is typed and clean | config |
| Console query-text assertions are formatting-coupled | Tests break on harmless query-builder edits | test polish |

## 6. Bigger bets (parked, with the trade-offs stated)

- **A local cached corpus for instant repeats.** After one 28-minute build, serving the same filter from Postgres is sub-second; an incremental refresh via a `pushed:` watermark could keep it fresh in under two minutes. This contradicts the locked live-only decision (D4) and leaves star-only changes stale; it is the only path to genuinely sub-2-minute *full* answers and deserves an explicit product ruling rather than an accident.
- **Richer discovery fields.** Pages currently carry a lean 14-field set (identity, counts, dates, fork/visibility) because GitHub's ~10-second timeout rejects the full 35-field query at 100 nodes. Topics/watchers/open-issue counts/size/branch/description are filled by hydration; for interactive runs where `max_hydrate < max_candidates`, rows outside the hydrate window stay thin until the next hydration. If console display needs those fields earlier, fetch them in a second lightweight pass, not by fattening the page query.
- **Above 32–48 concurrent search requests.** GitHub's documented ceiling is 100 concurrent; the project's own policy is one personal token and no app fan-out, and the search phase is already ~3% of the run. Not worth the risk.
- **Paid/burst vendors or mirrors.** Already evaluated and rejected in `findings/04`; nothing has changed.

## 7. How to measure the next iteration

1. Run the same filter through the console and capture the run's stage timings (once 4.5 exists, they are in the payload; until then re-use the scratch timers).
2. For hydration, prefer `py-spy` (or per-thread cProfile) over plain cProfile — multi-thread accounting makes cumulative sums exceed wall time.
3. Compare repos/min, requests/repo, per-batch latency, fallbacks, unresolved, and points spent before/after a change. The profile's tables in `docs/findings/2026-10-08-hydration-profile.md` are the baseline to beat.

## 8. Where the code lives

- Engine: `src/discover/graphql_search.py` (counts, pages, mapping, halving), `src/discover/pipeline.py` (plan → shards → parallel workers), `src/scheduler/shard_planner.py` (`plan_shards`).
- Limits and meters: `src/limiter/buckets.py` (`graphql` points bucket, `bound_concurrency`), `src/store/settings.py` + `src/serve/settings_spec.py` (`discovery_concurrency`, corpus preset), `src/serve/runner.py` (hydration concurrency `min(limiter_max_concurrent, 20)`).
- Evidence: `design/corpus-building-efficient-engineering.md` §9.4, `docs/findings/2026-10-08-hydration-profile.md`, `docs/development-log.md` (2026-10-08 entry).
