# Contract: `GET /vsearch/repos` (gitcrawl serve API)

**Date**: 2026-09-29. Upstream: `GET /search/repositories` (allowlist enforced — unknown params never forwarded). Versioned with `X-GitHub-Api-Version` pinned server-side. Narrative version of this contract: `../how-the-data-flows.md` (Stages 0–1, 8–9).

## Request

```
GET /vsearch/repos?q={query}&sort={sort}&order={order}&per_page={n}&page={n}
  &min_stars={n}&team_topic={topic}&has_dockerfile={bool}&owner_country={ISO2}
  [&<future-virtual>=...]
```

| Param | Type | Rules |
|---|---|---|
| `q` | string, optional (default `""`) | Validated against `06 §2` allowlist; `props.*` requires single-`org:` in `q`; typos → `400` (never forwarded) |
| `sort` | enum, optional | `stars`/`forks`/`help-wanted-issues`/`updated` only (upstream set); omitted = best-match |
| `order` | enum, optional | `desc` (default)/`asc`; ignored without `sort` (mirrors upstream) |
| `per_page` | int, optional | 1–100, default 20 (serve-side; upstream always fetched at 100 then sliced) |
| `page` | int, optional | ≥1, default 1 (serve-side over stored rows; unbounded by upstream 1000-cap) |
| `min_stars` | int, optional | Virtual → appends `stars:>=N` to upstream discovery; post-filters stored `stargazers` |
| `team_topic` | string, optional | Virtual → appends `topic:{value}` upstream + post-filter |
| `has_dockerfile` | bool, optional | Virtual → post-filter via stored trees/metafiles (`true` = Dockerfile present at default branch) |
| `owner_country` | ISO-3166-1 alpha-2, optional | Virtual → post-filter on stored `owners.country_iso`; optional `&min_geo_confidence=` threshold (default: gazetteer and above) |
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

## Invariants

- **Serve is live-only (locked decision).** Every Find queries live GitHub at `ran_at`; results cache briefly under `filter_hash` (minutes TTL) so repeats cost zero calls. The stored tables are the *within-run working set* (upsert by `id` for dedupe + bundle source), not a background-maintained index. Upstream allowlist (`q,sort,order,per_page,page`) is closed: any other key destined upstream is a bug; virtuals translate, never forward.
- Export is JSON + CSV always (`GET .../export`): raw upstream JSON per repo for replication, flat CSV for spreadsheets — thesis-ready by default.
- Every call emits the FR-010 audit record (`quickstart.md` Step 3 asserts it).

## Filter input: UI form (primary) + JSON upload (optional alternative)

Picking filters has two doors into the same pipeline — both converge on one validated filter-spec before anything runs:

- **UI form (primary): `GET /vsearch/`** — a server-rendered filter page enumerating every parameter from the `06` matrix, grouped: keywords + `in:` scope; owner (`user:`/`org:`/`repo:`); counts (`stars/forks/size/followers/topics` with comparator + range widgets); dates (`created`/`pushed` with presets + custom ranges); meta (`language` datalist, `topic:`, `license`); flags (`fork/archived/mirror/template/visibility/sponsorable/funding-file/issue-label counts`); `props.*` (enabled only when a single `org:` is set, per the single-org rule); sort/order/page; and the virtual section (`min_stars`, `team_topic`, `has_dockerfile`, `min_commits`, `min_loc`, `owner_country` + confidence threshold). Submitting builds the identical filter-spec the JSON path produces and runs it. No separate frontend project — one template, laptop-native.
- **JSON upload (optional): `POST /vsearch/run`** — for replication across devices, in the JSON format below. Same validation, same run, same bundle; the form even offers "download these filters as JSON" so any UI search becomes a shareable file.

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

- `POST /vsearch/run` accepts the doc (body or file upload), validates against the allowlist + virtual table (typos → `400` with hints, never forwarded), executes, and returns `{filter_hash, ran_at, api_version, total_count, incomplete, items}`.
- `GET /vsearch/runs/{filter_hash}` replays the identical query on any device (tokens stay per-device; results merge by immutable `id`, dedupe-safe).
- Honest replication: GitHub is live, so reruns may drift (stars move, repos vanish). Every run records `{filter_hash, ran_at, api_version, total_count, incomplete_flags}`; the doc may pin `"as_of"` for audit diffing. Byte-identical reproduction comes from the exported run bundle (filters + metadata + raw upstream JSON), which doubles as the thesis replication-package artifact.
- `GET /vsearch/runs/{filter_hash}/export` downloads the run bundle (raw JSON + CSV).

### Clone action (optional, post-fetch)

Fetching never clones. After results display, a **Clone** control offers a slider + numeric input for how many of the found repos to clone (default: top-N by current sort; options: shallow `--depth 1`, `--no-checkout` file-only, or windowed-log mode). A disk estimate previews before confirming (per-repo `size_kb` sum × depth factor).

- `GET /vsearch/runs/{filter_hash}/clone-estimate?limit=N&mode=shallow` → `{repos, estimated_mb, warnings[]}` (warns on laptop disk < threshold).
- `POST /vsearch/runs/{filter_hash}/clone` with `{limit, mode, paths}` → clones into `clones/{run_hash}/{owner}__{repo}/`, resumable (completed repos skipped on retry), progress per repo in run metadata. Zero clones is always valid — the button is never required.
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

`buckets` declares old/new sampling frames; `attrition: "count"` records private/deleted/dotfiles drops instead of hiding them; `size_splits` tags every repo; `study_window` bounds commit history; `star_floor` is optional (Dabic frame). All fields optional; all recorded verbatim in the run bundle for cross-stack comparison.

## Console routes (US4)

The operator console adds server-rendered pages and run endpoints on top of this JSON contract — full route table (dashboard, `/find`, `/runs`, run detail/export/replay, diff, `/filters`, htmx partials, `/health`) and their rules live in `../console-spec.md`. This file remains the authority for `GET /vsearch/repos`, `POST /vsearch/run`, and `GET /vsearch/runs/{filter_hash}`.
