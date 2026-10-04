# 06 — Exhaustive Repository Parameters: Searchable, Enrichable, and Unavailable + How to Get Each

**Read this last, as the build matrix, after `01–05`.** It implements the `05` patch plan and the `04` dual-path recommendation.

**The one-paragraph version**: gitcrawl needs every repo attribute — but GitHub spreads them across five different worlds. Some are searchable inside `q` (a closed set). Some live on the REST/GraphQL repo object (nullable, sometimes gated by access). Some come from list/enumeration endpoints (no 1000-cap). Some are org custom properties (single-org only). Some need a per-repo enrichment call. Some exist only in mirrors and datasets (stale, but bulk). And roughly thirty of the things you actually want — Dockerfile, coverage, CI status, LOC, Scorecard — are available nowhere natively and need a workaround. This file is the master matrix: every parameter, its availability code, and for each unavailable one the cheapest working way to get it.

> **Date**: 2026-09-29. **Method**: 3 parallel agents (A: official REST/Search/GraphQL, B: mirrors, C: unavailable+workarounds) + live docs verification.
> **Companion**: `01` (canonical ref), `02` (power guide), `03` (custom verdicts), `04` (landscape), `05` (gaps). This file is the master matrix.

## 0. How to read this file

Availability codes — think of them as "where does this fact live?":

- **S** = `GET /search/repositories` `q` qualifier / top-level param
- **R** = `GET /repos/{o}/{r}` REST field
- **G** = GraphQL `Repository` field
- **L** = list/enumeration (`GET /repositories?since=`, `/orgs/{o}/repos`)
- **C** = custom-properties API
- **E** = per-repo enrichment REST (contents/languages/stats/etc.)
- **M** = mirror/dataset (GH Archive/BQ/WoC/SWH/Stack/ecosyste.ms/deps.dev)
- **X** = not available anywhere native → workaround in §6

The rule: if it's not in the §1–§5 tables, treat it as X and use §6. Unknown `&foo=bar` top-level params are ignored with a `200`; an unknown `q` qualifier becomes free text with a `200` (never `422`). See §7.

## 1. Top-level search params (S)

`GET https://api.github.com/search/repositories?q={query}{&sort,order,per_page,page}`. Max 100/page, max 1000/query, ~4000 scanned/query, `incomplete_results:true` on timeout. `q` >256 chars (excl. operators/qualifiers) or >5 `AND/OR/NOT` → `422`. `30/min` auth, `10/min` anon (`code_search` separate `10/min` auth-only).

| Param | Avail | Values / Default | Notes |
|---|---|---|---|
| `q` | S req | keywords + qualifiers | URL-encode. Default scope name+description+topics (NOT README unless `in:readme`). |
| `sort` | S opt | `stars\|forks\|help-wanted-issues\|updated`, default best-match | No `created`/`pushed` sort. |
| `order` | S opt | `desc` (def), `asc` | Ignored without `sort`. |
| `per_page` | S opt | 1–100, def 30 | `>100` clamped silently. |
| `page` | S opt | def 1 | Only first 1000 reachable; past cap → `422 Only first 1000`, not `[]`. Follow `Link: rel="next"` verbatim. |
| envelope | S | `total_count`, `incomplete_results`, `items[]+score`, `text_matches[]` (text-match header only, repos name+desc only) | `total_count` approximate. |

## 2. Searchable `q` qualifiers (S) — complete

Comparators `> >= < <=`, ranges `n..n`/`n..*`/`*..n`, dates `YYYY-MM-DD[THH:MM:SS+00:00]`, case-insensitive, quotes for phrases, `-qualifier` exclusion, `user:@me` only with qualifier.

| Qualifier | Example | Notes |
|---|---|---|
| bare keywords | `tetris assembly` | name+desc+topics only. |
| `in:name,description,topics,readme` | `jquery in:name`, `octocat in:readme` | Comma-combine. Only README file-content in repo search. |
| `repo:OWNER/NAME` | `repo:octocat/hello-world` | Single repo; 422 if no access. |
| `user:`, `org:` | `user:defunkt forks:>100`, `org:github` | `org:` **required scope for `props.*`** or silently ignored. |
| `size:` (KB) | `size:>=30000`, `size:50..120` | Disk size. |
| `followers:` | `followers:>=10000` | Repo followers. |
| `forks:` (count) | `forks:>=205` | Distinct from `fork:` bool. |
| `stars:` | `stars:500`, `stars:10..20` | Star count. |
| `created:` | `created:<2011-01-01` | Creation date. |
| `pushed:` | `pushed:>2013-02-01` | Last push any branch. **No `updated:` qualifier exists** — filter `pushed:` + order `sort=updated`. |
| `language:` | `language:javascript` | Primary Linguist only, one value/clause. |
| `topic:` / `topics:` (count) | `topic:jekyll`, `topics:>3` | Name match vs count. |
| `license:` | `license:apache-2.0` | SPDX-ish; `null`-licensed never match. Recall-limited. |
| `is:public/private` | `is:public org:github` | Private needs access; unauth → 422/empty. |
| `props.NAME:VALUE` | `org:github props.environment:production` | Single-org only. Types `string/single_select/multi_select/true_false/url`. |
| `mirror:/template:/archived:` | `mirror:true GNOME`, `archived:false` | `archived` read-only flag. |
| `fork:true/only` | `github fork:true` | **Forks excluded by default.** Code search uses `is:fork` instead. |
| `good-first-issues:>n`, `help-wanted-issues:>n` | `good-first-issues:>2` | Min labeled-issue counts; the latter is also a `sort=` value. |
| `is:sponsorable`, `has:funding-file` | `is:sponsorable` | Bool only (tiers/urls need §6). |
| `deployable:/deployed:` | org linked-artifacts | Org-scoped storage/deployment records. |

