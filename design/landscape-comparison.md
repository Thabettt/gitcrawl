# Market Comparison: Does Anything Do What gitcrawl Does?

**Date**: 2026-10-02. Companion to `how-the-data-flows.md`, `corpus-building-efficient-engineering.md`, and `gitcrawl-vs-seart.md`. Facts verified against the tools' public sites, repositories, and papers on 2026-10-02.

**Purpose of this document**: answer a blunt build-or-buy question — *is there already a tool that does what gitcrawl does?* — by surveying the adjacent landscape honestly, showing a capability matrix, and identifying the gap. If the intersection is empty, that emptiness is itself the justification for building it.

---

## 1. The one-paragraph answer

There are many tools that **touch** this problem, but no tool that **is** this problem. The landscape splits into four families: pre-crawled research catalogs (SEART), giant offline archives (World of Code, GH Archive, BigQuery snapshots), analytics dashboards (OSS Insight), and thin API wrappers (`gh search repos`, assistant connectors). Each family solves the part it was designed for. None combines all six things this project needs: **live measurement at a chosen moment, arbitrary filters including file presence and owner location, coverage below 10 stars, frozen per-run evidence with raw upstream responses, ToS-aware paced collection with audit telemetry, and a path to coding-agent detection**. This is not a crowded market with a leader to adopt; it is a set of adjacent tools with a hole in the middle exactly where the thesis sits.

---

## 2. The landscape at a glance

| Family | Examples | What they actually provide | Why it is not gitcrawl |
|---|---|---|---|
| **Pre-crawled research catalog** | SEART GitHub Search + Data Hub | Queryable database of ~1.9M repos continuously crawled and stored by someone else | Fixed columns, 10-star floor, crawl-time freshness, no file/geo filters, no per-run evidence (details in `gitcrawl-vs-seart.md`) |
| **Offline archives at ecosystem scale** | World of Code, GH Archive, BigQuery GitHub datasets | Raw or organized historical data: events, commits, blobs, cross-references | Not a repo-selection tool with live metadata filters; you build the tooling yourself; freshness varies from hourly (GH Archive) to monthly snapshots (WoC) to frozen-in-time (BigQuery snapshot) |
| **Analytics dashboards** | OSS Insight, DevStats, trending/star-history sites | Aggregated insight over events: trends, rankings, comparisons, stargazer geography | Aggregate answers, not frozen corpora; no file-level or owner-profile filters; no reproducible per-repo evidence |
| **Thin wrappers and generators** | `gh search repos`, GitHub MCP servers, Glean connector | Pass-through to GitHub's own search with better ergonomics | No virtual filters, no enrichment, no persistence/bundles, no pacing/audit, no runner; whoever calls it still has to build gitcrawl's real work |
| **Adjacent helpers (complements, not competitors)** | ecosyste.ms, deps.dev, Libraries.io, PyDriller, GitHubMiner, Software Heritage | Metadata mirrors, mining libraries, source-code archives | Useful *inside* gitcrawl (offload, clone analysis); none selects and freezes a live corpus |

---

## 3. Tool-by-tool, honestly

### 3.1 SEART GitHub Search (the closest catalog)
Covered fully in `gitcrawl-vs-seart.md`. In short: a shared, continuously crawled database (~1.9M repos, 35 attributes, 10-star floor, dumps up to 15 days old) with a good web form. Fast, citable, and genuinely better for standard off-the-shelf samples. It cannot filter by owner location or file presence, cannot see below 10 stars, and its timestamp is its crawl time, not yours.

### 3.2 World of Code (the scale champion)
The most impressive infrastructure in the space: V2605 holds roughly **7.3 billion commits, 27 billion file versions (blobs), 351 million repositories (284 million after fork resolution), and 124 million author identities**. It cross-references every commit, blob, and author across all public forges, supports stratified sampling, and offers a browser, Python driver, and API. It is the right answer to "I need the entire population of git objects." It is the wrong answer to "give me the Rust repos that have a CLAUDE.md, an owner in Germany, and today's pushed_at, with a frozen evidence bundle" — WoC is monthly-snapshot infrastructure with no live GitHub metadata filters, no owner-profile geography, no per-run raw API evidence, and real onboarding (registration, server access or API key). It is a complement at the history tier, not the live selection tier.

