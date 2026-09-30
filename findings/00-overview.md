# 00 — Overview: What This Research Is and How to Read It

> Project: gitcrawl. Date: 2026-09-29. Method: parallel research agents + live docs verification (all sources accessed 2026-09-29 unless noted).

## Background

gitcrawl is a planned continuous, large-scale GitHub repository discovery crawler: given criteria (language, stars, topics, activity, license, custom tags), it must find *every* matching public repository — not just the first page — and keep that list fresh over time without burning rate limits or violating GitHub's terms.

GitHub's search looks simple (`GET /search/repositories?q=...`) but hides hard constraints that break naive crawlers: only 5 top-level params, all filtering power packed into a single `q` string with a closed qualifier set, max 1000 fetchable results per query, max 4000 repos scanned per query, silent ignoring of unknown params/typos (still `200 OK`), separate `search` vs `core` rate buckets (30/min vs 5000/hr), an async search index that lags real pushes, and no server-side custom fields except single-org `props.*`. Around it sits a wider ecosystem — GraphQL, list-enumeration endpoints, GH Archive/BigQuery/World of Code/Software Heritage mirrors, ecosyste.ms/deps.dev enrichment, Sourcegraph code search, and paid scrape wrappers — each with different freshness, cost, and legal trade-offs.

## Goals

This bundle answers, in order:
1. What parameters does repo search actually support? (`01`)
2. How do you use them well at scale — encoding, sorting, paging, throttling, polling? (`02`)
3. Can you add custom parameters, and if not, what are the sanctioned workarounds? (`03`)
4. What already exists that uses these APIs — which tool for which job, at what cost? (`04`)
5. What did we miss that would break a build — the gap audit? (`05`)
6. What is the *complete* universe of repo attributes — searchable, enrichable, mirror-only, or unavailable — and exactly how to get each? (`06`)

Out of scope: private-code search internals, Enterprise Server administration, non-GitHub forges. Those are named where excluded so a reviewer doesn't mistake omission for oversight (see `05 §C`).

## Audience + prerequisites

Reader is a developer about to design or build gitcrawl. Assumes comfort with REST/JSON, URL encoding, pagination, and rate-limit backoff; no prior GitHub Search knowledge assumed. Keep the docs glossary (§5) nearby — `q`, 1000-cap, watermark, and `S/R/G/L/C/E/M/X` codes recur everywhere.

## Reading path

- New to the API? Read `00` → `01` (reference) → `02` §§1–6 (power guide) → `03` §§1–3 (verdicts).
- Designing the crawler? `02` §§5–7 (playbook + code) → `06` §§4–8 (fetch plan) → `05` §A (critical gaps: `since`-enumeration, `id` PK/lifecycle, auth/SSO, watermark/ETag, bulk datasets, legal gates).
- Choosing vendors/mirrors? `04` §§3–7 (datasets + table + a/b/c/d recommendations) → `06` §5 (mirror fields).
- Implementing filters? `06` §§2–3 (searchable) + §6 (X→workaround per param) + §7 (typo/validation). Validate every new qualifier with the delta test — silent `200`s lie.

Each file keeps its tables intact; narrative intros (Background/Goal, 4–6 lines) sit above them so a newcomer gets story first, reference second.

## Glossary (short)

- `q`: the single query string carrying keywords + `qualifier:value` filters (e.g. `language:python stars:>500`).
- Qualifier: a documented `name:value` filter inside `q` (closed set — customs become plain text).
- 1000-cap: only the first 1000 results per logical query are fetchable (`10×100`); `total_count` may be larger and is approximate.
- 4000-scan: ranking considers at most ~4000 matching repos per query — narrow queries rank better.
- Watermark: persisted `max(pushed_at)` per shard (+1h overlap) for incremental polling; never mix `pushed_at` watermark with `updated_at` ordering without overlap.
- `S/R/G/L/C/E/M/X`: availability codes used in `06` — Search `q` / REST repo / GraphQL / List-enumeration / Custom-props API / Enrichment REST / Mirror dataset / unavailable→workaround.

## Next steps

1. Apply the `05` action plan patches in order (auth/SSO, `since`-enumeration, `id`-PK lifecycle, watermark/ETag fixes, bulk-dataset bootstrap, legal gates).
2. Prototype the `06 §8` fetch plan against one golden org (e.g. `org:github`) and verify `id`-set coverage vs `since` scan before scaling tokens.
3. Re-verify quarterly: pin `X-GitHub-Api-Version`, record response headers, refresh star/pricing/version numbers (all marked "as of 2026-09-29").

Files: `01` canonical params · `02` power guide + full code · `03` custom verdicts · `04` landscape + costs · `05` gaps audit + evidence · `06` exhaustive matrix. Start with `01`. Building or onboarding? Read `../design/how-the-data-flows.md` first — the beginner narrative over this research.
