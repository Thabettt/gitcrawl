# Corpus Building: Efficient Engineering

**Date**: 2026-10-02. Companion to `how-the-data-flows.md`, `gitcrawl-vs-seart.md`, `spec.md`, and `research.md`. You need no prior GitHub API knowledge — every term is introduced where it is first used. All GitHub numbers were verified against GitHub's official documentation on 2026-10-02.

**Purpose of this document**: explain, from scratch, how we can build the largest and freshest repository corpus possible with **one GitHub account**, as fast as GitHub's rules permit, without ever losing work and without breaking a single term of service. It records the math, the strategies, the legal boundaries, and the design decisions we reached — including the GraphQL batching engine, now built, that changes the speed by roughly 25x.

**Status note (2026-10-08)**: a later spike (recorded in §9.4) measured that GitHub's GraphQL search connection draws on the GraphQL **points** meter, not the REST search meter — so both the sharding probes and the result pages can ride it. The same ~39,000-repo shard-and-fetch that took about 20 minutes through REST search on 2026-10-07 completed in **67.9 seconds** on one token. The engine shipped the same day (`docs/superpowers/specs/2026-10-08-graphql-discovery-design.md`): the §3–§4 REST-search numbers are now historical — the shipped discovery path rides the points meter exactly as §9.4 describes.

