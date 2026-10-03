from __future__ import annotations

import hmac
import json
import logging
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
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.engine import Engine
from starlette.concurrency import run_in_threadpool

from enrich.cloner import CloneMode, parse_mode
from lib.gh_client import API_VERSION, load_tokens
from serve.diff import diff_runs
from serve.executor import RunExecutor, Runner, create_run
from serve.filter_spec import (
    FilterSpecError,
    describe_spec,
    parse_filter_spec,
    spec_to_dict,
    spec_to_query,
)
from serve.forms import build_spec_from_form, form_state, spec_to_form_values
from serve.library import LibraryError, create_filter, get_filter, list_filters
from serve.metrics import severity
from serve.quality import sentence_for
from serve.runs import (
    CloneRegistry,
    cancel_clone,
    clone_estimate_for_run,
    progress_payload,
    read_clone_progress,
    start_clone,
)
from store.models import Repo, RunItem, Runs

CSRF_COOKIE = "gc_csrf"
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf"
FORM_CONTENT_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")
HEALTH_TIMEOUT_SECONDS = 0.25
HEALTH_CACHE_SECONDS = 5.0
RECENT_RUNS_LIMIT = 20
TABLE_PAGE_SIZE = 50
PREVIEW_PAGE_SIZE = 20
RUN_SORT_COLUMNS = {
    "stars": RunItem.stargazers,
    "pushed": RunItem.pushed_at,
    "name": RunItem.full_name,
}
RESULTS_PAGE_SIZES = (25, 50, 100, 200)
RESULTS_DIRECTIONS = ("asc", "desc")
NON_TERMINAL_STATUSES = ("queued", "running")
HISTORY_STATUSES = ("queued", "running", "done", "partial", "failed")
HISTORY_PAGE_SIZE = 50
R44_VIRTUALS = ("min_loc", "max_loc")
SAVE_ERROR_STATUS = {"invalid_name": 400, "invalid_spec": 400, "duplicate_name": 409}
STATUS_LABELS = {
    "queued": "Waiting",
    "running": "Running",
    "done": "Finished",
    "partial": "Finished — incomplete",
    "failed": "Failed",
}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
_templates = Jinja2Templates(
    env=Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(),
        auto_reload=False,
    )
)
_templates.env.globals["severity"] = severity
_templates.env.globals["status_label"] = status_label
_redis_client = None
logger = logging.getLogger("gitcrawl.serve")


class CachedStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers.setdefault("Cache-Control", "public, max-age=3600")
        return response


class _Probe:
    def __init__(self, *, timeout: float = HEALTH_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout
        self._lock = threading.Lock()
        self._event: threading.Event | None = None
        self._value = False

    def run(self, check: Callable[[], object]) -> bool:
        with self._lock:
            event = self._event
            if event is None:
                event = threading.Event()
                self._event = event
                threading.Thread(target=self._execute, args=(check, event), daemon=True).start()
        if not event.wait(self._timeout):
            return False
        with self._lock:
            if self._event is event:
                self._event = None
        return self._value

    def _execute(self, check: Callable[[], object], event: threading.Event) -> None:
        try:
            value = bool(check())
        except Exception:
            value = False
        with self._lock:
            self._value = value
        event.set()


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
    except Exception as exc:
        logger.warning("redis health probe failed (%s: %s)", type(exc).__name__, exc)
        return False


def build_health_snapshot(
    engine_factory: Callable[[], Engine],
    redis_ping: Callable[[], object] | None = None,
    token_present: Callable[[], bool] | None = None,
) -> Callable[[], dict[str, bool]]:
    db_probe = _Probe()
    redis_probe = _Probe()

    def database_ok() -> bool:
        try:
            engine = engine_factory()
            with engine.connect() as connection:
                connection.execute(
                    text(f"SET LOCAL statement_timeout = {int(HEALTH_TIMEOUT_SECONDS * 1000)}")
                )
                connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    def redis_ok() -> bool:
        return redis_probe.run(redis_ping or _default_redis_ping)

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
            "database": db_probe.run(database_ok),
            "redis": redis_ok(),
            "github_token_present": token_ok(),
        }
        if os.environ.get("REDIS_URL") and not value["redis"]:
            value["redis_degraded"] = True
        with health_lock:
            health_cache["at"] = time.monotonic()
            health_cache["value"] = dict(value)
        return value

    return health_snapshot


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


