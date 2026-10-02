# Console Settings Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/settings` console page that persists and edits the run limits, request deadline, GraphQL batching knobs, and limiter concurrency in the database, and makes every new run honor them — so the per-run caps that were test guardrails become operator-controlled, with environment variables still able to pin any field.

**Architecture:** A single-row `app_settings` table (migration `0008`) seeded with today's values; `src/store/settings.py` owns the `RunSettings` dataclass, env-override resolution (env → row → default), and updates. `src/serve/settings_spec.py` validates the form (mirroring `filter_spec`), `src/serve/settings.py` renders/saves the page (CSRF + audit), and `src/serve/runner.py` maps `RunSettings` → `RunnerConfig` so each submitted run snapshots the settings at start. `fetch_batch` gains `allow_requests=False` to mean "fallback-only", which is how the batching toggle is honored without deleting the engine.

**Tech Stack:** Python 3.12, FastAPI + Jinja2, SQLAlchemy 2 + Alembic (Postgres), pytest, ruff, black.

**Spec:** The approved design in the 2026-10-02 session; this plan **depends on** `docs/superpowers/plans/2026-10-02-graphql-batch-engine.md` being executed first (the batching knobs only exist after it).

## Global Constraints

- Python `>=3.12`; line length 100; ruff (`E,F,I,UP,B`) + black clean; coverage floor `fail_under = 93`.
- DB tests require `TEST_DATABASE_URL` ending in `_test` (or `GITCRAWL_REQUIRE_TEST_DB=1`); use `tests/conftest.py` fixtures.
- Run tests with: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest <path> -q` (Windows PowerShell).
- Migration `0008` (down_revision `0007`) is claimed here. Numbers after this plan: the UX redesign's corpora work takes `0009`; the deferred agent-detection plan renumbers to `0010` when/if executed — record this in `docs/development-log.md`.
- Resolution precedence per field: **environment override → database row → built-in default.** Defaults equal today's values (`10/500/200/100`, deadline `3600`, batching on, batch size `20`, concurrency `10`), so nothing changes until an operator edits.
- Settings apply to **new** runs only: values are read once at submit; a running run is never mutated.
- Tokens are never stored, accepted, or rendered: the page shows a present/absent badge only (`docs/legal-gates.md`).
- Validation bounds: `max_shards` 1–10,000; `max_candidates`/`max_hydrate`/`max_enrich` 1–1,000,000; `request_deadline_seconds` 60–86,400; `graphql_batch_size` 1–20; `limiter_max_concurrent` 1–100; `max_hydrate ≤ max_candidates`.
- All commits: conventional prefixes (`migration:`, `feat:`, `test:`, `docs:`).

---

## AMENDMENTS (2026-10-02 UX alignment — apply while executing; these supersede conflicting steps below)

The UX redesign is approved (`design/console-ux-redesign.md`). Settings becomes **System → Limits** and its labels become plain language. Apply:

1. **Nav (Task 3 Step 5):** do NOT add a "Settings" nav link. Add `<a href="/settings">System</a>` in `base.html` instead. The UX plan later replaces it with `/system`; `/settings` survives as the Limits page behind it. Contract tests assert the page is reachable and titled plainly; they should not assert a "Settings" nav label.
2. **Page heading and labels (Task 3 Step 4):** the page is titled **“Limits”** with the subtitle “Run limits for new searches. Part of System.” Replace raw field-name labels with:

   | field (`name` attribute unchanged) | label | help line under the input |
   |---|---|---|
   | `max_shards` | Search breadth (slices) | How many slices a live search may be split into. Broader = longer. |
   | `max_candidates` | Repos to keep per search | How many matching repos the search holds on to. |
   | `max_hydrate` | Repos to save details for | How many of those get current details fetched (uses your allowance). Cannot exceed “Repos to keep”. |
   | `max_enrich` | Extra rules to check | How many repos get file and country checks. |
   | `request_deadline_seconds` | Stop a search after (seconds) | Long searches abort past this; raise it for corpus builds. |
   | `graphql_batch` | Batch repo lookups | Faster saving with fewer requests. Off = one request per repo. |
   | `graphql_batch_size` | Repos per batch | Keep at 20 or below; GitHub cuts off large batches. |
   | `limiter_max_concurrent` | Simultaneous requests | How many requests run at once. Keep well under GitHub’s ceiling of 100. |

   Section headings become **“Search limits”**, **“Faster saving (batching)”**, **“Request pacing”**. All `name` attributes, ids, and validation stay exactly as the plan specifies.
3. **Contract tests (Task 3 Step 1):** additionally assert the plain labels (`"Search breadth (slices)"`, `"Repos to save details for"`) and the help text `"keep well under GitHub’s ceiling"` appear; keep every existing behavioral assertion.
4. **Docs (Task 5):** refer to “System → Limits (the `/settings` page)” instead of “Settings page”. The env table stays.
5. **Migration note (Task 1/5):** settings is `0008`; UX corpora is `0009`; detection is `0010`.

---

## File Structure

**Create**

- `migrations/versions/0008_app_settings.py` — single-row settings table.
- `src/store/settings.py` — `RunSettings`, defaults, env overrides, `load_run_settings`, `update_run_settings`, `env_pinned_fields`.
- `src/serve/settings_spec.py` — `SettingsError`, `parse_settings_form`.
- `src/serve/settings.py` — `register_settings` routes, `record_settings_change` audit helper.
- `src/serve/templates/settings.html`
- `tests/unit/test_run_settings.py`
- `tests/unit/test_settings_spec.py`
- `tests/integration/test_settings_store.py`
- `tests/contract/test_settings.py`

**Modify**

- `src/store/models.py` — `AppSettings` ORM model.
- `src/serve/app.py` — register settings; default runner reads settings per run.
- `src/serve/runner.py` — `RunnerConfig` (+`request_deadline_seconds`, `graphql_batch`, `graphql_batch_size`), `runner_config_from`, `build_deps(max_concurrent=...)`, `run_filter` deadline from config; thread batch size/flag into `_hydrate`/file/geo paths.
- `src/lib/graphql_batch.py` — `allow_requests` parameter.
- `src/hydrate/tail.py` — `refresh_repos_batched(batch_size=..., allow_requests=...)`.
- `src/serve/templates/base.html` — nav link.
- `tests/integration/test_models_migrations.py` — `app_settings` in `TABLES`/`EXPECTED_COLUMNS`.
- `tests/unit/test_graphql_batch_core.py` — `allow_requests=False` test.
- `tests/integration/test_runner.py` — settings-driven cap test.
- `tests/golden/conftest.py` + snapshots — `/settings` case; `openapi.sha256` re-pin.
- `docs/environment.md`, `docs/development-log.md` — corpus profile and outcome.

---

## Task 1: Settings persistence — migration 0008, model, resolution

**Files:**
- Create: `migrations/versions/0008_app_settings.py`, `src/store/settings.py`
- Modify: `src/store/models.py`
- Test: `tests/integration/test_settings_store.py`, `tests/unit/test_run_settings.py`, `tests/integration/test_models_migrations.py`

**Interfaces:**
- Produces (used by Tasks 2–4):
  - `@dataclass(frozen=True) class RunSettings` with fields `max_shards=10`, `max_candidates=500`, `max_hydrate=200`, `max_enrich=100`, `request_deadline_seconds=3600`, `graphql_batch=True`, `graphql_batch_size=20`, `limiter_max_concurrent=10` and `as_dict()`.
  - `SETTINGS_ENV: Mapping[str, str]` mapping field → env var.
  - `env_pinned_fields() -> frozenset[str]`.
  - `load_run_settings(engine) -> RunSettings` (row → env overlay → defaults).
  - `update_run_settings(engine, values: Mapping[str, object]) -> RunSettings` (ignores unknown keys; raises `KeyError` for unknown).
  - ORM `store.models.AppSettings`.

- [ ] **Step 1: Write the failing store tests**

Create `tests/integration/test_settings_store.py`:

```python
from __future__ import annotations

