# Run Limits (what they are, what backs them, and how to set them)

**Date**: 2026-10-04. Companions: `corpus-building-efficient-engineering.md` (the meters), `console-ux-redesign.md` §4.9 (the page), `docs/environment.md` (operations), `../docs/superpowers/plans/2026-10-02-console-settings.md` (the implementation plan). Platform numbers re-verified 2026-10-04 against GitHub's docs.

**Purpose of this document**: the Limits page (`/settings`, System → Limits) has eight fields. Until now, nobody had written down where their values come from — the defaults were inherited from the console build, the bounds were round ceilings, and the "maximum" preset pushed every dial to its limit at once, which is not an operating point any real run should use. This file derives each limit from the rate budget, defines three coherent presets, and states the rules that keep a run from being configured into nonsense.

---

## The one-paragraph version

A run's size is bounded by three things: GitHub's meters (search 30/min, core 5,000/hr, GraphQL 5,000 points/hr per token), GitHub's anti-abuse ceilings (100 concurrent requests shared across REST and GraphQL; 900/2,000 points per minute), and the wall-clock deadline. The Limits page controls how much of those budgets one run may spend. The right values are not "as high as possible" — they are a coherent operating point where discovery, saving, and checking all fit inside the deadline with headroom. The defaults are the interactive point (minutes, laptop-friendly). The old "maximum" preset was replaced with a **corpus-build** preset: 1,000 slices, 100,000 repos at each stage, 24-hour deadline, batching on, 10 concurrent — about four hours of real budget on one token, so it finishes rather than aborting incomplete.

---

## 1. The eight fields, in plain words

| Field (code name) | Plain label | Default | Allowed (bounds) | What it bounds |
|---|---|---|---|---|
| `max_shards` | Search breadth (slices) | 10 | 1–10,000 | How many date slices the planner may create to get past the 1,000-result cap |
| `max_candidates` | Repos to keep per search | 500 | 1–1,000,000 | How many discovered repos the run holds on to |
| `max_hydrate` | Repos to save details for | 200 | 1–1,000,000 | How many candidates get their current details fetched (must be ≤ candidates) |
| `max_enrich` | Extra rules to check | 100 | 1–1,000,000 | Budget for file/country checks on hydrated repos |
| `request_deadline_seconds` | Stop a search after (seconds) | 3,600 | 60–86,400 | Wall-clock cap; past it the run aborts, marked incomplete |
| `graphql_batch` | Batch repo lookups | on | on/off | Whether GraphQL batching is used (off = one REST call per repo) |
| `graphql_batch_size` | Repos per batch | 20 | 1–20 | Aliases per GraphQL query; GitHub cuts off large batches |
| `limiter_max_concurrent` | Simultaneous requests | 10 | 1–100 | In-flight requests per endpoint/token |

**Two numbers, two jobs.** The *Allowed* column is a fence — the most the field will accept, there to stop typos and keep a run sane. The *Default* column is where the app starts. Neither is a recommendation for a big run; the presets in §5 are. The old "maximum" preset confused the three by pushing every field to its fence at once.

## 2. What backs them today (the honest audit)

- **Defaults (10 / 500 / 200 / 100 / 3,600 / on / 20 / 10)** — inherited, not derived. The settings plan states it explicitly: "defaults equal today's values, so nothing changes until an operator edits." They are sensible interactive values (a run measured in minutes), but no document derived them from the meters. They survive this audit: keep them.
- **`graphql_batch_size` 1–20** — backed. GitHub's GraphQL resource caps punish large `first` + nesting; the batch engine's own design says 20–50, and the project uses 20. The bound matches the implementation (`MAX_BATCH_SIZE=20`) and the DB check constraint.
- **`limiter_max_concurrent` 1–100** — backed, but read it carefully: 100 is GitHub's *documented hard ceiling*, shared across REST and GraphQL. It is not a target. The page's own help text says so. The operational value is ~10 per endpoint per token (900 points/min ÷ ~700 ms p50). Keep the bound at 100 (platform truth); never preset it to 100.
- **`request_deadline_seconds` 60–86,400** — a product choice: 24 hours is the longest a single run may take. Fine.
- **`max_shards` 1–10,000 and the three count limits 1–1,000,000** — round safety ceilings. Not wrong as guardrails, but they are not "proper limits": they ignore the rate budget entirely. The proper operating values are derived below.

## 3. The budget model (how to define a proper limit)

All numbers verified 2026-10-04 (see Sources). Per token:

| Meter | Rate | Buys |
|---|---|---|
| Search | 30/min = 1,800/hr | Planning counts + result pages; one page = 100 repos |
| Core (REST) | 5,000/hr | One hydrated repo per call (fallbacks, trees, contents) |
| GraphQL | 5,000 points/hr | A batch of ≤20 repos ≈ 1 point |
| Secondary | 100 concurrent (REST+GraphQL shared); 900 pts/min REST; 2,000 pts/min GraphQL | Pacing ceiling — stay far below |

Time estimates for a run of `C` candidates, `S` shards, batch size `B`:

- **Discovery** ≈ `(S + C/100) / 30` minutes. (Each shard needs a count query; each page returns 100 repos. Planning adds extra count queries while bisecting.)
- **Hydration** ≈ `C / (20 × 1)` GraphQL points ≈ `C/20 / 5,000` hours with batching; ≈ `C / 5,000` hours without.
- **Enrichment** ≈ per check: one batched query per `B` repos (≈1 point per 20 repos per check type), or one core call per repo without batching.
- **Wall clock** = discovery + hydration + enrichment + overhead, and must be < `request_deadline_seconds`, or the run aborts incomplete.

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
| Discovery | ~1,000 shard counts + ~1,000 pages | ~67 min |
| Hydration | ~5,000 GraphQL points | ~1 h |
| Enrichment (2 checks, batched) | ~10,000 points | ~2 h |
| **Total** | | **~4 h** (inside a 24 h deadline, with headroom for fallbacks) |

With batching off, the same run would need ~20 h of core for hydration alone and would likely abort. That is why the corpus preset keeps batching on.

## 5. The presets

| Preset | shards | candidates | hydrate | enrich | deadline | batch | size | concurrent | Use when |
|---|---|---|---|---|---|---|---|---|---|
| **Interactive (default)** | 10 | 500 | 200 | 100 | 3,600 | on | 20 | 10 | Exploring a filter; results in minutes |
| **Corpus build (the button)** | 1,000 | 100,000 | 100,000 | 100,000 | 86,400 | on | 20 | 10 | A real corpus on one token, overnight (~4 h of budget) |
| **Multi-token scale-up (documented, no button)** | 10,000 | 1,000,000 | 1,000,000 | 1,000,000 | 86,400 | on | 20 | 10/token | Only with 5–10 tokens; single-token 1M runs exceed the deadline and abort incomplete |

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
