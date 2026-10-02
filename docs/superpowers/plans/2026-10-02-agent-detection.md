# Agent-Use Detection (v1, API-only) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `detect` run kind that scans a frozen find-run corpus through GitHub APIs only, detects coding-agent use across the papers' seven channels, stores per-repo/per-agent/per-channel evidence, and exposes an options page plus a summary/matrix/evidence console page.

**Architecture:** Detection is a first-class run kind: `runs.kind='detect'` with `source_run_id`, candidates read from the source run's frozen `run_items`, a new `src/detect/` package does pack management + channel detection + orchestration, and results are persisted in a new `detection_evidence` table plus a per-repo rollup column. It reuses the existing executor, limiter, run history, exports, and bundle machinery.

**Tech Stack:** Python 3.12, FastAPI + Jinja2 + htmx, SQLAlchemy 2 + Alembic (Postgres), Redis bucket limiter, httpx, pytest, ruff, black.

**Spec:** `docs/superpowers/specs/2026-10-02-agent-detection-design.md`

## Global Constraints

- Python `>=3.12`; line length 100; ruff (`E,F,I,UP,B`) + black clean; coverage floor `fail_under = 93` (`pyproject.toml`).
- DB tests require `TEST_DATABASE_URL` ending in `_test` (or `GITCRAWL_REQUIRE_TEST_DB=1` to fail loudly); use `tests/conftest.py` fixtures.
- Detection runs MUST NOT call discovery/search endpoints (`/search/repositories`, `/search/code`, `/repositories?since=`); enforced by test.
- Candidates come from the source run's `run_items`; top-N order is `stargazers DESC, repo_id ASC`.
- Channels (exact names): `file, author, branch, label, bot, commit_trailer, pr_metadata`.
- Depth windows (v1): commits `first:100`, PRs `first:100`; both recorded in the run spec and shown in the UI.
- No raw tokens in logs/bundles; limiter keys use `token_fingerprint`.
- Migration head is `0007`; the new migration is `0008` (`down_revision="0007"`).
- Run statuses: `queued|running|done|failed|partial`. Never present partial data as complete.
- All commits: conventional prefixes matching repo style (`feat:`, `test:`, `docs:`, `migration:`).
- Run tests with: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest <path> -q` (Windows PowerShell).

## File Structure

**Create**

- `migrations/versions/0008_detect_runs.py` — trace_packs, runs kind/source/summary, run_items.detection, detection_evidence.
- `src/detect/__init__.py`
- `src/detect/packs.py` — CSV→pack build, validate, freeze (DB), load.
- `src/detect/attribution.py` — pattern→agent matching, weights, exclusions, dedupe.
- `src/detect/channels.py` — per-channel API detectors.
- `src/detect/estimate.py` — call/time estimates.
- `src/detect/orchestrator.py` — candidates, execution, evidence + rollup, summary.
- `src/serve/detect_spec.py` — detect-spec validation (mirrors `filter_spec.py`).
- `src/serve/detect.py` — routes + page rendering.
- `src/serve/templates/detect_options.html`, `detect_detail.html`
- `src/serve/templates/partials/detect_status.html`, `partials/detect_evidence.html`, `partials/detect_estimate.html`
- `docs/detection-tiers.md`
- Tests: `tests/unit/conftest.py`, `tests/unit/test_detect_packs.py`, `test_detect_attribution.py`, `test_detect_channels.py`, `test_detect_estimate.py`, `test_detect_orchestrator.py`, `tests/contract/test_detect_spec.py`, `test_detect_routes.py`, `test_detect_pages.py`

**Modify**

- `src/store/models.py` — `Runs` (+kind, source_run_id, result_summary), `RunItem` (+detection), new `DetectionEvidence`, `TracePack`.
- `src/serve/executor.py` — `RunPayloadItem.detection`, `RunPayload.result_summary`/`detections`, `create_run(kind=..., source_run_id=...)`, `execute_run` writes summary + `detections.csv`.
- `src/lib/gh_client.py` — `resource_for_url` maps `/graphql` → `graphql`.
- `src/limiter/buckets.py` — add `"graphql": (5000, 3600.0)`.
- `src/serve/app.py` — register detect routes.
- `src/serve/templates/run_detail.html` — Detect button (find runs) + latest-detect badge.
- `tests/integration/test_models_migrations.py` — expected schema + round-trip.
- `tests/golden/snapshots/openapi.sha256` — re-pin after new routes.
- `design/research.md` — D13 pointer to `docs/detection-tiers.md`.

---

## Phase 1 — Persistence and packs

### Task 1: Migration 0008 and model updates

**Files:**
- Create: `migrations/versions/0008_detect_runs.py`
- Modify: `src/store/models.py` (class `Runs` at ~:157, `RunItem` at ~:187)
- Modify: `tests/integration/test_models_migrations.py` (TABLES, EXPECTED_COLUMNS)
- Test: `tests/integration/test_models_migrations.py`

**Interfaces:**
- Consumes: migration head `0007`.
- Produces: tables `trace_packs`, `detection_evidence`; columns `runs.kind`, `runs.source_run_id`, `runs.result_summary`, `run_items.detection`; ORM models `TracePack`, `DetectionEvidence` and updated `Runs`/`RunItem`.

- [ ] **Step 1: Write the failing migration test**

Append to `tests/integration/test_models_migrations.py`:

```python
def test_detect_migration_roundtrip(alembic_config, alembic_engine):
    command.upgrade(alembic_config, "head")
    inspector = inspect(alembic_engine)
    assert "trace_packs" in inspector.get_table_names()
    assert "detection_evidence" in inspector.get_table_names()
    run_cols = {c["name"] for c in inspector.get_columns("runs")}
    assert {"kind", "source_run_id", "result_summary"} <= run_cols
    item_cols = {c["name"] for c in inspector.get_columns("run_items")}
    assert "detection" in item_cols
    with alembic_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO trace_packs (version, authors, files, branches, labels)"
                " VALUES ('t1', '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb)"
            )
        )
        source_id = connection.execute(
            text(
                "INSERT INTO runs (filter_hash, filter_spec, api_version)"
                " VALUES ('h', '{}'::jsonb, '2022-11-28') RETURNING id"
            )
        ).scalar_one()
        detect_id = connection.execute(
            text(
                "INSERT INTO runs (filter_hash, filter_spec, api_version, kind, source_run_id)"
                " VALUES ('h2', '{}'::jsonb, '2022-11-28', 'detect', :src) RETURNING id"
            ),
            {"src": source_id},
        ).scalar_one()
        connection.execute(text("INSERT INTO owners (id, login, type) VALUES (1, 'octo', 'User')"))
        connection.execute(
            text(
                "INSERT INTO repos (id, node_id, full_name, owner_id, name, visibility)"
                " VALUES (1, 'n1', 'octo/one', 1, 'one', 'public')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO detection_evidence (run_id, repo_id, agent, channel, evidence, pack_version)"
                " VALUES (:run, 1, 'claude', 'file', '{\"path\": \"CLAUDE.md\"}'::jsonb, 't1')"
            ),
            {"run": detect_id},
        )
        assert detect_id != source_id
    command.downgrade(alembic_config, "0007")
    inspector = inspect(alembic_engine)
    assert "detection_evidence" not in inspector.get_table_names()
    assert "kind" not in {c["name"] for c in inspector.get_columns("runs")}
    command.upgrade(alembic_config, "head")  # leave the schema at head for the rest of the module
```

Also add `"trace_packs"` and `"detection_evidence"` to `TABLES`, and add the new column names to `EXPECTED_COLUMNS` for `runs` (`"kind": False, "source_run_id": True, "result_summary": True`) and `run_items` (`"detection": True`).

- [ ] **Step 2: Run it to verify it fails**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/integration/test_models_migrations.py::test_detect_migration_roundtrip -q`
Expected: FAIL (`no such table: trace_packs` / assertion).

- [ ] **Step 3: Write the migration**

Create `migrations/versions/0008_detect_runs.py`:

```python
"""detect runs: trace packs + detection evidence"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trace_packs",
        sa.Column("version", sa.Text(), primary_key=True),
        sa.Column("authors", postgresql.JSONB(), nullable=False),
        sa.Column("files", postgresql.JSONB(), nullable=False),
        sa.Column("branches", postgresql.JSONB(), nullable=False),
        sa.Column("labels", postgresql.JSONB(), nullable=False),
        sa.Column("bots", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("generic_weights", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("exclusions", postgresql.JSONB(), nullable=False, server_default=sa.text("""'["CONVENTIONS.md"]'""")),
        sa.Column("frozen_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.add_column("runs", sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'find'")))
    op.add_column("runs", sa.Column("source_run_id", sa.BigInteger(), nullable=True))
    op.add_column("runs", sa.Column("result_summary", postgresql.JSONB(), nullable=True))
    op.create_check_constraint("runs_kind_check", "runs", "kind IN ('find','detect')")
    op.create_foreign_key("runs_source_run_id_fkey", "runs", "runs", ["source_run_id"], ["id"])
    op.create_index("runs_kind_idx", "runs", ["kind", sa.text("created_at DESC")])
    op.create_index(
        "runs_source_idx",
        "runs",
        ["source_run_id"],
        postgresql_where=sa.text("source_run_id IS NOT NULL"),
    )
    op.add_column("run_items", sa.Column("detection", postgresql.JSONB(), nullable=True))
    op.create_table(
        "detection_evidence",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.BigInteger(), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("repo_id", sa.BigInteger(), sa.ForeignKey("repos.id"), nullable=False),
        sa.Column("agent", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("pack_version", sa.Text(), sa.ForeignKey("trace_packs.version"), nullable=False),
        sa.Column("detected_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "channel IN ('file','author','branch','label','bot','commit_trailer','pr_metadata')",
            name="detection_evidence_channel_check",
        ),
    )
    op.create_index("de_run_repo_idx", "detection_evidence", ["run_id", "repo_id"])
    op.create_index("de_run_agent_idx", "detection_evidence", ["run_id", "agent"])
    op.create_index("de_run_channel_idx", "detection_evidence", ["run_id", "channel"])
    op.create_index("de_repo_idx", "detection_evidence", ["repo_id"])


def downgrade() -> None:
    op.drop_index("de_repo_idx", table_name="detection_evidence")
    op.drop_index("de_run_channel_idx", table_name="detection_evidence")
    op.drop_index("de_run_agent_idx", table_name="detection_evidence")
    op.drop_index("de_run_repo_idx", table_name="detection_evidence")
    op.drop_table("detection_evidence")
    op.drop_column("run_items", "detection")
    op.drop_index("runs_source_idx", table_name="runs")
    op.drop_index("runs_kind_idx", table_name="runs")
    op.drop_constraint("runs_source_run_id_fkey", "runs", type_="foreignkey")
    op.drop_constraint("runs_kind_check", "runs", type_="check")
    op.drop_column("runs", "result_summary")
    op.drop_column("runs", "source_run_id")
    op.drop_column("runs", "kind")
    op.drop_table("trace_packs")
```

- [ ] **Step 4: Mirror in `src/store/models.py`**

Add imports already present for JSONB/BigInteger/Text/CheckConstraint/Index/ForeignKey. Extend `Runs`:

```python
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'find'"))
    source_run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("runs.id"))
    result_summary: Mapped[dict | None] = mapped_column(JSONB)
```

Extend `RunItem`:

```python
    detection: Mapped[dict | None] = mapped_column(JSONB)
```

Add models (after `SavedFilter` or near `RunItem`):

```python
class TracePack(Base):
    __tablename__ = "trace_packs"

    version: Mapped[str] = mapped_column(Text, primary_key=True)
    authors: Mapped[list] = mapped_column(JSONB, nullable=False)
    files: Mapped[list] = mapped_column(JSONB, nullable=False)
    branches: Mapped[list] = mapped_column(JSONB, nullable=False)
    labels: Mapped[list] = mapped_column(JSONB, nullable=False)
    bots: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'"))
    generic_weights: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    exclusions: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("""'["CONVENTIONS.md"]'""")
    )
    frozen_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class DetectionEvidence(Base):
    __tablename__ = "detection_evidence"
    __table_args__ = (
        Index("de_run_repo_idx", "run_id", "repo_id"),
        Index("de_run_agent_idx", "run_id", "agent"),
        Index("de_run_channel_idx", "run_id", "channel"),
        Index("de_repo_idx", "repo_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    repo_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("repos.id"), nullable=False)
    agent: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    pack_version: Mapped[str] = mapped_column(Text, ForeignKey("trace_packs.version"), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
```

