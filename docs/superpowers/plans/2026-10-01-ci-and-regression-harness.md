# CI and Regression Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Phase 0 quality gates real: a two-OS CI workflow backed by a real PostgreSQL 17 and Redis 7, a database guard that fails (never skips) when `TEST_DATABASE_URL` is missing in CI, a golden endpoint harness with an OpenAPI hash pin, a measured coverage floor with a ratchet policy, clean `black` formatting, and an advisory `pip-audit` job — with zero runtime behavior change.

**Architecture:** Every deliverable is CI, tests, docs, or config (INV-4). Task 1 makes `black --check` green before the gate exists; Task 2 adds the fail-not-skip guard plus the two-OS workflow so every later task is gated; Task 3 seeds a fixed corpus into the migrated real database and snapshots every registered GET route; Task 4 pins `app.openapi()`; Task 5 adds `pytest-cov`, records the measured baseline, and enforces a floor; Task 6 adds an advisory dependency audit. `ubuntu-latest` uses `services:` containers; `windows-latest` cannot run Linux service containers (GitHub docs: service containers require a Linux runner), so it starts the image's preinstalled PostgreSQL 17 and a Redis 7 protocol-compatible Memurai server.

**Tech Stack:** GitHub Actions, pytest 9.1.1 + FastAPI/Starlette `TestClient`, PostgreSQL 17, Redis 7 (Memurai 4.1.8 on Windows), `pytest-cov` 7.1.0, `black` 26.5.1, `ruff` 0.16.9, `pip-audit` 2.10.1.

**Spec:** `docs/superpowers/specs/2026-10-01-quality-hardening-design.md` (Phase 0, §4; freeze contract §2; acceptance §4 and §8)

## Global Constraints

- INV-1 — Identical normal behavior: every observable output (HTTP status, body, headers except additive ones, files, DB rows, ordering, counts) for normal use is identical to today.
- INV-2 — Additive-only UI: no existing control moves, disappears, gains a required step, changes meaning, or changes its result.
- INV-3 — Allowed behavior changes only for cross-site requests, requests that hang beyond a deadline, oversized bodies, infrastructure-outage visibility, and the single approved run-summary counter fix (E1) after sign-off.
- INV-4 — No runtime surface: CI, formatting, typing, coverage, fixture centralization, annotation, and documentation work change no runtime behavior.
- INV-5 — Any unavoidable deviation is recorded in the spec's §11 exception register with an explicit approval checkbox before it ships.
- Python 3.12; `ruff` line length 100 is the linter only; `black` line length 100 is the single formatter; `ruff format` is never run (`pyproject.toml:10-21`).
- `src/skeleton.py` stays excluded from black and ruff via `pyproject.toml:13,21`; it is not deleted.
- Integration tests require `TEST_DATABASE_URL` whose database name ends in `_test` (`tests/conftest.py:19-26`); CI sets it, local runs may skip.
- `GITCRAWL_REQUIRE_TEST_DB=1` (set only by CI) must make a missing `TEST_DATABASE_URL` fail, never skip.
- Golden JSON bodies are compared byte-exact; HTML is compared after deterministic normalization; snapshot updates happen only through `UPDATE_GOLDEN=1` plus an explicit diff review.
- One commit per task; commit only the files named in that task's `Files:` block.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `.github/workflows/ci.yml` | Create | Two-OS test jobs (services on Linux, native PG17/Memurai on Windows), lint/format/test steps, missing-URL fail proof, advisory `pip-audit` job |
| `tests/conftest.py` | Modify | Fail-not-skip database guard driven by `GITCRAWL_REQUIRE_TEST_DB`; local skip behavior preserved |
| `tests/unit/test_database_guard.py` | Create | End-to-end proof that a required-but-missing `TEST_DATABASE_URL` produces a non-zero pytest exit with no skip |
| `tests/golden/conftest.py` | Create | Fixed-corpus seed SQL, deterministic golden app factory, HTML normalizer, GET route enumeration, session capture fixture |
| `tests/golden/test_golden_endpoints.py` | Create | Snapshot comparison and stale-snapshot detection |
| `tests/golden/test_openapi_pin.py` | Create | SHA-256 pin of canonical `create_app().openapi()` |
| `tests/golden/snapshots/openapi.sha256` | Create | Pinned OpenAPI digest |
| `tests/golden/snapshots/*.json` | Create (generated) | One snapshot per GET route case (23 cases) |
| `requirements-dev.txt` | Modify | Add `pytest-cov==7.1.0` and `pip-audit==2.10.1` pins |
| `pyproject.toml` | Modify | `[tool.coverage.run]` / `[tool.coverage.report]` with `fail_under = 93` |
| `docs/development-log.md` | Modify | Coverage baseline, ratchet policy, golden/CI/OpenAPI inventory |
| `src/enrich/segment_executor.py`, `src/serve/executor.py`, `src/serve/runs.py`, `src/serve/pages.py`, `tests/unit/test_audit.py`, `tests/unit/test_classifier.py`, `tests/contract/test_runs_export.py` | Modify (reformat only) | Make `black --check src tests` clean |

No source behavior changes: the only `src/` edit is black reformatting; everything else is CI, tests, docs, or config.

---

### Task 1: Black formatting authority — clean the drift

**Files:**
- Modify (reformat only): `src/enrich/segment_executor.py`, `src/serve/executor.py`, `src/serve/runs.py`, `src/serve/pages.py`, `tests/unit/test_audit.py`, `tests/unit/test_classifier.py`, `tests/contract/test_runs_export.py`
- Test: `black --check src tests` and `ruff check src tests` (commands, no new test file)
- Config: no change — `pyproject.toml:18-21` already configures black (line length 100, skeleton excluded)

