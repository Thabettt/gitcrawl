# Quota Utilization and Limiter Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the unused half of the 5,000-point GraphQL meter into speed (batch 29, hydration concurrency 32) and fix the local limiter's edge bugs (window anchored to the real reset, header-supplied limit, no hot re-knock on a 200 body that says "rate limit"), while capturing the point telemetry Theme 3's controller will need.

**Architecture:** Five static changes plus one migration: (1) `DEFAULT_BATCH_SIZE`/settings/corpus default 29 (largest 1-point batch: `round(5·29/100)=1`), DB defaults realigned by migration `0014`; (2) hydration concurrency ceiling raised from a hard 20 to a named 32 (`HYDRATION_CONCURRENCY_CEILING`) with the corpus preset supplying the slot cap — Theme 3 later replaces the static `min()` with a live AIMD window inside the same ceiling; (3) hydration queries request `rateLimit { cost used remaining }`, `BatchStats` gains point fields, audit rows gain `rl_used`/`run_id`/`phase`, and the runner persists a per-phase `PointsLedger` under `field_stats["points"]`; (4) the Redis fixed-window counter is re-anchored from `floor(now/window)` to a stored `window_end` taken from `x-ratelimit-reset`, and reconcile uses `x-ratelimit-limit` when present; (5) final 403/429 keep GitHub's message and a 200 body containing rate-limit markers pauses the bucket (reusing the classifier's header parser) instead of hot-requeueing.

**Tech Stack:** Python 3.12, httpx 0.28.1 (sync client, `MockTransport` in tests), sync SQLAlchemy 2.1 + psycopg3, Redis (fakeredis in tests), Alembic, pytest, ruff/black/mypy.

