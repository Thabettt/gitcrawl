from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

from discover.since_scan import SincePage, iter_since_pages, save_checkpoint
from lib.gh_client import API_BASE, RequestFailed

NEXT_URL = "https://api.github.com/repositories?since=98765&per_page=100&odd=keep%2Bme"


def since_page(items, *, headers=None):
    return httpx.Response(200, json=list(items), headers=headers or {})


def client_from(responses, recorder=None):
    iterator = iter(responses)

    def handler(request):
        if recorder is not None:
            recorder.append(request)
        return next(iterator)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_first_request_uses_since_cursor_and_repositories_endpoint():
    captured = []
    client = client_from([since_page([{"id": 1}])], captured)
    pages = list(iter_since_pages(client, since=0))
    parsed = urlparse(str(captured[0].url))
    assert parsed.path == "/repositories"
    assert parse_qs(parsed.query) == {"since": ["0"], "per_page": ["100"]}
    assert pages[0].since == 0
    assert pages[0].items == ({"id": 1},)
    assert pages[0].max_id == 1


def test_dict_envelope_with_items_is_also_accepted():
    response = httpx.Response(200, json={"items": [{"id": 4}, {"id": 8}]})
    client = client_from([response])
    pages = list(iter_since_pages(client, since=0))
    assert pages[0].items == ({"id": 4}, {"id": 8})
    assert pages[0].max_id == 8


def test_link_next_url_is_followed_verbatim_and_next_since_is_parsed():
    captured = []
    responses = [
        since_page([{"id": 1}], headers={"Link": f'<{NEXT_URL}>; rel="next"'}),
        since_page([{"id": 2}]),
    ]
    client = client_from(responses, captured)
    pages = list(iter_since_pages(client, since=0, max_pages=5))
    assert len(captured) == 2
    assert str(captured[1].url) == NEXT_URL
    assert pages[0].next_since == 98765
    assert pages[1].since == 98765
    assert pages[1].next_since is None


def test_max_id_is_the_largest_item_id():
    client = client_from([since_page([{"id": 7}, {"id": 3}, {"id": 12}])])
    pages = list(iter_since_pages(client, since=100))
    assert pages[0].max_id == 12
    assert pages[0].since == 100


def test_empty_page_yields_page_with_none_max_id_and_stops():
    captured = []
    client = client_from([since_page([], headers={"Link": f'<{NEXT_URL}>; rel="next"'})], captured)
    pages = list(iter_since_pages(client, since=0))
    assert len(captured) == 1
    assert len(pages) == 1
    assert pages[0].items == ()
    assert pages[0].max_id is None


def test_missing_next_link_stops_pagination():
    captured = []
    client = client_from([since_page([{"id": 5}])], captured)
    pages = list(iter_since_pages(client, since=0))
    assert len(captured) == 1
    assert len(pages) == 1
    assert pages[0].next_since is None


def test_max_pages_caps_requests_even_with_next_links():
    captured = []
    responses = [since_page([{"id": 1}], headers={"Link": f'<{NEXT_URL}>; rel="next"'})] * 3
    client = client_from(responses, captured)
    pages = list(iter_since_pages(client, since=0, max_pages=2))
    assert len(captured) == 2
    assert len(pages) == 2


def test_stop_after_id_stops_on_the_page_that_crosses_the_boundary():
    captured = []
    responses = [
        since_page([{"id": 5}, {"id": 15}], headers={"Link": f'<{NEXT_URL}>; rel="next"'}),
        since_page([{"id": 20}]),
    ]
    client = client_from(responses, captured)
    pages = list(iter_since_pages(client, since=0, stop_after_id=10))
    assert len(captured) == 1
    assert pages[0].max_id == 15


def test_stop_after_id_continues_while_page_stays_inside_the_boundary():
    captured = []
    responses = [
        since_page([{"id": 5}], headers={"Link": f'<{NEXT_URL}>; rel="next"'}),
        since_page([{"id": 15}]),
    ]
    client = client_from(responses, captured)
    pages = list(iter_since_pages(client, since=0, stop_after_id=10, max_pages=10))
    assert len(captured) == 2
    assert [page.max_id for page in pages] == [5, 15]


def test_non_200_raises_request_failed_without_retry():
    captured = []
    responses = [httpx.Response(404, json={"message": "Not Found"})]
    client = client_from(responses, captured)
    with pytest.raises(RequestFailed) as excinfo:
        list(iter_since_pages(client, since=0))
    assert excinfo.value.status == 404
    assert excinfo.value.message == "Not Found"
    assert len(captured) == 1


def test_response_reaction_hook_receives_every_response():
    recorded = []
    client = client_from([since_page([{"id": 1}])])
    list(
        iter_since_pages(
            client,
            since=0,
            on_response=lambda response, latency_ms: recorded.append(response.status_code),
        )
    )
    assert recorded == [200]


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


def test_save_checkpoint_sets_since_max_and_updated_at(alembic_engine: Engine):
    with alembic_engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE shards RESTART IDENTITY CASCADE"))
        shard_id = connection.execute(
            text(
                "INSERT INTO shards (kind, updated_at) "
                "VALUES ('since-range', '2000-01-01T00:00:00Z') RETURNING id"
            )
        ).scalar_one()
    save_checkpoint(alembic_engine, shard_id, 4242)
    with alembic_engine.connect() as connection:
        row = (
            connection.execute(
                text("SELECT since_max, updated_at FROM shards WHERE id = :id"),
                {"id": shard_id},
            )
            .mappings()
            .one()
        )
    assert row["since_max"] == 4242
    assert row["updated_at"] > datetime(2000, 1, 1, tzinfo=UTC)
    assert API_BASE == "https://api.github.com"
    assert SincePage.__dataclass_params__.frozen is True
