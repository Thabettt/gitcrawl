import fakeredis
import pytest

from limiter.retry import RetryQueue


@pytest.fixture
def redis():
    return fakeredis.FakeRedis()


@pytest.fixture
def queue(redis):
    return RetryQueue(redis)


def test_schedule_then_pop_due_returns_payload(queue):
    queue.schedule("payload-a", due_at=10.0)
    assert queue.pop_due(now=9.999) == []
    assert queue.pop_due(now=10.0) == ["payload-a"]


def test_pop_due_orders_by_due_at(queue):
    queue.schedule("later", 30.0)
    queue.schedule("sooner", 10.0)
    queue.schedule("middle", 20.0)
    assert queue.pop_due(now=25.0) == ["sooner", "middle"]
    assert queue.size() == 1
    assert queue.pop_due(now=30.0) == ["later"]


def test_pop_due_respects_limit(queue):
    for index in range(5):
        queue.schedule(f"item-{index}", 1.0 + index)
    assert queue.pop_due(now=100.0, limit=2) == ["item-0", "item-1"]
    assert queue.size() == 3


def test_pop_due_never_returns_same_payload_twice(queue):
    queue.schedule("only-once", 5.0)
    assert queue.pop_due(now=10.0) == ["only-once"]
    assert queue.pop_due(now=10.0) == []
    assert queue.size() == 0


def test_pop_due_returns_empty_when_nothing_is_due(queue):
    queue.schedule("future", 100.0)
    assert queue.pop_due(now=50.0) == []
    assert queue.size() == 1


def test_size_tracks_scheduled_items(queue):
    assert queue.size() == 0
    queue.schedule("a", 1.0)
    queue.schedule("b", 2.0)
    assert queue.size() == 2


def test_rescheduling_updates_due_at(queue):
    queue.schedule("job", 10.0)
    queue.schedule("job", 50.0)
    assert queue.size() == 1
    assert queue.pop_due(now=20.0) == []
    assert queue.pop_due(now=50.0) == ["job"]


def test_default_key_is_used(redis, queue):
    queue.schedule("a", 1.0)
    assert redis.zcard("gitcrawl:retry") == 1


def test_custom_key_is_used(redis):
    queue = RetryQueue(redis, key="custom:retry")
    queue.schedule("a", 1.0)
    assert redis.zcard("custom:retry") == 1