## 3. REST repo object (R) + GraphQL (G) — every field

Path params `owner/repo` (case-insensitive, no `.git`); `301` on rename, `403/404` on no access. `parent/source` fork-only. `security_and_analysis` admin-only. `permissions` auth-only. Search `items[]` omits `organization/parent/source/security_and_analysis/custom_properties` and adds `score/text_matches`/legacy `forks/open_issues/watchers`+`has_downloads`.

| Field | R | G | Notes |
|---|---|---|---|
| `id` / `node_id` | R | `databaseId`/`id` | Immutable PK. `since` cursor. Store this, not `full_name`. |
| `name` / `full_name` | R | `name`/`nameWithOwner` | `full_name` mutable (rename/transfer). |
| `owner{login,id,type}` / `organization` | R | `owner` | `type` User vs Organization. |
| `private` / `visibility` | R | `isPrivate`/`visibility` | `public/private/internal`. |
| `fork` + `parent`/`source` | R | `isFork`/`parent` | Fork-only parent chain. |
| `archived` / `disabled` / `is_template`+`template_repository` / `mirror_url` | R | `isArchived/isDisabled/isTemplate/templateRepository/isMirror/mirrorUrl` | Archived read-only; `disabled` unsearchable. |
| `description` (null ok) | R | `description(+HTML)` | Text-match field. |
| `homepage` | R | `homepageUrl` | Nullable. |
| `language` (null ok) | R | `primaryLanguage` + `languages{edges{size}}` | Primary only; bytes via `GET .../languages` or G `languages`. |
| `topics[]` | R | `repositoryTopics` | Exact `topic:` vs count `topics:`. |
| `license{key,spdx_id}` (null ok) | R | `licenseInfo` | Null common = unlicensed/`NOASSERTION`. |
| `size` (KB) | R | `diskUsage` | Hourly; coarse. |
| `stargazers_count` / `watchers_count`+`subscribers_count` / `forks_count` (+legacy `forks/open_issues/watchers`) / `open_issues_count` | R | `stargazerCount/watchers{totalCount}/forkCount/issues{totalCount}` | Map `stars:`→`stargazers_count`. `open_issues` incl. PRs. |
| `pushed_at` / `created_at` / `updated_at` (+`archivedAt` G-only) | R | `pushedAt/createdAt/updatedAt/archivedAt` | `pushed` = any-branch commit; `updated` = any activity. Null on empty repo. |
| `default_branch` (+`master_branch` legacy) | R | `defaultBranchRef` | — |
| `has_issues/projects/wiki/pages/discussions/pull_requests/downloads` | R | `hasIssuesEnabled/hasWikiEnabled/hasDiscussionsEnabled/...` | `has_downloads` search-items only. |
| `isLocked/lockReason`, `isEmpty/isMirror/isSecurityPolicyEnabled/isUserConfigurationRepository/isBlankIssuesEnabled`, `securityPolicyUrl`, `usesCustomOpenGraphImage` | — | G-only | No REST counterpart. |
| merge/branch settings (`allow_*`, `*_commit_title/message`, `delete_branch_on_merge`, `allow_update_branch`, `pull_request_creation_policy`) | R | G same names | 1:1. |
| `permissions{admin,push,pull(+maintain/triage?)}` / `role_name` | R | `viewerPermission/viewerCan*` | Auth-only; App auth null. |
| `custom_properties{}` | R | `repositoryCustomPropertyValues` | Org props; §4. |
| collaborators/teams (`collaborators/mentionableUsers/assignableUsers/suggestedActors`) | E | G conn | Needs push/admin. |
| issues/PRs/milestones/labels/releases/deployments/discussions/projects/packages/refs/object | E | G conn (`issues{totalCount}` etc.) | Counts via `totalCount`; exact open PR via `pullRequests(states:OPEN){totalCount}`. |
| `codeowners` / `codeOfConduct` / `contributingGuidelines` / `contactLinks` / `fundingLinks{platform,url}` / `branchProtectionRules`/`rulesets` / `interactionAbility` | E | G obj/conn | `fundingLinks` ⊃ `has:funding-file`. |
| vuln/security (`vulnerabilityAlerts/dependencyGraphManifests`, `security_and_analysis{advanced_security,code_security,dependabot,secret_scanning*}`) | R(admin)/E | G conn | Dependabot/code/secret alerts need scopes. |
| URL templates (`contents_url/languages_url/commits_url/...`, `clone_url/git_url/ssh_url`) / `temp_clone_token` | R | `sshUrl/tempCloneToken` | Hypermedia; `{/...}` are templates. |

## 4. List / enumeration (L) + custom-properties API (C)

