from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode

import httpx

from discover.search_shards import RequestFailed, _short_message
from lib.gh_client import request_with_retry

ECOSYSTEMS_BASE_URL = "https://repos.ecosyste.ms/api/v1/hosts/GitHub/repositories"
DEPSDEV_BASE_URL = "https://api.deps.dev/v3/projects"
SCORECARD_BASE_URL = "https://api.scorecard.dev/projects"

_NOT_FOUND_STATUSES = frozenset({404, 410})
_METADATA_KEYS = ("funding", "readme", "codeowners", "security")


@dataclass(frozen=True)
class MirrorRepo:
    full_name: str
    stars: int | None
    forks: int | None
    language: str | None
    license: str | None
    topics: tuple[str, ...] = ()
    pushed_at: str | None = None
    metadata: dict = field(default_factory=dict)


def _json_dict(response: httpx.Response) -> dict:
    if response.status_code != 200:
        raise RequestFailed(int(response.status_code), _short_message(response))
    try:
        payload = response.json()
    except ValueError:
        raise RequestFailed(200, _short_message(response)) from None
    if not isinstance(payload, dict):
        raise RequestFailed(200, _short_message(response))
    return payload


def _fetch_json(
    client: httpx.Client,
    url: str,
    *,
    sleep: Callable[[float], None],
    now: Callable[[], float],
    jitter: Callable[[], float] | None,
    on_response: Callable[[httpx.Response, float], None] | None,
) -> dict | None:
    response = request_with_retry(
        client,
        "GET",
        url,
        auth=False,
        sleep=sleep,
        now=now,
        jitter=jitter,
        on_response=on_response,
    )
    if response.status_code in _NOT_FOUND_STATUSES:
        return None
    return _json_dict(response)


def _as_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _as_str(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _license_name(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("spdx_id", "spdx", "name"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    return None


def _topics(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _metadata(payload: dict) -> dict:
    raw = payload.get("metadata")
    metadata = dict(raw) if isinstance(raw, dict) else {}
    for key in _METADATA_KEYS:
        if key not in metadata and key in payload:
            metadata[key] = payload[key]
    return metadata


def fetch_ecosystems_repo(
    client: httpx.Client,
    full_name: str,
    *,
    mailto: str | None = None,
    base_url: str = ECOSYSTEMS_BASE_URL,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
    on_response: Callable[[httpx.Response, float], None] | None = None,
) -> MirrorRepo | None:
    url = f"{base_url}/{quote(full_name, safe='')}"
    if mailto:
        url = f"{url}?{urlencode({'mailto': mailto})}"
    payload = _fetch_json(
        client,
        url,
        sleep=sleep,
        now=now,
        jitter=jitter,
        on_response=on_response,
    )
    if payload is None:
        return None
    return MirrorRepo(
        full_name=_as_str(payload.get("full_name")) or full_name,
        stars=_as_int(payload.get("stargazers_count")),
        forks=_as_int(payload.get("forks_count")),
        language=_as_str(payload.get("language")),
        license=_license_name(payload.get("license")),
        topics=_topics(payload.get("topics")),
        pushed_at=_as_str(payload.get("pushed_at")),
        metadata=_metadata(payload),
    )


def fetch_depsdev_project(
    client: httpx.Client,
    full_name: str,
    *,
    base_url: str = DEPSDEV_BASE_URL,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
    on_response: Callable[[httpx.Response, float], None] | None = None,
) -> dict | None:
    project = quote(f"github.com/{full_name}", safe="")
    return _fetch_json(
        client,
        f"{base_url}/{project}",
        sleep=sleep,
        now=now,
        jitter=jitter,
        on_response=on_response,
    )


def fetch_scorecard(
    client: httpx.Client,
    full_name: str,
    *,
    base_url: str = SCORECARD_BASE_URL,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
    on_response: Callable[[httpx.Response, float], None] | None = None,
) -> dict | None:
    return _fetch_json(
        client,
        f"{base_url}/github.com/{full_name}",
        sleep=sleep,
        now=now,
        jitter=jitter,
        on_response=on_response,
    )
