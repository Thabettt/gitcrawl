# 02 — Advanced / Optimal Use of Search Repositories Parameters

**Read this after `01`.** Feeds `06 §8` (fetch plan) and `05 §A6` (watermark/ETag/pagination corrections).

**The one-paragraph version**: knowing the parameters is not enough. At scale you meet three walls — the 1,000-result cap, the ~4,000-repo scan scope, and `incomplete_results` timeouts — plus two separate rate meters (`search` at 30/min and `core` at 5,000/hr). Naive parallel bursts get throttled; naive `page=11` gets a `422`. This file is the power-user playbook: how to build and encode queries, which qualifiers combine well, how to sort and page, what the headers do, how to handle every failure status correctly, and how to poll for changes. It ends with full curl/JS/Python code you can run and 17 pitfalls that silently corrupt crawls.

> **Research date**: 2026-09-29. Docs live-fetched 2026-09-29; prefer 2025–2026 API versions where noted.
> Companion to `01-search-repos-parameters.md` (canonical ref). This file is the power-user guide.

## 0. Endpoint TL;DR

```
GET https://api.github.com/search/repositories?q={query}{&sort,order,per_page,page}
```

| Parameter | Required | Default | Allowed values |
|---|---|---|---|
| `q` | **yes** | — | keywords + qualifiers, e.g. `tetris+language:assembly&sort=stars&order=desc` |
| `sort` | no | `best match` | `stars`, `forks`, `help-wanted-issues`, `updated` only |
| `order` | no | `desc` | `desc`, `asc`. **Ignored unless `sort` is provided** |
| `per_page` | no | `30` | `1..100`. `>100` silently clamped |
| `page` | no | `1` | `1..10` usable (1000-cap) |

Platform limits to internalize first:

* **1,000 fetchable results per logical query** (`10×100`). `total_count` may be far larger.
* **4,000-repo scan scope**: the API scans up to 4,000 repos matching your filters.
* **Timeout → `incomplete_results: true`**, with partial results returned.
* **256-char query limit** (excluding operators/qualifiers) + **max 5 `AND`/`OR`/`NOT`**.
* **Search rate limit: 30 req/min authenticated, 10 req/min unauthenticated** (code search separate: 10/min authenticated).

**Plain effect**: any query whose result set is larger than 1,000 must be split (sharded) before you can fetch it all. That single fact shapes every strategy below.

## 1. Query construction

### 1.1 Format

```text
SEARCH_KEYWORD_1 SEARCH_KEYWORD_N QUALIFIER_1 QUALIFIER_N
# e.g. GitHub Octocat in:readme user:defunkt
```

### 1.2 Encoding — always `encodeURIComponent`

The single most common silent failure: a raw `:` or `>` in the URL changes how the query is parsed. Encode the whole `q` value, every time.

```javascript
const queryString = 'q=' + encodeURIComponent('GitHub Octocat in:readme user:defunkt');
```

| Language | Correct | Wrong |
|---|---|---|
| curl | `--data-urlencode "q=..."` or pre-encoded `%3A`, `%3E`, `+` | raw `:` `>` spaces |
| JS | `encodeURIComponent(q)` | `encodeURI(q)` (leaves `:`/`?`/`&`) |
| Python | `urllib.parse.quote(q, safe='')` / `params={...}` in `requests` | manual `+` join |

`:` → `%3A`, `>` → `%3E`, space → `+` or `%20`, `*` in `*..n` encode as `%2A`.

### 1.3 Implicit AND, explicit AND/OR/NOT

| Operator | Notes |
|---|---|
| Implicit AND via `space` | `tetris language:assembly` |
| `AND` / `OR` / `NOT` uppercase | `NOT` = strings only, not numerals/dates |
| Limit **max 5 combined** | 6th → `422` |
| Length **>256 chars excl. operators/qualifiers → `422`** | Keep keywords short, move selectivity to qualifiers |

### 1.4 Quotes, exclusion, case

- Quotes: `cats NOT "hello world"`, `label:"bug fix"`
- Exclude qualifier: `-language:javascript`, `mentions:defunkt -org:github`
- Case-insensitive; `user:@me` only with qualifier, not bare term.

### 1.5 Ranges and comparisons

`>n` / `>=n` / `<n` / `<=n`, `n..n`, `n..*` (=`>=n`), `*..n` (=`<=n`). Applies to `stars`, `forks`, `size` (KB), `followers`, `topics`, `created`, `pushed`.
E.g. `stars:10..50`, `size:50..120`, `topics:>3`, `followers:>=10000`.

