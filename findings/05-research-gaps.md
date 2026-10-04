# 05 — Research Gaps Audit (What We Overlooked)

**Read this after `01–04`, before building.** The patches land in `01–04` and `06`; this file is the checklist.

**The one-paragraph version**: the first research pass got the core parameters right, but when three reviewers went through it against live docs, they found load-bearing blind spots — the `since`-enumeration backfill, lifecycle on immutable IDs, auth/SSO/App rate buckets, watermark/ETag/`total_count` traps, bulk datasets, feed alternatives, and legal/ops gates. Any one of these would break a production crawler, and none of them announce themselves — they just produce a corpus that is quietly wrong or a run that gets throttled forever. This file names every gap with a severity, the evidence, and the concrete fix, in the order the fixes should land.

> **Date**: 2026-09-29 (all evidence links accessed 2026-09-29 unless noted). **Method**: `requesting-code-review` — 3 parallel reviewer subagents (technical-API, landscape/architecture, legal/ops/repro) + re-read of `01–04` + README + live docs verification.
> **Verdict**: `01–04` are correct on core params/qualifiers/limits but have load-bearing gaps for building gitcrawl. Nothing in `01–04` needs deletion; ~12 Critical gaps need new sections, 12 Important need corrections, and the rest are rot-proofing.
> **Full evidence URLs**: see `## E. Evidence links` at the end of this file; each A/B item also cites inline.

## A. Critical — will break or misdirect the build if not fixed

These are the "you will ship a wrong corpus" items. Read them as a pre-flight checklist, not as criticism.

### A1. Missed `GET /repositories?since=` full enumeration (no search, no 1000-cap)

- `02 §6` / `04 §7` recommendation (b) only offer `search + created:` bisect for backfill (70 calls/3 min per broad query, unstable `total_count`). The bulk backfill primitive is `GET /repositories?since=<id>&per_page=100` + `Link: rel=next`, in creation-ID order, under the Core limit (5000/hr/PAT) — not Search (30/min).
- Evidence: `https://docs.github.com/en/rest/repos/repos` — "List public repositories … Pagination is powered exclusively by the `since` parameter."
- Fix (landed in `06 §4`): loop from `since=0`, checkpoint `max(id)`, math (~200M+ repos/100 ≈ 2M pages ≈ 400hr/PAT, /N via ID-range shards), caveat (minimal objects, creation-order only, pair with `GET /repos/{o}/{r}` enrichment).

### A2. Missed org/user enumeration as a search alternative

- For single-org/user scope (the `props.*` case in `03`), `GET /orgs/{org}/repos` (`sort=created|updated|pushed|full_name`, `type=all|public|forks|sources`) and `GET /users/{u}/repos`, `GET /users?since=` have no 1000-cap and are authoritative vs `q=org:x` sharding.
- Fix (landed in `04 §7` recommendation (b) and `06 §4`): "single org/user → prefer enumeration; reserve search shards for cross-GitHub queries."

### A3. Storage PK, dedupe, lifecycle absent

- `04 §7` recommendation (b) says "PK owner/name" — but `full_name` is mutable (rename/transfer). The key must be `id BIGINT PK` + `full_name UNIQUE` + `node_id`, plus lifecycle handling: renames (`GET /repos` 301 follow), deletes/private-turns (search silently drops them; `GET` 404 → tombstone), fork-network dedupe via `parent/source`.
- Evidence: `GET /repos/{o}/{r}` fields `id/node_id/full_name/owner/private/fork/archived`.
- Fix: add DDL sketch + indexes (`pushed_at`, `updated_at`, `stargazers`, GIN `topics`, `owner_id`), upsert on `id`, a `full_name` history table, `deleted_at/renamed_from`, periodic revalidation.

### A4. Auth model underspecified (scopes, SSO, App buckets, GITHUB_TOKEN)

- `04 §1.3` "PAT, no scope for public" omits the fine-grained minimum (`metadata:read`, single-owner, 50-token cap breaks naive multi-PAT), SAML SSO `X-GitHub-SSO: partial-results` silent filtering (not 422), GitHub App install (repo-subset only, search bucket separate `30/min` does not scale with `5k→12.5k/hr` core), and `GITHUB_TOKEN` being single-repo only (cross-org queries come back empty).
- Evidence: `rest/authentication/...`, `rate-limits-for-the-rest-api` vs `rest/search/search#rate-limit`, `troubleshooting#404-for-existing-resource`.
- Fix: document the Metadata-read baseline, check the SSO header (fail loudly), budget `search` vs `core` separately, forbid `GITHUB_TOKEN` for discovery, prefer App over OAuth (never embed `client_secret`).

