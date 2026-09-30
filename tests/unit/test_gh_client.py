import hashlib

import fakeredis
import httpx
import pytest

from lib.gh_client import (
    ACCEPT,
    API_BASE,
    API_VERSION,
    USER_AGENT,
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