- [ ] **Step 5: Run migration tests (up and down)**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/integration/test_models_migrations.py -q`
Expected: PASS (including `test_detect_migration_roundtrip` and all pre-existing schema assertions).

- [ ] **Step 6: Commit**

```bash
git add migrations/versions/0008_detect_runs.py src/store/models.py tests/integration/test_models_migrations.py
git commit -m "migration: add detect runs, trace packs, and detection evidence"
```

---

### Task 2: Pack build / validate / freeze / load

**Files:**
- Create: `src/detect/__init__.py`, `src/detect/packs.py`
- Test: `tests/unit/test_detect_packs.py`

**Interfaces:**
- Consumes: `store.models.TracePack`, `Deps.engine` pattern.
- Produces (used by Tasks 3, 9, 10):
  - `@dataclass(frozen=True) class Pattern: pattern: str; agent: str` (patterns are Python regexes, matching the replication package's semantics)
  - `@dataclass(frozen=True) class Pack: version: str; agents: Mapping[str,str]; authors/branches/labels/bots/files: tuple[Pattern,...]; generic_weights: Mapping[str,float]; exclusions: frozenset[str]` (`agents` maps tool name → docs URL from `tools.csv`)
  - `build_pack_from_dir(src: Path, *, version: str) -> Pack`
  - `pack_to_document(pack: Pack) -> dict` and `pack_from_document(doc: dict) -> Pack`
  - `load_pack_file(path: Path) -> Pack`
  - `freeze_pack(engine, pack: Pack) -> None` (immutable: raises `PackConflict` if version exists with different content)
  - `load_frozen(engine, version: str) -> Pack`
  - `main(argv: Sequence[str] | None = None) -> int` for `python -m detect.packs build|freeze`

- [ ] **Step 1: Fetch the real pattern CSVs (once)**

From the public replication package `labri-progress/agent-impact`, fetch `config/patterns/{authors,files,branches,labels,bots,tools}.csv` (raw URLs under `https://raw.githubusercontent.com/labri-progress/agent-impact/main/config/patterns/`). Verified schema (2026-10-02):

- `authors.csv` / `files.csv` / `branches.csv` / `labels.csv` / `bots.csv`: columns `pattern,type,subtype`; `subtype` is the tool label (`Claude Code`, `Cursor`, `Generic`, `dependabot`, …); `type` is informational (`AI`, `AI_FILE`, `AI-PR`, `Bot`).
- `tools.csv`: columns `name,url` (tool display name → docs URL).
- Extra files to ignore: `counts.csv`, `rg_patterns.csv`, `top-orgs.csv`.

