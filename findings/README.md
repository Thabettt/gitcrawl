# Findings — GitHub Search Repos API Research for gitcrawl

**Project**: gitcrawl — a planned continuous, large-scale GitHub repository discovery crawler. **Date**: 2026-09-29. **Method**: parallel research agents + live docs verification (dispatching-parallel-agents + last30days-style online reading; all sources accessed 2026-09-29 unless noted).

## The one-paragraph version

Finding *every* repository that matches your criteria — language, stars, topics, activity, license, custom tags — and keeping that list fresh sounds like one API call. It isn't. GitHub's search has a small lobby (only the first 1,000 results of any query are reachable), two prepaid meters (search is 30 requests a minute; visiting repos is 5,000 an hour), a closed set of filters where a typo quietly becomes plain search text instead of an error, an index that lags real pushes, and no server-side way to ask for "has a Dockerfile" or "owner in Germany" at all. This bundle is the research that makes gitcrawl's design match reality instead of assumptions — every constraint, every workaround, and every source, verified against live docs.

## Why this exists

A crawler built on wrong assumptions fails in the worst way: silently. You get `200 OK`, plausible-looking JSON, and a corpus that is quietly incomplete or unfiltered. So the research came first, and it answered one question at a time:

1. What parameters does repo search actually support? (`01`)
2. How do you use them well at scale? (`02`)
3. Can you add custom parameters, and if not, what are the sanctioned workarounds? (`03`)
4. What already exists that uses these APIs — build or borrow? (`04`)
5. What did the first pass miss that would break a build? (`05`)
6. What is the *complete* universe of repo attributes, and how do you get each one? (`06`)

Start with `00-overview.md` for the glossary, audience, and next steps.

## How to read it

- **New to the API?** `00` → `01` (the reference) → `02` §§1–6 (the power guide) → `03` §§1–3 (the verdicts).
- **Designing the crawler?** `02` §§5–7 (playbook + code) → `06` §§4–8 (fetch plan) → `05` §A (critical gaps).
- **Choosing vendors or mirrors?** `04` §§3–7 (datasets, comparison table, recommendations) → `06` §5 (mirror fields).
- **Implementing filters?** `06` §§2–3 (searchable) + §6 (X → workaround per param) + §7 (typo/validation). Validate every new qualifier with the delta test — a silent `200` lies.
- **New to the whole project?** Read `../design/how-the-data-flows.md` first — it narrates the pipeline (filters → Find → hydrate → enrich → display → bundle) with each GitHub limitation explained — then come back here for reference depth.

## The files, one line each

- `00-overview.md` — background, goals, audience/prerequisites, reading path, glossary (`q`, 1000-cap, watermark, S/R/G/L/C/E/M/X), next steps.
- `01-search-repos-parameters.md` — canonical reference: top-level params (`q`, `sort`, `order`, `per_page`, `page`), the full `q` qualifier list, headers, limits/errors, curl/JS examples, and the last-30-days delta (no repo-search change 2026-08-29→09-29; code-search sunset 2026-09-27; issues `advanced_search`/`semantic|hybrid`).
- `02-advanced-parameter-usage.md` — power guide: query construction/encoding, qualifier combos, sort/page to the 1000-cap, `Accept`/`X-GitHub-Api-Version`/`Authorization`/ETag-304, the rate/error playbook, narrow-first + date-shard + `sort=updated` polling, full curl/JS/Python paginated + poll code, 17 pitfalls.
- `03-custom-parameters.md` — verdicts: arbitrary top-level params **NO (ignored, `200`)**; custom `q` qualifiers **NO (become text)**; the real custom tools are `props.*` (single-org only) + `topic:` + client post-filter; GraphQL customizes output, not input; proxy/FastAPI/own-DB/`gh`-extension workarounds with code; matrix + ToS risks.
- `04-existing-solutions.md` — the landscape: official (Web UI, `gh`, REST, GraphQL, code/Blackbird), SDKs (Octokit/PyGithub/go-github/octocrab), datasets (GHTorrent dead, GH Archive+BQ with the Oct-2025 cliff, GrimoireLab, Boa, SEART GHS ≥10★ template), third-party (Sourcegraph, grep.app, Libraries.io/ecosyste.ms/deps.dev), commercial (SerpAPI/Bright/Apify) + comparison table + (a)/(b)/(c)/(d) recommendations. **For a continuous crawl: copy SEART — sharded REST discover → local DB → GraphQL/ecosyste.ms enrich.**
- `05-research-gaps.md` — the gap audit from 3 parallel reviewers: 12 Critical items (since-enumeration, PK/lifecycle, auth/SSO/App/GHES, watermark/ETag/total/pagination, schema/score/4k-sort, bulk datasets, feeds, legal/ops, logging/SLOs, repro dates), Important items, out-of-scope items, and the patch plan for `01–04` with full evidence links.
- `06-exhaustive-parameters.md` — the master matrix: every S/R/G/L/C/E/M param (top-level, `q` qualifiers, repo fields, list params, props API, mirror fields) + the X→workaround table for ~30 unsearchable params (Dockerfile/coverage/CI/LOC/contributors/PRs/releases/commits/README/code/deps/dependents/vulns/Scorecard/funding/traffic/tiers/discussions/wiki/teams/CODEOWNERS/protection/cross-org props/updated/multi-lang/license-variants/deletes/star-history/LFS) + typo/validation + the fetch plan.

## The four things to remember

1. **Only 5 top-level params, and all the power is in `q`.** Validate qualifiers client-side — a typo is a silent text search, not an error.
2. **The three ceilings shape everything:** 1,000 fetchable results, 30 search requests a minute, ~4,000 repos scanned per query. So date-shard (`created:`/`pushed:` bisect), page sequentially at `per_page=100` with ~2s pacing, and use multiple tokens where allowed. Prefer `GET /repositories?since=` or org enumeration when the scope allows it (see `05` A1–A2, `06` §4).
3. **No server-side custom params.** Implement virtual params in a proxy or your own database; use `props.*` only if you administer a single org, otherwise a `topic:` convention plus client filtering.
4. **Don't re-search broadly on every run.** Store locally on the immutable `id` (the SEART pattern) and query your own database. Handle renames and deletes, watermark with overlap, and respect the legal gates (token-sharing, scraping, deletion/GDPR, resale) per `05`.
