from __future__ import annotations

from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _same_origin(candidate: str, base) -> bool:
    try:
        parsed = urlsplit(candidate)
        if parsed.scheme not in ("http", "https") or parsed.hostname is None:
            return False
        if parsed.hostname != base.hostname or parsed.scheme != base.scheme:
            return False
        return (parsed.port or _default_port(parsed.scheme)) == (
            base.port or _default_port(base.scheme)
        )
    except ValueError:
        return False


class OriginCsrfMiddleware:
    """Reject cross-site state-changing requests when Origin/Referer is present.

    Requests without Origin and Referer pass unchanged (script clients). Per-route
    CSRF validation stays in place as defense in depth.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in STATE_CHANGING_METHODS:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        source = origin if origin is not None else headers.get("referer")
        if source is not None and not _same_origin(source, Request(scope, receive).base_url):
            response = PlainTextResponse("cross-origin request rejected", status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
