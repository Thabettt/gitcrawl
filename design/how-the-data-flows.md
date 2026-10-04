# How the Data Flows (read this first if you're new)

**Date**: 2026-09-29 (updated 2026-10-02). Companion to `spec.md`, `contracts/search-api.md`, and `findings/06-exhaustive-parameters.md`. No prior GitHub API knowledge assumed — every concept is introduced where it's first used.

**The document set** (read together — what it does, how fast it can go, why it exists, and what else is out there):
- This document — what gitcrawl does and the stages data passes through.
- `corpus-building-efficient-engineering.md` — GitHub's rate meters, the math of what is findable vs. savable, the legal limits, and the designed batching engine.
- `gitcrawl-vs-seart.md` — how gitcrawl compares to SEART GitHub Search, where each wins, and why gitcrawl fits this project's thesis work.
- `landscape-comparison.md` — the full adjacent-tool landscape, the capability gap that justifies the build, and the `gitcrawl` name collision.

## The one-paragraph version

You describe what you want in a filter file and click **Find**. gitcrawl checks your filters for mistakes, asks GitHub's live search for matching repositories, fetches full details for each match, applies the filters GitHub itself can't express, enriches the survivors, figures out owner countries, merges everything into one result set, shows it to you, and saves a replayable bundle. Each stage exists because of a specific GitHub limitation — this doc explains every stage and its reason.

## Cast of characters (the only 5 things you must know)

- **GitHub search** (`GET /search/repositories`): the front door. You send one text string (`q`) with filters inside it (e.g. `language:rust stars:>100`) and get back *thin* repo records: name, description, stars, language, dates, plus a `score`. It returns at most 100 per page and 1000 per query, allows ~30 requests/minute per token, and silently treats typos as plain words instead of erroring. Full reference: `findings/01-search-repos-parameters.md`.
- **Hydration** (`GET /repos/{owner}/{repo}`): asking for one repo's *full* record — topics, license, exact counts, flags, dates. Costs 1 call per repo from a separate budget (5,000/hr per token). Unchanged repos can be re-fetched nearly free via caching headers (ETag).
- **Virtual filters**: filters GitHub never built (`has_dockerfile`, `min_commits`, `owner_country`). gitcrawl computes them itself per repo, after hydration.
- **Enrichment**: extra detail fetched per repo beyond the full record — language byte breakdowns, file trees, releases, funding links, discussions. Each costs calls, so it's spent only on repos that survived filtering.
- **Run bundle**: the saved artifact of one Find — your filters + when it ran + what GitHub said + final results. It's how a second device replays your search and how a thesis proves what was measured.

## The journey, stage by stage

```
filters.json → Validate → Live search → Thin matches → Hydrate → Virtual filters
→ Enrich → Geo resolve → Merge + cache → Display → Bundle/export
```

### Stage 0 — Your filters (a file, not a form)

Everything starts as a `filter-spec v1` JSON doc: the `q` string GitHub understands, sort/order/page options, and `virtual` filters only gitcrawl understands. Because it's a file, it travels between devices; because it holds no tokens, it's safe to share. Example: the thesis Rust corpus (`language:rust`, not forks, pushed in range, `min_commits: 100`, `min_loc: 5000`). Contract: `contracts/search-api.md`.

### Stage 1 — Validation (mistakes die here, free)

