from __future__ import annotations

from discover.pipeline import _collect_ids


class AppendOnlyList(list):
    def __init__(self) -> None:
        super().__init__()
        self.appends = 0

    def append(self, value: object) -> None:
        self.appends += 1
        super().append(value)

    def extend(self, values: object) -> None:
        raise AssertionError("extend would recopy the accumulated ids")

    def __iadd__(self, values: object) -> None:
        raise AssertionError("in-place concatenation would recopy the accumulated ids")


def test_collect_ids_appends_in_order_and_deduplicates():
    seen: set[int] = set()
    collected: list[int] = []
    _collect_ids(seen, collected, [{"id": 3}, {"id": 1}, {"id": 3}, {"nope": 1}, {"id": True}])
    assert collected == [3, 1]
    assert seen == {3, 1}


def test_collect_ids_appends_once_per_new_id():
    seen: set[int] = set()
    collected = AppendOnlyList()
    items = [{"id": value} for value in range(100_000)]
    _collect_ids(seen, collected, items)
    assert len(collected) == 100_000
    assert collected.appends == 100_000
    assert seen == set(range(100_000))
