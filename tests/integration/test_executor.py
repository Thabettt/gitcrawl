from __future__ import annotations

import csv
import hashlib
import json
import threading
import time
from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

from serve import executor as executor_module
from serve.executor import (
    RunExecutor,
    RunPayload,
    RunPayloadItem,
    create_run,
    execute_run,
    run_status,
)

FILTER = {"q": "stars:>10", "sort": "stars", "order": "desc"}
FILTER_HASH = hashlib.sha256(
    json.dumps(FILTER, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()

CSV_HEADER = [
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

RUN_STATUS_KEYS = {
    "id",
    "status",
    "filter_hash",
    "total_count",
    "fetched",
    "inserted",
    "updated",
    "unchanged",
    "skipped",
    "incomplete_shards",
    "error",
    "created_at",
    "started_at",
    "finished_at",
    "bundle_dir",
}


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def db(clean_db):
    return clean_db(
        owners=[{"id": 1, "login": "octo", "type": "User"}],
        repos=[
            {
                "id": 1,
                "node_id": "n1",
                "full_name": "octo/hello",
                "owner_id": 1,
                "name": "hello",
                "visibility": "public",
            },
            {
                "id": 2,
                "node_id": "n2",
                "full_name": "octo/world",
                "owner_id": 1,
                "name": "world",
                "visibility": "public",
            },
            {
                "id": 3,
                "node_id": "n3",
                "full_name": "octo/extra",
                "owner_id": 1,
                "name": "extra",
                "visibility": "public",
            },
        ],
    )


def _item(repo_id: int = 1, full_name: str = "octo/hello", **overrides) -> RunPayloadItem:
    values: dict[str, object] = {
        "repo_id": repo_id,
        "full_name": full_name,
        "stargazers": 10,
        "pushed_at": "2026-09-30T12:00:00+00:00",
        "archived": False,
        "language": "Python",
        "license_spdx": "MIT",
        "country_iso": "DE",
        "geo_confidence": "name",
        "virtuals": {"has_dockerfile": True},
    }
    values.update(overrides)
    return RunPayloadItem(**values)


def _expected_snapshot() -> dict[str, object]:
    return {
        "repo_id": 1,
        "full_name": "octo/hello",
        "stargazers": 10,
        "pushed_at": "2026-09-30T12:00:00+00:00",
        "archived": False,
        "language": "Python",
        "license_spdx": "MIT",
        "country_iso": "DE",
        "geo_confidence": "name",
        "virtuals": {"has_dockerfile": True},
    }


def test_create_run_hashes_canonical_filter_spec_and_starts_queued(db: Engine):
    first = create_run(db, FILTER, api_version="v1")
    second = create_run(db, {"order": "desc", "sort": "stars", "q": "stars:>10"}, api_version="v1")
    assert first != second
    status = run_status(db, first)
    assert set(status) == RUN_STATUS_KEYS
    assert status["id"] == first
    assert status["status"] == "queued"
    assert status["filter_hash"] == FILTER_HASH
    assert status["started_at"] is None
    assert status["finished_at"] is None
    assert status["bundle_dir"] is None
    assert status["error"] is None
    assert status["total_count"] is None
    assert status["fetched"] == 0
    assert status["inserted"] == 0
    assert run_status(db, second)["filter_hash"] == FILTER_HASH
    assert run_status(db, second)["status"] == "queued"


def test_run_status_missing_run_raises_keyerror(db: Engine):
    with pytest.raises(KeyError):
        run_status(db, 999999)


def test_execute_run_done_snapshots_rows_and_writes_bundle(db: Engine, tmp_path):
    run_id = create_run(db, FILTER, api_version="v1")
    raw = {"id": 2, "full_name": "octo/world", "stargazers": 99}
    payload = RunPayload(
        total_count=57,
        fetched=3,
        items=[
            _item(),
            RunPayloadItem(repo_id=2, full_name="octo/world", raw=raw),
            _item(
                repo_id=3,
                full_name="octo/extra",
                stargazers=None,
                pushed_at=None,
                archived=None,
                language=None,
                license_spdx=None,
                country_iso=None,
                geo_confidence=None,
                virtuals={},
            ),
        ],
    )
    execute_run(db, run_id, runner=lambda rid, spec: payload, runs_root=str(tmp_path))
    status = run_status(db, run_id)
    assert status["status"] == "done"
    assert status["total_count"] == 57
    assert status["fetched"] == 3
    assert status["inserted"] == 3
    assert status["updated"] == 0
    assert status["unchanged"] == 0
    assert status["skipped"] == 0
    assert status["incomplete_shards"] == 0
    assert status["error"] is None
    assert status["started_at"] is not None
    assert status["finished_at"] is not None
    assert status["created_at"] <= status["started_at"] <= status["finished_at"]
    assert status["bundle_dir"] == f"{tmp_path.as_posix()}/{FILTER_HASH}/{run_id}/"

    with db.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT run_id, repo_id, full_name, stargazers, pushed_at, archived, "
                    "language, license_spdx, country_iso, geo_confidence, virtuals "
                    "FROM run_items ORDER BY repo_id"
                )
            )
            .mappings()
            .all()
        )
    assert [dict(row) for row in rows] == [
        {
            "run_id": run_id,
            "repo_id": 1,
            "full_name": "octo/hello",
            "stargazers": 10,
            "pushed_at": datetime(2026, 9, 30, 12, 0, tzinfo=UTC),
            "archived": False,
            "language": "Python",
            "license_spdx": "MIT",
            "country_iso": "DE",
            "geo_confidence": "name",
            "virtuals": {"has_dockerfile": True},
        },
        {
            "run_id": run_id,
            "repo_id": 2,
            "full_name": "octo/world",
            "stargazers": None,
            "pushed_at": None,
            "archived": None,
            "language": None,
            "license_spdx": None,
            "country_iso": None,
            "geo_confidence": None,
            "virtuals": {},
        },
        {
            "run_id": run_id,
            "repo_id": 3,
            "full_name": "octo/extra",
            "stargazers": None,
            "pushed_at": None,
            "archived": None,
            "language": None,
            "license_spdx": None,
            "country_iso": None,
            "geo_confidence": None,
            "virtuals": {},
        },
    ]

    directory = tmp_path / FILTER_HASH / str(run_id)
    bundle = json.loads((directory / "bundle.json").read_text(encoding="utf-8"))
    assert set(bundle) == {
        "filter",
        "filter_hash",
        "run_id",
        "ran_at",
        "api_version",
        "total_count",
        "fetched",
        "incomplete",
        "items",
        "field_stats",
    }
    assert bundle["field_stats"] == {}
    assert bundle["filter"] == FILTER
    assert bundle["filter_hash"] == FILTER_HASH
    assert bundle["run_id"] == run_id
    assert bundle["api_version"] == "v1"
    assert bundle["total_count"] == 57
    assert bundle["fetched"] == 3
    assert bundle["incomplete"] is False
    assert bundle["items"] == [
        _expected_snapshot(),
        raw,
        {
            "repo_id": 3,
            "full_name": "octo/extra",
            "stargazers": None,
            "pushed_at": None,
            "archived": None,
            "language": None,
            "license_spdx": None,
            "country_iso": None,
            "geo_confidence": None,
            "virtuals": {},
        },
    ]
    assert datetime.fromisoformat(bundle["ran_at"]).tzinfo is not None

    with (directory / "corpus.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows == [
        CSV_HEADER,
        [
            "1",
            "octo/hello",
            "10",
            "2026-09-30T12:00:00+00:00",
            "false",
            "Python",
            "MIT",
            "DE",
            "name",
        ],
        ["2", "octo/world", "", "", "", "", "", "", ""],
        ["3", "octo/extra", "", "", "", "", "", "", ""],
    ]


