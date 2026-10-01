from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.engine import Engine

from store.models import RunItem, Runs

CHANGED_FIELDS = (
    "stargazers",
    "pushed_at",
    "archived",
    "language",
    "license_spdx",
    "country_iso",
)

_ITEM_COLUMNS = (
    RunItem.repo_id,
    RunItem.full_name,
    RunItem.stargazers,
    RunItem.pushed_at,
    RunItem.archived,
    RunItem.language,
    RunItem.license_spdx,
    RunItem.country_iso,
)


@dataclass(frozen=True)
class RunDiff:
    run_a: int
    run_b: int
    added: tuple[dict, ...]
    removed: tuple[dict, ...]
    changed: tuple[dict, ...]
    summary: dict[str, int]


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _output_value(value: object) -> object:
    if isinstance(value, datetime):
        return _iso(value)
    return value


def _item_key(row) -> tuple[bool, int, str]:
    repo_id = row["repo_id"]
    return (repo_id is None, repo_id or 0, str(row["full_name"]))


def _run_items(engine: Engine, run_id: int) -> dict[tuple[bool, int, str], dict]:
    with engine.connect() as connection:
        known = connection.execute(select(Runs.id).where(Runs.id == run_id)).scalar_one_or_none()
        if known is None:
            raise KeyError(run_id)
        rows = connection.execute(select(*_ITEM_COLUMNS).where(RunItem.run_id == run_id)).mappings()
        return {_item_key(row): dict(row) for row in rows}


def diff_runs(engine: Engine, run_a: int, run_b: int) -> RunDiff:
    items_a = _run_items(engine, run_a)
    items_b = _run_items(engine, run_b)

    added = tuple(
        {"repo_id": items_b[key]["repo_id"], "full_name": items_b[key]["full_name"]}
        for key in sorted(items_b.keys() - items_a.keys())
    )
    removed = tuple(
        {"repo_id": items_a[key]["repo_id"], "full_name": items_a[key]["full_name"]}
        for key in sorted(items_a.keys() - items_b.keys())
    )

    changed: list[dict] = []
    changed_repos = 0
    for key in sorted(items_a.keys() & items_b.keys()):
        row_a = items_a[key]
        row_b = items_b[key]
        repo_changed = False
        for field in CHANGED_FIELDS:
            before = row_a[field]
            after = row_b[field]
            if before == after:
                continue
            repo_changed = True
            changed.append(
                {
                    "repo_id": row_b["repo_id"],
                    "full_name": row_b["full_name"],
                    "field": field,
                    "from": _output_value(before),
                    "to": _output_value(after),
                }
            )
        if repo_changed:
            changed_repos += 1

    return RunDiff(
        run_a=run_a,
        run_b=run_b,
        added=added,
        removed=removed,
        changed=tuple(changed),
        summary={
            "added": len(added),
            "removed": len(removed),
            "changed_repos": changed_repos,
            "changed_fields": len(changed),
        },
    )
