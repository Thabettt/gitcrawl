from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import fakeredis
import httpx
import pytest

from discover.search_shards import RequestFailed, SearchCapExceeded, iter_shard_pages
from lib.gh_client import API_BASE, build_headers, load_tokens, request_with_retry
from limiter.buckets import BucketLimiter

QUERY = "language:python stars:>500"
NEXT_URL = (
    "https://api.github.com/search/repositories?q=odd%3Aquery+%26more&per_page=37"
    "&page=9&sort=updated&unknown=keep%2Bme"
)


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


def client_from(responses, recorder=None, headers=None):
    iterator = iter(responses)

    def handler(request):
        if recorder is not None:
            recorder.append(request)
        return next(iterator)

    return httpx.Client(transport=httpx.MockTransport(handler), headers=headers)


def test_first_request_uses_pinned_params_and_env_built_headers(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKENS", "env-token")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    captured = []
    headers = build_headers(load_tokens()[0])
    client = client_from([search_page()], captured, headers=headers)
    pages = list(iter_shard_pages(client, QUERY, max_pages=1))
    request = captured[0]
    parsed = urlparse(str(request.url))
    assert parsed.path == "/search/repositories"
    assert parse_qs(parsed.query) == {"q": [QUERY], "per_page": ["100"], "page": ["1"]}
    for name, value in headers.items():
        assert request.headers[name] == value
    assert pages[0].url == str(request.url)


def test_next_link_url_is_followed_verbatim_without_rebuilding():
    captured = []
    responses = [
        search_page(headers={"Link": f'<{NEXT_URL}>; rel="next"'}),
        search_page(items=[{"id": 3}]),
    ]
    client = client_from(responses, captured)
    pages = list(iter_shard_pages(client, QUERY, max_pages=5))
    assert len(captured) == 2
    assert str(captured[1].url) == NEXT_URL
    assert pages[1].url == NEXT_URL
    assert pages[0].next_url == NEXT_URL
    assert pages[1].next_url is None


def test_missing_next_link_stops_pagination():
    captured = []
    client = client_from([search_page()], captured)
    pages = list(iter_shard_pages(client, QUERY))
    assert len(captured) == 1
    assert len(pages) == 1
    assert pages[0].next_url is None


def test_max_pages_caps_requests_even_with_next_links():
    captured = []
    responses = [
        search_page(headers={"Link": f'<{NEXT_URL}>; rel="next"'}),
        search_page(headers={"Link": f'<{NEXT_URL}>; rel="next"'}),
        search_page(headers={"Link": f'<{NEXT_URL}>; rel="next"'}),
    ]
    client = client_from(responses, captured)
    pages = list(iter_shard_pages(client, QUERY, max_pages=2))
    assert len(captured) == 2
    assert len(pages) == 2
    assert pages[0].exhausted is False
    assert pages[1].exhausted is True


def test_exhausted_flag_is_false_when_the_final_page_has_no_next_link():
    client = client_from([search_page()])
    pages = list(iter_shard_pages(client, QUERY, max_pages=1))
    assert len(pages) == 1
    assert pages[0].exhausted is False


def test_result_cap_422_raises_search_cap_exceeded_with_single_request():
    captured = []
    sleeps = []
    responses = [
        httpx.Response(422, json={"message": "Only the first 1000 search results are available"})
    ]
    client = client_from(responses, captured)
    with pytest.raises(SearchCapExceeded) as excinfo:
        list(iter_shard_pages(client, QUERY, sleep=sleeps.append, now=lambda: 1000.0))
    assert excinfo.value.query == QUERY
    assert len(captured) == 1
    assert sleeps == []


def test_validation_422_raises_request_failed_without_retry():
    captured = []
    responses = [
        httpx.Response(
            422,
            json={
                "message": "Validation Failed",
                "errors": [{"resource": "Search", "field": "q", "code": "invalid"}],
            },
        )
    ]
    client = client_from(responses, captured)
    with pytest.raises(RequestFailed) as excinfo:
        list(iter_shard_pages(client, QUERY, now=lambda: 1000.0))
    assert excinfo.value.status == 422
    assert excinfo.value.message == "Validation Failed"
    assert len(captured) == 1


def test_forbidden_retry_after_sleeps_exactly_then_retries_to_success():
    captured = []
    sleeps = []
    responses = [
        httpx.Response(403, headers={"retry-after": "7"}, json={"message": "secondary limit"}),
        search_page(),
    ]
    client = client_from(responses, captured)
    pages = list(iter_shard_pages(client, QUERY, sleep=sleeps.append, now=lambda: 1000.0))
    assert len(captured) == 2
    assert sleeps == [7.0]
    assert len(pages) == 1


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
    pages = list(iter_shard_pages(client, QUERY, sleep=sleeps.append, now=lambda: 1000.0))
    assert len(captured) == 2
    assert sleeps == [300.0]
    assert len(pages) == 1


def test_service_unavailable_backs_off_then_retries():
    captured = []
    sleeps = []
    responses = [httpx.Response(503, json={"message": "server error"}), search_page()]
    client = client_from(responses, captured)
    pages = list(
        iter_shard_pages(client, QUERY, sleep=sleeps.append, now=lambda: 1000.0, jitter=lambda: 0.0)
    )
    assert len(captured) == 2
    assert sleeps == [60.0]
    assert len(pages) == 1


def test_unauthorized_fails_loud_without_sleep_or_retry():
    captured = []
    sleeps = []
    responses = [httpx.Response(401, json={"message": "Bad credentials"})]
    client = client_from(responses, captured)
    with pytest.raises(RequestFailed) as excinfo:
        list(iter_shard_pages(client, QUERY, sleep=sleeps.append, now=lambda: 1000.0))
    assert excinfo.value.status == 401
    assert excinfo.value.message == "Bad credentials"
    assert len(captured) == 1
    assert sleeps == []


def test_incomplete_results_is_surfaced_on_the_page():
    client = client_from([search_page(total_count=1234, incomplete=True)])
    pages = list(iter_shard_pages(client, QUERY))
    assert pages[0].incomplete is True
    assert pages[0].total_count == 1234
    assert pages[0].items == ({"id": 1}, {"id": 2})


def test_limiter_gate_acquires_and_reconciles_from_response_headers():
    redis = fakeredis.FakeRedis()
    limiter = BucketLimiter(redis, specs={"search": (30, 60.0)}, max_concurrent=100)
    responses = [
        search_page(headers={"x-ratelimit-remaining": "25", "x-ratelimit-resource": "search"})
    ]
    client = client_from(responses)
    list(iter_shard_pages(client, QUERY, limiter=limiter, token_id="tok", now=lambda: 1000.0))
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
    list(
        iter_shard_pages(
            client,
            QUERY,
            sleep=lambda seconds: None,
            now=SteppingClock(),
            jitter=lambda: 0.0,
            on_response=lambda response, latency_ms: recorded.append(
                (response.status_code, latency_ms)
            ),
        )
    )
    assert recorded == [(503, 250.0), (200, 250.0)]


def test_not_modified_returns_a_response_without_retry():
    captured = []
    client = client_from([httpx.Response(304)], captured)
    response = request_with_retry(client, "GET", f"{API_BASE}/repos/octocat/hello-world")
    assert response.status_code == 304
    assert len(captured) == 1


def test_list_200_body_is_treated_as_items_with_zero_total():
    client = client_from([httpx.Response(200, json=[{"id": 1}, {"id": 2}])])
    pages = list(iter_shard_pages(client, QUERY))
    assert pages[0].items == ({"id": 1}, {"id": 2})
    assert pages[0].total_count == 0
    assert pages[0].incomplete is False


def test_non_json_200_body_raises_request_failed():
    client = client_from([httpx.Response(200, text="<html>proxy error</html>")])
    with pytest.raises(RequestFailed) as excinfo:
        list(iter_shard_pages(client, QUERY, now=lambda: 1000.0))
    assert excinfo.value.status == 200
    assert "proxy error" in excinfo.value.message


def test_scalar_json_200_body_raises_request_failed():
    client = client_from([httpx.Response(200, json=7)])
    with pytest.raises(RequestFailed) as excinfo:
        list(iter_shard_pages(client, QUERY, now=lambda: 1000.0))
    assert excinfo.value.status == 200


def test_audit_cached_body_is_reused_for_the_page_parse():
    from lib.audit import record_from_response

    parses: list[int] = []
    response = search_page()
    original = response.json

    def counting_json():
        parses.append(1)
        return original()

    response.json = counting_json
    client = client_from([response])

    pages = list(
        iter_shard_pages(
            client,
            QUERY,
            on_response=lambda resp, latency_ms: record_from_response(
                {}, resp, token_fp="fp", latency_ms=latency_ms
            ),
        )
    )

    assert pages[0].items == ({"id": 1}, {"id": 2})
    assert parses == [1]
