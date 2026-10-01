# Discovery & Scheduler Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the Θ(k²) ID accumulation, the unbounded `IN` parameter lists, and the eager shard-planner search-budget blowup in the discovery path.

**Architecture:** All changes are additive or localized: a pure `_collect_ids` helper replaces tuple concatenation, a `chunked` helper bounds statement sizes, `ShardPlanner.iter_plan()` yields leaves lazily while the existing `plan()` stays list-returning (so existing tests and callers are untouched), and two small queue/state helpers get constant-time implementations.

**Tech Stack:** Python 3.12, SQLAlchemy 2.1 + psycopg3 (Postgres 18), Redis Streams, pytest, ruff.

**Spec:** `docs/superpowers/plans/2026-10-01-efficiency-audit-report.md` (findings S1, S2, S3, #14, #28, #29, #30, #44, #47)

## Global Constraints

- Python 3.12, `from __future__ import annotations` in new modules.
- ruff config: `select = ["E","F","I","UP","B"]`, line length 100. Run `ruff check src tests` before every commit.
- Tests: `pytest` from repo root (`pythonpath = ["src"]`, `-q` in `addopts`). Integration tests require `TEST_DATABASE_URL` ending in `_test` (`tests/conftest.py:19-33`) and fail/skip without it; run them locally before claiming done.
- Public API shape must not change: `DiscoveryStats.repo_ids` stays a `tuple[int, ...]`, `ShardPlanner.plan()` keeps returning `list[ShardSpec]`.
- One commit per task, message prefix `perf:`.

---

## File Map

| File | Change |
|---|---|
| `src/discover/pipeline.py` | `_collect_ids` helper; local list accumulation; `deque` pending; use `iter_plan`; pass `total_count` |
| `src/lib/batching.py` | **new** — `chunked()` sequence helper |
| `src/serve/runner.py` | `_load_rows` chunking; pass `total_count` into discovery |
| `src/hydrate/tail.py` | `_stored_etags` chunking |
| `src/scheduler/shard_planner.py` | `root_count` override; `iter_plan()` generator |
| `src/scheduler/state_machine.py` | single-statement `set_state`; memoized `_ensure_group`; direct `_get_field` |
| `tests/unit/test_id_accumulation.py` | **new** |
| `tests/unit/test_batching.py` | **new** |
| `tests/unit/test_shard_planner.py` | add root-count + laziness tests |
| `tests/unit/test_state_machine.py` | add group-memo test |
| `tests/integration/test_lifecycle.py` | add batched-ETag test |
| `tests/integration/test_cloner.py` (fixtures in `test_cloner.py`) | add batched `_load_rows` test |
| `tests/integration/test_runner.py` | update pinned duplicate-count assertion (lines ~164-182) |

---

### Task 1: Linear ID accumulation

**Files:**
- Modify: `src/discover/pipeline.py` (stats at `:44-57`; loop at `:250-261`; after loop before `_log_summary` at `:301`)
- Test: `tests/unit/test_id_accumulation.py`

**Interfaces:**
- Consumes: `DiscoveryStats` (unchanged shape).
- Produces: `pipeline._collect_ids(seen: set[int], collected: list[int], items: Iterable[Mapping]) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_id_accumulation.py
from __future__ import annotations

import time

from discover.pipeline import _collect_ids


def test_collect_ids_appends_in_order_and_deduplicates():
    seen: set[int] = set()
    collected: list[int] = []
    _collect_ids(seen, collected, [{"id": 3}, {"id": 1}, {"id": 3}, {"nope": 1}, {"id": True}])
    assert collected == [3, 1]
    assert seen == {3, 1}


def test_collect_ids_stays_linear_at_100k_items():
    seen: set[int] = set()
    collected: list[int] = []
    items = [{"id": value} for value in range(100_000)]
    started = time.perf_counter()
    _collect_ids(seen, collected, items)
    elapsed = time.perf_counter() - started
    assert len(collected) == 100_000
    # tuple concatenation at this size copies ~40 GB and takes seconds-to-minutes.
    assert elapsed < 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_id_accumulation.py -v`
Expected: FAIL — `ImportError: cannot import name '_collect_ids'`.

- [ ] **Step 3: Implement**

Add near the top of `src/discover/pipeline.py` (after `_audit_hook`):

```python
def _collect_ids(seen: set[int], collected: list[int], items: Iterable[Mapping]) -> None:
    for item in items:
        if not isinstance(item, Mapping):
            continue
        repo_id = item.get("id")
        if isinstance(repo_id, bool) or not isinstance(repo_id, int) or repo_id in seen:
            continue
        seen.add(repo_id)
        collected.append(repo_id)
```

In `run_search_discovery`, find the existing `seen_ids: set[int] = set()` line (grep: `rg -n "seen_ids" src/discover/pipeline.py`) and add next to it:

```python
    collected_ids: list[int] = []
```

Replace `pipeline.py:250-261`:

```python
                for item in page.items:
                    if not isinstance(item, Mapping):
                        continue
                    repo_id = item.get("id")
                    if (
                        isinstance(repo_id, bool)
                        or not isinstance(repo_id, int)
                        or repo_id in seen_ids
                    ):
                        continue
                    seen_ids.add(repo_id)
                    stats.repo_ids += (repo_id,)
```

with:

```python
                _collect_ids(seen_ids, collected_ids, page.items)
```

Before `_log_summary("search", stats)` (`pipeline.py:301`) add:

```python
    stats.repo_ids = tuple(collected_ids)
```

Also, deduplicate the page before upserting so within-page duplicates cannot overcount stats/history: change the upsert call at `pipeline.py:249` to

```python
                _fold(stats, upsert_repos(deps.engine, dedupe_items(page.items)))
```

and import `dedupe_items` from `store.upserts` (the module already imports from `store.upserts`; add the symbol).

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_id_accumulation.py tests/integration/test_pipeline.py tests/integration/test_runner.py -q`
Expected: PASS. `tests/integration/test_runner.py:160` (`assert stats.repo_ids == (1, 2, 3)`) still passes because the public type is unchanged. If `dedupe_items` import creates a cycle, import it inside the function instead.

- [ ] **Step 5: Commit**

```bash
git add src/discover/pipeline.py tests/unit/test_id_accumulation.py
git commit -m "perf: linear repo_id accumulation in discovery"
```

---

### Task 2: Bounded `IN` queries via chunking

**Files:**
- Create: `src/lib/batching.py`
- Modify: `src/serve/runner.py:127-162` (`_load_rows`), `src/hydrate/tail.py:28-35` (`_stored_etags`)
- Test: `tests/unit/test_batching.py`, `tests/integration/test_lifecycle.py`, `tests/unit/test_cloner.py`

**Interfaces:**
- Produces: `lib.batching.chunked(values: Sequence[T], size: int) -> Iterator[Sequence[T]]`.
- `_load_rows(engine, repo_ids, *, batch_size: int = 5000)` and `_stored_etags(engine, names, *, batch_size: int = 5000)` keep their return shapes.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_batching.py
from __future__ import annotations

import pytest

from lib.batching import chunked


def test_chunked_splits_exactly_and_leaves_a_remainder():
    assert [list(batch) for batch in chunked([1, 2, 3, 4, 5], 2)] == [[1, 2], [3, 4], [5]]


def test_chunked_single_batch_when_smaller_than_size():
    assert [list(batch) for batch in chunked([1, 2], 10)] == [[1, 2]]


def test_chunked_rejects_non_positive_size():
    with pytest.raises(ValueError):
        list(chunked([1], 0))
```

Add to `tests/integration/test_lifecycle.py`:

```python
def test_stored_etags_batches(clean: Engine):
    from hydrate.tail import _stored_etags

    seed(clean, "octo/a", repo_id=1, etag="e1")
    seed(clean, "octo/b", repo_id=2, etag="e2")
    assert _stored_etags(clean, ["octo/a", "octo/b"], batch_size=1) == {
        "octo/a": "e1",
        "octo/b": "e2",
    }
```

Add to `tests/unit/test_cloner.py` (it has `seed_run`):

```python
def test_load_rows_batches(clean: Engine):
    from serve.runner import _load_rows

    run_id, _ = seed_run(clean, [(1, "octo/one", 10, 1), (2, "octo/two", 10, 2), (3, "octo/three", 10, 3)])
    rows = _load_rows(clean, [1, 2, 3], batch_size=2)
    assert set(rows) == {1, 2, 3}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_batching.py tests/integration/test_lifecycle.py::test_stored_etags_batches tests/unit/test_cloner.py::test_load_rows_batches -v`
Expected: FAIL — missing module / unexpected keyword `batch_size`.

- [ ] **Step 3: Implement**

```python
# src/lib/batching.py
from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TypeVar

T = TypeVar("T")


def chunked(values: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    if size < 1:
        raise ValueError("size must be >= 1")
    for start in range(0, len(values), size):
        yield values[start : start + size]
```

`src/hydrate/tail.py`:

```python
from lib.batching import chunked

_NAME_BATCH = 5000


def _stored_etags(engine: Engine, names: list[str], *, batch_size: int = _NAME_BATCH) -> dict[str, str | None]:
    if not names:
        return {}
    merged: dict[str, str | None] = {}
    with engine.connect() as connection:
        for batch in chunked(names, batch_size):
            rows = connection.execute(
                select(Repo.full_name, Repo.etag).where(Repo.full_name.in_(batch))
            ).all()
            merged.update({str(full_name).casefold(): etag for full_name, etag in rows})
    return merged
```

`src/serve/runner.py` `_load_rows`: wrap the existing `select(...)` in `for batch in chunked(repo_ids, batch_size):` inside the single connection and accumulate into `result` (move the `result` dict and the per-row loop inside the batch loop). Add `from lib.batching import chunked` and `_ID_BATCH = 5000`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_batching.py tests/unit/test_cloner.py tests/integration/test_lifecycle.py tests/integration/test_runner.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lib/batching.py src/serve/runner.py src/hydrate/tail.py tests/unit/test_batching.py tests/integration/test_lifecycle.py tests/unit/test_cloner.py
git commit -m "perf: chunk candidate and etag IN queries"
```

---

### Task 3: Lazy shard planning + single root count

**Files:**
- Modify: `src/scheduler/shard_planner.py:19-72`, `src/discover/pipeline.py:210`, `src/serve/runner.py:501-507`
- Test: `tests/unit/test_shard_planner.py`, `tests/integration/test_runner.py`

**Interfaces:**
- `ShardPlanner(count_fn, *, max_fetchable=1000, scan_target=4000, min_date=..., root_count: int | None = None)`.
- `ShardPlanner.iter_plan(query, *, now: datetime | None = None) -> Iterator[ShardSpec]` (new).
- `ShardPlanner.plan(...) -> list[ShardSpec]` unchanged (delegates to `list(iter_plan(...))`).
- `run_search_discovery(..., total_count: int | None = None)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_shard_planner.py`:

```python
def test_root_count_override_skips_root_probe():
    calls = []

    def count_fn(query):
        calls.append(query)
        return 4000

    planner = ShardPlanner(count_fn, min_date=MIN_DATE, root_count=5000)
    specs = planner.plan("q", now=NOW)
    assert sum(spec.total_count for spec in specs) == 5000
    assert "q" not in calls


def test_iter_plan_probes_lazily():
    count_fn, calls = uniform_count(per_day=5000)
    iterator = ShardPlanner(count_fn, min_date=MIN_DATE).iter_plan("q", now=NOW)
    first = next(iterator)
    assert first.range_start.date() == MIN_DATE
    # A full plan for this window probes 19 times (10 leaves); the first leaf
    # must require only the root plus its ancestor halves.
    assert len(calls) < 19
```

Update the pinned duplicate count in `tests/integration/test_runner.py` (search for `topic:ai`; the assertion currently expects `["topic:ai", "topic:ai"]`): change the expected list to `["topic:ai"]`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_shard_planner.py -v`
Expected: FAIL — `iter_plan` missing; `root_count` unexpected.

- [ ] **Step 3: Implement**

`src/scheduler/shard_planner.py`:

```python
from collections.abc import Iterator


    def __init__(
        self,
        count_fn: Callable[[str], int],
        *,
        max_fetchable: int = 1000,
        scan_target: int = 4000,
        min_date: date = date(2008, 1, 1),
        root_count: int | None = None,
    ) -> None:
        ...
        self._root_count = root_count


    def iter_plan(self, query: str, *, now: datetime | None = None) -> Iterator[ShardSpec]:
        current = now if now is not None else datetime.now(UTC)
        self._window_end = current.date()
        root = self._root_count if self._root_count is not None else self._count_fn(query)
        if root == 0:
            return
        if self._fetchable(root):
            yield ShardSpec(query=query, range_start=None, range_end=None, total_count=root)
            return
        if self._window_end < self._min_date:
            raise ValueError("min_date is after now; there is no window to bisect")
        yield from self._bisect(query, self._min_date, self._window_end, root, 0)

    def plan(self, query: str, *, now: datetime | None = None) -> list[ShardSpec]:
        return list(self.iter_plan(query, now=now))
```

Convert `_bisect` to a generator (keep the body, replace `leaves.append(...)` with `yield ...`, `leaves.extend(self._bisect(...))` with `yield from self._bisect(...)`, drop the `leaves` list).

`src/discover/pipeline.py:210`:

```python
    for spec in ShardPlanner(count_fn, root_count=total_count).iter_plan(query):
```

Add `total_count: int | None = None` to `run_search_discovery`'s signature (keyword-only area near `max_shards`) and define `count_fn` as today.

`src/serve/runner.py:501-507`:

```python
    total_count = pipeline.count_total(deps, query)
    stats = pipeline.run_search_discovery(
        deps,
        query,
        max_shards=cfg.max_shards,
        max_pages=spec.max_pages,
        total_count=total_count,
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_shard_planner.py tests/integration/test_runner.py tests/integration/test_pipeline.py -q`
Expected: PASS. If an integration test calls `run_search_discovery` with a positional `total_count` expectation, keep it keyword-only.

- [ ] **Step 5: Commit**

```bash
git add src/scheduler/shard_planner.py src/discover/pipeline.py src/serve/runner.py tests/unit/test_shard_planner.py tests/integration/test_runner.py
git commit -m "perf: lazily plan shards and reuse the root count"
```

---

### Task 4: `deque` pending queue + O(1) Redis field access

**Files:**
- Modify: `src/discover/pipeline.py:288-291`, `src/scheduler/state_machine.py:54-64`
- Test: existing `tests/integration/test_pipeline.py`, `tests/unit/test_state_machine.py`

**Interfaces:** none public.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_state_machine.py`:

```python
def test_get_field_prefers_direct_key_access():
    from scheduler.state_machine import _get_field

    assert _get_field({"shard_id": "7"}, "shard_id") == "7"
    assert _get_field({b"shard_id": b"8"}, "shard_id") == "8"
    assert _get_field({}, "shard_id") is None
```

- [ ] **Step 2: Run test to verify it fails if behavior differs**

Run: `pytest tests/unit/test_state_machine.py::test_get_field_prefers_direct_key_access -v`
Expected: PASS already (current linear scan has the same behavior). This is a keep-green refactor test; continue.

- [ ] **Step 3: Implement**

`state_machine.py`:

```python
def _get_field(fields, name: str) -> str | None:
    value = fields.get(name)
    if value is None:
        value = fields.get(name.encode())
    return _as_text(value) if value is not None else None
```

`pipeline.py`: change `pending: list[int] = []` (find via `rg -n "pending" src/discover/pipeline.py` near line 189) to `pending: deque[int] = deque()`, add `from collections import deque`, and change `process(pending.pop(0))` to `process(pending.popleft())`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_state_machine.py tests/integration/test_pipeline.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/discover/pipeline.py src/scheduler/state_machine.py tests/unit/test_state_machine.py
git commit -m "perf: constant-time pending queue and Redis field lookup"
```

---

### Task 5: Atomic state transition + memoized stream groups

**Files:**
- Modify: `src/scheduler/state_machine.py:119-142` (`set_state`), `:153-181` (`_ensure_group`)
- Test: `tests/unit/test_state_machine.py`

**Interfaces:** behavior of `ShardStore.set_state` is unchanged (same success/KeyError/ValueError semantics); `_ensure_group` becomes idempotent per lane.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_state_machine.py`:

```python
def test_set_state_missing_shard_raises_key_error(store):
    with pytest.raises(KeyError):
        store.set_state(999, ShardState.ACTIVE)


def test_ensure_group_runs_once_per_lane(redis):
    class CountingRedis:
        def __init__(self, inner):
            self._inner = inner
            self.group_creates = 0

        def xgroup_create(self, *args, **kwargs):
            self.group_creates += 1
            return self._inner.xgroup_create(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    counting = CountingRedis(redis)
    queue = ShardQueue(counting)
    queue.enqueue(1)
    first = counting.group_creates
    assert first >= 1
    queue.claim("consumer", count=10)
    after_claim = counting.group_creates
    queue.claim("consumer", count=10)
    assert counting.group_creates == after_claim
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_state_machine.py::test_ensure_group_runs_once_per_lane -v`
Expected: FAIL — second `claim` re-issues `xgroup_create` per lane.

- [ ] **Step 3: Implement**

`_ensure_group` memoization:

```python
    def __init__(self, redis, *, lanes: int = 4, max_attempts: int = 3, prefix: str = "gitcrawl:shards") -> None:
        ...
        self._groups_ready: set[int] = set()

    def _ensure_group(self, lane: int) -> None:
        if lane in self._groups_ready:
            return
        try:
            self._redis.xgroup_create(self._lane_key(lane), _GROUP, id="0", mkstream=True)
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise
        self._groups_ready.add(lane)
```

`set_state` as one guarded statement:

```python
_SOURCES: dict[ShardState, frozenset[ShardState]] = {
    target: frozenset(source for source, targets in ALLOWED_TRANSITIONS.items() if target in targets)
    for target in ShardState
}


    def set_state(self, shard_id: int, new_state: ShardState, *, fetched=None, incomplete=None, total_count=None) -> None:
        values: dict[str, object] = {"state": new_state.value}
        if fetched is not None:
            values["fetched"] = fetched
        if incomplete is not None:
            values["incomplete"] = incomplete
        if total_count is not None:
            values["total_count"] = total_count
        allowed = [state.value for state in _SOURCES[new_state]]
        with self._engine.begin() as connection:
            updated = connection.execute(
                update(Shard)
                .where(Shard.id == shard_id, Shard.state.in_(allowed))
                .values(**values)
                .returning(Shard.id)
            ).scalar_one_or_none()
            if updated is None:
                current = connection.execute(
                    select(Shard.state).where(Shard.id == shard_id)
                ).scalar_one_or_none()
                if current is None:
                    raise KeyError(shard_id)
                raise ValueError(f"illegal shard transition {current} -> {new_state.value}")
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_state_machine.py tests/integration/test_pipeline.py -q`
Expected: PASS — existing illegal-transition and metrics tests cover `set_state`.

- [ ] **Step 5: Commit**

```bash
git add src/scheduler/state_machine.py tests/unit/test_state_machine.py
git commit -m "perf: atomic shard transitions and memoized stream groups"
```

---

## Plan self-review

- **Spec coverage:** S1 → T1; S2 → T2; S3 + #14 → T3; #30/#44 → T4; #28/#29 → T5; #47 → T1 step 3.
- **Placeholders:** none; every code step contains code. Two steps say "find with `rg`" because the anchor line moves as lines are added earlier in the file; the command is given.
- **Type consistency:** `chunked` takes `Sequence[T]`; `_collect_ids` takes `Iterable[Mapping]` (Mapping already imported in pipeline.py); `iter_plan` yields `ShardSpec`; `root_count` is keyword-only everywhere.
- **Regression guard:** `plan()` remains list-returning so `tests/unit/test_shard_planner.py:38-149` is untouched except for the two additive tests.
