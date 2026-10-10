# Transport and Pipeline Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove avoidable serialization, parsing, and database round trips from the network hot paths: pin the shared httpx pool, reuse the audit hook's parsed JSON, buffer discovery upserts per worker, and move audit INSERTs onto a bounded writer thread.

**Architecture:** Four independent changes behind existing interfaces. (1) `lib.gh_client.create_client` gains explicit `httpx.Limits(max_connections=64, max_keepalive_connections=64, keepalive_expiry=60.0)` and `trust_env=False`; HTTP/2 stays off (the `h2` package is not installed — deliberate non-goal, server processing dominates). (2) `lib.graphql_batch._post` reads `audit.cached_json(response)` before falling back to `response.json()`, matching `discover/graphql_search.py:255-260`. (3) `discover.pipeline._Worker` accumulates each page's items in a bounded per-shard buffer and calls `upsert_repos` only every N pages or M items (and at shard exit), keeping dedupe, `_collect_ids`/`_fold`, and shard-state semantics. (4) `lib.audit.AuditBuffer` hands record batches to a daemon writer thread behind a bounded queue; `flush()` stays a synchronous barrier, so runner.py's final flush guarantee is unchanged.

**Tech Stack:** Python 3.12; httpx 0.28.1 (`httpx.Client`, `httpx.MockTransport` in tests); SQLAlchemy 2.1 + psycopg; Redis/fakeredis; pytest + pytest-cov; ruff/black/mypy.

**Spec:** `design/runtime-audit-and-adaptive-control.md`

## Global Constraints

- Python 3.12; `httpx==0.28.1` pinned in `requirements.txt`; **no new runtime dependencies** — in particular, do **not** add `h2` or enable `http2=True` (spec §3.1 non-goal: server-side latency is ~5.1 s/call vs 0.2–0.6 s network).
- Scope is exactly spec §3.1 (pool leaks), §3.3 (double parse; discovery inline upsert), and recommendation 7 of §8 (both optional cleanups). Batch size defaults, concurrency caps, limiter window semantics, and any adaptive controller are owned by other plans — do not touch them.
- No schema changes, no Alembic migrations, no new settings in this plan.
- Lint: `.venv\Scripts\python.exe -m ruff check src tests` (rules E, F, I, UP, B; line length 100). Format: `.venv\Scripts\python.exe -m black --check src tests`. Types: `.venv\Scripts\python.exe -m mypy` (gated packages `src/lib`, `src/limiter`, `src/store`, `src/scheduler` per `pyproject.toml:21-23`).
- Tests: `.venv\Scripts\python.exe -m pytest`; coverage gate `fail_under = 93` via `-q --cov=src --cov-report=term-missing`. Integration tests need `TEST_DATABASE_URL` pointing at a database whose name ends in `_test` (`tests/conftest.py:20-24`); CI sets `GITCRAWL_REQUIRE_TEST_DB=1`.
- Windows dev host (PowerShell 5.1); commands follow the sample plan's style.
- `trust_env=False` is a deliberate behavior change: this repo has no proxy support (no `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` handling anywhere in `src`), so environment proxy auto-discovery is pinned off.
- `run_search_discovery` gains keyword-only parameters with defaults; every existing call site keeps working unchanged.
- Existing audit tests in `tests/integration/test_audit_db.py` stay green; the single test whose observation point is inherently asynchronous (`test_audit_buffer_batches_inserts`) is explicitly updated in Task 4 to await the writer, and its original assertion (10 records -> exactly 1 INSERT) is preserved.
- Do not commit secrets; no raw token in logs or tests.

---

### Task 1: Pin the httpx connection pool and disable environment proxy discovery

The audit (spec §3.1) found the keep-alive pool defaults (20 idle connections, 5 s expiry) are smaller than discovery's 32 workers, forcing fresh TCP+TLS handshakes after every DB-gap idle period. The transport itself stays HTTP/1.1.

**Files:**
- Modify: `src/lib/gh_client.py:51-52` (add `CLIENT_POOL_LIMITS` above `create_client`, extend the constructor call)
- Test: `tests/unit/test_gh_client.py` (append after `test_create_client_honors_custom_timeout`, line 157)

**Interfaces:**
- Produces: `lib.gh_client.CLIENT_POOL_LIMITS: httpx.Limits` with `max_connections == 64`, `max_keepalive_connections == 64`, `keepalive_expiry == 60.0`.
- Produces: `create_client(token: str | None = None, *, timeout: float = 30.0) -> httpx.Client` now constructed with `limits=CLIENT_POOL_LIMITS` and `trust_env=False`; no `http2` argument is passed.
- Consumes: `httpx.Limits`, `build_headers(token)` (unchanged).

- [ ] **Step 1: Write the failing test**

