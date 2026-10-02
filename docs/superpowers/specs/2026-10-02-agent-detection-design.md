# Agent-Use Detection Flow — Design

**Date**: 2026-10-02
**Status**: Draft for review — plan only, not approved for execution
**Depends on**: US1 discovery, US2 lifecycle, US3 enrichment/serve, US4 console (all shipped, `001-gitcrawl`)
**Spec lineage**: FR-015 (trace packs), FR-016 (commit history), FR-017 (PR channel); this design is the API-only first tier of those tracks.

## 1. Context

The console already produces frozen corpus runs: a `find` run discovers repos live, stores them in `run_items`, and exports a bundle. Agent detection is Phase 6 (T039+) and currently unbuilt. This design adds a second run kind — `detect` — that runs the research papers' detection channels over a frozen corpus run, entirely through the GitHub API, with no clone and no BigQuery.

Decisions already made with the operator:

- v1 covers **API-only, all channels**: files (incl. content weighting), authors, branches, labels, bots, commit trailers, PR metadata. Deep history (windowed commits, adoption dates, per-file stats) is explicitly out of scope and documented as the next tier.
- Detection defaults to **all items of the source run**, with an optional top-N limit for pilot runs. No per-row manual selection.
- Results surface as **summary + per-repo matrix + evidence drill-down**, exportable.
- Architecture: **detection is a first-class run type** reusing the runs table, executor, limiter, history, export, and bundle machinery.

## 2. Goals / Non-goals

**Goals**

1. One action from a find run's detail page: "Detect coding agent use".
2. An options page: pack version, channel checkboxes with select-all, top-N limit, cost/time estimate.
3. A detection run that scans the frozen source-run items API-only and stores per-repo, per-agent, per-channel evidence keyed by pack version.
4. Summary page: repos scanned, % with any signal, per-agent and per-channel counts, per-repo agent matrix with evidence drill-down, CSV/JSON export.
5. Corpus building and detection stay separate: a detect run never calls discovery/search endpoints.
6. Documentation deliverable `docs/detection-tiers.md`: what is knowable API-only vs what requires clone/BigQuery.

**Non-goals (v1)**

- Windowed commit history, adoption dates, commit ratios, diffstats (FR-016 deep tier).
- Per-file first-added dates, LOC, two-clone inventory (FR-018).
- Full-history PR sweep beyond the GraphQL window (FR-017 deep tier).
- κ/adjudication validation UI (FR-021) — evidence is exported for it.
- Pack editing UI; packs are frozen files loaded read-only.
- Clone-based ground truth; `clones/` stays a manual/optional follow-up.

## 3. Invariants

1. **No discovery calls.** Detect runs call only per-repo REST/GraphQL endpoints. Enforced by a test that fails if any `search/*` or `repositories?since=` path is touched during a detect run.
2. **Sourced from frozen items.** Candidate membership comes from the source run's `run_items` snapshot, never a live re-query. Top-N selection is deterministic: order by `stargazers DESC, repo_id ASC`, take N. (Signal *content* is fetched live per repo at detect time and stamped with `detected_at`; corpus membership and signal freshness are separate concerns.)
3. **Versioned results.** Every evidence row carries `pack_version`; reruns with a new pack are new rows on a new run, never overwrites.
4. **Labelled depth.** Depth windows (last-100-commits, recent PRs) are stored in the run spec and shown in the UI; no shallow result is presented as a census.
5. **No silent partials.** Unavailable repos, truncated trees, GraphQL caps, and missing channels are counted and shown, never dropped.

## 4. Data model (migration `0008_detect_runs`, down_revision `0007`)

The repo's latest revision is `0007_run_items_order_idx`. `trace_packs` exists only in `data-model.md`, never in a migration — this migration creates it.