def test_execute_run_persists_field_stats_in_the_bundle(db: Engine, tmp_path):
    run_id = create_run(db, FILTER, api_version="v1")
    field_stats = {
        "per_field_sources": {"owner_country": 2, "has_dockerfile": 1},
        "calls_spent": {"owner_country": 2, "has_dockerfile": 1},
        "requeues": 1,
    }
    payload = RunPayload(total_count=1, fetched=1, items=[_item()], field_stats=field_stats)

    execute_run(db, run_id, runner=lambda rid, spec: payload, runs_root=str(tmp_path))

    bundle = json.loads((tmp_path / FILTER_HASH / str(run_id) / "bundle.json").read_text())
    assert bundle["field_stats"] == field_stats


def test_execute_run_incomplete_marks_partial(db: Engine, tmp_path):
    run_id = create_run(db, FILTER, api_version="v1")
    payload = RunPayload(total_count=100, items=[_item()], incomplete=True, fetched=1)
    execute_run(db, run_id, runner=lambda rid, spec: payload, runs_root=str(tmp_path))
    status = run_status(db, run_id)
    assert status["status"] == "partial"
    assert status["incomplete_shards"] == 1
    bundle = json.loads((tmp_path / FILTER_HASH / str(run_id) / "bundle.json").read_text())
    assert bundle["incomplete"] is True
    assert bundle["fetched"] == 1


