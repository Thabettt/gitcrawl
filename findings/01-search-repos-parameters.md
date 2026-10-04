# 01 — GitHub Search Repositories API: Supported Parameters (Canonical Reference)

**Read this after `00-overview.md`.** Companions: `02` (how to use these well), `06` (every attribute, searchable or not).

**The one-paragraph version**: this is the file you trust when you need to know exactly what `GET /search/repositories` accepts. The API is small on purpose — five top-level parameters, one query string that carries every filter, and a closed list of qualifiers. The dangerous part is how it handles mistakes: a typo'd qualifier is not an error, it quietly becomes a search word, and you get a `200 OK` full of wrong results. Everything downstream (sharding, validation, workarounds) stands on the tables below, so each claim carries its documentation link.

> **Research date**: 2026-09-29 (all sources accessed 2026-09-29 unless noted)
> **Endpoint**: `GET /search/repositories`
> **Sources:**
> - REST Search reference + query construction + 1000-cap + 4000-scan + timeouts + 422 rules + text-match: https://docs.github.com/en/rest/search/search?apiVersion=2022-11-28 (accessed 2026-09-29)
> - Repository qualifiers incl. `props.*` single-org rule: https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories (accessed 2026-09-29)
> - Search syntax (ranges, dates, exclusion, NOT, quotes, @me, AND/OR/NOT ≤5): https://docs.github.com/en/search-github/getting-started-with-searching-on-github/understanding-the-search-syntax (accessed 2026-09-29)
> - Sorting search results: https://docs.github.com/en/search-github/getting-started-with-searching-on-github/sorting-search-results (accessed 2026-09-29)
> - Searching in forks (forks excluded by default; `fork:true`/`fork:only`): https://docs.github.com/en/search-github/searching-on-github/searching-in-forks (accessed 2026-09-29)
> - Troubleshooting search queries (256-char + 5-operator caps): https://docs.github.com/en/search-github/getting-started-with-searching-on-github/troubleshooting-search-queries (accessed 2026-09-29)
> - Rate limits + `GET /rate_limit` (`search`/`core`/`code_search` resources): https://docs.github.com/en/rest/rate-limit/rate-limit?apiVersion=2026-03-10 (accessed 2026-09-29) and https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api (accessed 2026-09-29)
> - Pagination (`per_page`/`page`, `Link: rel=next/last`, clamping): https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api?apiVersion=2026-03-10 (accessed 2026-09-29)
> - Best practices (conditional `ETag`/`If-None-Match` → `304` free if authed; `x-poll-interval`): https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api (accessed 2026-09-29)
> - API versions (`2022-11-28` default; `2026-03-10` current) + breaking changes: https://docs.github.com/en/rest/about-the-rest-api/api-versions (accessed 2026-09-29) and https://docs.github.com/en/rest/about-the-rest-api/breaking-changes (accessed 2026-09-29)
> - Auth (PAT/OAuth/GitHub App/`GITHUB_TOKEN` buckets; 401→403 escalation): https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api?apiVersion=2026-03-10 (accessed 2026-09-29)
> - Troubleshooting REST (`422 Invalid request` / `Validation Failed` codes): https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api (accessed 2026-09-29)

## 1. Top-level query parameters — five, and only five

Every knob GitHub gives you for repository search is in this table. If you want anything else (a custom filter, an extra option), it has to be handled by *you*, after the response — see `03`.

| Param | Required | Type | Allowed / Default |
|-------|----------|------|-------------------|
| `q` | **yes** | string | keywords + qualifiers, e.g. `tetris language:assembly`, `GitHub Octocat in:readme user:defunkt`. Must URL-encode. |
| `sort` | no | string | `stars`, `forks`, `help-wanted-issues`, `updated`. Anything else → `422`. Default: `best match` (omit param). |
| `order` | no | string | `desc`, `asc`. Ignored unless `sort` given. Default: `desc`. |
| `per_page` | no | integer | `1–100`. Default: `30`. |
| `page` | no | integer | `>=1`. Default: `1`. Only pages 1–10 usable at `per_page=100` (1000-result cap). |

No `advanced_search` / `search_type` — those are `/search/issues`-only.

## 2. `q` qualifiers (full list)

This is the closed set. Anything not listed here — a made-up qualifier, a typo like `updated:` — is not rejected; it becomes a plain-text search term and silently changes nothing about the filtering. That single behavior is why validation matters more here than in a typical API.

Format: `SEARCH_KEYWORD_1 SEARCH_KEYWORD_N QUALIFIER_1 QUALIFIER_N`. Space/`+` = implicit `AND`.

- **Free text + `in:` scope:** bare keyword (default scope = name+description+topics, NOT README); `in:name`, `in:description`, `in:topics`, `in:readme`, `in:name,description`; `repo:owner/name`
- **Owner:** `user:USERNAME` (supports `user:@me`), `org:ORGNAME`
- **Numeric** (all support `> >= < <= n..n n..* *..n`): `stars:`, `forks:`, `size:` (KB), `followers:`, `topics:` (count)
- **Dates** (`YYYY-MM-DD`, optional `THH:MM:SS+00:00`/`Z`): `created:`, `pushed:` (last push any branch). Note: no documented `updated:` qualifier for repos — use `pushed:` + `sort=updated`.
- **Meta:** `language:` (e.g. `python`, `typescript`, `c++`, `jupyter-notebook`), `topic:TOPIC` (exact), `license:` (`mit`, `apache-2.0`, `gpl-3.0`, `bsd-3-clause`, `agpl-3.0`, `lgpl-3.0`, `mpl-2.0`, `cc0-1.0`, `unlicense`, `other`, `NOASSERTION`)
- **Boolean flags:** `fork:true` / `fork:only` (default excludes forks), `archived:true/false`, `mirror:true/false`, `template:true/false`, `is:public` / `is:private`, `is:sponsorable`, `has:funding-file`, `good-first-issues:>n`, `help-wanted-issues:>n`, `props.PROPERTY:VALUE` (org custom properties, **requires single-`org:` scope or silently ignored**), `deployable:true` / `deployed:true`
- **Operators:** `-qualifier` exclusion (`-language:javascript`), `NOT` for strings only (`hello NOT world`), quotes for phrases (`"machine learning"`), `AND`/`OR`/`NOT` explicit (max 5 combined), case-insensitive.