Matching semantics (copied from the replication package's `code/heuristics.py`, do not invent): every `pattern` is a Python regex, case-sensitive, searched with `finditer`; patterns sharing a `subtype` are combined into one named group per channel; for files, bare dots are escaped (`pattern.replace(".", "\\.")` when no `\\.` present) and patterns are prefixed with `(?:^|/)` unless already anchored with `^` or `(?:^|/)`. `bots.csv` is **generic CI bots to exclude**, not agent detection — agent bots (`copilot-swe-agent`, `devin-ai-integration`, `factory-droid[bot]`, …) live in `authors.csv`.

The full files are not vendored into this plan; Task 2 copies them (unchanged) into `packs/patterns/` for reproducibility, and the freeze step records the pack digest.

- [ ] **Step 2: Write the failing tests**

Create `tests/unit/test_detect_packs.py`:

```python
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from detect.packs import (
    PackConflict,
    build_pack_from_dir,
    freeze_pack,
    load_frozen,
    pack_from_document,
    pack_to_document,
)


def write_csv(path: Path, header: str, rows: list[str]) -> None:
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")


def make_src(tmp_path: Path) -> Path:
    src = tmp_path / "patterns"
    src.mkdir()
    write_csv(src / "tools.csv", "name,url", ["Claude Code,https://claude.com", "Cursor,https://cursor.com"])
    write_csv(src / "authors.csv", "pattern,type,subtype", ["noreply@anthropic.com,AI,Claude Code"])
    write_csv(src / "files.csv", "pattern,type,subtype", ["CLAUDE.md,AI_FILE,Claude Code", ".cursor/,AI_FILE,Cursor"])
    write_csv(src / "branches.csv", "pattern,type,subtype", ["claude/,AI-PR,Claude Code"])
    write_csv(src / "labels.csv", "pattern,type,subtype", ["ai-generated,AI-PR,Generic"])
    write_csv(src / "bots.csv", "pattern,type,subtype", [r"dependabot\[bot\],Bot,dependabot"])
    return src


def test_build_pack_from_dir_maps_all_channels(tmp_path):
    pack = build_pack_from_dir(make_src(tmp_path), version="2026-10-02-test")
    assert pack.version == "2026-10-02-test"
    assert pack.agents["Claude Code"] == "https://claude.com"
    assert pack.files[0].pattern == "CLAUDE.md"
    assert pack.files[0].agent == "Claude Code"
    assert pack.bots[0].agent == "dependabot"
    assert pack.generic_weights == {"AGENTS.md": 0.3}
    assert pack.exclusions == frozenset({"CONVENTIONS.md"})


def test_document_roundtrip(tmp_path):
    pack = build_pack_from_dir(make_src(tmp_path), version="v1")
    assert pack_from_document(pack_to_document(pack)) == pack


def test_freeze_is_immutable(clean_engine, tmp_path):
    pack = build_pack_from_dir(make_src(tmp_path), version="v1")
    freeze_pack(clean_engine, pack)
    # trace_packs stores patterns only; the agents name->url map is not persisted
    assert load_frozen(clean_engine, "v1") == replace(pack, agents={})
    with pytest.raises(PackConflict):
        freeze_pack(clean_engine, replace(pack, generic_weights={"AGENTS.md": 0.1}))


def test_missing_channel_file_is_rejected(tmp_path):
    src = make_src(tmp_path)
    (src / "labels.csv").unlink()
    with pytest.raises(ValueError, match="labels.csv"):
        build_pack_from_dir(src, version="v1")
```

Create `tests/unit/conftest.py` with a DB fixture shared by the detect unit tests. It mirrors the `db` fixture in `tests/integration/test_executor.py:61-84` (upgrade to head, truncate, seed owner/repos), extended with the new tables:

```python
from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import text


@pytest.fixture()
def clean_engine(alembic_config, alembic_engine):
    command.upgrade(alembic_config, "head")
    with alembic_engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE detection_evidence, trace_packs, run_items, runs, saved_filters, "
                "audit_log, shards, owners, repos, full_name_history RESTART IDENTITY CASCADE"
            )
        )
        connection.execute(text("INSERT INTO owners (id, login, type) VALUES (1, 'octo', 'User')"))
        connection.execute(
            text(
                "INSERT INTO repos (id, node_id, full_name, owner_id, name, visibility) VALUES "
                "(1, 'n1', 'octo/hello', 1, 'hello', 'public'), "
                "(2, 'n2', 'octo/world', 1, 'world', 'public'), "
                "(3, 'n3', 'octo/extra', 1, 'extra', 'public')"
            )
        )
    return alembic_engine
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_detect_packs.py -q`
Expected: FAIL (`ModuleNotFoundError: detect.packs`).

- [ ] **Step 4: Implement `src/detect/packs.py`**

```python
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

from store.models import TracePack

CHANNEL_FILES = ("authors.csv", "files.csv", "branches.csv", "labels.csv", "bots.csv")
DEFAULT_GENERIC_WEIGHTS = {"AGENTS.md": 0.3}
DEFAULT_EXCLUSIONS = ("CONVENTIONS.md",)


class PackConflict(ValueError):
    pass


@dataclass(frozen=True)
class Pattern:
    pattern: str
    agent: str


@dataclass(frozen=True)
class Pack:
    version: str
    agents: Mapping[str, str]
    authors: tuple[Pattern, ...]
    files: tuple[Pattern, ...]
    branches: tuple[Pattern, ...]
    labels: tuple[Pattern, ...]
    bots: tuple[Pattern, ...]
    generic_weights: Mapping[str, float] = None  # type: ignore[assignment]
    exclusions: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "generic_weights", dict(self.generic_weights or DEFAULT_GENERIC_WEIGHTS))
        object.__setattr__(self, "exclusions", frozenset(self.exclusions or DEFAULT_EXCLUSIONS))


def _read_patterns(path: Path) -> tuple[Pattern, ...]:
    if not path.exists():
        raise ValueError(f"missing pattern file: {path.name}")
    patterns: list[Pattern] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            pattern = (row.get("pattern") or "").strip()
            agent = (row.get("subtype") or "").strip()
            if not pattern or not agent:
                continue
            patterns.append(Pattern(pattern=pattern, agent=agent))
    return tuple(patterns)


def build_pack_from_dir(src: Path, *, version: str) -> Pack:
    src = Path(src)
    tools_path = src / "tools.csv"
    if not tools_path.exists():
        raise ValueError("missing pattern file: tools.csv")
    agents: dict[str, str] = {}
    with tools_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            name = (row.get("name") or "").strip()
            url = (row.get("url") or "").strip()
            if name:
                agents[name] = url
    channels = {name: _read_patterns(src / name) for name in CHANNEL_FILES}
    return Pack(
        version=version,
        agents=agents,
        authors=channels["authors.csv"],
        files=channels["files.csv"],
        branches=channels["branches.csv"],
        labels=channels["labels.csv"],
        bots=channels["bots.csv"],
    )


def pack_to_document(pack: Pack) -> dict:
    def patterns(items: Sequence[Pattern]) -> list[dict]:
        return [{"pattern": p.pattern, "agent": p.agent} for p in items]

    return {
        "version": pack.version,
        "agents": dict(pack.agents),
        "authors": patterns(pack.authors),
        "files": patterns(pack.files),
        "branches": patterns(pack.branches),
        "labels": patterns(pack.labels),
        "bots": patterns(pack.bots),
        "generic_weights": dict(pack.generic_weights),
        "exclusions": sorted(pack.exclusions),
    }


def pack_from_document(doc: Mapping[str, object]) -> Pack:
    def patterns(key: str) -> tuple[Pattern, ...]:
        raw = doc.get(key) or []
        return tuple(
            Pattern(str(item["pattern"]), str(item["agent"]))
            for item in raw  # type: ignore[union-attr]
        )

    return Pack(
        version=str(doc["version"]),
        agents={str(k): str(v) for k, v in dict(doc.get("agents") or {}).items()},  # type: ignore[union-attr]
        authors=patterns("authors"),
        files=patterns("files"),
        branches=patterns("branches"),
        labels=patterns("labels"),
        bots=patterns("bots"),
        generic_weights={str(k): float(v) for k, v in dict(doc.get("generic_weights") or {}).items()},  # type: ignore[union-attr]
        exclusions=frozenset(str(x) for x in (doc.get("exclusions") or [])),  # type: ignore[union-attr]
    )


def pack_digest(pack: Pack) -> str:
    canonical = json.dumps(pack_to_document(pack), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def write_pack_file(pack: Pack, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pack_to_document(pack), indent=2), encoding="utf-8")
    return pack_digest(pack)


def load_pack_file(path: Path) -> Pack:
    return pack_from_document(json.loads(Path(path).read_text(encoding="utf-8")))


def freeze_pack(engine: Engine, pack: Pack) -> None:
    doc = pack_to_document(pack)
    with engine.begin() as connection:
        existing = connection.execute(
            select(TracePack).where(TracePack.version == pack.version)
        ).mappings().one_or_none()
        if existing is not None:
            stored = pack_from_document(
                {
                    "version": existing["version"],
                    "agents": {},  # agents are not persisted in trace_packs; version identity is content-based
                    "authors": existing["authors"],
                    "files": existing["files"],
                    "branches": existing["branches"],
                    "labels": existing["labels"],
                    "bots": existing["bots"],
                    "generic_weights": existing["generic_weights"],
                    "exclusions": existing["exclusions"],
                }
            )
            if stored != replace(pack, agents={}):
                raise PackConflict(f"pack version {pack.version!r} already frozen with different content")
            return
        connection.execute(
            insert(TracePack).values(
                version=pack.version,
                authors=doc["authors"],
                files=doc["files"],
                branches=doc["branches"],
                labels=doc["labels"],
                bots=doc["bots"],
                generic_weights=doc["generic_weights"],
                exclusions=doc["exclusions"],
            )
        )


def load_frozen(engine: Engine, version: str) -> Pack:
    with engine.connect() as connection:
        row = connection.execute(
            select(TracePack).where(TracePack.version == version)
        ).mappings().one_or_none()
    if row is None:
        raise KeyError(version)
    return pack_from_document(
        {
            "version": row["version"],
            "agents": {},
            "authors": row["authors"],
            "files": row["files"],
            "branches": row["branches"],
            "labels": row["labels"],
            "bots": row["bots"],
            "generic_weights": row["generic_weights"],
            "exclusions": row["exclusions"],
        }
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m detect.packs")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--src", required=True)
    build.add_argument("--version", required=True)
    build.add_argument("--out", required=True)
    freeze = sub.add_parser("freeze")
    freeze.add_argument("--pack", required=True)
    args = parser.parse_args(argv)
    if args.command == "build":
        pack = build_pack_from_dir(Path(args.src), version=args.version)
        digest = write_pack_file(pack, Path(args.out))
        print(f"wrote {args.out} sha256={digest[:12]}")
        return 0
    from sqlalchemy import create_engine

    engine = create_engine(os.environ["DATABASE_URL"])
    freeze_pack(engine, load_pack_file(Path(args.pack)))
    print("frozen")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Note: `trace_packs` does not persist the `agents` name→url map (patterns only); `load_frozen` returns an empty `agents` map. Attribution only needs the `subtype` strings stored on the patterns; the URL map is UI/reference sugar best read from the vendored pack file. Keep this behavior and assert it in tests.

- [ ] **Step 5: Run tests to verify they pass**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_detect_packs.py -q`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add src/detect/__init__.py src/detect/packs.py tests/unit/test_detect_packs.py
git commit -m "feat: trace pack build, freeze, and load"
```

---

### Task 3: Attribution matching

**Files:**
- Create: `src/detect/attribution.py`
- Test: `tests/unit/test_detect_attribution.py`

**Interfaces:**
- Consumes: `detect.packs.Pack`, `detect.packs.Pattern`, `enrich.trees_first.FilePresence`.
- Produces (used by Task 9):
  - `@dataclass(frozen=True) class Hit: agent: str; channel: str; evidence: dict`
  - `@dataclass(frozen=True) class ChannelRegex: regex: re.Pattern | None; names: Mapping[str, str]`
  - `build_channel_regex(patterns, *, is_files=False) -> ChannelRegex` — one alternation regex per channel with a named group per agent subtype (sanitized `\W+`→`_`), matching the replication package's `code/heuristics.py` semantics (case-sensitive `finditer`; file patterns dot-escaped and prefixed `(?:^|/)`).
  - `matchers_for(pack) -> dict[str, ChannelRegex]` (keys `file`, `author`, `branch`, `label`, `bot`; author serves both author and commit-trailer channels; cached per pack version, `clear_matcher_cache()` for tests)
  - `match_files(pack, presence) -> list[Hit]`
  - `match_author_identities(pack, identities) -> list[Hit]`
  - `match_commit_messages(pack, messages) -> list[Hit]`
  - `match_branches(pack, branches) -> list[Hit]`
  - `match_labels(pack, labels) -> list[Hit]`
  - `match_bots(pack, identities) -> list[Hit]` — generic CI bot evidence; **not** an adoption signal
  - `match_pr_metadata(pack, prs) -> list[Hit]` — head-branch regex, label regex, and the Codex body marker `https://chatgpt.com/codex/tasks`
  - `dedupe_hits(hits) -> list[Hit]`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_detect_attribution.py`:

```python
from __future__ import annotations

from detect.attribution import (
    CODEX_PR_BODY_MARKER,
    Hit,
    build_channel_regex,
    dedupe_hits,
    match_author_identities,
    match_branches,
    match_commit_messages,
    match_files,
    match_labels,
    match_pr_metadata,
)
from detect.packs import Pack, Pattern
from enrich.trees_first import FilePresence

PACK = Pack(
    version="v1",
    agents={"Claude Code": "https://claude.com", "Cursor": "https://cursor.com"},
    authors=(Pattern(r"noreply@anthropic\.com", "Claude Code"), Pattern("cursoragent", "Cursor")),
    files=(Pattern(r"CLAUDE\.md", "Claude Code"), Pattern(r"\.cursor/", "Cursor"), Pattern(r"AGENTS\.md", "Generic")),
    branches=(Pattern("claude/", "Claude Code"), Pattern("cursor/", "Cursor")),
    labels=(Pattern("ai-generated", "Claude Code"), Pattern("^amp$", "Amp")),
    bots=(Pattern(r"dependabot\[bot\]", "dependabot"),),
    generic_weights={"AGENTS.md": 0.3},
    exclusions=frozenset({"CONVENTIONS.md"}),
)


def presence(paths):
    return FilePresence(repo_full_name="o/r", paths=frozenset(paths), truncated=False, source="tree")


def test_channel_regex_builds_named_groups_and_escapes_file_dots():
    channel = build_channel_regex(PACK.files, is_files=True)
    assert channel.regex is not None
    match = channel.regex.search(".cursor/rules/x.md")
    assert match is not None
    assert channel.names[match.lastgroup] == "Cursor"


def test_match_files_maps_regexes_and_weights():
    hits = match_files(
        PACK, presence(["CLAUDE.md", "AGENTS.md", "CONVENTIONS.md", ".cursor/rules/x.md", "claude.md"])
    )
    by_path = {h.evidence["path"]: h for h in hits}
    assert by_path["CLAUDE.md"].agent == "Claude Code"
    assert by_path[".cursor/rules/x.md"].agent == "Cursor"
    assert by_path["AGENTS.md"].agent == "Generic"
    assert by_path["AGENTS.md"].evidence["generic"] is True
    assert by_path["AGENTS.md"].evidence["weight"] == 0.3
    assert "CONVENTIONS.md" not in by_path
    assert "claude.md" not in by_path  # case-sensitive, like the replication package


def test_match_author_identities_and_messages():
    authors = match_author_identities(PACK, ["Jane <noreply@anthropic.com>"])
    assert [(h.agent, h.channel) for h in authors] == [("Claude Code", "author")]
    trailers = match_commit_messages(PACK, ["fix: thing\n\nCo-authored-by: Cursor <cursoragent@x>"])
    assert [(h.agent, h.channel) for h in trailers] == [("Cursor", "commit_trailer")]


def test_match_branches_labels_prs_and_codex_marker():
    assert [h.agent for h in match_branches(PACK, ["claude/add-tests"])] == ["Claude Code"]
    assert [h.agent for h in match_labels(PACK, ["ai-generated", "example"])] == ["Claude Code"]
    assert [h.agent for h in match_labels(PACK, ["amp"])] == ["Amp"]
    prs = [
        {
            "number": 7,
            "headRefName": "cursor/fix",
            "labels": [],
            "body": f"task: {CODEX_PR_BODY_MARKER}",
        }
    ]
    hits = match_pr_metadata(PACK, prs)
    assert {h.agent for h in hits} == {"Cursor", "Codex"}
    assert all(h.evidence["pr"] == 7 for h in hits)


def test_dedupe_collapses_identical_hits():
    hits = [Hit("Claude Code", "file", {"path": "CLAUDE.md"}), Hit("Claude Code", "file", {"path": "CLAUDE.md"})]
    assert dedupe_hits(hits) == [hits[0]]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_detect_attribution.py -q`
Expected: FAIL (`ModuleNotFoundError: detect.attribution`).

- [ ] **Step 3: Implement `src/detect/attribution.py`**

```python
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from detect.packs import Pack, Pattern
from enrich.trees_first import FilePresence

CODEX_PR_BODY_MARKER = "https://chatgpt.com/codex/tasks"


@dataclass(frozen=True)
class Hit:
    agent: str
    channel: str
    evidence: dict


@dataclass(frozen=True)
class ChannelRegex:
    regex: re.Pattern[str] | None
    names: Mapping[str, str]


def _sanitize(name: str) -> str:
    return re.sub(r"\W+", "_", name)


def build_channel_regex(patterns: Sequence[Pattern], *, is_files: bool = False) -> ChannelRegex:
    groups: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for pattern in patterns:
        name = _sanitize(pattern.agent)
        names.setdefault(name, pattern.agent)
        raw = pattern.pattern
        if is_files:
            if "." in raw and "\\." not in raw:
                raw = raw.replace(".", "\\.")
            if not raw.startswith("(?:^|/)") and not raw.startswith("^"):
                raw = "(?:^|/)" + raw
        groups.setdefault(name, []).append(raw)
    if not groups:
        return ChannelRegex(regex=None, names={})
    parts = [f"(?P<{name}>{'|'.join(pats)})" for name, pats in groups.items()]
    return ChannelRegex(regex=re.compile("|".join(parts)), names=names)


_MATCHER_CACHE: dict[str, dict[str, ChannelRegex]] = {}


def clear_matcher_cache() -> None:
    _MATCHER_CACHE.clear()


def matchers_for(pack: Pack) -> dict[str, ChannelRegex]:
    cached = _MATCHER_CACHE.get(pack.version)
    if cached is None:
        cached = {
            "file": build_channel_regex(pack.files, is_files=True),
            "author": build_channel_regex(pack.authors),
            "branch": build_channel_regex(pack.branches),
            "label": build_channel_regex(pack.labels),
            "bot": build_channel_regex(pack.bots),
        }
        _MATCHER_CACHE[pack.version] = cached
    return cached


def _generic_weight(pack: Pack, path: str) -> float | None:
    for pattern in pack.files:
        if pattern.pattern in pack.generic_weights:
            escaped = pattern.pattern.replace(".", "\\.")
            if re.search(f"(?:^|/){escaped}", path):
                return pack.generic_weights[pattern.pattern]
    return None


def match_files(pack: Pack, presence: FilePresence) -> list[Hit]:
    channel = matchers_for(pack)["file"]
    if channel.regex is None:
        return []
    hits: list[Hit] = []
    for path in sorted(presence.paths):
        if path.rsplit("/", 1)[-1] in pack.exclusions or path in pack.exclusions:
            continue
        for match in channel.regex.finditer(path):
            if match.lastgroup is None:
                continue
            evidence: dict = {"path": path, "source": presence.source}
            weight = _generic_weight(pack, path)
            if weight is not None:
                evidence["generic"] = True
                evidence["weight"] = weight
            hits.append(Hit(channel.names[match.lastgroup], "file", evidence))
    return dedupe_hits(hits)


def _match_values(
    channel: ChannelRegex,
    values: Iterable[str],
    *,
    name: str,
    key: str,
) -> list[Hit]:
    if channel.regex is None:
        return []
    hits: list[Hit] = []
    for value in values:
        if not value:
            continue
        for match in channel.regex.finditer(value):
            if match.lastgroup is None:
                continue
            hits.append(
                Hit(
                    channel.names[match.lastgroup],
                    name,
                    {key: value[:300], "match": match.group()[:100]},
                )
            )
    return hits


def match_author_identities(pack: Pack, identities: Iterable[str]) -> list[Hit]:
    return _match_values(matchers_for(pack)["author"], identities, name="author", key="identity")


def match_commit_messages(pack: Pack, messages: Iterable[str]) -> list[Hit]:
    return _match_values(matchers_for(pack)["author"], messages, name="commit_trailer", key="message")


def match_branches(pack: Pack, branches: Iterable[str]) -> list[Hit]:
    return _match_values(matchers_for(pack)["branch"], branches, name="branch", key="branch")


def match_labels(pack: Pack, labels: Iterable[str]) -> list[Hit]:
    return _match_values(matchers_for(pack)["label"], labels, name="label", key="label")


def match_bots(pack: Pack, identities: Iterable[str]) -> list[Hit]:
    return _match_values(matchers_for(pack)["bot"], identities, name="bot", key="identity")


def match_pr_metadata(pack: Pack, prs: Iterable[Mapping[str, object]]) -> list[Hit]:
    hits: list[Hit] = []
    for pr in prs:
        number = pr.get("number")
        head = str(pr.get("headRefName") or "")
        labels = [str(x) for x in pr.get("labels") or []]
        body = str(pr.get("body") or "")
        for hit in match_branches(pack, [head] if head else []):
            hits.append(Hit(hit.agent, "pr_metadata", {"pr": number, "head": head, "match": hit.evidence["match"]}))
        for hit in match_labels(pack, labels):
            hits.append(Hit(hit.agent, "pr_metadata", {"pr": number, "label": hit.evidence["label"], "match": hit.evidence["match"]}))
        if body and CODEX_PR_BODY_MARKER in body:
            hits.append(Hit("Codex", "pr_metadata", {"pr": number, "snippet": CODEX_PR_BODY_MARKER}))
    return hits


def dedupe_hits(hits: Iterable[Hit]) -> list[Hit]:
    seen: set[str] = set()
    unique: list[Hit] = []
    for hit in hits:
        key = json.dumps([hit.agent, hit.channel, hit.evidence], sort_keys=True, separators=(",", ":"))
        if key not in seen:
            seen.add(key)
            unique.append(hit)
    return unique
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_detect_attribution.py tests/unit/test_trees_first.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/detect/attribution.py tests/unit/test_detect_attribution.py
git commit -m "feat: trace-pack attribution matching"
```

---

## Phase 2 — Channels

### Task 4: File channel (trees + metafiles + weighting)

**Files:**
- Create: `src/detect/channels.py`
- Test: `tests/unit/test_detect_channels.py`

**Interfaces:**
- Consumes: `discover.pipeline.Deps`, `detect.packs.Pack`, `detect.attribution`, `enrich.trees_first.fetch_tree` / `fetch_metafiles`.
- Produces (used by Task 9):
  - `CHANNELS: tuple[str, ...]`
  - `@dataclass class ChannelResult: hits: list[Hit]; status: str; note: str | None = None` with `status in {"ok","partial","unavailable","skipped"}`
  - `detect_files(deps, full_name: str, pack: Pack) -> ChannelResult`
  - `detect_branches(deps, full_name, pack) -> ChannelResult` (Task 5)
  - `detect_labels(deps, full_name, pack) -> ChannelResult` (Task 5)
  - `detect_bots(deps, full_name, pack) -> ChannelResult` (Task 5)
  - `detect_history(deps, full_name, pack, *, limit=100) -> dict[str, ChannelResult]` (Task 6; keys `author`, `commit_trailer`, `bot`)
  - `detect_prs(deps, full_name, pack, *, limit=100) -> ChannelResult` (Task 7)

- [ ] **Step 1: Write the failing file-channel test**

Create `tests/unit/test_detect_channels.py`:

```python
from __future__ import annotations

import base64
import json

import httpx
import pytest

from detect.channels import detect_files
from detect.packs import Pack, Pattern

PACK = Pack(
    version="v1",
    agents={"claude": "Claude Code", "cursor": "Cursor"},
    authors=(Pattern(r"noreply@anthropic\.com", "claude"),),
    files=(
        Pattern(r"CLAUDE\.md", "claude"),
        Pattern(r"\.cursor/", "cursor"),
        Pattern(r"AGENTS\.md", "Generic"),
    ),
    branches=(Pattern("claude/", "claude"),),
    labels=(Pattern("ai-generated", "claude"),),
    bots=(Pattern(r"dependabot\[bot\]", "dependabot"),),
)


class Deps:
    def __init__(self, handler):
        self.client = httpx.Client(transport=httpx.MockTransport(handler))
        self.engine = None
        self.redis = None
        self.limiter = None
        self.token_id = "test"
        self.audit_buffer = None


def tree_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/repos/octo/repo":
        return httpx.Response(200, json={"default_branch": "main"})
    if path.endswith("/contents/.gitignore"):
        content = base64.b64encode(b"node_modules/\nCLAUDE.md\n").decode()
        return httpx.Response(200, json={"encoding": "base64", "content": content})
    return httpx.Response(
        200,
        json={
            "sha": "head",
            "truncated": False,
            "tree": [
                {"path": "CLAUDE.md", "type": "blob"},
                {"path": "AGENTS.md", "type": "blob"},
                {"path": ".cursor/rules/x.md", "type": "blob"},
            ],
        },
    )


def test_detect_files_returns_tree_and_gitignore_hits_with_weighting():
    result = detect_files(Deps(tree_handler), "octo/repo", PACK)
    assert result.status == "ok"
    sources = {(h.agent, h.evidence["source"]) for h in result.hits}
    assert ("claude", "tree") in sources
    assert ("claude", "gitignore") in sources  # CLAUDE.md appears only in .gitignore (visible-via-ignore case)
    assert ("cursor", "tree") in sources
    generic = [h for h in result.hits if h.evidence.get("path") == "AGENTS.md"][0]
    assert generic.evidence["weight"] == 0.3


def test_detect_files_404_is_unavailable():
    def handler(request):
        return httpx.Response(404, json={"message": "Not Found"})

    result = detect_files(Deps(handler), "octo/gone", PACK)
    assert result.status == "unavailable"
    assert result.hits == []
```

Note: `AGENTS.md` is matched on the tree path only; the `.gitignore` fixture also lists `CLAUDE.md`, producing a second Claude Code hit with `source="gitignore"` — that is the paper's "visible only via ignore" signal and must not be deduped away (evidence differs by `source`).

- [ ] **Step 2: Run to verify failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_detect_channels.py -q`
Expected: FAIL (`ModuleNotFoundError: detect.channels`).

- [ ] **Step 3: Implement `src/detect/channels.py` (file channel first)**

```python
from __future__ import annotations

import base64
from dataclasses import dataclass, field

from detect.attribution import Hit, dedupe_hits, match_files, matchers_for
from detect.packs import Pack
from discover.pipeline import Deps
from discover.search_shards import RequestFailed
from enrich.trees_first import fetch_tree
from lib.gh_client import API_BASE, request_with_retry

CHANNELS: tuple[str, ...] = (
    "file",
    "author",
    "branch",
    "label",
    "bot",
    "commit_trailer",
    "pr_metadata",
)


@dataclass
class ChannelResult:
    hits: list[Hit] = field(default_factory=list)
    status: str = "ok"
    note: str | None = None


def _gitignore_hits(deps: Deps, full_name: str, pack: Pack) -> tuple[list[Hit], str, str | None]:
    response = request_with_retry(
        deps.client,
        "GET",
        f"{API_BASE}/repos/{full_name}/contents/.gitignore",
        limiter=deps.limiter,
        token_id=deps.token_id,
    )
    if response.status_code == 404:
        return [], "ok", None
    if response.status_code != 200:
        return [], "partial", f"gitignore fetch failed: HTTP {response.status_code}"
    payload = response.json()
    try:
        content = base64.b64decode(payload.get("content") or "").decode("utf-8", "replace")
    except Exception:
        return [], "partial", "gitignore decode failed"
    channel = matchers_for(pack)["file"]
    lines = [line.strip() for line in content.splitlines() if line.strip() and not line.startswith("#")]
    hits: list[Hit] = []
    if channel.regex is not None:
        for line in lines:
            for match in channel.regex.finditer(line):
                if match.lastgroup is None:
                    continue
                hits.append(
                    Hit(channel.names[match.lastgroup], "file", {"path": line, "source": "gitignore"})
                )
    return hits, "ok", None


def detect_files(deps: Deps, full_name: str, pack: Pack) -> ChannelResult:
    try:
        presence = fetch_tree(deps.client, full_name, limiter=deps.limiter, token_id=deps.token_id)
    except RequestFailed as exc:
        if exc.status == 404:
            return ChannelResult(status="unavailable", note="repo not found")
        return ChannelResult(status="partial", note=f"tree fetch failed: {exc.status}")
    hits = match_files(pack, presence)
    status = "partial" if presence.truncated else "ok"
    note = "tree truncated" if presence.truncated else None
    ignore_hits, ignore_status, ignore_note = _gitignore_hits(deps, full_name, pack)
    hits.extend(ignore_hits)
    if ignore_status == "partial":
        status = "partial"
    if ignore_note:
        note = f"{note}; {ignore_note}" if note else ignore_note
    return ChannelResult(hits=dedupe_hits(hits), status=status, note=note)
```

`RequestFailed` is defined in `discover.search_shards`; `fetch_tree` re-exports it, but import from the defining module as above.

- [ ] **Step 4: Run tests to verify pass**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_detect_channels.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/detect/channels.py tests/unit/test_detect_channels.py
git commit -m "feat: file channel detection"
```

---

### Task 5: Branch, label, and bot channels (REST)

**Files:**
- Modify: `src/detect/channels.py`
- Test: `tests/unit/test_detect_channels.py`

**Interfaces:**
- Consumes: `request_with_retry` from `lib.gh_client`.
- Produces: `detect_branches`, `detect_labels`, `detect_bots` with the same signature/return as `detect_files`.

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_detect_channels.py`:

```python
from detect.channels import detect_bots, detect_branches, detect_labels
from lib.gh_client import API_BASE


def paged(items):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path in {
            "/repos/octo/repo/branches",
            "/repos/octo/repo/labels",
            "/repos/octo/repo/contributors",
        }
        return httpx.Response(200, json=items)

    return handler


def test_branch_label_and_bot_channels():
    branches = [{"name": "claude/add-tests"}, {"name": "main"}]
    labels = [{"name": "ai-generated"}]
    contributors = [{"login": "dependabot[bot]"}]
    assert [h.agent for h in detect_branches(Deps(paged(branches)), "octo/repo", PACK).hits] == ["claude"]
    assert [h.agent for h in detect_labels(Deps(paged(labels)), "octo/repo", PACK).hits] == ["claude"]
    assert [h.agent for h in detect_bots(Deps(paged(contributors)), "octo/repo", PACK).hits] == ["dependabot"]


def test_branch_channel_404_unavailable():
    def handler(request):
        return httpx.Response(404, json={"message": "Not Found"})

    assert detect_branches(Deps(handler), "octo/gone", PACK).status == "unavailable"
```

- [ ] **Step 2: Run to verify failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_detect_channels.py -q`
Expected: FAIL (`ImportError: cannot import name 'detect_branches'`).

- [ ] **Step 3: Implement the three detectors**

Add to `src/detect/channels.py` (merge the attribution import; the `match_*` helpers were defined in Task 3 and `API_BASE` / `request_with_retry` are already imported):

```python
from detect.attribution import match_bots, match_branches, match_labels


def _paginated_names(deps: Deps, url: str, key: str) -> tuple[list[str], str, str | None]:
    names: list[str] = []
    page = 1
    while page <= 10:
        response = request_with_retry(
            deps.client,
            "GET",
            f"{url}?per_page=100&page={page}",
            limiter=deps.limiter,
            token_id=deps.token_id,
        )
        if response.status_code == 404:
            return names, "unavailable", "repo not found"
        if response.status_code != 200:
            return names, "partial", f"HTTP {response.status_code}"
        payload = response.json()
        if not isinstance(payload, list):
            return names, "partial", "unexpected payload"
        names.extend(str(item.get(key) or "") for item in payload if isinstance(item, dict))
        if len(payload) < 100:
            break
        page += 1
    return names, "ok", None


def detect_branches(deps: Deps, full_name: str, pack: Pack) -> ChannelResult:
    names, status, note = _paginated_names(deps, f"{API_BASE}/repos/{full_name}/branches", "name")
    hits = match_branches(pack, names)
    return ChannelResult(hits=dedupe_hits(hits), status=status, note=note)


def detect_labels(deps: Deps, full_name: str, pack: Pack) -> ChannelResult:
    names, status, note = _paginated_names(deps, f"{API_BASE}/repos/{full_name}/labels", "name")
    hits = match_labels(pack, names)
    return ChannelResult(hits=dedupe_hits(hits), status=status, note=note)


def detect_bots(deps: Deps, full_name: str, pack: Pack) -> ChannelResult:
    names, status, note = _paginated_names(
        deps, f"{API_BASE}/repos/{full_name}/contributors", "login"
    )
    hits = match_bots(pack, names)
    if status == "ok":
        note = "presence-only: top 100 contributors, absence is not proof"
    return ChannelResult(hits=dedupe_hits(hits), status=status, note=note)
```

Remove the now-duplicated `from detect.attribution import Hit, dedupe_hits, match_files` line by merging imports.

- [ ] **Step 4: Run tests to verify pass**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_detect_channels.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/detect/channels.py tests/unit/test_detect_channels.py
git commit -m "feat: branch, label, and bot channels"
```

---

### Task 6: GraphQL history channel + `graphql` limiter bucket

**Files:**
- Modify: `src/detect/channels.py`
- Modify: `src/lib/gh_client.py` (`resource_for_url`, ~:53)
- Modify: `src/limiter/buckets.py` (`RESOURCE_SPECS`, ~:7)
- Test: `tests/unit/test_detect_channels.py`, `tests/unit/test_buckets.py`, `tests/unit/test_gh_client.py`

**Interfaces:**
- Produces: `detect_history(deps, full_name, pack, *, limit=100) -> dict[str, ChannelResult]` with keys `author`, `commit_trailer`, `bot`; one GraphQL call serves all three.
- Produces: `resource_for_url("https://api.github.com/graphql") == "graphql"`; limiter spec `("graphql", (5000, 3600.0))`.

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_detect_channels.py`:

```python
from detect.channels import detect_history


def graphql_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/graphql"
    body = json.loads(request.content)
    assert body["variables"]["limit"] == 100
    return httpx.Response(
        200,
        json={
            "data": {
                "repository": {
                    "defaultBranchRef": {"name": "main"},
                    "object": {
                        "history": {
                            "nodes": [
                                {
                                    "message": "feat: x\n\nCo-authored-by: Cursor <cursoragent@x>",
                                    "author": {"name": "Jane", "email": "noreply@anthropic.com", "user": {"login": "jane"}},
                                    "committer": {"name": "Jane", "email": "noreply@anthropic.com", "user": {"login": "jane"}},
                                },
                                {
                                    "message": "chore: bump",
                                    "author": {"name": "bot", "email": "b@x", "user": {"login": "dependabot[bot]"}},
                                    "committer": {"name": "bot", "email": "b@x", "user": {"login": "dependabot[bot]"}},
                                },
                            ]
                        }
                    },
                }
            }
        },
    )


def test_history_channels_share_one_call():
    result = detect_history(Deps(graphql_handler), "octo/repo", PACK, limit=100)
    assert [h.agent for h in result["author"].hits] == ["claude"]
    assert [h.agent for h in result["commit_trailer"].hits] == ["cursor"]
    assert [h.agent for h in result["bot"].hits] == ["dependabot"]


def test_history_null_repository_is_unavailable():
    def handler(request):
        return httpx.Response(200, json={"data": {"repository": None}})

    result = detect_history(Deps(handler), "octo/gone", PACK)
    assert result["author"].status == "unavailable"
```

Add to `tests/unit/test_buckets.py`:

```python
def test_graphql_bucket_spec():
    from limiter.buckets import RESOURCE_SPECS

    assert RESOURCE_SPECS["graphql"] == (5000, 3600.0)
```

Add to `tests/unit/test_gh_client.py`:

```python
def test_resource_for_graphql():
    from lib.gh_client import resource_for_url

    assert resource_for_url("https://api.github.com/graphql") == "graphql"
```

- [ ] **Step 2: Run to verify failures**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_detect_channels.py tests/unit/test_buckets.py tests/unit/test_gh_client.py -q`
Expected: FAIL (missing `detect_history`, `graphql` key/spec).

- [ ] **Step 3: Implement**

`src/lib/gh_client.py`:

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

`src/limiter/buckets.py`:

```python
RESOURCE_SPECS: dict[str, tuple[int, float]] = {
    "search": (30, 60.0),
    "core": (5000, 3600.0),
    "code_search": (10, 60.0),
    "graphql": (5000, 3600.0),
}
```

`src/detect/channels.py`:

```python
import json

from detect.attribution import match_author_identities, match_bots, match_commit_messages

_HISTORY_QUERY = """
query($owner: String!, $name: String!, $limit: Int!) {
  repository(owner: $owner, name: $name) {
    object(expression: "HEAD") {
      ... on Commit {
        history(first: $limit) {
          nodes {
            message
            author { name email user { login } }
            committer { name email user { login } }
          }
        }
      }
    }
  }
}
"""


def _graphql(deps: Deps, query: str, variables: dict) -> tuple[dict | None, str, str | None]:
    response = request_with_retry(
        deps.client,
        "POST",
        f"{API_BASE}/graphql",
        json_body={"query": query, "variables": variables},
        limiter=deps.limiter,
        token_id=deps.token_id,
    )
    if response.status_code != 200:
        return None, "partial", f"HTTP {response.status_code}"
    payload = response.json()
    if payload.get("errors"):
        return payload, "partial", "graphql errors"
    return payload, "ok", None


def detect_history(deps: Deps, full_name: str, pack: Pack, *, limit: int = 100) -> dict[str, ChannelResult]:
    owner, _, name = full_name.partition("/")
    payload, status, note = _graphql(deps, _HISTORY_QUERY, {"owner": owner, "name": name, "limit": limit})
    empty = {key: ChannelResult(status=status, note=note) for key in ("author", "commit_trailer", "bot")}
    if payload is None:
        return empty
    repository = (payload.get("data") or {}).get("repository")
    if repository is None:
        return {key: ChannelResult(status="unavailable", note="repo not found") for key in empty}
    nodes = (
        ((repository.get("object") or {}).get("history") or {}).get("nodes") or []
    )
    identities: list[str] = []
    messages: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        messages.append(str(node.get("message") or ""))
        for key in ("author", "committer"):
            person = node.get(key) or {}
            login = (person.get("user") or {}).get("login") or ""
            identities.append(f"{person.get('name') or ''} <{person.get('email') or ''}> {login}".strip())
    return {
        "author": ChannelResult(hits=dedupe_hits(match_author_identities(pack, identities)), status=status, note=note),
        "commit_trailer": ChannelResult(hits=dedupe_hits(match_commit_messages(pack, messages)), status=status, note=note),
        "bot": ChannelResult(
            hits=dedupe_hits(match_bots(pack, identities)),
            status=status,
            note="window: last 100 commits; generic bot evidence only",
        ),
    }
```

- [ ] **Step 4: Run tests to verify pass**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_detect_channels.py tests/unit/test_buckets.py tests/unit/test_gh_client.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/detect/channels.py src/lib/gh_client.py src/limiter/buckets.py tests/unit/test_detect_channels.py tests/unit/test_buckets.py tests/unit/test_gh_client.py
git commit -m "feat: graphql history channel and graphql rate bucket"
```

---

### Task 7: PR metadata channel (GraphQL)

**Files:**
- Modify: `src/detect/channels.py`
- Test: `tests/unit/test_detect_channels.py`

**Interfaces:**
- Produces: `detect_prs(deps, full_name, pack, *, limit=100) -> ChannelResult`; evidence uses `match_pr_metadata` (`pr`, `head`, `label`, or `snippet`).

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_detect_channels.py`:

```python
from detect.channels import detect_prs


def pr_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    assert body["variables"]["limit"] == 100
    return httpx.Response(
        200,
        json={
            "data": {
                "repository": {
                    "pullRequests": {
                        "nodes": [
                            {
                                "number": 7,
                                "headRefName": "cursor/fix",
                                "bodyText": "task: https://chatgpt.com/codex/tasks",
                                "labels": {"nodes": [{"name": "ai-generated"}]},
                            }
                        ]
                    }
                }
            }
        },
    )


def test_detect_prs_matches_branch_label_and_codex_marker():
    result = detect_prs(Deps(pr_handler), "octo/repo", PACK, limit=100)
    assert result.status == "ok"
    assert {(h.agent, h.evidence["pr"]) for h in result.hits} == {("cursor", 7), ("claude", 7), ("Codex", 7)}
```

- [ ] **Step 2: Run to verify failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_detect_channels.py::test_detect_prs_matches_branch_label_and_body -q`
Expected: FAIL (`cannot import name 'detect_prs'`).

- [ ] **Step 3: Implement `detect_prs`**

Add to `src/detect/channels.py`:

```python
from detect.attribution import match_pr_metadata

_PR_QUERY = """
query($owner: String!, $name: String!, $limit: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: $limit, orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes {
        number
        headRefName
        bodyText
        labels(first: 20) { nodes { name } }
      }
    }
  }
}
"""


def detect_prs(deps: Deps, full_name: str, pack: Pack, *, limit: int = 100) -> ChannelResult:
    owner, _, name = full_name.partition("/")
    payload, status, note = _graphql(deps, _PR_QUERY, {"owner": owner, "name": name, "limit": limit})
    if payload is None:
        return ChannelResult(status=status, note=note)
    repository = (payload.get("data") or {}).get("repository")
    if repository is None:
        return ChannelResult(status="unavailable", note="repo not found")
    nodes = (repository.get("pullRequests") or {}).get("nodes") or []
    prs = [
        {
            "number": node.get("number"),
            "headRefName": node.get("headRefName"),
            "body": node.get("bodyText"),
            "labels": [label.get("name") for label in (node.get("labels") or {}).get("nodes") or []],
        }
        for node in nodes
        if isinstance(node, dict)
    ]
    return ChannelResult(
        hits=dedupe_hits(match_pr_metadata(pack, prs)),
        status=status,
        note="window: most recent 100 PRs",
    )
```

- [ ] **Step 4: Run all channel tests**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_detect_channels.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/detect/channels.py tests/unit/test_detect_channels.py
git commit -m "feat: PR metadata channel"
```

---

## Phase 3 — Orchestration

### Task 8: Call/time estimate

**Files:**
- Create: `src/detect/estimate.py`
- Test: `tests/unit/test_detect_estimate.py`

**Interfaces:**
- Produces (used by Tasks 9, 10):
  - `estimate(candidates: int, channels: Sequence[str], *, tokens: int) -> dict` with keys `repos`, `rest_calls`, `graphql_calls`, `hours`, `per_channel`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_detect_estimate.py`:

```python
from __future__ import annotations

from detect.estimate import estimate


def test_estimate_all_channels():
    result = estimate(1000, ["file", "author", "branch", "label", "bot", "commit_trailer", "pr_metadata"], tokens=2)
    assert result["repos"] == 1000
    assert result["rest_calls"] == 6500  # file(tree+gitignore)=2 + branch 1 + label 1 + bot 1 + pr 1.5
    assert result["graphql_calls"] == 1000
    assert result["hours"] > 0
    assert result["per_channel"]["commit_trailer"] == 0


def test_estimate_top_n_and_single_channel():
    result = estimate(200, ["file"], tokens=1)
    assert result["rest_calls"] == 400
    assert result["graphql_calls"] == 0


def test_estimate_rejects_zero_tokens():
    import pytest

    with pytest.raises(ValueError, match="tokens"):
        estimate(10, ["file"], tokens=0)
```

- [ ] **Step 2: Run to verify failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_detect_estimate.py -q`
Expected: FAIL (`ModuleNotFoundError: detect.estimate`).

- [ ] **Step 3: Implement `src/detect/estimate.py`**

```python
from __future__ import annotations

from collections.abc import Sequence

CHANNELS = ("file", "author", "branch", "label", "bot", "commit_trailer", "pr_metadata")
REST_CALLS_PER_REPO = {
    "file": 2.0,  # tree + .gitignore contents
    "author": 0.0,
    "branch": 1.0,
    "label": 1.0,
    "bot": 1.0,
    "commit_trailer": 0.0,
    "pr_metadata": 1.5,
}
GRAPHQL_REPOS_PER_CALL = {"author": 1.0, "commit_trailer": 0.0, "bot": 0.0}
_CORE_PER_HOUR = 5000.0
_GRAPHQL_PER_HOUR = 5000.0
_CONCURRENCY = 10.0


def estimate(candidates: int, channels: Sequence[str], *, tokens: int) -> dict:
    if tokens <= 0:
        raise ValueError("tokens must be >= 1")
    unknown = [c for c in channels if c not in CHANNELS]
    if unknown:
        raise ValueError(f"unknown channels: {unknown}")
    per_channel = {c: (candidates * REST_CALLS_PER_REPO[c]) for c in channels}
    rest_calls = sum(per_channel.values())
    graphql_calls = sum(candidates * GRAPHQL_REPOS_PER_CALL[c] for c in channels)
    rest_hours = rest_calls / (_CORE_PER_HOUR * tokens)
    graphql_hours = graphql_calls / (_GRAPHQL_PER_HOUR * tokens)
    return {
        "repos": candidates,
        "rest_calls": int(rest_calls),
        "graphql_calls": int(graphql_calls),
        "per_channel": per_channel,
        "hours": round(max(rest_hours, graphql_hours), 2),
    }
```

(Concurrency does not change the steady-state rate cap in this model; keep it out of the formula and note it in the options-page copy.)

- [ ] **Step 4: Run to verify pass**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_detect_estimate.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/detect/estimate.py tests/unit/test_detect_estimate.py
git commit -m "feat: detection call/time estimate"
```

---

### Task 9: Orchestrator + executor integration + no-search invariant

**Files:**
- Create: `src/detect/orchestrator.py`
- Modify: `src/serve/executor.py` (`RunPayloadItem` ~:21, `RunPayload` ~:36, `_snapshot_item` ~:74, `create_run` ~:93, `_write_bundle` ~:167, `execute_run` ~:189)
- Test: `tests/unit/test_detect_orchestrator.py`

**Interfaces:**
- Consumes: `detect.channels.detect_*`, `detect.channels.CHANNELS`, `detect.packs.Pack`, `serve.executor.RunPayload`/`RunPayloadItem`, `store.models.DetectionEvidence`.
- Produces (used by Task 10):
  - `DetectSpec` protocol: object with `.source_run_id`, `.pack_version`, `.channels`, `.limit`, `.windows` (real class in Task 10; orchestrator accepts a `Mapping`-like dataclass defined in `src/detect/orchestrator.py` to stay import-independent).
  - `candidate_items(engine, source_run_id, limit) -> list[dict]`
  - `run_detection(deps, run_id, spec, pack) -> RunPayload`
  - Writes `detection_evidence` rows and returns `RunPayload` whose items carry `detection` rollups, `detections` = flat evidence rows for the CSV, `result_summary` = counts.

- [ ] **Step 1: Add `detection` to the executor payload + create_run kind**

Modify `src/serve/executor.py`:

```python
@dataclass
class RunPayloadItem:
    # existing fields unchanged
    detection: dict | None = None

@dataclass
class RunPayload:
    # existing fields unchanged
    result_summary: dict = field(default_factory=dict)
    detections: list[dict] = field(default_factory=list)
```

`_snapshot_item` adds `"detection": item.detection`. `create_run` gains keywords:

```python
def create_run(
    engine: Engine,
    filter_spec: dict,
    *,
    api_version: str,
    kind: str = "find",
    source_run_id: int | None = None,
) -> int:
    with engine.begin() as connection:
        run_id = connection.execute(
            insert(Runs)
            .values(
                filter_hash=_filter_hash(filter_spec),
                filter_spec=filter_spec,
                api_version=api_version,
                kind=kind,
                source_run_id=source_run_id,
            )
            .returning(Runs.id)
        ).scalar_one()
    return int(run_id)
```

`execute_run`: in the success branch, keep the existing `payload = ...`, `_snapshot_items(...)`, `_write_bundle(...)` calls and add `result_summary=payload.result_summary or None` to the existing `update(Runs)` values (alongside `incomplete_shards=...`):

```python
                    incomplete_shards=1 if payload.incomplete else 0,
                    result_summary=payload.result_summary or None,
```

`_write_bundle`: keep its existing body and append the detections write before returning:

```python
def _write_bundle(run: dict, payload: RunPayload, runs_root: str) -> str:
    # existing bundle.json + corpus.csv writes unchanged
    _write_corpus(directory / "corpus.csv", payload.items)
    if payload.detections:
        _write_detections(directory / "detections.csv", payload.detections)
    return f"{directory.as_posix()}/"


def _write_detections(path: Path, rows: list[dict]) -> None:
    fields = ["repo_id", "full_name", "agent", "channel", "pack_version", "evidence"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({**{k: _csv_cell(row.get(k)) for k in fields if k != "evidence"},
                             "evidence": json.dumps(row.get("evidence") or {}, ensure_ascii=False)})
```

- [ ] **Step 2: Write the failing orchestrator test**

Create `tests/unit/test_detect_orchestrator.py`:

```python
from __future__ import annotations

import json
import re

import httpx
from sqlalchemy import insert, text

from detect.orchestrator import DetectOptions, run_detection
from detect.packs import Pack, Pattern, freeze_pack
from discover.pipeline import Deps
from serve.executor import create_run
from store.models import RunItem, Runs

PACK = Pack(
    version="v1",
    agents={"claude": "Claude Code"},
    authors=(Pattern(r"noreply@anthropic\.com", "claude"),),
    files=(Pattern(r"CLAUDE\.md", "claude"),),
    branches=(Pattern("claude/", "claude"),),
    labels=(Pattern("ai-generated", "claude"),),
    bots=(Pattern(r"dependabot\[bot\]", "dependabot"),),
)

ALL = ("file", "branch", "label", "bot", "author", "commit_trailer", "pr_metadata")


def seed_source(engine, stars=((10, 1),)) -> int:
    with engine.begin() as connection:
        source_id = connection.execute(
            insert(Runs)
            .values(
                filter_hash="src-hash",
                filter_spec={"q": "language:rust"},
                api_version="2022-11-28",
                status="done",
            )
            .returning(Runs.id)
        ).scalar_one()
        connection.execute(
            insert(RunItem),
            [
                {
                    "run_id": source_id,
                    "repo_id": repo_id,
                    "full_name": f"octo/repo{repo_id}",
                    "stargazers": star,
                    "virtuals": {},
                }
                for star, repo_id in stars
            ],
        )
    return int(source_id)


def handler(seen: list[str]):
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        path = request.url.path
        if re.fullmatch(r"/repos/[^/]+/[^/]+", path):
            return httpx.Response(200, json={"default_branch": "main"})
        if "/git/trees/" in path:
            return httpx.Response(
                200,
                json={"sha": "h", "truncated": False, "tree": [{"path": "CLAUDE.md", "type": "blob"}]},
            )
        if path.startswith("/repos/") and path.endswith("/contents/.gitignore"):
            return httpx.Response(200, json={"encoding": "base64", "content": ""})
        if path.endswith("/branches"):
            return httpx.Response(200, json=[{"name": "claude/x"}])
        if path.endswith("/labels"):
            return httpx.Response(200, json=[{"name": "ai-generated"}])
        if path.endswith("/contributors"):
            return httpx.Response(200, json=[{"login": "dependabot[bot]"}])
        if path == "/graphql":
            body = json.loads(request.content)
            if "history" in body["query"]:
                return httpx.Response(
                    200, json={"data": {"repository": {"object": {"history": {"nodes": []}}}}}
                )
            return httpx.Response(
                200, json={"data": {"repository": {"pullRequests": {"nodes": []}}}}
            )
        raise AssertionError(f"unexpected path {path}")

    return handle


def deps_for(client, engine) -> Deps:
    return Deps(client=client, engine=engine, redis=None, limiter=None, token_id="t", audit_buffer=None)


def make_detect_run(engine, source_id: int) -> int:
    return create_run(
        engine,
        {"gitcrawl_detect": 1},
        api_version="2022-11-28",
        kind="detect",
        source_run_id=source_id,
    )


def test_run_detection_writes_evidence_and_never_searches(clean_engine):
    engine = clean_engine
    freeze_pack(engine, PACK)
    source_id = seed_source(engine)
    detect_id = make_detect_run(engine, source_id)
    seen: list[str] = []
    deps = deps_for(httpx.Client(transport=httpx.MockTransport(handler(seen))), engine)
    spec = DetectOptions(
        source_run_id=source_id,
        pack_version="v1",
        channels=ALL,
        limit=None,
        windows={"commits": 100, "prs": 100},
    )
    payload = run_detection(deps, detect_id, spec, PACK)
    assert payload.fetched == 1
    assert payload.result_summary["repos_scanned"] == 1
    assert payload.result_summary["agents"]["claude"] >= 3  # file + branch + label
    assert payload.result_summary["bots"]["dependabot"] == 1
    assert payload.items[0].detection["agents"] == ["claude"]  # generic bots are not adoption signals
    with engine.connect() as connection:
        count = connection.scalar(text("SELECT count(*) FROM detection_evidence"))
    assert count >= 4
    assert len(payload.detections) == count
    assert not any("/search/" in path or "since" in path for path in seen)


def test_top_n_is_deterministic(clean_engine):
    engine = clean_engine
    freeze_pack(engine, PACK)
    source_id = seed_source(engine, stars=((1, 1), (3, 2), (2, 3)))
    detect_id = make_detect_run(engine, source_id)
    seen: list[str] = []
    deps = deps_for(httpx.Client(transport=httpx.MockTransport(handler(seen))), engine)
    spec = DetectOptions(
        source_run_id=source_id,
        pack_version="v1",
        channels=("file",),
        limit=2,
        windows={"commits": 100, "prs": 100},
    )
    payload = run_detection(deps, detect_id, spec, PACK)
    assert [item.repo_id for item in payload.items] == [2, 3]
```

- [ ] **Step 3: Run to verify failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_detect_orchestrator.py -q`
Expected: FAIL (`ModuleNotFoundError: detect.orchestrator`).

- [ ] **Step 4: Implement `src/detect/orchestrator.py`**

```python
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

from detect.channels import (
    detect_bots,
    detect_branches,
    detect_files,
    detect_history,
    detect_labels,
    detect_prs,
)
from detect.packs import Pack
from discover.pipeline import Deps
from serve.executor import RunPayload, RunPayloadItem
from store.models import DetectionEvidence, RunItem


@dataclass(frozen=True)
class DetectOptions:
    source_run_id: int
    pack_version: str
    channels: tuple[str, ...]
    limit: int | None = None
    windows: Mapping[str, int] = field(default_factory=lambda: {"commits": 100, "prs": 100})


def candidate_items(engine: Engine, source_run_id: int, limit: int | None) -> list[dict]:
    statement = (
        select(RunItem)
        .where(RunItem.run_id == source_run_id)
        .order_by(RunItem.stargazers.desc().nullslast(), RunItem.repo_id.asc())
    )
    if limit is not None:
        statement = statement.limit(limit)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(statement).mappings()]


def run_detection(deps: Deps, run_id: int, spec: DetectOptions, pack: Pack) -> RunPayload:
    candidates = candidate_items(deps.engine, spec.source_run_id, spec.limit)
    items: list[RunPayloadItem] = []
    evidence_rows: list[dict] = []
    detections: list[dict] = []
    agent_counts: Counter[str] = Counter()
    bot_counts: Counter[str] = Counter()
    channel_counts: Counter[str] = Counter()
    unavailable = 0
    partial = False
    failures: list[str] = []
    for candidate in candidates:
        repo_id = int(candidate["repo_id"])
        full_name = str(candidate["full_name"])
        hits = []
        statuses: dict[str, str] = {}
        notes: list[str] = []
        for channel in spec.channels:
            try:
                if channel == "file":
                    result = detect_files(deps, full_name, pack)
                elif channel == "branch":
                    result = detect_branches(deps, full_name, pack)
                elif channel == "label":
                    result = detect_labels(deps, full_name, pack)
                elif channel == "bot":
                    result = detect_bots(deps, full_name, pack)
                elif channel == "pr_metadata":
                    result = detect_prs(deps, full_name, pack, limit=spec.windows["prs"])
                elif channel in {"author", "commit_trailer"}:
                    shared = detect_history(deps, full_name, pack, limit=spec.windows["commits"])
                    result = shared[channel]
                else:
                    continue
            except Exception as exc:  # channel isolation: one failure never kills the run
                result = None
                statuses[channel] = "partial"
                notes.append(f"{channel}: {type(exc).__name__}")
                failures.append(f"{full_name}:{channel}")
            if result is None:
                continue
            statuses[channel] = result.status
            if result.status == "unavailable":
                unavailable += 1
            if result.status == "partial":
                partial = True
            if result.note:
                notes.append(f"{channel}: {result.note}")
            for hit in result.hits:
                hits.append(hit)
                if hit.channel == "bot":
                    bot_counts[hit.agent] += 1
                else:
                    agent_counts[hit.agent] += 1
                channel_counts[hit.channel] += 1
                row = {
                    "run_id": run_id,
                    "repo_id": repo_id,
                    "full_name": full_name,
                    "agent": hit.agent,
                    "channel": hit.channel,
                    "evidence": hit.evidence,
                    "pack_version": pack.version,
                }
                evidence_rows.append(row)
                detections.append(row)
        agents = sorted({hit.agent for hit in hits if hit.channel != "bot"})
        items.append(
            RunPayloadItem(
                repo_id=repo_id,
                full_name=full_name,
                stargazers=candidate.get("stargazers"),
                pushed_at=_iso(candidate.get("pushed_at")),
                archived=candidate.get("archived"),
                language=candidate.get("language"),
                license_spdx=candidate.get("license_spdx"),
                country_iso=candidate.get("country_iso"),
                geo_confidence=candidate.get("geo_confidence"),
                virtuals=candidate.get("virtuals") or {},
                detection={"agents": agents, "channels": statuses, "notes": notes},
            )
        )
    if evidence_rows:
        insert_rows = [{k: v for k, v in row.items() if k != "full_name"} for row in evidence_rows]
        with deps.engine.begin() as connection:
            connection.execute(insert(DetectionEvidence), insert_rows)
    summary = {
        "repos_scanned": len(items),
        "repos_unavailable": unavailable,
        "signals": len(evidence_rows),
        "agents": dict(agent_counts),
        "bots": dict(bot_counts),
        "channels": dict(channel_counts),
        "pack_version": pack.version,
        "limits": dict(spec.windows),
    }
    return RunPayload(
        total_count=len(items),
        items=items,
        incomplete=partial,
        fetched=len(items),
        result_summary=summary,
        detections=detections,
    )


def _iso(value) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else value
```

Note: the implementation above already strips `full_name` from `DetectionEvidence` insert rows (the evidence table has no `full_name` column).

- [ ] **Step 5: Run orchestrator + executor tests**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/unit/test_detect_orchestrator.py tests/unit/test_executor_unit.py tests/integration/test_executor.py -q`
Expected: PASS, including the no-search assertion and existing executor tests (backwards compatible defaults).

- [ ] **Step 6: Commit**

```bash
git add src/detect/orchestrator.py src/serve/executor.py tests/unit/test_detect_orchestrator.py
git commit -m "feat: detection orchestrator and detect run payloads"
```

---

## Phase 4 — Serve and UI

### Task 10: Detect spec validation + options/estimate/create routes

**Files:**
- Create: `src/serve/detect_spec.py`, `src/serve/detect.py`
- Modify: `src/serve/app.py` (call `register_detect` next to `register_pages`, ~:712)
- Test: `tests/contract/test_detect_spec.py`, `tests/contract/test_detect_routes.py`

**Interfaces:**
- Produces: `DetectSpecError(errors, hints)`; `DetectSpec` dataclass; `parse_detect_spec(doc) -> DetectSpec`.
- Produces routes: `GET /runs/{run_id}/detect` (options page), `POST /runs/{run_id}/detect` (create + enqueue + 303), `GET /runs/{run_id}/detect-estimate` (htmx fragment).
- Produces in `app.create_app`: new keyword `detect_runner_factory: Callable[[Engine], Callable[[int, dict], RunPayload]] | None = None` for tests.

- [ ] **Step 1: Write failing spec tests**

Create `tests/contract/test_detect_spec.py`:

```python
from __future__ import annotations

import pytest

from serve.detect_spec import DetectSpecError, parse_detect_spec


def test_parse_minimal_spec():
    spec = parse_detect_spec(
        {
            "gitcrawl_detect": 1,
            "source_run_id": 5,
            "pack_version": "v1",
            "channels": ["file", "author"],
        }
    )
    assert spec.source_run_id == 5
    assert spec.channels == ("file", "author")
    assert spec.limit is None
    assert spec.windows == {"commits": 100, "prs": 100}


def test_unknown_channel_and_hint():
    with pytest.raises(DetectSpecError) as exc:
        parse_detect_spec(
            {"gitcrawl_detect": 1, "source_run_id": 5, "pack_version": "v1", "channels": ["files"]}
        )
    assert any("channel" in e for e in exc.value.errors)
    assert exc.value.hints


def test_limit_and_windows_bounds():
    with pytest.raises(DetectSpecError):
        parse_detect_spec(
            {
                "gitcrawl_detect": 1,
                "source_run_id": 5,
                "pack_version": "v1",
                "channels": ["file"],
                "limit": 0,
            }
        )
    with pytest.raises(DetectSpecError):
        parse_detect_spec(
            {
                "gitcrawl_detect": 1,
                "source_run_id": 5,
                "pack_version": "v1",
                "channels": ["file"],
                "windows": {"commits": 500},
            }
        )
```

- [ ] **Step 2: Implement `src/serve/detect_spec.py`**

```python
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from detect.channels import CHANNELS


class DetectSpecError(ValueError):
    def __init__(self, errors, hints=()):
        self.errors = tuple(errors)
        self.hints = tuple(hints)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class DetectSpec:
    source_run_id: int
    pack_version: str
    channels: tuple[str, ...]
    limit: int | None
    windows: Mapping[str, int]
    raw: Mapping[str, object]


def parse_detect_spec(doc: Mapping[str, object]) -> DetectSpec:
    errors: list[str] = []
    hints: list[str] = []
    if doc.get("gitcrawl_detect") != 1:
        errors.append("`gitcrawl_detect` must be 1")
    source_run_id = doc.get("source_run_id")
    if not isinstance(source_run_id, int) or source_run_id <= 0:
        errors.append("`source_run_id` must be a positive integer")
    pack_version = doc.get("pack_version")
    if not isinstance(pack_version, str) or not pack_version:
        errors.append("`pack_version` is required")
    raw_channels = doc.get("channels")
    channels: tuple[str, ...] = ()
    if not isinstance(raw_channels, list) or not raw_channels:
        errors.append("`channels` must be a non-empty list")
        hints.append(f"pick from: {', '.join(CHANNELS)}")
    else:
        bad = [str(c) for c in raw_channels if c not in CHANNELS]
        if bad:
            errors.append(f"unknown channel(s): {', '.join(bad)}")
            hints.append(f"valid channels: {', '.join(CHANNELS)}")
        channels = tuple(dict.fromkeys(str(c) for c in raw_channels if c in CHANNELS))
    limit = doc.get("limit")
    if limit is not None and (not isinstance(limit, int) or limit <= 0):
        errors.append("`limit` must be a positive integer or null")
    windows = {"commits": 100, "prs": 100}
    raw_windows = doc.get("windows")
    if raw_windows is not None:
        if not isinstance(raw_windows, Mapping):
            errors.append("`windows` must be an object")
        else:
            for key in ("commits", "prs"):
                value = raw_windows.get(key, windows[key])
                if not isinstance(value, int) or not 1 <= value <= 100:
                    errors.append(f"`windows.{key}` must be an integer 1-100")
                else:
                    windows[key] = value
    if errors:
        raise DetectSpecError(errors, hints)
    return DetectSpec(
        source_run_id=source_run_id,  # type: ignore[arg-type]
        pack_version=pack_version,  # type: ignore[arg-type]
        channels=channels,
        limit=limit,  # type: ignore[arg-type]
        windows=windows,
        raw=dict(doc),
    )
```

- [ ] **Step 3: Run spec tests**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/contract/test_detect_spec.py -q`
Expected: PASS.

- [ ] **Step 4: Write failing route tests**

Create `tests/contract/test_detect_routes.py`. Copy the module fixtures/helpers from `tests/contract/test_pages.py` verbatim: the autouse `schema` fixture (:41), `clean` (:47), `make_client` (:90), `healthy_client` (:106), `seed_run` (:118), and the CSRF-token extraction logic used by its POST tests. Then add:

```python
from sqlalchemy import text

from serve.executor import RunPayload, create_run


def fake_detect_factory(engine, spec):
    def runner(run_id: int, spec_doc: dict) -> RunPayload:
        return RunPayload(total_count=0, items=[], result_summary={"repos_scanned": 0})

    return runner


@pytest.fixture
def client_with_source(clean, tmp_path, monkeypatch):
    source_id, _ = seed_run(clean, tmp_path)
    client = healthy_client(clean, tmp_path, monkeypatch, detect_runner_factory=fake_detect_factory)
    return client, source_id


def csrf_token(client) -> str:
    response = client.get("/")
    marker = 'name="csrf-token" content="'
    start = response.text.index(marker) + len(marker)
    return response.text[start : response.text.index('"', start)]


def test_options_page_lists_channels(client_with_source):
    client, source_id = client_with_source
    response = client.get(f"/runs/{source_id}/detect")
    assert response.status_code == 200
    for channel in ("file", "author", "branch", "label", "bot", "commit_trailer", "pr_metadata"):
        assert channel in response.text
    assert "Select all" in response.text


def test_post_detect_creates_run_and_redirects(client_with_source, clean):
    client, source_id = client_with_source
    token = csrf_token(client)
    response = client.post(
        f"/runs/{source_id}/detect",
        data={"pack_version": "v1", "channels": ["file", "branch"], "limit": ""},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    run_id = int(response.headers["location"].rsplit("/", 1)[1])
    with clean.connect() as connection:
        row = connection.execute(
            text("SELECT kind, source_run_id FROM runs WHERE id = :id"), {"id": run_id}
        ).one()
    assert row.kind == "detect"
    assert row.source_run_id == source_id


def test_detect_on_a_detect_run_is_rejected(client_with_source, clean):
    client, source_id = client_with_source
    detect_id = create_run(
        clean, {"gitcrawl_detect": 1}, api_version="2022-11-28", kind="detect", source_run_id=source_id
    )
    token = csrf_token(client)
    response = client.post(
        f"/runs/{detect_id}/detect",
        data={"pack_version": "v1", "channels": ["file"], "limit": ""},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_bad_channel_is_a_local_400(client_with_source):
    client, source_id = client_with_source
    token = csrf_token(client)
    response = client.post(
        f"/runs/{source_id}/detect",
        data={"pack_version": "v1", "channels": ["files"], "limit": ""},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "unknown channel" in response.text
```

Match the existing CSRF handling used by other POST tests in `tests/contract/test_console_api.py` (copy the token cookie flow exactly from there rather than inventing it).

- [ ] **Step 5: Implement `src/serve/detect.py` and register in `app.py`**

`src/serve/detect.py`:

```python
from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine

from detect.channels import CHANNELS
from detect.estimate import estimate
from detect.orchestrator import run_detection
from detect.packs import load_frozen
from lib.gh_client import API_VERSION, load_tokens
from serve import pages
from serve.detect_spec import DetectSpec, DetectSpecError, parse_detect_spec
from serve.executor import Runner, RunExecutor, RunPayload, create_run
from serve.runner import build_deps
from store.models import RunItem, Runs

DetectRunnerFactory = Callable[[Engine, DetectSpec], Runner]

_ELIGIBLE_STATUSES = {"done", "partial"}


def _count_candidates(engine: Engine, run_id: int, limit: int | None) -> int:
    with engine.connect() as connection:
        total = connection.scalar(
            select(func.count()).select_from(RunItem).where(RunItem.run_id == run_id)
        ) or 0
    total = int(total)
    return total if limit is None else min(limit, total)


def register_detect(
    application: FastAPI,
    *,
    engine_factory: Callable[[], Engine],
    executor_factory: Callable[[], RunExecutor],
    detect_runner_factory: DetectRunnerFactory | None = None,
) -> None:
    def runner_for(engine: Engine, spec: DetectSpec) -> Runner:
        if detect_runner_factory is not None:
            return detect_runner_factory(engine, spec)

        def runner(run_id: int, _spec_doc: dict) -> RunPayload:
            deps = build_deps(engine)
            pack = load_frozen(engine, spec.pack_version)
            return run_detection(deps, run_id, spec, pack)

        return runner

    def source_run(engine: Engine, run_id: int):
        with engine.connect() as connection:
            return connection.execute(select(Runs).where(Runs.id == run_id)).mappings().one_or_none()

    def eligible(row) -> bool:
        return row is not None and row["kind"] == "find" and row["status"] in _ELIGIBLE_STATUSES

    @application.get("/runs/{run_id}/detect", response_class=HTMLResponse)
    def detect_options(request: Request, run_id: int):
        engine = engine_factory()
        row = source_run(engine, run_id)
        packs: list[str] = []
        if eligible(row):
            with engine.connect() as connection:
                packs = [
                    version
                    for (version,) in connection.execute(
                        text("SELECT version FROM trace_packs ORDER BY version DESC")
                    )
                ]
        return pages._templates.TemplateResponse(
            request,
            "detect_options.html",
            {
                "run": dict(row) if row else None,
                "eligible": eligible(row) and bool(packs),
                "packs": packs,
                "channels": list(CHANNELS),
            },
        )

    @application.get("/runs/{run_id}/detect-estimate", response_class=HTMLResponse)
    def detect_estimate(request: Request, run_id: int, channels: str = "", limit: int | None = None):
        engine = engine_factory()
        selected = [c for c in channels.split(",") if c in CHANNELS] or list(CHANNELS)
        count = _count_candidates(engine, run_id, limit)
        result = estimate(count, selected, tokens=max(1, len(load_tokens())))
        return pages._templates.TemplateResponse(
            request, "partials/detect_estimate.html", {"estimate": result}
        )

    @application.post("/runs/{run_id}/detect")
    async def detect_start(request: Request, run_id: int):
        if not await pages.validate_csrf(request):
            return HTMLResponse("CSRF", status_code=403)
        form = await request.form()
        engine = engine_factory()
        row = source_run(engine, run_id)
        if not eligible(row):
            return HTMLResponse("source run not eligible", status_code=400)
        limit_raw = form.get("limit")
        spec_doc = {
            "gitcrawl_detect": 1,
            "source_run_id": run_id,
            "pack_version": form.get("pack_version") or "",
            "channels": list(form.getlist("channels")),
            "limit": int(limit_raw) if isinstance(limit_raw, str) and limit_raw.strip() else None,
        }
        try:
            spec = parse_detect_spec(spec_doc)
        except DetectSpecError as exc:
            return HTMLResponse("; ".join(exc.errors), status_code=400)
        detect_id = create_run(
            engine, spec_doc, api_version=API_VERSION, kind="detect", source_run_id=run_id
        )
        executor_factory().submit(detect_id, runner_for(engine, spec))
        return RedirectResponse(f"/runs/{detect_id}", status_code=303)
```

`app.py`: import `register_detect` and call it immediately after the existing `register_pages(...)` call (`src/serve/app.py:712-721`):

```python
    register_detect(
        application,
        engine_factory=engine_for,
        executor_factory=executor_for,
        detect_runner_factory=detect_runner_factory,
    )
```

Add `detect_runner_factory: Callable[[Engine, DetectSpec], Runner] | None = None` to `create_app`'s signature (import the type from `serve.detect`) and pass it through to `register_detect`. `pages._templates` is module-private but stable; if you prefer, add `templates = _templates` to `pages.py` and use that instead.

When copying `make_client`/`healthy_client`, extend `make_client` with a `detect_runner_factory=None` keyword and forward it to `create_app`, and extend the copied `clean` fixture's `TRUNCATE` statement to include `detection_evidence, trace_packs`.

Also create the two templates used here: `src/serve/templates/detect_options.html` (extends `base.html`; source run summary; pack `<select name="pack_version">`; a `Select all` checkbox mirroring the channel checkboxes; channel checkbox rows with calls/repo copy; top-N `<input type="number" name="limit">`; an estimate panel that htmx-polls `/runs/{id}/detect-estimate?channels=...&limit=...`; disabled state when `not eligible`; link to the docs tier table) and `src/serve/templates/partials/detect_estimate.html` (renders `estimate.repos`, `estimate.rest_calls`, `estimate.graphql_calls`, `estimate.hours`).

- [ ] **Step 6: Run route + spec tests**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/contract/test_detect_spec.py tests/contract/test_detect_routes.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/serve/detect_spec.py src/serve/detect.py src/serve/app.py src/serve/templates/detect_options.html src/serve/templates/partials/detect_estimate.html tests/contract/test_detect_spec.py tests/contract/test_detect_routes.py
git commit -m "feat: detect options, estimate, and start routes"
```

---

### Task 11: Detect run detail page, evidence partial, find-run button/badge, exports

**Files:**
- Modify: `src/serve/detect.py`
- Modify: `src/serve/pages.py` (find run detail context: `_run_detail_summary` ~:377, `render_run_detail` if present)
- Modify: `src/serve/templates/run_detail.html`
- Modify: `src/serve/runs.py` (`export_bundle`, ~:173) if detect exports need `detections.csv` in the zip/json list
- Create: `src/serve/templates/detect_detail.html`, `partials/detect_status.html`, `partials/detect_evidence.html`
- Test: `tests/contract/test_detect_pages.py`

**Interfaces:**
- Produces routes: `GET /runs/{run_id}` must branch to `detect_detail.html` when `kind='detect'` (guarded inside the existing detail renderer or a dedicated `GET /runs/{run_id}/detect-view` — prefer a shared renderer branch); `GET /partials/runs/{run_id}/detect-status`; `GET /partials/runs/{run_id}/detect-evidence?repo_id=&agent=`.
- Produces: find run detail shows `Detect coding agent use` link and, when a detect run exists, `N agent signals` badge linking to the latest detect run.

- [ ] **Step 1: Write failing page tests**

Create `tests/contract/test_detect_pages.py`. Copy the module fixtures/helpers from `tests/contract/test_pages.py` verbatim (`schema` :41, `clean` :47, `make_client` :90, `healthy_client` :106, `seed_run` :118) and the CSRF-token extraction used by its POST tests, extending `clean`'s `TRUNCATE` to include `detection_evidence, trace_packs`. Then add:

```python
from sqlalchemy import insert, text

from serve.executor import create_run
from store.models import DetectionEvidence, RunItem


def seed_detect_run(clean, source_id: int, *, status: str = "done") -> int:
    detect_id = create_run(
        clean,
        {"gitcrawl_detect": 1},
        api_version="2022-11-28",
        kind="detect",
        source_run_id=source_id,
    )
    with clean.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO trace_packs (version, authors, files, branches, labels)"
                " VALUES ('v1', '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb)"
                " ON CONFLICT (version) DO NOTHING"
            )
        )
        connection.execute(
            insert(RunItem).values(
                run_id=detect_id,
                repo_id=1,
                full_name="octo/hello",
                stargazers=10,
                virtuals={},
                detection={
                    "agents": ["claude"],
                    "channels": {"file": "ok", "author": "ok"},
                    "notes": ["commit_trailer: window: last 100 commits"],
                },
            )
        )
        connection.execute(
            insert(DetectionEvidence).values(
                run_id=detect_id,
                repo_id=1,
                agent="claude",
                channel="file",
                evidence={"path": "CLAUDE.md"},
                pack_version="v1",
            )
        )
        connection.execute(
            text(
                "UPDATE runs SET status = :status, result_summary = '{\"repos_scanned\": 1,"
                " \"agents\": {\"claude\": 1}, \"channels\": {\"file\": 1},"
                " \"signals\": 1, \"pack_version\": \"v1\"}'::jsonb WHERE id = :id"
            ),
            {"status": status, "id": detect_id},
        )
    return detect_id


