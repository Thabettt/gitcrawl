from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from lib.gh_client import API_VERSION, load_tokens
from serve.executor import Runner, create_run, execute_run
from serve.filter_spec import FilterSpecError, parse_filter_spec, spec_to_dict
from serve.forms import build_spec_from_form, form_state
from store.models import RunItem, Runs

CSRF_COOKIE = "gc_csrf"
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf"
FORM_CONTENT_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")
HEALTH_TIMEOUT_SECONDS = 0.25
RECENT_RUNS_LIMIT = 20
_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


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
    url = os.environ.get("REDIS_URL")
    if not url:
        return False
    import redis

    client = redis.Redis.from_url(
        url,
        socket_connect_timeout=HEALTH_TIMEOUT_SECONDS,
        socket_timeout=HEALTH_TIMEOUT_SECONDS,
    )
    try:
        return bool(client.ping())
    finally:
        client.close()


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
    return hmac.compare_digest(str(cookie), str(supplied))


def _flat_form(form) -> dict[str, str]:
    grouped: dict[str, list[str]] = {}
    for key, value in form.multi_items():
        if isinstance(value, str):
            grouped.setdefault(key, []).append(value)
    return {key: ",".join(values) for key, values in grouped.items()}


def register_pages(
    app: FastAPI,
    *,
    engine_factory: Callable[[], Engine],
    runs_root: str,
    redis_ping: Callable[[], object] | None = None,
    token_present: Callable[[], bool] | None = None,
    runner_factory: Callable[[], Runner] | None = None,
) -> None:
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
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

    def health_snapshot() -> dict[str, bool]:
        return {
            "database": _bounded(database_ok),
            "redis": redis_ok(),
            "github_token_present": token_ok(),
        }

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
        values = {key: value for key, value in request.query_params.multi_items() if key != "q"}
        if "q" in request.query_params:
            values["keywords"] = request.query_params["q"]
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
        try:
            runner = runner_or_none()
        except Exception:
            return Response(status_code=500, content="runner unavailable", media_type="text/plain")
        engine = engine_factory()
        run_id = create_run(engine, spec_to_dict(spec), api_version=API_VERSION)
        execute_run(engine, run_id, runner=runner, runs_root=runs_root)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: int):
        engine = engine_factory()
        with engine.connect() as connection:
            row = connection.execute(select(Runs).where(Runs.id == run_id)).mappings().one_or_none()
            if row is None:
                return templates.TemplateResponse(
                    request,
                    "run_min.html",
                    {"run": None, "rows": [], "csrf_token": request.state.csrf_token},
                    status_code=404,
                )
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
                    )
                    .where(RunItem.run_id == run_id)
                    .order_by(RunItem.stargazers.desc(), RunItem.repo_id)
                    .limit(100)
                )
                .mappings()
                .all()
            )
        summary = {
            **dict(row),
            "duration": _duration(row["started_at"], row["finished_at"]),
            "ran_at": _relative_time(row["finished_at"] or row["started_at"] or row["created_at"]),
        }
        return templates.TemplateResponse(
            request,
            "run_min.html",
            {
                "run": summary,
                "rows": [{**dict(item), "pushed_at": _iso(item["pushed_at"])} for item in rows],
                "csrf_token": request.state.csrf_token,
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
        execute_run(engine, new_id, runner=runner, runs_root=runs_root)
        return RedirectResponse(f"/runs/{new_id}", status_code=303)
