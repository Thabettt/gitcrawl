from __future__ import annotations

import fakeredis
import pytest

from serve import runner


def _unreachable(monkeypatch):
    def boom(*args, **kwargs):
        raise ConnectionError("connection refused")

    monkeypatch.setattr("redis.Redis.from_url", boom)


def test_unreachable_redis_logs_the_reason_and_falls_back(monkeypatch, caplog):
    _unreachable(monkeypatch)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6390/0")
    monkeypatch.delenv("GITCRAWL_REDIS_STRICT", raising=False)

    with caplog.at_level("WARNING", logger="gitcrawl.serve"):
        client = runner._redis_or_fake()

    assert isinstance(client, fakeredis.FakeRedis)
    assert "connection refused" in caplog.text


def test_strict_mode_refuses_the_fake_fallback(monkeypatch):
    _unreachable(monkeypatch)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6390/0")
    monkeypatch.setenv("GITCRAWL_REDIS_STRICT", "1")

    with pytest.raises(runner.RedisUnavailable):
        runner._redis_or_fake()


def test_no_redis_url_uses_the_fake_without_a_warning(monkeypatch, caplog):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("GITCRAWL_REDIS_STRICT", raising=False)

    with caplog.at_level("WARNING", logger="gitcrawl.serve"):
        client = runner._redis_or_fake()

    assert isinstance(client, fakeredis.FakeRedis)
    assert caplog.text == ""
