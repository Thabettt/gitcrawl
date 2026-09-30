# Quickstart: Golden-Org Validation Runbook

**Date**: 2026-09-29. Proves US1–US3 on fixtures/small scope before scaling tokens. Collect every output as review evidence. New? Read `how-the-data-flows.md` first — each step below maps to its stages.

## Prerequisites

- Python 3.12+, Docker (Postgres 17 + Redis 7 via `docker-compose up -d`; SQLite suffices for T000 skeleton/laptop), owned PAT(s) via env (fine-grained, `metadata:read` baseline; no extra scopes for public), `X-GitHub-Api-Version: 2022-11-28` pinned. Pool: 1 PAT dev/golden-org, 5–10 for 100k+ runs.
- `export GITHUB_TOKEN=<owned-PAT>` (or `GITHUB_TOKENS=a,b,c` for pool; never commit, never hardcode, never log raw; token fingerprint only in audit).
- Legal pre-checks done (`docs/legal-gates.md`): token owned/consented, `curl https://github.com/robots.txt` recorded (API-only; no HTML fallback if `/search` disallowed).

## Step 0 — Walking skeleton (locked first milestone, ~1 day)

```bash
python src/skeleton.py  # one hardcoded filter → live search → display → bundle JSON + CSV
# Expected: printed results + runs/{hash}/bundle.json + corpus.csv on disk, zero new concepts.
# Do this before Step 1; everything below layers depth onto this loop.
```

## Step 1 — US1: golden-org discovery parity (15 min)

```bash
pytest tests/integration/test_golden_org.py -v
# Expected: PASS — org:github shards all <1000 fetchable, stored id-set diffs clean
# against an independent GET /repositories?since= sample; every >1000 query auto-sharded;
# typo probe (updated:>...) rejected local-400 with zero GitHub calls.
```

Evidence to record: shard table dump (`query, total_count, fetched, incomplete`), `id`-set diff output, qualifier-400 log line.

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

All four steps green + audit_log shows per-request fields (FR-010) + SLO query returns `incomplete_results_ratio`, `422/403+429` rates, geo-unmatched rate. Paste outputs into the review; then scale tokens/shards per plan.md Scale/Scope.

## Step 5 — Thesis tracks smoke (fixtures only, ~10 min) — DEFERRED (see tasks.md Phase 6)

```bash
pytest tests/unit/test_trace_packs.py -v
# Expected: PASS — frozen pack validates (generic AGENTS.md down-weighted, CONVENTIONS.md excluded),
# per-agent attribution correct on trace fixtures.
```
Evidence: frozen pack version recorded; corpus-frame config (`buckets`, `attrition: count`, `size_splits`, `study_window`) validates and round-trips through `POST /vsearch/run`. Full history/PR/inventory/crates/snowball/validation tracks (T040–T045) exercise against fixtures next, not in this smoke pass.
