# The Adoption Window

**Date**: 2026-10-04. Companions: `spec.md` (FR-015–FR-022), `research.md` D13, `loc-dilemma.md`, `corpus-building-efficient-engineering.md`, `findings/06-exhaustive-parameters.md`. No prior knowledge assumed.

**Purpose of this document**: records the answer to a methodology question that came up in review — *when does the study of coding-agent adoption begin, and what exactly gets excluded: repositories or commits?* It gathers the verified release timeline, the two constraints that decide the cutoff, the two-cohort design, what commit counts actually buy the study, the cost reality (including a landmine in the planned data source), and the frame config that expresses all of it.

---

## The one-paragraph version

A study of "the rise and adoption of coding agents" has to decide when observation starts and what gets excluded. Starting in October 2025 (Cursor 2.0) would capture only the newest wave and miss the most interesting growth; excluding repos created before the cutoff would throw away the installed base, which is where adoption actually happens. The better design keeps all repos, windows the commits, and runs two cohorts: **new repos** (adoption at birth) and **existing repos** (diffusion, with each repo as its own before/after control). This file records the verified timeline, the detector floor, the two-cohort design, what commit counts buy us, the GH Archive landmine, and the frame config that expresses it.

---

## 1. Why this exists

The review question was: *"If we measure from the rise of coding agents, should that be repo-level exclusion or commit-level exclusion?"* Repo-level (drop repos created before the cutoff) leaves barely a year of new repos and excludes the installed base. Commit-level (keep all repos, observe only commits after the cutoff) is more expensive but measures actual adoption. The instinct that prompted this file — that commit-level is fairer and answers the thesis title — is correct. This document makes that instinct precise, and fixes the date.

## 2. The timeline (verified 2026-10-04)

"Coding agents" did not arrive on one day. They arrived in waves:

| Date | What happened | Why it matters |
|---|---|---|
| Jun 2022 | GitHub Copilot GA | First mainstream AI code completion — *assistance*, not an agent |
| Nov 2022 | ChatGPT | Mass awareness; leaves almost no commit traces |
| May 2023 | **Aider** launched (repo created 2023-05-09; Show HN May 11) | First widely used agent-ish CLI; auto-commits with a detectable prefix |
| Mar 2024 | Devin announced | First "AI software engineer" — minimal real traction, per its own history |
| Feb 2025 | **Claude Code** released | `CLAUDE.md` and "Generated with Claude Code" traces begin |
| Apr–May 2025 | Codex CLI (Apr 16); OpenAI Codex cloud agent research preview (May 16) | Second major agent; GA Oct 6, 2025 |
| Aug 19, 2025 | **AGENTS.md** published | The cross-agent convention; 20k repos within a week, 60k+ now |
| Oct 29, 2025 | **Cursor 2.0 / Composer** | Cursor's first own agentic model + multi-agent UI — the newest wave |

**Plain effect:** "October 2025" is the newest wave's flagship release, not the rise. Starting there measures *"what happened since late 2025"* — a different study.

## 3. Two constraints decide the cutoff

**Constraint 1 — waves, not points.** Picking one date bakes in one tool. Any cutoff is a choice, and it should be justified and tested (see §4), not derived from a single product launch.

**Constraint 2 — the detectors have a birthday (the detector floor).** Our trace packs detect files and trailers that did not exist before their tools did:

- `CLAUDE.md` cannot exist before Feb 2025.
- `AGENTS.md` cannot exist before Aug 2025.
- Cursor rules and Aider commit prefixes exist earlier, but coverage varies per signal.

A commit from 2023 with no traces is **not evidence of non-adoption** — the signal did not exist yet. This is a measurement floor, not a finding, and it must be stated wherever pre-floor history is shown.

## 4. Choosing the cutoff

**Recommendation: `2025-01-01`** for an "agentic coding agents" thesis — it matches the first real agentic wave (Claude Code, Codex, then AGENTS.md) and the detector floor for the file-based signals. With today's date, that gives the new-repo cohort ~21 months instead of the ~12 months an October 2025 cutoff would leave.

Run **robustness checks** at:

- `2024-01-01` — includes the Devin wave and early Cursor/Aider adoption;
- `2025-10-29` — the Cursor 2.0 wave, for the narrow "newest tools" reading.

If conclusions flip between cutoffs, that is a finding about the diffusion curve, not a bug. If the thesis wants Copilot-era *assistance* too, that is a different construct ("AI-assisted" vs "agentic"), needs its own signal family (co-author trailers), and its own window — do not blur the two.

## 5. Repo-level vs commit-level: two different questions

The two options answer different questions, and the thesis title picks the answer:

