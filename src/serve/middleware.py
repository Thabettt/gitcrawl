from __future__ import annotations

import json
from collections.abc import Mapping
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
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


JSON_BODY_LIMIT_BYTES = 1024 * 1024
UPLOAD_BODY_LIMIT_BYTES = 10 * 1024 * 1024


class BodyLimitMiddleware:
    """Cap state-changing request bodies before the route reads them."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        default_limit: int,
        path_limits: Mapping[str, int] | None = None,
    ) -> None:
        self.app = app
        self.default_limit = default_limit
        self.path_limits = dict(path_limits or {})

    def limit_for(self, path: str) -> int:
        return self.path_limits.get(path, self.default_limit)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in STATE_CHANGING_METHODS:
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope["path"])
        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    await self._reject(scope, receive, send, limit)
                    return
            except ValueError:
                pass
        buffered: list[dict] = []
        received = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            received += len(message.get("body", b""))
            if received > limit:
                await self._reject(scope, receive, send, limit)
                return
            buffered.append(message)
            if not message.get("more_body", False):
                break
        iterator = iter(buffered)

        async def replay() -> dict:
            try:
                return next(iterator)
            except StopIteration:
                return {"type": "http.disconnect"}

        await self.app(scope, replay, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        body = json.dumps({"error": "payload_too_large", "limit": limit}).encode()
        response = Response(body, status_code=413, media_type="application/json")
        await response(scope, receive, send)
