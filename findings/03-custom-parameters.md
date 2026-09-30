# 03 — Can Custom Parameters Be Added? (Definitive Verdict)

> Read after `01–02`. Companion: `06 §6` (per-param workarounds), `06 §7` (typo/validation).
> Background: gitcrawl wants filters GitHub never built (`has_dockerfile`, coverage, team) — teams instinctively try `&myfilter=x` or `q=myfield:value` and get silent `200`s with unfiltered results, mistaking success for filtering.
> Goal: settle it with YES/NO verdicts (top-level customs: NO; `q` customs: NO; real customs: `props.*` single-org + `topic:` + client/proxy/own-DB), then give the sanctioned workarounds with proxy/FastAPI/GraphQL/`gh`-extension code and a use-case→approach matrix.

> Date: 2026-09-29. Scope: `GET /search/repositories` REST + `search(type:REPOSITORY)` GraphQL + `gh search repos`.

## TL;DR verdicts

| Question | Verdict |
|---|---|
| Arbitrary **top-level** params e.g. `&myfilter=y`? | **NO — silently ignored (200, not 422). Do not do it.** |
| Custom **qualifiers inside `q`** e.g. `myfield:value`? | **NO — treated as plain-text keyword. No filtering.** |
| Anything officially "custom"? | **YES — only `props.*` org custom properties (single-org scoped) + `topic:` taxonomy + client-side post-filter.** |
| GraphQL `search()` custom server filters? | **NO for `query:` syntax. YES for custom field selection + client logic.** |
| `gh` CLI / proxy / own DB virtual params? | **YES — client/proxy-side only, never GitHub-side.** |

> Docs rule: "A query can contain any combination of search qualifiers **supported** on GitHub" — closed set, same as web UI.

## 1. Arbitrary top-level params (`&myfilter=x`)

Supported set only: `q` (required), `sort` (`stars|forks|help-wanted-issues|updated`), `order` (`desc|asc`, ignored without `sort`), `per_page` (max 100), `page` (first 1000 only), plus `Accept`/`Authorization`/`X-GitHub-Api-Version` headers. Limits: 4000 repos scanned, `incomplete_results:true` on timeout, 30/min auth (10/min unauth).

```bash
# custom top-level param → 200 OK, zero effect (dangerous silent success)
curl -H "Accept: application/vnd.github+json" -H "Authorization: Bearer $GH_TOKEN" -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/search/repositories?q=language%3Apython+stars%3A%3E1000&per_page=5&myfilter=x&min_coverage=80"
```

`422` is for semantic validation (q>256 chars, >5 AND/OR/NOT, inaccessible `repo:/user:/org:`, bad `sort` enum, spam heuristic) — never for unknown params. OpenAPI has no `additionalProperties` for search; server drops unknowns.

**Fail closed in your client/proxy:** allowlist `q,sort,order,per_page,page`; reject others with your own `400`, don't forward.

## 2. Custom qualifiers inside `q`

**NO.** `myfield:value` → tokenized as keywords, no filtering, inflated `total_count`, no `422`. Typos behave same: `is:archive` vs `archived:true`, `is:fork` vs `fork:true` silently become text search. Must validate qualifier names client-side.

Allowlist for repos: `in:name,description,topics,readme`, `repo:`, `user:`, `org:`, `size:`, `followers:`, `forks:`, `stars:`, `created:`, `pushed:`, `language:`, `topic:`, `topics:`, `license:`, `is:public/private`, `mirror:`, `template:`, `archived:`, `good-first-issues:`, `help-wanted-issues:`, `is:sponsorable`, `has:funding-file`, `fork:true/only`, `deployable:`, `deployed:`, `props.*` (scoped), ranges, `-qualifier`, `AND/OR/NOT` (≤5).

## 3. What IS supported that feels custom

### 3a. `props.*` — org custom properties (only true custom filter)

Org owners define schema (`text|single_select|multi_select|true/false`), set per-repo values, search `props.PROPERTY:VALUE`. **Must include single-org scope** or ignored:

```text
org:myorg props.environment:production        # works
props.environment:production stars:>100       # silently ignored
```

Manage via UI (`Org → Settings → Repository → Custom Properties`) + REST `GET/POST/PATCH /orgs/{org}/properties/schema`, `GET/PATCH /repos/{owner}/{repo}/properties/values`, `GET /orgs/{org}/properties/values?repository_query=...`. Names `[a-zA-Z0-9_-$#]`, ≤75 chars. Visibility = repo visibility. Test with/without clause (`total_count` delta).

### 3b. `topic:` — user taxonomy

```bash
gh search repos --topic=unix,terminal --language=go
gh search repos "topic:machine-learning topic:llm stars:>500"
```

