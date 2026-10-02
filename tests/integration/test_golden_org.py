from __future__ import annotations

import os
import time
from collections.abc import Iterator

import httpx
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

from discover.pipeline import Deps, run_org_enum
from discover.search_shards import iter_shard_pages
from discover.since_scan import iter_since_pages
from lib.gh_client import build_headers, token_fingerprint
from lib.qualify import validate
from limiter.buckets import BucketLimiter

GOLDEN_ORG = "github"
SEARCH_QUERY = "org:github fork:true"
SKEW_RETRY_SECONDS = 5.0

pytestmark = pytest.mark.skipif(
    not os.environ.get("GITHUB_TOKEN"),
    reason="GITHUB_TOKEN is not set; skipping the live golden-org parity run",
)


def _redis_client():
    url = os.environ.get("REDIS_URL")
    if url:
        import redis

        return redis.Redis.from_url(url)
    import fakeredis

    return fakeredis.FakeRedis()


def _stored_github_repos(engine: Engine) -> tuple[set[int], list[str]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT r.id, r.full_name FROM repos r JOIN owners o ON o.id = r.owner_id "
                "WHERE o.login = 'github' AND r.deleted_at IS NULL"
            )
        ).all()
    return {int(row[0]) for row in rows}, [str(row[1]) for row in rows]


def _search_ids(deps: Deps) -> set[int]:
    found: set[int] = set()
    for page in iter_shard_pages(
        deps.client,
        SEARCH_QUERY,
        limiter=deps.limiter,
        token_id=deps.token_fp,
        per_page=100,
        max_pages=10,
    ):
        found.update(item["id"] for item in page.items if isinstance(item.get("id"), int))
    return found


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture(scope="module")
def deps(alembic_engine: Engine, schema) -> Iterator[Deps]:
    token = os.environ["GITHUB_TOKEN"]
    redis_client = _redis_client()
    with alembic_engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE owners, repos, full_name_history, audit_log "
                "RESTART IDENTITY CASCADE"
            )
        )
    client = httpx.Client(headers=build_headers(token), timeout=30.0)
    yield Deps(
        client=client,
        engine=alembic_engine,
        redis=redis_client,
        limiter=BucketLimiter(redis_client),
        token_fp=token_fingerprint(token),
    )
    client.close()


@pytest.fixture(scope="module")
def golden(deps: Deps) -> dict:
    stats = run_org_enum(deps, org=GOLDEN_ORG, repo_type="all")
    ids, full_names = _stored_github_repos(deps.engine)
    return {"stats": stats, "ids": ids, "full_names": full_names}


def test_golden_org_parity_live(deps: Deps, golden: dict) -> None:
    stored_ids = golden["ids"]
    assert stored_ids, "org enumeration stored no github-owned repositories"
    search_ids = _search_ids(deps)
    if stored_ids != search_ids:
        time.sleep(SKEW_RETRY_SECONDS)
        search_ids = _search_ids(deps)
    print(
        f"golden-org parity: A={len(stored_ids)} B={len(search_ids)} "
        f"org_stats_pages={golden['stats'].pages} org_stats_fetched={golden['stats'].fetched}"
    )
    assert stored_ids == search_ids, (
        f"parity mismatch: A={len(stored_ids)} B={len(search_ids)} "
        f"only_in_A={sorted(stored_ids - search_ids)[:20]} "
        f"only_in_B={sorted(search_ids - stored_ids)[:20]}"
    )
    assert all(name.startswith("github/") for name in golden["full_names"])


def test_updated_qualifier_is_rejected_with_pushed_hint() -> None:
    result = validate("updated:>2024-01-01")
    assert result.ok is False
    assert any("pushed:" in hint for hint in result.hints)


def test_since_page_returns_ids_after_cursor(deps: Deps, golden: dict) -> None:
    stored_ids = golden["ids"]
    assert stored_ids
    since = min(stored_ids) - 1
    pages = list(
        iter_since_pages(
            deps.client,
            since=since,
            max_pages=1,
            limiter=deps.limiter,
            token_id=deps.token_fp,
        )
    )
    assert pages
    page = pages[0]
    print(f"since sanity: since={since} page_items={len(page.items)}")
    assert page.items, "first since page is empty"
    assert all(item["id"] > since for item in page.items)
