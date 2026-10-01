import hashlib
import os
import time
from collections.abc import Callable, Mapping
from urllib.parse import urlparse

import httpx

from limiter.buckets import BucketLimiter
from limiter.classifier import Action, classify

API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "gitcrawl/0.0.1"
ACCEPT = "application/vnd.github+json"


def load_tokens() -> list[str]:
    raw = os.environ.get("GITHUB_TOKENS", "")
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    if tokens:
        return tokens
    single = os.environ.get("GITHUB_TOKEN", "").strip()
    if single:
        return [single]
    return []


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def build_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": ACCEPT,
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def create_client(token: str | None = None, *, timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(headers=build_headers(token), timeout=timeout)


_TRIAGE_STATUSES = frozenset({403, 429, 422, 500, 502, 503, 504})
_RETRYABLE_ACTIONS = frozenset({Action.RETRY_AFTER, Action.WAIT_RESET, Action.BACKOFF})
_BODY_FALLBACK_CHARS = 300


def resource_for_url(url: str) -> str:
    path = urlparse(url).path
    if path == "/search/repositories":
        return "search"
    if path == "/search/code":
        return "code_search"
    return "core"


class ThrottledError(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"local limiter denied the request; retry after {retry_after}s")
        self.retry_after = retry_after


class PartialResultsError(Exception):
    def __init__(self, response: httpx.Response) -> None:
        request_url = str(response.request.url) if response.request is not None else ""
        super().__init__(
            f"GitHub returned SAML SSO partial results for {request_url} "
            f"(status {response.status_code})"
        )
        self.status_code = response.status_code
        self.url = request_url


def _sso_partial_results(headers: httpx.Headers) -> bool:
    value = headers.get("x-github-sso")
    return value is not None and "partial-results" in str(value).lower()


def _error_fields(response: httpx.Response) -> tuple[str | None, str]:
    payload = None
    try:
        payload = response.json()
    except Exception:
        payload = None
    error_code: str | None = None
    message = ""
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            code = errors[0].get("code")
            if code is not None:
                error_code = str(code)
        raw_message = payload.get("message")
        if isinstance(raw_message, str):
            message = raw_message
    if not message:
        message = response.text[:_BODY_FALLBACK_CHARS]
    return error_code, message


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    json_body: dict | None = None,
    extra_headers: Mapping[str, str] | None = None,
    auth: bool = True,
    limiter: BucketLimiter | None = None,
    token_id: str | None = None,
    max_attempts: int = 5,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] | None = None,
    on_response: Callable[[httpx.Response, float], None] | None = None,
) -> httpx.Response:
    resource = resource_for_url(url)
    request_kwargs: dict = {}
    if json_body is not None:
        request_kwargs["json"] = json_body
    if extra_headers is not None:
        request_kwargs["headers"] = dict(extra_headers)
    denials = 0
    attempt = 0
    while True:
        if limiter is not None:
            acquired = limiter.acquire(resource, token_id, now=now())
            if not acquired.allowed:
                denials += 1
                if denials >= max_attempts:
                    raise ThrottledError(acquired.retry_after)
                sleep(acquired.retry_after)
                continue
            denials = 0
        try:
            started = now()
            if auth:
                response = client.request(method, url, **request_kwargs)
            else:
                request = client.build_request(method, url, **request_kwargs)
                request.headers.pop("Authorization", None)
                response = client.send(request)
            latency_ms = (now() - started) * 1000.0
            if on_response is not None:
                on_response(response, latency_ms)
            if limiter is not None:
                limiter.update_from_headers(resource, token_id, response.headers, now=now())
            if _sso_partial_results(response.headers):
                raise PartialResultsError(response)
            if response.status_code not in _TRIAGE_STATUSES:
                return response
            error_code, message = _error_fields(response)
            extra = {} if jitter is None else {"jitter": jitter}
            decision = classify(
                response.status_code,
                response.headers,
                attempt=attempt,
                error_code=error_code,
                message=message,
                now=now(),
                **extra,
            )
        finally:
            if limiter is not None:
                limiter.release(resource, token_id)
        if decision.action in _RETRYABLE_ACTIONS:
            attempt += 1
            if attempt >= max_attempts:
                return response
            sleep(decision.sleep_seconds)
            continue
        return response
