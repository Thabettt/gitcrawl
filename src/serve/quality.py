from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from store.models import RunItem, Runs

_SPARSE_EXPECTED = {"country_iso"}
_WARN_NULL_RATIO = 0.5


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    value: object | None = None


@dataclass(frozen=True)
class QualityReport:
    run_id: int
    status: str
    checks: tuple[Check, ...]
    generated_at: datetime


def _overall(checks: list[Check]) -> str:
    if any(check.status == "fail" for check in checks):
        return "fail"
    if any(check.status == "warn" for check in checks):
        return "warn"
    return "ok"


def _bundle_path(runs_root: str, filter_hash: str, run_id: int) -> Path:
    return Path(runs_root) / filter_hash / str(run_id) / "bundle.json"


def _load_bundle(runs_root: str, filter_hash: str, run_id: int) -> dict | None:
    path = _bundle_path(runs_root, filter_hash, run_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def run_quality(
    engine: Engine,
    run_id: int,
    *,
    runs_root: str = "runs",
    now: Callable[[], datetime] | None = None,
) -> QualityReport:
    with engine.connect() as connection:
        run = connection.execute(select(Runs).where(Runs.id == run_id)).mappings().one_or_none()
        if run is None:
            raise KeyError(run_id)
        item_count = connection.scalar(
            select(func.count()).select_from(RunItem).where(RunItem.run_id == run_id)
        )
        duplicate_names = connection.scalar(
            select(func.count()).select_from(
                select(RunItem.full_name)
                .where(RunItem.run_id == run_id)
                .group_by(RunItem.full_name)
                .having(func.count(func.distinct(RunItem.repo_id)) > 1)
                .subquery()
            )
        )
        null_language = connection.scalar(
            select(func.count())
            .select_from(RunItem)
            .where(RunItem.run_id == run_id, RunItem.language.is_(None))
        )
        null_license = connection.scalar(
            select(func.count())
            .select_from(RunItem)
            .where(RunItem.run_id == run_id, RunItem.license_spdx.is_(None))
        )
        null_country = connection.scalar(
            select(func.count())
            .select_from(RunItem)
            .where(RunItem.run_id == run_id, RunItem.country_iso.is_(None))
        )
    checks: list[Check] = []
    item_count = int(item_count or 0)
    expected = int(run["inserted"] or 0)
    parity_status = "ok" if item_count == expected else "warn"
    checks.append(
        Check(
            "count_parity",
            parity_status,
            f"run_items={item_count} runs.inserted={expected} fetched={run['fetched']}",
            value=item_count - expected,
        )
    )
    duplicates = int(duplicate_names or 0)
    checks.append(
        Check(
            "duplicate_full_names",
            "fail" if duplicates else "ok",
            f"{duplicates} full_name value(s) mapped to multiple repo ids",
            value=duplicates,
        )
    )
    for name, nulls in (
        ("language", null_language),
        ("license_spdx", null_license),
        ("country_iso", null_country),
    ):
        ratio = (int(nulls or 0) / item_count) if item_count else 0.0
        limit = 0.9 if name in _SPARSE_EXPECTED else _WARN_NULL_RATIO
        checks.append(
            Check(
                f"missing_{name}",
                "warn" if ratio > limit else "ok",
                f"{ratio:.0%} null ({nulls}/{item_count})",
                value=ratio,
            )
        )
    incomplete = run["incomplete_shards"] or 0
    checks.append(
        Check(
            "incomplete",
            "warn" if run["status"] == "partial" or incomplete else "ok",
            f"status={run['status']} incomplete_shards={incomplete}",
        )
    )
    bundle = _load_bundle(runs_root, str(run["filter_hash"]), run_id)
    if bundle is None:
        checks.append(Check("bundle", "warn", "bundle.json missing or unreadable"))
    else:
        bundle_items = bundle.get("items")
        if not isinstance(bundle_items, list):
            bundle_items = []
        consistent = len(bundle_items) == item_count
        checks.append(
            Check(
                "bundle",
                "ok" if consistent else "warn",
                f"bundle items={len(bundle_items)} run_items={item_count}",
            )
        )
        field_stats = bundle.get("field_stats")
        if not isinstance(field_stats, dict):
            field_stats = {}
        empty_fields = [name for name, stats in field_stats.items() if not stats]
        checks.append(
            Check(
                "field_coverage",
                "warn" if empty_fields else "ok",
                (
                    f"fields with no data: {', '.join(sorted(empty_fields))}"
                    if empty_fields
                    else "all enriched fields have data"
                ),
                value=empty_fields,
            )
        )
    return QualityReport(
        run_id=run_id,
        status=_overall(checks),
        checks=tuple(checks),
        generated_at=now() if now is not None else datetime.now(UTC),
    )