### 1.6 Dates — ISO8601

`YYYY-MM-DD` + optional `THH:MM:SS+00:00` / `Z`. `pushed` = last commit any branch; `created` = repo creation.

```text
pushed:2016-04-30..2016-07-04
created:2017-01-01T01:00:00+07:00..2017-03-01T15:30:15+07:00
```

## 2. Qualifier power combos

Default scope without `in:` = name+description+topics. README **not** searched unless `in:readme`.

| Category | Qualifier | Example |
|---|---|---|
| Scope | `in:name,description,readme,topics`, `repo:owner/name` | `jquery in:name`, `repo:octocat/hello-world` |
| Owner | `user:`, `org:` | `user:defunkt forks:>100`, `org:github` |
| Counts | `stars:`, `forks:`, `size:` (KB), `followers:`, `topics:` | `stars:>=500`, `size:>=30000` |
| Dates | `created:`, `pushed:` | `webos created:<2011-01-01`, `css pushed:>2013-02-01` |
| Meta | `language:`, `topic:`, `license:` | `license:apache-2.0`, `topic:jekyll` |
| Visibility/custom | `is:public/private`, `is:sponsorable`, `has:funding-file`, `fork:true/only`, `archived:`, `mirror:`, `template:`, `good-first-issues:>n`, `help-wanted-issues:>n`, `props.NAME:VALUE` (single-org only) | `archived:false GNOME`, `org:github props.environment:production` |

High-value crawl seeds:

```text
org:github language:python stars:>500 pushed:>2024-01-01 archived:false
topic:machine-learning language:python license:mit stars:100..5000 pushed:2024-01-01..2025-01-01
language:typescript stars:>1000 created:2020-01-01..2020-06-30
```

`props.*` rule: must pair with a single `org:` or it is silently ignored.

## 3. sort / order / per_page / page

| `sort` | Use when |
|---|---|
| *(omitted)* best match | keyword discovery |
| `stars` | popularity leaderboard |
| `forks` | influence / derivation |
| `help-wanted-issues` | contributor opportunities |
| `updated` | incremental crawl / freshness |

`order` ignored without `sort`. Always `per_page=100` for crawls; pages `1..10` = 1000 max. `page=11+` → `[]`/empty by design. Follow `Link: rel="next"/"last"`. Octokit `paginate()` must strip `total_count`/`incomplete_results` before concat.

`incomplete_results:true` → log, retry once after backoff, then narrow the query (add `org:`/`language:`/date shard). The 4000-scan scope means a broad `q=python` ranks poorly; narrow queries rank better.

## 4. Headers

- `Accept: application/vnd.github+json` (default) vs `application/vnd.github.text-match+json` (adds `text_matches[]`; repos cover `name`+`description` only; indices relative to `fragment`).
- `X-GitHub-Api-Version: 2022-11-28` (EOS 2028-03-10, default if omitted) vs `2026-03-10` (current; removes `rate` → use `resources.core`).
- `Authorization: Bearer <TOKEN>`: unauth 10/min + 60 core/hr; PAT 30/min + 5000/hr; GitHub App install scales 5k→12.5k/hr; `GITHUB_TOKEN` 1k/hr/repo. Bad creds → `401` then temporary `403`.
- Conditional: `ETag`/`If-None-Match` → `304` (free if authed); `If-Modified-Since`. Keep shard URLs stable for 304 hits (search shifts often, hit rate lower but worth it).
- Rate/pagination headers: `x-ratelimit-{limit,remaining,used,reset,resource}`, `retry-after`, `link`, `etag`, `x-poll-interval`. `GET /rate_limit` → `resources.search/code_search/core`.

## 5. Rate-limit & error playbook

Every status has exactly one correct response. Treat this table as law — the wrong response (blind retrying a `422`, for example) is how crawlers get suspended.

| Status | Action |
|---|---|
| `200` + `incomplete_results:true` | narrow + retry |
| `304` | cache hit, no cost |
| `401` | fix token |
| `403` remaining=0 | sleep until `x-ratelimit-reset` |
| `403`/`429` + secondary | honor `retry-after`, else exp backoff 1m/2m/4m, max 5 attempts |
| `422` | fix query/access, don't blind-retry |
| `503` | exp backoff + jitter |

Sequential + pacing is correct: ~1 req/2s/token (30/min), max ~10 concurrent (100 hard ceiling but search degrades earlier), `per_page=100`, pause **all** workers on backoff, cache ETags.

## 6. Performance tips

