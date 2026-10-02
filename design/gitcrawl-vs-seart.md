# gitcrawl vs. SEART GitHub Search

**Date**: 2026-10-02. Companion to `how-the-data-flows.md` and `corpus-building-efficient-engineering.md`. No prior knowledge assumed. SEART facts below were verified against its public site, repository, and published papers on 2026-10-02.

**Purpose of this document**: explain, plainly, what SEART GitHub Search is, what gitcrawl is, where each one genuinely wins, and why gitcrawl is the better fit for this project's thesis work. It is written to be fair: SEART is a good, citable research platform, and there are jobs where it is clearly the better choice. The claim is not "gitcrawl beats SEART at everything" — it is "gitcrawl wins at the specific job we need done."

---

## 1. The two tools in one paragraph each

**SEART GitHub Search** (seart-ghs.si.usi.ch) is a research platform built by the SEART group at the Università della Svizzera italiana. Their servers continuously crawl GitHub and store repository metadata in a central database. Researchers then use a web form to sample repositories by criteria like language, stars, commits, contributors, issues, pull requests, branches, releases, creation date, last-commit date, license, forks, and lines of code. Results can be exported as CSV/JSON/XML, and the whole database is published as periodic dumps. It is a *shared, pre-crawled catalog*: you search what they have already collected.

**gitcrawl** is the tool in this repository. It runs a live search against GitHub on demand, at the moment you press Find (`ran_at`), saves every candidate's current details, applies filters GitHub itself cannot express (does it have a Dockerfile? is the owner in Germany?), and writes a frozen, replayable bundle containing exactly what GitHub said, plus explicit notes about anything incomplete. It is an *on-demand instrument*: you measure what GitHub is right now, and you keep the evidence.

The one-sentence difference: **SEART asks "what does my catalog contain that matches?"; gitcrawl asks "what does GitHub contain right now that matches, and prove it."**

---

## 2. Side-by-side

| Dimension | SEART GitHub Search | gitcrawl |
|---|---|---|
| **Where data comes from** | Their servers' continuous crawl into their database | Live GitHub API at the moment the run executes |
| **Freshness** | Crawl cycles about every 6 hours; public dumps at most 15 days behind; the platform can lag or be temporarily unavailable (a 502 outage issue is open) | Exact timestamp per run (`ran_at`); no third-party lag |
| **Population** | Repos with **at least 10 stars**, within the languages their crawler targets (~1.9 million repos across 35 attributes in recent third-party analysis; 1.57M after cleanup) | Anything GitHub search can express; no star floor unless you set one; can deliberately include low-star repos |
| **Filters** | Fixed catalog of precomputed columns (stars, commits, contributors, issues, PRs, branches, releases, dates, license, forks, LOC-style metrics) | Same, plus **virtual filters**: Dockerfile presence, owner country + confidence, topic checks, and future commit/LOC filters — you can add a new one without waiting for anyone's crawler |
| **Owner location** | Not collected, not filterable | Resolved from free text with confidence labels; cached per owner (see §4.1) |
| **File-level facts** | Not available (fixed metadata columns only) | One tree call per repo answers all path questions; `has_dockerfile` today, agent-file detection later (see §4.3) |
| **Reproducibility** | "What our database held at crawl time"; not tied to a run you can replay | Frozen bundle per run: filter spec, filter hash, API version, raw upstream JSON per repo, incompleteness flags; byte-identical replay from the bundle |
| **Evidence for a thesis** | Catalog export | Per-repo raw GitHub responses — the replication package's raw layer; audit rows for every request; rate-limit telemetry |
| **Honesty about gaps** | Catalog semantics (you see what was collected) | Explicit partial flags: capped pages, timed-out shards, throttled retries, dropped candidates, unresolved repos — never silently missing |
| **Setup cost** | Nearly zero: open the site or download a dump | Needs a token, a database, Redis, and a running console |
| **Heavy metrics** | Already computed for ~1.9M repos (commits, contributors, issues, PRs, LOC via `cloc`) | Commits/LOC are recorded but **not yet enforced** (a known gap); file and country checks work today |
| **Cloning / code** | Not available in GitHub Search itself; their separate Data Hub offers Java/Python code datasets with preprocessing by email | Optional clone control (shallow/file-only/windowed) into the run, never required |
| **Code/legality posture** | Shared public platform with published papers and a DOI | Owned tokens, fingerprint-only logging, paced to GitHub's limits, legal gates documented in-repo |
| **Best at** | Fast, no-setup sampling from a large prebuilt catalog | Bespoke, live, evidence-backed corpora with filters nobody has precomputed |

---

## 3. Where SEART is genuinely better

Stating this first is not politeness; it is accuracy.

