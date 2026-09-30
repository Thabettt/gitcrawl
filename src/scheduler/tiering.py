from __future__ import annotations

from datetime import UTC, datetime

from scheduler.shard_planner import ShardSpec


def order_shards(specs: list[ShardSpec]) -> list[ShardSpec]:
    return sorted(
        specs,
        key=lambda spec: (
            spec.range_start is not None,
            spec.range_start or datetime.min.replace(tzinfo=UTC),
        ),
    )


def _stars(item: dict) -> float:
    value = item.get("stargazers")
    if value is None:
        value = item.get("stargazers_count")
    return value if isinstance(value, (int, float)) else 0


def _pushed(item: dict) -> str:
    value = item.get("pushed_at")
    return value if isinstance(value, str) else ""


def order_repos(items: list[dict]) -> list[dict]:
    newest_first = sorted(items, key=_pushed, reverse=True)
    return sorted(newest_first, key=lambda item: -_stars(item))