def test_find_run_shows_detect_button(clean, tmp_path, monkeypatch):
    source_id, _ = seed_run(clean, tmp_path)
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get(f"/runs/{source_id}")
    assert "Detect coding agent use" in response.text


def test_detect_run_detail_shows_summary_and_matrix(clean, tmp_path, monkeypatch):
    source_id, _ = seed_run(clean, tmp_path)
    detect_id = seed_detect_run(clean, source_id)
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get(f"/runs/{detect_id}")
    assert response.status_code == 200
    assert "Repos scanned" in response.text
    assert "claude" in response.text
    assert "last 100 commits".lower() in response.text.lower()
    evidence = client.get(f"/partials/runs/{detect_id}/detect-evidence?repo_id=1")
    assert evidence.status_code == 200
    assert "CLAUDE.md" in evidence.text


def test_detect_status_partial_reflects_queue(clean, tmp_path, monkeypatch):
    source_id, _ = seed_run(clean, tmp_path)
    detect_id = seed_detect_run(clean, source_id, status="queued")
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.get(f"/partials/runs/{detect_id}/detect-status")
    assert response.status_code == 200
    assert "queued" in response.text
```

- [ ] **Step 2: Run to verify failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/contract/test_detect_pages.py -q`
Expected: FAIL (missing UI strings/routes).

