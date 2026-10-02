from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fnmatch import translate

import httpx

from discover.search_shards import RequestFailed, _short_message
from lib import audit
from lib.gh_client import API_BASE, request_with_retry
from limiter.buckets import BucketLimiter

_LITERAL_CHARS = frozenset("*?[")


def _is_literal(pattern: str) -> bool:
    return not any(char in _LITERAL_CHARS for char in pattern)


@dataclass(frozen=True)
class FilePresence:
    repo_full_name: str
    paths: frozenset[str]
    truncated: bool
    source: str

    def has(self, path: str) -> bool:
        return path in self.paths

    def match(self, patterns: Sequence[str]) -> dict[str, bool]:
        result: dict[str, bool] = {}
        globs: list[tuple[str, re.Pattern[str]]] = []
        for pattern in patterns:
            if _is_literal(pattern):
                result[pattern] = pattern in self.paths
            else:
                globs.append((pattern, re.compile(translate(pattern))))
                result[pattern] = False
        if globs:
            pending = {pattern for pattern, _ in globs}
            for path in self.paths:
                for pattern, regex in globs:
                    if pattern in pending and regex.match(path):
                        pending.discard(pattern)
                        result[pattern] = True
                if not pending:
                    break
        return result


def _require_json(response: httpx.Response) -> dict:
    if response.status_code != 200:
        raise RequestFailed(int(response.status_code), _short_message(response))
    try:
        payload = audit.cached_json(response) or response.json()
    except ValueError:
        raise RequestFailed(200, _short_message(response)) from None
    if not isinstance(payload, dict):
        raise RequestFailed(200, _short_message(response))
    return payload


def fetch_tree(
    client: httpx.Client,
    full_name: str,
    *,
    ref: str | None = None,
    limiter: BucketLimiter | None = None,
    token_id: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
    on_response: Callable[[httpx.Response, float], None] | None = None,
) -> FilePresence:
    if ref is None:
        repo_payload = _require_json(
            request_with_retry(
                client,
                "GET",
                f"{API_BASE}/repos/{full_name}",
                limiter=limiter,
                token_id=token_id,
                sleep=sleep,
                now=now,
                jitter=jitter,
                on_response=on_response,
            )
        )
        default_branch = repo_payload.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch:
            raise RequestFailed(200, "missing default_branch")
        ref = default_branch
    payload = _require_json(
        request_with_retry(
            client,
            "GET",
            f"{API_BASE}/repos/{full_name}/git/trees/{ref}?recursive=1",
            limiter=limiter,
            token_id=token_id,
            sleep=sleep,
            now=now,
            jitter=jitter,
            on_response=on_response,
        )
    )
    raw_tree = payload.get("tree")
    if not isinstance(raw_tree, list):
        raise RequestFailed(200, "missing tree list")
    paths = frozenset(
        entry["path"]
        for entry in raw_tree
        if isinstance(entry, dict)
        and entry.get("type") == "blob"
        and isinstance(entry.get("path"), str)
    )
    return FilePresence(
        repo_full_name=full_name,
        paths=paths,
        truncated=bool(payload.get("truncated")),
        source="tree",
    )
