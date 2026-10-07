from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token

_current: ContextVar[Callable[[], bool] | None] = ContextVar("gitcrawl_cancel", default=None)


class RunCancelled(Exception):
    """Raised at cooperative checkpoints when the user aborts a run."""


def bind(cancel: Callable[[], bool] | None) -> Token:
    return _current.set(cancel)


def reset(token: Token) -> None:
    _current.reset(token)


def check() -> None:
    cancel = _current.get()
    if cancel is not None and cancel():
        raise RunCancelled("cancelled by user")