import pytest
from alembic import command

from store.settings import RunSettings, load_run_settings, update_run_settings


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(clean_db):
    return clean_db()


def test_defaults_are_seeded_by_the_migration(clean):
    assert load_run_settings(clean) == RunSettings()


def test_update_persists_and_returns_effective_settings(clean):
    updated = update_run_settings(clean, {"max_hydrate": 50, "graphql_batch": False})
    assert updated.max_hydrate == 50
    assert updated.graphql_batch is False
    assert load_run_settings(clean) == updated


def test_env_overrides_the_row(clean, monkeypatch):
    update_run_settings(clean, {"max_shards": 25})
    monkeypatch.setenv("GITCRAWL_MAX_SHARDS", "3")
    settings = load_run_settings(clean)
    assert settings.max_shards == 3


def test_invalid_env_value_falls_back_to_the_row(clean, monkeypatch):
    update_run_settings(clean, {"max_shards": 25})
    monkeypatch.setenv("GITCRAWL_MAX_SHARDS", "not-a-number")
    assert load_run_settings(clean).max_shards == 25


def test_unknown_field_is_rejected(clean):
    with pytest.raises(KeyError):
        update_run_settings(clean, {"nope": 1})
```

Create `tests/unit/test_run_settings.py`:

```python
from __future__ import annotations

from store.settings import SETTINGS_ENV, RunSettings, env_pinned_fields


def test_settings_env_names_are_stable():
    assert SETTINGS_ENV["max_shards"] == "GITCRAWL_MAX_SHARDS"
    assert SETTINGS_ENV["request_deadline_seconds"] == "GITCRAWL_REQUEST_DEADLINE_SECONDS"
    assert SETTINGS_ENV["graphql_batch"] == "GITCRAWL_GRAPHQL_BATCH"
    assert SETTINGS_ENV["limiter_max_concurrent"] == "GITCRAWL_MAX_CONCURRENT"


def test_env_pinned_fields_lists_only_set_variables(monkeypatch):
    monkeypatch.delenv("GITCRAWL_MAX_SHARDS", raising=False)
    monkeypatch.setenv("GITCRAWL_GRAPHQL_BATCH", "0")
    assert env_pinned_fields() == frozenset({"graphql_batch"})


