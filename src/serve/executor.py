from __future__ import annotations

import csv
import hashlib
import itertools
import json
import queue
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.engine import Engine

from lib import cancellation, progress
from lib.batching import chunked
from lib.cancellation import RunCancelled
from store.models import RunItem, Runs


@dataclass
class RunPayloadItem:
    repo_id: int
    full_name: str
    stargazers: int | None = None
    pushed_at: str | None = None
    archived: bool | None = None
    language: str | None = None
    license_spdx: str | None = None
    country_iso: str | None = None
    geo_confidence: str | None = None
    virtuals: dict = field(default_factory=dict)
    raw: dict | None = None


@dataclass
class RunPayload:
    total_count: int | None
    items: list[RunPayloadItem]
    incomplete: bool = False
    fetched: int | None = None
    warnings: list[str] = field(default_factory=list)
    field_stats: dict = field(default_factory=dict)
    updated: int | None = None
    unchanged: int | None = None
    skipped: int | None = None
    timings: dict = field(default_factory=dict)


Runner = Callable[[int, dict], RunPayload]

_CSV_HEADER = [
    "id",
    "full_name",
    "stargazers",
    "pushed_at",
    "archived",
    "language",
    "license_spdx",
    "country_iso",
    "geo_confidence",
]


def _filter_hash(filter_spec: dict) -> str:
    canonical = json.dumps(filter_spec, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _default_runner(run_id: int, filter_spec: dict) -> RunPayload:
    raise RuntimeError("no runner configured")


def _error_message(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    text = text.split("\n", 1)[0]
    for marker in ("{", "Validation Failed", "upstream"):
        index = text.find(marker)
        if index > 0:
            text = text[:index].rstrip(" :")
    return text[:300]


def _snapshot_item(item: RunPayloadItem) -> dict:
    return {
        "repo_id": item.repo_id,
        "full_name": item.full_name,
        "stargazers": item.stargazers,
        "pushed_at": item.pushed_at,
        "archived": item.archived,
        "language": item.language,
        "license_spdx": item.license_spdx,
        "country_iso": item.country_iso,
        "geo_confidence": item.geo_confidence,
        "virtuals": item.virtuals,
    }


def _bundle_item(item: RunPayloadItem) -> dict:
    return item.raw if item.raw is not None else _snapshot_item(item)


def create_run(engine: Engine, filter_spec: dict, *, api_version: str) -> int:
    with engine.begin() as connection:
        run_id = connection.execute(
            insert(Runs)
            .values(
                filter_hash=_filter_hash(filter_spec),
                filter_spec=filter_spec,
                api_version=api_version,
            )
            .returning(Runs.id)
        ).scalar_one()
    return int(run_id)


def run_status(engine: Engine, run_id: int) -> dict:
    with engine.connect() as connection:
        run = connection.execute(select(Runs).where(Runs.id == run_id)).mappings().one_or_none()
    if run is None:
        raise KeyError(run_id)
    return {
        "id": run["id"],
        "status": run["status"],
        "filter_hash": run["filter_hash"],
        "total_count": run["total_count"],
        "fetched": run["fetched"],
        "inserted": run["inserted"],
        "updated": run["updated"],
        "unchanged": run["unchanged"],
        "skipped": run["skipped"],
        "incomplete_shards": run["incomplete_shards"],
        "error": run["error"],
        "created_at": run["created_at"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "bundle_dir": run["bundle_dir"],
    }


_SNAPSHOT_BATCH = 1000


def _snapshot_items(engine: Engine, run_id: int, items: list[RunPayloadItem]) -> None:
    with engine.begin() as connection:
        connection.execute(delete(RunItem).where(RunItem.run_id == run_id))
        for batch in chunked(items, _SNAPSHOT_BATCH):
            rows = [{"run_id": run_id, **_snapshot_item(item)} for item in batch]
            connection.execute(insert(RunItem), rows)


def _csv_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _write_corpus(path: Path, items: list[RunPayloadItem]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(_CSV_HEADER)
        for item in items:
            writer.writerow(
                [
                    _csv_cell(item.repo_id),
                    _csv_cell(item.full_name),
                    _csv_cell(item.stargazers),
                    _csv_cell(item.pushed_at),
                    _csv_cell(item.archived),
                    _csv_cell(item.language),
                    _csv_cell(item.license_spdx),
                    _csv_cell(item.country_iso),
                    _csv_cell(item.geo_confidence),
                ]
            )


def _write_bundle(run: dict, payload: RunPayload, runs_root: str) -> str:
    directory = Path(runs_root) / run["filter_hash"] / str(run["id"])
    directory.mkdir(parents=True, exist_ok=True)
    bundle = {
        "filter": run["filter_spec"],
        "filter_hash": run["filter_hash"],
        "run_id": run["id"],
        "ran_at": datetime.now(UTC).isoformat(),
        "api_version": run["api_version"],
        "total_count": payload.total_count,
        "fetched": payload.fetched,
        "incomplete": payload.incomplete,
        "items": [_bundle_item(item) for item in payload.items],
        "field_stats": payload.field_stats,
        "timings": payload.timings,
    }
    with (directory / "bundle.json").open("w", encoding="utf-8") as handle:
        json.dump(bundle, handle, ensure_ascii=False, indent=2)
    _write_corpus(directory / "corpus.csv", payload.items)
    return f"{directory.as_posix()}/"


class ProgressReporter:
    """Throttled writer of the live progress columns on the runs row."""

    def __init__(self, engine: Engine, run_id: int, *, min_interval: float = 1.0) -> None:
        self._engine = engine
        self._run_id = run_id
        self._min_interval = min_interval
        self._last_write = 0.0
        self._last_phase = ""

    def update(
        self,
        phase: str,
        done: int,
        total: int | None,
        counters: Mapping[str, int],
        *,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        if not force and phase == self._last_phase and now - self._last_write < self._min_interval:
            return
        phase_changed = phase != self._last_phase
        self._last_write = now
        self._last_phase = phase
        values: dict[str, object] = {
            "progress_phase": phase,
            "progress_done": int(done),
            "progress_total": total,
            "progress_updated_at": func.now(),
        }
        if phase_changed:
            values["progress_started_at"] = func.now()
        for name in ("total_count", "fetched", "updated", "unchanged", "skipped"):
            value = counters.get(name)
            if isinstance(value, int):
                values[name] = value
        with self._engine.begin() as connection:
            connection.execute(update(Runs).where(Runs.id == self._run_id).values(**values))


def execute_run(
    engine: Engine,
    run_id: int,
    *,
    runner: Runner | None = None,
    runs_root: str = "runs",
    cancel: threading.Event | None = None,
) -> None:
    cancel = cancel if cancel is not None else threading.Event()
    reporter = ProgressReporter(engine, run_id)
    with engine.begin() as connection:
        run = connection.execute(select(Runs).where(Runs.id == run_id)).mappings().one_or_none()
        if run is None:
            raise KeyError(run_id)
        if cancel.is_set():
            connection.execute(
                update(Runs)
                .where(Runs.id == run_id)
                .values(
                    status="cancelled",
                    error=None,
                    finished_at=func.now(),
                    progress_phase=None,
                    progress_done=None,
                    progress_total=None,
                    progress_started_at=None,
                )
            )
            return
        connection.execute(
            update(Runs)
            .where(Runs.id == run_id)
            .values(
                status="running",
                started_at=func.now(),
                error=None,
                progress_phase="starting",
                progress_done=0,
                progress_total=None,
            )
        )
    cancel_token = cancellation.bind(cancel.is_set)
    progress_token = progress.bind(reporter.update)
    try:
        payload = (runner or _default_runner)(run_id, run["filter_spec"])
        cancellation.check()
        reporter.update("writing", 0, len(payload.items), {}, force=True)
        snapshot_started = time.perf_counter()
        _snapshot_items(engine, run_id, payload.items)
        payload.timings["snapshot"] = time.perf_counter() - snapshot_started
        bundle_dir = _write_bundle(run, payload, runs_root)
        with engine.begin() as connection:
            connection.execute(
                update(Runs)
                .where(Runs.id == run_id)
                .values(
                    status="partial" if payload.incomplete else "done",
                    total_count=payload.total_count,
                    fetched=payload.fetched if payload.fetched is not None else len(payload.items),
                    inserted=len(payload.items),
                    updated=payload.updated or 0,
                    unchanged=payload.unchanged or 0,
                    skipped=payload.skipped or 0,
                    incomplete_shards=1 if payload.incomplete else 0,
                    bundle_dir=bundle_dir,
                    finished_at=func.now(),
                    error=None,
                    progress_phase=None,
                    progress_done=None,
                    progress_total=None,
                    progress_started_at=None,
                )
            )
    except RunCancelled:
        with engine.begin() as connection:
            connection.execute(
                update(Runs)
                .where(Runs.id == run_id)
                .values(
                    status="cancelled",
                    error=None,
                    finished_at=func.now(),
                    progress_phase=None,
                    progress_done=None,
                    progress_total=None,
                    progress_started_at=None,
                )
            )
    except Exception as exc:
        with engine.begin() as connection:
            connection.execute(
                update(Runs)
                .where(Runs.id == run_id)
                .values(
                    status="failed",
                    error=_error_message(exc),
                    finished_at=func.now(),
                    progress_phase=None,
                    progress_done=None,
                    progress_total=None,
                    progress_started_at=None,
                )
            )
    finally:
        progress.reset(progress_token)
        cancellation.reset(cancel_token)


def recover_orphaned_runs(engine: Engine) -> int:
    with engine.begin() as connection:
        result = connection.execute(
            update(Runs)
            .where(Runs.status.in_(("queued", "running")))
            .values(
                status="failed",
                error="orphaned: process restarted",
                finished_at=func.now(),
                progress_phase=None,
                progress_done=None,
                progress_total=None,
                progress_started_at=None,
            )
        )
    return int(result.rowcount or 0)


def reset_run_artifacts(engine: Engine, run_id: int) -> None:
    with engine.begin() as connection:
        connection.execute(delete(RunItem).where(RunItem.run_id == run_id))
        if connection.scalar(text("SELECT to_regclass('detection_evidence')")) is not None:
            connection.execute(
                text("DELETE FROM detection_evidence WHERE run_id = :run_id"),
                {"run_id": run_id},
            )


class RunExecutor:
    def __init__(
        self,
        engine: Engine,
        *,
        runner: Runner | None = None,
        runs_root: str = "runs",
    ):
        self._engine = engine
        self._runner = runner
        self._runs_root = runs_root
        self._queue: queue.PriorityQueue[tuple[int, int, Callable[[], object], Future]] = (
            queue.PriorityQueue()
        )
        self._sequence = itertools.count()
        self._idle = threading.Event()
        self._idle.set()
        self._futures: dict[int, Future] = {}
        self._cancel_events: dict[int, threading.Event] = {}
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def submit(self, run_id: int, runner: Runner | None = None) -> Future:
        future: Future = Future()
        with self._lock:
            done = [key for key, value in self._futures.items() if value.done()]
            for key in done:
                del self._futures[key]
            self._futures[run_id] = future
            cancel = self._cancel_events.get(run_id)
            if cancel is None or not cancel.is_set():
                cancel = threading.Event()
            self._cancel_events[run_id] = cancel

        def job() -> None:
            execute_run(
                self._engine,
                run_id,
                runner=runner or self._runner,
                runs_root=self._runs_root,
                cancel=cancel,
            )

        self._enqueue(1, job, future)
        return future

    def request_cancel(self, run_id: int) -> bool:
        with self._lock:
            event = self._cancel_events.setdefault(run_id, threading.Event())
            event.set()
        return True

    def is_cancelled(self, run_id: int) -> bool:
        with self._lock:
            event = self._cancel_events.get(run_id)
        return event is not None and event.is_set()

    def submit_call(self, func: Callable[[], object]) -> Future:
        future: Future = Future()
        self._enqueue(0, func, future)
        return future

    def _enqueue(self, priority: int, job: Callable[[], object], future: Future) -> None:
        with self._lock:
            self._idle.clear()
            self._queue.put((priority, next(self._sequence), job, future))

    def wait_for(self, run_id: int, timeout: float | None = None) -> bool:
        with self._lock:
            future = self._futures.get(run_id)
        if future is None:
            return False
        try:
            future.result(timeout=timeout)
        except TimeoutError:
            return False
        except Exception:
            return True
        return True

    def wait(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout) and self._queue.unfinished_tasks == 0

    def _work(self) -> None:
        while True:
            _priority, _sequence, job, future = self._queue.get()
            try:
                result = job()
                if not future.done():
                    future.set_result(result)
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
            finally:
                self._queue.task_done()
                with self._lock:
                    if self._queue.unfinished_tasks == 0:
                        self._idle.set()
