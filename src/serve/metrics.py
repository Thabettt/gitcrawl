from __future__ import annotations

from dataclasses import asdict
from time import time

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from lib.audit import slo_snapshot
from store.models import Runs

_PREFIX = "gitcrawl:rl:"

THRESHOLDS = {
    "search_remaining": ("lt", 100, 10),
    "incomplete_results_ratio": ("gt", 0.05, 0.2),
    "rate_422": ("gt", 0.0, 0.02),
    "rate_403_429": ("gt", 0.0, 0.05),
    "p95_latency_ms": ("gt", 800, 2000),
    "shard_coverage": ("lt", 0.95, 0.8),
    "geo_unmatched_rate": ("gt", 0.4, 0.6),
}

LABELS = {
    "search_remaining": "Search remaining",
    "incomplete_results_ratio": "Incomplete ratio",
    "rate_422": "422 rate",
    "rate_403_429": "403/429 rate",
    "p95_latency_ms": "p95 latency",
    "shard_coverage": "Shard coverage",
    "geo_unmatched_rate": "Geo unmatched",
}


def severity(name: str, value) -> str:
    if value is None:
        return "na"
    direction, warn, fail = THRESHOLDS[name]
    bad = (value < fail) if direction == "lt" else (value > fail)
    if bad:
        return "fail"
    near = (value < warn) if direction == "lt" else (value > warn)
    return "warn" if near else "ok"


def _paused(redis_client) -> list[dict]:
    if redis_client is None:
        return []
    now = time()
    try:
        keys = list(redis_client.scan_iter(match=f"{_PREFIX}*", count=100))[:100]
    except Exception:
        return []
    paused: list[dict] = []
    for key in keys:
        try:
            name = key.decode() if isinstance(key, bytes) else str(key)
            _, _, resource, token_fp = name.split(":", 3)
            raw = redis_client.hget(key, "paused_until")
            if raw is None:
                continue
            until = float(raw.decode() if isinstance(raw, bytes) else raw)
        except Exception:
            continue
        if until > now:
            paused.append(
                {"resource": resource, "token_fp": token_fp, "seconds": round(until - now, 1)}
            )
    return sorted(paused, key=lambda item: -item["seconds"])


def _queue_pel(redis_client) -> int | None:
    if redis_client is None:
        return None
    try:
        from scheduler.state_machine import ShardQueue

        return int(ShardQueue(redis_client).total_pel())
    except Exception:
        return None


def _runs_by_status(engine: Engine) -> dict[str, int]:
    counts = {"queued": 0, "running": 0, "done": 0, "failed": 0, "partial": 0}
    with engine.connect() as connection:
        rows = connection.execute(select(Runs.status, func.count()).group_by(Runs.status)).all()
    for status, count in rows:
        if status in counts:
            counts[status] = int(count)
    return counts


def metrics_payload(engine: Engine, *, redis_client=None, window: int = 1000) -> dict:
    slo = asdict(slo_snapshot(engine, window=window))
    pel = _queue_pel(redis_client)
    return {
        "slo": slo,
        "runs": _runs_by_status(engine),
        "limiter": {"paused": _paused(redis_client), "degraded": redis_client is None},
        "queue": {"pel": pel, "degraded": redis_client is None or pel is None},
    }
