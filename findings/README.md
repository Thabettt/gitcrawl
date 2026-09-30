# Findings — GitHub Search Repos API Research for gitcrawl

> Project: gitcrawl — a planned continuous, large-scale GitHub repository discovery crawler. Date: 2026-09-29. Method: parallel research agents + live docs verification (dispatching-parallel-agents + last30days-style online reading; all sources accessed 2026-09-29 unless noted).

## Background

Finding *every* repo matching criteria (language, stars, topics, activity, license, custom tags) and keeping that list fresh sounds like one API call — until GitHub's constraints hit: 5 top-level params with all filtering in a single `q` string, a closed qualifier set that turns typos into silent plain-text matches, 1000 fetchable results and ~4000 scanned repos per query, separate `search` (30/min) vs `core` (5000/hr) buckets, an async index that lags pushes, and no server-side custom fields except single-org `props.*`. This bundle exists so gitcrawl is designed against reality, not assumptions.

## Goals

Answer in order: what search supports (`01`), how to use it well at scale (`02`), whether custom params exist and the sanctioned workarounds (`03`), what to build vs borrow (`04`), what we missed (`05`), and the complete attribute universe with a fetch plan (`06`). Start with `00-overview.md` for glossary, audience, and next steps.

## Reading path

- New to the API: `00` → `01` → `02` §§1–6 → `03` §§1–3.
- Designing the crawler: `02` §§5–7 → `06` §§4–8 → `05` §A.
- Choosing vendors/mirrors: `04` §§3–7 → `06` §5.
- Implementing filters: `06` §§2–3 + §6 (X→workaround) + §7 (validation).
- New to the whole project: `../design/how-the-data-flows.md` — the beginner narrative (filters → Find → hydrate → enrich → display → bundle) with every GitHub limitation explained, then come back here for reference depth.

## Files

- `00-overview.md` — background, goals, audience/prerequisites, reading path, glossary (`q`, 1000-cap, watermark, S/R/G/L/C/E/M/X), next steps.
- `01-search-repos-parameters.md` — canonical reference: top-level params (`q`, `sort`, `order`, `per_page`, `page`), full `q` qualifier list, headers, limits/errors, curl/JS examples, last-30-days delta (no repo-search change 2026-08-29→09-29; code-search sunset 2026-09-27; issues `advanced_search`/`semantic|hybrid`).
- `02-advanced-parameter-usage.md` — power guide: query construction/encoding, qualifier combos, sort/page to 1000-cap, `Accept`/`X-GitHub-Api-Version`/`Authorization`/ETag-304, rate/error playbook, narrow-first + date-shard + `sort=updated` polling, full curl/JS/Python paginated + poll code, 17 pitfalls.
- `03-custom-parameters.md` — verdicts: arbitrary top-level params **NO (ignored 200)**; custom `q` qualifiers **NO (become text)**; real custom = `props.*` (single-org only) + `topic:` + client post-filter; GraphQL custom output not input; proxy/FastAPI/own-DB/`gh`-extension workarounds with code; matrix + ToS risks.
- `04-existing-solutions.md` — landscape: official (Web UI, `gh`, REST, GraphQL, code/Blackbird), SDKs (Octokit/PyGithub/go-github/octocrab), datasets (GHTorrent dead, GH Archive+BQ with Oct-2025 cliff, GrimoireLab, Boa, SEART GHS ≥10★ template), third-party (Sourcegraph, grep.app, Libraries.io/ecosyste.ms/deps.dev), commercial (SerpAPI/Bright/Apify) + comparison table + (a)/(b)/(c)/(d) recommendations. **For gitcrawl continuous crawl: copy SEART — sharded REST discover → local DB → GraphQL/ecosyste.ms enrich.**
- `05-research-gaps.md` — gap audit via 3 parallel reviewers: 12 Critical (since-enumeration, PK/lifecycle, auth/SSO/App/GHES, watermark/ETag/total/pagination, schema/score/4k-sort, bulk datasets, feeds, legal/ops, logging/SLOs, repro dates), Important + out-of-scope + patch plan for `01–04` + full evidence links.
- `06-exhaustive-parameters.md` — master matrix: every S/R/G/L/C/E/M param (top-level, q qualifiers, repo fields, list params, props API, mirror fields) + X→workaround table for ~30 unsearchable params (Dockerfile/coverage/CI/LOC/contributors/PRs/releases/commits/README/code/deps/dependents/vulns/Scorecard/funding/traffic/tiers/discussions/wiki/teams/CODEOWNERS/protection/cross-org props/updated/multi-lang/license-variants/deletes/star-history/LFS) + typo/validation + fetch plan.

## Key takeaways for gitcrawl

1. Only 5 top-level params; all power is in `q`. Validate qualifiers client-side (typos = silent text search).
2. 1000-fetchable + 30/min + 4000-scanned → must date-shard (`created:/pushed:` bisect) + `per_page=100` sequential + 2s pacing + multi-PAT. Prefer `GET /repositories?since=` / org enumeration where scope allows (see `05` A1–A2, `06` §4).
3. No server custom params — implement virtual params in proxy/own DB; use `props.*` only if single-org admin, else `topic:` convention.
4. Don't re-search broadly every run — store locally on immutable `id` (SEART pattern) for arbitrary filtering; handle renames/deletes, watermark with overlap, and legal gates (token-sharing, scraping, deletion/GDPR, resale) per `05`.