- [ ] **Step 3: Implement the detect detail and partials**

- `detect_detail.html` extends `base.html`, mirrors `run_detail.html` structure: status banner (`partials/detect_status.html`, htmx poll while `queued|running`), summary cards from `runs.result_summary` (repos scanned, unavailable, signals, % any signal, per-agent counts, per-channel counts, pack version, windows), per-repo table (paginated) with agent chips and an "evidence" expander loading `partials/detect_evidence.html` via htmx (`hx-get="/partials/runs/{id}/detect-evidence?repo_id=..."`).
- `detect_status.html` copies the `partials/status.html` polling contract (`hx-trigger="every 2s"`, stop at terminal states).
- `detect_evidence.html` groups `detection_evidence` rows by channel and renders `evidence.path|commit|branch|label|pr|snippet`.
- In `src/serve/pages.py`'s run-detail renderer (the `_run_detail_row` → `_run_detail_summary` → template path at :327-400): after loading the run row, branch on `row["kind"] == "detect"` before the find summary is built, and render `detect_detail.html` with `{"run": dict(row), "summary": row["result_summary"] or {}, "items": <the existing run_items page rows>}`. Add the detect button to the find-run HTML (only when `done|partial`) and the latest-detect badge via `SELECT id, result_summary FROM runs WHERE kind='detect' AND source_run_id=:id ORDER BY created_at DESC LIMIT 1`.
- Register the two partial routes in `src/serve/detect.py` (`register_detect`).
- `src/serve/runs.py::export_bundle`: include `detections.csv` in the archive listing for detect runs (read it from the bundle dir like `corpus.csv`).

