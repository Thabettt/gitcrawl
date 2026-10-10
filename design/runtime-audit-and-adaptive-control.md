# Runtime Audit and Adaptive Control: Going Faster Without Hitting the Wall

**Date**: 2026-10-09. Companion to `corpus-building-efficient-engineering.md`, `run-limits.md`, and `further-optimization.md`. No prior rate-limit knowledge is needed — terms are reintroduced where they matter, and the mother document's glossary words ("meter", "touch", "point") keep their meaning here.

**Purpose of this document**: answer one operator question honestly — *"We finish a 39,000-repo run in 10 minutes and use barely half of our 5,000-point hourly allowance. How much faster can we go before GitHub pushes back, and can we make the client adapt instead of slamming into fixed walls?"* It records the audit of the current pipeline, the measured quota ledger, the hidden ceilings (including the ones GitHub does not advertise), what the wider engineering world does about this exact problem, a recommended adaptive design, and a risk verdict. Everything here was verified against this working tree and the recorded run bundles on 2026-10-09; GitHub's numbers were re-verified against the live docs the same day.

**Status note**: this is an audit and a design study. **No code was changed.** Recommendations are ranked, line-level, and ready to schedule separately.

---

## 1. The one-paragraph summary

A 39,128-repo run takes about 10 minutes and spends about 2,916 of the 5,000 hourly GraphQL points. The slow part is **not** our client and **not** the network — it is GitHub's own per-query processing time (about 5 seconds per batch) multiplied by how many batches we allow in flight (20). Two simple levers get the run under 6 minutes: fetch 29 repos per call instead of 20 (still 1 point, 31% fewer calls), and let 32–40 calls run at once instead of 20. The 5,000-point allowance is **not** what limits our speed; the hidden ceilings are (a) an unadvertised CPU-time rule, (b) the documented 100-concurrent cap, (c) the 10-second query timeout, which *charges extra points* when it fires, and (d) our own client's fixed settings, which currently stall instead of adapting. The recommended fix is a small dynamic controller — not a rewrite — that shrinks the batch on the first timeout, halves concurrency on the first pushback, paces the hour so we never drain the meter to zero, and pauses the whole pool on a rate-limit signal instead of letting siblings keep knocking.

---

## 2. How one run actually spends its ten minutes

The run on record is `runs/8d661df8…/19` (39,128 repos). Its own timings:

| Phase | Seconds | Share | What it is |
|---|---|---|---|
| count | 0.7 | 0% | One `repositoryCount` probe |
| discovery | 95.6 | 16% | ~390–480 search pages at 100 repos/page |
| candidates | 1.4 | 0% | Ordering the candidate list |
| **hydration** | **501.7** | **83%** | **1,959 GraphQL detail calls at batch 20** |
| enrich | 0.3 | 0% | Served from hydration data |
| sort + snapshot | 2.1 | 0% | Payload assembly |
| **total** | **~602** | 100% | The operator's "10m 5s" |

**Plain correction:** the "~390 batched calls" figure is the *discovery page count* (39,128 ÷ 100), not the hydration count. Hydration is 1,959 calls, because the current batch size is 20 — the engine's own cap allows 50 (`src/lib/graphql_batch.py:19-20`), but nothing raises the setting. The graph is lopsided: five out of six minutes are the 1,959 small batches.

**The same night tells the other half of the story.** Run `…/18` used the exact same 1,959 requests and took **1,006 seconds** — 2x slower than run 19, same code, same data. Server-side latency varies that much. No client tuning can remove that variance; the goal is to make the client fast enough that even a bad day stays under the operator's target.

---

## 3. The audit, layer by layer

### 3.1 Networking and transport — healthy, with two small leaks

**What we found.** Every call goes through one shared, synchronous `httpx.Client`, created once per run and closed at the end (`src/lib/gh_client.py:51-52`, `src/serve/runner.py:127`, `src/serve/app.py:403`). One connection pool, one TLS session cache, no per-batch reconnections — the right shape.

Two leaks against that design:

1. **The keep-alive pool is smaller than the work.** httpx defaults hold only 20 idle connections for 5 seconds (`max_keepalive_connections=20`, `keepalive_expiry=5.0`), while discovery runs 32 workers. Surplus connections are dropped, and every idle gap longer than 5 seconds (a database upsert between search pages does this) forces a fresh TCP + TLS handshake.
2. **HTTP/1.1 only.** `http2=True` is not set, and the `h2` package is not installed — enabling it needs a new dependency, not just a flag.

**What it costs.** From this machine (Egypt, AS8452 — no nearby GitHub PoP), one measured handshake is ~75–126 ms TCP + ~155–553 ms TLS, and a full round trip is 218–640 ms. But one hydration call takes ~5,100 ms server-side, so **the network is only 5–10% of the story**. Fixing the pool is worth seconds, not minutes. It is worth doing because it is nearly free.