1. **Time to first result.** No token, no crawl, no setup — the catalog already exists. For a quick sample, SEART wins outright.
2. **Precomputed heavy metrics.** Commits, contributors, issues, PRs, and `cloc` line counts across ~1.9M repos cost them months of crawling; asking gitcrawl to (eventually) compute the same is real work.
3. **Citable and known.** Peer reviewers in empirical software engineering recognize SEART (Dabić et al., MSR 2021; Zenodo DOI). "We sampled from SEART" is a sentence reviewers accept without explanation.
4. **Bulk downloads.** Their dumps let you analyze offline without any API interaction at all.
5. **Zero GitHub-account risk.** You are not the one calling GitHub, so you cannot trip rate or abuse limits.

If the task were "give me 5,000 Java repos with more than 100 commits, I don't care whether it is today's state," SEART is the right tool and gitcrawl would be wasted effort.

---

## 4. Where gitcrawl wins, and why it matters here

Three capability differences are decisive for this project. They share one root cause: **SEART can only show columns its crawler already collected; gitcrawl can ask GitHub a brand-new question at run time.** The file question in particular is not just a filter — it is the first stage of the agent-detection work that follows corpus building.

### 4.1 Location: knowing where the owner is

GitHub search has no "owner is in Germany" filter. An owner's `location` is free-form human text ("🇩🇪 Berlin", "Lagos", "🌍 remote", often empty), and SEART collects no owner-geography columns at all: it cannot filter by country, and it cannot hand you the data to filter later.

gitcrawl resolves owner location itself — flag emojis decode deterministically, an offline city gazetteer covers most text, aliases catch variants, a cached geocoder handles the residue, and every result carries `{country_iso, confidence, raw_location}` with an explicit unmatched bucket. Geography becomes a first-class study variable (country-level adoption comparisons, for example) with honest confidence labels instead of a blind spot. Cost: about one request per owner, cached forever after.

### 4.2 Stars below 10: the invisible majority

SEART's crawler ships with a minimum of 10 stars (`ghs.crawler.minimum-stars=10`), and its own site notes that a repo excluded for falling below a threshold only appears "a few days after" it crosses. That default hides the absolute bottom tail of GitHub: a 2026 third-party analysis of the SEART platform (arXiv 2609.18953) opens by noting **317 million repositories with fewer than 10 stars**, and that "unpopular repositories are not always noise" — many are highly active with hundreds of commits.

gitcrawl has no such floor. `stars:<10`, `stars:0..9`, or no star filter at all are all valid search filters; the star policy becomes a study-design decision recorded in the frozen filter spec instead of a platform default imposed on every query. The practical cost is volume: the low-star slice is enormous, so such runs lean on language/date filters and sharded discovery (search can list ~600,000 repos per 4 hours; saving them is the usual bottleneck — see `corpus-building-efficient-engineering.md` §3–§4).

### 4.3 Finding files — and why it is also the road to agent detection

SEART stores repository metadata, not repository contents. It cannot answer "does this repo contain `CLAUDE.md`?", "is there a `.cursor/` directory?", "is there an `AGENTS.md`?", or even "does it have a Dockerfile?" — those are not columns in its catalog. Its separate Data Hub clones repositories for Java/Python code datasets, but that is a different tool, a different input, and not a filter you can put in a query.

gitcrawl inspects files directly: one recursive tree call answers every path question for a repo at once (never one call per file), with `.gitignore` lines read as a secondary "visible only via ignore" signal. `has_dockerfile` ships today as the first customer of this machinery.

That same machinery **is** the file channel of the agent-detection work (currently deferred): the trace packs match file patterns such as `CLAUDE.md`, `.cursor/`, and `AGENTS.md`, with `AGENTS.md` flagged as a generic (shared) signal and `CONVENTIONS.md` excluded. Building "find files" now therefore builds the first detector for later at no extra conceptual cost — the query, the matching, and the evidence records all get reused. No catalog platform can offer that, because detection needs per-repo evidence recorded at the observation time, not a precomputed column.

### 4.4 The rest of the advantages

1. **Live at `ran_at`.** The thesis compares adoption across ecosystems at a defined observation time. SEART's crawl time is theirs, not yours; their database may lag hours to days, and a repo crossing a threshold today shows up "a few days later" by their own documentation. gitcrawl stamps every run and every response with the moment it ran.
2. **Frozen evidence, not just a result list.** gitcrawl stores raw upstream JSON per repo plus the filter spec and API version, so a reviewer can re-run the same frozen spec and diff drift. SEART hands you rows; the underlying GitHub response of a given day is not part of the artifact.
3. **Explicit incompleteness.** gitcrawl's rule is "never present partial data as complete": capped searches, throttled pages, dropped candidates, and unresolved repos all become named flags in the run. A shared catalog cannot tell you what it missed.
4. **Independence.** gitcrawl does not stop working when someone else's service returns 502, runs its crawler on a 6-hour cycle, or changes its schema. The pipeline is in this repository, under this project's control and audit.
5. **One pipeline for the whole study.** Search, virtual filters, geo, optional cloning, run bundles, and (later) the detection channels all live in one place. With SEART, repo selection is one tool and everything else is your own separate scripts.
6. **Roadmap off the same core.** The planned GraphQL batching engine (`corpus-building-efficient-engineering.md` §9–§10) raises gitcrawl's ceiling from ~20,000 to hundreds of thousands of repos per 4 hours, and the planned commit/LOC work closes the one metric gap where SEART is ahead.

