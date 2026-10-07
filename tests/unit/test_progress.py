from __future__ import annotations

from lib.progress import bind, report, reset


def test_report_forwards_phase_counts_and_counters_to_the_bound_reporter():
    calls: list[tuple] = []

    def reporter(phase, done, total, counters):
        calls.append((phase, done, total, dict(counters)))

    token = bind(reporter)
    try:
        report("discovering", 3, 10, fetched=300)
    finally:
        reset(token)
    assert calls == [("discovering", 3, 10, {"fetched": 300})]


def test_report_without_a_binding_is_a_noop():
    report("discovering", 1, 2)
