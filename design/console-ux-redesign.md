# Console UX Redesign — Flow and Interaction Design

**Date**: 2026-10-02. Status: approved in the visual brainstorming session of 2026-10-02; ready for implementation planning.
**Companions**: `design/console-spec.md` (backend/route contract — still authoritative), `design/how-the-data-flows.md` (what the system does), `design/corpus-building-efficient-engineering.md` (limits and batching), `design/landscape-comparison.md` (why).
**Scope**: information architecture and interaction design only. Styling is out of scope (current styling stays). No new GitHub behavior: every cost statement in the copy deck reflects what the backend already does or what the two queued plans (`2026-10-02-graphql-batch-engine.md`, `2026-10-02-console-settings.md`) add.

---

## 1. The problem this fixes

The console is feature-complete but organized by what the code can do, not by what the operator is trying to do. The audit found:

- A ~40-control Find form with four submit buttons that silently ignore each other's inputs.
- Runs identified by an 8-character filter hash; the actual query is visible nowhere.
- `Diff`, `Metrics`, and `Health` are orphaned (no nav, no inbound links).
- Pipeline jargon as primary copy: `fetched/inserted/unchanged/skipped`, `shards incomplete`, `count_parity`, `Queue PEL`, `422 rate`, `partial`.
- Dead ends: no back links from a run, no filter recap, empty states without next actions.
- Naming split four ways: Find / filter / run / Replay / Resume.
- Misleading affordances: a dashboard example query GitHub rejects; Export downloads the latest ready run for the hash, not the run on screen.

## 2. Design principles (apply to every screen)

1. **Plain words first.** Internal vocabulary is never a label. If a term must appear (`q`, hashes, mode names), it sits behind a "Technical details" disclosure.
2. **Every action explains itself.** One line under or beside each button: what it does, whether it calls GitHub, and what it costs (search request / API allowance / local only).
3. **Every number explains itself.** Counters carry a permanent one-line meaning (chosen over tooltips: visible without hovering).
4. **Every status explains itself.** Waiting / Running / Finished / Finished — incomplete / Failed, always with the reason when not clean.
5. **Progressive disclosure, never hiding.** Everything stays reachable; sections collapse but are on the page or one click away.
6. **No dead ends.** Every page offers the next step for its state; every page is reachable in ≤2 clicks from Home or nav.
7. **Honest data.** Approximate counts show `~`; incomplete runs say so; frozen data states its frozen time; unavailable repos are counted and named.
8. **Works without JS** for read paths and forms (current behavior is preserved).

## 3. Mental model and navigation

**Model: Search → Corpus → Detection.** A Search queries GitHub live. Its finished results can be frozen into a named **Corpus**. Detections run on a frozen corpus so numbers are reproducible.

Primary nav: `Home · Searches · Corpora · Detections · Library · System`, plus a **status dot** in the header (green/amber/red) linking to System.

### 3.1 Term map (what the operator sees)

| Today | Becomes |
|---|---|
| Find / Run / filter hash | **Search**, shown as “rust · 100+ stars”, never a hash |
| Replay | **Search again** (new run, same filter) |
| Resume | **Resume** (failed searches only; starts over safely, explained) |
| `partial` | **Finished — incomplete** (with reason) |
| queued / running / done / failed | Waiting / Running / Finished / Failed |
| filter-spec, virtual filters | **Filters**; “extra rules (files, country, activity)” |
| fetched / inserted / updated / unchanged / skipped | **Found / Saved / Passed filters / Unavailable** |
| run_items, bundle | Results, Export |
| SLO dashboard / PEL / p95 / 422 | **System → Performance**: requests left, failures, median response |
| quality checks (`count_parity`, …) | **Data quality**: grouped, sentence-form checks |
| clone modes (shallow/file-only/windowed) | Same names + a one-line trade-off each |

### 3.2 Cross-link graph (target)

