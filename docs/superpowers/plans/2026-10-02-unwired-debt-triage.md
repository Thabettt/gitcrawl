# Unwired-Debt Triage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the built-but-unwired debt (R24/R57): wire the two pieces with a real consumer, delete the rest, fix the parked minors, and record the superseding ruling.

**Architecture:** This plan **supersedes the "quarantine, don't delete" ruling** documented in `docs/development-log.md` (R24/R57) and `docs/superpowers/specs/2026-10-01-quality-hardening-design.md` §9 / zero-behavior-fixes. Task 1 records the new ruling (R58) after the operator confirms the decision table; Tasks 2–4 execute it. `pel_size` is kept and wired by the SLO dashboard plan.

**Tech Stack:** Python 3.12, Redis Streams (scheduler), pytest.

**Spec:** none — decision table inline. Conflicts resolved explicitly (see Task 1).

**Depends on:** current `001-gitcrawl` head. `tests/quarantine_manifest.txt` does **not** exist yet (the test-and-tooling-hardening plan is pending); if that plan lands first, Task 3 must also update the manifest.

## Global Constraints

- Python `>=3.12`; ruff + black clean; coverage floor 93.
- No behavior change to live paths except the two intentional wirings (crash reclaim; nothing else). Deleting unwired functions must not change any wired import graph.
- Every deletion removes the function **and** its tests; every retained-but-unwired item must have a named consumer or an explicit "keep" decision in R58.
- DB/Redis tests use the existing fixtures (`clean_db`, fakeredis); contract helpers copied from `tests/contract/test_pages.py`.
- Run tests with: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest <path> -q`.

## Decision Table (verified 2026-10-02)

| # | Item | Location | Verified state | Decision | Reason |
|---|---|---|---|---|---|
| 1 | `RetryQueue` | `src/limiter/retry.py` | unused | **delete** | superseded by `request_with_retry` classifier |
| 2 | `order_shards` | `src/scheduler/tiering.py` | unused; `order_repos` wired | **delete** | planner decides shard order; repo ordering is the live path |
| 3 | `plan_id_ranges` | `src/discover/since_scan.py` | unused; `max_id` checkpoint wired | **delete** | single-consumer since scan doesn't need ID-range workers |
| 4 | `reclaim_stale` | `src/scheduler/state_machine.py` | unused | **wire** | crash recovery: reclaim another consumer's orphaned PEL entries |
| 5 | `pel_size` | `src/scheduler/state_machine.py` | unused | **keep + wire** | queue depth metric in the SLO dashboard plan |
| 6 | `retry_or_dlq` | `src/scheduler/state_machine.py` | already wired (`discover/pipeline.py:292`) | **keep** | live |
| 7 | `mirrors.py` | `src/enrich/mirrors.py` | unused | **delete unless claimed** | no v1 corpus/detection consumer; recover from git if a track needs it |
| 8 | `graphql_batch.py` | `src/enrich/graphql_batch.py` | unused | **delete unless claimed** | detection has its own GraphQL; batch enrichment not v1 |
| 9 | `fetch_metafiles` | `src/enrich/trees_first.py` | unused; `auth=False` already fixed | **delete unless claimed** | only covers standard metafiles, not a full-tree substitute |
| 10 | `skeleton.py` | `src/skeleton.py` | unused placeholder excluded from lint | **delete** | pure scaffolding, zero consumers |
| 11 | `fetch_metafiles` auth leak (R55 parked) | — | `auth=False` verified | **close** | already fixed |
| 12 | executor `_futures` never evicts | `src/serve/executor.py` | evicts done futures in `submit()` | **close** | stale note |
| 13 | lazy executor/runner init unsynchronized | `src/serve/app.py` `_LazyLoaders` | no lock around build | **fix** | small race under concurrent requests |
| 14 | `runs.error` may carry upstream body text | `src/serve/executor.py::_error_message` | exception text truncated to 300 chars | **fix** | sanitize to type + status, never upstream bodies |
| 15 | migration 0004 downgrade is lossy for NULL `repo_id` | `migrations/versions/0004_run_items_repo_nullable.py` | known | **document** | one-line note in the migration docstring + dev log |
| 16 | browser pass / keyboard tests | console | markup-tested only | **checklist** | manual step, not code; record in `docs/environment.md` |

## File Structure

**Delete**
- `src/limiter/retry.py` + its tests in `tests/unit/test_retry.py`
- `src/scheduler/tiering.py::order_shards` (+ tests for it in `tests/unit/test_tiering.py`)
- `src/discover/since_scan.py::plan_id_ranges` (+ tests for it in `tests/unit/test_since_scan.py`)
- `src/enrich/mirrors.py` + `tests/unit/test_mirrors.py`
- `src/enrich/graphql_batch.py` + `tests/unit/test_graphql_batch.py`
- `src/enrich/trees_first.py::fetch_metafiles` (+ its tests in `tests/unit/test_trees_first.py`)
- `src/skeleton.py` + the `extend-exclude` entries in `pyproject.toml`

**Modify**
- `src/scheduler/state_machine.py` — `reclaim_stale` gets its first caller
- `src/discover/pipeline.py` — call reclaim before the claim loop
- `src/serve/app.py` — lock in `_LazyLoaders`
- `src/serve/executor.py` — `_error_message` sanitization
- `migrations/versions/0004_run_items_repo_nullable.py` — downgrade note
- `docs/development-log.md` — R58 + parked-item closures
- Tests: `tests/unit/test_state_machine.py`, `tests/unit/test_pipeline_live.py`-style queue test, `tests/unit/test_app_singletons.py`, `tests/unit/test_executor_unit.py`

---

### Task 1: Record the superseding ruling (gate)

**Files:**
- Modify: `docs/development-log.md`

**Interfaces:**
- Consumes: the decision table above.
- Produces: ruling **R58** that explicitly supersedes the quarantine policy for the listed items, and closes parked minors 11–12.

- [ ] **Step 1: Confirm the table**

Operator confirms (or amends) each row, especially **"delete unless claimed"** rows 7–9: if any of the six explorations claims `mirrors.py`, `graphql_batch.py`, or `fetch_metafiles`, that row flips to **keep + wire** and its task moves to the claiming exploration's plan. No code changes before this confirmation.

- [ ] **Step 2: Append R58 to `docs/development-log.md`**

Add under the ruling table:

```markdown
| R58 | Supersedes the R24/R57 quarantine policy: unwired surface is triaged, not preserved. Wiring: `reclaim_stale` (crash reclaim), `pel_size` (SLO metric). Deletions: `RetryQueue`, `order_shards`, `plan_id_ranges`, `mirrors`, `graphql_batch`, `fetch_metafiles`, `skeleton.py`. Parked minors closed: metafiles auth (fixed), executor future eviction (fixed). Rationale: dead code is a trap; deleted work is recoverable from git. | lost work if an exploration later claims a deleted module (re-add is a fresh request) |
```

- [ ] **Step 3: Commit**

```bash
git add docs/development-log.md
git commit -m "docs: ruling R58 triages the unwired surface"
```

---

### Task 2: Wire `reclaim_stale` into discovery

**Files:**
- Modify: `src/discover/pipeline.py` (`run_search_discovery`, queue branch ~:275-300)
- Test: `tests/unit/test_state_machine.py` (unit), `tests/unit/test_reclaim_wiring.py` (new wiring test)

**Interfaces:**
- Consumes: `ShardQueue` (`src/scheduler/state_machine.py`), `queue.reclaim_stale(consumer) -> int`.
- Produces: before the claim loop, stale PEL entries from dead consumers are reclaimed so a crashed run's in-flight shards are not orphaned.

- [ ] **Step 1: Write the failing wiring test**

Create `tests/unit/test_reclaim_wiring.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

