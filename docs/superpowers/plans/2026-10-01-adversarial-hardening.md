# Adversarial-Path Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the audited web-security and outage findings by adding origin-aware CSRF, Host allowlisting, request-body caps, a single deadline policy, clone timeout/cancellation, Redis fail-loud, a single-flight health probe, and malformed-input tolerance — without changing one byte of normal-use behavior.

**Architecture:** Two pure-ASGI middlewares (`OriginCsrfMiddleware`, `BodyLimitMiddleware`) plus Starlette's `TrustedHostMiddleware` wrap the app; a `Deadline` value object in `lib` is bound to the per-run limiter and propagated to executor/payload-cache waits, surfacing as HTTP 503 `{"error":"timeout"}`; clone lifecycle gains an env-driven timeout and a `threading.Event` cancellation path exposed through `DELETE /runs/{id}/clone`; `/health` gains a `redis_degraded` key (only when `REDIS_URL` is set and unreachable) and replaces per-probe abandoned threads with one single-flight probe.

**Tech Stack:** FastAPI 0.142.2 / Starlette 1.7.0 (pure ASGI middleware), SQLAlchemy 2.1 + psycopg3, redis-py 8.1 + fakeredis, pytest + FastAPI TestClient, concurrent.futures, threading.Event.

**Spec:** docs/superpowers/specs/2026-10-01-quality-hardening-design.md (Phase 2, §6; binding invariants §2; exception register §11)

## Global Constraints

- **INV-1 — Identical normal behavior.** For normal use, every observable output (HTTP status, body, headers except additive ones, files, DB rows, ordering, counts) is identical to today.
- **INV-2 — Additive-only UI.** No existing control moves, disappears, gains a required step, changes meaning, or changes its result.
- **INV-3 — Allowed behavior changes.** Behavior may differ only for: (1) cross-site requests (`Origin`/`Referer` present and mismatched, or non-local `Host`), (2) requests that hang beyond a generous deadline (today: hang forever), (3) request bodies above a generous cap (today: unbounded), (4) visibility of infrastructure outage (today: silent), and (5) the single approved visible fix (run summary counters), which requires explicit sign-off before its task executes.
- **INV-4 — No runtime surface.** CI, formatting, typing, coverage, fixture centralization, annotation and documentation work change no runtime behavior.
- **INV-5 — Exception register.** Any discovered unavoidable deviation is recorded in §11 with an explicit approval checkbox before it ships.
- Every task below changes only adversarial/hung/oversized/outage paths and must state its INV-3 category and regression check.
- **Explicitly out of scope:** adding authentication; changing error codes for consistency (visible; exception E2 stays as-is); enabling clone worker parallelism; adding `/vsearch` concurrency caps; deleting unwired code; any Phase 3 UI work.
- Python 3.12; ruff line length 100; `black` is the formatter authority; one commit per task, prefix `hardening:`.
- Every task's regression check runs the existing contract suites named in that task; integration tests need `TEST_DATABASE_URL` ending `_test` and otherwise skip (existing behavior).
- Deadline defaults must be ≥ 4× the worst legitimate run; the numeric justification is in Task 4. Clone default per-repo timeout is 1800 s.

---

## File Structure

| File | Change |
|---|---|
| `src/serve/middleware.py` | **new** — `OriginCsrfMiddleware`, `BodyLimitMiddleware`, body-cap constants |
| `src/lib/deadlines.py` | **new** — `Deadline`, `DeadlineExceededError`, env policy functions |
| `src/serve/app.py` | wire middlewares; bound `payload_cache`/executor waits; `_timeout_response` |
| `src/serve/payload_cache.py` | `run_once(..., timeout=None)` waiter bound |
| `src/limiter/buckets.py` | `BucketLimiter` deadline binding + property |
| `src/lib/gh_client.py` | bound the limiter denial sleep |
| `src/serve/runner.py` | bind/clear the per-run deadline; Redis fail-loud |
| `src/enrich/cloner.py` | `CloneCancelled`, terminal-safe git env, cancel/timeout-aware runner, cancel-aware `clone_repos` |
| `src/serve/runs.py` | `CloneRegistry` cancel events; `start_clone` timeout/event wiring; `cancel_clone`; tolerant `read_clone_progress` |
| `src/serve/pages.py` | `DELETE /runs/{id}/clone`; `redis_degraded`; `_Probe` single-flight health |
| `src/serve/forms.py` | `_int_or_raw` rejects non-ASCII digits |
| `src/scheduler/state_machine.py` | `set_state` refreshes `updated_at` |
| `tests/conftest.py` | autouse `GITCRAWL_ALLOWED_HOSTS=testserver` for TestClient |
| `tests/contract/test_adversarial_hardening.py` | **new** — origin/host/body/deadline/redis/health contract tests |
| `tests/unit/test_body_limit.py` | **new** — ASGI-level cap tests |
| `tests/unit/test_deadlines.py` | **new** — clock-injected deadline tests |
| `tests/unit/test_health_probe.py` | **new** — `_Probe` tests |
| `tests/unit/test_redis_fallback.py` | **new** — fallback/strict/log tests |
| `tests/unit/test_forms.py` | **new** — non-ASCII digit tests |
| `tests/unit/test_run_cache.py` | waiter timeout test |
| `tests/unit/test_gh_client.py` | deadline-bounded deny-loop tests |
| `tests/unit/test_cloner.py` | terminal env + cancel tests |
| `tests/integration/test_state_machine.py` | `updated_at` test |
| `tests/contract/test_clone_endpoints.py` | cancel endpoint + malformed progress tests; stub kwargs |
| `tests/contract/test_vsearch_api.py`, `tests/contract/test_console_pages.py`, `tests/contract/test_pages.py` | regression only |

---

### Task 1: Origin-aware CSRF middleware

**Files:**
- Create: `src/serve/middleware.py`
- Modify: `src/serve/app.py:286` (immediately after `application = FastAPI(title="gitcrawl", version="0.0.1")`)
- Test: `tests/contract/test_adversarial_hardening.py` (new)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `STATE_CHANGING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})`
  - `OriginCsrfMiddleware(app: ASGIApp)` — pure ASGI; rejects with plain-text 403 `"cross-origin request rejected"`.
  - `_same_origin(candidate: str, base: URL) -> bool` — scheme/host/effective-port comparison (default ports 80/443).

**Normal-use invariance:** INV-3 category 1 (cross-site requests). Same-origin browser forms/htmx and script/curl clients behave exactly as today: no Origin/Referer means pass, matching Origin means pass, and existing per-route `validate_csrf` (`src/serve/pages.py:473-492`) is untouched. Regression: `pytest tests/contract/test_console_pages.py tests/contract/test_console_api.py tests/contract/test_filter_form.py -q`.

- [ ] **Step 1: Write the failing tests**

Create `tests/contract/test_adversarial_hardening.py`:

```python
from __future__ import annotations

import threading
import time

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from serve.app import create_app
from serve.executor import RunPayload, RunPayloadItem

FILTER = {"gitcrawl_filter": 1, "q": "language:rust"}


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(alembic_engine: Engine) -> Engine:
    with alembic_engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE run_items, runs, saved_filters, audit_log, shards, geo_cache, "
                "owners, repos, full_name_history RESTART IDENTITY CASCADE"
            )
        )
    return alembic_engine


def payload_item(repo_id: int = 1, full_name: str = "octo/hello") -> RunPayloadItem:
    return RunPayloadItem(repo_id=repo_id, full_name=full_name, stargazers=10, virtuals={})


def counting_runner(payload: RunPayload, calls: list):
    def runner(run_id: int, filter_spec: dict) -> RunPayload:
        calls.append(filter_spec)
        return payload

    return runner


def make_client(engine: Engine, tmp_path, *, runner=None, **kwargs) -> TestClient:
    if runner is None:
        runner = counting_runner(RunPayload(total_count=0, items=[]), [])
    application = create_app(
        engine=engine,
        runner_factory=lambda _engine: runner,
        runs_root=str(tmp_path / "runs"),
        clone_root=str(tmp_path / "clones"),
        **kwargs,
    )
    return TestClient(application, raise_server_exceptions=False)


def test_cross_origin_post_is_rejected(clean: Engine, tmp_path):
    calls: list = []
    client = make_client(clean, tmp_path, runner=counting_runner(RunPayload(0, []), calls))

    response = client.post(
        "/vsearch/run", json=FILTER, headers={"Origin": "http://evil.example"}
    )

    assert response.status_code == 403
    assert calls == []


def test_same_origin_post_passes(clean: Engine, tmp_path):
    calls: list = []
    client = make_client(
        clean, tmp_path, runner=counting_runner(RunPayload(total_count=0, items=[]), calls)
    )

    response = client.post("/vsearch/run", json=FILTER, headers={"Origin": "http://testserver"})

    assert response.status_code == 200
    assert len(calls) == 1


def test_default_port_origin_passes(clean: Engine, tmp_path):
    calls: list = []
    client = make_client(
        clean, tmp_path, runner=counting_runner(RunPayload(total_count=0, items=[]), calls)
    )

    response = client.post(
        "/vsearch/run", json=FILTER, headers={"Origin": "http://testserver:80"}
    )

    assert response.status_code == 200


def test_post_without_origin_passes(clean: Engine, tmp_path):
    calls: list = []
    client = make_client(
        clean, tmp_path, runner=counting_runner(RunPayload(total_count=0, items=[]), calls)
    )

    response = client.post("/vsearch/run", json=FILTER)

    assert response.status_code == 200
    assert len(calls) == 1


@pytest.mark.parametrize(
    "origin",
    ["https://testserver", "http://testserver:9999", "null"],
)
def test_mismatched_origin_is_rejected(clean: Engine, tmp_path, origin):
    client = make_client(clean, tmp_path)

    response = client.post("/vsearch/run", json=FILTER, headers={"Origin": origin})

    assert response.status_code == 403


def test_cross_origin_referer_is_rejected_when_origin_is_absent(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    response = client.post(
        "/vsearch/run", json=FILTER, headers={"Referer": "http://evil.example/find"}
    )

    assert response.status_code == 403


def test_same_origin_referer_passes_when_origin_is_absent(clean: Engine, tmp_path):
    calls: list = []
    client = make_client(
        clean, tmp_path, runner=counting_runner(RunPayload(total_count=0, items=[]), calls)
    )

    response = client.post(
        "/vsearch/run", json=FILTER, headers={"Referer": "http://testserver/runs/1"}
    )

    assert response.status_code == 200
    assert len(calls) == 1


def test_cross_origin_delete_clone_is_rejected_before_routing(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    response = client.delete("/runs/999999/clone", headers={"Origin": "http://evil.example"})

    assert response.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/contract/test_adversarial_hardening.py -k origin -v`