```
Home ─┬─> New search ──> Search (detail)
      ├─> Search (recent rows, each with its next action)
      ├─> Corpora ──> Corpus ──> Detection (future)
      ├─> Library ──> New search (prefilled) | Run now
      └─> System (Status / Performance / Limits)

Search ─┬─> View all results (full table)
        ├─> Export results
        ├─> Clone code…
        ├─> Freeze as corpus… ──> Corpus
        ├─> Search again
        ├─> Resume (only when failed)
        ├─> Compare with an earlier search (diff)
        └─> Back to Searches
```
Every orphan today (`/runs/{id}/diff`, `/metrics`, `/health`) gets an inbound link in this graph.

## 4. Screen specifications

### 4.1 Home — Command center

- Three job cards: **Start a search** (“find repos matching my filters right now”), **Continue a search/corpus** (“pick up where my last build stopped”), **Detect agent use** (“scan a frozen corpus”, greyed until detection ships).
- **Recent work** list: each row = human name + status + result count + relative time + its own next-step button (Watch progress / View results / Resume / Freeze as corpus).
- One quiet **System** line: database · queue · token.
- Empty state: explains the three jobs and links into New search with an example filter.

### 4.2 New search

- **Summary strip** (sticky rail, always visible): plain-English restatement of the filter (“Rust · 100+ stars · this year · no forks”) and a **Check matches** action.
  - Check matches = **one search request**, returns an approximate count (`~4,200`), and covers **GitHub-evaluable filters only**. Copy states: “Has Dockerfile and owner country are applied after fetching, so the final corpus will be smaller.”
- **Common** section (always open): keywords, language, minimum stars, updated since, include forks.
- **Advanced** section (collapsible, remembers state): owner/org, size, followers, topics, created between, license, owner country + confidence, has Dockerfile, team topic, custom properties (enabled only with a single `org:`), and the remaining qualifier groups. **All filters live on this one page.**
- **Unavailable filters stay on the page but render disabled with a one-line reason** (currently `min_loc`/`max_loc`: “Not available yet — needs full-history analysis”). Nothing is silently ignored, and nothing inactive looks active.
- **Every numeric filter offers min and max where the data allows**: native qualifiers already use ranges (stars, forks, size, followers, topics); `min_commits`/`max_commits` are enforced from default-branch commit counts (batch-engine plan, Task 8); LOC min/max are the disabled pair above. Min-only qualifiers (`good-first-issues`, `help-wanted-issues`) are labelled “minimum”.
- **Actions, each with its one-line explanation:**
  - *Run search (live)* — finds matching repos now, saves their details, applies file & country filters, freezes the results; uses API allowance; can take minutes.
  - *Save to library* — keeps this filter preset on this machine; no GitHub calls.
  - *Download file* — portable JSON of this filter; no GitHub calls.
- Validation stays server-side with hints inline; live client-side typo hints are a later enhancement, not required for this design.
- Upload-and-run remains available but moves under a “Use a filter file” disclosure, so it stops competing with the main action.

### 4.3 Running search

- Status line: “Running, step 2 of 3”.
- Three explained stages: **1 Finding repos → 2 Saving details → 3 Applying extra rules**, current stage highlighted; each stage has a one-line meaning (“unchanged repos are free”).
- Progress bar based on saved/discovered, plus a **rough** time estimate; copy says “rough estimate”.
- Reassurance: “You can close this tab — the search keeps running. Failed searches get a Resume button.”
- Live counters use the same explanations as the finished page, including failures and their retry state.

### 4.4 Search (results workspace)

- Header: human name + status + frozen/observation time; actions: **Detect agent use** (greyed, coming soon), **Export results**, **Clone code…**, **Freeze as corpus…**, **Search again**, **Resume** (failed only), **Compare with…** (diff, pre-filled with the previous same-filter search).
- **Results preview** (hero): top 20 rows, compact columns (Repository, Stars, Updated, Language, Country, Flags), with **View all results →**.
- **Side panel — “What happened”** with permanently visible explanations:
  - Found — “Repos GitHub said matched your search.”
  - Saved — “We fetched each repo’s current details.”
  - Passed filters — “Also met your file and country rules.”
  - With a Dockerfile — “Contain the file you asked about.”
  - Unavailable — “Deleted or private by the time we looked.”