1. **Narrow first:** `repo:/org:/user:` > `language:+stars:/forks:` > `created:/pushed:` shard > `topic:/license:` > bare keyword.
2. **Date-shard to bypass the 1000-cap:** if `total_count>1000`, bisect on `created:`/`pushed:` (or `stars:` ranges) until each shard is `<1000`; dedupe by `id`/`full_name`; verify `sum(shard counts)==total`.
3. **`sort=updated` polling:** `q=org:my-org pushed:>WATERMARK&sort=updated&order=desc&per_page=100`, watermark on `pushed_at`, respect `x-poll-interval`, send `If-None-Match`.
4. **Avoid >256 chars / >5 operators:** fan out into multiple queries.

## 7. Code examples

### 7.1 Basic — curl / JS / Python

```bash
curl -s \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  --get "https://api.github.com/search/repositories" \
  --data-urlencode "q=tetris language:assembly" \
  --data-urlencode "sort=stars" \
  --data-urlencode "order=desc" \
  --data-urlencode "per_page=100" | head -c 2000
```

```javascript
const q = encodeURIComponent('tetris language:assembly');
const res = await fetch(
  `https://api.github.com/search/repositories?q=${q}&sort=stars&order=desc&per_page=100`,
  { headers: { Accept: 'application/vnd.github+json', Authorization: `Bearer ${process.env.GITHUB_TOKEN}`, 'X-GitHub-Api-Version': '2022-11-28' } }
);
if (res.status === 403 && res.headers.get('x-ratelimit-remaining') === '0') {
  const reset = Number(res.headers.get('x-ratelimit-reset')) * 1000;
  await new Promise(r => setTimeout(r, Math.max(0, reset - Date.now() + 5000)));
}
const data = await res.json();
```

```python
import os, requests
headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "X-GitHub-Api-Version": "2022-11-28"}
params = {"q": "tetris language:assembly", "sort": "stars", "order": "desc", "per_page": 100}
r = requests.get("https://api.github.com/search/repositories", headers=headers, params=params, timeout=30)
data = r.json()
```

### 7.2 Qualifier-heavy

```text
org:github language:python stars:>=500 pushed:>2024-01-01 archived:false good-first-issues:>2
```

```bash
curl -s -H "Accept: application/vnd.github+json" -H "Authorization: Bearer $GITHUB_TOKEN" -H "X-GitHub-Api-Version: 2022-11-28" \
  --get "https://api.github.com/search/repositories" \
  --data-urlencode "q=org:github language:python stars:>=500 pushed:>2024-01-01 archived:false good-first-issues:>2" \
  --data-urlencode "sort=stars" --data-urlencode "order=desc" --data-urlencode "per_page=100" | jq '{total: .total_count, incomplete: .incomplete_results, n: (.items|length)}'
```

### 7.3 Paginated crawl loop (sequential, 2s pacing, backoff — full code, verified 2026-09-29)

Rules: loop `page=1..10`, stop when `items.length<100`, on `403/429/503` honor `retry-after` or `x-ratelimit-reset` else `60s*2^attempt+jitter` max 5 attempts, warn on `incomplete_results`, if `total_count>1000` shard by date (see §6, tip 2). Sources: https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api?apiVersion=2026-03-10 (accessed 2026-09-29), https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api (accessed 2026-09-29), https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api (accessed 2026-09-29).

**curl (bash loop, sequential — 2s pacing):**

```bash
#!/usr/bin/env bash
set -euo pipefail
Q="language:typescript stars:>1000 archived:false"
for p in $(seq 1 10); do
  echo "page $p" >&2
  curl -s \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    --get "https://api.github.com/search/repositories" \
    --data-urlencode "q=$Q" \
    --data-urlencode "sort=stars" \
    --data-urlencode "order=desc" \
    --data-urlencode "per_page=100" \
    --data-urlencode "page=$p" \
    -D headers.txt -o "page-$p.json"
  if grep -qi "x-ratelimit-remaining: 0" headers.txt; then
    RESET=$(grep -i "x-ratelimit-reset" headers.txt | tr -d '\r' | awk '{print $2}')
    SLEEP_FOR=$(( RESET - $(date +%s) + 5 ))
    echo "rate limited, sleeping $SLEEP_FOR s" >&2
    sleep "$SLEEP_FOR"
  else
    sleep 2
  fi
  COUNT=$(jq '.items | length' "page-$p.json")
  [ "$COUNT" -lt 100 ] && break
