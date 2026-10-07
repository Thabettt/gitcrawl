from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass

import httpx

from lib import audit
from lib.gh_client import RequestFailed, request_with_retry
from lib.graphql_batch import (
    GRAPHQL_URL,
    GraphQLAuthError,
    MalformedResponse,
    is_transient_error,
)
from limiter.buckets import BucketLimiter

MAX_ALIASES = 20
PAGE_SIZE = 100
AuditHook = Callable[[httpx.Response, float, Mapping[str, object]], None]

_NODE_FIELDS = """      databaseId
      id
      nameWithOwner
      name
      description
      homepageUrl
      primaryLanguage { name }
      licenseInfo { spdxId }
      repositoryTopics(first: 100) { nodes { topic { name } } }
      visibility
      isFork
      parent { nameWithOwner }
      isArchived
      isDisabled
      isTemplate
      mirrorUrl
      diskUsage
      stargazerCount
      forkCount
      watchers { totalCount }
      issues(states: [OPEN]) { totalCount }
      defaultBranchRef { name }
      hasIssuesEnabled
      hasWikiEnabled
      hasProjectsEnabled
      hasDiscussionsEnabled
      hasPullRequestsEnabled
      owner {
        login
        __typename
        ... on User { databaseId }
        ... on Organization { databaseId }
      }
      createdAt
      pushedAt
      updatedAt"""


@dataclass(frozen=True)
class SearchPage:
    query: str
    items: tuple[dict, ...]
    repository_count: int
    has_next: bool
    end_cursor: str | None
    exhausted: bool = False
    incomplete: bool = False


def _as_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _total_count(value: object) -> int | None:
    if not isinstance(value, Mapping):
        return None
    return _as_int(value.get("totalCount"))


def _owner(node: Mapping[str, object]) -> dict | None:
    raw = node.get("owner")
    if not isinstance(raw, Mapping):
        return None
    owner_id = _as_int(raw.get("databaseId"))
    login = _as_str(raw.get("login"))
    if owner_id is None or not login:
        return None
    kind = raw.get("__typename")
    return {
        "id": owner_id,
        "login": login,
        "type": "Organization" if kind == "Organization" else "User",
    }


def _license(node: Mapping[str, object]) -> dict | None:
    raw = node.get("licenseInfo")
    if not isinstance(raw, Mapping):
        return None
    spdx = _as_str(raw.get("spdxId"))
    return {"spdx_id": spdx} if spdx is not None else None


def _topics(node: Mapping[str, object]) -> list[str]:
    raw = node.get("repositoryTopics")
    if not isinstance(raw, Mapping):
        return []
    entries = raw.get("nodes")
    if not isinstance(entries, list):
        return []
    topics: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        topic = entry.get("topic")
        if not isinstance(topic, Mapping):
            continue
        name = _as_str(topic.get("name"))
        if name is not None:
            topics.append(name)
    return topics


def _language(node: Mapping[str, object]) -> str | None:
    raw = node.get("primaryLanguage")
    if not isinstance(raw, Mapping):
        return None
    return _as_str(raw.get("name"))


def _parent(node: Mapping[str, object]) -> dict | None:
    raw = node.get("parent")
    if not isinstance(raw, Mapping):
        return None
    full_name = _as_str(raw.get("nameWithOwner"))
    return {"full_name": full_name} if full_name is not None else None


def _branch(node: Mapping[str, object]) -> str | None:
    raw = node.get("defaultBranchRef")
    if not isinstance(raw, Mapping):
        return None
    return _as_str(raw.get("name"))


def _visibility(node: Mapping[str, object]) -> str | None:
    raw = _as_str(node.get("visibility"))
    return raw.lower() if raw else None