- Collapsibles: **What this search asked for** (plain sentence + raw `q` under Technical details), **Data quality**, **Technical details**.
- Clone modal keeps slider/number/mode and adds a one-line trade-off per mode; destination path is surfaced before starting.

### 4.5 View all results

- Full-width page: every column (Repository, Stars, Forks, Updated, Language, License, Country + Confidence, Flags, Saved time), sort, its own filter row (Has Dockerfile / Country / Language), standard pagination, and a **per-page selector** (25 / 50 / 100 / 200).
- All state lives in the URL (`?page=&per_page=&sort=`), so links and Back work.
- Invalid parameters render the page with an inline hint (fixes today’s JSON-400 during htmx swaps).
- Export is available from here with the same explanation as elsewhere.

### 4.6 Freeze as corpus

- Action on a finished search; dialog: name (defaults to filters+date), included count, optional note, and the explanation: “records the exact repos and details; does not copy code; does not call GitHub.”
- Corpora page: Name · Repos · Frozen · From search · Last detection · Actions (Open, Detect greyed).
- Corpus page: frozen-time-is-observation-time explanation, preview + View all results, Export, Detect (greyed until shipped), Rename/Delete with semantics (“deleting a corpus never affects its source search”).
- Backend note: this needs a small `corpora` reference (name, source search, note, frozen time) plus list/detail routes; freezing itself copies nothing because a finished search’s `run_items` are already frozen. A small spec is required (see §7).

### 4.7 Detections (planned)

- Entry points only until the detection plan ships: greyed buttons on Home, Search, and Corpus pages, each with the same explanation: “scans a frozen corpus through GitHub’s API on its own allowance; stores evidence per repo.”
- When shipped, the existing detection plan’s options/detail pages adopt this IA and naming (Detections in nav; corpus as the required source).

### 4.8 Library

- Framing: “A saved filter is the recipe for a search. Running it again searches GitHub live; frozen results stay with each Search.”
- Row: Name · filter in plain words · last used (when + search + result count) · actions.
- Actions: **Run now** (live, explained), **Open in editor** (no calls), Rename (inline edit mode, not an always-visible input), **Delete** (proper confirm dialog: “removes the recipe only; Searches and Corpora are untouched”).
- Empty state explains how to create one.

### 4.9 System

- One page, three sections:
  - **Status**: database / queue / token (present or missing; never values).
  - **Performance**: translated metrics — “Search requests left this hour”, “Failures”, “Median response”, “Queue waiting”, window stated.
  - **Limits**: the settings page from `2026-10-02-console-settings.md` (run caps, request deadline, batching, concurrency), relabeled from snake_case to plain names with help text. Env-pinned fields still show the “set by environment” badge.
- Header status dot: green = all ready; amber = degraded (e.g., Redis fallback, no token); red = database down. Links to System.
- `/health` JSON stays for scripts; the page is the human face.

### 4.10 Quality and Diff

- **Data quality** (per search or corpus): grouped checks in sentence form, each with why it matters and what to do when it warns (e.g., “33% of results have no license — fine for counting, a problem for license-filtered studies”). Raw check names move under Technical details.
- **Diff**: becomes “Compare with…” on a search page; summary in plain words (“new repos: 42 · gone: 7 · changed stars: 118”), tables below; state lives in the URL.

## 5. Global components

- **Explanations**: the copy deck in §6 is the source of truth; new controls ship with a line from it.
- **Error pages**: custom 404/500 HTML with a next step (Back to Home / Back to Searches), replacing today’s inconsistent JSON-vs-HTML and plain-text CSRF responses (CSRF failures re-render the form with a hint).
- **Confirmation dialogs**: destructive actions (delete filter/corpus, cancel clone) use the existing modal with an explicit sentence about scope; `window.confirm` is retired.
- **Empty, loading, error, disabled**: every list has an explanatory empty state with a next action; the busy bar stays; disabled controls say why (title/adjacent line).
- **Keyboard**: the existing shortcuts stay and the help modal is updated for new pages (`g s` Searches, `g c` Corpora, `g y` System suggested; final keys during implementation).
- **Accessibility**: `aria-current` on nav, dialog focus trap (existing), status text not color-only.

