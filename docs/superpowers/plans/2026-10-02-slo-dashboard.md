# SLO / Ops Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local `/metrics` page (plus JSON API) showing the FR-010 signals explorers and operators need: search budget remaining, incomplete-results ratio, 422 / 403+429 rates, p95 latency, shard coverage, geo unmatched rate, plus limiter pauses, queue depth, and run counts by status.

**Architecture:** `slo_snapshot()` already computes the seven FR-010 metrics in `src/serve/audit.py`; this plan wraps it in a `metrics_payload` (adding live Redis state), renders it as a page + htmx partial + JSON, and wires `pel_size` retained by the unwired-debt triage plan.

**Tech Stack:** Python 3.12, FastAPI + Jinja2 + htmx, SQLAlchemy 2, Redis (fakeredis in tests), pytest.

**Spec:** `design/spec.md` FR-010 (audit logs + SLO dashboards); `docs/superpowers/plans/2026-10-02-unwired-debt-triage.md` (row 5, `pel_size` retained for this plan).

**Depends on:** triage plan for the `pel_size` keep decision; works standalone without it (queue card shows `null`).

## Global Constraints

- Python `>=3.12`; ruff + black clean; coverage floor 93.
- Read-only: the dashboard never mutates DB or Redis.
- Missing Redis ⇒ payload still renders with `null` live fields (degraded, never an error page).
- Thresholds live in one `_thresholds` table, tested; colors are copy, not logic.
- Contract helpers copied from `tests/contract/test_pages.py`.
- Run tests with: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest <path> -q`.

## File Structure

**Create**
- `src/serve/metrics.py` — `metrics_payload(engine, *, redis_client=None, window=1000) -> dict`.
- `src/serve/templates/metrics.html`, `src/serve/templates/partials/metrics_cards.html`.
- `tests/unit/test_metrics.py`, `tests/contract/test_metrics_pages.py`.

**Modify**
- `src/serve/app.py` — `GET /metrics`, `GET /partials/metrics`, `GET /api/metrics`.
- `tests/golden/snapshots/openapi.sha256` — re-pin.
- `docs/development-log.md` — record the new routes/monitoring surface.

---

### Task 1: Metrics payload module

**Files:**
- Create: `src/serve/metrics.py`
- Test: `tests/unit/test_metrics.py`

**Interfaces:**
- Consumes: `serve.audit.slo_snapshot`, `store.models.{AuditLog, Runs, Shard, Owner}`, optional Redis client with `scan_iter`/`hget`.
- Produces (used by Task 2):
  - `metrics_payload(engine: Engine, *, redis_client=None, window: int = 1000) -> dict` with keys:
    `slo: {search_remaining, incomplete_results_ratio, rate_422, rate_403_429, p95_latency_ms, shard_coverage, geo_unmatched_rate}`,
    `runs: {queued, running, done, failed, partial}`,
    `limiter: {paused: [{resource, token_fp, seconds}], degraded: bool}`,
    `queue: {pel: int | None, degraded: bool}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_metrics.py`:

```python
from __future__ import annotations

import fakeredis
from sqlalchemy import text

from serve.metrics import metrics_payload


def seed_audit(engine, rows: list[tuple]) -> None:
    with engine.begin() as connection:
        for status, remaining, incomplete, latency, resource in rows:
            connection.execute(
                text(
                    "INSERT INTO audit_log (query_hash, params, status, rl_remaining,"
                    " incomplete_results, latency_ms, token_fp, rl_resource)"
                    " VALUES ('h', '{}'::jsonb, :status, :remaining, :incomplete, :latency, 'fp', :resource)"
                ),
                {
                    "status": status,
                    "remaining": remaining,
                    "incomplete": incomplete,
                    "latency": latency,
                    "resource": resource,
                },
            )


