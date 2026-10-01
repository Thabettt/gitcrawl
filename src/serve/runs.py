from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import Engine, Row
from sqlalchemy.engine.row import RowMapping

from serve.executor import _CSV_HEADER, _csv_cell
from store.models import RunItem, Runs

_MEDIA_TYPES = {"json": "application/json", "csv": "text/csv"}
_BUNDLE_FILES = {"json": "bundle.json", "csv": "corpus.csv"}


def run_bundle_dir(runs_root: str, filter_hash: str, run_id: int) -> Path:
    return Path(runs_root) / filter_hash / str(run_id)


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def latest_run_for_hash(engine: Engine, filter_hash: str) -> Row | None:
    with engine.connect() as connection:
        return (
            connection.execute(
                select(Runs)
                .where(Runs.filter_hash == filter_hash)
                .order_by(Runs.created_at.desc(), Runs.id.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )


def _row_for_run(engine: Engine, run_id: int) -> RowMapping:
    with engine.connect() as connection:
        row = connection.execute(select(Runs).where(Runs.id == run_id)).mappings().one_or_none()
    if row is None:
        raise KeyError(run_id)
    return row


def _item_rows(engine: Engine, run_id: int) -> list[RowMapping]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                select(
                    RunItem.repo_id,
                    RunItem.full_name,
                    RunItem.stargazers,
                    RunItem.pushed_at,
                    RunItem.archived,
                    RunItem.language,
                    RunItem.license_spdx,
                    RunItem.country_iso,
                    RunItem.geo_confidence,
                    RunItem.virtuals,
                )
                .where(RunItem.run_id == run_id)
                .order_by(RunItem.repo_id)
            ).mappings()
        )


def _snapshot(row: RowMapping) -> dict:
    return {
        "repo_id": row["repo_id"],
        "full_name": row["full_name"],
        "stargazers": row["stargazers"],
        "pushed_at": _iso(row["pushed_at"]),
        "archived": row["archived"],
        "language": row["language"],
        "license_spdx": row["license_spdx"],
        "country_iso": row["country_iso"],
        "geo_confidence": row["geo_confidence"],
        "virtuals": dict(row["virtuals"] or {}),
    }


def _regenerate_json(engine: Engine, run_row: RowMapping, run_id: int) -> bytes:
    bundle = {
        "filter": run_row["filter_spec"],
        "filter_hash": run_row["filter_hash"],
        "run_id": run_id,
        "ran_at": _iso(run_row["finished_at"] or run_row["created_at"]),
        "api_version": run_row["api_version"],
        "total_count": run_row["total_count"],
        "fetched": run_row["fetched"],
        "incomplete": run_row["status"] == "partial",
        "items": [_snapshot(row) for row in _item_rows(engine, run_id)],
    }
    return json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")


def _regenerate_csv(engine: Engine, run_id: int) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_CSV_HEADER)
    for row in _item_rows(engine, run_id):
        item = _snapshot(row)
        writer.writerow(
            [
                _csv_cell(item["repo_id"]),
                _csv_cell(item["full_name"]),
                _csv_cell(item["stargazers"]),
                _csv_cell(item["pushed_at"]),
                _csv_cell(item["archived"]),
                _csv_cell(item["language"]),
                _csv_cell(item["license_spdx"]),
                _csv_cell(item["country_iso"]),
                _csv_cell(item["geo_confidence"]),
            ]
        )
    return buffer.getvalue().encode("utf-8")


def export_bundle(
    engine: Engine, run_id: int, *, format: str, runs_root: str = "runs"
) -> tuple[bytes, str]:
    if format not in _MEDIA_TYPES:
        raise ValueError("format", "format must be one of: json, csv")
    run_row = _row_for_run(engine, run_id)
    path = run_bundle_dir(runs_root, run_row["filter_hash"], run_id) / _BUNDLE_FILES[format]
    if path.is_file():
        return path.read_bytes(), _MEDIA_TYPES[format]
    if format == "json":
        return _regenerate_json(engine, run_row, run_id), _MEDIA_TYPES[format]
    return _regenerate_csv(engine, run_id), _MEDIA_TYPES[format]
