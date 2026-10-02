from __future__ import annotations

import asyncio

from serve.middleware import BodyLimitMiddleware


def run_middleware(middleware, scope):
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    return sent


def test_declared_content_length_over_the_limit_is_rejected_without_reading():
    called: list[bool] = []

    async def app(scope, receive, send):  # pragma: no cover - must not run
        called.append(True)

    middleware = BodyLimitMiddleware(app, default_limit=4)

    messages = run_middleware(
        middleware,
        {
            "type": "http",
            "method": "POST",
            "path": "/vsearch/run",
            "headers": [(b"content-length", b"5")],
        },
    )

    assert called == []
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 413


def test_path_specific_limit_allows_a_larger_upload():
    async def app(scope, receive, send):
        message = await receive()
        assert message["body"] == b"123456"
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = BodyLimitMiddleware(app, default_limit=4, path_limits={"/find": 8})
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"123456", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(
        middleware(
            {"type": "http", "method": "POST", "path": "/find", "headers": []},
            receive,
            send,
        )
    )

    assert sent[0]["status"] == 204
