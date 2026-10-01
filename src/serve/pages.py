from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.engine import Engine

from enrich.cloner import CloneMode, parse_mode
from lib.gh_client import API_VERSION, load_tokens
from serve.diff import diff_runs
from serve.executor import RunExecutor, Runner, create_run
from serve.filter_spec import FilterSpecError, parse_filter_spec, spec_to_dict
from serve.forms import build_spec_from_form, form_state, spec_to_form_values
from serve.library import LibraryError, create_filter, get_filter, list_filters
from serve.runs import (
    CloneRegistry,
    clone_estimate_for_run,
    progress_payload,
    read_clone_progress,
    start_clone,
)
from store.models import RunItem, Runs

CSRF_COOKIE = "gc_csrf"
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf"
FORM_CONTENT_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")
HEALTH_TIMEOUT_SECONDS = 0.25
HEALTH_CACHE_SECONDS = 5.0
RECENT_RUNS_LIMIT = 20
TABLE_PAGE_SIZE = 50
RUN_SORT_COLUMNS = {
    "stars": RunItem.stargazers,
    "pushed": RunItem.pushed_at,
    "name": RunItem.full_name,
}
NON_TERMINAL_STATUSES = ("queued", "running")
HISTORY_STATUSES = ("queued", "running", "done", "partial", "failed")
HISTORY_PAGE_SIZE = 50
R44_VIRTUALS = ("min_commits", "min_loc")
SAVE_ERROR_STATUS = {"invalid_name": 400, "invalid_spec": 400, "duplicate_name": 409}
_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
_redis_client = None


def _bounded(check: Callable[[], object], timeout: float = HEALTH_TIMEOUT_SECONDS) -> bool:
    result: list[bool] = []

    def target() -> None:
        try:
            result.append(bool(check()))
        except Exception:
            result.append(False)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout)
    return result[0] if result else False


def _default_redis_ping() -> bool:
    global _redis_client
    url = os.environ.get("REDIS_URL")
    if not url:
        return False
    if _redis_client is None:
        import redis

        _redis_client = redis.Redis.from_url(
            url,
            socket_connect_timeout=HEALTH_TIMEOUT_SECONDS,
            socket_timeout=HEALTH_TIMEOUT_SECONDS,
        )
    try:
        return bool(_redis_client.ping())
    except Exception:
        return False


def _relative_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    seconds = max((datetime.now(UTC) - value).total_seconds(), 0.0)
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    return f"{days // 30}mo ago"


def _duration(started: datetime | None, finished: datetime | None) -> str:
    if started is None or finished is None:
        return "—"
    seconds = max((finished - started).total_seconds(), 0.0)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes}m {remainder}s"


def _iso(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _page_param(raw: str | int | None) -> int:
    if raw is None:
        return 1
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("page", "page must be an integer >= 1") from None
    if value < 1:
        raise ValueError("page", "page must be an integer >= 1")
    return value


def _run_summary(row) -> dict:
    return {
        "id": row["id"],
        "filter_hash": row["filter_hash"],
        "hash_short": row["filter_hash"][:8],
        "status": row["status"],
        "total_count": row["total_count"],
        "inserted": row["inserted"],
        "duration": _duration(row["started_at"], row["finished_at"]),
        "ran_at": _relative_time(row["finished_at"] or row["started_at"] or row["created_at"]),
    }


def _recent_runs(engine: Engine, limit: int = RECENT_RUNS_LIMIT) -> list[dict]:
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(Runs).order_by(Runs.created_at.desc(), Runs.id.desc()).limit(limit)
            )
            .mappings()
            .all()
        )
    return [_run_summary(row) for row in rows]


def _history_summary(row) -> dict:
    return {
        "id": row["id"],
        "filter_hash": row["filter_hash"],
        "hash_short": row["filter_hash"][:8],
        "status": row["status"],
        "fetched": row["fetched"],
        "inserted": row["inserted"],
        "total_count": row["total_count"],
        "duration": _duration(row["started_at"], row["finished_at"]),
        "ran_at": _relative_time(row["finished_at"] or row["started_at"] or row["created_at"]),
        "incomplete": row["status"] == "partial" or bool(row["incomplete_shards"]),
        "terminal": row["status"] not in NON_TERMINAL_STATUSES,
        "error": row["error"],
    }


