# The LOC Dilemma

**Date**: 2026-10-04. Companion to `how-the-data-flows.md`, `corpus-building-efficient-engineering.md`, and `findings/06-exhaustive-parameters.md`. No prior GitHub API knowledge assumed — every term is introduced where it's first used.

**Purpose of this document**: `min_loc`/`max_loc` are the last virtual filters that are recorded but not enforced (ruling R44). Before wiring them, two questions need honest answers: *can we get lines of code without cloning*, and *what does "LOC" actually mean?* This document answers both, records the measurement that exposed the problem — the same filter returned 12,383 repos from SEART and 16,382 from our live estimate — and ends with the design we should build.

---

## The one-paragraph version

GitHub never tells you how many lines a repository has. Not in search, not in the repo endpoint, nowhere — the language bar is measured in *bytes*. Any LOC filter is therefore one of three things: an estimate, a borrowed count, or a count you perform somewhere else. We applied the same filter ("Python, 10+ stars, 50+ commits, 5,000+ LOC, forks excluded, window starting 2025-10-01") to SEART, which counts lines with `cloc`, and to our live bytes-based estimate. SEART said **12,383** repositories; we said **16,382**. The 4,000-repo gap looked like estimator error. It is far more likely that the two tools answered two different questions with the same word: "lines." This document explains why, how to prove which side the difference belongs to, and how to wire `min_loc`/`max_loc` so the number we report is defensible.

---

## 1. The fact that starts everything

GitHub's language breakdown comes from **Linguist**, the open-source library behind the colored bar on every repo page. Linguist goes through the files, excludes binary data, vendored code, generated files, documentation, and prose, and reports the remaining **bytes per language**.

Bytes. Not lines. There is no `lines` field in the REST API, the GraphQL API, or search. Linguist even ships a single-file inspector that does report line counts (`github-linguist file.rb` → `105 lines (96 sloc)`), but GitHub.com does not expose that output through any endpoint.

**Plain effect:** a "5,000 lines" filter is never a lookup. It is a measurement *you* perform, and every measurement method has a cost, a definition, and an error bar.

## 2. What we measured (the experiment)

One filter, two tools:

| | SEART GitHub Search | Live pipeline (bytes estimate) |
|---|---|---|
| Query | `language:python` + stars ≥ 10 + commits ≥ 50 + LOC ≥ 5,000 + forks excluded + window from 2025-10-01 | same, with LOC computed locally |
| Result | **12,383 repositories** | **16,382 repositories** |
| Difference | — | **+3,999 (~32% more)** |

The other conditions in that filter are server-side searchable (stars, language, window) or near-agreed (commit counts, fork flags). LOC is the one condition each side measures itself, with a different tool. So the gap lives there — but *where exactly* is a question worth answering properly.

## 3. The two "5,000"s are not the same 5,000

SEART filters on `cloc`'s **Code Lines**: physical lines that contain code — no blank lines, no comment lines. Its search form offers three separate metrics: Non-Blank Lines, Code Lines, Comment Lines.

Our bytes-to-lines estimate produces **total physical lines**: everything that ends in a newline — code, comments, and blanks together. A byte count cannot tell a comment from a statement; both are just text.

A worked example makes the gap obvious:

| Repo | Code | Comments | Blanks | Total | SEART sees | We see |
|---|---|---|---|---|---|---|
| A | 3,800 | 1,100 | 400 | 5,300 | 3,800 → fails 5,000 | 5,300 → passes |

Python is unusually comment-heavy (module docstrings, function docstrings, and `#` comments all count as comment lines), so repos sitting just under the line in code terms are everywhere. At a hard cutoff, a definitional difference of 20–30% is not a rounding error — it is roughly a third of the population. The measured per-repo error of our estimate (±20–30%) and the size of the population gap (+32%) line up almost exactly, which is why the definitional explanation is the leading one.

**Plain effect:** before calling either number wrong, check whether the two tools are counting the same thing. They are not.

## 4. The other suspects (and how to rule them out)

The definitional difference is the leading explanation, not the only one. Three more deserve a check:

