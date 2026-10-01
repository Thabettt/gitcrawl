from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from urllib.parse import urlencode

import httpx

from lib.gh_client import API_BASE, request_with_retry
from limiter.buckets import BucketLimiter
from serve import audit

_CAP_MESSAGE = "only the first 1000"
_BODY_FALLBACK_CHARS = 300


class SearchCapExceeded(Exception):
    def __init__(self, query: str) -> None:
        super().__init__(query)
        self.query = query


class RequestFailed(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


@dataclass(frozen=True)
class ShardPage:
    url: str
    items: tuple[dict, ...]
    total_count: int
    incomplete: bool
    next_url: str | None
    exhausted: bool = False


def _next_link(header: str | None) -> str | None:
    if not header:
        return None
    for part in header.split(","):
        segments = part.split(";")
        url_part = segments[0].strip()
        if not (url_part.startswith("<") and url_part.endswith(">")):
            continue
        for segment in segments[1:]:
            key, _, value = segment.partition("=")
            if key.strip().lower() == "rel" and value.strip().strip('"').lower() == "next":
                return url_part[1:-1]
    return None


def _short_message(response: httpx.Response) -> str:
    payload = None
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("message"), str):
        return payload["message"]
    return response.text[:_BODY_FALLBACK_CHARS]


def iter_shard_pages(
    client: httpx.Client,
    query: str,
    *,
    limiter: BucketLimiter | None = None,
    token_id: str | None = None,
    per_page: int = 100,
    max_pages: int = 10,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
    on_response: Callable[[httpx.Response, float], None] | None = None,
) -> Iterator[ShardPage]:
    url: str | None = (
        f"{API_BASE}/search/repositories?"
        f"{urlencode({'q': query, 'per_page': per_page, 'page': 1})}"
    )
    pages = 0
    while url is not None and pages < max_pages:
        response = request_with_retry(
            client,
            "GET",
            url,
            limiter=limiter,
            token_id=token_id,
            sleep=sleep,
            now=now,
            jitter=jitter,
            on_response=on_response,
        )
        if response.status_code == 200:
            try:
                payload = audit.cached_json(response) or response.json()
            except ValueError:
                raise RequestFailed(200, _short_message(response)) from None
            if isinstance(payload, list):
                raw_items = payload
                total_count = 0
                incomplete = False
            elif isinstance(payload, dict):
                raw_items = payload.get("items")
                total_count = int(payload.get("total_count") or 0)
                incomplete = bool(payload.get("incomplete_results"))
            else:
                raise RequestFailed(200, _short_message(response))
            next_url = _next_link(response.headers.get("link"))
            pages += 1
            page = ShardPage(
                url=url,
                items=tuple(raw_items or ()),
                total_count=total_count,
                incomplete=incomplete,
                next_url=next_url,
            )
            if next_url is not None and pages >= max_pages:
                page = replace(page, exhausted=True)
            yield page
            url = next_url
            continue
        message = _short_message(response)
        if response.status_code == 422 and _CAP_MESSAGE in message.lower():
            raise SearchCapExceeded(query)
        raise RequestFailed(int(response.status_code), message)
