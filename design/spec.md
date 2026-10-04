# Feature Specification: gitcrawl — Continuous GitHub Repository Discovery Crawler

**Feature Branch**: `001-gitcrawl`

**Created**: 2026-09-29

**Status**: Frozen (v3 — 2026-10-01 amendment: US4 operator console added, see `console-spec.md`; run history is operator-triggered artifacts, live-only freshness unchanged)

**Input**: Ultimate crawler design per `findings/00–06` (SEART-style crawl → store → serve, hardened with `05` gap fixes + efficiency review). Scope: cross-GitHub public · Stack: Python + Postgres · Freshness: live-at-fetch only (no background polling — entire job runs on the go at `ran_at`).

> **Status note (2026-10-04)**: US1–US4 are implemented (see `../docs/development-log.md`). The thesis tracks **FR-015–FR-022 are parked** — designed, not built — and their entities (`TracePack`, `CorpusFrame`) exist as design intent only; `tasks.md` Phase 6 stays deferred. `min_loc`/`max_loc` are accepted but recorded-only (R44); a no-clone estimate path is designed in `loc-dilemma.md`. This spec's requirement text stays frozen; the status note is the pointer.

**New here? Read `how-the-data-flows.md` first** — it narrates every stage below (filters → Find → hydrate → enrich → display → bundle) with each GitHub limitation explained.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Discover every matching public repo (Priority: P1)

An operator defines criteria (language, stars, topics, license, pushed range, custom virtual filters) and gitcrawl returns the *complete* matching set — not the first 1000 — stored locally for arbitrary querying.

**Why this priority**: Without complete discovery nothing downstream (freshness, enrichment, serving) has value. This is the MVP slice: sharded search + `since`-enumeration backfill into Postgres.

**Independent Test**: Fully testable by running discovery against golden org `org:github`, then diffing stored `id`-set vs an independent `GET /repositories?since=` sample — coverage measured, no other story needed.

**Acceptance Scenarios**:

1. **Given** criteria `language:python stars:>500`, **When** discovery runs to exhaustion, **Then** every stored repo matches the criteria and each shard records `total_count`, fetched count, and `incomplete_results=false` (or `incomplete` with auto-shard applied).
2. **Given** a logical query with `total_count > 1000`, **When** discovery runs, **Then** the system shards by `created:` ranges until every shard is fetchable, dedupes by immutable `id`, and never requests `page` past the 1000-result cap (cap-`422` triggers auto-shard, never a blind loop).

3. **Given** a thesis corpus frame (`language:rust`, not forks, commits≥100, LOC≥5K, study window, old/new buckets, size splits), **When** discovery runs, **Then** every filter enforces (searchable server-side, commits/LOC post-hydration), attrition (private/deleted/dotfiles) is counted not hidden, size splits are recorded per repo, raw upstream JSON is stored per repo, and the run bundle carries the frozen frame config for cross-stack comparison.
4. **Given** an unknown qualifier typo (e.g. `updated:>2024-01-01`), **When** the query is submitted, **Then** the client allowlist rejects it with a local `400` before any GitHub call (silent-`200`-as-text is never mistaken for filtering, verified by the delta test: a real filter narrows `total_count`).

---

### User Story 2 - Reflect current state at fetch moment (Priority: P2)

Each run reflects GitHub as it is at fetch time (`ran_at`): current matches, renames/transfers followed, deletions/privatizations recorded. No background polling — the entire job runs on the go.

**Why this priority**: With live-only execution there is no stored index to keep fresh; correctness means the bundle matches live state at `ran_at`. Lifecycle handling within the run is still required.

**Independent Test**: Testable on seeded fixtures without advancing wall-clock: push/rename/delete fixtures → single run → current `pushed_at` present, renames collapse to one `id`, `404`s become `deleted_at` tombstones — verifiable from bundle + audit logs alone.

**Acceptance Scenarios**:

1. **Given** a live search + hydration at `ran_at`, **When** the run executes, **Then** repos carry their current `pushed_at`, `id`-dedupe absorbs overlap pages, and the run stays within 30 search req/min/token with zero background jobs.
2. **Given** a repo rename/transfer, **When** revalidation runs (`GET /repos/{o}/{r}`), **Then** the `301` is followed, `full_name` history gains a row, and exactly one `id` row survives (no orphan duplicates).
3. **Given** a repo deletion or privatization, **When** revalidation gets `404`, **Then** the row is tombstoned (`deleted_at` set, quarantined from serve within the retention window) rather than silently dropped.

