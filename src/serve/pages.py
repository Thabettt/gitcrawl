from __future__ import annotations

import hmac
import os
import secrets
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from lib.gh_client import load_tokens
from store.models import Runs

CSRF_COOKIE = "gc_csrf"
CSRF_HEADER = "x-csrf-token"
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


def validate_csrf(request: Request) -> bool:
    cookie = request.cookies.get(CSRF_COOKIE)
    supplied = request.headers.get(CSRF_HEADER)
    if not cookie or not supplied:
        return False
    return hmac.compare_digest(str(cookie), str(supplied))


def register_pages(
    app: FastAPI,
    *,
    engine_factory: Callable[[], Engine],
    runs_root: str,
    redis_ping: Callable[[], object] | None = None,
    token_present: Callable[[], bool] | None = None,
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
