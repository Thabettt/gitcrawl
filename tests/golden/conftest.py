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
    "/runs/{run_id}/quality": {"run_id": RUN_A},
    "/runs/{run_id}/clone-estimate": {"run_id": RUN_A},
    "/partials/runs/{run_id}/status": {"run_id": RUN_A},
    "/partials/runs/{run_id}/quality": {"run_id": RUN_A},
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
    cleaned = path.strip("/").replace("{", "").replace("}", "").replace("/", "_").replace(".", "_")
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


def canonical_content_type(value: str) -> str:
    # Windows mimetypes maps .js to application/javascript; Linux CPython uses
    # text/javascript (RFC 9239). Canonicalize so golden headers are OS-deterministic.
    if value == "application/javascript":
        return "text/javascript"
    return value


def observe(client: TestClient, case: str, path: str, headers: dict[str, str]) -> Observation:
    response = client.get(path, headers=headers)
    content_type = canonical_content_type(response.headers.get("content-type", ""))
    captured = {
        name: content_type if name == "content-type" else response.headers[name]
        for name in CAPTURED_HEADERS
        if name in response.headers
    }
    body = response.content.decode("utf-8")
    if content_type.startswith("text/"):
        body = body.replace("\r\n", "\n")
    if content_type.startswith("text/html"):
        body = normalize_html(body)
    body = DISK_WARNING.sub("<disk-warning>", body)
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