- [ ] **Step 4: Run page tests + export tests**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/contract/test_detect_pages.py tests/contract/test_run_detail.py tests/contract/test_runs_export.py -q`
Expected: PASS.

- [ ] **Step 5: Re-pin the OpenAPI golden hash**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/golden/test_openapi_pin.py -q`
Expected: FAIL with the new hash; update `tests/golden/snapshots/openapi.sha256` to the printed value, re-run to PASS. Record the update in `docs/development-log.md`.

- [ ] **Step 6: Commit**

```bash
git add src/serve/detect.py src/serve/pages.py src/serve/runs.py src/serve/templates/detect_detail.html src/serve/templates/partials/detect_status.html src/serve/templates/partials/detect_evidence.html tests/contract/test_detect_pages.py tests/golden/snapshots/openapi.sha256 docs/development-log.md
git commit -m "feat: detect run detail, evidence drill-down, and export"
```

---

## Phase 5 — Docs and hardening

### Task 12: `docs/detection-tiers.md`, docs links, full-suite verification

**Files:**
- Create: `docs/detection-tiers.md`
- Modify: `design/research.md` (D13), `src/serve/templates/detect_options.html` (link)
- Test: full suite + coverage; optional live smoke (`tests/integration/test_golden_org.py` pattern)

**Interfaces:**
- Consumes: everything above.
- Produces: the documented tier boundary the operator asked for, linked from the UI.

