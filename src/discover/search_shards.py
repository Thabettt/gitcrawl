from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from urllib.parse import urlencode

import httpx

from lib import audit
from lib.gh_client import API_BASE, RequestFailed, next_link, request_with_retry, short_message
from limiter.buckets import BucketLimiter

_CAP_MESSAGE = "only the first 1000"


class SearchCapExceeded(Exception):
    def __init__(self, query: str) -> None:
        super().__init__(query)
        self.query = query


@dataclass(frozen=True)
class ShardPage:
    url: str
    items: tuple[dict, ...]
    total_count: int
    incomplete: bool
    next_url: str | None
    exhausted: bool = False


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
                raise RequestFailed(200, short_message(response)) from None
            if isinstance(payload, list):
                raw_items = payload
                total_count = 0
                incomplete = False
            elif isinstance(payload, dict):
                raw_items = payload.get("items")
                total_count = int(payload.get("total_count") or 0)
                incomplete = bool(payload.get("incomplete_results"))
            else:
                raise RequestFailed(200, short_message(response))
            next_url = next_link(response.headers.get("link"))
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
        message = short_message(response)
        if response.status_code == 422 and _CAP_MESSAGE in message.lower():
            raise SearchCapExceeded(query)
        raise RequestFailed(int(response.status_code), message)
