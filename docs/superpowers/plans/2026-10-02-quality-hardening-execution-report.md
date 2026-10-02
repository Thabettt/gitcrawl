# gitcrawl Quality-Hardening Execution Report

**Date:** 2026-10-02
**Branch:** `quality-hardening` (off `main`; `main` untouched at `c6cb2df`)
**Base commit:** `c6cb2df20e25b518653b5663ff6cf870677cd79d`
**HEAD:** `94a81e22552124e54cfb4f3b86a902fcc0da8fd9`
**Commits on branch:** 43 (1 docs commit + 40 task commits + 1 style commit + 1 final-review docs fix)
**Status:** Program complete; all five plans executed; every gate green.

---

## 1. Baseline vs final verification

| Gate | Baseline (before first source change) | Final | Command |
|---|---|---|---|
| pytest | 1089 tests, 0 failures/errors/skips, exit 0 | **1214 tests, 0 failures/errors/skips, exit 0** | `.\.venv\Scripts\python.exe -m pytest -q` |
| coverage | not measured | **94.74%** (5380 stmts, 283 missed), floor 93 | `pytest -q --cov=src --cov-report=term` |
| ruff | All checks passed | **All checks passed** | `python -m ruff check src tests` |
| black | 7 files would be reformatted | **112 files unchanged** | `python -m black --check src tests` |
| mypy | not installed | **Success: 14 files** | `python -m mypy` |
| Node | 1 test file | **23 tests, 23 pass, 0 fail** | `node --test tests/js/*.test.mjs` |
| Golden byte-stability | n/a | `UPDATE_GOLDEN=1 pytest tests/golden -q` leaves `git status tests/golden` **empty** (snapshots regenerate byte-identically) | |
| OpenAPI pin | n/a | pin matches recomputed canonical digest (`5d46f629…` after the Phase 2 DELETE route) | `pytest tests/golden/test_openapi_pin.py` |
| Schema changes | none | **none** (`git diff --name-only base..HEAD -- migrations/ alembic.ini` empty) | |
| Secrets in diff | none | **none** (no token-shaped strings in `git log -p`); audit records fingerprints only | |
| main | — | untouched at `c6cb2df`; never pushed | |

Per-phase gates were also run at each phase boundary and passed (pytest/ruff/black; mypy and Node from Phase 1b onward).

---

## 2. Per-plan task status

All 40 planned tasks are complete and reviewed (spec + quality) with fix loops where needed. Per-task evidence, commits, and deferred minors live in the git history; the workspace ledgers were used during execution.

### Phase 0 — CI and regression harness (6/6)
| Task | Commit(s) | Result |
|---|---|---|
| T1 black formatting authority | `b401fd3` | 7 drifted files reformatted; black/ruff clean |
| T2 fail-not-skip guard + two-OS CI | `2205dbf` | guard test RED→GREEN; `.github/workflows/ci.yml` (Ubuntu containers / Windows PG17+Memurai per approved deviation) |
| T3 endpoint golden harness | `f1e1897` | 23 GET-route snapshots; byte-stable; mutation fails; fix round added OS-deterministic media-type/CRLF/disk-warning normalization |
| T4 OpenAPI hash pin | `4a4ff05` | digest matches measured `7d29b8a0…` at the time; later re-pinned for the new DELETE route |
| T5 coverage floor + baseline | `5a5d947` | measured 93.41% then; `fail_under=93`; CI `--cov` enforced |
| T6 advisory dependency audit | `9ae1630` | `pip-audit` clean; job advisory (blocking flip deferred — see §6) |