### A5. GHES differences absent

- No hostname/version-skew matrix (`2.8` sort only `stars|forks|updated`, no `help-wanted-issues`/`props.*`), "rate limits disabled by default — ask admin", Elasticsearch CCR/repair lag, `environment:local|github` qualifier.
- Evidence: `enterprise-server@3.21/rest/search/search`, `.../about-searching-on-github`, blog 2026-03-03 CCR.
- Fix: add a GHES box: base URL param, feature-detect, admin limit lookup, index-health check.

### A6. Watermark + ETag + total_count + pagination wrong in combination

- `02 §6.3/§7.5` watermarks on `pushed_at` while ordering by `updated_at` (an opaque activity bump) — never mix them. `02 §4–6` promises `304 free` for search, but `total_count`/`score` churn + `sort=updated` instability → ~0% hit rate (best-practices: "stable sort… `sort=updated` reorders"). `02 §6.2` "verify `sum(shards)==total`" treats approximate `total_count` as ground truth. `02 §3` "`page=11+ → []`" contradicts `04 §1.3` "`422 Only first 1000`" — the truth is `422` at result 1001 (e.g. p21 at 50/pg) + often-absent `rel="last"`; never rebuild `?page=`, follow `Link: rel="next"` verbatim; Octokit `paginate()` aborts on search-422; GraphQL uses `first:100 + pageInfo{endCursor}` + 2025 resource-cap partials.
- Evidence: `rest/search/search` (timeouts/`incomplete_results`, 1000/4000 limits), `best-practices` (conditional/cachable), `using-pagination`, `graphql/overview/rate-limits`, saas-diary 2026-09-13 cap measurement.
- Fix: backfill-shard on immutable `created:`, live delta `pushed:>watermark-1h_overlap&sort=updated&order=desc` with watermark `max(pushed_at)` per shard + `id` dedupe; keep the ETag code but budget `x-ratelimit-*` not 304; validate via `id`-set diff + overlap, not `total_count`; catch cap-422 → auto-shard; 3-branch throttle classifier (`retry-after` → `remaining==0` wait `reset` keyed on `x-ratelimit-resource: search` vs `core` → else 60s×2^n+jitter max 5); distinguish 200-partial / 503-retry / 10s gateway shrink-`per_page`; branch spam-422 (`errors[].code==custom` → backoff) vs cap-422 (→shard) vs validation-422 (→fix).

### A7. Response schema + nulls + score + 4k-sort distortion undocumented

- The bundle never lists `items[]` (`id/full_name/private/fork/archived/disabled/visibility/topics[]/license(null|{key,spdx_id})/language(null)/size/stargazers_count/forks_count/open_issues/pushed_at/created_at/updated_at/score/custom_properties`). `score` is query-relative, meaningless off best-match, ties unordered → nondeterministic pages; never persist/sort by it. `sort` applies after the 4000-scan → a broad-query `sort=stars` is top-of-scanned, not global top; shard until `total<<4000`. `language:` = nullable primary Linguist (vendored ignored, `c++` canonicalization) → enrich via `languages_url`; `license:` recall-limited (`null`/`NOASSERTION`/`other` common) → null-safe post-filter. `archived` is read-only (still in repo search, excluded from the code index); `disabled` unsearchable; `is_template` vs `template:`.
- Evidence: `rest/search/search#search-repositories` schema + `#search-scope-limits`, enterprise 2.8 `score` note.
- Fix: nullable-aware deserializer (`license?`, `language?`, map `stargazers_count` not `stars`, ignore unknowns); dedupe `id/full_name`.

### A8. Offline bulk datasets missed (bigger than SEART)

- `04` covers GH Archive events + Boa but not `bigquery-public-data.github_repos.*` (SQL without tokens), World of Code (~351M raw / ~284M deforked, 7.3B commits, monthly + hourly LMDB), Software Heritage graph (58B nodes/1T edges, 2025-10-08 30 TiB ORC / 1.7 TiB history), HuggingFace `bigcode/the-stack-v2` (3B files/104M repos + license provenance), ClickHouse `github_events` playground / Snowflake / Databricks mirrors (zero-ops SQL vs BQ $5/TB past 1TB free).
- Evidence: `cloud.google.com/blog/.../github-on-bigquery`, `docs.cloud.google.com/bigquery/public-data`, `worldofcode.org`, `docs.softwareheritage.org/.../dataset.html`, `huggingface.co/datasets/bigcode/the-stack-v2`, `play.clickhouse.com`.
- Fix (landed in `06 §5`): freshness/cost/access/use table (bootstrap vs code vs provenance) + license caveat; dual-path architecture (batch `since`/BQ/WoC bootstrap → live search-poll/webhook tail) with scale math (calls = shards×10, time = calls/30/min/N tokens, $ = vendor $/1k×repos).

