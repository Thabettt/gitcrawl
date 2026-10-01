# Zero-Behavior Runtime Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the Phase 1a runtime findings from the quality-hardening spec — token leak, lazy singleton races, clone guard/registry, shard poison recovery, upsert input isolation, GraphQL partial surfacing, and GeoCache bind chunking — with zero observable change for normal use, plus one explicitly gated counter fix (E1).

**Architecture:** Each fix is local and additive to internal signatures. `_LazyLoaders` in `app.py` serializes first construction per key; `CloneRegistry.claim` moves the check-and-claim under the registry lock and evicts only terminal entries; `discover.pipeline.process` rolls a failed shard back to `PENDING` and hands the message to the existing `retry_or_dlq` path; `normalize_repo` coerces the remaining unbindable raw fields; `graphql_batch._fetch` returns a `BatchFetch` carrying surviving results plus an explicit `incomplete`/`errors` signal that `fetch_graphql_batch` logs and discards; `GeoCache.put_many` chunks binds like `get_many`; the E1 task threads `DiscoveryStats.updated/unchanged/skipped` through `RunPayload` to the run row and adds the missing `unchanged` readout.

**Tech Stack:** Python 3.12, FastAPI/Starlette, SQLAlchemy 2 + psycopg3, Redis (fakeredis in tests), pytest, httpx MockTransport.

**Spec:** docs/superpowers/specs/2026-10-01-quality-hardening-design.md

## Global Constraints

- **INV-1 — Identical normal behavior.** For normal use (definition in spec §2), every observable output (HTTP status, body, headers except additive ones, files, DB rows, ordering, counts) is identical to today.
- **INV-2 — Additive-only UI.** No existing control moves, disappears, gains a required step, changes meaning, or changes its result.
- **INV-3 — Allowed behavior changes.** Behavior may differ only for cross-site requests, requests that hang past a generous deadline, bodies above a generous cap, visibility of infrastructure outage, and the single approved visible fix (E1 run summary counters).
- **INV-4 — No runtime surface.** CI, formatting, typing, coverage, fixture centralization, annotation, and documentation work change no runtime behavior.
- **INV-5 — Exception register.** Any unavoidable deviation is recorded in spec §11 with an explicit approval checkbox before it ships.
- No database migrations or schema changes. Do not delete unwired modules (`limiter/retry.py`, `scheduler.tiering.order_shards`, `since_scan.plan_id_ranges`, `upserts.bootstrap_copy`, `mirrors`, `graphql_batch`, `skeleton.py`).
- Python 3.12; ruff line length 100; run `pytest` from the repository root.
- Integration tests require `TEST_DATABASE_URL` (database name must end in `_test`, `tests/conftest.py:19-26`).
- One commit per task, message prefix `fix:` (task 8 uses `fix:` too, but only after the E1 gate is signed off).
- Task 8 is **gated**: do not execute it unless the E1 checkbox in spec §11 is checked and signed off.
- Existing tests remain the contract; every task must leave the suites named in its run step green.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/enrich/trees_first.py` | modify `fetch_metafiles` call site | send `auth=False` so no GitHub `Authorization` header reaches `METAFILES_BASE_URL` |
| `src/serve/app.py` | modify `create_app` state block | `_LazyLoaders` per-key single-flight construction for engine/runner/executor |
| `src/serve/runs.py` | modify `CloneRegistry`, `start_clone` | atomic `claim(run_id, factory)`; bound registry by idle age and count, terminal entries only |
| `src/discover/pipeline.py` | modify `process` and the queue consumer | roll failed shards back to `PENDING`; route the message through `retry_or_dlq`; stop acking retried messages |
| `src/store/upserts.py` | modify `normalize_repo` | coerce/validate `size`, `language`, `visibility`, and boolean flags so one dirty row cannot abort a chunk |
| `src/enrich/graphql_batch.py` | modify `_fetch`, `fetch_graphql_batch` | return surviving partials plus an explicit incompleteness signal; log it in the caller |
| `src/enrich/geo_resolver.py` | modify `put_many` | chunk binds like `get_many` |
| `src/serve/executor.py` | modify `RunPayload`, `execute_run` (task 8, gated) | carry and persist `updated`/`unchanged`/`skipped` |
| `src/serve/runner.py` | modify `_run_filter` return (task 8, gated) | copy `DiscoveryStats` counters into `RunPayload` |
| `src/serve/templates/partials/status.html` | modify counts row (task 8, gated) | add the missing `unchanged` readout |
| `tests/unit/test_app_singletons.py` | **create** | `_LazyLoaders` concurrency and `create_app` wiring |
| `tests/unit/test_clone_registry.py` | **create** | claim atomicity, terminal reuse, eviction semantics |
| `tests/integration/test_clone_registry.py` | **create** | concurrent `start_clone` identity; evicted entry falls back to the identical progress file |
| `tests/unit/test_trees_first.py` | modify | no `Authorization` header on the metafiles call |
| `tests/integration/test_pipeline.py` | modify | poison shard rollback/retry/DLQ |
| `tests/integration/test_upserts.py` | modify | dirty-field coercion and chunk survival |
| `tests/unit/test_graphql_batch.py` | modify | partial signal and warning log |
| `tests/integration/test_geo_resolver.py` | modify | `put_many` statement chunking |
| `tests/integration/test_executor.py`, `tests/integration/test_runner.py`, `tests/contract/test_run_detail.py` | modify (task 8, gated) | counter persistence, runner population, UI readout |

---

### Task 1: Keep the GitHub token off the metafiles host

**Files:**
- Modify: `src/enrich/trees_first.py:176-188` (the `request_with_retry` call inside `fetch_metafiles`)
- Test: `tests/unit/test_trees_first.py` (append after line 380)

**Interfaces:**
- Consumes: `request_with_retry(client, method, url, *, ..., auth: bool = True, ...)` (`src/lib/gh_client.py:106-148`); when `auth=False` it builds the request and pops `Authorization` before sending (`src/lib/gh_client.py:143-148`). Precedent: `src/enrich/mirrors.py:54-63` already passes `auth=False`.
- Produces: `fetch_metafiles(client, full_name, *, names, base_url=METAFILES_BASE_URL, ...) -> FilePresence` — signature and return value unchanged; only the outbound request loses `Authorization`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_trees_first.py`:

```python
def test_fetch_metafiles_never_forwards_the_client_authorization_header():
    from enrich.trees_first import fetch_metafiles

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"manifests": [{"filename": "Dockerfile"}]})

    client = httpx.Client(
        headers={"Authorization": "Bearer super-secret"},
        transport=httpx.MockTransport(handler),
    )

    presence = fetch_metafiles(client, "octo/hello", names=("Dockerfile",))

    assert presence.paths == frozenset({"Dockerfile"})
    assert len(captured) == 1
    assert "authorization" not in captured[0].headers
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_trees_first.py::test_fetch_metafiles_never_forwards_the_client_authorization_header -v`
Expected: FAIL — `assert "authorization" not in captured[0].headers` because `fetch_metafiles` omits `auth=False` and the client-level `Authorization: Bearer super-secret` is sent to `repos.ecosyste.ms`.

- [ ] **Step 3: Write minimal implementation**

In `src/enrich/trees_first.py`, add `auth=False` to the `request_with_retry` call inside `fetch_metafiles`:

```python
    payload = _require_json(
        request_with_retry(
            client,
            "GET",
            f"{base_url}/{full_name}",
            auth=False,
            limiter=limiter,
            token_id=token_id,
            sleep=sleep,
            now=now,
            jitter=jitter,
            on_response=on_response,
        )
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_trees_first.py -q`
Expected: PASS — the new test and all existing `fetch_metafiles`/`fetch_tree` tests (which use clients without default auth headers) are green.