done
jq -s '{total: map(.total_count)[0], fetched: map(.items|length)|add}' page-*.json
```

**JavaScript (sequential, Link-aware, exponential backoff):**

```javascript
async function crawlSearch(baseQuery, { token, sort='stars', order='desc' } = {}) {
  const all = [];
  let total_count = 0;
  for (let page = 1; page <= 10; page++) {
    const url = `https://api.github.com/search/repositories?q=${encodeURIComponent(baseQuery)}&sort=${sort}&order=${order}&per_page=100&page=${page}`;
    let attempt = 0;
    for (;;) {
      const res = await fetch(url, { headers: {
        Accept: 'application/vnd.github+json',
        Authorization: `Bearer ${token}`,
        'X-GitHub-Api-Version': '2022-11-28' } });
      if (res.status === 200) {
        const body = await res.json();
        total_count = body.total_count;
        if (body.incomplete_results) console.warn(`page ${page}: incomplete_results=true — consider narrowing query`);
        all.push(...body.items);
        if (body.items.length < 100) return { total_count, items: all };
        break;
      }
      if ((res.status === 403 || res.status === 429 || res.status === 503) && attempt < 5) {
        const retryAfter = Number(res.headers.get('retry-after') || 0);
        const reset = Number(res.headers.get('x-ratelimit-reset') || 0) * 1000;
        const delay = retryAfter ? retryAfter*1000
          : (res.headers.get('x-ratelimit-remaining') === '0' && reset ? Math.max(0, reset-Date.now()+5000)
          : Math.min(8*60*1000, 60*1000 * 2**attempt) + Math.random()*1000);
        await new Promise(r => setTimeout(r, delay));
        attempt++;
        continue;
      }
      throw new Error(`search failed: ${res.status} ${await res.text()}`);
    }
    await new Promise(r => setTimeout(r, 2000)); // 30/min pacing
  }
  return { total_count, items: all }; // max 1000; if total_count>1000 → shard query
}
```

**Python (sequential + date-shard hint):**

```python
import os, time, requests

HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
    "X-GitHub-Api-Version": "2022-11-28",
}

def crawl(query, sort="stars", order="desc", max_pages=10):
    all_items, total = [], None
    for page in range(1, max_pages + 1):
        for attempt in range(6):
            r = requests.get(
                "https://api.github.com/search/repositories",
                headers=HEADERS,
                params={"q": query, "sort": sort, "order": order, "per_page": 100, "page": page},
                timeout=30,
            )
            if r.status_code == 200:
                body = r.json()
                total = body["total_count"]
                if body["incomplete_results"]:
                    print(f"WARN page {page}: incomplete_results=true")
                all_items.extend(body["items"])
                break
            if r.status_code in (403, 429, 503) and attempt < 5:
                wait = int(r.headers.get("Retry-After", 60 * (2 ** attempt)))
                if r.headers.get("x-ratelimit-remaining") == "0" and r.headers.get("x-ratelimit-reset"):
                    wait = max(wait, int(r.headers["x-ratelimit-reset"]) - int(time.time()) + 5)
                print(f"backoff {wait}s ({r.status_code})")
                time.sleep(wait)
                continue
            r.raise_for_status()
        else:
            raise RuntimeError("retries exhausted")
        if len(all_items) >= 1000 or (total is not None and len(all_items) >= min(total, 1000)):
            break
        time.sleep(2)  # search pacing: ~30/min
    if total and total > 1000:
        print(f"total_count={total} > 1000 fetchable — shard by created:/pushed: ranges")
    return {"total_count": total, "items": all_items[:1000]}
```

### 7.4 text-match

```bash
curl -s -H "Accept: application/vnd.github.text-match+json" -H "Authorization: Bearer $GITHUB_TOKEN" -H "X-GitHub-Api-Version: 2022-11-28" \
  --get "https://api.github.com/search/repositories" --data-urlencode "q=tetris language:assembly" --data-urlencode "per_page=5" | jq '.items[0] | {name, text_matches}'
```

### 7.5 Incremental poll (full code — `sort=updated` + `pushed:` + ETag; sources: best-practices conditional requests + sorting-search-results, both accessed 2026-09-29)

```text
q=org:my-org pushed:>2026-09-28T00:00:00Z&sort=updated&order=desc&per_page=100 + If-None-Match: <ETag>
→ 304 = unchanged/free (only if correctly authorized); 200 → update watermark to max(pushed_at)
```

**curl:**

```bash
WATERMARK="2026-09-28T00:00:00Z"
ETAG=$(cat .etag 2>/dev/null || echo "")
curl -s -D poll-headers.txt \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  ${ETAG:+-H "If-None-Match: $ETAG"} \
  --get "https://api.github.com/search/repositories" \
  --data-urlencode "q=org:my-org pushed:>$WATERMARK" \
  --data-urlencode "sort=updated" \
  --data-urlencode "order=desc" \
  --data-urlencode "per_page=100" -o poll.json
