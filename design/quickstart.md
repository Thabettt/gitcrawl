# Quickstart: Golden-Org Validation Runbook

**Date**: 2026-09-29 (updated 2026-10-07). Proves US1–US3 on fixtures/small scope before scaling tokens. Collect every output as review evidence. New? Read `how-the-data-flows.md` first — each step below maps to its stages.

**The one-paragraph version**: this runbook is how you convince yourself the system works before spending real API allowance. It walks the four layers in order — discovery completeness on a known org, lifecycle on fixtures, enrichment/serve/geo on fixtures, and the throttle classifier — and names the evidence to record at each step. If all four pass, you can scale tokens and shards with a clear conscience.

> **Note (2026-10-04)**: the original Step 0 walked a stdlib-only skeleton (`src/skeleton.py`). That file was retired under ruling R58 (dead code is a trap; the real loop replaced it). The current equivalent is: start the console, run a small Find, and check the bundle on disk — or simply run the golden-org test in Step 1, which exercises the same loop with assertions.

## Prerequisites

- Python 3.12+, Docker (Postgres 17 + Redis 7 via `docker-compose up -d`). PostgreSQL is the only supported store — the old SQLite skeleton/laptop path was retired with the skeleton. A project-local PG cluster works too (see `../docs/environment.md`).
- Owned PAT(s) via env (fine-grained, `metadata:read` baseline; no extra scopes for public). `X-GitHub-Api-Version: 2022-11-28` pinned. Pool: 1 PAT dev/golden-org, 5–10 for 100k+ runs.
- `export GITHUB_TOKEN=<owned-PAT>` (or `GITHUB_TOKENS=a,b,c` for pool; never commit, never hardcode, never log raw; token fingerprint only in audit).
- Legal pre-checks done (`../docs/legal-gates.md`): token owned/consented, `curl https://github.com/robots.txt` recorded (API-only; no HTML fallback if `/search` disallowed).

## Step 0 — First live run (replaces the retired skeleton, ~5 min)

```bash
# start the console (Postgres + Redis up, DATABASE_URL and GITHUB_TOKEN set)
PYTHONPATH=src python -m serve
# then open http://127.0.0.1:8000/ , run a small Find (e.g. language:rust stars:>100),
# and confirm runs/<filter_hash>/<run_id>/bundle.json + corpus.csv exist when it finishes.
```

Expected: the dashboard loads, `/health` shows database/Redis/token present, a Find reaches a terminal status while the run page shows live phase progress (phase, done/total, percent bar, ETA) with a **Stop search** button that leaves it resumable as "Stopped", and the bundle appears on disk. This is the same loop the original skeleton proved, now with the real pipeline.

## Step 1 — US1: golden-org discovery parity (15 min)

```bash
pytest tests/integration/test_golden_org.py -v
# Expected: PASS — org:github shards all <1000 fetchable, stored id-set diffs clean
# against an independent GET /repositories?since= sample; every >1000 query auto-sharded;
# the plan stays inside the query's own created: window when it has one (the date token is
# replaced, never appended — GitHub unions duplicate same-type qualifiers);
# typo probe (updated:>...) rejected local-400 with zero GitHub calls.
```

Evidence to record: shard table dump (`query, total_count, fetched, incomplete`) and the plan's `created:` ranges, `id`-set diff output, qualifier-400 log line.

## Step 2 — US2: live current-state + lifecycle on fixtures (10 min, no background jobs)

```bash
pytest tests/integration/test_lifecycle.py tests/unit/test_watermark.py -v
# Expected: PASS — push fixture records current pushed_at at ran_at (overlap pages deduped by id);
# rename fixture (301) yields one id + history row; delete fixture (404) yields tombstone.
```

Evidence: `ran_at` vs recorded `pushed_at`, history row count, `deleted_at` set with quarantine (serve excludes it).

## Step 3 — US3: enrich + serve + geo on 3 fixtures (10 min)

```bash
pytest tests/contract/test_vsearch_params.py tests/unit/test_trees_first.py tests/integration/test_geo_resolver.py -v
# Expected: PASS — unknown &foo= → own 400, never forwarded;
# has_dockerfile via trees (not contents loop); geo: "🇩🇪 Berlin"→{DE,exact-ISO},
# "Lagos"→{NG,gazetteer-city}, "🌍 remote"→{null,unmatched}.
# Scheduler spot-check: run metadata shows filters executed cheap-first with per-field
# source + calls spent (spot the zero-call mirror hits vs single-call vs batched rows).
# Hydration runs as bounded-concurrency GraphQL batches (owner ids via User/Organization
# inline fragments; language bytes and commit counts in the same query) — the bundle's
# field_stats.graphql should show batched requests with zero REST fallbacks on healthy repos.
curl "http://localhost:8000/vsearch/repos?q=org:github&has_dockerfile=true&owner_country=DE&per_page=5"
# Expected: 200 with items[] carrying enrichment + {country_iso, confidence, raw_location} per owner.
# Filter-spec portability: save the query as filter-spec v1 JSON, POST /vsearch/run, then
# GET /vsearch/runs/{filter_hash} on a second machine — identical validation + merged-by-id results;
# GET .../export yields the run bundle (filters + metadata + raw JSON).
# Form parity: open GET /vsearch/ in a browser, pick the same filters, submit —
# results identical to the JSON run above; "download as JSON" reproduces the file.
# Optional clone: set the Clone slider to 5, confirm the disk estimate —
# 5 repos land in clones/{run_hash}/, retry skips completed; zero clones also valid.
```

## Step 4 — Throttle classifier sanity (5 min)

```bash
pytest tests/unit/test_classifier.py tests/contract/test_github_pagination.py -v
# Expected: PASS — retry-after → exact sleep; remaining==0 → reset sleep (per-bucket);
# spam-422 backs off, cap-422 shards, validation-422 fails fast; Link followed verbatim.
```

## Done criteria

All four steps green + audit_log shows per-request fields (FR-010) + SLO query returns `incomplete_results_ratio`, `422/403+429` rates, geo-unmatched rate. Paste outputs into the review; then scale tokens/shards per `plan.md` Scale/Scope.

## Step 5 — Thesis tracks smoke (fixtures only, ~10 min) — DEFERRED (see tasks.md Phase 6)

```bash
pytest tests/unit/test_trace_packs.py -v
# Expected (when the track ships): frozen pack validates (generic AGENTS.md down-weighted,
# CONVENTIONS.md excluded), per-agent attribution correct on trace fixtures.
```

Evidence: frozen pack version recorded; corpus-frame config (`buckets`, `attrition: count`, `size_splits`, `study_window`) validates and round-trips through `POST /vsearch/run`. The full history/PR/inventory/crates/snowball/validation tracks (T040–T045) are parked; they exercise against fixtures when the thesis work resumes, not in this smoke pass.