```sql
CREATE TABLE trace_packs (
  version         TEXT PRIMARY KEY,
  authors         JSONB NOT NULL,
  files           JSONB NOT NULL,
  branches        JSONB NOT NULL,
  labels          JSONB NOT NULL,
  bots            JSONB NOT NULL DEFAULT '[]',
  generic_weights JSONB NOT NULL DEFAULT '{}',
  exclusions      JSONB NOT NULL DEFAULT '["CONVENTIONS.md"]',
  frozen_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE runs
  ADD COLUMN kind TEXT NOT NULL DEFAULT 'find' CHECK (kind IN ('find','detect')),
  ADD COLUMN source_run_id BIGINT NULL REFERENCES runs(id),
  ADD COLUMN result_summary JSONB NULL;      -- detect: per-agent/per-channel counts at finish
CREATE INDEX runs_kind_idx ON runs (kind, created_at DESC);
CREATE INDEX runs_source_idx ON runs (source_run_id) WHERE source_run_id IS NOT NULL;

ALTER TABLE run_items
  ADD COLUMN detection JSONB NULL;           -- detect runs: per-repo rollup
                                             -- {agents:[...], channels:{...}, unavailable:bool, notes:[...]}

CREATE TABLE detection_evidence (
  id            BIGSERIAL PRIMARY KEY,
  run_id        BIGINT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  repo_id       BIGINT NOT NULL REFERENCES repos(id),
  agent         TEXT NOT NULL,               -- canonical tool id from tools.csv
  channel       TEXT NOT NULL CHECK (channel IN
                  ('file','author','branch','label','bot','commit_trailer','pr_metadata')),
  evidence      JSONB NOT NULL,              -- {path, sha, commit, branch, label, pr, snippet, ...}
  pack_version  TEXT NOT NULL REFERENCES trace_packs(version),
  detected_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX de_run_repo_idx ON detection_evidence (run_id, repo_id);
CREATE INDEX de_run_agent_idx ON detection_evidence (run_id, agent);
CREATE INDEX de_run_channel_idx ON detection_evidence (run_id, channel);
CREATE INDEX de_repo_idx ON detection_evidence (repo_id);
```

- `trace_packs` is created in this migration (see above).
- For detect runs, `runs.filter_spec` holds the detection spec and `filter_hash` is its sha256, so replay/diff/export reuse existing machinery:
  `{kind:"detect", source_run_id, pack_version, channels:[...], limit:null|N, windows:{commits:100, prs:100}, base_filter_hash}`.
- Detect-spec validation lives in a new `src/serve/detect_spec.py`, mirroring `src/serve/filter_spec.py` (local `400` + hint before enqueue).
- A detect run materializes one `run_items` row per scanned repo (same snapshot semantics as find), with `detection` rollup and `virtuals` left as-is.
- Summary counts live in `runs.result_summary` at finish (fast pages); raw truth remains the evidence table.
- Source find runs are never mutated.

## 5. Pack management

- Source of truth for patterns: `labri-progress/agent-impact` `config/patterns/{authors,files,branches,labels,tools,bots}.csv` (public, verified 2026-10-02; documented in the thesis repo `research-notes.md`). Schema: pattern files are `pattern,type,subtype` where `subtype` is the tool label; `tools.csv` is `name,url`. Extra files (`counts.csv`, `rg_patterns.csv`, `top-orgs.csv`) are ignored.
- Matching fidelity: every pattern is a Python regex, case-sensitive, matched as the replication package's `code/heuristics.py` does — one combined alternation per channel with a named group per `subtype`; file patterns get bare dots escaped and a `(?:^|/)` anchor.
- `bots.csv` is **generic CI-bot noise** (dependabot, renovate, `[bot]` accounts) used for exclusion/QA evidence — it is not agent detection. Agent bot accounts (`copilot-swe-agent`, `devin-ai-integration`, `factory-droid[bot]`, …) are rows in `authors.csv`. Bot-channel hits are stored but never counted as adoption.
- Import converts the CSVs unchanged into a vendored `packs/patterns/` snapshot and a versioned pack file `packs/<YYYY-MM-DD>-<sha8>.json` with `{version, agents(name→url), authors[], files[], branches[], labels[], bots[], generic_weights, exclusions}`; `generic_weights` defaults to down-weighting shared `AGENTS.md` (whose subtype is `Generic`, so it is flagged rather than attributed to a specific agent); `exclusions` defaults to `["CONVENTIONS.md"]`.
- A freeze command validates the pack and upserts it into `trace_packs`; packs are immutable once frozen. The UI offers a read-only dropdown of frozen versions; default = latest.
- Attribution: a match maps to the `subtype` recorded by the replication package; per-agent evidence rows are written (one row per agent per signal).

