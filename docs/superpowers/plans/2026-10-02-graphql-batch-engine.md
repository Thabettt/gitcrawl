# GraphQL Batch Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a generic GraphQL batch core plus three adapters (repo details, file presence, owner location) so corpus building batches 20 repos per request on a dedicated meter, isolates per-repo failures so one bad repo can never discard its neighbours, and reports every outcome — never stalling, never losing good data, never silent.

**Architecture:** `src/lib/graphql_batch.py` owns the hard guarantees: alias generation, request send, parse-data-first, per-repo error attribution by GraphQL error `path`, bounded split/requeue of only failed keys, REST fallback for anything a batch cannot resolve, deadline checks, and audited outcomes. Three thin adapters supply only a query builder and a parser: repo details in `src/hydrate/graphql_repo.py`, file presence and owner location in `src/enrich/`. The existing one-by-one REST paths remain as fallbacks and keep the ETag/"nothing changed" freebie. A dedicated `graphql` limiter bucket (5,000 points/hour) rides beside `search`/`core`.

**Tech Stack:** Python 3.12, httpx, SQLAlchemy 2 + Postgres, Redis bucket limiter, pytest, ruff, black.

**Spec:** `design/corpus-building-efficient-engineering.md` §9–§10 (the agreed design), `design/landscape-comparison.md` (why), `gitcrawl-vs-seart.md` §4.3 (file channel → detection bridge).

## Global Constraints

- Python `>=3.12`; line length 100; ruff (`E,F,I,UP,B`) + black clean; coverage floor `fail_under = 93` (`pyproject.toml`).
- DB tests require `TEST_DATABASE_URL` ending in `_test` (or `GITCRAWL_REQUIRE_TEST_DB=1`); use `tests/conftest.py` fixtures (`clean_db` is a factory).
- Run tests with: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest <path> -q` (Windows PowerShell).
- GraphQL batches are capped at **20 aliases per query** (resource limits; spec FR-007 says ≤10–20).
- Batch requests are **sequential**; no concurrent GraphQL calls (GitHub best practice, and the point budget is the binding limit).
- Good data in a mixed response is **always kept**; only repos named in errors are retried; one repo's failure can never discard another repo's result.
- Every repo ends in a known state: `values` (saved), `fallbacks` (saved via REST), `handled` (fallback legitimately had nothing, e.g. tombstone/304), or `unresolved` (with a reason). Any unresolved repo → run marked partial via warnings; never silent.
- Auth failures (`HTTP 401`, `Bad credentials`) and SSO `partial-results` **abort the run loudly**; they are never treated as per-repo failures.
- All commits: conventional prefixes matching repo style (`feat:`, `test:`, `fix:`, `docs:`).
- No raw tokens in logs, bundles, or requests added by this plan; the httpx client already carries auth.

---

## File Structure

**Create**

- `src/lib/graphql_batch.py` — generic batch core: `ParsedBatch`, `GraphQLAdapter`, `BatchStats`, `BatchOutcome`, `fetch_batch`, `GraphQLAuthError`.
- `src/hydrate/graphql_repo.py` — `RepoDetails` + `RepoDetailsAdapter` (GraphQL node → REST-shaped payload ready for `upsert_repos`).
- `src/enrich/graphql_file_presence.py` — `FilePresenceAdapter(path)` (one `object(expression:)` per repo).
- `src/enrich/graphql_owner_location.py` — `OwnerLocationAdapter(login → account type)`.
- `tests/unit/test_graphql_batch_core.py` — happy path, attribution, splits, fallback, deadline, auth.
- `tests/unit/test_graphql_repo_adapter.py`
- `tests/unit/test_graphql_file_presence_adapter.py`
- `tests/unit/test_graphql_owner_location_adapter.py`
- `tests/integration/test_hydrate_batch.py` — DB-backed batched hydration + fallback + unresolved.

**Modify**

- `src/limiter/buckets.py` — add `"graphql": (5000, 3600.0)` to `RESOURCE_SPECS`.
- `src/lib/gh_client.py` — `resource_for_url` maps `/graphql` → `"graphql"`.
- `src/hydrate/tail.py` — `RefreshStats` gains batch counters; new `refresh_repos_batched(...)`; existing `refresh_repos` untouched (REST/ETag path stays).
- `src/serve/runner.py` — `_hydrate` uses the batched path and returns stats; `_apply_dockerfile` and `_apply_geo` batch; `_load_owners` selects `Owner.type`; `run_filter` reports batch outcomes in `field_stats["graphql"]` and warnings.
- `tests/unit/test_buckets.py`, `tests/unit/test_gh_client.py` — new meters.
- `tests/integration/test_runner.py` — GraphQL-aware mock helper; updated hydration/tree assertions; new isolation and unresolved tests.
- `docs/development-log.md` — outcome + coverage entry (final task).
- `design/corpus-building-efficient-engineering.md` — §10 status note flips from "not built" to built (final task).

---

## Phase 1 — Meter and core engine

### Task 1: GraphQL rate bucket and resource mapping

**Files:**
- Modify: `src/limiter/buckets.py:9-13`
- Modify: `src/lib/gh_client.py:53-59`
- Test: `tests/unit/test_buckets.py`, `tests/unit/test_gh_client.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `RESOURCE_SPECS["graphql"] == (5000, 3600.0)`; `resource_for_url("https://api.github.com/graphql") == "graphql"`. Later tasks rely on both.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_buckets.py`:

```python
def test_graphql_bucket_spec():
    from limiter.buckets import RESOURCE_SPECS

    assert RESOURCE_SPECS["graphql"] == (5000, 3600.0)
```

Append to `tests/unit/test_gh_client.py`:

```python
def test_resource_for_graphql():
    from lib.gh_client import resource_for_url

    assert resource_for_url("https://api.github.com/graphql") == "graphql"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_buckets.py tests/unit/test_gh_client.py -q`
Expected: FAIL (`KeyError: 'graphql'` / assertion).

- [ ] **Step 3: Implement**

In `src/limiter/buckets.py`, extend `RESOURCE_SPECS`:

```python
RESOURCE_SPECS: dict[str, tuple[int, float]] = {
    "search": (30, 60.0),
    "core": (5000, 3600.0),
    "code_search": (10, 60.0),
    "graphql": (5000, 3600.0),
}
```

In `src/lib/gh_client.py`, extend `resource_for_url`:

```python
def resource_for_url(url: str) -> str:
    path = urlparse(url).path
    if path == "/search/repositories":
        return "search"
    if path == "/search/code":
        return "code_search"
    if path == "/graphql":
        return "graphql"
    return "core"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_buckets.py tests/unit/test_gh_client.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/limiter/buckets.py src/lib/gh_client.py tests/unit/test_buckets.py tests/unit/test_gh_client.py
git commit -m "feat: graphql rate bucket and resource mapping"
```

---

### Task 2: Generic batch core — parse first, attribute per repo

**Files:**
- Create: `src/lib/graphql_batch.py`
- Test: `tests/unit/test_graphql_batch_core.py`

**Interfaces:**
- Consumes: `resource_for_url`/`request_with_retry` (Task 1), `Deadline` (`src/lib/deadlines.py`), `BucketLimiter`.
- Produces (used by Tasks 4, 6, 7):
  - `GRAPHQL_URL: str`, `MAX_BATCH_SIZE = 20`
  - `class GraphQLAuthError(RuntimeError)`
  - `class MalformedResponse(ValueError)`
  - `@dataclass(frozen=True) class ParsedBatch[T]: values: Mapping[str, T]; failures: Mapping[str, str]`
  - `class GraphQLAdapter[T](Protocol): name: str; batch_size: int; build_query(aliases) -> str; parse(payload, aliases) -> ParsedBatch[T]`
  - `@dataclass class BatchStats: keys, requests, values, fallbacks, handled, unresolved, requeues, deadline_hit` + `as_dict()`
  - `@dataclass(frozen=True) class BatchOutcome[T]: values: Mapping[str, T]; unresolved: Mapping[str, str]; stats: BatchStats`
  - `fetch_batch(adapter, keys, *, client, limiter=None, token_id=None, fallback=None, deadline=None, on_response=None, sleep=time.sleep, now=time.time, jitter=None, max_attempts=3) -> BatchOutcome[T]`
  - Alias convention: `aliases = {f"n{i}": key for i, key in enumerate(ordered_unique_keys)}`; adapters build queries from the alias map and parse against it.

- [ ] **Step 1: Write the failing test (happy path, attribution, missing)**

Create `tests/unit/test_graphql_batch_core.py`:

```python
from __future__ import annotations

import json
import re
from collections.abc import Mapping

import httpx

from lib.graphql_batch import ParsedBatch, fetch_batch

ALIAS_RE = re.compile(r"(n\d+): field")


class DictAdapter:
    name = "dict"
    batch_size = 20

    def build_query(self, aliases: Mapping[str, str]) -> str:
        return "query { " + " ".join(f"{alias}: field" for alias in aliases) + " }"

    def parse(self, payload: Mapping[str, object], aliases: Mapping[str, str]) -> ParsedBatch[str]:
        data = payload.get("data")
        values: dict[str, str] = {}
        if isinstance(data, dict):
            for alias, node in data.items():
                key = aliases.get(alias)
                if key is None or not isinstance(node, str):
                    continue
                values[key] = node
        return ParsedBatch(values=values)


def client_from(responses, recorder=None):
    iterator = iter(responses)

    def handler(request):
        if recorder is not None:
            recorder.append(request)
        return next(iterator)

    return httpx.Client(transport=httpx.MockTransport(handler))


def node_payload(keys, *, prefix="value-"):
    data = {"rateLimit": {"cost": 1, "remaining": 4999}}
    for index, key in enumerate(keys):
        data[f"n{index}"] = f"{prefix}{key}"
    return {"data": data}


def test_fetch_batch_single_request_maps_all_values():
    captured = []
    client = client_from([httpx.Response(200, json=node_payload(["1", "2", "3"]))], captured)
    outcome = fetch_batch(DictAdapter(), ["1", "2", "3"], client=client)
    assert outcome.values == {"1": "value-1", "2": "value-2", "3": "value-3"}
    assert outcome.unresolved == {}
    assert outcome.stats.as_dict()["requests"] == 1
    assert len(captured) == 1
    query = json.loads(captured[0].content)["query"]
    assert ALIAS_RE.findall(query) == ["n0", "n1", "n2"]


def test_fetch_batch_keeps_good_results_when_one_alias_errors():
    payload = node_payload(["1", "2"])
    payload["data"]["n1"] = None
    payload["errors"] = [
        {"message": "Could not resolve to a node", "path": ["n1"]},
    ]
    captured = []
    client = client_from([httpx.Response(200, json=payload)], captured)
    outcome = fetch_batch(
        DictAdapter(),
        ["1", "2"],
        client=client,
        fallback=lambda key: f"fallback-{key}",
    )
    assert outcome.values == {"1": "value-1", "2": "fallback-2"}
    assert outcome.unresolved == {}
    assert outcome.stats.fallbacks == 1
    assert len(captured) == 1  # per-repo errors never trigger a split


