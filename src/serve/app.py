from __future__ import annotations

import asyncio
import difflib
import os
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.engine import Engine
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from lib.batching import chunked
from lib.deadlines import DeadlineExceededError, request_deadline_seconds
from lib.gh_client import API_VERSION, PartialResultsError, RequestFailed, ThrottledError
from serve import errors, pages
from serve.corpora import register_corpora
from serve.diff import diff_runs
from serve.executor import (
    RunExecutor,
    Runner,
    RunPayload,
    RunPayloadItem,
    create_run,
    reset_run_artifacts,
)
from serve.filter_spec import (
    FILTER_SPEC_VERSION,
    FilterSpecError,
    parse_filter_spec,
    spec_hash,
    spec_to_dict,
    spec_to_query,
)
from serve.library import (
    LibraryError,
    create_filter,
    delete_filter,
    get_filter,
    list_filters,
    rename_filter,
)
from serve.middleware import (
    JSON_BODY_LIMIT_BYTES,
    UPLOAD_BODY_LIMIT_BYTES,
    BodyLimitMiddleware,
    OriginCsrfMiddleware,
)
from serve.pages import (
    FORM_CONTENT_TYPES,
    register_pages,
    render_library,
    validate_csrf,
)
from serve.payload_cache import CACHE_TTL_SECONDS, RunPayloadCache
from serve.runner import apply_sort, build_deps
from serve.runs import export_run, latest_run_for_hash
from serve.settings import register_settings
from serve.system import register_system
from serve.virtual_params import VIRTUAL_FILTERS
from store.models import Owner, Repo, RunItem, Runs

DEFAULT_PER_PAGE = 20
PER_PAGE_RANGE = (1, 100)
READY_STATUSES = frozenset({"done", "partial"})
_GET_PARAMS = ("q", "sort", "order", "per_page", "page", *VIRTUAL_FILTERS)
_GET_PARAM_SET = frozenset(_GET_PARAMS)
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_DETAIL_BATCH = 5000


def _allowed_hosts() -> list[str]:
    hosts = ["localhost", "127.0.0.1", "[::1]"]
    extra = os.environ.get("GITCRAWL_ALLOWED_HOSTS", "")
    hosts.extend(host.strip() for host in extra.split(",") if host.strip())
    return hosts


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


_LIBRARY_STATUS = {
    "invalid_name": 400,
    "invalid_spec": 400,
    "duplicate_name": 409,
    "not_found": 404,
}


def _library_error_response(exc: LibraryError) -> JSONResponse:
    return JSONResponse(
        status_code=_LIBRARY_STATUS.get(exc.code, 400),
        content={"error": exc.code, "message": exc.message, "hints": list(exc.hints)},
    )


def _timeout_response(exc: DeadlineExceededError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"error": "timeout", "retry_after": max(1, int(exc.retry_after))},
    )


def _library_view_payload(view) -> dict:
    return {"id": view.id, "name": view.name, "filter_hash": view.filter_hash}


def _detail_map(engine: Engine, repo_ids: list[int]) -> dict[int, dict]:
    unique = list(dict.fromkeys(repo_ids))
    if not unique:
        return {}
    result: dict[int, dict] = {}
    with engine.connect() as connection:
        for batch in chunked(unique, _DETAIL_BATCH):
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
                .where(Repo.id.in_(batch))
            ).mappings()
            result.update({row["id"]: dict(row) for row in rows})
    return result


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


class _LazyLoaders:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._gates: dict[str, threading.Lock] = {}
        self._values: dict[str, object] = {}

    def seed(self, key: str, value: object) -> None:
        with self._lock:
            self._values[key] = value

    def get(self, key: str, factory: Callable[[], object]) -> object:
        with self._lock:
            if key in self._values:
                return self._values[key]
            gate = self._gates.setdefault(key, threading.Lock())
        with gate:
            with self._lock:
                if key in self._values:
                    return self._values[key]
            value = factory()
            with self._lock:
                self._values[key] = value
            return value