## 6. Components, channels and API mapping

New code:

- `src/detect/packs.py` — CSV import, pack build/validate/freeze/load.
- `src/detect/channels.py` — one detector per channel; pattern matching only.
- `src/detect/attribution.py` — pattern→agent mapping, generic weights, exclusions, evidence dedupe.
- `src/detect/estimate.py` — calls/time estimate from channels × candidates × token count.
- `src/detect/orchestrator.py` — candidates from source `run_items`, cost-ordered channel execution, token-lane segmentation, rollup + `result_summary`, bundle/export writing.
- `src/serve/detect.py` + templates — routes and detect-kind run detail page.
- `src/limiter/buckets.py` — adds a `"graphql": (5000, 3600.0)` point bucket (REST buckets today are `search`/`core`/`code_search`; GraphQL has its own GitHub limit and must not share `core`).

Channels:

| Channel | Source call | Extra calls/repo | Depth |
|---|---|---|---|
| `file` | `GET /repos/{o}/{r}/git/trees/HEAD?recursive=1` + `GET .../contents/.gitignore` (the paper's "visible only via ignore" signal) | 2 | full tree + ignore file |
| `author` | GraphQL `history(first:100)` author name/email | shares 1 GraphQL call | last 100 commits (labelled) |
| `branch` | `GET /repos/{o}/{r}/branches?per_page=100` | 1 (paginate if >100) | all branches |
| `label` | `GET /repos/{o}/{r}/labels?per_page=100` | 1 | all labels |
| `bot` | `GET /repos/{o}/{r}/contributors?per_page=100` + author identities from history | 1 | generic CI-bot evidence only; excluded from adoption counts |
| `commit_trailer` | GraphQL `history(first:100)` messages | shares 1 GraphQL call | last 100 commits (labelled) |
| `pr_metadata` | GraphQL recent PRs (labels, headRefName, body snippet) + Codex body marker | 1–2 | recent window, default 100 PRs (labelled) |

- Full selection ≈ **6–7 core calls + 1 GraphQL call per repo** (GraphQL cost ≈ 1 point with shallow `first:100`).
- ETag/conditional requests reused from hydration where available; `304` responses do not consume quota.
- Ordering: cheapest channels first within a repo so a rate-limited run still yields partial signal; repos processed in survivor-first order (`stargazers DESC`) across token-lane segments, reusing the scheduler segmentation pattern.
- Rate limit: existing Redis buckets for REST (`search`/`core`/`code_search`) plus the new `graphql` point bucket, per-endpoint concurrency cap, existing retry/backoff classifier. Token pool from `GITHUB_TOKENS` as today.

## 7. Routes and UI

New routes (all reuse CSRF/validation patterns):

| Method | Path | Purpose |
|---|---|---|
| GET | `/runs/{id}/detect` | Options page for a finished find run |
| POST | `/runs/{id}/detect` | Validate, create detect run, enqueue, 303 → `/runs/{detect_id}` |
| GET | `/runs/{id}/detect-estimate?channels=&limit=` | htmx: calls/time estimate for current options |
| GET | `/partials/runs/{id}/detect-status` | htmx progress poll (repos scanned, signals, failures) |
| GET | `/runs/{id}/export?format=json\|csv` | Detect runs: evidence CSV/JSON + bundle |

- **Find run detail** gains a "Detect coding agent use" button (enabled when status `done`/`partial`).
- **Options page**: pack version dropdown; 7 channel checkboxes with descriptions and calls/repo; "Select all" (default on); top-N slider+digit (default all); live estimate panel (calls, wall-clock at current token count, disk); start button.
- **Detect run detail** (run detail branches on `kind`): status banner; summary cards (repos scanned, % any signal, per-agent top-N counts, per-channel counts, pack version, depth windows); per-repo table (agent chips per row) with expandable evidence grouped by channel; filters (has-signal, agent, channel); export.
- **Find run table**: agent badge column showing signal count from the latest detect run over that source run (cheap join), linking to it.
- **History**: kind badge on `/runs`; replay uses the stored detect spec; diff between two detect runs over the same source run is a later task (v1.1) — diff for find runs is unchanged.
- **Docs link**: options page links to `docs/detection-tiers.md`.

## 8. Evidence and attribution rules

- One evidence row per `(repo, agent, channel, signal)`; identical signals dedupe.
- File matching is regex-based over tree paths plus `.gitignore` lines; `AGENTS.md` (subtype `Generic`) is flagged with its weight, `CONVENTIONS.md` is excluded; generic hits never inflate a specific agent's count.
- Commit-trailer matching: co-author trailers and "generated with" footers in the history window, using the replication package's author regexes (case-sensitive).
- PR matching: head-branch regex, label regex, and the hardcoded Codex body marker `https://chatgpt.com/codex/tasks`.
- Generic CI bots (`bots.csv`) are stored as channel `bot` evidence for QA only and are excluded from `result_summary.agents` and per-repo agent lists.
- v1 stores which channel(s) fired and the raw evidence; it does not compute a probabilistic confidence. Papers' baseline (~0.5–1% FP, ~3% borderline) is stated in docs and exports.
- Disabled/opt-out agents (Claude opt-out, Pi/OpenCode unsigned) mean detection is an undercount; stated in the UI and export metadata, never silently corrected.

## 9. Error handling

- Repo `404`/private → `detection.unavailable=true`, counted, no evidence; no tombstone changes (source semantics untouched).
- Tree truncated (>100k entries/7 MB) → fall back to per-matched-path `contents` probes; if still incomplete, mark `partial` with reason.
- GraphQL resource-cap partials/timeouts → split or narrow once, then mark channel partial; never retry the same shape.
- `401`/credential lockout → loud fail (existing token incident behavior). `429/403` → existing limiter classifier.
- Missing/failed pack, unknown agent mapping, or empty source run → local `400` with hint before enqueue.
- Any channel failure degrades the run to `partial` with per-channel failure counters in `result_summary`.

## 10. Documentation deliverable

`docs/detection-tiers.md`: table of every signal (7 channels) × tier:

1. **API-only, full** — files (existence+content), branches, labels.
2. **API-only, windowed** — authors, commit trailers, PR metadata, bots (presence).
3. **Clone required** — diffstats, per-file first-added/LOC, ground-truth content audit, `.gitignore`-only visibility scans.
4. **BigQuery required** — full windowed commit history beyond `first:100`, adoption dates/ratios, 2025+ PR bodies at scale.

Each row states call cost, freshness, and what it cannot tell you. Linked from the options page and referenced by `research.md` D13.

## 11. Testing

- **Unit**: per-channel pattern matching from pack fixtures; attribution weights/exclusions; deterministic top-N selection; estimate math; dedupe.
- **Contract**: every new route (status codes, hints, CSRF, 303); `kind` enforcement (detect needs `source_run_id`); export headers/shapes; migration `0008` up/down.
- **Integration (offline fixtures)**: repo with `CLAUDE.md` + Claude trailer + `cursor/` branch → expected agent set and evidence; 404 repo counted unavailable; tree-truncation fallback; GraphQL cap → partial; retry via limiter fake.
- **Invariant**: detect run over a seeded source run makes zero discovery/search calls (spy on the HTTP client).
- **Golden**: detection summary + evidence CSV snapshot.
- **Live smoke (token-guarded, optional)**: 3-repo fixture org, all channels, one pack.
- Coverage floor stays 93.

## 12. Success criteria

1. A 10k-item Rust find run can be detected API-only, all channels, producing summary + per-repo matrix + evidence export, with zero discovery calls and zero unlabelled partials.
2. Same source run + same pack + same options reproduces the same evidence set on rerun (modulo recorded live drift: ETag/`fetched_at` differences are reported).
3. `docs/detection-tiers.md` shipped and linked from the options page.
4. Top-N pilot (e.g. 200 repos) completes and estimates match actuals within a stated tolerance.

## 13. Assumptions / open items

- Pattern CSVs are public and verified (2026-10-02, `labri-progress/agent-impact`); the import vendors a snapshot under `packs/patterns/` and records the pack digest.
- GraphQL `history(first:100)` is enough for the pilot; deeper history is the documented next tier.
- Contributors top-100 is presence-only and does not prove absence of a bot.
- PR window default 100; the 10k cap applies only if the window is widened later.
- Detect-run diff is v1.1.
