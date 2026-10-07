# GraphQL Discovery Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the REST `count_total` + `ShardPlanner` + `iter_shard_pages` discovery path with a two-phase GraphQL engine (batched aliased count probes + parallel single-connection page fetches), preserving shard rows, Stop/resume, `max_pages`, audit, and warnings; then profile hydration.

**Architecture:** New `src/discover/graphql_search.py` owns GraphQL search traffic (counts batched 20 per query at 1 point; pages one connection per query). `scheduler/shard_planner.py` becomes a level-batched `plan_shards`. `discover/pipeline.py` plans, persists shard rows, and runs a `ThreadPoolExecutor(discovery_concurrency)` where each worker walks one shard's pages. The Redis Streams queue, `search_shards.py`, and queue metrics are deleted (R58).

**Tech Stack:** Python 3.12, httpx (sync client + `MockTransport` in tests), SQLAlchemy/Alembic, pytest, fakeredis, FastAPI/Jinja2 console.

**Spec:** `docs/superpowers/specs/2026-10-08-graphql-discovery-design.md`

## Global Constraints

- One personal token; GraphQL search rides the `graphql` points meter (5,000/hr). Count batches are ≤ 20 aliases; pages are one `search` connection per query.
- Every GraphQL call goes through `request_with_retry` (limiter, classifier, audit, SSRF guard).
- Planner target math is unchanged: `min(1000, per_page × max_pages)` with the 1000 fallback when `total_count > max_shards × target`; `max_pages` semantics are kept exactly.
- Shard rows and `ShardStore` states (`pending/active/done/incomplete`) stay; Stop/deadline leave in-flight shards `PENDING`; no row may be stranded `ACTIVE`.
- New setting: `discovery_concurrency`, default 32, bounds 1–64, env `GITCRAWL_DISCOVERY_CONCURRENCY`, corpus preset 32, migration `0012`.
- Error copy rules unchanged: partial runs are declared, never silent.
- Windows: PowerShell 5.1; run tests with `.venv\Scripts\python.exe -m pytest`; lint `.venv\Scripts\python.exe -m ruff check src tests`; format `.venv\Scripts\python.exe -m black --check src tests`; types `.venv\Scripts\python.exe -m mypy`; set `$env:PYTHONPATH = 'src'` for ad-hoc runs.
- Do not commit secrets; no raw token in logs or tests.

---

### Task 1: Move shared REST helpers to `lib/gh_client.py`

`RequestFailed`, `short_message`, `next_link` are used by org-enum, since-scan, hydration, enrichment, console, and the batch engine today from `discover/search_shards.py`. Move them before deleting that module.

**Files:**
- Modify: `src/lib/gh_client.py` (append after `PartialResultsError`)
- Modify: `src/lib/graphql_batch.py:13` (import), `src/hydrate/repo_client.py:10`, `src/hydrate/tail.py:11`, `src/enrich/trees_first.py:11`, `src/discover/org_enum.py:10`, `src/discover/since_scan.py:12`, `src/serve/app.py:26`, `src/serve/runner.py:16`
- Modify: `src/discover/search_shards.py` (re-export from `lib.gh_client` until its deletion in Task 8)
- Test: `tests/unit/test_gh_client.py`

**Interfaces:**
- Produces: `lib.gh_client.RequestFailed(status: int, message: str)` (attrs `status`, `message`); `lib.gh_client.short_message(response: httpx.Response) -> str`; `lib.gh_client.next_link(header: str | None) -> str | None`.

- [ ] **Step 1: Write failing tests**

```python
def test_short_message_prefers_json_message():
    response = httpx.Response(422, json={"message": "Only the first 1000 search results are available"})
    assert short_message(response) == "Only the first 1000 search results are available"


def test_short_message_falls_back_to_body_text():
    response = httpx.Response(500, text="<html>proxy error</html>")
    assert "proxy error" in short_message(response)


def test_next_link_returns_the_rel_next_url():
    header = '<https://api.github.com/x?page=2>; rel="next", <https://api.github.com/x?page=5>; rel="last"'
    assert next_link(header) == "https://api.github.com/x?page=2"
    assert next_link(None) is None
    assert next_link("<https://api.github.com/x?page=5>; rel=\"last\"") is None


def test_request_failed_carries_status_and_message():
    error = RequestFailed(404, "Not Found")
    assert error.status == 404 and error.message == "Not Found" and str(error) == "404: Not Found"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_gh_client.py -q`
Expected: FAIL — `cannot import name 'short_message'`.