### A9. Incremental alternatives not evaluated

- Only `sort=updated` polling was prescribed. Missing: the `GET /events` family (ETag + `x-poll-interval`, ~5min delay), GitHub App webhooks (`repository created/public/push`, `fork`, `public`) for watched orgs (needs a public endpoint + install), GH Archive hourly as a bulk tail (1hr lag, Oct-2025 field loss already noted but not framed as a feed).
- Evidence: `rest/activity/events`, `webhooks/...#repository`, `gharchive.org`.
- Fix: add `02 §6.3b` decision table (polling simple/index-laggy vs events firehose unfiltered vs webhooks realtime/org-scoped vs GH Archive bulk).

### A10. Legal/ops blockers: token-sharing, scraping, robots, deletion, resale, PII, trademark

- Multi-PAT "rotation" without the §H guardrail ("You may not share API tokens to exceed rate limitations… suspension… sole discretion… warn via email") = suspension risk — rotate only across accounts you control, log token hashes, back off then stop. The scraping advice was over-permissive: continuous commercial gitcrawl qualifies for neither the Researcher (public non-personal + open-access pubs only) nor Archivist exception; the `§C.5` reference is stale → current Acceptable Use §7; `github.com/robots.txt` (`Disallow: /search`?) was never checked — default API-only, HTML fallback needs legal review. No deletion/privatization runbook (search never pushes deletes; `GET` 404 = purge/quarantine in N days, audit-log `repo.destroy`, retention window, email minimization, GDPR/CCPA access/erase via `privacy@github`). The resale/high-throughput subscription trigger (§H "GitHub may offer subscription… for high-throughput… or resale") was ignored despite the `crawl→store→serve` recommendation. The spam/selling-PII ban (§H + AU §7) was not operationalized (forbid `user:email`, recruiter use, bulk outreach; allowlist stored fields). Trademark gap (`GITHUB®/OCTOCAT®`, no endorsement, no Invertocat logo) if gitcrawl ships UI.
- Evidence: ToS §H, Acceptable Use §7, Privacy Statement, Logo Policy/Brand Toolkit (verified 2026-09-29).
- Fix: legal gates + runbooks before any crawl/serve/scrape step; least-privilege fine-grained PAT (no `repo` scope for public), vault/OIDC, `User-Agent: gitcrawl/…`, secret-scanning.

### A11. Token storage, audit logging, observability missing

- No scope/storage guidance, no per-request audit log (timestamp, query hash + `q/sort/order/per_page/page`, `etag/If-None-Match`, `x-ratelimit-*/retry-after/x-poll-interval/Link`, `total_count/incomplete_results`, status, token fingerprint, latency), no SLOs (`search_remaining`, `incomplete_results_ratio`, `422/403+429` rates, p95 latency, shard coverage).
- Fix: mandate logging + dashboards; pace from response headers (authoritative over `GET /rate_limit`, which costs secondary and varies regionally; poll sparingly).

### A12. Reproducibility rot: dates, citations, version pinning (status 2026-09-29: `01` sources since fixed with full dated links; counts below suffixed — keep re-verifying quarterly)

- `01` sources lacked access dates; secondary numbers (`GITHUB_TOKEN 1k/hr`, App `5k→12.5k`, `2000pts/min`, `~10 concurrent`) were uncited; `2022-11-28 EOS 2028-03-10` + `2026-03-10 removes rate→resources.core` uncited (omitted header defaults `2022-11-28`, dead version → `410 Gone`, additives apply to all — handle 410 as upgrade); stargazer-privacy (2026-06-30 + 2026-09-04 star-history) + `text-match` (repos name+desc only, indices vs `fragment`) + `incomplete_results (not necessarily incomplete)` + the last-30-days delta (Sep 2026 Copilot/Actions/CodeQL, code sunset 2026-09-27, issues `semantic|hybrid` 2026-04-02) each need per-claim links or a downgrade to "observed". Stars/pricing (`Octokit ~7.8k`, `go-github 11.3k v92`, `Apify $0.90/1k`, `Bright $1.5/1k`, `ecosyste.ms 343M as of 2026`, GH Archive `18k vs 2.7M`) will rot.
- Fix: `accessed YYYY-MM-DD + apiVersion` on every source; "as of 2026-09-29, re-verify" suffix on all counts/pricing; always send `X-GitHub-Api-Version`, record response headers, re-verify quarterly; keep `256-char excl operators/qualifiers + max 5 AND/OR/NOT` + `4000 scanned` as verbatim quotes with links in all files.

