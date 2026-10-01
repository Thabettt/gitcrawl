import hashlib
import json

import fakeredis
import httpx
import pytest

from lib.gh_client import (
    ACCEPT,
    API_BASE,
    API_VERSION,
    USER_AGENT,
    PartialResultsError,
    ThrottledError,
    build_headers,
    create_client,
    load_tokens,
    request_with_retry,
    resource_for_url,
    token_fingerprint,
)
from limiter.buckets import BucketLimiter


def test_constants_pin_github_api_values():
    assert API_BASE == "https://api.github.com"
    assert API_VERSION == "2022-11-28"
    assert USER_AGENT == "gitcrawl/0.0.1"
    assert ACCEPT == "application/vnd.github+json"


def test_load_tokens_reads_comma_list(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKENS", "token-one,token-two,token-three")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert load_tokens() == ["token-one", "token-two", "token-three"]


def test_load_tokens_trims_whitespace_and_drops_empty_entries(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKENS", "  token-one ,, token-two ,   ,token-three  ")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert load_tokens() == ["token-one", "token-two", "token-three"]


def test_load_tokens_empty_comma_list_falls_back_to_single(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKENS", " , ")
    monkeypatch.setenv("GITHUB_TOKEN", "single-token")
    assert load_tokens() == ["single-token"]


def test_load_tokens_falls_back_to_single_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKENS", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "single-token")
    assert load_tokens() == ["single-token"]


def test_load_tokens_prefers_comma_list_when_both_set(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKENS", "list-token")
    monkeypatch.setenv("GITHUB_TOKEN", "single-token")
    assert load_tokens() == ["list-token"]


def test_load_tokens_returns_empty_list_when_neither_set(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKENS", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert load_tokens() == []


def test_token_fingerprint_is_stable_sha256_prefix():
    token = "ghp_example-token"
    expected = hashlib.sha256(token.encode()).hexdigest()[:12]
    assert token_fingerprint(token) == expected
    assert token_fingerprint(token) == token_fingerprint(token)


def test_token_fingerprint_is_twelve_hex_chars_and_not_the_token():
    token = "ghp_example-token"
    fingerprint = token_fingerprint(token)
    assert len(fingerprint) == 12
    assert all(character in "0123456789abcdef" for character in fingerprint)
    assert fingerprint != token


def test_token_fingerprint_differs_per_token():
    assert token_fingerprint("token-a") != token_fingerprint("token-b")


def test_build_headers_without_token_has_exact_defaults():
    assert build_headers() == {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "gitcrawl/0.0.1",
    }


def test_build_headers_with_none_token_has_no_authorization():
    assert "Authorization" not in build_headers(None)


def test_build_headers_with_empty_token_has_no_authorization():
    assert "Authorization" not in build_headers("")


def test_build_headers_with_token_adds_bearer_authorization():
    assert build_headers("ghp_example-token") == {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "gitcrawl/0.0.1",
        "Authorization": "Bearer ghp_example-token",
    }


def capture_client_traffic(monkeypatch, token, timeout=30.0):
    captured = {}
    real_client = httpx.Client

    def handler(request):
        captured["headers"] = request.headers
        return httpx.Response(200, json={"ok": True})

    def factory(**kwargs):
        captured["timeout"] = kwargs.get("timeout")
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    client = create_client(token, timeout=timeout)
    response = client.get(API_BASE + "/")
    client.close()
    return captured, response


def test_create_client_sends_required_headers(monkeypatch):
    captured, response = capture_client_traffic(monkeypatch, "ghp_example-token")
    assert response.status_code == 200
    headers = captured["headers"]
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert headers["User-Agent"] == "gitcrawl/0.0.1"
    assert headers["Authorization"] == "Bearer ghp_example-token"


def test_create_client_without_token_sends_no_authorization(monkeypatch):
    captured, _ = capture_client_traffic(monkeypatch, None)
    assert "Authorization" not in captured["headers"]


def test_create_client_uses_default_timeout(monkeypatch):
    captured, _ = capture_client_traffic(monkeypatch, None)
    assert captured["timeout"] == 30.0


def test_create_client_honors_custom_timeout(monkeypatch):
    captured, _ = capture_client_traffic(monkeypatch, None, timeout=5.0)
    assert captured["timeout"] == 5.0


@pytest.mark.parametrize(
    ("url", "resource"),
    [
        ("https://api.github.com/search/repositories", "search"),
        ("https://api.github.com/search/repositories?q=x&page=2", "search"),
        ("https://api.github.com/search/code", "code_search"),
        ("https://api.github.com/repos/octocat/hello-world", "core"),
        ("https://api.github.com/orgs/github/repos", "core"),
        ("https://api.github.com/rate_limit", "core"),
    ],
)
def test_resource_for_url_maps_github_resources(url, resource):
    assert resource_for_url(url) == resource


def test_request_with_retry_deny_loop_raises_throttled_error_without_http():
    redis = fakeredis.FakeRedis()
    limiter = BucketLimiter(redis, specs={"search": (0, 60.0)}, max_concurrent=100)
    sleeps = []
    requests = []

    def handler(request):
        requests.append(request)
        raise AssertionError("HTTP must not be attempted while the limiter denies")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ThrottledError) as excinfo:
        request_with_retry(
            client,
            "GET",
            "https://api.github.com/search/repositories?q=x",
            limiter=limiter,
            token_id="tok",
            max_attempts=3,
            sleep=sleeps.append,
            now=lambda: 1000.0,
        )
    assert excinfo.value.retry_after == 20.0
    assert sleeps == [20.0, 20.0]
    assert requests == []


def test_request_with_retry_propagates_on_response_exceptions():
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    def boom(response, latency_ms):
        raise RuntimeError("audit exploded")

    with pytest.raises(RuntimeError, match="audit exploded"):
        request_with_retry(
            client,
            "GET",
            "https://api.github.com/x",
            on_response=boom,
            now=lambda: 1.0,
        )


def test_sso_partial_results_raises_on_200():
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"X-GitHub-SSO": "required; partial-results"},
                json={"ok": True},
            )
        )
    )
    with pytest.raises(PartialResultsError) as excinfo:
        request_with_retry(
            client,
            "GET",
            "https://api.github.com/search/repositories?q=x",
            now=lambda: 1000.0,
        )
    assert excinfo.value.status_code == 200