- [ ] **Step 3: Implement in `lib/gh_client.py`** (move the bodies verbatim from `discover/search_shards.py`; rename `_short_message` → `short_message`, `_next_link` → `next_link`)

```python
_BODY_FALLBACK_CHARS = 300


class RequestFailed(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


def short_message(response: httpx.Response) -> str:
    payload = None
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("message"), str):
        return payload["message"]
    return response.text[:_BODY_FALLBACK_CHARS]


def next_link(header: str | None) -> str | None:
    if not header:
        return None
    for part in header.split(","):
        segments = part.split(";")
        url_part = segments[0].strip()
        if not (url_part.startswith("<") and url_part.endswith(">")):
            continue
        for segment in segments[1:]:
            key, _, value = segment.partition("=")
            if key.strip().lower() == "rel" and value.strip().strip('"').lower() == "next":
                return url_part[1:-1]
    return None
```

- [ ] **Step 4: Update imports everywhere** — replace `from discover.search_shards import RequestFailed` with `from lib.gh_client import RequestFailed`, and `RequestFailed, _short_message` → `RequestFailed, short_message` (call sites: `_short_message(` → `short_message(`), `_next_link` → `next_link` in `org_enum.py`, `since_scan.py`. In `discover/search_shards.py`, delete the moved definitions and import them:

```python
from lib.gh_client import RequestFailed, next_link, short_message
```

(`_next_link` and `_short_message` aliases are kept in `search_shards.py` only until Task 8 deletes the file.)

- [ ] **Step 5: Run the focused and full unit suites**

Run: `.venv\Scripts\python.exe -m pytest tests/unit -q`
Expected: PASS.

- [ ] **Step 6: Lint/format/types then commit**

```bash
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m black --check src tests
.venv\Scripts\python.exe -m mypy
git add -A
git commit -m "refactor(lib): move RequestFailed/short_message/next_link to gh_client"
```

### Task 2: `src/discover/graphql_search.py` — counts, pages, mapping

**Files:**
- Create: `src/discover/graphql_search.py`
- Test: `tests/unit/test_graphql_search.py`

**Interfaces:**
- Produces:
  - `MAX_ALIASES = 20`
  - `SearchPage` frozen dataclass: `query: str`, `items: tuple[dict, ...]`, `repository_count: int`, `has_next: bool`, `end_cursor: str | None`, `exhausted: bool`, `incomplete: bool`.
  - `node_to_item(node: Mapping[str, object]) -> dict` (always returns a REST-shaped dict; invalid identity values become `None`/`0` and are skipped later by `normalize_repo`).
  - `count_queries(client, queries, *, limiter=None, token_id=None, on_response=None, sleep=time.sleep, now=time.time, jitter=None, max_attempts=3) -> list[int]`
  - `iter_pages(client, query, *, max_pages=10, limiter=None, token_id=None, on_response=None, sleep=time.sleep, now=time.time, jitter=None, max_attempts=3) -> Iterator[SearchPage]`
  - `on_response` signature for both: `(httpx.Response, float, Mapping[str, object]) -> None` (third arg = audit params).
- Consumes: `lib.gh_client.request_with_retry`, `RequestFailed`; `lib.graphql_batch.GraphQLAuthError`, `MalformedResponse`, `is_transient_error`; `lib.audit.cached_json`.

- [ ] **Step 1: Add `is_transient_error` to `lib/graphql_batch.py`**

```python
def is_transient_error(message: str) -> bool:
    """Public form of the batch engine's transient-error classifier."""
    return _is_transient(message)
```

- [ ] **Step 2: Write failing tests** (MockTransport; representative payloads)

