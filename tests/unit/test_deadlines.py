from __future__ import annotations

from types import SimpleNamespace

import fakeredis
import pytest

from lib.deadlines import (
    DEFAULT_CLONE_TIMEOUT_SECONDS,
    DEFAULT_REQUEST_DEADLINE_SECONDS,
    Deadline,
    DeadlineExceededError,
    clone_timeout_seconds,
    request_deadline_seconds,
)
from limiter.buckets import BucketLimiter
from serve import runner as runner_module


def test_defaults_are_generous():
    assert DEFAULT_REQUEST_DEADLINE_SECONDS == 3600.0
    assert DEFAULT_CLONE_TIMEOUT_SECONDS == 1800.0


def test_env_overrides_are_read(monkeypatch):
    monkeypatch.setenv("GITCRAWL_REQUEST_DEADLINE_SECONDS", "120")
    monkeypatch.setenv("GITCRAWL_CLONE_TIMEOUT_SECONDS", "60")
    assert request_deadline_seconds() == 120.0
    assert clone_timeout_seconds() == 60.0


@pytest.mark.parametrize("raw", ["", "abc", "0", "-5"])
def test_invalid_env_values_fall_back_to_defaults(monkeypatch, raw):
    monkeypatch.setenv("GITCRAWL_REQUEST_DEADLINE_SECONDS", raw)
    assert request_deadline_seconds() == DEFAULT_REQUEST_DEADLINE_SECONDS


def test_remaining_uses_the_injected_clock():
    now = [0.0]
    deadline = Deadline(10.0, clock=lambda: now[0])
    assert deadline.remaining == pytest.approx(10.0)
    now[0] = 9.5
    assert deadline.remaining == pytest.approx(0.5)


def test_bound_wait_raises_only_when_the_wait_exceeds_the_deadline():
    now = [0.0]
    deadline = Deadline(10.0, clock=lambda: now[0])
    assert deadline.bound_wait(10.0) is None
    with pytest.raises(DeadlineExceededError) as excinfo:
        deadline.bound_wait(10.001)
    assert excinfo.value.retry_after == 10.001
    now[0] = 10.0
    with pytest.raises(DeadlineExceededError):
        deadline.bound_wait(0.001)


def test_run_filter_binds_a_deadline_for_the_run_and_clears_it(monkeypatch):
    limiter = BucketLimiter(fakeredis.FakeRedis(), specs={"core": (1, 3600.0)})
    deps = SimpleNamespace(limiter=limiter, audit_buffer=None)
    seen: dict[str, object] = {}

    def fake_inner(deps, spec, *, config=None, queue_prefix=None):
        seen["during"] = limiter.deadline
        return "payload"

    monkeypatch.setattr(runner_module, "_run_filter", fake_inner)

    assert runner_module.run_filter(deps, object()) == "payload"
    assert isinstance(seen["during"], Deadline)
    assert limiter.deadline is None