**Interfaces:**
- Consumes: `pyproject.toml` black config; `black==26.5.1` from `requirements-dev.txt:2`.
- Produces: a black-clean `src` and `tests` tree; `ruff` remains the linter and `ruff format` is never invoked.

Current measured drift (2026-10-01, Windows, Python 3.12.10): 7 files would be reformatted, 78 unchanged. The spec's audit note says "10 files"; the measured set is the 7 listed above.

- [ ] **Step 1: Confirm the failing format check**

Run: `black --check src tests`
Expected: exit code 1, output listing exactly the 7 files above, ending `7 files would be reformatted, 78 files would be left unchanged.`

- [ ] **Step 2: Reformat**

Run: `black src tests`
Expected: `7 files reformatted, 78 files left unchanged.`

- [ ] **Step 3: Verify the check is now clean**

Run: `black --check src tests`
Expected: `All done!` / `85 files would be left unchanged.` with exit code 0.

- [ ] **Step 4: Verify the linter and the suite are unaffected**

Run: `ruff check src tests`
Expected: `All checks passed!`

Run: `pytest -q`
Expected: PASS (no failures; live GitHub modules skip when `GITHUB_TOKEN` is unset). Formatting changes no AST behavior.

- [ ] **Step 5: Commit**

```bash
git add src/enrich/segment_executor.py src/serve/executor.py src/serve/runs.py src/serve/pages.py tests/unit/test_audit.py tests/unit/test_classifier.py tests/contract/test_runs_export.py
git commit -m "style: apply black formatting to drifted files"
```

---

### Task 2: Fail-not-skip database guard + two-OS CI workflow

**Files:**
- Modify: `tests/conftest.py:29-41`
- Create: `tests/unit/test_database_guard.py`
- Create: `.github/workflows/ci.yml`
- Test: `tests/unit/test_database_guard.py`

**Interfaces:**
- Consumes: existing session fixtures `test_database_url`, `alembic_config`, `alembic_engine` (`tests/conftest.py:36-56`); `pytest.fail`/`pytest.skip` semantics.
- Produces:
  - `tests/conftest.REQUIRE_TEST_DB_ENV = "GITCRAWL_REQUIRE_TEST_DB"`
  - `tests/conftest.MissingTestDatabase(RuntimeError)`
  - `tests/conftest.require_test_database() -> bool`
  - `tests/conftest.resolve_test_database_url() -> str | None` (raises `MissingTestDatabase` when required and absent)
  - `.github/workflows/ci.yml` jobs `tests-linux`, `tests-windows`, `audit` (audit added in Task 6; this task creates the file without it).

- [ ] **Step 1: Write the failing guard test**

Create `tests/unit/test_database_guard.py`:

```python
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = "tests/contract/test_pages.py::test_dashboard_renders_empty_state_and_key_elements"


def test_missing_test_database_url_fails_when_required():
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"TEST_DATABASE_URL", "DATABASE_URL"}
    }
    env["GITCRAWL_REQUIRE_TEST_DB"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", TARGET],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "TEST_DATABASE_URL" in result.stdout
    assert "skipped" not in result.stdout.lower()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/unit/test_database_guard.py -v`
Expected: FAIL — today the fixture calls `pytest.skip("TEST_DATABASE_URL is not set")` (`tests/conftest.py:40`), the nested run exits 0, and the `returncode != 0` assertion fails.

- [ ] **Step 3: Implement the guard**

In `tests/conftest.py`, replace lines 29-41 (the current `resolve_test_database_url` and `test_database_url` fixture) with:

```python
REQUIRE_TEST_DB_ENV = "GITCRAWL_REQUIRE_TEST_DB"


class MissingTestDatabase(RuntimeError):
    pass


def require_test_database() -> bool:
    return os.environ.get(REQUIRE_TEST_DB_ENV) == "1"


def resolve_test_database_url() -> str | None:
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        if require_test_database():
            raise MissingTestDatabase(
                "TEST_DATABASE_URL is required when GITCRAWL_REQUIRE_TEST_DB=1; "
                "refusing to skip database tests"
            )
        return None
    return assert_test_database(url)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    try:
        url = resolve_test_database_url()
    except MissingTestDatabase as exc:
        pytest.fail(str(exc), pytrace=False)
    if url is None:
        pytest.skip("TEST_DATABASE_URL is not set")
    return url
```

- [ ] **Step 4: Run the guard test and the suite**

Run: `pytest tests/unit/test_database_guard.py -v`
Expected: PASS — the nested run errors with `TEST_DATABASE_URL is required when GITCRAWL_REQUIRE_TEST_DB=1; refusing to skip database tests` and exits non-zero.

Run: `pytest -q`
Expected: PASS with `TEST_DATABASE_URL` set (no behavior change for normal local runs).

- [ ] **Step 5: Create the CI workflow**

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

env:
  PYTHON_VERSION: "3.12"
  TEST_DATABASE_URL: postgresql+psycopg://gitcrawl:gitcrawl@localhost:5432/gitcrawl_test
  GITCRAWL_REQUIRE_TEST_DB: "1"