```python
GRAPHQL_URL = "https://api.github.com/graphql"

def graphql_client(payloads, captured=None):
    iterator = iter(payloads)
    def handler(request):
        if captured is not None:
            captured.append(request)
        return next(iterator)
    return httpx.Client(transport=httpx.MockTransport(handler))


def node(**overrides):
    base = {
        "databaseId": 1, "id": "R_1", "nameWithOwner": "o/r", "name": "r",
        "description": "d", "homepageUrl": None,
        "primaryLanguage": {"name": "Rust"}, "licenseInfo": {"spdxId": "MIT"},
        "repositoryTopics": {"nodes": [{"topic": {"name": "cli"}}]},
        "visibility": "PUBLIC", "isFork": False, "parent": None,
        "isArchived": False, "isDisabled": False, "isTemplate": False, "mirrorUrl": None,
        "diskUsage": 12, "stargazerCount": 7, "forkCount": 1,
        "watchers": {"totalCount": 3}, "issues": {"totalCount": 2},
        "defaultBranchRef": {"name": "main"},
        "hasIssuesEnabled": True, "hasWikiEnabled": False, "hasProjectsEnabled": True,
        "hasDiscussionsEnabled": False, "hasPullRequestsEnabled": True,
        "owner": {"login": "o", "__typename": "User", "databaseId": 9},
        "createdAt": "2025-01-01T00:00:00Z", "pushedAt": "2025-02-01T00:00:00Z",
        "updatedAt": "2025-02-02T00:00:00Z",
    }
    base.update(overrides)
    return base


def page_payload(nodes, *, count=1, has_next=False, cursor=None, errors=None):
    body = {"data": {"s": {"repositoryCount": count,
                           "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                           "nodes": nodes}}}
    if errors is not None:
        body["errors"] = errors
    return httpx.Response(200, json=body)


def test_node_to_item_maps_rest_shape():
    item = node_to_item(node())
    assert item["id"] == 1 and item["node_id"] == "R_1" and item["full_name"] == "o/r"
    assert item["owner"] == {"id": 9, "login": "o", "type": "User"}
    assert item["license"] == {"spdx_id": "MIT"}
    assert item["topics"] == ["cli"]
    assert item["stargazers_count"] == 7 and item["forks_count"] == 1
    assert item["watchers_count"] == 3 and item["open_issues_count"] == 2
    assert item["archived"] is False and item["visibility"] == "public"
    assert item["has_pages"] is None and item["custom_properties"] == {}


def test_iter_pages_follows_cursor_and_caps_pages():
    captured = []
    payloads = [
        page_payload([node()], count=250, has_next=True, cursor="c1"),
        page_payload([node(databaseId=2)], count=250, has_next=True, cursor="c2"),
    ]
    pages = list(iter_pages(graphql_client(payloads, captured), "language:rust",
                            max_pages=2, now=lambda: 1000.0))
    assert [page.exhausted for page in pages] == [False, True]
    assert pages[0].items[0]["id"] == 1 and pages[1].items[0]["id"] == 2
    first, second = (json.loads(r.content) for r in captured)
    assert "after" not in first["query"] and '"after": "c1"' in second["query"]
    assert "first: 100" in first["query"]


def test_count_queries_batches_twenty_aliases_in_one_request():
    captured = []
    queries = [f"language:rust created:2025-01-{d:02d}" for d in range(1, 22)]
    payload = {"data": {f"s{i}": {"repositoryCount": i} for i in range(20)}}
    payload["data"]["s0"] = {"repositoryCount": 5}
    pages = [httpx.Response(200, json=payload), httpx.Response(200, json={"data": {"s0": {"repositoryCount": 9}}})]
    counts = count_queries(graphql_client(pages, captured), queries, now=lambda: 1000.0)
    assert len(captured) == 2 and counts[:20] == [5] + list(range(1, 20)) and counts[20] == 9


def test_count_queries_retries_missing_aliases():
    captured = []
    payloads = [
        httpx.Response(200, json={"data": {"s0": {"repositoryCount": 4}}}),
        httpx.Response(200, json={"data": {"s0": {"repositoryCount": 4}}}),
    ]
    counts = count_queries(graphql_client(payloads, captured), ["q"], sleep=lambda s: None,
                           now=lambda: 1000.0)
    assert counts == [4] and len(captured) == 1


def test_page_transient_error_retries_then_succeeds():
    captured = []
    payloads = [
        httpx.Response(200, json={"errors": [{"message": "resource limits exceeded"}]}),
        page_payload([node()]),
    ]
    pages = list(iter_pages(graphql_client(payloads, captured), "q", sleep=lambda s: None,
                            now=lambda: 1000.0, jitter=lambda: 0.0))
    assert len(pages) == 1 and len(captured) == 2


def test_page_partial_data_with_errors_is_incomplete_but_kept():
    payloads = [page_payload([node()], errors=[{"message": "resource limits exceeded"}])]
    page = next(iter(iter_pages(graphql_client(payloads), "q", sleep=lambda s: None)))
    assert page.incomplete is True and len(page.items) == 1


def test_unauthorized_raises_graphql_auth_error():
    payloads = [httpx.Response(401, json={"message": "Bad credentials"})]
    with pytest.raises(GraphQLAuthError):
        list(iter_pages(graphql_client(payloads), "q", now=lambda: 1000.0))


def test_on_response_receives_audit_params():
    recorded = []
    payloads = [page_payload([node()])]
    list(iter_pages(graphql_client(payloads), "q", max_pages=1, now=lambda: 1000.0,
                    on_response=lambda response, ms, params: recorded.append(params)))
    assert recorded[0]["q"] == "q" and recorded[0]["after"] is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_search.py -q`
