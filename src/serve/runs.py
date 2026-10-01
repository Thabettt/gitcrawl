from __future__ import annotations

import csv
import io
import json
import threading
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import Engine, Row
from sqlalchemy.engine.row import RowMapping

from enrich.cloner import (
    PROGRESS_NAME,
    CloneEstimate,
    CloneMode,
    CloneProgress,
    GitRunner,
    clone_repos,
    estimate_clone,
    estimate_clone_from_totals,
    free_disk_mb,
)
from serve.executor import _CSV_HEADER, _csv_cell
from store.models import Repo, RunItem, Runs

_MEDIA_TYPES = {"json": "application/json", "csv": "text/csv"}
_BUNDLE_FILES = {"json": "bundle.json", "csv": "corpus.csv"}


def run_bundle_dir(runs_root: str, filter_hash: str, run_id: int) -> Path:
    return Path(runs_root) / filter_hash / str(run_id)


def bundle_file(runs_root: str, filter_hash: str, run_id: int, format: str) -> Path:
    return run_bundle_dir(runs_root, filter_hash, run_id) / _BUNDLE_FILES[format]


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


def latest_run_for_hash(
    engine: Engine, filter_hash: str, *, statuses: tuple[str, ...] | None = None
) -> Row | None:
    statement = select(Runs).where(Runs.filter_hash == filter_hash)
    if statuses is not None:
        statement = statement.where(Runs.status.in_(statuses))
    with engine.connect() as connection:
        return (
            connection.execute(statement.order_by(Runs.created_at.desc(), Runs.id.desc()).limit(1))
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
                .order_by(RunItem.stargazers.desc().nullslast(), RunItem.repo_id)
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
        "regenerated": True,
        "items": [_snapshot(row) for row in _item_rows(engine, run_id)],
        "field_stats": {},
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
    path = bundle_file(runs_root, run_row["filter_hash"], run_id, format)
    if path.is_file():
        return path.read_bytes(), _MEDIA_TYPES[format]
    if format == "json":
        return _regenerate_json(engine, run_row, run_id), _MEDIA_TYPES[format]
    return _regenerate_csv(engine, run_id), _MEDIA_TYPES[format]


class CloneRegistry:
    def __init__(self) -> None:
        self._entries: dict[int, CloneProgress] = {}
        self._lock = threading.Lock()

    def get(self, run_id: int) -> CloneProgress | None:
        with self._lock:
            return self._entries.get(run_id)

    def set(self, run_id: int, progress: CloneProgress) -> None:
        with self._lock:
            self._entries[run_id] = progress


class _PersistedProgress(CloneProgress):
    def __init__(self, path: Path, **values: object) -> None:
        super().__init__(**values)
        self._path = path

    def emit(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(asdict(self), ensure_ascii=False), encoding="utf-8")


def _count_run_items(engine: Engine, run_id: int) -> int:
    with engine.connect() as connection:
        count = connection.scalar(
            select(func.count()).select_from(RunItem).where(RunItem.run_id == run_id)
        )
    return int(count or 0)


def _run_totals(engine: Engine, run_id: int) -> tuple[int, int]:
    with engine.connect() as connection:
        count, size_kb = connection.execute(
            select(func.count(), func.coalesce(func.sum(Repo.size_kb), 0))
            .select_from(RunItem)
            .outerjoin(Repo, Repo.id == RunItem.repo_id)
            .where(RunItem.run_id == run_id)
        ).one()
    return int(count or 0), int(size_kb or 0)


def clone_estimate_for_run(
    engine: Engine,
    run_id: int,
    *,
    limit: int | None,
    mode: CloneMode,
    dest_root: str = "clones",
    low_disk_threshold_mb: float = 2048.0,
) -> CloneEstimate:
    if limit is None:
        repos, size_kb = _run_totals(engine, run_id)
        _row_for_run(engine, run_id)  # preserve KeyError semantics
        return estimate_clone_from_totals(
            repos,
            size_kb,
            mode=mode,
            disk_free_mb=free_disk_mb(dest_root),
            low_disk_threshold_mb=low_disk_threshold_mb,
        )
    if limit < 0:
        raise ValueError("limit", "limit must be >= 0")
    return estimate_clone(
        engine,
        run_id,
        limit=limit,
        mode=mode,
        disk_free_mb=free_disk_mb(dest_root),
        low_disk_threshold_mb=low_disk_threshold_mb,
    )


def progress_payload(progress: CloneProgress) -> dict:
    return asdict(progress)


def read_clone_progress(
    engine: Engine,
    run_id: int,
    *,
    registry: CloneRegistry,
    runs_root: str = "runs",
) -> CloneProgress:
    tracked = registry.get(run_id)
    if tracked is not None:
        return tracked
    row = _row_for_run(engine, run_id)
    path = run_bundle_dir(runs_root, row["filter_hash"], run_id) / PROGRESS_NAME
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            data = None
        if isinstance(data, dict):
            errors = data.get("errors")
            error_list = [str(item) for item in errors] if isinstance(errors, list) else []
            return CloneProgress(
                status=str(data.get("status", "done")),
                total=int(data.get("total", 0)),
                completed=int(data.get("completed", 0)),
                failed=int(data.get("failed", 0)),
                current=data.get("current"),
                errors=error_list,
                error_count=int(data.get("error_count", len(error_list))),
            )
    return CloneProgress(status="done", total=0, completed=0, failed=0)


def start_clone(
    engine: Engine,
    run_id: int,
    *,
    limit: int,
    mode: CloneMode,
    registry: CloneRegistry,
    runs_root: str = "runs",
    dest_root: str = "clones",
    git_runner: GitRunner | None = None,
) -> CloneProgress:
    row = _row_for_run(engine, run_id)
    existing = registry.get(run_id)
    if existing is not None and existing.status == "running":
        return existing
    bundle_dir = run_bundle_dir(runs_root, row["filter_hash"], run_id)
    progress = _PersistedProgress(
        bundle_dir / PROGRESS_NAME,
        status="running",
        total=0,
        completed=0,
        failed=0,
    )
    registry.set(run_id, progress)
    progress.total = min(limit, _count_run_items(engine, run_id)) if limit > 0 else 0
    if limit <= 0:
        progress.status = "done"
        progress.emit()
        return progress
    worker = threading.Thread(
        target=_clone_worker,
        args=(engine, run_id, limit, mode, dest_root, git_runner, progress),
        daemon=True,
    )
    worker.start()
    return progress


def _clone_worker(
    engine: Engine,
    run_id: int,
    limit: int,
    mode: CloneMode,
    dest_root: str,
    git_runner: GitRunner | None,
    progress: CloneProgress,
) -> None:
    try:
        clone_repos(
            engine,
            run_id,
            limit=limit,
            mode=mode,
            dest_root=dest_root,
            git_runner=git_runner,
            progress=progress,
        )
    except Exception as exc:
        progress.status = "failed"
        progress.current = None
        progress.errors.append(f"{type(exc).__name__}: {exc}"[:300])
    finally:
        progress.emit()
