from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urljoin, urlparse

import httpx

from discover.search_shards import RequestFailed, _short_message
from lib import audit
from lib.gh_client import API_BASE, request_with_retry
from limiter.buckets import BucketLimiter

_REDIRECT_STATUSES = frozenset({301, 302})
_MAX_REDIRECTS = 3


class RepoNotFound(Exception):
    def __init__(self, full_name: str) -> None:
        super().__init__(full_name)
        self.full_name = full_name


@dataclass(frozen=True)
class HydratedRepo:
    id: int
    node_id: str
    full_name: str
    payload: dict | None
    etag: str | None
    not_modified: bool


def _valid_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    repo_id = payload.get("id")
    return (
        isinstance(repo_id, int)
        and not isinstance(repo_id, bool)
        and isinstance(payload.get("node_id"), str)
        and isinstance(payload.get("full_name"), str)
    )


def hydrate_repo(
    client: httpx.Client,
    full_name: str,
    *,
    etag: str | None = None,
    limiter: BucketLimiter | None = None,
    token_id: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
    on_response: Callable[[httpx.Response, float], None] | None = None,
) -> HydratedRepo:
    url = f"{API_BASE}/repos/{full_name}"
    headers = {"If-None-Match": etag} if etag else {}
    hops = 0
    while True:
        response = request_with_retry(
            client,
            "GET",
            url,
            extra_headers=headers or None,
            limiter=limiter,
            token_id=token_id,
            sleep=sleep,
            now=now,
            jitter=jitter,
            on_response=on_response,
        )
        if response.status_code in _REDIRECT_STATUSES:
            location = response.headers.get("location")
            if not location or hops >= _MAX_REDIRECTS:
                raise RequestFailed(int(response.status_code), "redirect limit exceeded")
            url = urljoin(url, location)
            headers = {}
            hops += 1
            continue
        if response.status_code == 304:
            return HydratedRepo(
                id=0,
                node_id="",
                full_name=full_name,
                payload=None,
                etag=response.headers.get("etag") or etag,
                not_modified=True,
            )
        if response.status_code == 404:
            raise RepoNotFound(full_name)
        if response.status_code != 200:
            raise RequestFailed(int(response.status_code), _short_message(response))
        try:
            payload = audit.cached_json(response) or response.json()
        except ValueError:
            raise RequestFailed(200, _short_message(response)) from None
        if not _valid_payload(payload):
            raise RequestFailed(200, _short_message(response))
        return HydratedRepo(
            id=payload["id"],
            node_id=payload["node_id"],
            full_name=payload["full_name"],
            payload=payload,
            etag=response.headers.get("etag"),
            not_modified=False,
        )


def fetch_commit_count(
    client: httpx.Client,
    full_name: str,
    *,
    limiter: BucketLimiter | None = None,
    token_id: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
    on_response: Callable[[httpx.Response, float], None] | None = None,
) -> int | None:
    response = request_with_retry(
        client,
        "GET",
        f"{API_BASE}/repos/{full_name}/commits?per_page=1",
        limiter=limiter,
        token_id=token_id,
        sleep=sleep,
        now=now,
        jitter=jitter,
        on_response=on_response,
    )
    if response.status_code != 200:
        return None
    for part in (response.headers.get("link") or "").split(","):
        segments = part.split(";")
        url_part = segments[0].strip()
        if not (url_part.startswith("<") and url_part.endswith(">")):
            continue
        if any('rel="last"' in segment for segment in segments[1:]):
            params = dict(parse_qsl(urlparse(url_part[1:-1]).query))
            page = params.get("page")
            if page and page.isdigit():
                return int(page)
    try:
        payload = response.json()
    except ValueError:
        return None
    return len(payload) if isinstance(payload, list) else None