**Status note (2026-10-10)**: the runtime/adaptive work landed (explicit HTTP pool, parse-once batch bodies, buffered discovery upserts, audit writer thread, batch default 29, hydration ceiling 32, per-call point telemetry, limiter window anchored to GitHub's real reset, and an adaptive controller behind a settings toggle that defaults off). Live runs then hit GitHub's secondary limits: concurrency 32 produced a 159×403 storm (run 24), and a follow-up run at batch 25 / concurrency 20 absorbed a 19×403 burst before being stopped (run 25). The proven-clean full-corpus point remains **batch 20 / concurrency 20**. The progressive log, including the failed attempts, lives in `runtime-audit-and-adaptive-control.md` §11.

---

## 1. The goal, in one paragraph

We want a list of repositories that match a filter (for example: Rust projects with more than 100 stars, pushed since 2024). We find candidates through GitHub's search, then we visit each candidate to save its current details, then we apply filters GitHub cannot do itself (like "has a Dockerfile" or "owner is in Germany"), and the survivors become the corpus. The entire process is done live through GitHub's public API. The engineering problem is that GitHub limits how fast anyone may ask, and that limit is small compared to the size of the corpus we want. This document is about squeezing every drop out of that limit, legally.

---

## 2. The funnel: five stages

Corpus building is a funnel. Many repos enter, fewer reach the end, because each stage can drop repos that do not match.

| # | Stage | Plain description | What it costs | How many survive |
|---|---|---|---|---|
| 1 | **Search** | Ask GitHub for repos matching the filter. Returns lists of up to 100 repos at a time. | "Search meter" only (see §3) | Everyone who matches |
| 2 | **Save** (also called hydration) | Visit each repo to read its full, current details, and write them down. **This is not cloning** — no files are downloaded. | 1 request per repo from the "main meter" | Usually all; deleted repos drop |
| 3 | **Free filters** | Drop repos using facts we already have in hand (stars, language, topic, dates). | Zero requests | Fewer |
| 4 | **Paid checks** | Drop repos using facts that need a question answered: owner country, Dockerfile presence, etc. The file machinery here is deliberately the same machinery that later becomes the agent-detection file channel. | 1 request per repo (or per owner) from the main meter | Fewer |
| 5 | **Corpus** | What survived is written to the database and the run bundle (CSV/JSON). | Zero requests | Final set |

The important asymmetry: **stage 1 deals in bulk; stages 2–4 touch repos one at a time.** That single fact creates the entire "cliff" described in §4.

---

## 3. GitHub's meters: the core mental model

Think of GitHub as a service with **prepaid meters**, like a taxi with two different meters. Each meter has its own allowance and refills on its own schedule. Spending from one meter does not reduce the other.

### 3.1 The meters

| Meter | Who it counts | Allowance (one account) | Refill | What it pays for |
|---|---|---|---|---|
| **Search** | Per account identity | 30 requests / minute | Every minute | Every search query and every results page |
| **Main (REST "core")** | Per account identity | 5,000 requests / hour | Every hour | Visiting repos, checking files, looking up owners |
| **GraphQL** | Per account identity | 5,000 *points* / hour (not requests) | Every hour | Batch queries (see §9) |

Two more useful facts:

- **Search returns up to 100 repos per request.** One request = 100 entries in the list.
- **GitHub refuses to show more than 1,000 results for any single search query.** A query matching 50,000 repos will not page past 1,000; page 11 at 100 per page returns `422 "Only the first 1000 search results are available"`. The query must be sliced into smaller queries (for example by creation date) until each slice is under 1,000. This slicing is called **sharding**, and the shard planner in this repo does it automatically.

### 3.2 The critical fact: limits belong to the account, not the token

GitHub's documentation says it directly: *"All of these requests count towards your personal rate limit of 5,000 requests per hour."*

That sentence covers personal access tokens (PATs), OAuth apps acting on your behalf, and GitHub App user tokens. They all draw from **one shared 5,000/hour budget per person or organization**.

Plain consequence: **adding more personal tokens to one account does not add one single request of capacity.** Ten tokens on one account are ten keys to the same bank account. (This repo stores a comma-separated token list in `GITHUB_TOKENS`, but the code today uses only the first token anyway.)

Independent budgets only exist across **different identities** (different accounts, or app installations placed on accounts/orgs — see §7 and §8).

### 3.3 The four-hour arithmetic

Everything in this document uses a 4-hour window because that is a natural overnight run. The math is simple multiplication:

| Meter | Per minute | Per hour | Per 4 hours |
|---|---|---|---|
| Search | 30 | 1,800 | **7,200 requests** |
| Main | — | 5,000 | **20,000 requests** |
| GraphQL | — | 5,000 points | **20,000 points** |

Search converts to repo listings at 100 per request: 7,200 x 100 = **720,000 repos listed** in 4 hours (a little less after planning overhead, so we say "about 600,000").

The main meter does not convert in bulk: 1 request = 1 repo visited. So 20,000 requests = **20,000 repos touched** in 4 hours. That is the cliff.

### 3.4 One freebie: "nothing changed"

GitHub supports **conditional requests**. When you ask about a repo you have already saved, you may send the fingerprint (ETag) of the copy you stored. If nothing changed, GitHub answers `304 Not Modified` — and the documentation states a 304 **does not count against your primary rate limit**.

Plain effect: the first build of a corpus costs full price; refreshing an unchanged corpus is nearly free.

### 3.5 The politeness ceiling (secondary limits)

Primary meters are not the only rules. GitHub also enforces **secondary limits**, described as protection against abuse:

| Rule | Limit |
|---|---|
| Concurrent requests | No more than **100 at once** (shared across REST and GraphQL) |
| Request points per minute, REST | 900 |
| Request points per minute, GraphQL | 2,000 |
| CPU time | 90 seconds per 60 seconds of real time |
| Content creation | 80/minute, 500/hour (not relevant here; we only read) |

GitHub's own best-practices page says: *"Avoid concurrent requests... make requests serially instead of concurrently."*

Plain effect: the primary meters run out long before the politeness ceiling is threatened, **provided we do not burst**. If the run paces itself to use its hourly allowance evenly (5,000/hour is only about 1.4 requests per second), the abuse alarms never fire. Bursting — hundreds of simultaneous calls — is the one behavior that can get an integration suspended or banned, and it does not even increase the total: the primary meter still caps the day.

A further warning from the docs: *"Continuing to make requests while you are rate limited may result in the banning of your integration."*

---

## 4. Why 600,000 are found but only 20,000 are saved

This is the sentence that confuses everyone the first time, so here it is step by step.

**Why search finds so many:** searching does not open repos. It returns **index cards** — up to 100 per request, 30 requests per minute. Reading 600,000 index cards never touches the 600,000 repos themselves. It is like asking a librarian for lists: the lists are cheap; pulling the books off the shelf is not.

**Why saving is so slow:** without batching, saving means asking GitHub *"give me the current full details of this one repo"* — one REST request per repo. The batched GraphQL path is now the default and is explained in §9–§10. One such request is what we call a **touch**. The main meter allows 20,000 touches per 4 hours.

**Why checks are slow too:** every check that needs a question answered is also a touch, and it spends from the *same* 20,000. One touch to save a repo. One more touch to check whether it has a Dockerfile. One more touch (per owner, not per repo) to look up a country.

### 4.1 The touches rule

> The main meter buys **20,000 touches per 4 hours**. Saving is a touch. Checking is a touch. A touch still costs even when the answer turns out to be "no."

If every repo must pass through every stage, the number of repos you can process is 20,000 divided by the number of touches per repo:

| Stages applied to each repo | Touches per repo | Repos per 4 hours |
|---|---|---|
| Save only | 1 | 20,000 |
| Save + 1 check | 2 | 10,000 |
| Save + 2 checks | 3 | ~6,700 |
| Save + 3 checks | 4 | 5,000 |

Real filters drop repos early, and dropped repos stop costing touches, so a real funnel does **better** than this table — never worse.

### 4.2 Worked ledger: 50,000 search hits, 4 hours

The main meter starts at 20,000. Watch it drain:

| Step | Action | Cost this step | Meter left | Repos remaining |
|---|---|---|---|---|
| 0 | Search finds 50,000 candidates | 0 (search meter paid) | 20,000 | 50,000 |
| 1 | Save the top 12,000 by stars | 12,000 | 8,000 | 12,000 |
| 2 | Free filters (stars, topic) drop to 8,000 | 0 | 8,000 | 8,000 |
| 3 | File-check those 8,000; 4,000 pass | 8,000 | 0 | **4,000** |

Result after 4 hours: **4,000 fully checked repos**, plus 8,000 repos saved but filtered out. The 38,000 candidates never saved are simply left for the next window, because the meter is empty.

The "save the top 12,000 by stars" choice is deliberate: order candidates most-valuable-first, so if the meter runs out, what was processed is the part that matters most. The run also reports exactly how many repos were left unprocessed — never a silent gap.

### 4.3 What this means for the thesis-sized corpus

The spec's Rust frame is on the order of 128,000 repos. With two touches per repo (save + one check), that is 256,000 touches.

| Setup | Time for 128,000 repos (2 touches each) |
|---|---|
| One personal account | ~51 hours |
| + 1 app installation | ~26 hours |
| + 2 app installations | ~17 hours |
| + 3 app installations | ~13 hours |
| With GraphQL batching (§9, built) | hours, not days |

---

## 5. Rules of the road: GitHub's terms, in plain words

Efficiency ends where the terms begin. These are the actual rules that apply, quoted from GitHub's Terms of Service and Acceptable Use Policies.

### 5.1 The hard lines

- **Do not multiply tokens to raise the ceiling.** Terms of Service §H: *"You may not share API tokens to exceed GitHub's rate limitations."*
- **Do not create extra accounts.** Terms of Service §B.3: *"One person or legal entity may maintain no more than one free Account"* (plus one free machine account, used exclusively for automation).
- **Do not become "excessive bulk activity."** The Acceptable Use Policies prohibit *"using our servers for any form of excessive automated bulk activity"* and activity that places *"undue burden on our servers through automated means."*
- **Do not use the data for spam or resale of personal information** (Acceptable Use Policies §7). Research use is explicitly contemplated: researchers may use public, non-personal data if resulting publications are open access.

### 5.2 What this means for our setup

- One person, one account: fine.
- One personal token, or several personal tokens: fine, but they share one meter, so more tokens are pointless.
- One GitHub App installed on your own account: explicitly documented and normal — the installation gets its own meter. See §7.
- Creating several bots/apps *purely* to multiply meters: mechanically possible, but it is exactly the pattern the abuse clauses exist for. See the risk discussion in §7.3.
- Every token we run must be owned by us and consented to; fingerprints (not raw tokens) are logged. This is already enforced by `docs/legal-gates.md` and the code.

### 5.3 If you genuinely need more than this

GitHub's Terms of Service says it plainly: *"GitHub may offer subscription-based access to our API for those Users who require high-throughput access."* Paying for higher throughput (or using GitHub Enterprise Cloud, which raises app installation limits) is the sanctioned path. Everything else is either against the rules or against their spirit.

**This document is engineering guidance, not legal advice.** GitHub's abuse policies are deliberately broad and include "undisclosed reasons"; no setup is provably immune. The protection is behaving like a polite, paced client with an honest identity.

---

## 6. Where the numbers come from (one-page reference)

| Thing | Number | Source (GitHub docs, verified 2026-10-02) |
|---|---|---|
| Authenticated user primary limit | 5,000 requests/hour | Rate limits for the REST API |
| Search limit (authenticated) | 30 requests/minute | Rate limits for the REST API |
| Results per search page | 100 | REST search docs |
| Results per search query | 1,000 maximum | REST search docs |
| Conditional 304 responses | Do not count against the primary limit | Best practices for the REST API |
| GraphQL points for a user | 5,000 points/hour | GraphQL rate limits |
| GraphQL cost formula | requests needed ÷ 100, rounded, minimum 1 | GraphQL rate limits |
| GitHub App installation (REST) | 5,000/hour, growing to 12,500 with repos/users; 15,000 on Enterprise Cloud | Rate limits for the REST API |
| GitHub App installation (GraphQL) | 5,000 points/hour; 10,000 on Enterprise Cloud | GraphQL rate limits |
| OAuth app, client credentials | 5,000/hour per app (public data) | Rate limits for the REST API |
| Concurrent requests (secondary) | 100 maximum, shared REST+GraphQL | Rate limits for the REST API |
| REST points/minute (secondary) | 900 | Rate limits for the REST API |
| GraphQL points/minute (secondary) | 2,000 | GraphQL rate limits |
| GraphQL query timeout | 10 seconds; timeouts cost extra points | GraphQL rate limits |
| Sources | `docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api`, `.../best-practices-for-using-the-rest-api`, `docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api`, `docs.github.com/en/site-policy/github-terms/github-terms-of-service`, `docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies` | — |

---

## 7. One account, no apps: what you actually get

The simplest legal setup: **one personal account, one (or more) personal tokens, no apps, no OAuth.**

| Window | Search found | Repos saved | Repos saved + 1 check |
|---|---|---|---|
| 1 hour | ~150,000 | 5,000 | 2,500 |
| 4 hours | ~600,000 | 20,000 | 10,000 |
| 1 day (24h) | ~3,600,000 | 120,000 | 60,000 |

This is enough for development, for slices of the thesis, and for incremental builds. It is not enough to rebuild a 128k corpus quickly from scratch — that takes about two days of continuous, paced running (§4.3).

### 7.1 Optional: one app, and what it buys

A GitHub App you create and install on your own account is a **separate identity with a separate meter** (documented behavior, not a hack). The numbers stack:

| Setup | Requests/hour (main) | GraphQL points/hour | Searches/minute |
|---|---|---|---|
| Personal only | 5,000 | 5,000 | 30 |
| + 1 app | 10,000 | 10,000 | ~60 |
| + 2 apps | 15,000 | 15,000 | ~90 |
| + 3 apps | 20,000 | 20,000 | ~120 |

The costs of an app: you must hold a private key and mint/refresh a token every hour, and the installation only reads public data unless granted more.

### 7.2 The scaling bonus

A single app installation is not always stuck at 5,000/hour: installations with more than 20 repositories receive an extra 50 requests/hour per repository (and 50 per user for organizations above 20 users), up to 12,500/hour. On Enterprise Cloud the installation starts at 15,000/hour. One app can therefore exceed the 5,000 figure on richer accounts.

### 7.3 The honest risk discussion

- **1 app**: normal, documented, safe.
- **2–3 apps**: mechanically identical to 1 app — each install has its own meter — but at some point you are creating apps *for the purpose of* raising the ceiling. Nothing in the docs forbids owning several apps; however, this is precisely the shape of behavior the broad "excessive automated bulk activity" clauses are meant to catch. Treat it as a judgment call, not a guarantee.
- **Many apps / many accounts**: evasion. Do not.
- All identities share the 100-concurrent politeness ceiling and the same IP address, so stacking meters never raises how *fast* you may burst — only how much you may spend per hour.

**Recommendation**: start personal-only. Add exactly one app if a job outgrows it. Beyond that, the right move is to get more value per touch (§8) or pay for higher throughput.

---

## 8. Strategies: getting more corpus from the same meters

Ordered from highest leverage to lowest.

1. **GraphQL batching (the big one — built).** Fetches ~20–100 repos per call on its own meter. Turns "one touch per repo" into "one touch per box of repos." Full explanation in §9.
2. **Use free "nothing changed" replies.** Store each repo's ETag; refresh an unchanged corpus at zero primary cost.
3. **Never fetch twice.** The database upserts by immutable repo id, so overlapping search pages, reruns, and multi-device runs dedupe instead of re-spending.
4. **Filter cheap-first, drop early, and stop paying for dead repos.** Stars/topic/language checks are free; owner country costs one call *per owner* (owners repeat across repos); file checks cost one call per surviving repo (and those same file checks are the future agent-detection file channel). Every repo dropped early is a future touch saved.
5. **Shard searches so no page is wasted.** GitHub caps a query at 1,000 results; the planner splits a broad filter into creation-date slices under the cap, so every fetched page carries 100 fresh repos instead of erroring or truncating. Sharded discovery is the difference between "top 1,000" and "all of them."
6. **Bulk enumeration instead of search when the filter allows it.** `GET /repositories?since=` walks all of GitHub in id order at 100 repos per call on the *main* meter (500,000 rows/hour), with no 1,000-row cap. Fine for bootstrap sweeps, useless for "Rust with 100 stars" which only search can answer.
7. **Zero-token mirrors.** ecosyste.ms and deps.dev provide package/file/metadata lookups that cost **no GitHub request at all**. Anything they can answer should never touch GitHub.
8. **BigQuery / GH Archive for history.** Commit and PR history at scale is cheaper and rule-safer to read from public datasets than to crawl repo by repo (billed by Google, not GitHub).
9. **Order by value.** Process candidates highest-stars-first so an interrupted or budget-capped run contains the most valuable part of the corpus, and report the unprocessed remainder explicitly.
10. **Pace, don't burst.** The hourly meter is the real cap; spreading requests evenly (~1.4/second for one account) keeps every secondary limit comfortably distant.
11. **Trust the response headers.** `x-ratelimit-remaining` and `x-ratelimit-reset` on every reply are authoritative; the code adapts its pacing to them instead of guessing.

---

## 9. The GraphQL trick, explained (the cliff fix)

### 9.1 REST versus GraphQL

- **REST (without batching)**: one request = one repo. Ordering 50 repo details is 50 phone calls.
- **GraphQL (batching, built)**: one request can carry a *shopping list* ("details for these 50 repos"), and the answer contains all 50. One phone call, 50 items.

GraphQL is on **its own meter** (5,000 points/hour) and its price is in **points**, not requests. The cost formula is roughly **1 point per 100 items requested** (minimum 1 point per query). A batch of 20–50 repos therefore costs about **1 point**.

### 9.2 The math

| | REST today | GraphQL batching |
|---|---|---|
| Cost to save 50,000 repos | 50,000 touches (2.5 days) | ~500–1,000 points (minutes) |
| 4-hour ceiling | 20,000 repos | hundreds of thousands of repos |
| Meter used | main | GraphQL (previously unused) |

Both meters work at once, so batching also frees the main meter for the things GraphQL cannot do.

### 9.3 The honest caveats

- Keep batches modest (20–50). Very large queries get cut off by GitHub with partial results; a 10-second timeout costs **extra** points.
- GraphQL has no "nothing changed" freebie. For repeat refreshes, the existing REST + ETag path stays valuable, so the design keeps both.
- GraphQL errors arrive inside otherwise-successful replies (`200` with an `errors` list), and the good data is often still there. Handling this correctly is the entire subject of §10.

### 9.4 Search itself on the points meter (measured 2026-10-08)

**What was measured on 2026-10-08.** A live spike asked a question the earlier sections did not: can *finding* repos — the count probes and the result pages — also ride the GraphQL points meter, instead of the REST search meter? The answer measured that day: yes, and the numbers are large. The engine was built from this measurement the same day (`docs/superpowers/specs/2026-10-08-graphql-discovery-design.md`); its own live end-to-end validation is the plan's final task (`docs/superpowers/plans/2026-10-08-graphql-discovery.md`).

**How the price works.** A GraphQL search that asks only for `repositoryCount` returns a single number, so sixteen probes fit in one query — and the whole query costs **1 point** (the minimum). Result pages are priced per search connection: one connection per query, with many queries in flight, stays cheap and fast. The 30-requests-per-minute REST search meter is never touched.

**The measured ledger**, same filter as the §11 live run (`language:rust stars:>=4 created:>=2025-02-24`, ~39,000 repos):

| Step | Result |
|---|---|
| Plan (aliased count probes, bisect until fetchable) | 111 probes in 9 batched queries → 56 shards in **8.9 s** |
| Fetch (one search connection per page, 32 in flight) | 418 pages → 38,969 unique repos in **59.0 s** |
| **Total shard-and-fetch** | **67.9 s**, 0 retries, 0 failures, 0 oversized shards |
| Meter used | ~430–500 GraphQL points (~10% of the hourly budget); REST search untouched |
| Same job through REST search | ~13 min of pages at 30/min + ~7 min of count probes ≈ **20 min** |

**What did not work, and why the recipe is shaped this way.** Putting several result pages in one query does not scale: two search connections at 100 repos each took ~6 s, three took ~8 s, and four hit GitHub's 10-second query timeout (a `502`). Probes are fine batched because each returns one number; pages are fetched one connection per query, in parallel.

**Cross-check from the same day**: 17,782 of the 17,795 repos the earlier REST run exported (99.93%) were rediscovered; the small remainder is the search drift this project already documents for REST runs. The shipped engine carries the treatment this claim needed — paced concurrency with backoff and audit rows — and the plan's final task is the live end-to-end validation (`docs/superpowers/plans/2026-10-08-graphql-discovery.md`).

---

## 10. The batching engine: agreed design (built)

This section records the design we settled on and built. It answers one worry: **one bad repo must never cut off the thousands behind it, and nothing may stall or fail silently.**

Status: built (see `docs/superpowers/plans/2026-10-02-graphql-batch-engine.md`); REST paths remain as fallbacks.

### 10.1 Shape

A small generic **batch core** plus three thin **adapters**:

- Core: builds a batch request, sends it, parses data **first**, attributes each error to the exact repo it belongs to, re-queries only the failed ones, retries within strict bounds, falls back where allowed, and records a final state for every repo.
- Adapters: (1) repo details (the save), (2) file existence (Dockerfile, trees), (3) owner location (geo). Each only supplies its query and its parser.
- The existing one-by-one REST path is kept: it is the fallback for stubborn repos and the free-ETag refresh path for reruns.

### 10.2 Non-negotiable guarantees

1. **Good data is always kept.** GraphQL commonly returns 19 good results plus 1 error in the same reply; the engine records the 19 and re-queries only the 1.
2. **Per-repo attribution.** GraphQL errors carry a `path` naming the alias (which repo) they belong to; the engine maps every error to a repo, never to "the batch."
3. **Bounded everything.** Split depth is capped; each repo gets a fixed number of attempts; every request runs under a deadline. There is no loop without a limit.
4. **Split only failures.** On a transient failure (timeout, resource limit), the batch halves; only the failing half is retried, and only down to singles.
5. **REST fallback.** A repo that still fails as a single batch call is retried once through today's one-by-one path; if that fails too, it is recorded **unresolved with a reason**.
6. **Explicit final states.** Every repo ends as *saved*, *fallback-saved*, or *unresolved (reason)*. Counts are reported at the end of the run; audit rows are written as failures happen.
7. **No silent partials.** Any unresolved repo marks the run **partial**, consistent with the project rule that partial data is never presented as complete.
8. **Bounded concurrency.** Batches run through a small thread pool under the shared limiter and the run deadline; hydration uses `min(limiter_max_concurrent, 20)` workers. Parallelism stays well inside GitHub's 100-concurrent politeness ceiling, and the point meters still cap the hour. (Measured: ~2,800 repos/min at concurrency 10, versus ~300–400/min sequential.)
9. **Dedicated GraphQL meter.** A `graphql` bucket (5,000 points/hour) is added beside `search`/`core` in the limiter, and `/graphql` is recognized as its own resource.

### 10.3 Why not the old module as-is

A `graphql_batch` module existed before and was deleted as unused (R58). It had five defects that map exactly onto the requirements above:

| Old defect | Requirement it violated | New rule |
|---|---|---|
| Discarded the whole batch's data when any error appeared | One bad repo killing 19 good ones | Parse data first; keep good results |
| Ignored error `path` | Could not tell which repo failed | Attribute per repo |
| Stopped splitting at depth 4, leaving collateral losses | No repo may sacrifice others | Re-query only failures; fall back |
| Returned results without the "incomplete" signal | No fallback, no visibility | Explicit per-repo outcomes |
| No dedicated GraphQL meter | Points charged like normal requests | Separate bucket + point accounting |

### 10.4 Out of scope (deliberately)

- The deferred agent-detection plan; this engine is for corpus building only. The file-check adapter is deliberately the same machinery that plan calls its file channel — building it now is corpus work, not detection work.
- Cloning; file contents still never leave GitHub unless the separate clone feature is used.
- Deep filters (commits, LOC, PR history); they will plug into the same core later, but are not part of this build.

---

## 11. Scenarios at a glance

| Scenario | Setup | Time |
|---|---|---|
| 10,000 repos, save + 1 check | personal only | ~4 hours |
| 128,000 repos, save + 1 check | personal only | ~51 hours |
| 128,000 repos, save + 1 check | + 1 app | ~26 hours |
| 128,000 repos, save + checks | personal + batching engine | hours (bounded by checks that cannot batch) |
| Refresh an unchanged corpus | personal + ETags | nearly free |

**Measured (2026-10-06/07).** A live run (`created:>=2025-02-24 language:rust`, min_stars 4, min_commits 50, min_language_bytes 175,000) found 38,833 repos and hydrated all of them in 1,942 GraphQL requests — 0 REST fallbacks, 0 unresolved — at roughly 2,800 repos/min with concurrency 10. The local filters then cost zero extra API calls: min_stars kept 38,833; min_commits kept 21,503; min_language_bytes exported 17,795 (0 skipped, 0 incomplete shards, status done). `corpus.csv` columns are id, full_name, stargazers, pushed_at, archived, language, license_spdx, country_iso, geo_confidence; the bundle also carries `field_stats` (per-field survivors and calls spent).

**Current code caveats** (as of this document): one run caps itself at 500 candidates, 200 saved, 100 checked (`RunnerConfig`), so today's single run yields hundreds, not thousands; those caps are now operator-tunable at `/settings` (env-pinnable); defaults remain the interactive values, and the derivation plus presets live in `run-limits.md`. The token loader accepts a list but the runner uses the first token; rotation adds nothing on one account anyway.

---

## 12. Glossary

- **API**: the machine-to-machine door into GitHub. Every question through it is a "request."
- **Rate limit**: the allowance of requests (or points) per time window.
- **Meter / pot**: informal name for one such allowance (`search`, main/`core`, `graphql`).
- **Touch**: one request spent on one repo (save it or check it). The main meter buys 20,000 touches per 4 hours.
- **Search**: GitHub's index query; answers in bulk lists, 100 per request, but each query is capped at 1,000 results.
- **Sharding**: splitting one broad filter into many smaller queries (usually by creation date) so each stays under the 1,000-result cap.
- **Hydration / save**: fetching one repo's full current details. Not cloning.
- **Cloning**: copying git history and files to disk. Optional, free of API requests, done by a separate feature.
- **ETag / 304**: a fingerprint you store for a repo; if nothing changed, GitHub says so for free.
- **GraphQL**: the query language that allows batch requests on a points meter.
- **Point**: GraphQL's unit of cost; roughly 1 point per 100 items requested, minimum 1.
- **Alias**: a named slot inside one GraphQL query (`c123: ...`) that lets the engine attribute results and errors to specific repos.
- **Batch**: one GraphQL call carrying many repos.
- **Partial response**: a reply containing both good data and errors; must never discard the good data.
- **Secondary limits**: GitHub's anti-abuse ceiling (100 concurrent requests, points/minute caps). Distinct from the hourly meters.
- **Unresolved**: a repo that could not be saved or checked after all allowed attempts; recorded with a reason and counts toward a partial run.
- **Identity**: the thing a meter belongs to — a user account, an app installation, or an OAuth app (never an individual token).

---

## 13. Sources

- Rate limits for the REST API — `https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api`
- Best practices for using the REST API — `https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api`
- Rate limits for the GraphQL API — `https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api`
- GitHub Terms of Service (§B.3 account terms, §H API terms) — `https://docs.github.com/en/site-policy/github-terms/github-terms-of-service`
- GitHub Acceptable Use Policies (§4 spam/inauthentic activity, §5 site access, §7 information usage) — `https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies`
- Project legal gates — `docs/legal-gates.md`
- Data flow and stages — `design/how-the-data-flows.md`
- Catalog-tool contrast (SEART) and why this pipeline exists — `design/gitcrawl-vs-seart.md`
- Market landscape and build justification — `design/landscape-comparison.md`
- Batching design discussion and prior-art analysis — this document, §9–§10
