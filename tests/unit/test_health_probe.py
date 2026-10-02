from __future__ import annotations

import threading

from serve.pages import _Probe


def test_probe_returns_the_check_result():
    probe = _Probe(timeout=0.5)

    assert probe.run(lambda: True) is True


def test_probe_returns_false_when_the_check_raises():
    probe = _Probe(timeout=0.5)

    def boom():
        raise RuntimeError("down")

    assert probe.run(boom) is False


def test_hung_check_does_not_accumulate_threads():
    hang = threading.Event()
    probe = _Probe(timeout=0.05)
    before = threading.active_count()

    assert probe.run(lambda: hang.wait(30)) is False
    assert probe.run(lambda: hang.wait(30)) is False

    assert threading.active_count() - before <= 1
    hang.set()
