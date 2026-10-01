from __future__ import annotations

import json
import os

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

from enrich.cloner import (
    MARKER_NAME,
    CloneEstimate,
    CloneMode,
    CloneProgress,
    CloneStats,
    _default_git_runner,
    clone_repos,
    estimate_clone,
    parse_mode,
)
from serve.executor import create_run

FILTER = {"gitcrawl_filter": 1, "q": "language:rust"}

MODE_FLAGS = {
    CloneMode.SHALLOW: ("--depth", "1"),
    CloneMode.FILE_ONLY: ("--depth", "1", "--no-checkout"),
    CloneMode.WINDOWED: ("--filter=blob:none", "--no-checkout"),
}


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(alembic_engine: Engine) -> Engine:
    with alembic_engine.begin() as connection:
        connection.execute(
            text("TRUNCATE TABLE run_items, runs, owners, repos RESTART IDENTITY CASCADE")
        )
        connection.execute(text("INSERT INTO owners (id, login, type) VALUES (1, 'octo', 'User')"))
    return alembic_engine


def seed_run(engine: Engine, specs: list[tuple[int, str, int | None, int]]) -> tuple[int, str]:
    with engine.begin() as connection:
        for repo_id, full_name, size_kb, _stars in specs:
            connection.execute(
                text(
                    "INSERT INTO repos (id, node_id, full_name, owner_id, name, visibility, "
                    "size_kb) VALUES (:id, :node, :full_name, 1, :name, 'public', :size_kb)"
                ),
                {
                    "id": repo_id,
                    "node": f"R_{repo_id}",
                    "full_name": full_name,
                    "name": full_name.split("/", 1)[1],
                    "size_kb": size_kb,
                },
            )
    run_id = create_run(engine, FILTER, api_version="v1")
    with engine.begin() as connection:
        for repo_id, full_name, _size_kb, stars in specs:
            connection.execute(
                text(
                    "INSERT INTO run_items (run_id, repo_id, full_name, stargazers) "
                    "VALUES (:run_id, :repo_id, :full_name, :stargazers)"
                ),
                {
                    "run_id": run_id,
                    "repo_id": repo_id,
                    "full_name": full_name,
                    "stargazers": stars,
                },
            )
    with engine.connect() as connection:
        filter_hash = connection.scalar(
            text("SELECT filter_hash FROM runs WHERE id = :id"), {"id": run_id}
        )
    return run_id, str(filter_hash)


def test_parse_mode_accepts_each_mode():
    assert parse_mode("shallow") is CloneMode.SHALLOW
    assert parse_mode("file_only") is CloneMode.FILE_ONLY
    assert parse_mode("windowed") is CloneMode.WINDOWED


def test_parse_mode_rejects_unknown_value():
    with pytest.raises(ValueError):
        parse_mode("deep")


@pytest.mark.parametrize(
    ("mode", "expected_mb"),
    [
        (CloneMode.SHALLOW, 3.0),
        (CloneMode.FILE_ONLY, 0.75),
        (CloneMode.WINDOWED, 1.5),
    ],
)
def test_estimate_applies_the_mode_factor(clean: Engine, mode: CloneMode, expected_mb: float):
    run_id, _ = seed_run(
        clean,
        [(1, "octo/one", 1024, 30), (2, "octo/two", 1024, 20), (3, "octo/three", 1024, 10)],
    )

    estimate = estimate_clone(clean, run_id, limit=3, mode=mode)

    assert estimate == CloneEstimate(repos=3, estimated_mb=pytest.approx(expected_mb), warnings=())


def test_estimate_selects_top_n_by_stars_then_repo_id(clean: Engine):
    run_id, _ = seed_run(
        clean,
        [
            (1, "octo/one", 1024, 50),
            (2, "octo/two", 4096, 40),
            (3, "octo/three", 8192, 40),
            (4, "octo/four", 16384, 10),
        ],
    )

    estimate = estimate_clone(clean, run_id, limit=2, mode=CloneMode.SHALLOW)

    assert estimate.repos == 2
    assert estimate.estimated_mb == pytest.approx((1024 + 4096) / 1024)