def test_fetch_batch_missing_alias_goes_to_fallback():
    client = client_from([httpx.Response(200, json=node_payload(["1"]))])
    outcome = fetch_batch(
        DictAdapter(),
        ["1", "2"],
        client=client,
        fallback=lambda key: f"fallback-{key}",
    )
    assert outcome.values == {"1": "value-1", "2": "fallback-2"}
    assert outcome.stats.fallbacks == 1


def test_fetch_batch_never_discards_good_data_when_errors_and_data_share_a_reply():
    payload = node_payload(["1", "2", "3"])
    payload["data"]["n1"] = None
    payload["errors"] = [
        {"message": "Could not resolve to a node", "path": ["n1"]},
    ]
    client = client_from([httpx.Response(200, json=payload)])
    seen_fallbacks = []

    def fallback(key):
        seen_fallbacks.append(key)
        return None

    outcome = fetch_batch(DictAdapter(), ["1", "2", "3"], client=client, fallback=fallback)
    assert outcome.values == {"1": "value-1", "3": "value-3"}
    assert seen_fallbacks == ["2"]
    assert outcome.stats.values == 2
    assert outcome.stats.handled == 1


def test_fetch_batch_without_keys_makes_no_request():
    def handler(request):
        raise AssertionError("no request expected")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(DictAdapter(), [], client=client)
    assert outcome.values == {}
    assert outcome.stats.as_dict()["keys"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_batch_core.py -q`
Expected: FAIL (`ModuleNotFoundError: lib.graphql_batch`).

- [ ] **Step 3: Implement `src/lib/graphql_batch.py` (core, full file)**

```python
from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from discover.search_shards import RequestFailed
from lib.deadlines import Deadline, DeadlineExceededError
from lib.gh_client import PartialResultsError, ThrottledError, request_with_retry
from limiter.buckets import BucketLimiter

GRAPHQL_URL = "https://api.github.com/graphql"
MAX_BATCH_SIZE = 20
DEFAULT_MAX_ATTEMPTS = 3
_TRANSIENT_MARKERS = (
    "timeout",
    "resource limits",
    "something went wrong",
    "try again",
    "temporarily",
)


class GraphQLAuthError(RuntimeError):
    """GitHub rejected the credentials; the whole run must fail loudly."""


class MalformedResponse(ValueError):
    """The GraphQL endpoint returned something that is not a JSON object."""


@dataclass(frozen=True)
class ParsedBatch[T]:
    values: Mapping[str, T] = field(default_factory=dict)
    failures: Mapping[str, str] = field(default_factory=dict)


class GraphQLAdapter[T](Protocol):
    name: str
    batch_size: int

    def build_query(self, aliases: Mapping[str, str]) -> str: ...

    def parse(
        self, payload: Mapping[str, object], aliases: Mapping[str, str]
    ) -> ParsedBatch[T]: ...


@dataclass
class BatchStats:
    keys: int = 0
    requests: int = 0
    values: int = 0
    fallbacks: int = 0
    handled: int = 0
    unresolved: int = 0
    requeues: int = 0
    deadline_hit: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "keys": self.keys,
            "requests": self.requests,
            "values": self.values,
            "fallbacks": self.fallbacks,
            "handled": self.handled,
            "unresolved": self.unresolved,
            "requeues": self.requeues,
            "deadline_hit": self.deadline_hit,
        }


@dataclass(frozen=True)
class BatchOutcome[T]:
    values: Mapping[str, T]
    unresolved: Mapping[str, str]
    stats: BatchStats


def _alias_map(keys: Sequence[str]) -> dict[str, str]:
    return {f"n{index}": key for index, key in enumerate(keys)}


def _chunks(keys: Sequence[str], size: int) -> list[list[str]]:
    return [list(keys[start : start + size]) for start in range(0, len(keys), size)]


def _error_entries(payload: Mapping[str, object]) -> list[tuple[str, tuple[str, ...]]]:
    raw = payload.get("errors")
    if not isinstance(raw, list):
        return []
    entries: list[tuple[str, tuple[str, ...]]] = []
    for error in raw:
        if not isinstance(error, dict):
            continue
        message = error.get("message")
        text = message if isinstance(message, str) else "unknown graphql error"
        raw_path = error.get("path")
        path = tuple(str(part) for part in raw_path) if isinstance(raw_path, list) else ()
        entries.append((text, path))
    return entries