| Endpoint | Params | Notes |
|---|---|---|
| `GET /orgs/{org}/repos` | `type=all\|public\|private\|forks\|sources\|member`, `sort=created\|updated\|pushed\|full_name`, `direction`, `per_page≤100`, `page` | `Minimal Repository[]`. No 1000-cap. Prefer over `q=org:x` for org crawl. |
| `GET /repositories` | `since=<id>` cursor only, `Link` next, creation-ID order | **Bulk backfill without search.** `~2M pages` for ~200M repos; shard ID ranges × tokens. |
| `GET /user/repos` | `visibility`, `affiliation=owner,collaborator,organization_member`, `type` (**422 with visibility/affiliation**), `sort/direction/per_page/page` | Auth repos only. |
| `GET /users/{u}/repos`, `GET /users?since=` | `type/sort/direction/per_page/page`, `since` cursor | Seed expansion. |
| Props schema/values | schema: `property_name/value_type/required/default_value/allowed_values[≤200]/values_editable_by/source_type`; values: `repository_names[≤30]+properties[{property_name,value}]` (`null` unsets); bulk `GET /orgs/{org}/properties/values?repository_query=`; external App routes `/installations[/schema|/values]` | Single-org search rule; cross-org needs your own DB (per-org loop → SQLite/Postgres). |

## 5. Mirror / third-party fields (M)

| Source | Key fields (join key `owner/repo`) | Freshness / Cost |
|---|---|---|
| GH Archive + BQ `githubarchive.day.*` | envelope survives cliff: `id/type/created_at/public/actor{id,login}/repo{id,name,url}/org/payload/action`; `PushEvent{ref,head,before,push_id}` (lost `size/commits[]`); `PullRequestEvent` 48→5 fields (lost body/user/timestamps/diffs; `action=merged` new); `ReviewEvent` intact; `Issues/Comment` intact; `Watch/Fork/Create/Delete/Release` intact; `Discussion` events added | Hourly, near-realtime post-Oct-2025 (was up to 8h delay); cliff timeline: brownout 2025-09-08 → permanent 2025-10-07; BQ 2026 pricing $6.25/TiB US, 1TiB/mo free, 10MB min/query, `LIMIT` doesn't prune — dry-run + bare `WHERE event_date=` pruning; hourly `.json.gz` free; 2025-10-09..13 outage invalid; bytes shrank permanently — monitor counts. |
| BQ `bigquery-public-data.github_repos` | `contents{repo_name,path,content}`, `commits{commit,tree,author/committer,date,subject,difference[]}`, `files{repo_name,path}`, `languages{repo_name,language{name,bytes}}`, `licenses{repo_name,license}` | **Frozen ~2016** (2.8M repos). Historical joins only. |
| World of Code | `c2datFull/c2pFull/p2cFull/p2PFull(deforked 284M/351M)/a2c/a2p(aliasing)/c2fbb/b2tac/blob-content/c2PtAbflPkg(imports)/bL2P(licenses)/P_metadata` — commit msg survives PushEvent cliff | Versioned (V2605 2026: 7.3B commits/27B blobs) + hourly LMDB pipeline; shell free w/ account, HTTP needs key. |
| Software Heritage | `origin→visits→snapshot→branches→revision{message,author,date,parents}/release/directory/content` + `origin/search/metadata-search`, extrinsic/intrinsic metadata, counters | Crawled days-weeks lag; free rate-limited; dumps need agreement. File-existence + bytes without GitHub tokens. |
| Stack-v2 (HF, via SWH 2023-09-06) | `blob_id/path/branch/extension/language/is_vendor/generated/src_encoding/length_bytes/detected_licenses/license_type/repo_name/github_id/gha_* /star_events_count+fork_events_count(→2023)/visit+revision+committer dates` | Frozen 2023 + opt-out purges (v2.2.0 2026-07-29); gated HF + S3 via SWH agreement; `star_events` = event counts not live stars. |
| ecosyste.ms Repos (343M total / 336M GitHub / 406M manifests / 25B deps as of 2026) | `host/full_name/owner/uuid/description/homepage/language/size/default_branch/topics/license/mirror/template/stars/forks/subscribers/open_issues/archived/fork/disabled/private/visibility/created/updated/pushed/last_synced/tags/releases/metadata{funding,readme,codeowners,security...}/manifests/dependencies/scorecard/packages/issues+commits+timeline links` + owners + packages/advisories/timeline/commits/docker indexes | Active non-fork resync ≤1wk; free open API 5000 req/hr/IP — join polite pool via `?mailto=`/`User-Agent: mailto:`; `POST /packages/bulk_lookup` for batch; zero-token local via `npx @ecosyste-ms/mcp`; Timeline 7B events / Commits 889M; `sort=pushed_at/stars/forks/...`; **Dockerfile/README/FUNDING existence without tokens** via metafiles/manifests. |
| deps.dev v3 (+BQ) | `GetPackage→versions`, `GetVersion→licenses/advisoryKeys/links/registries/slsa/relatedProjects{github.com/o/r}`, `GetRequirements/Dependencies`, `GetProject github.com/o/r→openIssues/stars/forks/license/scorecard{checks,overallScore}/ossFuzz`, `GetProjectPackageVersions/GetAdvisory/Query(hash→package)`; **batch-first: `GetVersionBatch`/`GetProjectBatch` (1 req for N identifiers), hash→≤1000 versions for content-addressed dedupe** | Registry-synced; free no key; package→repo linkage + vuln/score without GitHub burn. |
| Libraries.io | `Project{platform/description/homepage/repository_url/keywords/licenses/language/rank/score/dependents/dependent_repos/versions/latest_release/status/stars/forks/contributions/urls}` + search `sort=rank,stars,dependents,...` | **Stale** (rows 2024 vintage); key required; prefer ecosyste.ms/deps.dev fresh. |
| ClickHouse `github_events` playground | Flattened ~60 cols (`time/type/actor_login/repo_name/repo_id/created_at/action/.../additions/deletions/commits/body/author_association/...`) — **filter `event_type` first for partition pruning** (`ORDER BY (event_type, repo_name, date)`); refreshable MVs make top-N ~0.01s | Hourly from GH Archive S3; free `user=play`; inherits Oct-2025 NULLs — use surviving `actor/repo/created/action`. |
| Sourcegraph / grep.app | `repo:@rev/select:repo/lang:/fork:yes-archived:has.meta()` + GraphQL git metadata (SG); `repo/path/snippet` only (grep.app) | Indexed HEAD lag min-hours/days; no stars/topics/license filters — enrich via ecosyste.ms/REST. |

