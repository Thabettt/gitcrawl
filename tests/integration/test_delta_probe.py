from __future__ import annotations

import os
import time
from urllib.parse import urlencode

import httpx
import pytest

from lib.gh_client import API_BASE, build_headers, request_with_retry, token_fingerprint
from lib.qualify import delta_narrows
from serve import audit

BASE_QUERY = "language:python"
NARROW_QUERY = "language:python stars:>1000"
SKEW_RETRY_SECONDS = 5.0

pytestmark = pytest.mark.skipif(
    not os.environ.get("GITHUB_TOKEN"),
    reason="GITHUB_TOKEN is not set; skipping the live delta probe",
)


def count_query(
    client: httpx.Client, query: str, *, token_fp: str, records: list[audit.AuditRecord]
) -> int:
    params = {"q": query, "per_page": 1, "page": 1}
    url = f"{API_BASE}/search/repositories?{urlencode(params)}"

    def hook(response: httpx.Response, latency_ms: float) -> None:
        records.append(
            audit.record_from_response(
                params,
                response,
                token_fp=token_fp,
                latency_ms=int(latency_ms),
            )
        )

    response = request_with_retry(client, "GET", url, on_response=hook, now=time.time)
    assert response.status_code == 200
    payload = response.json()
    return int(payload.get("total_count") or 0)


def probe(
    client: httpx.Client, *, token_fp: str, records: list[audit.AuditRecord]
) -> tuple[int, int]:
    base = count_query(client, BASE_QUERY, token_fp=token_fp, records=records)
    candidate = count_query(client, NARROW_QUERY, token_fp=token_fp, records=records)
    return base, candidate


def test_delta_probe_narrows_candidate_live() -> None:
    token = os.environ["GITHUB_TOKEN"]
    token_fp = token_fingerprint(token)
    records: list[audit.AuditRecord] = []
    client = httpx.Client(headers=build_headers(token), timeout=30.0)
    try:
        base, candidate = probe(client, token_fp=token_fp, records=records)
        if not delta_narrows(base, candidate):
            time.sleep(SKEW_RETRY_SECONDS)
            base, candidate = probe(client, token_fp=token_fp, records=records)
        print(
            f"delta probe: base({BASE_QUERY})={base} "
            f"candidate({NARROW_QUERY})={candidate} "
            f"delta_narrows={delta_narrows(base, candidate)}"
        )
        assert delta_narrows(base, candidate)
        assert len(records) >= 2
        assert all(record.status == 200 for record in records)
    finally:
        client.close()
