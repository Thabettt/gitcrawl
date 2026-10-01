from __future__ import annotations

import difflib
import os
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine

from discover.search_shards import RequestFailed
from lib.gh_client import API_VERSION, PartialResultsError, ThrottledError
from serve.executor import Runner, RunPayload, RunPayloadItem, create_run, execute_run
from serve.filter_spec import (
    FILTER_SPEC_VERSION,
    FilterSpecError,
    parse_filter_spec,
    spec_hash,
    spec_to_dict,
)
from serve.pages import register_pages
from serve.runner import apply_sort, build_deps, make_runner
from serve.runs import export_bundle, latest_run_for_hash
from serve.virtual_params import VIRTUAL_FILTERS
from store.models import Owner, Repo, RunItem, Runs

CACHE_TTL_SECONDS = 120.0
DEFAULT_PER_PAGE = 20
PER_PAGE_RANGE = (1, 100)
_GET_PARAMS = ("q", "sort", "order", "per_page", "page", *VIRTUAL_FILTERS)
_GET_PARAM_SET = frozenset(_GET_PARAMS)
_BACKTICK_RE = re.compile(r"`([^`]+)`")


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _unknown_param_hint(name: str) -> str:
    valid = ", ".join(_GET_PARAMS)
    close = difflib.get_close_matches(name, list(_GET_PARAMS), n=1)
    if close:
        return f"did you mean `{close[0]}`? valid params: {valid}"
    return f"valid params: {valid}"


def _coerce_virtual(name: str, raw: str) -> object:
    rule = VIRTUAL_FILTERS[name]
    if rule.kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return raw
    if rule.kind == "bool":
        lowered = raw.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        return raw
    return raw


def _page_size(raw: str | None) -> int:
    if raw is None:
        return DEFAULT_PER_PAGE
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("per_page", "per_page must be an integer between 1 and 100") from None
    low, high = PER_PAGE_RANGE
    if not low <= value <= high:
        raise ValueError("per_page", "per_page must be an integer between 1 and 100")
    return value


def _page_number(raw: str | None) -> int:
    if raw is None:
        return 1
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("page", "page must be an integer >= 1") from None
    if value < 1:
        raise ValueError("page", "page must be an integer >= 1")
    return value


def _spec_error_param(exc: FilterSpecError) -> str:
    for error in exc.errors:
        match = _BACKTICK_RE.search(error)
        if match is None:
            continue
        token = match.group(1)
        if token in _GET_PARAM_SET:
            return token
        if ":" in token:
            return "q"
    return "q"


def _spec_error_hint(exc: FilterSpecError, param: str) -> str:
    for hint in exc.hints:
        if param in hint:
            return hint
    if exc.hints:
        return exc.hints[0]
    return exc.errors[0] if exc.errors else "invalid filter"


def _detail_map(engine: Engine, repo_ids: list[int]) -> dict[int, dict]:
    unique = list(dict.fromkeys(repo_ids))
    if not unique:
        return {}
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                Repo.id,
                Repo.description,
                Repo.language,
                Repo.license_spdx,
                Repo.topics,
                Repo.stargazers,
                Repo.forks_count,
                Repo.open_issues,
                Repo.pushed_at,
                Owner.login.label("owner_login"),
                Owner.type.label("owner_type"),
                Owner.location_raw.label("raw_location"),
            )
            .join(Owner, Owner.id == Repo.owner_id)
            .where(Repo.id.in_(unique))
        ).mappings()
        return {row["id"]: dict(row) for row in rows}


def _render_item(item: RunPayloadItem, detail: dict | None) -> dict:
    detail = detail or {}
    login = detail.get("owner_login")
    if login is None and "/" in item.full_name:
        login = item.full_name.split("/", 1)[0]
    language = item.language if item.language is not None else detail.get("language")
    license_spdx = (
        item.license_spdx if item.license_spdx is not None else detail.get("license_spdx")
    )
    stargazers = item.stargazers if item.stargazers is not None else detail.get("stargazers")
    pushed_at = item.pushed_at if item.pushed_at is not None else _iso(detail.get("pushed_at"))
    virtuals = dict(item.virtuals or {})
    return {
        "id": item.repo_id,
        "full_name": item.full_name,
        "description": detail.get("description"),
        "language": language,
        "license_spdx": license_spdx,
        "topics": list(detail.get("topics") or []),
        "stargazers": stargazers,
        "forks_count": detail.get("forks_count"),
        "open_issues": detail.get("open_issues"),
        "pushed_at": pushed_at,
        "owner": {
            "login": login,
            "type": detail.get("owner_type"),
            "country_iso": item.country_iso,
            "geo_confidence": item.geo_confidence,
            "raw_location": detail.get("raw_location"),
        },
        "has_dockerfile": virtuals.get("has_dockerfile"),
        "virtuals": virtuals,
    }


