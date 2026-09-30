import dataclasses

import pytest

from limiter.classifier import Action, Decision, classify


def no_jitter():
    return 0.0


def quarter_jitter():
    return 0.25


def test_action_values_are_exact():
    assert {action.value for action in Action} == {
        "free",
        "retry_after",
        "wait_reset",
        "backoff",
        "shard",
        "fix",
        "fail_loud",
    }


@pytest.mark.parametrize("status", [200, 201, 204, 299])
def test_success_statuses_are_free(status):
    decision = classify(status, {}, now=0.0)
    assert decision.action is Action.FREE
    assert decision.sleep_seconds is None


def test_not_modified_is_free():
    decision = classify(304, {}, now=0.0)
    assert decision.action is Action.FREE
    assert decision.sleep_seconds is None


def test_unauthorized_fails_loud():
    decision = classify(401, {}, now=0.0)
    assert decision.action is Action.FAIL_LOUD
    assert decision.sleep_seconds is None


def test_sso_partial_results_fails_loud_even_on_success():
    headers = {"x-github-sso": "required; partial-results; sso-url=https://example.com"}
    decision = classify(200, headers, now=0.0)
    assert decision.action is Action.FAIL_LOUD


def test_sso_partial_results_detection_is_case_insensitive():
    decision = classify(200, {"X-GitHub-SSO": "Partial-Results"}, now=0.0)
    assert decision.action is Action.FAIL_LOUD


def test_sso_header_without_partial_results_is_free():
    headers = {"x-github-sso": "required; sso-url=https://example.com"}
    decision = classify(200, headers, now=0.0)
    assert decision.action is Action.FREE


def test_forbidden_with_retry_after_is_honored_exactly():
    decision = classify(403, {"retry-after": "30"}, now=1000.0)
    assert decision.action is Action.RETRY_AFTER
    assert decision.sleep_seconds == 30.0


def test_rate_limited_with_retry_after_is_honored_exactly():
    decision = classify(429, {"retry-after": "120.5"}, now=1000.0)
    assert decision.action is Action.RETRY_AFTER
    assert decision.sleep_seconds == 120.5


def test_retry_after_zero_falls_through_to_backoff():
    decision = classify(403, {"retry-after": "0"}, now=1000.0, jitter=no_jitter)
    assert decision.action is Action.BACKOFF
    assert decision.sleep_seconds == 60.0


def test_non_numeric_retry_after_is_ignored():
    decision = classify(403, {"retry-after": "soon"}, now=1000.0, jitter=no_jitter)
    assert decision.action is Action.BACKOFF


def test_non_numeric_retry_after_falls_back_to_reset():
    headers = {"retry-after": "later", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "1300"}
    decision = classify(403, headers, now=1000.0)
    assert decision.action is Action.WAIT_RESET
    assert decision.sleep_seconds == 300.0


def test_forbidden_with_exhausted_quota_waits_for_reset():
    headers = {
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": "1300",
        "x-ratelimit-resource": "search",
    }
    decision = classify(403, headers, now=1000.0)
    assert decision.action is Action.WAIT_RESET
    assert decision.sleep_seconds == 300.0
    assert decision.resource == "search"


def test_wait_reset_in_the_past_sleeps_zero():
    headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "900"}
    decision = classify(403, headers, now=1000.0)
    assert decision.action is Action.WAIT_RESET
    assert decision.sleep_seconds == 0.0


def test_forbidden_with_remaining_quota_backs_off():
    headers = {"x-ratelimit-remaining": "7", "x-ratelimit-reset": "1300"}
    decision = classify(403, headers, now=1000.0, jitter=no_jitter)
    assert decision.action is Action.BACKOFF
    assert decision.sleep_seconds == 60.0


def test_reset_must_parse_for_wait_reset():
    headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "nope"}
    decision = classify(403, headers, now=1000.0, jitter=no_jitter)
    assert decision.action is Action.BACKOFF


