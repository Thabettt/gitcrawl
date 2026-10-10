from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

from lib.audit import AuditRecord, SloSnapshot, query_hash, record_audit, slo_snapshot

START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(clean_db):
    return clean_db()


def _record(
    index: int,
    *,
    status: int = 200,
    rl_resource: str | None = "graphql",
    rl_remaining: int | None = 30,
    incomplete_results: bool | None = False,
    latency_ms: int = 100,
    link_next: bool = False,
    rl_used: int | None = None,
    run_id: int | None = None,
    phase: str | None = None,
) -> AuditRecord:
    params = {"q": f"topic:ai-{index}", "page": 1}
    return AuditRecord(
        ts=START + timedelta(seconds=index),
        query_hash=query_hash(params),
        params=params,
        etag_sent=None,
        status=status,
        rl_limit=30,
        rl_remaining=rl_remaining,
        rl_reset=1770000000,
        rl_resource=rl_resource,
        retry_after=None,
        link_next=link_next,
        total_count=1000,
        incomplete_results=incomplete_results,
        token_fp="fp-test",
        latency_ms=latency_ms,
        rl_used=rl_used,
        run_id=run_id,
        phase=phase,
    )


def test_record_audit_inserts_and_reads_back(clean: Engine):
    params = {"q": "org:github", "sort": "stars", "nested": {"per_page": 100}}
    record = AuditRecord(
        ts=START,
        query_hash=query_hash(params),
        params=params,
        etag_sent='W/"etag-1"',
        status=422,
        rl_limit=30,
        rl_remaining=0,
        rl_reset=1770000001,
        rl_resource="search",
        retry_after=7,
        link_next=True,
        total_count=999,
        incomplete_results=False,
        token_fp="deadbeef",
        latency_ms=123,
    )
    record_audit(clean, record)
    with clean.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT ts, query_hash, params, etag_sent, status, rl_limit, rl_remaining, "
                    "rl_reset, rl_resource, retry_after, link_next, total_count, "
                    "incomplete_results, token_fp, latency_ms FROM audit_log"
                )
            )
            .mappings()
            .one()
        )
    assert row["ts"] == record.ts
    assert row["query_hash"] == record.query_hash
    assert row["params"] == record.params
    assert row["etag_sent"] == record.etag_sent
    assert row["status"] == record.status
    assert row["rl_limit"] == record.rl_limit
    assert row["rl_remaining"] == record.rl_remaining
    assert row["rl_reset"] == record.rl_reset
    assert row["rl_resource"] == record.rl_resource
    assert row["retry_after"] == record.retry_after
    assert row["link_next"] == record.link_next
    assert row["total_count"] == record.total_count
    assert row["incomplete_results"] == record.incomplete_results
    assert row["token_fp"] == record.token_fp
    assert row["latency_ms"] == record.latency_ms


def test_record_audit_persists_point_telemetry(clean: Engine):
    record = _record(0, rl_used=4123, run_id=13, phase="hydration")
    record_audit(clean, record)
    with clean.connect() as connection:
        row = (
            connection.execute(text("SELECT rl_used, run_id, phase FROM audit_log"))
            .mappings()
            .one()
        )
    assert dict(row) == {"rl_used": 4123, "run_id": 13, "phase": "hydration"}


def _audit_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.scalar(text("SELECT count(*) FROM audit_log")))


def test_audit_buffer_batches_inserts(clean: Engine):
    from sqlalchemy import event

    from lib.audit import AuditBuffer

    statements: list[str] = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if "INTO AUDIT_LOG" in statement.upper():
            statements.append(statement)

    buffer = AuditBuffer(clean, batch_size=10)
    event.listen(clean, "before_cursor_execute", listener)
    try:
        for index in range(10):
            buffer.add(_record(index))
        buffer.flush()
    finally:
        event.remove(clean, "before_cursor_execute", listener)
    assert len(statements) == 1
    assert _audit_count(clean) == 10