- [ ] **Step 5: Commit**

```bash
git add src/enrich/trees_first.py tests/unit/test_trees_first.py
git commit -m "fix: keep GitHub token off the metafiles host"
```

**Regression note (INV-1):** `fetch_metafiles` has no callers under `src/` (grep shows only its definition and tests), so no HTTP response, DB row, or UI output changes for normal use. The existing parsing tests at `tests/unit/test_trees_first.py:260-356` pin the returned `FilePresence`, and they stay untouched.

---

### Task 2: Thread-safe lazy engine/runner/executor singletons

**Files:**
- Modify: `src/serve/app.py:1-15` (add `import threading`, `from typing import cast`), `src/serve/app.py:284-313` (replace the `state` dict and the three closures)
- Test: create `tests/unit/test_app_singletons.py`

**Interfaces:**
- Consumes: `create_engine`, `make_runner`, `build_deps`, `RunExecutor`, `register_pages` (already imported in `app.py`).
- Produces:
  - `class _LazyLoaders` with `seed(key: str, value: object) -> None` and `get(key: str, factory: Callable[[], object]) -> object`; first `get` per key runs `factory` exactly once even under concurrent callers.
  - `create_app(...)` signature and all route behavior unchanged.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_app_singletons.py`:

```python
from __future__ import annotations

import threading
import time

from serve.app import _LazyLoaders, create_app


def test_lazy_loader_constructs_once_under_concurrency():
    loaders = _LazyLoaders()
    created: list[object] = []
    created_lock = threading.Lock()

    def factory() -> object:
        time.sleep(0.05)
        value = object()
        with created_lock:
            created.append(value)
        return value

    results: list[object] = []
    results_lock = threading.Lock()

    def worker() -> None:
        value = loaders.get("engine", factory)
        with results_lock:
            results.append(value)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert len(created) == 1
    assert results == [created[0]] * 8


def test_lazy_loader_reuses_a_seeded_value():
    loaders = _LazyLoaders()
    sentinel = object()
    loaders.seed("engine", sentinel)

    def factory() -> object:  # pragma: no cover - seeded values must not rebuild
        raise AssertionError("seeded values must not be rebuilt")

    assert loaders.get("engine", factory) is sentinel


