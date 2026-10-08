import fakeredis
import pytest

from limiter.buckets import RESOURCE_SPECS, AcquireResult, BucketLimiter


@pytest.fixture
def redis():
    return fakeredis.FakeRedis()


@pytest.fixture
def limiter(redis):
    return BucketLimiter(redis)


def test_resource_specs_are_exact():
    assert RESOURCE_SPECS == {
        "search": (30, 60.0),
        "core": (5000, 3600.0),
        "code_search": (10, 60.0),
        "graphql": (5000, 3600.0),
    }


def test_first_acquire_is_allowed_without_retry_after(limiter):
    assert limiter.acquire("search", "token-a", now=100.0) == AcquireResult(True, None)


def window_limiter(redis, **kwargs):
    return BucketLimiter(redis, max_concurrent=100, **kwargs)


def test_search_allows_thirty_per_window_and_denies_the_next(redis):
    limiter = window_limiter(redis)
    results = [limiter.acquire("search", "token-a", now=10.0) for _ in range(30)]
    assert all(result.allowed for result in results)
    denied = limiter.acquire("search", "token-a", now=10.0)
    assert denied.allowed is False
    assert denied.retry_after == 50.0


def test_search_boundary_thirtieth_allowed_thirty_first_denied(redis):
    limiter = window_limiter(redis, specs={"search": (30, 60.0)})
    for _ in range(29):
        assert limiter.acquire("search", "token-a", now=0.0).allowed
    assert limiter.acquire("search", "token-a", now=0.0).allowed is True
    assert limiter.acquire("search", "token-a", now=0.0).allowed is False


def test_window_rollover_resets_count(redis):
    limiter = window_limiter(redis)
    for _ in range(30):
        limiter.acquire("search", "token-a", now=10.0)
    assert limiter.acquire("search", "token-a", now=10.0).allowed is False
    assert limiter.acquire("search", "token-a", now=60.0).allowed is True


def test_code_search_denies_after_ten_per_window(redis):
    limiter = window_limiter(redis)
    for _ in range(10):
        assert limiter.acquire("code_search", "token-a", now=0.0).allowed
    denied = limiter.acquire("code_search", "token-a", now=0.0)
    assert denied.allowed is False
    assert denied.retry_after == 60.0


def test_tokens_have_independent_buckets(redis):
    limiter = window_limiter(redis)
    for _ in range(30):
        limiter.acquire("search", "token-a", now=0.0)
    assert limiter.acquire("search", "token-a", now=0.0).allowed is False
    assert limiter.acquire("search", "token-b", now=0.0).allowed is True


def test_resources_have_independent_buckets(redis):
    limiter = window_limiter(redis)
    for _ in range(10):
        limiter.acquire("code_search", "token-a", now=0.0)
    assert limiter.acquire("code_search", "token-a", now=0.0).allowed is False
    assert limiter.acquire("search", "token-a", now=0.0).allowed is True


def test_pause_blocks_until_cleared(limiter):
    limiter.pause("search", "token-a", 30.0, now=100.0)
    assert limiter.paused_until("search", "token-a") == 130.0
    blocked = limiter.acquire("search", "token-a", now=110.0)
    assert blocked.allowed is False
    assert blocked.retry_after == 20.0
    assert limiter.acquire("search", "token-a", now=130.0).allowed is True


def test_paused_until_is_none_when_never_paused(limiter):
    assert limiter.paused_until("search", "token-a") is None


def test_paused_denial_does_not_consume_window(redis):
    limiter = BucketLimiter(redis)
    limiter.pause("search", "token-a", 10.0, now=0.0)
    assert limiter.acquire("search", "token-a", now=0.0).allowed is False
    assert limiter.acquire("search", "token-a", now=10.0).allowed is True
    assert redis.hget("gitcrawl:rl:search:token-a", "count") == b"1"


def test_update_from_headers_remaining_zero_pauses_until_reset(limiter):
    limiter.update_from_headers(
        "search",
        "token-a",
        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1234"},
        now=1000.0,
    )
    assert limiter.paused_until("search", "token-a") == 1234.0
    denied = limiter.acquire("search", "token-a", now=1000.0)
    assert denied.allowed is False
    assert denied.retry_after == 234.0


def test_update_from_headers_reconciles_window_count(redis):
    limiter = BucketLimiter(redis, specs={"core": (5, 60.0)})
    limiter.update_from_headers(
        "core",
        "token-a",
        {"x-ratelimit-remaining": "2", "x-ratelimit-reset": "60"},
        now=0.0,
    )
    assert limiter.acquire("core", "token-a", now=0.0).allowed is True
    assert limiter.acquire("core", "token-a", now=0.0).allowed is True
    assert limiter.acquire("core", "token-a", now=0.0).allowed is False


def test_update_from_headers_never_lowers_current_count(redis):
    limiter = BucketLimiter(redis, specs={"core": (5, 60.0)})
    for _ in range(4):
        assert limiter.acquire("core", "token-a", now=0.0).allowed
    limiter.update_from_headers(
        "core",
        "token-a",
        {"x-ratelimit-remaining": "2", "x-ratelimit-reset": "60"},
        now=0.0,
    )
    assert limiter.acquire("core", "token-a", now=0.0).allowed is True
    assert limiter.acquire("core", "token-a", now=0.0).allowed is False