---

### User Story 3 - Enrich and serve with virtual params incl. owner country (Priority: P3)

Consumers query gitcrawl's own API with filters GitHub never built (`has_dockerfile`, team, `owner_country`, coverage) and get enriched repo records (languages bytes, releases, funding links, discussions, Scorecard linkage, owner country with confidence).

**Why this priority**: Turns the raw index into the product: arbitrary offline filtering + the geo resolver + a stable serve contract. Depends on US1/US2 data but serves independently once they have run.

**Independent Test**: Testable by seeding 3 fixture repos and calling `GET /vsearch/repos?has_dockerfile=true&owner_country=DE` — correct filtering plus `{country_iso, confidence, raw_location}` on each owner proves the slice without live crawling.

**Acceptance Scenarios**:

1. **Given** `GET /vsearch/repos?q=...&min_stars=100&team_topic=platform&has_dockerfile=true&owner_country=DE`, **When** called, **Then** unknown params are never forwarded to GitHub (allowlist `q,sort,order,per_page,page`, own `400` otherwise), virtual params translate to legal GitHub calls + post-filters, and results carry enrichment + geo confidence tiers.
2. **Given** owner `location` values `"🇩🇪 Berlin"`, `"Lagos"`, `"🌍 remote"`, **When** the geo resolver runs, **Then** results are `{DE, exact-ISO}`, `{NG, gazetteer-city}`, `{null, unmatched}` respectively — flag decode beats gazetteer beats unmatched-bucket, never a forced guess.
3. **Given** a filter-spec JSON doc (`gitcrawl_filter: 1` with `q/sort/order/virtual/page`) uploaded via `POST /vsearch/run`, **When** it contains a typo'd qualifier, **Then** validation rejects it with a local `400` + hint before any GitHub call; **When** valid, **Then** the run returns `{filter_hash, ran_at, api_version, total_count, incomplete, items}` and `GET /vsearch/runs/{filter_hash}` replays it identically on another device (tokens per-device, results merging by `id`); rerun drift is reported via recorded metadata, and byte-identical reproduction comes from the exported run bundle (`GET .../export`: filters + metadata + raw upstream JSON).

4. **Given** per-repo enrichment load, **When** file-existence is needed for 1M repos, **Then** the system uses 1× `trees?recursive=1` (or zero-call ecosyste.ms metafiles) instead of N× `contents` calls (8× budget waste eliminated), and GraphQL is used only where it replaces ≥3 REST calls with `first≤50`.

5. **Given** the filter form at `GET /vsearch/` with every parameter from the `06` matrix (searchable groups + virtual section + `props.*` gated on single-`org:`), **When** a user picks filters and submits (or uploads an equivalent JSON file), **Then** both doors build the byte-identical filter-spec and return identical results; the form additionally offers "download these filters as JSON" for sharing.

6. **Given** a run with mixed cheap and expensive filters over thousands of candidates, **When** the enrichment scheduler plans execution, **Then** filters run in cost order with id-range segments across tokens survivors-first, and the run reports per-field source + calls spent — ~3–4× under naive per-repo enrichment with zero fields dropped.

7. **Given** displayed results with an optional Clone control (slider + digit input for N, mode radio, disk-estimate preview), **When** the user clones N repos (or zero — always valid), **Then** the top-N by current sort clone into `clones/{run_hash}/` (shallow/file-only/windowed modes), completed repos skip on retry, progress records into run metadata, and a low-disk warning fires before confirming.

---

### Edge Cases