Anyone with push can tag; co-opt namespaced convention (`topic:myorg-tier-gold`). No enforcement.

### 3c. Client-side post-processing

`items[]` returns `topics[]`, `language`, `license`, `size`, counts, dates, `archived`, `fork`, `visibility`, `custom_properties{}` — filter anything GitHub can't:

```python
import requests
r = requests.get("https://api.github.com/search/repositories",
  params={"q": "org:myorg stars:>10", "per_page": 100},
  headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}).json()
hits = [x for x in r["items"] if "myorg-tier-gold" in x.get("topics", []) and not x["archived"]]
```

Cost: pagination (max 1k) + extra Contents/Languages calls (burns `core` limit). Cache aggressively.

## 4. Workarounds

**A. Client filter** — simplest, <1k results, needs file/content checks. Paginate via `Link`, respect `incomplete_results`.

**B. Proxy with virtual params (recommended for teams)** — expose `&min_stars`, `&team`, `&has_dockerfile`, translate to legal GitHub calls + post-filter:

```js
// Node/Express: never forward unknown params to GitHub
import express from "express";
const app = express();
app.get("/vsearch/repos", async (req, res) => {
  const { q = "", min_stars, team_topic, has_dockerfile } = req.query;
  const qualifiers = [q];
  if (min_stars) qualifiers.push(`stars:>=${Number(min_stars)}`);
  if (team_topic) qualifiers.push(`topic:${team_topic}`);
  const gh = await fetch(`https://api.github.com/search/repositories?q=${encodeURIComponent(qualifiers.join(" "))}&per_page=100`,
    { headers: { Authorization: `Bearer ${process.env.GH_TOKEN}`, Accept: "application/vnd.github+json" } }).then(r => r.json());
  let items = gh.items ?? [];
  if (has_dockerfile === "true") {
    items = (await Promise.all(items.map(async (repo) => {
      const f = await fetch(`https://api.github.com/repos/${repo.full_name}/contents/Dockerfile`,
        { headers: { Authorization: `Bearer ${process.env.GH_TOKEN}` } });
      return f.ok ? repo : null;
    }))).filter(Boolean);
  }
  res.json({ total_count: items.length, items });
});
```

```python
# FastAPI
from fastapi import FastAPI
import httpx
app = FastAPI()
@app.get("/vsearch/repos")
async def vsearch(q: str = "", team: str | None = None, min_stars: int | None = None):
    quals = [q]
    if min_stars is not None: quals.append(f"stars:>={min_stars}")
    if team: quals.append(f"topic:{team}")
    async with httpx.AsyncClient() as c:
        r = await c.get("https://api.github.com/search/repositories",
            params={"q": " ".join(quals), "per_page": 100},
            headers={"Authorization": f"Bearer {TOKEN}"})
    return {"total_count": len(r.json()["items"]), "items": r.json()["items"]}