def test_estimate_orders_null_stars_and_repo_ids_last(clean: Engine):
    run_id, _ = seed_run(
        clean,
        [(1, "octo/one", 4096, 50), (2, "octo/null-stars", 1024, None)],
    )
    with clean.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO run_items (run_id, repo_id, full_name, stargazers) "
                "VALUES (:run, NULL, 'octo/detached', 50)"
            ),
            {"run": run_id},
        )

    estimate = estimate_clone(clean, run_id, limit=1, mode=CloneMode.SHALLOW)

    assert estimate.repos == 1
    assert estimate.estimated_mb == pytest.approx(4.0)


def test_estimate_caps_at_the_available_repos_and_treats_missing_size_as_zero(clean: Engine):
    run_id, _ = seed_run(clean, [(1, "octo/one", None, 10), (2, "octo/two", 1024, 5)])

    estimate = estimate_clone(clean, run_id, limit=9, mode=CloneMode.SHALLOW)

    assert estimate.repos == 2
    assert estimate.estimated_mb == pytest.approx(1.0)


def test_estimate_low_disk_warning_boundary(clean: Engine):
    run_id, _ = seed_run(clean, [(1, "octo/one", 2048, 10)])

    at_boundary = estimate_clone(
        clean,
        run_id,
        limit=1,
        mode=CloneMode.SHALLOW,
        disk_free_mb=2050.0,
        low_disk_threshold_mb=2048.0,
    )
    tight = estimate_clone(
        clean,
        run_id,
        limit=1,
        mode=CloneMode.SHALLOW,
        disk_free_mb=2049.0,
        low_disk_threshold_mb=2048.0,
    )

    assert at_boundary.estimated_mb == pytest.approx(2.0)
    assert at_boundary.warnings == ()
    assert len(tight.warnings) == 1
    assert "low disk" in tight.warnings[0]


def test_estimate_zero_limit_is_empty(clean: Engine):
    run_id, _ = seed_run(clean, [(1, "octo/one", 1024, 10)])

    estimate = estimate_clone(clean, run_id, limit=0, mode=CloneMode.SHALLOW)

    assert estimate == CloneEstimate(repos=0, estimated_mb=0.0, warnings=())


def test_full_run_clone_estimate_uses_one_aggregate(clean: Engine, tmp_path):
    from sqlalchemy import event

    from serve.runs import clone_estimate_for_run

    run_id, _ = seed_run(clean, [(1, "octo/one", 1024, 1), (2, "octo/two", 2048, 2)])
    statements: list[str] = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(clean, "before_cursor_execute", listener)
    try:
        estimate = clone_estimate_for_run(
            clean, run_id, limit=None, mode=CloneMode.SHALLOW, dest_root=str(tmp_path)
        )
    finally:
        event.remove(clean, "before_cursor_execute", listener)
    assert estimate.repos == 2
    assert estimate.estimated_mb == pytest.approx(3.0)
    selects = [s for s in statements if "FROM run_items" in s]
    assert len(selects) == 1  # one aggregate, no per-item row fetch


def test_full_run_clone_estimate_matches_the_full_scan_path(clean: Engine, tmp_path):
    from serve.runs import clone_estimate_for_run

    run_id, _ = seed_run(clean, [(1, "octo/one", 1024, 10), (2, "octo/two", None, 5)])
    with clean.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO run_items (run_id, repo_id, full_name, stargazers) "
                "VALUES (:run, NULL, 'octo/detached', 1)"
            ),
            {"run": run_id},
        )

    aggregate = clone_estimate_for_run(
        clean, run_id, limit=None, mode=CloneMode.SHALLOW, dest_root=str(tmp_path)
    )
    full_scan = estimate_clone(clean, run_id, limit=3, mode=CloneMode.SHALLOW)

    assert aggregate.repos == full_scan.repos == 3
    assert aggregate.estimated_mb == pytest.approx(full_scan.estimated_mb)
    assert aggregate.warnings == full_scan.warnings


def test_full_run_clone_estimate_missing_run_raises_keyerror(clean: Engine, tmp_path):
    from serve.runs import clone_estimate_for_run

    with pytest.raises(KeyError):
        clone_estimate_for_run(
            clean, 424242, limit=None, mode=CloneMode.SHALLOW, dest_root=str(tmp_path)
        )