- [ ] **Step 1: Write `docs/detection-tiers.md`**

Content (exact table):

| Signal / channel | Tier | Cost | What it cannot tell you |
|---|---|---|---|
| Files (`file`) | API-only full | 1 trees call (+0–3 contents for weighting) or 0 via ecosyste.ms | content semantics beyond pattern match; ground-truth "used vs present" |
| Branches (`branch`) | API-only full | 1 call/repo | whether the branch's PR was actually agent-authored beyond patterns |
| Labels (`label`) | API-only full | 1 call/repo | labels removed after merge; history not searched |
| Bots (`bot`) | API-only, presence | 1 call/repo (top 100 contributors) + history window | absence is not proof; only top-100 contributors |
| Authors (`author`) | API-only, windowed | shares 1 GraphQL call (last 100 commits) | older commits; full-history ratios |
| Commit trailers (`commit_trailer`) | API-only, windowed | shares 1 GraphQL call (last 100 commits) | trailers in commits older than the window |
| PR metadata (`pr_metadata`) | API-only, windowed | 1–2 GraphQL calls (recent 100 PRs) | full PR history; the 10k cap applies if widened |
| Diffstats, per-file first-added/LOC, `.gitignore`-visibility, ground-truth content audit | Clone required | shallow/file-only/windowed clones (FR-018) | — |
| Full windowed commit history, adoption dates/ratios, 2025+ PR bodies at scale | BigQuery required | BQ scan ($6.25/TiB, prune by `event_date`), API top-up | — |

