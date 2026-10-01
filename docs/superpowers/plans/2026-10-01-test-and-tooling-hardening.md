# Test and Tooling Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Centralize the duplicated DB fixtures, make the test suite independent of the launch directory, remove the wall-clock flake assertions, cover the extractable `app.js` helpers with Node tests, quarantine and test the intentionally-unwired surface, add a scoped mypy baseline, enforce the core-never-imports-`serve` boundary, and record the new gates in the development log.

**Architecture:** Pure test/tooling work under the spec's freeze contract: no runtime behavior changes. A `clean_db` factory in `tests/conftest.py` replaces 20 hand-rolled TRUNCATE/seed fixtures with declarative row dictionaries (SQLAlchemy Core inserts, proven equivalent by the existing suite). Flake-prone timing assertions become operation-count spies, injected events, and barriers. `src/serve/static/app.js` pure logic moves to a UMD `applib.js` mirroring the `rownav.js` pattern. A `tests/quarantine_manifest.txt` plus a static import-graph gate documents and protects the built-but-unwired surface. Scoped mypy (`src/lib`, `src/limiter`, `src/store`, `src/scheduler`) is added with per-module tightening, and the audit module moves from `serve` to `lib` so core packages never import `serve`.

**Tech Stack:** Python 3.12, pytest 9.1.1, SQLAlchemy 2.x + psycopg3 (PostgreSQL 18 local test cluster), Alembic, mypy 2.3.1, Node 26 (`node --test`), vanilla ES5-style JS, ruff, black.

**Spec:** `docs/superpowers/specs/2026-10-01-quality-hardening-design.md` (Phase 1b = §5 items 8-11 and §8 acceptance)

## Global Constraints

- INV-1 — Identical normal behavior: every observable output for normal use is unchanged.
- INV-2 — Additive-only UI: no existing control moves, disappears, becomes required, or changes its result.
- INV-3 — Behavior may differ only for cross-site requests, hung requests, oversized bodies, infrastructure-outage visibility, and the approved run-summary fix.
- INV-4 — CI, formatting, typing, coverage, fixture centralization, annotation, and documentation work change no runtime behavior.
- INV-5 — Any unavoidable deviation is recorded in spec §11 with an explicit approval checkbox before it ships.
- Python 3.12; ruff line length 100 and `black` is the single formatter (do not run `ruff format`).
- Integration/contract tests require `TEST_DATABASE_URL` whose database name ends in `_test`; the local environment also has `GITHUB_TOKEN` set (never print it).
- One commit per task; no commit mixes two tasks.
- Every rewritten test must keep its original observable assertions; only the flake mechanism changes.
- Baseline before this plan: **1089 tests, 0 failures, 0 errors, 0 skips** (`pytest -q --junitxml=...`); suite wall time ~104 s.
- Execution order: run this plan **after Phase 0 (`2026-10-01-ci-and-regression-harness.md`) and Phase 1a (`2026-10-01-zero-behavior-fixes.md`)**. Both sibling plans exist as of 2026-10-01. Phase 1a does not implement spec §5 item 8, so Task 7 executes as written. Do not run this plan concurrently with Phase 1a: both touch `src/discover/pipeline.py`, `src/store/upserts.py`, `tests/integration/test_pipeline.py`, `tests/integration/test_upserts.py`, and `tests/integration/test_executor.py`.

---

## File Structure

| File | Change |
|---|---|
| `tests/conftest.py` | **modify** — add `repo_root` fixture and `clean_db` factory |
| `tests/unit/test_fixture_centralization.py` | **new** — guard: raw `TRUNCATE TABLE` only in the shared factory + 3 allowlisted single-purpose fixtures |
| `tests/unit/test_cwd_independence.py` | **new** — static `Path("...")` guard + subprocess runs from another CWD |
| `tests/unit/test_rownav_js.py` | **modify** — absolute paths + repo-root CWD |
| `tests/contract/test_console_pages.py` | **modify** — factory wrapper, absolute CSS path, applib contract assertions |
| `tests/unit/test_id_accumulation.py` | **modify** — operation-count spy replaces `perf_counter` |
| `tests/contract/test_filter_form.py` | **modify** — factory wrapper; drop `elapsed < 2.0`, assert release not set |
| `tests/unit/test_cloner.py` | **modify** — factory wrapper; barrier replaces sleep/peak |
| `tests/integration/test_executor.py` | **modify** — factory `db` wrapper; event/barrier synchronization; drop `import time` |
| `src/serve/static/applib.js` | **new** — UMD pure helpers extracted from `app.js` |
| `src/serve/static/app.js` | **modify** — consume `gitcrawlApp`; identical behavior |
| `src/serve/templates/base.html` | **modify** — load `applib.js` before `app.js` |
| `tests/js/applib.test.mjs` | **new** — Node tests for the extracted helpers |
| `tests/unit/test_applib_js.py` | **new** — pytest wrapper for the Node test |
| `tests/quarantine_manifest.txt` | **new** — intentionally-unwired modules/functions |
| `tests/unit/test_quarantine_manifest.py` | **new** — import-graph + manifest gate |
| `tests/unit/test_skeleton.py` | **new** — first test for the quarantined `skeleton` module |
| `tests/unit/test_serve_main.py` | **new** — import smoke test for `serve.__main__` |
| `requirements-dev.txt` | **modify** — add `mypy==2.3.1` |
| `pyproject.toml` | **modify** — `[tool.mypy]` scope + overrides + ratchet policy comments |
| `src/lib/gh_client.py` | **modify** — `limiter_key` narrowing + `or 0.0` for optional retry hints |
| `src/limiter/buckets.py` | **modify** — `Any` Redis handle; `value: object` helpers |
| `src/limiter/classifier.py` | **modify** — typed header/number helpers |
| `src/store/upserts.py` | **modify** — `TypeGuard[int]` on `_valid_id`; driver guard |
| `src/store/lifecycle.py` | **modify** — `cast(int, …)`; `TYPE_CHECKING` import breaks the store↔hydrate cycle |
| `src/discover/pipeline.py` | **modify** — `Deps.token_id` → `Deps.token_fp`; `from lib import audit` |
| `src/serve/runner.py` | **modify** — `deps.token_fp`; `from lib import audit` |
| `src/lib/audit.py` | **rename from `src/serve/audit.py`** |
| `src/discover/search_shards.py`, `src/enrich/trees_first.py`, `src/hydrate/repo_client.py` | **modify** — `from lib import audit` |
| `tests/unit/test_import_boundaries.py` | **new** — core-never-imports-`serve` + store-never-imports-hydrate |
| `tests/unit/test_audit.py`, `tests/integration/test_audit_db.py`, `tests/integration/test_pipeline.py`, `tests/integration/test_lifecycle.py`, `tests/integration/test_runner.py`, `tests/contract/test_github_pagination.py`, `tests/unit/test_trees_first.py`, `tests/integration/test_delta_probe.py` | **modify** — import `lib.audit` |
| `docs/development-log.md` | **modify** — program entry points + gates |
| 16 further test files listed in Task 1 | **modify** — factory wrappers |

---

### Task 1: Centralize duplicated DB clean/seed fixtures as `conftest` factories

**Files:**
- Modify: `tests/conftest.py:1-56`
- Create: `tests/unit/test_fixture_centralization.py`
- Modify (fixture only): `tests/unit/test_cloner.py:38-45`, `tests/unit/test_watermark.py:38-44`, `tests/contract/test_clone_endpoints.py:36-53`, `tests/contract/test_console_api.py:23-41`, `tests/contract/test_console_pages.py:27-45`, `tests/contract/test_filter_form.py:175-199`, `tests/contract/test_pages.py:46-70`, `tests/contract/test_run_detail.py:41-70`, `tests/contract/test_runs_export.py:28-52`, `tests/contract/test_vsearch_api.py:46-70`, `tests/integration/test_audit_db.py:20-29`, `tests/integration/test_diff.py:20-40`, `tests/integration/test_geo_resolver.py:26-30`, `tests/integration/test_library.py:30-34`, `tests/integration/test_lifecycle.py:46-55`, `tests/integration/test_models_console.py:77-86`, `tests/integration/test_pipeline.py:141-150`, `tests/integration/test_runner.py:33-42`, `tests/integration/test_upserts.py:112-118`, `tests/integration/test_executor.py:66-84`, `tests/integration/test_clone_registry.py:467-471` (created by Phase 1a Task 3 — migrate it here)

