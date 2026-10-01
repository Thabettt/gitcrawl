from __future__ import annotations

import hashlib
import json

import pytest

from serve import executor
from serve.executor import RunPayload, RunPayloadItem


def test_run_payload_item_defaults_are_independent():
    first = RunPayloadItem(repo_id=1, full_name="octo/hello")
    second = RunPayloadItem(repo_id=2, full_name="octo/world")
    first.virtuals["has_dockerfile"] = True
    assert second.virtuals == {}
    assert first.stargazers is None
    assert first.pushed_at is None
    assert first.archived is None
    assert first.language is None
    assert first.license_spdx is None
    assert first.country_iso is None
    assert first.geo_confidence is None
    assert first.raw is None


def test_run_payload_defaults():
    payload = RunPayload(total_count=None, items=[])
    assert payload.incomplete is False
    assert payload.fetched is None


def test_filter_hash_is_canonical_across_key_order():
    left = executor._filter_hash({"q": "stars:>10", "sort": "stars", "order": "desc"})
    right = executor._filter_hash({"order": "desc", "sort": "stars", "q": "stars:>10"})
    expected = hashlib.sha256(
        json.dumps(
            {"q": "stars:>10", "sort": "stars", "order": "desc"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert left == right == expected


def test_default_runner_raises_runtime_error():
    with pytest.raises(RuntimeError, match="no runner configured"):
        executor._default_runner(1, {})


def test_error_message_is_class_prefixed_and_truncated():
    message = executor._error_message(ValueError("x" * 1000))
    assert message == ("ValueError: " + "x" * 1000)[:300]
    assert len(message) == 300
    assert message.startswith("ValueError: ")


def test_bundle_item_prefers_raw_and_falls_back_to_snapshot():
    raw = {"id": 1, "full_name": "octo/hello", "extra": True}
    assert executor._bundle_item(RunPayloadItem(repo_id=1, full_name="octo/hello", raw=raw)) == raw
    snapshot = executor._bundle_item(
        RunPayloadItem(
            repo_id=2,
            full_name="octo/world",
            stargazers=5,
            pushed_at="2026-09-30T12:00:00+00:00",
            archived=True,
            language="Go",
            license_spdx="Apache-2.0",
            country_iso="US",
            geo_confidence="gazetteer-city",
            virtuals={"team_topic": True},
        )
    )
    assert snapshot == {
        "repo_id": 2,
        "full_name": "octo/world",
        "stargazers": 5,
        "pushed_at": "2026-09-30T12:00:00+00:00",
        "archived": True,
        "language": "Go",
        "license_spdx": "Apache-2.0",
        "country_iso": "US",
        "geo_confidence": "gazetteer-city",
        "virtuals": {"team_topic": True},
    }
