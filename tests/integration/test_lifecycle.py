from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

from discover.search_shards import RequestFailed, iter_shard_pages
from store.upserts import upsert_repos


def payload(
    repo_id: int = 1,
    *,
    full_name: str = "octo/repo1",
    pushed_at: str = "2024-06-07T08:09:10Z",
    **overrides,
) -> dict:
    item = {
        "id": repo_id,
        "node_id": f"R_{repo_id}",
        "name": full_name.rsplit("/", 1)[-1],
        "full_name": full_name,
        "owner": {"id": repo_id * 10, "login": f"owner{repo_id}", "type": "User"},
        "private": False,
        "pushed_at": pushed_at,
        "updated_at": pushed_at,
    }
    item.update(overrides)
    return item


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(alembic_engine: Engine) -> Engine:
    with alembic_engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE owners, repos, full_name_history, shards, audit_log "
                "RESTART IDENTITY CASCADE"
            )
        )
    return alembic_engine


def seed(
    engine: Engine,
    full_name: str,
    *,
    repo_id: int = 1,
    etag: str | None = None,
    pushed_at: str = "2024-06-07T08:09:10Z",
) -> None:
    upsert_repos(engine, [payload(repo_id, full_name=full_name, pushed_at=pushed_at)])
    if etag is not None:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE repos SET etag = :etag WHERE id = :id"),
                {"etag": etag, "id": repo_id},
            )


def repo_row(engine: Engine, repo_id: int) -> dict | None:
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT id, full_name, etag, deleted_at, xmin::text AS xmin "
                    "FROM repos WHERE id = :id"
                ),
                {"id": repo_id},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row is not None else None


def repo_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.scalar(text("SELECT count(*) FROM repos")))


def repo_ids(engine: Engine) -> list[int]:
    with engine.connect() as connection:
        return [int(row[0]) for row in connection.execute(text("SELECT id FROM repos ORDER BY id"))]


def history_names(engine: Engine, repo_id: int) -> list[str]:
    with engine.connect() as connection:
        return [
            str(row[0])
            for row in connection.execute(
                text("SELECT full_name FROM full_name_history WHERE repo_id = :id ORDER BY id"),
                {"id": repo_id},
            )
        ]


def test_hydrate_repo_returns_payload_and_etag():
    from hydrate.repo_client import HydratedRepo, hydrate_repo

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json=payload(1, full_name="octo/one"), headers={"ETag": 'W/"one"'}
        )

    hydrated = hydrate_repo(mock_client(handler), "octo/one", now=lambda: 1000.0)
    assert hydrated == HydratedRepo(
        id=1,
        node_id="R_1",
        full_name="octo/one",
        payload=payload(1, full_name="octo/one"),
        etag='W/"one"',
        not_modified=False,
    )
    assert urlparse(str(requests[0].url)).path == "/repos/octo/one"
    assert "if-none-match" not in requests[0].headers


def test_hydrate_repo_sends_if_none_match_and_reports_304():
    from hydrate.repo_client import hydrate_repo

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(304, headers={"ETag": 'W/"one"'})

    hydrated = hydrate_repo(mock_client(handler), "octo/one", etag='W/"one"', now=lambda: 1000.0)
    assert hydrated.not_modified is True
    assert hydrated.payload is None
    assert hydrated.full_name == "octo/one"
    assert hydrated.etag == 'W/"one"'
    assert requests[0].headers["If-None-Match"] == 'W/"one"'


def test_hydrate_repo_304_without_response_etag_keeps_supplied_etag():
    from hydrate.repo_client import hydrate_repo

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304)

    hydrated = hydrate_repo(mock_client(handler), "octo/one", etag='W/"old"', now=lambda: 1000.0)
    assert hydrated.not_modified is True
    assert hydrated.etag == 'W/"old"'


def test_hydrate_repo_follows_301_manually_and_reports_current_name():
    from hydrate.repo_client import hydrate_repo

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if urlparse(str(request.url)).path == "/repos/octo/old":
            return httpx.Response(
                301, headers={"Location": "https://api.github.com/repos/octo/new"}
            )
        return httpx.Response(
            200, json=payload(1, full_name="octo/new"), headers={"ETag": 'W/"new"'}
        )

    hydrated = hydrate_repo(mock_client(handler), "octo/old", etag='W/"old"', now=lambda: 1000.0)
    assert hydrated.full_name == "octo/new"
    assert hydrated.id == 1
    assert hydrated.payload == payload(1, full_name="octo/new")
    assert [urlparse(str(request.url)).path for request in requests] == [
        "/repos/octo/old",
        "/repos/octo/new",
    ]
    assert requests[0].headers["If-None-Match"] == 'W/"old"'
    assert "if-none-match" not in requests[1].headers