| Measured from this execution region (2026-10-09) | Value |
|---|---|
| DNS resolution | 12–45 ms (TTL 14 s, one A record: 140.82.121.6) |
| TCP connect | 75–126 ms |
| TLS handshake | 155–553 ms |
| Full request round trip (TTFB) | 218–640 ms |
| What GitHub itself needs per hydration call | ~5,100 ms |

**Plain consequence:** transport tuning cannot rescue a run made slow by server-side processing. Concurrency and batch size can.

### 3.2 Concurrency and rate limiting — fixed everywhere, adaptive nowhere

**What we found.** The pipeline is pure threads, no asyncio. Hydration runs a thread pool whose size is `min(limiter_max_concurrent, 20)` — a hard cap of 20 (`src/serve/runner.py:81`), even though settings allow 100. Discovery runs 32 workers, count probes 8. Between calls there is **no fixed sleep** — the run is fast on the wire and slow only because 20×5 s = one batch per quarter-second.

Rate-limit tracking already reads the right headers: `x-ratelimit-remaining`, `-reset`, `retry-after` feed the Redis bucket and the classifier (`src/limiter/buckets.py:193-221`, `src/limiter/classifier.py:85-95`). `retry-after` is honored exactly; a `remaining: 0` reply pauses the bucket until reset; 403/429 backoff climbs 60 → 120 → 240 → 480 seconds.

Three gaps:

1. **GraphQL's own cost receipt is thrown away.** Discovery asks for `rateLimit { cost remaining }` and never reads it; hydration does not ask at all. The client literally books "1 point" per request and only learns the truth when the next response's headers arrive (`src/limiter/buckets.py:53`, `:212`).
2. **A final 403/429 loses its message.** After retries, the classifier returns the response, and the batch layer replaces it with a generic "graphql request failed" (`src/lib/graphql_batch.py:155-156`) — the very sentence that would say "secondary rate limit" never reaches the logs.
3. **The 200-with-error path can hot-retry.** When GitHub returns HTTP 200 whose body says "rate limit" or "secondary rate", failed keys are requeued with **no sleep** (`src/lib/graphql_batch.py:24-34`, `:357-374`). The pause usually comes from the response headers — but when headers are missing (which happens), the loop re-knocks immediately, up to three times.

**Plain consequence:** safety exists, but it is reactive. The client is polite until something goes wrong, then it relies on a fixed ladder of sleeps — including the exact multi-minute stalls the operator wants to avoid.

### 3.3 Serialization and backpressure — decoupled where it matters, inline where it doesn't

**What we found.** Response parsing happens on the worker threads, never on an event loop (there is no event loop). Hydration has a proper producer/consumer shape: a dedicated apply thread drains a bounded queue (20 chunks × 500 rows) while fetching continues (`src/hydrate/tail.py:203-205`, `:272-275`). Apply time is 59.7 s against 500.7 s of fetching, so the consumer is comfortably ahead.

Two rough edges:

1. **Every batch body is parsed twice.** The audit hook already parses and caches the JSON on the response object (`src/lib/audit.py:99-104`); the batch engine then calls `response.json()` again (`src/lib/graphql_batch.py:158`). Discovery uses the cached copy; hydration does not.
2. **Discovery writes to Postgres between pages on the fetch thread** (`src/discover/pipeline.py:266-282`). The next search page starts only after the previous page's upsert commits, so database latency is added directly into the 95.6-second discovery phase.

**Plain consequence:** none of this is the bottleneck today, and the double parse is a few CPU seconds at most. Fix the parse for free; revisit discovery's inline upsert only when discovery time matters again.

### 3.4 The GraphQL query itself — dense but honest

**What we found.** Each hydrated repo asks for 31 scalar fields plus **five connections** (topics, languages, watchers, open issues, commit history). Every field is consumed; the last two connections only feed optional virtual filters and are fetched on every run regardless. The cost formula counts connection requests: 5 × batch size ÷ 100, rounded, minimum 1.

| Batch size | Connection requests | Points per call | Calls for 39k repos |
|---|---|---|---|
| 20 (today) | 100 | 1 | 1,959 |
| **29 (the sweet spot)** | 145 | **1** | **1,345** |
| 30 | 150 | 2 | 1,300 |
| 50 (the cap) | 250 | 2–3 | 780 |

**Plain consequence:** batch size 29 is the largest batch that still costs exactly 1 point. Batch 30 doubles the price per call; batch 50 may triple it. The 20→29 move is free money: 31% fewer calls, same total points. Anything at 30+ needs cost telemetry first.

---

## 4. The quota ledger: what we spend and what is left

From the recorded ledger (`docs/findings/2026-10-08-hydration-profile.md:75-77`) and run 19:

| Question | Answer |
|---|---|
| Points spent by a full 39k run (batch 20) | **2,916 measured** (2,455 accounted + ~460 in timeout penalties and retries) |
| Share of the hourly allowance | **58%** |
| Cost per repo | 0.0745 points |
| Repos per hour at that cost | ~67,000 |
| Peak request rate (secondary-point meter) | 235/minute — **12%** of the 2,000/minute ceiling |
| Concurrent requests | 20 — **20%** of the documented 100 |
| Cost at batch 29 | ~2,300 points/run (~46–48%), ~85,000 repos/hour realistic, ~106,000 ideal |

