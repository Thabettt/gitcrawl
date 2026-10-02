# Run Resume After Crash Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After a process crash, runs stuck in `queued`/`running` are recovered (marked orphaned), and failed/orphaned runs can be re-executed from their stored filter spec via a console Resume action.

**Architecture:** Orphan recovery is a startup/CLI step (`recover_orphaned_runs`) because the executor queue is in-memory. Resume re-executes the **same run row**: it clears the run's in-DB artifacts (`run_items`, and `detection_evidence` for detect runs) and re-submits to the existing FIFO executor. The runner is rebuilt from `runs.filter_spec` (find) or the detect spec + frozen pack (detect). Idempotent upserts make the re-fetch safe.

**Tech Stack:** Python 3.12, FastAPI + Jinja2 + htmx, SQLAlchemy 2, pytest.

**Spec:** none — design captured here (relates to reliability workstream C). Depends on the agent-detection plan for `runs.kind`/`detection_evidence`; Task 3 is conditional on that landing.

**Out of scope (v2):** shard/page-level continuation for discovery. Shards have no `run_id` column, so true mid-run continuation needs a schema change; v1 re-fetches from the start (upserts dedupe, so no corruption).

## Global Constraints

- Python `>=3.12`; ruff + black clean; coverage floor 93.
- Same-run re-execution only: never mutate another run's rows; never lose `filter_spec`/`filter_hash` (needed for replay and diff).
- CSRF required on the POST; wrong state → local `400` with a hint, never a silent no-op.
- Detect runs must delete `detection_evidence` rows before re-running (evidence is per-run and would otherwise duplicate).
- Contract helpers copied from `tests/contract/test_pages.py` (`schema`, `clean`, `make_client`, `healthy_client`, `seed_run`).
- Run tests with: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest <path> -q`.

## File Structure

**Modify**
- `src/serve/executor.py` — `recover_orphaned_runs(engine)`, `reset_run_artifacts(engine, run_id)`
- `src/serve/__main__.py` — call recovery before serving
- `src/serve/app.py` — `POST /runs/{run_id}/resume` route
- `src/serve/pages.py` + `src/serve/templates/run_detail.html` — Resume button
- `docs/environment.md` — runbook line

**Create**
- `tests/unit/test_orphan_recovery.py`
- `tests/contract/test_resume.py`

---

### Task 1: Orphan recovery

**Files:**
- Modify: `src/serve/executor.py`
- Modify: `src/serve/__main__.py`
- Test: `tests/unit/test_orphan_recovery.py`

**Interfaces:**
- Produces (used by Tasks 2–3 and the CLI):
  - `recover_orphaned_runs(engine: Engine) -> int` — marks `queued`/`running` runs as `failed` with `error='orphaned: process restarted'` and `finished_at=now()`; returns the count.
- Consumes: `store.models.Runs`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_orphan_recovery.py`:

```python
from __future__ import annotations

from sqlalchemy import text

from serve.executor import recover_orphaned_runs


def seed_run_with_status(engine, status: str) -> int:
    with engine.begin() as connection:
        return int(
            connection.execute(
                text(
                    "INSERT INTO runs (filter_hash, filter_spec, api_version, status)"
                    " VALUES (gen_random_uuid()::text, '{}'::jsonb, '2022-11-28', :status)"
                    " RETURNING id"
                ),
                {"status": status},
            ).scalar_one()
        )


def test_recover_marks_queued_and_running_as_orphaned(clean_db):
    engine = clean_db()
    queued = seed_run_with_status(engine, "queued")
    running = seed_run_with_status(engine, "running")
    done = seed_run_with_status(engine, "done")
    failed = seed_run_with_status(engine, "failed")

    count = recover_orphaned_runs(engine)
    assert count == 2
    with engine.connect() as connection:
        rows = dict(
            connection.execute(
                text("SELECT id, status, error FROM runs WHERE id = ANY(:ids)"),
                {"ids": [queued, running, done, failed]},
            ).all()
        )
    assert rows[queued]["status"] == "failed"
    assert rows[running]["status"] == "failed"
    assert "orphaned" in rows[running]["error"]
    assert rows[done]["status"] == "done"  # untouched
    assert rows[failed]["status"] == "failed"  # untouched


def test_recover_is_idempotent(clean_db):
    engine = clean_db()
    seed_run_with_status(engine, "running")
    assert recover_orphaned_runs(engine) == 1
    assert recover_orphaned_runs(engine) == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_orphan_recovery.py -q`
