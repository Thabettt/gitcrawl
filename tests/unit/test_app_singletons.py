from __future__ import annotations

import threading
import time

from serve.app import _LazyLoaders, create_app


def test_lazy_loader_constructs_once_under_concurrency():
    loaders = _LazyLoaders()
    created: list[object] = []
    created_lock = threading.Lock()

    def factory() -> object:
        time.sleep(0.05)
        value = object()
        with created_lock:
            created.append(value)
        return value

    results: list[object] = []
    results_lock = threading.Lock()

    def worker() -> None:
        value = loaders.get("engine", factory)
        with results_lock:
            results.append(value)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert len(created) == 1
    assert results == [created[0]] * 8


def test_lazy_loader_reuses_a_seeded_value():
    loaders = _LazyLoaders()
    sentinel = object()
    loaders.seed("engine", sentinel)

    def factory() -> object:  # pragma: no cover - seeded values must not rebuild
        raise AssertionError("seeded values must not be rebuilt")

    assert loaders.get("engine", factory) is sentinel


def test_create_app_lazy_factories_are_single_flight(monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "serve.app.register_pages", lambda app, **kwargs: captured.update(kwargs)
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://gitcrawl@localhost/gitcrawl")
    engine_calls: list[str] = []
    monkeypatch.setattr(
        "serve.app.create_engine", lambda url: engine_calls.append(url) or object()
    )
    executor_calls: list[object] = []

    class StubExecutor:
        def __init__(self, engine, *, runs_root="runs"):
            executor_calls.append(engine)

    monkeypatch.setattr("serve.app.RunExecutor", StubExecutor)
    runner_calls: list[object] = []
    create_app(
        engine=None,
        runner_factory=lambda engine: runner_calls.append(engine)
        or (lambda _run_id, _spec: None),
    )

    engine_factory = captured["engine_factory"]
    runner_factory = captured["runner_factory"]
    executor_factory = captured["executor_factory"]

    engines = [engine_factory() for _ in range(2)]
    assert engines[0] is engines[1]
    assert engine_calls == ["postgresql://gitcrawl@localhost/gitcrawl"]

    runner = runner_factory()
    assert runner_factory() is runner
    assert len(runner_calls) == 1

    executor = executor_factory()
    assert executor_factory() is executor
    assert len(executor_calls) == 1