- Query exceeds 256 chars (excl. operators/qualifiers) or 6+ `AND`/`OR`/`NOT` → local `400` with fix hint, never a live `422` surprise.
- Search `422 Only the first 1000` → auto-shard, never page-11 loop; `422` spam/abuse signature (`errors[].code==custom`) → backoff, never shard; other `422` → fix query, never retry verbatim.
- `incomplete_results:true` → narrow once, then accept partial + mark shard `incomplete` (no infinite re-probe; `total_count` is approximate, never ground truth).
- `403/429` on one bucket pauses only that bucket (`x-ratelimit-resource` keyed); `retry-after` → exact sleep; `remaining==0` → sleep to `reset`; else `60s×2^n`+jitter, max 5 attempts.
- `GET /repos` 304 handling: ETag kept for hydrate/trees/contents (stable URLs, free if authed), never for search path (~0% hit rate).
- Token incident: any `401`, bad-credential `403` lockout, or SAML SSO `partial-results` header → loud failure + runbook, never silent filtered results.
- `robots.txt` disallows `/search` → HTML fallback stays disabled (API-only default; scrape needs legal review).
- Private/internal repos, `is:private` without access, GHES hosts, and vendor outages → defined `400`/`502` mappings in `contracts/search-api.md`, never leaked upstream bodies.
- Deployment-status history older than 90d is never crawled (auto-deleted upstream); `?fields=`-style sparse fieldsets are never assumed (GitHub REST has none — media types only).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST discover repos via sharded `GET /search/repositories` (`created:` bisect until each shard fetchable, `per_page=100`, `Link: rel="next"` followed verbatim) AND `GET /repositories?since=` ID-cursor backfill (creation order, `max(id)` checkpoint, ID-range sharding).
- **FR-002**: System MUST prefer `GET /orgs/{org}/repos` / `GET /users/{u}/repos` enumeration over `q=org:x`/`q=user:x` for single-org/user scopes (core bucket, no 1000-cap, authoritative).
- **FR-003**: System MUST validate every qualifier client-side against the `06 §2` allowlist (plus `props.*`-only-with-`org:` rule) and reject unknowns/typos with local `400` (delta test in CI: unchanged `total_count` = fail).
- **FR-004**: System MUST persist repos keyed on immutable `id BIGINT` PK with `full_name UNIQUE` + `full_name_history` + `deleted_at` tombstones; follow `301` on rename; tombstone on `404`.
- **FR-005**: System MUST reflect live state at `ran_at` with within-run `id`-dedupe; MUST record current `pushed_at` per repo; MUST NOT maintain persistent watermarks, hot/cold tiers, or background/GH Archive tails (live-only).
- **FR-006**: System MUST rate-limit via central Redis token buckets keyed per `search`/`core`/`code_search` resource, pacing from response headers (`x-ratelimit-*`, `retry-after`), honoring the 3-branch classifier and per-bucket pausing; MUST load tokens at runtime from `GITHUB_TOKEN` (single) or `GITHUB_TOKENS` (comma-list) — env only, never hardcoded — and record fingerprints, never raw tokens, in audit logs; MUST NOT share tokens across owners to evade limits; MUST cap ~10 concurrent requests per endpoint per token (900 pts/min/endpoint math); MUST accept both classic 40-char and stateless `ghs_` JWT install-token formats; MUST read `resources.core` from `GET /rate_limit` (top-level `rate` removed).
- **FR-007**: System MUST hydrate via `GET /repos/{o}/{r}` (+ETag) and enrich cost-aware: 1× `trees?recursive=1`/ecosyste.ms metafiles for file-existence (never N× `contents` loops; sparse blobless clones only for very large path counts; identical blobs fetched once by SHA); GraphQL batched only where it replaces ≥3 REST calls (`first≤50`, ≤10–20 aliases per query, shallow fields, `dryRun` cost gate, split-on-timeout never retry-same-shape); deps.dev batch endpoints first (`GetVersionBatch`/`GetProjectBatch`); ecosyste.ms via polite-pool headers (`?mailto=`); star history via `stargazers/history`+`/count` (never stargazer pagination); `languages`, releases/tags, commits/stats, issues/PRs, SBOM/OSV, Scorecard weekly feed (`criticality_score` bulk is dead — never depend on it), fundingLinks/discussions per `06 §6` cost notes.
- **FR-008**: System MUST resolve owner country via the `06 §6.1` pipeline (flag-emoji decode → normalize/split → gazetteer → alias table → cached residue geocoder → weak tiebreakers) and persist `{country_iso, confidence, raw_location}` with an unmatched bucket (never forced guesses).
- **FR-009**: System MUST serve `GET /vsearch/repos` per `contracts/search-api.md` (virtual params translated server-side, upstream allowlist enforced, enrichment + geo tiers in responses).
- **FR-010**: System MUST emit per-request audit logs (timestamp, query hash + full params, ETag, `x-ratelimit-*`, `retry-after`, `Link`, `total_count`/`incomplete_results`, status, token fingerprint, latency) and SLO dashboards (`search_remaining`, `incomplete_results_ratio`, `422/403+429` rates, p95 latency, shard coverage, geo unmatched rate).
- **FR-011**: System MUST enforce legal gates: API-only default, `robots.txt` check before any HTML fallback, deletion/GDPR purge window with email minimization, no spam/resale-of-PII use, resale/high-throughput subscription review, trademark-safe naming (no endorsement, no Invertocat).
- **FR-012**: System MUST support filter-spec v1 JSON upload (`POST /vsearch/run`), deterministic replay (`GET /vsearch/runs/{filter_hash}`), and run-bundle export (`GET .../export` with filters + metadata + raw upstream JSON); files are validated pre-execution, hold no tokens/state, and merge cross-device by immutable `id`.
- **FR-013**: System MUST serve a filter form at `GET /vsearch/` enumerating every parameter from the `06` matrix (searchable groups + virtual section, `props.*` gated on single-`org:`); form submissions and JSON uploads MUST converge on the identical validated filter-spec and identical results; the form MUST offer filter download-as-JSON.
- **FR-014**: System MUST schedule enrichment by cost order (record fields → single-call checks → batched multi-call enrichment), segment candidates by id range across tokens with survivors-first ordering, prefer zero-call mirrors (ecosyste.ms/deps.dev/geo-cache/blob-SHA) before GitHub calls, batch via GraphQL aliases + deps.dev/ecosyste.ms bulk endpoints, enrich lazily (visible page for interactive, full depth for exports), and report per-field source + calls spent in run metadata.
- **FR-015**: System MUST support versioned trace-pattern packs for agent detection (79-author + 93-file + 20-branch + 4-label + bots signatures for 63 agents, frozen before each run): fetch guidance-file *content* (not just existence), weight generic `AGENTS.md` down (19+ agents share it), exclude `CONVENTIONS.md`, and attribute matches per agent for preference ranking.
- **FR-016**: System MUST build windowed commit histories per repo (`since`/`until` on the study period) with messages, author/committer names + emails, trailers, and diffstat; filter merges/reverts/bumps + bots; compute adoption-date (earliest file-add OR AI-assisted commit) and commit ratios; BigQuery (`githubarchive.day.*`) primary, API top-up for gaps.
- **FR-017**: System MUST hydrate the PR channel per repo: head-branch regexes, label match, merge status/type mapping (merge/rebase/squash — squash = partial-AI imprecision), outcome (merged vs closed → acceptance rate, time-to-merge), iteration counts, revert detection, and the Codex rule (tag all PR-author commits inside an AI-PR as AI-assisted); GraphQL bulk fetch honoring the 10k PR cap.
- **FR-018**: System MUST build per-repo file inventories: file list via REST/tree *plus* `.gitignore` content scan (repos visible only via ignore still count), one-path-per-line text files, per-file first-added date / touching commits / current LOC, matched-file checkout for audit; two-clone strategy (HEAD shallow-no-checkout for files + windowed log-only history, no diffs).
- **FR-019**: System MUST link crates.io (package metadata, downloads, yanked, versions) and parse `Cargo.toml`/workspace structure per repo, including crate-hallucination checks (hyphen near-miss against the registry).
- **FR-020**: System MUST support snowball expansion for community sub-corpora: `user-search` seeding (`location:` + `language:`), followers/following frontier queue with depth + dedup, org-member expansion with multinational exclusion, bulk seed import, city-vs-language disambiguation (e.g. Cairo) with enforced `language:` filter, and pre-registered geographic fallback (e.g. Egypt → MENA) as run config.
- **FR-021**: System MUST support validation workflows: stratified random sampling (default n=400), double-label queue with Cohen's κ, Wilson CIs, Chapman capture-recapture over the commit ∩ file ∩ branch ∩ PR overlap matrix, and pre-registration freeze of packs + config before measurement runs.
- **FR-022**: System MUST support corpus-frame config in filter-spec: old/new bucket date boundaries (frozen-list date), attrition accounting (private/deleted/dotfiles drops expected ~2k at 128k scale), size splits (small/med/large), study-window bounds (e.g. drop pre-2025), and the ≥10-star Dabic frame as an option.
- **FR-023**: System MUST offer optional post-fetch cloning (never required): count selector (slider + digit input, top-N by current sort), shallow/file-only/windowed modes into `clones/{run_hash}/`, pre-confirm disk estimate with low-disk warning, resumable retries that skip completed repos, per-repo progress in run metadata.