**Plain consequence:** we are using barely half the meter, and the unused half can be converted to either *speed* (finish 39k in ~4.5–6 minutes) or *volume* (about 85k repos per hour, or two 39k runs per hour with a slim margin). The meter is not the wall that speed runs into.

---

## 5. The hidden ceilings

GitHub publishes the big numbers and leaves the sharp edges vague. These are the sharp edges, with the honest gaps marked.

### 5.1 CPU time — the unadvertised one

The docs: *"No more than 90 seconds of CPU time per 60 seconds of real time… No more than 60 seconds of this CPU time may be for the GraphQL API. You can roughly estimate the CPU time by measuring the total response time for your API requests."* There is no header, no endpoint, and no published formula. Taken literally (total response time per wall second ≈ average requests in flight), the GraphQL budget would be about **60 requests in flight** — and a very conservative reading would be far lower still.

**The honest contradiction:** if that literal reading were enforced, our 20-in-flight runs would be several times over the line and every run would be throttled. All recorded runs completed with zero 403/429s. So the proxy is either much looser than the sentence suggests, or server CPU is a small fraction of response time, or both. **Treat this as an invisible wall with an unknown calibration: use it as a relative stress gauge, and never plan around a formula we cannot see.** Practical plan: 32 concurrent first (measured over one clean run), 40–48 as the ceiling, and every pushback treated as real.

### 5.2 Concurrency — documented, shared, and sometimes overstated

*"No more than 100 concurrent requests are allowed. This limit is shared across the REST API and GraphQL API."* Note the second sentence. The same best-practices pages also say *"avoid concurrent requests… make requests serially."* Both are true: 100 is the tripwire, not a target. Community probes show >100 in flight produces immediate 403s while the point meter is untouched. Our 20 is a fifth of the cap; 32–48 keeps a wide margin while leaving room for other tooling sharing the identity.

### 5.3 Secondary points per minute — real, but out of reach

2,000 GraphQL points per minute, where each non-mutating query counts 1 point regardless of shape. To reach it we would need ~33 requests/second; with 100 concurrent and 5–6 seconds per call, the ceiling is ~17/second (about 1,000/minute). **The 100-concurrent cap makes the 2,000/minute cap unreachable at our latency** — one wall stands in front of the other.

### 5.4 The 10-second timeout — the one that charges us

GitHub kills queries at 10 seconds (502/504) and *"additional points will be deducted from your primary rate limit for the next hour."* No amount is published; a 2026 field report measured 21 points charged for a query whose normal cost was 12. The run's own ~460 unaccounted points are consistent with this. Batch 20 averages ~5.1 s, half the cliff; batch 29 is roughly 40% more server work, so the margin narrows. **The batch-size controller in §7 exists specifically to shrink before this cliff, not after.**

### 5.5 Per-query resource limits — the 2025 addition

Since September 2025, very large or deeply nested queries get *partial results plus an error*, a separate mechanism from rate limits. A 50-alias hydration document (46 KB, up to ~5,500 nodes) is much closer to this than a 20-alias one. Another reason 29 beats 50.

### 5.6 Search — the 1,000-result ceiling

Every search tops out at 1,000 results; the shard planner already slices around this. No separate GraphQL search meter is documented — GraphQL search rides the same points meter — though GitHub Enterprise admins can tune a "CPU limit for searching," which hints that search work is weighted somewhere. Discovery pages take ~7.8 s each at 100 repos; keep the page size where it is (it was reduced from 35 fields to 14 after timeout problems) and raise *concurrency*, not page size, if discovery needs speeding up.

### 5.7 The window anchor — an undocumented edge

The docs say `x-ratelimit-reset` is "the time at which the current rate limit window resets" but never say when the window *starts*. Community samples show reset timestamps at random off-hour minutes, pointing to a window anchored at first token use, not top-of-hour. Our client assumes top-of-hour (see §6). This matters only once runs consume close to 5,000 points — exactly where this document is heading.

---

## 6. Two personality quirks inside our own limiter

The Redis limiter (`src/limiter/buckets.py`) is a fixed-window counter plus concurrency slots. Two behaviors are worth knowing before we push it to the edge.

**Quirk 1 — the counter only ratchets up, and the window is the UTC hour.** Every response reconciles the local count to `5,000 − remaining`, but only upward, and the count resets only when `floor(now/3600)` changes (`buckets.py:32`, `:69-87`). Consequences:

- If a run ever *does* drain the meter to zero and GitHub's real reset lands before the top of our local hour, the client can deny itself fresh allowance for up to ~59 minutes — a self-inflicted stall, exactly the outcome the operator wants to avoid.
- If the local hour rolls while GitHub is still empty (anchor mismatch the other way), up to ~20–32 requests may launch before the first response corrects the count — harmless noise, but noise the abuse heuristics can see.

**Quirk 2 — the client books 1 point per request, always.** True at batch ≤29. Above 29 the real price is 2–3 points per request while the local count still spends 1, so pushing the batch size or any new heavy query would over-issue before the headers catch up.