Expected: FAIL (`ImportError: cannot import name 'recover_orphaned_runs'`).

- [ ] **Step 3: Implement**

Add to `src/serve/executor.py`:

```python
def recover_orphaned_runs(engine: Engine) -> int:
    with engine.begin() as connection:
        result = connection.execute(
            update(Runs)
            .where(Runs.status.in_(("queued", "running")))
            .values(
                status="failed",
                error="orphaned: process restarted",
                finished_at=func.now(),
            )
        )
    return int(result.rowcount or 0)
```

`_error_message` sanitization (from the triage plan, if landed) applies here: the fixed literal is already safe.

In `src/serve/__main__.py`, recover before serving:

```python
def main() -> None:
    import uvicorn

    url = os.environ.get("DATABASE_URL")
    if url:
        from sqlalchemy import create_engine

        from serve.executor import recover_orphaned_runs

        engine = create_engine(url)
        try:
            recovered = recover_orphaned_runs(engine)
            if recovered:
                print(f"recovered {recovered} orphaned run(s)")
        finally:
            engine.dispose()
    host = os.environ.get("GITCRAWL_HOST", "127.0.0.1")
    port = int(os.environ.get("GITCRAWL_PORT", "8000"))
    uvicorn.run("serve.app:app", host=host, port=port)
```

- [ ] **Step 4: Run tests**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_orphan_recovery.py tests/unit/test_executor_unit.py tests/integration/test_executor.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve/executor.py src/serve/__main__.py tests/unit/test_orphan_recovery.py
git commit -m "fix: recover runs orphaned by a process restart"
```

---

### Task 2: Reset artifacts + Resume route (find runs)

**Files:**
- Modify: `src/serve/executor.py`
- Modify: `src/serve/app.py`
- Test: `tests/contract/test_resume.py`

**Interfaces:**
- Produces:
  - `reset_run_artifacts(engine: Engine, run_id: int) -> None` — deletes `run_items` (and `detection_evidence` when that table is present) for the run; leaves `runs` row, `filter_spec`, `filter_hash`, and bundle dir untouched until the re-run overwrites them.
  - `POST /runs/{run_id}/resume` — guards, resets, re-submits, `303` to `/runs/{run_id}`.
- Consumes: `serve.executor.RunExecutor`, `serve.runner.make_runner`/`build_deps` (find path).

- [ ] **Step 1: Write the failing contract tests**

Create `tests/contract/test_resume.py`. Copy the helpers from `tests/contract/test_pages.py`, add a fake runner factory and:

```python
from sqlalchemy import text

from serve.runner import RunnerConfig


def fake_runner_factory(engine):
    from serve.executor import RunPayload, RunPayloadItem

    def runner(run_id: int, spec: dict):
        return RunPayload(
            total_count=1,
            fetched=1,
            items=[RunPayloadItem(repo_id=1, full_name="octo/hello", stargazers=10, virtuals={})],
        )

    return runner


def make_client_with_runner(engine, tmp_path, monkeypatch):
    client = healthy_client(engine, tmp_path, monkeypatch)
    # the route resolves runner_factory from create_app; pass it through make_client
    return client


