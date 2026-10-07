# The LOC Dilemma

**Date**: 2026-10-04. Companion to `how-the-data-flows.md`, `corpus-building-efficient-engineering.md`, `findings/06-exhaustive-parameters.md`, and `adoption-window.md`. No prior GitHub API knowledge assumed — every term is introduced where it's first used.

**Purpose of this document**: `min_loc`/`max_loc` are the last virtual filters that are recorded but not enforced (ruling R44). Before wiring them, two questions need honest answers: *can we get lines of code without cloning*, and *what does "LOC" actually mean?* This document answers both, records the measurement that exposed the problem — the same filter returned 12,383 repos from SEART and 16,382 from our live estimate — and ends with the fallback architecture we should build: how every available source works together, and how per-language filtering changes the answer.

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
- **Commit count definitions.** Our `min_commits` uses the default branch's commit count from the GitHub API (captured in the batched hydration query, with a REST fallback per repo). SEART counts commits from its own crawl, which may include other branches or count differently. A different denominator changes the candidate set before LOC even runs.
- **Fork handling.** Both sides exclude forks, but the flag comes from different sources (GitHub's `fork` field vs SEART's stored flag). Worth a spot check, not a rewrite.

None of these can be settled by staring at totals. The afternoon experiment in §7 settles all of them for the cost of a small download.

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

