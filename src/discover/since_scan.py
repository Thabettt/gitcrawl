from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse

import httpx
from sqlalchemy import func, update
from sqlalchemy.engine import Engine

from discover.search_shards import RequestFailed, _next_link, _short_message
from lib.gh_client import API_BASE, request_with_retry
from limiter.buckets import BucketLimiter
from store.models import Shard


@dataclass(frozen=True)
class SincePage:
    since: int
    items: tuple[dict, ...]
    max_id: int | None
    next_since: int | None


def _since_param(url: str) -> int | None:
    for key, value in parse_qsl(urlparse(url).query):
        if key == "since":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _page_max_id(items: tuple[dict, ...]) -> int | None:
    ids = [item["id"] for item in items if isinstance(item.get("id"), int)]
    return max(ids) if ids else None


def iter_since_pages(
    client: httpx.Client,
    *,
    since: int = 0,
    stop_after_id: int | None = None,
    per_page: int = 100,
    max_pages: int | None = None,
    limiter: BucketLimiter | None = None,
    token_id: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
    on_response: Callable[[httpx.Response, float], None] | None = None,
) -> Iterator[SincePage]:
    url: str | None = f"{API_BASE}/repositories?{urlencode({'since': since, 'per_page': per_page})}"
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
        max_id = _page_max_id(items)
        next_url = _next_link(response.headers.get("link"))
        cursor = _since_param(url)
        yield SincePage(
            since=since if cursor is None else cursor,
            items=items,
            max_id=max_id,
            next_since=_since_param(next_url) if next_url else None,
        )
        pages += 1
        if not items:
            break
        if max_id is not None and stop_after_id is not None and max_id > stop_after_id:
            break
        url = next_url


def save_checkpoint(engine: Engine, shard_id: int, max_id: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(Shard)
            .where(Shard.id == shard_id)
            .values(since_max=max_id, updated_at=func.now())
        )
