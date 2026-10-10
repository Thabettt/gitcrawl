# Adaptive rate controller soak validation (Task 4, plan 2026-10-09)

Live soak of the env-gated adaptive rate controller (`GITCRAWL_ADAPTIVE=1`) on the profiled
corpus filter, measured **2026-10-10** on branch `perf/runtime-adaptive-control` at `65d0b4c`
(Tasks 1-3 complete; full suite 1,555 passed at this commit, not re-run per the task brief).
The run used one personal token (`GITHUB_TOKEN`) against `api.github.com`, local PostgreSQL
(`localhost:5433`) and local Redis (`localhost:6379`). No source changes were made for this
task; the only artifacts are this note and an uncommitted scratch script in the opencode temp
directory. No secrets appear in this note or in any captured output (token fingerprints only
in the existing audit columns).

- Filter: `language:rust stars:>=4 created:>=2025-02-24` (the profiled corpus filter, ~39k repos).
- Trigger mode (deviation, controller-ruled): runs were executed **directly in-process through
  `serve.runner.run_filter`** from a scratch script at
  `C:\Users\Abdul\AppData\Local\Temp\opencode\soak\soak_run.py` (not committed), not through the
  console/server. The code path is identical (`run_filter` -> `_hydrate` ->
  `refresh_repos_batched` -> `fetch_batch(..., adaptive=...)`); `os.environ["GITCRAWL_ADAPTIVE"]`
  was set to `"1"` inside the script before the run, so no server restart was needed.
- Corpus preset applied via `update_run_settings(engine, parse_settings_form({"preset": "corpus"}))`
  and recorded: `limiter_max_concurrent` changed 17 -> 32; `max_shards=1000`,
  `max_candidates=100000`, `max_hydrate=100000`, `max_enrich=100000`,
  `request_deadline_seconds=86400`, `graphql_batch_size=29`, `graphql_batch=true`,
  `discovery_concurrency=32` were already at corpus values. `RunnerConfig.concurrency` resolved to
  32, so the controller envelope was `min(MAX_WINDOW=48, 32)=32`, initial window 20, initial
  batch `min(29, MAX_BATCH)=29`.
- Environment prerequisite (deviation note): the dev database was at alembic `0013` while HEAD
  writes the `0014` audit columns (`rl_used`, `run_id`, `phase`); the documented
  `alembic upgrade head` was applied before the run so audit telemetry could be recorded. No
  source files were touched.

## Run A (single run)

Raw payload: `C:\Users\Abdul\AppData\Local\Temp\opencode\soak\soak_A_payload.json`
(summary: `soak_A_summary.json` in the same directory).

| Field | Value |
|---|---|
| Started / ended (UTC) | 2026-10-10T15:59:03Z / 2026-10-10T16:19:57Z |
| Wall time | **1,254.1 s (20.9 min)** |
| Total count / payload items | 39,418 / 39,418 |
| Points spent | **2,898** (count 1, discovery 510, hydration 2,387) |
| GitHub rate-limit window | 4,976 remaining at start, reset 16:10:55Z (mid-run) |
| Remaining after | 4,156 (post-reset window; `remaining` never went below 3,206) |
| `incomplete` | `false` |
| Warnings | **none** (`warnings: []`) |

### Stage timings (`payload.timings`)

| Stage | Seconds | Detail |
|---|---|---|
| count | 0.9 | `total_count=39,418` |
| discovery | 111.1 | 510 GraphQL search/count responses, 0 incomplete, no warnings |
| candidates | 2.4 | local row load/order |
| hydration | 1,136.3 | fetch 1,134.1, apply 97.0 |
| enrich | 0.2 | all virtuals served from hydration data |
| sort_payload | 0.2 | |

### Hydration telemetry (`payload.field_stats`)

- `graphql.hydration`: keys 39,418 / requests 2,389 / values 39,418 / fallbacks 0 / handled 0 /
  **unresolved 0** / requeues 2 / deadline_hit false / points_cost 2,387 / points_used 1,794
  (per-window peak) / points_remaining 3,206 (window minimum) / **deferred 0**.
- Final adaptive snapshot (`graphql.hydration.adaptive`): `window=10`, `batch=26`, `drops=2`,
  `pauses=2`, `deferred=0`, `p95_latency_ms=6068.7`, `pacer_remaining=4157`,
  `pacer_rate=1.2262`, `paused_for=0.0`.
- `field_stats["points"]`: `count {1/1}`, `discovery {510/510}`, `hydration {2387/2387}`
  (points/responses).

### Audit window (2,900 rows, `audit_log` between run start and end)

- Status counts: **2,898 x 200, 2 x 504**, **0 x 403, 0 x 429**.
- Both 504s were hydration responses with ~11.2 s latency and **no `retry-after` and no
  `x-ratelimit-*` headers**: 16:02:22.873Z (key causes first drop/pause) and 16:08:17.491Z
  (second drop/pause). Both were requeued and resolved (`requeues=2`, `unresolved=0`).
- Hydration request rate: ~348/min in the first minute, 145-190/min between the drops, then
  90-120/min after the mid-run rate-limit reset (the pacer settling at ~1.23 points/s).

### Timeline

1. 15:59Z run starts; discovery 15:59-16:01Z; hydration starts 16:01:04Z, W=20, B=29.
2. 16:02:22Z first 504 -> drop 1 (W 20 -> 10, B 29 -> 20) + pause 1 (no `retry-after`; pause
   length 60-120 s by config, visible as the 11-request minute at 16:03Z).
