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
_HYDRATE_CONSTRAINT = "`max_hydrate` must not exceed `max_candidates`"
_HYDRATE_HINT = "raise `max_candidates` or lower `max_hydrate`"


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
        effective = {**before.as_dict(), **parsed}
        if effective["max_hydrate"] > effective["max_candidates"]:
            return _render(
                request,
                engine,
                token_present=token_ok,
                status_code=400,
                errors=(_HYDRATE_CONSTRAINT,),
                hints=(_HYDRATE_HINT,),
            )
        after = update_run_settings(engine, parsed)
        record_settings_change(engine, before.as_dict(), after.as_dict())
        return RedirectResponse("/settings?saved=1", status_code=303)