Expected: FAIL — module not found.

- [ ] **Step 4: Implement `graphql_search.py`**

Key shape (full code in the repository after implementation; this is the contract):

```python
from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from lib import audit
from lib.deadlines import DeadlineExceededError
from lib.gh_client import RequestFailed, ThrottledError, request_with_retry
from lib.graphql_batch import GRAPHQL_URL, GraphQLAuthError, MalformedResponse, is_transient_error
from limiter.buckets import BucketLimiter

MAX_ALIASES = 20
PAGE_SIZE = 100
AuditHook = Callable[[httpx.Response, float, Mapping[str, object]], None]

_NODE_FIELDS = """..."""  # the field selection from the spec §3


@dataclass(frozen=True)
class SearchPage:
    query: str
    items: tuple[dict, ...]
    repository_count: int
    has_next: bool
    end_cursor: str | None
    exhausted: bool = False
    incomplete: bool = False


def node_to_item(node: Mapping[str, object]) -> dict: ...       # spec §3 mapping
def _page_query(query: str, after: str | None) -> str: ...
def _count_query(queries: Sequence[str]) -> str: ...


def _post(client, query_text, *, limiter, token_id, on_response, params, sleep, now, jitter) -> tuple[dict, list[str]]:
    response = request_with_retry(client, "POST", GRAPHQL_URL, json_body={"query": query_text},
                                  limiter=limiter, token_id=token_id,
                                  on_response=(None if on_response is None else
                                               (lambda resp, ms: on_response(resp, ms, params))),
                                  sleep=sleep, now=now, jitter=jitter)
    if response.status_code == 401:
        raise GraphQLAuthError("github rejected the token (HTTP 401)")
    if response.status_code != 200:
        raise RequestFailed(int(response.status_code), "graphql request failed")
    body = audit.cached_json(response)
    if body is None:
        try:
            body = response.json()
        except ValueError:
            raise MalformedResponse("graphql response is not JSON") from None
    if not isinstance(body, dict):
        raise MalformedResponse("graphql response is not an object")
    data = body.get("data")
    errors = [str(e.get("message")) for e in body.get("errors") or [] if isinstance(e, dict)]
    return (data if isinstance(data, dict) else {}), errors
```

`count_queries`: batches by `MAX_ALIASES`; retries missing aliases and transient batch errors up to `max_attempts` with `sleep(min(2 ** attempt, 30) + jitter)`; non-transient batch error or exhausted retries → `RequestFailed(200, ...)`.

`iter_pages`: loop `while pages < max_pages`; each iteration posts; if `search` missing after retries → `RequestFailed`; if `search` present and `errors` → yield with `incomplete=True` (no retry) and stop; `exhausted = has_next and pages + 1 >= max_pages`; stop when `not has_next`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_search.py -q`
Expected: PASS.

- [ ] **Step 6: Lint/format/types then commit**

```bash
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m black --check src tests
.venv\Scripts\python.exe -m mypy
git add -A
git commit -m "feat(discover): graphql search client for batched counts and parallel pages"
```

### Task 3: Batched planner `plan_shards`

**Files:**
- Modify: `src/scheduler/shard_planner.py`
- Test: `tests/unit/test_shard_planner.py` (rewrite; old cases adapted)

**Interfaces:**
- Produces: `plan_shards(query, probe_counts, *, max_fetchable=1000, scan_target=4000, min_date=date(2008,1,1), root_count=None, max_shards=10_000, batch_size=20, now=None) -> tuple[list[ShardSpec], bool]`
  - `probe_counts: Callable[[Sequence[str]], Sequence[int]]` — query strings in, counts in the same order.
  - second tuple element = `plan_capped`.
- Keeps: `ShardSpec`, `split_created`, `replace_created`, `_merge_windows`; converts `ShardPlanner._range_query` to module-level `range_query(base, start, end)`.

- [ ] **Step 1: Rewrite the tests** — keep every old behavioral case, ported to `probe_counts` over query strings:

```python
def probe_from(count_fn):
    calls = []
    def probe(queries):
        calls.append(list(queries))
        return [count_fn(q) for q in queries]
    return probe, calls