## 6. Copy deck (the explanations the operator will see)

**Actions**
- Run search (live) — “Finds matching repos now, saves their details, applies file and country rules, and freezes the results. Uses your GitHub allowance; can take minutes.”
- Check matches — “One search request. Approximate count of repos GitHub can evaluate; extra rules (files, country) will shrink this when you run.”
- Save to library — “Keeps this filter on this machine for reuse. No GitHub calls.”
- Download file — “Exports this filter as a portable JSON file. No GitHub calls.”
- Search again — “Starts a new search with the same filter. The old results stay frozen.”
- Resume — “Only for failed searches. Clears partial work and starts over safely.”
- Freeze as corpus — “Records the exact repos and details as of now. No GitHub calls; does not copy code.”
- Detect agent use — “Scans the frozen corpus through GitHub’s API on its own allowance and stores evidence per repo.” (greyed until shipped)
- Export results — “Downloads the frozen result list as JSON or CSV.”
- Clone code… — “Copies repo contents to this machine. No GitHub API calls; uses disk and time.”

**Counts** — as §4.4. **Statuses** — Waiting / Running / Finished / Finished — incomplete / Failed (+ reason). **Filter sections** — Common and Advanced each open with: “These limit which repos match. GitHub evaluates the first part; extra rules run after fetching.”

## 7. Backend implications (what implementation must add or change)

| Change | Size | Source |
|---|---|---|
| Corpora reference + list/detail/freeze routes (name, source search, note, frozen time) | Small spec + migration | New; needed by model B |
| View-all results route with `page`, `per_page`, `sort` (server-rendered, URL state) | Small | New |
| Custom 404/500 HTML + consistent invalid-param rendering | Small | Fixes audit item |
| Export the viewed search (not “latest ready run for hash”) | Small | Fixes audit item |
| Metrics/labels translation layer (plain labels, window) | Small | Uses existing `metrics.py` data |
| Quality checks → grouped sentence form; raw names behind disclosure | Small | Uses existing `quality.py` data |
| Settings page relabeled to §4.9 (plan already exists) | Plan edit | `2026-10-02-console-settings.md` |
| Detection pages adopt this IA (plan already exists) | Plan edit | `2026-10-02-agent-detection.md` |
| Progress-stage state + rough ETA | Optional | Nice-to-have; stages can be derived from counters without new storage |
| Enforce `min_commits`/`max_commits` (default branch only) | Planned — batch-engine plan, Task 8 | Counts commits reachable from the default branch, snapshot at `ran_at` |
| Enforce `min_loc`/`max_loc` | Out of scope — needs the full-history tier | Greyed in the UI; runs keep the “incomplete” warning (R44); never remove or weaken it |

## 8. Suggested implementation order

1. **Shell**: nav rename + status dot + System page + custom error pages + term map. (No schema.)
2. **New search**: Common/Advanced split, sticky rail, Check matches, action copy.
3. **Search workspace**: header with filter recap, preview + View all, explained counters, action gating.
4. **Library**: Run now, sentence rows, proper dialogs.
5. **Corpora**: entity + freeze dialog + pages; wire greyed Detect.
6. **Settings**: existing plan, relabeled into System → Limits.
7. **Quality/Diff/Metrics translations** and the remaining orphan links.
8. **Detection**: existing plan, adopting IA.

## 9. Success criteria

- A first-time operator can go open → search → freeze a corpus without reading any documentation.
- Every action states its cost; every count, status, and section has a permanent explanation.
- No page is orphaned; every page offers its next step.
- Vocabulary matches §3.1 everywhere (tests assert key labels).
- All read paths and forms still work without JS.

## 10. Open items

- Corpus schema details (name uniqueness, note length, delete semantics) — needs its own small spec.
- ETA: ship as “rough estimate” or omit; decide during implementation.
- Header status-dot thresholds (what counts as degraded amber).
- Keyboard keys for the new nav sections.
- Whether Check matches should optionally count virtual filters by sampling (no, for now — copy states the limitation).