### 3.3 GH Archive + BigQuery (the raw firehose)
GH Archive records the public GitHub event timeline hourly (archive files and a BigQuery dataset updated every hour); it is the backbone of many downstream analytics. The companion `bigquery-public-data.github_repos` tables are different: a **static snapshot of ~2.8M repositories from 2016**, with community reports that some tables (languages) stopped updating years later. Either way, these are raw data, not a tool: no filters beyond SQL you write, no file-presence checks, no owner geography, no bundles. gitcrawl's own design already treats this tier as a zero-GitHub-cost history source — a complement, not a rival.

### 3.4 OSS Insight (the analytics showcase)
Analyzes **10+ billion GitHub events** in real time, with trending rankings, repo/developer analytics, side-by-side comparisons, a free API, and a natural-language Data Explorer that writes the SQL for you. It even surfaces geography — of *stargazers, issue creators, and PR creators*, derived from events. That is aggregate trend insight about popular projects, not a corpus builder: no file-level filters, no owner-location filter, no frozen per-repo evidence, no low-star sampling, and its population skews toward the projects people already care about.

### 3.5 GHTorrent (the cautionary tale)
The original GitHub research mirror. It stopped updating years ago, shell access ended in 2019, and the site later went down (the domain was reported hijacked in 2024). Any study built on it is historical. Its lesson shapes gitcrawl's design: never depend on someone else's crawl for a number that must be defensible. Own the pipeline, own the evidence.

### 3.6 `gh search repos` and assistant connectors (the DIY baseline)
The GitHub CLI exposes GitHub's search qualifiers (`--stars`, `--language`, `--created`, `--topic`, `--license`, `--size`, and so on) with JSON output, up to 100 per page. MCP servers and enterprise connectors (for example Glean's "Search repositories") are the same thing with friendlier plumbing. All of them inherit GitHub's 1,000-result cap, offer no virtual filters, no batching, no hydration/enrichment, no persistence, no bundles, and no rate pacing beyond GitHub's own errors. They are the tool you would start writing gitcrawl with — and the tool you would still be building after the first week.

### 3.7 Complementary tools (use inside gitcrawl, not instead of it)
- **ecosyste.ms / deps.dev / Libraries.io**: package and repo metadata offloads that cost zero GitHub calls — already part of the design.
- **PyDriller / GitHubMiner**: excellent for mining *after* a corpus is chosen (clone-based); they do not select corpora.
- **Software Heritage**: a long-term source-code archive with its own search-by-origin; archival, not live filtered selection.

### 3.8 Name collision: there is already a tool called `gitcrawl`
An active, unrelated open-source project named **`gitcrawl`** exists (`openclaw/gitcrawl`, Go, ~121 stars, releases through v0.12.0 in September 2026). It is a **local-first GitHub issue and pull request crawler for maintainer triage**: syncs threads into SQLite, clusters them, offers a TUI, and explicitly has no HTTP API. It does not compete functionally — it mirrors conversations for maintainers, while this project samples repository corpora with evidence — but the name overlap is real and has consequences: search confusion, package/binary namespace collisions if this project is ever published, and ambiguity in citations or introductions ("the gitcrawl tool" could mean either).

Options, in rising order of effort: (a) keep the name and state the distinction plainly in the README and papers; (b) rename only published surfaces (binary, package, repo) and keep "gitcrawl" as the working title; (c) rename the project outright. This decision is independent of the functionality and should be made before anything is published.

---

## 4. Capability matrix: the hole in the middle

Legend: ✅ full · ◐ partial/workaround · ❌ absent.

