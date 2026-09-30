from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from discover.org_enum import EnumPage, iter_org_repos, iter_user_repos
from discover.search_shards import RequestFailed

NEXT_URL = "https://api.github.com/orgs/odd%20org/repos?type=sources&per_page=37&odd=keep%2Bme"


def enum_page(items, *, headers=None):
    return httpx.Response(200, json=items, headers=headers or {})


def client_from(responses, recorder=None):
    iterator = iter(responses)

    def handler(request):
        if recorder is not None:
            recorder.append(request)
        return next(iterator)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_org_first_request_url_and_default_params():
    captured = []
    client = client_from([enum_page([{"id": 1}])], captured)
    pages = list(iter_org_repos(client, "github"))
    parsed = urlparse(str(captured[0].url))
    assert parsed.path == "/orgs/github/repos"
    assert parse_qs(parsed.query) == {"type": ["all"], "per_page": ["100"]}
    assert pages[0].url == str(captured[0].url)
    assert pages[0].items == ({"id": 1},)
    assert pages[0].next_url is None


def test_user_first_request_url_and_owner_type_default():
    captured = []
    client = client_from([enum_page([{"id": 2}])], captured)
    pages = list(iter_user_repos(client, "octocat"))
    parsed = urlparse(str(captured[0].url))
    assert parsed.path == "/users/octocat/repos"
    assert parse_qs(parsed.query) == {"type": ["owner"], "per_page": ["100"]}
    assert pages[0].items == ({"id": 2},)


def test_odd_org_names_are_url_encoded_in_path():
    captured = []
    client = client_from([enum_page([])], captured)
    list(iter_org_repos(client, "my org/odd?name"))
    assert urlparse(str(captured[0].url)).path == "/orgs/my%20org%2Fodd%3Fname/repos"


def test_odd_user_names_are_url_encoded_in_path():
    captured = []
    client = client_from([enum_page([])], captured)
    list(iter_user_repos(client, "café/bob"))
    assert urlparse(str(captured[0].url)).path == "/users/caf%C3%A9%2Fbob/repos"


def test_org_repo_type_passthrough():
    captured = []
    client = client_from([enum_page([{"id": 1}])], captured)
    list(iter_org_repos(client, "github", repo_type="sources", per_page=25))
    parsed = urlparse(str(captured[0].url))
    assert parse_qs(parsed.query) == {"type": ["sources"], "per_page": ["25"]}


def test_user_repo_type_passthrough():
    captured = []
    client = client_from([enum_page([{"id": 1}])], captured)
    list(iter_user_repos(client, "octocat", repo_type="member"))
    assert parse_qs(urlparse(str(captured[0].url)).query) == {
        "type": ["member"],
        "per_page": ["100"],
    }


def test_link_next_url_is_followed_verbatim():
    captured = []
    responses = [
        enum_page([{"id": 1}], headers={"Link": f'<{NEXT_URL}>; rel="next"'}),
        enum_page([{"id": 2}]),
    ]
    client = client_from(responses, captured)
    pages = list(iter_org_repos(client, "github", max_pages=5))
    assert len(captured) == 2
    assert str(captured[1].url) == NEXT_URL
    assert pages[0].next_url == NEXT_URL
    assert pages[1].url == NEXT_URL
    assert pages[1].next_url is None


def test_max_pages_caps_requests_even_with_next_links():
    captured = []
    responses = [enum_page([{"id": 1}], headers={"Link": f'<{NEXT_URL}>; rel="next"'})] * 3
    client = client_from(responses, captured)
    pages = list(iter_user_repos(client, "octocat", max_pages=2))
    assert len(captured) == 2
    assert len(pages) == 2


def test_non_200_raises_request_failed():
    captured = []
    responses = [httpx.Response(404, json={"message": "Not Found"})]
    client = client_from(responses, captured)
    with pytest.raises(RequestFailed) as excinfo:
        list(iter_org_repos(client, "does-not-exist"))
    assert excinfo.value.status == 404
    assert excinfo.value.message == "Not Found"
    assert len(captured) == 1


def test_empty_org_yields_no_pages_and_makes_one_request():
    captured = []
    client = client_from([enum_page([])], captured)
    assert list(iter_org_repos(client, "empty-org")) == []
    assert len(captured) == 1


def test_page_items_are_returned_raw():
    raw = {"id": 3, "full_name": "o/r", "private": True}
    client = client_from([enum_page([raw])])
    pages = list(iter_user_repos(client, "o"))
    assert pages[0].items == (raw,)
    assert EnumPage.__dataclass_params__.frozen is True
