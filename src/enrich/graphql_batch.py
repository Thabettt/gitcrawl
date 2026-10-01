from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import httpx

from discover.search_shards import RequestFailed, _short_message
from lib.gh_client import request_with_retry
from limiter.buckets import BucketLimiter

GRAPHQL_URL = "https://api.github.com/graphql"

_MAX_SPLIT_DEPTH = 4
_SPLIT_MARKERS = ("timeout", "resource limits", "something went wrong")

_REPOSITORY_FIELDS = (
    "      fundingLinks { url platform }",
    "      hasDiscussionsEnabled",
    "      discussions(first: 1) { totalCount }",
    "      sponsorsListing { tiers(first: 1) { monthlyPriceInDollars } }",
)


@dataclass(frozen=True)
class RepoGraphQL:
    repo_id: int
    funding_links: tuple[dict, ...] = ()
    has_discussions: bool | None = None
    discussions_count: int | None = None
    sponsors_tiers: tuple[dict, ...] = ()
    rate_limit_cost: int | None = None


def build_batch_query(
    repo_ids: Sequence[int],
    *,
    node_ids: Sequence[str] | None = None,
    max_aliases: int = 20,
) -> str:
    if node_ids is None:
        raise ValueError("node_ids are required")
    ids = list(repo_ids)
    nodes = list(node_ids)
    if len(ids) != len(nodes):
        raise ValueError("repo_ids and node_ids must have the same length")
    if len(ids) > max_aliases:
        raise ValueError(f"too many aliases: {len(ids)} exceeds {max_aliases}")
    lines = ["query {", "  rateLimit { cost remaining resetAt }"]
    for repo_id, node_id in zip(ids, nodes, strict=True):
        lines.append(f'  c{repo_id}: node(id: "{node_id}") {{')
        lines.append("    ... on Repository {")
        lines.extend(_REPOSITORY_FIELDS)
        lines.append("    }")
        lines.append("  }")
    lines.append("}")
    return "\n".join(lines)


def _as_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _funding_links(node: dict) -> tuple[dict, ...]:
    raw = node.get("fundingLinks")
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, dict))


def _discussions_count(node: dict) -> int | None:
    raw = node.get("discussions")
    if not isinstance(raw, dict):
        return None
    return _as_int(raw.get("totalCount"))


def _sponsors_tiers(node: dict) -> tuple[dict, ...]:
    listing = node.get("sponsorsListing")
    if not isinstance(listing, dict):
        return ()
    raw = listing.get("tiers")
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, dict))


def _repo_id_from_alias(alias: str) -> int | None:
    if len(alias) < 2 or alias[0] != "c":
        return None
    digits = alias[1:]
    if not digits.isdigit():
        return None
    return int(digits)


def parse_batch_response(payload: dict) -> dict[int, RepoGraphQL]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    rate = data.get("rateLimit")
    rate_limit_cost = _as_int(rate.get("cost")) if isinstance(rate, dict) else None
    results: dict[int, RepoGraphQL] = {}
    for alias, node in data.items():
        repo_id = _repo_id_from_alias(alias)
        if repo_id is None or not isinstance(node, dict):
            continue
        has_discussions = node.get("hasDiscussionsEnabled")
        results[repo_id] = RepoGraphQL(
            repo_id=repo_id,
            funding_links=_funding_links(node),
            has_discussions=has_discussions if isinstance(has_discussions, bool) else None,
            discussions_count=_discussions_count(node),
            sponsors_tiers=_sponsors_tiers(node),
            rate_limit_cost=rate_limit_cost,
        )
    return results


def _error_messages(payload: dict) -> list[str]:
    raw = payload.get("errors")
    if not isinstance(raw, list):
        return []
    messages = []
    for error in raw:
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            messages.append(error["message"])
    return messages


def _is_split_worthy(messages: Sequence[str]) -> bool:
    for message in messages:
        lowered = message.lower()
        if any(marker in lowered for marker in _SPLIT_MARKERS):
            return True
    return False


