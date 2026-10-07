from __future__ import annotations

from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token

Reporter = Callable[[str, int, int | None, Mapping[str, int]], None]

_current: ContextVar[Reporter | None] = ContextVar("gitcrawl_progress", default=None)


def bind(reporter: Reporter | None) -> Token:
    return _current.set(reporter)


def reset(token: Token) -> None:
    _current.reset(token)


def report(
    phase: str,
    done: int,
    total: int | None = None,
    **counters: int,
) -> None:
    reporter = _current.get()
    if reporter is not None:
        reporter(phase, done, total, counters)
