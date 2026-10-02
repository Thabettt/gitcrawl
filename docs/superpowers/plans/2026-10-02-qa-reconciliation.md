# Run Quality Reconciliation (QA) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every run's trustworthiness visible at a glance: a computed quality report (count parity, duplicates, missing fields, incompleteness, bundle consistency, field coverage) rendered as a panel on run detail and exposed as JSON.

**Architecture:** A pure read-side module `src/serve/quality.py` computes checks from `runs`, `run_items`, and the run's `bundle.json`; routes render it as an htmx partial plus JSON; nothing about the existing run pipeline changes.

**Tech Stack:** Python 3.12, FastAPI + Jinja2 + htmx, SQLAlchemy 2, pytest.

**Spec:** none — design captured in this plan (relates to FR-010 audit/SLO and the operator console's trust requirements).

**Depends on:** current `001-gitcrawl` head. No migrations, no schema changes.

## Global Constraints

- Python `>=3.12`; ruff (`E,F,I,UP,B`) + black clean; coverage floor `fail_under = 93`.
- DB tests use the `clean_db` factory in `tests/conftest.py` (never hand-roll TRUNCATE/seed); contract tests copy `make_client`/`healthy_client`/`seed_run` from `tests/contract/test_pages.py`.
- Read-only feature: it must never mutate runs, run_items, or bundles.
- Never surface upstream bodies in errors; quality details are local computed strings only.
- Run tests with: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest <path> -q`.

## File Structure

**Create**

- `src/serve/quality.py` — `Check`, `QualityReport`, `run_quality(engine, run_id, *, runs_root="runs")`.
- `src/serve/templates/partials/quality.html` — panel fragment.
- `tests/unit/test_quality.py` — check logic against seeded runs.
- `tests/contract/test_quality_pages.py` — route + panel rendering.

**Modify**

- `src/serve/app.py` — register `GET /runs/{run_id}/quality` and `GET /partials/runs/{run_id}/quality`.
- `src/serve/pages.py` — include the quality partial in the run-detail context (find runs only; detect runs reuse the same module later).
- `src/serve/templates/run_detail.html` — render the panel.

---

### Task 1: Quality report module

**Files:**
- Create: `src/serve/quality.py`
- Test: `tests/unit/test_quality.py`

**Interfaces:**
- Consumes: `store.models.Runs`, `store.models.RunItem`, the bundle at `runs/{filter_hash}/{run_id}/bundle.json`.
- Produces (used by Tasks 2–3):
  - `@dataclass(frozen=True) class Check: name: str; status: str; detail: str; value: object | None = None` (`status in {"ok","warn","fail"}`)
  - `@dataclass(frozen=True) class QualityReport: run_id: int; status: str; checks: tuple[Check, ...]; generated_at: datetime`
  - `run_quality(engine: Engine, run_id: int, *, runs_root: str = "runs") -> QualityReport`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_quality.py`:

```python
from __future__ import annotations

import json

import pytest
from sqlalchemy import insert, text

from serve.executor import RunPayload, RunPayloadItem, create_run, execute_run
from serve.quality import run_quality
from store.models import RunItem, Runs

FILTER = {"q": "language:rust"}


def seed_run(clean_db, tmp_path, *, item_count=3, inserted=None, status="done"):
    engine = clean_db(
        owners=[
            {"id": 1, "login": "octo", "type": "User"},
            {"id": 2, "login": "octo2", "type": "User"},
            {"id": 3, "login": "octo3", "type": "User"},
        ],
        repos=[
            {
                "id": i,
                "node_id": f"n{i}",
                "full_name": f"octo/repo{i}",
                "owner_id": i,
                "name": f"repo{i}",
                "visibility": "public",
            }
            for i in (1, 2, 3)
        ],
    )
    run_id = create_run(engine, FILTER, api_version="2022-11-28")
    payload = RunPayload(
        total_count=item_count,
        fetched=item_count,
        items=[
            RunPayloadItem(
                repo_id=i + 1,
                full_name=f"octo/repo{i + 1}",
                stargazers=10 + i,
                language="Rust" if i < item_count - 1 else None,
                license_spdx="MIT" if i < item_count - 1 else None,
                country_iso="DE" if i == 0 else None,
                virtuals={},
            )
            for i in range(item_count)
        ],
    )
    execute_run(engine, run_id, runner=lambda _rid, _spec: payload, runs_root=str(tmp_path))
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE runs SET status = :status, inserted = :inserted WHERE id = :id"
            ),
            {
                "status": status,
                "inserted": inserted if inserted is not None else item_count,
                "id": run_id,
            },
        )
    return engine, run_id


def test_clean_run_is_ok(clean_db, tmp_path):
    engine, run_id = seed_run(clean_db, tmp_path)
    report = run_quality(engine, run_id, runs_root=str(tmp_path))
    assert report.status == "ok"
    assert {check.name for check in report.checks} >= {
        "count_parity",
        "duplicate_full_names",
        "missing_language",
        "missing_license_spdx",
        "missing_country_iso",
        "bundle",
    }


def test_count_mismatch_warns(clean_db, tmp_path):
    engine, run_id = seed_run(clean_db, tmp_path, inserted=99)
    report = run_quality(engine, run_id, runs_root=str(tmp_path))
    check = next(c for c in report.checks if c.name == "count_parity")
    assert check.status == "warn"
    assert report.status in {"warn", "fail"}


def test_duplicate_full_name_fails(clean_db, tmp_path):
    engine, run_id = seed_run(clean_db, tmp_path, item_count=2)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE run_items SET full_name = 'octo/repo1' WHERE run_id = :id"),
            {"id": run_id},
        )
    report = run_quality(engine, run_id, runs_root=str(tmp_path))
    check = next(c for c in report.checks if c.name == "duplicate_full_names")
    assert check.status == "fail"
    assert report.status == "fail"


def test_missing_bundle_warns(clean_db, tmp_path):
    engine, run_id = seed_run(clean_db, tmp_path)
    for path in (tmp_path / "runs").rglob("bundle.json"):
        path.unlink()
    report = run_quality(engine, run_id, runs_root=str(tmp_path))
    check = next(c for c in report.checks if c.name == "bundle")
    assert check.status == "warn"


def test_partial_status_is_flagged(clean_db, tmp_path):
    engine, run_id = seed_run(clean_db, tmp_path, status="partial")
    report = run_quality(engine, run_id, runs_root=str(tmp_path))
    check = next(c for c in report.checks if c.name == "incomplete")
    assert check.status == "warn"


def test_unknown_run_raises_keyerror(clean_db, tmp_path):
    engine = clean_db()
    with pytest.raises(KeyError):
        run_quality(engine, 4242, runs_root=str(tmp_path))
```

Note: `clean_db` is a **factory** (`tests/conftest.py:100`): `engine = clean_db(owners=[...], repos=[...])`, returning the session engine after truncation + seeding. `execute_run` writes `corpus.csv` + `bundle.json` under `runs_root=tmp_path`.

- [ ] **Step 2: Run to verify failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_quality.py -q`
Expected: FAIL (`ModuleNotFoundError: serve.quality`).

- [ ] **Step 3: Implement `src/serve/quality.py`**

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from store.models import RunItem, Runs

_SPARSE_EXPECTED = {"country_iso"}
_WARN_NULL_RATIO = 0.2


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    value: object | None = None


@dataclass(frozen=True)
class QualityReport:
    run_id: int
    status: str
    checks: tuple[Check, ...]
    generated_at: datetime


def _overall(checks: list[Check]) -> str:
    if any(check.status == "fail" for check in checks):
        return "fail"
    if any(check.status == "warn" for check in checks):
        return "warn"
    return "ok"


def _bundle_path(runs_root: str, filter_hash: str, run_id: int) -> Path:
    return Path(runs_root) / filter_hash / str(run_id) / "bundle.json"


def _load_bundle(runs_root: str, filter_hash: str, run_id: int) -> dict | None:
    path = _bundle_path(runs_root, filter_hash, run_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def run_quality(engine: Engine, run_id: int, *, runs_root: str = "runs") -> QualityReport:
    with engine.connect() as connection:
        run = connection.execute(select(Runs).where(Runs.id == run_id)).mappings().one_or_none()
        if run is None:
            raise KeyError(run_id)
        item_count = connection.scalar(
            select(func.count()).select_from(RunItem).where(RunItem.run_id == run_id)
        )
        duplicate_names = connection.scalar(
            select(func.count()).select_from(
                select(RunItem.full_name)
                .where(RunItem.run_id == run_id)
                .group_by(RunItem.full_name)
                .having(func.count(func.distinct(RunItem.repo_id)) > 1)
                .subquery()
            )
        )
        null_language = connection.scalar(
            select(func.count()).select_from(RunItem).where(
                RunItem.run_id == run_id, RunItem.language.is_(None)
            )
        )
        null_license = connection.scalar(
            select(func.count()).select_from(RunItem).where(
                RunItem.run_id == run_id, RunItem.license_spdx.is_(None)
            )
        )
        null_country = connection.scalar(
            select(func.count()).select_from(RunItem).where(
                RunItem.run_id == run_id, RunItem.country_iso.is_(None)
            )
        )
    checks: list[Check] = []
    item_count = int(item_count or 0)
    expected = int(run["inserted"] or 0)
    parity_status = "ok" if item_count == expected else "warn"
    checks.append(
        Check(
            "count_parity",
            parity_status,
            f"run_items={item_count} runs.inserted={expected} fetched={run['fetched']}",
            value=item_count - expected,
        )
    )
    duplicates = int(duplicate_names or 0)
    checks.append(
        Check(
            "duplicate_full_names",
            "fail" if duplicates else "ok",
            f"{duplicates} full_name value(s) mapped to multiple repo ids",
            value=duplicates,
        )
    )
    for name, nulls in (
        ("language", null_language),
        ("license_spdx", null_license),
        ("country_iso", null_country),
    ):
        ratio = (int(nulls or 0) / item_count) if item_count else 0.0
        limit = 0.9 if name in _SPARSE_EXPECTED else _WARN_NULL_RATIO
        checks.append(
            Check(
                f"missing_{name}",
                "warn" if ratio > limit else "ok",
                f"{ratio:.0%} null ({nulls}/{item_count})",
                value=ratio,
            )
        )
    incomplete = run["incomplete_shards"] or 0
    checks.append(
        Check(
            "incomplete",
            "warn" if run["status"] == "partial" or incomplete else "ok",
            f"status={run['status']} incomplete_shards={incomplete}",
        )
    )
    bundle = _load_bundle(runs_root, str(run["filter_hash"]), run_id)
    if bundle is None:
        checks.append(Check("bundle", "warn", "bundle.json missing or unreadable"))
    else:
        bundle_items = bundle.get("items") or []
        consistent = len(bundle_items) == item_count
        checks.append(
            Check(
                "bundle",
                "ok" if consistent else "warn",
                f"bundle items={len(bundle_items)} run_items={item_count}",
            )
        )
        field_stats = bundle.get("field_stats") or {}
        empty_fields = [name for name, stats in field_stats.items() if not stats]
        checks.append(
            Check(
                "field_coverage",
                "warn" if empty_fields else "ok",
                f"fields with no data: {', '.join(sorted(empty_fields))}" if empty_fields else "all enriched fields have data",
                value=empty_fields,
            )
        )
    return QualityReport(
        run_id=run_id,
        status=_overall(checks),
        checks=tuple(checks),
        generated_at=datetime.now(UTC),
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_quality.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/serve/quality.py tests/unit/test_quality.py
git commit -m "feat: computed run quality report"
```

---

### Task 2: Quality routes and run-detail panel

**Files:**
- Modify: `src/serve/app.py` (register next to the other run routes; `register_pages(...)` call is at ~:712)
- Modify: `src/serve/pages.py` (`_run_detail_summary` ~:377 area; renderer `_run_detail_row` ~:327)
- Modify: `src/serve/templates/run_detail.html`
- Create: `src/serve/templates/partials/quality.html`
- Test: `tests/contract/test_quality_pages.py`

**Interfaces:**
- Consumes: `serve.quality.run_quality`.
- Produces:
  - `GET /runs/{run_id}/quality` → JSON `{run_id, status, generated_at, checks:[{name,status,detail,value}]}` (404 unknown run)
  - `GET /partials/runs/{run_id}/quality` → HTML fragment for htmx
  - Run detail renders the panel for `kind='find'` runs.

- [ ] **Step 1: Write the failing contract tests**

Create `tests/contract/test_quality_pages.py`. Copy the `schema`, `clean`, `make_client`, `healthy_client`, `seed_run` helpers from `tests/contract/test_pages.py` verbatim, then add:

```python
import json


def test_quality_json_route_reports_status(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get(f"/runs/{run_id}/quality")
    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == run_id
    assert payload["status"] in {"ok", "warn", "fail"}
    assert any(check["name"] == "count_parity" for check in payload["checks"])


def test_quality_json_unknown_run_is_404(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    assert client.get("/runs/9999/quality").status_code == 404


def test_run_detail_renders_quality_panel(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get(f"/runs/{run_id}")
    assert "Run quality" in response.text
    assert f'hx-get="/partials/runs/{run_id}/quality"' in response.text


def test_quality_partial_renders_checks(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get(f"/partials/runs/{run_id}/quality")
    assert response.status_code == 200
    assert "count_parity" in response.text
    assert "bundle" in response.text
```

- [ ] **Step 2: Run to verify failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/contract/test_quality_pages.py -q`
Expected: FAIL (404s / missing strings).

- [ ] **Step 3: Implement the routes**

In `src/serve/app.py`, inside `create_app` add next to the diff route (~:674):

```python
    @application.get("/runs/{run_id}/quality")
    def run_quality_json(run_id: int):
        from serve.quality import run_quality

        try:
            report = run_quality(engine_for(), run_id, runs_root=runs_root)
        except KeyError:
            return JSONResponse(status_code=404, content={"error": "run_not_found", "run_id": run_id})
        return {
            "run_id": report.run_id,
            "status": report.status,
            "generated_at": report.generated_at.isoformat(),
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail, "value": c.value}
                for c in report.checks
            ],
        }

    @application.get("/partials/runs/{run_id}/quality", response_class=HTMLResponse)
    def run_quality_partial(request: Request, run_id: int):
        from serve.pages import quality_panel

        try:
            report = run_quality(engine_for(), run_id, runs_root=runs_root)
        except KeyError:
            return HTMLResponse("run not found", status_code=404)
        return quality_panel(request, report)
