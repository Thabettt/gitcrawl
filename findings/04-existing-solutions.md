# 04 — Existing Solutions Using Search APIs (Landscape, Pros/Cons, Recommendations)

**Read this after `01–03` when choosing build-vs-borrow.** Feeds `06 §5` (mirror fields) and `06 §8` (dual-path architecture).

**The one-paragraph version**: you don't have to crawl from zero. Official surfaces (`gh`, REST, GraphQL), SDKs, dataset mirrors (GH Archive, SEART GHS, ecosyste.ms), code-search indexes (Sourcegraph), and paid wrappers each solve one slice of the problem — live discovery, history, enrichment, or code content — with different freshness, cost, and terms-of-service constraints. This file surveys them honestly, compares them in one table, and ends with (a)/(b)/(c)/(d) recommendations. The conclusion: for a continuous crawl, copy the SEART pattern — sharded REST discovery → local database → GraphQL/ecosyste.ms enrichment — and use burst vendors only for one-offs.

> **Research date**: 2026-09-29. Bias 2025–2026 docs/changelogs/activity. Scope: repo-discovery at scale (the gitcrawl use-case).

## 1. Official GitHub surfaces

### 1.1 GitHub Web Search UI

`https://github.com/search?q=...&type=repositories` + Advanced search (docs: https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories, accessed 2026-09-29). Same qualifiers as the API. The code side is internal (Blackbird — https://github.blog/engineering/architecture-optimization/the-technology-behind-githubs-new-code-search/), web-only, and requires login. No bulk export; scraping violates ToS expectations (https://docs.github.com/site-policy/github-terms/github-terms-of-service). Best for: prototyping qualifiers before coding. Free/proprietary.

### 1.2 GitHub CLI `gh search repos`

A thin CLI over REST (manual: https://cli.github.com/manual/gh_search_repos; source: https://github.com/cli/cli/blob/trunk/pkg/cmd/search/repos/repos.go, both accessed 2026-09-29). Flags mirror qualifiers: `--language`, `--stars`, `--topic`, `--created`, `--archived`, `--limit 1..1000`, `--sort stars|forks|updated`, `--json`, `--web`. Inherits 30/min auth. `SearchMaxResults=1000` enforced client-side. Strengths: zero-code, pipeable to `jq`, good for cron prototypes. Weaknesses: same 1000-cap, no date-splitting/checkpointing. Best for one-off discovery. OSS MIT `cli/cli`, very active.

### 1.3 GitHub REST `GET /search/repositories`

The canonical discovery path (ref: https://docs.github.com/en/rest/search/search, accessed 2026-09-29): `q`, `sort` (stars|forks|help-wanted-issues|updated), `order`, `per_page` max 100, `page` max 10 → 1000 total. Auth 30/min, unauth 10/min (code separate 10/min auth-required; rate docs: https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api). Max 4000 scanned, `incomplete_results` on timeout. Beyond 1000 → `422 "Only the first 1000 search results are available"` at page 11/`per_page=100` (live-verified 2026-10-06/07; also measured 2026-09-13: https://saas-diary.com/tech-log/github-search-api-422-first-1000-results-only/); `total_count` is approximate, can exceed 1,000, and drifts over hours (38,820 → 38,823 → 38,833 in ~5 h; live-verified 2026-10-06/07). Workaround: narrow via `created:`/`stars:` intervals recursively — **replace** the date clause per child, since duplicate same-type qualifiers union rather than intersect (repo-scoped probe, live-verified 2026-10-06/07) — and treat `sum(shards)==total` as a sanity check, not truth. Strengths: rich qualifiers, stable versioning (`2022-11-28` → `2026-03-10`; versions: https://docs.github.com/en/rest/about-the-rest-api/api-versions). Weaknesses: 7 shards × 10 pages = 70 calls ≈ 3 min minimum per broad query; `total_count` unstable; no history. Best for authoritative live discovery — gitcrawl's shard strategy builds on this. Free (PAT, no scope for public).

### 1.4 GitHub GraphQL `search(type:REPOSITORY, query:)`

The same index, but you choose the fields; `repositoryCount`, `edges{node{...}}`, max 100/page, max 1000 total. Auth required. Points model: PAT 5000/hr (10k GHE Cloud org app), App install 5000–12500/hr, Actions `GITHUB_TOKEN` 1000/hr, min 1/query, scales with `first/last` fan-out; secondary 2000 pts/min; the 2025-09-01 single-query resource caps → partials on large `first`+nesting. Cost = `round(connection-requests needed / 100)`, minimum 1 point, 5,000 points/hour for users — batching N repos into one request is far cheaper than N requests (live-verified 2026-10-06/07). Schema trap: `owner.databaseId` is not valid on the `RepositoryOwner` interface and fails per-alias under HTTP `200`; select it with inline fragments (`... on User` / `... on Organization`) (live-verified 2026-10-06/07). Strengths: avoids over-fetch, `rateLimit{cost,remaining}` introspectable, one-shot enrichment. Weaknesses: same 1000-cap, cost math punishes `first:100`+nested connections. Best for metadata enrichment (SEART GHS uses GraphQL exclusively). Free/proprietary.

### 1.5 Code Search (legacy REST vs Blackbird web-only)

Legacy `/search/code`: keyword + `language:/extension:/in:file`, 100/page, best-match only (sort deprecated 2023-04-10), auth mandatory 10/min. New Blackbird (Rust, 115TB→25TB index, 15.5B docs beta): `github.com/search?type=code` regex/boolean/`symbol:/path:` — **no public REST parity**. Indexed subset only (default branch, <384KB, UTF-8, <500k files/repo, active last year, forks only if stars>parent+push, archived excluded); web capped 100 (5 pages); ≤1000 chars. Best for manual spot checks; at scale use Sourcegraph/grep.app or BigQuery mirrors. Free w/ login, closed-source (blog 2023-02-06).

## 2. SDKs / wrappers (they inherit every limit)

| SDK | Lang | Stars/activity | Strengths | Weaknesses | Best for |
|---|---|---|---|---|---|
| Octokit.js | JS/TS | ~7.8k, active Mar 2025 deprecation handling | GitHub-maintained, `paginate`, throttling/retry plugins, typed `search.repos()` | Still 1000-cap/30-min; misconfig masks 429 vs 422 | JS workers, Actions |
| PyGithub | Python | ~7.7k, v2.7.0 2025-07-31 | OO `search_repositories(q)`, notebooks | Manual pagination/rate handling; LGPL-3.0 | Python one-offs |
| go-github | Go | 11.3k★ 2.6k forks v92 | Typed `RateLimitError` vs `AbuseRateLimitError`, `SleepUntil...`, iterators, `go-github-ratelimit` | Still 1000-cap; GraphQL via separate lib | Go continuous crawler |
| octocrab | Rust | 1349★ v0.49.7 2026-03-30 | Async, `search` module, raw HTTP escape hatch | Typed API behind GitHub; smaller community | Rust crawler |

No SDK bypasses the caps — they smooth retries only.

## 3. Crawling / dataset tools

### 3.1 GHTorrent — DEAD

A MySQL/Mongo mirror that stopped updating around 2019–2021; `ghtorrent.org` is down/hijacked. Useful only for legacy reproducibility. Its BigQuery snapshot froze in 2019-06.

### 3.2 GH Archive + BigQuery — historical stream, not search

Hourly `*.json.gz` since 2011 (`igrigorik/gharchive.org`) + BigQuery. It polls `/events` pages 1–3 with ETag, not the search API. An Oct 2025 caching bug dropped it to ~18k rows/day (vs 2.7M, #312); May 2025 saw a completeness drop (#310). **The payload cliff:** the 2025-08-08 changelog (https://github.blog/changelog/2025-08-08-upcoming-changes-to-github-events-api-payloads/) → brownout 2025-09-08 → permanent 2025-10-07 removed slow fields: `PullRequestEvent.pull_request` 48→5 fields, `PushEvent.commits/size` removed; PR share 8%→<1% in 2026 samples. Upside: prior latency up to 8h → near-realtime + faster endpoints (trade IDs survive, rehydrate via REST). Strengths: the only global history, no 1000-cap, hourly, SQL. Weaknesses: events ≠ criteria search; post-Oct-2025 needs per-repo refetch. Best for backfill/trends, a complement to live search. OSS crawler, data public + BQ query cost (BQ 2026: $6.25/TiB US, 1TiB/mo free, 10MB minimum per query/table, `LIMIT` does NOT reduce scan — dry-run + prune with bare `WHERE event_date=` or die; https://cloud.google.com/bigquery/pricing).

### 3.3 GrimoireLab / Perceval — enrichment, not discovery

CHAOSS Python: Perceval `github` backends → SirMordred → ES/MariaDB. REST+git, PAT/App tokens, `from-date` incremental, identity merging. 322★, Perceval 1.4.6 (2026-03-03). You feed it a repo list; it does not solve the 1000-cap and is heavyweight. Best for longitudinal per-repo enrichment. GPL-3.0.

### 3.4 Boa + MSR — frozen corpus

A DSL over TB-scale snapshots. No rate limits, reproducible, but stale, fixed languages, no live filtering. Best for frozen code-content studies. Academic.

### 3.5 SEART / GHS + Data Hub — the closest prior art to gitcrawl

A Spring Boot crawler mines GitHub for repos **≥10 stars** into MySQL, serving REST+UI (`seart-ghs.si.usi.ch`); the Data Hub shallow-clones ≥10★ Java/Python (316k repos, 22M files, 2024 paper) at ~1.3k/day, tree-sitter parsed. **GraphQL**, multi-PAT rotation (`ghs.github.tokens`), `minimum-stars=10`, `start-date=2008-01-01`, `delay PT6H`, Docker Compose + ≤15-day dumps. It solves the 1000-cap by never re-searching broadly — crawl once, query locally. Weaknesses: ≥10★ floor, Java/Python code focus, MySQL ops. **Template for gitcrawl: crawl → store → serve.** MIT, self-hostable, active v1.17.1 (2024–2025), paper arxiv 2409.18658.

## 4. Third-party discovery

### 4.1 Sourcegraph — scalable code→repo

A Zoekt index, streaming SSE API, `src-cli`, MCP (`list_repos` to 10k, `keyword_search`). Syntax `repo:`, `lang:`, `select:repo`, `count:all`, `fork:yes`, `archived:only`, `repo:has.topic()`. Public small queries need no auth; a token unlocks `count:all`; self-hosted is bounded by your corpus. The only scalable regex/symbol code search; `select:repo` turns hits into discovery. Corpus ≠ all GitHub (lag); metadata weaker than stars/pushed. Best for code-content + code-signal discovery. Freemium, `zoekt` OSS.

### 4.2 grep.app — the fastest zero-setup grep

Regex over ~1M+ repos, free, throttled on heavy use. No star/date filters, smaller index, no SLA. Best for seed lists by import/string. Free (Vercel).

### 4.3 Libraries.io / Ecosyste.ms / deps.dev — enrichment

- **Libraries.io API:** package→repo, deps/dependents, needs a key. Freemium (Tidelift).
- **Ecosyste.ms:** **343M repos (336M GitHub) / 406M manifests / 25B deps as of 2026** (was 287M/1952 hosts), OpenAPI 3.0.1, CC-BY-SA-4.0, mostly public. The closest thing to bulk metadata without GitHub tokens. Polite-pool: 5000 req/hr/IP; join via `?mailto=you@x` / `User-Agent: mailto:` for prioritized latency (https://blog.ecosyste.ms/2025/09/01/rate-limiting-the-right-way.html); `POST /packages/bulk_lookup` for batch; a zero-token local path via `npx @ecosyste-ms/mcp` (bundled SQLite). Timeline 7B events / Commits 889M indexes.
- **deps.dev v3:** package versions + OSV advisories + Scorecard; project stars/forks only for package-associated projects. Free (Google). Batch-first: `GetVersionBatch`/`GetProjectBatch` (1 req for N identifiers) + hash→versions lookup; 2025 coverage jumps (RubyGems, Gradle Plugins, Sigstore). **Note: the `criticality_score` bulk feed (GCS+BQ) is DEAD since 2026-08-29 (infra down since 2026-05) — use the Scorecard weekly feed instead** (https://github.com/ossf/criticality_score/blob/main/README.md).
- Package-centric — it can't discover non-packaged repos. Best for dependency/metadata enrichment.

## 5. Commercial / scraping — ToS notes

> ToS: **Scraping = automated extraction via bot/crawler, NOT via API** (§C.5). API = §H (no abuse, suspension at discretion). Both forbid spam/selling personal info and require Privacy compliance. Researchers: public non-personal only, with open-access outputs. Prefer API+tokens; HTML scraping only for public non-personal metadata, never PII.

| Vendor | Rate/cost (Sep 2026) | Strengths | Weaknesses |
|---|---|---|---|
| SerpAPI | $50–500/mo | Bypasses 30/min via Google index | Stale, no qualifier fidelity |
| Bright Data | 5k free; $1.5/1k; $499/mo 384k (2026-09-15) | No proxy ops, webhook/S3 | 1M repos ≈ $1300–1500; still 1000-cap/query |
| Apify `bovi` / `dami_studio` | $2/1k repos $3/1k profiles / **$0.90/1k flat** + compute | Cheapest managed, full syntax, 429 handling | Still 1000/query — split yourself |

Verdict: useful for bursty one-offs, strictly pricier than PAT rotation for a continuous crawl, and no cap escape.

## 6. Comparison table

| Tool | API | Rate (auth) | 1000-cap | History | Custom filter | Cost | Self-host |
|---|---|---|---|---|---|---|---|
| Web UI | internal | abuse-based | manual | no | qualifiers, no export | free | no |
| `gh search repos` | REST | 30/min | hard stop | no | qualifiers+sort | free/OSS | CLI yes |
| REST search | REST | 30/min | 422, split by `created:/stars:` | no | richest | free/token | client |
| GraphQL search | GraphQL | 5000 pts/hr | hard stop | no | +field select | free/token | client |
| Legacy code | REST code | 10/min | 100/pg | no | weak | free | client |
| Blackbird web | internal | login | 100 web-only | no | regex/symbol | free | no |
| SDKs | REST+GQL | inherits | inherits | no | via query | free/OSS | yes |
| GHTorrent | mirror | offline | frozen | to 2019, dead | SQL | free/dead | dump yes |
| GH Archive+BQ | events poll | 5000/hr core | stream, no cap | 2011–now, Oct-25 cliff | SQL events | public+BQ cost | crawler OSS |
| GrimoireLab | REST+git | multi-token | per-repo | incremental | SQL/ES | free/GPL | heavy yes |
| SEART GHS | GraphQL→MySQL | multi-PAT | **solved via DB (≥10★)** | from crawl start | arbitrary SQL/API | free/MIT | **Compose yes** |
| Sourcegraph | Zoekt | token/limit | `count:all` | lag | code-first | freemium | enterprise |
| grep.app | own index | fair-use | n/a | lag | code only | free | no |
| ecosyste.ms | aggregation | open | n/a | package hist | package→repo | free/open | partial |
| SerpAPI/Bright/Apify | scrape/REST-wrap | vendor quota | still 1000 if REST | no | vendor fields | $0.90–3/1k | no |

## 7. Recommendations

**(a) One-off:** prototype in the Web UI → `gh search repos --limit 100 --json` / an Octokit snippet. For code signals: grep.app → Sourcegraph `select:repo`. Burst only: Apify `dami_studio` at $0.90/1k.

**(b) Continuous crawl like gitcrawl — copy SEART GHS:** 1) **Discover:** sharded REST (`created:` bisect until `<1000` — **replace** the date clause per child, never append: duplicate same-type qualifiers union, not AND (repo-scoped probe, live-verified 2026-10-06/07); `sum==parent` as sanity only; 100/pg; `X-RateLimit` headers, cap-422→split, 403/429→backoff, multi-PAT, 6h cadence; expect small corpus drift between runs — pagination has no stability guarantee, observed 49 added / 11 removed / net 38 over ~12 h; live-verified 2026-10-06/07). 2) **Store:** Postgres/MySQL (PK owner/name, stars, pushedAt, license, topics) + Compose + dumps. 3) **Enrich:** GraphQL batched, GH Archive trends (accept the field loss), ecosyste.ms/deps.dev linkage. Avoid GHTorrent (dead), pure Apify/Bright at scale (cost), and HTML PII scraping (ToS).

**(c) Code-content:** the official API can't do it at scale → Sourcegraph streaming/MCP + `select:repo` to produce a repo list; grep.app for seeds; Boa only for frozen corpora.

**(d) Enrichment:** ecosyste.ms Repos/Packages first (343M repos as of 2026, open, no burn), deps.dev v3 Scorecard/OSV, Libraries.io dependents, GrimoireLab for longitudinal issues/PRs.

## 8. Sources (all accessed 2026-09-29 unless noted — full clickable URLs)

- REST Search (`/search/repositories`, 30/min auth, 10/min unauth, 1000 results, 4000 scanned): https://docs.github.com/en/rest/search/search (accessed 2026-09-29)
- Rate limits for REST (primary + secondary): https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api (accessed 2026-09-29); Rate Limit endpoint (`resources.search/code_search/core`): https://docs.github.com/en/rest/rate-limit/rate-limit (accessed 2026-09-29)
- GraphQL rate/query limits (5000 pts/hr, 2000/min secondary): https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api (accessed 2026-09-29)
- GraphQL resource limits changelog 2025-09-01: https://github.blog/changelog/2025-09-01-graphql-api-resource-limits/ (accessed 2026-09-29)
- REST vs GraphQL guidance 2025-04-22: https://github.blog/developer-skills/github/exploring-github-cli-how-to-interact-with-githubs-graphql-api-endpoint/ (accessed 2026-09-29)
- GraphQL `search(type:REPOSITORY)` max 1000: https://docs.github.com/en/graphql/reference/queries#search (accessed 2026-09-29)
- `gh search repos` manual: https://cli.github.com/manual/gh_search_repos (accessed 2026-09-29); source `repos.go` (`--limit 1..1000`, `SearchMaxResults`): https://github.com/cli/cli/blob/trunk/pkg/cmd/search/repos/repos.go (accessed 2026-09-29); repo: https://github.com/cli/cli (accessed 2026-09-29)
- Code search limits (login, 100 results, Blackbird): https://docs.github.com/en/search-github/github-code-search/about-github-code-search (accessed 2026-09-29); legacy syntax: https://docs.github.com/en/search-github/searching-on-github/searching-code (accessed 2026-09-29); API change 2023-03-10 (10/min, auth, no sort): https://github.blog/changelog/2023-03-10-changes-to-the-code-search-api/ (accessed 2026-09-29); Blackbird engineering 2023-02-06: https://github.blog/engineering/architecture-optimization/the-technology-behind-githubs-new-code-search/ (accessed 2026-09-29)
- 1000-cap 422 + `created:` splitting measurement 2026-09-13: https://saas-diary.com/tech-log/github-search-api-422-first-1000-results-only/ (accessed 2026-09-29)
- Octokit.js (~7830★ as of 2026-09-29): https://github.com/octokit/octokit.js (accessed 2026-09-29); search docs + issues deprecation Sep 2025: https://github.com/octokit/octokit.js/issues/2828 (2025-03-18) and https://github.com/octokit/octokit.js/discussions/2592 (accessed 2026-09-29)
- PyGithub (~7769★, v2.7.0 2025-07-31, as of 2026-09-29): https://github.com/PyGithub/PyGithub (accessed 2026-09-29); release: https://github.com/PyGithub/PyGithub/releases/tag/v2.7.0 (accessed 2026-09-29)
- go-github (11.3k★, 2.6k forks, v92, as of 2026-09-29): https://github.com/google/go-github (accessed 2026-09-29)
- octocrab (1349★, v0.49.7 2026-03-30, push 2026-04-02, as of 2026-09-29): https://github.com/XAMPPRocky/octocrab (accessed 2026-09-29); docs: https://docs.rs/octocrab (accessed 2026-09-29)
- GHTorrent dead (r/DataHoarder 2024-05-27): https://www.reddit.com/r/DataHoarder/comments/1d1tuek/ (accessed 2026-09-29); BigQuery snapshot frozen 2019-06 (as of 2026-09-29, re-verify)
- GH Archive completeness drop #310 (2025-07-16): https://github.com/igrigorik/gharchive.org/issues/310 (accessed 2026-09-29); cache collapse #312 (2025-10-12): https://github.com/igrigorik/gharchive.org/issues/312 (accessed 2026-09-29); payload change 2025-08-08: https://github.blog/changelog/2025-08-08-upcoming-changes-to-github-events-api-payloads/ (accessed 2026-09-29); CodePulse cliff study (48→5 fields, 2026): https://codepulsehq.com/research/github-archive-payload-cliff (accessed 2026-09-29); crawler: https://github.com/igrigorik/gharchive.org (accessed 2026-09-29)
- Perceval (322★, 1.4.6 2026-03-03, as of 2026-09-29): https://github.com/chaoss/grimoirelab-perceval (accessed 2026-09-29); GrimoireLab: https://github.com/chaoss/grimoirelab (1.13.0-rc.1 2025-06-03, accessed 2026-09-29)
- SEART GHS (Spring Boot, GraphQL, ≥10★, multi-token, Compose; v1.17.1 as of 2026-09-29): https://github.com/seart-group/ghs (accessed 2026-09-29); instance: https://seart-ghs.si.usi.ch (accessed 2026-09-29); Data Hub: https://seart-dh.si.usi.ch (accessed 2026-09-29); paper (316k repos, 22M files, 1.3k/day): https://arxiv.org/pdf/2409.18658 (accessed 2026-09-29)
- Sourcegraph streaming API: https://sourcegraph.com/docs/api/stream-api.md (accessed 2026-09-29); MCP: https://sourcegraph.com/docs/api/mcp (accessed 2026-09-29); query syntax: https://sourcegraph.com/docs/code-search/queries (accessed 2026-09-29); site: https://sourcegraph.com (accessed 2026-09-29)
- grep.app: https://grep.app (accessed 2026-09-29; Vercel-owned, free UI)
- ecosyste.ms API: https://ecosyste.ms/api (accessed 2026-09-29); Repos: https://repos.ecosyste.ms (accessed 2026-09-29); Packages: https://packages.ecosyste.ms (accessed 2026-09-29); docs: https://docs.ecosyste.ms/ (accessed 2026-09-29); rate-limit polite-pool (2025-09-01): https://blog.ecosyste.ms/2025/09/01/rate-limiting-the-right-way.html (accessed 2026-09-29); MCP zero-token local: https://github.com/ecosyste-ms/mcp (accessed 2026-09-29)
- deps.dev v3 + batch API: https://docs.deps.dev/api/v3/ (accessed 2026-09-29); https://blog.deps.dev/api-v3/index.html (2024-03-11, accessed 2026-09-29)
- `criticality_score` bulk feed DEAD (GCS+BQ killed 2026-08-29; use the Scorecard weekly feed): https://github.com/ossf/criticality_score/blob/main/README.md (accessed 2026-09-29); Scorecard repo: https://github.com/ossf/scorecard (accessed 2026-09-29)
- BQ pricing 2026 ($6.25/TiB, 10MB min, LIMIT doesn't prune): https://cloud.google.com/bigquery/pricing (accessed 2026-09-29)
- DuckDB hourly-poller template (T-3h safety): https://github.com/Harishankar1988/gharchive-duckdb-pipeline (accessed 2026-09-29)
- Libraries.io API: https://libraries.io/api (accessed 2026-09-29); repo: https://github.com/librariesio/libraries.io (accessed 2026-09-29)
- deps.dev v3 docs: https://docs.deps.dev/api/v3/ (accessed 2026-09-29); site: https://deps.dev (accessed 2026-09-29)
- Bright Data GitHub Scraper (5k free, $1.5/1k, $499/384k, page modified 2026-09-15; prices as of 2026-09-29, re-verify): https://brightdata.com/products/web-scraper/github (accessed 2026-09-29)
- Apify `bovi/github-scraper` ($2/1k repos, $3/1k profiles, as of 2026-09-29): https://apify.com/bovi/github-scraper (API: https://apify.com/bovi/github-scraper/api/python; accessed 2026-09-29); Apify `dami_studio/github-scraper` ($0.90/1k, 1000/query cap, as of 2026-09-29): https://apify.com/dami_studio/github-scraper (accessed 2026-09-29)
- SerpAPI: https://serpapi.com (accessed 2026-09-29; ~$50–500/mo tiers as of 2026-09-29, re-verify)
- ToS + Acceptable Use + site-policy: https://docs.github.com/site-policy/github-terms/github-terms-of-service (accessed 2026-09-29); https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies (accessed 2026-09-29); https://github.com/github/site-policy (accessed 2026-09-29)

Note: all star counts, pricing, and version numbers above are as of 2026-09-29 — re-verify before capacity/cost decisions (see `05-research-gaps.md`). API-behavior facts marked "live-verified 2026-10-06/07" were probe-confirmed against the live API on that date.