def _history_page(engine: Engine, *, status: str, hash_prefix: str, page: int) -> dict:
    statement = select(Runs)
    if status:
        statement = statement.where(Runs.status == status)
    if hash_prefix:
        statement = statement.where(Runs.filter_hash.startswith(hash_prefix))
    with engine.connect() as connection:
        total = int(connection.scalar(select(func.count()).select_from(statement.subquery())) or 0)
        pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
        current = min(max(page, 1), pages)
        rows = (
            connection.execute(
                statement.order_by(Runs.created_at.desc(), Runs.id.desc())
                .offset((current - 1) * HISTORY_PAGE_SIZE)
                .limit(HISTORY_PAGE_SIZE)
            )
            .mappings()
            .all()
        )
    return {
        "runs": [_history_summary(row) for row in rows],
        "page": current,
        "pages": pages,
        "total": total,
    }


def _same_hash_runs(engine: Engine, row, *, limit: int = 50) -> list[dict]:
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(Runs)
                .where(Runs.filter_hash == row["filter_hash"], Runs.id != row["id"])
                .order_by(Runs.created_at.desc(), Runs.id.desc())
                .limit(limit)
            )
            .mappings()
            .all()
        )
    return [
        {
            "id": candidate["id"],
            "status": candidate["status"],
            "hash_short": candidate["filter_hash"][:8],
            "ran_at": _relative_time(
                candidate["finished_at"] or candidate["started_at"] or candidate["created_at"]
            ),
        }
        for candidate in rows
    ]


def _previous_same_hash_run(engine: Engine, row) -> int | None:
    with engine.connect() as connection:
        return connection.scalar(
            select(Runs.id)
            .where(
                Runs.filter_hash == row["filter_hash"],
                Runs.id != row["id"],
                or_(
                    Runs.created_at < row["created_at"],
                    and_(Runs.created_at == row["created_at"], Runs.id < row["id"]),
                ),
            )
            .order_by(Runs.created_at.desc(), Runs.id.desc())
            .limit(1)
        )


def _last_run_times(engine: Engine, hashes: list[str]) -> dict[str, str]:
    unique = [value for value in dict.fromkeys(hashes) if value]
    if not unique:
        return {}
    with engine.connect() as connection:
        rows = connection.execute(
            select(Runs.filter_hash, func.max(Runs.created_at))
            .where(Runs.filter_hash.in_(unique))
            .group_by(Runs.filter_hash)
        ).all()
    return {row[0]: _relative_time(row[1]) for row in rows}


def render_library(
    request: Request,
    engine: Engine,
    *,
    error: str = "",
    hints: tuple[str, ...] = (),
    status_code: int = 200,
) -> HTMLResponse:
    library_error = error
    try:
        views = list_filters(engine)
        last_runs = _last_run_times(engine, [view.filter_hash for view in views])
    except Exception:
        views = []
        last_runs = {}
        library_error = library_error or "Saved filters unavailable; the database did not answer."
        hints = ()
    items = [
        {
            "id": view.id,
            "name": view.name,
            "filter_hash": view.filter_hash,
            "hash_short": view.filter_hash[:8],
            "created_at": _relative_time(view.created_at),
            "last_run": last_runs.get(view.filter_hash),
        }
        for view in views
    ]
    return _templates.TemplateResponse(
        request,
        "library.html",
        {
            "filters": items,
            "error": library_error,
            "hints": list(hints),
            "csrf_token": request.state.csrf_token,
        },
        status_code=status_code,
    )


def _run_detail_row(engine: Engine, run_id: int):
    with engine.connect() as connection:
        return connection.execute(select(Runs).where(Runs.id == run_id)).mappings().one_or_none()


def _item_count(engine: Engine, run_id: int) -> int:
    with engine.connect() as connection:
        count = connection.scalar(
            select(func.count()).select_from(RunItem).where(RunItem.run_id == run_id)
        )
    return int(count or 0)


def _virtual_filters(filter_spec: object) -> dict:
    if isinstance(filter_spec, dict):
        virtual = filter_spec.get("virtual")
        if isinstance(virtual, dict):
            return virtual
    return {}


def _run_flags(row, virtual: dict) -> list[dict]:
    flags: list[dict] = []
    if row["error"]:
        flags.append({"kind": "error", "label": f"failed: {row['error']}"})
    if row["status"] == "partial" or row["incomplete_shards"]:
        shards = row["incomplete_shards"] or 1
        flags.append(
            {
                "kind": "incomplete",
                "label": f"incomplete: {shards} discovery shard(s) incomplete; results are partial",
            }
        )
    total = row["total_count"]
    if isinstance(total, int) and row["fetched"] < total:
        flags.append(
            {"kind": "truncation", "label": f"truncated: fetched {row['fetched']} of ~{total}"}
        )
    for name in R44_VIRTUALS:
        if name in virtual:
            flags.append(
                {
                    "kind": "r44",
                    "label": f"`{name}` is recorded but unenforceable in this run; "
                    "results are incomplete",
                }
            )
    return flags