@pytest.mark.parametrize(
    ("attempt", "sleep"),
    [(0, 60.0), (1, 120.0), (2, 240.0), (3, 480.0), (4, 480.0)],
)
def test_backoff_is_exponential_capped_at_480(attempt, sleep):
    decision = classify(500, {}, now=0.0, attempt=attempt, jitter=no_jitter)
    assert decision.action is Action.BACKOFF
    assert decision.sleep_seconds == sleep


def test_backoff_adds_injected_jitter():
    decision = classify(500, {}, now=0.0, attempt=1, jitter=quarter_jitter)
    assert decision.sleep_seconds == 120.25


@pytest.mark.parametrize("status", [403, 429, 500, 502, 503, 504])
def test_attempt_five_fails_loud(status):
    decision = classify(status, {}, now=0.0, attempt=5, jitter=no_jitter)
    assert decision.action is Action.FAIL_LOUD
    assert decision.sleep_seconds is None
    assert decision.reason == "retries exhausted"


def test_attempt_four_still_backs_off():
    decision = classify(503, {}, now=0.0, attempt=4, jitter=no_jitter)
    assert decision.action is Action.BACKOFF
    assert decision.sleep_seconds == 480.0


def test_422_custom_error_is_spam_backoff():
    decision = classify(
        422, {}, error_code="custom", message="Secondary rate limit", now=0.0, jitter=no_jitter
    )
    assert decision.action is Action.BACKOFF
    assert decision.sleep_seconds == 60.0


def test_422_custom_error_takes_precedence_over_cap_message():
    decision = classify(
        422,
        {},
        error_code="custom",
        message="Only the first 1000 results",
        now=0.0,
        jitter=no_jitter,
    )
    assert decision.action is Action.BACKOFF


def test_422_custom_error_attempt_four_still_backs_off():
    decision = classify(
        422,
        {},
        error_code="custom",
        message="Secondary rate limit",
        now=0.0,
        attempt=4,
        jitter=no_jitter,
    )
    assert decision.action is Action.BACKOFF
    assert decision.sleep_seconds == 480.0


def test_422_custom_error_attempt_five_fails_loud():
    decision = classify(
        422,
        {},
        error_code="custom",
        message="Secondary rate limit",
        now=0.0,
        attempt=5,
        jitter=no_jitter,
    )
    assert decision.action is Action.FAIL_LOUD
    assert decision.sleep_seconds is None
    assert decision.reason == "retries exhausted"


def test_422_result_cap_shards():
    decision = classify(
        422, {}, message="Only the first 1000 search results are available", now=0.0
    )
    assert decision.action is Action.SHARD
    assert decision.sleep_seconds is None


def test_422_result_cap_detection_is_case_insensitive():
    decision = classify(422, {}, message="only THE FIRST 1000 results", now=0.0)
    assert decision.action is Action.SHARD


def test_422_validation_error_needs_fix():
    decision = classify(422, {}, error_code="invalid", message="Validation Failed", now=0.0)
    assert decision.action is Action.FIX
    assert decision.sleep_seconds is None


def test_server_error_backs_off():
    decision = classify(500, {}, now=0.0, jitter=no_jitter)
    assert decision.action is Action.BACKOFF
    assert decision.sleep_seconds == 60.0


def test_unexpected_status_needs_fix():
    decision = classify(418, {}, now=0.0)
    assert decision.action is Action.FIX


def test_not_found_needs_fix():
    decision = classify(404, {}, now=0.0)
    assert decision.action is Action.FIX


def test_resource_header_is_reported_for_retry_after():
    headers = {"retry-after": "1", "x-ratelimit-resource": "code_search"}
    decision = classify(429, headers, now=0.0)
    assert decision.resource == "code_search"


def test_resource_header_lookup_is_case_insensitive():
    decision = classify(403, {"X-RateLimit-Resource": "core"}, now=0.0)
    assert decision.resource == "core"


def test_resource_is_none_without_header():
    decision = classify(403, {}, now=0.0)
    assert decision.resource is None


def test_success_carries_resource_header():
    decision = classify(200, {"x-ratelimit-resource": "search"}, now=0.0)
    assert decision.resource == "search"


def test_decision_is_frozen():
    decision = classify(200, {}, now=0.0)
    assert decision == Decision(Action.FREE, None, None, "success")
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.action = Action.FIX