def test_execute_run_failure_is_sanitized_and_does_not_propagate(db: Engine, tmp_path):
    run_id = create_run(db, FILTER, api_version="v1")
    body = "upstream body " * 100

    def failing(rid: int, spec: dict) -> RunPayload:
        raise RuntimeError(body)

    assert execute_run(db, run_id, runner=failing, runs_root=str(tmp_path)) is None
    status = run_status(db, run_id)
    assert status["status"] == "failed"
    assert status["error"] == ("RuntimeError: " + body)[:300]
    assert len(status["error"]) == 300
    assert status["finished_at"] is not None
    assert status["started_at"] is not None
    with db.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM run_items")).scalar() == 0

    second = create_run(db, FILTER, api_version="v1")
    execute_run(
        db, second, runner=lambda rid, spec: RunPayload(1, [_item()]), runs_root=str(tmp_path)
    )
    assert run_status(db, second)["status"] == "done"


def test_execute_run_without_runner_marks_failed(db: Engine, tmp_path):
    run_id = create_run(db, FILTER, api_version="v1")
    execute_run(db, run_id, runs_root=str(tmp_path))
    status = run_status(db, run_id)
    assert status["status"] == "failed"
    assert status["error"] == "RuntimeError: no runner configured"
    assert status["finished_at"] is not None


def test_execute_run_missing_run_raises_keyerror(db: Engine, tmp_path):
    with pytest.raises(KeyError):
        execute_run(db, 999999, runs_root=str(tmp_path))


def test_execute_run_replaces_previous_snapshot(db: Engine, tmp_path):
    run_id = create_run(db, FILTER, api_version="v1")
    execute_run(
        db,
        run_id,
        runner=lambda rid, spec: RunPayload(2, [_item(), _item(repo_id=2, full_name="octo/world")]),
        runs_root=str(tmp_path),
    )
    execute_run(
        db,
        run_id,
        runner=lambda rid, spec: RunPayload(1, [_item(repo_id=3, full_name="octo/extra")]),
        runs_root=str(tmp_path),
    )
    assert run_status(db, run_id)["inserted"] == 1
    with db.connect() as connection:
        rows = connection.execute(text("SELECT repo_id FROM run_items ORDER BY repo_id")).scalars()
    assert list(rows) == [3]