def test_audit_buffer_flush_persists_remaining_records_and_clears(clean: Engine):
    from lib.audit import AuditBuffer

    buffer = AuditBuffer(clean, batch_size=10)
    for index in range(3):
        buffer.add(_record(index))
    buffer.flush()
    assert _audit_count(clean) == 3
    buffer.flush()
    assert _audit_count(clean) == 3


def test_audit_buffer_rejects_non_positive_batch_size(clean: Engine):
    from lib.audit import AuditBuffer

    with pytest.raises(ValueError):
        AuditBuffer(clean, batch_size=0)


class _BlockingEngine:
    def __init__(self, engine: Engine, started: threading.Event, release: threading.Event) -> None:
        self._engine = engine
        self.started = started
        self.release = release

    def begin(self):
        self.started.set()
        if not self.release.wait(timeout=5.0):
            raise TimeoutError("the test never released the blocked insert")
        return self._engine.begin()


class _FailingEngine:
    def begin(self):
        raise RuntimeError("audit sink down")


def test_audit_buffer_add_returns_while_the_insert_is_blocked(clean: Engine):
    from lib.audit import AuditBuffer

    started = threading.Event()
    release = threading.Event()
    buffer = AuditBuffer(_BlockingEngine(clean, started, release), batch_size=1)

    buffer.add(_record(0))  # must return without waiting for the INSERT

    assert started.wait(timeout=5.0)
    release.set()
    buffer.flush()
    assert _audit_count(clean) == 1


def test_audit_buffer_flush_waits_for_the_blocked_insert(clean: Engine):
    from lib.audit import AuditBuffer

    started = threading.Event()
    release = threading.Event()
    buffer = AuditBuffer(_BlockingEngine(clean, started, release), batch_size=10)
    for index in range(3):
        buffer.add(_record(index))

    flushed = threading.Event()

    def run_flush() -> None:
        buffer.flush()
        flushed.set()

    flusher = threading.Thread(target=run_flush)
    flusher.start()
    assert started.wait(timeout=5.0)
    assert not flushed.is_set()  # flush is a barrier: it is still waiting on the INSERT

    release.set()
    flusher.join(timeout=5.0)
    assert flushed.is_set()
    assert _audit_count(clean) == 3


def test_audit_buffer_flush_raises_the_write_failure():
    from lib.audit import AuditBuffer

    buffer = AuditBuffer(_FailingEngine(), batch_size=10)
    buffer.add(_record(0))
    with pytest.raises(RuntimeError, match="audit sink down"):
        buffer.flush()


def test_audit_buffer_add_raises_a_stored_write_failure():
    from lib.audit import AuditBuffer

    buffer = AuditBuffer(_FailingEngine(), batch_size=1)
    buffer.add(_record(0))
    deadline = time.monotonic() + 5.0
    while buffer._error is None and time.monotonic() < deadline:
        time.sleep(0.01)

    with pytest.raises(RuntimeError, match="audit sink down"):
        buffer.add(_record(1))
    with pytest.raises(RuntimeError, match="audit sink down"):
        buffer.flush()


def test_slo_snapshot_empty_tables_returns_all_none(clean: Engine):
    assert slo_snapshot(clean) == SloSnapshot(
        graphql_remaining=None,
        incomplete_results_ratio=None,
        rate_422=None,
        rate_403_429=None,
        p95_latency_ms=None,
        shard_coverage=None,
        geo_unmatched_rate=None,
    )


def test_slo_snapshot_computes_audit_metrics(clean: Engine):
    record_audit(
        clean,
        _record(0, status=200, rl_remaining=30, incomplete_results=False, latency_ms=100),
    )
    record_audit(
        clean,
        _record(1, status=422, rl_remaining=29, incomplete_results=None, latency_ms=200),
    )
    record_audit(
        clean,
        _record(
            2,
            status=403,
            rl_resource="core",
            rl_remaining=5,
            incomplete_results=True,
            latency_ms=300,
        ),
    )
    record_audit(
        clean,
        _record(3, status=429, rl_remaining=28, incomplete_results=True, latency_ms=400),
    )
    snapshot = slo_snapshot(clean)
    assert snapshot.graphql_remaining == 28
    assert snapshot.incomplete_results_ratio == pytest.approx(2 / 3)
    assert snapshot.rate_422 == pytest.approx(0.25)
    assert snapshot.rate_403_429 == pytest.approx(0.5)
    assert snapshot.p95_latency_ms == pytest.approx(385.0)