```

Assertions to keep: zero root → `([], False)`; under-limit root → one unbounded spec; over-limit → gap-free sorted date leaves with matching `RANGE_RE` and UTC bounds; deterministic; `root_count` override skips probing the root query (`"q" not in all_probed_queries`); scan target; single-day oversized; empty-range `ValueError`; closed range stays inside; disjoint windows each inside their window; `replace_created`/`split_created` unchanged.
New assertions: a 41-window workload probes in batches of ≤ 20 (`max(len(batch) for batch in calls) <= 20`); `max_shards=3` on an overflowing query returns exactly 3 oldest leaves with `plan_capped is True`; exceptions from `probe_counts` propagate.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_shard_planner.py -q`
Expected: FAIL — `plan_shards` not defined.

- [ ] **Step 3: Implement `plan_shards`** (replace the `ShardPlanner` class; keep helpers)

```python
def plan_shards(query, probe_counts, *, max_fetchable=1000, scan_target=4000,
                min_date=date(2008, 1, 1), root_count=None, max_shards=10_000,
                batch_size=20, now=None) -> tuple[list[ShardSpec], bool]:
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    current = now if now is not None else datetime.now(UTC)
    ceiling = current.date()
    if ceiling < min_date:
        raise ValueError("min_date is after now; there is no window to bisect")
    root = root_count if root_count is not None else probe_counts([query])[0]
    if root == 0:
        return [], False
    if _fetchable(root, max_fetchable, scan_target):
        return [ShardSpec(query=query, range_start=None, range_end=None, total_count=root)], False
    base, windows = split_created(query)
    if not windows:
        windows = [(None, None)]
    merged = _merge_windows(windows, floor=min_date, ceiling=ceiling)
    use_root_for_single = root_count is not None and len(merged) == 1
    pending: deque[tuple[date, date]] = deque(merged)
    leaves: list[ShardSpec] = []
    plan_capped = False
    while pending and not plan_capped:
        level = [pending.popleft() for _ in range(min(batch_size, len(pending)))]
        if use_root_for_single and len(level) == 1 and level[0] == merged[0] and level[0][0] != level[0][1]:
            counts = [root]
        else:
            counts = list(probe_counts([range_query(base, s, e) for (s, e) in level]))
        for (start, end), count in zip(level, counts, strict=True):
            if count == 0:
                continue
            if _fetchable(count, max_fetchable, scan_target):
                leaves.append(_leaf(base, start, end, count, oversized=False))
            elif start == end:
                leaves.append(_leaf(base, start, end, count, oversized=True))
            else:
                middle = start + (end - start) // 2
                pending.append((start, middle))
                pending.append((middle + timedelta(days=1), end))
        if len(leaves) >= max_shards:
            plan_capped = True
    leaves.sort(key=lambda spec: (spec.range_start or datetime.min.replace(tzinfo=UTC)))
    if plan_capped:
        leaves = leaves[:max_shards]
    return leaves, plan_capped
```

- [ ] **Step 4: Run tests; then grep for leftover users**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_shard_planner.py -q`
Then: `rg "ShardPlanner" src tests` — expected hits only in `discover/pipeline.py` (rewritten in Task 5) and tests still importing the class; leave those until their tasks and note them.

- [ ] **Step 5: Commit**

```bash
git add src/scheduler/shard_planner.py tests/unit/test_shard_planner.py
git commit -m "feat(scheduler): level-batched graphql shard planner"
```

### Task 4: `discovery_concurrency` setting (migration 0012)

**Files:**
- Create: `migrations/versions/0012_discovery_concurrency.py`
- Modify: `src/store/models.py` (AppSettings), `src/store/settings.py`, `src/serve/settings_spec.py`, `src/serve/runner.py` (`RunnerConfig` + mapping), `src/serve/templates/settings.html`, `tests/conftest.py` (table list if any), `docs/environment.md`, `design/run-limits.md`
- Test: `tests/unit/test_run_settings.py`, `tests/unit/test_settings_spec.py`, `tests/contract/test_settings.py`, `tests/integration/test_models_migrations.py`

**Interfaces:**
- Produces: `RunSettings.discovery_concurrency: int = 32`; `RunnerConfig.discovery_concurrency: int = 32`; env `GITCRAWL_DISCOVERY_CONCURRENCY`; bounds (1, 64).

- [ ] **Step 1: Write failing tests** — follow the existing patterns exactly (look at the current `test_run_settings.py`, `test_settings_spec.py`, `test_settings.py` for the `max_shards` cases and clone them for `discovery_concurrency`): defaults 32; env override; bounds reject 0 and 65; form round-trips; corpus preset applies 32; migration head `0012` and downgrade chain passes.

- [ ] **Step 2: Run to fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_run_settings.py tests/unit/test_settings_spec.py -q`
Expected: FAIL — unknown field.

