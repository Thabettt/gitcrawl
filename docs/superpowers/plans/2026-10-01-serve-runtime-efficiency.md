# Serve Runtime Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound the payload cache and collapse duplicate crawls, stop interactive searches queueing behind bulk runs, cut per-poll and per-page DB round trips, fix the `run_items` index/deep-page cost, stream exports instead of buffering whole runs, and batch audit writes while parsing each response body once.

**Architecture:** A small `RunPayloadCache` owns LRU + TTL + single-flight; the executor gains a priority lane (`submit_call` priority 0, bulk `submit` priority 1) while keeping one worker; page handlers thread already-fetched rows/counts; export becomes a byte-iterator behind `StreamingResponse`; audit buffering rides on `Deps` so existing no-buffer tests keep their per-record semantics.

**Tech Stack:** FastAPI/Starlette, Jinja2, SQLAlchemy 2.1 + psycopg3, Alembic, pytest.

**Spec:** `docs/superpowers/plans/2026-10-01-efficiency-audit-report.md` (findings S4, S5, S6, S12, S13, S15, #7, #20-23, #35, #43)

## Global Constraints

- Python 3.12, ruff line length 100.
- `CONSOLE` behavior/contracts unchanged: JSON/CSV export keys and values, cache TTL semantics (`CACHE_TTL_SECONDS=120`), and pagination sizes stay the same.
- Existing tests are the contract; only update expectations that intentionally change (export buffering is internal; response bytes must still parse identically).
- Migration revision `0007` follows `0006` from the store plan.
- Integration tests need `TEST_DATABASE_URL` ending `_test`.
- One commit per task, prefix `perf:`.

---

## File Map

| File | Change |
|---|---|
| `src/serve/payload_cache.py` | **new** — `RunPayloadCache` LRU/TTL/single-flight |
| `src/serve/app.py` | wire cache; wrap heavy sync work in threadpool |
| `src/serve/executor.py` | priority queue, completed-future pruning, event-based `wait` |
| `src/serve/pages.py` | `_table_view` row/total injection; run-page single count; health cache; same-hash limit |
| `src/enrich/cloner.py` | `estimate_clone_from_totals` |
| `src/serve/runs.py` | `_run_totals` fast path; registry-first progress; batched export iterators |
| `src/store/models.py`, `migrations/versions/0007_run_items_order_idx.py` | index matching the `NULLS LAST` order |
| `src/serve/diff.py` | NULL-safe composite key |
| `src/serve/audit.py` | `AuditBuffer`, `cached_json`, `record_from_response(body=…)` |
| `src/discover/pipeline.py`, `src/serve/runner.py` | buffer-aware audit hooks; flush |
| `src/discover/search_shards.py`, `src/hydrate/repo_client.py`, `src/enrich/trees_first.py` | reuse `cached_json` |

---

### Task 1: Bounded, single-flight payload cache

**Files:**
- Create: `src/serve/payload_cache.py`
- Modify: `src/serve/app.py:274-302,391-411`
- Test: `tests/unit/test_run_cache.py` (new), `tests/contract/test_vsearch_api.py`

**Interfaces:**
- `RunPayloadCache(*, max_entries: int = 100, ttl_seconds: float = CACHE_TTL_SECONDS, clock: Callable[[], float] = time.time)`.
- `get(key) -> RunPayload | None`; `set(key, payload) -> None`; `run_once(key, producer: Callable[[], RunPayload]) -> RunPayload`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_run_cache.py
from __future__ import annotations

import threading
import time

from serve.payload_cache import RunPayloadCache


def _payload(marker: str):
    from serve.runner import RunPayload

    return RunPayload(total_count=1, fetched=1, incomplete=False, items=[], field_stats={}) if False else marker


def test_ttl_expiry_uses_injected_clock():
    now = {"value": 0.0}
    cache = RunPayloadCache(ttl_seconds=120, clock=lambda: now["value"])
    cache.set("k", "v1")
    now["value"] = 119.0
    assert cache.get("k") == "v1"
    now["value"] = 120.0
    assert cache.get("k") is None


def test_lru_eviction_at_capacity():
    cache = RunPayloadCache(max_entries=2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")
    cache.set("c", 3)
    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3


def test_run_once_collapses_concurrent_misses():
    cache = RunPayloadCache()
    calls = {"n": 0}
    barrier = threading.Barrier(4)

    def producer():
        calls["n"] += 1
        time.sleep(0.05)
        return "payload"

    results: list[str] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        value = cache.run_once("key", producer)
        with lock:
            results.append(value)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls["n"] == 1
    assert results == ["payload"] * 4
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_run_cache.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# src/serve/payload_cache.py
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future
from typing import TypeVar

T = TypeVar("T")


class RunPayloadCache:
    def __init__(
        self,
        *,
        max_entries: int = 100,
        ttl_seconds: float = 120.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._entries: OrderedDict[str, tuple[float, T]] = OrderedDict()
        self._inflight: dict[str, Future] = {}
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._ttl = ttl_seconds
        self._clock = clock

    def get(self, key: str) -> T | None:
        with self._lock:
            return self._get_locked(key)

    def _get_locked(self, key: str) -> T | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() - entry[0] >= self._ttl:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry[1]

    def set(self, key: str, payload: T) -> None:
        with self._lock:
            self._entries[key] = (self._clock(), payload)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def run_once(self, key: str, producer: Callable[[], T]) -> T:
        with self._lock:
            cached = self._get_locked(key)
            if cached is not None:
                return cached
            pending = self._inflight.get(key)
            if pending is None:
                pending = Future()
                self._inflight[key] = pending
                owner = True
            else:
                owner = False
        if not owner:
            return pending.result()
        try:
            value = producer()
        except BaseException as exc:
            with self._lock:
                self._inflight.pop(key, None)
            pending.set_exception(exc)
            raise
        self.set(key, value)
        with self._lock:
            self._inflight.pop(key, None)
        pending.set_result(value)
        return value
```

`app.py`: delete the `cache`/`lock`/`_cached_payload` code (`:274-302`) and build `payload_cache = RunPayloadCache(clock=clock)` in `create_app`; in `list_repos` replace the miss branch with:

```python
        payload = payload_cache.run_once(
            key,
            lambda: executor_for().submit_call(lambda: runner_for()(0, spec_to_dict(spec))).result(),
        )
```

keeping the existing `try/except (RequestFailed, PartialResultsError, ThrottledError)` around the `run_once` call.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_run_cache.py tests/contract/test_vsearch_api.py -q`
Expected: PASS — the cache-expiry contract test still sees a second runner call after 121 s via the injected clock.

- [ ] **Step 5: Commit**

```bash
git add src/serve/payload_cache.py src/serve/app.py tests/unit/test_run_cache.py
git commit -m "perf: bounded single-flight payload cache"
```

---

### Task 2: Interactive priority lane + future pruning

**Files:**
- Modify: `src/serve/executor.py:231-301`
- Test: `tests/integration/test_executor.py`

**Interfaces:** `RunExecutor` keeps its public API; `submit_call` jobs run before queued `submit` runs; `_futures` no longer grows one entry per completed run; `wait(timeout)` remains a bounded blocker.

- [ ] **Step 1: Write the failing test**

```python
def test_interactive_calls_jump_ahead_of_bulk_runs(engine):
    import threading
    import time as time_module

    order: list[str] = []
    release = threading.Event()

    def slow(run_id, spec):
        order.append(f"bulk-{run_id}")
        release.wait(timeout=2)
        return payload_for(run_id)

    executor = RunExecutor(engine, runner=slow)
    executor.submit(1)
    time_module.sleep(0.05)
    interactive = executor.submit_call(lambda: order.append("interactive") or "done")
    executor.submit(2)
    release.set()
    assert interactive.result(timeout=2) == "done"
    assert order.index("interactive") < order.index("bulk-2")
```

(`payload_for` / `engine` should mirror the file's existing helpers.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_executor.py::test_interactive_calls_jump_ahead_of_bulk_runs -v`
Expected: FAIL — interactive is processed strictly FIFO after bulk-2.

- [ ] **Step 3: Implement**

```python
import itertools

        self._queue: queue.PriorityQueue[tuple[int, int, Callable[[], object], Future]] = (
            queue.PriorityQueue()
        )
        self._sequence = itertools.count()
        self._idle = threading.Event()
        self._idle.set()
```

`submit` (priority 1) and `submit_call` (priority 0):

```python
    def submit(self, run_id: int, runner: Runner | None = None) -> Future:
        future: Future = Future()
        with self._lock:
            done = [key for key, value in self._futures.items() if value.done()]
            for key in done:
                del self._futures[key]
            self._futures[run_id] = future

        def job() -> None:
            execute_run(self._engine, run_id, runner=runner or self._runner, runs_root=self._runs_root)

        self._enqueue(1, job, future)
        return future

    def submit_call(self, func: Callable[[], object]) -> Future:
        future: Future = Future()
        self._enqueue(0, func, future)
        return future

    def _enqueue(self, priority: int, job: Callable[[], object], future: Future) -> None:
        self._idle.clear()
        self._queue.put((priority, next(self._sequence), job, future))
```

`wait`:

```python
    def wait(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout) and self._queue.unfinished_tasks == 0
```

`_work`: unpack `(priority, sequence, job, future)`; in `finally`, after `task_done()`, `if self._queue.unfinished_tasks == 0: self._idle.set()`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/integration/test_executor.py -q`
Expected: PASS — FIFO ordering for equal-priority bulk runs is preserved by the sequence counter.

- [ ] **Step 5: Commit**

```bash
git add src/serve/executor.py tests/integration/test_executor.py
git commit -m "perf: interactive lane and future pruning in run executor"
```

---

### Task 3: One run row, one item count, one aggregate estimate, registry-first progress

**Files:**
- Modify: `src/serve/pages.py:59-88,305-315,379-437,746-772,893-907`, `src/serve/runs.py:196-250`, `src/enrich/cloner.py:113-133`
- Test: `tests/integration/test_executor.py` (statement spy), `tests/integration/test_runner.py`, `tests/contract/test_clone_endpoints.py`, `tests/contract/test_pages.py`

**Interfaces:**
- `_table_view(engine, run_id, sort, direction, page, *, run_row=None, total=None)`.
- `cloner.estimate_clone_from_totals(repos, size_kb, *, mode, disk_free_mb=None, low_disk_threshold_mb=LOW_DISK_THRESHOLD_MB) -> CloneEstimate`.
- `read_clone_progress` consults the registry before the DB.

- [ ] **Step 1: Write the failing test**

```python
def test_full_run_clone_estimate_uses_one_aggregate(clean: Engine, tmp_path):
    from sqlalchemy import event

    from serve.runs import clone_estimate_for_run

    run_id, _ = seed_run(clean, [(1, "octo/one", 1024, 1), (2, "octo/two", 2048, 2)])
    statements: list[str] = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(clean, "before_cursor_execute", listener)
    try:
        estimate = clone_estimate_for_run(
            clean, run_id, limit=None, mode=CloneMode.SHALLOW, dest_root=str(tmp_path)
        )
    finally:
        event.remove(clean, "before_cursor_execute", listener)
    assert estimate.repos == 2
    assert estimate.estimated_mb == pytest.approx(3.0)
    selects = [s for s in statements if "FROM run_items" in s]
    assert len(selects) == 1  # one aggregate, no per-item row fetch
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_cloner.py::test_full_run_clone_estimate_uses_one_aggregate -v`
Expected: FAIL — estimate fetches all joined rows.

- [ ] **Step 3: Implement**

`cloner.py`:

```python
def estimate_clone_from_totals(
    repos: int,
    size_kb: int,
    *,
    mode: CloneMode,
    disk_free_mb: float | None = None,
    low_disk_threshold_mb: float = LOW_DISK_THRESHOLD_MB,
) -> CloneEstimate:
    mode = CloneMode(mode)
    estimated_mb = size_kb * MODE_FACTORS[mode] / 1024
    warnings: list[str] = []
    if disk_free_mb is not None and estimated_mb > disk_free_mb - low_disk_threshold_mb:
        warnings.append(
            f"low disk: estimated {estimated_mb:.1f} MB with {disk_free_mb:.1f} MB free "
            f"(reserve {low_disk_threshold_mb:.0f} MB)"
        )
    return CloneEstimate(repos=repos, estimated_mb=estimated_mb, warnings=tuple(warnings))
```

`estimate_clone` delegates the warning/factor math to it after summing `_top_items`.

`runs.py`:

```python
def _run_totals(engine: Engine, run_id: int) -> tuple[int, int]:
    with engine.connect() as connection:
        count, size_kb = connection.execute(
            select(func.count(), func.coalesce(func.sum(Repo.size_kb), 0))
            .select_from(RunItem)
            .outerjoin(Repo, Repo.id == RunItem.repo_id)
            .where(RunItem.run_id == run_id)
        ).one()
    return int(count or 0), int(size_kb or 0)


def clone_estimate_for_run(...):
    if limit is None:
        repos, size_kb = _run_totals(engine, run_id)
        _row_for_run(engine, run_id)  # preserve KeyError semantics
        return estimate_clone_from_totals(
            repos, size_kb, mode=mode, disk_free_mb=free_disk_mb(dest_root),
            low_disk_threshold_mb=low_disk_threshold_mb,
        )
    ...
```

`read_clone_progress`: move `tracked = registry.get(run_id)` above `_row_for_run`.

`pages.py` `_table_view`: accept keyword-only `run_row` and `total`; skip `_run_detail_row` when `run_row` is not None and skip the `count(*)` when `total` is not None. `run_page`:

```python
        row = _run_detail_row(engine, run_id)
        ...
        item_count = _item_count(engine, run_id)
        table = _table_view(engine, run_id, "stars", "desc", 1, run_row=row, total=item_count)
        ...
                "run": _run_detail_summary(row, item_count),
```

Health cache in `pages.py`:

```python
HEALTH_CACHE_SECONDS = 5.0
_redis_client = None


def _default_redis_ping() -> bool:
    global _redis_client
    url = os.environ.get("REDIS_URL")
    if not url:
        return False
    if _redis_client is None:
        import redis

        _redis_client = redis.Redis.from_url(
            url,
            socket_connect_timeout=HEALTH_TIMEOUT_SECONDS,
            socket_timeout=HEALTH_TIMEOUT_SECONDS,
        )
    try:
        return bool(_redis_client.ping())
    except Exception:
        return False
```

Memoize the dashboard health dict with an `_health_cache` `{at, value}` guarded by a lock and a 5-second TTL at its call site (search `health_snapshot` in `pages.py`; wrap the returned dict). Ensure the cached value is a dict copy to avoid mutation.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_cloner.py tests/contract/test_clone_endpoints.py tests/contract/test_pages.py tests/contract/test_run_detail.py -q`
Expected: PASS. With `redis_ping` injected, add a two-request dashboard test asserting the injected ping ran once.

- [ ] **Step 5: Commit**

```bash
git add src/serve/pages.py src/serve/runs.py src/enrich/cloner.py tests/unit/test_cloner.py tests/contract/test_pages.py
git commit -m "perf: single run lookup/count, aggregate clone estimate, cached health"
```

---

### Task 4: `run_items` index matches the sort; cap same-hash candidates

**Files:**
- Modify: `src/store/models.py:179-185`, `src/serve/pages.py:210-231`, `src/serve/runs.py:91`
- Create: `migrations/versions/0007_run_items_order_idx.py`
- Test: `tests/integration/test_models_migrations.py`, `tests/contract/test_console_pages.py` / `tests/integration/test_diff.py`

**Interfaces:** `_same_hash_runs(engine, row, *, limit: int = 50) -> list[dict]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_same_hash_candidates_are_capped(clean: Engine):
    from serve.pages import _same_hash_runs

    run_id, filter_hash = seed_run(clean, [(1, "octo/one", 1, 1)])
    for _ in range(55):
        create_run(clean, FILTER, api_version="v1")
    with clean.connect() as connection:
        row = connection.execute(
            text("SELECT * FROM runs WHERE id = :id"), {"id": run_id}
        ).mappings().one()
    assert len(_same_hash_runs(clean, row)) == 50
```

(Use the file's existing run-seeding helper; `seed_run` returns `(run_id, filter_hash)` in `test_cloner.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_diff.py -k same_hash_candidates -v`
Expected: FAIL — 55 candidates returned.

- [ ] **Step 3: Implement**

`pages.py`:

```python
def _same_hash_runs(engine: Engine, row, *, limit: int = 50) -> list[dict]:
    ...
                .order_by(Runs.created_at.desc(), Runs.id.desc())
                .limit(limit)
```

`models.py` `RunItem.__table_args__`:

```python
        Index(
            "run_items_stars_idx",
            "run_id",
            text("stargazers DESC NULLS LAST"),
            "repo_id",
        ),
```

`runs.py:91` align the export order: `.order_by(RunItem.stargazers.desc().nullslast(), RunItem.repo_id)`.

`migrations/versions/0007_run_items_order_idx.py`:

```python
"""run_items index matching NULLS LAST ordering

Revision ID: 0007
Revises: 0006
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("run_items_stars_idx", table_name="run_items")
    op.create_index(
        "run_items_stars_idx",
        "run_items",
        ["run_id", sa.text("stargazers DESC NULLS LAST"), "repo_id"],
    )


def downgrade() -> None:
    op.drop_index("run_items_stars_idx", table_name="run_items")
    op.create_index("run_items_stars_idx", "run_items", ["run_id", sa.text("stargazers DESC")])
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/integration/test_models_migrations.py tests/integration/test_diff.py tests/contract/test_console_pages.py tests/contract/test_run_detail.py -q`
Expected: PASS; update the parity test's expected index definition if it enumerates `run_items_stars_idx`.

- [ ] **Step 5: Commit**

```bash
git add src/store/models.py migrations/versions/0007_run_items_order_idx.py src/serve/pages.py src/serve/runs.py tests/integration/test_models_migrations.py tests/integration/test_diff.py
git commit -m "perf: align run_items index with NULLS LAST sort and cap diff candidates"
```

---

### Task 5: Streaming export + NULL-safe diff

**Files:**
- Modify: `src/serve/runs.py:74-161`, `src/serve/diff.py:54-111`, `src/serve/app.py:508-545`
- Test: `tests/contract/test_runs_export.py`, `tests/integration/test_diff.py`

**Interfaces:**
- `export_bundle(engine, run_id, *, format, runs_root="runs") -> tuple[Iterator[bytes], str]` (was `bytes`).
- `_run_items` keys become `(repo_id is None, repo_id or 0, full_name)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/integration/test_diff.py`:

```python
def test_diff_handles_null_repo_ids(clean: Engine):
    from serve.diff import diff_runs

    base_id = create_run(clean, FILTER, api_version="v1")
    viewed_id = create_run(clean, FILTER, api_version="v1")
    with clean.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO run_items (run_id, repo_id, full_name, stargazers)"
                " VALUES (:run_id, NULL, 'ghost/one', 1)"
            ),
            {"run_id": base_id},
        )
        connection.execute(
            text(
                "INSERT INTO run_items (run_id, repo_id, full_name, stargazers)"
                " VALUES (:run_id, NULL, 'ghost/two', 2)"
            ),
            {"run_id": viewed_id},
        )
        connection.execute(
            text(
                "INSERT INTO run_items (run_id, repo_id, full_name, stargazers)"
                " VALUES (:run_id, NULL, 'ghost/one', 1)"
            ),
            {"run_id": viewed_id},
        )
    result = diff_runs(clean, base_id, viewed_id)
    assert result.summary == {
        "added": 1,
        "removed": 0,
        "changed_repos": 0,
        "changed_fields": 0,
    }
```

For export, keep existing tests but change assertions to consume the stream: `content = b"".join(export_bundle(...)[0])` then `json.loads(content)` and compare parsed structure plus `X-Gitcrawl-Regenerated`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_diff.py::test_diff_handles_null_repo_ids tests/contract/test_runs_export.py -v`
Expected: FAIL — `TypeError` on `None` keys; `export_bundle` returns bytes so `b"".join` fails.

- [ ] **Step 3: Implement**

`runs.py`:

```python
def _file_chunks(path: Path, chunk_size: int = 65536) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                return
            yield block


def _json_chunks(engine: Engine, run_row: RowMapping, run_id: int) -> Iterator[bytes]:
    header = {
        "filter": run_row["filter_spec"],
        "filter_hash": run_row["filter_hash"],
        "run_id": run_id,
        "ran_at": _iso(run_row["finished_at"] or run_row["created_at"]),
        "api_version": run_row["api_version"],
        "total_count": run_row["total_count"],
        "fetched": run_row["fetched"],
        "incomplete": run_row["status"] == "partial",
        "regenerated": True,
    }
    yield b"{"
    for key, value in header.items():
        yield json.dumps(key).encode() + b": " + json.dumps(value, ensure_ascii=False).encode() + b","
    yield b'"items": ['
    first = True
    for row in _item_rows(engine, run_id):
        chunk = json.dumps(_snapshot(row), ensure_ascii=False).encode()
        yield chunk if first else b"," + chunk
        first = False
    yield b'], "field_stats": {}}'


def _csv_chunks(engine: Engine, run_id: int) -> Iterator[bytes]:
    header_buffer = io.StringIO()
    csv.writer(header_buffer).writerow(_CSV_HEADER)
    yield header_buffer.getvalue().encode("utf-8")
    for row in _item_rows(engine, run_id):
        buffer = io.StringIO()
        csv.writer(buffer).writerow(
            [
                _csv_cell(row["repo_id"]), _csv_cell(row["full_name"]), _csv_cell(row["stargazers"]),
                _csv_cell(_iso(row["pushed_at"])), _csv_cell(row["archived"]), _csv_cell(row["language"]),
                _csv_cell(row["license_spdx"]), _csv_cell(row["country_iso"]), _csv_cell(row["geo_confidence"]),
            ]
        )
        yield buffer.getvalue().encode("utf-8")


def export_bundle(engine, run_id, *, format, runs_root="runs") -> tuple[Iterator[bytes], str]:
    ...
    if path.is_file():
        return _file_chunks(path), _MEDIA_TYPES[format]
    if format == "json":
        return _json_chunks(engine, run_row, run_id), _MEDIA_TYPES[format]
    return _csv_chunks(engine, run_id), _MEDIA_TYPES[format]
```

`app.py` export route: set `X-Gitcrawl-Regenerated` from `bundle_file(...).is_file()` before building the response, then `return StreamingResponse(chunks, media_type=media_type, headers=headers)`.

`diff.py`:

```python
def _item_key(row) -> tuple[bool, int, str]:
    repo_id = row["repo_id"]
    return (repo_id is None, repo_id or 0, str(row["full_name"]))


    return {_item_key(row): dict(row) for row in rows}
```

Update `added`/`removed`/`changed` loops to iterate sorted keys and read `repo_id`/`full_name` from the dict values (never from the key).

- [ ] **Step 4: Run tests**

Run: `pytest tests/contract/test_runs_export.py tests/integration/test_diff.py -q`
Expected: PASS; JSON/CSV parsed content identical to before (byte layout differs only by whitespace).

- [ ] **Step 5: Commit**

```bash
git add src/serve/runs.py src/serve/diff.py src/serve/app.py tests/contract/test_runs_export.py tests/integration/test_diff.py
git commit -m "perf: stream exports in constant memory and make diff NULL-safe"
```

---

### Task 6: Batched audit + parse-once + threadpool for sync DB work

**Files:**
- Modify: `src/serve/audit.py:79-122`, `src/discover/pipeline.py:72-82`, `src/serve/runner.py:78-124,496-544`, `src/discover/search_shards.py`, `src/hydrate/repo_client.py:95-100`, `src/enrich/trees_first.py:36-45`, `src/serve/app.py` async routes
- Test: `tests/unit/test_audit.py`, `tests/integration/test_audit_db.py`, `tests/integration/test_pipeline.py`, contract suites

**Interfaces:**
- `record_from_response(..., body: object | None = None)`; parsed body stored on `response.extensions["gitcrawl.json"]`.
- `cached_json(response) -> object | None`.
- `AuditBuffer(engine, *, batch_size=100)` with `add(record)` and `flush()`.
- `Deps.audit_buffer: AuditBuffer | None = None` (default preserves per-record `record_audit` calls).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_audit.py
def test_record_from_response_caches_parsed_body():
    from serve.audit import cached_json

    response = _response(200, json={"total_count": 7, "incomplete_results": False})
    body = response.json()
    # emulate the hook having cached it
    response.extensions["gitcrawl.json"] = body
    assert cached_json(response) is body


def test_record_from_response_accepts_preparsed_body():
    from serve.audit import record_from_response

    response = _response(200)
    record = record_from_response(
        {}, response, token_fp="fp", latency_ms=1, now=NOW, body={"total_count": 9}
    )
    assert record.total_count == 9
```

```python
# tests/integration/test_audit_db.py
def test_audit_buffer_batches_inserts(clean: Engine):
    from sqlalchemy import event

    from serve.audit import AuditBuffer

    statements: list[str] = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if "INTO audit_log" in statement.upper():
            statements.append(statement)

    buffer = AuditBuffer(clean, batch_size=10)
    event.listen(clean, "before_cursor_execute", listener)
    try:
        for index in range(10):
            buffer.add(_record(index))
    finally:
        event.remove(clean, "before_cursor_execute", listener)
    assert len(statements) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_audit.py tests/integration/test_audit_db.py -k "cached or buffer or preparsed" -v`
Expected: FAIL — missing `cached_json`, `body` param, `AuditBuffer`.

- [ ] **Step 3: Implement**

`audit.py`:

```python
_PARSED_BODY_KEY = "gitcrawl.json"


def cached_json(response: httpx.Response) -> object | None:
    return response.extensions.get(_PARSED_BODY_KEY)


def record_from_response(..., body: object | None = None, ...) -> AuditRecord:
    if body is None:
        try:
            body = response.json()
        except Exception:
            body = None
    response.extensions[_PARSED_BODY_KEY] = body
    ...


class AuditBuffer:
    def __init__(self, engine: Engine, *, batch_size: int = 100) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self._engine = engine
        self._batch_size = batch_size
        self._records: list[AuditRecord] = []

    def add(self, record: AuditRecord) -> None:
        self._records.append(record)
        if len(self._records) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._records:
            return
        records, self._records = self._records, []
        with self._engine.begin() as connection:
            connection.execute(insert(AuditLog), [asdict(record) for record in records])
```

`pipeline._audit_hook(deps)`: when `deps.audit_buffer is not None`, `buffer.add(record)`; otherwise `audit.record_audit(...)` (preserves `test_audit_hook_exception_propagates`). `runner._audit_hook` likewise. `runner.build_deps` creates the buffer; `run_filter` flushes in a `finally`.

Callers that parse: replace `response.json()` with `audit.cached_json(response) or response.json()` in `discover/search_shards.py` (page parse), `hydrate/repo_client.py:96`, and `enrich/trees_first.py:40` (`_require_json`). Keep the existing `try/except ValueError` semantics: `cached_json` returns an already-parsed object, so validate with the same type checks.

`app.py`: in `create_run_route`, `filter_submit`-style handlers, and `clone_start_route`, wrap sync engine work with `await run_in_threadpool(...)` from `starlette.concurrency` (e.g. `run_id = await run_in_threadpool(create_run, bound_engine, spec_to_dict(spec), api_version=API_VERSION)`), leaving `await request.json()`/CSRF as-is. Do not change response shapes or status codes.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_audit.py tests/integration/test_audit_db.py tests/integration/test_pipeline.py tests/integration/test_runner.py tests/contract -q`
Expected: PASS. Update `test_audit_hook_exception_propagates` only if `build_deps` now wires a buffer in integration fixtures; with `buffer=None` it is unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/serve/audit.py src/discover/pipeline.py src/serve/runner.py src/discover/search_shards.py src/hydrate/repo_client.py src/enrich/trees_first.py src/serve/app.py tests/unit/test_audit.py tests/integration/test_audit_db.py
git commit -m "perf: batch audit inserts, parse bodies once, unblock the event loop"
```

---

## Plan self-review

- **Spec coverage:** S4 → T1; S5 → T2; S15 (run page/estimate/registry/health) → T3; S13 + #21 → T4; S12 + #7 → T5; S6 → T6.
- **Placeholders:** none; all logic and test bodies are concrete. Two tests reference existing helpers (`seed_run`, `payload_for`, `_record`, `_response`, `FILTER`) that live in the named files.
- **Type consistency:** `RunPayloadCache` is generic; `AuditBuffer.flush` uses one multi-row insert via `insert(AuditLog), [dict, …]`; `export_bundle` iterator type flows into `StreamingResponse`.
- **Ordering:** T4's migration (0007) requires the store plan's 0005/0006 to exist; execute this plan after `2026-10-01-store-hydrate-efficiency.md`.
