from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from serve import pages
from serve.metrics import LABELS, metrics_payload

_PLAIN = {
    "search_remaining": "Requests left this hour",
    "incomplete_results_ratio": "Searches that came back incomplete",
    "rate_422": "Rejected filters",
    "rate_403_429": "Throttled responses",
    "p95_latency_ms": "Median response (ms)",
    "shard_coverage": "Search coverage",
    "geo_unmatched_rate": "Owners without a country",
}


def _dot(status: dict[str, bool]) -> tuple[str, str]:
    if not status.get("database", False):
        return "red", "Database is down"
    if not status.get("redis", True) or not status.get("github_token_present", False):
        return "amber", "Degraded — see System"
    return "green", "All systems ready"


def register_system(
    application: FastAPI,
    *,
    engine_factory: Callable,
    health_snapshot: Callable[[], dict[str, bool]],
    metrics_redis: Callable | None = None,
) -> None:
    @application.get("/partials/status-dot", response_class=HTMLResponse)
    def status_dot(request: Request):
        color, label = _dot(health_snapshot())
        return pages._templates.TemplateResponse(
            request, "partials/status_dot.html", {"color": color, "label": label}
        )

    @application.get("/system", response_class=HTMLResponse)
    def system_page(request: Request):
        status = health_snapshot()
        try:
            payload = metrics_payload(
                engine_factory(), redis_client=metrics_redis and metrics_redis()
            )
        except Exception:
            payload = None
        return pages._templates.TemplateResponse(
            request,
            "system.html",
            {
                "status": status,
                "metrics": payload,
                "metrics_error": payload is None,
                "plain": _PLAIN,
                "labels": LABELS,
                "csrf_token": request.state.csrf_token,
            },
        )