**Interfaces:**
- Consumes: existing `alembic_engine` fixture; `store.models.Owner`, `store.models.Repo`.
- Produces: `CleanDbFactory = Callable[..., Engine]`; fixture `clean_db` returning `seed(*, owners: Sequence[Mapping[str, object]] = (), repos: Sequence[Mapping[str, object]] = ()) -> Engine` which truncates all nine tables with `RESTART IDENTITY CASCADE` and inserts the given owner/repo rows with SQLAlchemy Core.
- All migrated local fixtures keep the name `clean` (or `db` in `test_executor.py`) and keep returning `Engine`, so no test function signature changes.

- [ ] **Step 1: Write the failing centralization guard**

```python
# tests/unit/test_fixture_centralization.py
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWED_TRUNCATE_FILES = {
    "tests/conftest.py",
    "tests/golden/conftest.py",
    "tests/integration/test_golden_org.py",
    "tests/unit/test_since_scan.py",
    "tests/unit/test_state_machine.py",
    "tests/unit/test_fixture_centralization.py",
}


def _truncate_offenders() -> list[str]:
    offenders: list[str] = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative in ALLOWED_TRUNCATE_FILES:
            continue
        if "TRUNCATE TABLE" in path.read_text(encoding="utf-8"):
            offenders.append(relative)
    return offenders


def test_truncate_sql_lives_only_in_the_shared_factory():
    assert _truncate_offenders() == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_fixture_centralization.py -q`
Expected: FAIL — the 20 fixture files listed above plus `tests/integration/test_clone_registry.py` (if Phase 1a landed) are reported as offenders.

- [ ] **Step 3: Add the `clean_db` factory and `repo_root` fixture to `tests/conftest.py`**

Append to `tests/conftest.py` (and extend the imports at the top with `from collections.abc import Callable, Mapping, Sequence`, `from sqlalchemy import text`, and `from store.models import Owner, Repo`):

```python
CleanDbFactory = Callable[..., Engine]

FULL_TRUNCATE_TABLES = (
    "run_items",
    "runs",
    "saved_filters",
    "audit_log",
    "shards",
    "geo_cache",
    "owners",
    "repos",
    "full_name_history",
)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture()
def clean_db(alembic_engine: Engine) -> CleanDbFactory:
    def seed(
        *,
        owners: Sequence[Mapping[str, object]] = (),
        repos: Sequence[Mapping[str, object]] = (),
    ) -> Engine:
        with alembic_engine.begin() as connection:
            connection.execute(
                text(
                    "TRUNCATE TABLE " + ", ".join(FULL_TRUNCATE_TABLES)
                    + " RESTART IDENTITY CASCADE"
                )
            )
            if owners:
                connection.execute(Owner.__table__.insert(), [dict(row) for row in owners])
            if repos:
                connection.execute(Repo.__table__.insert(), [dict(row) for row in repos])
        return alembic_engine

    return seed
```

The full-table truncate is a superset of every fixture's previous table list; truncating strictly more tables cannot weaken an assertion, and each test seeds everything it uses.

- [ ] **Step 4: Migrate every local fixture to a declarative wrapper**

The wrappers below cover every seed shape in the 20 files. Replace each fixture body with the matching wrapper (the owner/repo dictionaries are the exact values from the current raw SQL; do not change ids, names, or timestamps).

**Variant A** — `tests/contract/test_filter_form.py`, `tests/contract/test_pages.py`, `tests/contract/test_runs_export.py`, `tests/contract/test_vsearch_api.py`:

```python
@pytest.fixture()
def clean(clean_db):
    return clean_db(
        owners=[
            {
                "id": 1,
                "login": "octo",
                "type": "User",
                "location_raw": "Berlin, Germany",
                "country_iso": "DE",
                "geo_confidence": "name",
            }
        ],
        repos=[
            {
                "id": 1296269,
                "node_id": "R_1296269",
                "full_name": "octo/hello",
                "owner_id": 1,
                "name": "hello",
                "visibility": "public",
                "description": "My first repo",
                "language": "Ruby",
                "license_spdx": "MIT",
                "topics": ["octocat"],
                "stargazers": 80,
                "forks_count": 9,
                "open_issues": 0,
                "pushed_at": "2011-01-26T19:06:43Z",
            }
        ],
    )
```

**Variant B** — `tests/contract/test_run_detail.py` (same owner as A; four repos):

```python
@pytest.fixture()
def clean(clean_db):
    return clean_db(
        owners=[
            {
                "id": 1,
                "login": "octo",
                "type": "User",
                "location_raw": "Berlin, Germany",
                "country_iso": "DE",
                "geo_confidence": "name",
            }
        ],
        repos=[
            {
                "id": 1296269,
                "node_id": "R_1296269",
                "full_name": "octo/hello",
                "owner_id": 1,
                "name": "hello",
                "visibility": "public",
                "size_kb": 1024,
                "stargazers": 80,
                "pushed_at": "2026-01-03T00:00:00Z",
                "archived": False,
                "language": "Ruby",
                "license_spdx": "MIT",
            },
            {
                "id": 101,
                "node_id": "R_101",
                "full_name": "octo/alpha",
                "owner_id": 1,
                "name": "alpha",
                "visibility": "public",
                "size_kb": 1024,
                "stargazers": 30,
                "pushed_at": "2026-01-03T00:00:00Z",
                "archived": False,
                "language": "Ruby",
                "license_spdx": "MIT",
            },
            {
                "id": 102,
                "node_id": "R_102",
                "full_name": "octo/beta",
                "owner_id": 1,
                "name": "beta",
                "visibility": "public",
                "size_kb": 1024,
                "stargazers": 20,
                "pushed_at": "2026-01-01T00:00:00Z",
                "archived": False,
                "language": "Rust",
                "license_spdx": "Apache-2.0",
            },
            {
                "id": 103,
                "node_id": "R_103",
                "full_name": "octo/gamma",
                "owner_id": 1,
                "name": "gamma",
                "visibility": "public",
                "size_kb": 1024,
                "stargazers": 10,
                "pushed_at": "2026-01-02T00:00:00Z",
                "archived": True,
                "language": "Go",
                "license_spdx": "MIT",
            },
        ],
    )
```

**Variant C** — `tests/contract/test_console_api.py`, `tests/contract/test_console_pages.py`:

```python
@pytest.fixture()
def clean(clean_db):
    return clean_db(
        owners=[{"id": 1, "login": "octo", "type": "User"}],
        repos=[
            {
                "id": 1,
                "node_id": "R_1",
                "full_name": "octo/r1",
                "owner_id": 1,
                "name": "r1",
                "visibility": "public",
            },
            {
                "id": 2,
                "node_id": "R_2",
                "full_name": "octo/r2",
                "owner_id": 1,
                "name": "r2",
                "visibility": "public",
            },
            {
                "id": 3,
                "node_id": "R_3",
                "full_name": "octo/r3",
                "owner_id": 1,
                "name": "r3",
                "visibility": "public",
            },
        ],
    )
```

**Variant D** — `tests/integration/test_diff.py` (Variant C rows plus two more): append these to the `repos` list inside the same wrapper:

```python
            {
                "id": 4,
                "node_id": "R_4",
                "full_name": "octo/r4",
                "owner_id": 1,
                "name": "r4",
                "visibility": "public",
            },
            {
                "id": 5,
                "node_id": "R_5",
                "full_name": "octo/r5",
                "owner_id": 1,
                "name": "r5",
                "visibility": "public",
            },
```

**Variant E** — `tests/contract/test_clone_endpoints.py`:

```python
@pytest.fixture()
def clean(clean_db):
    return clean_db(
        owners=[{"id": 1, "login": "octo", "type": "User"}],
        repos=[
            {
                "id": 1296269,
                "node_id": "R_1296269",
                "full_name": "octo/hello",
                "owner_id": 1,
                "name": "hello",
                "visibility": "public",
                "size_kb": 1024,
            },
            {
                "id": 2,
                "node_id": "R_2",
                "full_name": "octo/world",
                "owner_id": 1,
                "name": "world",
                "visibility": "public",
                "size_kb": 2048,
            },
        ],
    )
```

**Variant F** — `tests/unit/test_cloner.py`:

```python
@pytest.fixture()
def clean(clean_db):
    return clean_db(owners=[{"id": 1, "login": "octo", "type": "User"}])
```

