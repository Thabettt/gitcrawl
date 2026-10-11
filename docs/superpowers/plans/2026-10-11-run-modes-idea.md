# Run Modes: Sprint vs Marathon (idea note)

**Date**: 2026-10-11. **Status: idea, not an implementation plan.** Captures a design direction agreed after the 2026-10-10/11 secondary-limit investigation; build it later. Context: `design/runtime-audit-and-adaptive-control.md` §11 and the hardening commits (`a723844`, `4620fb0`).

## The insight

The cost of a cooldown is relative to the run's horizon:

- A 10-minute run that takes one 60 s pause loses >10% — and with retry dynamics we measured 10 → 20.9 min. Short runs must optimize for **zero variance: never touch the wall**.
- A 7-hour, 1M-repo build loses 0.24% to the same pause, while hammering through the wall costs hours. Long runs must optimize for **sustained throughput: pauses are acceptable**.

Same machinery, opposite economics. Today's single behavior (adaptive controller + escalation, default off) fits marathons and hurts sprints.

Principle: **sprints calibrate before; marathons adapt during.**

## The two modes

| | Sprint (short runs) | Marathon (long runs) |
|---|---|---|
| Pre-flight | Canary: 5–10 hydration-shaped probes to measure the token's current ceiling; run just under it, or abort before burning the run | none |
| Concurrency | Static, just under the measured ceiling (fallback: batch 20 / C=20 cool, C=8–10 warm) | Adaptive AIMD window |
| On first secondary hit | Fail fast — abort cleanly (partial, resumable). A paused sprint is a lost sprint | Pause the pool, escalate 60→120→240→480 + jitter, continue |
| Hourly budget | Not a concern (~2k points) | Pacer must **wait for the hourly reset and resume** — a 1M build spans 7+ windows |
| Stop budget | 1–2 hits | 15+ |

## The setting

One new choice in `/settings`: **Run mode: auto | sprint | marathon** (default `auto`).

- `auto` — after discovery the run knows `total_count`; the estimated wall time at the safe rate picks the posture (threshold ≈ 30–45 min).
- `sprint` / `marathon` — operator override.

## What exists vs what is missing

Exists: pool-wide 403/429 pause, escalation + jitter, per-pass hit budget, adaptive controller (AIMD + pacer + reserve), settings toggle, migration 0015.

Missing:
1. The mode concept and the `auto | sprint | marathon` setting (migration + spec + UI + wiring).
2. Sprint canary (probe before committing to a run — tonight's diagnostic tripped at 3 probes on a hot token; a canary would have saved the run).
3. Sprint fail-fast (abort on first hit instead of pausing).
4. Marathon pacing that sleeps through hourly budget resets and resumes (today's reserve behavior stops the run — right for sprints, wrong for a 1M build).
5. Auto-selection from `total_count` after discovery.

## Open questions for later

- Canary shape: probe count, concurrency, abort threshold.
- Fail-fast granularity: abort the whole run vs skip remaining hydration and finish partial (data already saved; check resumability).
- Auto-mode threshold tuning (30–45 min start).