**Which one guarantees accuracy?** Only Option 5. Option 1 is real counted data but net-over-history (not today's files) and unavailable on big repos; Option 2 is a measured estimate, not a count. Everything else is as good as its distance from Option 5 — which is why the fallback architecture below uses it as the arbiter.

## 6. The fallback architecture (how the five options work together)

The principle: **every repo gets the best number available at the lowest cost, and only the repos near the cutoff get expensive treatment.** Each tier answers or hands off cleanly.

| Tier | Source | Role | Cost | Accuracy |
|---|---|---|---|---|
| **0. Reuse** | Our own cache + borrowed dumps (SEART, Open Hub, WoC, SWH) | Answer from what's already known | Free | Exact if the repo hasn't been pushed since the count was taken; otherwise stale |
| **1. GitHub's math** | `stats/code_frequency` (added − removed) | Borderline refinement + cross-check | 1 call, <10k commits only | Counted, but net-over-history, not today's file count |
| **2. Tree bytes** | Blob sizes from the tree call we already make (+ `/languages` fallback when truncated) | The everyday estimate for the whole search | ~0 extra calls | ±20–30%, calibrated |
| **3. Hosted counters** | tokei.rs / cloc.info / ghloc | Refinement when we can't download ourselves | Rate-limited | Near-exact for the ref they counted |
| **4. Archive + tokei** | Tarball → local count | The arbiter: borderline cases + calibration | Bandwidth only | Exact for that commit |
| **5. Offline datasets** | WoC / SWH / Stack v2 / BigQuery | Not live — computes priors that feed Tier 0 | Batch compute | Exact for the snapshot |

**The flow for one repo:**

1. **Tier 0 lookup first.** A count is valid for a commit. If the repo's `pushed_at` is older than the borrowed/cached count's date, that number is still exact — use it and stop. Borrowed counts aren't automatically stale; they're stale only if the repo moved since.
2. **Otherwise Tier 2 estimate** (the tree call is already paid for `has_dockerfile`), with the calibration constant for its language.
3. **Filter with a band, not a knife.** Include if the lower bound clears `min_loc`; exclude if the upper misses; the gray zone (say ±20%) goes to refinement.
4. **Gray-zone refinement, cheapest-first:** Tier 1 if the repo is under 10k commits → Tier 4 tarball if the bandwidth budget allows → Tier 3 hosted counter if we're bandwidth-capped. First exact answer wins; record which tier produced it.
5. **Never block a run.** Refinements are budgeted (e.g., 200 archives per run) and deadline-aware; whatever wasn't refined stays labeled *estimated*.

**Two rules that make the cascade coherent:**

- **One definition.** Option 1's net lines and Option 2's bytes both approximate *total physical lines*; SEART's `cloc` number is *code lines*. Pick total physical lines as the canonical metric, keep code lines separately when a real counter ran, and never mix the two inside one filter.
- **Everything carries provenance.** Store `loc_value`, `loc_method` (`cached`, `borrowed:seart`, `gh_code_frequency`, `tree_bytes`, `tokei_archive`, `service:tokei.rs`), `loc_confidence` (exact / counted / estimate), `loc_commit` (the SHA it belongs to), and `loc_at`. The run bundle reports the mix per method.

**And one loop that keeps it honest:** every so often, sample ~300 repos across languages, run Tier 4 on them, refit the per-language bytes-per-line constants, and measure Tier 1's bias against the exact counts. Version the constants (`calibration v2`), and record the error distribution in `field_stats`. That is what turns Tier 2 from a guess into a measured estimate.

## 7. The afternoon experiment (who is right?)

Take 50–100 repositories that are in *our* list but not in SEART's. Download each zip, run `tokei`, and record two columns: **total lines** and **code lines**.

- **If code lines are mostly under 5,000 while totals are over 5,000** → the gap is definitional. Our estimator is fine; we were filtering on total lines while SEART filtered on code lines. Fix: pick a definition, calibrate, document.
- **If code lines are mostly over 5,000** → SEART is missing repos (lag or coverage), and our live number is the more complete one. Fix: keep the estimate, note the catalog gap.

Either outcome is useful and neither is embarrassing — but only one of them can be claimed, and this is the test that decides which.

## 8. How to wire `min_loc`/`max_loc` (the recommendation)

1. **Pick a definition and name it.** Code lines (comparable with SEART) or total physical lines (cheaper, noisier). The filter, the UI label, and the bundle must all say which one.
2. **Estimate in a pass we already make.** When `has_dockerfile` fetches the tree, sum blob sizes for code extensions → bytes → lines using per-language constants. When the tree is truncated, fall back to Linguist `/languages` bytes. Zero extra calls in the common case.
3. **Calibrate on real counts.** Sample 200–500 repos stratified by language, count them with `tokei` from their zips, and fit per-language bytes-per-line constants. Publish the error distribution (the tested per-repo error is ±20–30%).
4. **Store provenance.** Per repo: `loc_estimate`, `loc_method` (`tree_bytes`, `languages_bytes`, `code_frequency`, `borrowed:seart`, …), and a confidence tier. In `field_stats`, record how many repos were estimated vs counted.
5. **Filter with a band, not a knife.** Include when the lower bound clears `min_loc`; exclude when the upper bound misses it; refine only the borderline band — `code_frequency` for repos under 10k commits, zip + `tokei` when exactness is required.
6. **Borrow before estimating.** If SEART / World of Code / Open Hub already counted the repo, use their number and record the source. A real count from last month beats a fresh estimate.
7. **Never present the estimate as exact.** The console and bundle call it an estimate with its method. The R44 warning changes from "unavailable" to "estimated" — honest either way, and the filter finally works.

## 9. Language filters and per-language LOC

This section exists because a "Python LOC" filter has two separate decisions hiding in it, and conflating them produces wrong corpora.

**Selection vs measurement.** GitHub decides *whether* a repo is a "Python repo" (the `language:` qualifier). We decide *what to count* once it's in (Python-only lines or whole-repo lines). Keep them separate.

### 9.1 Python-only, or the whole repo?

Both are available, so the choice is ours — and it must be written down:

- **GitHub `/languages`** returns bytes *per language*, so the Python slice is available directly.
- **Tree bytes (Option 2)** can filter to `.py` files only, or sum every code file.
- **tokei/cloc (Option 5)** gives exact per-language rows — Python-only is exact.
- **`code_frequency` (Option 1)** is whole-repo only; GitHub does not break additions/deletions down by language.
- **SEART** stores per-language metrics — but verify whether its "Code Lines" filter uses the Python row or the sum across languages before comparing.
- **The `min_language_bytes`/`max_language_bytes` virtuals (added 2026-10-06)** enforce the primary-language slice from the per-language byte counts captured during hydration — the GraphQL repo query's `languages(first: 10) { edges { size node { name } } }`, so no extra call per repo — budget-aware, with the byte count recorded per result. This is the first tier of the fallback architecture (§6) implemented in code.

For a Python-project filter, **count Python-only** — but **store both** (Python lines, total lines, Python share). Under `language:python`, Python can be as little as ~30% of a repo, so Python-only LOC can be a fraction of the repo's total. If the thesis says "5k+ LOC" and a reviewer reads it as repo-total, the wrong number is being defended.

### 9.2 What percentage gets a repo picked?

**There is no percentage threshold.** `language:python` means *Python has more code bytes than any other single language* — a plurality, not a majority:

| Repo | GitHub sees | `language:python`? |
|---|---|---|
| 70% Python / 20% JS / 10% others | Python plurality | ✅ |
| 40% Python / 35% JS / 25% Rust | Python still the largest single language | ✅ |
| 45% Python / 55% JS | JS plurality | ❌ — missed, despite 45% Python |
| 49% Python / 26% JS / 25% Rust | Python plurality | ✅ |

Not >50%, not >50.01% — just "strictly more than every other single language." Exact byte ties are broken by Linguist's internal ordering; treat that boundary as fuzzy. Two precision points: the percentages are computed over **code bytes only** (vendored, generated, documentation, data, and prose files are excluded from the denominator first), and GitHub **cannot express** "at least 50% Python" — that is a virtual filter we compute ourselves (Python bytes ÷ total code bytes). "Python-majority" and "Python-primary" are different populations.

### 9.3 The traps

**Classification traps:**

1. **Primary ≠ contains.** `language:python` misses repos that are 45% Python but 55% JS — a large Python population, invisible. It also includes repos that are 30% Python only because the rest is split. If "has meaningful Python" is the goal, a share-based virtual filter is needed, not the search qualifier.
2. **Bytes decide the label, lines don't.** A verbose language can have more lines while having fewer bytes. The language filter and the LOC filter can disagree.
3. **Our estimate's denominator won't match GitHub's.** A raw tree sum counts vendored/generated/docs/data unless we replicate Linguist's exclusions — so it will not reconcile with `/languages` or the language bar. Decide the exclusion rules once and apply them in every tier.
4. **`.gitattributes` can override everything.** `linguist-language=Python` forces files to count as Python; `linguist-vendored` removes them. Rare, but the label is partly the maintainer's choice.
5. **Big repos get no language stats at all.** Linguist only works under 100,000 files — giant monorepos can be invisible to both `language:` and `/languages`.
6. **Language names must be Linguist's.** `language:js` and `language:ts` silently become text searches; the real names are `javascript`, `typescript`, `shell` (not `bash`), `c++`, `jupyter-notebook`. A typo costs the filter with a `200 OK`.
7. **One language per clause.** To cover multiple languages, issue separate queries and merge by `id` (the shard pipeline already does this); don't rely on repeated `language:` clauses OR-ing without a delta test.

**Measurement traps:**

8. **Submodules and LFS aren't counted anywhere.** Tree listings show submodules as commits, LFS files as pointer stubs — including in the tarball + tokei count. Repos with most of their Python in either undercount in *every* tier, even the "exact" one.
9. **Per-language conversion constants.** Bytes→lines must be calibrated per language and computed per language inside mixed repos — never one global constant.
10. **Index lag and near-tie instability.** Search's primary language can trail a recent push (`/languages` is fresher), and adding one file can flip the primary. A repo can be in the Python corpus today and the JS corpus next run — run-to-run diffs are how that becomes visible instead of silent.

**Study-design traps:**

11. **Store the raw material, not just the verdict.** Save per-language bytes and the Python share on each row, so "Python-majority ≥ 50%" can be re-derived later without refetching.
12. **Cross-stack comparability.** If Python uses Python-only lines and Rust uses repo-total lines, the adoption comparison is contaminated. Fix one definition for every stack.
13. **Empty and unrecognized repos** have no primary language and are invisible to any `language:` filter — count them as attrition if the frame cares.
14. **The SEART gap may be two definitional gaps stacked:** their "Code Lines" scope (primary-language vs total) *and* the language-share rule behind their selection. Verify both before attributing the 4k delta to comments and blanks alone.

## 10. What this means for the thesis

A LOC number without its method is not comparable. The replication record should state: the definition (code lines in the primary language, vendored excluded), the counter (`cloc` version, via SEART or our own), the window, and the measured difference between the two paths (12,383 vs 16,382, with the §7 test explaining it). Cross-stack comparisons — Rust vs Python vs the other frames — only hold if every stack is measured the same way. That sentence belongs in the methodology, not in a footnote.

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
- **Primary language** — the language with the most code bytes in a repo (a plurality, not a majority).
- **Fallback tier** — one step of the cascade in §6; each tier either answers or hands off to the next.

## Sources (verified 2026-10-04)

- GitHub repository statistics — `code_frequency` (weekly additions/deletions; <10,000 commits) and `contributors` (zeroed additions/deletions at 10k+): `https://docs.github.com/en/rest/metrics/statistics`
- Git trees — blob `size`, 100,000-entry / 7 MB recursive limit, `truncated` flag: `https://docs.github.com/en/rest/git/trees`
- Linguist — what it excludes and the bytes-based language bar: `https://github.com/github-linguist/linguist/blob/main/docs/how-linguist-works.md`
- Searching for repositories — `language:` qualifier semantics and the qualifier set: `https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories`
- GraphQL `Commit.additions` / `deletions` (for sampling, not bulk): `https://docs.github.com/en/graphql/reference/commits`
- SEART GitHub Search — LOC metrics via `cloc` (Code / Comment / Blank Lines): `https://seart-ghs.si.usi.ch/`; source: `https://github.com/seart-group/ghs`
- World of Code — blob contents and LOC-based sampling: `https://worldofcode.org/`; Tutko et al., *More Effective Software Repository Mining* (`https://arxiv.org/pdf/2008.03439`)
- Open Hub — `Ohcount` line counts and API: `https://openhub.net/tools`
- Server-side counters — tokei.rs (`https://tokei.rs/`), cloc.info (`https://cloc.info/`), ghloc (`https://ghloc.dev/`), SonarCloud measures (`https://sonarcloud.io/web_api/api/measures`)
- CodeTabs shutdown (archived 2026-06-30): `https://github.com/jolav/codetabs`

## Where to go next

- The pipeline this plugs into: `how-the-data-flows.md` (Stage 4, virtual filters).
- The metering and cost model: `corpus-building-efficient-engineering.md` §4 (the touches rule).
- The adoption study this feeds: `adoption-window.md` (cohorts, cutoffs, and why commit counts are the ruler).
- The current recorded-only behavior and R44: `../docs/development-log.md`.
- Implementation, when approved: an estimator module + calibration harness + `field_stats` provenance + the console un-greying of `min_loc`/`max_loc`.