**Plain consequence:** the meter's *hard* enforcement is GitHub's, not ours — but at the edge, our own bookkeeping is what stalls first. Any push toward full quota should first re-anchor the local window to the response's `reset` value and count real cost, not requests.

---

## 7. Dynamic control: what the world does, and what we should do

### 7.1 The landscape, in one table

| System | Signal it watches | How it adapts | Lesson for us |
|---|---|---|---|
| octokit plugin-throttling (the vendor-recommended client) | `remaining`, `retry-after`, error text | **Not at all** — static groups (global 10 concurrent; GraphQL gated to 1/s) plus reactive retry | The canonical client is static; a per-request retry does not pause siblings (open bug #629) |
| go-github-ratelimit | `reset`, `retry-after` | Blocks the request | Reactive sleeping, no adaptation |
| hub4j github-api | `remaining` before every call | A pre-flight checker sleeps when remaining is low ("stop *before* exceeding") | The reserve pattern — pace so you never touch zero |
| PyGithub | Error text | Fixed spacing (0.25 s / 1 s) | Static pacing is the common floor |
| gh CLI | Nothing | Nothing deliberate | Even mature tools ignore the headers |
| python-github-backup | Wait-to-reset after 403 | **Burst, then ~20-minute pauses** — its own README recommends smooth `hour ÷ budget` pacing instead | The documented failure our operator fears |
| Sourcegraph | External feedback | Delays background work; static burst/rate | Gates work, not concurrency |
| Microsoft ghcrawler (archived) | Sum of response times as a **compute budget** (`15 s of API time per 15 s wall`), token reserve, Redis-shared | Adapts loop count to the compute budget | The closest real precedent: treat response time as the resource |
| Netflix concurrency-limits | RTT + drops | AIMD, Vegas, Gradient families | Use **loss-based** variants here; latency-based control misreads compute-bound latency |
| Google SRE client-side throttling | Accept/reject counts over 2 min | Clients reject themselves with `(requests − 2·accepts)/(requests+1)` | A backstop for wall-avoidance; cap self-rejection to avoid lockout |
| Envoy adaptive concurrency | Sampled latency vs minRTT | Gradient with pinned-probe baselines | Good theory, wrong signal for a hidden quota; its minRTT probing assumes queueing latency |

### 7.2 The lessons that decide the design

1. **Use explicit signals, not latency, as the primary trigger.** Rejections, timeouts and the response headers are visible; the true cause of a wall is not. Netflix's own guidance points clients to loss-based control, and Gradient2 ignores drops entirely.
2. **One authority per timescale.** The hour is owned by a quota pacer; the minute by concurrency; the single request by the timeout guard. Controllers that share a timescale fight each other.
3. **Pause the whole pool, not the request.** The octokit bug (#629) is the cautionary tale: after a 60-second pause, sibling requests keep firing and re-trip the limit. GitHub also warns that *"continuing to make requests while you are rate limited may result in the banning of your integration."*
4. **Shrink fast, grow slowly.** Multiplicative decrease on the first bad signal, additive increase after a long clean window; never increase while a stop signal is active.
5. **Bursting then stalling is worse than pacing.** The backup tool's own README documents the pattern; the taxi-meter metaphor from the mother document says the same thing — an hourly allowance spent evenly (~1.4 requests/second) keeps every abuse heuristic quiet.
6. **Retries need jitter and a budget.** Un-jittered retries synchronize (Google's "Pokémon GO" incident); a retry budget of ≤10% of calls prevents retry storms from becoming the real load.

### 7.3 The recommended design: three loops and a safety envelope

Nothing here replaces the existing hard limits. The Redis bucket, the batch cap, and the settings bounds stay as the outer envelope. The controller only *reduces* within it.

```
┌ Loop 3 — the hour   Quota pacer: rate = (remaining − reserve) ÷ seconds-to-reset
├ Loop 2 — minutes    AIMD concurrency window W ∈ [8, 48], starting at 20 (proven safe)
└ Loop 1 — per batch  Batch-size guard B ∈ [10, 29], shrink on the first timeout signal
   Always:            Whole-pool pause on a drop · jittered retries · a retry budget
```

| Loop | Timescale | Watches | Does | Bounds |
|---|---|---|---|---|
| **1. Batch guard** | per batch | 502/504, "resource limits" errors, p95 latency > ~7 s | B × 0.7 on a bad signal; +1 after 50 clean batches | 10–29 |
| **2. Concurrency window** | 5–10 min | 403/429, transient errors, timeout splits | W × 0.5 on a drop (immediate), freeze during cooldown; +1 after a clean window **and** only when nearly saturated | 8–48 |
| **3. Quota pacer** | 1–5 min | `remaining`, `reset` headers | Token bucket refilled at `(remaining − reserve) ÷ seconds-to-reset` | reserve 300–500 points; small burst only |
| **Pause rule** | on any drop | `retry-after` else 60 s | Pause every worker, not one request; cap escalation; fail clean after a bounded number | full jitter |

**Where it plugs in.** The submission gate in `src/lib/graphql_batch.py` already checks `len(in_flight) < concurrency` — the window becomes a live value instead of a constant, with the thread pool constructed at the ceiling (48) and the window gating submissions. The response hook (`src/lib/gh_client.py:230-232`) already receives every status and latency; the limiter's allow/deny result and the classifier's decisions are the other inputs. Loop 1 builds on the existing split-on-timeout behavior. Loop 3 reads the headers the client already collects. Keep the Redis slot cap at the ceiling so it remains the hard backstop rather than a competitor.

**What not to do.**

- No latency-only controller (Gradient/Vegas) — wrong signal, and drop-blind.
- No cold start at 1–3 concurrent based on the literal CPU sentence — our own C=20 data disproves the strict reading.
- Never probe the wall on purpose; every throttle is a serious event (ban risk language is explicit).
- Never leave a rate-limit error folded into a generic message; knowing *which* wall we hit is the controller's food.
- No per-request retry while the pool keeps running.

**Expected result.**

| Failure the operator named | Today | With the controller |
|---|---|---|
| "We hit the wall" | Fixed 20 until the envelope tightens, then sudden 403/429/502s | W halves on the first pushback; B shrinks on the first timeout |
| "We keep stalled" | 60 → 480 s single-request backoff; up to an hour sleeping to reset | Pool-wide 60 s pause at most; reserve pacing means the meter never reaches zero |
| "A penalty ruins the gains" | Timeouts silently charge extra points | Batches shrink before the 10-second cliff; penalty points become rare |

On a good day, W walks 20 → 32 → 40 (roughly 1.5–2x today's speed) with zero pushback; on a bad day (run 18's doubled latency) it backs off gracefully instead of timing out.

---

## 8. Ranked recommendations

Highest leverage first. Items 1–3 are safe to do now; items 4–6 should accompany the controller; item 7 is optional.

1. **Batch size 20 → 29.** `src/lib/graphql_batch.py:20` (`DEFAULT_BATCH_SIZE`) and `src/serve/settings_spec.py:22` (corpus preset). Largest batch that still costs 1 point; −31% hydration calls. Never 30+ without item 5.
2. **Concurrency cap 20 → 32, then 40.** `src/serve/runner.py:81` (`min(settings.limiter_max_concurrent, 20)`). One clean run at 32 before 40; 48 is the ceiling, not the default.
3. **Pool and session hygiene.** `src/lib/gh_client.py:51-52`: `limits=httpx.Limits(max_connections=64, max_keepalive_connections=64, keepalive_expiry=60.0)`, `trust_env=False` (if no proxy is intended), optionally transport retries. `http2=True` needs the `h2` package added to `requirements.txt` — low priority, since server time dominates.
4. **Dynamic controller (three loops).** `src/lib/graphql_batch.py` submission gate + `src/lib/gh_client.py` response hook + a new small controller module under `src/limiter/`. Start at today's proven 20; shrink fast, grow slowly (§7.3).
5. **Cost and latency telemetry.** Add `rateLimit { cost used remaining }` to the hydration query (`src/hydrate/graphql_repo.py`), capture `x-ratelimit-used` in `AuditRecord` (`src/lib/audit.py`) plus `run_id`/phase, and persist per-phase points in the run bundle. This is what turns "about 2,900 points" into exact numbers and validates the 5-connection cost model before batch >29.
6. **Limiter correctness at the edge.** Re-anchor the local window to the response's `reset` value instead of `floor(now/3600)` (`src/limiter/buckets.py:32`, `:75`), and use `x-ratelimit-limit` in the reconcile target (`:212`) instead of the hardcoded 5,000. Add a reserve floor so a run stops cleanly instead of sleeping to reset. Preserve the body message on final 403/429 (`src/lib/graphql_batch.py:155-156`) and pause the bucket on 200-with-rate-limit errors instead of hot-requeuing (`:357-374`).
7. **Optional cleanup.** Use the already-cached JSON instead of parsing batch bodies twice (`src/lib/graphql_batch.py:158`; cache lives at `src/lib/audit.py:99-104`); batch discovery's per-page upserts off the fetch thread (`src/discover/pipeline.py:282`). Both are small.

---

## 9. Risk verdict

**Can we get below 10 minutes on one token without tripping GitHub?** Yes, and with room. The 10-minute number is self-imposed: it is 1,959 small batches at 20 in flight. At batch 29 and concurrency 32–40, the same corpus is 1,345 calls at ~5–6 seconds each — roughly **4.5–6 minutes**, using ~2,300 points (46%) and peaking near 400 secondary points/minute (20% of that ceiling), 40 concurrent (40% of the documented cap, ~two-thirds of the CPU-proxy gauge). Even the worst recorded latency day (run 18, 2x) fits within ~8–9 minutes.

**What could still go wrong, honestly:**

- The CPU-time rule is invisible. The plan that respects it is empirical: ramp 20 → 32 → 40 with telemetry, stop at the first unexplained pushback, and never treat the published sentences as a contract.
- Timeout penalties grow with batch size. 29 is the calculated sweet spot; if 502/504s appear at 29, the controller's job is to shrink, and the fallback plan is 25.
- Pushing usage toward the full 5,000 in one hour turns §6's ratchet quirk from trivia into an operational hazard. Re-anchor the window (recommendation 6) before operating at the edge, not after.
- Everything about secondary limits is "subject to change without notice." The controller's greatest virtue is not cleverness; it is that it stops climbing after the first no.

---

## 10. What we still do not know

- GitHub's true CPU/secondary accounting — no header, no formula, no reliable public measurement. Our controller treats it as relative noise, not a number.
- The exact reset-window anchor. Evidence points to first-use anchoring, but the docs are silent; the pacer therefore trusts the latest headers and assumes nothing.
- The size of the timeout penalty. One field report: 9 extra points on a 12-point query. Our run's ~460 unaccounted points suggest it is real and worth engineering around.
- Whether a client can adapt on GitHub GraphQL *at all*: no public client — not even the vendor's — does adaptive concurrency today. We would be early, which argues for small steps and fast fallback.

---

## 11. Production log: the runs, the walls, and what we learned (2026-10-10)

Everything above was written before the work shipped. This section is the honest sequel, written after we ran it for real — including the runs that hit walls and the attempts that failed, because those are where the lessons live. If you read only one section of this document, read this one.

### 11.1 What actually shipped

Three implementation plans went through task-by-task review and merged to `main` the same day:

| Theme | What landed | Evidence |
|---|---|---|
| Transport & pipeline | One shared HTTP client with an explicit pool (64 connections, 60 s keep-alive) and no silent env-proxy surprises; every batch body parsed once (the audit hook's cache is reused); discovery upserts buffered per worker (10 pages / 1,000 items); audit inserts moved to a bounded writer thread | `docs/findings/2026-10-09-transport-pipeline-efficiency.md`; full suite green, 94.79% coverage |
| Quota & limiter | Migration 0014 (audit rows carry `rl_used`/`run_id`/`phase`; batch default 29); per-call point telemetry (`rateLimit { cost used remaining }` → `BatchStats` → `field_stats["points"]`); hydration ceiling 32; limiter window re-anchored to GitHub's real reset; 403/429 messages preserved; 200-body rate-limit bodies pause the bucket | full suite 1,514 passed, 94.86% coverage |
| Adaptive controller | `src/limiter/adaptive.py` (AIMD window 8–48, batch guard 10–29, quota pacer with a 400-point reserve, whole-pool pause), wired into `fetch_batch` behind `adaptive=`; env kill switch `GITCRAWL_ADAPTIVE`; later a settings toggle (migration 0015, default **off**) | soak note `docs/findings/2026-10-09-adaptive-soak.md`; full suite 1,555 passed |

Plain consequence: the engine can now *measure* its own points, back off when told, and be switched at runtime — but the defaults deliberately stayed conservative until the road tests below.

### 11.2 The soak: the wall story that wasn't a wall

The controller's first live test ran the full 39k corpus with adaptive ON. GitHub never pushed back — the controller pushed back on itself:

| Reading | Value |
|---|---|
| Wall clock | 20.9 min (baseline: ~10) |
| Drops | 2 — both 10-second timeouts (504) |
| Window W | 20 → 10 → 8 (never recovered; the dwell is 300 s) |
| Batch B | 29 → 26 (partial regrowth) |
| Pauses | 2 (60–120 s each, pool-wide) |
| Deferred / unresolved | 0 / 0 |
| Points spent | 2,898 of 5,000; the 400-point reserve never touched |
| 403s | 0 |

Plain consequence: the safety behavior is real — the run stayed clean and never drained the meter — but the policy is too sticky for speed: one timeout halves the whole window, and at the proven-safe ceiling of 20 the controller can only ever *reduce* concurrency. Decision recorded then and unchanged: **keep the flag off** at C=20. The controller is a seatbelt for driving above 20, not a faster engine.

### 11.3 Run 24: the 403 storm (the first real wall)

The next run used the static "first step" configuration the plans had agreed on — batch 29, concurrency 32, controller off. The audit rows tell the story:

| Phase | Requests | Notes |
|---|---|---|
| Discovery (32 workers) | 434× 200, 1× 504 | **clean** — 96 s for the whole corpus |
| Hydration (32 workers, batch 29) | 465× 200, **159× 403**, 2× 504 | the 403s started **8 seconds** after hydration began |

Every 403 was a fast rejection (0.3–1.1 s) carrying `retry-after: 60` and **no** rate-limit headers — GitHub's secondary/compute limit, not the points meter (only ~370 of 5,000 points were used). Throughput per minute told the story: 344 → 198 → **25** → 104 → 124 → 33. The run was cancelled at 26%.

The mechanism matters: a 403 only slept the worker that received it (60 s, its `retry-after`); the other 31 kept knocking and collected their own 403s. That is precisely the octokit-#629 anti-pattern this project set out to avoid — and we had only closed it for *200-body* rate-limit markers, not for 403s. The pool-wide pause existed only inside the controller, which was off.

Plain consequence: **concurrency 32 was the mistake, not batch 29** — discovery at 32 was clean, hydration at 32 was not. The hidden compute limit tripped on the 32-wide wave of heavy five-connection queries.

### 11.4 The settings optimization (between the two walls)

| Setting | Before | After | Why |
|---|---|---|---|
| `limiter_max_concurrent` | 32 | **20** | The only value with a proven-clean full run (run 19) |
| `graphql_batch_size` | 29 | **25** | 504 timeouts appeared at 29; 25 keeps the 1-point cost with more headroom |
| `discovery_concurrency` | 32 | 32 | Discovery was clean at 32 in both runs |

Expected result: ~8.5–9 min for 39k, zero 403s, ~2,050 points.

### 11.5 Run 25: the wall that got absorbed (and the controller that didn't see it)

The next run used the optimized settings — and this time the controller toggle was **on** (migration 0015's checkbox, enabled before the run).

| Time | Event |
|---|---|
| 20:56 | Start; discovery 84 s, clean |
| 20:57:34 | Hydration at full W=20; ~6,000 repos/min |
| **20:59:20–42** | **19× 403** in 22 seconds — `retry-after: 60`, no headers |
| 21:00 | 120 batches/min — 19 workers sleeping their 60 s |
| 21:01–21:03 | Recovered to 292 / 287 / 226 per min; zero further 403s |
| 21:03:51 | Cancelled at ~19,500 / 39,432 (50%, on track for ~21:07) |

Two findings, one uncomfortable:

1. **Why the wall appeared at the "safe" settings.** Two compounding reasons: the token was still *warm* — run 24 had generated 159 secondary 403s only 40 minutes earlier, and GitHub's secondary sensitivity escalates with repeated triggers and decays slowly; and the profile was slightly hotter than run 19's (batch 25 = +25% compute per query, and faster responses meant ~290–330 requests/min vs run 19's ~235). The burst passed in about a minute with zero failed batches.
2. **The controller was on and did nothing — and could not.** Its "drop" signal fires only on *batch-level* failures (403/429/timeouts that exhaust all retries). These 403s were absorbed inside `request_with_retry`: each worker slept its `retry-after`, retried, and succeeded. No drop, no window reduction, no pool pause — the Redis pause timestamp never changed. The controller is blind, by construction, to this class of transient secondary wave.

Plain consequence: enabling the toggle neither helped nor hurt this run; the failure mode it was built to catch (batch-level drops) never fired. The wall was absorbed by the per-request retry-after sleep — which is luck of timing, not design: had the retries also failed, run 24's storm would have repeated.

### 11.6 The scoreboard

| Profile | Run | Outcome |
|---|---|---|
| B=20, C=20, static | run 19 (before this work) | 39,128 repos in ~602 s; **zero 403s**; 2,916 points |
| B=29, W≤20, adaptive ON | soak | 20.9 min; 2 timeout drops; 0 403s; safe but slow |
| B=29, C=32, static | run 24 | **159× 403** storm; cancelled at 26% |
| B=25, C=20, adaptive ON | run 25 | 19× 403 absorbed in ~1 min; ~50% done at cancel; controller inert |

The current recommended operating point for a single token: **batch 20–25, concurrency 20** (16 if a storm is recent), discovery 32, adaptive off, and **a cooldown of 30–60+ minutes after any secondary-limit storm** before the next run. The only profile with a zero-403, full-corpus history is batch 20 / concurrency 20.

### 11.7 Open gaps and next moves

1. **Pool-wide pause on 403/429.** The real missing piece: when a secondary-limit response arrives, pause the shared bucket for `retry-after` so every worker stops knocking, instead of each sleeping independently. This also makes the event visible to the controller.
2. **Cooldown discipline.** Repeated secondary storms escalate; GitHub's own words: *"Continuing to make requests while you are rate limited may result in the banning of your integration."* Space out runs after a storm.
3. **Adaptive's role, clarified.** It is a seatbelt for concurrency > 20, not a speed feature; its drop signal does not see retry-absorbed 403s. Default stays off; the toggle (migration 0015) exists for experiments at higher ceilings.
4. **Small knowns** (deferred from reviews): the controller's default clock is `time.monotonic` (production passes `time.time`); a `retry-after: 0` falls through to 60–120 s; the deferral reason always reads "point reserve"; the pause wait doesn't check the run deadline.

---

## 12. Glossary of new terms

- **AIMD (Additive Increase, Multiplicative Decrease)**: increase the allowance slowly, cut it hard when something breaks. The TCP playbook, applied to concurrency.
- **Adaptive controller**: a loop that reads a signal and adjusts a knob, instead of a fixed setting.
- **Batch / alias batch**: one GraphQL call carrying N repos, one alias per repo.
- **Concurrency window (W)**: how many calls we allow in flight at once.
- **CPU proxy**: measured response time used as an estimate of GitHub's compute cost — GitHub's own suggested gauge.
- **Drop**: any signal that something pushed back — 403/429, 502/504 timeout, a rate-limit error inside a 200 reply.
- **Pacer**: the hourly loop that spends the meter evenly, so it never hits zero.
- **Reserve**: points deliberately left unspent as the safety margin (300–500).
- **Timeout cliff**: the 10-second wall; crossing it costs extra points, not just time.
- **Whole-pool pause**: stopping every worker on a rate-limit signal, because one more knock can cost more than the pause.

---

## 13. Sources

**GitHub documentation (verified live 2026-10-09):**

- Rate limits for the GraphQL API — `https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api`
- Rate limits for the REST API (secondary limits, CPU rule, header caveats) — `https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api`
- Best practices for using the REST API — `https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api`
- Timeouts now count against the primary limit (2025-07-21) — `https://github.blog/changelog/2025-07-21-including-timeouts-in-primary-rate-limits/`
- GraphQL per-query resource limits (2025-09-01) — `https://github.blog/changelog/2025-09-01-graphql-api-resource-limits`
- GraphQL meta reference (`rateLimit.cost`, `dryRun`) — `https://docs.github.com/en/graphql/reference/meta`
- Terms of Service §H and Acceptable Use Policies — `https://docs.github.com/en/site-policy/github-terms/github-terms-of-service`, `https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies`

**Adaptive-control literature and implementations:**

- Netflix concurrency-limits (AIMD, Vegas, Gradient, Gradient2, Windowed) — `https://github.com/Netflix/concurrency-limits`; Netflix, "Performance Under Load" — `https://netflixtechblog.medium.com/performance-under-load-3e6fa9a60581`
- Envoy adaptive concurrency filter — `https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/adaptive_concurrency_filter`
- Google SRE Book, "Handling Overload" (client-side adaptive throttling) — `https://sre.google/sre-book/handling-overload/`; SRE Workbook, "Managing Load" (Pokémon GO retry sync) — `https://sre.google/workbook/managing-load/`
- AWS, "Exponential Backoff and Jitter" — `https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/`
- Bronson et al., "Metastable Failures in Distributed Systems" (HotOS 2021) — `https://sigops.org/s/conferences/hotos/2021/papers/hotos21-s11-bronson.pdf`
- TCP AIMD — `https://www.rfc-editor.org/rfc/rfc5681.txt`; TCP Vegas — `https://cs.arizona.edu/sites/default/files/TR94-04.pdf`; BBR — `https://queue.acm.org/detail.cfm?id=3022184`
- Doorman lease-based fairness — `https://github.com/youtube/doorman`; Finagle retry budgets — `https://finagle.github.io/blog/2016/02/08/retry-budgets/`

**GitHub-client practice and failure reports:**

- octokit plugin-throttling (static groups; GraphQL gated as writes) — `https://github.com/octokit/plugin-throttling.js`; sibling-retry bug — `https://github.com/octokit/plugin-throttling.js/issues/629`
- go-github-ratelimit — `https://github.com/gofri/go-github-ratelimit`; gh CLI ignores headers — `https://github.com/cli/cli/issues/3292`; cached-header bug — `https://github.com/cli/cli/issues/12812`
- hub4j RateLimitChecker (pre-flight reserve) — `https://github.com/hub4j/github-api/blob/main/src/main/java/org/kohsuke/github/RateLimitChecker.java`; missing-header reports — `https://github.com/hub4j/github-api/issues/1805`, `https://github.com/hub4j/github-api/issues/2009`
- PyGithub fixed spacing — `https://github.com/PyGithub/PyGithub/blob/main/github/Requester.py`; no-retry-after reality — `https://github.com/PyGithub/PyGithub/issues/2113`
- python-github-backup burst-then-pause and `3600/5000` pacing — `https://github.com/josegonzalez/python-github-backup`
- Sourcegraph rate-limit handling — `https://sourcegraph.com/docs/admin/code-hosts/rate-limits`
- Microsoft ghcrawler compute-budget configuration — `https://github.com/Microsoft/ghcrawler/wiki/Configuration`
- Secondary-limit concurrency probe (community measurement, >100 concurrent) — `https://www.allanninal.dev/github/secondary-limit-concurrency`; timeout penalty measurement — `https://www.allanninal.dev/github/graphql-timeout-point-penalty/`
- GitHub Community discussions on secondary limits and adaptive self-throttling — `https://github.com/orgs/community/discussions/141073`, `https://github.com/orgs/community/discussions/189255`, `https://github.com/orgs/community/discussions/198220`

**This repository's measured evidence:**

- Hydration profile ledger (points 4,912 → 1,996; 1,951 requests; stage timings) — `docs/findings/2026-10-08-hydration-profile.md`
- Run bundles — `runs/8d661df8…/18/bundle.json` and `…/19/bundle.json` (run 19 is the 10-minute run analyzed in §2)
- Engine design and guarantees — `design/corpus-building-efficient-engineering.md` §9–§10
- Limits derivation and presets — `design/run-limits.md`; deferred telemetry ask — `design/further-optimization.md`
