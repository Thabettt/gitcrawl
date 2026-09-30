import hashlib

import httpx

from lib.gh_client import (
    ACCEPT,
    API_BASE,
    API_VERSION,
    USER_AGENT,
    build_headers,
    create_client,
    load_tokens,
    token_fingerprint,
)


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
