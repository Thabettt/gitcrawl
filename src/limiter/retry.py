from __future__ import annotations

DEFAULT_KEY = "gitcrawl:retry"

_POP_DUE_SCRIPT = """
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
local removed = {}
for index = 1, #due do
  local member = due[index]
  if redis.call('ZREM', KEYS[1], member) == 1 then
    removed[#removed + 1] = member
  end
end
return removed
"""


def _as_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


class RetryQueue:
    def __init__(self, redis, key: str = DEFAULT_KEY) -> None:
        self._redis = redis
        self._key = key

    def schedule(self, payload: str, due_at: float) -> None:
        self._redis.zadd(self._key, {payload: due_at})

    def pop_due(self, *, now: float, limit: int = 100) -> list[str]:
        raw = self._redis.eval(_POP_DUE_SCRIPT, 1, self._key, now, limit)
        return [_as_text(item) for item in raw]

    def size(self) -> int:
        return int(self._redis.zcard(self._key))