**Variant G** — no seeds: `tests/unit/test_watermark.py`, `tests/integration/test_audit_db.py`, `tests/integration/test_geo_resolver.py`, `tests/integration/test_library.py`, `tests/integration/test_lifecycle.py`, `tests/integration/test_models_console.py`, `tests/integration/test_pipeline.py`, `tests/integration/test_runner.py`, `tests/integration/test_upserts.py`, `tests/integration/test_clone_registry.py` (the latter created by Phase 1a):

```python
@pytest.fixture()
def clean(clean_db):
    return clean_db()
```

**Variant H** — `tests/integration/test_executor.py` keeps the fixture name `db`:

```python
@pytest.fixture()
def db(clean_db):
    return clean_db(
        owners=[{"id": 1, "login": "octo", "type": "User"}],
        repos=[
            {
                "id": 1,
                "node_id": "n1",
                "full_name": "octo/hello",
                "owner_id": 1,
                "name": "hello",
                "visibility": "public",
            },
            {
                "id": 2,
                "node_id": "n2",
                "full_name": "octo/world",
                "owner_id": 1,
                "name": "world",
                "visibility": "public",
            },
            {
                "id": 3,
                "node_id": "n3",
                "full_name": "octo/extra",
                "owner_id": 1,
                "name": "extra",
                "visibility": "public",
            },
        ],
    )
```

File-to-variant map (this is the complete list of 19 `def clean` fixtures plus `test_executor`'s `db`; verified by `rg -n "def clean" tests`):

| Variant | Files |
|---|---|
| A | `tests/contract/test_filter_form.py`, `tests/contract/test_pages.py`, `tests/contract/test_runs_export.py`, `tests/contract/test_vsearch_api.py` |
| B | `tests/contract/test_run_detail.py` |
| C | `tests/contract/test_console_api.py`, `tests/contract/test_console_pages.py` |
| D | `tests/integration/test_diff.py` |
| E | `tests/contract/test_clone_endpoints.py` |
| F | `tests/unit/test_cloner.py` |
| G | `tests/unit/test_watermark.py`, `tests/integration/test_audit_db.py`, `tests/integration/test_geo_resolver.py`, `tests/integration/test_library.py`, `tests/integration/test_lifecycle.py`, `tests/integration/test_models_console.py`, `tests/integration/test_pipeline.py`, `tests/integration/test_runner.py`, `tests/integration/test_upserts.py`, `tests/integration/test_clone_registry.py` (Phase 1a-created) |
| H | `tests/integration/test_executor.py` (`db`) |

Remove the now-unused `from sqlalchemy import text` import from `tests/contract/test_console_api.py` and `tests/integration/test_library.py` (their only `text(` uses were the fixtures). Leave `test_golden_org.py`, `test_state_machine.py`, and `test_since_scan.py` untouched (live/single-table uses, allowlisted above).

- [ ] **Step 5: Run the guard and the full suite**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_fixture_centralization.py -q`
Expected: PASS.

Run: `.\.venv\Scripts\python.exe -m pytest -q`
Expected: exit 0; progress reaches 100% with no `F`/`E`; all 1089 pre-existing tests still pass (plus the new guard test).

- [ ] **Step 6: Commit**

```bash
git add tests/conftest.py tests/unit/test_fixture_centralization.py tests/contract tests/integration tests/unit
git commit -m "test: centralize duplicated DB clean/seed fixtures"
```

---

### Task 2: Make test paths independent of the launch directory

**Files:**
- Modify: `tests/conftest.py` (add `repo_root`; done in Task 1)
- Modify: `tests/unit/test_rownav_js.py:9-17`
- Modify: `tests/contract/test_console_pages.py:569-574`
- Create: `tests/unit/test_cwd_independence.py`

**Interfaces:**
- Consumes: `repo_root: Path` session fixture from `tests/conftest.py`.
- Produces: CWD-independent Node wrapper; static guard that no test passes a relative string to `Path(...)`.

- [ ] **Step 1: Write the failing CWD-independence tests**

```python
# tests/unit/test_cwd_independence.py
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_no_relative_path_literals_in_tests(repo_root: Path):
    offenders: list[str] = []
    for path in sorted((repo_root / "tests").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or getattr(node.func, "id", "") != "Path":
                continue
            if not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if not os.path.isabs(first.value):
                    offenders.append(f"{path.relative_to(repo_root).as_posix()}:{node.lineno}")
    assert offenders == []


def _run_from_another_cwd(repo_root: Path, tmp_path: Path, target: str, *extra: str):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(repo_root / target),
            *extra,
            "-q",
            "-p",
            "no:warnings",
        ],
        cwd=str(tmp_path),
        env=dict(os.environ),
        capture_output=True,
        text=True,
        check=False,
    )


