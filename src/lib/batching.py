from __future__ import annotations

from collections.abc import Iterator, Sequence


def chunked[T](values: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    if size < 1:
        raise ValueError("size must be >= 1")
    for start in range(0, len(values), size):
        yield values[start : start + size]