3. 16:08:17Z second 504 -> drop 2 (W 10 -> 8, B 20 -> 14) + pause 2.
4. 16:10:55Z rate-limit window reset; the pacer's refill rate drops from ~5-6 points/s to
   `(5000-400)/3600 = 1.28` points/s, which caps the remaining ~1,300 point-batches and
   dominates the tail.
5. W regrows 8 -> 9 -> 10 after the 120 s cooldown + 300 s dwell; B regrows +1 per 50 clean
   batches, 14 -> 26 by run end. No further drops. 16:19:57Z end.

## Watch table readings (Run A; Run B was not run, see below)

| # | Signal | Healthy | Run A reading | Verdict |
|---|---|---|---|---|
| 1 | Window `W` | starts 20; climbs toward 32 on a clean run | 20 -> 10 (drop 1) -> 8 (drop 2) -> 10 (two dwell growths). Not pinned at 8 | **Stop condition: drops > 0.** W collapse is a direct consequence of the two drops |
| 2 | Batch `B` | stable at configured size (29) | 29 -> 20 -> 14 -> 26; never held at the floor 10 | Non-clean (drops shrank it and the 50-clean-batch regrowth was still ramping): final 26 |
| 3 | Drops | 0 | **2** (both HTTP 504, ~11.2 s latency, no rate-limit headers), requeued and resolved | **Stop and investigate** (any drop) |
| 4 | Pauses | 0; total pause time < 5 min | **2** (one per drop; no `retry-after` so 60-120 s each; total <= ~4 min) | **Stop and investigate** (any pause) |
| 5 | Pacer | remaining stays above 400; rate ~1.4 points/s early | remaining min 3,206 (never near 400); final `pacer_rate` 1.2262; no deferral; pacing was the binding limit after the 16:10:55Z reset | Healthy on the reserve criterion; **noted**: post-reset pacing alone implies a ~19-min minimum tail for this workload |
| 6 | Deferred | 0 | 0 | Healthy |
| 7 | Per-repo outcomes | unresolved `{}`; no unresolved warning | `unresolved=0`, `warnings=[]`, `incomplete=false`, all 39,418 values written | Healthy |
| 8 | Wall time | <= ~10-min baseline for 39k | **20.9 min** (> 12 min investigate line) | **Stop and investigate** (pacer/gate cost); causes: 2 pauses (~2-4 min), post-reset pacer at 1.23 points/s (~5-6 min extra), and more requests because B shrank (2,389 vs ~1,360 at B=29) |
| 9 | Audit 403/429 | 0 | 0 (two 504s are timeout statuses, not rate limits) | Healthy |

## Run B: skipped

Run B was not executed. The brief's rule is to repeat only when run A completed cleanly with
`drops == 0` and `deferred == 0`; run A recorded **2 drops** (both 504 timeouts). Watch row 3
also says to capture the bundle and stop on any drop. No additional points were spent on a
second run; the token sat at 4,156 remaining (reset 17:10:57Z) after run A.

## Assessment and decision

**Decision: keep `GITCRAWL_ADAPTIVE` off (default unchanged).** Do not propose flipping the
default yet.

The controller did what it is designed to do in the presence of upstream failures: it halved W
on the first 504/pause, shrank B, never approached the 400-point reserve, deferred nothing, and
the run still produced a complete, warning-free result (39,418/39,418, `incomplete=false`).
But the soak did not demonstrate a clean, fast run:

1. **Two GitHub-side 504s (11.2 s, no `retry-after`, no rate-limit headers) triggered full
   drop+pause cycles** even though there was zero rate-limit pushback (0 x 403/429). The drop
   policy treats timeout statuses as drops by design; on this evidence, isolated 502/504s with
   no corroborating rate-limit signal cost ~2-4 min of pauses and left W at 8-10 for the rest
   of the run. Worth confirming with a repeat soak whether these are random GitHub timeouts or
   concurrency-induced; a follow-up could require a corroborating signal (or a retry) before
   treating a lone timeout as a drop.
2. **Wall time 20.9 min vs the watch table's ~10-min baseline.** Two structural drivers, both
   visible in the data: (a) the pacer's `(remaining - reserve) / seconds_to_reset` rate is 1.23
   points/s right after a rate-limit reset, so a ~2.4k-point hydration can take ~19 min on its
   own; (b) after a shrink, B regrows only +1 per 50 clean batches, so the two drops pushed the
   run from ~1,360 to 2,389 point-batches. A 10-minute corpus completion with this pacer is only
   reachable when the run starts late in the rate-limit window (higher rate); starting right
   after a reset would take ~35+ min. This should be reconciled in the pacer/baseline
   expectations before any default flip (the 2026-10-08 profile spent the same ~2.9k points in
   22.3 min at concurrency 10 with no pacer, so the current 20.9 min is not a regression versus
   the only previously recorded live run - but it is not the clean adaptive win the watch table
   wants as flip evidence).

Recommended follow-ups (out of scope here): (a) re-run the soak on a token/window where no
reset bisects the run and with a decision on lone-504 handling; (b) decide whether the pacer
should spread spend across the whole window (current) or allow a full corpus run to finish in
one burst above the reserve; (c) if the drop policy changes, re-run this same watch table before
proposing the default flip.