def test_update_from_headers_ignores_malformed_values(redis, limiter):
    limiter.update_from_headers(
        "search",
        "token-a",
        {"x-ratelimit-remaining": "zero", "x-ratelimit-reset": "soon"},
        now=0.0,
    )
    assert limiter.paused_until("search", "token-a") is None
    assert redis.hget("gitcrawl:rl:search:token-a", "count") is None


def test_update_from_headers_ignores_missing_headers(limiter):
    limiter.update_from_headers("search", "token-a", {}, now=0.0)
    assert limiter.paused_until("search", "token-a") is None
    assert limiter.acquire("search", "token-a", now=0.0).allowed is True


def test_update_from_headers_remaining_zero_without_reset_does_not_pause(limiter):
    limiter.update_from_headers(
        "search",
        "token-a",
        {"x-ratelimit-remaining": "0"},
        now=0.0,
    )
    assert limiter.paused_until("search", "token-a") is None


def test_concurrency_cap_blocks_eleventh_slot(limiter):
    for _ in range(10):
        assert limiter.acquire("core", "token-a", now=0.0).allowed
    denied = limiter.acquire("core", "token-a", now=0.0)
    assert denied.allowed is False
    assert denied.retry_after == 0.5


def test_release_frees_a_concurrency_slot(limiter):
    for _ in range(10):
        limiter.acquire("core", "token-a", now=0.0)
    limiter.release("core", "token-a")
    assert limiter.acquire("core", "token-a", now=0.0).allowed is True


def test_custom_max_concurrent(redis):
    limiter = BucketLimiter(redis, max_concurrent=2)
    assert limiter.acquire("search", "token-a", now=0.0).allowed is True
    assert limiter.acquire("search", "token-a", now=0.0).allowed is True
    assert limiter.acquire("search", "token-a", now=0.0).allowed is False


def test_bound_concurrency_raises_the_slot_ceiling_for_the_body(redis):
    limiter = BucketLimiter(redis, max_concurrent=2)
    with limiter.bound_concurrency(5):
        assert limiter.max_concurrent == 5
        results = [limiter.acquire("core", "token-a", now=0.0) for _ in range(5)]
        assert all(result.allowed for result in results)
    assert limiter.max_concurrent == 2
    assert limiter.acquire("core", "token-b", now=0.0).allowed is True
    assert limiter.acquire("core", "token-b", now=0.0).allowed is True
    assert limiter.acquire("core", "token-b", now=0.0).allowed is False


def test_bound_concurrency_restores_across_nested_and_repeated_bodies(redis):
    limiter = BucketLimiter(redis, max_concurrent=2)
    with limiter.bound_concurrency(5):
        assert limiter.max_concurrent == 5
        with limiter.bound_concurrency(8):
            assert limiter.max_concurrent == 8
        assert limiter.max_concurrent == 5
        with limiter.bound_concurrency(3):
            assert limiter.max_concurrent == 5
    assert limiter.max_concurrent == 2
    with limiter.bound_concurrency(5):
        assert limiter.max_concurrent == 5
    assert limiter.max_concurrent == 2


def test_bound_concurrency_never_lowers_the_current_cap(redis):
    limiter = BucketLimiter(redis, max_concurrent=10)
    with limiter.bound_concurrency(4):
        assert limiter.max_concurrent == 10
    assert limiter.max_concurrent == 10


def test_bound_concurrency_restores_the_cap_after_an_exception(redis):
    limiter = BucketLimiter(redis, max_concurrent=2)
    with pytest.raises(RuntimeError):
        with limiter.bound_concurrency(5):
            raise RuntimeError("boom")
    assert limiter.max_concurrent == 2


def test_release_floors_at_zero(redis):
    limiter = BucketLimiter(redis)
    limiter.release("search", "token-a")
    limiter.release("search", "token-a")
    for _ in range(10):
        assert limiter.acquire("search", "token-a", now=0.0).allowed
    assert limiter.acquire("search", "token-a", now=0.0).allowed is False


def test_release_decrements_slot_field(redis):
    limiter = BucketLimiter(redis)
    limiter.acquire("search", "token-a", now=0.0)
    limiter.acquire("search", "token-a", now=0.0)
    limiter.release("search", "token-a")
    assert redis.hget("gitcrawl:rl:search:token-a", "slots") == b"1"


def test_concurrency_denial_does_not_consume_window(redis):
    limiter = BucketLimiter(redis, max_concurrent=1)
    assert limiter.acquire("search", "token-a", now=0.0).allowed is True
    assert limiter.acquire("search", "token-a", now=0.0).allowed is False
    assert redis.hget("gitcrawl:rl:search:token-a", "count") == b"1"


def test_acquire_uses_hash_key_with_ttl(redis, limiter):
    limiter.acquire("search", "token-a", now=0.0)
    key = "gitcrawl:rl:search:token-a"
    assert redis.type(key) == b"hash"
    assert redis.ttl(key) > 0
    assert redis.hget(key, "count") == b"1"
    assert redis.hget(key, "slots") == b"1"
    assert redis.hget(key, "window") == b"0"


def test_graphql_bucket_spec():
    from limiter.buckets import RESOURCE_SPECS

    assert RESOURCE_SPECS["graphql"] == (5000, 3600.0)
