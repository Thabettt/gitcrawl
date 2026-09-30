import hashlib
import os

import httpx

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