def _render_items(engine: Engine, items: list[RunPayloadItem]) -> list[dict]:
    details = _detail_map(engine, [item.repo_id for item in items])
    return [_render_item(item, details.get(item.repo_id)) for item in items]


def _run_items(engine: Engine, run_id: int) -> list[RunPayloadItem]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                RunItem.repo_id,
                RunItem.full_name,
                RunItem.stargazers,
                RunItem.pushed_at,
                RunItem.archived,
                RunItem.language,
                RunItem.license_spdx,
                RunItem.country_iso,
                RunItem.geo_confidence,
                RunItem.virtuals,
            ).where(RunItem.run_id == run_id)
        ).mappings()
        return [
            RunPayloadItem(
                repo_id=row["repo_id"],
                full_name=row["full_name"],
                stargazers=row["stargazers"],
                pushed_at=_iso(row["pushed_at"]),
                archived=row["archived"],
                language=row["language"],
                license_spdx=row["license_spdx"],
                country_iso=row["country_iso"],
                geo_confidence=row["geo_confidence"],
                virtuals=dict(row["virtuals"] or {}),
            )
            for row in rows
        ]


def _replay_body(engine: Engine, run_row: dict, run_id: int) -> dict:
    items = _render_items(engine, _run_items(engine, run_id))
    filter_spec = run_row["filter_spec"]
    sort = filter_spec.get("sort") if isinstance(filter_spec, dict) else None
    order = filter_spec.get("order") if isinstance(filter_spec, dict) else None
    items = apply_sort(items, sort, order)
    return {
        "filter_hash": run_row["filter_hash"],
        "ran_at": _iso(run_row["finished_at"] or run_row["created_at"]),
        "api_version": run_row["api_version"],
        "total_count": len(items),
        "incomplete": run_row["status"] == "partial",
        "items": items,
    }


def _cached_payload(
    cache: dict[str, tuple[float, RunPayload]],
    lock: threading.Lock,
    key: str,
    clock: Callable[[], float],
) -> RunPayload | None:
    with lock:
        entry = cache.get(key)
    if entry is None:
        return None
    stored_at, payload = entry
    if clock() - stored_at >= CACHE_TTL_SECONDS:
        return None
    return payload