| Capability | SEART | World of Code | GH Archive / BQ | OSS Insight | `gh` / connectors | openclaw/gitcrawl | **this gitcrawl** |
|---|---|---|---|---|---|---|---|
| Live query at a chosen `ran_at` | ❌ (crawl time) | ❌ (monthly snapshots) | ◐ (hourly events) | ◐ (real-time events) | ✅ | ❌ (local mirror) | ✅ |
| Arbitrary GitHub qualifiers | ◐ (fixed form) | ❌ | ❌ (SQL you write) | ❌ | ✅ (GitHub's own) | ◐ (per-repo sync) | ✅ |
| File-level checks (presence of specific files) | ❌ | ◐ (blobs, offline analysis) | ❌ | ❌ | ❌ | ◐ (indexes code locally per repo) | ✅ |
| Owner location / geography filtering | ❌ | ❌ | ❌ | ◐ (stargazer geography, not owner) | ❌ | ❌ | ✅ (with confidence) |
| Coverage below 10 stars | ❌ (crawler floor) | ✅ (all git objects) | ✅ (raw events) | ❌ (trend-skewed) | ✅ | ◐ (repos you sync) | ✅ |
| Frozen evidence bundle with raw upstream JSON | ❌ | ◐ (raw objects, not per-run evidence) | ◐ (raw events, not repo bundles) | ❌ | ❌ | ◐ (local mirrors) | ✅ |
| ToS-aware pacing + audit telemetry | ◐ (their problem, not yours) | ❌ | ❌ | ❌ | ◐ (GitHub throttles) | ◐ | ✅ |
| Path to coding-agent detection | ❌ | ❌ | ❌ | ◐ (aggregate AI rankings) | ❌ | ❌ | ✅ (planned, same file machinery) |

Read the matrix by column: every column has at least one ❌ where gitcrawl has ✅, and the ones that come closest (SEART, WoC) come closest on different axes. No column is full.

---

## 5. Why this justifies the build

1. **The intersection is empty.** The six capabilities that define the thesis's corpus problem never co-exist in one tool. Adopting any existing tool means giving up at least one requirement the study cannot give up — most commonly file presence, owner geography, or timestamped evidence.
2. **The closests alternatives are structurally unable to close the gap.** SEART's catalog cannot grow a file column retroactively without re-cloning the world; WoC's monthly object store cannot answer "today's pushed_at." These are architecture choices, not missing features.
3. **The alternatives carry dependency risk.** GHTorrent's arc (abandoned, site lost) is the extreme case; SEART's open 502 issue and 15-day dump lag are the everyday version. A thesis number that must survive review should not ride on a third party's uptime.
4. **The build is bounded and already partly done.** The pipeline, limiter, run bundles, executor, and UI exist; the gap-closing work is the designed batching engine, which is additive. This is not a from-scratch bet against a funded competitor; it is finishing the only tool shaped for the job.
5. **It is a selling point, stated correctly.** The honest pitch is not "nothing like this exists" — catalogs and archives exist and are good. It is: *"no tool provides live, custom-filtered, evidence-bundled corpus building with owner geography and file-level signals; the available tools each solve an adjacent slice."* That sentence is defensible because this document shows the slices.

---

## 6. Bottom line

Buy SEART for quick standard samples; borrow World of Code for ecosystem-scale history; mine GH Archive for events; watch OSS Insight for trends; script `gh` for one-off lookups. Build gitcrawl for the one job none of them does: a live, timestamped, custom-filtered, fully evidenced corpus — the artifact the thesis actually needs. And before publishing, resolve the `gitcrawl` name overlap with the existing triage tool.

---

## 7. Glossary

- **Catalog**: pre-collected database you query (SEART).
- **Archive**: raw historical data you process yourself (GH Archive, WoC, BigQuery).
- **Analytics platform**: aggregate answers over events (OSS Insight).
- **Wrapper**: ergonomic pass-through to GitHub's own API (`gh`, MCP connectors).
- **Virtual filter**: a condition GitHub search cannot express (file presence, owner country, commit counts).
- **Evidence bundle**: a frozen per-run artifact with raw upstream responses.
- **`ran_at`**: the exact moment a live measurement was taken.
- **Fork resolution / deforking**: collapsing forked copies into one project (WoC).

---

## 8. Sources

- SEART GitHub Search — `https://seart-ghs.si.usi.ch/`; source `https://github.com/seart-group/ghs`; Dabić et al., MSR 2021 (Zenodo DOI `10.5281/zenodo.4588464`)
- World of Code — `https://worldofcode.org/` and `https://worldofcode.org/docs/` (V2605 counts, monthly snapshots, API); Ma et al., MSR 2019 / EMSE 2021
- GH Archive — `http://www.gharchive.org/`; BigQuery hourly dataset
- BigQuery GitHub data — Google Cloud public datasets documentation; "GitHub on BigQuery" (2016 snapshot of ~2.8M repos); community report of stale `github_repos.languages` table
- OSS Insight — `https://ossinsight.io/`, `https://github.com/pingcap/ossinsight`, public API docs
- GHTorrent — `https://github.com/ghtorrent/ghtorrent.org` (shell access ended July 2019; project stopped updating; domain loss reported 2024)
- GitHub CLI `gh search repos` — `https://cli.github.com/manual/gh_search_repos`; Glean GitHub connector docs
- openclaw/gitcrawl (name collision) — `https://github.com/openclaw/gitcrawl`, `https://pkg.go.dev/github.com/openclaw/gitcrawl`
- Project companions — `design/how-the-data-flows.md`, `design/corpus-building-efficient-engineering.md`, `design/gitcrawl-vs-seart.md`