@pytest.mark.parametrize("mode", list(MODE_FLAGS))
def test_clone_repos_runs_the_mode_specific_argv(clean: Engine, tmp_path, mode: CloneMode):
    run_id, filter_hash = seed_run(clean, [(1, "octo/hello", 1024, 10)])
    calls: list[tuple[list[str], str]] = []

    clone_repos(
        clean,
        run_id,
        limit=1,
        mode=mode,
        dest_root=str(tmp_path / "clones"),
        git_runner=lambda argv, cwd: calls.append((list(argv), cwd)),
    )

    destination = tmp_path / "clones" / filter_hash / str(run_id) / "octo__hello"
    assert calls == [
        (
            [
                "git",
                "clone",
                *MODE_FLAGS[mode],
                "https://github.com/octo/hello.git",
                str(destination),
            ],
            str(destination.parent),
        )
    ]


def test_clone_repos_writes_marker_at_destination_and_reports_stats(clean: Engine, tmp_path):
    run_id, filter_hash = seed_run(clean, [(1, "octo/hello", 1024, 10)])
    progress = CloneProgress(status="running", total=1, completed=0, failed=0)

    stats = clone_repos(
        clean,
        run_id,
        limit=1,
        mode=CloneMode.SHALLOW,
        dest_root=str(tmp_path / "clones"),
        git_runner=lambda argv, cwd: None,
        progress=progress,
    )

    destination = tmp_path / "clones" / filter_hash / str(run_id) / "octo__hello"
    assert destination.is_dir()
    marker = destination / MARKER_NAME
    assert marker.is_file()
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "full_name": "octo/hello",
        "mode": "shallow",
    }
    assert stats == CloneStats(
        requested=1,
        completed=1,
        skipped=0,
        failed=0,
        dest_root=str(tmp_path / "clones"),
    )
    assert progress.completed == 1
    assert progress.failed == 0
    assert progress.status == "done"
    assert progress.current is None


def test_clone_repos_skips_repos_with_a_done_marker(clean: Engine, tmp_path):
    run_id, filter_hash = seed_run(clean, [(1, "octo/hello", 1024, 10)])
    destination = tmp_path / "clones" / filter_hash / str(run_id) / "octo__hello"
    destination.mkdir(parents=True)
    (destination / MARKER_NAME).write_text("{}", encoding="utf-8")
    calls: list[list[str]] = []
    progress = CloneProgress(status="running", total=1, completed=0, failed=0)

    stats = clone_repos(
        clean,
        run_id,
        limit=1,
        mode=CloneMode.SHALLOW,
        dest_root=str(tmp_path / "clones"),
        git_runner=lambda argv, cwd: calls.append(list(argv)),
        progress=progress,
    )

    assert calls == []
    assert stats.skipped == 1
    assert stats.completed == 0
    assert progress.completed == 1
    assert progress.status == "done"


def test_clone_repos_continues_after_failure_and_records_error(clean: Engine, tmp_path):
    run_id, filter_hash = seed_run(clean, [(1, "octo/one", 1024, 20), (2, "octo/two", 1024, 10)])
    progress = CloneProgress(status="running", total=2, completed=0, failed=0)

    def runner(argv: list[str], cwd: str) -> None:
        if argv[-2].startswith("https://github.com/octo/one"):
            destination = tmp_path / "partial"
            destination.mkdir(parents=True, exist_ok=True)
            raise RuntimeError("boom")

    stats = clone_repos(
        clean,
        run_id,
        limit=2,
        mode=CloneMode.SHALLOW,
        dest_root=str(tmp_path / "clones"),
        git_runner=runner,
        progress=progress,
    )

    failed = tmp_path / "clones" / filter_hash / str(run_id) / "octo__one"
    succeeded = tmp_path / "clones" / filter_hash / str(run_id) / "octo__two"
    assert not failed.exists()
    assert (succeeded / MARKER_NAME).is_file()
    assert stats.failed == 1
    assert stats.completed == 1
    assert stats.requested == 2
    assert len(progress.errors) == 1
    assert "octo/one" in progress.errors[0]
    assert "RuntimeError" in progress.errors[0]
    assert progress.failed == 1
    assert progress.completed == 1
    assert progress.status == "done"