- [ ] **Step 3: Implement** — migration:

```python
def upgrade() -> None:
    op.add_column("app_settings", sa.Column("discovery_concurrency", sa.Integer(), nullable=False, server_default=sa.text("32")))
    op.create_check_constraint("app_settings_discovery_concurrency", "app_settings", "discovery_concurrency BETWEEN 1 AND 64")


def downgrade() -> None:
    op.drop_constraint("app_settings_discovery_concurrency", "app_settings", type_="check")
    op.drop_column("app_settings", "discovery_concurrency")
```

Model: column + `CheckConstraint("discovery_concurrency BETWEEN 1 AND 64", name="app_settings_discovery_concurrency")`. `RunSettings` + `SETTINGS_ENV` + `_BOUNDS` + `_CORPUS_VALUES` + `RunnerConfig`/`runner_config_from` + settings template row (`number_field("discovery_concurrency", "Discovery workers")`). Docs tables get the field with default 32, bounds 1–64, env var; `run-limits.md` notes discovery's derivation (measured operating point, one token).

- [ ] **Step 4: Run the settings/migration suites**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_run_settings.py tests/unit/test_settings_spec.py tests/contract/test_settings.py tests/integration/test_models_migrations.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(settings): discovery_concurrency (default 32) with migration 0012"
```

### Task 5: Pipeline rewrite — GraphQL planning + parallel executor

**Files:**
- Modify: `src/discover/pipeline.py:161-506` (`count_total`, `run_search_discovery`)
- Modify: `src/serve/runner.py:763-842` (pass `discovery_concurrency`, drop `queue_prefix`), `src/serve/app.py:346-420` (drop `_shard_queue_prefix`, its call, and the queue test), `src/serve/metrics.py` (drop `_queue_pel`; Task 7 covers copy/SLO)
- Test: `tests/integration/test_pipeline.py` (rewrite discovery cases), `tests/contract/test_github_pagination.py` (delete — replaced), `tests/integration/test_golden_org.py` (adapt), `tests/integration/test_lifecycle.py:270`, `tests/unit/test_app_singletons.py` (drop queue-prefix test)

**Interfaces:**
- Produces: `run_search_discovery(deps, query, *, per_page=100, max_pages=10, max_shards=100, total_count=None, discovery_concurrency=32, sleep=time.sleep, now=time.time, jitter=None) -> DiscoveryStats`; `DiscoveryStats` without `deferred_shards`/`cap_splits`; `count_total(deps, query, *, on_response=None, sleep, now, jitter) -> int` (GraphQL).
- Consumes: `plan_shards`, `graphql_search.count_queries`, `graphql_search.iter_pages`, `ShardStore`, `upsert_repos`, `progress`, `cancellation`.

- [ ] **Step 1: Rewrite the discovery integration tests** — MockTransport GraphQL responses and the existing `clean` DB fixture; cases:
  - two shards fetched concurrently (payloads for shard A and B), ids deduped, shards `done`, `stats.pages == 2`;
  - planner target 300 for `max_pages=3` (assert created shard queries carry `created:` windows and counts);
  - `max_pages` cap: a shard payload with `hasNextPage=True` at the cap → `page_capped_shards == 1`, `incomplete_shards == 1`;
  - 200-with-transient-errors page retried then succeeds; after exhaustion → `incomplete_shards == 1`, run continues;
  - deadline: a fake limiter deadline already expired → shards stay `PENDING`, `deadline_hit is True`;
  - cancellation mid-page → shards stay `PENDING`, `RunCancelled` propagates;
  - `plan_capped` when `max_shards` < leaves;
  - `test_run_filter_and_pipeline_share_count_total` (runner) keeps working; `test_find_matches` count monkeypatch keeps working.
- Delete `tests/contract/test_github_pagination.py`; in `test_golden_org.py` replace `iter_shard_pages` with `graphql_search.iter_pages` (same loop shape: `page.items`); in `test_lifecycle.py` swap the `iter_shard_pages` assertion for an equivalent `iter_pages` assertion.

- [ ] **Step 2: Run to fail**

Run: `.venv\Scripts\python.exe -m pytest tests/integration/test_pipeline.py -q`
Expected: FAIL — GraphQL payloads not understood by the old REST pipeline.

- [ ] **Step 3: Implement the new `count_total` and `run_search_discovery`**

```python
def count_total(deps, query, *, on_response=None, sleep=time.sleep, now=time.time, jitter=None) -> int:
    hook = _graphql_audit_hook(deps) if on_response is None else on_response
    return count_queries(deps.client, [query], limiter=deps.limiter, token_id=deps.token_fp,
                         on_response=hook, sleep=sleep, now=now, jitter=jitter)[0]