- **Catalog lag.** SEART is a crawl-time database: it knows what its crawlers collected, and how recently. Repos created or pushed after its last pass may simply be missing from their 12,383 — meaning some of our "extra" 4,000 are repos SEART has not seen yet.
- **Commit count definitions.** Our `min_commits` uses the default branch's commit count from the GitHub API. SEART counts commits from its own crawl, which may include other branches or count differently. A different denominator changes the candidate set before LOC even runs.
- **Fork handling.** Both sides exclude forks, but the flag comes from different sources (GitHub's `fork` field vs SEART's stored flag). Worth a spot check, not a rewrite.

None of these can be settled by staring at totals. The afternoon experiment in §6 settles all of them for the cost of a small download.

## 5. Every way to get LOC without cloning

Ranked from cheapest to most exact. "Without cloning" means no `git clone` on our machine — some options download a zip, which is a content download, not a git history clone; that distinction is called out where it matters.

**1. Do math on GitHub's own numbers** (`GET /repos/{o}/{r}/stats/code_frequency`)
GitHub tracks weekly additions and deletions. Summed over history, `additions − deletions` approximates the lines the repo has today. This is *counted* data, not bytes — but the endpoint only works for repositories with **fewer than 10,000 commits** (it returns `422` above that), and it excludes merge commits. The sibling `stats/contributors` endpoint returns zeros for additions/deletions in repos of 10,000+ commits, so it is strictly worse. Useful for small repos and as a cross-check; useless for the big ones.

**2. Estimate from file sizes** (the tree call we already make)
`GET /git/trees/{branch}?recursive=1` lists every file with its exact **size in bytes**. Sum the code files, divide by a per-language "average bytes per line" constant, and you have an estimate. We already fetch this tree to answer `has_dockerfile`, so on runs that use file filters the LOC estimate costs zero extra calls. Limits: the response truncates at 100,000 entries or 7 MB (flagged as `truncated`; fall back to walking subtrees), and the estimate inherits the definitional problem of §3 — bytes cannot separate code from comments.

**3. Borrow a count someone already made** (zero GitHub calls)
- **SEART** has `cloc` counts (code, comment, blank lines) for ~1.9M repositories. If the repo is in their catalog, their number is a real count — just not from today.
- **World of Code** stores the actual file contents; counting blob lines to sample projects by LOC is a documented use case. It is snapshot infrastructure, but the count is real and offline.
- **Open Hub (Black Duck)** has `Ohcount` line counts for ~256k registered projects, with an API. Coverage is narrow and staleness varies.

**4. Ask a service that counts for you** (spot checks, not bulk)
tokei.rs (`?category=code`), cloc.info, ghloc.dev, and SonarCloud's `ncloc` measure all return real counted numbers for a repo you name. They are fine for validating a handful of repos while building. They are not a pipeline: free services have rate limits, unclear bulk-use terms, and no SLA. (CodeTabs' LOC API, once a popular option, **shut down on 2026-06-30** — do not build on it.)

**5. Download the zip and count it yourself** (the accuracy ceiling)
`GET /repos/{o}/{r}/tarball/{ref}` downloads the working tree — no git history, so not a clone — and `tokei`/`scc`/`cloc` counts it exactly. This is how we calibrate everything above and how we settle borderline repos. It costs bandwidth, so it runs on samples and finalists, never on the whole search.

## 6. The afternoon experiment (who is right?)

Take 50–100 repositories that are in *our* list but not in SEART's. Download each zip, run `tokei`, and record two columns: **total lines** and **code lines**.

- **If code lines are mostly under 5,000 while totals are over 5,000** → the gap is definitional. Our estimator is fine; we were filtering on total lines while SEART filtered on code lines. Fix: pick a definition, calibrate, document.
- **If code lines are mostly over 5,000** → SEART is missing repos (lag or coverage), and our live number is the more complete one. Fix: keep the estimate, note the catalog gap.

Either outcome is useful and neither is embarrassing — but only one of them can be claimed, and this is the test that decides which.

## 7. How to wire `min_loc`/`max_loc` (the recommendation)

