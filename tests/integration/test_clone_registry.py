from __future__ import annotations

import threading

import pytest
from alembic import command
from sqlalchemy.engine import Engine

from enrich.cloner import CloneMode
from lib.gh_client import API_VERSION
from serve.executor import create_run
from serve.runs import CloneRegistry, progress_payload, read_clone_progress, start_clone

FILTER = {"gitcrawl_filter": 1, "q": "language:rust"}


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(clean_db):
    return clean_db()


def test_concurrent_starts_return_the_same_progress_object(clean: Engine, tmp_path, monkeypatch):
    run_id = create_run(clean, FILTER, api_version=API_VERSION)
    registry = CloneRegistry()
    release = threading.Event()
    calls: list[int] = []

    def fake_worker(
        engine, run_id, limit, mode, dest_root, git_runner, progress, registry, timeout, event
    ):
        calls.append(run_id)
        assert release.wait(10)

    monkeypatch.setattr("serve.runs._clone_worker", fake_worker)
    barrier = threading.Barrier(2)
    results: list[object] = []
    results_lock = threading.Lock()

    def start() -> None:
        barrier.wait(5)
        progress = start_clone(
            clean,
            run_id,
            limit=2,
            mode=CloneMode.SHALLOW,
            registry=registry,
            runs_root=str(tmp_path),
        )
        with results_lock:
            results.append(progress)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    release.set()

    assert len(results) == 2
    assert results[0] is results[1]
    assert calls == [run_id]


def test_evicted_terminal_entry_falls_back_to_its_identical_progress_file(clean, tmp_path):
    first = create_run(clean, FILTER, api_version=API_VERSION)
    second = create_run(clean, FILTER, api_version=API_VERSION)
    registry = CloneRegistry(max_entries=1)

    progress = start_clone(
        clean,
        first,
        limit=0,
        mode=CloneMode.SHALLOW,
        registry=registry,
        runs_root=str(tmp_path),
    )
    assert progress.status == "done"

    start_clone(
        clean,
        second,
        limit=0,
        mode=CloneMode.SHALLOW,
        registry=registry,
        runs_root=str(tmp_path),
    )

    assert registry.get(first) is None
    fallback = read_clone_progress(clean, first, registry=registry, runs_root=str(tmp_path))
    assert progress_payload(fallback) == progress_payload(progress)