---

## 5. Worked comparison: the thesis Rust frame

Filter: Rust, not a fork, pushed since a cutoff, at least 100 stars, plus "has a Dockerfile" and "owner located in Germany."

| Step | SEART | gitcrawl |
|---|---|---|
| Find candidates | Instant query over their catalog (if Rust is in their crawl scope) | Live search, sharded to beat the 1,000-result cap |
| Star/date/license filters | Yes, precomputed | Yes, from search results (free) |
| Repos with fewer than 10 stars | Invisible (crawler floor) | Included by choice |
| Current counts as of today | As of their last crawl | As of `ran_at` |
| "Has a Dockerfile" | **Not possible** | One file-listing call per repo |
| "Owner in Germany" | **Not possible** | Geo pipeline: one call per owner, cached |
| Evidence artifact | Catalog export | Bundle: filter spec, hash, timestamp, API version, raw JSON per repo, flags |
| Time for a 10,000-repo slice | Minutes | About 4 hours for save + one check at the current meters (`corpus-building-efficient-engineering.md` §4), less with batching once built |

Neither column is universally better — the point is that only one column can answer the last two filter questions at all, and only one can prove what GitHub said on the day.

---

## 6. Honest limitations of gitcrawl (as of this writing)

- **A single run is capped small** (500 candidates, 200 saved, 100 checked) until the configuration is raised; SEART has no such caps.
- **Commit and LOC filters are recorded but not enforced yet** — SEART is ahead here today.
- **GraphQL batching is designed but not built**, so saving is still one request per repo.
- **It needs infrastructure**: Postgres, Redis, a token, and operational care. SEART needs a browser.
- **You own the rate-limit responsibility.** gitcrawl paces itself and records fingerprints, but the token and its behavior are yours (see `corpus-building-efficient-engineering.md` §5).

---

## 7. Bottom line

SEART is a catalog: fast, citable, precomputed, and fixed in what it contains. gitcrawl is an instrument: live, custom-filterable, evidence-producing, and reproducible. For a one-off sample of standard criteria, use SEART. For a thesis corpus whose every number must be traceable to a timestamped GitHub response — and whose filters include things no catalog stores — gitcrawl is the better tool.

The three differences that decide this project all point the same way: **owner location**, **stars below ten**, and **file presence** are exactly the questions a prebuilt catalog cannot answer, and the file machinery is the same machinery that will detect coding agents later. The batching work already designed is what will make gitcrawl fast enough to be the *only* tool needed.

---

## 8. Glossary

- **Catalog vs. instrument**: pre-collected data you query (SEART) versus a live measurement you run (gitcrawl).
- **Crawl**: a server periodically walking GitHub and storing what it finds.
- **Dump**: a downloadable copy of a catalog's database.
- **Virtual filter**: a condition GitHub's own search cannot express, computed by the tool (Dockerfile presence, owner country, commit counts).
- **`ran_at`**: the exact moment a run measured GitHub.
- **Run bundle**: gitcrawl's frozen artifact — spec, hash, timestamp, API version, raw per-repo responses, flags.
- **Incompleteness flags**: named, explicit notes about anything a run could not finish.
- **10-star floor**: SEART's minimum-star crawl rule; gitcrawl has none.

---

## 9. Sources

- SEART GitHub Search — `https://seart-ghs.si.usi.ch/`
- SEART GitHub Search source and README (crawler settings, dumps, Docker images) — `https://github.com/seart-group/ghs`
- Dabić, Aghajani, Bavota, "Sampling Projects in GitHub for MSR Studies" (MSR 2021); Zenodo DOI `10.5281/zenodo.4588464`
- Dabić, Tufano, Bavota, "SEART Data Hub" (arXiv 2409.18658, 2024)
- "Removing Noise or Introducing Bias? The Hidden Cost of MSR Filtering" (arXiv 2609.18953) — third-party analysis of ~1.9M SEART repos, 1.57M clean baseline, and the limits of the 10-star threshold
- gitcrawl data flow — `design/how-the-data-flows.md`
- gitcrawl throughput, meters, and batching design — `design/corpus-building-efficient-engineering.md`
- Full market landscape, capability gap, and the `gitcrawl` name collision — `design/landscape-comparison.md`
