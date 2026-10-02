from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import insert, text
from sqlalchemy.engine import Engine

from store.models import AuditLog


@dataclass(frozen=True)
class AuditRecord:
    ts: datetime
    query_hash: str
    params: dict
    etag_sent: str | None
    status: int
    rl_limit: int | None
    rl_remaining: int | None
    rl_reset: int | None
    rl_resource: str | None
    retry_after: int | None
    link_next: bool
    total_count: int | None
    incomplete_results: bool | None
    token_fp: str
    latency_ms: int


@dataclass(frozen=True)
class SloSnapshot:
    search_remaining: int | None
    incomplete_results_ratio: float | None
    rate_422: float | None
    rate_403_429: float | None
    p95_latency_ms: float | None
    shard_coverage: float | None
    geo_unmatched_rate: float | None


def query_hash(params: Mapping[str, object]) -> str:
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if str(key).lower() == name:
            return str(value)
    return None


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _has_next_link(link: str | None) -> bool:
    if not link:
        return False
    for part in link.split(","):
        segments = part.split(";")
        for segment in segments[1:]:
            key, _, value = segment.partition("=")
            if key.strip().lower() == "rel" and value.strip().strip('"').lower() == "next":
                return True
    return False


_PARSED_BODY_KEY = "gitcrawl.json"


def cached_json(response: httpx.Response) -> object | None:
    return response.extensions.get(_PARSED_BODY_KEY)


def record_from_response(
    params: Mapping[str, object],
    response: httpx.Response,
    *,
    token_fp: str,
    latency_ms: int,
    etag_sent: str | None = None,
    now: datetime | None = None,
    body: object | None = None,
) -> AuditRecord:
    headers = response.headers
    if body is None:
        try:
            body = response.json()
        except Exception:
            body = None
    response.extensions[_PARSED_BODY_KEY] = body
    total_count: int | None = None
    incomplete_results: bool | None = None
    if isinstance(body, Mapping):
        total_count = _as_int(body.get("total_count"))
        incomplete = body.get("incomplete_results")
        if isinstance(incomplete, bool):
            incomplete_results = incomplete
    return AuditRecord(
        ts=now if now is not None else datetime.now(UTC),
        query_hash=query_hash(params),
        params=dict(params),
        etag_sent=etag_sent,
        status=int(response.status_code),
        rl_limit=_as_int(_header(headers, "x-ratelimit-limit")),
        rl_remaining=_as_int(_header(headers, "x-ratelimit-remaining")),
        rl_reset=_as_int(_header(headers, "x-ratelimit-reset")),
        rl_resource=_header(headers, "x-ratelimit-resource"),
        retry_after=_as_int(_header(headers, "retry-after")),
        link_next=_has_next_link(_header(headers, "link")),
        total_count=total_count,
        incomplete_results=incomplete_results,
        token_fp=token_fp,
        latency_ms=int(latency_ms),
    )


def record_audit(engine: Engine, record: AuditRecord) -> None:
    with engine.begin() as connection:
        connection.execute(insert(AuditLog).values(**asdict(record)))


class AuditBuffer:
    def __init__(self, engine: Engine, *, batch_size: int = 100) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self._engine = engine
        self._batch_size = batch_size
        self._records: list[AuditRecord] = []

    def add(self, record: AuditRecord) -> None:
        self._records.append(record)
        if len(self._records) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._records:
            return
        records, self._records = self._records, []
        with self._engine.begin() as connection:
            connection.execute(insert(AuditLog), [asdict(record) for record in records])


_AUDIT_WINDOW_SQL = text("""
    WITH window_rows AS (
        SELECT * FROM audit_log ORDER BY ts DESC, id DESC LIMIT :window
    )
    SELECT
        (
            SELECT rl_remaining FROM window_rows
            WHERE rl_resource = 'search' AND rl_remaining IS NOT NULL
            ORDER BY ts DESC, id DESC LIMIT 1
        ) AS search_remaining,
        count(*) AS total,
        count(incomplete_results) AS incomplete_known,
        count(*) FILTER (WHERE incomplete_results) AS incomplete_true,
        count(*) FILTER (WHERE status = 422) AS n422,
        count(*) FILTER (WHERE status IN (403, 429)) AS n403_429,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95
    FROM window_rows
    """)

_SHARD_SQL = text("""
    SELECT
        count(*) AS total,
        count(*) FILTER (WHERE state IN ('done', 'incomplete')) AS finished
    FROM shards
    """)

_GEO_SQL = text("""
    SELECT
        count(*) FILTER (WHERE location_raw IS NOT NULL) AS with_location,
        count(*) FILTER (
            WHERE location_raw IS NOT NULL AND geo_confidence = 'unmatched'
        ) AS unmatched
    FROM owners
    """)


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def slo_snapshot(engine: Engine, *, window: int = 1000) -> SloSnapshot:
    with engine.connect() as connection:
        audit = connection.execute(_AUDIT_WINDOW_SQL, {"window": window}).mappings().one()
        shards = connection.execute(_SHARD_SQL).mappings().one()
        geo = connection.execute(_GEO_SQL).mappings().one()
    return SloSnapshot(
        search_remaining=audit["search_remaining"],
        incomplete_results_ratio=_ratio(audit["incomplete_true"], audit["incomplete_known"]),
        rate_422=_ratio(audit["n422"], audit["total"]),
        rate_403_429=_ratio(audit["n403_429"], audit["total"]),
        p95_latency_ms=audit["p95"],
        shard_coverage=_ratio(shards["finished"], shards["total"]),
        geo_unmatched_rate=_ratio(geo["unmatched"], geo["with_location"]),
    )