def _post_query(
    client: httpx.Client,
    *,
    token: str,
    repo_ids: Sequence[int],
    node_ids: Sequence[str],
    graphql_url: str,
    max_aliases: int,
    limiter: BucketLimiter | None,
    sleep: Callable[[float], None],
    now: Callable[[], float],
    jitter: Callable[[], float] | None,
    on_response: Callable[[httpx.Response, float], None] | None,
) -> dict:
    query = build_batch_query(repo_ids, node_ids=node_ids, max_aliases=max_aliases)
    response = request_with_retry(
        client,
        "POST",
        graphql_url,
        json_body={"query": query},
        extra_headers={"Authorization": f"Bearer {token}"},
        limiter=limiter,
        sleep=sleep,
        now=now,
        jitter=jitter,
        on_response=on_response,
    )
    if response.status_code != 200:
        raise RequestFailed(int(response.status_code), _short_message(response))
    try:
        payload = response.json()
    except ValueError:
        raise RequestFailed(200, _short_message(response)) from None
    if not isinstance(payload, dict):
        raise RequestFailed(200, _short_message(response))
    return payload


def _fetch(
    client: httpx.Client,
    *,
    token: str,
    repo_ids: list[int],
    node_ids: list[str],
    graphql_url: str,
    max_aliases: int,
    limiter: BucketLimiter | None,
    sleep: Callable[[float], None],
    now: Callable[[], float],
    jitter: Callable[[], float] | None,
    on_response: Callable[[httpx.Response, float], None] | None,
    depth: int,
) -> dict[int, RepoGraphQL]:
    merged: dict[int, RepoGraphQL] = {}
    stack: list[tuple[list[int], list[str], int]] = [(repo_ids, node_ids, depth)]
    last_error: RequestFailed | None = None
    while stack:
        ids, nodes, current_depth = stack.pop()
        try:
            payload = _post_query(
                client,
                token=token,
                repo_ids=ids,
                node_ids=nodes,
                graphql_url=graphql_url,
                max_aliases=max_aliases,
                limiter=limiter,
                sleep=sleep,
                now=now,
                jitter=jitter,
                on_response=on_response,
            )
        except RequestFailed as exc:
            last_error = exc
            if len(ids) > 1 and current_depth < _MAX_SPLIT_DEPTH:
                middle = len(ids) // 2
                stack.append((ids[middle:], nodes[middle:], current_depth + 1))
                stack.append((ids[:middle], nodes[:middle], current_depth + 1))
            continue
        messages = _error_messages(payload)
        if not messages:
            merged.update(parse_batch_response(payload))
            continue
        if _is_split_worthy(messages) and len(ids) > 1 and current_depth < _MAX_SPLIT_DEPTH:
            middle = len(ids) // 2
            stack.append((ids[middle:], nodes[middle:], current_depth + 1))
            stack.append((ids[:middle], nodes[:middle], current_depth + 1))
            continue
        last_error = RequestFailed(200, messages[0])
    if not merged and last_error is not None:
        raise last_error
    return merged


def fetch_graphql_batch(
    client: httpx.Client,
    *,
    token: str,
    repo_ids: Sequence[int],
    node_ids: Sequence[str] | None = None,
    graphql_url: str = GRAPHQL_URL,
    max_aliases: int = 20,
    limiter: BucketLimiter | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
    on_response: Callable[[httpx.Response, float], None] | None = None,
) -> dict[int, RepoGraphQL]:
    if not repo_ids:
        return {}
    if node_ids is None:
        raise ValueError("node_ids are required")
    return _fetch(
        client,
        token=token,
        repo_ids=list(repo_ids),
        node_ids=list(node_ids),
        graphql_url=graphql_url,
        max_aliases=max_aliases,
        limiter=limiter,
        sleep=sleep,
        now=now,
        jitter=jitter,
        on_response=on_response,
        depth=0,
    )