## B. Important (will cause overruns / bad SLAs)

Not fatal, but each one costs time, calls, or trust if unhandled.

- Shard planner: monthly `created:` buckets halved on `>=1000`, narrow `stars:` slices for `>1000★`, `language+license` fan-out, persisted shard state machine (`pending/active/done/incomplete`).
- Token coordinator: `scheduler → Redis bucket per reset/retry-after → N PAT pool`, separate `search`/`core`/`code_search` buckets, jittered backoff, pause-all-workers.
- `is:private` model: unauth → 422/empty; a token sees the accessible subset only; require a repo-scope check, treat empty as "no access".
- Code/repo qualifier confusion: code needs auth 10/min, `in:file/extension:`, default-branch `<384KB/<500k/active-year` subset, sort→`indexed` only; `fork:true` (repos) vs `is:fork` (code); gate code-content to Sourcegraph/grep.app.
- `per_page>max` clamped silently (no error) — copy the clamp warning to `01`, verify `items.length` vs requested; `page=0/negative`, `per_page=0` → 422; allowlist `1..100` / `>=1`.
- Unicode/emoji/CJK must be UTF-8 + `encodeURIComponent`; `*` in `*..n` → `%2A`; the 256-count is keywords only.
- Testing harness: golden-org exhaustive check, `id`-set diff vs a `since` sample, `404`-tombstone + `incomplete_results` dashboards.

## C. Explicitly out of scope (state once to close)

- No global repo sitemap (only per-Pages `sitemap.xml`) — not a path.
- GHE audit-log streaming, Dependabot/SBOM/Advisories/secret-scanning, OpenAlex/Crossref — enrichment or Enterprise-internal, not public discovery (deps.dev already covered).

## D. Action plan

1. Patch `01`: access dates, clamp warning, `410` handling, `is:private` + schema-nulls box. (Done 2026-09-29 — see `01` header + §4 + §6 with full URLs.)
2. Patch `02`: fix watermark/ETag/`total_count`/page-11/Link/GraphQL/throttle sections per A6; add the `§6.3b` feed decision table + shard planner + coordinator box + testing checklist. (Partially done 2026-09-29 — full crawl-loop + poll code backfilled in §7.3/§7.5. The feed decision table was never added; the polling/events/webhooks/archive options live in `04 §3.2` and `06 §5`.)
3. Patch `03`: SSO/App/`GITHUB_TOKEN`/GHES notes; keep the verdicts (they stand). (Done 2026-09-29 — full sources with issue links.)
4. Patch `04`: add `§1.6` (`since`/org enumeration), `§3.6` (BQ/WoC/SWH/Stack/ClickHouse), the cost/SLA/dual-path notes, legal gates + robots + deletion + trademark. (Partially done 2026-09-29 — full §8 sources + inline links; enumeration + bulk datasets landed in `06` §§4–5, and the cost/SLA/dual-path notes in `06 §8` + `04 §7`.)
5. This file (`05`) is the checklist — close each item with a commit + re-verify quarterly.

## E. Evidence links (all accessed 2026-09-29 unless noted)