```

Add `quality_panel` to `src/serve/pages.py`:

```python
def quality_panel(request: Request, report) -> HTMLResponse:
    return _templates.TemplateResponse(
        request,
        "partials/quality.html",
        {"report": report, "badge": {"ok": "good", "warn": "warn", "fail": "bad"}[report.status]},
    )
```

`src/serve/templates/partials/quality.html`:

```html
<section id="quality-panel" class="panel quality-{{ badge }}">
  <h2>Run quality: {{ badge }}</h2>
  <ul>
    {% for check in report.checks %}
    <li class="check-{{ check.status }}"><strong>{{ check.name }}</strong>: {{ check.detail }}</li>
    {% endfor %}
  </ul>
</section>
```

In `run_detail.html`, after the flags list:

```html
<section hx-get="/partials/runs/{{ summary.id }}/quality" hx-trigger="load" hx-swap="outerHTML">
  <p>Loading run quality…</p>
</section>
```

- [ ] **Step 4: Run contract + page tests**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/contract/test_quality_pages.py tests/contract/test_run_detail.py -q`
Expected: PASS.

- [ ] **Step 5: Full suite + coverage + OpenAPI re-pin (two new routes change the schema hash)**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest --cov=src --cov-report=term-missing -q`
Expected: PASS, coverage `>= 93%`. If `tests/golden/test_openapi_pin.py` fails, update `tests/golden/snapshots/openapi.sha256` to the printed hash and note it in `docs/development-log.md`.

- [ ] **Step 6: Commit**

```bash
git add src/serve/app.py src/serve/pages.py src/serve/templates/run_detail.html src/serve/templates/partials/quality.html tests/contract/test_quality_pages.py tests/golden/snapshots/openapi.sha256 docs/development-log.md
git commit -m "feat: run quality panel and JSON route"
```

---

## Self-Review Checklist

- [ ] Count parity, duplicates, missing fields, incompleteness, bundle consistency, field coverage all computed and tested.
- [ ] Panel is read-only; no writes to runs/run_items/bundles (assert by inspection; tests use real execute_run output).
- [ ] Unknown run → 404 on both routes; no upstream bodies leaked.
- [ ] OpenAPI golden re-pinned after new routes.
- [ ] Explorers can answer "is this run trustworthy" from the run detail page in one screen.