def node_to_item(node: Mapping[str, object]) -> dict:
    return {
        "id": _as_int(node.get("databaseId")),
        "node_id": _as_str(node.get("id")),
        "full_name": _as_str(node.get("nameWithOwner")),
        "owner": _owner(node),
        "name": _as_str(node.get("name")),
        "description": _as_str(node.get("description")),
        "homepage": _as_str(node.get("homepageUrl")),
        "language": _language(node),
        "license": _license(node),
        "topics": _topics(node),
        "visibility": _visibility(node),
        "fork": _as_bool(node.get("isFork")),
        "parent": _parent(node),
        "archived": _as_bool(node.get("isArchived")),
        "disabled": _as_bool(node.get("isDisabled")),
        "mirror_url": _as_str(node.get("mirrorUrl")),
        "is_template": _as_bool(node.get("isTemplate")),
        "size": _as_int(node.get("diskUsage")),
        "stargazers_count": _as_int(node.get("stargazerCount")),
        "forks_count": _as_int(node.get("forkCount")),
        "watchers_count": _total_count(node.get("watchers")),
        "open_issues_count": _total_count(node.get("issues")),
        "default_branch": _branch(node),
        "has_wiki": _as_bool(node.get("hasWikiEnabled")),
        "has_issues": _as_bool(node.get("hasIssuesEnabled")),
        "has_projects": _as_bool(node.get("hasProjectsEnabled")),
        "has_pages": None,
        "has_discussions": _as_bool(node.get("hasDiscussionsEnabled")),
        "has_pull_requests": _as_bool(node.get("hasPullRequestsEnabled")),
        "custom_properties": {},
        "created_at": _as_str(node.get("createdAt")),
        "pushed_at": _as_str(node.get("pushedAt")),
        "updated_at": _as_str(node.get("updatedAt")),
    }


def _page_query(query: str, after: str | None) -> str:
    args = [f"first: {PAGE_SIZE}"]
    if after is not None:
        args.append(f'"after": {json.dumps(after)}')
    args.append(f"query: {json.dumps(query)}")
    args.append("type: REPOSITORY")
    return (
        "query {\n"
        f"  s: search({', '.join(args)}) {{\n"
        "    repositoryCount\n"
        "    pageInfo { hasNextPage endCursor }\n"
        "    nodes { ... on Repository {\n"
        f"{_NODE_FIELDS}\n"
        "    } }\n"
        "  }\n"
        "  rateLimit { cost remaining }\n"
        "}"
    )


def _count_query(queries: Sequence[str]) -> str:
    lines = ["query {"]
    for index, query in enumerate(queries):
        lines.append(
            f"  s{index}: search(first: 1, query: {json.dumps(query)}, type: REPOSITORY) "
            "{ repositoryCount }"
        )
    lines.append("  rateLimit { cost remaining }")
    lines.append("}")
    return "\n".join(lines)


def _post(
    client: httpx.Client,
    query_text: str,
    *,
    limiter: BucketLimiter | None,
    token_id: str | None,
    on_response: AuditHook | None,
    params: Mapping[str, object],
    sleep: Callable[[float], None],
    now: Callable[[], float],
    jitter: Callable[[], float] | None,
) -> tuple[dict, list[str]]:
    transport_hook = (
        None
        if on_response is None
        else (lambda response, latency_ms: on_response(response, latency_ms, params))
    )
    response = request_with_retry(
        client,
        "POST",
        GRAPHQL_URL,
        json_body={"query": query_text},
        limiter=limiter,
        token_id=token_id,
        on_response=transport_hook,
        sleep=sleep,
        now=now,
        jitter=jitter,
    )
    if response.status_code == 401:
        raise GraphQLAuthError("github rejected the token (HTTP 401)")
    if response.status_code != 200:
        raise RequestFailed(int(response.status_code), "graphql request failed")
    body = audit.cached_json(response)
    if body is None:
        try:
            body = response.json()
        except ValueError:
            raise MalformedResponse("graphql response is not JSON") from None
    if not isinstance(body, dict):
        raise MalformedResponse("graphql response is not an object")
    data = body.get("data")
    errors = [
        str(entry.get("message")) for entry in body.get("errors") or [] if isinstance(entry, dict)
    ]
    return (data if isinstance(data, dict) else {}), errors


def _retry_wait(attempt: int, jitter: Callable[[], float] | None) -> float:
    extra = jitter() if jitter is not None else 0.0
    return min(2.0**attempt, 30.0) + extra


def _count_value(node: object) -> int | None:
    if not isinstance(node, Mapping):
        return None
    return _as_int(node.get("repositoryCount"))