jobs:
  tests-linux:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:17
        env:
          POSTGRES_USER: gitcrawl
          POSTGRES_PASSWORD: gitcrawl
          POSTGRES_DB: gitcrawl_test
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U gitcrawl -d gitcrawl_test"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
      redis:
        image: redis:7
        ports:
          - 6379:6379
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ env.PYTHON_VERSION }}
          cache: pip
      - name: Install dependencies
        run: pip install -r requirements.txt -r requirements-dev.txt
      - name: Lint
        run: ruff check src tests
      - name: Format check
        run: black --check src tests
      - name: Prove missing TEST_DATABASE_URL fails
        shell: bash
        env:
          TEST_DATABASE_URL: ""
          DATABASE_URL: ""
        run: |
          if pytest -q tests/contract/test_pages.py::test_dashboard_renders_empty_state_and_key_elements; then
            echo "::error::pytest skipped instead of failing without TEST_DATABASE_URL"
            exit 1
          fi
      - name: Tests
        run: pytest -q -rs

  tests-windows:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - name: Start PostgreSQL 17
        shell: pwsh
        run: |
          Set-Service postgresql-x64-17 -StartupType Automatic
          Start-Service postgresql-x64-17
          for ($i = 0; $i -lt 30; $i++) {
            & "C:\Program Files\PostgreSQL\17\bin\pg_isready.exe" -h localhost -p 5432 -q
            if ($LASTEXITCODE -eq 0) { break }
            Start-Sleep -Seconds 1
          }
          if ($LASTEXITCODE -ne 0) { throw "PostgreSQL did not accept connections" }
          $env:PGPASSWORD = "root"
          $psql = "C:\Program Files\PostgreSQL\17\bin\psql.exe"
          & $psql -U postgres -h localhost -c "CREATE ROLE gitcrawl LOGIN PASSWORD 'gitcrawl'"
          & $psql -U postgres -h localhost -c "CREATE DATABASE gitcrawl_test OWNER gitcrawl"
      - name: Start Redis 7-compatible server (Memurai)
        shell: pwsh
        run: |
          choco install memurai-developer --version=4.1.8 --no-progress -y
          Start-Service -Name Memurai -ErrorAction SilentlyContinue
          & "C:\Program Files\Memurai\memurai-cli.exe" ping
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ env.PYTHON_VERSION }}
          cache: pip
      - name: Install dependencies
        shell: pwsh
        run: pip install -r requirements.txt -r requirements-dev.txt
      - name: Lint
        shell: pwsh
        run: ruff check src tests
      - name: Format check
        shell: pwsh
        run: black --check src tests
      - name: Prove missing TEST_DATABASE_URL fails
        shell: bash
        env:
          TEST_DATABASE_URL: ""
          DATABASE_URL: ""
        run: |
          if pytest -q tests/contract/test_pages.py::test_dashboard_renders_empty_state_and_key_elements; then
            echo "::error::pytest skipped instead of failing without TEST_DATABASE_URL"
            exit 1
          fi
      - name: Tests
        shell: pwsh
        run: pytest -q -rs