### Phase 1a — Zero-behavior fixes (8/8)
| Task | Commit | Result |
|---|---|---|
| T1 token off metafiles host | `00692a0` | `auth=False`; transport-level test |
| T2 lazy singletons | `def016b` | `_LazyLoaders` single-flight; contract suites unchanged |
| T3 atomic clone claim + bounded registry | `d79eb49` | `claim()` + count/idle eviction; integration identity test |
| T4 shard poison recovery | `86a5def` | PENDING rollback + retry/DLQ; no-queue still raises |
| T5 upsert input isolation | `ebc5822` | dirty `size`/`language`/`visibility`/flags normalized or skipped |
| T6 GraphQL partial surfacing | `9109948` | `BatchFetch` + warning; public shape unchanged |
| T7 GeoCache chunking | `968cbf1` | one statement per 1000 binds; default path identical |
| T8 (E1, approved) run counters | `d894460` | `updated/unchanged/skipped` persisted + `unchanged` span; golden insertion-only |

### Phase 1b — Test and tooling hardening (8/8)
| Task | Commit | Result |
|---|---|---|
| T1 fixture centralization | `187087d` | 22 fixtures across 22 files on `clean_db`; guard rejects raw TRUNCATE |
| T2 CWD-independent paths | `1d0c1ea` | `repo_root`; AST guard; Node wrapper CWD-proof |
| T3 flake removal | `616e6d6` | events/barriers/append-counter replace sleeps |
| T4 applib.js extraction | `062b4fb` | UMD helpers + 5 Node tests; app.js behavior identical |
| T5 quarantine manifest | `38d3e94` | 13 entries; 3-way gate; `skeleton`/`serve.__main__` tests |
| T6 scoped mypy | `4046712` | 12→14 files; `Deps.token_fp`; annotation fixes |
| T7 audit→lib + boundaries | `753d633` | content-identical rename; core-never-imports-serve gate |
| T8 development log | `8f911e9` | program record with measured facts |

### Phase 2 — Adversarial hardening (9/9)
| Task | Commit | Result |
|---|---|---|
| T1 origin CSRF | `01254bc` | same-origin/absent pass; mismatched/malformed → 403 |
| T2 Host allowlist | `804a6a6` | loopback + env extras; non-local → 400 |
| T3 body caps | `0eabc08` | 1 MiB JSON / 10 MiB `/find`; 413 JSON; chunked streams capped |
| T4 deadlines + limiter | `1366979` | `Deadline`, 3600/1800 env policy; no-deadline sleeps identical |
| T5 bounded waits + 503 | `61afc42` | cache/executor waits → 503 `{"error":"timeout","retry_after":N}` |
| T6 clone lifecycle | `841e7a8` | env timeout, terminal-safe git, cancel event, `DELETE /runs/{id}/clone`; **merged with Phase 1a registry**; OpenAPI pin/snapshot updated |
| T7 Redis fail-loud | `3396637` | warning + `redis_degraded` + `GITCRAWL_REDIS_STRICT` |
| T8 single-flight health probe | `739788e` | `_Probe`; statement timeout; thread leak gone; T7 key preserved |
| T9 malformed input + freshness | `321cee4` | tolerant progress JSON; ASCII digits; `updated_at` refreshed |

### Phase 3 — UX polish (9/9)
| Task | Commit | Result |
|---|---|---|
| T1 busy feedback | `304c2bc` | nav bar + htmx indicators; no control changed |
| T2 actionable clone errors | `753a195` | structured error text + retry/dismiss; guard preserved |
| T3 recent filters | `d62c082` | hidden by default; opt-in localStorage; never auto-applied |
| T4 empty states | `a896938` | example-query links appended |
| T5 keyboard help opener | `4c5a7cd` | `?` button; binding unshadowed |
| T6 copy link | `ac5923f` | `#copy-link`; hash button unchanged |
| T7 clone cancel button | `c17ffb9` | running-only button consuming Phase 2 DELETE |
| T8 modal focus trap | `df98909` | Tab wrap + focus restore; mouse behavior unchanged |
| T9 sticky header + row count | `8f35eff` | CSS-only pinning + read-only count |

