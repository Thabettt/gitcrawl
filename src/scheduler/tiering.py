from __future__ import annotations


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