def _previous_same_hash_runs(engine: Engine, row, *, limit: int = 3) -> list[dict]:
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(Runs)
                .where(
                    Runs.filter_hash == row["filter_hash"],
                    Runs.id != row["id"],
                    or_(
                        Runs.created_at < row["created_at"],
                        and_(Runs.created_at == row["created_at"], Runs.id < row["id"]),
                    ),
                )
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


def _last_runs(engine: Engine, hashes: list[str]) -> dict[str, dict]:
    unique = [value for value in dict.fromkeys(hashes) if value]
    if not unique:
        return {}
    ranked = (
        select(
            Runs.id,
            Runs.filter_hash,
            Runs.created_at,
            Runs.total_count,
            Runs.fetched,
            func.row_number()
            .over(
                partition_by=Runs.filter_hash,
                order_by=(Runs.created_at.desc(), Runs.id.desc()),
            )
            .label("run_rank"),
        )
        .where(Runs.filter_hash.in_(unique))
        .subquery()
    )
    with engine.connect() as connection:
        rows = connection.execute(select(ranked).where(ranked.c.run_rank == 1)).mappings()
        return {
            row["filter_hash"]: {
                "id": row["id"],
                "ran_at": _relative_time(row["created_at"]),
                "count": _int_or_zero(
                    row["total_count"] if row["total_count"] is not None else row["fetched"]
                ),
            }
            for row in rows
        }


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
        last_runs = _last_runs(engine, [view.filter_hash for view in views])
    except Exception:
        views = []
        last_runs = {}
        library_error = library_error or "Saved filters unavailable; the database did not answer."
        hints = ()
    items = [
        {
            "id": view.id,
            "name": view.name,
            "sentence": _spec_sentence(view.filter_spec),
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


def quality_panel(request: Request, report) -> HTMLResponse:
    checks = [{"status": check.status, "sentence": sentence_for(check)} for check in report.checks]
    return _templates.TemplateResponse(
        request,
        "partials/quality.html",
        {"checks": checks, "badge": {"ok": "good", "warn": "warn", "fail": "bad"}[report.status]},
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
        steps = row["incomplete_shards"] or 1
        flags.append(
            {
                "kind": "incomplete",
                "label": f"incomplete: {steps} step(s) of finding repos did not finish; "
                "some results may be missing",
            }
        )
    total = row["total_count"]
    if isinstance(total, int) and row["fetched"] < total:
        flags.append(
            {"kind": "truncation", "label": f"truncated: found {row['fetched']} of ~{total}"}
        )
    for name in R44_VIRTUALS:
        if name in virtual:
            flags.append(
                {
                    "kind": "r44",
                    "label": f"`{name}` is recorded but unenforceable in this search; "
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
        "q": _raw_query(row["filter_spec"]),
        "flags": _run_flags(row, _virtual_filters(row["filter_spec"])),
    }


def _raw_query(filter_spec: object) -> str:
    if isinstance(filter_spec, dict):
        value = filter_spec.get("q")
        if isinstance(value, str):
            return value
    return ""


def _int_or_zero(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _dockerfile_count(engine: Engine, run_id: int) -> int:
    with engine.connect() as connection:
        count = connection.scalar(
            text(
                "SELECT count(*) FROM run_items "
                "WHERE run_id = :run_id AND virtuals->>'has_dockerfile' = 'true'"
            ),
            {"run_id": run_id},
        )
    return int(count or 0)


def _run_counts(engine: Engine, row) -> dict[str, dict]:
    counts: dict[str, dict] = {
        "found": {
            "label": "Found",
            "explain": "Repos GitHub said matched your search.",
            "value": _int_or_zero(row["fetched"]),
        },
        "saved": {
            "label": "Saved",
            "explain": "We fetched each repo’s current details.",
            "value": _int_or_zero(row["inserted"]) + _int_or_zero(row["updated"]),
        },
        "passed": {
            "label": "Passed filters",
            "explain": "Also met your file and country rules.",
            "value": _int_or_zero(row["unchanged"]),
        },
    }
    if _virtual_filters(row["filter_spec"]).get("has_dockerfile"):
        counts["with_file"] = {
            "label": "With a Dockerfile",
            "explain": "Contain the file you asked about.",
            "value": _dockerfile_count(engine, row["id"]),
        }
    counts["unavailable"] = {
        "label": "Unavailable",
        "explain": "Deleted or private by the time we looked.",
        "value": _int_or_zero(row["skipped"]),
    }
    return counts


def results_context(
    engine: Engine,
    run_id: int,
    *,
    page: int,
    per_page: int,
    sort: str,
    dir: str,
    run_row=None,
    total: int | None = None,
) -> dict | None:
    if run_row is None:
        run_row = _run_detail_row(engine, run_id)
        if run_row is None:
            return None
    if sort not in RUN_SORT_COLUMNS:
        sort = "stars"
    if dir not in RESULTS_DIRECTIONS:
        dir = "desc"
    per_page = max(1, int(per_page))
    column = RUN_SORT_COLUMNS[sort]
    order = column.asc() if dir == "asc" else column.desc()
    with engine.connect() as connection:
        if total is None:
            total = int(
                connection.scalar(
                    select(func.count()).select_from(RunItem).where(RunItem.run_id == run_id)
                )
                or 0
            )
        total = int(total)
        pages = max(1, (total + per_page - 1) // per_page)
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
                    Repo.forks_count,
                )
                .outerjoin(Repo, Repo.id == RunItem.repo_id)
                .where(RunItem.run_id == run_id)
                .order_by(order.nullslast(), RunItem.repo_id)
                .offset((current - 1) * per_page)
                .limit(per_page)
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
        "dir": dir,
        "page": current,
        "pages": pages,
        "total": total,
        "per_page": per_page,
        "saved_at": _iso(run_row["finished_at"] or run_row["created_at"]),
    }


def _results_page_number(raw: str) -> tuple[int, str]:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0
    if value < 1:
        return 1, "Page must be a whole number of 1 or more."
    return value, ""


def _spec_sentence(filter_spec: object) -> str:
    try:
        return describe_spec(parse_filter_spec(filter_spec))
    except (FilterSpecError, TypeError):
        return "Everything"


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


def _flat_items(items) -> dict[str, str]:
    grouped: dict[str, list[str]] = {}
    for key, value in items:
        if isinstance(value, str):
            grouped.setdefault(key, []).append(value)
    return {key: ",".join(values) for key, values in grouped.items()}


def _flat_form(form) -> dict[str, str]:
    return _flat_items(form.multi_items())


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
    health_snapshot: Callable[[], dict[str, bool]] | None = None,
    runner_factory: Callable[[], Runner] | None = None,
    find_count_factory: Callable[[], Callable[[str], int]] | None = None,
    executor_factory: Callable[[], RunExecutor],
) -> None:
    templates = _templates
    if _STATIC_DIR.is_dir():
        app.mount("/static", CachedStaticFiles(directory=str(_STATIC_DIR)), name="static")

    snapshot = health_snapshot or build_health_snapshot(
        engine_factory, redis_ping=redis_ping, token_present=token_present
    )

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
        return snapshot()

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
                "health": snapshot(),
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
        sentence: str = "",
    ) -> HTMLResponse:
        state = replace(form_state(values), errors=tuple(errors), hints=tuple(hints))
        return templates.TemplateResponse(
            request,
            "filters.html",
            {"state": state, "csrf_token": request.state.csrf_token, "spec_sentence": sentence},
            status_code=status_code,
        )

    def runner_or_none() -> Runner | None:
        if runner_factory is None:
            return None
        return runner_factory()

    def default_find_count_factory() -> Callable[[str], int]:
        def count(query: str) -> int:
            from discover.pipeline import count_total
            from serve.runner import build_deps

            deps = build_deps(engine_factory())
            try:
                return count_total(deps, query)
            finally:
                deps.client.close()

        return count

    count_factory = find_count_factory or default_find_count_factory

    @app.get("/partials/find/matches", response_class=HTMLResponse)
    def find_matches(request: Request):
        values = _flat_items(request.query_params.multi_items())
        if "q" in request.query_params:
            values["keywords"] = request.query_params["q"]
        context: dict[str, object] = {
            "errors": (),
            "hints": (),
            "csrf_token": request.state.csrf_token,
        }
        try:
            spec = parse_filter_spec(build_spec_from_form(values))
        except FilterSpecError as exc:
            context["errors"] = exc.errors
            context["hints"] = exc.hints
        else:
            try:
                count = count_factory()(spec_to_query(spec))
            except Exception:
                context["errors"] = ("Could not check matches right now.",)
            else:
                context["count"] = count
                context["sentence"] = describe_spec(spec)
        if request.headers.get("hx-request") == "true":
            return templates.TemplateResponse(request, "partials/find_matches.html", context)
        return templates.TemplateResponse(request, "find_matches_full.html", context)

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
        sentence = ""
        if values:
            try:
                sentence = describe_spec(parse_filter_spec(build_spec_from_form(values)))
            except FilterSpecError:
                sentence = ""
        return render_filters(request, values, sentence=sentence)

    @app.post("/find")
    async def filter_submit(request: Request):
        if not await validate_csrf(request):
            from serve.errors import csrf_error_page

            return csrf_error_page(request)
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
                await run_in_threadpool(
                    create_filter, engine_factory(), values.get("name", ""), spec_to_dict(spec)
                )
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
            runner = await run_in_threadpool(runner_or_none)
        except Exception:
            return Response(status_code=500, content="runner unavailable", media_type="text/plain")
        engine = engine_factory()
        run_id = await run_in_threadpool(
            create_run, engine, spec_to_dict(spec), api_version=API_VERSION
        )
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
        table = results_context(
            engine,
            run_id,
            page=1,
            per_page=PREVIEW_PAGE_SIZE,
            sort="stars",
            dir="desc",
            run_row=row,
            total=item_count,
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
                "sentence": _spec_sentence(row["filter_spec"]),
                "counts": _run_counts(engine, row),
                "compare_candidates": _previous_same_hash_runs(engine, row),
                "preview_page_size": PREVIEW_PAGE_SIZE,
                "clone_root": clone_root,
                "csrf_token": request.state.csrf_token,
                "estimate": estimate,
                "progress": progress,
                **(table or {}),
            },
        )

    @app.post("/runs/{run_id}/replay")
    async def replay_run(request: Request, run_id: int):
        if not await validate_csrf(request):
            from serve.errors import csrf_error_page

            return csrf_error_page(request)
        engine = engine_factory()
        row = await run_in_threadpool(_run_detail_row, engine, run_id)
        if row is None:
            return Response(status_code=404, content="run not found", media_type="text/plain")
        try:
            runner = await run_in_threadpool(runner_or_none)
        except Exception:
            return Response(status_code=500, content="runner unavailable", media_type="text/plain")
        new_id = await run_in_threadpool(
            create_run, engine, dict(row["filter_spec"]), api_version=row["api_version"]
        )
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
            {
                "run": _run_detail_summary(row, _item_count(engine, run_id)),
                "counts": _run_counts(engine, row),
            },
        )

    @app.get("/runs/{run_id}/results", response_class=HTMLResponse)
    def run_results_page(
        request: Request,
        run_id: int,
        page: str = "1",
        per_page: str = "50",
        sort: str = "stars",
        dir: str = "desc",
    ):
        engine = engine_factory()
        row = _run_detail_row(engine, run_id)
        if row is None:
            return templates.TemplateResponse(
                request, "results.html", {"run": None}, status_code=404
            )
        hints: list[str] = []
        page_number, hint = _results_page_number(page)
        if hint:
            hints.append(hint)
        try:
            page_size = int(per_page)
        except (TypeError, ValueError):
            page_size = 0
        if page_size not in RESULTS_PAGE_SIZES:
            hints.append("Per page must be one of 25, 50, 100, 200.")
            page_size = TABLE_PAGE_SIZE
        if sort not in RUN_SORT_COLUMNS:
            hints.append("Sort must be one of: stars, pushed, name.")
            sort = "stars"
        if dir not in RESULTS_DIRECTIONS:
            hints.append("Direction must be one of: asc, desc.")
            dir = "desc"
        table = results_context(
            engine,
            run_id,
            page=page_number,
            per_page=page_size,
            sort=sort,
            dir=dir,
            run_row=row,
        )
        return templates.TemplateResponse(
            request,
            "results.html",
            {
                "run": row,
                "run_id": run_id,
                "sentence": _spec_sentence(row["filter_spec"]),
                "hints": hints,
                "per_page_options": RESULTS_PAGE_SIZES,
                **(table or {}),
            },
            status_code=400 if hints else 200,
        )

    @app.get("/partials/runs/{run_id}/table", response_class=HTMLResponse)
    def run_table_partial(
        request: Request,
        run_id: int,
        sort: str = "stars",
        dir: str = "desc",
        page: str = "1",
    ):
        page_number, hint = _results_page_number(page)
        table = results_context(
            engine_factory(),
            run_id,
            page=page_number,
            per_page=TABLE_PAGE_SIZE,
            sort=sort,
            dir=dir,
        )
        if table is None:
            return _run_not_found(run_id)
        return templates.TemplateResponse(
            request,
            "partials/table.html",
            {"run_id": run_id, "hint": hint, **table},
            status_code=400 if hint else 200,
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
            progress = await run_in_threadpool(
                start_clone,
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

    @app.delete("/runs/{run_id}/clone")
    def clone_cancel_route(run_id: int):
        try:
            progress = cancel_clone(
                engine_factory(), run_id, registry=progress_registry, runs_root=runs_root
            )
        except KeyError:
            return _run_not_found(run_id)
        return progress_payload(progress)

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