def test_as_dict_round_trips():
    settings = RunSettings()
    assert RunSettings(**settings.as_dict()) == settings
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/integration/test_settings_store.py tests/unit/test_run_settings.py -q`
Expected: FAIL (`ModuleNotFoundError: store.settings` / no table).

- [ ] **Step 3: Write migration `0008_app_settings.py`**

```python
"""app settings: single-row operator-tunable run limits

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_DEFAULTS = {
    "max_shards": "10",
    "max_candidates": "500",
    "max_hydrate": "200",
    "max_enrich": "100",
    "request_deadline_seconds": "3600",
    "graphql_batch_size": "20",
    "limiter_max_concurrent": "10",
}


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False, nullable=False),
        *(
            sa.Column(name, sa.Integer(), nullable=False, server_default=sa.text(value))
            for name, value in _DEFAULTS.items()
        ),
        sa.Column(
            "graphql_batch",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("id = 1", name="app_settings_single_row"),
        sa.CheckConstraint(
            "graphql_batch_size BETWEEN 1 AND 20", name="app_settings_batch_size"
        ),
        sa.CheckConstraint(
            "limiter_max_concurrent BETWEEN 1 AND 100", name="app_settings_concurrency"
        ),
    )
    op.execute("INSERT INTO app_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING")


def downgrade() -> None:
    op.drop_table("app_settings")
```

- [ ] **Step 4: Add the ORM model to `src/store/models.py`**

Add `CheckConstraint` to the existing `sqlalchemy` import, then append near `SavedFilter`:

```python
class AppSettings(Base):
    __tablename__ = "app_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="app_settings_single_row"),
        CheckConstraint("graphql_batch_size BETWEEN 1 AND 20", name="app_settings_batch_size"),
        CheckConstraint(
            "limiter_max_concurrent BETWEEN 1 AND 100", name="app_settings_concurrency"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    max_shards: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("10"))
    max_candidates: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("500")
    )
    max_hydrate: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("200"))
    max_enrich: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    request_deadline_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3600")
    )
    graphql_batch: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    graphql_batch_size: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("20")
    )
    limiter_max_concurrent: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("10")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
```

- [ ] **Step 5: Implement `src/store/settings.py`**

```python
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Engine

from store.models import AppSettings

SETTINGS_ENV: Mapping[str, str] = {
    "max_shards": "GITCRAWL_MAX_SHARDS",
    "max_candidates": "GITCRAWL_MAX_CANDIDATES",
    "max_hydrate": "GITCRAWL_MAX_HYDRATE",
    "max_enrich": "GITCRAWL_MAX_ENRICH",
    "request_deadline_seconds": "GITCRAWL_REQUEST_DEADLINE_SECONDS",
    "graphql_batch": "GITCRAWL_GRAPHQL_BATCH",
    "graphql_batch_size": "GITCRAWL_GRAPHQL_BATCH_SIZE",
    "limiter_max_concurrent": "GITCRAWL_MAX_CONCURRENT",
}

_BOOL_FIELDS = frozenset({"graphql_batch"})


@dataclass(frozen=True)
class RunSettings:
    max_shards: int = 10
    max_candidates: int = 500
    max_hydrate: int = 200
    max_enrich: int = 100
    request_deadline_seconds: int = 3600
    graphql_batch: bool = True
    graphql_batch_size: int = 20
    limiter_max_concurrent: int = 10

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _parse_env(field: str, raw: str) -> object | None:
    value = raw.strip()
    if not value:
        return None
    if field in _BOOL_FIELDS:
        lowered = value.lower()
        if lowered in {"1", "true", "on", "yes"}:
            return True
        if lowered in {"0", "false", "off", "no"}:
            return False
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _env_overrides() -> dict[str, object]:
    overrides: dict[str, object] = {}
    for field, variable in SETTINGS_ENV.items():
        raw = os.environ.get(variable)
        if raw is None:
            continue
        parsed = _parse_env(field, raw)
        if parsed is not None:
            overrides[field] = parsed
    return overrides


def env_pinned_fields() -> frozenset[str]:
    return frozenset(
        field for field, variable in SETTINGS_ENV.items() if os.environ.get(variable) is not None
    )


def load_run_settings(engine: Engine) -> RunSettings:
    with engine.connect() as connection:
        row = (
            connection.execute(select(AppSettings).where(AppSettings.id == 1))
            .mappings()
            .one_or_none()
        )
    values = dict(row) if row is not None else {}
    values.pop("id", None)
    values.pop("updated_at", None)
    fields = RunSettings.__dataclass_fields__
    valid = {field: values[field] for field in fields if field in values}
    settings = RunSettings(**valid)
    overrides = _env_overrides()
    return RunSettings(**{**settings.as_dict(), **overrides}) if overrides else settings


def update_run_settings(engine: Engine, values: Mapping[str, object]) -> RunSettings:
    unknown = set(values) - set(RunSettings.__dataclass_fields__)
    if unknown:
        raise KeyError(sorted(unknown)[0])
    with engine.begin() as connection:
        result = connection.execute(
            update(AppSettings)
            .where(AppSettings.id == 1)
            .values(**values, updated_at=func.now())
        )
        if not result.rowcount:
            connection.execute(insert(AppSettings).values(id=1, **values))
    return load_run_settings(engine)
```

- [ ] **Step 6: Reset settings between DB tests**

Settings live in the database, and `clean_db` currently truncates everything except them — which would leak values between tests. In `tests/conftest.py`, add `"app_settings"` to `FULL_TRUNCATE_TABLES` and reseed the default row in the `clean_db` factory, immediately after the `TRUNCATE` statement:

```python
FULL_TRUNCATE_TABLES = (
    "app_settings",
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
```

```python
        with alembic_engine.begin() as connection:
            connection.execute(
                text(
                    "TRUNCATE TABLE "
                    + ", ".join(FULL_TRUNCATE_TABLES)
                    + " RESTART IDENTITY CASCADE"
                )
            )
            connection.execute(text("INSERT INTO app_settings (id) VALUES (1)"))
```

Also add the same truncate+reseed line to the golden seed in `tests/golden/conftest.py` (extend `GOLDEN_SEED_SQL` with `"INSERT INTO app_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING"` and add `app_settings` to its `TRUNCATE` list) so golden snapshots are deterministic.

- [ ] **Step 7: Update the migration schema test**

In `tests/integration/test_models_migrations.py`, add `"app_settings"` to `TABLES` and this entry to `EXPECTED_COLUMNS`:

```python
    "app_settings": {
        "id": False,
        "max_shards": False,
        "max_candidates": False,
        "max_hydrate": False,
        "max_enrich": False,
        "request_deadline_seconds": False,
        "graphql_batch": False,
        "graphql_batch_size": False,
        "limiter_max_concurrent": False,
        "updated_at": False,
    },
```

- [ ] **Step 8: Run the tests**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/integration/test_settings_store.py tests/unit/test_run_settings.py tests/integration/test_models_migrations.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add migrations/versions/0008_app_settings.py src/store/models.py src/store/settings.py tests/integration/test_settings_store.py tests/unit/test_run_settings.py tests/integration/test_models_migrations.py
git commit -m "migration: add single-row app settings table"
```

---

## Task 2: Settings form validation

**Files:**
- Create: `src/serve/settings_spec.py`
- Test: `tests/unit/test_settings_spec.py`

**Interfaces:**
- Consumes: nothing.
- Produces (used by Task 3): `SettingsError(errors, hints)`; `parse_settings_form(form: Mapping[str, str], *, pinned: frozenset[str] = frozenset()) -> dict[str, object]` returning only the writable, validated fields (checkbox `graphql_batch` parsed from `"on"`; a `reset=1` entry returns `{}` so the caller writes defaults).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_settings_spec.py`:

```python
from __future__ import annotations

import pytest

from serve.settings_spec import SettingsError, parse_settings_form


def form(**overrides: str) -> dict[str, str]:
    values = {
        "max_shards": "10",
        "max_candidates": "500",
        "max_hydrate": "200",
        "max_enrich": "100",
        "request_deadline_seconds": "3600",
        "graphql_batch": "on",
        "graphql_batch_size": "20",
        "limiter_max_concurrent": "10",
    }
    values.update(overrides)
    return values


def test_parse_accepts_valid_form():
    parsed = parse_settings_form(form())
    assert parsed["max_hydrate"] == 200
    assert parsed["graphql_batch"] is True


def test_unchecked_checkbox_is_false():
    values = form()
    values.pop("graphql_batch")
    assert parse_settings_form(values)["graphql_batch"] is False


def test_out_of_range_value_is_rejected_with_a_hint():
    with pytest.raises(SettingsError) as excinfo:
        parse_settings_form(form(graphql_batch_size="21"))
    assert any("graphql_batch_size" in error for error in excinfo.value.errors)
    assert excinfo.value.hints


def test_non_integer_is_rejected():
    with pytest.raises(SettingsError):
        parse_settings_form(form(max_shards="lots"))


def test_hydrate_above_candidates_is_rejected():
    with pytest.raises(SettingsError) as excinfo:
        parse_settings_form(form(max_candidates="100", max_hydrate="200"))
    assert any("max_hydrate" in error for error in excinfo.value.errors)


def test_pinned_fields_are_ignored():
    parsed = parse_settings_form(form(max_shards="999"), pinned=frozenset({"max_shards"}))
    assert "max_shards" not in parsed


def test_reset_returns_empty_mapping():
    assert parse_settings_form(form(reset="1")) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_settings_spec.py -q`
Expected: FAIL (`ModuleNotFoundError: serve.settings_spec`).

- [ ] **Step 3: Implement `src/serve/settings_spec.py`**

```python
from __future__ import annotations

from collections.abc import Mapping

_BOUNDS: dict[str, tuple[int, int]] = {
    "max_shards": (1, 10_000),
    "max_candidates": (1, 1_000_000),
    "max_hydrate": (1, 1_000_000),
    "max_enrich": (1, 1_000_000),
    "request_deadline_seconds": (60, 86_400),
    "graphql_batch_size": (1, 20),
    "limiter_max_concurrent": (1, 100),
}


class SettingsError(ValueError):
    def __init__(self, errors, hints=()):
        self.errors = tuple(errors)
        self.hints = tuple(hints)
        super().__init__("; ".join(self.errors))


def parse_settings_form(
    form: Mapping[str, str], *, pinned: frozenset[str] = frozenset()
) -> dict[str, object]:
    if form.get("reset") == "1":
        return {}
    errors: list[str] = []
    values: dict[str, object] = {}
    for field, (low, high) in _BOUNDS.items():
        if field in pinned:
            continue
        raw = str(form.get(field, "")).strip()
        try:
            parsed = int(raw)
        except ValueError:
            errors.append(f"`{field}` must be an integer between {low} and {high}")
            continue
        if not low <= parsed <= high:
            errors.append(f"`{field}` must be between {low} and {high}")
            continue
        values[field] = parsed
    if "graphql_batch" not in pinned:
        values["graphql_batch"] = form.get("graphql_batch") in {"on", "1", "true"}
    hydrate = values.get("max_hydrate")
    candidates = values.get("max_candidates")
    if isinstance(hydrate, int) and isinstance(candidates, int) and hydrate > candidates:
        errors.append("`max_hydrate` must not exceed `max_candidates`")
    if errors:
        raise SettingsError(
            errors, ("raise `max_candidates` or lower `max_hydrate`",)
        )
    return values
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_settings_spec.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/serve/settings_spec.py tests/unit/test_settings_spec.py
git commit -m "feat: settings form validation"
```

---

## Task 3: Settings page, save route, audit, navigation

**Files:**
- Create: `src/serve/settings.py`, `src/serve/templates/settings.html`
- Modify: `src/serve/app.py` (register call), `src/serve/templates/base.html` (nav)
- Test: `tests/contract/test_settings.py`, `tests/golden/conftest.py`, `tests/golden/snapshots/`

**Interfaces:**
- Consumes: Task 1 (`RunSettings`, `load_run_settings`, `update_run_settings`, `env_pinned_fields`) and Task 2 (`SettingsError`, `parse_settings_form`).
- Produces:
  - `GET /settings` → `settings.html` with `values`, `defaults`, `pinned`, `token_present`, `errors`, `hints`, `saved`.
  - `POST /settings` → CSRF-checked; writes; audit row; `303 /settings?saved=1`; invalid → `400` re-render with errors/hints.
  - `record_settings_change(engine, before: Mapping, after: Mapping) -> None`.

- [ ] **Step 1: Write the failing contract tests**

Create `tests/contract/test_settings.py`. Copy the `schema`/`clean` fixtures and `make_client`/`healthy_client` helpers from `tests/contract/test_pages.py:41-122` verbatim (adjusting imports), then add:

```python
from sqlalchemy import text

from store.settings import load_run_settings


def csrf_token(client) -> str:
    response = client.get("/")
    marker = 'name="csrf-token" content="'
    start = response.text.index(marker) + len(marker)
    return response.text[start : response.text.index('"', start)]


def test_settings_page_renders_current_values(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get("/settings")
    assert response.status_code == 200
    assert "max_hydrate" in response.text
    assert "limiter_max_concurrent" in response.text
    assert "applies to new runs" in response.text


def test_settings_page_never_renders_the_token(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)  # sets GITHUB_TOKEN=super-secret-token-value
    response = client.get("/settings")
    assert "super-secret-token-value" not in response.text


def test_post_saves_values_and_redirects(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    token = csrf_token(client)
    response = client.post(
        "/settings",
        data={
            "max_shards": "50",
            "max_candidates": "5000",
            "max_hydrate": "4000",
            "max_enrich": "1000",
            "request_deadline_seconds": "7200",
            "graphql_batch": "on",
            "graphql_batch_size": "10",
            "limiter_max_concurrent": "4",
        },
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1"
    saved = load_run_settings(clean)
    assert saved.max_shards == 50
    assert saved.max_hydrate == 4000
    assert saved.limiter_max_concurrent == 4


def test_invalid_settings_are_a_local_400(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    token = csrf_token(client)
    response = client.post(
        "/settings",
        data={"max_shards": "0", "graphql_batch_size": "99"},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "max_shards" in response.text


def test_missing_csrf_is_403(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.post("/settings", data={"max_shards": "5"}, follow_redirects=False)
    assert response.status_code == 403


def test_save_writes_an_audit_row(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    token = csrf_token(client)
    client.post(
        "/settings",
        data={
            "max_shards": "10",
            "max_candidates": "500",
            "max_hydrate": "200",
            "max_enrich": "100",
            "request_deadline_seconds": "3600",
            "graphql_batch": "on",
            "graphql_batch_size": "20",
            "limiter_max_concurrent": "10",
        },
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    with clean.connect() as connection:
        params = connection.scalar(
            text("SELECT params FROM audit_log WHERE token_fp = 'settings' ORDER BY id DESC LIMIT 1")
        )
    assert params["app_settings"]["after"]["max_shards"] == 10
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/contract/test_settings.py -q`
Expected: FAIL (404 on `/settings`).

- [ ] **Step 3: Implement `src/serve/settings.py`**

```python
from __future__ import annotations

from collections.abc import Callable, Mapping

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, insert
from sqlalchemy.engine import Engine

from lib.audit import query_hash
from lib.gh_client import load_tokens
from serve import pages
from serve.settings_spec import SettingsError, parse_settings_form
from store.models import AuditLog
from store.settings import (
    RunSettings,
    env_pinned_fields,
    load_run_settings,
    update_run_settings,
)

_DEFAULTS = RunSettings()


def record_settings_change(
    engine: Engine, before: Mapping[str, object], after: Mapping[str, object]
) -> None:
    params = {"app_settings": {"before": dict(before), "after": dict(after)}}
    with engine.begin() as connection:
        connection.execute(
            insert(AuditLog).values(
                ts=func.now(),
                query_hash=query_hash(params),
                params=params,
                status=200,
                token_fp="settings",
                latency_ms=0,
            )
        )


def _render(
    request: Request,
    engine: Engine,
    *,
    token_present: Callable[[], bool],
    status_code: int = 200,
    values: RunSettings | None = None,
    errors: tuple[str, ...] = (),
    hints: tuple[str, ...] = (),
    saved: bool = False,
):
    effective = values if values is not None else load_run_settings(engine)
    return pages._templates.TemplateResponse(
        request,
        "settings.html",
        {
            "values": effective.as_dict(),
            "defaults": _DEFAULTS.as_dict(),
            "pinned": env_pinned_fields(),
            "token_present": bool(token_present()),
            "errors": list(errors),
            "hints": list(hints),
            "saved": saved,
            "csrf_token": request.state.csrf_token,
        },
        status_code=status_code,
    )


def register_settings(
    application: FastAPI,
    *,
    engine_factory: Callable[[], Engine],
    token_present: Callable[[], bool] | None = None,
) -> None:
    def token_ok() -> bool:
        if token_present is None:
            return bool(load_tokens())
        return bool(token_present())

    @application.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, saved: int = 0):
        return _render(request, engine_factory(), token_present=token_ok, saved=bool(saved))

    @application.post("/settings")
    async def settings_save(request: Request):
        if not await pages.validate_csrf(request):
            return HTMLResponse("CSRF", status_code=403)
        engine = engine_factory()
        before = load_run_settings(engine)
        pinned = env_pinned_fields()
        form = await request.form()
        flat = {key: str(value) for key, value in form.items() if isinstance(value, str)}
        try:
            parsed = parse_settings_form(flat, pinned=pinned)
        except SettingsError as exc:
            return _render(
                request,
                engine,
                token_present=token_ok,
                status_code=400,
                errors=exc.errors,
                hints=exc.hints,
            )
        if not parsed:
            parsed = {k: v for k, v in _DEFAULTS.as_dict().items() if k not in pinned}
        after = update_run_settings(engine, parsed)
        record_settings_change(engine, before.as_dict(), after.as_dict())
        return RedirectResponse("/settings?saved=1", status_code=303)
```

(The error re-render shows the stored values and names the bad field in the error list — the same simplicity level as the filter form's local-400 path.)

- [ ] **Step 4: Create `src/serve/templates/settings.html`**

```html
{% extends "base.html" %}
{% block body %}
<section class="panel">
  <h1>Settings</h1>
  <p class="muted">
    Applies to new runs only. Values set by environment variables are pinned and shown read-only.
  </p>
  {% if saved %}<p class="banner" role="status">Settings saved.</p>{% endif %}
  {% if errors %}
  <ul class="errors">
    {% for error in errors %}<li>{{ error }}</li>{% endfor %}
  </ul>
  {% endif %}
  {% if hints %}
  <ul class="hints">
    {% for hint in hints %}<li>{{ hint }}</li>{% endfor %}
  </ul>
  {% endif %}
  <p class="badge">
    GitHub token: <strong>{{ "present" if token_present else "missing" }}</strong>
  </p>
  <form method="post" action="/settings">
    <input type="hidden" name="csrf" value="{{ csrf_token }}">
    <h2>Run limits</h2>
    {% for field in ("max_shards", "max_candidates", "max_hydrate", "max_enrich") %}
    <label>
      {{ field }}
      <input type="number" name="{{ field }}" value="{{ values[field] }}"
             {% if field in pinned %}readonly{% endif %}>
      {% if field in pinned %}<span class="badge">set by environment</span>{% endif %}
    </label>
    {% endfor %}
    <label>
      request_deadline_seconds
      <input type="number" name="request_deadline_seconds"
             value="{{ values["request_deadline_seconds"] }}"
             {% if "request_deadline_seconds" in pinned %}readonly{% endif %}>
      {% if "request_deadline_seconds" in pinned %}<span class="badge">set by environment</span>{% endif %}
    </label>
    <h2>Batching</h2>
    <label>
      <input type="checkbox" name="graphql_batch" {% if values["graphql_batch"] %}checked{% endif %}
             {% if "graphql_batch" in pinned %}disabled{% endif %}>
      graphql_batch
      {% if "graphql_batch" in pinned %}<span class="badge">set by environment</span>{% endif %}
    </label>
    <label>
      graphql_batch_size
      <input type="number" name="graphql_batch_size" value="{{ values["graphql_batch_size"] }}"
             {% if "graphql_batch_size" in pinned %}readonly{% endif %}>
      {% if "graphql_batch_size" in pinned %}<span class="badge">set by environment</span>{% endif %}
    </label>
    <h2>Concurrency</h2>
    <label>
      limiter_max_concurrent
      <input type="number" name="limiter_max_concurrent" value="{{ values["limiter_max_concurrent"] }}"
             {% if "limiter_max_concurrent" in pinned %}readonly{% endif %}>
      {% if "limiter_max_concurrent" in pinned %}<span class="badge">set by environment</span>{% endif %}
    </label>
    <div class="actions">
      <button type="submit">Save</button>
      <button type="submit" name="reset" value="1">Reset to defaults</button>
    </div>
  </form>
</section>
{% endblock %}
```

- [ ] **Step 5: Register the routes and add the nav link**

In `src/serve/app.py`, add the import `from serve.settings import register_settings` and, immediately before the `register_pages(...)` call at the end of `create_app`:

```python
    register_settings(
        application,
        engine_factory=engine_for,
        token_present=token_present,
    )
```

In `src/serve/templates/base.html`, add to the nav after `/filters`:

```html
    <a href="/settings">Settings</a>
```

- [ ] **Step 6: Run contract tests**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/contract/test_settings.py -q`
Expected: PASS.

- [ ] **Step 7: Update the golden snapshots**

`/settings` is a new GET route; `tests/golden/conftest.py::enumerate_get_cases` raises until it has request values. Add to `PATH_PARAMS`:

```python
    "/settings": {},
```

Then:

Run: `$env:PYTHONPATH='src'; $env:UPDATE_GOLDEN='1'; .\.venv\Scripts\python.exe -m pytest tests/golden -q`
Expected: snapshots rewritten, including a new `settings.json`.

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/golden/test_openapi_pin.py -q`
Expected: FAIL with the new hash printed. Update `tests/golden/snapshots/openapi.sha256` to the printed value, re-run to PASS.

- [ ] **Step 8: Commit**

```bash
git add src/serve/settings.py src/serve/templates/settings.html src/serve/app.py src/serve/templates/base.html tests/contract/test_settings.py tests/golden/conftest.py tests/golden/snapshots
git commit -m "feat: settings page, audit trail, and navigation"
```

---

## Task 4: Apply settings to new runs

**Files:**
- Modify: `src/lib/graphql_batch.py`, `src/hydrate/tail.py`, `src/serve/runner.py`, `src/serve/app.py`
- Test: `tests/unit/test_graphql_batch_core.py`, `tests/integration/test_runner.py`

**Interfaces:**
- Consumes: Tasks 1–3, and the batch engine from `2026-10-02-graphql-batch-engine.md`.
- Produces:
  - `fetch_batch(..., allow_requests: bool = True)`; `False` skips all GraphQL requests and resolves every key through `fallback` (the batching-off path).
  - `refresh_repos_batched(..., batch_size=20, allow_requests=True)`.
  - `RunnerConfig` adds `request_deadline_seconds: float | None = None`, `graphql_batch: bool = True`, `graphql_batch_size: int = 20`; `runner_config_from(settings: RunSettings) -> RunnerConfig`.
  - `build_deps(..., max_concurrent: int = 10)`.
  - `run_filter` binds `Deadline(cfg.request_deadline_seconds or request_deadline_seconds())`.
  - `app.py` default runner loads settings per submitted run, builds deps with the configured concurrency, and closes the client after the run.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_graphql_batch_core.py`:

```python
def test_allow_requests_false_resolves_everything_through_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected when batching is disabled")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = fetch_batch(
        KeyAdapter(), ["1", "2"], client=client,
        allow_requests=False, fallback=lambda key: f"rest-{key}",
    )
    assert outcome.values == {"1": "rest-1", "2": "rest-2"}
    assert outcome.stats.requests == 0
    assert outcome.stats.fallbacks == 2
```

Append to `tests/integration/test_runner.py`:

```python
def test_runner_config_from_maps_settings(clean: Engine):
    from store.settings import update_run_settings
    from serve.runner import runner_config_from

    settings = update_run_settings(
        clean, {"max_shards": 1, "max_candidates": 2, "max_hydrate": 1, "graphql_batch": False}
    )
    config = runner_config_from(settings)
    assert config.max_shards == 1
    assert config.max_candidates == 2
    assert config.max_hydrate == 1
    assert config.graphql_batch is False


def test_build_deps_honors_max_concurrent(clean: Engine):
    import fakeredis
    from limiter.buckets import BucketLimiter
    from serve.runner import build_deps

    deps = build_deps(clean, token="t", redis_client=fakeredis.FakeRedis(), max_concurrent=2)
    assert isinstance(deps.limiter, BucketLimiter)
    assert deps.limiter.max_concurrent == 2
```

(`BucketLimiter` gains a public `max_concurrent` property in the implement step.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_batch_core.py tests/integration/test_runner.py -q`
Expected: FAIL (`allow_requests` unknown / `runner_config_from` missing).

- [ ] **Step 3: Add `allow_requests` to the batch core**

In `src/lib/graphql_batch.py`, add the parameter to `fetch_batch` after `fallback`:

```python
    allow_requests: bool = True,
```

Immediately after the `fall_back` helper is defined (inside `fetch_batch`), add:

```python
    if not allow_requests:
        for key in unique:
            fall_back(key, "graphql batching disabled")
        stats.values = len(values)
        stats.unresolved = len(unresolved)
        return BatchOutcome(values=values, unresolved=unresolved, stats=stats)
```

- [ ] **Step 4: Thread batch settings through tail and runner**

In `src/hydrate/tail.py`, extend `refresh_repos_batched`'s signature and calls:

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
    batch_size: int = MAX_BATCH_SIZE,
    allow_requests: bool = True,
) -> RefreshStats:
```

and inside:

```python
        RepoDetailsAdapter(dict(candidates), batch_size=batch_size),
```

plus pass `allow_requests=allow_requests` to `fetch_batch`. Import `MAX_BATCH_SIZE` from `lib.graphql_batch`.

In `src/serve/runner.py`:

```python
@dataclass
class RunnerConfig:
    max_shards: int = 10
    max_candidates: int = 500
    max_hydrate: int = 200
    max_enrich: int = 100
    request_deadline_seconds: float | None = None
    graphql_batch: bool = True
    graphql_batch_size: int = 20


def runner_config_from(settings: RunSettings) -> RunnerConfig:
    return RunnerConfig(
        max_shards=settings.max_shards,
        max_candidates=settings.max_candidates,
        max_hydrate=settings.max_hydrate,
        max_enrich=settings.max_enrich,
        request_deadline_seconds=float(settings.request_deadline_seconds),
        graphql_batch=settings.graphql_batch,
        graphql_batch_size=settings.graphql_batch_size,
    )
```

`_hydrate(deps, rows, cfg, hook)` now takes the whole config and passes `batch_size=cfg.graphql_batch_size, allow_requests=cfg.graphql_batch` to `refresh_repos_batched`; update its call site to `_hydrate(deps, candidates, cfg, hook)`.

`_enrich_handlers(...)` gains `cfg: RunnerConfig`; `_apply_dockerfile` and `_apply_geo` pass `batch_size=cfg.graphql_batch_size, allow_requests=cfg.graphql_batch` to their `fetch_batch` calls. Update the call site:

```python
    handlers, unsupported = _enrich_handlers(
        deps, rows, virtual, budget, hook, skipped, graphql_report, cfg
    )
```

`run_filter` deadline:

```python
    seconds = (
        cfg.request_deadline_seconds
        if cfg.request_deadline_seconds is not None
        else request_deadline_seconds()
    )
    deps.limiter.bind_deadline(Deadline(seconds))
```

`build_deps` gains `max_concurrent: int = 10` and passes it:

```python
        limiter=BucketLimiter(redis_client, max_concurrent=max_concurrent)
        if redis_client is not None
        else None,
```

Add to `BucketLimiter` in `src/limiter/buckets.py`:

```python
    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent
```

- [ ] **Step 5: Read settings per run in `app.py`**

Replace the default branch of `runner_for()` (lines ~375-381):

```python
    def runner_for() -> Runner:
        def build() -> Runner:
            if runner_factory is not None:
                return runner_factory(engine_for())

            def runner(run_id: int, filter_spec: dict) -> RunPayload:
                from serve.filter_spec import parse_filter_spec
                from serve.runner import run_filter, runner_config_from
                from store.settings import load_run_settings

                settings = load_run_settings(engine_for())
                deps = build_deps(
                    engine_for(), max_concurrent=settings.limiter_max_concurrent
                )
                try:
                    return run_filter(
                        deps,
                        parse_filter_spec(filter_spec),
                        config=runner_config_from(settings),
                    )
                finally:
                    deps.client.close()

            return runner

        return cast(Runner, loaders.get("runner", build))
```

Add the top-level import `from serve.runner import build_deps` (it is currently imported inside the resume route; keep the local import there too or remove it — either is fine, but do not leave an unused top-level name).

- [ ] **Step 6: Run the affected suites**

Run: `$env:PYTHONPATH='src'; $env:GITCRAWL_REQUIRE_TEST_DB='1'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_batch_core.py tests/integration/test_runner.py tests/contract/test_settings.py tests/contract/test_pages.py tests/contract/test_console_api.py -q`
Expected: PASS. If a contract test posts `/find`, `healthy_client` sets `GITHUB_TOKEN`, so the per-run `build_deps` succeeds; fix any fixture that does not.

- [ ] **Step 7: Commit**

```bash
git add src/lib/graphql_batch.py src/hydrate/tail.py src/serve/runner.py src/serve/app.py src/limiter/buckets.py tests/unit/test_graphql_batch_core.py tests/integration/test_runner.py
git commit -m "feat: apply persisted settings to new runs"
```

---

## Task 5: Documentation, corpus profile, full verification

**Files:**
- Modify: `docs/environment.md`, `docs/development-log.md`, `design/corpus-building-efficient-engineering.md`

**Interfaces:**
- Consumes: everything above.
- Produces: documented operations and a verified branch.

- [ ] **Step 1: Document the corpus profile in `docs/environment.md`**

Add a short section:

```markdown
## Corpus-build profile (settings page vs environment)

Open `/settings` to raise the run limits for corpus builds. The page persists to the database and
applies to new runs only. Environment variables pin a field (the page shows it read-only):

| Field | Environment variable | Default | Corpus-build example |
|---|---|---|---|
| Run shards | `GITCRAWL_MAX_SHARDS` | 10 | 10000 |
| Candidates | `GITCRAWL_MAX_CANDIDATES` | 500 | 100000 |
| Hydrations | `GITCRAWL_MAX_HYDRATE` | 200 | 100000 |
| Enrichment checks | `GITCRAWL_MAX_ENRICH` | 100 | 100000 |
| Request deadline (s) | `GITCRAWL_REQUEST_DEADLINE_SECONDS` | 3600 | 86400 |
| GraphQL batching | `GITCRAWL_GRAPHQL_BATCH` | on | on |
| Batch size | `GITCRAWL_GRAPHQL_BATCH_SIZE` | 20 | 20 |
| Concurrency | `GITCRAWL_MAX_CONCURRENT` | 10 | 10 |

A corpus run occupies the single executor for its whole duration; run it overnight, and note that
progress is not checkpointed inside a run (resume re-fetches from the start).
```

- [ ] **Step 2: Append the outcome to `docs/development-log.md`**

Record: the settings page; migration `0008` claimed and detection renumbered to `0009`; resolution precedence; defaults unchanged; the `allow_requests` fallback-only mode; full-suite counts and coverage.

- [ ] **Step 3: Note the settings-driven caps in the design doc**

In `design/corpus-building-efficient-engineering.md` §11 ("Current code caveats"), replace the sentence "those caps are configuration, not GitHub limits" with a pointer: "those caps are now operator-tunable at `/settings` (env-pinnable); defaults remain the interactive values."

- [ ] **Step 4: Full suite with coverage**

Run: `$env:PYTHONPATH='src'; $env:GITCRAWL_REQUIRE_TEST_DB='1'; .\.venv\Scripts\python.exe -m pytest --cov=src --cov-report=term-missing -q`
Expected: all pass; coverage `>= 93%`.

- [ ] **Step 5: Lint and format**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m ruff check src tests; .\.venv\Scripts\python.exe -m black --check src tests`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add docs/environment.md docs/development-log.md design/corpus-building-efficient-engineering.md
git commit -m "docs: settings page, corpus profile, and outcome"
```

---

## Self-Review Checklist (run after implementation)

- [ ] **Persistence:** single-row `app_settings`, migration up/down, seeded defaults (Task 1).
- [ ] **Precedence:** env → row → default, with env-pinned fields read-only in the UI (Tasks 1, 3).
- [ ] **Validation:** bounds + `max_hydrate ≤ max_candidates`, local 400 with hints (Task 2).
- [ ] **Security:** CSRF enforced; token never stored or rendered (Task 3 tests).
- [ ] **Audit:** every save writes an `audit_log` row with before/after (Task 3 test).
- [ ] **Applied to new runs only:** per-run settings snapshot; batching toggle honored via `allow_requests`; concurrency via `build_deps`; deadline from config (Task 4).
- [ ] **No behavior change until edited:** defaults equal the pre-existing values; existing tests pass unmodified except the new ones.
- [ ] **Migration numbering:** `0008` here; detection plan renumbered to `0009` in the dev log.
- [ ] **Goldens:** `/settings` snapshot regenerated; `openapi.sha256` re-pinned.