def test_clone_repos_zero_limit_is_a_noop(clean: Engine, tmp_path):
    run_id, _ = seed_run(clean, [(1, "octo/hello", 1024, 10)])
    calls: list[list[str]] = []
    progress = CloneProgress(status="running", total=5, completed=0, failed=0)

    stats = clone_repos(
        clean,
        run_id,
        limit=0,
        mode=CloneMode.SHALLOW,
        dest_root=str(tmp_path / "clones"),
        git_runner=lambda argv, cwd: calls.append(list(argv)),
        progress=progress,
    )

    assert calls == []
    assert stats == CloneStats(
        requested=0,
        completed=0,
        skipped=0,
        failed=0,
        dest_root=str(tmp_path / "clones"),
    )
    assert progress == CloneProgress(status="done", total=0, completed=0, failed=0)
    assert not (tmp_path / "clones").exists()


def test_clone_workers_run_in_parallel(clean: Engine, tmp_path):
    import threading
    import time as time_module

    specs = [(repo_id, f"octo/r{repo_id}", 1, repo_id) for repo_id in range(1, 5)]
    run_id, _ = seed_run(clean, specs)
    active = 0
    peak = 0
    lock = threading.Lock()

    def fake_runner(argv, cwd):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time_module.sleep(0.05)
        with lock:
            active -= 1

    clone_repos(
        clean,
        run_id,
        limit=4,
        mode=CloneMode.SHALLOW,
        dest_root=str(tmp_path),
        git_runner=fake_runner,
        workers=4,
    )
    assert peak >= 2


def test_clone_errors_are_capped(clean: Engine, tmp_path):
    specs = [(repo_id, f"octo/r{repo_id}", 1, repo_id) for repo_id in range(1, 21)]
    run_id, _ = seed_run(clean, specs)
    progress = CloneProgress(status="running", total=0, completed=0, failed=0)

    def failing(argv, cwd):
        raise RuntimeError("boom")

    stats = clone_repos(
        clean,
        run_id,
        limit=20,
        mode=CloneMode.SHALLOW,
        dest_root=str(tmp_path),
        git_runner=failing,
        errors_cap=5,
        progress=progress,
    )
    assert stats.failed == 20
    assert progress.error_count == 20
    assert len(progress.errors) == 5


def test_default_git_runner_enforces_timeout():
    import subprocess

    with pytest.raises(subprocess.TimeoutExpired):
        _default_git_runner(["python", "-c", "import time; time.sleep(5)"], ".", timeout=0.05)


def test_load_rows_batches(clean: Engine):
    from serve.runner import _load_rows

    seed_run(
        clean,
        [(1, "octo/one", 10, 1), (2, "octo/two", 10, 2), (3, "octo/three", 10, 3)],
    )
    rows = _load_rows(clean, [1, 2, 3], batch_size=2)
    assert set(rows) == {1, 2, 3}


def test_persisted_progress_throttles_and_replaces_atomically(tmp_path):
    from serve.runs import _PersistedProgress

    now = {"value": 0.0}
    progress = _PersistedProgress(
        tmp_path / "progress.json",
        status="running",
        total=10,
        completed=0,
        failed=0,
        now=lambda: now["value"],
    )
    progress.completed = 1
    progress.emit()
    first = (tmp_path / "progress.json").read_text(encoding="utf-8")
    progress.completed = 2
    progress.emit()  # throttled
    assert (tmp_path / "progress.json").read_text(encoding="utf-8") == first
    progress.emit(force=True)
    assert '"completed": 2' in (tmp_path / "progress.json").read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.tmp"))


def test_persisted_progress_retries_transient_replace_failures(tmp_path, monkeypatch):
    from serve.runs import _PersistedProgress

    real_replace = os.replace
    attempts = {"count": 0}

    def flaky_replace(source, destination):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise PermissionError("locked by a reader")
        real_replace(source, destination)

    monkeypatch.setattr("serve.runs.os.replace", flaky_replace)
    progress = _PersistedProgress(
        tmp_path / "progress.json", status="running", total=1, completed=0, failed=0
    )

    progress.emit(force=True)

    data = json.loads((tmp_path / "progress.json").read_text(encoding="utf-8"))
    assert attempts["count"] == 2
    assert data["status"] == "running"
    assert not list(tmp_path.glob("*.tmp"))