def test_hydrate_repo_follows_relative_location():
    from hydrate.repo_client import hydrate_repo

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if urlparse(str(request.url)).path == "/repos/octo/old":
            return httpx.Response(302, headers={"Location": "/repos/octo/new"})
        return httpx.Response(200, json=payload(1, full_name="octo/new"))

    hydrated = hydrate_repo(mock_client(handler), "octo/old", now=lambda: 1000.0)
    assert hydrated.full_name == "octo/new"
    assert len(requests) == 2


def test_hydrate_repo_stops_after_three_redirects():
    from hydrate.repo_client import hydrate_repo

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "/repos/octo/loop"})

    with pytest.raises(RequestFailed) as excinfo:
        hydrate_repo(mock_client(handler), "octo/loop", now=lambda: 1000.0)
    assert excinfo.value.status == 302
    assert len(requests) == 4


def test_hydrate_repo_404_raises_repo_not_found():
    from hydrate.repo_client import RepoNotFound, hydrate_repo

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    with pytest.raises(RepoNotFound) as excinfo:
        hydrate_repo(mock_client(handler), "octo/gone", now=lambda: 1000.0)
    assert excinfo.value.full_name == "octo/gone"


def test_hydrate_repo_non_200_after_retries_raises_request_failed():
    from hydrate.repo_client import hydrate_repo

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, json={"message": "boom"})

    with pytest.raises(RequestFailed) as excinfo:
        hydrate_repo(
            mock_client(handler),
            "octo/boom",
            sleep=lambda _: None,
            now=lambda: 1000.0,
            jitter=lambda: 0.0,
        )
    assert excinfo.value.status == 500
    assert excinfo.value.message == "boom"
    assert len(requests) == 5


def test_hydrate_repo_malformed_payload_raises_request_failed():
    from hydrate.repo_client import hydrate_repo

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "a", "repo"])

    with pytest.raises(RequestFailed) as excinfo:
        hydrate_repo(mock_client(handler), "octo/one", now=lambda: 1000.0)
    assert excinfo.value.status == 200


def test_search_path_never_sends_if_none_match():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"total_count": 0, "items": [], "incomplete_results": False},
            headers={"x-ratelimit-resource": "search"},
        )

    assert list(iter_shard_pages(mock_client(handler), "language:python", now=lambda: 1000.0))
    assert requests
    assert all("if-none-match" not in request.headers for request in requests)


def test_apply_hydration_301_rename_keeps_one_row_and_records_history(clean: Engine):
    from hydrate.repo_client import hydrate_repo
    from store.lifecycle import LifecycleOutcome, apply_hydration

    seed(clean, "octo/old", repo_id=1, etag='W/"old"')

    def handler(request: httpx.Request) -> httpx.Response:
        if urlparse(str(request.url)).path == "/repos/octo/old":
            return httpx.Response(
                301, headers={"Location": "https://api.github.com/repos/octo/new"}
            )
        return httpx.Response(
            200, json=payload(1, full_name="octo/new"), headers={"ETag": 'W/"new"'}
        )

    hydrated = hydrate_repo(mock_client(handler), "octo/old", etag='W/"old"', now=lambda: 1000.0)
    outcome = apply_hydration(clean, "octo/old", hydrated)
    assert outcome == LifecycleOutcome(
        repo_id=1, renamed_from="octo/old", history_added=True, tombstoned=False
    )
    assert repo_count(clean) == 1
    row = repo_row(clean, 1)
    assert row is not None
    assert row["full_name"] == "octo/new"
    assert row["etag"] == 'W/"new"'
    assert set(history_names(clean, 1)) == {"octo/old"}


def test_apply_hydration_200_renamed_mismatch_records_history(clean: Engine):
    from hydrate.repo_client import hydrate_repo
    from store.lifecycle import LifecycleOutcome, apply_hydration

    seed(clean, "octo/old", repo_id=1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload(1, full_name="octo/new"))

    hydrated = hydrate_repo(mock_client(handler), "octo/old", now=lambda: 1000.0)
    outcome = apply_hydration(clean, "octo/old", hydrated)
    assert outcome == LifecycleOutcome(
        repo_id=1, renamed_from="octo/old", history_added=True, tombstoned=False
    )
    assert repo_count(clean) == 1
    assert repo_row(clean, 1)["full_name"] == "octo/new"
    assert set(history_names(clean, 1)) == {"octo/old"}


def test_apply_hydration_200_records_etag_without_rename(clean: Engine):
    from hydrate.repo_client import hydrate_repo
    from store.lifecycle import LifecycleOutcome, apply_hydration

    seed(clean, "octo/ok", repo_id=1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload(1, full_name="octo/ok"), headers={"ETag": 'W/"ok"'})

    hydrated = hydrate_repo(mock_client(handler), "octo/ok", now=lambda: 1000.0)
    outcome = apply_hydration(clean, "octo/ok", hydrated)
    assert outcome == LifecycleOutcome(
        repo_id=1, renamed_from=None, history_added=False, tombstoned=False
    )
    assert repo_row(clean, 1)["etag"] == 'W/"ok"'