def test_css_asset_test_passes_from_another_cwd(repo_root: Path, tmp_path: Path):
    result = _run_from_another_cwd(
        repo_root,
        tmp_path,
        "tests/contract/test_console_pages.py",
        "-k",
        "app_css_uses_compositor_progress",
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_node_wrapper_passes_from_another_cwd(repo_root: Path, tmp_path: Path):
    result = _run_from_another_cwd(repo_root, tmp_path, "tests/unit/test_rownav_js.py")
    assert result.returncode == 0, result.stdout + result.stderr
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_cwd_independence.py -q`
Expected: FAIL — the static guard reports `tests/contract/test_console_pages.py:572`; the CSS subprocess fails because `src/serve/static/app.css` is resolved against the temporary CWD.

- [ ] **Step 3: Anchor the two CWD-relative paths**

`tests/contract/test_console_pages.py` — change the test signature and the CSS read:

```python
def test_app_css_uses_compositor_progress_and_row_containment(repo_root: Path):
    css = (repo_root / "src/serve/static/app.css").read_text(encoding="utf-8")
    assert "transform: scaleX(var(--progress, 0))" in css
    assert "content-visibility: auto" in css
```

(Add `from pathlib import Path` at the top of the file if it is not already present.)

`tests/unit/test_rownav_js.py` — replace the whole file:

```python
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_rownav_module(repo_root: Path):
    result = subprocess.run(
        ["node", "--test", str(repo_root / "tests/js/rownav.test.mjs")],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

- [ ] **Step 4: Run the CWD tests and the full suite**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_cwd_independence.py tests/unit/test_rownav_js.py -q`
Expected: PASS (the Node test runs because Node 26 is installed).

Run: `.\.venv\Scripts\python.exe -m pytest -q`
Expected: exit 0, no `F`/`E`.

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py tests/unit/test_cwd_independence.py tests/unit/test_rownav_js.py tests/contract/test_console_pages.py
git commit -m "test: make test paths independent of the launch directory"
```

---

### Task 3: Replace wall-clock flake assertions with deterministic synchronization

**Files:**
- Modify: `tests/unit/test_id_accumulation.py:16-25`
- Modify: `tests/contract/test_filter_form.py:508-534`
- Modify: `tests/unit/test_cloner.py:403-431`
- Modify: `tests/integration/test_executor.py:438-457,473-493,545-601,604-643` (and the `import time` at line 7)

**Interfaces:** No production interfaces change; the same tests assert the same observable ordering/parallelism properties.

- [ ] **Step 1: Rewrite `test_collect_ids_stays_linear_at_100k_items` as an operation-count spy**

Replace lines 16-25 of `tests/unit/test_id_accumulation.py` and delete `import time` (line 3):

```python
class AppendOnlyList(list):
    def __init__(self) -> None:
        super().__init__()
        self.appends = 0

    def append(self, value: object) -> None:
        self.appends += 1
        super().append(value)

    def extend(self, values: object) -> None:
        raise AssertionError("extend would recopy the accumulated ids")

    def __iadd__(self, values: object) -> None:
        raise AssertionError("in-place concatenation would recopy the accumulated ids")


def test_collect_ids_appends_once_per_new_id():
    seen: set[int] = set()
    collected = AppendOnlyList()
    items = [{"id": value} for value in range(100_000)]
    _collect_ids(seen, collected, items)
    assert len(collected) == 100_000
    assert collected.appends == 100_000
    assert seen == set(range(100_000))
```

This keeps the 100k scale and fails deterministically if the collector ever regrows by tuple concatenation or list extension, without measuring time.

- [ ] **Step 2: Drop the 2-second redirect assertion in `test_find_redirects_before_the_run_finishes`**

Replace lines 520-534 of `tests/contract/test_filter_form.py` with:

```python
    response = client.post(
        "/find", data={"csrf": token, "action": "find", "keywords": "language:rust"}
    )

    assert response.status_code == 303
    assert release.is_set() is False
    assert started.wait(10)
    run_id = int(response.headers["location"].rsplit("/", 1)[1])
    with clean.connect() as connection:
        status = connection.scalar(text("SELECT status FROM runs WHERE id = :id"), {"id": run_id})
    assert status in ("queued", "running")
    release.set()
    assert wait_for_run(clean, run_id) == "done"
```

The runner blocks on `release`, so a 303 response while `release` is still unset is a deterministic proof the route never waited for run completion.

- [ ] **Step 3: Replace the cloner sleep/peak test with a barrier**

Replace lines 403-431 of `tests/unit/test_cloner.py` with:

```python
def test_clone_workers_run_in_parallel(clean: Engine, tmp_path):
    specs = [(repo_id, f"octo/r{repo_id}", 1, repo_id) for repo_id in range(1, 5)]
    run_id, _ = seed_run(clean, specs)
    barrier = threading.Barrier(4)

    def fake_runner(argv, cwd):
        barrier.wait(timeout=5)

    stats = clone_repos(
        clean,
        run_id,
        limit=4,
        mode=CloneMode.SHALLOW,
        dest_root=str(tmp_path),
        git_runner=fake_runner,
        workers=4,
    )
    assert stats.completed == 4
    assert stats.failed == 0
```

Add `import threading` at the top of `tests/unit/test_cloner.py`. With one worker all four barrier waits time out and `clone_one` records failures, so the test still fails loudly (in ~20 s) rather than passing by chance.

- [ ] **Step 4: Replace the executor sleep-based ordering tests**

`tests/integration/test_executor.py` — delete `import time` (line 7; no use remains after these rewrites).

`test_interactive_calls_jump_ahead_of_bulk_runs` (lines 438-457) becomes:

```python
def test_interactive_calls_jump_ahead_of_bulk_runs(db: Engine, tmp_path):
    order: list[str] = []
    started = threading.Event()
    release = threading.Event()

    def slow(run_id: int, spec: dict) -> RunPayload:
        order.append(f"bulk-{run_id}")
        started.set()
        release.wait(timeout=10)
        return RunPayload(1, [_item()])

    first = create_run(db, FILTER, api_version="v1")
    second = create_run(db, FILTER, api_version="v1")
    executor = RunExecutor(db, runner=slow, runs_root=str(tmp_path))
    executor.submit(first)
    assert started.wait(10) is True
    bulk = executor.submit(second)
    interactive = executor.submit_call(lambda: order.append("interactive") or "done")
    release.set()
    assert interactive.result(timeout=2) == "done"
    bulk.result(timeout=2)
    assert order.index("interactive") < order.index(f"bulk-{second}")
```

`test_run_executor_fifo_and_single_worker` (lines 473-493) becomes:

```python
def test_run_executor_fifo_and_single_worker(db: Engine, tmp_path):
    events: list[tuple[str, int]] = []
    lock = threading.Lock()
    first_started = threading.Event()
    release_first = threading.Event()

    def runner(run_id: int, spec: dict) -> RunPayload:
        with lock:
            events.append(("start", run_id))
        if run_id == first:
            first_started.set()
            assert release_first.wait(10)
        with lock:
            events.append(("end", run_id))
        return RunPayload(1, [_item()])

    first = create_run(db, FILTER, api_version="v1")
    second = create_run(db, FILTER, api_version="v1")
    executor = RunExecutor(db, runner=runner, runs_root=str(tmp_path))
    executor.submit(first)
    assert first_started.wait(10) is True
    executor.submit(second)
    with lock:
        assert events == [("start", first)]
    release_first.set()
    assert executor.wait(10) is True
    assert events == [("start", first), ("end", first), ("start", second), ("end", second)]
    assert run_status(db, first)["status"] == "done"
    assert run_status(db, second)["status"] == "done"
```

`test_run_executor_enqueue_keeps_idle_cleared_for_outstanding_job` — replace the polling block (lines 590-596) with a direct assertion:

```python
    release_first.set()
    assert task_done_finished.wait(10) is True
    try:
        assert executor._idle.is_set() is False
    finally:
        allow_put.set()
```

`test_run_executor_concurrent_submits_keep_single_worker` (lines 604-643) becomes:

```python
def test_run_executor_concurrent_submits_keep_single_worker(db: Engine, tmp_path, monkeypatch):
    events: list[tuple[str, int]] = []
    lock = threading.Lock()
    first_started = threading.Event()
    release_first = threading.Event()
    extra_start = threading.Event()

    def runner(run_id: int, spec: dict) -> RunPayload:
        with lock:
            starts = sum(1 for kind, _ in events if kind == "start")
            events.append(("start", run_id))
            if starts >= 1:
                extra_start.set()
        if run_id == first:
            first_started.set()
            assert release_first.wait(10)
        with lock:
            events.append(("end", run_id))
        return RunPayload(1, [_item()])

    executor = RunExecutor(db, runner=runner, runs_root=str(tmp_path))
    run_ids = [create_run(db, FILTER, api_version="v1") for _ in range(4)]
    first = run_ids[0]
    real_thread = threading.Thread
    gate = threading.Barrier(len(run_ids))

    class GatedThread(real_thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            try:
                gate.wait(1)
            except threading.BrokenBarrierError:
                pass

    monkeypatch.setattr(executor_module.threading, "Thread", GatedThread)
    submitters = [real_thread(target=executor.submit, args=(run_id,)) for run_id in run_ids]
    for thread in submitters:
        thread.start()
    for thread in submitters:
        thread.join(10)
    assert first_started.wait(10) is True
    assert extra_start.wait(0.5) is False
    release_first.set()
    assert executor.wait(30) is True

    order = [run_id for _, run_id in events]
    assert sorted(order[::2]) == sorted(run_ids)
    assert order[::2] == order[1::2]
    assert all(kind == "start" for kind, _ in events[::2])
    assert all(kind == "end" for kind, _ in events[1::2])
    for run_id in run_ids:
        assert run_status(db, run_id)["status"] == "done"
```

The 0.5 s wait is a liveness allowance for a second worker to start a queued job while the first is blocked — not a performance threshold. A single-worker executor cannot set `extra_start` before `release_first`.

- [ ] **Step 5: Run the rewritten tests, then the full suite**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_id_accumulation.py tests/unit/test_cloner.py tests/contract/test_filter_form.py tests/integration/test_executor.py -q`
Expected: PASS. If any rewritten test fails on the current source, stop and investigate an ordering bug; do not re-loosen the assertion.

Run: `.\.venv\Scripts\python.exe -m pytest -q`
Expected: exit 0, no `F`/`E`.

- [ ] **Step 6: Commit**

```bash
git add tests/unit/test_id_accumulation.py tests/unit/test_cloner.py tests/contract/test_filter_form.py tests/integration/test_executor.py
git commit -m "test: replace wall-clock flake assertions with deterministic synchronization"
```

---

### Task 4: Extract `app.js` pure helpers into a UMD module and cover them with Node tests

**Files:**
- Create: `src/serve/static/applib.js`, `tests/js/applib.test.mjs`, `tests/unit/test_applib_js.py`
- Modify: `src/serve/static/app.js:1-51,89-103,256-305,307-325`, `src/serve/templates/base.html:22-24`
- Modify (Phase 0 golden snapshots): `tests/golden/snapshots/static_js.json`, all HTML snapshots containing the script tags
- Test: `tests/contract/test_console_pages.py` (new contract assertions)

**Interfaces:**
- Produces: UMD global/CommonJS `gitcrawlApp` exposing `TOAST_DURATION_MS: number`, `requestErrorMessage(status) -> string`, `cloneErrorMessage() -> string`, `cloneStartedMessage() -> string`, `parseCloneLimit(raw) -> number`, `isEditableTarget(target) -> boolean`, `createRowCache() -> {get(compute), invalidate()}`, `createStartGuard() -> {begin(): boolean, end()}`.
- Consumes in the browser: `base.html` loads `applib.js` before `app.js`; `app.js` reads `window.gitcrawlApp`.

- [ ] **Step 1: Write the failing Node test and its pytest wrapper**

```javascript
// tests/js/applib.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

import applib from "../../src/serve/static/applib.js";

test("row cache computes once until invalidated", () => {
  const cache = applib.createRowCache();
  const rows = [1, 2, 3];
  let calls = 0;
  const compute = () => {
    calls += 1;
    return rows;
  };
  assert.equal(cache.get(compute), rows);
  assert.equal(cache.get(compute), rows);
  assert.equal(calls, 1);
  cache.invalidate();
  assert.equal(cache.get(compute), rows);
  assert.equal(calls, 2);
});

test("start guard refuses a second begin until end", () => {
  const guard = applib.createStartGuard();
  assert.equal(guard.begin(), true);
  assert.equal(guard.begin(), false);
  guard.end();
  assert.equal(guard.begin(), true);
});

test("parseCloneLimit clamps missing and negative values to zero", () => {
  assert.equal(applib.parseCloneLimit("42"), 42);
  assert.equal(applib.parseCloneLimit("3.9"), 3);
  assert.equal(applib.parseCloneLimit("abc"), 0);
  assert.equal(applib.parseCloneLimit("-1"), 0);
  assert.equal(applib.parseCloneLimit(""), 0);
});

test("isEditableTarget detects form controls and contenteditable", () => {
  assert.equal(applib.isEditableTarget(null), false);
  assert.equal(applib.isEditableTarget({ tagName: "INPUT" }), true);
  assert.equal(applib.isEditableTarget({ tagName: "textarea" }), true);
  assert.equal(applib.isEditableTarget({ tagName: "div" }), false);
  assert.equal(applib.isEditableTarget({ tagName: "div", isContentEditable: true }), true);
});

test("toast messages and duration are stable", () => {
  assert.equal(applib.requestErrorMessage(500), "Request failed (500)");
  assert.equal(applib.requestErrorMessage(""), "Request failed");
  assert.equal(applib.cloneErrorMessage(), "Clone request failed");
  assert.equal(applib.cloneStartedMessage(), "Clone started");
  assert.equal(applib.TOAST_DURATION_MS, 6000);
});
```

```python
# tests/unit/test_applib_js.py
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_applib_module(repo_root: Path):
    result = subprocess.run(
        ["node", "--test", str(repo_root / "tests/js/applib.test.mjs")],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

- [ ] **Step 2: Run to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_applib_js.py -q`
Expected: FAIL — `applib.js` does not exist.

- [ ] **Step 3: Implement `applib.js` and rewire `app.js` without behavior change**

```javascript
// src/serve/static/applib.js
(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.gitcrawlApp = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var TOAST_DURATION_MS = 6000;

  function requestErrorMessage(status) {
    return status ? "Request failed (" + status + ")" : "Request failed";
  }

  function cloneErrorMessage() {
    return "Clone request failed";
  }

  function cloneStartedMessage() {
    return "Clone started";
  }

  function parseCloneLimit(raw) {
    var limit = parseInt(raw, 10);
    if (isNaN(limit) || limit < 0) {
      return 0;
    }
    return limit;
  }

  function isEditableTarget(target) {
    if (!target) {
      return false;
    }
    if (target.isContentEditable) {
      return true;
    }
    var tag = target.tagName ? target.tagName.toLowerCase() : "";
    return tag === "input" || tag === "textarea" || tag === "select" || tag === "option";
  }

  function createRowCache() {
    var cached = null;
    return {
      get: function (compute) {
        if (cached === null) {
          cached = compute();
        }
        return cached;
      },
      invalidate: function () {
        cached = null;
      }
    };
  }

  function createStartGuard() {
    var busy = false;
    return {
      begin: function () {
        if (busy) {
          return false;
        }
        busy = true;
        return true;
      },
      end: function () {
        busy = false;
      }
    };
  }

  return {
    TOAST_DURATION_MS: TOAST_DURATION_MS,
    requestErrorMessage: requestErrorMessage,
    cloneErrorMessage: cloneErrorMessage,
    cloneStartedMessage: cloneStartedMessage,
    parseCloneLimit: parseCloneLimit,
    isEditableTarget: isEditableTarget,
    createRowCache: createRowCache,
    createStartGuard: createStartGuard
  };
});
```

`src/serve/templates/base.html` — insert between lines 23 and 24:

```html
<script src="/static/applib.js" defer></script>
```

`src/serve/static/app.js` edits:

1. After `"use strict";` add `var applib = window.gitcrawlApp;`.
2. `showToast` line 39: use `applib.TOAST_DURATION_MS` instead of `6000`.
3. Delete the `isEditable` function (lines 42-51) and change `handleKeyboard` line 161 to `if (applib.isEditableTarget(event.target)) {`.
4. Replace lines 89-103 (`rowCache`/`invalidateRowCache`/`selectableRows`) with:

```javascript
  var rowCache = applib.createRowCache();

  function invalidateRowCache() {
    rowCache.invalidate();
  }

  function selectableRows() {
    return rowCache.get(function () {
      return Array.prototype.slice.call(document.querySelectorAll("tbody tr")).filter(function (row) {
        return !row.hidden && row.querySelector("a[href]");
      });
    });
  }
```

5. Replace `startClone`'s guard and limit parsing (lines 261-275) with:

```javascript
  var startGuard = applib.createStartGuard();

  function startClone(button) {
    if (button.disabled || !startGuard.begin()) {
      return;
    }
    button.disabled = true;
    var modal = document.getElementById("clone-modal");
    var runId =
      button.getAttribute("data-run-id") || (modal ? modal.getAttribute("data-run-id") : "");
    var number = document.getElementById("clone-limit-input");
    var slider = document.getElementById("clone-limit");
    var raw = number ? number.value : slider ? slider.value : "0";
    var limit = applib.parseCloneLimit(raw);
```

6. In the fetch chain: `showToast(applib.cloneStartedMessage());` (line 291), `showToast(applib.cloneErrorMessage());` (line 300), and in `finally` add `startGuard.end();` after `button.disabled = false;`.
7. `htmx:responseError` handler line 319: `showToast(applib.requestErrorMessage(status));`.

- [ ] **Step 4: Add the contract assertions and run the JS/contract tests**

Add to `tests/contract/test_console_pages.py`:

```python
def test_applib_loads_before_app_js(client: TestClient):
    html = client.get("/").text

    assert 'src="/static/rownav.js"' in html
    assert 'src="/static/applib.js"' in html
    assert html.index("/static/applib.js") < html.index("/static/app.js")


def test_app_js_invalidates_the_row_cache_after_htmx_swaps(client: TestClient):
    script = client.get("/static/app.js").text

    assert "window.gitcrawlApp" in script
    assert "applib.createRowCache" in script
    assert '"htmx:afterSwap", invalidateRowCache' in script
```

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_applib_js.py tests/unit/test_rownav_js.py tests/contract/test_console_pages.py -q`
Expected: PASS.

- [ ] **Step 5: Refresh the Phase 0 golden snapshots and prove the diff is additive-only**

Run: `$env:UPDATE_GOLDEN = "1"; .\.venv\Scripts\python.exe -m pytest tests/golden -q`
Expected: PASS.

Run: `git diff tests/golden/snapshots`
Expected: only `static_js.json` (the new app.js body), plus the added `<script src="/static/applib.js" defer></script>` line in HTML snapshots (e.g. `root.json`). No status, no captured header, and no existing markup line changes. Delete the `UPDATE_GOLDEN` variable (`Remove-Item Env:UPDATE_GOLDEN`) and re-run `pytest tests/golden -q`; expected PASS.

If the Phase 0 golden harness is not present (Phase 0 not executed), skip this step.

- [ ] **Step 6: Full suite and commit**

Run: `.\.venv\Scripts\python.exe -m pytest -q`
Expected: exit 0, no `F`/`E`.

```bash
git add src/serve/static/applib.js src/serve/static/app.js src/serve/templates/base.html tests/js/applib.test.mjs tests/unit/test_applib_js.py tests/contract/test_console_pages.py tests/golden/snapshots
git commit -m "test: extract and cover app.js pure helpers"
```

---

### Task 5: Quarantine manifest and CI gate for the unwired surface

**Files:**
- Create: `tests/quarantine_manifest.txt`, `tests/unit/test_quarantine_manifest.py`, `tests/unit/test_skeleton.py`, `tests/unit/test_serve_main.py`

**Interfaces:**
- `tests/quarantine_manifest.txt`: one dotted entry per line (module or `module.function`), `#` comments allowed. The entries (verified by grep; all already have tests except `skeleton`, whose test is added here) are exactly the list created in Step 1.
- Gate behavior: every `src/**/*.py` module is imported by at least one test file **or** matched by a manifest entry; every manifest entry resolves to a real module/function; every manifest entry has at least one test.

- [ ] **Step 1: Create the manifest, then write the failing quarantine gate and skeleton tests**

Create `tests/quarantine_manifest.txt` with exactly:

```text
# Intentionally unwired surface (spec 5.9 / ruling R24 + R57).
# Every entry must keep at least one test; see tests/unit/test_quarantine_manifest.py.
limiter.retry
enrich.mirrors
enrich.graphql_batch
skeleton
discover.pipeline.run_since_scan
discover.since_scan.plan_id_ranges
enrich.trees_first.fetch_metafiles
scheduler.state_machine.pel_size
scheduler.state_machine.reclaim_stale
scheduler.state_machine.retry_or_dlq
scheduler.tiering.order_shards
store.lifecycle.purge_tombstones
store.upserts.bootstrap_copy
```

Then write the test files:

```python
# tests/unit/test_quarantine_manifest.py
from __future__ import annotations

import ast
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parents[1]
ROOT = TESTS_ROOT.parent
SRC = ROOT / "src"
MANIFEST = TESTS_ROOT / "quarantine_manifest.txt"


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _src_modules() -> set[str]:
    return {_module_name(path) for path in SRC.rglob("*.py")}


def _test_imports() -> set[str]:
    names: set[str] = set()
    for path in TESTS_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names.add(node.module)
    return names


def _manifest_entries() -> list[str]:
    return [
        line.strip()
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _imported_by_tests(module: str, imports: set[str]) -> bool:
    return any(name == module or name.startswith(module + ".") for name in imports)


def _entry_covers(entry: str, module: str) -> bool:
    return entry == module or entry.startswith(module + ".")


def test_every_src_module_is_tested_or_quarantined():
    imports = _test_imports()
    entries = _manifest_entries()
    missing = [
        module
        for module in sorted(_src_modules())
        if not _imported_by_tests(module, imports)
        and not any(_entry_covers(entry, module) for entry in entries)
    ]
    assert missing == []


def test_every_quarantine_entry_has_a_test():
    imports = _test_imports()
    modules = _src_modules()
    bodies = [path.read_text(encoding="utf-8") for path in TESTS_ROOT.rglob("*.py")]
    unresolved: list[str] = []
    for entry in _manifest_entries():
        if entry in modules:
            covered = _imported_by_tests(entry, imports)
        else:
            _, _, name = entry.rpartition(".")
            covered = any(name in body for body in bodies)
        if not covered:
            unresolved.append(entry)
    assert unresolved == []


def test_manifest_entries_exist_in_src():
    for entry in _manifest_entries():
        if (SRC / (entry.replace(".", "/") + ".py")).is_file():
            continue
        module, _, name = entry.rpartition(".")
        source = SRC / (module.replace(".", "/") + ".py")
        assert source.is_file(), f"manifest module is missing: {module}"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        assert name in names, f"{entry} is not defined in {module}"
```

```python
# tests/unit/test_skeleton.py
from __future__ import annotations

import pytest

import skeleton


def test_filter_hash_is_stable():
    assert skeleton.filter_hash() == "2bc6b774968a"


def test_has_next_detects_only_the_next_link():
    assert skeleton.has_next('<https://api.github.com/x?page=2>; rel="next"') is True
    assert skeleton.has_next('<https://api.github.com/x?page=5>; rel="last"') is False
    assert skeleton.has_next(None) is False


def test_retry_delay_prefers_retry_after():
    assert skeleton.retry_delay({"Retry-After": "2.5"}) == 2.5
    assert skeleton.retry_delay({"Retry-After": "soon"}) == 0.0


def test_build_headers_sends_no_authorization_without_a_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    headers = skeleton.build_headers()

    assert "Authorization" not in headers
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["User-Agent"] == "gitcrawl/0.0-skeleton"


def test_validate_rejects_unknown_qualifiers(capsys):
    with pytest.raises(SystemExit) as excinfo:
        skeleton.validate("language:rust license:mit")

    assert excinfo.value.code == 2
    assert "invalid qualifier: license" in capsys.readouterr().err
```

```python
# tests/unit/test_serve_main.py
from __future__ import annotations


def test_serve_main_is_importable():
    import serve.__main__ as serve_main

    assert callable(serve_main.main)
```

- [ ] **Step 2: Run to verify the gate fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_quarantine_manifest.py -q`
Expected: FAIL — `test_every_src_module_is_tested_or_quarantined` reports `['serve.__main__']`, and `test_every_quarantine_entry_has_a_test` reports `['skeleton']`.

- [ ] **Step 3: Add the two tests from Step 1 and re-run**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_quarantine_manifest.py tests/unit/test_skeleton.py tests/unit/test_serve_main.py -q`
Expected: PASS.

- [ ] **Step 4: Full suite and commit**

Run: `.\.venv\Scripts\python.exe -m pytest -q`
Expected: exit 0, no `F`/`E`.

```bash
git add tests/quarantine_manifest.txt tests/unit/test_quarantine_manifest.py tests/unit/test_skeleton.py tests/unit/test_serve_main.py
git commit -m "test: add quarantine manifest and tested skeleton entrypoint"
```

---

### Task 6: Scoped mypy baseline and annotation-defect fixes

**Files:**
- Modify: `requirements-dev.txt`, `pyproject.toml`
- Modify: `src/lib/gh_client.py:128-138,151,164,184`, `src/limiter/buckets.py:91-122`, `src/limiter/classifier.py:27-40`, `src/store/upserts.py:95-96,443-445`, `src/store/lifecycle.py:7-13,80`
- Modify: `src/discover/pipeline.py:42,79,147,260,340,380,394`, `src/serve/runner.py:92,122,192`
- Modify tests: `tests/integration/test_pipeline.py:117-118,548`, `tests/integration/test_runner.py:117,682`, `tests/integration/test_golden_org.py:58,88,135`

**Interfaces:**
- `Deps.token_id` is renamed to `Deps.token_fp` (`str`, the token fingerprint already produced by `lib.gh_client.token_fingerprint`). Callers keep passing their existing values; function kwargs elsewhere keep the name `token_id`.
- `mypy` config: `files = ["src/lib", "src/limiter", "src/store", "src/scheduler"]`, `mypy_path = "src"`, `explicit_package_bases = true`, `ignore_missing_imports = true`, `follow_imports = "silent"`; per-module override turns on `disallow_untyped_defs` for `limiter.buckets` and `limiter.classifier`.
- Ratchet policy (also embedded as comments in `pyproject.toml`): expand scope one package at a time only after the current scope is green in CI; never add a bare `# type: ignore`; overrides may only tighten; next packages are `discover`, `hydrate`, `enrich`, `serve`; the end state removes `follow-imports = "silent"`.

- [ ] **Step 1: Add mypy and the config, then observe the red baseline**

Append `mypy==2.3.1` to `requirements-dev.txt` and add to `pyproject.toml`:

```toml
[tool.mypy]
python_version = "3.12"
files = ["src/lib", "src/limiter", "src/store", "src/scheduler"]
mypy_path = "src"
explicit_package_bases = true
ignore_missing_imports = true
follow_imports = "silent"
warn_unused_ignores = true

# Ratchet policy: expand "files" one package at a time only after the current
# scope is green in CI; never add a bare "# type: ignore"; overrides only tighten.
# Next packages: discover, hydrate, enrich, serve. The end state deletes
# follow_imports = "silent" so every imported module is gated.

[[tool.mypy.overrides]]
module = ["limiter.buckets", "limiter.classifier"]
disallow_untyped_defs = true
```

Run: `.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`

Run: `.\.venv\Scripts\python.exe -m mypy`
Expected: FAIL with 16 errors in 5 files:

```text
src/store/upserts.py:181: Invalid index type "Any | None" for "dict[int, int]"
src/store/upserts.py:182: Invalid index type "Any | None" for "dict[int, object]"
src/store/upserts.py:445: Item "None" of "Any | None" has no attribute "cursor"
src/store/lifecycle.py:85: Argument 2 to "_record_history" has incompatible type "Any | None"
src/lib/gh_client.py:132,151,164: Argument 2 to limiter method has incompatible type "str | None"
src/lib/gh_client.py:136,137,184: optional retry hint passed where float expected
src/limiter/buckets.py:91,97,106,116: missing parameter annotations (no-untyped-def)
src/limiter/classifier.py:27,34: missing annotations (no-untyped-def)
```

- [ ] **Step 2: Fix the concrete annotation defects**

`src/store/upserts.py` — add `from typing import TypeGuard` and change `_valid_id` (line 95):

```python
def _valid_id(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)
```

and the DBAPI driver guard (line 444):

```python
            driver = connection.connection.driver_connection
            if driver is None:
                raise RuntimeError("database driver does not expose a DBAPI connection")
            with driver.cursor() as cursor:
```

`src/store/lifecycle.py` — imports (line 6) and the hydration repo id (line 80):

```python
from typing import TYPE_CHECKING, cast
```

and replace the module-level `from hydrate.repo_client import HydratedRepo` (line 11) with:

```python
if TYPE_CHECKING:
    from hydrate.repo_client import HydratedRepo
```

and line 80:

```python
    repo_id = cast(int, payload.get("id"))
```

`src/lib/gh_client.py` — before the retry loop (after line 127) add:

```python
    if limiter is not None and token_id is None:
        raise ValueError("token_id is required when a limiter is configured")
    limiter_key = token_id or ""
```

then pass `limiter_key` instead of `token_id` to `limiter.acquire` (line 132), `limiter.release` (line 151), and `limiter.update_from_headers` (line 164). Change the optional hint uses:

```python
                    raise ThrottledError(acquired.retry_after or 0.0)
```

```python
                sleep(acquired.retry_after or 0.0)
```

```python
            sleep(decision.sleep_seconds or 0.0)
```

`src/limiter/buckets.py` — add `from typing import Any`, annotate the helpers and the Redis handle:

```python
def _as_text(value: object) -> str:
```

```python
def _parse_int(value: object) -> int | None:
```

```python
def _parse_float(value: object) -> float | None:
```

```python
    def __init__(
        self,
        redis: Any,
        *,
        specs: dict[str, tuple[int, float]] = RESOURCE_SPECS,
        max_concurrent: int = 10,
    ) -> None:
```

`src/limiter/classifier.py` — annotate the helpers:

```python
def _header(headers: Mapping[str, str], name: str) -> str | None:
```

```python
def _parse_number(value: object) -> float | None:
```

- [ ] **Step 3: Run mypy to verify the scoped scope is green**

Run: `.\.venv\Scripts\python.exe -m mypy`
Expected: `Success: no issues found in 12 source files`.

- [ ] **Step 4: Rename `Deps.token_id` to `Deps.token_fp` and update every user**

`src/discover/pipeline.py`: line 42 becomes `token_fp: str = "anonymous"`; `deps.token_id` becomes `deps.token_fp` on lines 79, 147, 260, 340, 380, 394 (keyword names `token_id=` passed to HTTP/limiter functions stay unchanged; only the attribute changes).

`src/serve/runner.py`: line 92 `token_fp=token_fingerprint(token)`; line 122 `token_fp=deps.token_fp`; line 192 `token_id=deps.token_fp`.

Tests:

```python
# tests/integration/test_pipeline.py:117-118
def make_deps(engine: Engine, client: httpx.Client, redis=None, token_fp: str = "test-fp") -> Deps:
    return Deps(client=client, engine=engine, redis=redis, limiter=None, token_fp=token_fp)
```

and line 548 `assert deps.token_fp == "anonymous"`.

`tests/integration/test_runner.py:117` `token_fp="test-fp"`; line 682 `assert deps.token_fp == token_fingerprint("sekret")`.

`tests/integration/test_golden_org.py:88` `token_fp=token_fingerprint(token)`; lines 58 and 135 `token_id=deps.token_fp` (the kwarg name is `token_id`).

- [ ] **Step 5: Run the affected tests and the full suite**

Run: `.\.venv\Scripts\python.exe -m pytest tests/integration/test_pipeline.py tests/integration/test_runner.py tests/integration/test_golden_org.py -q`
Expected: PASS (golden-org runs live because `GITHUB_TOKEN` is set).

Run: `.\.venv\Scripts\python.exe -m mypy; .\.venv\Scripts\python.exe -m pytest -q`
Expected: mypy `Success: no issues found in 12 source files`; pytest exit 0 with no `F`/`E`.

- [ ] **Step 6: Commit**

```bash
git add requirements-dev.txt pyproject.toml src/lib/gh_client.py src/limiter/buckets.py src/limiter/classifier.py src/store/upserts.py src/store/lifecycle.py src/discover/pipeline.py src/serve/runner.py tests/integration/test_pipeline.py tests/integration/test_runner.py tests/integration/test_golden_org.py
git commit -m "types: add scoped mypy baseline and fix annotation defects"
```

---

### Task 7: Move audit out of `serve` and enforce the import boundary

**Files:**
- Rename: `src/serve/audit.py` → `src/lib/audit.py`
- Create: `tests/unit/test_import_boundaries.py`
- Modify: `src/discover/pipeline.py:28`, `src/discover/search_shards.py:12`, `src/enrich/trees_first.py:14`, `src/hydrate/repo_client.py:13`, `src/serve/runner.py:33`
- Modify tests: `tests/unit/test_audit.py:10,185,195,205,214`, `tests/integration/test_audit_db.py:10,119,139,151`, `tests/integration/test_pipeline.py:16,553`, `tests/integration/test_lifecycle.py:646`, `tests/integration/test_runner.py:676,690,709`, `tests/contract/test_github_pagination.py:284`, `tests/unit/test_trees_first.py:361`, `tests/integration/test_delta_probe.py:12`

**Interfaces:**
- Final import direction: `discover`, `enrich`, `hydrate`, `scheduler`, `limiter`, `store`, `lib` never import `serve` (at runtime); `lib.audit` is the single home of `AuditRecord`, `cached_json`, `record_from_response`, `record_audit`, `AuditBuffer`, `slo_snapshot`, `query_hash`; `serve` imports `lib`; `store.lifecycle` no longer runtime-imports `hydrate`.
- Monkeypatch compatibility: tests that patch the audit module through its importer (`runner_module.audit`) keep working because `serve/runner.py` imports `lib.audit` as `audit`.

- [ ] **Step 1: Write the failing boundary tests**

```python
# tests/unit/test_import_boundaries.py
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE_PACKAGES = ("discover", "enrich", "hydrate", "scheduler", "limiter", "store", "lib")


def _is_type_checking(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "TYPE_CHECKING") or (
        isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"
    )


def _runtime_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            parent = parents.get(node)
            in_type_checking = False
            while parent is not None:
                if isinstance(parent, ast.If) and _is_type_checking(parent.test):
                    in_type_checking = True
                    break
                parent = parents.get(parent)
            if not in_type_checking:
                names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def _offenders(package: str, prefix: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted((ROOT / "src" / package).rglob("*.py")):
        hits = sorted(
            name
            for name in _runtime_imports(path)
            if name == prefix or name.startswith(prefix + ".")
        )
        if hits:
            found[path.relative_to(ROOT).as_posix()] = hits
    return found


def test_core_never_imports_serve():
    offenders: dict[str, list[str]] = {}
    for package in CORE_PACKAGES:
        offenders.update(_offenders(package, "serve"))
    assert offenders == {}


def test_store_does_not_import_hydrate_at_runtime():
    assert _offenders("store", "hydrate") == {}
```

- [ ] **Step 2: Run to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_import_boundaries.py -q`
Expected: FAIL — `discover/pipeline.py`, `discover/search_shards.py`, `enrich/trees_first.py`, `hydrate/repo_client.py` import `serve`, and `store/lifecycle.py` imports `hydrate.repo_client`.

- [ ] **Step 3: Move `audit` to `lib` and update every import**

```bash
git mv src/serve/audit.py src/lib/audit.py
```

Then change the root-package imports:

- `src/discover/pipeline.py:28`: `from serve import audit` → `from lib import audit`
- `src/discover/search_shards.py:12`: `from serve import audit` → `from lib import audit`
- `src/enrich/trees_first.py:14`: `from serve import audit` → `from lib import audit`
- `src/hydrate/repo_client.py:13`: `from serve import audit` → `from lib import audit`
- `src/serve/runner.py:33`: `from serve import audit` → `from lib import audit`

And in tests replace `from serve.audit import …` with `from lib.audit import …` at every site listed in Files above; `tests/integration/test_pipeline.py:16` becomes `import lib.audit as audit_module`; `tests/integration/test_delta_probe.py:12` becomes `from lib import audit`.

- [ ] **Step 4: Break the store↔hydrate runtime cycle**

The `TYPE_CHECKING` guard added in Task 6 Step 2 already removes `store/lifecycle.py`'s runtime import of `hydrate.repo_client`. Verify with:

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_import_boundaries.py -q`
Expected: PASS.

Run: `.\.venv\Scripts\python.exe -m mypy`
Expected: `Success: no issues found in 13 source files` (the moved `lib/audit.py` joins the scope).

- [ ] **Step 5: Run the affected tests and the full suite**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_audit.py tests/integration/test_audit_db.py tests/integration/test_pipeline.py tests/integration/test_lifecycle.py tests/integration/test_runner.py tests/contract/test_github_pagination.py tests/unit/test_trees_first.py tests/integration/test_delta_probe.py -q`
Expected: PASS, including `test_audit_hook_exception_propagates` (its monkeypatch target moves with the module).

Run: `.\.venv\Scripts\python.exe -m pytest -q`
Expected: exit 0, no `F`/`E`.

- [ ] **Step 6: Commit**

```bash
git add src/lib/audit.py src/serve/audit.py src/discover src/enrich src/hydrate src/serve/runner.py tests/unit/test_import_boundaries.py tests/unit/test_audit.py tests/unit/test_trees_first.py tests/integration/test_audit_db.py tests/integration/test_pipeline.py tests/integration/test_lifecycle.py tests/integration/test_runner.py tests/integration/test_delta_probe.py tests/contract/test_github_pagination.py
git commit -m "refactor: move audit to lib and enforce import boundaries"
```

---

### Task 8: Record the program entry points and gates in the development log

**Files:**
- Modify: `docs/development-log.md` (append one section before `## Repo/git facts`)

**Interfaces:** Documentation only; no code interfaces.

- [ ] **Step 1: Append the quality-hardening record**

Add this section immediately before `## Repo/git facts`:

````markdown
## Quality hardening program (2026-10-01)

**Entry points:** `docs/superpowers/specs/2026-10-01-quality-hardening-design.md` (freeze contract, phases, acceptance), with plans `2026-10-01-ci-and-regression-harness.md` (Phase 0), `2026-10-01-zero-behavior-fixes.md` (Phase 1a), `2026-10-01-test-and-tooling-hardening.md` (Phase 1b), `2026-10-01-adversarial-hardening.md` (Phase 2), `2026-10-01-ux-polish.md` (Phase 3).

**Gates added by Phase 1b:**

- Fixtures: all 19 duplicated `clean` fixtures plus `test_executor.py::db` now call the `clean_db` factory in `tests/conftest.py`; `tests/unit/test_fixture_centralization.py` rejects raw `TRUNCATE TABLE` outside the factory and three allowlisted single-purpose fixtures.
- CWD: `tests/conftest.py::repo_root` anchors asset paths; `tests/unit/test_cwd_independence.py` runs representative tests from a foreign directory and rejects relative `Path("…")` literals.
- Flakes: no wall-clock assertions remain; ordering tests use threads events/barriers and an append-operation counter (`test_id_accumulation.py`, `test_executor.py`, `test_cloner.py`, `test_filter_form.py`).
- JS: pure helpers live in `src/serve/static/applib.js`; `tests/js/applib.test.mjs` + `tests/unit/test_applib_js.py` cover the row cache, clone-start guard, clone limit parsing, editable-target detection, and toast messages.
- Quarantine: `tests/quarantine_manifest.txt` lists every intentionally-unwired module/function; `tests/unit/test_quarantine_manifest.py` fails when a `src` module loses test coverage or a manifest entry loses its test.
- Types: `mypy` (pinned in `requirements-dev.txt`; config in `pyproject.toml`) gates `src/lib`, `src/limiter`, `src/store`, `src/scheduler` with `disallow_untyped_defs` for `limiter.buckets`/`limiter.classifier`; ratchet policy in the config comments; `Deps.token_id` renamed to `Deps.token_fp`.
- Layering: `src/lib/audit.py` (was `src/serve/audit.py`) is core's telemetry dependency; `tests/unit/test_import_boundaries.py` enforces core-never-imports-`serve` and breaks the `store`↔`hydrate` runtime cycle.

**Run the gates:**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m mypy
node --test tests/js/rownav.test.mjs tests/js/applib.test.mjs
```
````

- [ ] **Step 2: Verify the log diff and the gates**

Run: `git diff --stat docs/development-log.md` and read the section for accuracy.
Run: `.\.venv\Scripts\python.exe -m pytest -q` (expect exit 0) and `.\.venv\Scripts\python.exe -m mypy` (expect success).

- [ ] **Step 3: Commit**

```bash
git add docs/development-log.md
git commit -m "docs: record quality-hardening program entry points and gates"
```

---

## Spec Coverage

| Spec item | Task |
|---|---|
| §5.8 Layering (audit extraction, store↔hydrate cycle, import-boundary test) | Task 7 (cycle guard written in Task 6 Step 2) |
| §5.9 Dead-surface quarantine (test every unwired module, manifest gate) | Task 5 |
| §5.10 Types (mypy in dev deps + config, core packages, `Deps.token_id`, untyped helpers, ratchet) | Task 6 |
| §5.11 Test infrastructure — fixture centralization | Task 1 |
| §5.11 Test infrastructure — CWD independence | Task 2 |
| §5.11 Test infrastructure — wall-clock flakes | Task 3 |
| §5.11 Test infrastructure — JS helpers | Task 4 |
| §8 Ops/DX — development log updated | Task 8 |
| §2 INV-1/INV-4 — no runtime behavior change | Tasks 1, 2, 3, 5, 6, 7, 8 (test/tooling only); Task 4 keeps app.js behavior identical and adds one additive script tag |
| §8 Tests — zero skips, no wall-clock flake assertions, JS helpers tested, quarantine tested | Tasks 1-5 |

## Risks / Known Unknowns

- **Duplicate count nuance:** `rg "def clean" tests` is exactly 19 files; `rg "TRUNCATE" tests` is 23 files. Four sites are not `clean`/seed fixtures (live `test_golden_org.py`, `test_state_machine.py::store`, inline `test_since_scan.py`, and `tests/conftest.py` after migration) and are allowlisted. `test_executor.py::db` is migrated as the 20th clean-shaped fixture. The plan does not touch the three single-purpose sites.
- **Fixture equivalence:** the factory truncates all nine tables where some fixtures truncated a subset. Superset cleanup cannot hide a missing seed because every test creates its own rows; the unchanged 1089-test suite is the oracle. The Core-insert path for array/timestamp/JSON columns was prototyped against the local PostgreSQL 18 test database.
- **Phase 1a overlap:** `docs/superpowers/plans/2026-10-01-zero-behavior-fixes.md` exists (written 2026-10-01; not yet executed). It touches `src/discover/pipeline.py`, `src/store/upserts.py`, `tests/integration/test_pipeline.py`, and `tests/integration/test_upserts.py` (verified against its File Structure table) and explicitly leaves §5.8 layering to this plan, so Task 7 executes as written. Run Phase 1a first and rebase this plan; the `Deps` rename (Task 6) also conflicts textually with pipeline edits.
- **Phase 0 not present:** no CI workflow, golden harness, or coverage floor exists yet. This plan adds no workflow edits; when Phase 0's workflow lands it must add `mypy` (config is picked up by running `mypy` with no args). If Phase 0's HTML snapshots already exist, Task 4's `base.html` change is a one-line insertion and must be accepted under the spec's insertion-only review rule.
- **"Sort-link handling" has no JS:** `app.js` contains no sort-link code; sorting is server-rendered in `templates/partials/table.html` and swaps via htmx. Task 4 therefore tests the row-cache invalidation that sort swaps depend on (`createRowCache` + `htmx:afterSwap` wiring) rather than inventing a new URL helper.
- **Mypy scope is deliberately partial:** `follow_imports = "silent"` hides known errors in unscoped modules (e.g. `discover/search_shards.py:107`, `hydrate/repo_client.py:103-106`, `serve/runner.py:123`). Removing `silent` is the documented end state, not this plan.
- **Quarantine gate is static:** it verifies a test *references* each manifest entry, not that the test runs (integration entries may skip without `TEST_DATABASE_URL`). Module coverage requires a direct test import, so a module exercised only transitively through another module is still flagged — that is the intended conservative behavior.
- **Node dependence:** `tests/unit/test_rownav_js.py`, `tests/unit/test_applib_js.py`, and one CWD subprocess test skip when `node` is absent. Node 26 is installed locally; Phase 0's zero-skip acceptance requires CI to install Node.
- **Secrets in env:** the environment exports `GITHUB_TOKEN`; `tests/unit/test_skeleton.py` deletes it before calling `build_headers()` and never prints header values.
- **Pytest summary suppression:** pytest 9.1.1 in this environment omits the trailing `N passed` line when the output is not a TTY; expected outputs above therefore use the exit code plus the absence of `F`/`E`. Baseline: 1089 tests, 0 failures, 0 errors, 0 skips (verified with `--junitxml`).
