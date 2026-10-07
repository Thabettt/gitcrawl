from __future__ import annotations

import pytest

from lib.cancellation import RunCancelled, bind, check, reset


def test_check_raises_when_the_bound_callback_is_true():
    token = bind(lambda: True)
    try:
        with pytest.raises(RunCancelled):
            check()
    finally:
        reset(token)


def test_check_is_a_noop_without_a_binding_or_when_false():
    check()
    token = bind(lambda: False)
    try:
        check()
    finally:
        reset(token)
