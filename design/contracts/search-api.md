# Contract: `GET /vsearch/repos` (gitcrawl serve API)

**Date**: 2026-09-29 (updated 2026-10-07). Upstream: `GET /search/repositories` (allowlist enforced — unknown params never forwarded). Versioned with `X-GitHub-Api-Version` pinned server-side. Narrative version of this contract: `../how-the-data-flows.md` (Stages 0–1, 8–9).

**The one-paragraph version**: this is the promise gitcrawl makes to anyone calling its serve API. You may send the upstream search parameters plus gitcrawl's own "virtual" filters; anything unknown is rejected locally with a helpful `400` instead of being forwarded to GitHub (where it would be silently ignored). Responses look like GitHub's shape — `total_count` + `items[]` — but the counts are exact, each owner carries a country with a confidence tier, and any incompleteness is labeled. The rest of this file is the precise table of what's accepted, what it translates to, and what comes back.

## Request

```
GET /vsearch/repos?q={query}&sort={sort}&order={order}&per_page={n}&page={n}
  &min_stars={n}&team_topic={topic}&has_dockerfile={bool}&owner_country={ISO2}
  &min_geo_confidence={tier}&min_commits={n}&max_commits={n}
  &min_language_bytes={n}&max_language_bytes={n}&min_loc={n}&max_loc={n}
```

| Param | Type | Rules |
|---|---|---|
| `q` | string, optional (default `""`) | Validated against `06 §2` allowlist; `props.*` requires single-`org:` in `q`; typos → `400` (never forwarded) |
| `sort` | enum, optional | `stars`/`forks`/`help-wanted-issues`/`updated` only (upstream set); omitted = best-match |
| `order` | enum, optional | `desc` (default)/`asc`; ignored without `sort` (mirrors upstream) |
| `per_page` | int, optional | 1–100, default 20 (serve-side; upstream always fetched at 100 then sliced) |
| `page` | int, optional | ≥1, default 1 (serve-side over stored rows; unbounded by upstream 1000-cap) |
| `min_stars` | int ≥ 0, optional | Virtual → appends `stars:>=N` to upstream discovery; post-filters stored `stargazers` |
| `team_topic` | topic slug, optional | Virtual → appends `topic:{value}` upstream + post-filter |
| `has_dockerfile` | bool, optional | Virtual → post-filter via stored trees/metafiles (`true` = Dockerfile present at default branch) |
| `owner_country` | ISO-3166-1 alpha-2, optional | Virtual → post-filter on stored `owners.country_iso`; implies `min_geo_confidence` default (gazetteer and above) |
| `min_geo_confidence` | enum: `exact-iso` \| `name` \| `gazetteer-city` \| `geocoder` \| `weak`, optional (default `gazetteer-city`) | Confidence floor for `owner_country` |
| `min_commits` / `max_commits` | int ≥ 0, optional | Virtual → post-filter on hydrated default-branch commit count |
| `min_language_bytes` / `max_language_bytes` | int ≥ 0, optional | Virtual → post-filter on the primary language's byte size captured during hydration (GraphQL `languages(first: 10)`; no extra call per repo) |
| `min_loc` / `max_loc` | int ≥ 0, optional | **Accepted and recorded, not yet enforced** — using them flags the run incomplete (R44). A no-clone estimate tier is designed in `../loc-dilemma.md`; until it ships, the UI renders these fields disabled |
| future virtuals | — | Added only with translation rule + tests here; upstream allowlist NEVER extended ad hoc |

## Response `200`

```json
{
  "total_count": 123,
  "items": [
    {
      "id": 1296269,
      "full_name": "octocat/Hello-World",
      "description": "...",
      "language": "Ruby",
      "license_spdx": "MIT",
      "topics": ["octocat"],
      "stargazers": 80, "forks_count": 9, "open_issues": 0,
      "pushed_at": "2011-01-26T19:06:43Z",
      "owner": {
        "login": "octocat", "type": "User",
        "country_iso": "DE", "geo_confidence": "gazetteer-city",
        "raw_location": "Berlin, Germany"
      },
      "has_dockerfile": true
    }
  ]
}
```

Envelope mirrors upstream shape (`total_count` + `items[]`) but counts are exact (local DB), and each owner carries `{country_iso, confidence, raw_location}`.