def test_sso_partial_results_raises_before_status_triage():
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                404,
                headers={"x-github-sso": "required; partial-results"},
                json={"message": "Not Found"},
            )
        )
    )
    with pytest.raises(PartialResultsError):
        request_with_retry(
            client,
            "GET",
            "https://api.github.com/repos/octo/missing",
            now=lambda: 1000.0,
        )


def test_sso_header_without_partial_results_stays_successful():
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"x-github-sso": "required"}, json={})
        )
    )
    response = request_with_retry(
        client,
        "GET",
        "https://api.github.com/repos/octo/hello-world",
        now=lambda: 1000.0,
    )
    assert response.status_code == 200


def test_malformed_retry_after_falls_back_to_backoff():
    sleeps = []
    responses = iter(
        [
            httpx.Response(403, headers={"retry-after": "soon"}, json={"message": "slow down"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda request: next(responses)))
    response = request_with_retry(
        client,
        "GET",
        "https://api.github.com/x",
        sleep=sleeps.append,
        now=lambda: 1000.0,
        jitter=lambda: 0.0,
    )
    assert response.status_code == 200
    assert sleeps == [60.0]


def test_spam_422_error_code_drives_backoff():
    sleeps = []
    responses = iter(
        [
            httpx.Response(
                422,
                json={
                    "message": "You have exceeded a secondary rate limit",
                    "errors": [{"code": "custom"}],
                },
            ),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda request: next(responses)))
    response = request_with_retry(
        client,
        "GET",
        "https://api.github.com/search/repositories?q=x",
        sleep=sleeps.append,
        now=lambda: 1000.0,
        jitter=lambda: 0.0,
    )
    assert response.status_code == 200
    assert sleeps == [60.0]


def test_request_with_retry_releases_limiter_slot_between_sequential_calls():
    redis = fakeredis.FakeRedis()
    limiter = BucketLimiter(redis, specs={"core": (5000, 3600.0)}, max_concurrent=1)

    def handler(request):
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    for _ in range(3):
        response = request_with_retry(
            client,
            "GET",
            "https://api.github.com/repos/octocat/hello-world",
            limiter=limiter,
            token_id="tok",
            now=lambda: 1000.0,
        )
        assert response.status_code == 200


def test_request_with_retry_releases_limiter_slot_when_send_raises():
    redis = fakeredis.FakeRedis()
    limiter = BucketLimiter(redis, specs={"core": (5000, 3600.0)}, max_concurrent=1)
    attempts = {"count": 0}

    def handler(request):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.ConnectError("connection reset")
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.ConnectError):
        request_with_retry(
            client,
            "GET",
            "https://api.github.com/repos/octocat/hello-world",
            limiter=limiter,
            token_id="tok",
            now=lambda: 1000.0,
        )
    response = request_with_retry(
        client,
        "GET",
        "https://api.github.com/repos/octocat/hello-world",
        limiter=limiter,
        token_id="tok",
        now=lambda: 1000.0,
    )
    assert response.status_code == 200
    assert attempts["count"] == 2


def test_persistent_retry_after_stops_at_max_attempts():
    sends = []
    sleeps = []

    def handler(request):
        sends.append(request)
        if len(sends) > 3:
            raise AssertionError("retried past max_attempts")
        return httpx.Response(403, headers={"retry-after": "5"}, json={"message": "slow"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    response = request_with_retry(
        client,
        "GET",
        "https://api.github.com/search/repositories?q=x",
        max_attempts=3,
        sleep=sleeps.append,
        now=lambda: 1000.0,
    )
    assert response.status_code == 403
    assert len(sends) == 3
    assert sleeps == [5.0, 5.0]


def test_persistent_wait_reset_stops_at_max_attempts():
    sends = []
    sleeps = []

    def handler(request):
        sends.append(request)
        if len(sends) > 2:
            raise AssertionError("retried past max_attempts")
        return httpx.Response(
            429,
            headers={
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": "1300",
                "x-ratelimit-resource": "search",
            },
            json={"message": "API rate limit exceeded"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    response = request_with_retry(
        client,
        "GET",
        "https://api.github.com/search/repositories?q=x",
        max_attempts=2,
        sleep=sleeps.append,
        now=lambda: 1000.0,
    )
    assert response.status_code == 429
    assert len(sends) == 2
    assert sleeps == [300.0]


def test_request_with_retry_posts_json_body_and_extra_headers():
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        captured["headers"] = request.headers
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(
        headers={"User-Agent": "gitcrawl/0.0.1"},
        transport=httpx.MockTransport(handler),
    )
    response = request_with_retry(
        client,
        "POST",
        "https://api.github.com/graphql",
        json_body={"query": "{ rateLimit { cost } }"},
        extra_headers={"Authorization": "Bearer tok"},
        now=lambda: 1000.0,
    )
    assert response.status_code == 200
    assert captured["method"] == "POST"
    assert captured["body"] == {"query": "{ rateLimit { cost } }"}
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert captured["headers"]["User-Agent"] == "gitcrawl/0.0.1"


def test_request_with_retry_auth_false_strips_client_authorization():
    captured = {}

    def handler(request):
        captured["headers"] = request.headers
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(
        headers={"Authorization": "Bearer ghp_secret", "User-Agent": "gitcrawl/0.0.1"},
        transport=httpx.MockTransport(handler),
    )
    response = request_with_retry(
        client,
        "GET",
        "https://repos.ecosyste.ms/api/v1/hosts/GitHub/repositories/octo%2Fhello",
        auth=False,
        now=lambda: 1000.0,
    )

    assert response.status_code == 200
    assert "Authorization" not in captured["headers"]
    assert captured["headers"]["User-Agent"] == "gitcrawl/0.0.1"


def test_request_with_retry_auth_default_keeps_client_authorization():
    captured = {}

    def handler(request):
        captured["headers"] = request.headers
        return httpx.Response(200, json={})

    client = httpx.Client(
        headers={"Authorization": "Bearer ghp_secret"},
        transport=httpx.MockTransport(handler),
    )
    request_with_retry(client, "GET", "https://api.github.com/repos/octo/hello", now=lambda: 1.0)

    assert captured["headers"]["Authorization"] == "Bearer ghp_secret"


def test_request_with_retry_merges_extra_headers_over_client_defaults():
    captured = {}

    def handler(request):
        captured["headers"] = request.headers
        return httpx.Response(200, json={})

    client = httpx.Client(
        headers={"User-Agent": "gitcrawl/0.0.1", "Authorization": "Bearer original"},
        transport=httpx.MockTransport(handler),
    )
    response = request_with_retry(
        client,
        "GET",
        "https://api.github.com/repos/octo/hello",
        extra_headers={"If-None-Match": 'W/"one"', "Authorization": "Bearer override"},
        now=lambda: 1000.0,
    )
    assert response.status_code == 200
    assert captured["headers"]["If-None-Match"] == 'W/"one"'
    assert captured["headers"]["Authorization"] == "Bearer override"
    assert captured["headers"]["User-Agent"] == "gitcrawl/0.0.1"


def test_request_with_retry_keeps_json_body_and_audit_hook_across_retries():
    bodies = []
    sleeps = []
    recorded = []
    responses = iter(
        [
            httpx.Response(403, headers={"retry-after": "5"}, json={"message": "slow"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    def handler(request):
        bodies.append(json.loads(request.content))
        return next(responses)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    response = request_with_retry(
        client,
        "POST",
        "https://api.github.com/graphql",
        json_body={"query": "q"},
        extra_headers={"Authorization": "Bearer tok"},
        sleep=sleeps.append,
        now=lambda: 1000.0,
        jitter=lambda: 0.0,
        on_response=lambda response, latency_ms: recorded.append(
            (response.status_code, latency_ms)
        ),
    )
    assert response.status_code == 200
    assert bodies == [{"query": "q"}, {"query": "q"}]
    assert sleeps == [5.0]
    assert [status for status, _ in recorded] == [403, 200]


def test_limiter_deny_loop_raises_at_configured_cap():
    redis = fakeredis.FakeRedis()
    limiter = BucketLimiter(redis, specs={"search": (0, 60.0)}, max_concurrent=100)
    sleeps = []
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    with pytest.raises(ThrottledError) as excinfo:
        request_with_retry(
            client,
            "GET",
            "https://api.github.com/search/repositories?q=x",
            limiter=limiter,
            token_id="tok",
            max_attempts=2,
            sleep=sleeps.append,
            now=lambda: 1000.0,
        )
    assert len(sleeps) == 1
    assert excinfo.value.retry_after == 20.0