```

**C. Pre-index own DB** — nightly crawl (`/orgs/{org}/repos` or `search?q=org:x` + `properties/values` + Languages/Contents) into Postgres/Elastic/SQLite; expose own `/search?coverage_min=80`. Only way for cross-org custom fields or >1k/complex queries.

**D. GraphQL custom output (not input)** — same `query:` syntax/limits/1k cap, power is field selection:

```graphql
query ($q: String!, $n: Int = 50) {
  search(query: $q, type: REPOSITORY, first: $n) {
    repositoryCount
    edges { node { ... on Repository {
      nameWithOwner url stargazerCount forkCount
      primaryLanguage { name }
      repositoryTopics(first: 20) { nodes { topic { name } } }
      licenseInfo { spdxId } isArchived isFork updatedAt
    } } }
  }
}
# vars: { "q": "org:myorg props.environment:production stars:>10" }
```

No `language:[go,java]` array — use aliases or separate queries. `semantic`/`hybrid` are issues-only.

**E. `gh` extensions** — `gh search repos` flags just build `q`; write `gh-mysearch` extension wrapping `gh api search/repositories` + post-filter for virtual params:

```bash
gh search repos --owner=microsoft --visibility=public --language=go --topic=unix,terminal
gh search repos -- "org:myorg" "props.environment:production" --sort=stars
gh search repos -- "starter -archived:true -language:php -topic:deprecated"
gh api /repos/OWNER/REPO/properties/values
```

## 5. Recommendation matrix

| Use-case | Approach | Example |
|---|---|---|
| One org you control | Native `props.*` | `org:acme props.data-class:gold` |
| Cross-GitHub, no admin | `topic:` + client filter | `topic:acme-gold stars:>50` + `archived==false` check |
| File/CI/coverage filter | Proxy / pre-index + Contents API | `GET /vsearch/repos?has_dockerfile=true` |
| 10k+ repos, complex logic | Own DB / Elastic | Nightly ETL, query Postgres locally |
| Rich context one call | GraphQL `search` | Query above, post-filter in code |
| Team CLI | `gh` extension over proxy | `gh mysearch --team platform --min-stars 100` |
| "Just add `&myfilter`" | Don't | Silently ignored; make it a proxy virtual param |

Shortcut: **one org + admin → `props.*`; no admin/cross-org → `topics` + client filter; non-GitHub data or >1k → own index.**

## 6. Risks

- Undocumented params: ignored today, `400/422`/redefined tomorrow; `X-GitHub-Api-Version` only pins documented behavior.
- Undocumented `q` qualifiers: parsed as text → wrong `200` results, no alert; validate client-side (CLI deliberately avoids hard-coding list for forward-compat).
- Rate/abuse: 30/min auth, 10/min anon/code; `403/429` + `retry-after` + exp backoff; `422 "spammed"` ≠ throttle, don't retry as throttle.
- ToS Section H: no token-sharing to evade limits, no spam/selling personal data, resale/high-throughput may need subscription.
- Scraping vs API: HTML scraping to dodge limits violates "excessive automated bulk activity" + less reliable; prefer API + caching + `ETag`/`304`.
- Privacy: public repo props visible to anyone; never put secrets/PII in props/topics/descriptions.

## Sources (all accessed 2026-09-29 unless noted)

- REST Search + query construction ("any combination of search qualifiers supported"): https://docs.github.com/en/rest/search/search?apiVersion=2022-11-28 (accessed 2026-09-29)
- Repo qualifiers + `props.*` single-org-ignore rule: https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories (accessed 2026-09-29)
- Custom properties setup (`prop` in org Repos search, allowed chars, types): https://docs.github.com/en/organizations/managing-organization-settings/managing-custom-properties-for-repositories-in-your-organization (accessed 2026-09-29)
- Org custom-properties REST + `repository_query` (`GET /orgs/{org}/properties/values`): https://docs.github.com/en/rest/orgs/custom-properties?apiVersion=2026-03-10 (accessed 2026-09-29)
- GraphQL `search(query:,type:)`: https://docs.github.com/en/graphql/reference/queries#search (accessed 2026-09-29)
- Troubleshooting REST (`422 Invalid request` / `Validation Failed` codes): https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api (accessed 2026-09-29)
- Rate limits (search `30/min`, `403/429`, `Link` pagination `per_page≤100`): https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api (accessed 2026-09-29) and https://docs.github.com/en/rest/rate-limit/rate-limit (accessed 2026-09-29)
- Search syntax (range, `-qualifier`, `@me`, `AND/OR/NOT≤5`) + sort (`stars/forks/updated`): https://docs.github.com/en/search-github/getting-started-with-searching-on-github/understanding-the-search-syntax (accessed 2026-09-29) and https://docs.github.com/en/search-github/getting-started-with-searching-on-github/sorting-search-results (accessed 2026-09-29)
- `gh search repos` flags (no `props` flag, raw qualifier pattern): https://cli.github.com/manual/gh_search_repos (accessed 2026-09-29) and https://github.com/cli/cli/blob/trunk/pkg/cmd/search/repos/repos.go (accessed 2026-09-29)
- `gh` custom-properties request + search qualifier note: https://github.com/cli/cli/issues/9254 (2024-06-25, accessed 2026-09-29); qualifier-ignored discussion: https://github.com/cli/cli/issues/8984 (2024-04-20, accessed 2026-09-29); Terraform `props.*` unfiltered without scope: https://github.com/integrations/terraform-provider-github/issues/2161 (2024-02-19, accessed 2026-09-29)
- Issues nested `AND/OR` rebuild (why only issues got `advanced_search`/`semantic`/`hybrid`, repos didn't): https://github.blog/changelog/2025-05-13- (Blog 2025-05-13, accessed 2026-09-29; see also https://github.blog/changelog/2025-03-06-github-issues-projects-api-support-for-issues-advanced-search-and-more and https://github.blog/changelog/2026-04-02-improved-search-for-github-issues-is-now-generally-available/)
- GraphQL repo filtering client-side pattern: https://stackoverflow.com/questions/ (see "Github GraphQL Search with Filtering" 2018, accessed 2026-09-29 — still accurate for `query:` model)
- ToS Section H API Terms + scraping distinction: https://docs.github.com/site-policy/github-terms/github-terms-of-service (accessed 2026-09-29) and https://github.com/github/site-policy (scraping policy, acceptable-use "excessive automated bulk activity"; accessed 2026-09-29)