- List public repos (`since` cursor only): https://docs.github.com/en/rest/repos/repos (accessed 2026-09-29)
- Get a repository (schema, 301/404, `parent`/`source`, `security_and_analysis`): https://docs.github.com/en/rest/repos/repos#get-a-repository (accessed 2026-09-29)
- Search mechanics (1000-cap, 4000-scan, `incomplete_results`, 256-char/5-op, rate): https://docs.github.com/en/rest/search/search?apiVersion=2022-11-28 (accessed 2026-09-29)
- Repo qualifiers + `props.*` single-org rule: https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories (accessed 2026-09-29)
- Search syntax + sorting + forks + troubleshooting: https://docs.github.com/en/search-github/getting-started-with-searching-on-github/understanding-the-search-syntax (accessed 2026-09-29), https://docs.github.com/en/search-github/getting-started-with-searching-on-github/sorting-search-results (accessed 2026-09-29), https://docs.github.com/en/search-github/searching-on-github/searching-in-forks (accessed 2026-09-29), https://docs.github.com/en/search-github/getting-started-with-searching-on-github/troubleshooting-search-queries (accessed 2026-09-29)
- Pagination (`Link`, clamping): https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api?apiVersion=2026-03-10 (accessed 2026-09-29)
- Rate limits (primary + secondary) + Rate Limit endpoint: https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api (accessed 2026-09-29), https://docs.github.com/en/rest/rate-limit/rate-limit (accessed 2026-09-29)
- Best practices (ETag/`304`, `x-poll-interval`, stable sort): https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api (accessed 2026-09-29)
- Auth (PAT/OAuth/App/`GITHUB_TOKEN`, SAML SSO `X-GitHub-SSO`, failed-login lockout, `User-Agent`): https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api?apiVersion=2026-03-10 (accessed 2026-09-29)
- Troubleshooting REST (422 codes, timeouts, 410): https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api (accessed 2026-09-29)
- API versions + breaking changes: https://docs.github.com/en/rest/about-the-rest-api/api-versions (accessed 2026-09-29), https://docs.github.com/en/rest/about-the-rest-api/breaking-changes (accessed 2026-09-29)
- GraphQL search + rate/points + resource caps: https://docs.github.com/en/graphql/reference/queries#search (accessed 2026-09-29), https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api (accessed 2026-09-29), https://github.blog/changelog/2025-09-01-graphql-api-resource-limits/ (accessed 2026-09-29)
- GHES search (rate limits disabled by default; version skew): https://docs.github.com/en/enterprise-server@3.21/rest/search/search (accessed 2026-09-29; re-verify per GHES version)
- Events API + webhooks + GH Archive: https://docs.github.com/en/rest/activity/events (accessed 2026-09-29), https://docs.github.com/en/webhooks/webhook-events-and-payloads#repository (accessed 2026-09-29), https://www.gharchive.org/ (accessed 2026-09-29)
- GH Archive payload change 2025-08-08: https://github.blog/changelog/2025-08-08-upcoming-changes-to-github-events-api-payloads/ (accessed 2026-09-29); issues: https://github.com/igrigorik/gharchive.org/issues/310 (2025-07-16), https://github.com/igrigorik/gharchive.org/issues/312 (2025-10-12); cliff study: https://codepulsehq.com/research/github-archive-payload-cliff (accessed 2026-09-29)
- 1000-cap measurement: https://saas-diary.com/tech-log/github-search-api-422-first-1000-results-only/ (2026-09-13, accessed 2026-09-29)
- Bulk datasets: https://cloud.google.com/blog/topics/public-datasets/github-on-bigquery-analyze-all-the-open-source-code (2016-06-30), https://docs.cloud.google.com/bigquery/public-data (accessed 2026-09-29), https://worldofcode.org/docs/ (accessed 2026-09-29), https://docs.softwareheritage.org/devel/swh-dataset/graph/dataset.html (accessed 2026-09-29), https://huggingface.co/datasets/bigcode/the-stack-v2 (accessed 2026-09-29) + paper https://arxiv.org/pdf/2402.19173 (accessed 2026-09-29), ClickHouse playground https://play.clickhouse.com (accessed 2026-09-29)
- Star privacy 2026-06-30 + star-history 2026-09-04 + breakage: https://github.blog/changelog/2026-06-30-upcoming-access-restrictions-to-public-api-endpoints-and-ui-views/ (accessed 2026-09-29), https://github.blog/changelog/2026-09-04-new-api-endpoint-provides-privacy-safe-star-history-data (accessed 2026-09-29), https://github.com/star-history/star-history/issues/539 (2026-07-04, accessed 2026-09-29); starring docs: https://docs.github.com/en/rest/activity/starring?apiVersion=2026-03-10 (accessed 2026-09-29)
- ToS + Acceptable Use + Privacy + Trademark: https://docs.github.com/site-policy/github-terms/github-terms-of-service (accessed 2026-09-29), https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies (accessed 2026-09-29), https://docs.github.com/en/site-policy/privacy-policies/github-privacy-statement (accessed 2026-09-29), https://github.com/github/site-policy (accessed 2026-09-29)
- `robots.txt` check (run before any HTML fallback): https://github.com/robots.txt (must fetch + record `Disallow: /search` on research date; accessed 2026-09-29)
- Dependents no-API (SO 2019-11-06, still valid 2026-09-29): https://stackoverflow.com/questions/58734176/how-to-use-github-api-to-get-a-repositorys-dependents-information-in-github (accessed 2026-09-29)