### Extra commits (non-task)
| Commit | Reason |
|---|---|
| `8e37d11 docs: add quality-hardening spec and plans` | operator-mandated first commit |
| `a36339c style: apply black formatting after phase 1a` | black drift introduced by T2/T7; amend/rebase forbidden, so a dedicated `style:` commit kept reviewed commits intact |
| `94a81e2 docs: document hardening operations and correct fixture count` | final-review fix wave (spec §10 docs requirement + factual fixture-count drift) |

---

## 3. Decisions, assumptions, and rulings (exhaustive)

Recorded as rulings during execution; each with the cost if wrong.

1. **Golden snapshot normalization** — canonicalized `application/javascript` → `text/javascript`; normalized CRLF→LF for all `text/*` bodies; applied the disk-warning scrub to every body (structure preserved). Cost if wrong: a non-warning JSON change matching the exact disk-warning regex would be masked (negligible).
2. **One-commit protocol vs fix loops** — pre-review fixes were folded into task commits via `git reset --soft` on unpushed tips (amend/rebase forbidden); one post-review fix round was folded the same way (Phase 2 T1). Reviewed commits were never rewritten. Cost if wrong: early pre-fold hashes (`5866a2b`, `21bb208`) no longer exist; nothing was pushed.
3. **`tests/golden/__init__.py`** accepted as a required test-infra addition (pytest 9.1.1 conftest module-name collision; full suite otherwise EXIT=2).
4. **Phase 2 T6 merge, not replace** — kept Phase 1a's atomic `claim` + bounded eviction and added cancel methods; `start_clone` keeps claim-first wiring; `_clone_worker` gained 3 args; the Phase 1a integration test's fake worker was updated without weakening its identity assertions. Cost if wrong: none observed (identity/eviction/race tests green).
5. **New DELETE route changes the OpenAPI contract** — Phase 2 T6 regenerated `openapi_json.json` + `openapi.sha256` in the same commit; reviewer recomputed the digest and confirmed only the new path/operation was added.
6. **E1 golden updates** — Phase 1a T8 regenerated the two affected HTML snapshots; reviewer proved the diff was exactly one inserted `unchanged` span.
7. **Malformed Origin port** — broadened the ValueError guard so `Origin: http://host:abc` gets 403, not 500 (spec §6.1 requires rejection). Cost if wrong: none (adversarial-only path).
8. **Phase 2 test file fixture** — `tests/contract/test_adversarial_hardening.py` uses the centralized `clean_db` factory instead of raw TRUNCATE (Phase 1b guard).
9. **Phase 2 T8 reviewer finding adjudicated** — the claimed "stale value interleaving" in `_Probe` is not reachable: a new check thread starts only when `_event is None`, which is set only after the prior event completed and published its value. The lingering event during a hung check is the intended bounded-thread trade-off. Cost if wrong: one stale health reading for one probe cycle (health-only, self-correcting).
10. **Phase 3 T5 assertion** — uses the post-Phase-1b `isEditableTarget(event.target)` instead of the stale `isEditable(event.target)`; intent preserved. Cost if wrong: none.
11. **Phase 3 T9 assertion** — CSS test uses the `repo_root` fixture because Phase 1b's static guard rejects relative `Path("...")` literals; assertions verbatim. Cost if wrong: none.
12. **Phase 3 `hx-indicator` placement** — on its own line before `hx-swap` (existing attributes keep their relative order); the plan's after-`hx-swap` snippet would split the closing `>` and create a word-diff deletion. HTML attribute order is semantically irrelevant.
13. **Phase 3 T6 marker adaptation** — link button selected via `[data-copy-url]` so the plan's required marker exists; behavior equivalent.
14. **Phase 3 Node wrappers** — use the `repo_root` pattern (absolute path + cwd) established by Phase 1b, not the plans' relative paths. Cost if wrong: none.
15. **Redis health tests** delete `GITHUB_TOKEN(S)` so their exact-dict assertions are hermetic in this dev environment; the tested production path is unchanged.
16. **`Deps.token_id` rename** — attribute reads became `token_fp`; function kwargs named `token_id=` at HTTP/limiter boundaries stayed unchanged.
17. **Fixture-count doc drift** corrected to the measured 22 files in the final docs commit.
18. **Plan/spec divergences recorded, not fixed**: (a) spec §5.9's "mark experimental in module docstrings" was reinterpreted by the Phase 1b plan as manifest + tests (no docstrings added); (b) `pip-audit` remains advisory until the first green CI run because pushing is forbidden; (c) CI status could not be observed locally.