- **Repo-level (new repos only)** → *"what share of new projects use agents from birth?"* That is **incidence**. Clean and cheap, but it excludes the installed base and cannot separate "repo created by an agent" from "human project that adopted an agent."
- **Commit-level (all repos, commits after the cutoff)** → *"how does agent use grow inside existing projects?"* That is **diffusion** — what "growth and rising adoption" actually means.

The strongest design runs **both cohorts**:

1. **Cohort A — new** (`created_at ≥ cutoff`): adoption at birth / incidence. Repo-level signals, cheap.
2. **Cohort B — existing** (`created_at < cutoff`, observed for commits in the window): diffusion, with a pre-cutoff baseline per repo. **Each repo becomes its own control**, which is the best available design for adoption.

Report both numbers, labeled: "X% of new repos" and "Y% of existing repos adopted by month N" are different sentences. Reviewers will ask which one is meant.

## 6. What the commit count buys you

This came up directly in review: *"how could the count of commits be useful?"* The non-obvious answer: **the commit count is not a measure of AI use — it is the denominator that turns adoption from a yes/no into a rate.** Presence of a trace says a repo *tried* an agent; the count says how much, how fast, and compared to what.

1. **A denominator for rates.** One agent commit means nothing alone: 1 of 5 commits is agent-driven; 1 of 500 is an experiment. Intensity = agent-signal commits ÷ total commits, per repo, per month — the difference between "tried it" and "uses it."
2. **Fair comparison.** Python and Rust repos commit at different volumes; big repos out-commit small ones. Rates normalize that; raw counts would just rank repos by size.
3. **A behavioral baseline (before/after).** The pre-cutoff count is the control: did adoption change *how much* people commit (volume) and *how* they commit (size, frequency, style), not just whether a trace appeared?
4. **Exposure time for the adoption curve.** Time-to-adoption must be measured in active commits/months, not calendar time — a repo dormant for six months was not "failing to adopt," it was not exposed. The count defines when a repo was at risk, which makes the survival curve honest.
5. **Eligibility and attrition accounting.** A commits ≥ N filter keeps toys and abandoned repos out; the windowed count identifies dormant repos so they are reported as attrition, never silently dropped.
6. **A data-quality check.** The `Link: rel="last"` count versus what was retrieved proves the windowed history is complete — the same discipline as the `id`-set diff in discovery.

**Short version: the count of commits is the ruler, not the thing being measured.**

## 7. The cost reality (and one landmine)

**The GH Archive landmine.** The plan says "BigQuery primary, API top-up" for commit history. But the payload cliff (permanent 2025-10-07) removed `PushEvent.commits` — GH Archive no longer carries per-commit lists for the entire observation window. The BigQuery path is broken for exactly the period that matters. Working alternatives:

- **World of Code** — commit messages survive the cliff; monthly snapshots; offline join. The practical historical source.
- **GraphQL history** — 100 commits per query, and each commit node can carry `additions`/`deletions`/`changedFilesIfAvailable`, so diffstats come in batches, not one call per commit.
- **REST commits** — the fallback for gaps; 1 call per 100 commits, messages only (stats need the single-commit endpoint).

**The math is affordable.** 128k repos × ~500 commits in window ÷ 100 per page ≈ 640k calls ≈ 13 hours on 10 tokens. At 2,000 commits average it is ~50 hours — days, not months. The truly expensive parts are the **file inventory and the two-clone strategy** (tens of GB and weeks of clone time), so those get sampled, not censused.

## 8. Commit-level caveats to design for

1. **Don't mix signal levels.** `CLAUDE.md` presence is a *repo-level* signal; commit-level adoption needs commit-level evidence (co-author trailers, branch prefixes, PR labels). A repo having the file ≠ a given commit being agent-written.
2. **Measure rates, not counts.** Use signals per active month or per commit, or the analysis will just rediscover that big active repos have more of everything.
3. **Survival analysis, not bare percentages.** First-signal dates are time-to-event data; Kaplan-Meier curves per language/ecosystem are the headline figure for "adoption over time."
4. **It is a lower bound.** Opt-outs (Claude), unsigned tools (Pi/OpenCode), and invisible workflows mean measured adoption undercounts. State it; never silently "correct" it.
5. **Survivorship.** Deleted/private repos vanish; WoC and GH Archive recover some history, but the attrition is real and should be counted.
6. **Matching.** Adopters are not random — younger, more active, specific languages. Use pre-cutoff activity as a covariate, or match adopters to similar non-adopters.

## 9. The frame config (proposal)

The filter-spec `frame` field already records study config verbatim (FR-022: `buckets`, `attrition`, `size_splits`, `study_window`, `star_floor`). This design needs a small extension:

```json
{
  "frame": {
    "cutoff": "2025-01-01",
    "cohorts": {
      "new": "created_at >= cutoff",
      "existing": "created_at < cutoff"
    },
    "baseline_window": { "months": 12, "ends_at": "cutoff" },
    "observation_window": { "from": "cutoff", "to": null },
    "metrics": ["incidence", "adoption", "intensity", "time_to_adoption"],
    "robustness_cutoffs": ["2024-01-01", "2025-10-29"]
  }
}
```

Metric definitions, so the numbers are unambiguous:

- **incidence** — share of the new cohort with ≥1 agent signal.
- **adoption** — share of the existing cohort whose first signal falls in the observation window (reported as a survival curve, not a single number).
- **intensity** — agent-signal commits ÷ total commits in the window, per repo and per month.
- **time-to-adoption** — active months (or commits) from the cutoff to the first signal.

This is a proposal, not implemented behavior: `frame` is currently opaque/verbatim (R41), and wiring these metrics is part of the deferred thesis tracks.

## 10. Bottom line

Keep all repos, window the commits, run two cohorts, and report incidence and diffusion separately. Start at `2025-01-01` with robustness at `2024-01-01` and `2025-10-29`, and state the detector floor wherever pre-2025 history appears. Use commit counts as the denominator and baseline — the ruler — and get history from World of Code or GraphQL, not GH Archive, for the post-October-2025 window. Done this way, the thesis measures *growth and rising adoption*, not just *share of new repos*.

## Glossary

- **Cohort** — a group of repos sharing a defining property; here, new vs existing relative to the cutoff.
- **Cutoff** — the date observation begins (per pre-registration; tested with robustness variants).
- **Detector floor** — the earliest date a given signal *could* exist (CLAUDE.md: Feb 2025; AGENTS.md: Aug 2025). Absence before it is not evidence.
- **Incidence** — the share of new repos using agents (a cross-section).
- **Diffusion** — the growth of adoption inside the existing population (the curve).
- **Installed base** — repos that already existed when observation began.
- **Exposure** — active time (months/commits) during which a repo could adopt; dormant time is not exposure.
- **Signal level** — whether evidence lives at the repo level (a file exists) or the commit level (a trailer on a commit).
- **Survival analysis** — the family of methods for time-to-event data; the right tool for "when did adoption happen."
- **Lower bound** — the honest reading of trace-based adoption: real use can be higher, never lower.

## Sources (agentic-tool dates verified 2026-10-04 against the linked pages; Copilot/ChatGPT/Devin are vendor announcements — re-verify before citing)

- Cursor 2.0 / Composer, Oct 29 2025: `https://cursor.com/blog/2-0`
- Aider — repository created 2023-05-09: `https://github.com/Aider-AI/aider`; Show HN May 11 2023: `https://news.ycombinator.com/item?id=35901649`
- Devin announcement, March 2024: `https://cognition.ai/blog/introducing-devin`
- Claude Code, Feb 2025 (as reported alongside the Codex launch): `https://techcrunch.com/2025/05/16/openai-launches-codex-an-ai-coding-agent-in-chatgpt`
- OpenAI Codex cloud agent research preview May 16 2025 (same source); Codex CLI Apr 16 2025: `https://github.com/openai/codex`; GA Oct 6 2025: `https://openai.com/index/codex-now-generally-available`
- AGENTS.md — convention site (60k+ projects, stewarded by the Agentic AI Foundation): `https://agents.md/`; repo created 2025-08-19: `https://github.com/agentsmd/agents.md`; adoption report Aug 27 2025: `https://www.infoq.com/news/2025/08/agents-md/`
- GitHub Copilot GA, June 2022: `https://github.blog/2022-06-21-github-copilot-is-generally-available-to-all-developers/`
- ChatGPT, Nov 30 2022: `https://openai.com/index/chatgpt/`
- GH Archive payload cliff (2025-08-08 changelog → permanent 2025-10-07): `https://github.blog/changelog/2025-08-08-upcoming-changes-to-github-events-api-payloads/`
- World of Code: `https://worldofcode.org/`
- GraphQL commit stats: `https://docs.github.com/en/graphql/reference/commits`
- Frame config today: `contracts/search-api.md` §Corpus-frame config; `spec.md` FR-022

## Where to go next

- The measurement method this depends on: `loc-dilemma.md` (fallback architecture and per-language LOC).
- The thesis tracks that consume these metrics: `spec.md` FR-015–FR-022; `tasks.md` Phase 6 (deferred).
- The data sources for windowed history: `findings/06-exhaustive-parameters.md` §6 and `corpus-building-efficient-engineering.md` §8–§10.
- The run artifacts these numbers live in: `how-the-data-flows.md` Stage 9 (bundle and export).
