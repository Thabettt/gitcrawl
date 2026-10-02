from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from lib.deadlines import Deadline

RESOURCE_SPECS: dict[str, tuple[int, float]] = {
    "search": (30, 60.0),
    "core": (5000, 3600.0),
    "code_search": (10, 60.0),
    "graphql": (5000, 3600.0),
}

_KEY_PREFIX = "gitcrawl:rl"
_CONCURRENCY_RETRY_AFTER = 0.5
_MIN_KEY_TTL = 3600.0

_ACQUIRE_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local max_concurrent = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
local paused = tonumber(redis.call('HGET', key, 'paused_until') or '0')
if paused > now then
  return {0, tostring(paused - now)}
end
local window = math.floor(now / window_seconds)
local stored_window = tonumber(redis.call('HGET', key, 'window') or '-1')
local count = tonumber(redis.call('HGET', key, 'count') or '0')
if stored_window ~= window then
  count = 0
end
if count >= limit then
  local retry_after = (window + 1) * window_seconds - now
  if retry_after < 0 then
    retry_after = 0
  end
  redis.call('HSET', key, 'window', window, 'count', count, 'paused_until', paused)
  redis.call('EXPIRE', key, ttl)
  return {0, tostring(retry_after)}
end
local slots = tonumber(redis.call('HGET', key, 'slots') or '0')
if slots >= max_concurrent then
  redis.call('HSET', key, 'window', window, 'count', count, 'paused_until', paused)
  redis.call('EXPIRE', key, ttl)
  return {0, tostring(CONCURRENCY_RETRY_AFTER)}
end
count = count + 1
slots = slots + 1
redis.call('HSET', key, 'window', window, 'count', count, 'slots', slots, 'paused_until', paused)
redis.call('EXPIRE', key, ttl)
return {1, '0'}
""".replace("CONCURRENCY_RETRY_AFTER", str(_CONCURRENCY_RETRY_AFTER))

_RELEASE_SCRIPT = """
local key = KEYS[1]
local slots = tonumber(redis.call('HGET', key, 'slots') or '0')
if slots > 0 then
  redis.call('HSET', key, 'slots', slots - 1)
end
return 1
"""

_RECONCILE_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local target = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local window = math.floor(now / window_seconds)
local stored_window = tonumber(redis.call('HGET', key, 'window') or '-1')
local count = tonumber(redis.call('HGET', key, 'count') or '0')
if stored_window ~= window then
  count = 0
end
if target > count then
  count = target
end
redis.call('HSET', key, 'window', window, 'count', count)
redis.call('EXPIRE', key, ttl)
return count
"""


@dataclass(frozen=True)
class AcquireResult:
    allowed: bool
    retry_after: float | None


def _as_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def _parse_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


class BucketLimiter:
    def __init__(
        self,
        redis: Any,
        *,
        specs: dict[str, tuple[int, float]] = RESOURCE_SPECS,
        max_concurrent: int = 10,
        deadline: Deadline | None = None,
    ) -> None:
        self._redis = redis
        self._specs = specs
        self._max_concurrent = max_concurrent
        self._deadline = deadline

    def bind_deadline(self, deadline: Deadline | None) -> None:
        self._deadline = deadline

    @property
    def deadline(self) -> Deadline | None:
        return self._deadline

    def _key(self, resource: str, token_id: str) -> str:
        return f"{_KEY_PREFIX}:{resource}:{token_id}"

    def _ttl(self, window_seconds: float, extra: float = 0.0) -> int:
        return int(max(2.0 * window_seconds, _MIN_KEY_TTL, extra)) + 1

    def acquire(self, resource: str, token_id: str, *, now: float) -> AcquireResult:
        limit, window_seconds = self._specs[resource]
        raw = self._redis.eval(
            _ACQUIRE_SCRIPT,
            1,
            self._key(resource, token_id),
            now,
            window_seconds,
            limit,
            self._max_concurrent,
            self._ttl(window_seconds),
        )
        if _as_text(raw[0]) == "1":
            return AcquireResult(True, None)
        return AcquireResult(False, float(_as_text(raw[1])))

    def release(self, resource: str, token_id: str) -> None:
        self._redis.eval(_RELEASE_SCRIPT, 1, self._key(resource, token_id))

    def pause(self, resource: str, token_id: str, seconds: float, *, now: float) -> None:
        window_seconds = self._specs.get(resource, (0, 60.0))[1]
        key = self._key(resource, token_id)
        pipe = self._redis.pipeline()
        pipe.hset(key, "paused_until", now + seconds)
        pipe.expire(key, self._ttl(window_seconds, seconds))
        pipe.execute()

    def paused_until(self, resource: str, token_id: str) -> float | None:
        raw = self._redis.hget(self._key(resource, token_id), "paused_until")
        if raw is None:
            return None
        return float(_as_text(raw))

    def update_from_headers(
        self,
        resource: str,
        token_id: str,
        headers: Mapping[str, str],
        *,
        now: float,
    ) -> None:
        normalized = {str(name).lower(): value for name, value in headers.items()}
        remaining = _parse_int(normalized.get("x-ratelimit-remaining"))
        reset = _parse_float(normalized.get("x-ratelimit-reset"))
        if remaining == 0 and reset is not None:
            self.pause(resource, token_id, max(0.0, reset - now), now=now)
        if remaining is None or remaining < 0:
            return
        spec = self._specs.get(resource)
        if spec is None:
            return
        limit, window_seconds = spec
        target = max(0, limit - remaining)
        self._redis.eval(
            _RECONCILE_SCRIPT,
            1,
            self._key(resource, token_id),
            now,
            window_seconds,
            target,
            self._ttl(window_seconds),
        )