import fakeredis
import httpx

from discover.pipeline import Deps, run_search_discovery
from scheduler.shard_planner import ShardSpec
from scheduler.state_machine import ShardQueue, ShardStore


def handler(request: httpx.Request) -> httpx.Response:
    query = request.url.params.get("q", "")
    if query == "stars:>0":
        return httpx.Response(
            200, json={"total_count": 1, "incomplete_results": False, "items": []}
        )
    # planner probe for the outer query: no shards to plan
    return httpx.Response(
        200, json={"total_count": 0, "incomplete_results": False, "items": []}
    )


def test_discovery_reclaims_and_processes_a_dead_consumers_shard(clean_db, monkeypatch):
    engine = clean_db()
    redis = fakeredis.FakeRedis()
    store = ShardStore(engine)
    shard_id = store.create(
        ShardSpec(query="stars:>0", range_start=None, range_end=None, total_count=1)
    )
    queue = ShardQueue(redis)
    queue.enqueue(shard_id)
    queue.claim("gitcrawl-dead", count=1)  # delivered to a consumer that died
    assert queue.pel_size() == 1

    # reclaim immediately in the test instead of waiting the production 60s idle window
    original = ShardQueue.reclaim_stale
    monkeypatch.setattr(
        ShardQueue,
        "reclaim_stale",
        lambda self, consumer, **kwargs: original(self, consumer, min_idle_ms=0, **kwargs),
    )

    deps = Deps(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        engine=engine,
        redis=redis,
        limiter=None,
        token_id="t",
        audit_buffer=None,
    )
    run_search_discovery(deps, "stars:>9999999", max_shards=1)

    assert queue.pel_size() == 0  # reclaimed, processed, and acked
    assert store.get(shard_id).state in {"done", "incomplete"}