### Key Entities

- **Repo**: immutable `id`, `node_id`, `full_name` (mutable) + history, owner ref, visibility/fork/archived/mirror/template flags, language/topics/license, counts (stars/forks/watchers/issues), size, `created_at`/`pushed_at`/`updated_at`, `custom_properties`, `deleted_at`. Relationships: one Owner (many repos), many Names (history), many Enrichments.
- **Owner**: `id`, `login`, `type` (User/Organization), `location` raw + resolved `{country_iso, confidence}` (cached by owner id), company/blog signals.
- **Shard**: dimension (`created:` range / `stars:` slice / org), state (`pending/active/done/incomplete`, within-run), `ran_at` snapshot (`max(pushed_at)` seen), order hint (no cadences), last `total_count`/fetched/`incomplete_results`.
- **AuditLog**: per-request record per FR-010; drives SLOs and incident forensics.
- **VirtualFilter**: named server-side filter (`min_stars`, `team_topic`, `has_dockerfile`, `owner_country`, …) with translation rule to legal upstream calls + post-filter.
- **FilterSpec**: versioned JSON doc (`gitcrawl_filter`, `q/sort/order`, `virtual`, `page`, optional `as_of`) — the portable unit of a search; no tokens/state.
- **RunBundle**: `{filter_hash, ran_at, api_version, total_count, incomplete_flags}` + raw upstream JSON results — the replication artifact; reruns diff against it to explain live drift.
- **TracePack**: versioned pattern set `{version, authors[], files[], branches[], labels[], bots[], generic_weights, exclusions[]}` — frozen before measurement runs; matches attribute per agent.
- **CorpusFrame**: `{buckets:{old_list_date, new_cutoff}, attrition_rules, size_splits, study_window, star_floor?}` — declares the sampling frame (e.g. old/new buckets, small/med/large splits) so adoption numbers are comparable across stacks.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Golden-org (`org:github`) discovery reaches `id`-set parity with an independent `since`-scan sample (dedupe by `id`, overlap windows) with zero cap-`422` escapes and every `>1000` query auto-sharded.
- **SC-002**: Every run reflects live `pushed_at` at `ran_at` (push/rename/delete fixtures handled within the run) while staying within 30 search req/min per token; zero background polling.
- **SC-003**: `GET /vsearch/repos` answers filtered queries (including `has_dockerfile` + `owner_country`) with correct post-filtering and geo confidence tiers on fixture data, with zero unknown params forwarded upstream.
- **SC-004**: In-run revalidation converts 100% of sampled renames to single-`id` history rows and 100% of sampled deletions to tombstones (no orphans, no silent drops).