1. **Pick a definition and name it.** Code lines (comparable with SEART) or total physical lines (cheaper, noisier). The filter, the UI label, and the bundle must all say which one.
2. **Estimate in a pass we already make.** When `has_dockerfile` fetches the tree, sum blob sizes for code extensions → bytes → lines using per-language constants. When the tree is truncated, fall back to Linguist `/languages` bytes. Zero extra calls in the common case.
3. **Calibrate on real counts.** Sample 200–500 repos stratified by language, count them with `tokei` from their zips, and fit per-language bytes-per-line constants. Publish the error distribution (the tested per-repo error is ±20–30%).
4. **Store provenance.** Per repo: `loc_estimate`, `loc_method` (`tree_bytes`, `languages_bytes`, `code_frequency`, `borrowed:seart`, …), and a confidence tier. In `field_stats`, record how many repos were estimated vs counted.
5. **Filter with a band, not a knife.** Include when the lower bound clears `min_loc`; exclude when the upper bound misses it; refine only the borderline band — `code_frequency` for repos under 10k commits, zip + `tokei` when exactness is required.
6. **Borrow before estimating.** If SEART / World of Code / Open Hub already counted the repo, use their number and record the source. A real count from last month beats a fresh estimate.
7. **Never present the estimate as exact.** The console and bundle call it an estimate with its method. The R44 warning changes from "unavailable" to "estimated" — honest either way, and the filter finally works.

## 8. What this means for the thesis

A LOC number without its method is not comparable. The replication record should state: the definition (code lines), the counter (`cloc` version, via SEART or our own), the window, and the measured difference between the two paths (12,383 vs 16,382, with the §6 test explaining it). Cross-stack comparisons — Rust vs Python vs the other frames — only hold if every stack is measured the same way. That sentence belongs in the methodology, not in a footnote.

## Glossary

- **Physical lines** — every line ending in a newline, including comments and blanks.
- **Code lines** — physical lines containing code; comments and blanks excluded (cloc's "Code Lines", Sonar's `ncloc`, tokei's "Code").
- **Comment lines / blank lines** — the other two columns every counter reports.
- **Bytes per line** — the calibration constant that converts a byte count into a line estimate; differs per language and per codebase style.
- **Estimator** — any method that produces LOC without counting lines (bytes math, additions/deletions math).
- **Calibration** — counting a sample exactly and fitting the estimator's constants to it.
- **Cutoff classification error** — at a hard threshold, per-repo error turns into population error: repos near the line get sorted differently even when the estimator is unbiased.
- **Catalog lag** — the time between a repository changing and a pre-crawled catalog (SEART, Open Hub) learning about it.
- **Linguist** — GitHub's language detector; counts bytes, excludes vendored/generated/docs/prose.

## Sources (verified 2026-10-04)

- GitHub repository statistics — `code_frequency` (weekly additions/deletions; <10,000 commits) and `contributors` (zeroed additions/deletions at 10k+): `https://docs.github.com/en/rest/metrics/statistics`
- Git trees — blob `size`, 100,000-entry / 7 MB recursive limit, `truncated` flag: `https://docs.github.com/en/rest/git/trees`
- Linguist — what it excludes and the bytes-based language bar: `https://github.com/github-linguist/linguist/blob/main/docs/how-linguist-works.md`
- GraphQL `Commit.additions` / `deletions` (for sampling, not bulk): `https://docs.github.com/en/graphql/reference/commits`
- SEART GitHub Search — LOC metrics via `cloc` (Code / Comment / Blank Lines): `https://seart-ghs.si.usi.ch/`; source: `https://github.com/seart-group/ghs`
- World of Code — blob contents and LOC-based sampling: `https://worldofcode.org/`; Tutko et al., *More Effective Software Repository Mining* (`https://arxiv.org/pdf/2008.03439`)
- Open Hub — `Ohcount` line counts and API: `https://openhub.net/tools`
- Server-side counters — tokei.rs (`https://tokei.rs/`), cloc.info (`https://cloc.info/`), ghloc (`https://ghloc.dev/`), SonarCloud measures (`https://sonarcloud.io/web_api/api/measures`)
- CodeTabs shutdown (archived 2026-06-30): `https://github.com/jolav/codetabs`

## Where to go next

- The pipeline this plugs into: `how-the-data-flows.md` (Stage 4, virtual filters).
- The metering and cost model: `corpus-building-efficient-engineering.md` §4 (the touches rule).
- The current recorded-only behavior and R44: `docs/development-log.md`.
- Implementation, when approved: an estimator module + calibration harness + `field_stats` provenance + the console un-greying of `min_loc`/`max_loc`.