def create_app(
    *,
    engine: Engine | None = None,
    runner_factory: Callable[[Engine], Runner] | None = None,
    runs_root: str = "runs",
    clock: Callable[[], float] = time.time,
    redis_ping: Callable[[], object] | None = None,
    token_present: Callable[[], bool] | None = None,
) -> FastAPI:
    state: dict[str, object] = {"engine": engine, "runner": None}
    cache: dict[str, tuple[float, RunPayload]] = {}
    lock = threading.Lock()
    application = FastAPI(title="gitcrawl", version="0.0.1")

    def engine_for() -> Engine:
        bound = state["engine"]
        if bound is None:
            url = os.environ.get("DATABASE_URL")
            if not url:
                raise RuntimeError("DATABASE_URL is not configured")
            bound = create_engine(url)
            state["engine"] = bound
        return bound

    def runner_for() -> Runner:
        bound = state["runner"]
        if bound is None:
            if runner_factory is None:
                bound = make_runner(build_deps(engine_for()))
            else:
                bound = runner_factory(engine_for())
            state["runner"] = bound
        return bound

    @application.get("/vsearch/repos")
    def list_repos(request: Request):
        supplied = request.query_params.multi_items()
        unknown = [key for key, _ in supplied if key not in _GET_PARAM_SET]
        if unknown:
            key = unknown[0]
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_param",
                    "param": key,
                    "hint": _unknown_param_hint(key),
                },
            )
        values: dict[str, str] = {}
        for key, value in supplied:
            values.setdefault(key, value)
        try:
            per_page = _page_size(values.get("per_page"))
            page_number = _page_number(values.get("page"))
        except ValueError as exc:
            name, hint = exc.args
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_param", "param": name, "hint": hint},
            )
        document: dict[str, object] = {"gitcrawl_filter": FILTER_SPEC_VERSION}
        if "q" in values:
            document["q"] = values["q"]
        if "sort" in values:
            document["sort"] = values["sort"]
        if "order" in values:
            document["order"] = values["order"]
        virtual = {
            name: _coerce_virtual(name, values[name]) for name in VIRTUAL_FILTERS if name in values
        }
        if virtual:
            document["virtual"] = virtual
        try:
            spec = parse_filter_spec(document)
        except FilterSpecError as exc:
            param = _spec_error_param(exc)
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_param",
                    "param": param,
                    "hint": _spec_error_hint(exc, param),
                },
            )
        key = spec_hash(spec)
        payload = _cached_payload(cache, lock, key, clock)
        if payload is None:
            try:
                payload = runner_for()(0, spec_to_dict(spec))
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
            with lock:
                cache[key] = (clock(), payload)
        rendered = apply_sort(_render_items(engine_for(), payload.items), spec.sort, spec.order)
        start = (page_number - 1) * per_page
        return {
            "total_count": len(rendered),
            "incomplete": payload.incomplete,
            "items": rendered[start : start + per_page],
        }

    @application.post("/vsearch/run")
    async def create_run_route(request: Request):
        try:
            document = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_param",
                    "errors": ["body must be valid JSON"],
                    "hints": ["pass a filter-spec v1 JSON object"],
                },
            )
        if not isinstance(document, dict):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_param",
                    "errors": ["filter-spec must be a JSON object"],
                    "hints": ["pass a filter-spec v1 JSON object"],
                },
            )
        try:
            spec = parse_filter_spec(document)
        except FilterSpecError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_param",
                    "errors": list(exc.errors),
                    "hints": list(exc.hints),
                },
            )
        bound_engine = engine_for()
        try:
            runner = runner_for()
        except Exception:
            return JSONResponse(status_code=500, content={"error": "internal_error"})
        run_id = create_run(bound_engine, spec_to_dict(spec), api_version=API_VERSION)
        captured: list[BaseException] = []

        def capturing(rid: int, filter_spec: dict) -> RunPayload:
            try:
                return runner(rid, filter_spec)
            except Exception as exc:
                captured.append(exc)
                raise

        execute_run(bound_engine, run_id, runner=capturing, runs_root=runs_root)
        if captured:
            error = captured[0]
            if isinstance(error, ThrottledError):
                return JSONResponse(
                    status_code=502,
                    content={
                        "error": "upstream_unavailable",
                        "retry_after_ms": int(error.retry_after * 1000),
                    },
                )
            if isinstance(error, (RequestFailed, PartialResultsError)):
                return JSONResponse(
                    status_code=502,
                    content={"error": "upstream_unavailable", "retry_after_ms": 0},
                )
            return JSONResponse(status_code=500, content={"error": "run_failed"})
        with bound_engine.connect() as connection:
            run_row = connection.execute(select(Runs).where(Runs.id == run_id)).mappings().one()
        if run_row["status"] == "failed":
            return JSONResponse(status_code=500, content={"error": "run_failed"})
        return _replay_body(bound_engine, run_row, run_id)

    @application.get("/vsearch/runs/{filter_hash}")
    def replay(filter_hash: str):
        bound_engine = engine_for()
        run_row = latest_run_for_hash(bound_engine, filter_hash)
        if run_row is None:
            return JSONResponse(
                status_code=404,
                content={"error": "run_not_found", "filter_hash": filter_hash},
            )
        return _replay_body(bound_engine, run_row, run_row["id"])

    @application.get("/vsearch/runs/{filter_hash}/export")
    def export(filter_hash: str, format: str = "json"):
        if format not in ("json", "csv"):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_param",
                    "param": "format",
                    "hint": "format must be one of: json, csv",
                },
            )
        bound_engine = engine_for()
        run_row = latest_run_for_hash(bound_engine, filter_hash)
        if run_row is None:
            return JSONResponse(
                status_code=404,
                content={"error": "run_not_found", "filter_hash": filter_hash},
            )
        try:
            content, media_type = export_bundle(
                bound_engine, run_row["id"], format=format, runs_root=runs_root
            )
        except KeyError:
            return JSONResponse(
                status_code=404,
                content={"error": "run_not_found", "filter_hash": filter_hash},
            )
        filename = f"gitcrawl-{filter_hash}-{run_row['id']}.{format}"
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    register_pages(
        application,
        engine_factory=engine_for,
        runs_root=runs_root,
        redis_ping=redis_ping,
        token_present=token_present,
    )

    return application


app = create_app()