## Assumptions

- Public GitHub (`api.github.com`) is the v1 target; GHES hosts are out of scope for v1 (version-skew matrix deferred, per `05` A5).
- **Serve is live-only (locked): every Find queries live GitHub at `ran_at` with minutes-TTL caching; the stored tables are the within-run working set + bundle source (upsert by `id`), not a background-maintained index. No hourly/weekly/daily jobs.** Auth day one is a personal PAT pool via env (`GITHUB_TOKEN` / `GITHUB_TOKENS`, never hardcoded; fingerprint-only logging; 1 PAT for dev/golden-org, 2+ for dev, 5–10 for 100k+ corpus runs); owned/consented only, never borrowed/shared; GitHub App installs deferred to scale-up.
- First build is a walking skeleton (one filter → live search → display → bundle + CSV) before corpus depth; thesis tracks defer to joint six-stack alignment.
- Tokens used are owned/consented (PATs and/or GitHub App installs); no borrowed/shared tokens; subscription review precedes any resale/high-throughput serving (ToS §H).
- `X-GitHub-Api-Version` pinned (e.g. `2022-11-28`) and re-verified quarterly; all numbers marked "as of 2026-09-29" are refreshed before capacity/cost commitments.
- Bulk-dump bootstrap (BQ/WoC/SWH) is an optional seed, not a v1 dependency; default bootstrap is `since`-enumeration + ecosyste.ms (zero-token metadata).
- Geo resolution targets country-level ISO only (no city lat/long product); unmatched bucket is an accepted outcome.

## Amendment v3 — US4 Operator Console (2026-10-01)

Adds **US4**: a local single-operator web console (FastAPI + Jinja2 + htmx, assets vendored, no Node at runtime) with run persistence (`runs`, `run_items`, `saved_filters`), a dashboard, the full filter form, run detail with sortable/paginated results, run history, run-to-run diff, saved filter library, optional clone control, keyboard-first navigation, and dark mode. This amendment also commits the delivery of US2 (within-run lifecycle) and US3 (enrichment + serve deck) that the console depends on. Success criteria **SC-005–SC-008** and the exact routes/DDL/behaviors are defined in `console-spec.md`; execution order is `console-plan.md` (batches B1–B10, tasks T052–T059 added alongside T019–T037/T051). Live-only freshness is unchanged: run history stores operator-triggered run artifacts, never a background index.