```python
def test_create_client_pins_the_pool_and_ignores_environment_proxies(monkeypatch):
    captured = {}
    real_client = httpx.Client

    def factory(**kwargs):
        captured.update(kwargs)
        kwargs["transport"] = httpx.MockTransport(lambda request: httpx.Response(200))
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    client = create_client("ghp_example-token")
    client.close()

    limits = captured["limits"]
    assert isinstance(limits, httpx.Limits)
    assert limits.max_connections == 64
    assert limits.max_keepalive_connections == 64
    assert limits.keepalive_expiry == 60.0
    assert captured["trust_env"] is False
    assert "http2" not in captured
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_gh_client.py::test_create_client_pins_the_pool_and_ignores_environment_proxies -q`
Expected: FAIL — `KeyError: 'limits'`.

- [ ] **Step 3: Write minimal implementation**

Replace lines `src/lib/gh_client.py:51-52` with:

```python
CLIENT_POOL_LIMITS = httpx.Limits(
    max_connections=64,
    max_keepalive_connections=64,
    keepalive_expiry=60.0,
)


def create_client(token: str | None = None, *, timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        headers=build_headers(token),
        timeout=timeout,
        limits=CLIENT_POOL_LIMITS,
        trust_env=False,
    )
```

Why 64: discovery runs 32 workers plus probe batches; the old default of 20 idle connections with a 5 s expiry dropped surplus connections and re-handshook after every DB pause (spec §3.1). `trust_env=False` keeps httpx from silently reading `HTTP_PROXY`/`NO_PROXY`/`.netrc`; no proxy path exists in this repo. Session/TLS reuse intent is unchanged — `build_deps` already creates exactly one shared client per run (`src/serve/runner.py:127`, asserted by `tests/integration/test_runner.py:1118`), and these limits let that client keep its connections warm.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_gh_client.py -q`
Expected: PASS (all 40+ client tests, including the four pre-existing `create_client` tests that forward kwargs into the real client).

- [ ] **Step 5: Lint/format/types then commit**

```bash
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m black --check src tests
.venv\Scripts\python.exe -m mypy
git add src/lib/gh_client.py tests/unit/test_gh_client.py
git commit -m "perf(lib): pin httpx pool limits and ignore environment proxies"
```

### Task 2: Read GraphQL batch bodies from the audit cache instead of re-parsing

The audit hook already parses every response body once and stores it on `response.extensions["gitcrawl.json"]` (`src/lib/audit.py:99-104`, read back by `cached_json` at `:83-84`). Discovery already exploits that (`src/discover/graphql_search.py:255-260`); the batch engine still calls `response.json()` again (`src/lib/graphql_batch.py:157-160`). This task makes `_post` follow the discovery pattern.

**Files:**
- Modify: `src/lib/graphql_batch.py:13` (import `audit`), `src/lib/graphql_batch.py:157-160` (parse block in `_post`)
- Test: `tests/unit/test_graphql_batch_core.py` (append after `test_fetch_batch_runs_chunks_concurrently`, line 301)

**Interfaces:**
- Produces: `lib.graphql_batch._post` keeps its exact signature `(adapter, keys, *, client, limiter, token_id, on_response, sleep, now, jitter) -> tuple[ParsedBatch, tuple[str, ...]]`; body parsing now reads `lib.audit.cached_json(response)` first and only falls back to `response.json()` when the cache is `None`.
- Consumes: `lib.audit.cached_json(response: httpx.Response) -> object | None` and the `"gitcrawl.json"` extension key that `record_from_response` populates.

- [ ] **Step 1: Write the failing tests**

```python
def test_fetch_batch_reads_the_hook_cached_body_without_reparsing(monkeypatch):
    payload = node_payload(["1"])
    response = httpx.Response(200, json=payload)
    calls = {"n": 0}
    original_json = response.json

    def counting_json():
        calls["n"] += 1
        return original_json()

    def caching_hook(resp: httpx.Response, latency_ms: float) -> None:
        resp.extensions["gitcrawl.json"] = resp.json()

    monkeypatch.setattr(response, "json", counting_json)
    client = client_from([response])
    outcome = fetch_batch(DictAdapter(), ["1"], client=client, on_response=caching_hook)

    assert outcome.values == {"1": "value-1"}
    assert calls["n"] == 1


