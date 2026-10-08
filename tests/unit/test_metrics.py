from __future__ import annotations

import fakeredis
import pytest
from alembic import command
from sqlalchemy import text

from serve.metrics import metrics_payload


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


def seed_audit(engine, rows: list[tuple]) -> None:
    with engine.begin() as connection:
        for status, remaining, incomplete, latency, resource in rows:
            connection.execute(
                text(
                    "INSERT INTO audit_log (query_hash, params, status, rl_remaining,"
                    " incomplete_results, latency_ms, token_fp, rl_resource)"
                    " VALUES ('h', '{}'::jsonb, :status, :remaining, :incomplete,"
                    " :latency, 'fp', :resource)"
                ),
                {
                    "status": status,
                    "remaining": remaining,
                    "incomplete": incomplete,
                    "latency": latency,
                    "resource": resource,
                },
            )


def test_metrics_payload_includes_all_slo_keys(clean_db):
    engine = clean_db()
    seed_audit(
        engine,
        [
            (200, 120, False, 100, "graphql"),
            (200, 90, True, 900, "graphql"),
            (422, 80, None, 1200, "graphql"),
            (429, 70, None, 50, "core"),
        ],
    )
    payload = metrics_payload(engine, redis_client=None)
    assert set(payload) == {"slo", "runs", "limiter"}
    slo = payload["slo"]
    assert set(slo) == {
        "graphql_remaining",
        "incomplete_results_ratio",
        "rate_422",
        "rate_403_429",
        "p95_latency_ms",
        "shard_coverage",
        "geo_unmatched_rate",
    }
    # most recent graphql-resource row (the trailing 429 is core budget)
    assert slo["graphql_remaining"] == 80
    assert slo["incomplete_results_ratio"] == 0.5
    assert slo["rate_422"] == 0.25
    assert payload["runs"]["done"] == 0
    assert payload["limiter"]["degraded"] is True  # no redis


def test_slo_snapshot_keys_match_thresholds_and_labels():
    from dataclasses import asdict

    from lib.audit import SloSnapshot
    from serve.metrics import LABELS, THRESHOLDS

    snapshot = SloSnapshot(
        graphql_remaining=None,
        incomplete_results_ratio=None,
        rate_422=None,
        rate_403_429=None,
        p95_latency_ms=None,
        shard_coverage=None,
        geo_unmatched_rate=None,
    )
    assert set(asdict(snapshot)) == set(THRESHOLDS) == set(LABELS)


def test_severity_thresholds():
    from serve.metrics import severity

    assert severity("p95_latency_ms", None) == "na"
    assert severity("p95_latency_ms", 100) == "ok"
    assert severity("p95_latency_ms", 1000) == "warn"
    assert severity("p95_latency_ms", 5000) == "fail"
    assert severity("shard_coverage", 0.99) == "ok"
    assert severity("shard_coverage", 0.9) == "warn"
    assert severity("shard_coverage", 0.5) == "fail"


def test_metrics_payload_reads_paused_buckets(clean_db, monkeypatch):
    engine = clean_db()
    redis = fakeredis.FakeRedis()
    redis.hset("gitcrawl:rl:search:abcdef123456", mapping={"paused_until": 10_000})
    monkeypatch.setattr("serve.metrics.time", lambda: 9_000.0)
    payload = metrics_payload(engine, redis_client=redis)
    paused = payload["limiter"]["paused"]
    assert paused == [{"resource": "search", "token_fp": "abcdef123456", "seconds": 1000.0}]


def test_metrics_payload_tolerates_malformed_rl_keys(clean_db, monkeypatch):
    engine = clean_db()
    redis = fakeredis.FakeRedis()
    redis.hset("gitcrawl:rl:search:abcdef123456", mapping={"paused_until": 10_000})
    redis.hset("gitcrawl:rl:oops", mapping={"paused_until": 10_000})
    redis.hset("gitcrawl:rl:core:badvalue", mapping={"paused_until": "not-a-number"})
    monkeypatch.setattr("serve.metrics.time", lambda: 9_000.0)
    payload = metrics_payload(engine, redis_client=redis)
    assert payload["limiter"]["paused"] == [
        {"resource": "search", "token_fp": "abcdef123456", "seconds": 1000.0}
    ]