## Errors (never leaked upstream bodies)

| Status | When | Body |
|---|---|---|
| `400` | Unknown top-level param, unknown qualifier/typo, bad `sort`, `per_page`/`page` out of range, bad ISO code | `{error:"invalid_param", param, hint}` with fix hint (e.g. `updated:` → use `pushed:` + `sort=updated`) |
| `502` | Upstream fetch needed and GitHub errors (throttle/exhaustion/outage) | `{error:"upstream_unavailable", retry_after_ms}` (server already backed off per classifier) |
| `200` + `incomplete:true` | Shard backing this query marked incomplete | `{total_count, incomplete:true, items}` — partial with provenance, never silent |

## Translation table (server-side, tested in `test_vsearch_params.py`)

| Virtual | Upstream discovery | Post-filter (stored) |
|---|---|---|
| `min_stars=N` | `stars:>=N` appended to shard queries | `stargazers >= N` |
| `team_topic=T` | `topic:T` appended | `T IN topics` |
| `has_dockerfile=true/false` | none (broad discovery) | trees/metafiles presence flag |
| `owner_country=CC` | none (broad discovery) | `owners.country_iso = CC` (+ confidence ≥ threshold) |
| `min_geo_confidence=T` | none | confidence rank ≥ `T` |
| `min_commits=N` / `max_commits=N` | none | default-branch commit count compared |
| `min_language_bytes=N` / `max_language_bytes=N` | none | primary-language bytes compared (captured during hydration) |
| `min_loc=N` / `max_loc=N` | none | recorded only (enforcement pending `../loc-dilemma.md`) |

## Upstream behavior this contract relies on (verified 2026-10-06/07)

- **Duplicate same-type qualifiers union.** GitHub treats two `created:` qualifiers as a union, not an intersection and not first-wins. The shard planner therefore *replaces* the existing `created:` token when it slices a query (`replace_created`), planning only inside the user's window; appending a second `created:` would silently widen the shard.
- **1,000-result cap per query.** Repository search returns at most 1,000 results; page 11 at `per_page=100` returns `422 "Only the first 1000 search results are available"`. `total_count` can exceed 1,000 and drifts over hours (38,820 → 38,823 → 38,833 within ~5 h for one unchanged query), so discovery shards by creation date and treats counts as approximate.
- **Forks excluded by default.** Repository search omits forks unless `fork:true` (include) or `fork:only` (restrict) is given.
- **Hydration is batched GraphQL.** Details, per-language bytes (`languages(first: 10)`), and default-branch commit counts arrive in the same batched query (≤ `graphql_batch_size` aliases per request; the bound is 1–20 and env overrides clamp to it). REST remains the fallback. Hydration concurrency is `min(limiter_max_concurrent, 20)`.
- **Search pagination has no stability guarantee.** Identical paginated requests can shift or skip items, which explains small cross-run corpus deltas; the exported run bundle (filters + raw items + field stats) is the reproducible artifact.

## Invariants

- **Serve is live-only (locked decision).** Every Find queries live GitHub at `ran_at`; results cache briefly under `filter_hash` (minutes TTL) so repeats cost zero calls. The stored tables are the *within-run working set* (upsert by `id` for dedupe + bundle source), not a background-maintained index. Upstream allowlist (`q,sort,order,per_page,page`) is closed: any other key destined upstream is a bug; virtuals translate, never forward.
- Export is JSON + CSV always (`GET .../export`): raw upstream JSON per repo for replication, flat CSV for spreadsheets — thesis-ready by default.
- Every call emits the FR-010 audit record (`quickstart.md` Step 3 asserts it).

## Filter input: UI form (primary) + JSON upload (optional alternative)

Picking filters has two doors into the same pipeline — both converge on one validated filter-spec before anything runs:

- **UI form (primary): `GET /vsearch/`** — a server-rendered filter page enumerating every parameter from the `06` matrix, grouped: keywords + `in:` scope; owner (`user:`/`org:`/`repo:`); counts (`stars/forks/size/followers/topics` with comparator + range widgets); dates (`created`/`pushed` with presets + custom ranges); meta (`language` datalist, `topic:`, `license`); flags (`fork/archived/mirror/template/visibility/sponsorable/funding-file/issue-label counts`); `props.*` (enabled only when a single `org:` is set, per the single-org rule); sort/order/page; and the virtual section (`min_stars`, `team_topic`, `has_dockerfile`, `min_commits`/`max_commits`, `owner_country` + confidence threshold, and the disabled `min_loc`/`max_loc` pair). Submitting builds the identical filter-spec the JSON path produces and runs it. No separate frontend project — one template, laptop-native.
- **JSON upload (optional): `POST /vsearch/run`** — for replication across devices, in the JSON format below. Same validation, same run, same bundle; the form even offers "download these filters as JSON" so any UI search becomes a shareable file. The endpoint takes the document as a **JSON body**; the browser's file-upload door is the `/find` page.

### Filter-spec v1 JSON (the upload format)

No built-in preset pack in v1. Instead, any search is fully described by a versioned JSON doc that can be uploaded on any device. The file holds filters only — never tokens or local state.

```json
{
  "gitcrawl_filter": 1,
  "q": "language:rust fork:false pushed:>2024-09-29",
  "sort": "stars", "order": "desc",
  "virtual": { "min_commits": 100, "min_loc": 5000, "has_dockerfile": false, "owner_country": null },
  "page": { "per_page": 100, "max_pages": 10 }
}
```

- `POST /vsearch/run` accepts the doc (JSON body), validates against the allowlist + virtual table (typos → `400` with hints, never forwarded), executes, and returns `{filter_hash, ran_at, api_version, total_count, incomplete, items}`.
- `GET /vsearch/runs/{filter_hash}` replays the identical query on any device (tokens stay per-device; results merge by immutable `id`, dedupe-safe).
- Honest replication: GitHub is live, so reruns may drift (stars move, repos vanish). Every run records `{filter_hash, ran_at, api_version, total_count, incomplete_flags}`; the doc may pin `"as_of"` for audit diffing. Byte-identical reproduction comes from the exported run bundle (filters + metadata + raw upstream JSON), which doubles as the thesis replication-package artifact.
- `GET /vsearch/runs/{filter_hash}/export` downloads the run bundle (raw JSON + CSV).

### Clone action (optional, post-fetch)

Fetching never clones. After results display, a **Clone** control offers a slider + numeric input for how many of the found repos to clone (default: top-N by current sort; options: shallow `--depth 1`, `--no-checkout` file-only, or windowed-log mode). A disk estimate previews before confirming (per-repo `size_kb` sum × depth factor).

> **Route note (R54)**: the canonical clone routes are run-scoped — `GET /runs/{id}/clone-estimate?limit=N&mode=shallow` and `POST /runs/{id}/clone` — not the earlier filter-hash variant. The console links to them from the run page; zero clones is always valid.

- `POST /runs/{id}/clone` with `{limit, mode}` → clones into `clones/{filter_hash}/{run_id}/{owner}__{repo}/`, resumable (completed repos skipped on retry), progress per repo in run metadata.
- UI: slider (1 … result count) synced with a digit input; mode radio; estimate readout; progress bar per batch.

### Corpus-frame config (filter-spec extension, FR-022)

```json
{
  "gitcrawl_filter": 1,
  "q": "language:rust fork:false pushed:>2024-09-29",
  "virtual": { "min_commits": 100, "min_loc": 5000 },
  "frame": {
    "buckets": { "frozen_list_date": "2025-08-29", "new_cutoff": "2025-08-29" },
    "attrition": "count",
    "size_splits": ["small", "medium", "large"],
    "study_window": { "from": "2025-01-01", "to": null },
    "star_floor": 10
  }
}
```

`buckets` declares old/new sampling frames; `attrition: "count"` records private/deleted/dotfiles drops instead of hiding them; `size_splits` tags every repo; `study_window` bounds commit history; `star_floor` is optional (Dabic frame). All fields optional; all recorded verbatim in the run bundle for cross-stack comparison. Frame config is accepted and recorded now; the downstream thesis tracks that consume it are parked (see `../tasks.md` Phase 6).

## Console routes (US4)

The operator console adds server-rendered pages and run endpoints on top of this JSON contract — full route table (dashboard, `/find`, `/runs`, run detail/export/replay, diff, `/filters`, htmx partials, `/health`) and their rules live in `../console-spec.md`. This file remains the authority for `GET /vsearch/repos`, `POST /vsearch/run`, and `GET /vsearch/runs/{filter_hash}`.