**Spec:** `design/runtime-audit-and-adaptive-control.md` (§4, §5, §6, §7.3's telemetry prerequisite, §8 recommendations 1, 2, 5, 6). Theme 3 owns the controller loops (AIMD window, batch-size adapter, quota pacer, pool-wide pause with retry budget, kill switch) and adds no migrations.

## Global Constraints

- Python 3.12; do not add dependencies; httpx stays 0.28.1; sync SQLAlchemy 2.1 + psycopg; Redis.
- Alembic must have exactly one new head: revision `0014`, `down_revision = "0013"`. Theme 3 creates no migrations.
- Coverage gate is `fail_under = 93` (`pyproject.toml`); the full suite must stay green.
- Lint/format/types: `.venv\Scripts\python.exe -m ruff check src tests` (E,F,I,UP,B; line 100), `black --check src tests` (line 100), `.venv\Scripts\python.exe -m mypy` (scope `src/lib`, `src/limiter`, `src/store`, `src/scheduler`).
- Keep the `graphql_batch_size` cap at 1..50 everywhere (`MAX_BATCH_SIZE = 50`, `app_settings_batch_size` check, `_BOUNDS`, form copy "1–50").
- The limiter test contracts at `tests/unit/test_buckets.py` are pinned deliberately: the ratchet test (`:131-142`) and the reconcile tests must keep passing unchanged; the two tests that encode top-of-hour flooring (`:34-40`, `:51-56`) and the `window` hash-field assertion (`:260-267`) are deliberately revised in Task 6, with the reason stated inline.
- No new settings UI fields; corpus preset values may change within existing bounds.
- Windows PowerShell 5.1. Run DB suites with `TEST_DATABASE_URL` pointing at a database whose name ends `_test`. `$env:PYTHONPATH = 'src'` for ad-hoc scripts.
- Do not commit secrets; no raw token in logs or tests.
- Out of scope (other themes): httpx client limits/`trust_env`/HTTP2, double-parse elimination, discovery upsert offloading, audit writer changes (Theme 1); AIMD controller, batch-size adapter, quota pacer/reserve, pool-wide coordinated pause, kill switch (Theme 3).

---

### Task 1: Migration `0014`, schema/model sync, batch-size 29 defaults

**Files:**
- Create: `migrations/versions/0014_audit_point_telemetry.py`
- Modify: `src/lib/graphql_batch.py:20`, `src/store/settings.py:38`, `src/serve/settings_spec.py:22`
- Modify: `src/store/models.py:253-263` (`AppSettings.__table_args__`), `:276-278` (`graphql_batch_size` server default), `:290-311` (`AuditLog`)
- Test: `tests/integration/test_models_migrations.py:111-128`, `:129-141`, `:152-161`, `:279-299`, `:396`; `tests/unit/test_run_settings.py`; `tests/unit/test_settings_spec.py:74-85`; `tests/contract/test_settings.py:292-312`; `tests/integration/test_settings_store.py:19-21`; golden snapshot `tests/golden/snapshots/settings.json`

**Interfaces:**
- Produces: `DEFAULT_BATCH_SIZE = 29` (`lib.graphql_batch`); `RunSettings.graphql_batch_size: int = 29`; corpus preset `graphql_batch_size = 29`; migration revision `"0014"` (down_revision `"0013"`).
- Produces (schema, consumed by Tasks 3-4): `audit_log.rl_used INTEGER NULL`, `audit_log.run_id BIGINT NULL`, `audit_log.phase TEXT NULL`, index `audit_run_idx (run_id)`; `app_settings.graphql_batch_size` server default `29`, existing rows at the old default `20` updated to `29`.
- DB default decision: the server default **must** move to 29. Without it, every fresh row (including the one `clean_db` and the golden fixture insert) is seeded 20 and silently overrides the code default; `test_defaults_are_seeded_by_the_migration` (`RunSettings()` equality) would fail. The migration therefore also runs `UPDATE app_settings SET graphql_batch_size = 29 WHERE graphql_batch_size = 20` — a one-time alignment of the migrated default; an explicitly saved 20 is indistinguishable from the old default, so this is accepted and documented (downgrade mirrors `0013`'s precedent and clamps 29 → 20). `run_id` is a plain BIGINT, not a FK to `runs`: audit_log is append-only log data, and a FK would couple log writes to run-row lifecycle. No backfill of audit rows is needed — they are historical log data and the new columns are nullable.

- [ ] **Step 1: Write/update the failing tests**

Add to `tests/unit/test_run_settings.py`:

```python
def test_graphql_batch_size_defaults_to_29():
    assert RunSettings().graphql_batch_size == 29
```

Update `tests/unit/test_settings_spec.py::test_corpus_preset_returns_the_coherent_profile` body to:

```python
    assert parse_settings_form({"preset": "corpus"}) == {
        "max_shards": 1_000,
        "max_candidates": 100_000,
        "max_hydrate": 100_000,
        "max_enrich": 100_000,
        "request_deadline_seconds": 86_400,
        "graphql_batch_size": 29,
        "limiter_max_concurrent": 20,
        "discovery_concurrency": 32,
        "graphql_batch": True,
    }
```

Update `tests/contract/test_settings.py::test_set_to_corpus_build_limits_saves_the_profile` to expect `graphql_batch_size=29` and `limiter_max_concurrent=20`.

Update `tests/integration/test_models_migrations.py`:
- `EXPECTED_COLUMNS["audit_log"]` gains `"rl_used": True`, `"run_id": True`, `"phase": True`.
- `EXPECTED_INDEXES` gains `"audit_run_idx": "audit_log"`.
- `test_server_defaults` gains `("app_settings", "graphql_batch_size"): "29"`.
- `test_model_metadata_indexes_match_contract` audit assertion becomes:

```python
    assert {index.name for index in Base.metadata.tables["audit_log"].indexes} == {
        "audit_ts_idx",
        "audit_run_idx",
    }
```

- [ ] **Step 2: Run to verify failure**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_run_settings.py tests/unit/test_settings_spec.py -q
```
Expected: FAIL — `graphql_batch_size` is 20 / corpus preset mismatch. (The DB tests fail only if the DB is already at 0013; the migration file lands in Step 3.)

- [ ] **Step 3: Implement migration, models, and constants**

`migrations/versions/0014_audit_point_telemetry.py`:

```python
"""audit_log: point telemetry columns, run attribution; batch default 29

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("rl_used", sa.Integer(), nullable=True))
    op.add_column("audit_log", sa.Column("run_id", sa.BigInteger(), nullable=True))
    op.add_column("audit_log", sa.Column("phase", sa.Text(), nullable=True))
    op.create_index("audit_run_idx", "audit_log", ["run_id"])
    op.alter_column("app_settings", "graphql_batch_size", server_default=sa.text("29"))
    op.execute(
        "UPDATE app_settings SET graphql_batch_size = 29 WHERE graphql_batch_size = 20"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE app_settings SET graphql_batch_size = 20 WHERE graphql_batch_size = 29"
    )
    op.alter_column("app_settings", "graphql_batch_size", server_default=sa.text("20"))
    op.drop_index("audit_run_idx", table_name="audit_log")
    op.drop_column("audit_log", "phase")
    op.drop_column("audit_log", "run_id")
    op.drop_column("audit_log", "rl_used")
```

`src/store/models.py` `AuditLog.__table_args__`:

```python
    __table_args__ = (Index("audit_ts_idx", "ts"), Index("audit_run_idx", "run_id"))
```

Append to `AuditLog` after `latency_ms`:

```python
    rl_used: Mapped[int | None] = mapped_column(Integer)
    run_id: Mapped[int | None] = mapped_column(BigInteger)
    phase: Mapped[str | None] = mapped_column(Text)
```

`AppSettings.graphql_batch_size` server default becomes `server_default=text("29")`.

Code defaults: `src/lib/graphql_batch.py` `DEFAULT_BATCH_SIZE = 29`; `src/store/settings.py` `graphql_batch_size: int = 29`; `src/serve/settings_spec.py` `_CORPUS_VALUES["graphql_batch_size"] = 29`.

- [ ] **Step 4: Run the focused suites**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_run_settings.py tests/unit/test_settings_spec.py tests/integration/test_settings_store.py tests/integration/test_models_migrations.py tests/contract/test_settings.py -q
```
Expected: PASS (`test_defaults_are_seeded_by_the_migration` passes because migration 0014 updates the seeded row; `test_downgrade_and_upgrade_round_trip` exercises 0014's downgrade).

- [ ] **Step 5: Regenerate the settings golden snapshot and run lint/types**

```powershell
$env:UPDATE_GOLDEN = "1"; .venv\Scripts\python.exe -m pytest tests/golden -q
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m black --check src tests
.venv\Scripts\python.exe -m mypy
```
The snapshot changes because the seeded `app_settings` row now renders `graphql_batch_size = 29`.

- [ ] **Step 6: Commit**

```powershell
git add migrations/versions/0014_audit_point_telemetry.py src/lib/graphql_batch.py src/store/settings.py src/store/models.py src/serve/settings_spec.py tests
git commit -m "feat(settings): batch default 29, audit point-telemetry columns (migration 0014)"
```

### Task 2: Hydration `rateLimit { cost used remaining }` telemetry into `BatchStats`

**Files:**
- Modify: `src/lib/graphql_batch.py:45-83` (`ParsedBatch`, `BatchStats`), `:127-175` (`_post`), `:333-337` (result folding)
- Modify: `src/hydrate/graphql_repo.py:230-240` (`build_query`), `:242-267` (`parse`)
- Test: `tests/unit/test_graphql_batch_core.py`, `tests/unit/test_graphql_repo_adapter.py`

**Interfaces:**
- Produces (consumed by Task 4 and Theme 3):

```python
@dataclass(frozen=True)
class RateLimitInfo:
    cost: int | None = None
    used: int | None = None
    remaining: int | None = None

def rate_limit_from_payload(payload: object) -> RateLimitInfo | None: ...
```

- `ParsedBatch` gains `rate_limit: RateLimitInfo | None = None` (default keeps `DictAdapter`, owner, and file adapters working unchanged).
- `BatchStats` gains `points_cost: int = 0`, `points_used: int | None = None`, `points_remaining: int | None = None`; `as_dict()` includes all three. Aggregation: `points_cost` sums `cost`; `points_used` keeps the max; `points_remaining` keeps the min (order-independent across concurrent responses).

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_graphql_batch_core.py` — extend `DictAdapter.parse` to forward telemetry:

```python
from lib.graphql_batch import (
    GraphQLAuthError,
    ParsedBatch,
    fetch_batch,
    rate_limit_from_payload,
)
```
(keep the existing imports otherwise), then in `DictAdapter.parse` build `values` as today and end with:

```python
        return ParsedBatch(values=values, rate_limit=rate_limit_from_payload(payload))
```

New test:

```python
def test_rate_limit_telemetry_folds_into_batch_stats():
    responses = [
        httpx.Response(
            200,
            json={
                "data": {
                    "n0": "value-1",
                    "rateLimit": {"cost": 1, "used": 400, "remaining": 4600},
                }
            },
        ),
        httpx.Response(
            200,
            json={
                "data": {
                    "n0": "value-2",
                    "rateLimit": {"cost": 2, "used": 402, "remaining": 4598},
                }
            },
        ),
    ]
    adapter = DictAdapter()
    adapter.batch_size = 1
    client = client_from(responses)
    outcome = fetch_batch(adapter, ["1", "2"], client=client, concurrency=1)
    assert outcome.values == {"1": "value-1", "2": "value-2"}
    assert outcome.stats.points_cost == 3
    assert outcome.stats.points_used == 402
    assert outcome.stats.points_remaining == 4598
    assert outcome.stats.as_dict()["points_cost"] == 3
```

Add to `tests/unit/test_graphql_repo_adapter.py`:

```python
def test_build_query_requests_rate_limit_telemetry():
    adapter = RepoDetailsAdapter({"911": "octo/alpha"})
    query = adapter.build_query({"n0": "911"})
    assert "rateLimit { cost used remaining }" in query


def test_parse_reads_the_rate_limit_block():
    from lib.graphql_batch import RateLimitInfo

    adapter = RepoDetailsAdapter({"911": "octo/alpha"})
    payload = {
        "data": {"n0": NODE, "rateLimit": {"cost": 1, "used": 412, "remaining": 4588}}
    }
    parsed = adapter.parse(payload, {"n0": "911"})
    assert parsed.rate_limit == RateLimitInfo(cost=1, used=412, remaining=4588)
```

- [ ] **Step 2: Run to verify failure**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_batch_core.py tests/unit/test_graphql_repo_adapter.py -q
```
Expected: FAIL — no `rate_limit_from_payload` / no `points_cost` / `rateLimit` not in query.

- [ ] **Step 3: Implement**

In `src/lib/graphql_batch.py` (place after `ParsedBatch`; annotate exactly so mypy stays clean):

```python
@dataclass(frozen=True)
class RateLimitInfo:
    cost: int | None = None
    used: int | None = None
    remaining: int | None = None


def _as_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def rate_limit_from_payload(payload: object) -> RateLimitInfo | None:
    """Read `data.rateLimit { cost used remaining }` from a GraphQL response body."""
    if not isinstance(payload, Mapping):
        return None
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None
    node = data.get("rateLimit")
    if not isinstance(node, Mapping):
        return None
    return RateLimitInfo(
        cost=_as_int(node.get("cost")),
        used=_as_int(node.get("used")),
        remaining=_as_int(node.get("remaining")),
    )
```

`ParsedBatch` becomes:

```python
@dataclass(frozen=True)
class ParsedBatch[T]:
    values: Mapping[str, T] = field(default_factory=dict)
    failures: Mapping[str, str] = field(default_factory=dict)
    rate_limit: RateLimitInfo | None = None
```

`BatchStats` new fields (after `deadline_hit`) and `as_dict()` entries:

```python
    points_cost: int = 0
    points_used: int | None = None
    points_remaining: int | None = None
```
```python
            "points_cost": self.points_cost,
            "points_used": self.points_used,
            "points_remaining": self.points_remaining,
```

Module-level fold helper:

```python
def _fold_rate_limit(stats: BatchStats, info: RateLimitInfo) -> None:
    if info.cost is not None:
        stats.points_cost += info.cost
    if info.used is not None:
        stats.points_used = (
            info.used if stats.points_used is None else max(stats.points_used, info.used)
        )
    if info.remaining is not None:
        stats.points_remaining = (
            info.remaining
            if stats.points_remaining is None
            else min(stats.points_remaining, info.remaining)
        )
```

In `_post`, forward the parsed telemetry instead of rebuilding a bare `ParsedBatch` (this is the bug where the block would otherwise be dropped):

```python
    return (
        ParsedBatch(values=values, failures=failures, rate_limit=parsed.rate_limit),
        tuple(batch_errors),
    )
```

In `fetch_batch`, immediately after `parsed, batch_errors = future.result()`:

```python
                    if parsed.rate_limit is not None:
                        _fold_rate_limit(stats, parsed.rate_limit)
```

In `src/hydrate/graphql_repo.py`, import `rate_limit_from_payload` alongside `ParsedBatch`, append the telemetry field in `build_query` before the closing brace:

```python
        lines.append("  rateLimit { cost used remaining }")
        lines.append("}")
        return "\n".join(lines)
```

and in `parse`, capture it before the early return path:

```python
        rate_limit = rate_limit_from_payload(payload)
        if not isinstance(data, dict):
            return ParsedBatch(values=values, failures=failures, rate_limit=rate_limit)
        ...
        return ParsedBatch(values=values, failures=failures, rate_limit=rate_limit)
```

- [ ] **Step 4: Run to verify pass**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_batch_core.py tests/unit/test_graphql_repo_adapter.py tests/unit/test_classifier.py tests/integration/test_hydrate_batch.py -q
```
Expected: PASS.

- [ ] **Step 5: Lint/types then commit**

```powershell
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m black --check src tests
.venv\Scripts\python.exe -m mypy
git add src/lib/graphql_batch.py src/hydrate/graphql_repo.py tests/unit/test_graphql_batch_core.py tests/unit/test_graphql_repo_adapter.py
git commit -m "feat(telemetry): parse GraphQL rateLimit cost/used/remaining into BatchStats"
```

### Task 3: `AuditRecord` point/run/phase fields and `PointsLedger`

**Files:**
- Modify: `src/lib/audit.py:17-33` (`AuditRecord`), `:87-128` (`record_from_response`); append `PointsLedger`
- Test: `tests/unit/test_audit.py`, `tests/integration/test_audit_db.py`

**Interfaces:**
- Produces: `AuditRecord.rl_used: int | None = None`, `AuditRecord.run_id: int | None = None`, `AuditRecord.phase: str | None = None` (trailing defaulted fields, so every existing keyword call site keeps working).
- Produces: `record_from_response(..., *, run_id: int | None = None, phase: str | None = None)`; `rl_used` is `_as_int(x-ratelimit-used header)`.
- Produces (consumed by Task 4): `audit.PointsLedger` with `add(phase: str, body: object | None) -> None` and `as_dict() -> dict[str, dict[str, int]]`; the dict shape is `{phase: {"points": int, "responses": int}}`, counting only responses that carry a `rateLimit.cost`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_audit.py`:

```python
def test_record_from_response_carries_run_phase_and_used_header():
    headers = {"x-ratelimit-used": "412", "x-ratelimit-resource": "graphql"}
    record = record_from_response(
        {},
        _response(200, headers),
        token_fp="fp",
        latency_ms=1,
        now=NOW,
        run_id=7,
        phase="hydration",
    )
    assert record.rl_used == 412
    assert record.run_id == 7
    assert record.phase == "hydration"


def test_record_from_response_defaults_point_attribution_to_none():
    record = record_from_response({}, _response(200), token_fp="fp", latency_ms=1, now=NOW)
    assert record.rl_used is None
    assert record.run_id is None
    assert record.phase is None


def test_points_ledger_sums_cost_per_phase_and_skips_bodies_without_telemetry():
    from lib.audit import PointsLedger

    ledger = PointsLedger()
    ledger.add("hydration", {"data": {"rateLimit": {"cost": 1, "used": 10, "remaining": 90}}})
    ledger.add("hydration", {"data": {"rateLimit": {"cost": 2, "used": 12, "remaining": 88}}})
    ledger.add("discovery", {"data": {"s": {}, "rateLimit": {"cost": 1, "used": 13, "remaining": 87}}})
    ledger.add("enrich", {"data": {}})
    assert ledger.as_dict() == {
        "discovery": {"points": 1, "responses": 1},
        "hydration": {"points": 3, "responses": 2},
    }
```

`tests/integration/test_audit_db.py` — extend the `_record` helper with `rl_used: int | None = None, run_id: int | None = None, phase: str | None = None` parameters forwarded into the `AuditRecord(...)` constructor, then add:

```python
def test_record_audit_persists_point_telemetry(clean: Engine):
    record = _record(0, rl_used=4123, run_id=13, phase="hydration")
    record_audit(clean, record)
    with clean.connect() as connection:
        row = connection.execute(
            text("SELECT rl_used, run_id, phase FROM audit_log")
        ).mappings().one()
    assert dict(row) == {"rl_used": 4123, "run_id": 13, "phase": "hydration"}
```

- [ ] **Step 2: Run to verify failure**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_audit.py tests/integration/test_audit_db.py -q
```
Expected: FAIL — unexpected keyword `run_id` / no `PointsLedger`.

- [ ] **Step 3: Implement**

`src/lib/audit.py` imports — add `field`. Import `rate_limit_from_payload` **inside** `PointsLedger.add`, not at module level: Theme 1's Task 2 makes `lib.graphql_batch` import `lib.audit` at module level, so a top-level `audit -> graphql_batch` import here creates an initialization-order-dependent circular import (reproduced on this repo's Python 3.12: `ImportError: cannot import name 'rate_limit_from_payload' from partially initialized module 'lib.graphql_batch'` whenever `lib.graphql_batch` is imported first, e.g. via `import store.settings`):

```python
from dataclasses import asdict, dataclass, field
```

`AuditRecord` trailing fields:

```python
    token_fp: str
    latency_ms: int
    rl_used: int | None = None
    run_id: int | None = None
    phase: str | None = None
```

`record_from_response` signature gains the two keyword-only params after `total_count`, and the returned record gains:

```python
        rl_used=_as_int(_header(headers, "x-ratelimit-used")),
        run_id=run_id,
        phase=phase,
```

Append the ledger (mypy-clean on `src/lib`):

```python
@dataclass
class PointsLedger:
    """Thread-safe per-phase GraphQL point totals observed in response bodies."""

    _points: dict[str, int] = field(default_factory=dict)
    _responses: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, phase: str, body: object | None) -> None:
        from lib.graphql_batch import rate_limit_from_payload

        info = rate_limit_from_payload(body)
        if info is None or info.cost is None:
            return
        with self._lock:
            self._points[phase] = self._points.get(phase, 0) + info.cost
            self._responses[phase] = self._responses.get(phase, 0) + 1

    def as_dict(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {
                phase: {"points": points, "responses": self._responses[phase]}
                for phase, points in sorted(self._points.items())
            }
```

- [ ] **Step 4: Run to verify pass**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_audit.py tests/integration/test_audit_db.py -q
```
Expected: PASS (existing record-equality tests pass because the new fields default to `None` on both sides).

- [ ] **Step 5: Lint/types then commit**

```powershell
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m black --check src tests
.venv\Scripts\python.exe -m mypy
git add src/lib/audit.py tests/unit/test_audit.py tests/integration/test_audit_db.py
git commit -m "feat(audit): rl_used/run_id/phase columns and per-phase PointsLedger"
```

### Task 4: Thread `run_id`/phase/ledger through pipeline and runner; `field_stats["points"]`

**Files:**
- Modify: `src/discover/pipeline.py:114-132` (`_graphql_audit_hook`), `:186-205` (`count_total`), `:345-443` (`run_search_discovery`)
- Modify: `src/serve/runner.py:152-173` (`_audit_hook`), `:771-793` (`run_filter`), `:794-969` (`_run_filter`), `:972-977` (`make_runner`)
- Modify: `src/serve/app.py:389-403` (pass `run_id`)
- Test: `tests/integration/test_runner.py:120-153` (helper), `:1238-1246`, `:1273-1287` (existing assertions), new end-to-end test

**Interfaces:**
- Consumes: `audit.PointsLedger` (Task 3), `rate_limit_from_payload` (Task 2).
- Produces: `pipeline.count_total(deps, query, *, on_response=None, sleep=time.sleep, now=time.time, jitter=None, run_id: int | None = None, phase: str = "count", ledger: audit.PointsLedger | None = None) -> int`.
- Produces: `pipeline.run_search_discovery(..., run_id: int | None = None, ledger: audit.PointsLedger | None = None)` — its page/probe hook is phase `"discovery"`.
- Produces: `runner.run_filter(deps, spec, *, config: RunnerConfig | None = None, run_id: int | None = None) -> RunPayload`; `RunPayload.field_stats["points"]` is the ledger dict (stable key even when empty: `{}`).
- Phase names are exactly `"count"`, `"discovery"`, `"hydration"`, `"enrich"`.

- [ ] **Step 1: Write the failing tests**

In `tests/integration/test_runner.py`, extend the mock helper `graphql_batch_response` (`:120-142`) with the two new keyword-only params:

```python
def graphql_batch_response(
    request: httpx.Request,
    items_by_full_name: dict[str, dict],
    *,
    commit_counts: dict[int, int] | None = None,
    rate_limit: dict[str, int] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
```
and before building the response:

```python
    if rate_limit is not None:
        data["rateLimit"] = rate_limit
```
```python
    return httpx.Response(200, json=payload, headers=headers)
```

Update the two existing `field_stats` assertions (`:1238-1246`, `:1273-1281`) to drop the new key alongside `graphql`:

```python
    field_stats = dict(payload.field_stats)
    field_stats.pop("graphql")
    field_stats.pop("points")
```

Add the end-to-end test:

```python
def test_run_filter_persists_per_phase_points_and_audit_attribution(clean: Engine):
    item = repo_item(1)

    def handler(request: httpx.Request):
        path = path_of(request)
        if is_search_request(request):
            if is_count(request):
                return count_response(request, 1)
            return page_response([item])
        if path == "/graphql":
            return graphql_batch_response(
                request,
                {item["full_name"]: item},
                rate_limit={"cost": 1, "used": 412, "remaining": 4588},
                headers={"x-ratelimit-used": "412"},
            )
        raise AssertionError(f"unexpected path {path}")

    client, _ = scripted(handler)
    payload = run_filter(
        make_deps(clean, client),
        spec_for(q="language:python"),
        config=RunnerConfig(max_shards=1, max_enrich=0),
        run_id=42,
    )
    points = payload.field_stats["points"]
    assert points["count"]["points"] == 1
    assert points["discovery"]["points"] == 1
    assert points["hydration"]["points"] == 1
    with clean.connect() as connection:
        phases = set(connection.execute(text("SELECT DISTINCT phase FROM audit_log")).scalars())
        run_ids = set(connection.execute(text("SELECT DISTINCT run_id FROM audit_log")).scalars())
        used = connection.execute(
            text("SELECT rl_used FROM audit_log WHERE phase = 'hydration'")
        ).scalar()
    assert {"count", "discovery", "hydration"} <= phases
    assert run_ids == {42}
    assert used == 412
```

- [ ] **Step 2: Run to verify failure**

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_runner.py -q -k "per_phase or field_stats"
```
Expected: FAIL — `run_filter` has no `run_id`; `field_stats` has no `points`.

- [ ] **Step 3: Implement pipeline hooks**

`src/discover/pipeline.py`:

```python
def _graphql_audit_hook(
    deps: Deps,
    *,
    run_id: int | None = None,
    phase: str = "discovery",
    ledger: audit.PointsLedger | None = None,
) -> Callable[[httpx.Response, float, Mapping[str, object]], None]:
    def hook(response: httpx.Response, latency_ms: float, params: Mapping[str, object]) -> None:
        body = _graphql_body(response)
        record = audit.record_from_response(
            params,
            response,
            token_fp=deps.token_fp,
            latency_ms=latency_ms,
            body=body,
            total_count=_graphql_total_count(body),
            run_id=run_id,
            phase=phase,
        )
        if ledger is not None:
            ledger.add(phase, body)
        if deps.audit_buffer is not None:
            deps.audit_buffer.add(record)
        else:
            audit.record_audit(deps.engine, record)

    return hook
```

`count_total` gains `run_id: int | None = None, phase: str = "count", ledger: audit.PointsLedger | None = None` and builds `hook = _graphql_audit_hook(deps, run_id=run_id, phase=phase, ledger=ledger) if on_response is None else on_response`.

`run_search_discovery` gains `run_id: int | None = None, ledger: audit.PointsLedger | None = None` and its hook line becomes:

```python
    hook = _graphql_audit_hook(deps, run_id=run_id, phase="discovery", ledger=ledger)
```

- [ ] **Step 4: Implement runner wiring**

`_audit_hook` gains `run_id`, `phase`, `ledger` keyword-only params; inside the hook, after building `record`, add:

```python
        if ledger is not None and phase is not None:
            ledger.add(phase, audit.cached_json(response))
```
(`record_from_response` has already parsed and cached the body.)

`run_filter` signature gains `run_id: int | None = None`, forwarded as `_run_filter(deps, spec, config=config, run_id=run_id)`.

`_run_filter` gains `run_id: int | None = None`; at the top:

```python
    ledger = audit.PointsLedger()
```
and the phase calls become:

```python
    total_count = pipeline.count_total(deps, query, run_id=run_id, ledger=ledger)
```
```python
    stats = pipeline.run_search_discovery(
        deps,
        query,
        max_shards=cfg.max_shards,
        max_pages=spec.max_pages,
        total_count=total_count,
        discovery_concurrency=cfg.discovery_concurrency,
        run_id=run_id,
        ledger=ledger,
    )
```
Replace the single `hook = _audit_hook(deps)` with two hooks and pass them to their stages:

```python
    hydration_hook = _audit_hook(deps, run_id=run_id, phase="hydration", ledger=ledger)
    ...
    hydration = _hydrate(deps, candidates, cfg, hydration_hook, ...)
    ...
    enrich_hook = _audit_hook(deps, run_id=run_id, phase="enrich", ledger=ledger)
    handlers, unsupported = _enrich_handlers(
        deps, rows, virtual, budget, enrich_hook, skipped, graphql_report,
        hydration.commit_counts, language_bytes=hydration.language_bytes, cfg=cfg,
    )
```
At the end:

```python
    field_stats = asdict(segment_stats)
    field_stats["points"] = ledger.as_dict()
    field_stats["graphql"] = graphql_report
```

`make_runner` passes the run id through:

```python
    def runner(run_id: int, filter_spec: dict) -> RunPayload:
        return run_filter(deps, parse_filter_spec(filter_spec), config=cfg, run_id=run_id)
```

`src/serve/app.py:397-401` adds `run_id=run_id` to its `run_filter(...)` call.

- [ ] **Step 5: Run the runner/pipeline suites**

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_runner.py tests/integration/test_pipeline.py tests/unit/test_executor_unit.py -q
```
Expected: PASS.

- [ ] **Step 6: Lint/types then commit**

```powershell
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m black --check src tests
.venv\Scripts\python.exe -m mypy
git add src/discover/pipeline.py src/serve/runner.py src/serve/app.py tests/integration/test_runner.py
git commit -m "feat(telemetry): run_id/phase audit attribution and per-phase points in field_stats"
```

### Task 5: Static hydration concurrency 20 → 32 as the ceiling, corpus preset 32

**Files:**
- Modify: `src/serve/runner.py:59-83` (constant + `runner_config_from`)
- Modify: `src/serve/settings_spec.py:23` (`_CORPUS_VALUES["limiter_max_concurrent"] = 32`)
- Test: `tests/integration/test_runner.py:1503-1522`, `tests/unit/test_settings_spec.py:74-85`, `tests/contract/test_settings.py:292-312`

**Interfaces:**
- Produces: `serve.runner.HYDRATION_CONCURRENCY_CEILING = 32`; `runner_config_from` yields `concurrency = min(settings.limiter_max_concurrent, HYDRATION_CONCURRENCY_CEILING)`.
- Deliberately static first step: the pool size is still the runtime value handed to `fetch_batch`, but 32 is now a named ceiling rather than an inline literal. Theme 3 will construct the pool at the ceiling and gate submissions with a live window; nothing in this task should be read as the final controller.
- The Redis slot cap already follows settings (`serve/app.py:395` passes `settings.limiter_max_concurrent` to `build_deps`); raising the corpus preset to 32 makes both numbers 32 for corpus runs, so `concurrency <= limiter.max_concurrent` holds.

- [ ] **Step 1: Update the failing tests**

`tests/integration/test_runner.py` — rename/rewrite `test_corpus_profile_hydrates_at_twenty_with_matching_limiter_cap`:

```python
def test_corpus_profile_hydrates_at_the_ceiling_with_matching_limiter_cap(clean: Engine):
    from serve.runner import build_deps, runner_config_from
    from serve.settings_spec import parse_settings_form
    from store.settings import update_run_settings

    settings = update_run_settings(clean, parse_settings_form({"preset": "corpus"}))
    config = runner_config_from(settings)
    deps = build_deps(
        clean,
        token="t",
        redis_client=fakeredis.FakeRedis(),
        max_concurrent=settings.limiter_max_concurrent,
    )
    assert settings.limiter_max_concurrent == 32
    assert config.concurrency == 32
    assert deps.limiter is not None
    assert config.concurrency <= deps.limiter.max_concurrent

    raised = update_run_settings(clean, {"limiter_max_concurrent": 64})
    assert runner_config_from(raised).concurrency == 32
    lowered = update_run_settings(clean, {"limiter_max_concurrent": 12})
    assert runner_config_from(lowered).concurrency == 12
```

Update `tests/unit/test_settings_spec.py::test_corpus_preset_returns_the_coherent_profile` and `tests/contract/test_settings.py::test_set_to_corpus_build_limits_saves_the_profile` to `limiter_max_concurrent: 32` / `limiter_max_concurrent=32` (batch size stays 29 from Task 1).

- [ ] **Step 2: Run to verify failure**

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_runner.py -q -k "corpus_profile" tests/unit/test_settings_spec.py
```
Expected: FAIL — corpus preset still 20, current cap still 20.

- [ ] **Step 3: Implement**

`src/serve/runner.py` near the top constants:

```python
HYDRATION_CONCURRENCY_CEILING = 32
```

`runner_config_from`:

```python
        concurrency=min(settings.limiter_max_concurrent, HYDRATION_CONCURRENCY_CEILING),
```

`src/serve/settings_spec.py` `_CORPUS_VALUES`: `"limiter_max_concurrent": 32`.

- [ ] **Step 4: Run to verify pass**

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_runner.py tests/unit/test_settings_spec.py tests/contract/test_settings.py tests/integration/test_pipeline.py -q
```
Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/serve/runner.py src/serve/settings_spec.py tests/integration/test_runner.py tests/unit/test_settings_spec.py tests/contract/test_settings.py
git commit -m "feat(runner): hydration concurrency ceiling 32 and corpus preset 32"
```

### Task 6: Limiter window re-anchor and header-supplied reconcile limit

**Files:**
- Modify: `src/limiter/buckets.py:21-58` (`_ACQUIRE_SCRIPT`), `:69-87` (`_RECONCILE_SCRIPT`), `:193-221` (`update_from_headers`)
- Test: `tests/unit/test_buckets.py:34-40`, `:51-56`, `:260-267` (revised deliberately); new reconcile tests

**Interfaces:**
- Redis hash fields change from `window` (hour index) to `window_end` (epoch seconds of the response's `x-ratelimit-reset`, or `now + window_seconds` at first use). No Python caller reads that field outside tests.
- `update_from_headers` reconcile target uses `x-ratelimit-limit` when present and positive, else the resource spec; it passes the parsed reset (or `-1.0` when absent) into the reconcile script.
- Unchanged: pause behavior, slot semantics, `release`, `bound_concurrency`, the ratchet guarantee (`count` never lowered by reconcile within a window).

- [ ] **Step 1: Revise/add the tests (failing first)**

`tests/unit/test_buckets.py` — deliberate revisions with reasons:

1. `test_search_allows_thirty_per_window_and_denies_the_next` (`:34-40`): denied retry is now the full first-use window. Change `assert denied.retry_after == 50.0` to `assert denied.retry_after == 60.0`. Reason: the window anchors at first use (`now=10`), not at `floor(10/60)`, so its end is 70 and the wait is 70−10.
2. `test_window_rollover_resets_count` (`:51-56`): rename to `test_window_rollover_resets_at_first_use_plus_window` and change the final acquire to `now=70.0`. Reason: `floor(now/60)` defined the old rollover; the new anchor is first use plus the window.
3. `test_acquire_uses_hash_key_with_ttl` (`:260-267`): change `assert redis.hget(key, "window") == b"0"` to `assert redis.hget(key, "window_end") == b"60"` and drop the `window` assertion. Reason: the stored field is now the epoch end.

New tests:

```python
def test_reconcile_anchors_the_window_to_the_response_reset(redis):
    limiter = BucketLimiter(redis, max_concurrent=100)
    for _ in range(30):
        assert limiter.acquire("search", "token-a", now=10.0).allowed
    limiter.update_from_headers(
        "search",
        "token-a",
        {"x-ratelimit-remaining": "7", "x-ratelimit-reset": "40"},
        now=10.0,
    )
    denied = limiter.acquire("search", "token-a", now=11.0)
    assert denied.allowed is False
    assert denied.retry_after == 29.0
    assert limiter.acquire("search", "token-a", now=40.0).allowed is True


def test_reconcile_uses_the_header_limit_over_the_spec(redis):
    limiter = BucketLimiter(redis, specs={"core": (5, 60.0)})
    limiter.update_from_headers(
        "core",
        "token-a",
        {"x-ratelimit-limit": "100", "x-ratelimit-remaining": "40", "x-ratelimit-reset": "60"},
        now=0.0,
    )
    assert redis.hget("gitcrawl:rl:core:token-a", "count") == b"60"
    denied = limiter.acquire("core", "token-a", now=0.0)
    assert denied.allowed is False
    assert denied.retry_after == 60.0
```

Keep unchanged and passing: `test_update_from_headers_remaining_zero_pauses_until_reset`, `test_update_from_headers_reconciles_window_count`, `test_update_from_headers_never_lowers_current_count` (the pinned ratchet test), `test_update_from_headers_ignores_malformed_values`, `test_update_from_headers_ignores_missing_headers`, `test_update_from_headers_remaining_zero_without_reset_does_not_pause`, all pause/concurrency/slot tests.

- [ ] **Step 2: Run to verify failure**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_buckets.py -q
```
Expected: FAIL — `retry_after == 60.0` (currently 50.0), `window_end` missing.

- [ ] **Step 3: Implement the Lua scripts and `update_from_headers`**

`_ACQUIRE_SCRIPT` (replaces lines 21-58; the `CONCURRENCY_RETRY_AFTER` sentinel replace stays):

```lua
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local max_concurrent = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
local paused = tonumber(redis.call('HGET', key, 'paused_until') or '0')
if paused > now then
  return {0, tostring(paused - now)}
end
local window_end = tonumber(redis.call('HGET', key, 'window_end') or '0')
local count = tonumber(redis.call('HGET', key, 'count') or '0')
if window_end <= now then
  count = 0
  window_end = now + window_seconds
end
if count >= limit then
  local retry_after = window_end - now
  if retry_after < 0 then
    retry_after = 0
  end
  redis.call('HSET', key, 'window_end', window_end, 'count', count, 'paused_until', paused)
  redis.call('EXPIRE', key, ttl)
  return {0, tostring(retry_after)}
end
local slots = tonumber(redis.call('HGET', key, 'slots') or '0')
if slots >= max_concurrent then
  redis.call('HSET', key, 'window_end', window_end, 'count', count, 'paused_until', paused)
  redis.call('EXPIRE', key, ttl)
  return {0, tostring(CONCURRENCY_RETRY_AFTER)}
end
count = count + 1
slots = slots + 1
redis.call('HSET', key, 'window_end', window_end, 'count', count, 'slots', slots, 'paused_until', paused)
redis.call('EXPIRE', key, ttl)
return {1, '0'}
```

`_RECONCILE_SCRIPT` (ARGV order now `now, window_seconds, target, reset, ttl`; `reset = -1` means "no reset header"):

```lua
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local target = tonumber(ARGV[3])
local reset = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
local window_end = tonumber(redis.call('HGET', key, 'window_end') or '0')
local count = tonumber(redis.call('HGET', key, 'count') or '0')
if window_end <= now then
  count = 0
  window_end = now + window_seconds
end
if target > count then
  count = target
end
if reset > now then
  window_end = reset
end
redis.call('HSET', key, 'window_end', window_end, 'count', count)
redis.call('EXPIRE', key, ttl)
return count
```

`update_from_headers`:

```python
        spec = self._specs.get(resource)
        if spec is None:
            return
        limit, window_seconds = spec
        header_limit = _parse_int(normalized.get("x-ratelimit-limit"))
        effective_limit = header_limit if header_limit is not None and header_limit > 0 else limit
        target = max(0, effective_limit - remaining)
        self._redis.eval(
            _RECONCILE_SCRIPT,
            1,
            self._key(resource, token_id),
            now,
            window_seconds,
            target,
            reset if reset is not None else -1.0,
            self._ttl(window_seconds),
        )
```

Semantics note for the plan's readers: if `window_end <= now` the counter restarts at `now + window_seconds`, then the next response re-anchors it to the real reset. A reset in the past does not move `window_end`, so `update_from_headers` never creates a backward window. This removes the `floor(now/3600)` self-stall; from now on the client's clock is GitHub's.

- [ ] **Step 4: Run to verify pass**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_buckets.py tests/unit/test_graphql_batch_core.py tests/integration/test_runner.py -q
```
Expected: PASS.

- [ ] **Step 5: Lint/types then commit**

```powershell
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m black --check src tests
.venv\Scripts\python.exe -m mypy
git add src/limiter/buckets.py tests/unit/test_buckets.py
git commit -m "fix(limiter): anchor the window to x-ratelimit-reset and use the header limit"
```

### Task 7: Preserve 403/429 messages and pause on 200-body rate-limit errors

**Files:**
- Modify: `src/limiter/classifier.py:66-95` (extract `rate_limit_wait`, reuse in `classify`)
- Modify: `src/lib/graphql_batch.py:15` (imports), `:24-34` (markers/constant), `:117-175` (`_post`), new helpers
- Test: `tests/unit/test_classifier.py`, `tests/unit/test_graphql_batch_core.py`

**Interfaces:**
- Produces: `limiter.classifier.rate_limit_wait(headers: Mapping[str, str], *, now: float) -> tuple[Action, float] | None` — `(Action.RETRY_AFTER, seconds)` for a positive `retry-after`, `(Action.WAIT_RESET, seconds)` for `remaining == 0` + parseable reset, else `None`.
- `classify`'s 403/429 branch delegates to it; outputs (actions, seconds, reasons) are unchanged, which the existing classifier tests pin.
- Produces: `graphql_batch._DEFAULT_RATE_LIMIT_PAUSE = 60.0`; on a 200 body whose `errors` contain a rate-limit marker and whose headers carry no usable signal, `_post` pauses the `graphql` bucket for 60 s + optional jitter before returning; when headers do carry a signal the pause uses the header-derived seconds exactly.
- `_post`'s non-200 error is `RequestFailed(status, short_message(response))`, so the final 403/429 message survives into `BatchOutcome.unresolved`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_classifier.py`:

```python
def test_rate_limit_wait_returns_retry_after():
    from limiter.classifier import rate_limit_wait

    assert rate_limit_wait({"retry-after": "30"}, now=1000.0) == (Action.RETRY_AFTER, 30.0)


def test_rate_limit_wait_returns_reset_when_exhausted():
    from limiter.classifier import rate_limit_wait

    assert rate_limit_wait(
        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1300"}, now=1000.0
    ) == (Action.WAIT_RESET, 300.0)


def test_rate_limit_wait_is_none_without_usable_signals():
    from limiter.classifier import rate_limit_wait

    assert rate_limit_wait({}, now=1000.0) is None
    assert rate_limit_wait({"retry-after": "0"}, now=1000.0) is None
```

`tests/unit/test_graphql_batch_core.py`:

```python
def test_final_forbidden_keeps_the_github_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"message": "You have exceeded a secondary rate limit. Please wait a few minutes"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        DictAdapter(), ["1"], client=client, max_attempts=1, sleep=lambda _seconds: None
    )
    assert "secondary rate limit" in outcome.unresolved["1"]


def test_rate_limit_error_body_pauses_the_bucket_when_headers_are_silent(redis):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": None, "errors": [{"message": "You have exceeded a secondary rate limit"}]},
        )

    limiter = BucketLimiter(redis, max_concurrent=10)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        DictAdapter(),
        ["1"],
        client=client,
        limiter=limiter,
        token_id="token-a",
        max_attempts=1,
        now=lambda: 1000.0,
        sleep=lambda _seconds: None,
    )
    assert "secondary rate limit" in outcome.unresolved["1"]
    assert limiter.paused_until("graphql", "token-a") == 1060.0


def test_rate_limit_error_body_with_reset_header_pauses_until_reset(redis):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": None, "errors": [{"message": "rate limit exceeded"}]},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1100"},
        )

    limiter = BucketLimiter(redis, max_concurrent=10)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetch_batch(
        DictAdapter(),
        ["1"],
        client=client,
        limiter=limiter,
        token_id="token-a",
        max_attempts=1,
        now=lambda: 1000.0,
        sleep=lambda _seconds: None,
    )
    assert limiter.paused_until("graphql", "token-a") == 1100.0
```

(The core test module already imports `BucketLimiter`? It does not — add `from limiter.buckets import BucketLimiter` and the `redis` fixture:)

```python
@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis()
```

- [ ] **Step 2: Run to verify failure**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_classifier.py tests/unit/test_graphql_batch_core.py -q
```
Expected: FAIL — no `rate_limit_wait`; unresolved says "graphql request failed"; bucket not paused.

- [ ] **Step 3: Implement the classifier extraction**

In `src/limiter/classifier.py`, add before `classify`:

```python
def rate_limit_wait(headers: Mapping[str, str], *, now: float) -> tuple[Action, float] | None:
    """Seconds to wait for a rate-limit response, from headers alone."""
    retry_after = _parse_retry_after(_header(headers, "retry-after"), now)
    if retry_after is not None and retry_after > 0:
        return Action.RETRY_AFTER, retry_after
    remaining = _header(headers, "x-ratelimit-remaining")
    reset = _parse_number(_header(headers, "x-ratelimit-reset"))
    if remaining is not None and str(remaining).strip() == "0" and reset is not None:
        return Action.WAIT_RESET, max(0.0, reset - now)
    return None
```

and rewrite the 403/429 branch to delegate:

```python
    if status in (403, 429):
        wait = rate_limit_wait(headers, now=now)
        if wait is not None:
            action, seconds = wait
            reason = "retry-after" if action is Action.RETRY_AFTER else "rate limit reset"
            return Decision(action, seconds, resource, reason)
        if attempt >= 5:
            return Decision(Action.FAIL_LOUD, None, resource, "retries exhausted")
        return Decision(Action.BACKOFF, _backoff_seconds(attempt, jitter), resource, "backoff")
```

- [ ] **Step 4: Implement the graphql_batch changes**

In `src/lib/graphql_batch.py`:

```python
from lib.gh_client import (
    PartialResultsError,
    RequestFailed,
    ThrottledError,
    request_with_retry,
    short_message,
)
from limiter.classifier import rate_limit_wait
```

New module constants:

```python
_DEFAULT_RATE_LIMIT_PAUSE = 60.0
_RATE_LIMIT_MARKERS = ("rate limit", "secondary rate", "abuse")
```

Helpers (place near `_is_transient`):

```python
def _is_rate_limit(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


def _pause_if_rate_limited(
    limiter: BucketLimiter | None,
    token_id: str | None,
    headers: Mapping[str, str],
    batch_errors: Sequence[str],
    *,
    now: Callable[[], float],
    jitter: Callable[[], float] | None,
) -> None:
    if limiter is None or token_id is None:
        return
    if not any(_is_rate_limit(message) for message in batch_errors):
        return
    wait = rate_limit_wait(headers, now=now())
    if wait is not None:
        seconds = wait[1]
    else:
        seconds = _DEFAULT_RATE_LIMIT_PAUSE + (jitter() if jitter is not None else 0.0)
    if seconds > 0:
        existing = limiter.paused_until("graphql", token_id)
        if existing is None or existing < now() + seconds:
            limiter.pause("graphql", token_id, seconds, now=now())
```

In `_post`: change the non-200 raise to `raise RequestFailed(int(response.status_code), short_message(response))`; just before returning, after `batch_errors` is built:

```python
    _pause_if_rate_limited(limiter, token_id, response.headers, batch_errors, now=now, jitter=jitter)
```

(With headers carrying `remaining: 0` + reset, `request_with_retry` already paused via `update_from_headers`; `rate_limit_wait` then returns the same seconds and the pause is idempotent. With silent headers, the 60 s default applies — this is the hot-retry fix. Requeued keys still go through `limiter.acquire`, so they block on the paused bucket rather than re-knocking; this is not Theme 3's pool-wide pause. The extend-only check mirrors Theme 3's `_pause_bucket`, so once both plans land a 60 s default can never shorten a longer controller pause on the shared `graphql` bucket.)

- [ ] **Step 5: Run to verify pass**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_classifier.py tests/unit/test_graphql_batch_core.py tests/integration/test_hydrate_batch.py -q
```
Expected: PASS — every pre-existing classifier assertion still holds after the extraction.

- [ ] **Step 6: Lint/types then commit**

```powershell
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m black --check src tests
.venv\Scripts\python.exe -m mypy
git add src/limiter/classifier.py src/lib/graphql_batch.py tests/unit/test_classifier.py tests/unit/test_graphql_batch_core.py
git commit -m "fix(graphql): keep GitHub error messages and pause the bucket on 200-body rate limits"
```

### Task 8: Docs sync and full verification

**Files:**
- Modify: `docs/environment.md:228-229`, `:234`, `:235`, `:247`; `src/serve/templates/settings.html:125-128`; `design/run-limits.md:11`, `:25`, `:34`, `:35`, `:48`, `:54`; `design/data-model.md:157`; `design/contracts/search-api.md:87`; append a dated entry to `docs/development-log.md`

**Interfaces:** none (documentation only).

- [ ] **Step 1: Update the living docs**

- `docs/environment.md:235`: hydration driver is `min(limiter_max_concurrent, 32)`; corpus preset 32; mention that Theme 3 will make the window dynamic inside that ceiling.
- `docs/environment.md:247`: head is `0014`; describe the three `audit_log` columns + `audit_run_idx`, and `graphql_batch_size` default 29 with the one-time 20→29 value alignment.
- `docs/environment.md:228-229`: corpus-build example column batch size `20` → `29` and concurrency `10` → `32`; `:234` "the default stays 20" → 29.
- `src/serve/templates/settings.html:125-128`: the static corpus-note copy ("batching on, 20 simultaneous requests") → "batching on, 29 repos per call, 32 simultaneous requests".
- `design/run-limits.md`: corpus preset "10 concurrent" → "32 concurrent" (:11); `graphql_batch_size` default 20 → 29 (:25, :34) and hydration batches "≤20 repos ≈ 1 point" → "≤29 repos ≈ 1 point" (:48); hydration workers `min(limiter_max_concurrent, 20)` → `min(limiter_max_concurrent, 32)` (:35) and `C/20` → `C/29` (:54).
- `design/data-model.md:157`: corpus preset batch 29, concurrency 32.
- `design/contracts/search-api.md:87`: hydration concurrency `min(limiter_max_concurrent, 32)`.
- `docs/development-log.md`: append a "Quota utilization and limiter correctness (2026-10-09)" entry: batch 29, concurrency ceiling 32, migration `0014`, `field_stats["points"]`, window re-anchor, message preservation, and the verification line from Step 2.

- [ ] **Step 2: Full verification**

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m black --check src tests
.venv\Scripts\python.exe -m mypy
```
Expected: full suite green with coverage `fail_under = 93`; lint, format, and types clean. The live golden-org tests are network-gated and may skip.

- [ ] **Step 3: Commit**

```powershell
git add docs/environment.md design/run-limits.md design/data-model.md design/contracts/search-api.md docs/development-log.md
git commit -m "docs: sync quota utilization and limiter correctness changes"
```

## Self-review notes

- Spec coverage: §4 quota ledger telemetry → Tasks 2-4 (`rateLimit` cost into `BatchStats`, `rl_used`/`run_id`/`phase` audit rows, `field_stats["points"]`); §5 batch-size sweet spot → Task 1 (29 = largest 1-point batch); §6 limiter quirks → Task 6 (window re-anchor, real limit, ratchet preserved); §7.3 telemetry prerequisite and the "pool at the ceiling, window gates submissions" seam → Tasks 2-5 (`HYDRATION_CONCURRENCY_CEILING` is the ceiling Theme 3 installs the pool at; no controller loops built here); §8 rec 1 → Task 1; rec 2 → Task 5; rec 5 → Tasks 2-4; rec 6 → Tasks 6-7. Recs 3, 4, 7 and the reserve floor are out of scope (Theme 1/3).
- Placeholder scan: no TBDs; every code step is concrete; migrations include up and down.
- Type consistency: `RateLimitInfo`/`rate_limit_from_payload` are defined in Task 2 and used by `PointsLedger` (Task 3) and `RepoDetailsAdapter` (Task 2); `audit.PointsLedger.add(phase, body)` / `.as_dict()` are fixed in Task 3 and consumed in Task 4; phase strings `"count"`, `"discovery"`, `"hydration"`, `"enrich"` match across tasks; `HYDRATION_CONCURRENCY_CEILING` is produced in Task 5 and referenced by Theme 3's plan.
- Known double-touches: `tests/unit/test_settings_spec.py` and `tests/contract/test_settings.py` corpus expectations change in Task 1 (batch 29) and Task 5 (concurrency 32); `tests/integration/test_runner.py` helper/assertions change in Task 4 and the corpus test in Task 5. Acceptable per the sample plan's convention; each edit is small and isolated.
- Cross-plan contract for Theme 3: consume `field_stats["points"]`, `BatchStats.points_cost/used/remaining`, and `audit_log(run_id, phase, rl_used)`; build the live window at `HYDRATION_CONCURRENCY_CEILING` by replacing the static `limit(concurrency)` submission gate in `fetch_batch` (the `min(settings.limiter_max_concurrent, HYDRATION_CONCURRENCY_CEILING)` value is only the starting window); own the reserve/pool-wide pause/kill switch; add no migrations.
- Feasibility concerns: `x-ratelimit-used` is not always present on GraphQL responses — `rl_used` stays nullable and `PointsLedger` relies primarily on the body `rateLimit.cost`, which now exists on hydration only because the query requests it; batch 29 narrows the 10-second timeout margin (§5.4), so live monitoring of 502/504 counts is required before any Theme 3 batch growth, with 25 as the documented fallback; unit tests cannot verify GitHub's live header behavior, only the plumbing.
