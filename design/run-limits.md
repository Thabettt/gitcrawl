# Run Limits (what they are, what backs them, and how to set them)

**Date**: 2026-10-04 (updated 2026-10-08 for the GraphQL discovery engine). Companions: `corpus-building-efficient-engineering.md` (the meters), `console-ux-redesign.md` §4.9 (the page), `docs/environment.md` (operations), `../docs/superpowers/plans/2026-10-02-console-settings.md` (the implementation plan). Platform numbers re-verified 2026-10-04 against GitHub's docs.

**Purpose of this document**: the Limits page (`/settings`, System → Limits) has nine fields. Until now, nobody had written down where their values come from — the defaults were inherited from the console build, the bounds were round ceilings, and the "maximum" preset pushed every dial to its limit at once, which is not an operating point any real run should use. This file derives each limit from the rate budget, defines three coherent presets, and states the rules that keep a run from being configured into nonsense.

---

## The one-paragraph version

A run's size is bounded by three things: GitHub's meters (search 30/min, core 5,000/hr, GraphQL 5,000 points/hr per token), GitHub's anti-abuse ceilings (100 concurrent requests shared across REST and GraphQL; 900/2,000 points per minute), and the wall-clock deadline. The Limits page controls how much of those budgets one run may spend. The right values are not "as high as possible" — they are a coherent operating point where discovery, saving, and checking all fit inside the deadline with headroom. The defaults are the interactive point (minutes, laptop-friendly). The old "maximum" preset was replaced with a **corpus-build** preset: 1,000 slices, 100,000 repos at each stage, 24-hour deadline, batching on, 32 concurrent, 32 discovery workers — about three hours of real budget on one token, so it finishes rather than aborting incomplete.

---

## 1. The nine fields, in plain words

| Field (code name) | Plain label | Default | Allowed (bounds) | What it bounds |
|---|---|---|---|---|
| `max_shards` | Search breadth (slices) | 10 | 1–10,000 | How many date slices the planner may create to get past the 1,000-result cap |
| `max_candidates` | Repos to keep per search | 500 | 1–1,000,000 | How many discovered repos the run holds on to |
| `max_hydrate` | Repos to save details for | 200 | 1–1,000,000 | How many candidates get their current details fetched (must be ≤ candidates) |
| `max_enrich` | Extra rules to check | 100 | 1–1,000,000 | Budget for file/country checks on hydrated repos |
| `request_deadline_seconds` | Stop a search after (seconds) | 3,600 | 60–86,400 | Wall-clock cap; past it the run stops gracefully (partial), marked incomplete and resumable |
| `graphql_batch` | Batch repo lookups | on | on/off | Whether GraphQL batching is used (off = one REST call per repo) |
| `graphql_batch_size` | Repos per batch | 29 | 1–50 | Aliases per GraphQL query; larger batches mean fewer requests but more timeout risk |
| `limiter_max_concurrent` | Simultaneous requests | 10 | 1–100 | In-flight requests per endpoint/token |
| `discovery_concurrency` | Discovery workers | 32 | 1–64 | Discovery pages fetched in parallel (one GraphQL connection each) |

**Two numbers, two jobs.** The *Allowed* column is a fence — the most the field will accept, there to stop typos and keep a run sane. The *Default* column is where the app starts. Neither is a recommendation for a big run; the presets in §5 are. The old "maximum" preset confused the three by pushing every field to its fence at once.

## 2. What backs them today (the honest audit)