```

`run_search_discovery`:

```python
def run_search_discovery(deps, query, *, per_page=100, max_pages=10, max_shards=100,
                         total_count=None, discovery_concurrency=32,
                         sleep=time.sleep, now=time.time, jitter=None) -> DiscoveryStats:
    stats = DiscoveryStats()
    hook = _graphql_audit_hook(deps)
    store = ShardStore(deps.engine)

    def probe_counts(queries):
        return count_queries(deps.client, queries, limiter=deps.limiter, token_id=deps.token_fp,
                             on_response=hook, sleep=sleep, now=now, jitter=jitter)

    target = min(1000, max(1, per_page * max_pages))
    if total_count is not None and total_count > max_shards * target:
        target = 1000
    cancellation.check()
    leaves, plan_capped = plan_shards(query, probe_counts, max_fetchable=target,
                                      root_count=total_count, max_shards=max_shards, now=now())
    stats.plan_capped = plan_capped
    shard_ids = [store.create(spec) for spec in leaves]
    stats.shards = len(shard_ids)
    progress.report("discovering", 0, stats.shards, **_progress_counters(stats, total_count))

    lock = threading.Lock()
    stop = threading.Event()
    worker = _Worker(deps, stats, lock, stop, max_pages=max_pages, query_hook=hook, sleep=sleep, now=now, jitter=jitter)

    def run_one(shard_id):
        try:
            worker.process(shard_id)
        finally:
            with lock:
                pass

    with ThreadPoolExecutor(max_workers=discovery_concurrency) as pool:
        futures = {pool.submit(contextvars.copy_context().run, worker.process, sid): sid for sid in shard_ids}
        for future in as_completed(futures):
            exc = future.exception()
            if exc is not None:
                pool.shutdown(wait=False, cancel_futures=True)
                raise exc
            progress.report("discovering", ..., stats.shards, **_progress_counters(stats, total_count))
    stats.repo_ids = tuple(worker.collected_ids)
    _flush_audit(deps)
    _log_summary("search", stats)
    return stats
```

The worker (`_Worker` class in `pipeline.py`) holds: `process(shard_id)` → get row, skip `done/incomplete`, `pending→active`, iterate `iter_pages`; per page: cancellation and deadline checks (deadline → `set_state(PENDING)`, `stop.set()`, `stats.deadline_hit=True`, return), writer-lock `upsert_repos` + id collection + `stats.pages/fetched` folding; `page.exhausted` → `page_capped_shards += 1`, `incomplete=True`; `page.incomplete` → `incomplete=True`; fetched < repository_count with no next → `incomplete=True`; final `DONE`/`INCOMPLETE`. Transient exceptions (`RequestFailed`, `ThrottledError`, `PartialResultsError`, `httpx.HTTPError`) after the page retries → shard `INCOMPLETE`, `stats.incomplete_shards += 1`; `RunCancelled` → shard `PENDING`, re-raise; anything else → shard `INCOMPLETE`, re-raise. No queue, no deferral.

- [ ] **Step 4: Run the pipeline, runner, lifecycle, golden-org suites**

Run: `.venv\Scripts\python.exe -m pytest tests/integration/test_pipeline.py tests/integration/test_runner.py tests/integration/test_lifecycle.py tests/unit/test_app_singletons.py -q`
Expected: PASS (golden-org is live and network-gated; run it in Task 9).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(discover): graphql discovery engine with parallel shard executor"
```

### Task 6: Delete the queue and the REST search module

**Files:**
- Delete: `src/discover/search_shards.py`, `tests/contract/test_github_pagination.py`, `tests/unit/test_reclaim_wiring.py`, `tests/unit/test_state_machine.py` queue cases
- Modify: `src/scheduler/state_machine.py` (remove `ShardQueue`, `QueuedShard`, `RetryOutcome`, Redis imports), `src/serve/metrics.py`, `src/serve/templates/system.html`, `src/serve/templates/partials/metrics_cards.html`, `tests/unit/test_metrics.py`, `tests/golden/snapshots/api_metrics.json`
- Test: full suite

