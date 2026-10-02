from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import Engine, Row
from sqlalchemy.engine.row import RowMapping
from starlette.responses import StreamingResponse

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
from lib.deadlines import clone_timeout_seconds
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


def _file_chunks(path: Path, chunk_size: int = 65536) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                return
            yield block


def _json_chunks(engine: Engine, run_row: RowMapping, run_id: int) -> Iterator[bytes]:
    header = {
        "filter": run_row["filter_spec"],
        "filter_hash": run_row["filter_hash"],
        "run_id": run_id,
        "ran_at": _iso(run_row["finished_at"] or run_row["created_at"]),
        "api_version": run_row["api_version"],
        "total_count": run_row["total_count"],
        "fetched": run_row["fetched"],
        "incomplete": run_row["status"] == "partial",
        "regenerated": True,
    }
    yield b"{"
    for key, value in header.items():
        yield (
            json.dumps(key).encode() + b": " + json.dumps(value, ensure_ascii=False).encode() + b","
        )
    yield b'"items": ['
    first = True
    for row in _item_rows(engine, run_id):
        chunk = json.dumps(_snapshot(row), ensure_ascii=False).encode()
        yield chunk if first else b"," + chunk
        first = False
    yield b'], "field_stats": {}}'


def _csv_chunks(engine: Engine, run_id: int) -> Iterator[bytes]:
    header_buffer = io.StringIO()
    csv.writer(header_buffer).writerow(_CSV_HEADER)
    yield header_buffer.getvalue().encode("utf-8")
    for row in _item_rows(engine, run_id):
        buffer = io.StringIO()
        csv.writer(buffer).writerow(
            [
                _csv_cell(row["repo_id"]),
                _csv_cell(row["full_name"]),
                _csv_cell(row["stargazers"]),
                _csv_cell(_iso(row["pushed_at"])),
                _csv_cell(row["archived"]),
                _csv_cell(row["language"]),
                _csv_cell(row["license_spdx"]),
                _csv_cell(row["country_iso"]),
                _csv_cell(row["geo_confidence"]),
            ]
        )
        yield buffer.getvalue().encode("utf-8")


def export_bundle(
    engine: Engine, run_id: int, *, format: str, runs_root: str = "runs"
) -> tuple[Iterator[bytes], str]:
    if format not in _MEDIA_TYPES:
        raise ValueError("format", "format must be one of: json, csv")
    run_row = _row_for_run(engine, run_id)
    path = bundle_file(runs_root, run_row["filter_hash"], run_id, format)
    if path.is_file():
        return _file_chunks(path), _MEDIA_TYPES[format]
    if format == "json":
        return _json_chunks(engine, run_row, run_id), _MEDIA_TYPES[format]
    return _csv_chunks(engine, run_id), _MEDIA_TYPES[format]


def export_run(
    engine: Engine, run_id: int, *, format: str = "json", runs_root: str = "runs"
) -> StreamingResponse:
    if format not in _MEDIA_TYPES:
        raise ValueError("format", "format must be one of: json, csv")
    run_row = _row_for_run(engine, run_id)
    regenerated = not bundle_file(runs_root, run_row["filter_hash"], run_id, format).is_file()
    chunks, media_type = export_bundle(engine, run_id, format=format, runs_root=runs_root)
    filename = f"gitcrawl-{run_row['filter_hash']}-{run_id}.{format}"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if regenerated:
        headers["X-Gitcrawl-Regenerated"] = "true"
    return StreamingResponse(chunks, media_type=media_type, headers=headers)