def test_execute_run_empty_items_writes_header_only_csv(db: Engine, tmp_path):
    run_id = create_run(db, FILTER, api_version="v1")
    execute_run(db, run_id, runner=lambda rid, spec: RunPayload(0, []), runs_root=str(tmp_path))
    status = run_status(db, run_id)
    assert status["status"] == "done"
    assert status["total_count"] == 0
    assert status["fetched"] == 0
    assert status["inserted"] == 0
    directory = tmp_path / FILTER_HASH / str(run_id)
    with (directory / "corpus.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows == [CSV_HEADER]
    bundle = json.loads((directory / "bundle.json").read_text(encoding="utf-8"))
    assert bundle["items"] == []
    assert bundle["fetched"] is None


def test_execute_run_persists_runner_upsert_counters(db: Engine, tmp_path):
    run_id = create_run(db, FILTER, api_version="v1")
    payload = RunPayload(
        total_count=3,
        fetched=3,
        items=[_item()],
        updated=2,
        unchanged=3,
        skipped=4,
    )
    execute_run(db, run_id, runner=lambda rid, spec: payload, runs_root=str(tmp_path))
    status = run_status(db, run_id)
    assert status["inserted"] == 1
    assert status["updated"] == 2
    assert status["unchanged"] == 3
    assert status["skipped"] == 4


def test_run_executor_submit_returns_a_per_run_completion_handle(db: Engine, tmp_path):
    run_id = create_run(db, FILTER, api_version="v1")
    executor = RunExecutor(
        db, runner=lambda rid, spec: RunPayload(1, [_item()]), runs_root=str(tmp_path)
    )

    future = executor.submit(run_id)
    future.result(timeout=10)
    assert run_status(db, run_id)["status"] == "done"
    assert executor.wait_for(run_id, timeout=10) is True
    assert executor.wait_for(999999, timeout=0) is False


def test_run_executor_submit_accepts_a_per_run_runner(db: Engine, tmp_path):
    run_id = create_run(db, FILTER, api_version="v1")
    executor = RunExecutor(
        db,
        runner=lambda rid, spec: RunPayload(1, [_item()]),
        runs_root=str(tmp_path),
    )

    executor.submit(
        run_id, runner=lambda rid, spec: RunPayload(1, [_item(repo_id=2, full_name="octo/world")])
    ).result(timeout=10)

    with db.connect() as connection:
        rows = connection.execute(text("SELECT repo_id FROM run_items ORDER BY repo_id")).scalars()
    assert list(rows) == [2]


def test_run_executor_submit_call_runs_a_callable_on_the_worker(db: Engine, tmp_path):
    executor = RunExecutor(db, runs_root=str(tmp_path))

    assert executor.submit_call(lambda: "payload").result(timeout=10) == "payload"

    with pytest.raises(RuntimeError, match="boom"):
        executor.submit_call(lambda: (_ for _ in ()).throw(RuntimeError("boom"))).result(timeout=10)


def test_interactive_calls_jump_ahead_of_bulk_runs(db: Engine, tmp_path):
    order: list[str] = []
    release = threading.Event()

    def slow(run_id: int, spec: dict) -> RunPayload:
        order.append(f"bulk-{run_id}")
        release.wait(timeout=2)
        return RunPayload(1, [_item()])

    first = create_run(db, FILTER, api_version="v1")
    second = create_run(db, FILTER, api_version="v1")
    executor = RunExecutor(db, runner=slow, runs_root=str(tmp_path))
    executor.submit(first)
    time.sleep(0.05)
    bulk = executor.submit(second)
    interactive = executor.submit_call(lambda: order.append("interactive") or "done")
    release.set()
    assert interactive.result(timeout=2) == "done"
    bulk.result(timeout=2)
    assert order.index("interactive") < order.index(f"bulk-{second}")


def test_run_executor_prunes_completed_futures(db: Engine, tmp_path):
    executor = RunExecutor(
        db, runner=lambda rid, spec: RunPayload(1, [_item()]), runs_root=str(tmp_path)
    )
    first = create_run(db, FILTER, api_version="v1")
    executor.submit(first).result(timeout=10)
    assert first in executor._futures

    second = create_run(db, FILTER, api_version="v1")
    executor.submit(second).result(timeout=10)
    assert first not in executor._futures


def test_run_executor_fifo_and_single_worker(db: Engine, tmp_path):
    events: list[tuple[str, int]] = []
    lock = threading.Lock()

    def runner(run_id: int, spec: dict) -> RunPayload:
        with lock:
            events.append(("start", run_id))
        time.sleep(0.05)
        with lock:
            events.append(("end", run_id))
        return RunPayload(1, [_item()])

    first = create_run(db, FILTER, api_version="v1")
    second = create_run(db, FILTER, api_version="v1")
    executor = RunExecutor(db, runner=runner, runs_root=str(tmp_path))
    executor.submit(first)
    executor.submit(second)
    assert executor.wait(10) is True
    assert events == [("start", first), ("end", first), ("start", second), ("end", second)]
    assert run_status(db, first)["status"] == "done"
    assert run_status(db, second)["status"] == "done"


def test_run_executor_survives_failed_run(db: Engine, tmp_path):
    def runner(run_id: int, spec: dict) -> RunPayload:
        if run_id == failing:
            raise RuntimeError("boom")
        return RunPayload(1, [_item()])

    failing = create_run(db, FILTER, api_version="v1")
    healthy = create_run(db, FILTER, api_version="v1")
    executor = RunExecutor(db, runner=runner, runs_root=str(tmp_path))
    executor.submit(failing)
    executor.submit(healthy)
    assert executor.wait(10) is True
    assert run_status(db, failing)["status"] == "failed"
    assert run_status(db, healthy)["status"] == "done"


def test_run_executor_survives_unknown_run_id(db: Engine, tmp_path):
    healthy = create_run(db, FILTER, api_version="v1")
    executor = RunExecutor(
        db,
        runner=lambda rid, spec: RunPayload(1, [_item()]),
        runs_root=str(tmp_path),
    )
    executor.submit(999999)
    executor.submit(healthy)
    assert executor.wait(10) is True
    assert run_status(db, healthy)["status"] == "done"


def test_run_executor_wait_timeout_and_idle(db: Engine, tmp_path):
    started = threading.Event()
    release = threading.Event()

    def runner(run_id: int, spec: dict) -> RunPayload:
        started.set()
        release.wait(10)
        return RunPayload(1, [_item()])

    run_id = create_run(db, FILTER, api_version="v1")
    executor = RunExecutor(db, runner=runner, runs_root=str(tmp_path))
    assert executor.wait(0) is True
    executor.submit(run_id)
    assert started.wait(10) is True
    assert executor.wait(0.05) is False
    release.set()
    assert executor.wait(10) is True
    assert run_status(db, run_id)["status"] == "done"


def test_run_executor_enqueue_keeps_idle_cleared_for_outstanding_job(
    db: Engine, tmp_path, monkeypatch
):
    started = threading.Event()
    release_first = threading.Event()

    def runner(run_id: int, spec: dict) -> RunPayload:
        started.set()
        release_first.wait(10)
        return RunPayload(1, [_item()])

    run_id = create_run(db, FILTER, api_version="v1")
    executor = RunExecutor(db, runner=runner, runs_root=str(tmp_path))
    executor.submit(run_id)
    assert started.wait(10) is True

    real_put = executor._queue.put
    real_task_done = executor._queue.task_done
    put_entered = threading.Event()
    allow_put = threading.Event()
    task_done_finished = threading.Event()

    def gated_put(item):
        put_entered.set()
        assert allow_put.wait(10) is True
        real_put(item)

    def gated_task_done():
        real_task_done()
        task_done_finished.set()

    monkeypatch.setattr(executor._queue, "put", gated_put)
    monkeypatch.setattr(executor._queue, "task_done", gated_task_done)

    pending: list = []

    def enqueue_interactive() -> None:
        pending.append(executor.submit_call(lambda: "interactive"))

    submitter = threading.Thread(target=enqueue_interactive)
    submitter.start()
    assert put_entered.wait(10) is True

    release_first.set()
    assert task_done_finished.wait(10) is True
    try:
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline and not executor._idle.is_set():
            time.sleep(0.005)
        assert executor._idle.is_set() is False
    finally:
        allow_put.set()
    submitter.join(10)

    assert len(pending) == 1
    assert pending[0].result(timeout=10) == "interactive"
    assert executor.wait(10) is True


def test_run_executor_concurrent_submits_keep_single_worker(db: Engine, tmp_path, monkeypatch):
    events: list[tuple[str, int]] = []
    lock = threading.Lock()

    def runner(run_id: int, spec: dict) -> RunPayload:
        with lock:
            events.append(("start", run_id))
        time.sleep(0.05)
        with lock:
            events.append(("end", run_id))
        return RunPayload(1, [_item()])

    executor = RunExecutor(db, runner=runner, runs_root=str(tmp_path))
    run_ids = [create_run(db, FILTER, api_version="v1") for _ in range(4)]
    real_thread = threading.Thread
    gate = threading.Barrier(len(run_ids))

    class GatedThread(real_thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            try:
                gate.wait(1)
            except threading.BrokenBarrierError:
                pass

    monkeypatch.setattr(executor_module.threading, "Thread", GatedThread)
    submitters = [real_thread(target=executor.submit, args=(run_id,)) for run_id in run_ids]
    for thread in submitters:
        thread.start()
    for thread in submitters:
        thread.join(10)
    assert executor.wait(30) is True

    order = [run_id for _, run_id in events]
    assert sorted(order[::2]) == sorted(run_ids)
    assert order[::2] == order[1::2]
    assert all(kind == "start" for kind, _ in events[::2])
    assert all(kind == "end" for kind, _ in events[1::2])
    for run_id in run_ids:
        assert run_status(db, run_id)["status"] == "done"