```

- [ ] **Step 2: Run to verify failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_reclaim_wiring.py -q`
Expected: FAIL (`pel_size` stays 1; the shard is never processed).

- [ ] **Step 3: Implement**

`reclaim_stale` claims entries into the caller's PEL and **returns** them; discarding the return value would strand the shards, so the reclaimed batch must be processed before the new-message loop. In `run_search_discovery`:

```python
    consumer = f"gitcrawl-{os.getpid()}"
    if queue is None:
        while pending:
            process(pending.popleft())
    else:
        for queued in queue.reclaim_stale(consumer):
            if process(queued.shard_id, queued):
                break
            queue.ack(queued.stream_id, queued.shard_id)
        while True:
            claimed = queue.claim(consumer, count=1)
            if not claimed:
                break
            retried = False
            for queued in claimed:
                retried = process(queued.shard_id, queued)
                if retried:
                    break
                queue.ack(queued.stream_id, queued.shard_id)
            if retried:
                break
```

- [ ] **Step 4: Run tests**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_reclaim_wiring.py tests/unit/test_state_machine.py tests/unit/test_since_scan.py tests/integration/test_pipeline.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/discover/pipeline.py tests/unit/test_reclaim_wiring.py
git commit -m "fix: reclaim stale shard deliveries before claiming"
```

---

### Task 3: Delete the superseded surfaces

**Files:**
- Delete: `src/limiter/retry.py`, `src/enrich/mirrors.py`, `src/enrich/graphql_batch.py`, `src/skeleton.py`
- Modify: `src/scheduler/tiering.py` (drop `order_shards`), `src/discover/since_scan.py` (drop `plan_id_ranges`), `src/enrich/trees_first.py` (drop `fetch_metafiles` and now-unused `_METAFILE_KEYS`/`_paths_from_value`/`_entry_names` if only used by it), `pyproject.toml` (remove `src/skeleton.py` excludes)
- Delete tests: `tests/unit/test_retry.py`, `tests/unit/test_mirrors.py`, `tests/unit/test_graphql_batch.py`
- Modify tests: `tests/unit/test_tiering.py` (drop `order_shards` cases), `tests/unit/test_since_scan.py` (drop `plan_id_ranges` cases), `tests/unit/test_trees_first.py` (drop `fetch_metafiles` cases)
- Modify: `tests/quarantine_manifest.txt` **if it exists** (remove deleted entries)

**Interfaces:**
- Produces: no wired import changes; the surviving functions (`order_repos`, `iter_since_pages`, `fetch_tree`) keep their signatures.

- [ ] **Step 1: Verify nothing imports the deleted names (gate)**

Run: `rg -n "RetryQueue|order_shards|plan_id_ranges|fetch_metafiles|fetch_graphql_batch|fetch_ecosystems_repo|fetch_depsdev_project|fetch_scorecard|skeleton" src tests --glob "*.py"`
Expected: matches only inside the definition files and the test files being deleted/edited. If a live import appears, stop and re-triage that row.

- [ ] **Step 2: Delete the modules and functions**

Use `git rm` for whole files; edit the three mixed modules to remove only the named functions and their private helpers (verified unused via Step 1). Remove the `src/skeleton.py` excludes from `pyproject.toml` (`extend-exclude = ["src/skeleton.py"]` and `extend-exclude = 'src/skeleton\.py'`).

- [ ] **Step 3: Run the remaining suites**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit -q`
Expected: PASS with the deleted tests gone and no import errors. Fix any test that imported a deleted helper (update imports, do not re-add the helper).