def create_app(
    *,
    engine: Engine | None = None,
    runner_factory: Callable[[Engine], Runner] | None = None,
    runs_root: str = "runs",
    clone_root: str = "clones",
    clock: Callable[[], float] = time.time,
    redis_ping: Callable[[], object] | None = None,
    token_present: Callable[[], bool] | None = None,
    metrics_redis: Callable[[], object] | None = None,
    find_count_factory: Callable[[], Callable[[str], int]] | None = None,
    clone_disk_free: Callable[[str], float] | None = None,
) -> FastAPI:
    loaders = _LazyLoaders()
    payload_cache = RunPayloadCache(ttl_seconds=CACHE_TTL_SECONDS, clock=clock)
    application = FastAPI(title="gitcrawl", version="0.0.1")
    application.add_middleware(OriginCsrfMiddleware)
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=_allowed_hosts(),
        www_redirect=False,
    )
    application.add_middleware(
        BodyLimitMiddleware,
        default_limit=JSON_BODY_LIMIT_BYTES,
        path_limits={"/find": UPLOAD_BODY_LIMIT_BYTES},
    )

    def build_engine() -> Engine:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL is not configured")
        return create_engine(url)

    if engine is not None:
        loaders.seed("engine", engine)

    def engine_for() -> Engine:
        return cast(Engine, loaders.get("engine", build_engine))

    def runner_for() -> Runner:
        def build() -> Runner:
            if runner_factory is not None:
                return runner_factory(engine_for())

            def runner(run_id: int, filter_spec: dict) -> RunPayload:
                from serve.filter_spec import parse_filter_spec
                from serve.runner import run_filter, runner_config_from
                from store.settings import load_run_settings

                settings = load_run_settings(engine_for())
                deps = build_deps(engine_for(), max_concurrent=settings.limiter_max_concurrent)
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

    def executor_for() -> RunExecutor:
        def build() -> RunExecutor:
            return RunExecutor(engine_for(), runs_root=runs_root)

        return cast(RunExecutor, loaders.get("executor", build))

    def run_with_deadline(job: Callable[[], object], timeout: float) -> object:
        try:
            return executor_for().submit_call(job).result(timeout=timeout)
        except DeadlineExceededError:
            raise
        except TimeoutError:
            raise DeadlineExceededError(timeout) from None

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
        if not spec_to_query(spec):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_param",
                    "param": "q",
                    "hint": "provide at least one keyword or filter",
                },
            )
        key = spec_hash(spec)
        timeout = request_deadline_seconds()
        try:
            runner = runner_for()
            payload = payload_cache.run_once(
                key,
                lambda: run_with_deadline(lambda: runner(0, spec_to_dict(spec)), timeout),
                timeout=timeout,
            )
        except DeadlineExceededError as exc:
            return _timeout_response(exc)
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
            runner = await run_in_threadpool(runner_for)
        except Exception:
            return JSONResponse(status_code=500, content={"error": "internal_error"})
        run_id = await run_in_threadpool(
            create_run, bound_engine, spec_to_dict(spec), api_version=API_VERSION
        )
        captured: list[BaseException] = []

        def capturing(rid: int, filter_spec: dict) -> RunPayload:
            try:
                return runner(rid, filter_spec)
            except Exception as exc:
                captured.append(exc)
                raise

        timeout = request_deadline_seconds()
        try:
            await asyncio.wait_for(
                asyncio.wrap_future(executor_for().submit(run_id, runner=capturing)),
                timeout,
            )
        except DeadlineExceededError as exc:
            return _timeout_response(exc)
        except TimeoutError:
            return _timeout_response(DeadlineExceededError(timeout))
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

        def finish() -> object:
            with bound_engine.connect() as connection:
                run_row = connection.execute(select(Runs).where(Runs.id == run_id)).mappings().one()
            if run_row["status"] == "failed":
                return JSONResponse(status_code=500, content={"error": "run_failed"})
            return _replay_body(bound_engine, run_row, run_id)

        return await run_in_threadpool(finish)

    @application.get("/vsearch/runs/{filter_hash}")
    def replay(filter_hash: str):
        bound_engine = engine_for()
        ready_statuses = tuple(sorted(READY_STATUSES))
        run_row = latest_run_for_hash(bound_engine, filter_hash, statuses=ready_statuses)
        if run_row is None:
            if latest_run_for_hash(bound_engine, filter_hash) is None:
                return JSONResponse(
                    status_code=404,
                    content={"error": "run_not_found", "filter_hash": filter_hash},
                )
            return JSONResponse(
                status_code=409,
                content={"error": "run_not_ready", "filter_hash": filter_hash},
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
        ready_statuses = tuple(sorted(READY_STATUSES))
        run_row = latest_run_for_hash(bound_engine, filter_hash, statuses=ready_statuses)
        if run_row is None:
            if latest_run_for_hash(bound_engine, filter_hash) is None:
                return JSONResponse(
                    status_code=404,
                    content={"error": "run_not_found", "filter_hash": filter_hash},
                )
            return JSONResponse(
                status_code=404,
                content={"error": "run_not_ready", "filter_hash": filter_hash},
            )
        try:
            return export_run(bound_engine, run_row["id"], format=format, runs_root=runs_root)
        except KeyError:
            return JSONResponse(
                status_code=404,
                content={"error": "run_not_found", "filter_hash": filter_hash},
            )

    def is_form_request(request: Request) -> bool:
        content_type = request.headers.get("content-type", "")
        return any(media_type in content_type for media_type in FORM_CONTENT_TYPES)

    @application.get("/filters")
    def list_filters_route(request: Request):
        if "text/html" in request.headers.get("accept", ""):
            return render_library(request, engine_for())
        views = list_filters(engine_for())
        return {
            "filters": [
                {
                    "id": view.id,
                    "name": view.name,
                    "filter_hash": view.filter_hash,
                    "created_at": _iso(view.created_at),
                    "updated_at": _iso(view.updated_at),
                }
                for view in views
            ]
        }

    @application.post("/filters")
    async def create_filter_route(request: Request):
        try:
            document = await request.json()
        except Exception:
            document = None
        if not isinstance(document, dict):
            return _library_error_response(
                LibraryError(
                    "invalid_spec",
                    "body must be a JSON object",
                    ("pass a JSON object with `name` and `spec`",),
                )
            )
        try:
            view = await run_in_threadpool(
                create_filter, engine_for(), document.get("name"), document.get("spec")
            )
        except LibraryError as exc:
            return _library_error_response(exc)
        return JSONResponse(status_code=201, content=_library_view_payload(view))

    @application.post("/filters/{filter_id}/rename")
    async def rename_filter_route(request: Request, filter_id: int):
        if is_form_request(request):
            if not await validate_csrf(request):
                return errors.csrf_error_page(request)
            form = await request.form()
            name = form.get("name")
            try:
                await run_in_threadpool(
                    rename_filter, engine_for(), filter_id, name if isinstance(name, str) else None
                )
            except LibraryError as exc:
                return await run_in_threadpool(
                    render_library,
                    request,
                    engine_for(),
                    error=exc.message,
                    hints=exc.hints,
                    status_code=_LIBRARY_STATUS.get(exc.code, 400),
                )
            return RedirectResponse("/filters", status_code=303)
        try:
            document = await request.json()
        except Exception:
            document = None
        if not isinstance(document, dict):
            return _library_error_response(
                LibraryError(
                    "invalid_name",
                    "body must be a JSON object",
                    ("pass a JSON object with `name`",),
                )
            )
        try:
            view = await run_in_threadpool(
                rename_filter, engine_for(), filter_id, document.get("name")
            )
        except LibraryError as exc:
            return _library_error_response(exc)
        return _library_view_payload(view)

    @application.post("/filters/{filter_id}/delete")
    async def delete_filter_route(request: Request, filter_id: int):
        if is_form_request(request):
            if not await validate_csrf(request):
                return errors.csrf_error_page(request)
            try:
                await run_in_threadpool(delete_filter, engine_for(), filter_id)
            except LibraryError as exc:
                return await run_in_threadpool(
                    render_library,
                    request,
                    engine_for(),
                    error=exc.message,
                    hints=exc.hints,
                    status_code=_LIBRARY_STATUS.get(exc.code, 400),
                )
            return RedirectResponse("/filters", status_code=303)
        try:
            await run_in_threadpool(delete_filter, engine_for(), filter_id)
        except LibraryError as exc:
            return _library_error_response(exc)
        return Response(status_code=204)

    @application.post("/filters/{filter_id}/run")
    async def run_saved_filter(request: Request, filter_id: int):
        if not await validate_csrf(request):
            return errors.csrf_error_page(request)
        engine = engine_for()
        try:
            view = await run_in_threadpool(get_filter, engine, filter_id)
        except LibraryError as exc:
            return await run_in_threadpool(
                render_library,
                request,
                engine,
                error=exc.message,
                hints=exc.hints,
                status_code=_LIBRARY_STATUS.get(exc.code, 400),
            )
        except FilterSpecError as exc:
            return await run_in_threadpool(
                render_library,
                request,
                engine,
                error=exc.errors[0] if exc.errors else "the saved filter is no longer valid",
                hints=exc.hints,
                status_code=400,
            )
        spec = parse_filter_spec(view.filter_spec)
        try:
            runner = await run_in_threadpool(runner_for)
        except Exception:
            return JSONResponse(status_code=500, content={"error": "internal_error"})
        run_id = await run_in_threadpool(
            create_run, engine, spec_to_dict(spec), api_version=API_VERSION
        )
        executor_for().submit(run_id, runner=runner)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @application.get("/api/runs/{run_id}/diff")
    def diff_route(run_id: int, against: str | None = None):
        if against is None:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_param",
                    "param": "against",
                    "hint": "against must be an existing run id",
                },
            )
        try:
            against_id = int(against)
        except (TypeError, ValueError):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_param",
                    "param": "against",
                    "hint": "against must be an existing run id",
                },
            )
        try:
            result = diff_runs(engine_for(), against_id, run_id)
        except KeyError as exc:
            return JSONResponse(
                status_code=404,
                content={"error": "run_not_found", "run_id": exc.args[0]},
            )
        return {
            "run_a": result.run_a,
            "run_b": result.run_b,
            "added": list(result.added),
            "removed": list(result.removed),
            "changed": list(result.changed),
            "summary": result.summary,
        }

    @application.post("/runs/{run_id}/resume")
    async def resume_run(request: Request, run_id: int):
        if not await validate_csrf(request):
            return errors.csrf_error_page(request)
        engine = engine_for()

        def load_run() -> dict | None:
            with engine.connect() as connection:
                row = (
                    connection.execute(select(Runs).where(Runs.id == run_id))
                    .mappings()
                    .one_or_none()
                )
            return dict(row) if row is not None else None

        row = await run_in_threadpool(load_run)
        if row is None:
            return HTMLResponse("run not found", status_code=404)
        if row["status"] not in ("failed", "partial", "cancelled"):
            return HTMLResponse(
                "only failed, partial, or cancelled runs can be resumed", status_code=400
            )

        if row.get("kind") == "detect":
            from detect.orchestrator import run_detection
            from detect.packs import load_frozen
            from serve.detect_spec import parse_detect_spec

            from serve.runner import build_deps

            spec = parse_detect_spec(row["filter_spec"])
            pack = load_frozen(engine, spec.pack_version)

            def detect_runner(_run_id: int, _spec_doc: dict):
                return run_detection(build_deps(engine), run_id, spec, pack)

            submit_runner = detect_runner
        else:
            submit_runner = runner_for()

        def reset_and_queue() -> None:
            reset_run_artifacts(engine, run_id)
            with engine.begin() as connection:
                connection.execute(
                    update(Runs)
                    .where(Runs.id == run_id)
                    .values(
                        status="queued",
                        error=None,
                        started_at=None,
                        finished_at=None,
                    )
                )

        await run_in_threadpool(reset_and_queue)
        executor_for().submit(run_id, submit_runner)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @application.post("/runs/{run_id}/cancel")
    async def cancel_run(request: Request, run_id: int):
        if not await validate_csrf(request):
            return errors.csrf_error_page(request)
        engine = engine_for()
        with engine.connect() as connection:
            row = connection.execute(select(Runs).where(Runs.id == run_id)).mappings().one_or_none()
        if row is None:
            return HTMLResponse("run not found", status_code=404)
        if row["status"] not in ("queued", "running"):
            return HTMLResponse("only queued or running runs can be stopped", status_code=400)
        executor_for().request_cancel(run_id)
        if row["status"] == "queued":

            def mark_stopped() -> None:
                with engine.begin() as connection:
                    connection.execute(
                        update(Runs)
                        .where(Runs.id == run_id, Runs.status == "queued")
                        .values(
                            status="cancelled",
                            error=None,
                            started_at=None,
                            finished_at=func.now(),
                            progress_phase=None,
                            progress_done=None,
                            progress_total=None,
                        )
                    )

            await run_in_threadpool(mark_stopped)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @application.post("/runs/{run_id}/save-filter")
    async def save_filter_run(request: Request, run_id: int):
        if not await validate_csrf(request):
            return errors.csrf_error_page(request)
        form = await request.form()
        name = str(form.get("name", "")).strip()
        engine = engine_for()

        def load_run() -> dict | None:
            with engine.connect() as connection:
                row = (
                    connection.execute(select(Runs).where(Runs.id == run_id))
                    .mappings()
                    .one_or_none()
                )
            return dict(row) if row is not None else None

        row = await run_in_threadpool(load_run)
        if row is None:
            return HTMLResponse("run not found", status_code=404)

        def save() -> str | None:
            try:
                create_filter(engine, name, row["filter_spec"])
            except LibraryError as exc:
                return (
                    exc.code
                    if exc.code in ("invalid_name", "invalid_spec", "duplicate_name")
                    else "invalid_spec"
                )
            return None

        error = await run_in_threadpool(save)
        if error is None:
            return RedirectResponse(f"/runs/{run_id}?saved=1", status_code=303)
        return RedirectResponse(f"/runs/{run_id}?save_error={error}", status_code=303)

    @application.get("/runs/{run_id}/export")
    def export_run_route(run_id: int, format: str = "json"):
        if format not in ("json", "csv"):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_param",
                    "param": "format",
                    "hint": "format must be one of: json, csv",
                },
            )
        try:
            return export_run(engine_for(), run_id, format=format, runs_root=runs_root)
        except KeyError:
            return JSONResponse(
                status_code=404,
                content={"error": "run_not_found", "run_id": run_id},
            )

    @application.get("/runs/{run_id}/quality")
    def run_quality_json(run_id: int):
        from serve.quality import run_quality

        try:
            report = run_quality(
                engine_for(),
                run_id,
                runs_root=runs_root,
                now=lambda: datetime.fromtimestamp(clock(), UTC),
            )
        except KeyError:
            return JSONResponse(
                status_code=404,
                content={"error": "run_not_found", "run_id": run_id},
            )
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
        from serve.quality import run_quality

        try:
            report = run_quality(
                engine_for(),
                run_id,
                runs_root=runs_root,
                now=lambda: datetime.fromtimestamp(clock(), UTC),
            )
        except KeyError:
            return HTMLResponse("run not found", status_code=404)
        return quality_panel(request, report)

    def metrics_redis_client() -> object | None:
        if metrics_redis is not None:
            try:
                return metrics_redis()
            except Exception:
                return None
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

        return metrics_payload(engine_for(), redis_client=metrics_redis_client())

    def _metrics_response(request: Request, template: str):
        from serve.metrics import LABELS, metrics_payload

        payload = metrics_payload(engine_for(), redis_client=metrics_redis_client())
        return pages._templates.TemplateResponse(
            request,
            template,
            {
                "payload": payload,
                "labels": LABELS,
                "csrf_token": request.state.csrf_token,
            },
        )

    @application.get("/metrics", response_class=HTMLResponse)
    def metrics_page(request: Request):
        return _metrics_response(request, "metrics.html")

    @application.get("/partials/metrics", response_class=HTMLResponse)
    def metrics_partial(request: Request):
        return _metrics_response(request, "partials/metrics_cards.html")

    errors.register_error_pages(application)

    health_snapshot = pages.build_health_snapshot(
        engine_for, redis_ping=redis_ping, token_present=token_present
    )

    register_settings(
        application,
        engine_factory=engine_for,
        token_present=token_present,
    )

    register_pages(
        application,
        engine_factory=engine_for,
        runs_root=runs_root,
        clone_root=clone_root,
        redis_ping=redis_ping,
        token_present=token_present,
        health_snapshot=health_snapshot,
        runner_factory=runner_for,
        find_count_factory=find_count_factory,
        clone_disk_free=clone_disk_free,
        executor_factory=executor_for,
    )

    register_system(
        application,
        engine_factory=engine_for,
        health_snapshot=health_snapshot,
        metrics_redis=metrics_redis_client,
    )

    register_corpora(application, engine_factory=engine_for)

    return application


app = create_app()