def test_resume_failed_run_requeues_and_clears_items(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    with clean.begin() as connection:
        connection.execute(text("UPDATE runs SET status='failed' WHERE id=:id"), {"id": run_id})
        connection.execute(
            text("INSERT INTO run_items (run_id, repo_id, full_name) VALUES (:id, 2, 'octo/world')"),
            {"id": run_id},
        )
    client = make_client(clean, tmp_path, runner_factory=fake_runner_factory)
    token = csrf_token(client)
    response = client.post(
        f"/runs/{run_id}/resume", headers={"x-csrf-token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/runs/{run_id}"
    with clean.connect() as connection:
        row = connection.execute(
            text("SELECT status FROM runs WHERE id=:id"), {"id": run_id}
        ).one()
    assert row.status in {"queued", "running", "done"}


def test_resume_done_run_is_400(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    client = make_client(clean, tmp_path, runner_factory=fake_runner_factory)
    token = csrf_token(client)
    response = client.post(f"/runs/{run_id}/resume", headers={"x-csrf-token": token})
    assert response.status_code == 400
    assert "failed" in response.text


def test_resume_unknown_run_is_404(clean, tmp_path, monkeypatch):
    client = make_client(clean, tmp_path, runner_factory=fake_runner_factory)
    token = csrf_token(client)
    assert client.post("/runs/4242/resume", headers={"x-csrf-token": token}).status_code == 404


def test_resume_requires_csrf(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    client = make_client(clean, tmp_path, runner_factory=fake_runner_factory)
    assert client.post(f"/runs/{run_id}/resume").status_code == 403
```

Copy `make_client` and `csrf_token` from the quality/QA plan's helpers (or `tests/contract/test_pages.py`); extend `make_client`'s signature with `runner_factory=None` forwarded to `create_app`. `create_app` already accepts `runner_factory`; use it for the find path.

- [ ] **Step 2: Run to verify failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/contract/test_resume.py -q`
Expected: FAIL (404 route missing).

- [ ] **Step 3: Implement `reset_run_artifacts` + route**

`src/serve/executor.py`:

```python
def reset_run_artifacts(engine: Engine, run_id: int) -> None:
    with engine.begin() as connection:
        connection.execute(delete(RunItem).where(RunItem.run_id == run_id))
        try:
            connection.execute(
                text("DELETE FROM detection_evidence WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
        except Exception:
            pass  # detection migration not applied; find path only
```

`src/serve/app.py`, next to the replay route:

```python
    @application.post("/runs/{run_id}/resume")
    async def resume_run(request: Request, run_id: int):
        if not await validate_csrf(request):
            return HTMLResponse("CSRF", status_code=403)
        engine = engine_for()
        with engine.connect() as connection:
            row = connection.execute(select(Runs).where(Runs.id == run_id)).mappings().one_or_none()
        if row is None:
            return HTMLResponse("run not found", status_code=404)
        if row["status"] != "failed":
            return HTMLResponse("only failed runs can be resumed", status_code=400)
        reset_run_artifacts(engine, run_id)
        with engine.begin() as connection:
            connection.execute(
                update(Runs).where(Runs.id == run_id).values(status="queued", error=None)
            )
        executor_for().submit(run_id, runner_for())
        return RedirectResponse(f"/runs/{run_id}", status_code=303)
```

`validate_csrf` is the pages module function; import it or use the same object `register_pages` uses. `runner_for()` is the existing factory inside `create_app`.

- [ ] **Step 4: Run tests**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/contract/test_resume.py tests/contract/test_console_pages.py tests/contract/test_run_detail.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve/executor.py src/serve/app.py tests/contract/test_resume.py
git commit -m "feat: resume failed find runs from the stored filter spec"
```

---

### Task 3: Detect-run resume + Resume button + runbook

**Files:**
- Modify: `src/serve/app.py` (resume route: detect branch)
- Modify: `src/serve/pages.py`, `src/serve/templates/run_detail.html`
- Modify: `docs/environment.md`
- Test: `tests/contract/test_resume.py`, `tests/contract/test_detect_pages.py` (from the detection plan)

**Interfaces:**
- Consumes (detection plan): `serve.detect_spec.parse_detect_spec`, `detect.packs.load_frozen`, `src/detect/orchestrator.run_detection`, and the `detect_runner_factory` injection on `create_app`.
- Produces: resume parity for `kind='detect'` runs; Resume button visible only on failed runs.

- [ ] **Step 1: Write failing tests**

Add to `tests/contract/test_resume.py` (skip if the detection migration is absent):

```python
@pytest.mark.skipif(not _detection_landed(clean_engine), reason="detection plan not executed yet")
def test_resume_detect_run_clears_evidence_then_reruns(...):
    # seed source find run + detect run (kind='detect', status='failed') + one trace_packs row
    # + one detection_evidence row and one run_items row for the detect run
    # POST /runs/{detect_id}/resume with the injected detect_runner_factory
    # assert evidence rows for the detect run == 0 before the fake runner runs, then == 1 after
    ...


def test_run_detail_shows_resume_only_for_failed(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    client = make_client(clean, tmp_path, runner_factory=fake_runner_factory)
    assert "Resume" not in client.get(f"/runs/{run_id}").text
    with clean.begin() as connection:
        connection.execute(text("UPDATE runs SET status='failed', error='boom' WHERE id=:id"), {"id": run_id})
    assert "Resume" in client.get(f"/runs/{run_id}").text
```

`_detection_landed(engine)` checks `to_regclass('detection_evidence')` via SQL.

- [ ] **Step 2: Implement the detect branch**

In the resume route, after the status guard, branch on `row["kind"]`:

```python
        if row["kind"] == "detect":
            from serve.detect_spec import parse_detect_spec
            from detect.packs import load_frozen
            from detect.orchestrator import run_detection
            from serve.runner import build_deps

            spec = parse_detect_spec(row["filter_spec"])
            pack = load_frozen(engine, spec.pack_version)

            def detect_runner(_run_id: int, _spec_doc: dict):
                return run_detection(build_deps(engine), run_id, spec, pack)

            submit_runner = detect_runner
        else:
            submit_runner = runner_for()
        reset_run_artifacts(engine, run_id)
        ...
        executor_for().submit(run_id, submit_runner)
```

For tests, prefer `create_app`'s `detect_runner_factory` when present so the fake can be injected; fall back to the closure above in production.

`run_detail.html` (find and detect detail templates): add near the action row:

```html
{% if summary.status == 'failed' %}
<form method="post" action="/runs/{{ summary.id }}/resume">
  <input type="hidden" name="csrf" value="{{ csrf_token }}">
  <button type="submit">Resume</button>
</form>
{% endif %}
```

Use whichever CSRF field name `validate_csrf` already accepts (`csrf`) and the existing template context variable for the token (mirror the replay form).

- [ ] **Step 3: Run tests**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/contract/test_resume.py tests/contract/test_run_detail.py tests/contract/test_detect_pages.py -q`
Expected: PASS (skips allowed only when the detection plan has not been executed).

- [ ] **Step 4: Runbook + OpenAPI re-pin**

Append to `docs/environment.md`:

```markdown
## After a crash
1. Restart the app (`python -m serve`) — queued/running runs are marked `failed: orphaned`.
2. Open the run and click **Resume** (or `POST /runs/{id}/resume` from the console).
3. Resume re-fetches from the start; upserts make it safe. Detection runs clear their evidence first.
```

Re-pin `tests/golden/snapshots/openapi.sha256` for the new route and note it in `docs/development-log.md`.

- [ ] **Step 5: Full suite + commit**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest --cov=src --cov-report=term-missing -q`
Expected: PASS, coverage `>= 93%`.

```bash
git add src/serve/app.py src/serve/pages.py src/serve/templates/run_detail.html docs/environment.md docs/development-log.md tests/contract/test_resume.py tests/golden/snapshots/openapi.sha256
git commit -m "feat: resume detect runs, console button, and crash runbook"
```

---

## Self-Review Checklist

- [ ] Orphan recovery runs before serving and is idempotent (Task 1).
- [ ] Resume keeps `filter_spec`/`filter_hash` intact so replay/diff still work (Task 2).
- [ ] Detect resume deletes `detection_evidence` for the run before re-running (Task 3).
- [ ] Guards: unknown → 404; non-failed → 400 with hint; no CSRF → 403 (Task 2).
- [ ] Shard-level continuation explicitly out of scope (documented); upserts make re-fetch safe.
- [ ] OpenAPI golden re-pinned; runbook recorded.