Enrichment REST (E) needed for full params: `GET .../languages` (bytes), `/contributors?anon=1` + `/stats/contributors` (counts; `202` retry; `≥10k` 422), `/releases|/tags` (+`/releases/latest`), `/commits?since=&until=&sha=` (+`Link` last-page count), `/issues|/pulls?state=` (+`/search/issues` `type:pr+is:merged`), `/contents/{path}` + `/git/trees/{sha}?recursive=1` (file-existence/glob; `100k/7MB truncated`), `/topics`+`/license`, `/stats/commit_activity|code_frequency|participation|punch_card`, `/community/profile` (health+files), `/traffic/views|clones|paths|referrers` (owner-only 14d), checks/runs/status (per-SHA CI), SBOM (`/dependency-graph/sbom` → async post-2026-11-13), dependabot/code/secret alerts, branches/protection/rulesets/CODEOWNERS/errors.

## 6. NOT searchable — workaround per param (X → how to get)

`NO` = no `q` filter/rank. The pattern throughout: search candidates → hydrate per-repo (`core` 5000/hr) → store locally.

| Desired param | Why not | Workaround (cheapest first) | Cost / Freshness |
|---|---|---|---|
| `has_file:<path>` (Dockerfile, `*.yml`, `package.json/go.mod/Cargo.toml/pyproject`) | No filename qualifier (only `in:` text; `path:/extension:` code-only) | `GET .../contents/{path}?ref={branch}` (`200` vs `404`); dir `GET .../contents/.github/workflows`; glob `GET .../git/trees/{sha}?recursive=1` filter prefix | `core` 1/repo/file or 1/repo tree (`truncated` → per-subtree/clone); live git. |
| coverage % | External (Codecov/Coveralls/Actions) | README badge regex; `GET .../actions/artifacts` → `coverage.xml`; vendor API | `core`+vendor quota; per-run. |
| CI status | Per-SHA, not repo attr | `GET .../commits/{ref}/check-runs+suites+status`, `GET .../actions/runs?head_sha=` → `conclusion` | `core` 2–3/SHA; HEAD only. |
| LOC / file count / size split | `size:` total KB only | `GET .../languages` (bytes) + `.../git/trees?recursive=1` (`len/sum/per-ext`); exact `git clone --depth 1; tokei/cloc` | `core` 2/repo; hourly vs live; clone zero-API at scale. |
| contributors / bus factor | Computed, cached | `GET .../contributors?anon=1` paginate `Link` (count); `GET .../stats/contributors` (`total/weeks`); sort cum `≥50%` → N | `core`; `202`-retry; `≥10k` limits; hours-old. |
| open/merged PRs | `open_issues` incl. PRs | open: `GET .../pulls?state=open&per_page=1` → `Link` last N; merged: `GET /search/issues?q=repo:O/R+type:pr+is:merged` → `total_count` or closed-filter `merged_at`; GQL `pullRequests(states:){totalCount}` | `core` 1–2 + search bucket / 1pt GQL; search approx on timeout. |
| issue close rate / MTTR | No aggregate | `GET /search/issues?q=repo:O/R+type:issue+state:closed/open` → rate; sample 100 `closed_at-created_at` → mean | Search cheap; full avg N pages. |
| releases (count/latest/semver) | Tags≠releases | `GET .../releases?per_page=100` paginate (+`/releases/latest`, fallback `/tags`); `packaging.version` parse | `core` 1–2; live. |
| commits (count/freq/messages) | `pushed:` filter only | count `GET .../commits?per_page=1` → `Link` last; freq `GET .../stats/commit_activity|participation|code_frequency` (`422` if `≥10k`); msgs `GET .../commits?per_page=100&sha=` | `core`; stats cached/`202`. |
| README length/sections/badges | `in:readme` keyword only | `GET .../readme` (`Accept: raw`) → `len/re.findall(^#{1,6})`/badge regex; `200` vs `404` = has_readme | `core` 1; live default-branch. |
| code content (imports/regex) | Code search throttled/web-only (`10/min`, default-branch, `<384KB`/`>350KiB` excl, best-match, ≤100 web) | Scoped `GET /search/code?q=term+repo:O/R` then `git clone --depth 1 && rg` fallback (ground truth); for existence sweeps prefer `git clone --filter=blob:none --no-checkout --depth 1 --sparse` + `sparse-checkout set <paths>` (up to 180× on monorepos); cross-repo identical files dedupe once via `GET .../git/blobs/{sha}` (content-addressed) | Code bucket expensive; clone+`rg` cheapest exhaustive; index lag vs clone truth. |
| dependencies (direct+transitive) | Graph not indexed | `GET .../dependency-graph/sbom` (async post-2026-11-13) + manifest `GET .../contents/{package.json,...}` | `core` 1–2; per-push. |
| dependents (`used-by`) | No API field (UI `>100` only) | Scrape `GET https://github.com/{o}/{r}/network/dependents` (`Box-row`) or ecosys/npm/PyPI APIs | HTML `~60/hr` IP, fragile, private hidden; use ecosys/deps.dev. |
| vulns | Needs access | `GET .../dependabot/alerts` (scope) else `api.osv.dev/v1/query` by SBOM + `/advisories` | `core` / OSV free; live if enabled else inferred. |
| Scorecard / criticality | External | `GET https://api.scorecard.dev/projects/github.com/{o}/{r}` (+badge/CLI); weekly Scorecard feed (1M critical projects) | External weekly; CLI on-demand fresh. **Note: the `criticality_score` bulk feed (GCS+BQ) is DEAD since 2026-08-29 — do not depend on it** (https://github.com/ossf/criticality_score/blob/main/README.md, accessed 2026-09-29). |
| funding exact urls | `has:funding-file` bool only | `GET .../contents/.github/FUNDING.yml` decode YAML; GQL `fundingLinks{platform,url}` | `core` 1 / 1pt; live. |
| traffic views/clones | Owner-only 14d | `GET .../traffic/views|clones|paths|referrers` (push access); persist daily (14d retention) | `core`; `403` non-collab; UTC buckets. |
| sponsor tiers | `is:sponsorable` bool only | GQL `user|organization(login:){sponsorsListing{tiers{monthlyPriceInDollars}}}` | GQL points; live. |
| discussions count | No REST | GQL `repository{discussions{totalCount}}` | 1pt; live. |
| wiki content | `has_wiki` flag only | `git ls-remote https://github.com/O/R.wiki.git HEAD` + clone | `core` 1 + git; live. |
| teams / CODEOWNERS / protection | Not indexed | `GET /orgs/{org}/teams`+`.../repos/{o}/{r}/teams`; contents `{/.github/}CODEOWNERS` + `.../codeowners/errors?ref=`; `GET .../branches?protected=true` + `.../branches/{b}/protection`+`/rulesets` | `core`; admin for write view; per-branch. |
| cross-org custom numeric/text | `props.*` single-org only | Per-org `GET /orgs/{org}/properties/values?repository_query=` + `GET .../properties/values` → own DB (SQLite/DuckDB/Postgres) query cross-org locally | `core` N_orgs×pages; indexed locally. |
| `updated:` | Doesn't exist | `q=... pushed:>=DATE` + `sort=updated&order=desc`, client-filter `updated_at>=X` | Search bucket; `pushed` commit vs `updated` activity. |
| multi-language | `language:` primary only | `GET .../languages` → `max()` primary, rest secondary | `core` 1; per-push Linguist. |
| license variants | `license:` SPDX recall-limited | `GET .../license` + `GET .../contents/LICENSE` (raw) regex + SBOM concluded/declared | `core` 2; live file. |
| deleted/private/renamed | Search never pushes deletes | `GET ...` → `200` (+`full_name` mismatch = renamed) / `301` follow / `404` tombstone (auth disambiguates); store immutable `id` | `core` 1 + `ETag`; API live, search stale hours-days. |
| star history (post-2026-06-30 privacy) | `stars:` current bucket only; `/stargazers` restricted to collab | New privacy-safe `GET .../stargazers/count` (`{count}`) + `GET .../stargazers/history?per_page=30&page=` (`[{week_start,stars,total}]` newest-first, zero-filled; concat = series) | `core` cheap 30/pg; weekly live. Old per-user timestamps collab-only. |
| owner country (geographical info) | No structured field; `location` free-text only; no `location:` qualifier on repo search (users-search only) | `GET /users/{login}` or `GET /orgs/{org}` → `location` (+`company/blog`) → normalize → gazetteer/geocode → ISO (see §6.1) | `core` 1/owner (cache by user id); gazetteer O(1); geocoder residue only. |
| agent-trace content (detection support) | `has_file` existence only; no trailer/branch/label search on repo search | Guidance-file *content* via `GET .../contents/{CLAUDE.md,AGENTS.md,...}`; commit trailers via `GET .../commits` messages + BigQuery; branch prefixes via `GET .../branches`; labels via PR API; versioned pattern packs recommended | `core` per file/commit page; BigQuery for history at scale. |
| windowed commit history | No history qualifier (only `pushed:` filter) | BigQuery `githubarchive.day.*` primary (`JSON_EXTRACT(payload)`); `GET .../commits?since=&until=` top-up; adoption-date + ratios computed downstream | BQ billed per scan (prune!); API `core` for gaps. |
| PR outcomes/iterations | No PR qualifiers on repo search | `GET .../pulls?state=all` (outcome, time-to-merge, iterations, reverts) + GraphQL bulk (10k cap) + `GET /search/issues?q=repo:O/R+type:pr` | `core` + search bucket; GraphQL points. |
| crates.io linkage | No package linkage in repo search | `GET https://crates.io/api/v1/crates/{name}` (metadata/downloads/yanked/versions) + `Cargo.toml`/workspace parse + registry check for hallucination | Free, no key; live. |
| user-search seeding + snowball | No social graph on repo search | `GET /search/users?q=location:X+language:Y` seed → `GET /users/{u}/followers|following` frontier + org-member expansion | Search bucket for seed; `core` per page thereafter. |
| `.gitignore` visibility | File-list only; no ignore-awareness | `GET .../contents/.gitignore` content scan — repos visible only via ignore still count (2–11.8% in studies) | `core` 1/repo. |
| disk incl. LFS | `size` git KB only | `size*1024` + `.gitattributes` `filter=lfs` + `git lfs ls-files -s`/batch API (`300/min` unauth/`3000/min` auth) | `core`+clone; hourly vs live. |

### 6.1 Owner location → country pipeline (mitigating emojis, cities, multi-values)

Native availability: `location: string|null` on `GET /users/{login}` / `GET /orgs/{org}` (https://docs.github.com/en/rest/users/users#get-a-user, accessed 2026-09-29); the substring `location:` qualifier exists only on `GET /search/users` (https://docs.github.com/en/search-github/searching-on-github/searching-users, accessed 2026-09-29), not on repo search — so resolve client-side per owner and cache by user `id`.

1. **Flag emojis first (deterministic).** Regional-indicator pairs map 1:1 to ISO alpha-2 — regex them out, highest confidence, zero API cost.
2. **Normalize, then split.** Lowercase, strip non-flag emojis/punctuation, split on `/ | , • ( )` ("Berlin / NYC" → candidates scored separately).
3. **Gazetteer before geocoder.** An offline city→country table (e.g. GeoNames cities1000 with a population threshold) in O(1); collisions (Paris FR vs Paris TX) resolved by population + co-occurring country token.
4. **Country alias table.** ISO names + alpha-2/alpha-3 + demonyms + variants ("USA", "UK", "UAE", "Nederland", "Deutschland").
5. **Geocoder for residue only** (Nominatim/Photon), cached by normalized string — a repeated location costs one call ever.
6. **Weak tiebreakers, never primaries.** Blog/company domain TLD, commit `tz_offset` (UTC band only, never country).
7. **Persist confidence tiers** `{country_iso, confidence, raw_location}` (exact-ISO > name > gazetteer-city > geocoder > TLD/timezone > unmatched) and dashboard the unmatched rate with spot-check sampling.

## 7. Unknown / typo behavior + validation

- Top-level `&foo=bar` → ignored `200` (only `q,sort,order,per_page,page` honored). Never forward customs — allowlist + your own `400`.
- `q` unknown qualifier (`has_dockerfile:true`, `updated:>...`, `is:archived`) → free-text `200` with drifted `total_count`. The only `422`s are for `>256 chars`, `>5 ops`, and inaccessible `repo:/user:/org:`.
- Typo table: `is:archive→archived:`, `is:fork/forks:true→fork:true/only (+forks:>N)`, `updated:/push:→pushed: (+sort=updated)`, `is:sponsor/has:funding→is:sponsorable/has:funding-file`, `props.*` alone→`org:O props.*`, `is:mirror/template→mirror:/template:`.
- CI validation (Python): parse `q` tokens `(\w[\w\.-]*):`, assert ⊆ allowlist + `props.*` only with `org:/user:`; delta test `total(base+cand) < total(base)` (a real filter narrows; text doesn't); assert `incomplete_results==False`; log `q/total/incomplete`. `gh search repos` + Terraform `data.github_repositories{query,sort}` pass through the same `q` — same rule.

```python
ALLOWED = {"in","repo","user","org","size","followers","forks","stars","created","pushed","language","topic","topics","license","is","mirror","template","archived","good-first-issues","help-wanted-issues","has","props","fork","deployable","deployed"}
```

## 8. Recommended fetch plan for gitcrawl

1. **Discover:** `GET /repositories?since=` backfill (or sharded `search` `created:` bisect if filtered) → candidate `full_name+id`.
2. **Hydrate:** `GET /repos/{o}/{r}` (+`ETag`) → R fields; GQL batch for `fundingLinks/discussions/scorecard-linkage` in one shot.
3. **Enrich:** `languages/contents/trees/releases/commits/issues+search-issues/stats/community/traffic(if owner)/SBOM/OSV/deps.dev+ecosyste.ms` per need; file-existence via ecosyste.ms metafiles to save `core`.
4. **Mirror-bootstrap:** BQ/WoC/SWH/Stack for history/content without burn; GH Archive/ClickHouse for trends (envelope fields survive the cliff).
5. **Store:** PK `id`, `full_name` history, `deleted_at`, `custom_properties`, watermark `max(pushed_at)` per shard + 1h overlap, `id` dedupe, audit log per §A11 of `05`.

## Sources — full clickable references (all accessed 2026-09-29 unless noted)

### Official Search / Repos / GraphQL
- Search mechanics/params/schema (`q/sort/order/per_page/page`, 1000-cap, 4000-scan, `incomplete_results`, 256-char/5-op, rate): https://docs.github.com/en/rest/search/search?apiVersion=2022-11-28 (accessed 2026-09-29)
- Repo qualifiers + single-org `props.*` rule + `pushed:` vs `created:` + no `updated:` + `has:funding-file`/`is:sponsorable`: https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories (accessed 2026-09-29)
- Forks excluded by default (`fork:true`/`fork:only`; code uses `is:fork`): https://docs.github.com/en/search-github/searching-on-github/searching-in-forks (accessed 2026-09-29)
- Full repo schema + list params (`GET /repos/{o}/{r}`, `GET /orgs/{org}/repos`, `GET /repositories?since=`): https://docs.github.com/en/rest/repos/repos (accessed 2026-09-29); get-a-repository: https://docs.github.com/en/rest/repos/repos#get-a-repository (accessed 2026-09-29)
- GraphQL Repository object: https://docs.github.com/en/graphql/reference/objects#repository (accessed 2026-09-29); GraphQL search: https://docs.github.com/en/graphql/reference/queries#search (accessed 2026-09-29); GraphQL rate/points + resource caps: https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api (accessed 2026-09-29); changelog: https://github.blog/changelog/2025-09-01-graphql-api-resource-limits/ (accessed 2026-09-29)
- Custom props REST + `repository_query`: https://docs.github.com/en/rest/orgs/custom-properties?apiVersion=2026-03-10 (accessed 2026-09-29); setup: https://docs.github.com/en/organizations/managing-organization-settings/managing-custom-properties-for-repositories-in-your-organization (accessed 2026-09-29)
- Limits/pagination/best-practices/auth/troubleshooting/versions/syntax/sort: https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api (accessed 2026-09-29), https://docs.github.com/en/rest/rate-limit/rate-limit (accessed 2026-09-29), https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api?apiVersion=2026-03-10 (accessed 2026-09-29), https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api (accessed 2026-09-29), https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api?apiVersion=2026-03-10 (accessed 2026-09-29), https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api (accessed 2026-09-29), https://docs.github.com/en/rest/about-the-rest-api/api-versions (accessed 2026-09-29), https://docs.github.com/en/rest/about-the-rest-api/breaking-changes (accessed 2026-09-29), https://docs.github.com/en/search-github/getting-started-with-searching-on-github/understanding-the-search-syntax (accessed 2026-09-29), https://docs.github.com/en/search-github/getting-started-with-searching-on-github/sorting-search-results (accessed 2026-09-29), https://docs.github.com/en/search-github/getting-started-with-searching-on-github/troubleshooting-search-queries (accessed 2026-09-29)

### Enrichment REST
- Contents: https://docs.github.com/en/rest/repos/contents (accessed 2026-09-29); Git trees: https://docs.github.com/en/rest/git/trees (accessed 2026-09-29); Releases: https://docs.github.com/en/rest/releases/releases (accessed 2026-09-29); Commits: https://docs.github.com/en/rest/commits/commits (accessed 2026-09-29); Stats/traffic/community: https://docs.github.com/en/rest/metrics/statistics (accessed 2026-09-29), https://docs.github.com/en/rest/metrics/traffic (accessed 2026-09-29), https://docs.github.com/en/rest/metrics (accessed 2026-09-29); SBOM: https://docs.github.com/en/rest/dependency-graph/sboms (accessed 2026-09-29); Dependabot alerts: https://docs.github.com/en/rest/dependabot/alerts (accessed 2026-09-29); Branch protection: https://docs.github.com/en/rest/branches/branch-protection (accessed 2026-09-29); Starring/star-history: https://docs.github.com/en/rest/activity/starring?apiVersion=2026-03-10 (accessed 2026-09-29); Users (`location` free-text): https://docs.github.com/en/rest/users/users#get-a-user (accessed 2026-09-29); Searching users (`location:` qualifier, users-search only): https://docs.github.com/en/search-github/searching-on-github/searching-users (accessed 2026-09-29)

### Code search + star privacy
- Code-search about/limits (350KiB/vendored/binary/UTF-8/100 web/1000 chars): https://docs.github.com/en/search-github/github-code-search/about-github-code-search (accessed 2026-09-29); legacy code search: https://docs.github.com/en/search-github/searching-on-github/searching-code (accessed 2026-09-29)
- Star access restriction 2026-06-30: https://github.blog/changelog/2026-06-30-upcoming-access-restrictions-to-public-api-endpoints-and-ui-views/ (accessed 2026-09-29); privacy-safe star history 2026-09-04: https://github.blog/changelog/2026-09-04-new-api-endpoint-provides-privacy-safe-star-history-data (accessed 2026-09-29); breakage report: https://github.com/star-history/star-history/issues/539 (2026-07-04, accessed 2026-09-29)

### Mirrors / datasets
- GH Archive: https://www.gharchive.org/ (accessed 2026-09-29); schema: https://github.com/igrigorik/gharchive.org/blob/master/bigquery/schema.js (accessed 2026-09-29); event types: https://docs.github.com/en/rest/using-the-rest-api/github-event-types (accessed 2026-09-29); payload changelog 2025-08-08: https://github.blog/changelog/2025-08-08-upcoming-changes-to-github-events-api-payloads/ (accessed 2026-09-29); issues: https://github.com/igrigorik/gharchive.org/issues/310 (2025-07-16), https://github.com/igrigorik/gharchive.org/issues/312 (2025-10-12); cliff study: https://codepulsehq.com/research/github-archive-payload-cliff (accessed 2026-09-29)
- BQ github_repos announcement (frozen ~2016): https://cloud.google.com/blog/topics/public-datasets/github-on-bigquery-analyze-all-the-open-source-code (2016-06-30, accessed 2026-09-29); BQ public data: https://docs.cloud.google.com/bigquery/public-data (accessed 2026-09-29); Kaggle mirror: https://www.kaggle.com/datasets/github/github-repos (accessed 2026-09-29)
- World of Code: https://worldofcode.org/docs/ (accessed 2026-09-29); catalog: https://www.worldofcode.org/catalog (accessed 2026-09-29); tutorial: https://github.com/woc-hack/tutorial (2025-05-13, accessed 2026-09-29)
- Software Heritage API + dataset: https://docs.softwareheritage.org/devel/getting-started/api.html (accessed 2026-09-29); https://docs.softwareheritage.org/devel/swh-dataset/graph/dataset.html (accessed 2026-09-29)
- Stack-v2: https://huggingface.co/datasets/bigcode/the-stack-v2 (accessed 2026-09-29); paper: https://arxiv.org/pdf/2402.19173 (accessed 2026-09-29)
- ecosyste.ms: https://docs.ecosyste.ms/docs/services/data-services/repositories/ (accessed 2026-09-29); https://ecosyste.ms/api (accessed 2026-09-29); https://repos.ecosyste.ms (accessed 2026-09-29); https://packages.ecosyste.ms (accessed 2026-09-29)
- deps.dev v3: https://docs.deps.dev/api/v3/ (accessed 2026-09-29); https://deps.dev (accessed 2026-09-29)
- Libraries.io: https://libraries.io/api (accessed 2026-09-29); https://github.com/librariesio/libraries.io (accessed 2026-09-29)
- ClickHouse github_events: https://clickhouse.com/docs/get-started/sample-datasets/github-events (accessed 2026-09-29); playground: https://play.clickhouse.com (accessed 2026-09-29)
- Sourcegraph queries/language/metadata + stream/MCP: https://sourcegraph.com/docs/code-search/queries (accessed 2026-09-29); https://sourcegraph.com/docs/api/stream-api.md (accessed 2026-09-29); https://sourcegraph.com/docs/api/mcp (accessed 2026-09-29); grep.app: https://grep.app (accessed 2026-09-29), help: https://grep.app/search/help (accessed 2026-09-29)

### CLI / Terraform / misc
- `gh search repos`: https://cli.github.com/manual/gh_search_repos (accessed 2026-09-29); issues: https://github.com/cli/cli/issues/1004 (accessed 2026-09-29), https://github.com/cli/cli/issues/5501 (accessed 2026-09-29), https://github.com/cli/cli/issues/8984 (2024-04-20), https://github.com/cli/cli/issues/9254 (2024-06-25); Terraform data source: https://registry.terraform.io/providers/integrations/github/latest/docs/data-sources/repositories (accessed 2026-09-29)
- Dependents no-API (SO 2019-11-06, still valid 2026-09-29): https://stackoverflow.com/questions/58734176/how-to-use-github-api-to-get-a-repositorys-dependents-information-in-github (accessed 2026-09-29)
- Scorecard API: https://api.scorecard.dev/projects/github.com/{o}/{r} (accessed 2026-09-29); OSV: https://api.osv.dev/v1/query (accessed 2026-09-29)
- Thesis support (all accessed 2026-09-29): crates.io API https://doc.rust-lang.org/cargo/reference/registry-index.html + https://crates.io/api/v1/crates/{name}; BigQuery githubarchive https://cloud.google.com/bigquery/public-data + dataset https://console.cloud.google.com/marketplace/product/github/github-repos; user-search seeding https://docs.github.com/en/search-github/searching-on-github/searching-users
- Efficiency refresh (all accessed 2026-09-29): GH payload changelog https://github.blog/changelog/2025-08-08-upcoming-changes-to-github-events-api-payloads/; BQ pricing https://cloud.google.com/bigquery/pricing; ecosyste.ms rate-limiting https://blog.ecosyste.ms/2025/09/01/rate-limiting-the-right-way.html + MCP https://github.com/ecosyste-ms/mcp; `criticality_score` bulk dead https://github.com/ossf/criticality_score/blob/main/README.md (use Scorecard feed); ClickHouse demo https://clickhouse.com/demos/explore-github-with-clickhouse-powered-real-time-analytics; DuckDB poller https://github.com/Harishankar1988/gharchive-duckdb-pipeline; GraphQL resource caps https://github.blog/changelog/2025-09-01-graphql-api-resource-limits + timeouts-in-limits https://github.blog/changelog/2025-07-21-including-timeouts-in-primary-rate-limits + dryRun https://docs.github.com/en/graphql/reference/meta; install-token math https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api?apiVersion=2026-03-10; stateless tokens https://github.blog/changelog/2026-05-15-github-app-installation-tokens-per-request-override-header/
