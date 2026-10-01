from __future__ import annotations

import pytest

import skeleton


def test_filter_hash_is_stable():
    assert skeleton.filter_hash() == "2bc6b774968a"


def test_has_next_detects_only_the_next_link():
    assert skeleton.has_next('<https://api.github.com/x?page=2>; rel="next"') is True
    assert skeleton.has_next('<https://api.github.com/x?page=5>; rel="last"') is False
    assert skeleton.has_next(None) is False


def test_retry_delay_prefers_retry_after():
    assert skeleton.retry_delay({"Retry-After": "2.5"}) == 2.5
    assert skeleton.retry_delay({"Retry-After": "soon"}) == 0.0


def test_build_headers_sends_no_authorization_without_a_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    headers = skeleton.build_headers()

    assert "Authorization" not in headers
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["User-Agent"] == "gitcrawl/0.0-skeleton"


def test_validate_rejects_unknown_qualifiers(capsys):
    with pytest.raises(SystemExit) as excinfo:
        skeleton.validate("language:rust license:mit")

    assert excinfo.value.code == 2
    assert "invalid qualifier: license" in capsys.readouterr().err