def _run_detail_summary(row, item_count: int) -> dict:
    return {
        "id": row["id"],
        "filter_hash": row["filter_hash"],
        "hash_short": row["filter_hash"][:8],
        "status": row["status"],
        "total_count": row["total_count"],
        "fetched": row["fetched"],
        "inserted": row["inserted"],
        "updated": row["updated"],
        "unchanged": row["unchanged"],
        "skipped": row["skipped"],
        "incomplete_shards": row["incomplete_shards"],
        "error": row["error"],
        "created_at": _iso(row["created_at"]),
        "started_at": _iso(row["started_at"]),
        "finished_at": _iso(row["finished_at"]),
        "duration": _duration(row["started_at"], row["finished_at"]),
        "item_count": item_count,
        "polling": row["status"] in NON_TERMINAL_STATUSES,
        "flags": _run_flags(row, _virtual_filters(row["filter_spec"])),
    }


def _table_view(
    engine: Engine,
    run_id: int,
    sort: str,
    direction: str,
    page: int,
    *,
    run_row=None,
    total: int | None = None,
) -> dict | None:
    if run_row is None and _run_detail_row(engine, run_id) is None:
        return None
    if sort not in RUN_SORT_COLUMNS:
        sort = "stars"
    if direction not in ("asc", "desc"):
        direction = "desc"
    column = RUN_SORT_COLUMNS[sort]
    order = column.asc() if direction == "asc" else column.desc()
    with engine.connect() as connection:
        if total is None:
            total = int(
                connection.scalar(
                    select(func.count()).select_from(RunItem).where(RunItem.run_id == run_id)
                )
                or 0
            )
        total = int(total)
        pages = max(1, (total + TABLE_PAGE_SIZE - 1) // TABLE_PAGE_SIZE)
        current = min(max(page, 1), pages)
        rows = (
            connection.execute(
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
                )
                .where(RunItem.run_id == run_id)
                .order_by(order.nullslast(), RunItem.repo_id)
                .offset((current - 1) * TABLE_PAGE_SIZE)
                .limit(TABLE_PAGE_SIZE)
            )
            .mappings()
            .all()
        )
    items = []
    for row in rows:
        virtuals = dict(row["virtuals"] or {})
        items.append(
            {
                **dict(row),
                "pushed_at": _iso(row["pushed_at"]),
                "incomplete": bool(virtuals.get("incomplete"))
                or any(name in virtuals for name in R44_VIRTUALS),
            }
        )
    return {
        "rows": items,
        "sort": sort,
        "dir": direction,
        "page": current,
        "pages": pages,
        "total": total,
    }


async def validate_csrf(request: Request) -> bool:
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie:
        return False
    supplied = request.headers.get(CSRF_HEADER)
    if not supplied:
        content_type = request.headers.get("content-type", "")
        if any(media_type in content_type for media_type in FORM_CONTENT_TYPES):
            try:
                form = await request.form()
            except Exception:
                return False
            value = form.get(CSRF_FIELD)
            supplied = value if isinstance(value, str) else None
    if not supplied:
        return False
    try:
        return hmac.compare_digest(str(cookie).encode("utf-8"), str(supplied).encode("utf-8"))
    except UnicodeError:
        return False


def _flat_form(form) -> dict[str, str]:
    grouped: dict[str, list[str]] = {}
    for key, value in form.multi_items():
        if isinstance(value, str):
            grouped.setdefault(key, []).append(value)
    return {key: ",".join(values) for key, values in grouped.items()}


def _invalid_param(param: str, hint: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": "invalid_param", "param": param, "hint": hint},
    )


def _run_not_found(run_id: int) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "run_not_found", "run_id": run_id})