def _probe_batch(
    client: httpx.Client,
    batch: Sequence[str],
    resolved: dict[str, int],
    *,
    limiter: BucketLimiter | None,
    token_id: str | None,
    on_response: AuditHook | None,
    sleep: Callable[[float], None],
    now: Callable[[], float],
    jitter: Callable[[], float] | None,
    max_attempts: int,
) -> None:
    pending = list(batch)
    attempt = 0
    while True:
        attempt += 1
        data, errors = _post(
            client,
            _count_query(pending),
            limiter=limiter,
            token_id=token_id,
            on_response=on_response,
            params={"q": pending[0], "after": None},
            sleep=sleep,
            now=now,
            jitter=jitter,
        )
        missing: list[str] = []
        for index, query in enumerate(pending):
            count = _count_value(data.get(f"s{index}"))
            if count is None:
                missing.append(query)
            else:
                resolved[query] = count
        if not missing:
            return
        if errors and not any(is_transient_error(message) for message in errors):
            raise RequestFailed(200, errors[0])
        if attempt >= max_attempts:
            detail = errors[0] if errors else f"graphql count probe missing for {missing[0]}"
            raise RequestFailed(200, detail)
        sleep(_retry_wait(attempt, jitter))
        pending = missing


def count_queries(
    client: httpx.Client,
    queries: Sequence[str],
    *,
    limiter: BucketLimiter | None = None,
    token_id: str | None = None,
    on_response: AuditHook | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
    max_attempts: int = 3,
) -> list[int]:
    unique = list(dict.fromkeys(queries))
    resolved: dict[str, int] = {}
    for start in range(0, len(unique), MAX_ALIASES):
        _probe_batch(
            client,
            unique[start : start + MAX_ALIASES],
            resolved,
            limiter=limiter,
            token_id=token_id,
            on_response=on_response,
            sleep=sleep,
            now=now,
            jitter=jitter,
            max_attempts=max_attempts,
        )
    return [resolved[query] for query in queries]


def _fetch_search(
    client: httpx.Client,
    query: str,
    after: str | None,
    *,
    limiter: BucketLimiter | None,
    token_id: str | None,
    on_response: AuditHook | None,
    sleep: Callable[[float], None],
    now: Callable[[], float],
    jitter: Callable[[], float] | None,
    max_attempts: int,
) -> tuple[Mapping[str, object], list[str]]:
    attempt = 0
    while True:
        attempt += 1
        data, errors = _post(
            client,
            _page_query(query, after),
            limiter=limiter,
            token_id=token_id,
            on_response=on_response,
            params={"q": query, "after": after},
            sleep=sleep,
            now=now,
            jitter=jitter,
        )
        search = data.get("s")
        if isinstance(search, Mapping):
            return search, errors
        if errors and not any(is_transient_error(message) for message in errors):
            raise RequestFailed(200, errors[0])
        if attempt >= max_attempts:
            detail = errors[0] if errors else "graphql search connection missing"
            raise RequestFailed(200, detail)
        sleep(_retry_wait(attempt, jitter))


def iter_pages(
    client: httpx.Client,
    query: str,
    *,
    max_pages: int = 10,
    limiter: BucketLimiter | None = None,
    token_id: str | None = None,
    on_response: AuditHook | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
    max_attempts: int = 3,
) -> Iterator[SearchPage]:
    after: str | None = None
    for page_number in range(1, max_pages + 1):
        search, errors = _fetch_search(
            client,
            query,
            after,
            limiter=limiter,
            token_id=token_id,
            on_response=on_response,
            sleep=sleep,
            now=now,
            jitter=jitter,
            max_attempts=max_attempts,
        )
        repository_count = _as_int(search.get("repositoryCount")) or 0
        page_info = search.get("pageInfo")
        info = page_info if isinstance(page_info, Mapping) else {}
        has_next = info.get("hasNextPage") is True
        end_cursor = _as_str(info.get("endCursor"))
        nodes = search.get("nodes")
        entries = nodes if isinstance(nodes, list) else []
        items = tuple(node_to_item(entry) for entry in entries if isinstance(entry, Mapping))
        incomplete = bool(errors)
        exhausted = has_next and page_number >= max_pages
        yield SearchPage(
            query=query,
            items=items,
            repository_count=repository_count,
            has_next=has_next,
            end_cursor=end_cursor,
            exhausted=exhausted,
            incomplete=incomplete,
        )
        if incomplete or not has_next or exhausted:
            return
        if end_cursor is None:
            raise RequestFailed(200, "graphql search page is missing its end cursor")
        after = end_cursor