Examples:

```text
q=tetris language:assembly stars:>100 pushed:>2023-01-01 archived:false
q="machine learning" topic:machine-learning language:python stars:100..5000 license:mit
q=user:defunkt forks:>100
q=org:github is:public mirror:false template:false
```

## 3. Headers

Three headers cover almost everything. `Accept` picks the response flavor; `Authorization` unlocks higher limits (and is required for private data); the version header pins behavior so a future API change can't surprise you.

```http
Accept: application/vnd.github+json
Authorization: Bearer <TOKEN>
X-GitHub-Api-Version: 2022-11-28
```

- `Accept: application/vnd.github+json` recommended; `application/vnd.github.text-match+json` adds `text_matches[] {object_url, object_type, property, fragment, matches[{text, indices}]}` (repos: `name`/`description` only).
- `Authorization`: optional for public, required for private + higher limits.
- `X-GitHub-Api-Version`: `2022-11-28` (supported to 2028-03-10) or `2026-03-10` (current).

## 4. Limits & errors (with sources, all accessed 2026-09-29)

Three ceilings define how you must crawl, and each failure mode has a different correct response. Read this section as the "physics" of the API.

- Max 100/page, max 1000 total per logical query; max 4000 repos scanned per query ("The REST API will find up to 4,000 repositories that match your filters" — https://docs.github.com/en/rest/search/search?apiVersion=2022-11-28); `incomplete_results:true` on timeout ("Reaching a timeout does not necessarily mean results are incomplete" — same page).
- `q` >256 chars (excl. operators/qualifiers) or >5 `AND`/`OR`/`NOT` → `422 Validation failed` (same page + https://docs.github.com/en/search-github/getting-started-with-searching-on-github/troubleshooting-search-queries).
- Search rate limit: **30 req/min authenticated, 10 req/min unauthenticated** (code search separate: 10/min auth-required) — https://docs.github.com/en/rest/search/search?apiVersion=2022-11-28. Check `x-ratelimit-*` + `GET /rate_limit` — https://docs.github.com/en/rest/rate-limit/rate-limit?apiVersion=2026-03-10.
- Auth/access: `repo:`/`user:`/`org:` on inaccessible resources → `422` or silent filtering to accessible subset (same search page, "Access errors or missing search results" section).
- Success `200`: `{ total_count, incomplete_results, items[] }`. Also `304` (conditional, https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api), `403/429` (throttle, https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api), `503`.

## 5. Example

One complete, correct call — note the URL encoding and the explicit version header.

```bash
curl -L \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer <YOUR-TOKEN>" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/search/repositories?q=tetris%20language%3Aassembly&sort=stars&order=desc&per_page=30&page=1"
```

```javascript
const qs = 'q=' + encodeURIComponent('tetris language:assembly') + '&sort=stars&order=desc&per_page=30&page=1';
fetch(`https://api.github.com/search/repositories?${qs}`, {
  headers: {
    Accept: 'application/vnd.github+json',
    Authorization: `Bearer ${token}`,
    'X-GitHub-Api-Version': '2022-11-28'
  }
});
```

## 6. Last-30-days check (2026-08-29 → 2026-09-29, all accessed 2026-09-29)

A snapshot of what changed (and didn't) around this research date — useful because "the docs said so last month" is not a verification.

No change to `GET /search/repositories` in window (changelog Sep 2026 = Copilot/Actions/CodeQL/Projects — https://github.blog/changelog/).

Relevant 12-mo context:
1. `/search/code` `sort`/`order` deprecated, Sunset 2026-09-27 — treat as removed (docs note "This field is closing down" — https://docs.github.com/en/rest/search/search?apiVersion=2022-11-28; discussion https://github.com/orgs/community/discussions/192167 reported 2026-04-11).
2. `/search/issues` gained `advanced_search=true` (2025-03-06 — https://github.blog/changelog/2025-03-06-github-issues-projects-api-support-for-issues-advanced-search-and-more) and `search_type=semantic|hybrid` GA 2026-04-02 (https://github.blog/changelog/2026-04-02-improved-search-for-github-issues-is-now-generally-available/) (auth, 10/min separate bucket).
3. No new repo qualifiers, no repo-search auth change (verified against https://docs.github.com/en/rest/search/search?apiVersion=2022-11-28 on 2026-09-29).
4. REST `2026-03-10` breaking changes: no search op removals, only nested field removals affecting payloads — https://docs.github.com/en/rest/about-the-rest-api/breaking-changes (accessed 2026-09-29).
5. 2026-06-30 stargazers/subscribers privacy restriction (https://github.blog/changelog/2026-06-30-upcoming-access-restrictions-to-public-api-endpoints-and-ui-views/) + 2026-09-04 privacy-safe star-history endpoint (https://github.blog/changelog/2026-09-04-new-api-endpoint-provides-privacy-safe-star-history-data; docs https://docs.github.com/en/rest/activity/starring?apiVersion=2026-03-10) (affects star-based discovery enrichment; as of 2026-09-29, re-verify).

See `02-advanced-parameter-usage.md`, `03-custom-parameters.md`, `04-existing-solutions.md` for deep dives.