```

The Windows runner image ships PostgreSQL 17 (`postgresql-x64-17`, `C:\Program Files\PostgreSQL\17`, postgres password `root`); it does not ship Redis, and GitHub service containers cannot run on Windows runners. Memurai is the Redis 7 protocol-compatible server for Windows (`memurai.conf` compatible with Redis 7 API; installs the `Memurai` service on port 6379 by default).

- [ ] **Step 6: Validate the workflow YAML parses**

Run: `python -c "import pathlib, yaml; yaml.safe_load(pathlib.Path('.github/workflows/ci.yml').read_text(encoding='utf-8')); print('yaml ok')"`
Expected: `yaml ok`

- [ ] **Step 7: Commit**

```bash
git add tests/conftest.py tests/unit/test_database_guard.py .github/workflows/ci.yml
git commit -m "ci: add two-OS workflow and fail-not-skip database guard"
```

---

### Task 3: Endpoint golden harness

**Files:**
- Create: `tests/golden/conftest.py`
- Create: `tests/golden/test_golden_endpoints.py`
- Create (generated): `tests/golden/snapshots/*.json` (23 cases)
- Test: `tests/golden/test_golden_endpoints.py`

**Interfaces:**
- Consumes: `alembic_config`, `alembic_engine` session fixtures (`tests/conftest.py:44-56`); `create_app(*, engine, runner_factory, runs_root, clone_root, clock, redis_ping, token_present) -> FastAPI` (`src/serve/app.py:274-283`); `RunPayload`/`RunPayloadItem` dataclasses (`src/serve/executor.py:21-46`); route table registered by `register_pages` (`src/serve/pages.py:514-966`) and `app.py:315-684`.
- Produces:
  - `tests.golden.conftest.` `Observation(case, path, status, headers, body)` with `to_document() -> dict`
  - `normalize_html(body: str) -> str`
  - `enumerate_get_cases(app) -> list[tuple[str, str, dict[str, str]]]`
  - `seed_corpus(engine: Engine) -> None`
  - `build_golden_app(engine: Engine, runs_root: Path) -> FastAPI`
  - fixtures `snapshot_dir`, `golden_app`, `golden_observations`
  - `tests/golden/snapshots/<case>.json` with keys `path`, `status`, `headers`, `body`

- [ ] **Step 1: Write the failing harness and comparison test**

Create `tests/golden/conftest.py`:

```python
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from serve.app import create_app
from serve.executor import RunPayload, RunPayloadItem

GOLDEN_DIR = Path(__file__).resolve().parent
SNAPSHOT_DIR = GOLDEN_DIR / "snapshots"
UPDATE_ENV = "UPDATE_GOLDEN"
FILTER_HASH = "c0ffee00" * 8
RUN_A = 931
RUN_B = 932

CAPTURED_HEADERS = (
    "content-type",
    "cache-control",
    "content-disposition",
    "x-gitcrawl-regenerated",
    "location",
)
RELATIVE_TIME = re.compile(r"\b(?:just now|\d+m ago|\d+h ago|\d+d ago|\d+mo ago)\b")
CSRF_META = re.compile(r'(<meta name="csrf-token" content=")[^"]*(")')
CSRF_INPUT = re.compile(r'(<input type="hidden" name="csrf" value=")[^"]*(")')
DISK_WARNING = re.compile(
    r"low disk: estimated [0-9.]+ MB with [0-9.]+ MB free \(reserve [0-9.]+ MB\)"
)

FILTER_SPEC_JSON = '{"gitcrawl_filter": 1, "q": "language:rust", "sort": "stars", "order": "desc"}'

GOLDEN_SEED_SQL = (
    "TRUNCATE TABLE run_items, runs, saved_filters, audit_log, shards, geo_cache, "
    "owners, repos, full_name_history RESTART IDENTITY CASCADE",
    """
    INSERT INTO owners (id, login, type, location_raw, country_iso, geo_confidence, company)
    VALUES (901, 'octo', 'User', 'Berlin, Germany', 'DE', 'name', 'GitCrawl Labs')
    """,
    """
    INSERT INTO repos (id, node_id, full_name, owner_id, name, visibility, description,
        language, license_spdx, topics, stargazers, forks_count, open_issues, archived,
        size_kb, pushed_at, created_at)
    VALUES
        (911, 'R_911', 'octo/alpha', 901, 'alpha', 'public', 'Fast crawler', 'Rust', 'MIT',
         ARRAY['cli', 'crawler'], 300, 30, 3, false, 100, '2025-12-30T12:00:00Z',
         '2024-01-01T00:00:00Z'),
        (912, 'R_912', 'octo/beta', 901, 'beta', 'public', 'Docs site', 'Python',
         'Apache-2.0', ARRAY['docs'], 200, 20, 2, false, 200, '2025-12-29T12:00:00Z',
         '2024-02-01T00:00:00Z'),
        (913, 'R_913', 'octo/gamma', 901, 'gamma', 'public', 'Archived tool', NULL, NULL,
         ARRAY[]::text[], 100, 10, 1, true, 300, '2025-01-15T12:00:00Z',
         '2023-03-01T00:00:00Z'),
        (914, 'R_914', 'octo/delta', 901, 'delta', 'public', 'Baseline only', 'Rust', 'MIT',
         ARRAY[]::text[], 250, 5, 0, false, 50, '2025-06-01T12:00:00Z',
         '2024-03-01T00:00:00Z')
    """,
    """
    INSERT INTO saved_filters (id, name, filter_spec, created_at, updated_at)
    VALUES (921, 'rust picks', '"""
    + FILTER_SPEC_JSON
    + """'::jsonb, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
    """,
    f"""
    INSERT INTO runs (id, filter_hash, filter_spec, status, created_at, started_at,
        finished_at, api_version, total_count, fetched, inserted, updated, unchanged,
        skipped, incomplete_shards)
    VALUES
        (931, '{FILTER_HASH}', '{FILTER_SPEC_JSON}'::jsonb, 'done',
         '2026-01-02T00:00:00Z', '2026-01-02T00:00:01Z', '2026-01-02T00:05:01Z',
         '2022-11-28', 3, 3, 3, 0, 0, 0, 0),
        (932, '{FILTER_HASH}', '{FILTER_SPEC_JSON}'::jsonb, 'partial',
         '2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z', '2026-01-01T00:04:01Z',
         '2022-11-28', 2, 2, 2, 0, 0, 0, 1)
    """,
    """
    INSERT INTO run_items (run_id, repo_id, full_name, stargazers, pushed_at, archived,
        language, license_spdx, country_iso, geo_confidence, virtuals)
    VALUES
        (931, 911, 'octo/alpha', 300, '2025-12-30T12:00:00Z', false, 'Rust', 'MIT', 'DE',
         'name', '{"has_dockerfile": true}'::jsonb),
        (931, 912, 'octo/beta', 200, '2025-12-29T12:00:00Z', false, 'Python',
         'Apache-2.0', NULL, NULL, '{}'::jsonb),
        (931, 913, 'octo/gamma', 100, '2025-01-15T12:00:00Z', true, NULL, NULL, NULL,
         NULL, '{}'::jsonb),
        (932, 911, 'octo/alpha', 250, '2025-12-30T12:00:00Z', false, 'Rust', 'MIT', 'DE',
         'name', '{"has_dockerfile": true}'::jsonb),
        (932, 914, 'octo/delta', 400, '2025-06-01T12:00:00Z', false, 'Rust', 'MIT', NULL,
         NULL, '{}'::jsonb)
    """,
    """
    INSERT INTO audit_log (id, ts, params, status, token_fp, latency_ms)
    VALUES (941, '2026-01-02T00:00:02Z', '{}'::jsonb, 200, 'golden', 12)
    """,
)

PATH_PARAMS = {
    "/vsearch/runs/{filter_hash}": {"filter_hash": FILTER_HASH},
    "/vsearch/runs/{filter_hash}/export": {"filter_hash": FILTER_HASH},
    "/api/runs/{run_id}/diff": {"run_id": RUN_A},
    "/runs/{run_id}/diff": {"run_id": RUN_A},
    "/runs/{run_id}": {"run_id": RUN_A},
    "/runs/{run_id}/clone-estimate": {"run_id": RUN_A},
    "/partials/runs/{run_id}/status": {"run_id": RUN_A},
    "/partials/runs/{run_id}/table": {"run_id": RUN_A},
    "/partials/runs/{run_id}/clone-progress": {"run_id": RUN_A},
}
QUERY = {
    "/vsearch/repos": "?q=language:rust&sort=stars&order=desc",
    "/api/runs/{run_id}/diff": f"?against={RUN_B}",
}
HEADERS = {"/filters": {"Accept": "text/html"}}
EXTRA_CASES = (
    ("static_css", "/static/app.css", {}),
    ("static_js", "/static/app.js", {}),
    ("filters_json", "/filters", {}),
)


@dataclass(frozen=True)
class Observation:
    case: str
    path: str
    status: int
    headers: dict[str, str]
    body: str

    def to_document(self) -> dict:
        return {
            "path": self.path,
            "status": self.status,
            "headers": self.headers,
            "body": self.body,
        }


def normalize_html(body: str) -> str:
    normalized = body.replace("\r\n", "\n")
    normalized = CSRF_META.sub(r"\1<redacted>\2", normalized)
    normalized = CSRF_INPUT.sub(r"\1<redacted>\2", normalized)
    normalized = RELATIVE_TIME.sub("<relative-time>", normalized)
    normalized = DISK_WARNING.sub("<disk-warning>", normalized)
    return normalized


def case_id(path: str) -> str:
    cleaned = path.strip("/").replace("{", "").replace("}", "").replace("/", "_")
    return cleaned or "root"


def enumerate_get_cases(app) -> list[tuple[str, str, dict[str, str]]]:
    cases: list[tuple[str, str, dict[str, str]]] = []
    seen: set[str] = set()
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        if "GET" not in methods:
            continue
        path = route.path
        if "{" in path and path not in PATH_PARAMS:
            raise AssertionError(
                f"no golden request values for GET route {path!r}; "
                "add them to PATH_PARAMS in tests/golden/conftest.py"
            )
        identifier = case_id(path)
        if identifier in seen:
            continue
        seen.add(identifier)
        request_path = path.format(**PATH_PARAMS.get(path, {})) + QUERY.get(path, "")
        cases.append((identifier, request_path, HEADERS.get(path, {})))
    cases.extend(EXTRA_CASES)
    return cases


def observe(client: TestClient, case: str, path: str, headers: dict[str, str]) -> Observation:
    response = client.get(path, headers=headers)
    content_type = response.headers.get("content-type", "")
    captured = {
        name: response.headers[name] for name in CAPTURED_HEADERS if name in response.headers
    }
    body = response.content.decode("utf-8")
    if content_type.startswith("text/html"):
        body = normalize_html(body)
    return Observation(
        case=case, path=path, status=response.status_code, headers=captured, body=body
    )


def seed_corpus(engine: Engine) -> None:
    with engine.begin() as connection:
        for statement in GOLDEN_SEED_SQL:
            connection.execute(text(statement))


def golden_payload() -> RunPayload:
    return RunPayload(
        total_count=1,
        fetched=1,
        items=[
            RunPayloadItem(
                repo_id=911,
                full_name="octo/alpha",
                stargazers=300,
                pushed_at="2025-12-30T12:00:00Z",
                archived=False,
                language="Rust",
                license_spdx="MIT",
                country_iso="DE",
                geo_confidence="name",
                virtuals={"has_dockerfile": True},
            )
        ],
    )


def build_golden_app(engine: Engine, runs_root: Path):
    def runner_factory(_engine: Engine):
        def runner(_run_id: int, _filter_spec: dict) -> RunPayload:
            return golden_payload()

        return runner

    return create_app(
        engine=engine,
        runner_factory=runner_factory,
        runs_root=str(runs_root),
        clone_root=str(runs_root / "clones"),
        clock=lambda: 0.0,
        redis_ping=lambda: True,
        token_present=lambda: True,
    )


def write_snapshots(observations: list[Observation]) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    for observation in observations:
        target = SNAPSHOT_DIR / f"{observation.case}.json"
        target.write_text(
            json.dumps(observation.to_document(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


@pytest.fixture(scope="session")
def snapshot_dir() -> Path:
    return SNAPSHOT_DIR


@pytest.fixture(scope="session")
def golden_app(alembic_config, alembic_engine, tmp_path_factory):
    command.upgrade(alembic_config, "head")
    runs_root = tmp_path_factory.mktemp("golden-runs")
    return build_golden_app(alembic_engine, runs_root)


@pytest.fixture(scope="session")
def golden_observations(golden_app, alembic_engine):
    seed_corpus(alembic_engine)
    cases = enumerate_get_cases(golden_app)
    with TestClient(golden_app, raise_server_exceptions=False) as client:
        observations = [observe(client, case, path, headers) for case, path, headers in cases]
    if os.environ.get(UPDATE_ENV) == "1":
        write_snapshots(observations)
    return observations
```

Create `tests/golden/test_golden_endpoints.py`:

```python
from __future__ import annotations

import difflib
import json


def test_golden_endpoints_match_snapshots(golden_observations, snapshot_dir):
    problems: list[str] = []
    for observation in golden_observations:
        snapshot = snapshot_dir / f"{observation.case}.json"
        if not snapshot.is_file():
            problems.append(
                f"{observation.case}: missing {snapshot}; "
                "regenerate with UPDATE_GOLDEN=1 pytest tests/golden -q"
            )
            continue
        expected = json.loads(snapshot.read_text(encoding="utf-8"))
        actual = observation.to_document()
        if expected == actual:
            continue
        problems.append(
            "\n".join(
                difflib.unified_diff(
                    json.dumps(expected, indent=2, sort_keys=True).splitlines(),
                    json.dumps(actual, indent=2, sort_keys=True).splitlines(),
                    fromfile=str(snapshot),
                    tofile=f"observed {observation.path}",
                    lineterm="",
                )
            )
        )
    assert not problems, "\n\n".join(problems)


def test_snapshot_set_matches_route_set(golden_observations, snapshot_dir):
    expected = {observation.case for observation in golden_observations}
    existing = {path.stem for path in snapshot_dir.glob("*.json")}
    assert existing - expected == set(), (
        f"stale golden snapshots: {sorted(existing - expected)}; "
        "delete them or add the route case"
    )
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/golden/test_golden_endpoints.py -q`
Expected: FAIL — `missing .../snapshots/openapi_json.json`, `missing .../snapshots/root.json`, and one message per case; 23 cases total.

- [ ] **Step 3: Generate the snapshots and re-run**

Run: `$env:UPDATE_GOLDEN = "1"; pytest tests/golden -q`
Expected: PASS, and `tests/golden/snapshots/` contains 23 `*.json` files. Inspect `openapi_json.json` and `root.json` to confirm bodies look sane.

Run: `pytest tests/golden -q`
Expected: PASS with no update env set.

- [ ] **Step 4: Prove byte-stability and that a diff fails**

Run:
```
git add tests/golden
$env:UPDATE_GOLDEN = "1"; pytest tests/golden -q
git status --short tests/golden
```
Expected: only `A ` lines; no `M ` or `AM ` lines (regeneration produced byte-identical files).

Run:
```
python -c "import json, pathlib; p = pathlib.Path('tests/golden/snapshots/health.json'); d = json.loads(p.read_text(encoding='utf-8')); d['status'] = 500; p.write_text(json.dumps(d, indent=2) + '\n', encoding='utf-8')"
pytest tests/golden/test_golden_endpoints.py -q
```
Expected: FAIL with a unified diff showing `-  "status": 200` and `+  "status": 500`.

Run:
```
git checkout -- tests/golden
pytest tests/golden/test_golden_endpoints.py -q
```
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: PASS; the golden session fixture truncates and re-seeds the test database once, then captures once, so later truncation by other suites cannot change the observed snapshots.

- [ ] **Step 6: Commit**

```bash
git add tests/golden/conftest.py tests/golden/test_golden_endpoints.py tests/golden/snapshots
git commit -m "test: add deterministic endpoint golden harness"
```

---

### Task 4: OpenAPI hash pin

**Files:**
- Create: `tests/golden/test_openapi_pin.py`
- Create: `tests/golden/snapshots/openapi.sha256`
- Test: `tests/golden/test_openapi_pin.py`

**Interfaces:**
- Consumes: `create_app()` (`src/serve/app.py:274-283`, module-level `app = create_app()` at `:700`).
- Produces: `openapi_digest() -> str` (SHA-256 over `json.dumps(app.openapi(), sort_keys=True, separators=(",", ":"))`); `tests/golden/snapshots/openapi.sha256`.

Measured 2026-10-01 with FastAPI 0.142.2 / pydantic 2.13.5: `7d29b8a0f38d9417948f2ccc76345d979ea67b5a1e5717a5e8ac159efeae1d31`.

- [ ] **Step 1: Write the failing test**

Create `tests/golden/test_openapi_pin.py`:

```python
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from serve.app import create_app

PIN_FILE = Path(__file__).resolve().parent / "snapshots" / "openapi.sha256"


def openapi_digest() -> str:
    schema = create_app().openapi()
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def test_openapi_schema_is_pinned():
    assert PIN_FILE.is_file(), f"missing OpenAPI pin at {PIN_FILE}"
    pinned = PIN_FILE.read_text(encoding="utf-8").strip()
    actual = openapi_digest()
    assert actual == pinned, (
        f"OpenAPI schema changed: pinned={pinned} actual={actual}; "
        f"update {PIN_FILE} only if the contract change is intended"
    )
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/golden/test_openapi_pin.py -v`
Expected: FAIL — `missing OpenAPI pin at .../snapshots/openapi.sha256`.

- [ ] **Step 3: Create the pin using the canonical digest**

Run:
```
python -c "import hashlib, json; from serve.app import create_app; schema = create_app().openapi(); print(hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest())"
```
Expected: `7d29b8a0f38d9417948f2ccc76345d979ea67b5a1e5717a5e8ac159efeae1d31`.

Write that exact value plus a trailing newline to `tests/golden/snapshots/openapi.sha256` (for example with a text editor or):
```
python -c "from pathlib import Path; Path('tests/golden/snapshots/openapi.sha256').write_text('7d29b8a0f38d9417948f2ccc76345d979ea67b5a1e5717a5e8ac159efeae1d31' + '\n', encoding='utf-8')"
```

- [ ] **Step 4: Run the pin test and prove it bites**

Run: `pytest tests/golden/test_openapi_pin.py -v`
Expected: PASS.

Run:
```
python -c "from pathlib import Path; p = Path('tests/golden/snapshots/openapi.sha256'); p.write_text('0' * 64 + '\n', encoding='utf-8')"
pytest tests/golden/test_openapi_pin.py -v
```
Expected: FAIL with `OpenAPI schema changed: pinned=000...`. Restore the file with the digest from Step 3.

- [ ] **Step 5: Commit**

```bash
git add tests/golden/test_openapi_pin.py tests/golden/snapshots/openapi.sha256
git commit -m "test: pin the OpenAPI schema hash"
```

---

### Task 5: Coverage floor, baseline record, and ratchet policy

**Files:**
- Modify: `requirements-dev.txt:4` (append after `fakeredis[lua]==2.38.0`)
- Modify: `pyproject.toml` (append coverage sections after line 21)
- Modify: `.github/workflows/ci.yml` (`Tests` steps in both jobs)
- Modify: `docs/development-log.md` (append a Phase 0 section at end of file)
- Test: `pytest -q --cov=src --cov-report=term-missing` with `--cov-fail-under` probes

**Interfaces:**
- Consumes: `requirements-dev.txt`, `pyproject.toml`, the CI workflow from Task 2.
- Produces: `pytest-cov==7.1.0`; `[tool.coverage.run] source = ["src"]`; `[tool.coverage.report] fail_under = 93`, `show_missing = true`, `skip_covered = true`; CI test command includes `--cov=src --cov-report=term-missing`.

Measured baseline 2026-10-01 (Windows, Python 3.12.10, 1089 tests, `TEST_DATABASE_URL` set, 0 skips): 4965 statements, 336 missed, **93.23%** total. Lowest modules: `src/skeleton.py` 0% (135 statements), `src/serve/runner.py` 89%, `src/serve/__main__.py` 89%, `src/serve/pages.py` 91%, `src/serve/app.py` 92%. Everything else is 92-100%. `app.js` has no pytest coverage at all (`tests/js/rownav.test.mjs` covers one extracted helper via Node). Real-Redis client paths are exercised only by `fakeredis`. The spec's §4.4 list also names `cloner.free_disk_mb` and `cloner._default_git_runner`; measured `src/enrich/cloner.py` is 99% with 1 missed statement, so confirm the actual missed lines in Step 3 and record them honestly.

- [ ] **Step 1: Add the dev dependency and coverage config**

In `requirements-dev.txt`, append:

```
pytest-cov==7.1.0
```

In `pyproject.toml`, append:

```toml
[tool.coverage.run]
source = ["src"]

[tool.coverage.report]
fail_under = 93
show_missing = true
skip_covered = true
```

- [ ] **Step 2: Install and measure the baseline**

Run: `pip install -r requirements-dev.txt`
Expected: `pytest-cov` and `coverage` install successfully.

Run: `pytest -q --cov=src --cov-report=term-missing`
Expected: PASS with exit code 0; the coverage table's `TOTAL` is at or above the pre-golden baseline of 93% (4965 statements / 336 missed). Note the exact `TOTAL` percentage.

- [ ] **Step 3: Prove the floor bites**

Run: `pytest -q --cov=src --cov-report=term-missing --cov-fail-under=94`
Expected: exit code 2, `Coverage failure: total of 93 is less than fail-under=94` (the exact total may be one point higher if the golden suite raised it).

- [ ] **Step 4: Record the baseline and ratchet policy**

Append to `docs/development-log.md` (replace the baseline numbers with the measured ones if the golden suite raised them):

```markdown
## Quality hardening Phase 0 — regression harness (2026-10-01)

| Item | Result |
|---|---|
| Coverage baseline | 93.23% total (4965 statements, 336 missed), Windows/Python 3.12.10, 1089 tests with `TEST_DATABASE_URL` set |
| Floor | `fail_under = 93` in `pyproject.toml` |
| Ratchet policy | Raise `fail_under` by 1 whenever a phase's measured total exceeds the floor by at least 2 points; never lower it; record each change here |
| Untested at baseline | `src/skeleton.py` 0% (excluded from black/ruff; quarantined in Phase 1b); `static/app.js` has no pytest coverage (`tests/js/rownav.test.mjs` covers one helper via Node); real-Redis client paths run against `fakeredis` only |
| CI | `.github/workflows/ci.yml`: ubuntu-latest `postgres:17` + `redis:7` services; windows-latest native PostgreSQL 17 + Memurai 4.1.8; `GITCRAWL_REQUIRE_TEST_DB=1` |
| Golden harness | `tests/golden/` snapshots 23 GET-route cases; JSON byte-exact, HTML normalized (CSRF, relative time, disk warning); regenerate with `UPDATE_GOLDEN=1 pytest tests/golden -q` |
| OpenAPI pin | `tests/golden/snapshots/openapi.sha256` pins canonical `app.openapi()`; a schema change fails CI |
```

- [ ] **Step 5: Enforce coverage in CI**

In `.github/workflows/ci.yml`, change both `Tests` steps from `pytest -q -rs` to:

```yaml
        run: pytest -q -rs --cov=src --cov-report=term-missing
```

- [ ] **Step 6: Validate YAML and commit**

Run: `python -c "import pathlib, yaml; yaml.safe_load(pathlib.Path('.github/workflows/ci.yml').read_text(encoding='utf-8')); print('yaml ok')"`
Expected: `yaml ok`

```bash
git add requirements-dev.txt pyproject.toml .github/workflows/ci.yml docs/development-log.md
git commit -m "ci: enforce a 93% coverage floor and record the baseline"
```

---

### Task 6: Advisory dependency audit

**Files:**
- Modify: `requirements-dev.txt` (append after `pytest-cov==7.1.0`)
- Modify: `.github/workflows/ci.yml` (append the `audit` job)
- Test: `pip-audit -r requirements.txt --progress-spinner off` (command, no new test file)

**Interfaces:**
- Consumes: `requirements.txt`, the CI workflow from Task 2/5.
- Produces: `pip-audit==2.10.1`; CI job `audit` with `continue-on-error: true` initially.

Measured 2026-10-01 with pip-audit 2.10.1 against `requirements.txt`: `No known vulnerabilities found` (exit 0).

- [ ] **Step 1: Pin pip-audit and reproduce the clean report**

In `requirements-dev.txt`, append:

```
pip-audit==2.10.1
```

Run: `pip install -r requirements-dev.txt`
Then: `pip-audit -r requirements.txt --progress-spinner off`
Expected: `No known vulnerabilities found` with exit code 0. (If the live advisory database reports an issue, keep the job advisory and note the finding; do not suppress or ignore it silently.)

- [ ] **Step 2: Add the advisory CI job**

Append to `.github/workflows/ci.yml`:

```yaml
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ env.PYTHON_VERSION }}
          cache: pip
      - name: Install audit tooling
        run: pip install -r requirements.txt -r requirements-dev.txt
      - name: Audit dependencies (advisory until first green run)
        continue-on-error: true
        run: pip-audit -r requirements.txt --progress-spinner off
```

- [ ] **Step 3: Validate and commit**

Run: `python -c "import pathlib, yaml; yaml.safe_load(pathlib.Path('.github/workflows/ci.yml').read_text(encoding='utf-8')); print('yaml ok')"`
Expected: `yaml ok`

```bash
git add requirements-dev.txt .github/workflows/ci.yml
git commit -m "ci: add advisory pip-audit dependency report"
```

- [ ] **Step 4: Flip to blocking after the first green audit run (follow-up commit)**

Once the `audit` job has completed green on `main`, remove the `continue-on-error: true` line from the audit step and commit:

```bash
git add .github/workflows/ci.yml
git commit -m "ci: make pip-audit blocking"
```

Spec §4.6: blocking after the first clean run.

---

## Spec Coverage

| Spec Phase 0 requirement | Task |
|---|---|
| §4.1 GitHub Actions on `ubuntu-latest` with `postgres:17` + `redis:7` services | T2 |
| §4.1 CI on `windows-latest` with PostgreSQL 17 + Redis 7 (platform adaptation) | T2 |
| §4.1 Create database `gitcrawl_test`, set `TEST_DATABASE_URL` | T2 |
| §4.1 Steps: pip install, `ruff check`, `black --check`, `pytest` | T2 |
| §4.1 Missing `TEST_DATABASE_URL` fails in CI, not skip | T2 (guard + workflow proof step + `tests/unit/test_database_guard.py`) |
| §4.2 Session fixture seeds a fixed corpus into the migrated real DB | T3 |
| §4.2 Enumerate every registered GET route | T3 (`enumerate_get_cases`; new parameterized routes fail loudly) |
| §4.2 Capture status + selected headers + full body | T3 (`CAPTURED_HEADERS`, `Observation`) |
| §4.2 JSON byte-exact; HTML deterministically normalized | T3 (`normalize_html`: CSRF, relative time, disk warning) |
| §4.2 Snapshots under `tests/golden/`; any diff fails | T3 (comparison + stale-snapshot tests) |
| §4.3 Hash `app.openapi()` and pin it | T4 |
| §4.4 Add `pytest-cov` | T5 |
| §4.4 Record baseline; set floor and ratchet policy | T5 (93 floor, 93.23% baseline, ratchet text in `docs/development-log.md`) |
| §4.4 Report untested modules (skeleton, cloner free-disk/git-runner, real Redis, `app.js`) | T5 (recorded in the log from measured `term-missing` output) |
| §4.5 `black` is the single formatter; CI runs `black --check`, not `ruff format` | T1 + T2 gate |
| §4.5 Reformat so the baseline is clean | T1 |
| §4.5 `src/skeleton.py` excluded via pyproject | T1 (config already present at `pyproject.toml:13,21`; verified clean) |
| §4.6 `pip-audit` job, non-blocking initially, blocking after first clean run | T6 |
| §4 acceptance: deliberate golden diff fails | T3 Step 4 |
| §4 acceptance: OpenAPI hash stable | T4 Step 4 |
| INV-4 no runtime surface | Every task touches only CI, tests, docs, config, or black-only reformat |

## Risks / Known Unknowns

1. **Windows cannot run Linux service containers.** GitHub documents that service containers require a Linux runner. The plan substitutes the Windows image's preinstalled PostgreSQL 17 (`postgresql-x64-17`, password `root`) and Memurai Developer 4.1.8 (Redis 7 protocol compatible) instead of literal `postgres:17`/`redis:7` containers on Windows. If the spec owner requires literal containers on Windows, the only alternative is a Linux-only DB/Redis job plus a Windows job without service tests; that is a spec deviation decision, not a plan detail.
2. **Image and package drift is unverified until the first CI run.** Windows runner paths/service names (`postgresql-x64-17`, `C:\Program Files\PostgreSQL\17`, `C:\Program Files\Memurai`, service `Memurai`) come from the 2026-09 runner image docs and Memurai docs; they were not executed here. `choco install memurai-developer` is pinned to the verified 4.1.8 package version. If the Windows job fails at service startup, fall back to `ikalnytskyi/action-setup-postgres@v8` (supports PostgreSQL 17 on Windows) for the database and record the change in the development log.
3. **Golden snapshots are generated at execution time, not pre-verified.** The plan fixes the corpus, route cases, and normalization rules, but the 23 snapshot bodies cannot be reproduced inside this document. Steps 2-4 of T3 create, re-check, and mutate-test them before commit.
4. **HTML normalization is an allowlist.** CSRF tokens, relative times, and low-disk warnings are the only known volatile values (`pages.py:112-129`, `:549-568`, `runs.py:258-286`). A future template adding another volatile value fails the suite loudly; the fix is to extend `normalize_html` deliberately, never to loosen equality.
5. **Coverage across OSes may differ slightly.** The 93.23% floor leaves roughly 11 statements of headroom. There are no `sys.platform` branches in `src/`, so Linux should match; if the Linux job measures below 93, set `fail_under = 92` in the same commit and record why in `docs/development-log.md` (the floor must stay honest, not aspirational).
6. **Two explicit skips remain in CI.** `tests/integration/test_delta_probe.py:19` and `tests/integration/test_golden_org.py:25` skip without `GITHUB_TOKEN`, which CI does not provide. They are reason-visible skips printed by `pytest -rs`, not silent DB skips. `tests/unit/test_rownav_js.py:9` skips only if Node is absent; CI runners include Node. Reaching literal zero skips requires a token policy outside Phase 0.
7. **OpenAPI digest is dependency-bound.** `7d29b8a0...` holds for FastAPI 0.142.2 / pydantic 2.13.5 from `requirements.txt`. A deliberate framework bump requires updating the pin in a reviewable commit.
8. **The fail-not-skip guard is test-only by design.** Locally without a database the suite still skips; only `GITCRAWL_REQUIRE_TEST_DB=1` (CI) turns absence into failure. This preserves developer ergonomics and satisfies INV-4.
9. **Spec drift found while planning:** the audit says 10 formatting-drift files; the measured set is 7. The spec's §4.4 untested-module list names `cloner.free_disk_mb` / `cloner._default_git_runner`, but measured `cloner.py` is 99% with one missed statement; T5 Step 3 records the actual missing lines instead of copying the stale list. The plan cites measured facts over the spec where they conflict.
10. **CI workflow behavior is only parse-validated locally.** The YAML is syntax-checked with PyYAML; green/red status can only be observed after the first push. The `audit` job is advisory until its first green run by design.
11. **No behavior change intended.** `src/` changes are black-only reformatting plus no runtime edits; the guard lives in `tests/conftest.py`. If any task finds it must touch runtime code, stop and route the change to Phase 1 with the spec's §11 exception register.
