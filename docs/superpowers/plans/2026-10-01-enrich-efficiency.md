# Enrich Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make tree matching O(patterns + hits), geo resolution batched with a process memo, GraphQL split failures keep paid-for results, clone runs bounded-concurrent and failure-storm-safe, and segment bucketing single-pass.

**Architecture:** Additive helpers on existing classes (`FilePresence.match`, `GeoCache.get_many/put_many`, `resolve_many`, `clone_repos(workers=…)`), plus one rewrite of `graphql_batch._fetch` from recursive to iterative so successful chunks survive a failing sibling. Defaults preserve current behavior (`workers=1`, serial execution).

**Tech Stack:** Python 3.12, httpx, SQLAlchemy 2.1 + psycopg3, `concurrent.futures`, pytest, ruff.

**Spec:** `docs/superpowers/plans/2026-10-01-efficiency-audit-report.md` (findings S8, S9, S10, S11, #18)

## Global Constraints

- Python 3.12, `from __future__ import annotations`, ruff line length 100.
- Tests: `pytest`; integration tests need `TEST_DATABASE_URL` ending `_test`.
- No new third-party dependencies.
- Preserve existing public function signatures and return shapes unless a task explicitly changes them.
- One commit per task, prefix `perf:`.

---

## File Map

| File | Change |
|---|---|
| `src/enrich/trees_first.py` | literal fast path + single glob pass in `match` |
| `src/enrich/geo_resolver.py` | `GeoCache.get_many/put_many`, `resolve_many`, capped `_MEMO` |
| `src/serve/runner.py` | `_apply_geo` batched cache + single owner update transaction |
| `src/enrich/graphql_batch.py` | iterative `_fetch` with partial results; `discussions(first: 1)` |
| `src/enrich/cloner.py` | `workers`, `clone_timeout`, `errors_cap`, `error_count`, emit-on-complete |
| `src/enrich/segment_executor.py` | `bisect_left` bucketing + `deque` queue |
| `tests/unit/test_trees_first.py` | add literal/glob match tests |
| `tests/integration/test_geo_resolver.py` | add bulk cache test |
| `tests/unit/test_graphql_batch.py` | update split/query-shape tests |
| `tests/unit/test_cloner.py` | add workers/timeout/error-cap tests |
| `tests/unit/test_segment_executor.py` | add single-pass bucketing test |

---

### Task 1: O(1) literal tree matching

**Files:**
- Modify: `src/enrich/trees_first.py:20-33`
- Test: `tests/unit/test_trees_first.py`

**Interfaces:**
- `FilePresence.match(patterns) -> dict[str, bool]` unchanged; literal patterns never call `fnmatchcase`; glob patterns match exactly as before.

- [ ] **Step 1: Write the failing test**

```python
def test_match_handles_literals_without_fnmatch(monkeypatch):
    from enrich import trees_first

    def boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("fnmatchcase called for a literal pattern")

    monkeypatch.setattr(trees_first, "fnmatchcase", boom)
    presence = trees_first.FilePresence(
        repo_full_name="octo/mono",
        paths=frozenset({"Dockerfile", "README.md", "src/main.py"}),
        truncated=False,
        source="tree",
    )
    assert presence.match(["Dockerfile", "LICENSE"]) == {"Dockerfile": True, "LICENSE": False}


def test_match_still_supports_globs():
    from enrich.trees_first import FilePresence

    presence = FilePresence(
        repo_full_name="octo/mono",
        paths=frozenset({"Dockerfile", "src/main.py", "docs/guide.md"}),
        truncated=False,
        source="tree",
    )
    assert presence.match(["**/main.py", "*.md", "Dockerfile"]) == {
        "**/main.py": True,
        "*.md": False,
        "Dockerfile": True,
    }
```

(`fnmatch` globs: `*.md` does not cross `/`, so `docs/guide.md` is False and `**/main.py` matches `src/main.py` — this encodes current `fnmatchcase` semantics.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_trees_first.py -v`
Expected: FAIL — `AssertionError: fnmatchcase called for a literal pattern`.

- [ ] **Step 3: Implement**

```python
import re
from fnmatch import fnmatchcase, translate

_LITERAL_CHARS = frozenset("*?[")


def _is_literal(pattern: str) -> bool:
    return not any(char in _LITERAL_CHARS for char in pattern)

    def match(self, patterns: Sequence[str]) -> dict[str, bool]:
        result: dict[str, bool] = {}
        globs: list[tuple[str, re.Pattern[str]]] = []
        for pattern in patterns:
            if _is_literal(pattern):
                result[pattern] = pattern in self.paths
            else:
                globs.append((pattern, re.compile(translate(pattern))))
        if globs:
            pending = {pattern: False for pattern, _ in globs}
            for path in self.paths:
                for pattern, regex in globs:
                    if not pending[pattern] and regex.match(path):
                        pending[pattern] = True
            result.update(pending)
        return result
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_trees_first.py tests/integration/test_runner.py -q`
Expected: PASS (runner `_apply_dockerfile` uses `presence.has`, unaffected).

- [ ] **Step 5: Commit**

```bash
git add src/enrich/trees_first.py tests/unit/test_trees_first.py
git commit -m "perf: O(1) literal path matching in tree presence"
```

---

### Task 2: Batched geo resolution with process memo

**Files:**
- Modify: `src/enrich/geo_resolver.py:729-790`, `src/serve/runner.py:256-315`
- Test: `tests/integration/test_geo_resolver.py`

**Interfaces:**
- `GeoCache.get_many(keys, *, batch_size=1000) -> dict[str, GeoResult]`
- `GeoCache.put_many(results: Mapping[str, GeoResult]) -> None`
- `geo_resolver.resolve_many(engine, raw_locations: Sequence[str | None], *, cache: GeoCache | None = None) -> dict[str | None, GeoResult]` — keyed by the normalized location (empty string for `None`).
- `GeoCache.get` / `GeoCache.put` / `resolve_owner` stay unchanged for existing callers/tests.

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_geo_resolver.py` (mirror the file's existing `schema` fixture pattern; truncate first):

```python
def test_geo_cache_bulk_roundtrip(alembic_engine):
    from sqlalchemy import text

    from enrich.geo_resolver import GeoCache, GeoResult

    with alembic_engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE geo_cache"))
    cache = GeoCache(alembic_engine)
    cache.put_many(
        {"london": GeoResult("GB", "name", "London"), "paris": GeoResult("FR", "name", "Paris")}
    )
    found = cache.get_many(["london", "paris", "missing"])
    assert found["london"].country_iso == "GB"
    assert found["paris"].country_iso == "FR"
    assert "missing" not in found
    cache.put_many({"london": GeoResult("GB", "name", "London")})
    with alembic_engine.connect() as connection:
        hits = connection.scalar(text("SELECT hits FROM geo_cache WHERE normalized = 'london'"))
    assert hits == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_geo_resolver.py::test_geo_cache_bulk_roundtrip -v`
Expected: FAIL — `AttributeError: 'GeoCache' object has no attribute 'put_many'`.

- [ ] **Step 3: Implement `geo_resolver.py`**

Add `from lib.batching import chunked` (created in Plan 1 Task 2 — if running this plan standalone, create it exactly as specified there) and:

```python
_MEMO_MAX = 4096
_MEMO: dict[str, GeoResult] = {}


def _cache_key(raw: str | None) -> str:
    cleaned = _as_raw(raw)
    return normalize_location(cleaned) if cleaned is not None else ""


class GeoCache:
    ...
    def get_many(self, keys: Sequence[str], *, batch_size: int = 1000) -> dict[str, GeoResult]:
        if not keys:
            return {}
        found: dict[str, GeoResult] = {}
        with self._engine.connect() as connection:
            for batch in chunked(keys, batch_size):
                rows = connection.execute(
                    sa.select(
                        GeoCacheRow.normalized,
                        GeoCacheRow.country_iso,
                        GeoCacheRow.confidence,
                        GeoCacheRow.raw_sample,
                    ).where(GeoCacheRow.normalized.in_(batch))
                ).all()
                for normalized, country_iso, confidence, raw_sample in rows:
                    found[str(normalized)] = GeoResult(country_iso, confidence, raw_sample)
        return found

    def put_many(self, results: Mapping[str, GeoResult]) -> None:
        if not results:
            return
        values = [
            {
                "normalized": key,
                "country_iso": result.country_iso,
                "confidence": result.confidence,
                "raw_sample": result.raw_location,
                "hits": 1,
            }
            for key, result in results.items()
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


def resolve_many(
    engine: Engine,
    raw_locations: Sequence[str | None],
    *,
    cache: GeoCache | None = None,
) -> dict[str | None, GeoResult]:
    if not raw_locations:
        return {}
    store = cache or GeoCache(engine)
    keys = [_cache_key(raw) for raw in raw_locations]
    raw_by_key: dict[str, str | None] = {}
    for key, raw in zip(keys, raw_locations, strict=True):
        raw_by_key.setdefault(key, raw)
    unique = list(raw_by_key)
    cached = store.get_many(unique)
    results: dict[str, GeoResult] = dict(cached)
    fresh: dict[str, GeoResult] = {}
    for key in unique:
        if key in results:
            continue
        memo = _MEMO.get(key)
        if memo is None:
            memo = resolve_location(_as_raw(raw_by_key[key]))
            if len(_MEMO) >= _MEMO_MAX:
                _MEMO.clear()
            _MEMO[key] = memo
            fresh[key] = memo
        results[key] = memo
    if fresh:
        store.put_many(fresh)
    return results
```

- [ ] **Step 4: Rewrite `serve/runner.py:_apply_geo` (lines 294-315) to batch**

```python
    owners = _load_owners(deps.engine, owner_ids)
    cache = GeoCache(deps.engine)
    used = 0
    pending: list[tuple[int, str | None]] = []
    for owner_id in owner_ids:
        owner = owners.get(owner_id)
        if owner is None:
            continue
        if owner["country_iso"] is not None or owner["geo_confidence"] == "unmatched":
            continue
        if owner["location_raw"] is None:
            if used >= budget:
                continue
            used += 1
            ok, location = _fetch_owner_location(deps, owner["login"], hook)
            if not ok:
                continue
            owner["location_raw"] = location
        pending.append((owner_id, owner["location_raw"]))
    resolved = resolve_many(deps.engine, [raw for _, raw in pending], cache=cache)
    updates: list[dict] = []
    for owner_id, raw in pending:
        result = resolved.get(_cache_key(raw))
        confidence = result.confidence if result is not None else "unmatched"
        country_iso = result.country_iso if result is not None else None
        updates.append(
            {
                "id": owner_id,
                "location_raw": raw,
                "country_iso": country_iso,
                "geo_confidence": confidence,
            }
        )
        owner = owners[owner_id]
        owner["country_iso"] = country_iso
        owner["geo_confidence"] = confidence
    if updates:
        with deps.engine.begin() as connection:
            connection.execute(update(Owner), updates)
```

Imports: `from enrich.geo_resolver import GeoCache, _cache_key, resolve_many` (keep `resolve_owner` import removed), and `owner_ids = list(dict.fromkeys(row["owner_id"] for row in rows))` replaces the append loop. Delete `_store_owner_geo` (no remaining callers — verify with `rg "_store_owner_geo"`).

- [ ] **Step 5: Run tests**

Run: `pytest tests/integration/test_geo_resolver.py tests/integration/test_runner.py -q`
Expected: PASS. Existing `test_runner.py` geo tests assert geocoder/cache call counts and country filtering; if one asserts a per-owner connection count, update it to the batch behavior and note the change in the commit body.

- [ ] **Step 6: Commit**

```bash
git add src/enrich/geo_resolver.py src/serve/runner.py tests/integration/test_geo_resolver.py
git commit -m "perf: batch geo cache reads/writes and memoize resolved locations"
```

---

### Task 3: GraphQL split keeps partial results; minimal count queries

**Files:**
- Modify: `src/enrich/graphql_batch.py:18-23` (query), `:186-246` (`_fetch`)
- Test: `tests/unit/test_graphql_batch.py`

**Interfaces:** `fetch_graphql_batch(...) -> dict[int, RepoGraphQL]` unchanged; on a batch where some chunks succeed and some fail after splitting, the returned map contains the successful chunks (missing IDs are absent, not zero-valued).

- [ ] **Step 1: Update the failing tests**

In `tests/unit/test_graphql_batch.py`, change the query-shape assertion (currently expects four `first: 50` occurrences, lines ~60-74) to expect `discussions(first: 1)` and `tiers(first: 1)` exactly four times each. Replace the split-retry test with:

```python
def test_split_returns_partial_results_and_retries_only_failures():
    # handler fails the first half, succeeds for the second half
    ...
    result = fetch_graphql_batch(client, token="tok", repo_ids=list(range(1, 5)), node_ids=[f"NODE_{i}" for i in range(1, 5)])
    assert set(result) == {3, 4}  # successful half retained
```

Build the handler with the file's existing `MockTransport` helpers; count POSTs and assert the successful half was requested exactly once and the failing half at most `_MAX_SPLIT_DEPTH` times, never re-requesting the successful half.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_graphql_batch.py -v`
Expected: FAIL — query text and split behavior differ.

- [ ] **Step 3: Implement**

Change the query template (`graphql_batch.py:18-23`) to `discussions(first: 1) { totalCount }` and `tiers(first: 1)` (keep all other fields identical).

Replace `_fetch` (`:186-246`) with:

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
    sleep,
    now,
    jitter,
    on_response,
    depth: int,
) -> dict[int, RepoGraphQL]:
    merged: dict[int, RepoGraphQL] = {}
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
    if not merged and last_error is not None:
        raise last_error
    return merged
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_graphql_batch.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/enrich/graphql_batch.py tests/unit/test_graphql_batch.py
git commit -m "perf: keep successful GraphQL split results and shrink count queries"
```

---

### Task 4: Bounded-concurrent clones, timeout, capped errors

**Files:**
- Modify: `src/enrich/cloner.py:43-61` (`CloneProgress`), `:146-147` (`_default_git_runner`), `:154-216` (`clone_repos`)
- Test: `tests/unit/test_cloner.py`

**Interfaces:**
- `CloneProgress` gains `error_count: int = 0`; `errors` stays capped by `errors_cap` (server renders it).
- `clone_repos(..., workers: int = 1, clone_timeout: float | None = None, errors_cap: int = 20)`.
- `_default_git_runner(argv, cwd, *, timeout: float | None = None)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_clone_workers_run_in_parallel(clean: Engine, tmp_path):
    import threading
    import time as time_module

    specs = [(repo_id, f"octo/r{repo_id}", 1, repo_id) for repo_id in range(1, 5)]
    run_id, _ = seed_run(clean, specs)
    active = 0
    peak = 0
    lock = threading.Lock()

    def fake_runner(argv, cwd):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time_module.sleep(0.05)
        with lock:
            active -= 1

    clone_repos(clean, run_id, limit=4, mode=CloneMode.SHALLOW, dest_root=str(tmp_path), git_runner=fake_runner, workers=4)
    assert peak >= 2


def test_clone_errors_are_capped(clean: Engine, tmp_path):
    specs = [(repo_id, f"octo/r{repo_id}", 1, repo_id) for repo_id in range(1, 21)]
    run_id, _ = seed_run(clean, specs)
    progress = CloneProgress(status="running", total=0, completed=0, failed=0)

    def failing(argv, cwd):
        raise RuntimeError("boom")

    stats = clone_repos(
        clean, run_id, limit=20, mode=CloneMode.SHALLOW, dest_root=str(tmp_path),
        git_runner=failing, errors_cap=5, progress=progress,
    )
    assert stats.failed == 20
    assert progress.error_count == 20
    assert len(progress.errors) == 5


def test_default_git_runner_enforces_timeout():
    import subprocess

    with pytest.raises(subprocess.TimeoutExpired):
        _default_git_runner(["python", "-c", "import time; time.sleep(5)"], ".", timeout=0.05)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_cloner.py -v`
Expected: FAIL — `unexpected keyword 'workers'` / missing `error_count` / timeout unsupported.

- [ ] **Step 3: Implement**

`CloneProgress`:

```python
    errors: list[str] = field(default_factory=list)
    error_count: int = 0
```

`_default_git_runner`:

```python
def _default_git_runner(argv: Sequence[str], cwd: str, *, timeout: float | None = None) -> None:
    subprocess.run(list(argv), cwd=cwd, check=True, timeout=timeout)
```

`clone_repos` body: keep `_run_row`/`_top_items`/empty handling; replace the serial loop (`:187-212`) with:

```python
    runner = git_runner
    lock = threading.Lock()
    progress.error_count = 0

    def clone_one(row) -> str:
        full_name = str(row["full_name"])
        destination = run_dir / full_name.replace("/", "__")
        with lock:
            progress.current = full_name
            progress.emit()
        if (destination / MARKER_NAME).exists():
            outcome = "skipped"
        else:
            try:
                if runner is not None:
                    runner(_clone_argv(full_name, destination, mode), str(destination.parent))
                else:
                    _default_git_runner(
                        _clone_argv(full_name, destination, mode),
                        str(destination.parent),
                        timeout=clone_timeout,
                    )
            except Exception as exc:
                shutil.rmtree(destination, ignore_errors=True)
                outcome = "failed"
                with lock:
                    stats.failed += 1
                    progress.failed += 1
                    progress.error_count += 1
                    if len(progress.errors) < errors_cap:
                        progress.errors.append(_error_message(full_name, exc))
            else:
                destination.mkdir(parents=True, exist_ok=True)
                (destination / MARKER_NAME).write_text(
                    json.dumps({"full_name": full_name, "mode": mode.value}), encoding="utf-8"
                )
                outcome = "completed"
        with lock:
            if outcome in ("completed", "skipped"):
                stats.completed += int(outcome == "completed")
                stats.skipped += int(outcome == "skipped")
                progress.completed += 1
            progress.emit()
        return outcome

    if workers <= 1:
        for row in items:
            clone_one(row)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(clone_one, items))
    progress.current = None
    progress.status = "done"
    progress.emit()
    return stats
```

Imports: `import threading`, `from concurrent.futures import ThreadPoolExecutor`. Note `stats.requested` is set before the loop as today.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_cloner.py tests/contract/test_clone_endpoints.py -q`
Expected: PASS. If `test_clone_endpoints.py` compares serialized progress payloads, add `error_count` to its expected dicts.

- [ ] **Step 5: Commit**

```bash
git add src/enrich/cloner.py tests/unit/test_cloner.py tests/contract/test_clone_endpoints.py
git commit -m "perf: bounded clone concurrency, timeouts, and capped error lists"
```

---

### Task 5: Single-pass segment bucketing

**Files:**
- Modify: `src/enrich/segment_executor.py:87-119`
- Test: `tests/unit/test_segment_executor.py`

**Interfaces:** `execute_segments` unchanged; bucket assignment becomes one `bisect_left` per ID and the work queue becomes a `deque` with `insert(1, …)` only on the rare requeue path.

- [ ] **Step 1: Write the failing test**

```python
def test_segment_bucketing_is_single_pass(monkeypatch):
    from enrich import segment_executor

    calls: list[int] = []
    real = segment_executor.bisect_left
    monkeypatch.setattr(
        segment_executor, "bisect_left", lambda seq, value: (calls.append(value), real(seq, value))[1]
    )
    candidates = [{"id": repo_id, "stargazers": 0, "pushed_at": None} for repo_id in range(1, 101)]
    handler = lambda ids: (ids, 0)
    execute_segments(candidates, {"x": handler}, segments=10)
    assert len(calls) == 100
```

(If the file's existing candidate helper differs, use it; `order_repos` requires `stargazers`, `pushed_at`, and `id` keys.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_segment_executor.py::test_segment_bucketing_is_single_pass -v`
Expected: FAIL — `segment_executor` has no `bisect_left` attribute.

- [ ] **Step 3: Implement**

```python
from bisect import bisect_left
from collections import deque
```

Replace lines `:87-92`:

```python
    segments_plan = plan_segments(ranked_ids, segments=segments)
    ends = [segment.end for segment in segments_plan]
    buckets: list[list[int]] = [[] for _ in segments_plan]
    for repo_id in ranked_ids:
        buckets[bisect_left(ends, repo_id)].append(repo_id)
    work = [
        (segment, ids)
        for segment, ids in zip(segments_plan, buckets, strict=True)
        if ids
    ]
    work.sort(key=lambda item: rank[item[1][0]])
```

Replace `queue = [...]` with `queue: deque[dict] = deque([...])` and `item = queue.pop(0)` with `item = queue.popleft()`; keep `queue.insert(1, item)` for the requeue branch (rare, semantics preserved).

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_segment_executor.py tests/integration/test_runner.py -q`
Expected: PASS — ordering and requeue tests must still hold.

- [ ] **Step 5: Commit**

```bash
git add src/enrich/segment_executor.py tests/unit/test_segment_executor.py
git commit -m "perf: single-pass segment bucketing with deque work queue"
```

---

## Plan self-review

- **Spec coverage:** S9 → T1; S8 + #12 → T2; S11 → T3; S10 + #19 → T4; #18 → T5.
- **Placeholders:** none. Two tests reference file-local helpers (`seed_run`, existing MockTransport helpers) because they exist and are reused by design; alternative shapes are given.
- **Type consistency:** `resolve_many` keys are normalized strings (`""` for `None`), matching `resolve_owner`'s keying; `clone_one` returns one of `"completed" | "skipped" | "failed"`; `error_count` is a plain counter while `errors` is capped.
- **Dependency note:** Task 2 imports `chunked` from Plan 1 Task 2. If executed independently, create `src/lib/batching.py` exactly as specified in that task first.