Before any network call, every qualifier is checked against the documented allowlist. `updated:>2024-01-01` (doesn't exist — correct is `pushed:`) or `is:archive` (correct is `archived:true`) is rejected with a hint, because GitHub would have returned `200 OK` with *unfiltered* results and no error. A passing filter set gets a `filter_hash` fingerprint used for caching and replay. Why this stage exists: GitHub's silent-`200` behavior makes typos the most expensive bug — paid in wrong data, not errors.

### Stage 2 — Live search (thin matches arrive)

The validated `q` goes to GitHub search, pages of 100 followed via `Link` headers. Each match is thin (see cast above); the `score` field is thrown away immediately — it's relevance *relative to that query*, meaningless to store or sort by later. Two honest limits surface here: past 1000 results GitHub errors (`422`) instead of paging, so broad queries must be narrowed (date-sharded) rather than forced; and `total_count` is an estimate, so it's displayed as approximate with an `incomplete` flag when timeouts cut a page short. Throttling is shared across all users of a token (30/min), so concurrent Finds queue with "retry in Ns" instead of failing.

### Stage 3 — Hydration (thin → full, only for candidates)

Each surviving match gets its full record (1 call each, separate budget). This is the most expensive stage per repo, so two economies apply: unchanged repos revalidate nearly free (ETag → `304`), and enrichment never starts for repos that later fail virtual filters. A repo renamed since discovery is followed (`301`); a deleted/privatized one becomes a recorded tombstone, not a crash.

### Stage 4 — Virtual filters (gitcrawl's own screening room)

Now the filters GitHub can't express run per repo, cheapest first: counts from the hydrated record (`min_commits` via commit count; `min_loc` is accepted but recorded-only until an estimate tier ships — see `loc-dilemma.md`), then file checks (`has_dockerfile` via one file-tree fetch covering all paths — never one call per file; this same machinery later becomes the agent-detection file channel), then `owner_country` via the geo pipeline. Failures drop with a recorded reason and are counted in the run metadata, so "47 repos matched search, 31 survived filters" is always explainable. The full cost-order table (free → mirrors → single-call → batched → deep) lives in `research.md` D12; the scheduler behind it segments candidates across tokens survivors-first, so even a stopped-early run stays useful.

### Stage 5 — Enrichment (detail only for survivors)

Only repos passing every filter get enriched: language byte breakdowns, releases, funding links, discussions, Scorecard linkage. Rules that keep this stage cheap: one file-tree call covers all path questions; GraphQL batches only where one query replaces three or more REST calls; identical files across repos are fetched once by content hash; package data comes from zero-token mirrors (ecosyste.ms, deps.dev) before paid GitHub calls. Nothing here is fetched for display you won't show.

### Stage 6 — Geo resolve (free text → country, honestly)

Owner `location` is unstructured human text ("🇩🇪 Berlin", "Lagos", "🌍 remote", often empty). The pipeline from `findings/06-exhaustive-parameters.md §6.1`: flag emojis decode directly to country codes (deterministic, free); text is normalized and split ("Berlin / NYC" → two candidates); an offline city→country gazetteer resolves cities with zero calls; a country-alias table catches variants ("USA", "Nederland"); only the residue touches a geocoder, cached forever by normalized string; weak hints (domain endings, timezones) break ties only. Every result stores `{country_iso, confidence, raw_location}` plus an explicit unmatched bucket — an honest unknown beats a confident wrong answer, and downstream filters threshold on confidence.

### Stage 7 — Merge + cache (one result set, many devices)

Survivors merge by immutable repo `id` (never by name — names change on rename/transfer), so the same filter file run on two laptops produces unionable, dedupe-safe sets. The merged set caches under `filter_hash` briefly: an identical Find minutes later costs zero GitHub calls. Owner geo resolves cache by owner id across repos.

### Stage 8 — Display (cards with provenance)

Each card shows enriched metadata + virtual-filter badges + owner country with confidence + *when* it was measured (`ran_at`). Truncation is always declared: capped-by-1000, timed-out pages, and throttled retries appear as labeled flags, never silent gaps.

### Stage 9 — Bundle and export (the replayable artifact)

The run persists `{filter_hash, ran_at, api_version, total_count, incomplete_flags}` plus the raw upstream JSON per repo. `GET .../export` downloads it (JSON + CSV). Byte-identical reproduction comes from this bundle — re-running the same file later may drift (stars move, repos vanish) and the bundle is what explains the diff. For the thesis, this bundle *is* the replication package's raw layer.

## The exhaustive run (when bounded isn't enough) — still live-only

Everything above is the per-Find live flow. For exhaustive coverage the *same* live run scales up: sharded date-range discovery plus ID-cursor backfill into the within-run working set (`spec.md` US1/US2, `design` scheduler/store stages), all executed on the go at `ran_at`. There is no second background path, no watermarks, no tails. A bounded Find answers "top matches right now"; an exhaustive run answers "everything matching right now, fetched live." Both feed the same display and bundle stages.

## Optional epilogue: cloning (never required)

Fetching never clones. If you want code on disk — for the thesis detection stage or just to read offline — the Clone control takes a count (slider + digit input, top-N by current sort) and a mode (shallow, file-only, or windowed-log), previews the disk estimate from stored sizes, warns on low disk, then clones into `clones/{run_hash}/`. Retries skip finished repos; progress lands in run metadata. Zero clones is a complete, valid run — cloning is an epilogue for later stages, not part of the Find.

## Thesis-corpus worked example (Rust adoption study)

Filters: `language:rust fork:false pushed:>2024-09-29` + virtual `min_commits: 100, min_loc: 5000` + frame `{buckets, attrition: count, size_splits, study_window}`. Find runs the live flow above; survivors then enter the thesis tracks in build order: frozen trace-pack applied (guidance content fetched, generic weights, exclusions) → windowed commit history (BigQuery primary, adoption-date + ratios) → PR channel (branch/label/outcome, Codex rule) → file inventory (list + `.gitignore` scan, two-clone strategy) → crates.io linkage → Egypt snowball (seed → frontier → fallback) in parallel → validation sampler exports n=400 with κ + Chapman. Raw upstream JSON per repo is stored throughout — that store *is* the replication package's raw layer, and the frozen pack + frame config in the bundle is what makes the Rust numbers comparable with the other five stacks.

## Where to go next

- Run it: `quickstart.md` (golden-org validation with evidence to record).
- Build it: `tasks.md` T001→T039, tests-first per story.
- Settle behavior: `contracts/search-api.md` (params, translation table, errors).
- Understand any attribute: `findings/06-exhaustive-parameters.md` (the master matrix).
- Know the limits: `findings/05-research-gaps.md` (what breaks, audited).
- Go faster: `corpus-building-efficient-engineering.md` (meters, the touches rule, strategies, GraphQL batching design).
- Compare tools: `gitcrawl-vs-seart.md` (shared research catalog vs. live evidence-producing instrument).
- Survey the market: `landscape-comparison.md` (adjacent tools, the capability gap, and the name collision).