def test_apply_hydration_304_leaves_the_row_unchanged(clean: Engine):
    from hydrate.repo_client import hydrate_repo
    from store.lifecycle import LifecycleOutcome, apply_hydration

    seed(clean, "octo/stale", repo_id=1, etag='W/"stale"')
    before = repo_row(clean, 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304)

    hydrated = hydrate_repo(
        mock_client(handler), "octo/stale", etag='W/"stale"', now=lambda: 1000.0
    )
    outcome = apply_hydration(clean, "octo/stale", hydrated)
    assert outcome == LifecycleOutcome(
        repo_id=1, renamed_from=None, history_added=False, tombstoned=False
    )
    assert repo_row(clean, 1) == before


def test_apply_hydration_404_tombstones_and_reports_outcome(clean: Engine):
    from store.lifecycle import LifecycleOutcome, apply_hydration

    seed(clean, "octo/gone", repo_id=9)
    outcome = apply_hydration(clean, "octo/gone", None, not_found=True)
    assert outcome == LifecycleOutcome(
        repo_id=9, renamed_from=None, history_added=False, tombstoned=True
    )
    assert repo_row(clean, 9)["deleted_at"] is not None


def test_tombstone_is_idempotent_and_unknown_name_returns_false(clean: Engine):
    from store.lifecycle import tombstone

    seed(clean, "octo/gone", repo_id=1)
    assert tombstone(clean, "octo/gone") is True
    assert tombstone(clean, "octo/gone") is False
    assert tombstone(clean, "octo/never") is False


def test_purge_tombstones_removes_only_rows_past_retention(clean: Engine):
    from store.lifecycle import purge_tombstones

    seed(clean, "octo/old-gone", repo_id=1)
    seed(clean, "octo/new-gone", repo_id=2)
    seed(clean, "octo/alive", repo_id=3)
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    with clean.begin() as connection:
        connection.execute(
            text("UPDATE repos SET deleted_at = :ts WHERE id = 1"),
            {"ts": now - timedelta(days=31)},
        )
        connection.execute(
            text("UPDATE repos SET deleted_at = :ts WHERE id = 2"),
            {"ts": now - timedelta(days=3)},
        )
    assert purge_tombstones(clean, retention_days=30, now=now) == 1
    assert repo_ids(clean) == [2, 3]


def test_purge_tombstones_keeps_rows_at_exactly_the_retention_boundary(clean: Engine):
    from store.lifecycle import purge_tombstones

    seed(clean, "octo/boundary", repo_id=1)
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    with clean.begin() as connection:
        connection.execute(
            text("UPDATE repos SET deleted_at = :ts WHERE id = 1"),
            {"ts": now - timedelta(days=30)},
        )
    assert purge_tombstones(clean, retention_days=30, now=now) == 0
    assert repo_ids(clean) == [1]


def test_refresh_repos_mixed_fixture_set(clean: Engine):
    from hydrate.tail import RefreshStats, refresh_repos

    seed(clean, "octo/ok", repo_id=1, etag='W/"ok"')
    seed(clean, "octo/ren", repo_id=2, etag='W/"ren"')
    seed(clean, "octo/stale", repo_id=3, etag='W/"stale"')
    seed(clean, "octo/gone", repo_id=4)
    seed(clean, "octo/boom", repo_id=5)
    before_stale = repo_row(clean, 3)

    def handler(request: httpx.Request) -> httpx.Response:
        path = urlparse(str(request.url)).path
        if path == "/repos/octo/ok":
            assert request.headers["If-None-Match"] == 'W/"ok"'
            return httpx.Response(
                200, json=payload(1, full_name="octo/ok"), headers={"ETag": 'W/"ok2"'}
            )
        if path == "/repos/octo/ren":
            assert request.headers["If-None-Match"] == 'W/"ren"'
            return httpx.Response(
                301, headers={"Location": "https://api.github.com/repos/octo/renamed"}
            )
        if path == "/repos/octo/renamed":
            return httpx.Response(200, json=payload(2, full_name="octo/renamed"))
        if path == "/repos/octo/stale":
            assert request.headers["If-None-Match"] == 'W/"stale"'
            return httpx.Response(304)
        if path == "/repos/octo/gone":
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(500, json={"message": "boom"})

    stats = refresh_repos(
        clean,
        mock_client(handler),
        ["octo/ok", "octo/ren", "octo/stale", "octo/gone", "octo/boom"],
        sleep=lambda _: None,
        now=lambda: 1000.0,
        jitter=lambda: 0.0,
    )
    assert stats == RefreshStats(refreshed=2, not_modified=1, renamed=1, tombstoned=1, failed=1)
    assert repo_count(clean) == 5
    assert repo_row(clean, 1)["etag"] == 'W/"ok2"'
    assert repo_row(clean, 2)["full_name"] == "octo/renamed"
    assert "octo/ren" in history_names(clean, 2)
    assert repo_row(clean, 3) == before_stale
    assert repo_row(clean, 4)["deleted_at"] is not None