Expected: FAIL — cross-origin POST currently returns 200; the module `serve.middleware` does not exist yet.

- [ ] **Step 3: Implement the middleware**

Create `src/serve/middleware.py`:

```python
from __future__ import annotations

from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _same_origin(candidate: str, base) -> bool:
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or parsed.hostname is None:
        return False
    if parsed.hostname != base.hostname or parsed.scheme != base.scheme:
        return False
    return (parsed.port or _default_port(parsed.scheme)) == (
        base.port or _default_port(base.scheme)
    )


class OriginCsrfMiddleware:
    """Reject cross-site state-changing requests when Origin/Referer is present.

    Requests without Origin and Referer pass unchanged (script clients). Per-route
    CSRF validation stays in place as defense in depth.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in STATE_CHANGING_METHODS:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        source = origin if origin is not None else headers.get("referer")
        if source is not None and not _same_origin(source, Request(scope, receive).base_url):
            response = PlainTextResponse("cross-origin request rejected", status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
```

In `src/serve/app.py`, add the import next to the other `serve` imports:

```python
from serve.middleware import OriginCsrfMiddleware
```

and insert immediately after `application = FastAPI(title="gitcrawl", version="0.0.1")` (`src/serve/app.py:286`):

```python
    application.add_middleware(OriginCsrfMiddleware)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/contract/test_adversarial_hardening.py -k origin -v`
Expected: PASS (8 origin tests).

Run the regression check: `pytest tests/contract/test_console_pages.py tests/contract/test_console_api.py tests/contract/test_filter_form.py -q`
Expected: PASS — form posts send no Origin/Referer under TestClient and are unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/serve/middleware.py src/serve/app.py tests/contract/test_adversarial_hardening.py
git commit -m "hardening: reject cross-origin state-changing requests"
```

---

### Task 2: Host allowlist (loopback defaults + env extension)

**Files:**
- Modify: `src/serve/app.py` (import `TrustedHostMiddleware`; add `_allowed_hosts`; insert one `add_middleware` call)
- Modify: `tests/conftest.py` (append autouse fixture)
- Test: `tests/contract/test_adversarial_hardening.py`

**Interfaces:**
- Consumes: Task 1's middleware registration site.
- Produces: `_allowed_hosts() -> list[str]` returning `["localhost", "127.0.0.1", "[::1]"]` plus comma-separated `GITCRAWL_ALLOWED_HOSTS` entries.
- No public Python signature changes; the bind default stays `GITCRAWL_HOST`/`127.0.0.1` in `src/serve/__main__.py:9`.

**Normal-use invariance:** INV-3 category 1 (non-local `Host`). Browsers on loopback send `Host: localhost` / `127.0.0.1` / `[::1]`, which are all allowed; `GITCRAWL_ALLOWED_HOSTS` keeps proxy deployments working. TestClient sends `Host: testserver`, so the test suite gets it via the conftest env extension — production behavior for real hosts is unchanged. Regression: the same three contract suites as Task 1 plus `pytest tests/contract/test_pages.py -q`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/contract/test_adversarial_hardening.py`:

```python
@pytest.fixture(autouse=True)
def _allow_testclient_host(monkeypatch):
    monkeypatch.setenv("GITCRAWL_ALLOWED_HOSTS", "testserver")


@pytest.mark.parametrize(
    "base_url",
    ["http://localhost", "http://127.0.0.1:8000", "http://[::1]:8000"],
)
def test_loopback_hosts_are_allowed(clean: Engine, tmp_path, base_url):
    application = create_app(engine=clean, runs_root=str(tmp_path / "runs"))
    client = TestClient(application, base_url=base_url, raise_server_exceptions=False)

    response = client.get("/health")

    assert response.status_code == 200


def test_non_local_host_is_rejected(clean: Engine, tmp_path):
    application = create_app(engine=clean, runs_root=str(tmp_path / "runs"))
    client = TestClient(application, base_url="http://evil.example", raise_server_exceptions=False)

    response = client.get("/health")

    assert response.status_code == 400
    assert "Invalid host header" in response.text


def test_allowed_hosts_env_extends_the_allowlist(clean: Engine, tmp_path, monkeypatch):
    monkeypatch.setenv("GITCRAWL_ALLOWED_HOSTS", "gitcrawl.internal")
    application = create_app(engine=clean, runs_root=str(tmp_path / "runs"))

    allowed = TestClient(
        application, base_url="http://gitcrawl.internal", raise_server_exceptions=False
    )
    replaced = TestClient(application, base_url="http://testserver", raise_server_exceptions=False)

    assert allowed.get("/health").status_code == 200
    assert replaced.get("/health").status_code == 400
```

Note: the fixture is duplicated here on purpose so this task stays runnable before the root-conftest fixture exists (Python resolves the closest fixture; this file-local one wins and is equivalent).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/contract/test_adversarial_hardening.py -k host -v`
Expected: FAIL — `evil.example` gets 200 because no Host allowlist is installed.

- [ ] **Step 3: Implement the allowlist**

In `src/serve/app.py`, add to the imports:

```python
from starlette.middleware.trustedhost import TrustedHostMiddleware
```

Add the helper near the other module constants (after `_GET_PARAM_SET`, `src/serve/app.py:51-52`):

```python
def _allowed_hosts() -> list[str]:
    hosts = ["localhost", "127.0.0.1", "[::1]"]
    extra = os.environ.get("GITCRAWL_ALLOWED_HOSTS", "")
    hosts.extend(host.strip() for host in extra.split(",") if host.strip())
    return hosts
```

Insert after the Origin middleware line added in Task 1:

```python
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=_allowed_hosts(),
        www_redirect=False,
    )
```

Append to `tests/conftest.py` (after the `alembic_engine` fixture):

```python
@pytest.fixture(autouse=True)
def allow_testclient_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITCRAWL_ALLOWED_HOSTS", "testserver")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/contract/test_adversarial_hardening.py -k host -v`
Expected: PASS (5 host tests).

Run the regression check: `pytest tests/contract/test_pages.py tests/contract/test_console_pages.py tests/contract/test_console_api.py tests/contract/test_filter_form.py tests/contract/test_vsearch_api.py -q`
Expected: PASS — the root-conftest fixture makes `testserver` allowed for every existing TestClient.

- [ ] **Step 5: Commit**

```bash
git add src/serve/app.py tests/conftest.py tests/contract/test_adversarial_hardening.py
git commit -m "hardening: allowlist loopback Host headers"
```

---

### Task 3: Body caps (1 MiB JSON, 10 MiB `/find` upload)

**Files:**
- Modify: `src/serve/middleware.py` (append `BodyLimitMiddleware` + constants)
- Modify: `src/serve/app.py` (import constants + middleware; register)
- Test: `tests/contract/test_adversarial_hardening.py`, `tests/unit/test_body_limit.py` (new)

**Interfaces:**
- Consumes: Task 1's `STATE_CHANGING_METHODS`.
- Produces:
  - `JSON_BODY_LIMIT_BYTES = 1024 * 1024`, `UPLOAD_BODY_LIMIT_BYTES = 10 * 1024 * 1024`
  - `BodyLimitMiddleware(app, *, default_limit: int, path_limits: Mapping[str, int] | None = None)` — pure ASGI; buffers at most `limit` bytes, returns 413 JSON `{"error": "payload_too_large", "limit": <int>}`.