---

## 4. Deviations from plan text (all recorded)

- Golden harness needed deterministic cross-OS handling the plans did not name (JS media type, CRLF, disk warning in JSON) — added in a reviewed fix round.
- `tests/golden/__init__.py` and `tests/unit/__init__.py` added for pytest module-name collisions (both required; documented in-task).
- Phase 2 T6 merged the cancel API into Phase 1a's registry (plans conflicted; the later plan's own risk note directed re-verification).
- Phase 2 T7's `redis_degraded` insert was carried through Phase 2 T8's `_Probe` rewrite (the adversarial plan's documented ordering caveat).
- Phase 3 plan assumed no golden harness; execution used the Phase 0 golden path as its own fallback instructed, plus the word-diff evidence.
- Extra `style:` and `docs:` commits (see §2) due to the no-amend/no-rebase protocol and the final-review docs fix wave.
- Two test files were reformatted by black after Phase 1a and fixed in the `style:` commit rather than rewriting reviewed commits.

---

## 5. Blocked items

**None.** No task was reverted or left incomplete. The only unreachable item is CI observation (no push allowed), which is an operator action, not a blocker.

---

## 6. Remaining work / operator action items

1. **Push and observe CI.** No CI run has ever executed (pushing was forbidden). The workflow is parse-validated only. Windows runner paths/service names and Memurai install are per the approved deviation and unverified until first run; the plan documents the `ikalnytskyi/action-setup-postgres@v8` fallback if the Windows DB step fails.
2. **Flip `pip-audit` to blocking** after the first green audit run (Phase 0 T6 Step 4; deferred by design).
3. **Queued 2026-10-02 session** — untracked operator plans/prompt appeared in the worktree mid-run; they are not part of this program and were never staged. Their precondition requires no other agent mid-run; this run is complete, so they may proceed. Note their plans still reference `serve.audit` (now `lib.audit`).
4. **Untracked/concurrent state left untouched:** an uncommitted external `.gitignore` edit (adds the queue prompt to ignores), five `docs/superpowers/plans/2026-10-02-*.md` files, `docs/superpowers/specs/2026-10-02-agent-detection-design.md`, `docs/superpowers/prompts/`, and an intermittent `scripts/` directory. None were staged, modified, or deleted by this run.
5. **Optional follow-ups** (parked minors; final review triaged none as merge-blocking): clone cancel-event clobber window on restart; stale persisted "running" cancel no-op; `_terminate` child-tree on Windows; `"inf"` deadline env; empty-string `_int_or_raw`; `graphql_batch` limiter-without-token_id if ever wired; BodyLimit path vs `root_path`; quarantine docstring reinterpretation; add the four new JS helpers to golden `EXTRA_CASES`; jsdom-level UX tests.

---

## 7. Final review outcome

Whole-branch review (base `c6cb2df`..HEAD) verdict: **Ready to merge — Yes**, zero Critical, zero Important findings. It independently re-ran the gates (1214/1214, 0 skips, 94.74%, mypy/ruff/black clean, 23 Node tests) and recomputed the OpenAPI digest. Its one spec-mandated docs gap (allowlist/deadline/proxy documentation) was fixed in `94a81e2` and scoped-re-reviewed as ADDRESSED with no new breakage. All remaining findings are Minor and parked (see §6.5).