def _is_transient(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


def _post(
    adapter: GraphQLAdapter,
    keys: Sequence[str],
    *,
    client: httpx.Client,
    limiter: BucketLimiter | None,
    token_id: str | None,
    on_response: Callable[[httpx.Response, float], None] | None,
    sleep: Callable[[float], None],
    now: Callable[[], float],
    jitter: Callable[[], float] | None,
) -> tuple[ParsedBatch, tuple[str, ...]]:
    aliases = _alias_map(keys)
    response = request_with_retry(
        client,
        "POST",
        GRAPHQL_URL,
        json_body={"query": adapter.build_query(aliases)},
        limiter=limiter,
        token_id=token_id,
        on_response=on_response,
        sleep=sleep,
        now=now,
        jitter=jitter,
    )
    if response.status_code == 401:
        raise GraphQLAuthError("github rejected the token (HTTP 401)")
    if response.status_code != 200:
        raise RequestFailed(int(response.status_code), "graphql request failed")
    try:
        payload = response.json()
    except ValueError:
        raise MalformedResponse("graphql response is not JSON") from None
    if not isinstance(payload, dict):
        raise MalformedResponse("graphql response is not an object")
    parsed = adapter.parse(payload, aliases)
    values = dict(parsed.values)
    failures = dict(parsed.failures)
    batch_errors: list[str] = []
    aliases_by_name = {alias: key for alias, key in aliases.items()}
    for message, path in _error_entries(payload):
        if path and path[0] in aliases_by_name:
            key = aliases_by_name[path[0]]
            if key not in values and key not in failures:
                failures[key] = message
        else:
            batch_errors.append(message)
    return ParsedBatch(values=values, failures=failures), tuple(batch_errors)


def fetch_batch(
    adapter: GraphQLAdapter,
    keys: Sequence[str],
    *,
    client: httpx.Client,
    limiter: BucketLimiter | None = None,
    token_id: str | None = None,
    fallback: Callable[[str], object | None] | None = None,
    deadline: Deadline | None = None,
    on_response: Callable[[httpx.Response, float], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> BatchOutcome:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    size = adapter.batch_size
    if not 1 <= size <= MAX_BATCH_SIZE:
        raise ValueError(f"adapter batch_size must be 1..{MAX_BATCH_SIZE}")
    unique = list(dict.fromkeys(keys))
    stats = BatchStats(keys=len(unique))
    values: dict[str, object] = {}
    unresolved: dict[str, str] = {}
    if not unique:
        return BatchOutcome(values=values, unresolved=unresolved, stats=stats)
    attempts = dict.fromkeys(unique, 0)
    queue: deque[list[str]] = deque(_chunks(unique, size))

    def fall_back(key: str, reason: str) -> None:
        if fallback is None:
            unresolved[key] = reason
            return
        try:
            result = fallback(key)
        except (GraphQLAuthError, PartialResultsError):
            raise
        except Exception as exc:  # a fallback failure becomes a typed unresolved repo
            unresolved[key] = f"{reason}; fallback failed: {type(exc).__name__}: {exc}"
            return
        if result is None:
            stats.handled += 1
        else:
            values[key] = result
            stats.fallbacks += 1

    def requeue(failed: list[str]) -> list[str]:
        fresh: list[str] = []
        exhausted: list[str] = []
        for key in failed:
            if attempts[key] >= max_attempts:
                exhausted.append(key)
            else:
                attempts[key] += 1
                fresh.append(key)
        if fresh:
            stats.requeues += 1
            if len(fresh) > 1:
                middle = len(fresh) // 2
                queue.appendleft(fresh[:middle])
                queue.appendleft(fresh[middle:])
            else:
                queue.appendleft(fresh)
        return exhausted

    while queue:
        chunk = queue.popleft()
        pending = [key for key in chunk if key not in values and key not in unresolved]
        if not pending:
            continue
        if deadline is not None and deadline.remaining <= 0:
            stats.deadline_hit = True
            for key in pending:
                unresolved[key] = "run deadline exceeded"
            continue
        stats.requests += 1
        try:
            parsed, batch_errors = _post(
                adapter,
                pending,
                client=client,
                limiter=limiter,
                token_id=token_id,
                on_response=on_response,
                sleep=sleep,
                now=now,
                jitter=jitter,
            )
        except (GraphQLAuthError, PartialResultsError):
            raise
        except DeadlineExceededError:
            stats.deadline_hit = True
            for key in pending:
                unresolved[key] = "run deadline exceeded"
            continue
        except (RequestFailed, MalformedResponse, ThrottledError, httpx.HTTPError) as exc:
            parsed = ParsedBatch()
            batch_errors = (f"{type(exc).__name__}: {exc}",)
        for key, value in parsed.values.items():
            if key in pending:
                values[key] = value
        failed = [key for key in pending if key not in values]
        if batch_errors:
            reason = batch_errors[0]
            if _is_transient(reason):
                for key in requeue(failed):
                    fall_back(key, reason)
            else:
                for key in failed:
                    fall_back(key, reason)
        else:
            for key in failed:
                fall_back(key, parsed.failures.get(key, "missing result"))
    stats.values = len(values)
    stats.unresolved = len(unresolved)
    return BatchOutcome(values=values, unresolved=unresolved, stats=stats)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_batch_core.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/lib/graphql_batch.py tests/unit/test_graphql_batch_core.py
git commit -m "feat: generic graphql batch core with per-repo attribution"
```

---

### Task 3: Failure isolation — splits, bounded retries, fallback, deadline, auth

**Files:**
- Modify: `src/lib/graphql_batch.py` (only if a test fails; the Task 2 code above already implements this behaviour)
- Test: `tests/unit/test_graphql_batch_core.py`

**Interfaces:**
- Consumes: everything produced by Task 2.
- Produces: guarantees that later adapter tasks rely on — transient batch errors split only the failed keys down to singles (bounded by `max_attempts`), fallback failures become typed unresolved reasons, `deadline_hit` stops all further requests, 401 raises `GraphQLAuthError`, SSO partial-results propagates.

- [ ] **Step 1: Write the failure-path tests**

Append to `tests/unit/test_graphql_batch_core.py`:

Add to the existing import block at the top of the file (`import httpx` is already there):

```python
import pytest

from lib.deadlines import Deadline
from lib.gh_client import PartialResultsError
from lib.graphql_batch import GraphQLAuthError, MalformedResponse
```

Then append (the key-encoded adapter lets the mock handler identify which repo each alias carries, since aliases are re-numbered per query):

```python
class KeyAdapter(DictAdapter):
    def build_query(self, aliases: Mapping[str, str]) -> str:
        pairs = " ".join(f"{alias}: field_{key}" for alias, key in aliases.items())
        return f"query {{ {pairs} }}"


def keys_in(request: httpx.Request) -> list[str]:
    query = json.loads(request.content)["query"]
    return re.findall(r"n\d+: field_(\d+)", query)


def test_transient_batch_error_splits_and_only_failing_keys_are_resent():
    requests: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys = keys_in(request)
        requests.append(keys)
        if len(keys) > 1:
            return httpx.Response(200, json={"data": None, "errors": [{"message": "timeout"}]})
        if keys == ["2"]:
            return httpx.Response(200, json={"data": {"n0": "value-2"}})
        return httpx.Response(200, json={"data": None, "errors": [{"message": "timeout"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(KeyAdapter(), ["1", "2", "3", "4"], client=client)
    assert outcome.values == {"2": "value-2"}
    assert set(outcome.unresolved) == {"1", "3", "4"}
    assert all("timeout" in reason for reason in outcome.unresolved.values())
    assert outcome.stats.requeues >= 2
    assert requests[0] == ["1", "2", "3", "4"]
    assert all(len(batch) <= 2 for batch in requests[1:])
    assert ["2"] in requests


def test_single_key_transient_failure_goes_to_fallback_after_attempts():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"data": None, "errors": [{"message": "timeout"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        DictAdapter(), ["1"], client=client, fallback=lambda key: f"rest-{key}"
    )
    assert outcome.values == {"1": "rest-1"}
    assert calls["n"] == 3  # max_attempts
    assert outcome.stats.fallbacks == 1


def test_non_transient_batch_error_skips_splitting_and_uses_fallback():
    requests: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(len(keys_in(request)))
        return httpx.Response(
            200, json={"data": None, "errors": [{"message": "Field 'x' doesn't exist"}]}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        DictAdapter(), ["1", "2", "3"], client=client, fallback=lambda key: f"rest-{key}"
    )
    assert requests == [3]
    assert outcome.values == {"1": "rest-1", "2": "rest-2", "3": "rest-3"}


def test_fallback_failure_is_recorded_as_unresolved_with_reason():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": None, "errors": [{"message": "timeout"}]})

    def fallback(key: str) -> object:
        raise RuntimeError("rest is down")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(DictAdapter(), ["7"], client=client, fallback=fallback)
    assert outcome.values == {}
    assert "RuntimeError" in outcome.unresolved["7"]
    assert outcome.stats.unresolved == 1


def test_deadline_expiry_stops_requests_and_marks_remaining_unresolved():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected after deadline")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    deadline = Deadline(0.0)
    outcome = fetch_batch(DictAdapter(), ["1", "2"], client=client, deadline=deadline)
    assert outcome.values == {}
    assert outcome.unresolved == {"1": "run deadline exceeded", "2": "run deadline exceeded"}
    assert outcome.stats.deadline_hit is True
    assert outcome.stats.requests == 0


def test_http_401_aborts_loudly():
    client = client_from([httpx.Response(401, json={"message": "Bad credentials"})])
    with pytest.raises(GraphQLAuthError):
        fetch_batch(DictAdapter(), ["1"], client=client)


def test_malformed_json_is_treated_as_a_transient_batch_failure():
    client = client_from([httpx.Response(200, text="<html>nope</html>")])
    outcome = fetch_batch(
        DictAdapter(), ["1"], client=client, fallback=lambda key: f"rest-{key}"
    )
    assert outcome.values == {"1": "rest-1"}
    assert outcome.stats.fallbacks == 1


def test_sso_partial_results_propagates():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {}}, headers={"x-github-sso": "partial-results; ..."}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(PartialResultsError):
        fetch_batch(DictAdapter(), ["1"], client=client)
```

- [ ] **Step 2: Run tests to verify they fail or pass**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_batch_core.py -q`
Expected: If the Task 2 implementation is correct, all pass. Any failure means Task 2's loop is wrong — fix `src/lib/graphql_batch.py` until green; do not weaken the tests.

- [ ] **Step 3: Run tests to verify they pass**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_batch_core.py -q`
Expected: PASS (13 tests). If any failure-path test fails, fix `src/lib/graphql_batch.py` — never weaken the test.

- [ ] **Step 4: Commit**

```bash
git add src/lib/graphql_batch.py tests/unit/test_graphql_batch_core.py
git commit -m "feat: graphql batch failure isolation and bounded retries"
```

---

## Phase 2 — Adapters

### Task 4: Repo details adapter

**Files:**
- Create: `src/hydrate/graphql_repo.py`
- Test: `tests/unit/test_graphql_repo_adapter.py`

**Interfaces:**
- Consumes: `ParsedBatch`, `GraphQLAdapter` (Task 2).
- Produces (used by Task 5):
  - `@dataclass(frozen=True) class RepoDetails: repo_id: int; node_id: str; full_name: str; payload: dict`
  - `class RepoDetailsAdapter: name = "repo_details"; batch_size; __init__(self, full_names: Mapping[str, str], *, batch_size=20)` where keys are `str(repo_id)`.
  - The `payload` is REST-shaped and satisfies `store.upserts.normalize_repo` (keys: `id`, `node_id`, `full_name`, `owner{id,login,type}`, `name`, `description`, `homepage`, `language`, `license{spdx_id}`, `topics`, `visibility`, `fork`, `parent`, `archived`, `disabled`, `is_template`, `size`, `stargazers_count`, `forks_count`, `watchers_count`, `open_issues_count`, `default_branch`, `has_wiki`, `has_issues`, `has_projects`, `has_pages`, `has_discussions`, `has_pull_requests`, `created_at`, `pushed_at`, `updated_at`).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_graphql_repo_adapter.py`:

```python
from __future__ import annotations

from collections.abc import Mapping

import httpx
import pytest

from hydrate.graphql_repo import RepoDetails, RepoDetailsAdapter
from lib.graphql_batch import fetch_batch
from store.upserts import normalize_repo

NODE = {
    "databaseId": 911,
    "id": "R_911",
    "name": "alpha",
    "nameWithOwner": "octo/alpha",
    "stargazerCount": 300,
    "forkCount": 30,
    "watchers": {"totalCount": 31},
    "issues": {"totalCount": 3},
    "diskUsage": 100,
    "isArchived": False,
    "isDisabled": False,
    "isFork": False,
    "isTemplate": False,
    "visibility": "PUBLIC",
    "description": "Fast crawler",
    "homepageUrl": "https://example.test",
    "pushedAt": "2025-12-30T12:00:00Z",
    "updatedAt": "2026-01-01T00:00:00Z",
    "createdAt": "2024-01-01T00:00:00Z",
    "defaultBranchRef": {"name": "main"},
    "primaryLanguage": {"name": "Rust"},
    "licenseInfo": {"spdxId": "MIT"},
    "repositoryTopics": {"nodes": [{"topic": {"name": "cli"}}, {"topic": {"name": "crawler"}}]},
    "hasIssuesEnabled": True,
    "hasWikiEnabled": False,
    "hasProjectsEnabled": True,
    "hasDiscussionsEnabled": True,
    "hasPullRequestsEnabled": True,
    "parent": None,
    "owner": {"databaseId": 901, "login": "octo", "__typename": "User"},
}


def test_build_query_uses_owner_name_aliases():
    adapter = RepoDetailsAdapter({"911": "octo/alpha"})
    query = adapter.build_query({"n0": "911"})
    assert 'n0: repository(owner: "octo", name: "alpha")' in query
    assert "databaseId" in query
    assert "stargazerCount" in query
    assert "first: 100" in query


def test_parse_maps_graphql_node_to_rest_shaped_payload():
    adapter = RepoDetailsAdapter({"911": "octo/alpha"})
    payload = {"data": {"n0": NODE}}
    parsed = adapter.parse(payload, {"n0": "911"})
    details = parsed.values["911"]
    assert isinstance(details, RepoDetails)
    assert details.repo_id == 911
    assert details.node_id == "R_911"
    assert details.full_name == "octo/alpha"
    row = normalize_repo(details.payload)
    assert row is not None
    assert row["id"] == 911
    assert row["owner_id"] == 901
    assert row["owner_login"] == "octo"
    assert row["stargazers"] == 300
    assert row["forks_count"] == 30
    assert row["watchers"] == 31
    assert row["open_issues"] == 3
    assert row["size_kb"] == 100
    assert row["language"] == "Rust"
    assert row["license_spdx"] == "MIT"
    assert row["topics"] == ["cli", "crawler"]
    assert row["visibility"] == "public"
    assert row["default_branch"] == "main"


def test_parse_organizations_and_missing_optional_fields():
    adapter = RepoDetailsAdapter({"911": "octo/alpha"})
    node = dict(NODE)
    node["owner"] = {"databaseId": 950, "login": "org", "__typename": "Organization"}
    node["licenseInfo"] = None
    node["primaryLanguage"] = None
    node["repositoryTopics"] = {"nodes": []}
    parsed = adapter.parse({"data": {"n0": node}}, {"n0": "911"})
    row = normalize_repo(parsed.values["911"].payload)
    assert row is not None
    assert row["owner_type"] == "Organization"
    assert row["license_spdx"] is None
    assert row["language"] is None
    assert row["topics"] == []


def test_parse_rejects_a_node_without_a_database_id():
    adapter = RepoDetailsAdapter({"911": "octo/alpha"})
    node = dict(NODE)
    node["databaseId"] = None
    parsed = adapter.parse({"data": {"n0": node}}, {"n0": "911"})
    assert parsed.values == {}
    assert "911" in parsed.failures


def test_adapter_end_to_end_with_batch_core():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"n0": NODE}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = RepoDetailsAdapter({"911": "octo/alpha"})
    outcome = fetch_batch(adapter, ["911"], client=client)
    assert outcome.values["911"].payload["id"] == 911
```

- [ ] **Step 2: Run test to verify it fails**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_repo_adapter.py -q`
Expected: FAIL (`ModuleNotFoundError: hydrate.graphql_repo`).

- [ ] **Step 3: Implement `src/hydrate/graphql_repo.py`**

```python
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from lib.graphql_batch import DEFAULT_MAX_ATTEMPTS, MAX_BATCH_SIZE, ParsedBatch

_FIELDS = """    databaseId
    id
    name
    nameWithOwner
    stargazerCount
    forkCount
    watchers(first: 0) { totalCount }
    issues(states: [OPEN]) { totalCount }
    diskUsage
    isArchived
    isDisabled
    isFork
    isTemplate
    visibility
    description
    homepageUrl
    pushedAt
    updatedAt
    createdAt
    defaultBranchRef { name }
    primaryLanguage { name }
    licenseInfo { spdxId }
    repositoryTopics(first: 100) { nodes { topic { name } } }
    hasIssuesEnabled
    hasWikiEnabled
    hasProjectsEnabled
    hasDiscussionsEnabled
    hasPullRequestsEnabled
    parent { nameWithOwner }
    owner { databaseId login __typename }"""


@dataclass(frozen=True)
class RepoDetails:
    repo_id: int
    node_id: str
    full_name: str
    payload: dict


def _as_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _total_count(value: object) -> int | None:
    if not isinstance(value, dict):
        return None
    return _as_int(value.get("totalCount"))


def _owner(node: dict) -> dict | None:
    raw = node.get("owner")
    if not isinstance(raw, dict):
        return None
    owner_id = _as_int(raw.get("databaseId"))
    login = raw.get("login")
    if owner_id is None or not isinstance(login, str) or not login:
        return None
    kind = raw.get("__typename")
    return {"id": owner_id, "login": login, "type": "Organization" if kind == "Organization" else "User"}


def _topics(node: dict) -> list[str]:
    raw = node.get("repositoryTopics")
    if not isinstance(raw, dict):
        return []
    nodes = raw.get("nodes")
    if not isinstance(nodes, list):
        return []
    topics: list[str] = []
    for entry in nodes:
        if not isinstance(entry, dict):
            continue
        topic = entry.get("topic")
        if isinstance(topic, dict) and isinstance(topic.get("name"), str):
            topics.append(topic["name"])
    return topics


def _license(node: dict) -> dict | None:
    raw = node.get("licenseInfo")
    if not isinstance(raw, dict):
        return None
    spdx = raw.get("spdxId")
    return {"spdx_id": spdx} if isinstance(spdx, str) else None


def _language(node: dict) -> str | None:
    raw = node.get("primaryLanguage")
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    return name if isinstance(name, str) else None


def _branch(node: dict) -> str | None:
    raw = node.get("defaultBranchRef")
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    return name if isinstance(name, str) else None


def _parent(node: dict) -> str | None:
    raw = node.get("parent")
    if not isinstance(raw, dict):
        return None
    full_name = raw.get("nameWithOwner")
    return full_name if isinstance(full_name, str) else None


def _visibility(node: dict) -> str:
    raw = node.get("visibility")
    if isinstance(raw, str) and raw:
        return raw.lower()
    return "public"


def _payload(repo_id: int, node: dict) -> dict:
    owner = _owner(node)
    node_id = node.get("id")
    full_name = node.get("nameWithOwner")
    if owner is None or not isinstance(node_id, str) or not isinstance(full_name, str):
        raise ValueError("graphql repo node is missing identity fields")
    return {
        "id": repo_id,
        "node_id": node_id,
        "full_name": full_name,
        "owner": owner,
        "name": node.get("name"),
        "description": node.get("description"),
        "homepage": node.get("homepageUrl"),
        "language": _language(node),
        "license": _license(node),
        "topics": _topics(node),
        "visibility": _visibility(node),
        "fork": bool(node.get("isFork")),
        "parent": {"full_name": _parent(node)} if _parent(node) else None,
        "archived": bool(node.get("isArchived")),
        "disabled": bool(node.get("isDisabled")),
        "is_template": bool(node.get("isTemplate")),
        "size": _as_int(node.get("diskUsage")),
        "stargazers_count": _as_int(node.get("stargazerCount")),
        "forks_count": _as_int(node.get("forkCount")),
        "watchers_count": _total_count(node.get("watchers")),
        "open_issues_count": _total_count(node.get("issues")),
        "default_branch": _branch(node),
        "has_wiki": node.get("hasWikiEnabled"),
        "has_issues": node.get("hasIssuesEnabled"),
        "has_projects": node.get("hasProjectsEnabled"),
        "has_discussions": node.get("hasDiscussionsEnabled"),
        "has_pull_requests": node.get("hasPullRequestsEnabled"),
        "custom_properties": {},
        "created_at": node.get("createdAt"),
        "pushed_at": node.get("pushedAt"),
        "updated_at": node.get("updatedAt"),
    }


class RepoDetailsAdapter:
    name = "repo_details"

    def __init__(self, full_names: Mapping[str, str], *, batch_size: int = MAX_BATCH_SIZE) -> None:
        self._full_names = dict(full_names)
        self.batch_size = batch_size

    def _parts(self, key: str) -> tuple[str, str]:
        owner, _, name = self._full_names[key].partition("/")
        return owner, name

    def build_query(self, aliases: Mapping[str, str]) -> str:
        lines = ["query {"]
        for alias, key in aliases.items():
            owner, name = self._parts(key)
            lines.append(f"  {alias}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{")
            lines.append(_FIELDS)
            lines.append("  }")
        lines.append("}")
        return "\n".join(lines)

    def parse(self, payload: Mapping[str, object], aliases: Mapping[str, str]) -> ParsedBatch:
        data = payload.get("data")
        values: dict[str, RepoDetails] = {}
        failures: dict[str, str] = {}
        if not isinstance(data, dict):
            return ParsedBatch(values=values, failures=failures)
        for alias, key in aliases.items():
            node = data.get(alias)
            if not isinstance(node, dict):
                continue
            repo_id = _as_int(node.get("databaseId"))
            if repo_id is None:
                failures[key] = "graphql node has no databaseId"
                continue
            try:
                values[key] = RepoDetails(
                    repo_id=repo_id,
                    node_id=str(node.get("id") or ""),
                    full_name=str(node.get("nameWithOwner") or ""),
                    payload=_payload(repo_id, node),
                )
            except ValueError as exc:
                failures[key] = str(exc)
        return ParsedBatch(values=values, failures=failures)
```

Note: `DEFAULT_MAX_ATTEMPTS` is imported but unused in this module — remove that import to keep ruff clean (only import `MAX_BATCH_SIZE, ParsedBatch`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_repo_adapter.py tests/unit/test_graphql_batch_core.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hydrate/graphql_repo.py tests/unit/test_graphql_repo_adapter.py
git commit -m "feat: graphql repo details adapter"
```

---

### Task 5: Batched hydration with REST fallback

**Files:**
- Modify: `src/hydrate/tail.py`
- Modify: `src/serve/runner.py` (`_hydrate` ~:191-207, `run_filter` ~:530-612)
- Create: `tests/integration/test_hydrate_batch.py`
- Modify: `tests/integration/test_runner.py`

**Interfaces:**
- Consumes: `fetch_batch` (Task 2), `RepoDetailsAdapter` (Task 4), `hydrate_repo`/`apply_hydration` (existing), `GraphQLAuthError`.
- Produces (used by Tasks 6, 7):
  - `RefreshStats` gains `unresolved: dict[str, str] = field(default_factory=dict)`, `batch: dict = field(default_factory=dict)`.
  - `refresh_repos_batched(engine, client, rows, *, limiter=None, token_id=None, on_response=None, sleep=time.sleep, now=time.time, jitter=None, deadline=None, batch_size=MAX_BATCH_SIZE) -> RefreshStats`
  - `_hydrate(deps, rows, cap, hook) -> RefreshStats` (was `-> None`).
  - `run_filter` merges `{"graphql": {"hydration": ...}}` into `payload.field_stats` and appends an unresolved warning.

- [ ] **Step 1: Write the failing DB-backed tests**

Create `tests/integration/test_hydrate_batch.py`:

```python
from __future__ import annotations

import json
import re
from collections.abc import Mapping

import httpx
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

from hydrate.tail import refresh_repos_batched
from lib.graphql_batch import GraphQLAuthError

ALIAS_RE = re.compile(r'(n\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\)')


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


def seed_repos(engine: Engine, count: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO owners (id, login, type) VALUES (901, 'octo', 'User')")
        )
        for repo_id in range(1, count + 1):
            connection.execute(
                text(
                    "INSERT INTO repos (id, node_id, full_name, owner_id, name, visibility)"
                    " VALUES (:id, :node, :full, 901, :name, 'public')"
                ),
                {"id": repo_id, "node": f"R_{repo_id}", "full": f"octo/repo{repo_id}", "name": f"repo{repo_id}"},
            )


def rows_for(count: int) -> list[dict]:
    return [
        {"id": repo_id, "full_name": f"octo/repo{repo_id}"} for repo_id in range(1, count + 1)
    ]


def graphql_node(repo_id: int) -> dict:
    return {
        "databaseId": repo_id,
        "id": f"R_{repo_id}",
        "name": f"repo{repo_id}",
        "nameWithOwner": f"octo/repo{repo_id}",
        "stargazerCount": repo_id,
        "forkCount": 0,
        "watchers": {"totalCount": 0},
        "issues": {"totalCount": 0},
        "diskUsage": 1,
        "isArchived": False,
        "isDisabled": False,
        "isFork": False,
        "isTemplate": False,
        "visibility": "PUBLIC",
        "description": None,
        "homepageUrl": None,
        "pushedAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
        "createdAt": "2024-01-01T00:00:00Z",
        "defaultBranchRef": {"name": "main"},
        "primaryLanguage": {"name": "Rust"},
        "licenseInfo": None,
        "repositoryTopics": {"nodes": []},
        "hasIssuesEnabled": True,
        "hasWikiEnabled": False,
        "hasProjectsEnabled": False,
        "hasDiscussionsEnabled": False,
        "hasPullRequestsEnabled": True,
        "parent": None,
        "owner": {"databaseId": 901, "login": "octo", "__typename": "User"},
    }


def graphql_handler(seen: list[str], *, bad_repos: frozenset[int] = frozenset()):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        body = json.loads(request.content)
        matches = ALIAS_RE.findall(body["query"])
        data: dict[str, object] = {}
        errors: list[dict] = []
        for alias, _owner, name in matches:
            repo_id = int(name.removeprefix("repo"))
            if repo_id in bad_repos:
                data[alias] = None
                errors.append({"message": "Could not resolve to a Repository", "path": [alias]})
            else:
                data[alias] = graphql_node(repo_id)
        payload: dict = {"data": data}
        if errors:
            payload["errors"] = errors
        return httpx.Response(200, json=payload)

    return handler


def test_batched_hydration_saves_every_repo_in_one_request(clean_db):
    engine = clean_db()
    seed_repos(engine, 25)
    seen: list[str] = []
    client = httpx.Client(transport=httpx.MockTransport(graphql_handler(seen)))
    stats = refresh_repos_batched(engine, client, rows_for(25))
    assert stats.refreshed == 25
    assert stats.unresolved == {}
    assert seen == ["/graphql", "/graphql"]  # 20 + 5
    with engine.connect() as connection:
        count = connection.scalar(text("SELECT count(*) FROM repos WHERE stargazers IS NOT NULL"))
    assert count == 25


def test_one_bad_repo_falls_back_to_rest_without_touching_its_neighbours(clean_db):
    engine = clean_db()
    seed_repos(engine, 3)
    graphql_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/graphql":
            return graphql_handler(graphql_seen, bad_repos=frozenset({2}))(request)
        if path == "/repos/octo/repo2":
            return httpx.Response(
                200,
                json={
                    "id": 2,
                    "node_id": "R_2",
                    "full_name": "octo/repo2",
                    "name": "repo2",
                    "owner": {"id": 901, "login": "octo", "type": "User"},
                    "private": False,
                    "topics": [],
                    "stargazers_count": 2,
                    "forks_count": 0,
                    "watchers_count": 0,
                    "open_issues_count": 0,
                    "default_branch": "main",
                    "pushed_at": "2026-01-01T00:00:00Z",
                },
                headers={"etag": 'W/"abc"'},
            )
        raise AssertionError(f"unexpected path {path}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = refresh_repos_batched(engine, client, rows_for(3))
    assert stats.refreshed == 3  # 2 batched + 1 fallback
    assert stats.fallbacks == 1
    assert stats.unresolved == {}
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT stargazers, etag FROM repos WHERE id = 2")
        ).one()
    assert row.stargazers == 2
    assert row.etag == 'W/"abc"'


def test_unresolved_repo_is_reported_not_swallowed(clean_db):
    engine = clean_db()
    seed_repos(engine, 1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/graphql":
            return graphql_handler([], bad_repos=frozenset({1}))(request)
        return httpx.Response(500, json={"message": "boom"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = refresh_repos_batched(engine, client, rows_for(1))
    assert stats.refreshed == 0
    assert "octo/repo1" in stats.unresolved
    assert stats.batch["unresolved"] == 1


def test_graphql_401_aborts_the_whole_hydration(clean_db):
    engine = clean_db()
    seed_repos(engine, 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GraphQLAuthError):
        refresh_repos_batched(engine, client, rows_for(1))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/integration/test_hydrate_batch.py -q`
Expected: FAIL (`ImportError: cannot import name 'refresh_repos_batched'`).

- [ ] **Step 3: Implement `refresh_repos_batched` in `src/hydrate/tail.py`**

Add these imports to `src/hydrate/tail.py`, then extend `RefreshStats` from a `@dataclass` to a `@dataclass` with `field` (import `field` from `dataclasses`):

```python
from hydrate.graphql_repo import RepoDetailsAdapter
from lib.deadlines import Deadline
from lib.graphql_batch import GraphQLAuthError, fetch_batch
```

Extend the dataclass:

```python
@dataclass
class RefreshStats:
    refreshed: int = 0
    not_modified: int = 0
    renamed: int = 0
    tombstoned: int = 0
    failed: int = 0
    unresolved: dict[str, str] = field(default_factory=dict)
    batch: dict = field(default_factory=dict)
```

(`field` needs importing from `dataclasses`.)

Append:

```python
def refresh_repos_batched(
    engine: Engine,
    client: httpx.Client,
    rows: Sequence[Mapping[str, object]],
    *,
    limiter: BucketLimiter | None = None,
    token_id: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
    on_response: Callable[[httpx.Response, float], None] | None = None,
    deadline: Deadline | None = None,
) -> RefreshStats:
    stats = RefreshStats()
    candidates = [(str(row["id"]), str(row["full_name"])) for row in rows]
    if not candidates:
        return stats
    etags = _stored_etags(engine, [full_name for _, full_name in candidates])

    def fallback(key: str) -> object | None:
        full_name = dict(candidates)[key]
        try:
            hydrated = hydrate_repo(
                client,
                full_name,
                etag=etags.get(full_name.casefold()),
                limiter=limiter,
                token_id=token_id,
                sleep=sleep,
                now=now,
                jitter=jitter,
                on_response=on_response,
            )
        except RepoNotFound:
            apply_hydration(engine, full_name, None, not_found=True)
            stats.tombstoned += 1
            return None
        except RequestFailed as exc:
            if exc.status == 401:
                raise GraphQLAuthError("github rejected the token (HTTP 401)") from exc
            stats.failed += 1
            return None
        except (ThrottledError, PartialResultsError):
            stats.failed += 1
            return None
        if hydrated.not_modified:
            stats.not_modified += 1
            return None
        outcome = apply_hydration(engine, full_name, hydrated)
        stats.refreshed += 1
        if outcome.renamed_from is not None:
            stats.renamed += 1
        return None

    outcome = fetch_batch(
        RepoDetailsAdapter(dict(candidates)),
        [key for key, _ in candidates],
        client=client,
        limiter=limiter,
        token_id=token_id,
        fallback=fallback,
        deadline=deadline,
        on_response=on_response,
        sleep=sleep,
        now=now,
        jitter=jitter,
    )
    full_name_by_key = dict(candidates)
    for key, details in outcome.values.items():
        hydrated = HydratedRepo(
            id=details.repo_id,
            node_id=details.node_id,
            full_name=details.full_name,
            payload=details.payload,
            etag=None,
            not_modified=False,
        )
        result = apply_hydration(engine, full_name_by_key[key], hydrated)
        stats.refreshed += 1
        if result.renamed_from is not None:
            stats.renamed += 1
    stats.unresolved = {
        full_name_by_key[key]: reason for key, reason in outcome.unresolved.items()
    }
    stats.batch = outcome.stats.as_dict()
    return stats
```

Add `HydratedRepo` to the existing `from hydrate.repo_client import RepoNotFound, hydrate_repo` line, and `Mapping, Sequence` to the `collections.abc` import.

Note: `fallback` returns `None` for every handled case so the engine counts it as `handled`, while `stats.refreshed`/`tombstoned`/`failed`/`not_modified` record the real outcome — this preserves the existing `RefreshStats` semantics exactly.

- [ ] **Step 4: Wire `_hydrate` and `run_filter` in `src/serve/runner.py`**

Replace `_hydrate` (currently lines ~191-207):

```python
def _hydrate(
    deps: Deps,
    rows: list[dict],
    cap: int,
    hook: Callable[[httpx.Response, float], None],
) -> RefreshStats:
    candidates = rows[:cap]
    if not candidates:
        return RefreshStats()
    return refresh_repos_batched(
        deps.engine,
        deps.client,
        candidates,
        limiter=deps.limiter,
        token_id=deps.token_fp,
        on_response=hook,
        deadline=deps.limiter.deadline if deps.limiter is not None else None,
    )
```

Update the imports in `src/serve/runner.py`: replace `from hydrate.tail import refresh_repos` with `from hydrate.tail import RefreshStats, refresh_repos_batched` (keep `refresh_repos` too if any other call site exists — none does; remove it).

In `run_filter`, capture the stats and build the report:

```python
    hydration = _hydrate(deps, candidates, cfg.max_hydrate, hook)
```

Where the `graphql_report` dict is initialised (immediately after the `_hydrate` call):

```python
    graphql_report: dict[str, object] = {"hydration": hydration.batch}
    if hydration.unresolved:
        sample = "; ".join(
            f"{name}: {reason}"
            for name, reason in list(hydration.unresolved.items())[:3]
        )
        warnings.append(
            f"{len(hydration.unresolved)} repo(s) could not be hydrated ({sample}); "
            "results are incomplete"
        )
```

And change the final payload construction to merge the report:

```python
    field_stats = asdict(segment_stats)
    field_stats["graphql"] = graphql_report
    ...
    return RunPayload(
        ...
        field_stats=field_stats,
        ...
    )
```

(Replace the existing `field_stats=asdict(segment_stats)` line — do not duplicate the argument.)

- [ ] **Step 5: Update the existing runner tests' mocks to speak GraphQL**

The hydration path is now GraphQL-first, so every handler in `tests/integration/test_runner.py` that serves `/repos/{owner}/{name}` must also answer `/graphql`. Add this shared helper near the top of the module (after `repo_item`):

```python
GRAPHQL_REPO_ALIAS_RE = re.compile(r'(n\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\)')


def rest_item_to_graphql_node(item: dict) -> dict:
    owner = item.get("owner") or {}
    return {
        "databaseId": item.get("id"),
        "id": item.get("node_id"),
        "name": item.get("name"),
        "nameWithOwner": item.get("full_name"),
        "stargazerCount": item.get("stargazers_count", 0),
        "forkCount": item.get("forks_count", 0),
        "watchers": {"totalCount": item.get("watchers_count", 0)},
        "issues": {"totalCount": item.get("open_issues_count", 0)},
        "diskUsage": item.get("size", 0),
        "isArchived": bool(item.get("archived")),
        "isDisabled": bool(item.get("disabled")),
        "isFork": bool(item.get("fork")),
        "isTemplate": bool(item.get("is_template")),
        "visibility": str(item.get("visibility") or "public").upper(),
        "description": item.get("description"),
        "homepageUrl": item.get("homepage"),
        "pushedAt": item.get("pushed_at"),
        "updatedAt": item.get("updated_at") or item.get("pushed_at"),
        "createdAt": item.get("created_at") or item.get("pushed_at"),
        "defaultBranchRef": {"name": item.get("default_branch") or "main"},
        "primaryLanguage": {"name": item.get("language")} if item.get("language") else None,
        "licenseInfo": {"spdxId": (item.get("license") or {}).get("spdx_id")}
        if item.get("license")
        else None,
        "repositoryTopics": {
            "nodes": [{"topic": {"name": topic}} for topic in item.get("topics", [])]
        },
        "hasIssuesEnabled": True,
        "hasWikiEnabled": False,
        "hasProjectsEnabled": False,
        "hasDiscussionsEnabled": False,
        "hasPullRequestsEnabled": True,
        "parent": None,
        "owner": {
            "databaseId": owner.get("id"),
            "login": owner.get("login"),
            "__typename": owner.get("type") or "User",
        },
    }


def graphql_batch_response(
    request: httpx.Request, items_by_full_name: dict[str, dict]
) -> httpx.Response:
    body = json.loads(request.content)
    data: dict[str, object] = {}
    errors: list[dict] = []
    for alias, owner, name in GRAPHQL_REPO_ALIAS_RE.findall(body["query"]):
        item = items_by_full_name.get(f"{owner}/{name}")
        if item is None:
            data[alias] = None
            errors.append({"message": "Could not resolve to a Repository", "path": [alias]})
        else:
            data[alias] = rest_item_to_graphql_node(item)
    payload: dict[str, object] = {"data": data}
    if errors:
        payload["errors"] = errors
    return httpx.Response(200, json=payload)
```

Then, for each test handler that currently returns a repo for `path == "/repos/ownerN/repoN"` (tests at lines 179, 209, 232, 257, 283, 338, 382, 407, 434, 494, 531, 562, 599, 626, 735, 795, 868), add at the top of the handler:

```python
        if path == "/graphql":
            return graphql_batch_response(request, {"owner1/repo1": repo_item(1), ...})
```

using the same repo dicts the handler already serves. Then fix the now-invalid assertions:

- `test_run_filter_honors_shard_candidate_and_hydrate_caps` (line ~205): the assertion `hydration == ["/repos/owner2/repo2"]` becomes `assert hydration == []` and add `assert any(request.url.path == "/graphql" for request in requests)`.
- `test_run_filter_has_dockerfile_*` and the cost-plan test (lines ~528, ~784): the tree calls are now GraphQL, so replace `tree_requests == ["/repos/..."]` with a GraphQL assertion (`assert list(seen_paths).count("/graphql") == ...`) or delete the REST-tree assertion and assert the virtual-filter value — whichever the test's intent is. Keep the repo-level behavioural assertion intact (e.g. only one repo is checked); never weaken it.
- `test_run_filter_tolerates_tree_fetch_failure` (line ~599): keep it as a fallback test — make `/graphql` return the repo with `object: "ERROR"` equivalent (an error for the file alias) and let the REST `/git/trees/...` handler return 500; assert the repo is dropped/skipped exactly as before.

- [ ] **Step 6: Add the new runner-level tests**

Append to `tests/integration/test_runner.py`:

```python
def test_run_filter_batches_hydration_and_isolates_a_bad_repo(clean: Engine):
    scripts = {
        "/search/repositories": page_response([repo_item(index) for index in (1, 2, 3)]),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if path == "/search/repositories":
            return scripts[path]
        if path == "/graphql":
            return graphql_batch_response(
                request,
                {"owner1/repo1": repo_item(1), "owner3/repo3": repo_item(3)},
            )
        if path == "/repos/owner2/repo2":
            return httpx.Response(500, json={"message": "boom"})
        raise AssertionError(f"unexpected path {path}")

    client, requests = scripted(handler)
    deps = make_deps(clean, client)
    payload = run_filter(deps, spec_for(q="language:python"))
    assert payload.fetched == 3
    assert payload.field_stats["graphql"]["hydration"]["unresolved"] == 0
    assert any("could not be hydrated" in warning for warning in payload.warnings)
    graphql_requests = [request for request in requests if request.url.path == "/graphql"]
    assert len(graphql_requests) == 1


def test_run_filter_reports_batch_counts_in_field_stats(clean: Engine):
    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if path == "/search/repositories":
            return page_response([repo_item(1)])
        if path == "/graphql":
            return graphql_batch_response(request, {"owner1/repo1": repo_item(1)})
        raise AssertionError(f"unexpected path {path}")

    client, _requests = scripted(handler)
    payload = run_filter(make_deps(clean, client), spec_for(q="language:python"))
    hydration = payload.field_stats["graphql"]["hydration"]
    assert hydration["keys"] == 1
    assert hydration["values"] == 1
    assert hydration["requests"] == 1
    assert hydration["deadline_hit"] is False
```

- [ ] **Step 7: Run hydration + runner tests**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/integration/test_hydrate_batch.py tests/integration/test_runner.py -q`
Expected: PASS. Fix any remaining handler that the failure list names using the Step 5 helper (never by weakening an assertion).

- [ ] **Step 8: Commit**

```bash
git add src/hydrate/tail.py src/serve/runner.py tests/integration/test_hydrate_batch.py tests/integration/test_runner.py
git commit -m "feat: batched hydration with rest fallback and outcome reporting"
```

---

### Task 6: File presence adapter and batched Dockerfile checks

**Files:**
- Create: `src/enrich/graphql_file_presence.py`
- Modify: `src/serve/runner.py` (`_apply_dockerfile` ~:366-401, `_dockerfile_handler` ~:425-435, `run_filter` report)
- Test: `tests/unit/test_graphql_file_presence_adapter.py`, `tests/integration/test_runner.py`

**Interfaces:**
- Consumes: `fetch_batch` (Task 2), existing `fetch_tree` for fallback.
- Produces:
  - `class FilePresenceAdapter: name = "file_presence"; __init__(self, path: str, full_names: Mapping[str, str], *, batch_size=20)`; values are `bool`.
  - `_apply_dockerfile(deps, rows, wanted, budget, hook, report) -> tuple[list[dict], int, int]` (new `report: dict` parameter); `report["files"] = stats.as_dict()`.
  - `_dockerfile_handler(...)` passes the shared report dict.

- [ ] **Step 1: Write the adapter test**

Create `tests/unit/test_graphql_file_presence_adapter.py`:

```python
from __future__ import annotations

import httpx

from enrich.graphql_file_presence import FilePresenceAdapter
from lib.graphql_batch import fetch_batch


def test_query_uses_head_expression_and_owner_name_aliases():
    adapter = FilePresenceAdapter("Dockerfile", {"911": "octo/alpha"})
    query = adapter.build_query({"n0": "911"})
    assert 'n0: repository(owner: "octo", name: "alpha")' in query
    assert 'object(expression: "HEAD:Dockerfile")' in query
    assert "__typename" in query


def test_parse_blob_is_true_and_missing_object_is_false():
    adapter = FilePresenceAdapter("Dockerfile", {"1": "octo/one", "2": "octo/two"})
    payload = {"data": {"n0": {"object": {"__typename": "Blob"}}, "n1": {"object": None}}}
    parsed = adapter.parse(payload, {"n0": "1", "n1": "2"})
    assert parsed.values == {"1": True, "2": False}


def test_parse_tree_object_is_false():
    adapter = FilePresenceAdapter("Dockerfile", {"1": "octo/one"})
    parsed = adapter.parse({"data": {"n0": {"object": {"__typename": "Tree"}}}}, {"n0": "1"})
    assert parsed.values == {"1": False}


def test_null_repository_is_missing_not_false():
    adapter = FilePresenceAdapter("Dockerfile", {"1": "octo/one"})
    parsed = adapter.parse({"data": {"n0": None}}, {"n0": "1"})
    assert parsed.values == {}
    assert parsed.failures == {}


def test_end_to_end_with_batch_core():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"n0": {"object": {"__typename": "Blob"}}}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = FilePresenceAdapter("Dockerfile", {"1": "octo/one"})
    outcome = fetch_batch(adapter, ["1"], client=client)
    assert outcome.values == {"1": True}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_file_presence_adapter.py -q`
Expected: FAIL (`ModuleNotFoundError: enrich.graphql_file_presence`).

- [ ] **Step 3: Implement `src/enrich/graphql_file_presence.py`**

```python
from __future__ import annotations

import json
from collections.abc import Mapping

from lib.graphql_batch import MAX_BATCH_SIZE, ParsedBatch


class FilePresenceAdapter:
    name = "file_presence"

    def __init__(
        self, path: str, full_names: Mapping[str, str], *, batch_size: int = MAX_BATCH_SIZE
    ) -> None:
        self._path = path
        self._full_names = dict(full_names)
        self.batch_size = batch_size

    def _parts(self, key: str) -> tuple[str, str]:
        owner, _, name = self._full_names[key].partition("/")
        return owner, name

    def build_query(self, aliases: Mapping[str, str]) -> str:
        expression = f"HEAD:{self._path}"
        lines = ["query {"]
        for alias, key in aliases.items():
            owner, name = self._parts(key)
            lines.append(
                f"  {alias}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{"
            )
            lines.append(f"    object(expression: {json.dumps(expression)}) {{ __typename }}")
            lines.append("  }")
        lines.append("}")
        return "\n".join(lines)

    def parse(self, payload: Mapping[str, object], aliases: Mapping[str, str]) -> ParsedBatch:
        data = payload.get("data")
        values: dict[str, bool] = {}
        if not isinstance(data, dict):
            return ParsedBatch(values=values)
        for alias, key in aliases.items():
            node = data.get(alias)
            if not isinstance(node, dict):
                continue  # null repository: fall back (rename/deleted handling lives in REST)
            obj = node.get("object")
            typename = obj.get("__typename") if isinstance(obj, dict) else None
            values[key] = typename == "Blob"
        return ParsedBatch(values=values)
```

- [ ] **Step 4: Run adapter tests**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_file_presence_adapter.py -q`
Expected: PASS.

- [ ] **Step 5: Wire `_apply_dockerfile` to batch (with REST tree fallback)**

Replace `_apply_dockerfile` in `src/serve/runner.py`:

```python
def _apply_dockerfile(
    deps: Deps,
    rows: list[dict],
    wanted: object,
    budget: int,
    hook: Callable[[httpx.Response, float], None],
    report: dict,
) -> tuple[list[dict], int, int]:
    if not isinstance(wanted, bool):
        return rows, 0, 0
    selected = rows[: max(0, budget)]
    leftover = [row for row in rows[max(0, budget) :]]
    if not selected:
        return [], len(leftover), 0

    def fallback(key: str) -> object | None:
        row = rows_by_key[key]
        try:
            presence = fetch_tree(
                deps.client,
                row["full_name"],
                ref=row["default_branch"] or None,
                limiter=deps.limiter,
                token_id=deps.token_fp,
                on_response=hook,
            )
        except (RequestFailed, ThrottledError, PartialResultsError):
            skipped["n"] += 1
            return None
        return presence.has(_DOCKERFILE_PATH)

    rows_by_key = {str(row["id"]): row for row in selected}
    skipped = {"n": 0}
    outcome = fetch_batch(
        FilePresenceAdapter(_DOCKERFILE_PATH, {key: row["full_name"] for key, row in rows_by_key.items()}),
        list(rows_by_key),
        client=deps.client,
        limiter=deps.limiter,
        token_id=deps.token_fp,
        fallback=fallback,
        on_response=hook,
        deadline=deps.limiter.deadline if deps.limiter is not None else None,
    )
    report["files"] = outcome.stats.as_dict()
    kept: list[dict] = []
    skipped_count = len(leftover) + skipped["n"]
    used = 0
    for key, row in rows_by_key.items():
        value = outcome.values.get(key)
        if value is None:
            skipped_count += 1
            continue
        used += 1
        row["has_dockerfile"] = bool(value)
        if bool(value) == wanted:
            kept.append(row)
    return kept, skipped_count, used
```

Add `from enrich.graphql_file_presence import FilePresenceAdapter` and `from lib.graphql_batch import fetch_batch` to the runner imports.

Update `_dockerfile_handler` to pass the report through:

```python
def _dockerfile_handler(deps, rows_by_id, wanted, budget, hook, skipped, report):
    def handler(ids):
        rows = [rows_by_id[repo_id] for repo_id in ids]
        kept, dockerfile_skipped, used = _apply_dockerfile(
            deps, rows, wanted, budget["remaining"], hook, report
        )
        budget["remaining"] = max(0, budget["remaining"] - used)
        skipped["dockerfile"] += dockerfile_skipped
        return [row["id"] for row in kept], used

    return handler
```

Update `_enrich_handlers`'s signature and call site: add a `report: dict` parameter, pass it to `_dockerfile_handler`. In `run_filter`, create `graphql_report` before `_enrich_handlers` (it already exists from Task 5) and pass it:

```python
    handlers, unsupported = _enrich_handlers(deps, rows, virtual, budget, hook, skipped, graphql_report)
```

- [ ] **Step 6: Update/extend the Dockerfile tests**

For `test_run_filter_has_dockerfile_true_enforces_presence_and_budget` and `..._false_keeps_absent_repos`: add a `/graphql` branch that answers the file query. Use a small local helper that inspects the alias→repo mapping and answers `Blob`/`null` from the test's tree map:

```python
FILE_ALIAS_RE = re.compile(r'(n\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\)')


def graphql_file_response(request: httpx.Request, blob_paths: dict[str, list[str]], path: str):
    body = json.loads(request.content)
    data = {}
    for alias, owner, name in FILE_ALIAS_RE.findall(body["query"]):
        tree = blob_paths.get(f"{owner}/{name}", [])
        data[alias] = {"object": {"__typename": "Blob"}} if path in tree else {"object": None}
    return httpx.Response(200, json={"data": data})
```

Then the existing assertions about `tree_requests` must change from REST paths to graphql requests: assert the number of `/graphql` requests equals the previous number of checked repos, and keep the "only owner1 was checked, not owner2" intent by asserting the alias set inside the query (`"repo1" in query and "repo2" not in query`).

For `test_run_filter_tolerates_tree_fetch_failure`: make `/graphql` return `{"errors": [{"message": "Something went wrong", "path": ["n0"]}], "data": {"n0": None}}` and keep the REST tree handler returning 500; assert the repo is skipped exactly as before (warning present, item dropped).

- [ ] **Step 7: Run the runner suite**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/integration/test_runner.py tests/unit/test_graphql_file_presence_adapter.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/enrich/graphql_file_presence.py src/serve/runner.py tests/unit/test_graphql_file_presence_adapter.py tests/integration/test_runner.py
git commit -m "feat: batched file presence checks with tree fallback"
```

---

### Task 7: Owner location adapter and batched geo lookups

**Files:**
- Create: `src/enrich/graphql_owner_location.py`
- Modify: `src/serve/runner.py` (`_load_owners` ~:232-245, `_apply_geo` ~:278-363, `_geo_handler` ~:414-422, `_enrich_handlers`)
- Test: `tests/unit/test_graphql_owner_location_adapter.py`, `tests/integration/test_runner.py`

**Interfaces:**
- Consumes: `fetch_batch` (Task 2), existing REST `_fetch_owner_location` for fallback.
- Produces:
  - `class OwnerLocationAdapter: name = "owner_location"; __init__(self, owners: Mapping[str, str], *, batch_size=20)` where the map is `login -> "Organization" | anything-else = User`; values are `str | None`.
  - `_apply_geo(deps, rows, virtual, budget, hook, report) -> tuple[list[dict], int, int]` (new `report` parameter); `report["owners"] = stats.as_dict()`.
  - `_load_owners` now selects `Owner.type`.

- [ ] **Step 1: Write the adapter test**

Create `tests/unit/test_graphql_owner_location_adapter.py`:

```python
from __future__ import annotations

from collections.abc import Mapping

import httpx

from enrich.graphql_owner_location import OwnerLocationAdapter
from lib.graphql_batch import fetch_batch


def test_query_picks_user_or_organization_per_login():
    adapter = OwnerLocationAdapter({"alice": "User", "acme": "Organization"})
    query = adapter.build_query({"n0": "alice", "n1": "acme"})
    assert 'n0: user(login: "alice") { location }' in query
    assert 'n1: organization(login: "acme") { location }' in query


def test_parse_keeps_none_locations_as_present_values():
    adapter = OwnerLocationAdapter({"alice": "User", "bob": "User"})
    payload = {"data": {"n0": {"location": "Berlin"}, "n1": {"location": None}}}
    parsed = adapter.parse(payload, {"n0": "alice", "n1": "bob"})
    assert parsed.values == {"alice": "Berlin", "bob": None}


def test_null_account_is_missing_for_fallback():
    adapter = OwnerLocationAdapter({"ghost": "User"})
    parsed = adapter.parse({"data": {"n0": None}}, {"n0": "ghost"})
    assert parsed.values == {}
    assert parsed.failures == {}


def test_bot_type_uses_user_query():
    adapter = OwnerLocationAdapter({"dependabot[bot]": "Bot"})
    query = adapter.build_query({"n0": "dependabot[bot]"})
    assert 'n0: user(login: "dependabot[bot]") { location }' in query


def test_end_to_end_with_batch_core():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"n0": {"location": "Lagos"}}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OwnerLocationAdapter({"alice": "User"})
    outcome = fetch_batch(adapter, ["alice"], client=client)
    assert outcome.values == {"alice": "Lagos"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_owner_location_adapter.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `src/enrich/graphql_owner_location.py`**

```python
from __future__ import annotations

import json
from collections.abc import Mapping

from lib.graphql_batch import MAX_BATCH_SIZE, ParsedBatch


class OwnerLocationAdapter:
    name = "owner_location"

    def __init__(self, owners: Mapping[str, str], *, batch_size: int = MAX_BATCH_SIZE) -> None:
        self._owners = dict(owners)
        self.batch_size = batch_size

    def _field(self, alias: str, login: str) -> str:
        kind = "organization" if self._owners[login] == "Organization" else "user"
        return f'  {alias}: {kind}(login: {json.dumps(login)}) {{ location }}'

    def build_query(self, aliases: Mapping[str, str]) -> str:
        lines = ["query {"]
        lines.extend(self._field(alias, key) for alias, key in aliases.items())
        lines.append("}")
        return "\n".join(lines)

    def parse(self, payload: Mapping[str, object], aliases: Mapping[str, str]) -> ParsedBatch:
        data = payload.get("data")
        values: dict[str, str | None] = {}
        if not isinstance(data, dict):
            return ParsedBatch(values=values)
        for alias, key in aliases.items():
            node = data.get(alias)
            if not isinstance(node, dict):
                continue
            location = node.get("location")
            values[key] = location if isinstance(location, str) and location.strip() else None
        return ParsedBatch(values=values)
```

- [ ] **Step 4: Run adapter tests**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_owner_location_adapter.py -q`
Expected: PASS.

- [ ] **Step 5: Wire `_load_owners` and `_apply_geo`**

In `_load_owners`, add `Owner.type` to the select and the returned dict, so callers can choose the wait query:

```python
        rows = connection.execute(
            select(
                Owner.id,
                Owner.login,
                Owner.type,
                Owner.location_raw,
                Owner.country_iso,
                Owner.geo_confidence,
            ).where(Owner.id.in_(owner_ids))
        ).mappings()
```

Replace the owner location loop inside `_apply_geo` (the `for owner_id in owner_ids:` block that calls `_fetch_owner_location`) with a batched fetch. Full replacement of the function's fetch section:

```python
    pending_owners: list[tuple[int, str]] = []
    fetch_targets: list[tuple[int, str]] = []
    for owner_id in owner_ids:
        owner = owners.get(owner_id)
        if owner is None:
            continue
        if owner["country_iso"] is not None or owner["geo_confidence"] == "unmatched":
            continue
        pending_owners.append((owner_id, owner["login"]))
        if owner["location_raw"] is None:
            fetch_targets.append((owner_id, owner["login"]))
    lookups = fetch_targets[: max(0, budget)]
    rows_by_login = {login: owner_id for owner_id, login in lookups}
    owner_types = {login: owners[owner_id]["type"] for owner_id, login in lookups}

    def fallback(login: str) -> object | None:
        ok, location = _fetch_owner_location(deps, login, hook)
        used["n"] += 1
        if not ok:
            return None
        owners[rows_by_login[login]]["location_raw"] = location
        return location

    used = {"n": 0}
    outcome = fetch_batch(
        OwnerLocationAdapter(owner_types),
        list(rows_by_login),
        client=deps.client,
        limiter=deps.limiter,
        token_id=deps.token_fp,
        fallback=fallback,
        on_response=hook,
        deadline=deps.limiter.deadline if deps.limiter is not None else None,
    )
    report["owners"] = outcome.stats.as_dict()
    for login, owner_id in rows_by_login.items():
        if login not in outcome.values:
            continue
        used["n"] += 1
        owners[owner_id]["location_raw"] = outcome.values[login]
    used_count = used["n"]
    pending = [
        (owner_id, owners[owner_id]["location_raw"])
        for owner_id, _login in pending_owners
        if owners[owner_id]["location_raw"] is not None
    ]
```

Then the existing `resolve_many`/update section stays, but the old `for owner_id in owner_ids:` resolution loop and the `used` local must be removed/replaced. The function's final tuple becomes `return kept, used_count, skipped`. The `budget` semantics stay: at most `budget` owner lookups per call, and `run_filter` decrements the budget by the returned used count.

Update `_geo_handler` to accept and pass `report`:

```python
def _geo_handler(deps, rows_by_id, virtual, budget, hook, skipped, report):
    def handler(ids):
        rows = [rows_by_id[repo_id] for repo_id in ids]
        kept, used, geo_skipped = _apply_geo(
            deps, rows, virtual, budget["remaining"], hook, report
        )
        budget["remaining"] = max(0, budget["remaining"] - used)
        skipped["geo"] += geo_skipped
        return [row["id"] for row in kept], used

    return handler
```

Update `_enrich_handlers` to pass `report` into both handlers. `_enrich_handlers` already receives `report` from Task 6; extend the `_geo_handler(...)` call with it.

- [ ] **Step 6: Update/extend the owner tests**

For `test_run_filter_owner_country_filters_and_bounds_owner_fetches`, `..._tolerates_owner_fetch_failure`, and `..._tolerates_owner_fetch_sso_partial_results`: add a `/graphql` branch answering `location` per login, built from the test's existing user mapping:

```python
OWNER_ALIAS_RE = re.compile(r'(n\d+): (?:user|organization)\(login: "([^"]+)"\)')


def graphql_owner_response(request: httpx.Request, locations: dict[str, str | None]):
    body = json.loads(request.content)
    data: dict[str, object] = {}
    errors: list[dict] = []
    for alias, login in OWNER_ALIAS_RE.findall(body["query"]):
        if login not in locations:
            data[alias] = None
            errors.append({"message": "Could not resolve to a User", "path": [alias]})
        else:
            data[alias] = {"location": locations[login]}
    payload: dict[str, object] = {"data": data}
    if errors:
        payload["errors"] = errors
    return httpx.Response(200, json=payload)
```

The SSO test must keep exercising the loud-fail path: have `/graphql` return headers `{"x-github-sso": "partial-results; ..."}` and assert `PartialResultsError` propagates (the existing test expectation), or point that test at the REST fallback by making `/graphql` fail transiently for that login and the REST `/users/...` respond with the SSO header.

Add a new isolation test:

```python
def test_run_filter_geo_batches_and_falls_back_per_owner(clean: Engine):
    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if path == "/search/repositories":
            return page_response([repo_item(1, login="alice"), repo_item(2, login="bob")])
        if path == "/graphql":
            return graphql_owner_response(request, {"alice": "Berlin"})
        if path == "/users/bob":
            return httpx.Response(200, json={"location": "Lagos"})
        raise AssertionError(f"unexpected path {path}")

    client, requests = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", virtual={"owner_country": "DE"}),
    )
    assert [item.repo_id for item in payload.items] == [1]
    assert payload.field_stats["graphql"]["owners"]["values"] == 1
    assert payload.field_stats["graphql"]["owners"]["fallbacks"] == 1
```

- [ ] **Step 7: Run the runner suite**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/integration/test_runner.py tests/unit/test_graphql_owner_location_adapter.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/enrich/graphql_owner_location.py src/serve/runner.py tests/unit/test_graphql_owner_location_adapter.py tests/integration/test_runner.py
git commit -m "feat: batched owner location lookups with rest fallback"
```

---

## Phase 3 — Verification, docs, hardening

### Task 8: Enforce min/max commits from batched commit counts

**Files:**
- Modify: `src/hydrate/graphql_repo.py`, `src/hydrate/repo_client.py`, `src/hydrate/tail.py`, `src/serve/runner.py`, `src/serve/virtual_params.py`, `src/serve/forms.py`, `src/enrich/cost_planner.py`
- Test: `tests/unit/test_graphql_repo_adapter.py`, `tests/integration/test_hydrate_batch.py`, `tests/integration/test_runner.py`

**Interfaces:**
- Produces: `RepoDetails.commit_count: int | None`; adapter field `defaultBranchRef { name target { ... on Commit { history(first: 1) { totalCount } } } }`; `hydrate.repo_client.fetch_commit_count(client, full_name, ...) -> int | None` (REST `Link rel="last"` page count); `RefreshStats.commit_counts: dict[str, int]`; virtual rules `max_commits`/`max_loc`; `_R44_VIRTUALS = ("min_loc",)`; planner costs `min_commits`/`max_commits` = 2, `min_loc`/`max_loc` = 4; `commit_count` surfaced in `run_items.virtuals`.
- Scope: default branch only, whatever its name; no branch enumeration. Merged-in commits are included; the number is a snapshot at `ran_at`. Empty repo → 0.

- [ ] **Step 1: Write the failing adapter tests**

Append to `tests/unit/test_graphql_repo_adapter.py`:

```python
def test_parse_reads_default_branch_commit_count():
    adapter = RepoDetailsAdapter({"911": "octo/alpha"})
    node = dict(NODE)
    node["defaultBranchRef"] = {"name": "main", "target": {"history": {"totalCount": 42}}}
    parsed = adapter.parse({"data": {"n0": node}}, {"n0": "911"})
    assert parsed.values["911"].commit_count == 42


def test_parse_empty_repository_reports_zero_commits():
    adapter = RepoDetailsAdapter({"911": "octo/alpha"})
    node = dict(NODE)
    node["defaultBranchRef"] = None
    parsed = adapter.parse({"data": {"n0": node}}, {"n0": "911"})
    assert parsed.values["911"].commit_count == 0
```

- [ ] **Step 2: Implement the adapter field**

In `src/hydrate/graphql_repo.py`: add `commit_count: int | None = None` to `RepoDetails`; change the query's `defaultBranchRef { name }` line to:

```
    defaultBranchRef {
      name
      target { ... on Commit { history(first: 1) { totalCount } } }
    }
```

Add:

```python
def _commit_count(node: dict) -> int | None:
    branch = node.get("defaultBranchRef")
    if branch is None:
        return 0
    if not isinstance(branch, dict):
        return None
    target = branch.get("target")
    history = target.get("history") if isinstance(target, dict) else None
    total = history.get("totalCount") if isinstance(history, dict) else None
    return total if isinstance(total, int) and not isinstance(total, bool) else None
```

and include `commit_count=_commit_count(node)` in the `RepoDetails(...)` construction.

- [ ] **Step 3: Write the failing hydration tests**

In `tests/integration/test_hydrate_batch.py`, extend `graphql_node` with `"defaultBranchRef": {"name": "main", "target": {"history": {"totalCount": repo_id * 10}}}` and assert `stats.commit_counts == {str(repo_id): repo_id * 10 for repo_id in range(1, 26)}`. Add a REST-fallback test: the fallback handler for `/repos/octo/repo2/commits?per_page=1` returns `httpx.Response(200, json=[{}], headers={"Link": '<https://api.github.com/…&page=77>; rel="last"'})` and `stats.commit_counts["2"] == 77`; a response without `Link` counts the returned items (1).

- [ ] **Step 4: Implement `fetch_commit_count` and capture counts**

`src/hydrate/repo_client.py`:

```python
def fetch_commit_count(
    client: httpx.Client,
    full_name: str,
    *,
    limiter: BucketLimiter | None = None,
    token_id: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
    on_response: Callable[[httpx.Response, float], None] | None = None,
) -> int | None:
    response = request_with_retry(
        client, "GET", f"{API_BASE}/repos/{full_name}/commits?per_page=1",
        limiter=limiter, token_id=token_id, sleep=sleep, now=now, jitter=jitter,
        on_response=on_response,
    )
    if response.status_code != 200:
        return None
    for part in (response.headers.get("link") or "").split(","):
        segments = part.split(";")
        url_part = segments[0].strip()
        if not (url_part.startswith("<") and url_part.endswith(">")):
            continue
        if any('rel="last"' in segment for segment in segments[1:]):
            params = dict(parse_qsl(urlparse(url_part[1:-1]).query))
            page = params.get("page")
            if page and page.isdigit():
                return int(page)
    try:
        payload = response.json()
    except ValueError:
        return None
    return len(payload) if isinstance(payload, list) else None
```

(`parse_qsl`, `urlparse` imports already exist in the repo style; add as needed.)

In `src/hydrate/tail.py`: `RefreshStats` gains `commit_counts: dict[str, int] = field(default_factory=dict)`; for GraphQL values record `if details.commit_count is not None: stats.commit_counts[key] = details.commit_count`; in the fallback closure, after a successful hydration, `count = fetch_commit_count(...)` and record it against `str(hydrated.id)` when not None.

- [ ] **Step 5: Wire the virtual filter, planner, and runner**

- `src/serve/virtual_params.py`: add rules `max_commits` (int, post “commit count <= max_commits”) and `max_loc` (int, post “lines of code <= max_loc”).
- `src/serve/forms.py`: extend the loop to `("min_stars", "min_commits", "max_commits", "min_loc", "max_loc")`.
- `src/enrich/cost_planner.py`: `min_commits: 2, max_commits: 2, min_loc: 4, max_loc: 4`.
- `src/serve/runner.py`: `_R44_VIRTUALS = ("min_loc",)`; `run_filter` passes `hydration.commit_counts` into `_enrich_handlers`; add the handler branch and function:

```python
def _commit_handler(rows_by_id, virtual, commit_counts, skipped):
    def handler(ids):
        kept = []
        for repo_id in ids:
            count = commit_counts.get(str(repo_id))
            if count is None:
                skipped["commits"] += 1
                continue
            rows_by_id[repo_id]["commit_count"] = count
            low = virtual.get("min_commits")
            high = virtual.get("max_commits")
            if isinstance(low, int) and count < low:
                continue
            if isinstance(high, int) and count > high:
                continue
            kept.append(repo_id)
        return kept, 0
    return handler
```

  `skipped` gains `"commits": 0`; after `execute_segments`, add a warning when `skipped["commits"] > 0`: “N repo(s) could not be checked for commit count; results are incomplete”. `_payload_item` adds `badges["commit_count"] = row["commit_count"]` when present.

- [ ] **Step 6: Write the failing runner test**

Append to `tests/integration/test_runner.py` (extend `rest_item_to_graphql_node`/`graphql_batch_response` with an optional `commit_counts: dict[int, int]` that fills `defaultBranchRef.target.history.totalCount`):

```python
def test_run_filter_enforces_min_and_max_commits(clean: Engine):
    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if path == "/search/repositories":
            return page_response([repo_item(index) for index in (1, 2, 3)])
        if path == "/graphql":
            return graphql_batch_response(
                request,
                {f"owner{i}/repo{i}": repo_item(i) for i in (1, 2, 3)},
                commit_counts={1: 500, 2: 50, 3: 5000},
            )
        raise AssertionError(f"unexpected path {path}")

    client, _requests = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python", virtual={"min_commits": 100, "max_commits": 1000}),
    )
    assert [item.repo_id for item in payload.items] == [1]
    assert not any("min_commits" in warning for warning in payload.warnings)
    assert payload.items[0].virtuals["commit_count"] == 500
```

- [ ] **Step 7: Run the suites**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_repo_adapter.py tests/integration/test_hydrate_batch.py tests/integration/test_runner.py tests/unit/test_virtual_params.py -q`
Expected: PASS. Existing tests asserting the R44 warning for `min_commits` must be updated to assert it only for `min_loc` (never weaken the warning itself).

- [ ] **Step 8: Commit**

```bash
git add src/hydrate/graphql_repo.py src/hydrate/repo_client.py src/hydrate/tail.py src/serve/runner.py src/serve/virtual_params.py src/serve/forms.py src/enrich/cost_planner.py tests/unit/test_graphql_repo_adapter.py tests/integration/test_hydrate_batch.py tests/integration/test_runner.py
git commit -m "feat: enforce min/max commits from batched commit counts"
```

---

### Task 9: Full verification, docs, and status flip

**Files:**
- Modify: `docs/development-log.md`
- Modify: `design/corpus-building-efficient-engineering.md` (§10 status note)
- Test: full suite + coverage

**Interfaces:**
- Consumes: everything above.
- Produces: the documented, verified, merge-ready state.

- [ ] **Step 1: Full suite with coverage**

Run: `$env:PYTHONPATH='src'; $env:GITCRAWL_REQUIRE_TEST_DB='1'; .\.venv\Scripts\python.exe -m pytest --cov=src --cov-report=term-missing -q`
Expected: all tests pass; coverage `>= 93%`. If coverage dips below the floor, add assertions for uncovered branches in `src/lib/graphql_batch.py` (the `batch_errors`/`requeue`/`deadline` paths), never by removing tests.

- [ ] **Step 2: Lint and format**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m ruff check src tests; .\.venv\Scripts\python.exe -m black --check src tests`
Expected: clean.

- [ ] **Step 3: Prove the isolation guarantee end-to-end (once, manually)**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/integration/test_hydrate_batch.py::test_one_bad_repo_falls_back_to_rest_without_touching_its_neighbours tests/integration/test_runner.py::test_run_filter_batches_hydration_and_isolates_a_bad_repo -v`
Expected: both PASS. These two tests are the contract the user asked for: one bad repo never cuts off its neighbours.

- [ ] **Step 4: Update the design status**

In `design/corpus-building-efficient-engineering.md` §10 opening, change "This section records the design we settled on" to note the engine is now built, and add one line: "Status: built (see `docs/superpowers/plans/2026-10-02-graphql-batch-engine.md`); REST paths remain as fallbacks."

- [ ] **Step 5: Append the outcome to `docs/development-log.md`**

Add a section with: date, what was built, the two isolation tests, the dedicated meter, test count, coverage, and any rulings (e.g. `RepoDetails.payload` is REST-shaped to reuse `upsert_repos` unchanged; `RefreshStats` fallback returns `None` so the engine counts `handled` while the adapter counts the real outcome).

- [ ] **Step 6: Commit**

```bash
git add docs/development-log.md design/corpus-building-efficient-engineering.md
git commit -m "docs: log the graphql batch engine outcome"
```

---

## Self-Review Checklist (run after implementation)

- [ ] **Scope (saving + current checks):** repo details batch (Task 5), file presence batch (Task 6), owner location batch (Task 7).
- [ ] **min/max commits:** enforced from default-branch counts (Task 8); `min_loc`/`max_loc` remain recorded-only and are greyed in the UI.
- [ ] **Parse-first, keep good data:** `test_fetch_batch_never_discards_good_data_when_errors_and_data_share_a_reply`, `test_fetch_batch_keeps_good_results_when_one_alias_errors`.
- [ ] **Per-repo error attribution by `path`:** Task 3 tests + `_post` alias mapping.
- [ ] **Split only failures, bounded:** `test_transient_batch_error_splits_and_only_failing_keys_are_resent`, `test_single_key_transient_failure_goes_to_fallback_after_attempts`.
- [ ] **REST fallback + unresolved with reason:** `test_fallback_failure_is_recorded_as_unresolved_with_reason`, Task 5 unresolved test.
- [ ] **No stalls:** deadline checks in `fetch_batch`; `test_deadline_expiry_stops_requests_and_marks_remaining_unresolved`.
- [ ] **Loud auth/SSO failure:** `test_http_401_aborts_loudly`, `test_sso_partial_results_propagates`, Task 5 401 test.
- [ ] **Dedicated meter:** Task 1 tests.
- [ ] **End-of-run report:** `field_stats["graphql"]` + unresolved warnings; Task 5 Steps 6 and 8.
- [ ] **Backwards compatible:** existing REST `refresh_repos`/`fetch_tree`/`_fetch_owner_location` remain as fallbacks; no schema change; goldens unchanged (golden app uses a canned runner).
- [ ] **Detection bridge intact:** the file adapter is a general path checker; the deferred detection plan can reuse it for `CLAUDE.md`, `.cursor/`, `AGENTS.md` without modification.