def test_slo_snapshot_respects_window(clean: Engine):
    record_audit(clean, _record(0, status=200, incomplete_results=False, latency_ms=100))
    record_audit(clean, _record(1, status=403, incomplete_results=True, latency_ms=300))
    record_audit(clean, _record(2, status=403, incomplete_results=True, latency_ms=400))
    snapshot = slo_snapshot(clean, window=2)
    assert snapshot.rate_422 == 0.0
    assert snapshot.rate_403_429 == 1.0
    assert snapshot.incomplete_results_ratio == 1.0
    assert snapshot.p95_latency_ms == pytest.approx(395.0)
    assert snapshot.graphql_remaining == 30


def test_slo_snapshot_computes_shard_coverage_and_geo_rate(clean: Engine):
    with clean.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO shards (kind, state) VALUES "
                "('search-range', 'done'), ('search-range', 'incomplete'), "
                "('search-range', 'pending')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO owners (id, login, type, location_raw, geo_confidence) VALUES "
                "(1, 'a', 'User', 'Berlin', 'exact-ISO'), "
                "(2, 'b', 'User', 'Nowhere', 'unmatched'), "
                "(3, 'c', 'User', NULL, 'unmatched')"
            )
        )
    snapshot = slo_snapshot(clean)
    assert snapshot.shard_coverage == pytest.approx(2 / 3)
    assert snapshot.geo_unmatched_rate == pytest.approx(0.5)
    assert snapshot.graphql_remaining is None
    assert snapshot.rate_422 is None


def test_slo_snapshot_zeros_are_not_none(clean: Engine):
    record_audit(clean, _record(0, status=200, incomplete_results=False))
    with clean.begin() as connection:
        connection.execute(text("INSERT INTO shards (kind, state) VALUES ('org', 'pending')"))
        connection.execute(
            text(
                "INSERT INTO owners (id, login, type, location_raw, geo_confidence) VALUES "
                "(1, 'a', 'User', 'Berlin', 'exact-ISO')"
            )
        )
    snapshot = slo_snapshot(clean)
    assert snapshot.rate_422 == 0.0
    assert snapshot.rate_403_429 == 0.0
    assert snapshot.incomplete_results_ratio == 0.0
    assert snapshot.shard_coverage == 0.0
    assert snapshot.geo_unmatched_rate == 0.0
    assert snapshot.graphql_remaining == 30


def test_slo_snapshot_ratio_is_none_without_known_incomplete_values(clean: Engine):
    record_audit(clean, _record(0, incomplete_results=None))
    snapshot = slo_snapshot(clean)
    assert snapshot.incomplete_results_ratio is None
    assert snapshot.rate_422 == 0.0


def test_slo_snapshot_graphql_remaining_is_none_without_graphql_rows(clean: Engine):
    record_audit(clean, _record(0, rl_resource="core"))
    snapshot = slo_snapshot(clean)
    assert snapshot.graphql_remaining is None


def test_slo_snapshot_graphql_remaining_ignores_other_resources(clean: Engine):
    record_audit(clean, _record(0, rl_resource="graphql", rl_remaining=25))
    record_audit(clean, _record(1, rl_resource="core", rl_remaining=99))
    record_audit(clean, _record(2, rl_resource="search", rl_remaining=88))
    snapshot = slo_snapshot(clean)
    assert snapshot.graphql_remaining == 25


def test_slo_snapshot_graphql_remaining_uses_the_latest_graphql_row(clean: Engine):
    record_audit(clean, _record(0, rl_resource="graphql", rl_remaining=25))
    record_audit(clean, _record(1, rl_resource="graphql", rl_remaining=19))
    assert slo_snapshot(clean).graphql_remaining == 19