grep -i "^HTTP" poll-headers.txt  # HTTP/2 304 → no changes, no cost
grep -i "^etag" poll-headers.txt | tr -d '\r' > .etag
jq -r '.items[] | [.full_name, .pushed_at] | @tsv' poll.json | head
```

**JavaScript:**

```javascript
let etag = null, watermark = '2026-09-28T00:00:00Z';
async function poll() {
  const url = `https://api.github.com/search/repositories?q=${encodeURIComponent(`org:my-org pushed:>${watermark}`)}&sort=updated&order=desc&per_page=100`;
  const res = await fetch(url, { headers: {
    Accept: 'application/vnd.github+json',
    Authorization: `Bearer ${process.env.GITHUB_TOKEN}`,
    'X-GitHub-Api-Version': '2022-11-28',
    ...(etag ? { 'If-None-Match': etag } : {}) } });
  if (res.status === 304) return []; // unchanged, free
  etag = res.headers.get('etag') ?? etag;
  const body = await res.json();
  if (body.items.length) watermark = body.items.reduce((m, r) => r.pushed_at > m ? r.pushed_at : m, watermark);
  return body.items;
}
```

**Python:**

```python
etag, watermark = None, "2026-09-28T00:00:00Z"
def poll():
    global etag, watermark
    h = dict(HEADERS)
    if etag:
        h["If-None-Match"] = etag
    r = requests.get("https://api.github.com/search/repositories", headers=h,
                     params={"q": f"org:my-org pushed:>{watermark}",
                             "sort": "updated", "order": "desc", "per_page": 100}, timeout=30)
    if r.status_code == 304:
        return []  # no change, no rate cost
    r.raise_for_status()
    etag = r.headers.get("ETag", etag)
    items = r.json()["items"]
    if items:
        watermark = max([watermark] + [x["pushed_at"] for x in items])
    return items
```

Note per the `05` gap audit: search `304` hit rate is low (`total_count`/`score` churn, `sort=updated` reorders — https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api) — keep the ETag code but budget `x-ratelimit-*`, and never mix a `pushed_at` watermark with `updated_at` ordering without overlap (see `05` A6).

## 8. Common pitfalls (17)

Each of these has burned someone. None of them errors loudly — that's the point.

1. Forks excluded by default — add `fork:true`/`fork:only`; don't confuse `fork:` vs `forks:`.
2. README not in default scope — add `in:readme`.
3. No `updated:` qualifier — use a `pushed:` filter + `sort=updated` param.
4. 256-char keyword limit ≠ 1000-result cap ≠ 4000 scan scope.
5. `total_count` is an estimate, not fetchable — shard, don't `page=11`.
6. `order` ignored without `sort`.
7. `*` must be encoded (`%2A`).
8. `NOT` strings-only; use `-qualifier` for exclusion.
9. >5 operators → `422`.
10. `props.*` without single-`org:` silently ignored.
11. `per_page>100` clamped silently.
12. `text_matches` repos-only name/description.
13. `422` on `repo:/org:` = no access; multi-resource queries silently filter.
14. Parallel bursts → `403/429` + `incomplete_results` — go sequential.
15. Search case-insensitive but use canonical `language:` casing in logs.
16. Omitting the version header = implicit `2022-11-28` — send it explicitly.
17. `sort:created/comments/interactions` are issues/commits sorts, not repos.

## Sources (accessed 2026-09-29)

- REST Search: `https://docs.github.com/en/rest/search/search?apiVersion=2022-11-28`
- Searching for repositories: `https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories`
- Search syntax: `https://docs.github.com/en/search-github/getting-started-with-searching-on-github/understanding-the-search-syntax`
- Sorting: `https://docs.github.com/en/search-github/getting-started-with-searching-on-github/sorting-search-results`
- Forks: `https://docs.github.com/en/search-github/searching-on-github/searching-in-forks`
- Pagination: `https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api?apiVersion=2026-03-10`
- Rate limits: `https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api`, `https://docs.github.com/en/rest/rate-limit/rate-limit`
- Best practices (ETag/304): `https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api`
- API versions / breaking changes: `https://docs.github.com/en/rest/about-the-rest-api/api-versions`, `.../breaking-changes`
- Auth: `https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api?apiVersion=2026-03-10`
- Troubleshooting search: `https://docs.github.com/en/search-github/getting-started-with-searching-on-github/troubleshooting-search-queries`