Add a short intro: "v1 of agent detection is the API-only tier; deeper tiers are documented next steps and are never implied by shallow results."

- [ ] **Step 2: Link it**

- `design/research.md` D13: add `Tier boundary: docs/detection-tiers.md`.
- `src/serve/templates/detect_options.html`: link text `Detection tiers (API-only vs clone vs BigQuery)`.

- [ ] **Step 3: Full suite + coverage**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest --cov=src --cov-report=term-missing -q`
Expected: all tests pass; coverage `>= 93%`. If below, add tests for the uncovered detect branches (record the new baseline in `docs/development-log.md`).

- [ ] **Step 4: Lint + format**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m ruff check src tests; .\.venv\Scripts\python.exe -m black --check src tests`
Expected: clean. (If the repo vendors ruff/black in the venv; otherwise use `python -m` on the installed tools.)

- [ ] **Step 5: Optional live smoke (token-guarded)**

Add one optional test (skip when `GITHUB_TOKEN` unset) that runs `detect_files` against a known repo (e.g. the gitcrawl repo itself is private-safe; use a stable public fixture like `pallets/flask` expecting no crash and a list of hits). Mark it `@pytest.mark.live` per existing live-test conventions in `tests/integration/test_golden_org.py`.

- [ ] **Step 6: Commit**

```bash
git add docs/detection-tiers.md design/research.md src/serve/templates/detect_options.html docs/development-log.md tests/integration/test_detect_live.py
git commit -m "docs: detection tier boundaries and final verification"
```

---

## Self-Review Checklist (run after implementation)

- [ ] Spec FS1 (options page + select-all): Task 10/11.
- [ ] Spec FS2 (summary/matrix/evidence): Task 11.
- [ ] Spec FS3 (API-only, 7 channels): Tasks 4–7.
- [ ] Spec FS4 (pack versioned + frozen): Tasks 1–2.
- [ ] Spec FS5 (frozen candidates, top-N deterministic): Task 9.
- [ ] Spec FS6 (no discovery calls): Task 9 test.
- [ ] Spec FS7 (no silent partials): Channel statuses + tests.
- [ ] Spec FS8 (docs tiers): Task 12.
- [ ] Every `runs`/`run_items` write path backwards compatible with find runs: Tasks 9, 11.
- [ ] OpenAPI hash re-pinned: Task 11.
