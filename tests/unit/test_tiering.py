from __future__ import annotations

from scheduler.tiering import order_repos


def repo(name, *, stargazers=None, pushed_at=None):
    item = {"full_name": name}
    if stargazers is not None:
        item["stargazers"] = stargazers
    if pushed_at is not None:
        item["pushed_at"] = pushed_at
    return item


def test_order_repos_sorts_by_stars_then_pushed_at():
    a = repo("a", stargazers=10, pushed_at="2024-01-01T00:00:00Z")
    b = repo("b", stargazers=10, pushed_at="2024-06-01T00:00:00Z")
    c = repo("c", stargazers=5, pushed_at="2024-06-01T00:00:00Z")
    assert order_repos([a, b, c]) == [b, a, c]


def test_order_repos_puts_missing_fields_last():
    complete = repo("complete", stargazers=1, pushed_at="2024-01-01T00:00:00Z")
    no_pushed = repo("no-pushed", stargazers=1)
    no_stars = repo("no-stars", pushed_at="2024-07-01T00:00:00Z")
    empty = repo("empty")
    assert order_repos([no_pushed, empty, no_stars, complete]) == [
        complete,
        no_pushed,
        no_stars,
        empty,
    ]


def test_order_repos_is_stable_for_full_ties():
    first = repo("first", stargazers=3, pushed_at="2024-01-01T00:00:00Z")
    second = repo("second", stargazers=3, pushed_at="2024-01-01T00:00:00Z")
    assert order_repos([first, second]) == [first, second]


def test_order_repos_falls_back_to_stargazers_count():
    legacy = repo("legacy", stargazers=10, pushed_at="2024-01-01T00:00:00Z")
    raw = {"full_name": "raw", "stargazers_count": 20, "pushed_at": "2024-01-01T00:00:00Z"}
    assert order_repos([legacy, raw]) == [raw, legacy]


def test_order_repos_prefers_stargazers_over_stargazers_count():
    preferred = repo("preferred", stargazers=100, pushed_at="2024-01-01T00:00:00Z")
    preferred["stargazers_count"] = 1
    fallback = repo("fallback", stargazers=50, pushed_at="2024-01-01T00:00:00Z")
    fallback["stargazers_count"] = 1
    assert order_repos([fallback, preferred]) == [preferred, fallback]