class CloneRegistry:
    def __init__(
        self,
        *,
        max_entries: int = 100,
        ttl_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._entries: OrderedDict[int, CloneProgress] = OrderedDict()
        self._touched: dict[int, float] = {}
        self._cancels: dict[int, threading.Event] = {}
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._ttl = ttl_seconds
        self._clock = clock

    def _evict_locked(self, now: float) -> None:
        for run_id in list(self._entries):
            progress = self._entries[run_id]
            if progress.status == "running":
                continue
            if now - self._touched.get(run_id, now) >= self._ttl:
                del self._entries[run_id]
                self._touched.pop(run_id, None)
        while len(self._entries) > self._max_entries:
            evicted = False
            for run_id in list(self._entries):
                if self._entries[run_id].status != "running":
                    del self._entries[run_id]
                    self._touched.pop(run_id, None)
                    evicted = True
                    break
            if not evicted:
                break

    def get(self, run_id: int) -> CloneProgress | None:
        with self._lock:
            now = self._clock()
            self._evict_locked(now)
            progress = self._entries.get(run_id)
            if progress is None:
                return None
            self._entries.move_to_end(run_id)
            self._touched[run_id] = now
            return progress

    def set(self, run_id: int, progress: CloneProgress) -> None:
        with self._lock:
            now = self._clock()
            self._entries[run_id] = progress
            self._entries.move_to_end(run_id)
            self._touched[run_id] = now
            self._evict_locked(now)

    def claim(
        self, run_id: int, factory: Callable[[], CloneProgress]
    ) -> tuple[CloneProgress, bool]:
        with self._lock:
            now = self._clock()
            self._evict_locked(now)
            existing = self._entries.get(run_id)
            if existing is not None and existing.status == "running":
                self._entries.move_to_end(run_id)
                self._touched[run_id] = now
                return existing, False
            progress = factory()
            self._entries[run_id] = progress
            self._entries.move_to_end(run_id)
            self._touched[run_id] = now
            self._evict_locked(now)
            return progress, True

    def set_cancel(self, run_id: int, event: threading.Event) -> None:
        with self._lock:
            self._cancels[run_id] = event

    def clear_cancel(self, run_id: int) -> None:
        with self._lock:
            self._cancels.pop(run_id, None)

    def cancel(self, run_id: int) -> bool:
        with self._lock:
            event = self._cancels.get(run_id)
        if event is None:
            return False
        event.set()
        return True


_EMIT_INTERVAL_SECONDS = 0.25


class _PersistedProgress(CloneProgress):
    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], float] = time.monotonic,
        emit_interval: float = _EMIT_INTERVAL_SECONDS,
        **values: object,
    ) -> None:
        super().__init__(**values)
        self._path = path
        self._now = now
        self._emit_interval = emit_interval
        self._last_emit = float("-inf")

    def emit(self, *, force: bool = False) -> None:
        moment = self._now()
        if not force and moment - self._last_emit < self._emit_interval:
            return
        self._last_emit = moment
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(".tmp")
        temp.write_text(json.dumps(asdict(self), ensure_ascii=False), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(temp, self._path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02)


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


def _progress_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _progress_errors(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


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
            errors = _progress_errors(data.get("errors"))
            status = data.get("status")
            current = data.get("current")
            return CloneProgress(
                status=status if isinstance(status, str) else "done",
                total=_progress_int(data.get("total")),
                completed=_progress_int(data.get("completed")),
                failed=_progress_int(data.get("failed")),
                current=current if isinstance(current, str) else None,
                errors=errors,
                error_count=_progress_int(data.get("error_count"), default=len(errors)),
            )
    return CloneProgress(status="done", total=0, completed=0, failed=0)


def cancel_clone(
    engine: Engine,
    run_id: int,
    *,
    registry: CloneRegistry,
    runs_root: str = "runs",
) -> CloneProgress:
    progress = read_clone_progress(engine, run_id, registry=registry, runs_root=runs_root)
    if progress.status == "running":
        registry.cancel(run_id)
    return progress


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
    clone_timeout: float | None = None,
    cancel_event: threading.Event | None = None,
) -> CloneProgress:
    row = _row_for_run(engine, run_id)
    bundle_dir = run_bundle_dir(runs_root, row["filter_hash"], run_id)

    def create_progress() -> CloneProgress:
        return _PersistedProgress(
            bundle_dir / PROGRESS_NAME,
            status="running",
            total=0,
            completed=0,
            failed=0,
        )

    progress, claimed = registry.claim(run_id, create_progress)
    if not claimed:
        return progress
    progress.total = min(limit, _count_run_items(engine, run_id)) if limit > 0 else 0
    if limit <= 0:
        progress.status = "done"
        progress.emit(force=True)
        return progress
    timeout = clone_timeout if clone_timeout is not None else clone_timeout_seconds()
    event = cancel_event if cancel_event is not None else threading.Event()
    registry.set_cancel(run_id, event)
    worker = threading.Thread(
        target=_clone_worker,
        args=(
            engine,
            run_id,
            limit,
            mode,
            dest_root,
            git_runner,
            progress,
            registry,
            timeout,
            event,
        ),
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
    registry: CloneRegistry,
    clone_timeout: float,
    cancel_event: threading.Event,
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
            clone_timeout=clone_timeout,
            cancel_event=cancel_event,
        )
    except Exception as exc:
        progress.status = "failed"
        progress.current = None
        progress.error_count += 1
        progress.errors.append(f"{type(exc).__name__}: {exc}"[:300])
    finally:
        registry.clear_cancel(run_id)
        progress.emit(force=True)