def test_fetch_batch_parses_once_without_a_caching_hook(monkeypatch):
    payload = node_payload(["1"])
    response = httpx.Response(200, json=payload)
    calls = {"n": 0}
    original_json = response.json

    def counting_json():
        calls["n"] += 1
        return original_json()

    monkeypatch.setattr(response, "json", counting_json)
    client = client_from([response])
    outcome = fetch_batch(DictAdapter(), ["1"], client=client)

    assert outcome.values == {"1": "value-1"}
    assert calls["n"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_batch_core.py -q -k "cached_body or parses_once"`
Expected: `test_fetch_batch_reads_the_hook_cached_body_without_reparsing` FAILS with `assert 2 == 1` (the second parse is `_post`'s), the other passes.

- [ ] **Step 3: Write minimal implementation**

In `src/lib/graphql_batch.py`, change line 13 from `from lib import cancellation` to:

```python
from lib import audit, cancellation
```

Replace the parse block at `src/lib/graphql_batch.py:157-160` with:

```python
    payload = audit.cached_json(response)
    if payload is None:
        try:
            payload = response.json()
        except ValueError:
            raise MalformedResponse("graphql response is not JSON") from None
```

The existing `if not isinstance(payload, dict): raise MalformedResponse(...)` on lines 161-162 stays. Note the cache stores `None` for an unparseable body (`src/lib/audit.py:104`), which sends us down the `response.json()` fallback exactly as discovery does; that fallback is still a single parse and still raises `MalformedResponse`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_batch_core.py tests/unit/test_graphql_search.py -q`
Expected: PASS. All 30+ existing batch tests (including the malformed-JSON and SSO cases) stay green because the no-hook path is unchanged.

- [ ] **Step 5: Lint/format/types then commit**

```bash
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m black --check src tests
.venv\Scripts\python.exe -m mypy
git add src/lib/graphql_batch.py tests/unit/test_graphql_batch_core.py
git commit -m "perf(lib): reuse the audit-cached graphql body in fetch_batch"
```

### Task 3: Buffer discovery upserts per worker behind page/item thresholds

Today every search page is upserted on the fetch thread before the next page is requested (`src/discover/pipeline.py:282`), so Postgres commit latency lands directly in the 95.6 s discovery phase (spec §3.3, recommendation 7). This task accumulates each worker's page items and upserts every N pages or M items (and once more at shard exit), still inside the worker loop — no new threads, no queue, no change to `ShardStore` semantics.

**Files:**
- Modify: `src/discover/pipeline.py:30-31` (add two module constants), `:208-235` (`_Worker.__init__` signature and assignments), `:246-342` (`_Worker.process` body), `:345-356` (`run_search_discovery` signature), `:406-418` (pass the thresholds when constructing `_Worker`)
- Test: `tests/integration/test_pipeline.py` (append after `test_page_upserts_do_not_serialize_on_the_worker_lock`, line 229)

**Interfaces:**
- Produces: `discover.pipeline._UPSERT_EVERY_PAGES = 10`, `discover.pipeline._UPSERT_EVERY_ITEMS = 1000`.
- Produces: `run_search_discovery(..., *, upsert_every_pages: int = _UPSERT_EVERY_PAGES, upsert_every_items: int = _UPSERT_EVERY_ITEMS, ...)` — keyword-only, defaults preserve today's aggregate behavior.
- Produces: `_Worker(..., *, max_pages, sleep, now, jitter, upsert_every_pages=..., upsert_every_items=...)`.
- Consumes: `store.upserts.upsert_repos(engine, items, *, batch_size=500, etags=None) -> UpsertStats` and `store.upserts.dedupe_items(items) -> list[dict]` (already imported at `src/discover/pipeline.py:26`).
- Preserves: `_collect_ids(seen, collected, page.items)` runs per page in arrival order; `_fold(stats, upserted)` sums the same `UpsertStats`; `page.exhausted` / `page.incomplete` / under-coverage completion flags and all `set_state` calls are untouched.

- [ ] **Step 1: Write the failing tests**

```python
def test_multi_page_shard_upserts_are_batched_by_page_threshold(clean: Engine, monkeypatch):
    calls: list[int] = []
    real_upsert = pipeline.upsert_repos

    def counting_upsert(engine, items, **kwargs):
        materialized = list(items)
        calls.append(len(materialized))
        return real_upsert(engine, materialized, **kwargs)

    monkeypatch.setattr(pipeline, "upsert_repos", counting_upsert)

    pages = iter(
        [
            page_payload(range(1000, 1100), total=250, has_next=True, cursor="c1"),
            page_payload(range(1100, 1200), total=250, has_next=True, cursor="c2"),
            page_payload(range(1200, 1250), total=250),
        ]
    )

    def handler(request: httpx.Request):
        if is_count(request):
            return count_payload(request, 250)
        return next(pages)

    deps = make_deps(clean, scripted_client(handler, []))
    stats = run_search_discovery(deps, "topic:ai", upsert_every_pages=2, jitter=lambda: 0.0)

    assert calls == [200, 50]
    assert stats.pages == 3
    assert stats.fetched == 250
    assert stats.inserted == 250
    assert stats.incomplete_shards == 0
    assert shard_state(clean, 1) == ("done", False)


def test_page_fetch_does_not_wait_for_the_upsert(clean: Engine, monkeypatch):
    events: list[str] = []
    real_upsert = pipeline.upsert_repos

    def slow_upsert(engine, items, **kwargs):
        events.append("upsert-start")
        time.sleep(0.2)
        result = real_upsert(engine, items, **kwargs)
        events.append("upsert-end")
        return result

    monkeypatch.setattr(pipeline, "upsert_repos", slow_upsert)

    pages = iter(
        [
            page_payload(range(2000, 2100), total=250, has_next=True, cursor="c1"),
            page_payload(range(2100, 2200), total=250, has_next=True, cursor="c2"),
            page_payload(range(2200, 2250), total=250),
        ]
    )

    def handler(request: httpx.Request):
        if is_count(request):
            return count_payload(request, 250)
        events.append("page")
        return next(pages)

    deps = make_deps(clean, scripted_client(handler, []))
    run_search_discovery(deps, "topic:ai", upsert_every_pages=3, jitter=lambda: 0.0)

    assert events == ["page", "page", "page", "upsert-start", "upsert-end"]


def test_item_threshold_flushes_before_the_page_threshold(clean: Engine, monkeypatch):
    calls: list[int] = []
    real_upsert = pipeline.upsert_repos

    def counting_upsert(engine, items, **kwargs):
        materialized = list(items)
        calls.append(len(materialized))
        return real_upsert(engine, materialized, **kwargs)

    monkeypatch.setattr(pipeline, "upsert_repos", counting_upsert)

    pages = iter(
        [
            page_payload(range(3000, 3100), total=150, has_next=True, cursor="c1"),
            page_payload(range(3100, 3150), total=150),
        ]
    )

    def handler(request: httpx.Request):
        if is_count(request):
            return count_payload(request, 150)
        return next(pages)

    deps = make_deps(clean, scripted_client(handler, []))
    stats = run_search_discovery(
        deps,
        "topic:ai",
        upsert_every_pages=50,
        upsert_every_items=150,
        jitter=lambda: 0.0,
    )

    assert calls == [150]
    assert stats.fetched == 150
    assert stats.inserted == 150


def test_repeated_repo_within_one_flush_window_is_deduped(clean: Engine):
    pages = iter(
        [
            page_payload([repo_node(4000)], total=2, has_next=True, cursor="c1"),
            page_payload([repo_node(4000)], total=2),
        ]
    )

    def handler(request: httpx.Request):
        if is_count(request):
            return count_payload(request, 2)
        return next(pages)

    deps = make_deps(clean, scripted_client(handler, []))
    stats = run_search_discovery(deps, "topic:ai", jitter=lambda: 0.0)

    assert stats.pages == 2
    assert stats.fetched == 2
    assert stats.inserted == 1
    assert stats.repo_ids == (4000,)
    assert scalar(clean, "SELECT count(*) FROM repos") == 1
```

`test_item_threshold_flushes_before_the_page_threshold` pins "one combined write, never 100+50": the 150-item threshold fires at page 2 and the empty end-of-shard flush is a no-op. `test_repeated_repo_within_one_flush_window_is_deduped` pins the one deliberate accounting difference: an id repeated inside a single flush window is one row (via `dedupe_items`, newest `pushed_at` wins) instead of yesterday's insert-then-update; `fetched`, `repo_ids`, and the DB end state are identical.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/integration/test_pipeline.py -q -k "batched_by_page_threshold or wait_for_the_upsert or item_threshold or repeated_repo"`
Expected: FAIL — `TypeError: run_search_discovery() got an unexpected keyword argument 'upsert_every_pages'`.

- [ ] **Step 3: Write minimal implementation**

Add after `_MAX_PROBE_CONCURRENCY = 8` (`src/discover/pipeline.py:31`):

```python
_UPSERT_EVERY_PAGES = 10
_UPSERT_EVERY_ITEMS = 1000
```

Extend `_Worker.__init__` (`src/discover/pipeline.py:208-235`) with the two parameters and assignments:

```python
        *,
        max_pages: int,
        sleep: Callable[[float], None],
        now: Callable[[], float],
        jitter: Callable[[], float] | None,
        upsert_every_pages: int = _UPSERT_EVERY_PAGES,
        upsert_every_items: int = _UPSERT_EVERY_ITEMS,
    ) -> None:
```

and inside the body:

```python
        self._upsert_every_pages = upsert_every_pages
        self._upsert_every_items = upsert_every_items
```

Replace the whole `_Worker.process` body (`src/discover/pipeline.py:246-342`) with:

```python
    def process(self, shard_id: int) -> None:
        row = self._store.get(shard_id)
        if row.query is None:
            raise ValueError(f"shard {shard_id} has no query")
        if row.state in (ShardState.DONE, ShardState.INCOMPLETE):
            return
        if self._stop.is_set():
            return
        cancellation.check()
        if self._deadline_expired():
            self._stop_for_deadline()
            if row.state is ShardState.ACTIVE:
                self._store.set_state(shard_id, ShardState.PENDING)
            return
        if row.state is ShardState.PENDING:
            self._store.set_state(shard_id, ShardState.ACTIVE)
        fetched = 0
        last_total = row.total_count
        incomplete = False
        pending: list[dict] = []
        pending_pages = 0

        def flush_pending() -> None:
            nonlocal pending_pages
            if not pending:
                return
            upserted = upsert_repos(self._deps.engine, dedupe_items(pending))
            with self._lock:
                _fold(self._stats, upserted)
            pending.clear()
            pending_pages = 0

        def flush_pending_best_effort() -> None:
            try:
                flush_pending()
            except Exception:
                logger.exception("could not flush buffered discovery pages")

        try:
            for page in iter_pages(
                self._deps.client,
                row.query,
                max_pages=self._max_pages,
                limiter=self._deps.limiter,
                token_id=self._deps.token_fp,
                on_response=self._hook,
                sleep=self._sleep,
                now=self._now,
                jitter=self._jitter,
            ):
                cancellation.check()
                if self._deadline_expired():
                    self._stop_for_deadline()
                    flush_pending()
                    self._store.set_state(shard_id, ShardState.PENDING)
                    return
                pending.extend(page.items)
                pending_pages += 1
                with self._lock:
                    self._stats.pages += 1
                    self._stats.fetched += len(page.items)
                    fetched += len(page.items)
                    last_total = page.repository_count
                    _collect_ids(self._seen, self._collected, page.items)
                    if page.exhausted:
                        self._stats.page_capped_shards += 1
                        incomplete = True
                    if page.incomplete:
                        incomplete = True
                    if page.repository_count > fetched and not page.has_next:
                        incomplete = True
                if (
                    pending_pages >= self._upsert_every_pages
                    or len(pending) >= self._upsert_every_items
                ):
                    flush_pending()
            flush_pending()
        except (
            RequestFailed,
            ThrottledError,
            PartialResultsError,
            MalformedResponse,
            httpx.HTTPError,
        ):
            flush_pending_best_effort()
            with self._lock:
                self._stats.incomplete_shards += 1
            self._store.set_state(
                shard_id,
                ShardState.INCOMPLETE,
                fetched=fetched,
                total_count=last_total,
                incomplete=True,
            )
            return
        except cancellation.RunCancelled:
            flush_pending_best_effort()
            self._store.set_state(shard_id, ShardState.PENDING)
            raise
        except DeadlineExceededError:
            flush_pending_best_effort()
            self._stop_for_deadline()
            self._store.set_state(shard_id, ShardState.PENDING)
            return
        except BaseException:
            flush_pending_best_effort()
            try:
                self._store.set_state(
                    shard_id,
                    ShardState.INCOMPLETE,
                    fetched=fetched,
                    total_count=last_total,
                    incomplete=True,
                )
            except Exception:
                logger.exception("could not mark shard %s incomplete", shard_id)
            raise
        if incomplete:
            with self._lock:
                self._stats.incomplete_shards += 1
        self._store.set_state(
            shard_id,
            ShardState.INCOMPLETE if incomplete else ShardState.DONE,
            fetched=fetched,
            total_count=last_total,
            incomplete=True if incomplete else None,
        )
```

Extend the `run_search_discovery` signature (`src/discover/pipeline.py:345-357`) with:

```python
    upsert_every_pages: int = _UPSERT_EVERY_PAGES,
    upsert_every_items: int = _UPSERT_EVERY_ITEMS,
```

placed after `discovery_concurrency: int = 32,`, and pass both through to `_Worker` (`src/discover/pipeline.py:406-418`):

```python
            upsert_every_pages=upsert_every_pages,
            upsert_every_items=upsert_every_items,
```

Design notes for the implementer: the buffer lives entirely inside `process`, so it is naturally per shard/worker and bounded at `upsert_every_items` items (`upsert_every_items=1000` -> at most ten 100-repo pages of dicts, a few MB). `flush_pending` runs on the worker thread only; `_collect_ids`, stats counters, and completion flags are updated per page exactly as before, so only the DB round trip is deferred. `flush_pending_best_effort` is used on exception paths so a failed flush cannot mask RunCancelled / RequestFailed and cannot lose the original shard-state write; those paths are already unwinding and the failure is logged at ERROR.

- [ ] **Step 4: Run tests to verify they pass, then the whole pipeline suite**

Run: `.venv\Scripts\python.exe -m pytest tests/integration/test_pipeline.py -q`
Expected: PASS — the four new tests plus every pre-existing discovery test (including `test_page_upserts_do_not_serialize_on_the_worker_lock`, which still sees two concurrent flushes from the two workers because the flush is outside the worker lock).

- [ ] **Step 5: Lint/format/types then commit**

```bash
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m black --check src tests
.venv\Scripts\python.exe -m mypy
git add src/discover/pipeline.py tests/integration/test_pipeline.py
git commit -m "perf(discover): buffer per-worker upserts on page/item thresholds"
```

### Task 4: Move audit INSERTs onto a bounded writer thread

Audit records are currently written by whichever network worker happens to fill the buffer (`src/lib/audit.py:145-150`), so roughly every 100 responses a worker blocks on the INSERT. This task hands batches to a daemon writer thread behind a bounded queue of at most 4 pending batches; `add()` never performs a DB round trip, and `flush()` remains a synchronous barrier so `runner.py:787-791` keeps its "everything is persisted before the run returns" guarantee.

**Files:**
- Modify: `src/lib/audit.py:3-8` (add `import queue`), `src/lib/audit.py:136-158` (replace `AuditBuffer`)
- Test: `tests/integration/test_audit_db.py:1-8` (add `import threading`, `import time`), `:109-128` (adapt `test_audit_buffer_batches_inserts`), append new tests after `:147`

**Interfaces:**
- Produces: `lib.audit.AuditBuffer(engine: Engine, *, batch_size: int = 100)` with unchanged public surface:
  - `add(record: AuditRecord) -> None` — appends and, at `batch_size`, enqueues a batch; never waits for an INSERT except when the bounded queue is full (backpressure at 4 pending batches).
  - `flush() -> None` — submits the partial batch, drains every submitted batch, stops the writer thread, and re-raises the first write failure. Must be called when no other thread is adding (true at every call site: discovery's `_flush_audit` runs after the shard pool joins, runner's finally runs after `_run_filter` returns).
- Produces: writer thread named `"gitcrawl-audit"`; `flush()` leaves `_writer` stopped so a long-lived process (the console server creates one buffer per run at `src/serve/runner.py:136`) does not accumulate threads; a later `add` restarts it.
- Consumes: `sqlalchemy.insert(AuditLog)`, `dataclasses.asdict`, `queue.Queue`.

- [ ] **Step 1: Adapt the existing batching test and write the new failing tests**

Adapt `tests/integration/test_audit_db.py:109-128` — the INSERT is asynchronous now, so the test must await it; the assertion (10 records -> exactly 1 INSERT) is unchanged:

```python
def test_audit_buffer_batches_inserts(clean: Engine):
    from sqlalchemy import event

    from lib.audit import AuditBuffer

    statements: list[str] = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if "INTO AUDIT_LOG" in statement.upper():
            statements.append(statement)

    buffer = AuditBuffer(clean, batch_size=10)
    event.listen(clean, "before_cursor_execute", listener)
    try:
        for index in range(10):
            buffer.add(_record(index))
        buffer.flush()
    finally:
        event.remove(clean, "before_cursor_execute", listener)
    assert len(statements) == 1
    assert _audit_count(clean) == 10
```

Append the new tests (and the two small engine doubles) after `test_audit_buffer_rejects_non_positive_batch_size` (line 147):

```python
class _BlockingEngine:
    def __init__(self, engine: Engine, started: threading.Event, release: threading.Event) -> None:
        self._engine = engine
        self.started = started
        self.release = release

    def begin(self):
        self.started.set()
        if not self.release.wait(timeout=5.0):
            raise TimeoutError("the test never released the blocked insert")
        return self._engine.begin()


class _FailingEngine:
    def begin(self):
        raise RuntimeError("audit sink down")


def test_audit_buffer_add_returns_while_the_insert_is_blocked(clean: Engine):
    from lib.audit import AuditBuffer

    started = threading.Event()
    release = threading.Event()
    buffer = AuditBuffer(_BlockingEngine(clean, started, release), batch_size=1)

    buffer.add(_record(0))  # must return without waiting for the INSERT

    assert started.wait(timeout=5.0)
    release.set()
    buffer.flush()
    assert _audit_count(clean) == 1


def test_audit_buffer_flush_waits_for_the_blocked_insert(clean: Engine):
    from lib.audit import AuditBuffer

    started = threading.Event()
    release = threading.Event()
    buffer = AuditBuffer(_BlockingEngine(clean, started, release), batch_size=10)
    for index in range(3):
        buffer.add(_record(index))

    flushed = threading.Event()

    def run_flush() -> None:
        buffer.flush()
        flushed.set()

    flusher = threading.Thread(target=run_flush)
    flusher.start()
    assert started.wait(timeout=5.0)
    assert not flushed.is_set()  # flush is a barrier: it is still waiting on the INSERT

    release.set()
    flusher.join(timeout=5.0)
    assert flushed.is_set()
    assert _audit_count(clean) == 3


def test_audit_buffer_flush_raises_the_write_failure():
    from lib.audit import AuditBuffer

    buffer = AuditBuffer(_FailingEngine(), batch_size=10)
    buffer.add(_record(0))
    with pytest.raises(RuntimeError, match="audit sink down"):
        buffer.flush()


def test_audit_buffer_add_raises_a_stored_write_failure():
    from lib.audit import AuditBuffer

    buffer = AuditBuffer(_FailingEngine(), batch_size=1)
    buffer.add(_record(0))
    deadline = time.monotonic() + 5.0
    while buffer._error is None and time.monotonic() < deadline:
        time.sleep(0.01)

    with pytest.raises(RuntimeError, match="audit sink down"):
        buffer.add(_record(1))
    with pytest.raises(RuntimeError, match="audit sink down"):
        buffer.flush()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/integration/test_audit_db.py -q -k "buffer"`
Expected: FAIL — `test_audit_buffer_add_returns_while_the_insert_is_blocked` blocks through `add` until `_BlockingEngine` times out and then raises `TimeoutError`; `test_audit_buffer_flush_raises_the_write_failure` fails because today `add` itself raises, not `flush`.

- [ ] **Step 3: Write minimal implementation**

In `src/lib/audit.py`, add `import queue` to the stdlib block (after `import json`, before `import threading`), then replace the `AuditBuffer` class (`src/lib/audit.py:136-158`) with:

```python
_WRITER_QUEUE_DEPTH = 4


class AuditBuffer:
    """Batch audit writes onto a bounded background writer thread.

    ``add`` never performs a database round trip: records are handed to a
    daemon writer through a bounded queue (at most ``_WRITER_QUEUE_DEPTH``
    pending batches), so a network worker never blocks on the audit INSERT
    unless the writer is more than four batches behind. ``flush`` is a
    barrier: it submits the partial batch, waits for every submitted batch,
    stops the writer, and re-raises the first write failure. Call ``flush``
    only when no other thread is adding records.
    """

    def __init__(self, engine: Engine, *, batch_size: int = 100) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self._engine = engine
        self._batch_size = batch_size
        self._records: list[AuditRecord] = []
        self._lock = threading.Lock()
        self._writer_lock = threading.Lock()
        self._batches: queue.Queue[list[AuditRecord] | None] = queue.Queue(
            maxsize=_WRITER_QUEUE_DEPTH
        )
        self._writer: threading.Thread | None = None
        self._error: BaseException | None = None

    def add(self, record: AuditRecord) -> None:
        with self._lock:
            self._records.append(record)
            if len(self._records) < self._batch_size:
                return
            batch, self._records = self._records, []
        self._submit(batch)
        if self._error is not None:
            raise self._error

    def flush(self) -> None:
        with self._lock:
            if self._records:
                batch, self._records = self._records, []
                self._submit(batch)
            if self._writer is not None and self._writer.is_alive():
                self._batches.put(None)
                self._writer.join()
            self._writer = None
        if self._error is not None:
            error, self._error = self._error, None
            raise error

    def _submit(self, batch: list[AuditRecord]) -> None:
        self._ensure_writer()
        self._batches.put(batch)

    def _ensure_writer(self) -> None:
        with self._writer_lock:
            if self._writer is not None and self._writer.is_alive():
                return
            self._writer = threading.Thread(
                target=self._write_loop, name="gitcrawl-audit", daemon=True
            )
            self._writer.start()

    def _write_loop(self) -> None:
        while True:
            batch = self._batches.get()
            try:
                if batch is None:
                    return
                try:
                    with self._engine.begin() as connection:
                        connection.execute(insert(AuditLog), [asdict(record) for record in batch])
                except BaseException as exc:
                    if self._error is None:
                        self._error = exc
            finally:
                self._batches.task_done()
```

Behavioral notes: the writer continues after a failed batch so a sentinel-based `flush()` can always join; the first failure is re-raised by the next `add` (fast failure for a live run) and by `flush()` (final guarantee). Batches queued after a sentinel are drained by the next writer start, so no record is dropped, at worst deferred. `runner.py` and `pipeline._flush_audit` need no changes: both already call `flush()` in the right places.

- [ ] **Step 4: Run the audit, runner, and pipeline suites**

Run: `.venv\Scripts\python.exe -m pytest tests/integration/test_audit_db.py tests/integration/test_runner.py tests/integration/test_pipeline.py -q`
Expected: PASS — including `test_run_filter_flushes_the_audit_buffer_at_run_end`, `test_run_filter_flushes_the_audit_buffer_when_the_run_fails`, and `test_audit_buffer_defers_record_audit_until_run_end`, which all rely on the unchanged `flush()` barrier.

- [ ] **Step 5: Lint/format/types then commit**

```bash
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m black --check src tests
.venv\Scripts\python.exe -m mypy
git add src/lib/audit.py tests/integration/test_audit_db.py
git commit -m "perf(audit): write audit batches on a bounded writer thread"
```

### Task 5: Verification and measurement

Prove each change with its deterministic test, then run the full gates. No source changes in this task: if everything below is green, the only artifact is the findings note.

**Files:**
- Create: `docs/findings/2026-10-09-transport-pipeline-efficiency.md`
- Scratch (not committed): a temp live-timing script under `C:\Users\Abdul\AppData\Local\Temp\opencode`

**Interfaces:**
- Consumes: all tests added in Tasks 1-4; `discover.pipeline.run_search_discovery` (live measurement only).

- [ ] **Step 1: Run the focused evidence tests and record the observed output**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_gh_client.py::test_create_client_pins_the_pool_and_ignores_environment_proxies -q
.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_batch_core.py -q -k "cached_body or parses_once"
.venv\Scripts\python.exe -m pytest tests/integration/test_pipeline.py -q -k "batched_by_page_threshold or wait_for_the_upsert or item_threshold or repeated_repo"
.venv\Scripts\python.exe -m pytest tests/integration/test_audit_db.py -q -k "buffer"
```

Expected evidence:
- `response.json()` is called exactly once per batch response when the hook cached the body (Task 2 test), and once without a hook.
- `events == ["page", "page", "page", "upsert-start", "upsert-end"]` — all three page fetches complete before the first Postgres upsert, and a 3-page shard issues 2 upserts instead of 3.
- `add()` returns while the writer is blocked inside the INSERT; `flush()` blocks until it returns; a failed write surfaces exactly once from `flush()`.
- `create_client` passes `httpx.Limits(max_connections=64, max_keepalive_connections=64, keepalive_expiry=60.0)` and `trust_env=False`, and never passes `http2` (connection reuse is asserted at configuration level; `httpx.MockTransport` bypasses pooling by design, and one shared client per run is already pinned by `tests/integration/test_runner.py:1118`).

- [ ] **Step 2: Run the full suite with the coverage gate**

```powershell
$env:TEST_DATABASE_URL = 'postgresql+psycopg://gitcrawl:gitcrawl@localhost:5432/gitcrawl_test'
.venv\Scripts\python.exe -m pytest -q --cov=src --cov-report=term-missing
```

Expected: PASS, exit 0, total coverage >= 93 (baseline was ~95–96 per `docs/development-log.md`).

- [ ] **Step 3: Run lint, format, and types**

```bash
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m black --check src tests
.venv\Scripts\python.exe -m mypy
```

Expected: all clean. `mypy` covers `src/lib` (Tasks 1, 2, 4); `src/discover` is outside the gated set but ruff/black still apply.

- [ ] **Step 4 (optional, needs a token + network): live discovery timing**

Write `C:\Users\Abdul\AppData\Local\Temp\opencode\measure_discovery.py` that builds real `Deps` (via `serve.runner.build_deps`), binds a `Deadline(86400)`, times `pipeline.run_search_discovery(deps, "language:rust stars:>=4 created:>=2025-02-24", max_shards=1000, discovery_concurrency=32)` with `time.perf_counter`, and prints `seconds / shards / pages / fetched`. Run it once and compare against the recorded 95.6 s discovery phase for the 39,128-repo run (spec §2). Treat run-to-run server latency variance as dominant (run 18 was 2x run 19 on identical code); the mocked tests in Step 1 are the deterministic evidence. If the live run is not executed, say so explicitly in the findings note.

- [ ] **Step 5: Write `docs/findings/2026-10-09-transport-pipeline-efficiency.md`**

Sections: (1) what changed, one line per task with file:line; (2) evidence table — command, expected observation, recorded observation, from Step 1; (3) full-suite and coverage totals from Step 2; (4) live timing from Step 4 or an explicit "not run"; (5) residual risks: async audit writes persist at `flush()` (a process kill loses at most the queue + partial batch, the same bound as today's unwritten buffer), window-level dedupe accounting for repeated ids within one flush window (DB end state identical), `trust_env=False` removes implicit env-proxy support (none documented), and HTTP/2 remains deliberately off (`h2` not installed).

- [ ] **Step 6: Commit the findings note**

```bash
git add docs/findings/2026-10-09-transport-pipeline-efficiency.md
git commit -m "docs(findings): transport and pipeline efficiency measurements"
```

## Self-review notes

- **Spec coverage:** §3.1 leaks — keep-alive pool now 64/64/60 s (Task 1); `trust_env=False` per recommendation 3; HTTP/1.1 retention is an explicit non-goal with the `h2`-not-installed reason. §3.3 rough edge 1 (double parse) — Task 2. §3.3 rough edge 2 and §8 recommendation 7 (discovery inline upsert) — Task 3. Recommendation 7's audit item is not in the spec; it is the user-scoped Task 4, bounded and flush-compatible. Other recommendations (1, 2, 4, 5, 6) are out of scope by instruction.
- **Placeholder scan:** no TBD/TODO; every code step shows the real test and implementation code; no "similar to Task N" references.
- **Type consistency:** `upsert_every_pages`/`upsert_every_items` are named identically in `run_search_discovery`, `_Worker.__init__`, and the tests; `flush_pending`/`flush_pending_best_effort` are internal to `process`; `AuditBuffer.add`/`flush` signatures are unchanged from today, so `runner.py`, `pipeline._flush_audit`, and all existing tests bind without edits.
- **Interfaces frozen:** `create_client`, `_post`, `run_search_discovery` (defaults only), `AuditBuffer` (defaults only) keep their public contracts; no migration, no settings, no dependency changes.