**Normal-use invariance:** INV-3 category 3 (oversized bodies). Normal JSON bodies and filter-spec uploads are kilobytes; the middleware buffers/replays them unchanged. Regression: same suites as Task 2 plus `pytest tests/contract/test_clone_endpoints.py tests/contract/test_run_detail.py -q`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/contract/test_adversarial_hardening.py` (add the import at the top: `from serve.middleware import JSON_BODY_LIMIT_BYTES, UPLOAD_BODY_LIMIT_BYTES`):

```python
@pytest.mark.parametrize("size", [JSON_BODY_LIMIT_BYTES - 1, JSON_BODY_LIMIT_BYTES])
def test_json_body_at_or_below_the_cap_is_parsed_not_rejected(clean: Engine, tmp_path, size):
    client = make_client(clean, tmp_path)

    response = client.post(
        "/vsearch/run",
        content=b"x" * size,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_param"


def test_json_body_above_the_cap_is_413(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    response = client.post(
        "/vsearch/run",
        content=b"x" * (JSON_BODY_LIMIT_BYTES + 1),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {
        "error": "payload_too_large",
        "limit": JSON_BODY_LIMIT_BYTES,
    }


@pytest.mark.parametrize("size", [UPLOAD_BODY_LIMIT_BYTES - 1, UPLOAD_BODY_LIMIT_BYTES])
def test_find_upload_at_or_below_the_cap_is_not_413(clean: Engine, tmp_path, size):
    client = make_client(clean, tmp_path)

    response = client.post("/find", content=b"x" * size)

    assert response.status_code == 403  # CSRF check, i.e. the cap did not trigger


def test_find_upload_above_the_cap_is_413(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    response = client.post("/find", content=b"x" * (UPLOAD_BODY_LIMIT_BYTES + 1))

    assert response.status_code == 413


def test_chunked_body_without_content_length_is_capped(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    def chunks():
        yield b"x" * (JSON_BODY_LIMIT_BYTES // 2)
        yield b"x" * (JSON_BODY_LIMIT_BYTES // 2)
        yield b"x"

    response = client.post(
        "/vsearch/run", content=chunks(), headers={"content-type": "application/json"}
    )

    assert response.status_code == 413
```

Create `tests/unit/test_body_limit.py`:

```python
from __future__ import annotations

import asyncio

from serve.middleware import BodyLimitMiddleware


def run_middleware(middleware, scope):
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    return sent


def test_declared_content_length_over_the_limit_is_rejected_without_reading():
    called: list[bool] = []

    async def app(scope, receive, send):  # pragma: no cover - must not run
        called.append(True)

    middleware = BodyLimitMiddleware(app, default_limit=4)

    messages = run_middleware(
        middleware,
        {
            "type": "http",
            "method": "POST",
            "path": "/vsearch/run",
            "headers": [(b"content-length", b"5")],
        },
    )

    assert called == []
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 413


def test_path_specific_limit_allows_a_larger_upload():
    async def app(scope, receive, send):
        message = await receive()
        assert message["body"] == b"123456"
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = BodyLimitMiddleware(app, default_limit=4, path_limits={"/find": 8})
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"123456", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(
        middleware(
            {"type": "http", "method": "POST", "path": "/find", "headers": []},
            receive,
            send,
        )
    )

    assert sent[0]["status"] == 204
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_body_limit.py -v`
Expected: FAIL — cannot import `BodyLimitMiddleware`.

Run: `pytest tests/contract/test_adversarial_hardening.py -k "cap or chunked" -v`
Expected: FAIL — oversized bodies currently reach route parsing (400/403, never 413).

- [ ] **Step 3: Implement the cap middleware**

First, extend the top-of-file import block of `src/serve/middleware.py` to its final form:

```python
from __future__ import annotations

import json
from collections.abc import Mapping
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send
```

Then append the constants and class to `src/serve/middleware.py`:

```python
JSON_BODY_LIMIT_BYTES = 1024 * 1024
UPLOAD_BODY_LIMIT_BYTES = 10 * 1024 * 1024


class BodyLimitMiddleware:
    """Cap state-changing request bodies before the route reads them."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        default_limit: int,
        path_limits: Mapping[str, int] | None = None,
    ) -> None:
        self.app = app
        self.default_limit = default_limit
        self.path_limits = dict(path_limits or {})

    def limit_for(self, path: str) -> int:
        return self.path_limits.get(path, self.default_limit)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in STATE_CHANGING_METHODS:
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope["path"])
        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    await self._reject(scope, receive, send, limit)
                    return
            except ValueError:
                pass
        buffered: list[dict] = []
        received = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            received += len(message.get("body", b""))
            if received > limit:
                await self._reject(scope, receive, send, limit)
                return
            buffered.append(message)
            if not message.get("more_body", False):
                break
        iterator = iter(buffered)

        async def replay() -> dict:
            try:
                return next(iterator)
            except StopIteration:
                return {"type": "http.disconnect"}

        await self.app(scope, replay, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        body = json.dumps({"error": "payload_too_large", "limit": limit}).encode()
        response = Response(body, status_code=413, media_type="application/json")
        await response(scope, receive, send)
```

In `src/serve/app.py`, extend the middleware import and register the cap middleware after the TrustedHost block:

```python
from serve.middleware import (
    JSON_BODY_LIMIT_BYTES,
    UPLOAD_BODY_LIMIT_BYTES,
    BodyLimitMiddleware,
    OriginCsrfMiddleware,
)
```

```python
    application.add_middleware(
        BodyLimitMiddleware,
        default_limit=JSON_BODY_LIMIT_BYTES,
        path_limits={"/find": UPLOAD_BODY_LIMIT_BYTES},
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_body_limit.py tests/contract/test_adversarial_hardening.py -k "cap or chunked or limit" -v`
Expected: PASS.

Run the regression check: `pytest tests/contract -q`
Expected: PASS — all normal bodies are far below the caps.

- [ ] **Step 5: Commit**

```bash
git add src/serve/middleware.py src/serve/app.py tests/contract/test_adversarial_hardening.py tests/unit/test_body_limit.py
git commit -m "hardening: cap JSON and upload request bodies"
```

---

### Task 4: Deadline policy and bounded limiter waits

**Files:**
- Create: `src/lib/deadlines.py`
- Modify: `src/limiter/buckets.py:115-147`, `src/lib/gh_client.py:128-139`, `src/serve/runner.py:505-510`
- Test: `tests/unit/test_deadlines.py` (new), `tests/unit/test_gh_client.py`

**Interfaces:**
- Consumes: nothing (stdlib only).
- Produces:
  - `DeadlineExceededError(TimeoutError)` with `.retry_after: float`
  - `Deadline(seconds: float, *, clock: Callable[[], float] = time.monotonic)`, `.remaining: float`, `.bound_wait(seconds: float) -> None` (raises when `seconds > remaining`)
  - `request_deadline_seconds() -> float` (`GITCRAWL_REQUEST_DEADLINE_SECONDS`, default 3600.0)
  - `clone_timeout_seconds() -> float` (`GITCRAWL_CLONE_TIMEOUT_SECONDS`, default 1800.0)
  - `BucketLimiter(..., deadline: Deadline | None = None)`, `.bind_deadline(deadline)`, `.deadline` property
  - `run_filter` binds a fresh `Deadline` to `deps.limiter` for the run and clears it in `finally`.

**Where the module lives:** `src/lib/deadlines.py`, not `src/serve/`, because `lib/gh_client.py` and `limiter/buckets.py` must consume it and core packages must not import `serve` (Phase 1 import-boundary rule). `serve` imports from `lib`, which is the allowed direction.

**Numeric justification (defaults ≥ 4× worst legitimate run):**

| Legitimate worst case | Bound | Budget | Floor |
|---|---|---|---|
| Root count + lazy planner probes (≤ 2 × `max_shards`) | ≤ 21 search calls | search 30/min | 42 s |
| Discovery pages (`max_shards=10` × `max_pages=10`) | ≤ 100 search calls | search 30/min | 200 s |
| Hydration (`max_hydrate=200`) | ≤ 200 core calls | core 5000/hr | 144 s |
| Enrich (`max_enrich=100`, trees + users) | ≤ 100 core calls | core 5000/hr | 72 s |
| **Total network-budget floor** | ≤ 421 calls | — | **≈ 458 s (7.6 min)** |

`3600 s ≈ 7.9×` the worst legitimate run, and it also absorbs queueing behind one bulk run. Clone timeout `1800 s` is ≈ 18× a 100 s 1 GB shallow clone.

**Normal-use invariance:** INV-3 category 2 (hangs beyond a generous deadline). The limiter sleeps exactly as before when no deadline is bound or the wait fits inside the deadline; `run_filter` binds for the duration of a normal run, which finishes far below 3600 s. Regression: `pytest tests/unit/test_gh_client.py tests/unit/test_buckets.py tests/integration/test_runner.py -q`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_deadlines.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

import fakeredis
import pytest

from lib.deadlines import (
    DEFAULT_CLONE_TIMEOUT_SECONDS,
    DEFAULT_REQUEST_DEADLINE_SECONDS,
    Deadline,
    DeadlineExceededError,
    clone_timeout_seconds,
    request_deadline_seconds,
)
from limiter.buckets import BucketLimiter
from serve import runner as runner_module


def test_defaults_are_generous():
    assert DEFAULT_REQUEST_DEADLINE_SECONDS == 3600.0
    assert DEFAULT_CLONE_TIMEOUT_SECONDS == 1800.0


def test_env_overrides_are_read(monkeypatch):
    monkeypatch.setenv("GITCRAWL_REQUEST_DEADLINE_SECONDS", "120")
    monkeypatch.setenv("GITCRAWL_CLONE_TIMEOUT_SECONDS", "60")
    assert request_deadline_seconds() == 120.0
    assert clone_timeout_seconds() == 60.0


@pytest.mark.parametrize("raw", ["", "abc", "0", "-5"])
def test_invalid_env_values_fall_back_to_defaults(monkeypatch, raw):
    monkeypatch.setenv("GITCRAWL_REQUEST_DEADLINE_SECONDS", raw)
    assert request_deadline_seconds() == DEFAULT_REQUEST_DEADLINE_SECONDS


def test_remaining_uses_the_injected_clock():
    now = [0.0]
    deadline = Deadline(10.0, clock=lambda: now[0])
    assert deadline.remaining == pytest.approx(10.0)
    now[0] = 9.5
    assert deadline.remaining == pytest.approx(0.5)


def test_bound_wait_raises_only_when_the_wait_exceeds_the_deadline():
    now = [0.0]
    deadline = Deadline(10.0, clock=lambda: now[0])
    assert deadline.bound_wait(10.0) is None
    with pytest.raises(DeadlineExceededError) as excinfo:
        deadline.bound_wait(10.001)
    assert excinfo.value.retry_after == 10.001
    now[0] = 10.0
    with pytest.raises(DeadlineExceededError):
        deadline.bound_wait(0.001)


def test_run_filter_binds_a_deadline_for_the_run_and_clears_it(monkeypatch):
    limiter = BucketLimiter(fakeredis.FakeRedis(), specs={"core": (1, 3600.0)})
    deps = SimpleNamespace(limiter=limiter, audit_buffer=None)
    seen: dict[str, object] = {}

    def fake_inner(deps, spec, *, config=None):
        seen["during"] = limiter.deadline
        return "payload"

    monkeypatch.setattr(runner_module, "_run_filter", fake_inner)

    assert runner_module.run_filter(deps, object()) == "payload"
    assert isinstance(seen["during"], Deadline)
    assert limiter.deadline is None
```

Append to `tests/unit/test_gh_client.py`:

```python
def test_limiter_deadline_stops_before_an_overlong_sleep():
    from lib.deadlines import Deadline, DeadlineExceededError

    redis = fakeredis.FakeRedis()
    now = [1000.0]
    limiter = BucketLimiter(
        redis,
        specs={"search": (0, 60.0)},
        max_concurrent=100,
        deadline=Deadline(5.0, clock=lambda: now[0]),
    )
    sleeps = []
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )

    with pytest.raises(DeadlineExceededError) as excinfo:
        request_with_retry(
            client,
            "GET",
            "https://api.github.com/search/repositories?q=x",
            limiter=limiter,
            token_id="tok",
            max_attempts=2,
            sleep=sleeps.append,
            now=lambda: now[0],
        )

    assert sleeps == []
    assert excinfo.value.retry_after == 20.0


def test_limiter_without_a_deadline_sleeps_exactly_as_before():
    redis = fakeredis.FakeRedis()
    limiter = BucketLimiter(redis, specs={"search": (0, 60.0)}, max_concurrent=100)
    sleeps = []
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )

    with pytest.raises(ThrottledError):
        request_with_retry(
            client,
            "GET",
            "https://api.github.com/search/repositories?q=x",
            limiter=limiter,
            token_id="tok",
            max_attempts=2,
            sleep=sleeps.append,
            now=lambda: 1000.0,
        )

    assert sleeps == [20.0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_deadlines.py -v`
Expected: FAIL — `lib.deadlines` does not exist.

Run: `pytest tests/unit/test_gh_client.py -k deadline -v`
Expected: FAIL — `BucketLimiter` has no `deadline` parameter.

- [ ] **Step 3: Implement the module and wiring**

Create `src/lib/deadlines.py`:

```python
from __future__ import annotations

import os
import time
from collections.abc import Callable

DEFAULT_REQUEST_DEADLINE_SECONDS = 3600.0
DEFAULT_CLONE_TIMEOUT_SECONDS = 1800.0


class DeadlineExceededError(TimeoutError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"deadline exceeded; retry after {retry_after:.0f}s")
        self.retry_after = float(retry_after)


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def request_deadline_seconds() -> float:
    return _env_seconds("GITCRAWL_REQUEST_DEADLINE_SECONDS", DEFAULT_REQUEST_DEADLINE_SECONDS)


def clone_timeout_seconds() -> float:
    return _env_seconds("GITCRAWL_CLONE_TIMEOUT_SECONDS", DEFAULT_CLONE_TIMEOUT_SECONDS)


class Deadline:
    def __init__(self, seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._expires_at = clock() + seconds

    @property
    def remaining(self) -> float:
        return self._expires_at - self._clock()

    def bound_wait(self, seconds: float) -> None:
        if seconds > self.remaining:
            raise DeadlineExceededError(seconds)
```

In `src/limiter/buckets.py`, add the import and extend `BucketLimiter.__init__` (`src/limiter/buckets.py:115-125`):

```python
from lib.deadlines import Deadline
```

```python
    def __init__(
        self,
        redis,
        *,
        specs: dict[str, tuple[int, float]] = RESOURCE_SPECS,
        max_concurrent: int = 10,
        deadline: Deadline | None = None,
    ) -> None:
        self._redis = redis
        self._specs = specs
        self._max_concurrent = max_concurrent
        self._deadline = deadline

    def bind_deadline(self, deadline: Deadline | None) -> None:
        self._deadline = deadline

    @property
    def deadline(self) -> Deadline | None:
        return self._deadline
```

In `src/lib/gh_client.py`, add the import and change the deny branch (`src/lib/gh_client.py:130-139`):

```python
from lib.deadlines import DeadlineExceededError
```

```python
        if limiter is not None:
            acquired = limiter.acquire(resource, token_id, now=now())
            if not acquired.allowed:
                denials += 1
                if denials >= max_attempts:
                    raise ThrottledError(acquired.retry_after)
                if limiter.deadline is not None:
                    limiter.deadline.bound_wait(acquired.retry_after)
                sleep(acquired.retry_after)
                continue
            denials = 0
```

In `src/serve/runner.py`, add the import and bind the run deadline in `run_filter` (`src/serve/runner.py:505-510`):

```python
from lib.deadlines import Deadline, request_deadline_seconds
```

```python
def run_filter(deps: Deps, spec: FilterSpec, *, config: RunnerConfig | None = None) -> RunPayload:
    if deps.limiter is not None:
        deps.limiter.bind_deadline(Deadline(request_deadline_seconds()))
    try:
        return _run_filter(deps, spec, config=config)
    finally:
        if deps.limiter is not None:
            deps.limiter.bind_deadline(None)
        if deps.audit_buffer is not None:
            deps.audit_buffer.flush()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_deadlines.py tests/unit/test_gh_client.py -q`
Expected: PASS — including the pre-existing `test_request_with_retry_deny_loop_raises_throttled_error_without_http` (`sleeps == [20.0, 20.0]`, no deadline bound).

Run the regression check: `pytest tests/unit/test_buckets.py tests/integration/test_runner.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lib/deadlines.py src/limiter/buckets.py src/lib/gh_client.py src/serve/runner.py tests/unit/test_deadlines.py tests/unit/test_gh_client.py
git commit -m "hardening: add deadline policy and bound limiter waits"
```

---

### Task 5: Bound executor and payload-cache waits; map expiry to 503

**Files:**
- Modify: `src/serve/payload_cache.py:53-78`, `src/serve/app.py:315-400` and `:402-477`
- Test: `tests/unit/test_run_cache.py`, `tests/contract/test_adversarial_hardening.py`

**Interfaces:**
- Consumes: Task 4's `DeadlineExceededError`, `request_deadline_seconds()`.
- Produces:
  - `RunPayloadCache.run_once(key, producer, *, timeout: float | None = None) -> T`; waiters bound by `timeout`, converting `TimeoutError` to `DeadlineExceededError`.
  - `_timeout_response(exc: DeadlineExceededError) -> JSONResponse` returning HTTP 503 `{"error": "timeout", "retry_after": <int seconds ≥ 1>}`.
  - `GET /vsearch/repos` and `POST /vsearch/run` return that 503 when the deadline expires.

**Normal-use invariance:** INV-3 category 2. `timeout=None` keeps `run_once`'s old blocking behavior; app routes pass the 3600 s default, and normal runs complete in ≤ ~8 minutes. Regression: `pytest tests/contract/test_vsearch_api.py tests/unit/test_run_cache.py -q`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_run_cache.py`:

```python
def test_run_once_waiter_timeout_raises_deadline_error():
    from concurrent.futures import Future

    from lib.deadlines import DeadlineExceededError

    cache = RunPayloadCache()
    cache._inflight["key"] = Future()

    with pytest.raises(DeadlineExceededError) as excinfo:
        cache.run_once("key", lambda: "never", timeout=0.01)

    assert excinfo.value.retry_after == 0.01


def test_run_once_waiter_returns_quickly_for_a_fast_producer():
    cache = RunPayloadCache()
    assert cache.run_once("key", lambda: "value", timeout=1.0) == "value"
```

Append to `tests/contract/test_adversarial_hardening.py`:

```python
def test_get_repos_timeout_returns_503(clean: Engine, tmp_path, monkeypatch):
    monkeypatch.setenv("GITCRAWL_REQUEST_DEADLINE_SECONDS", "0.05")
    release = threading.Event()
    started = threading.Event()

    def blocking(run_id: int, filter_spec: dict) -> RunPayload:
        started.set()
        release.wait(10)
        return RunPayload(total_count=0, items=[])

    client = make_client(clean, tmp_path, runner=blocking)
    try:
        response = client.get("/vsearch/repos", params={"q": "language:rust"})
    finally:
        release.set()

    assert response.status_code == 503
    assert response.json() == {"error": "timeout", "retry_after": 1}


def test_post_run_timeout_returns_503(clean: Engine, tmp_path, monkeypatch):
    monkeypatch.setenv("GITCRAWL_REQUEST_DEADLINE_SECONDS", "0.05")
    release = threading.Event()

    def blocking(run_id: int, filter_spec: dict) -> RunPayload:
        release.wait(10)
        return RunPayload(total_count=0, items=[])

    client = make_client(clean, tmp_path, runner=blocking)
    try:
        response = client.post("/vsearch/run", json=FILTER)
    finally:
        release.set()

    assert response.status_code == 503
    assert response.json() == {"error": "timeout", "retry_after": 1}


def test_deadline_env_does_not_affect_a_fast_run(clean: Engine, tmp_path):
    calls: list = []
    client = make_client(
        clean, tmp_path, runner=counting_runner(RunPayload(total_count=0, items=[]), calls)
    )

    response = client.post("/vsearch/run", json=FILTER)

    assert response.status_code == 200
    assert len(calls) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_run_cache.py -k timeout -v`
Expected: FAIL — `run_once()` takes no `timeout` keyword.

Run: `pytest tests/contract/test_adversarial_hardening.py -k timeout -v`
Expected: FAIL — blocking calls hang far past 0.05 s (the test still returns after `release.set()`, with a 200/500, not 503).

- [ ] **Step 3: Implement the bounds and mapping**

In `src/serve/payload_cache.py`, add the import and change `run_once`:

```python
from lib.deadlines import DeadlineExceededError
```

```python
    def run_once(
        self, key: str, producer: Callable[[], T], *, timeout: float | None = None
    ) -> T:
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
            try:
                return pending.result(timeout=timeout)
            except DeadlineExceededError:
                raise
            except TimeoutError as exc:
                raise DeadlineExceededError(timeout or 0.0) from exc
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

In `src/serve/app.py`, add imports:

```python
from lib.deadlines import DeadlineExceededError, request_deadline_seconds
```

Add the module-level helper near `_library_error_response` (`src/serve/app.py:148-152`):

```python
def _timeout_response(exc: DeadlineExceededError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"error": "timeout", "retry_after": max(1, int(exc.retry_after))},
    )
```

Inside `create_app`, add a nested executor helper next to `executor_for` (`src/serve/app.py:308-313`):

```python
    def run_with_deadline(job: Callable[[], object], timeout: float) -> object:
        try:
            return executor_for().submit_call(job).result(timeout=timeout)
        except DeadlineExceededError:
            raise
        except TimeoutError:
            raise DeadlineExceededError(timeout) from None
```

In `list_repos` (`src/serve/app.py:374-393`), replace the cache call and add the timeout branch:

```python
        key = spec_hash(spec)
        timeout = request_deadline_seconds()
        try:
            runner = runner_for()
            payload = payload_cache.run_once(
                key,
                lambda: run_with_deadline(lambda: runner(0, spec_to_dict(spec)), timeout),
                timeout=timeout,
            )
        except DeadlineExceededError as exc:
            return _timeout_response(exc)
        except (RequestFailed, PartialResultsError):
            return JSONResponse(
                status_code=502,
                content={"error": "upstream_unavailable", "retry_after_ms": 0},
            )
        except ThrottledError as exc:
            return JSONResponse(
                status_code=502,
                content={
                    "error": "upstream_unavailable",
                    "retry_after_ms": int(exc.retry_after * 1000),
                },
            )
```

In `create_run_route`, replace the unbounded await (`src/serve/app.py:452`):

```python
        timeout = request_deadline_seconds()
        try:
            await asyncio.wait_for(
                asyncio.wrap_future(executor_for().submit(run_id, runner=capturing)),
                timeout,
            )
        except DeadlineExceededError as exc:
            return _timeout_response(exc)
        except TimeoutError:
            return _timeout_response(DeadlineExceededError(timeout))
```

(`TimeoutError` is the built-in alias of `asyncio.TimeoutError` on Python 3.12; the `DeadlineExceededError` clause must come first because it subclasses `TimeoutError`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_run_cache.py tests/contract/test_adversarial_hardening.py -k "timeout or deadline or cache or fast" -v`
Expected: PASS.

Run the regression check: `pytest tests/contract/test_vsearch_api.py tests/unit/test_run_cache.py -q`
Expected: PASS — the cache-expiry/pagination contract tests are unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/serve/payload_cache.py src/serve/app.py tests/unit/test_run_cache.py tests/contract/test_adversarial_hardening.py
git commit -m "hardening: bound executor and payload-cache waits with 503"
```

---

### Task 6: Clone lifecycle — env timeout, terminal-safe git, cancellation

**Files:**
- Modify: `src/enrich/cloner.py:167-168` (`_default_git_runner`), `:175-268` (`clone_repos`), top imports
- Modify: `src/serve/runs.py:189-200` (`CloneRegistry`), `:325-388` (`start_clone`, `_clone_worker`), add `cancel_clone`
- Modify: `src/serve/pages.py:916-966` (add `DELETE /runs/{id}/clone` after `clone_start_route`)
- Test: `tests/unit/test_cloner.py`, `tests/contract/test_clone_endpoints.py`

**Interfaces:**
- Consumes: Task 4's `clone_timeout_seconds()`.
- Produces:
  - `CloneCancelled(Exception)` in `enrich.cloner`
  - `_default_git_runner(argv, cwd, *, timeout=None, cancel_event=None)` — sets `GIT_TERMINAL_PROMPT=0`, `stdin=DEVNULL`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=os.devnull`; honours cancellation by terminating the process
  - `clone_repos(..., cancel_event: threading.Event | None = None)`; on cancellation sets `progress.status = "cancelled"` and stops
  - `CloneRegistry.set_cancel(run_id, event)`, `.clear_cancel(run_id)`, `.cancel(run_id) -> bool`
  - `start_clone(..., clone_timeout: float | None = None, cancel_event: threading.Event | None = None)`
  - `cancel_clone(engine, run_id, *, registry, runs_root="runs") -> CloneProgress`
  - `DELETE /runs/{run_id}/clone` returns the progress payload (200) or 404 `run_not_found`

**Normal-use invariance:** INV-3 category 2 (hangs) plus the new-cancel capability (spec §6.6: "New capability, no existing flow altered"). Normal clones never set the event and `clone_timeout` (default 1800 s) is ~18× a large legitimate clone; statuses/`CloneProgress` fields/response bytes for untouched runs are unchanged (no new dataclass field). Regression: `pytest tests/contract/test_clone_endpoints.py tests/contract/test_run_detail.py tests/unit/test_cloner.py -q`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_cloner.py` (imports at the top of the file gain `_git_env` and `CloneCancelled`):

```python
def test_git_env_disables_prompts_and_credential_helpers(monkeypatch):
    from enrich.cloner import _git_env

    env = _git_env()

    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull


def test_default_git_runner_closes_stdin_and_sets_the_terminal_env(monkeypatch):
    import subprocess

    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("enrich.cloner.subprocess.run", fake_run)

    _default_git_runner(["git", "clone"], ".")

    assert captured["stdin"] == subprocess.DEVNULL
    assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_default_git_runner_honours_cancellation():
    import threading
    import time as time_module

    from enrich.cloner import CloneCancelled

    cancel_event = threading.Event()
    timer = threading.Timer(0.05, cancel_event.set)
    timer.start()
    started = time_module.monotonic()
    try:
        with pytest.raises(CloneCancelled):
            _default_git_runner(
                ["python", "-c", "import time; time.sleep(5)"],
                ".",
                cancel_event=cancel_event,
            )
    finally:
        timer.cancel()
    assert time_module.monotonic() - started < 4.0


def test_clone_repos_cancellation_marks_status_and_stops(clean: Engine, tmp_path):
    import threading

    run_id, _ = seed_run(
        clean, [(1, "octo/one", 1024, 20), (2, "octo/two", 1024, 10), (3, "octo/three", 1, 5)]
    )
    progress = CloneProgress(status="running", total=0, completed=0, failed=0)
    cancel_event = threading.Event()
    calls: list[str] = []

    def runner(argv, cwd):
        calls.append(argv[-2])
        cancel_event.set()

    stats = clone_repos(
        clean,
        run_id,
        limit=3,
        mode=CloneMode.SHALLOW,
        dest_root=str(tmp_path),
        git_runner=runner,
        progress=progress,
        cancel_event=cancel_event,
    )

    assert progress.status == "cancelled"
    assert progress.completed == 1
    assert stats.failed == 0
    assert len(calls) == 1
```

Append to `tests/contract/test_clone_endpoints.py`:

```python
def test_clone_wires_env_timeout_and_cancel_event(clean: Engine, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    captured: dict = {}
    called = threading.Event()

    def fake_clone_repos(engine, run_id, **kwargs):
        captured.update(kwargs)
        called.set()
        return cloner.CloneStats(requested=0, completed=0, skipped=0, failed=0, dest_root="clones")

    monkeypatch.setattr("serve.runs.clone_repos", fake_clone_repos)
    monkeypatch.setenv("GITCRAWL_CLONE_TIMEOUT_SECONDS", "42")
    client = make_client(clean, tmp_path)

    response = client.post(f"/runs/{run_id}/clone", json={"limit": 1, "mode": "shallow"})

    assert response.status_code == 200
    assert called.wait(10) is True
    assert captured["clone_timeout"] == 42.0
    assert isinstance(captured["cancel_event"], threading.Event)


def test_clone_cancel_stops_remaining_repos(clean: Engine, tmp_path, monkeypatch):
    run_id, filter_hash = seed_run(clean, tmp_path)
    started = threading.Event()
    release = threading.Event()
    calls: list[list[str]] = []

    def runner(argv: list[str], cwd: str, **kwargs) -> None:
        calls.append(list(argv))
        started.set()
        release.wait(10)

    monkeypatch.setattr(cloner, "_default_git_runner", runner)
    client = make_client(clean, tmp_path)

    started_response = client.post(
        f"/runs/{run_id}/clone", json={"limit": 2, "mode": "shallow"}
    )
    assert started_response.json()["status"] == "running"
    assert started.wait(10) is True

    cancelled = client.delete(f"/runs/{run_id}/clone")
    assert cancelled.status_code == 200
    release.set()

    done = wait_for_status(client, run_id)
    assert done["status"] == "cancelled"
    assert len(calls) == 1
    assert done["failed"] == 0


def test_clone_cancel_on_an_idle_run_returns_the_idle_progress(clean: Engine, tmp_path):
    run_id, _ = seed_run(clean, tmp_path)
    client = make_client(clean, tmp_path)

    response = client.delete(f"/runs/{run_id}/clone")

    assert response.status_code == 200
    assert response.json() == IDLE_PROGRESS


def test_clone_cancel_unknown_run_is_404(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    response = client.delete("/runs/424242/clone")

    assert response.status_code == 404
    assert response.json()["error"] == "run_not_found"
```

Update the two existing `_default_git_runner` stubs in `tests/contract/test_clone_endpoints.py` to accept the new keyword arguments (behavior unchanged):
- `test_clone_start_returns_running_then_progress_completes`: `lambda argv, cwd: ...` becomes `lambda argv, cwd, **kwargs: ...`
- `test_progress_is_live_in_flight_and_persisted_when_done`: `def runner(argv: list[str], cwd: str) -> None:` becomes `def runner(argv: list[str], cwd: str, **kwargs) -> None:`

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_cloner.py -k "git_env or cancellation or stdin" -v`
Expected: FAIL — `_git_env`/`CloneCancelled`/`cancel_event` do not exist.

Run: `pytest tests/contract/test_clone_endpoints.py -k cancel -v`
Expected: FAIL — DELETE returns 405.

- [ ] **Step 3: Implement cloner changes**

In `src/enrich/cloner.py`, extend the imports:

```python
import os
import time
```

Add after `GitRunner` (`src/enrich/cloner.py:23`):

```python
class CloneCancelled(Exception):
    pass
```

Replace `_default_git_runner` (`src/enrich/cloner.py:167-168`):

```python
def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    return env


def _terminate(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _default_git_runner(
    argv: Sequence[str],
    cwd: str,
    *,
    timeout: float | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    if cancel_event is None:
        subprocess.run(
            list(argv),
            cwd=cwd,
            check=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=_git_env(),
        )
        return
    process = subprocess.Popen(
        list(argv), cwd=cwd, stdin=subprocess.DEVNULL, env=_git_env()
    )
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if cancel_event.is_set():
            _terminate(process)
            raise CloneCancelled()
        remaining = 0.1
        if deadline is not None:
            remaining = min(remaining, max(0.0, deadline - time.monotonic()))
        try:
            process.wait(timeout=remaining)
            break
        except subprocess.TimeoutExpired:
            if deadline is not None and time.monotonic() >= deadline:
                _terminate(process)
                raise subprocess.TimeoutExpired(list(argv), timeout) from None
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, list(argv))
```

In `clone_repos` (`src/enrich/cloner.py:175-188`), add the keyword parameter:

```python
def clone_repos(
    engine: Engine,
    run_id: int,
    *,
    limit: int,
    mode: CloneMode,
    dest_root: str = "clones",
    git_runner: Callable[[Sequence[str], str], None] | None = None,
    progress: CloneProgress | None = None,
    disk_free_mb: float | None = None,
    workers: int = 1,
    clone_timeout: float | None = None,
    errors_cap: int = 20,
    cancel_event: threading.Event | None = None,
) -> CloneStats:
```

Replace the runner branch and `clone_one` exception handling (`src/enrich/cloner.py:214-257`):

```python
    def clone_one(row: RowMapping) -> str:
        full_name = str(row["full_name"])
        destination = run_dir / full_name.replace("/", "__")
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled"
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
                        cancel_event=cancel_event,
                    )
            except CloneCancelled:
                shutil.rmtree(destination, ignore_errors=True)
                outcome = "cancelled"
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
```

Replace the loop/finish block (`src/enrich/cloner.py:259-268`):

```python
    if workers <= 1:
        for row in items:
            if clone_one(row) == "cancelled":
                with lock:
                    progress.status = "cancelled"
                break
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for outcome in pool.map(clone_one, items):
                if outcome == "cancelled":
                    with lock:
                        progress.status = "cancelled"
                    break
    progress.current = None
    if progress.status != "cancelled":
        progress.status = "done"
    progress.emit(force=True)
    return stats
```

- [ ] **Step 4: Implement runs.py and pages.py changes**

In `src/serve/runs.py`, extend `CloneRegistry` (`src/serve/runs.py:189-200`):

```python
class CloneRegistry:
    def __init__(self) -> None:
        self._entries: dict[int, CloneProgress] = {}
        self._cancels: dict[int, threading.Event] = {}
        self._lock = threading.Lock()

    def get(self, run_id: int) -> CloneProgress | None:
        with self._lock:
            return self._entries.get(run_id)

    def set(self, run_id: int, progress: CloneProgress) -> None:
        with self._lock:
            self._entries[run_id] = progress

    def set_cancel(self, run_id: int, event: threading.Event) -> None:
        with self._lock:
            self._cancels[run_id] = event

    def clear_cancel(self, run_id: int) -> None:
        with self._lock:
            self._cancels.pop(run_id, None)

    def cancel(self, run_id: int) -> bool:
        with self._lock:
            event = self._cancels.get(run_id)
        if event is None:
            return False
        event.set()
        return True
```

Add the timeout-helper import: `from lib.deadlines import clone_timeout_seconds`.

Change `start_clone` (`src/serve/runs.py:325-360`) to accept and wire the timeout/event:

```python
def start_clone(
    engine: Engine,
    run_id: int,
    *,
    limit: int,
    mode: CloneMode,
    registry: CloneRegistry,
    runs_root: str = "runs",
    dest_root: str = "clones",
    git_runner: GitRunner | None = None,
    clone_timeout: float | None = None,
    cancel_event: threading.Event | None = None,
) -> CloneProgress:
    row = _row_for_run(engine, run_id)
    existing = registry.get(run_id)
    if existing is not None and existing.status == "running":
        return existing
    bundle_dir = run_bundle_dir(runs_root, row["filter_hash"], run_id)
    progress = _PersistedProgress(
        bundle_dir / PROGRESS_NAME,
        status="running",
        total=0,
        completed=0,
        failed=0,
    )
    registry.set(run_id, progress)
    progress.total = min(limit, _count_run_items(engine, run_id)) if limit > 0 else 0
    if limit <= 0:
        progress.status = "done"
        progress.emit(force=True)
        return progress
    timeout = clone_timeout if clone_timeout is not None else clone_timeout_seconds()
    event = cancel_event if cancel_event is not None else threading.Event()
    registry.set_cancel(run_id, event)
    worker = threading.Thread(
        target=_clone_worker,
        args=(
            engine,
            run_id,
            limit,
            mode,
            dest_root,
            git_runner,
            progress,
            registry,
            timeout,
            event,
        ),
        daemon=True,
    )
    worker.start()
    return progress
```

Replace `_clone_worker` (`src/serve/runs.py:363-388`) with this single final version (the registry parameter lets the worker clear the cancel event when it finishes):

```python
def _clone_worker(
    engine: Engine,
    run_id: int,
    limit: int,
    mode: CloneMode,
    dest_root: str,
    git_runner: GitRunner | None,
    progress: CloneProgress,
    registry: CloneRegistry,
    clone_timeout: float,
    cancel_event: threading.Event,
) -> None:
    try:
        clone_repos(
            engine,
            run_id,
            limit=limit,
            mode=mode,
            dest_root=dest_root,
            git_runner=git_runner,
            progress=progress,
            clone_timeout=clone_timeout,
            cancel_event=cancel_event,
        )
    except Exception as exc:
        progress.status = "failed"
        progress.current = None
        progress.error_count += 1
        progress.errors.append(f"{type(exc).__name__}: {exc}"[:300])
    finally:
        registry.clear_cancel(run_id)
        progress.emit(force=True)
```

Add `cancel_clone` right after `read_clone_progress` (`src/serve/runs.py:293-322`):

```python
def cancel_clone(
    engine: Engine,
    run_id: int,
    *,
    registry: CloneRegistry,
    runs_root: str = "runs",
) -> CloneProgress:
    progress = read_clone_progress(engine, run_id, registry=registry, runs_root=runs_root)
    if progress.status == "running":
        registry.cancel(run_id)
    return progress
```

In `src/serve/pages.py`, extend the `serve.runs` import block (`src/serve/pages.py:31-37`) with `cancel_clone`, and register the DELETE route after `clone_start_route` ends (`src/serve/pages.py:950`) and before `clone_progress_route` (`:952`):

```python
    @app.delete("/runs/{run_id}/clone")
    def clone_cancel_route(run_id: int):
        try:
            progress = cancel_clone(
                engine_factory(), run_id, registry=progress_registry, runs_root=runs_root
            )
        except KeyError:
            return _run_not_found(run_id)
        return progress_payload(progress)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_cloner.py tests/contract/test_clone_endpoints.py -q`
Expected: PASS — including the pre-existing `test_default_git_runner_enforces_timeout`, pinned argv test, live-progress test, and the exact normal-clone payload assertions.

Run the regression check: `pytest tests/contract/test_run_detail.py tests/contract/test_console_pages.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/enrich/cloner.py src/serve/runs.py src/serve/pages.py tests/unit/test_cloner.py tests/contract/test_clone_endpoints.py
git commit -m "hardening: clone timeout, terminal-safe git, cancellation"
```

---

### Task 7: Redis fail-loud — warning, degraded health, strict mode

**Files:**
- Modify: `src/serve/runner.py:53-69` (`_redis_or_fake`), add logger + `RedisUnavailable`
- Modify: `src/serve/pages.py:93-109` (log reason) and `:549-568` (`redis_degraded` key)
- Test: `tests/unit/test_redis_fallback.py` (new), `tests/contract/test_adversarial_hardening.py`, `tests/contract/test_pages.py:263-279`

**Interfaces:**
- Consumes: `REDIS_URL`, `GITCRAWL_REDIS_STRICT`.
- Produces:
  - `RedisUnavailable(RuntimeError)` in `serve.runner`
  - `_redis_or_fake()` logs `logging.getLogger("gitcrawl.serve").warning(...)` with the exception reason when `REDIS_URL` is set but unreachable, then falls back to fakeredis; with `GITCRAWL_REDIS_STRICT=1` it raises `RedisUnavailable` instead
  - `/health` adds `"redis_degraded": true` **only** when `REDIS_URL` is set and the redis check is false; otherwise the body is byte-identical to today

**Normal-use invariance:** INV-3 category 4 (outage visibility). With no `REDIS_URL`, behavior is unchanged (fake fallback, no warning); with a reachable Redis, no warning and no extra health key; strict mode is opt-in. Regression: `pytest tests/contract/test_pages.py tests/integration/test_runner.py -q`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_redis_fallback.py`:

```python
from __future__ import annotations

import fakeredis
import pytest

from serve import runner


def _unreachable(monkeypatch):
    def boom(*args, **kwargs):
        raise ConnectionError("connection refused")

    monkeypatch.setattr("redis.Redis.from_url", boom)


def test_unreachable_redis_logs_the_reason_and_falls_back(monkeypatch, caplog):
    _unreachable(monkeypatch)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6390/0")
    monkeypatch.delenv("GITCRAWL_REDIS_STRICT", raising=False)

    with caplog.at_level("WARNING", logger="gitcrawl.serve"):
        client = runner._redis_or_fake()

    assert isinstance(client, fakeredis.FakeRedis)
    assert "connection refused" in caplog.text


def test_strict_mode_refuses_the_fake_fallback(monkeypatch):
    _unreachable(monkeypatch)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6390/0")
    monkeypatch.setenv("GITCRAWL_REDIS_STRICT", "1")

    with pytest.raises(runner.RedisUnavailable):
        runner._redis_or_fake()


def test_no_redis_url_uses_the_fake_without_a_warning(monkeypatch, caplog):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("GITCRAWL_REDIS_STRICT", raising=False)

    with caplog.at_level("WARNING", logger="gitcrawl.serve"):
        client = runner._redis_or_fake()

    assert isinstance(client, fakeredis.FakeRedis)
    assert caplog.text == ""
```

Append to `tests/contract/test_adversarial_hardening.py`:

```python
def test_health_reports_degraded_redis_when_the_url_is_unreachable(
    clean: Engine, tmp_path, monkeypatch
):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6390/0")
    client = make_client(clean, tmp_path, redis_ping=lambda: False)

    body = client.get("/health").json()

    assert body["redis"] is False
    assert body["redis_degraded"] is True


def test_health_omits_degraded_redis_when_redis_is_healthy(clean: Engine, tmp_path, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6390/0")
    client = make_client(clean, tmp_path, redis_ping=lambda: True)

    body = client.get("/health").json()

    assert body == {"database": True, "redis": True, "github_token_present": False}


def test_health_omits_degraded_redis_without_a_configured_url(clean: Engine, tmp_path, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    client = make_client(clean, tmp_path, redis_ping=lambda: False)

    body = client.get("/health").json()

    assert body == {"database": True, "redis": False, "github_token_present": False}
```

In `tests/contract/test_pages.py`, make `test_health_reports_booleans_for_unhealthy_clients` (`:263-279`) deterministic against a developer environment that has `REDIS_URL` set by adding at the top of the test body:

```python
    monkeypatch.delenv("REDIS_URL", raising=False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_redis_fallback.py -v`
Expected: FAIL — `runner.RedisUnavailable` does not exist and no warning is logged.

Run: `pytest tests/contract/test_adversarial_hardening.py -k degraded -v`
Expected: FAIL — `redis_degraded` key absent.

- [ ] **Step 3: Implement fail-loud**

In `src/serve/runner.py`, add `import logging` and near the top-level constants:

```python
logger = logging.getLogger("gitcrawl.serve")


class RedisUnavailable(RuntimeError):
    pass
```

Replace `_redis_or_fake` (`src/serve/runner.py:53-69`):

```python
def _redis_or_fake():
    url = os.environ.get("REDIS_URL")
    if url:
        try:
            import redis as redis_module

            client = redis_module.Redis.from_url(url)
            client.ping()
            return client
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            if os.environ.get("GITCRAWL_REDIS_STRICT") == "1":
                raise RedisUnavailable(reason) from exc
            logger.warning(
                "REDIS_URL is set but unreachable (%s); falling back to fakeredis", reason
            )
    try:
        import fakeredis

        return fakeredis.FakeRedis()
    except ImportError:
        return None
```

In `src/serve/pages.py`, add a logger import/instance and log in `_default_redis_ping` (`src/serve/pages.py:93-109`):

```python
import logging
...
logger = logging.getLogger("gitcrawl.serve")
```

```python
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
    except Exception as exc:
        logger.warning("redis health probe failed (%s: %s)", type(exc).__name__, exc)
        return False
```

In `health_snapshot` (`src/serve/pages.py:549-568`), add the conditional key before caching:

```python
        value = {
            "database": _bounded(database_ok),
            "redis": redis_ok(),
            "github_token_present": token_ok(),
        }
        if os.environ.get("REDIS_URL") and not value["redis"]:
            value["redis_degraded"] = True
        with health_lock:
            health_cache["at"] = time.monotonic()
            health_cache["value"] = dict(value)
        return value
```

(If Task 8 has already replaced `_bounded`, the same edit applies to the `_Probe.run` calls; the key logic is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_redis_fallback.py tests/contract/test_adversarial_hardening.py -k "redis or degraded" tests/contract/test_pages.py -q`
Expected: PASS — the pre-existing healthy/unhealthy health-shape tests still compare exact 3-key dicts.

- [ ] **Step 5: Commit**

```bash
git add src/serve/runner.py src/serve/pages.py tests/unit/test_redis_fallback.py tests/contract/test_adversarial_hardening.py tests/contract/test_pages.py
git commit -m "hardening: fail loud on degraded redis"
```

---

### Task 8: Single-flight health probe

**Files:**
- Modify: `src/serve/pages.py:78-90` (replace `_bounded` with `_Probe`), `:529-568` (use the probes; statement timeout in `database_ok`)
- Test: `tests/unit/test_health_probe.py` (new), `tests/contract/test_adversarial_hardening.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_Probe(*, timeout: float = HEALTH_TIMEOUT_SECONDS)` with `run(check: Callable[[], object]) -> bool`. At most one check thread exists at a time per probe; overlapping callers join the in-flight check and wait at most `timeout`; a hung check returns `False` and never spawns another thread until it completes.

**Normal-use invariance:** INV-3 category 2. Healthy `/health` and dashboard output is byte-identical (same booleans, same 5 s memoization in `health_snapshot`). Regression: `pytest tests/contract/test_pages.py -q`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_health_probe.py`:

```python
from __future__ import annotations

import threading

from serve.pages import _Probe


def test_probe_returns_the_check_result():
    probe = _Probe(timeout=0.5)

    assert probe.run(lambda: True) is True


def test_probe_returns_false_when_the_check_raises():
    probe = _Probe(timeout=0.5)

    def boom():
        raise RuntimeError("down")

    assert probe.run(boom) is False


def test_hung_check_does_not_accumulate_threads():
    hang = threading.Event()
    probe = _Probe(timeout=0.05)
    before = threading.active_count()

    assert probe.run(lambda: hang.wait(30)) is False
    assert probe.run(lambda: hang.wait(30)) is False

    assert threading.active_count() - before <= 1
    hang.set()
```

Append to `tests/contract/test_adversarial_hardening.py`:

```python
def test_health_returns_false_quickly_for_a_hung_database(clean: Engine, tmp_path):
    release = threading.Event()

    class HangingEngine:
        def connect(self):
            release.wait(30)
            raise RuntimeError("unreachable")

    client = make_client(HangingEngine(), tmp_path)
    started = time.monotonic()
    try:
        response = client.get("/health")
    finally:
        release.set()

    assert response.status_code == 200
    assert response.json()["database"] is False
    assert time.monotonic() - started < 2.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_health_probe.py -v`
Expected: FAIL — `serve.pages._Probe` does not exist (`_bounded` is still the implementation).

- [ ] **Step 3: Implement the probe**

In `src/serve/pages.py`, replace `_bounded` (`src/serve/pages.py:78-90`) with:

```python
class _Probe:
    def __init__(self, *, timeout: float = HEALTH_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout
        self._lock = threading.Lock()
        self._event: threading.Event | None = None
        self._value = False

    def run(self, check: Callable[[], object]) -> bool:
        with self._lock:
            event = self._event
            if event is None:
                event = threading.Event()
                self._event = event
                threading.Thread(
                    target=self._execute, args=(check, event), daemon=True
                ).start()
        if not event.wait(self._timeout):
            return False
        with self._lock:
            if self._event is event:
                self._event = None
        return self._value

    def _execute(self, check: Callable[[], object], event: threading.Event) -> None:
        try:
            value = bool(check())
        except Exception:
            value = False
        with self._lock:
            self._value = value
        event.set()
```

In `register_pages`, add statement-timeout execution to `database_ok` and swap both `_bounded` call sites for probes:

```python
    db_probe = _Probe()
    redis_probe = _Probe()

    def database_ok() -> bool:
        try:
            engine = engine_factory()
            with engine.connect() as connection:
                connection.execute(
                    text(f"SET LOCAL statement_timeout = {int(HEALTH_TIMEOUT_SECONDS * 1000)}")
                )
                connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    def redis_ok() -> bool:
        return redis_probe.run(redis_ping or _default_redis_ping)
```

and in `health_snapshot`:

```python
        value = {
            "database": db_probe.run(database_ok),
            "redis": redis_ok(),
            "github_token_present": token_ok(),
        }
```

`SET LOCAL statement_timeout = 250` is a psycopg/PostgreSQL statement; non-PostgreSQL test engines would return `False` here, which is a health-only path (the project's tests use PostgreSQL).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_health_probe.py tests/contract/test_adversarial_hardening.py -k "probe or hung" -v`
Expected: PASS.

Run the regression check: `pytest tests/contract/test_pages.py -q`
Expected: PASS — healthy/unhealthy bodies and memoization unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/serve/pages.py tests/unit/test_health_probe.py tests/contract/test_adversarial_hardening.py
git commit -m "hardening: single-flight health probe"
```

---

### Task 9: Malformed-input and state-freshness hardening

**Files:**
- Modify: `src/serve/runs.py:293-322` (`read_clone_progress`), `src/serve/forms.py:29-30` (`_int_or_raw`), `src/scheduler/state_machine.py:135-141` (`set_state`)
- Test: `tests/contract/test_clone_endpoints.py`, `tests/unit/test_forms.py` (new), `tests/unit/test_state_machine.py`

**Interfaces:**
- Produces:
  - `read_clone_progress` coerces non-`int` numerics to 0, non-`str` status to `"done"`, non-`str` current to `None`, non-list errors to `[]`, and stringifies list entries; corrupt JSON still yields the idle progress.
  - `_int_or_raw(value)` only converts ASCII digits (`"123"` → `123`; `"١٢٣"`/`"²"` stay strings).
  - `ShardStore.set_state` writes `updated_at=func.now()`.

**Normal-use invariance:** INV-3 categories 2/3 (malformed input paths only). Well-formed progress files, ASCII numeric form fields, and shard transitions behave identically; `updated_at` is not part of any API response (spec §6.9). Regression: `pytest tests/contract/test_clone_endpoints.py tests/unit/test_state_machine.py tests/contract/test_filter_form.py -q`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_forms.py` (new file):

```python
from __future__ import annotations

from serve.forms import _int_or_raw, build_spec_from_form


def test_int_or_raw_converts_ascii_digits():
    assert _int_or_raw("123") == 123


def test_int_or_raw_rejects_non_ascii_digits():
    assert _int_or_raw("١٢٣") == "١٢٣"
    assert _int_or_raw("１２３") == "１２３"
    assert _int_or_raw("²") == "²"


def test_form_virtuals_keep_non_ascii_digits_as_text():
    spec = build_spec_from_form({"min_stars": "١٢٣"})

    assert spec["virtual"]["min_stars"] == "١٢٣"
```

Append to `tests/unit/test_state_machine.py`:

```python
def test_set_state_refreshes_updated_at(store, alembic_engine):
    shard_id = store.create(spec())
    with alembic_engine.begin() as connection:
        connection.execute(
            text("UPDATE shards SET updated_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": shard_id},
        )
        before = connection.scalar(
            text("SELECT updated_at FROM shards WHERE id = :id"), {"id": shard_id}
        )

    store.set_state(shard_id, ShardState.ACTIVE)

    with alembic_engine.connect() as connection:
        after = connection.scalar(
            text("SELECT updated_at FROM shards WHERE id = :id"), {"id": shard_id}
        )
    assert after > before
```

Append to `tests/contract/test_clone_endpoints.py`:

```python
def test_progress_tolerates_corrupt_json_and_wrong_types(clean: Engine, tmp_path):
    run_id, filter_hash = seed_run(clean, tmp_path)
    progress_path = tmp_path / "runs" / filter_hash / str(run_id) / "clone-progress.json"
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    client = make_client(clean, tmp_path)

    progress_path.write_text("{not json", encoding="utf-8")
    assert client.get(f"/partials/runs/{run_id}/clone-progress").json() == IDLE_PROGRESS

    progress_path.write_text(
        json.dumps(
            {
                "status": 7,
                "total": "many",
                "completed": [],
                "failed": None,
                "current": 5,
                "errors": {"a": 1},
                "error_count": "nope",
            }
        ),
        encoding="utf-8",
    )
    response = client.get(f"/partials/runs/{run_id}/clone-progress")

    assert response.status_code == 200
    assert response.json() == IDLE_PROGRESS

    progress_path.write_text(
        json.dumps(
            {
                "status": "running",
                "total": 2,
                "completed": 1,
                "failed": 0,
                "errors": [1, None],
                "error_count": 2,
            }
        ),
        encoding="utf-8",
    )
    body = client.get(f"/partials/runs/{run_id}/clone-progress").json()

    assert body["errors"] == ["1", "None"]
    assert body["error_count"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_forms.py tests/unit/test_state_machine.py -k "non_ascii or refreshed" -v`
Expected: FAIL — `_int_or_raw("²")` raises `ValueError`, and `updated_at` is stale.

Run: `pytest tests/contract/test_clone_endpoints.py -k wrong_types -v`
Expected: FAIL — `int("many")`/`int("nope")` raise inside the route (500).

- [ ] **Step 3: Implement the coercions**

In `src/serve/forms.py`, replace `_int_or_raw` (`src/serve/forms.py:29-30`):

```python
def _int_or_raw(value: str) -> int | str:
    return int(value) if value.isascii() and value.isdigit() else value
```

In `src/scheduler/state_machine.py`, inside `set_state` add the timestamp before executing (`src/scheduler/state_machine.py:135-141`):

```python
        values: dict[str, object] = {"state": new_state.value, "updated_at": func.now()}
        if fetched is not None:
            values["fetched"] = fetched
        if incomplete is not None:
            values["incomplete"] = incomplete
        if total_count is not None:
            values["total_count"] = total_count
```

In `src/serve/runs.py`, add helpers above `read_clone_progress` and rewrite the body (`src/serve/runs.py:293-322`):

```python
def _progress_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _progress_errors(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def read_clone_progress(
    engine: Engine,
    run_id: int,
    *,
    registry: CloneRegistry,
    runs_root: str = "runs",
) -> CloneProgress:
    tracked = registry.get(run_id)
    if tracked is not None:
        return tracked
    row = _row_for_run(engine, run_id)
    path = run_bundle_dir(runs_root, row["filter_hash"], run_id) / PROGRESS_NAME
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            data = None
        if isinstance(data, dict):
            errors = _progress_errors(data.get("errors"))
            status = data.get("status")
            current = data.get("current")
            return CloneProgress(
                status=status if isinstance(status, str) else "done",
                total=_progress_int(data.get("total")),
                completed=_progress_int(data.get("completed")),
                failed=_progress_int(data.get("failed")),
                current=current if isinstance(current, str) else None,
                errors=errors,
                error_count=_progress_int(data.get("error_count"), default=len(errors)),
            )
    return CloneProgress(status="done", total=0, completed=0, failed=0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_forms.py tests/unit/test_state_machine.py tests/contract/test_clone_endpoints.py -q`
Expected: PASS — including the existing "caps rendered errors" persisted-progress test.

Run the regression check: `pytest tests/contract/test_filter_form.py -q`
Expected: PASS — ASCII form values behave identically.

- [ ] **Step 5: Commit**

```bash
git add src/serve/runs.py src/serve/forms.py src/scheduler/state_machine.py tests/contract/test_clone_endpoints.py tests/unit/test_forms.py tests/unit/test_state_machine.py
git commit -m "hardening: tolerate malformed progress, digits, and shard timestamps"
```

---

## Plan self-review

- **Spec coverage:** every Phase 2 item maps to a task (table below); INV-1..INV-5 are restated in Global Constraints and each task's "Normal-use invariance" line names its INV-3 category and regression suites.
- **Placeholder scan:** no TBD/TODO/"handle edge cases"; every test and implementation body is concrete and self-contained. No "similar to Task N" references.
- **Type consistency:** `DeadlineExceededError.retry_after` is a float everywhere; `Deadline` is imported from `lib.deadlines` by `limiter`, `lib`, and `serve`; `CloneCancelled` is raised only by `enrich.cloner` and caught only there; `CloneRegistry.cancel/set_cancel/clear_cancel` names match across `runs.py`, `pages.py`, and tests; `BodyLimitMiddleware` signature `(app, *, default_limit, path_limits)` matches both call sites.
- **Task independence:** Tasks 1-3 each add one middleware and are individually revertible; Tasks 4 and 5 can land separately (Task 5 only consumes Task 4's names); Tasks 6-9 touch disjoint code paths except `pages.py`, where each edit names its exact anchor.
- **Ordering caveat:** Task 7 edited `health_snapshot` against the current `_bounded` body; Task 8 then replaces `_bounded`. If Task 8 executes first, apply Task 7's `redis_degraded` insert to the `_Probe` version (the condition is identical).

## Spec Coverage

| Spec item | Task |
|---|---|
| §6.1 Origin-aware CSRF (Origin/Referer, absent passes, per-route CSRF stays) | Task 1 |
| §6.2 Host allowlist (`localhost`, `127.0.0.1`, `[::1]`, `GITCRAWL_ALLOWED_HOSTS`; bind default unchanged) | Task 2 |
| §6.3 Deadlines: single module + env overrides (`GITCRAWL_REQUEST_DEADLINE_SECONDS`, `GITCRAWL_CLONE_TIMEOUT_SECONDS`) | Task 4 |
| §6.3 Deadlines: RunExecutor waits, payload-cache waiters, 503 `{"error":"timeout","retry_after":...}` | Task 5 |
| §6.3 Deadlines: bounded limiter sleep raising typed `TimeoutError` | Task 4 |
| §6.4 Body caps: 1 MiB JSON, 10 MiB `/find`, 413, chunked streams | Task 3 |
| §6.5 Redis fail-loud: warning + `/health` degraded + `GITCRAWL_REDIS_STRICT=1` | Task 7 |
| §6.6 Clone lifecycle: `clone_timeout`, terminal env, cancel event, `DELETE /runs/{id}/clone` | Task 6 |
| §6.7 Malformed input: `read_clone_progress`, `_int_or_raw` | Task 9 |
| §6.8 Health probe: cached single-flight with statement timeout | Task 8 |
| §6.9 State freshness: `set_state` updates `updated_at` | Task 9 |
| §2 INV-1..INV-5 | Global Constraints + per-task invariance statements |
| §11 exception register | Not touched: E1 remains gated/approval-dependent; E2 keep-as-is; E3 is Phase 3 |
| Out of scope (auth, status-code consistency, worker parallelism, `/vsearch` caps) | Global Constraints (excluded explicitly) |

## Risks / Known Unknowns

| Risk | Mitigation |
|---|---|
| TestClient sends `Host: testserver`, which would fail the new allowlist and break every contract test | Root `conftest.py` autouse fixture extends `GITCRAWL_ALLOWED_HOSTS=testserver`; Task 2 test overrides it to prove the env extension and the reject path |
| Phase 1 (`CloneRegistry.claim`, uvloop guards, import boundary) is not in this checkout; this plan must run on current `main` | Task 6 adds `set_cancel`/`clear_cancel`/`cancel` alongside the existing `get`/`set`; no Phase 1 API is assumed. Re-verify `runs.py` line anchors if Phase 1 lands first |
| Body cap buffers up to 10 MiB per `/find` request | Bounded by design; normal filter uploads are KB; the alternative (streaming tee) adds complexity without changing the cap |
| `asyncio.wait_for` cancels the asyncio wrapper on timeout while the executor job keeps running | Accepted: INV-3 category 2; the run row still completes/fails normally and the client receives a 503 hint |
| `SET LOCAL statement_timeout` is PostgreSQL-specific | The project targets PostgreSQL (psycopg3) and all DB tests use it; the health check returns `false` on any other backend rather than 500 |
| `GIT_CONFIG_GLOBAL=os.devnull` on Windows (`nul`) | Git for Windows treats `nul` as the null device; Task 6 unit test pins the env, and `stdin=DEVNULL` + `GIT_TERMINAL_PROMPT=0` cover the interactive-prompt hang even if a global config is read |
| Starlette returns 400 (not 403) for rejected hosts | Explicitly asserted; spec only requires rejection |
| `DeadlineExceededError` subclasses `TimeoutError`, so bare `except TimeoutError` can misclassify | Catching order is specified in Tasks 4/5 (`DeadlineExceededError` first) and tested |
| Existing tests that monkeypatch `_default_git_runner` with 2-arg callables | Task 6 updates the two stubs to `**kwargs`; assertions on argv/cwd/results are unchanged |
| Developer environments with `REDIS_URL` set could change the exact `/health` dict | Task 7 adds `monkeypatch.delenv("REDIS_URL")` to the pre-existing unhealthy-clients test |
| Defaults 3600 s / 1800 s could be too tight for pathological legitimate runs | Both are env-overridable; justification table in Task 4 shows ≥ 4× the budget-bound worst case; golden/contract suites prove normal-path output is unchanged |
