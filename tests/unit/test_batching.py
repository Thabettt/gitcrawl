from __future__ import annotations

import pytest

from lib.batching import chunked


def test_chunked_splits_exactly_and_leaves_a_remainder():
    assert [list(batch) for batch in chunked([1, 2, 3, 4, 5], 2)] == [[1, 2], [3, 4], [5]]


def test_chunked_single_batch_when_smaller_than_size():
    assert [list(batch) for batch in chunked([1, 2], 10)] == [[1, 2]]


def test_chunked_rejects_non_positive_size():
    with pytest.raises(ValueError):
        list(chunked([1], 0))