def test_metrics_payload_includes_all_slo_keys(clean_db):
    engine = clean_db()
    seed_audit(
        engine,
        [
            (200, 120, False, 100, "search"),
            (200, 90, True, 900, "search"),
            (422, 80, None, 1200, "search"),
            (429, 70, None, 50, "core"),
        ],
    )
    payload = metrics_payload(engine, redis_client=None)
    assert set(payload) == {"slo", "runs", "limiter", "queue"}
    slo = payload["slo"]
    assert set(slo) == {
        "search_remaining",
        "incomplete_results_ratio",
        "rate_422",
        "rate_403_429",
        "p95_latency_ms",
        "shard_coverage",
        "geo_unmatched_rate",
    }
    assert slo["search_remaining"] == 90  # most recent search-row remaining
    assert slo["incomplete_results_ratio"] == 0.5
    assert slo["rate_422"] == 0.25
    assert payload["runs"]["done"] == 0
    assert payload["limiter"]["degraded"] is True  # no redis
    assert payload["queue"]["pel"] is None


def test_severity_thresholds():
    from serve.metrics import severity

    assert severity("p95_latency_ms", None) == "na"
    assert severity("p95_latency_ms", 100) == "ok"
    assert severity("p95_latency_ms", 1000) == "warn"
    assert severity("p95_latency_ms", 5000) == "fail"
    assert severity("shard_coverage", 0.99) == "ok"
    assert severity("shard_coverage", 0.9) == "warn"
    assert severity("shard_coverage", 0.5) == "fail"


def test_metrics_payload_reads_paused_buckets_and_pel(clean_db, monkeypatch):
    engine = clean_db()
    redis = fakeredis.FakeRedis()
    redis.hset("gitcrawl:rl:search:abcdef123456", mapping={"paused_until": 10_000})
    monkeypatch.setattr("serve.metrics.time", lambda: 9_000.0)
    payload = metrics_payload(engine, redis_client=redis)
    paused = payload["limiter"]["paused"]
    assert paused == [{"resource": "search", "token_fp": "abcdef123456", "seconds": 1000.0}]
    assert payload["queue"]["pel"] is not None  # wired by triage Task 2; value may be 0
```

- [ ] **Step 2: Run to verify failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_metrics.py -q`
Expected: FAIL (`ModuleNotFoundError: serve.metrics`).

- [ ] **Step 3: Implement `src/serve/metrics.py`**

```python
from __future__ import annotations

import time
from dataclasses import asdict

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from serve.audit import slo_snapshot
from store.models import Runs

_PREFIX = "gitcrawl:rl:"

THRESHOLDS = {
    "search_remaining": ("lt", 100, 10),
    "incomplete_results_ratio": ("gt", 0.05, 0.2),
    "rate_422": ("gt", 0.0, 0.02),
    "rate_403_429": ("gt", 0.0, 0.05),
    "p95_latency_ms": ("gt", 800, 2000),
    "shard_coverage": ("lt", 0.95, 0.8),
    "geo_unmatched_rate": ("gt", 0.4, 0.6),
}


def severity(name: str, value) -> str:
    if value is None:
        return "na"
    direction, warn, fail = THRESHOLDS[name]
    bad = (value < fail) if direction == "lt" else (value > fail)
    if bad:
        return "fail"
    near = (value < warn) if direction == "lt" else (value > warn)
    return "warn" if near else "ok"


def _paused(redis_client) -> list[dict]:
    if redis_client is None:
        return []
    now = time.time()
    paused: list[dict] = []
    for key in list(redis_client.scan_iter(match=f"{_PREFIX}*", count=100))[:100]:
        name = key.decode() if isinstance(key, bytes) else str(key)
        _, _, resource, token_fp = name.split(":", 3)
        raw = redis_client.hget(key, "paused_until")
        if raw is None:
            continue
        until = float(raw.decode() if isinstance(raw, bytes) else raw)
        if until > now:
            paused.append({"resource": resource, "token_fp": token_fp, "seconds": round(until - now, 1)})
    return sorted(paused, key=lambda item: -item["seconds"])


def _queue_pel(redis_client) -> int | None:
    if redis_client is None:
        return None
    try:
        from scheduler.state_machine import ShardQueue

        return int(ShardQueue(redis_client).pel_size())
    except Exception:
        return None


def _runs_by_status(engine: Engine) -> dict[str, int]:
    counts = {"queued": 0, "running": 0, "done": 0, "failed": 0, "partial": 0}
    with engine.connect() as connection:
        rows = connection.execute(select(Runs.status, func.count()).group_by(Runs.status)).all()
    for status, count in rows:
        if status in counts:
            counts[status] = int(count)
    return counts


def metrics_payload(engine: Engine, *, redis_client=None, window: int = 1000) -> dict:
    slo = asdict(slo_snapshot(engine, window=window))
    return {
        "slo": slo,
        "runs": _runs_by_status(engine),
        "limiter": {"paused": _paused(redis_client), "degraded": redis_client is None},
        "queue": {"pel": _queue_pel(redis_client), "degraded": redis_client is None},
    }
```

