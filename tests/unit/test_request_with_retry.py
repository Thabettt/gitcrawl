from __future__ import annotations

import fakeredis
import httpx
import pytest

from lib.gh_client import API_BASE, request_with_retry
from limiter.buckets import BucketLimiter

SEARCH_URL = f"{API_BASE}/search/repositories"


def search_page(items=None, *, total_count=2, incomplete=False, headers=None):
    return httpx.Response(
        200,
        json={
            "total_count": total_count,
            "incomplete_results": incomplete,
            "items": [{"id": 1}, {"id": 2}] if items is None else items,
        },
        headers=headers or {},
    )


def client_from(responses, recorder=None):
    iterator = iter(responses)

    def handler(request):
        if recorder is not None:
            recorder.append(request)
        return next(iterator)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_forbidden_retry_after_sleeps_exactly_then_retries_to_success():
    captured = []
    sleeps = []
    responses = [
        httpx.Response(403, headers={"retry-after": "7"}, json={"message": "secondary limit"}),
        search_page(),
    ]
    client = client_from(responses, captured)
    response = request_with_retry(
        client, "GET", SEARCH_URL, sleep=sleeps.append, now=lambda: 1000.0
    )
    assert response.status_code == 200
    assert len(captured) == 2
    assert sleeps == [7.0]


def test_rate_limited_exhausted_quota_waits_for_reset_then_retries():
    captured = []
    sleeps = []
    responses = [
        httpx.Response(
            429,
            headers={
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": "1300",
                "x-ratelimit-resource": "search",
            },
            json={"message": "API rate limit exceeded"},
        ),
        search_page(),
    ]
    client = client_from(responses, captured)
    response = request_with_retry(
        client, "GET", SEARCH_URL, sleep=sleeps.append, now=lambda: 1000.0
    )
    assert response.status_code == 200
    assert len(captured) == 2
    assert sleeps == [300.0]


def test_service_unavailable_backs_off_then_retries():
    captured = []
    sleeps = []
    responses = [httpx.Response(503, json={"message": "server error"}), search_page()]
    client = client_from(responses, captured)
    response = request_with_retry(
        client, "GET", SEARCH_URL, sleep=sleeps.append, now=lambda: 1000.0, jitter=lambda: 0.0
    )
    assert response.status_code == 200
    assert len(captured) == 2
    assert sleeps == [60.0]


@pytest.mark.parametrize("status", [502, 504])
def test_retry_5xx_false_returns_the_timeout_error_after_one_request_without_sleep(status):
    captured = []
    sleeps = []
    responses = [httpx.Response(status, json={"message": "server error"})]
    client = client_from(responses, captured)
    response = request_with_retry(
        client,
        "GET",
        SEARCH_URL,
        retry_5xx=False,
        sleep=sleeps.append,
        now=lambda: 1000.0,
        jitter=lambda: 0.0,
    )
    assert response.status_code == status
    assert len(captured) == 1
    assert sleeps == []


@pytest.mark.parametrize("status", [500, 503])
def test_retry_5xx_false_still_backs_off_and_retries_server_errors(status):
    captured = []
    sleeps = []
    responses = [httpx.Response(status, json={"message": "server error"}), search_page()]
    client = client_from(responses, captured)
    response = request_with_retry(
        client,
        "GET",
        SEARCH_URL,
        retry_5xx=False,
        sleep=sleeps.append,
        now=lambda: 1000.0,
        jitter=lambda: 0.0,
    )
    assert response.status_code == 200
    assert len(captured) == 2
    assert sleeps == [60.0]


def test_unauthorized_fails_loud_without_sleep_or_retry():
    captured = []
    sleeps = []
    responses = [httpx.Response(401, json={"message": "Bad credentials"})]
    client = client_from(responses, captured)
    response = request_with_retry(
        client, "GET", SEARCH_URL, sleep=sleeps.append, now=lambda: 1000.0
    )
    assert response.status_code == 401
    assert len(captured) == 1
    assert sleeps == []


def test_not_modified_returns_a_response_without_retry():
    captured = []
    client = client_from([httpx.Response(304)], captured)
    response = request_with_retry(client, "GET", f"{API_BASE}/repos/octocat/hello-world")
    assert response.status_code == 304
    assert len(captured) == 1


def test_limiter_gate_acquires_and_reconciles_from_response_headers():
    redis = fakeredis.FakeRedis()
    limiter = BucketLimiter(redis, specs={"search": (30, 60.0)}, max_concurrent=100)
    responses = [
        search_page(headers={"x-ratelimit-remaining": "25", "x-ratelimit-resource": "search"})
    ]
    client = client_from(responses)
    request_with_retry(
        client, "GET", SEARCH_URL, limiter=limiter, token_id="tok", now=lambda: 1000.0
    )
    allowed = 0
    while limiter.acquire("search", "tok", now=1000.0).allowed:
        allowed += 1
    assert allowed == 25


class SteppingClock:
    def __init__(self, step=0.25):
        self._value = 0.0
        self._step = step

    def __call__(self):
        value = self._value
        self._value += self._step
        return value


def test_on_response_is_called_for_every_response_with_latency():
    recorded = []
    responses = [httpx.Response(503, json={"message": "server error"}), search_page()]
    client = client_from(responses)
    request_with_retry(
        client,
        "GET",
        SEARCH_URL,
        sleep=lambda seconds: None,
        now=SteppingClock(),
        jitter=lambda: 0.0,
        on_response=lambda response, latency_ms: recorded.append(
            (response.status_code, latency_ms)
        ),
    )
    assert recorded == [(503, 250.0), (200, 250.0)]