- **Defaults (10 / 500 / 200 / 100 / 3,600 / on / 29 / 10 / 32)** — inherited, not derived. The settings plan states it explicitly: "defaults equal today's values, so nothing changes until an operator edits." They are sensible interactive values (a run measured in minutes), but no document derived them from the meters. They survive this audit: keep them.
- **`graphql_batch_size` 1–50** — backed. GitHub's GraphQL resource caps punish large `first` + nesting; the batch engine's own design says 20–50, and the default is 29 (the largest batch that still costs one GraphQL point). The bound matches the implementation (`MAX_BATCH_SIZE=50`) and the DB check constraint. Env overrides are clamped to the bound at settings load: an override above 50 clamps down to 50, and a non-positive value falls back to the default.
- **`limiter_max_concurrent` 1–100** — backed, but read it carefully: 100 is GitHub's *documented hard ceiling*, shared across REST and GraphQL. It is not a target. The page's own help text says so. The operational value is ~10 per endpoint per token (900 points/min ÷ ~700 ms p50). Keep the bound at 100 (platform truth); never preset it to 100. Hydration itself runs at `min(limiter_max_concurrent, 32)` workers (the hydration concurrency ceiling), so raising the setting past 32 does not raise hydration parallelism.
- **`discovery_concurrency` 1–64** — backed by the 2026-10-08 measurement: GraphQL discovery rides the points meter with one page per connection, and 32 pages in flight was the measured operating point (`corpus-building-efficient-engineering.md` §9.4) — comfortably under GitHub's 100-concurrent ceiling.
- **`request_deadline_seconds` 60–86,400** — a product choice: 24 hours is the longest a single run may take. Fine.
- **`max_shards` 1–10,000 and the three count limits 1–1,000,000** — round safety ceilings. Not wrong as guardrails, but they are not "proper limits": they ignore the rate budget entirely. The proper operating values are derived below.

## 3. The budget model (how to define a proper limit)

All numbers verified 2026-10-04 (see Sources). Per token:

| Meter | Rate | Buys |
|---|---|---|
| Search | 30/min = 1,800/hr | Not used by the shipped paths — discovery is GraphQL, and `since`/org enumeration run on core |
| Core (REST) | 5,000/hr | One hydrated repo per call (fallbacks, trees, contents) |
| GraphQL | 5,000 points/hr | Discovery (20 count probes per query = 1 point; one search connection per page) and hydration batches (≤29 repos ≈ 1 point) |
| Secondary | 100 concurrent (REST+GraphQL shared); 900 pts/min REST; 2,000 pts/min GraphQL | Pacing ceiling — stay far below |

Time estimates for a run of `C` candidates, `S` shards, batch size `B`:

- **Discovery** ≈ `(plan probes + pages) / throughput` on the GraphQL points meter with `discovery_concurrency` workers (32 by default). Count probes batch 20 per query at 1 point per query; each page is one search connection. Measured 2026-10-08: 8.9 s to plan + 59.0 s to fetch 418 pages / 38,969 repos (`corpus-building-efficient-engineering.md` §9.4). For history, the old REST path cost `(S + C/100) / 30` minutes — a count query per shard, one page per 100 repos, planning bisection extra — but that arithmetic is no longer the shipped engine.
- **Hydration** ≈ `C / (29 × 1)` GraphQL points ≈ `C/29 / 5,000` hours with batching; ≈ `C / 5,000` hours without.
- **Enrichment** ≈ per check: one batched query per `B` repos (≈1 point per 29 repos per check type), or one core call per repo without batching.
- **Wall clock** = discovery + hydration + enrichment + overhead, and must be < `request_deadline_seconds`, or the run stops partial (incomplete, resumable).

**Coherence rules** (a run that violates one of these will finish incomplete, waste calls, or both):

1. `max_hydrate ≤ max_candidates` — already enforced by the form.
2. `max_candidates ≲ max_shards × max_pages × 100`. The filter-spec's `max_pages` (1–10) caps each shard's fetch. With the default `max_pages=3`, 10 shards can discover at most ~3,000 repos; raising `max_candidates` without raising `max_shards` just shows an incomplete-run warning.
3. Large hydration/enrichment requires batching on. With batching off, the core meter buys 5,000 repos/hour — a 24-hour deadline caps you near ~120,000 saved repos, and 1M is physically impossible.
4. `limiter_max_concurrent` ≈ 10 per token. The rate meters bind long before the concurrency ceiling; higher concurrency just queues against the limiter.
5. Set the deadline above the estimate, not equal to it. Headroom absorbs retries and backoffs.
6. The bounds are guardrails, not goals. The preset exists to pick a coherent point, not the maximum of every field.

## 4. A worked example (100,000 repos, one token, batching on)

| Stage | Calls / points | Time (one token) |
|---|---|---|
| Discovery | ~1,000 batched count probes + ~1,000 pages | ~2 min (GraphQL points meter, 32 workers) |
| Hydration | ~5,000 GraphQL points | ~1 h |
| Enrichment (2 checks, batched) | ~10,000 points | ~2 h |
| **Total** | | **~3 h** (inside a 24 h deadline, with headroom for fallbacks) |

With batching off, the same run would need ~20 h of core for hydration alone and would likely abort. That is why the corpus preset keeps batching on.