def test_create_app_lazy_factories_are_single_flight(monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "serve.app.register_pages", lambda app, **kwargs: captured.update(kwargs)
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://gitcrawl@localhost/gitcrawl")
    engine_calls: list[str] = []
    monkeypatch.setattr(
        "serve.app.create_engine", lambda url: engine_calls.append(url) or object()
    )
    executor_calls: list[object] = []

    class StubExecutor:
        def __init__(self, engine, *, runs_root="runs"):
            executor_calls.append(engine)

    monkeypatch.setattr("serve.app.RunExecutor", StubExecutor)
    runner_calls: list[object] = []
    create_app(
        engine=None,
        runner_factory=lambda engine: runner_calls.append(engine)
        or (lambda _run_id, _spec: None),
    )

    engine_factory = captured["engine_factory"]
    runner_factory = captured["runner_factory"]
    executor_factory = captured["executor_factory"]

    engines = [engine_factory() for _ in range(2)]
    assert engines[0] is engines[1]
    assert engine_calls == ["postgresql://gitcrawl@localhost/gitcrawl"]

    runner = runner_factory()
    assert runner_factory() is runner
    assert len(runner_calls) == 1

    executor = executor_factory()
    assert executor_factory() is executor
    assert len(executor_calls) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_app_singletons.py -v`
Expected: FAIL — `ImportError: cannot import name '_LazyLoaders' from 'serve.app'`.

- [ ] **Step 3: Write minimal implementation**

In `src/serve/app.py`, extend the imports at the top of the file:

```python
import threading
from typing import cast
```

Replace `src/serve/app.py:284-313` (the `state` dict, `engine_for`, `runner_for`, `executor_for`) with:

```python
class _LazyLoaders:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._gates: dict[str, threading.Lock] = {}
        self._values: dict[str, object] = {}

    def seed(self, key: str, value: object) -> None:
        with self._lock:
            self._values[key] = value

    def get(self, key: str, factory: Callable[[], object]) -> object:
        with self._lock:
            if key in self._values:
                return self._values[key]
            gate = self._gates.setdefault(key, threading.Lock())
        with gate:
            with self._lock:
                if key in self._values:
                    return self._values[key]
            value = factory()
            with self._lock:
                self._values[key] = value
            return value


def create_app(
    *,
    engine: Engine | None = None,
    runner_factory: Callable[[Engine], Runner] | None = None,
    runs_root: str = "runs",
    clone_root: str = "clones",
    clock: Callable[[], float] = time.time,
    redis_ping: Callable[[], object] | None = None,
    token_present: Callable[[], bool] | None = None,
) -> FastAPI:
    loaders = _LazyLoaders()
    payload_cache = RunPayloadCache(ttl_seconds=CACHE_TTL_SECONDS, clock=clock)
    application = FastAPI(title="gitcrawl", version="0.0.1")

    def build_engine() -> Engine:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL is not configured")
        return create_engine(url)

    if engine is not None:
        loaders.seed("engine", engine)

    def engine_for() -> Engine:
        return cast(Engine, loaders.get("engine", build_engine))

    def runner_for() -> Runner:
        def build() -> Runner:
            if runner_factory is None:
                return make_runner(build_deps(engine_for()))
            return runner_factory(engine_for())

        return cast(Runner, loaders.get("runner", build))

    def executor_for() -> RunExecutor:
        def build() -> RunExecutor:
            return RunExecutor(engine_for(), runs_root=runs_root)

        return cast(RunExecutor, loaders.get("executor", build))
```

Keep the rest of `create_app` (routes and `register_pages` call) exactly as it is.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_app_singletons.py tests/contract -q`
Expected: PASS — the three new tests pass and every contract test that builds `create_app(engine=...)` (e.g. `tests/contract/test_run_detail.py:73-79`, `tests/contract/test_clone_endpoints.py:88-94`) behaves identically because a seeded loader returns the injected engine on first call.

- [ ] **Step 5: Commit**

```bash
git add src/serve/app.py tests/unit/test_app_singletons.py
git commit -m "fix: serialize lazy serve singleton construction"
```

**Regression note (INV-1):** Construction happens under a per-key gate, so exactly one engine/runner/executor is built even when first requests race; there is no losing duplicate to dispose. Single-threaded first-request behavior is unchanged: `engine_for` still reads `DATABASE_URL` only when no engine was injected, `runner_for` still builds through `runner_factory`/`make_runner(build_deps(...))`, and `executor_for` still builds `RunExecutor(engine, runs_root=...)`. No route code or response shape changes; the existing contract suites are the byte-level check.

---

### Task 3: Atomic clone claim and bounded clone registry

**Files:**
- Modify: `src/serve/runs.py:7` (add `OrderedDict` import), `src/serve/runs.py:189-200` (`CloneRegistry`), `src/serve/runs.py:325-360` (`start_clone`)
- Test: create `tests/unit/test_clone_registry.py`, create `tests/integration/test_clone_registry.py`

**Interfaces:**
- Consumes: `CloneProgress` / `CloneMode` / `_PersistedProgress` (`src/enrich/cloner.py:52-63`, `src/serve/runs.py:206-236`), `_count_run_items`, `_clone_worker`, `run_bundle_dir`, `PROGRESS_NAME`.
- Produces:
  - `CloneRegistry(*, max_entries: int = 100, ttl_seconds: float = 3600.0, clock: Callable[[], float] = time.monotonic)`.
  - `get(run_id: int) -> CloneProgress | None` (evicts eligible entries before returning, then refreshes the entry's idle clock).
  - `set(run_id: int, progress: CloneProgress) -> None`.
  - `claim(run_id: int, factory: Callable[[], CloneProgress]) -> tuple[CloneProgress, bool]`; concurrent callers for a running entry get the identical object with `claimed=False`.
  - `start_clone(...)` signature unchanged; returns the same `CloneProgress` object to concurrent callers and starts exactly one worker.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_clone_registry.py`:

```python
from __future__ import annotations

import threading
import time

from enrich.cloner import CloneProgress
from serve.runs import CloneRegistry


def running() -> CloneProgress:
    return CloneProgress(status="running", total=2, completed=0, failed=0)


def done() -> CloneProgress:
    return CloneProgress(status="done", total=2, completed=2, failed=0)


def test_claim_constructs_once_and_returns_the_same_object_to_racers():
    registry = CloneRegistry()
    created: list[CloneProgress] = []
    created_lock = threading.Lock()

    def factory() -> CloneProgress:
        time.sleep(0.05)
        progress = running()
        with created_lock:
            created.append(progress)
        return progress

    results: list[tuple[CloneProgress, bool]] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait(5)
        result = registry.claim(7, factory)
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert len(created) == 1
    assert [progress for progress, _ in results] == [created[0]] * 8
    assert [claimed for _, claimed in results].count(True) == 1


def test_claim_starts_a_new_entry_once_the_previous_one_is_terminal():
    registry = CloneRegistry()
    first, claimed = registry.claim(1, done)
    assert claimed is True
    second, claimed_again = registry.claim(1, running)
    assert claimed_again is True
    assert second is not first


def test_registry_evicts_terminal_entries_by_count_and_keeps_running_ones():
    registry = CloneRegistry(max_entries=2)
    live = running()
    registry.set(1, live)
    registry.set(2, done())
    registry.set(3, done())

    assert registry.get(1) is live
    assert registry.get(2) is None
    assert registry.get(3) is not None


def test_registry_evicts_terminal_entries_by_idle_age():
    now = {"value": 0.0}
    registry = CloneRegistry(ttl_seconds=10.0, clock=lambda: now["value"])
    registry.set(1, done())

    assert registry.get(1) is not None
    now["value"] = 11.0
    assert registry.get(1) is None
```

Create `tests/integration/test_clone_registry.py`:

```python
from __future__ import annotations

import threading

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

from enrich.cloner import CloneMode
from lib.gh_client import API_VERSION
from serve.executor import create_run
from serve.runs import CloneRegistry, progress_payload, read_clone_progress, start_clone

FILTER = {"gitcrawl_filter": 1, "q": "language:rust"}


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(alembic_engine: Engine) -> Engine:
    with alembic_engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE run_items, runs RESTART IDENTITY CASCADE"))
    return alembic_engine


def test_concurrent_starts_return_the_same_progress_object(clean: Engine, tmp_path, monkeypatch):
    run_id = create_run(clean, FILTER, api_version=API_VERSION)
    registry = CloneRegistry()
    release = threading.Event()
    calls: list[int] = []

    def fake_worker(engine, run_id, limit, mode, dest_root, git_runner, progress):
        calls.append(run_id)
        assert release.wait(10)

    monkeypatch.setattr("serve.runs._clone_worker", fake_worker)
    barrier = threading.Barrier(2)
    results: list[object] = []
    results_lock = threading.Lock()

    def start() -> None:
        barrier.wait(5)
        progress = start_clone(
            clean,
            run_id,
            limit=2,
            mode=CloneMode.SHALLOW,
            registry=registry,
            runs_root=str(tmp_path),
        )
        with results_lock:
            results.append(progress)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    release.set()

    assert len(results) == 2
    assert results[0] is results[1]
    assert calls == [run_id]


def test_evicted_terminal_entry_falls_back_to_its_identical_progress_file(clean, tmp_path):
    first = create_run(clean, FILTER, api_version=API_VERSION)
    second = create_run(clean, FILTER, api_version=API_VERSION)
    registry = CloneRegistry(max_entries=1)

    progress = start_clone(
        clean,
        first,
        limit=0,
        mode=CloneMode.SHALLOW,
        registry=registry,
        runs_root=str(tmp_path),
    )
    assert progress.status == "done"

    start_clone(
        clean,
        second,
        limit=0,
        mode=CloneMode.SHALLOW,
        registry=registry,
        runs_root=str(tmp_path),
    )

    assert registry.get(first) is None
    fallback = read_clone_progress(clean, first, registry=registry, runs_root=str(tmp_path))
    assert progress_payload(fallback) == progress_payload(progress)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_clone_registry.py -v`
Expected: FAIL — `AttributeError: 'CloneRegistry' object has no attribute 'claim'`.

Run: `pytest tests/integration/test_clone_registry.py -v` (with `TEST_DATABASE_URL` set)
Expected: FAIL — the concurrent test builds two `_PersistedProgress` objects and starts two workers (`results[0] is results[1]` false, `calls` length 2); the fallback test also cannot construct `CloneRegistry(max_entries=1)`.

- [ ] **Step 3: Write minimal implementation**

In `src/serve/runs.py`, change the import at line 7 to:

```python
from collections import OrderedDict
```

Replace `CloneRegistry` (`src/serve/runs.py:189-200`) with:

```python
class CloneRegistry:
    def __init__(
        self,
        *,
        max_entries: int = 100,
        ttl_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._entries: OrderedDict[int, CloneProgress] = OrderedDict()
        self._touched: dict[int, float] = {}
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._ttl = ttl_seconds
        self._clock = clock

    def _evict_locked(self, now: float) -> None:
        for run_id in list(self._entries):
            progress = self._entries[run_id]
            if progress.status == "running":
                continue
            if now - self._touched.get(run_id, now) >= self._ttl:
                del self._entries[run_id]
                self._touched.pop(run_id, None)
        while len(self._entries) > self._max_entries:
            evicted = False
            for run_id in list(self._entries):
                if self._entries[run_id].status != "running":
                    del self._entries[run_id]
                    self._touched.pop(run_id, None)
                    evicted = True
                    break
            if not evicted:
                break

    def get(self, run_id: int) -> CloneProgress | None:
        with self._lock:
            now = self._clock()
            self._evict_locked(now)
            progress = self._entries.get(run_id)
            if progress is None:
                return None
            self._entries.move_to_end(run_id)
            self._touched[run_id] = now
            return progress

    def set(self, run_id: int, progress: CloneProgress) -> None:
        with self._lock:
            now = self._clock()
            self._entries[run_id] = progress
            self._entries.move_to_end(run_id)
            self._touched[run_id] = now
            self._evict_locked(now)

    def claim(
        self, run_id: int, factory: Callable[[], CloneProgress]
    ) -> tuple[CloneProgress, bool]:
        with self._lock:
            now = self._clock()
            self._evict_locked(now)
            existing = self._entries.get(run_id)
            if existing is not None and existing.status == "running":
                self._entries.move_to_end(run_id)
                self._touched[run_id] = now
                return existing, False
            progress = factory()
            self._entries[run_id] = progress
            self._entries.move_to_end(run_id)
            self._touched[run_id] = now
            self._evict_locked(now)
            return progress, True
```

Replace the head of `start_clone` (`src/serve/runs.py:336-348`) with:

```python
    row = _row_for_run(engine, run_id)
    bundle_dir = run_bundle_dir(runs_root, row["filter_hash"], run_id)

    def create_progress() -> CloneProgress:
        return _PersistedProgress(
            bundle_dir / PROGRESS_NAME,
            status="running",
            total=0,
            completed=0,
            failed=0,
        )

    progress, claimed = registry.claim(run_id, create_progress)
    if not claimed:
        return progress
    progress.total = min(limit, _count_run_items(engine, run_id)) if limit > 0 else 0
```

Keep the rest of `start_clone` (`if limit <= 0: ...`, `worker = threading.Thread(...)`, `return progress`) exactly as it is.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_clone_registry.py tests/integration/test_clone_registry.py tests/contract/test_clone_endpoints.py tests/contract/test_run_detail.py -q`
Expected: PASS — claim identity, new-entry-after-terminal, both eviction rules, concurrent `start_clone` identity, evicted-fallback equality, and every existing clone endpoint test.

- [ ] **Step 5: Commit**

```bash
git add src/serve/runs.py tests/unit/test_clone_registry.py tests/integration/test_clone_registry.py
git commit -m "fix: atomic clone claim and bounded clone registry"
```

**Regression note (INV-1):** `read_clone_progress` still consults `registry.get(run_id)` first (`src/serve/runs.py:300-302`); with the production defaults (100 entries, 3600 s idle TTL) no contract-test app comes close to eviction, so every poll response is byte-identical. For an evicted terminal entry, `_clone_worker` force-emits before finishing (`src/serve/runs.py:382-388`) and the integration test asserts the persisted-file fallback payload equals the in-memory payload. Running entries are never evicted, so live progress is never degraded.

---

### Task 4: Shard poison recovery through the retry/DLQ path

**Files:**
- Modify: `src/discover/pipeline.py:27` (import `QueuedShard`), `src/discover/pipeline.py:246-311` (`process` and the queue consumer)
- Test: `tests/integration/test_pipeline.py` (append after line 356)

**Interfaces:**
- Consumes: `ShardStore.set_state` with the existing `ACTIVE -> PENDING` transition (`src/scheduler/state_machine.py:22-27`, `:126-156`); `ShardQueue.retry_or_dlq(stream_id, shard_id, *, attempts)` (`src/scheduler/state_machine.py:251-261`), which `XACK`s and `XADD`s a replacement (`attempts < max_attempts`) or appends to the DLQ.
- Produces: `process(shard_id: int, queued: QueuedShard | None = None) -> bool`; `True` means the queue message was moved to the retry/DLQ path and must not be acked. `run_search_discovery(...) -> DiscoveryStats` signature unchanged; on `RequestFailed` with no queue it still raises (after rolling the shard back).

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_pipeline.py`:

```python
def test_request_failed_shard_is_rolled_back_and_queued_for_retry(clean: Engine, redis):
    def handler(request: httpx.Request):
        if params_of(request).get("per_page") == ["1"]:
            return count_response(500)
        return httpx.Response(500, json={"message": "boom"})

    deps = make_deps(clean, scripted_client(handler, []), redis)
    stats = run_search_discovery(
        deps,
        "language:python",
        sleep=lambda _: None,
        now=lambda: 1000.0,
        jitter=lambda: 0.0,
    )

    assert stats.fetched == 0
    assert shard_state(clean, 1) == ("pending", False)
    claimed = ShardQueue(redis, lanes=4).claim("probe", count=10)
    assert [(item.shard_id, item.attempts) for item in claimed] == [(1, 1)]
    assert ShardQueue(redis, lanes=4).pel_size() == 1


def test_poison_shard_reaches_the_dlq_after_max_attempts(clean: Engine, redis):
    def handler(request: httpx.Request):
        if params_of(request).get("per_page") == ["1"]:
            return count_response(500)
        return httpx.Response(500, json={"message": "boom"})

    deps = make_deps(clean, scripted_client(handler, []), redis)
    for _ in range(3):
        run_search_discovery(
            deps,
            "language:python",
            sleep=lambda _: None,
            now=lambda 1000.0: 1000.0,
            jitter=lambda: 0.0,
        )

    assert redis.xlen("gitcrawl:shards:dlq") == 1
    entry = redis.xrange("gitcrawl:shards:dlq")[0]
    assert entry[1][b"shard_id"] == b"1"
    assert shard_state(clean, 1) == ("pending", False)
```

Note: fix the lambda in the loop to `now=lambda: 1000.0` when you write the file (the plan shows the exact call shape; no `now=lambda 1000.0: ...` typo is acceptable).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_pipeline.py::test_request_failed_shard_is_rolled_back_and_queued_for_retry tests/integration/test_pipeline.py::test_poison_shard_reaches_the_dlq_after_max_attempts -v`
Expected: FAIL — both tests error with `RequestFailed: 500: boom` escaping `run_search_discovery`, because `process` leaves the shard `ACTIVE` and never calls `retry_or_dlq`.

- [ ] **Step 3: Write minimal implementation**

In `src/discover/pipeline.py:27`, extend the import:

```python
from scheduler.state_machine import QueuedShard, ShardQueue, ShardRow, ShardState, ShardStore
```

Change the signature and add the `RequestFailed` handler in `process` (`src/discover/pipeline.py:246-298`):

```python
    def process(shard_id: int, queued: QueuedShard | None = None) -> bool:
        row = store.get(shard_id)
        if row.query is None:
            raise ValueError(f"shard {shard_id} has no query")
        store.set_state(shard_id, ShardState.ACTIVE)
        fetched = 0
        last_total = row.total_count
        narrowed = False
        incomplete = False
        try:
            for page in iter_shard_pages(
                deps.client,
                row.query,
                limiter=deps.limiter,
                token_id=deps.token_id,
                per_page=per_page,
                max_pages=max_pages,
                sleep=sleep,
                now=now,
                jitter=jitter,
                on_response=on_response,
            ):
                stats.pages += 1
                fetched += len(page.items)
                stats.fetched += len(page.items)
                last_total = page.total_count
                _fold(stats, upsert_repos(deps.engine, dedupe_items(page.items)))
                _collect_ids(seen_ids, collected_ids, page.items)
                if page.exhausted:
                    stats.page_capped_shards += 1
                    incomplete = True
                if page.incomplete:
                    if not narrowed and spawn_narrower(row):
                        narrowed = True
                    else:
                        incomplete = True
        except SearchCapExceeded:
            if spawn_narrower(row):
                stats.cap_splits += 1
                store.set_state(shard_id, ShardState.DONE, fetched=fetched, total_count=last_total)
                return False
            incomplete = True
        except RequestFailed:
            store.set_state(shard_id, ShardState.PENDING)
            if queued is None or queue is None:
                raise
            queue.retry_or_dlq(queued.stream_id, shard_id, attempts=queued.attempts + 1)
            return True
        if incomplete:
            stats.incomplete_shards += 1
            store.set_state(
                shard_id,
                ShardState.INCOMPLETE,
                fetched=fetched,
                total_count=last_total,
                incomplete=True,
            )
        else:
            store.set_state(shard_id, ShardState.DONE, fetched=fetched, total_count=last_total)
        return False
```

Replace the queue consumer (`src/discover/pipeline.py:304-311`) with:

```python
    else:
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

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_pipeline.py tests/unit/test_state_machine.py tests/integration/test_runner.py -q`
Expected: PASS — the two new tests, the existing `test_non_200_exhaustion_raises_request_failed` (no queue still raises after rollback), and every successful-shard pipeline/runner test.

- [ ] **Step 5: Commit**

```bash
git add src/discover/pipeline.py tests/integration/test_pipeline.py
git commit -m "fix: roll back and retry failed discovery shards"
```

**Regression note (INV-1):** successful shards take the same `ACTIVE -> DONE`/`INCOMPLETE` transitions, the same `queue.ack`, and the same stats folds as before (only the return value `False` was added). The no-redis path re-raises `RequestFailed` exactly as today, just after rolling the shard back to `PENDING`. Only failure handling changed, and one retry is attempted per run so a poison shard cannot monopolize a run; after `max_attempts` it lands in the existing DLQ.

---

### Task 5: Upsert input isolation for size, language, visibility, and boolean flags

**Files:**
- Modify: `src/store/upserts.py:65-168` (helpers plus `normalize_repo`)
- Test: `tests/integration/test_upserts.py` (unit tests after line 289, integration test after line 421)

**Interfaces:**
- Consumes: existing `normalize_repo(item: dict) -> dict | None` callers — `upsert_repos` (`src/store/upserts.py:330`) and `bootstrap_copy` (`:471`).
- Produces: `normalize_repo` keeps its signature. New private helpers `_coerce_optional_int`, `_optional_str`, `_optional_bool`, `_flag`, and sentinel `_INVALID`:
  - bad `size` (`bool`, `float("inf")`, non-numeric string, list, dict) → `normalize_repo` returns `None` (row skipped, counted in `UpsertStats.skipped`);
  - coercible `size` (`int`, `float`, numeric string) → `int`; missing/`None` → `None`;
  - non-str `language` → `None`;
  - non-str or empty `visibility` → `"private"` if `private` is truthy else `"public"`;
  - non-bool `fork`/`archived`/`disabled`/`is_template` → `False`; non-bool `has_*` → `None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_upserts.py` (after `test_normalize_repo_coerces_coercible_numeric_counts`, line 289):

```python
def test_normalize_repo_skips_a_non_numeric_size():
    assert normalize_repo(repo_item(1, size="not-a-number")) is None
    assert normalize_repo(repo_item(1, size=[1])) is None


def test_normalize_repo_normalizes_dirty_language_visibility_and_flags():
    row = normalize_repo(
        repo_item(
            1,
            language={"name": "Python"},
            visibility=7,
            has_wiki="yes",
            has_issues=1,
            archived="yes",
            fork="yes",
        )
    )
    assert row is not None
    assert row["language"] is None
    assert row["visibility"] == "public"
    assert row["has_wiki"] is None
    assert row["has_issues"] is None
    assert row["archived"] is False
    assert row["fork"] is False


def test_normalize_repo_keeps_private_fallback_for_a_dirty_visibility():
    row = normalize_repo(repo_item(1, private=True, visibility=7))
    assert row["visibility"] == "private"


def test_normalize_repo_keeps_coercible_numeric_sizes():
    assert normalize_repo(repo_item(1, size="2048"))["size_kb"] == 2048
    assert normalize_repo(repo_item(1, size=2048.0))["size_kb"] == 2048
```

Append after `test_dirty_page_rows_are_skipped_without_aborting_the_batch` (line 421):

```python
def test_upsert_repos_isolates_rows_that_would_abort_the_chunk(clean: Engine):
    stats = upsert_repos(
        clean,
        [
            repo_item(1),
            repo_item(2, size="not-a-number"),
            repo_item(3, has_wiki="yes", archived="not-a-bool", language=["Python"]),
        ],
    )
    assert stats == UpsertStats(inserted=2, skipped=1, history_rows=2)
    repos = {repo["id"]: repo for repo in dump_repos(clean)}
    assert sorted(repos) == [1, 3]
    assert repos[3]["has_wiki"] is None
    assert repos[3]["archived"] is False
    assert repos[3]["language"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_upserts.py -k "dirty_language or non_numeric_size or coercible_numeric_sizes or isolates_rows" -v`
Expected: FAIL — `test_normalize_repo_normalizes_dirty_language_visibility_and_flags` gets `{"name": "Python"}` back for `language` and `7` for `visibility`; `test_upsert_repos_isolates_rows_that_would_abort_the_chunk` raises a psycopg `ProgrammingError`/`DataError` (dict/str bound to `text`/`integer`/`boolean` columns), aborting the whole chunk.

- [ ] **Step 3: Write minimal implementation**

In `src/store/upserts.py`, add after `_valid_id` (line 95-96):

```python
_INVALID = object()


def _coerce_optional_int(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, bool):
        return _INVALID
    if isinstance(value, int):
        return value
    if isinstance(value, (float, str)):
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return _INVALID
    return _INVALID


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _flag(value: object) -> bool:
    return value if isinstance(value, bool) else False
```

In `normalize_repo`, replace the count/visibility/return block (`src/store/upserts.py:121-168`) with:

```python
    stargazers = _coerce_count(item.get("stargazers_count"))
    forks_count = _coerce_count(item.get("forks_count"))
    watchers = _coerce_count(item.get("watchers_count"))
    open_issues = _coerce_count(item.get("open_issues_count"))
    if stargazers is None or forks_count is None or watchers is None or open_issues is None:
        return None
    size_kb = _coerce_optional_int(item.get("size"))
    if size_kb is _INVALID:
        return None
    raw_visibility = item.get("visibility")
    if isinstance(raw_visibility, str) and raw_visibility:
        visibility = raw_visibility
    else:
        visibility = "private" if item.get("private") else "public"
    license_obj = item.get("license")
    parent = item.get("parent")
    source = item.get("source")
    return {
        "id": repo_id,
        "node_id": node_id,
        "full_name": full_name,
        "owner_id": owner_id,
        "name": item.get("name") or _last_segment(full_name),
        "description": item.get("description"),
        "homepage": item.get("homepage"),
        "language": _optional_str(item.get("language")),
        "license_spdx": license_obj.get("spdx_id") if isinstance(license_obj, dict) else None,
        "topics": topics,
        "visibility": visibility,
        "fork": _flag(item.get("fork")),
        "parent_full_name": parent.get("full_name") if isinstance(parent, dict) else None,
        "source_full_name": source.get("full_name") if isinstance(source, dict) else None,
        "archived": _flag(item.get("archived")),
        "disabled": _flag(item.get("disabled")),
        "mirror_url": item.get("mirror_url"),
        "is_template": _flag(item.get("is_template")),
        "size_kb": size_kb,
        "stargazers": stargazers,
        "forks_count": forks_count,
        "watchers": watchers,
        "open_issues": open_issues,
        "default_branch": item.get("default_branch"),
        "has_wiki": _optional_bool(item.get("has_wiki")),
        "has_issues": _optional_bool(item.get("has_issues")),
        "has_projects": _optional_bool(item.get("has_projects")),
        "has_pages": _optional_bool(item.get("has_pages")),
        "has_discussions": _optional_bool(item.get("has_discussions")),
        "has_pull_requests": _optional_bool(item.get("has_pull_requests")),
        "custom_properties": dict(item.get("custom_properties") or {}),
        "created_at": _parse_timestamp(item.get("created_at")),
        "pushed_at": _parse_timestamp(item.get("pushed_at")),
        "updated_at": _parse_timestamp(item.get("updated_at")),
        "owner_login": owner_login,
        "owner_type": owner.get("type") or "User",
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_upserts.py tests/integration/test_pipeline.py tests/integration/test_lifecycle.py -q`
Expected: PASS — the new coercion/isolation tests plus every existing normalize/upsert/bootstrap test. In particular `test_normalize_repo_maps_a_full_object` (pins all well-formed outputs), `test_normalize_repo_applies_defaults_for_a_minimal_object` (`size_kb is None` preserved), and `test_bootstrap_copy_rejects_a_dirty_row_without_aborting` (still `skipped == 1`) stay green.

- [ ] **Step 5: Commit**

```bash
git add src/store/upserts.py tests/integration/test_upserts.py
git commit -m "fix: isolate dirty rows from chunked repo upserts"
```

**Regression note (INV-1):** for every well-formed GitHub payload (`size: int`, `language: str|None`, `visibility: "public"|"private"|"internal"`, real booleans) the produced normalized dict is unchanged; `tests/integration/test_upserts.py:148-192` asserts that field-by-field and `:318-325` asserts identical reruns still write nothing. Only malformed values are affected: bad `size` rows are now skipped like bad counts/topics, and the remaining variants are normalized instead of hitting bind parameters.

---

### Task 6: Surface GraphQL partials instead of swallowing them

**Files:**
- Modify: `src/enrich/graphql_batch.py:1-11` (add `logging` and a module logger), `:182-235` (`_fetch`), `:238-269` (`fetch_graphql_batch`)
- Test: `tests/unit/test_graphql_batch.py` (add `import logging` at the top, append tests after line 269)

**Interfaces:**
- Consumes: `parse_batch_response`, `_error_messages`, `_is_split_worthy`, `RequestFailed` (all unchanged).
- Produces:
  - `@dataclass(frozen=True) class BatchFetch` with `results: dict[int, RepoGraphQL]`, `incomplete: bool = False`, `errors: tuple[str, ...] = ()`.
  - `_fetch(...) -> BatchFetch` (was `dict[int, RepoGraphQL]`); raises the last `RequestFailed` only when no partial results survived.
  - `fetch_graphql_batch(...) -> dict[int, RepoGraphQL]` unchanged; logs a warning when `BatchFetch.incomplete` is true.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_graphql_batch.py`, add `import logging` next to `import json` (line 3), then append:

```python
def test_partial_batches_surface_an_incompleteness_signal():
    from enrich.graphql_batch import _fetch

    def handler(request):
        query = json.loads(request.content)["query"]
        ids = [int(match) for match in re.findall(r"c(\d+): node", query)]
        if ids == [1, 2]:
            return httpx.Response(200, json={"data": None, "errors": [{"message": "timeout"}]})
        if ids == [1]:
            return httpx.Response(
                200, json={"data": None, "errors": [{"message": "Could not resolve to a node"}]}
            )
        return httpx.Response(200, json=payload_for(ids))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    batch = _fetch(
        client,
        token="tok",
        repo_ids=[1, 2],
        node_ids=["NODE_1", "NODE_2"],
        graphql_url=GRAPHQL_URL,
        max_aliases=20,
        limiter=None,
        sleep=lambda _: None,
        now=lambda: 1000.0,
        jitter=None,
        on_response=None,
        depth=0,
    )

    assert set(batch.results) == {2}
    assert batch.incomplete is True
    assert any("Could not resolve" in error for error in batch.errors)


def test_fetch_graphql_batch_logs_incomplete_partials(caplog):
    def handler(request):
        query = json.loads(request.content)["query"]
        ids = [int(match) for match in re.findall(r"c(\d+): node", query)]
        if ids == [1, 2]:
            return httpx.Response(200, json={"data": None, "errors": [{"message": "timeout"}]})
        if ids == [1]:
            return httpx.Response(
                200, json={"data": None, "errors": [{"message": "Could not resolve to a node"}]}
            )
        return httpx.Response(200, json=payload_for(ids))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.WARNING, logger="enrich.graphql_batch"):
        result = fetch_graphql_batch(
            client, token="tok", repo_ids=[1, 2], node_ids=["NODE_1", "NODE_2"]
        )

    assert set(result) == {2}
    assert any("incomplete" in record.getMessage() for record in caplog.records)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_graphql_batch.py -k "partial_batches_surface or logs_incomplete" -v`
Expected: FAIL — `test_partial_batches_surface_an_incompleteness_signal` gets a `dict` back and errors with `AttributeError: 'dict' object has no attribute 'results'`; the logging test sees no warning records.

- [ ] **Step 3: Write minimal implementation**

In `src/enrich/graphql_batch.py`, add at the top:

```python
import logging
```

and after the imports:

```python
logger = logging.getLogger(__name__)
```

Add the dataclass after `RepoGraphQL` (line 33):

```python
@dataclass(frozen=True)
class BatchFetch:
    results: dict[int, RepoGraphQL]
    incomplete: bool = False
    errors: tuple[str, ...] = ()
```

Replace `_fetch` (`src/enrich/graphql_batch.py:182-235`) with:

```python
def _fetch(
    client: httpx.Client,
    *,
    token: str,
    repo_ids: list[int],
    node_ids: list[str],
    graphql_url: str,
    max_aliases: int,
    limiter: BucketLimiter | None,
    sleep: Callable[[float], None],
    now: Callable[[], float],
    jitter: Callable[[], float] | None,
    on_response: Callable[[httpx.Response, float], None] | None,
    depth: int,
) -> BatchFetch:
    merged: dict[int, RepoGraphQL] = {}
    errors: list[str] = []
    stack: list[tuple[list[int], list[str], int]] = [(repo_ids, node_ids, depth)]
    last_error: RequestFailed | None = None
    while stack:
        ids, nodes, current_depth = stack.pop()
        try:
            payload = _post_query(
                client,
                token=token,
                repo_ids=ids,
                node_ids=nodes,
                graphql_url=graphql_url,
                max_aliases=max_aliases,
                limiter=limiter,
                sleep=sleep,
                now=now,
                jitter=jitter,
                on_response=on_response,
            )
        except RequestFailed as exc:
            last_error = exc
            errors.append(f"{exc.status}: {exc.message}")
            if len(ids) > 1 and current_depth < _MAX_SPLIT_DEPTH:
                middle = len(ids) // 2
                stack.append((ids[middle:], nodes[middle:], current_depth + 1))
                stack.append((ids[:middle], nodes[:middle], current_depth + 1))
            continue
        messages = _error_messages(payload)
        if not messages:
            merged.update(parse_batch_response(payload))
            continue
        if _is_split_worthy(messages) and len(ids) > 1 and current_depth < _MAX_SPLIT_DEPTH:
            middle = len(ids) // 2
            stack.append((ids[middle:], nodes[middle:], current_depth + 1))
            stack.append((ids[:middle], nodes[:middle], current_depth + 1))
            continue
        last_error = RequestFailed(200, messages[0])
        errors.extend(messages)
    if not merged and last_error is not None:
        raise last_error
    return BatchFetch(results=merged, incomplete=bool(errors), errors=tuple(errors))
```

Replace the return in `fetch_graphql_batch` (`src/enrich/graphql_batch.py:256-269`) with:

```python
    batch = _fetch(
        client,
        token=token,
        repo_ids=list(repo_ids),
        node_ids=list(node_ids),
        graphql_url=graphql_url,
        max_aliases=max_aliases,
        limiter=limiter,
        sleep=sleep,
        now=now,
        jitter=jitter,
        on_response=on_response,
        depth=0,
    )
    if batch.incomplete:
        logger.warning(
            "graphql batch incomplete: %d error(s), %d result(s); first: %s",
            len(batch.errors),
            len(batch.results),
            batch.errors[0],
        )
    return batch.results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_graphql_batch.py -q`
Expected: PASS — the two new tests plus every existing test, including `test_fetch_graphql_batch_depth_cap_stops_recursion_with_request_failed` (no surviving partials still raises) and `test_split_returns_partial_results_and_retries_only_failures` (public result shape unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/enrich/graphql_batch.py tests/unit/test_graphql_batch.py
git commit -m "fix: surface incomplete GraphQL sub-batches"
```

**Regression note (INV-1):** `fetch_graphql_batch` still returns the exact same `dict[int, RepoGraphQL]` for every success and still raises for total failure; only the internal `_fetch` return value (`BatchFetch`) and a warning log line are new. The audit lists `graphql_batch` as unwired/quarantined (spec §5 item 9), so no runtime flow observes either change.

---

### Task 7: Chunk `GeoCache.put_many` binds

**Files:**
- Modify: `src/enrich/geo_resolver.py:795-820`
- Test: `tests/integration/test_geo_resolver.py` (append after line 292)

**Interfaces:**
- Consumes: `chunked(values: Sequence[T], size: int) -> Iterator[Sequence[T]]` (`src/lib/batching.py:6-10`), already imported in `geo_resolver.py:14`; the upsert body and semantics of `put`.
- Produces: `put_many(results: Mapping[str, GeoResult], *, batch_size: int = 1000) -> None`; one multi-row `INSERT ... ON CONFLICT` statement per chunk; results and `hits` semantics identical.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_geo_resolver.py`:

```python
def test_geo_cache_put_many_chunks_statements(clean: Engine):
    statements: list[str] = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if "INTO geo_cache" in statement.upper():
            statements.append(statement)

    cache = GeoCache(clean)
    results = {
        f"city-{index}": GeoResult("US", "gazetteer-city", f"City {index}") for index in range(5)
    }
    sa.event.listen(clean, "before_cursor_execute", listener)
    try:
        cache.put_many(results, batch_size=2)
    finally:
        sa.event.remove(clean, "before_cursor_execute", listener)

    assert len(statements) == 3
    found = cache.get_many(list(results))
    assert {key: result.country_iso for key, result in found.items()} == {
        key: "US" for key in results
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_geo_resolver.py::test_geo_cache_put_many_chunks_statements -v`
Expected: FAIL — `TypeError: GeoCache.put_many() got an unexpected keyword argument 'batch_size'`.

- [ ] **Step 3: Write minimal implementation**

Replace `put_many` (`src/enrich/geo_resolver.py:795-820`) with:

```python
    def put_many(
        self, results: Mapping[str, GeoResult], *, batch_size: int = 1000
    ) -> None:
        if not results:
            return
        for batch in chunked(list(results.items()), batch_size):
            values = [
                {
                    "normalized": key,
                    "country_iso": result.country_iso,
                    "confidence": result.confidence,
                    "raw_sample": result.raw_location,
                    "hits": 1,
                }
                for key, result in batch
            ]
            base = pg_insert(GeoCacheRow).values(values)
            statement = base.on_conflict_do_update(
                index_elements=[GeoCacheRow.normalized],
                set_={
                    "country_iso": base.excluded.country_iso,
                    "confidence": base.excluded.confidence,
                    "raw_sample": base.excluded.raw_sample,
                    "hits": GeoCacheRow.hits + 1,
                    "updated_at": sa.func.now(),
                },
            )
            with self._engine.begin() as connection:
                connection.execute(statement)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_geo_resolver.py -q`
Expected: PASS — the chunking test plus `test_geo_cache_bulk_roundtrip` (2 rows, default single batch, `hits == 2`) and all other geo tests.

- [ ] **Step 5: Commit**

```bash
git add src/enrich/geo_resolver.py tests/integration/test_geo_resolver.py
git commit -m "fix: chunk geo cache bulk writes"
```

**Regression note (INV-1):** with the default `batch_size=1000`, console enrichment touches at most `max_enrich=100` owners per run (`src/serve/runner.py:45-50`), so `put_many` still issues exactly one statement with the same rows and the same `hits = hits + 1` conflict update. Only >1000-key writes are split, which today would hit the PostgreSQL bind limit.

---

### Task 8 (GATED — E1 only): Correct run summary counters

> **GATE: do not execute this task unless the E1 checkbox in
> `docs/superpowers/specs/2026-10-01-quality-hardening-design.md` §11 is checked and
> signed off.** If it is not checked, skip this task entirely and report it as blocked.

**Files:**
- Modify: `src/serve/executor.py:36-43` (`RunPayload`), `src/serve/executor.py:207-221` (`execute_run` values)
- Modify: `src/serve/runner.py:585-592` (`_run_filter` return)
- Modify: `src/serve/templates/partials/status.html:7-13` (counts row)
- Test: `tests/integration/test_executor.py` (append after line 394), `tests/integration/test_runner.py` (append after line 833), `tests/contract/test_run_detail.py` (append after line 221)

**Before/after visible change:** today a real run always shows `updated 0` and `skipped 0` (and no `unchanged` counter at all) on `/runs/{id}` and `/partials/runs/{id}/status`, even when discovery updated or left rows unchanged. After this task, the run page shows the discovery upsert counts it actually produced, and a new `unchanged` readout appears between `updated` and `skipped`. Nothing else moves: `inserted` keeps its current meaning (`len(payload.items)`), all JSON/API shapes are unchanged (`run_status` already exposes these keys), and runs seeded directly in tests still show zeros.

**Interfaces:**
- Consumes: `DiscoveryStats` counters (`src/discover/pipeline.py:46-59`) already computed by `_run_filter` at `src/serve/runner.py:519-525`.
- Produces:
  - `RunPayload` gains `updated: int | None = None`, `unchanged: int | None = None`, `skipped: int | None = None` appended after `field_stats` (positional constructions like `RunPayload(1, [_item()])` keep working).
  - `execute_run` persists `payload.updated/unchanged/skipped or 0`; `inserted` unchanged.
  - `_run_filter` populates the three fields from `stats`.
  - `partials/status.html` renders one additional `data-count="unchanged"` span.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_executor.py`:

```python
def test_execute_run_persists_runner_upsert_counters(db: Engine, tmp_path):
    run_id = create_run(db, FILTER, api_version="v1")
    payload = RunPayload(
        total_count=3,
        fetched=3,
        items=[_item()],
        updated=2,
        unchanged=3,
        skipped=4,
    )
    execute_run(db, run_id, runner=lambda rid, spec: payload, runs_root=str(tmp_path))
    status = run_status(db, run_id)
    assert status["inserted"] == 1
    assert status["updated"] == 2
    assert status["unchanged"] == 3
    assert status["skipped"] == 4
```

Append to `tests/integration/test_runner.py`:

```python
def test_run_filter_reports_discovery_upsert_counters(clean: Engine, monkeypatch):
    from discover.pipeline import DiscoveryStats

    stats = DiscoveryStats(
        shards=1,
        pages=1,
        fetched=2,
        inserted=1,
        updated=2,
        unchanged=3,
        skipped=4,
    )
    monkeypatch.setattr(runner_module.pipeline, "count_total", lambda *args, **kwargs: 10)
    monkeypatch.setattr(
        runner_module.pipeline, "run_search_discovery", lambda *args, **kwargs: stats
    )
    client, _ = scripted(lambda request: httpx.Response(500))
    payload = run_filter(make_deps(clean, client), spec_for(q="language:python"))

    assert payload.updated == 2
    assert payload.unchanged == 3
    assert payload.skipped == 4
```

Append to `tests/contract/test_run_detail.py`:

```python
def test_run_page_reports_updated_unchanged_and_skipped(clean: Engine, tmp_path):
    run_id = seed_run(clean, tmp_path)
    with clean.begin() as connection:
        connection.execute(
            text("UPDATE runs SET updated = 2, unchanged = 3, skipped = 4 WHERE id = :id"),
            {"id": run_id},
        )
    client = make_client(clean, tmp_path)

    html = client.get(f"/runs/{run_id}").text

    assert 'data-count="updated">updated <strong>2</strong>' in html
    assert 'data-count="unchanged">unchanged <strong>3</strong>' in html
    assert 'data-count="skipped">skipped <strong>4</strong>' in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_executor.py::test_execute_run_persists_runner_upsert_counters tests/integration/test_runner.py::test_run_filter_reports_discovery_upsert_counters tests/contract/test_run_detail.py::test_run_page_reports_updated_unchanged_and_skipped -v`
Expected: FAIL — `TypeError: RunPayload.__init__() got an unexpected keyword argument 'updated'` in the first two; the contract test cannot find `data-count="unchanged"` in the HTML.

- [ ] **Step 3: Write minimal implementation**

In `src/serve/executor.py`, append the fields to `RunPayload` (`src/serve/executor.py:36-43`):

```python
@dataclass
class RunPayload:
    total_count: int | None
    items: list[RunPayloadItem]
    incomplete: bool = False
    fetched: int | None = None
    warnings: list[str] = field(default_factory=list)
    field_stats: dict = field(default_factory=dict)
    updated: int | None = None
    unchanged: int | None = None
    skipped: int | None = None
```

In `execute_run`, replace the `.values(...)` counters block (`src/serve/executor.py:207-221`):

```python
            connection.execute(
                update(Runs)
                .where(Runs.id == run_id)
                .values(
                    status="partial" if payload.incomplete else "done",
                    total_count=payload.total_count,
                    fetched=payload.fetched if payload.fetched is not None else len(payload.items),
                    inserted=len(payload.items),
                    updated=payload.updated or 0,
                    unchanged=payload.unchanged or 0,
                    skipped=payload.skipped or 0,
                    incomplete_shards=1 if payload.incomplete else 0,
                    bundle_dir=bundle_dir,
                    finished_at=func.now(),
                    error=None,
                )
            )
```

In `src/serve/runner.py`, replace the final `return RunPayload(...)` (`src/serve/runner.py:585-592`):

```python
    return RunPayload(
        total_count=total_count,
        fetched=stats.fetched,
        incomplete=bool(warnings) or bool(segment_stats.warnings) or stats.incomplete_shards > 0,
        warnings=warnings,
        items=items,
        field_stats=asdict(segment_stats),
        updated=stats.updated,
        unchanged=stats.unchanged,
        skipped=stats.skipped,
    )
```

In `src/serve/templates/partials/status.html`, insert the unchanged counter between the `updated` and `skipped` spans (`src/serve/templates/partials/status.html:9-11`):

```html
    <span class="count" data-count="inserted">inserted <strong>{{ run.inserted }}</strong></span>
    <span class="count" data-count="updated">updated <strong>{{ run.updated }}</strong></span>
    <span class="count" data-count="unchanged">unchanged <strong>{{ run.unchanged }}</strong></span>
    <span class="count" data-count="skipped">skipped <strong>{{ run.skipped }}</strong></span>
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_executor.py tests/integration/test_runner.py tests/contract/test_run_detail.py tests/contract/test_console_pages.py -q`
Expected: PASS — the three new tests plus every existing run/executor/runner/console test (including `test_run_page_reports_counts_and_export_links`, which checks fetched/inserted/incomplete only). `src/serve/pages.py:377-388` already reads `row["updated"]`, `row["unchanged"]`, `row["skipped"]`, so no page-code change is needed.

- [ ] **Step 5: Commit**

```bash
git add src/serve/executor.py src/serve/runner.py src/serve/templates/partials/status.html tests/integration/test_executor.py tests/integration/test_runner.py tests/contract/test_run_detail.py
git commit -m "fix: persist runner upsert counters in run summaries (E1)"
```

**Regression note (INV-1/E1):** this is the single approved visible change from spec §11 (E1) and must not ship before sign-off. The HTML change is one inserted `<span>` in `#run-counts`, which is additive (no existing control moves or changes). Runs executed through `execute_run` with a `RunPayload` that omits the new fields (every existing test and any external fixture) still persist zeros exactly as today because the fields default to `None`. `run_status` keys are unchanged.

---

## Spec Coverage

| Spec §5 item | Task(s) | Proof |
|---|---|---|
| 1. Token leak (`fetch_metafiles` sends `Authorization`) | Task 1 | Unit test asserts no `Authorization` on the mock request; function unwired |
| 2. Singleton init races (`app.py:284-313`) | Task 2 | `_LazyLoaders` concurrency test + `create_app` wiring test; contract suites unchanged |
| 3. Clone guard atomicity | Task 3 | `CloneRegistry.claim` racer identity test + concurrent `start_clone` integration test |
| 4. Clone registry bound | Task 3 | Count/idle-age eviction unit tests + evicted-fallback equality integration test |
| 5. Shard poison recovery (`pipeline.py:246-311`) | Task 4 | Rollback-to-`PENDING` + retry + DLQ integration tests; no-queue path still raises |
| 6. Upsert input isolation (`upserts.py:130-161`) | Task 5 | Normalize coercion unit tests + chunk-abort integration test |
| 7. GraphQL partial surfacing (`graphql_batch.py:216-234`) | Task 6 | `BatchFetch` signal test + warning-log test; existing all-failure/success tests |
| 12. Geo chunking (`geo_resolver.py:795-820`) | Task 7 | Per-statement spy test; bulk roundtrip unchanged |
| 14 / E1. Run summary counters (`executor.py:208-221`) | Task 8 (gated) | Counter persistence + runner population + status.html readout tests |

Out of scope for this plan (covered by other Phase 1 plans): §5 items 8 (layering), 9 (dead-surface quarantine), 10 (types), 11 (test infrastructure), 13 (formatting).

## Risks and Known Unknowns

- **Task 2 interpretation.** The spec's wording "dispose losing duplicates" anticipates a create-outside-the-lock design. This plan instead constructs under a per-key lock, so a losing duplicate can never exist; there is nothing to dispose. This is strictly stronger for `RunExecutor`, whose constructor would otherwise leak a worker thread on a lost race. If a reviewer requires explicit disposal, the alternative is candidate-then-publish with `candidate.dispose()` for the engine and a new `RunExecutor.close()`; that addition is not planned here.
- **Task 3 eviction race window.** A `CloneProgress` becomes terminal `done` inside `clone_repos` (`src/enrich/cloner.py:266`) immediately before the forced `emit` (`:267`), and `_clone_worker` forces another emit in `finally` (`src/serve/runs.py:387-388`). Eviction triggered in that sub-millisecond window could expose one stale poll before the file catches up. Defaults (100 entries, 3600 s idle TTL) make this unreachable in normal use; the evicted-fallback test proves the steady-state equality.
- **Task 4 retry cadence.** This plan attempts at most one retry per `run_search_discovery` call (the consumer breaks after `retry_or_dlq`), so a persistent failure needs `max_attempts` runs before reaching the DLQ. If immediate same-run retries are preferred, remove the `break` and let the `while` loop re-claim; tests would then need to expect DLQ after one run.
- **Task 5 field scope.** The user-specified fields are hardened; other raw values (`description`, `homepage`, `default_branch`, `mirror_url`, `custom_properties`, `name`) can still theoretically carry a hostile type from a compromised upstream and abort a chunk (`custom_properties` is `dict(...)`-coerced and can raise on non-mapping iterables). This is left out to keep normal-use behavior pinned; a follow-up could apply the same pattern.
- **Task 6 public return.** `fetch_graphql_batch` deliberately keeps returning `dict[int, RepoGraphQL]`; the new `BatchFetch` type is only visible on the internal `_fetch`. Any future caller that needs the signal must call `_fetch` or promote the type.
- **Task 8 semantics.** `skipped` is the discovery upsert skip count from `DiscoveryStats`, not the enrichment skip count (`skipped["geo"]`/`skipped["dockerfile"]` in `runner.py:557-577`), and `inserted` keeps its existing `len(payload.items)` meaning. That matches E1's named counters but is a decision worth confirming during E1 sign-off.
- **Unverified without a database.** Tasks 3, 4, 5, 7, and 8 have integration tests that require `TEST_DATABASE_URL`; their exact failure modes in Step 2 were reasoned from the code, not executed in this planning session. The unit-level tests (Tasks 1, 2, 3-unit, 6) are runnable without a database.