- [ ] **Step 4: Full suite + coverage**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest --cov=src --cov-report=term-missing -q`
Expected: PASS, coverage `>= 93%`. Deleting covered dead code usually raises coverage; if coverage drops, the deleted functions had live callers — stop and re-triage.

- [ ] **Step 5: Commit**

```bash
git add -A src tests/quarantine_manifest.txt pyproject.toml
git commit -m "chore: delete superseded unwired surfaces (R58)"
```

---

### Task 4: Parked-minor fixes (lock, error sanitize, migration note)

**Files:**
- Modify: `src/serve/app.py` (`_LazyLoaders` ~:270)
- Modify: `src/serve/executor.py` (`_error_message` ~:70)
- Modify: `migrations/versions/0004_run_items_repo_nullable.py`
- Test: `tests/unit/test_app_singletons.py`, `tests/unit/test_executor_unit.py`

**Interfaces:**
- Produces: thread-safe lazy construction; `runs.error` never contains upstream response bodies.

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_app_singletons.py`:

```python
import threading


def test_lazy_loaders_build_once_under_concurrency():
    from serve.app import _LazyLoaders

    loaders = _LazyLoaders()
    builds: list[int] = []

    def build():
        builds.append(1)
        return object()

    results: list[object] = []
    threads = [threading.Thread(target=lambda: results.append(loaders.get("engine", build))) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(builds) == 1
    assert len({id(result) for result in results}) == 1
```

Add to `tests/unit/test_executor_unit.py`:

```python
def test_error_message_strips_upstream_body():
    from serve.executor import _error_message

    message = _error_message(ValueError("422: Validation Failed: {'message': 'upstream body secret'}"))
    assert "upstream body secret" not in message
    assert message.startswith("ValueError")
```

- [ ] **Step 2: Run to verify failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_app_singletons.py tests/unit/test_executor_unit.py -q`
Expected: FAIL (multiple builds; secret text present).

- [ ] **Step 3: Implement**

`_LazyLoaders.get` wraps its check-build-store in a `threading.Lock` (store the lock on the instance; keep the fast path read outside the lock).

`_error_message` sanitizes:

```python
def _error_message(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    text = text.split("\n", 1)[0]
    for marker in ("{", "Validation Failed", "upstream"):
        index = text.find(marker)
        if index > 0:
            text = text[:index].rstrip(" :")
    return text[:300]
```

`0004_run_items_repo_nullable.py` downgrade docstring:

```python
    """Downgrade restores NOT NULL and therefore drops rows whose repo_id is NULL.

    Lossy by design; export those rows before downgrading (see ruling R55).
    """
```

- [ ] **Step 4: Run tests**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_app_singletons.py tests/unit/test_executor_unit.py tests/integration/test_executor.py -q`
Expected: PASS.

- [ ] **Step 5: Record the browser/keyboard manual checklist**

Append to `docs/environment.md`:

```markdown
## Console manual pass (run before demos)
- [ ] `/`, `/find`, `/runs/{id}`, `/filters`, `/health` load without console errors
- [ ] keyboard rownav (j/k/enter/esc) works on the results table
- [ ] dark mode toggle persists
- [ ] clone modal estimate + start on a small run
```

- [ ] **Step 6: Commit**

```bash
git add src/serve/app.py src/serve/executor.py migrations/versions/0004_run_items_repo_nullable.py tests/unit/test_app_singletons.py tests/unit/test_executor_unit.py docs/environment.md docs/development-log.md
git commit -m "fix: lazy-init lock, sanitized run errors, migration downgrade note"
```

---

## Self-Review Checklist

- [ ] R58 recorded and supersedes R24/R57 explicitly (Task 1).
- [ ] `reclaim_stale` has a real caller and a regression test (Task 2).
- [ ] `pel_size` retained for the SLO dashboard plan (row 5); no other unwired surface remains without a documented keep decision.
- [ ] Every deletion passes the Step 1 import gate first.
- [ ] Parked minors 11–12 closed in the dev log; 13–15 fixed; 16 recorded as a manual checklist.
- [ ] Full suite + coverage green after each task.