- [ ] **Step 4: Run tests**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_metrics.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve/metrics.py tests/unit/test_metrics.py
git commit -m "feat: ops metrics payload"
```

---

### Task 2: `/metrics` page, partial, and JSON API

**Files:**
- Modify: `src/serve/app.py`
- Create: `src/serve/templates/metrics.html`, `src/serve/templates/partials/metrics_cards.html`
- Test: `tests/contract/test_metrics_pages.py`

**Interfaces:**
- Consumes: `serve.metrics.metrics_payload`, the app's `redis_ping`-style Redis accessor used by health (reuse the same client builder, or `serve.runner._redis_or_fake` for tests/local).
- Produces:
  - `GET /api/metrics` → JSON payload (200).
  - `GET /metrics` → page with cards; refresh button.
  - `GET /partials/metrics` → cards fragment, htmx `every 10s`.

- [ ] **Step 1: Write the failing contract tests**

Create `tests/contract/test_metrics_pages.py`. Copy `schema`, `clean`, `make_client`, `healthy_client` from `tests/contract/test_pages.py`, then:

```python
def test_api_metrics_json_shape(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get("/api/metrics")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"slo", "runs", "limiter", "queue"}


def test_metrics_page_renders_cards(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get("/metrics")
    assert response.status_code == 200
    for label in (
        "Search remaining",
        "Incomplete ratio",
        "422 rate",
        "403/429 rate",
        "p95 latency",
        "Shard coverage",
        "Geo unmatched",
        "Queue PEL",
    ):
        assert label in response.text
    assert 'hx-get="/partials/metrics"' in response.text


def test_metrics_partial_degrades_without_redis(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get("/partials/metrics")
    assert response.status_code == 200
    assert "degraded" in response.text.lower() or "n/a" in response.text.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/contract/test_metrics_pages.py -q`
Expected: FAIL (404s).

- [ ] **Step 3: Implement routes + templates**

In `create_app`, add a Redis accessor next to the health checks:

```python
    def metrics_redis():
        url = os.environ.get("REDIS_URL")
        if not url:
            return None
        try:
            import redis as redis_module

            client = redis_module.Redis.from_url(url, socket_connect_timeout=0.5)
            client.ping()
            return client
        except Exception:
            return None

    @application.get("/api/metrics")
    def api_metrics():
        from serve.metrics import metrics_payload

        return metrics_payload(engine_for(), redis_client=metrics_redis())

    def _metrics_response(request: Request, template: str):
        from serve.metrics import metrics_payload

        payload = metrics_payload(engine_for(), redis_client=metrics_redis())
        return pages._templates.TemplateResponse(
            request, template, {"payload": payload}
        )

    @application.get("/metrics", response_class=HTMLResponse)
    def metrics_page(request: Request):
        return _metrics_response(request, "metrics.html")

    @application.get("/partials/metrics", response_class=HTMLResponse)
    def metrics_partial(request: Request):
        return _metrics_response(request, "partials/metrics_cards.html")
```

Add `from serve import pages` to `app.py` imports for `pages._templates` (if the QA/resume plans landed first, the import already exists).

`partials/metrics_cards.html` renders the cards; each value formats `None` as `n/a` and applies the threshold class via `severity` (defined in Task 1).

Template loop:

```html
<section id="metrics-cards" hx-get="/partials/metrics" hx-trigger="every 10s" hx-swap="outerHTML">
  <div class="cards">
    {% for name, value in payload.slo.items() %}
    <article class="card {{ severity(name, value) }}">
      <h3>{{ name.replace('_', ' ').title() }}</h3>
      <p>{{ 'n/a' if value is none else value }}</p>
    </article>
    {% endfor %}
    <article class="card {{ 'na' if payload.queue.pel is none else 'ok' }}">
      <h3>Queue PEL</h3>
      <p>{{ 'n/a' if payload.queue.pel is none else payload.queue.pel }}</p>
    </article>
  </div>
  <p class="limiter">Paused buckets: {{ payload.limiter.paused|length }}
    {% if payload.limiter.degraded %}(degraded: no Redis){% endif %}</p>
</section>
```

Expose `severity` to the template environment in `serve/pages.py` (`_templates.env.globals["severity"] = severity`, importing from `serve.metrics`) and add "Search remaining" style titles via a `LABELS` map in `metrics.py` if the `.title()` output doesn't match the test strings (e.g. `p95_latency_ms` → "P95 Latency Ms"; use an explicit `LABELS` dict with the exact strings from the test).

- [ ] **Step 4: Run tests + re-pin OpenAPI**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/contract/test_metrics_pages.py tests/contract/test_pages.py -q`
Expected: PASS. Re-pin `tests/golden/snapshots/openapi.sha256` for the three new routes; note it in `docs/development-log.md`.

- [ ] **Step 5: Commit**

```bash
git add src/serve/app.py src/serve/pages.py src/serve/metrics.py src/serve/templates/metrics.html src/serve/templates/partials/metrics_cards.html tests/contract/test_metrics_pages.py tests/golden/snapshots/openapi.sha256 docs/development-log.md
git commit -m "feat: SLO dashboard page, partial, and JSON API"
```

---

### Task 3: Wire `pel_size` (triage row 5)

**Files:**
- Modify: `src/serve/metrics.py` (already calls `ShardQueue.pel_size()`; this task verifies the integration end-to-end)
- Test: `tests/unit/test_metrics.py`

**Interfaces:**
- Consumes: `scheduler.state_machine.ShardQueue.pel_size` (retained by the triage plan) and `reclaim_stale` (wired in triage Task 2).
- Produces: a real PEL count in the dashboard when Redis is reachable.

- [ ] **Step 1: Write the failing integration test**

Add to `tests/unit/test_metrics.py`:

```python
def test_queue_pel_reflects_delivered_unacked(clean_db):
    import fakeredis
    from scheduler.state_machine import ShardQueue

    engine = clean_db()
    redis = fakeredis.FakeRedis()
    queue = ShardQueue(redis)
    queue.enqueue(1)
    queue.claim("gitcrawl-dead", count=1)  # delivered, never acked
    payload = metrics_payload(engine, redis_client=redis)
    assert payload["queue"]["pel"] == 1
```

- [ ] **Step 2: Run it**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_metrics.py::test_queue_pel_reflects_delivered_unacked -q`
Expected: PASS if triage Task 2 landed and `pel_size` is wired; FAIL (pel `None`) if `ShardQueue.pel_size` was deleted instead — then stop and flip triage row 5 to delete, and drop the queue card.

- [ ] **Step 3: Commit (test only; module already calls it)**

```bash
git add tests/unit/test_metrics.py
git commit -m "test: verify queue PEL wiring in the metrics payload"
```

---

## Self-Review Checklist

- [ ] All seven FR-010 metrics render with thresholds and `n/a` degradation (Tasks 1–2).
- [ ] Live Redis state (paused buckets, PEL) present when reachable, degraded otherwise (Tasks 1–3).
- [ ] Read-only: no writes anywhere in `metrics.py` or routes.
- [ ] OpenAPI golden re-pinned; dev log updated.
- [ ] Triage dependency explicit: removing `pel_size` flips the queue card to `n/a` (Task 3 guard).
