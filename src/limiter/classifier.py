from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from enum import StrEnum


class Action(StrEnum):
    FREE = "free"
    RETRY_AFTER = "retry_after"
    WAIT_RESET = "wait_reset"
    BACKOFF = "backoff"
    SHARD = "shard"
    FIX = "fix"
    FAIL_LOUD = "fail_loud"


@dataclass(frozen=True)
class Decision:
    action: Action
    sleep_seconds: float | None
    resource: str | None
    reason: str


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if str(key).lower() == name:
            return value
    return None


def _parse_number(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_retry_after(value: object, now: float) -> float | None:
    seconds = _parse_number(value)
    if seconds is not None:
        return seconds
    if value is None:
        return None
    try:
        parsed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, parsed.timestamp() - now)


def _backoff_seconds(attempt: int, jitter: Callable[[], float]) -> float:
    return min(480.0, 60.0 * 2**attempt) + jitter()


def classify(
    status: int,
    headers: Mapping[str, str],
    *,
    attempt: int = 0,
    error_code: str | None = None,
    message: str = "",
    now: float,
    jitter: Callable[[], float] = lambda: random.random(),
) -> Decision:
    resource = _header(headers, "x-ratelimit-resource")
    sso = _header(headers, "x-github-sso")
    if sso is not None and "partial-results" in str(sso).lower():
        return Decision(Action.FAIL_LOUD, None, resource, "sso partial-results")
    if status == 401:
        return Decision(Action.FAIL_LOUD, None, resource, "unauthorized")
    if 200 <= status < 300 or status == 304:
        reason = "success" if status != 304 else "not_modified"
        return Decision(Action.FREE, None, resource, reason)
    if status in (403, 429):
        retry_after = _parse_retry_after(_header(headers, "retry-after"), now)
        if retry_after is not None and retry_after > 0:
            return Decision(Action.RETRY_AFTER, retry_after, resource, "retry-after")
        remaining = _header(headers, "x-ratelimit-remaining")
        reset = _parse_number(_header(headers, "x-ratelimit-reset"))
        if remaining is not None and str(remaining).strip() == "0" and reset is not None:
            return Decision(Action.WAIT_RESET, max(0.0, reset - now), resource, "rate limit reset")
        if attempt >= 5:
            return Decision(Action.FAIL_LOUD, None, resource, "retries exhausted")
        return Decision(Action.BACKOFF, _backoff_seconds(attempt, jitter), resource, "backoff")
    if status == 422:
        if error_code == "custom":
            if attempt >= 5:
                return Decision(Action.FAIL_LOUD, None, resource, "retries exhausted")
            return Decision(
                Action.BACKOFF, _backoff_seconds(attempt, jitter), resource, "spam signature"
            )
        if "only the first 1000" in message.lower():
            return Decision(Action.SHARD, None, resource, "result cap; shard the query")
        return Decision(Action.FIX, None, resource, "validation failed")
    if status in (500, 502, 503, 504):
        if attempt >= 5:
            return Decision(Action.FAIL_LOUD, None, resource, "retries exhausted")
        return Decision(Action.BACKOFF, _backoff_seconds(attempt, jitter), resource, "server error")
    return Decision(Action.FIX, None, resource, "unexpected status")


_TRANSPORT_MAX_ATTEMPTS = 3
_TRANSPORT_BACKOFF_CAP = 15.0


def classify_transport(
    attempt: int, *, jitter: Callable[[], float] = lambda: random.random()
) -> Decision:
    if attempt >= _TRANSPORT_MAX_ATTEMPTS:
        return Decision(Action.FAIL_LOUD, None, None, "transport retries exhausted")
    delay = min(_TRANSPORT_BACKOFF_CAP, 2.0 * 2**attempt) + jitter()
    return Decision(Action.BACKOFF, delay, None, "transport error")