**Measured (2026-10-06/07, run #9).** A live run found 38,833 repos and hydrated all of them in 1,942 GraphQL requests — 0 REST fallbacks, 0 unresolved — at roughly 2,800 repos/min with concurrency 10 (sequential REST was ~300–400/min). Hydrate + local filters took ~25 min including per-repo writes; the resumed attempt took 78 min end-to-end, dominated by re-planning and page fetching at the 30/min search cap. (Discovery in that run was the REST path; the GraphQL engine replaced it on 2026-10-08 — next paragraph.) The 100,000-repo estimates above are therefore conservative, not aspirational.

**Measured (2026-10-08, GraphQL discovery engine).** The run-#9 filter's shard-and-fetch (38,969 unique repos) planned in 8.9 s (111 count probes in 9 batched queries → 56 shards) and fetched 418 pages in 59.0 s with `discovery_concurrency=32` — 67.9 s total, 0 retries, ~430–500 GraphQL points (~10% of the hourly budget), REST search untouched. The discovery line in the 100,000-repo estimate above is an extrapolation from that operating point. Full ledger: `corpus-building-efficient-engineering.md` §9.4.

## 5. The presets

| Preset | shards | candidates | hydrate | enrich | deadline | batch | size | concurrent | discovery | Use when |
|---|---|---|---|---|---|---|---|---|---|---|
| **Interactive (default)** | 10 | 500 | 200 | 100 | 3,600 | on | 29 | 10 | 32 | Exploring a filter; results in minutes |
| **Corpus build (the button)** | 1,000 | 100,000 | 100,000 | 100,000 | 86,400 | on | 29 | 32 | 32 | A real corpus on one token, overnight (~3 h of budget) |
| **Multi-token scale-up (documented, no button)** | 10,000 | 1,000,000 | 1,000,000 | 1,000,000 | 86,400 | on | 29 | 10/token | 32 | Only with 5–10 tokens; single-token 1M runs exceed the deadline and abort incomplete |

The **corpus-build preset replaced the old "maximum" preset** (which set every field to its upper bound: 10,000 shards, 1M everywhere, 100 concurrent). That preset was internally contradictory — it set the concurrency to the ceiling the page warns against, spent hours on shard planning that the candidate cap then threw away, and produced a run that could not finish inside its own deadline on one token.

## 6. What changed, and what did not

- **Changed**: the `preset=corpus` button values (above) and its label ("Set to corpus-build limits"); the help note explains it is a coherent point, not the maximum.
- **Kept**: every default (interactive behavior is unchanged until an operator edits), every bound (they remain safety guardrails), the env-pin precedence, and the `max_hydrate ≤ max_candidates` rule.
- **Not enforced (documented instead)**: rules 2–5 above are advice; the form validates only bounds and the hydrate/candidates relation. Soft hints beat hard blocks here — an operator may deliberately run an incomplete experiment.

## Glossary

- **Bound** — the allowed range for a field (a guardrail).
- **Preset** — a coherent set of values for a purpose (interactive, corpus).
- **Meter** — a per-token allowance (search, core, GraphQL).
- **Point** — GraphQL's cost unit; roughly 1 per 100 requested items, minimum 1 per query.
- **Coherence rule** — a relation between fields that keeps a run finishable.

## Sources (verified 2026-10-04)

- Rate limits for the REST API — 5,000/hr authenticated, 100 concurrent (shared REST+GraphQL), 900 points/min REST, 2,000 points/min GraphQL, 60/hr unauthenticated: `https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api`
- Search rate limit — 30/min authenticated, 10/min unauthenticated, code search 10/min: `https://docs.github.com/en/rest/search/search`
- Rate-limit resources (`core`/`search`/`code_search`/`graphql`): `https://docs.github.com/en/rest/rate-limit/rate-limit`
- GraphQL points and batch guidance: `https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api`
- The meters and the touches rule in narrative form: `corpus-building-efficient-engineering.md` §3–§4

## Where to go next

- The meters in depth: `corpus-building-efficient-engineering.md`.
- The page's UX rules: `console-ux-redesign.md` §4.9.
- Operations (env pinning, corpus profile): `../docs/environment.md`.
- Implementation: `src/store/settings.py` (defaults), `src/serve/settings_spec.py` (bounds + preset), `src/serve/settings.py` (page + audit).
