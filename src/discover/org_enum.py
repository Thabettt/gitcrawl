from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from urllib.parse import quote, urlencode

import httpx

from discover.search_shards import RequestFailed, _next_link, _short_message
from lib.gh_client import API_BASE, request_with_retry
from limiter.buckets import BucketLimiter


@dataclass(frozen=True)
class EnumPage:
    url: str
    items: tuple[dict, ...]
    next_url: str | None


def _iter_pages(
    client: httpx.Client,
    first_url: str,
    *,
    max_pages: int | None,
    limiter: BucketLimiter | None,
    token_id: str | None,
    sleep: Callable[[float], None],
    now: Callable[[], float],
    jitter: Callable[[], float] | None,
    on_response: Callable[[httpx.Response, float], None] | None,
) -> Iterator[EnumPage]:
    url: str | None = first_url
    pages = 0
    while url is not None and (max_pages is None or pages < max_pages):
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
        if response.status_code != 200:
            raise RequestFailed(int(response.status_code), _short_message(response))
        payload = response.json()
        if isinstance(payload, list):
            raw_items = payload
        elif isinstance(payload, dict):
            raw_items = payload.get("items")
        else:
            raw_items = None
        items = tuple(raw_items or ())
        if not items:
            return
        next_url = _next_link(response.headers.get("link"))
        yield EnumPage(url=url, items=items, next_url=next_url)
        pages += 1
        url = next_url


def iter_org_repos(
    client: httpx.Client,
    org: str,
    *,
    repo_type: str = "all",
    per_page: int = 100,
    max_pages: int | None = None,
    limiter: BucketLimiter | None = None,
    token_id: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
    on_response: Callable[[httpx.Response, float], None] | None = None,
) -> Iterator[EnumPage]:
    url = (
        f"{API_BASE}/orgs/{quote(org, safe='')}/repos?"
        f"{urlencode({'type': repo_type, 'per_page': per_page})}"
    )
    yield from _iter_pages(
        client,
        url,
        max_pages=max_pages,
        limiter=limiter,
        token_id=token_id,
        sleep=sleep,
        now=now,
        jitter=jitter,
        on_response=on_response,
    )


def iter_user_repos(
    client: httpx.Client,
    user: str,
    *,
    repo_type: str = "owner",
    per_page: int = 100,
    max_pages: int | None = None,
    limiter: BucketLimiter | None = None,
    token_id: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
    on_response: Callable[[httpx.Response, float], None] | None = None,
) -> Iterator[EnumPage]:
    url = (
        f"{API_BASE}/users/{quote(user, safe='')}/repos?"
        f"{urlencode({'type': repo_type, 'per_page': per_page})}"
    )
    yield from _iter_pages(
        client,
        url,
        max_pages=max_pages,
        limiter=limiter,
        token_id=token_id,
        sleep=sleep,
        now=now,
        jitter=jitter,
        on_response=on_response,
    )