def register_pages(
    app: FastAPI,
    *,
    engine_factory: Callable[[], Engine],
    runs_root: str,
    clone_root: str = "clones",
    redis_ping: Callable[[], object] | None = None,
    token_present: Callable[[], bool] | None = None,
    runner_factory: Callable[[], Runner] | None = None,
    executor_factory: Callable[[], RunExecutor],
) -> None:
    templates = _templates
    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    def database_ok() -> bool:
        try:
            engine = engine_factory()
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    def redis_ok() -> bool:
        return _bounded(redis_ping or _default_redis_ping)

    def token_ok() -> bool:
        if token_present is None:
            return bool(load_tokens())
        return bool(token_present())

    health_cache: dict[str, float | dict[str, bool] | None] = {"at": 0.0, "value": None}
    health_lock = threading.Lock()

    def health_snapshot() -> dict[str, bool]:
        now = time.monotonic()
        with health_lock:
            cached = health_cache["value"]
            cached_at = health_cache["at"]
            if (
                isinstance(cached, dict)
                and isinstance(cached_at, float)
                and now - cached_at < HEALTH_CACHE_SECONDS
            ):
                return dict(cached)
        value = {
            "database": _bounded(database_ok),
            "redis": redis_ok(),
            "github_token_present": token_ok(),
        }
        with health_lock:
            health_cache["at"] = time.monotonic()
            health_cache["value"] = dict(value)
        return value

    @app.middleware("http")
    async def csrf_cookie(request: Request, call_next):
        issued = request.cookies.get(CSRF_COOKIE) is None
        token = secrets.token_urlsafe(32) if issued else request.cookies[CSRF_COOKIE]
        request.state.csrf_token = token
        response = await call_next(request)
        if issued:
            response.set_cookie(
                CSRF_COOKIE,
                token,
                httponly=True,
                samesite="lax",
            )
        return response

    @app.get("/health")
    def health():
        return health_snapshot()

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        try:
            runs = _recent_runs(engine_factory())
            runs_error = False
        except Exception:
            runs = []
            runs_error = True
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "runs": runs,
                "runs_error": runs_error,
                "health": health_snapshot(),
                "runs_root": runs_root,
                "csrf_token": request.state.csrf_token,
            },
        )

    def render_filters(
        request: Request,
        values: dict[str, str],
        errors: tuple[str, ...] = (),
        hints: tuple[str, ...] = (),
        status_code: int = 200,
    ) -> HTMLResponse:
        state = replace(form_state(values), errors=tuple(errors), hints=tuple(hints))
        return templates.TemplateResponse(
            request,
            "filters.html",
            {"state": state, "csrf_token": request.state.csrf_token},
            status_code=status_code,
        )

    def runner_or_none() -> Runner | None:
        if runner_factory is None:
            return None
        return runner_factory()

    @app.get("/find", response_class=HTMLResponse)
    @app.get("/vsearch/", response_class=HTMLResponse)
    def filter_form(request: Request):
        values = {
            key: value
            for key, value in request.query_params.multi_items()
            if key not in ("q", "filter")
        }
        if "q" in request.query_params:
            values["keywords"] = request.query_params["q"]
        filter_id = request.query_params.get("filter")
        if filter_id is not None:
            try:
                view = get_filter(engine_factory(), int(filter_id))
            except (TypeError, ValueError, LibraryError):
                return render_filters(
                    request,
                    values,
                    ("saved filter not found",),
                    ("pick a saved filter from the library",),
                    404,
                )
            values.update(spec_to_form_values(view.filter_spec))
        return render_filters(request, values)

    @app.post("/find")
    async def filter_submit(request: Request):
        if not await validate_csrf(request):
            return Response(status_code=403, content="invalid csrf token", media_type="text/plain")
        form = await request.form()
        values = _flat_form(form)
        action = values.get("action", "find")
        if action == "upload":
            upload = form.get("spec_file")
            reader = getattr(upload, "read", None)
            if reader is None:
                return render_filters(
                    request,
                    values,
                    ("no filter-spec file uploaded",),
                    ("choose a filter-spec v1 JSON file for `spec_file`",),
                    400,
                )
            try:
                document = json.loads((await reader()).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return render_filters(
                    request,
                    values,
                    ("`spec_file` is not valid JSON",),
                    ("upload a filter-spec v1 JSON object",),
                    400,
                )
            if not isinstance(document, dict):
                return render_filters(
                    request,
                    values,
                    ("filter-spec must be a JSON object",),
                    ("upload a filter-spec v1 JSON object",),
                    400,
                )
        else:
            document = build_spec_from_form(values)
        try:
            spec = parse_filter_spec(document)
        except FilterSpecError as exc:
            return render_filters(request, values, exc.errors, exc.hints, 400)
        if action == "download":
            payload = json.dumps(spec_to_dict(spec), indent=2).encode("utf-8")
            return Response(
                content=payload,
                media_type="application/json",
                headers={"Content-Disposition": 'attachment; filename="gitcrawl-filter.json"'},
            )
        if action == "save":
            try:
                create_filter(engine_factory(), values.get("name", ""), spec_to_dict(spec))
            except LibraryError as exc:
                return render_filters(
                    request,
                    values,
                    (exc.message,),
                    exc.hints,
                    SAVE_ERROR_STATUS.get(exc.code, 400),
                )
            return RedirectResponse("/filters", status_code=303)
        try:
            runner = runner_or_none()
        except Exception:
            return Response(status_code=500, content="runner unavailable", media_type="text/plain")
        engine = engine_factory()
        run_id = create_run(engine, spec_to_dict(spec), api_version=API_VERSION)
        executor_factory().submit(run_id, runner=runner)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request, status: str = "", hash: str = "", page: str = "1"):
        try:
            page_number = _page_param(page)
        except ValueError:
            return _invalid_param("page", "page must be an integer >= 1")
        hash_prefix = hash.strip()
        filtered_status = status if status in HISTORY_STATUSES else ""
        filter_error = ""
        if status and not filtered_status:
            filter_error = f"unknown status `{status}`"
        try:
            view = _history_page(
                engine_factory(), status=filtered_status, hash_prefix=hash_prefix, page=page_number
            )
            runs_error = False
        except Exception:
            view = {"runs": [], "page": 1, "pages": 1, "total": 0}
            runs_error = True
        filter_query = urlencode({"status": filtered_status, "hash": hash_prefix})
        query_suffix = f"&{filter_query}" if (filtered_status or hash_prefix) else ""
        return templates.TemplateResponse(
            request,
            "runs.html",
            {
                "csrf_token": request.state.csrf_token,
                "status": filtered_status,
                "hash": hash_prefix,
                "statuses": HISTORY_STATUSES,
                "filter_error": filter_error,
                "runs_error": runs_error,
                "query_suffix": query_suffix,
                **view,
            },
        )

    @app.get("/runs/{run_id}/diff", response_class=HTMLResponse)
    def run_diff_page(request: Request, run_id: int, against: str | None = None):
        engine = engine_factory()
        viewed = _run_detail_row(engine, run_id)
        if viewed is None:
            return templates.TemplateResponse(request, "diff.html", {"run": None}, status_code=404)
        candidates = _same_hash_runs(engine, viewed)
        baseline_id: int | None
        if against is None or not against.strip():
            baseline_id = _previous_same_hash_run(engine, viewed)
        else:
            try:
                baseline_id = int(against)
            except (TypeError, ValueError):
                baseline_id = None
            if baseline_id is None or _run_detail_row(engine, baseline_id) is None:
                return templates.TemplateResponse(
                    request, "diff.html", {"run": None}, status_code=404
                )
        context = {
            "run_id": run_id,
            "hash_short": viewed["filter_hash"][:8],
            "against": baseline_id,
            "candidates": candidates,
            "result": None,
            "empty": False,
            "no_baseline": baseline_id is None,
        }
        if baseline_id is not None:
            result = diff_runs(engine, baseline_id, run_id)
            context["result"] = result
            context["empty"] = not (result.added or result.removed or result.changed)
        return templates.TemplateResponse(request, "diff.html", context)

    progress_registry = CloneRegistry()

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: int):
        engine = engine_factory()
        row = _run_detail_row(engine, run_id)
        if row is None:
            return templates.TemplateResponse(
                request, "run_detail.html", {"run": None}, status_code=404
            )
        item_count = _item_count(engine, run_id)
        table = _table_view(
            engine, run_id, "stars", "desc", 1, run_row=row, total=item_count
        )
        estimate = clone_estimate_for_run(
            engine, run_id, limit=None, mode=CloneMode.SHALLOW, dest_root=clone_root
        )
        progress = read_clone_progress(
            engine, run_id, registry=progress_registry, runs_root=runs_root
        )
        return templates.TemplateResponse(
            request,
            "run_detail.html",
            {
                "run": _run_detail_summary(row, item_count),
                "run_id": run_id,
                "csrf_token": request.state.csrf_token,
                "estimate": estimate,
                "progress": progress,
                **(table or {}),
            },
        )

    @app.post("/runs/{run_id}/replay")
    async def replay_run(request: Request, run_id: int):
        if not await validate_csrf(request):
            return Response(status_code=403, content="invalid csrf token", media_type="text/plain")
        engine = engine_factory()
        with engine.connect() as connection:
            row = connection.execute(select(Runs).where(Runs.id == run_id)).mappings().one_or_none()
        if row is None:
            return Response(status_code=404, content="run not found", media_type="text/plain")
        try:
            runner = runner_or_none()
        except Exception:
            return Response(status_code=500, content="runner unavailable", media_type="text/plain")
        new_id = create_run(engine, dict(row["filter_spec"]), api_version=row["api_version"])
        executor_factory().submit(new_id, runner=runner)
        return RedirectResponse(f"/runs/{new_id}", status_code=303)

    @app.get("/partials/runs/{run_id}/status", response_class=HTMLResponse)
    def run_status_partial(request: Request, run_id: int):
        engine = engine_factory()
        row = _run_detail_row(engine, run_id)
        if row is None:
            return _run_not_found(run_id)
        return templates.TemplateResponse(
            request,
            "partials/status.html",
            {"run": _run_detail_summary(row, _item_count(engine, run_id))},
        )

    @app.get("/partials/runs/{run_id}/table", response_class=HTMLResponse)
    def run_table_partial(
        request: Request,
        run_id: int,
        sort: str = "stars",
        dir: str = "desc",
        page: str = "1",
    ):
        try:
            page_number = _page_param(page)
        except ValueError:
            return _invalid_param("page", "page must be an integer >= 1")
        table = _table_view(engine_factory(), run_id, sort, dir, page_number)
        if table is None:
            return _run_not_found(run_id)
        return templates.TemplateResponse(
            request, "partials/table.html", {"run_id": run_id, **table}
        )

    @app.get("/runs/{run_id}/clone-estimate")
    def clone_estimate_route(request: Request, run_id: int):
        raw_limit = request.query_params.get("limit")
        limit: int | None = None
        if raw_limit is not None:
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                return _invalid_param("limit", "limit must be an integer >= 0")
            if limit < 0:
                return _invalid_param("limit", "limit must be an integer >= 0")
        raw_mode = request.query_params.get("mode", "shallow")
        try:
            mode = parse_mode(raw_mode)
        except ValueError:
            return _invalid_param("mode", "mode must be one of: shallow, file_only, windowed")
        try:
            estimate = clone_estimate_for_run(
                engine_factory(), run_id, limit=limit, mode=mode, dest_root=clone_root
            )
        except KeyError:
            return _run_not_found(run_id)
        except ValueError:
            return _invalid_param("limit", "limit must be an integer >= 0")
        if request.headers.get("HX-Request") == "true":
            return templates.TemplateResponse(
                request,
                "partials/clone_modal.html",
                {"estimate": estimate, "estimate_only": True},
            )
        return {
            "repos": estimate.repos,
            "estimated_mb": estimate.estimated_mb,
            "warnings": list(estimate.warnings),
        }

    @app.post("/runs/{run_id}/clone")
    async def clone_start_route(request: Request, run_id: int):
        try:
            document = await request.json()
        except Exception:
            return _invalid_param("body", "body must be a JSON object with limit and mode")
        if not isinstance(document, dict):
            return _invalid_param("body", "body must be a JSON object with limit and mode")
        limit = document.get("limit", 0)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            return _invalid_param("limit", "limit must be an integer >= 0")
        raw_mode = document.get("mode", "shallow")
        if not isinstance(raw_mode, str):
            return _invalid_param("mode", "mode must be one of: shallow, file_only, windowed")
        try:
            mode = parse_mode(raw_mode)
        except ValueError:
            return _invalid_param("mode", "mode must be one of: shallow, file_only, windowed")
        try:
            progress = start_clone(
                engine_factory(),
                run_id,
                limit=limit,
                mode=mode,
                registry=progress_registry,
                runs_root=runs_root,
                dest_root=clone_root,
            )
        except KeyError:
            return _run_not_found(run_id)
        payload = progress_payload(progress)
        if limit > 0:
            payload["status"] = "running"
        return payload

    @app.get("/partials/runs/{run_id}/clone-progress")
    def clone_progress_route(request: Request, run_id: int):
        try:
            progress = read_clone_progress(
                engine_factory(), run_id, registry=progress_registry, runs_root=runs_root
            )
        except KeyError:
            return _run_not_found(run_id)
        if request.headers.get("HX-Request") == "true":
            return templates.TemplateResponse(
                request,
                "partials/clone_progress.html",
                {"run_id": run_id, "progress": progress},
            )
        return progress_payload(progress)