- [ ] **Step 1:** Remove queue code and tests; `rg "ShardQueue|QueuedShard|RetryOutcome|iter_shard_pages|SearchCapExceeded|queue_prefix|_shard_queue_prefix|deferred_shards|cap_splits" src tests` must come back empty after the removals.
- [ ] **Step 2:** Update `test_metrics.py` to the payload without `queue` (or with the Task 7 shape if already landed; order tasks so this file is touched once).
- [ ] **Step 3:** Run `pytest tests/unit tests/contract -q`; regenerate golden snapshots as needed (`UPDATE_GOLDEN=1 pytest tests/golden`).
- [ ] **Step 4:** Commit as `refactor(discover): delete redis shard queue and REST search paging`.

### Task 7: Audit total_count override, GraphQL SLO, metrics copy

**Files:**
- Modify: `src/lib/audit.py` (`record_from_response(..., total_count=None)`), `_AUDIT_WINDOW_SQL` + `SloSnapshot` (`graphql_remaining`), `src/serve/metrics.py` thresholds/labels, templates, golden snapshot
- Test: `tests/unit/test_audit.py`, `tests/unit/test_metrics.py`, contract page tests, `test_pipeline` audit assertions

- [ ] **Step 1: Failing tests** — `record_from_response(..., total_count=42)` records 42; SLO reads latest `rl_resource='graphql'` remaining into `graphql_remaining`; `/metrics` payload and pages carry `graphql_remaining` and no `queue`.
- [ ] **Step 2: Implement**; the pipeline GraphQL audit hook parses `data.s.repositoryCount` (page alias `s`; count batches use `s0`) and passes it as the override.
- [ ] **Step 3: Run `pytest -q`; `UPDATE_GOLDEN=1 pytest tests/golden`; commit.**

### Task 8: Docs synced with the new engine

**Files:**
- Modify: `README.md` (discovery description lines), `design/how-the-data-flows.md:40`, `design/run-limits.md` (§4 formula: GraphQL discovery time, points), `docs/environment.md` (settings table + run profile), `docs/development-log.md` (dated session entry with the measured ledger and rulings: queue deleted, max_pages kept, one-token policy)
- Test: doc consistency only (no code)

- [ ] **Step 1:** Update each doc in the repo's human language; keep the §9.4 measurement as the cited evidence.
- [ ] **Step 2:** Commit as `docs: sync discovery docs with the graphql engine`.

### Task 9: Live validation + hydration profile

**Files:**
- Create: `docs/findings/2026-10-08-hydration-profile.md`
- Scratch (not committed): a temp runner script under `%TEMP%\opencode`

- [ ] **Step 1: Live shard-and-fetch validation** — run `run_search_discovery` for `language:rust stars:>=4 created:>=2025-02-24` (corpus `max_shards=1000`, `discovery_concurrency=32`) against the real API and DB; record wall time, shards, pages, unique ids; compare with `gql_spike_ids.txt` (expect ~99.9% overlap, residual = drift) and assert ~< 2 minutes.
- [ ] **Step 2: Hydration profile** — run `run_filter` end-to-end with corpus settings and per-stage timers around `count`/discovery/`_hydrate`/enrich/writes; wrap `refresh_repos_batched` in `cProfile` (or a dedicated harness); capture top cumulative functions and repos/minute.
- [ ] **Step 3: Write `docs/findings/2026-10-08-hydration-profile.md`** — measured stage table, cProfile top-10, the ranked next bottleneck, and 2–3 concrete fix proposals (no code changes).
- [ ] **Step 4: Commit** `docs(findings): graphql discovery validation and hydration profile`.

## Self-review notes

- Spec coverage: §2 (Tasks 2–5), §3 (Task 2), §4 (Task 2 + 5 worker), §5 (Task 4), §6 (Task 7), §7 (Tasks 5–6), §8 (Tasks 1–6 tests), §9 (Task 9), §10 (Task 9 step 1 validates live; concurrency is the watchpoint), §11 respected (since/org untouched; no filter-spec changes).
- One ordering dependency: delete `search_shards.py` only in Task 6, after org/since imports moved (Task 1) and pipeline no longer imports it (Task 5).
- `test_metrics.py` is touched in Tasks 6 and 7; if executed back-to-back the second edit is small (SLO field only). Acceptable.
