from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlparse

import httpx
from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from discover import pipeline
from discover.pipeline import Deps
from discover.search_shards import RequestFailed
from enrich.geo_resolver import GeoResult, resolve_owner
from enrich.trees_first import fetch_tree
from hydrate.tail import refresh_repos
from lib.gh_client import (
    API_BASE,
    PartialResultsError,
    ThrottledError,
    create_client,
    load_tokens,
    request_with_retry,
    token_fingerprint,
)
from limiter.buckets import BucketLimiter
from scheduler.tiering import order_repos
from serve import audit
from serve.executor import Runner, RunPayload, RunPayloadItem
from serve.filter_spec import FilterSpec, parse_filter_spec, spec_to_query
from serve.virtual_params import GEO_CONFIDENCE_ORDER
from store.models import Owner, Repo

_INT_PARAMS = ("page", "per_page", "since")
_R44_VIRTUALS = ("min_commits", "min_loc")
_DOCKERFILE_PATH = "Dockerfile"


@dataclass
class RunnerConfig:
    max_shards: int = 10
    max_candidates: int = 500
    max_hydrate: int = 200
    max_enrich: int = 100


def _redis_or_fake():
    url = os.environ.get("REDIS_URL")
    if url:
        try:
            import redis as redis_module

            client = redis_module.Redis.from_url(url)
            client.ping()
            return client
        except Exception:
            pass
    try:
        import fakeredis

        return fakeredis.FakeRedis()
    except ImportError:
        return None


def build_deps(
    engine: Engine,
    *,
    token: str | None = None,
    redis_client=None,
    client: httpx.Client | None = None,
    config: RunnerConfig | None = None,
) -> Deps:
    if token is None:
        tokens = load_tokens()
        if not tokens:
            raise ValueError("no GitHub token configured; set GITHUB_TOKEN or GITHUB_TOKENS")
        token = tokens[0]
    if redis_client is None:
        redis_client = _redis_or_fake()
    return Deps(
        client=client if client is not None else create_client(token),
        engine=engine,
        redis=redis_client,
        limiter=BucketLimiter(redis_client) if redis_client is not None else None,
        token_id=token_fingerprint(token),
    )


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _audit_hook(deps: Deps) -> Callable[[httpx.Response, float], None]:
    def hook(response: httpx.Response, latency_ms: float) -> None:
        request = response.request
        params: dict[str, object] = {}
        if request is not None:
            params = dict(parse_qsl(urlparse(str(request.url)).query))
            for key in _INT_PARAMS:
                raw = params.get(key)
                if isinstance(raw, str) and raw.isdigit():
                    params[key] = int(raw)
        record = audit.record_from_response(
            params,
            response,
            token_fp=deps.token_id,
            latency_ms=latency_ms,
        )
        audit.record_audit(deps.engine, record)

    return hook


def _load_rows(engine: Engine, repo_ids: list[int]) -> dict[int, dict]:
    if not repo_ids:
        return {}
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                Repo.id,
                Repo.full_name,
                Repo.owner_id,
                Repo.stargazers,
                Repo.pushed_at,
                Repo.archived,
                Repo.language,
                Repo.license_spdx,
                Repo.topics,
                Repo.default_branch,
                Repo.deleted_at,
                Owner.login.label("owner_login"),
                Owner.location_raw.label("owner_location_raw"),
                Owner.country_iso.label("owner_country_iso"),
                Owner.geo_confidence.label("owner_geo_confidence"),
            )
            .join(Owner, Owner.id == Repo.owner_id)
            .where(Repo.id.in_(repo_ids))
        ).mappings()
        result: dict[int, dict] = {}
        for row in rows:
            if row["deleted_at"] is not None:
                continue
            view = dict(row)
            view["pushed_at"] = _iso(row["pushed_at"])
            view["country_iso"] = row["owner_country_iso"]
            view["geo_confidence"] = row["owner_geo_confidence"]
            result[row["id"]] = view
        return result


def _ordered_rows(engine: Engine, repo_ids: list[int]) -> list[dict]:
    return order_repos(list(_load_rows(engine, repo_ids).values()))


def _hydrate(
    deps: Deps,
    rows: list[dict],
    cap: int,
    hook: Callable[[httpx.Response, float], None],
) -> None:
    names = [row["full_name"] for row in rows[:cap]]
    if not names:
        return
    refresh_repos(
        deps.engine,
        deps.client,
        names,
        limiter=deps.limiter,
        token_id=deps.token_id,
        on_response=hook,
    )


def _apply_db_filters(rows: list[dict], virtual: dict) -> list[dict]:
    min_stars = virtual.get("min_stars")
    team_topic = virtual.get("team_topic")
    kept: list[dict] = []
    for row in rows:
        if isinstance(min_stars, int) and (row["stargazers"] or 0) < min_stars:
            continue
        if isinstance(team_topic, str):
            topics = [topic.casefold() for topic in _topics(row)]
            if team_topic.casefold() not in topics:
                continue
        kept.append(row)
    return kept


def _topics(row: dict) -> list[str]:
    topics = row.get("topics")
    if isinstance(topics, list):
        return [topic for topic in topics if isinstance(topic, str)]
    return []


def _load_owners(engine: Engine, owner_ids: list[int]) -> dict[int, dict]:
    if not owner_ids:
        return {}
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                Owner.id,
                Owner.login,
                Owner.location_raw,
                Owner.country_iso,
                Owner.geo_confidence,
            ).where(Owner.id.in_(owner_ids))
        ).mappings()
        return {row["id"]: dict(row) for row in rows}


def _fetch_owner_location(
    deps: Deps,
    login: str,
    hook: Callable[[httpx.Response, float], None],
) -> tuple[bool, str | None]:
    response = request_with_retry(
        deps.client,
        "GET",
        f"{API_BASE}/users/{login}",
        limiter=deps.limiter,
        token_id=deps.token_id,
        on_response=hook,
    )
    if response.status_code != 200:
        return False, None
    try:
        payload = response.json()
    except ValueError:
        return False, None
    if not isinstance(payload, dict):
        return True, None
    location = payload.get("location")
    if isinstance(location, str) and location.strip():
        return True, location
    return True, None


def _store_owner_geo(
    engine: Engine,
    owner_id: int,
    *,
    location_raw: str | None,
    result: GeoResult | None,
) -> None:
    confidence = result.confidence if result is not None else "unmatched"
    country_iso = result.country_iso if result is not None else None
    with engine.begin() as connection:
        connection.execute(
            update(Owner)
            .where(Owner.id == owner_id)
            .values(
                location_raw=location_raw,
                country_iso=country_iso,
                geo_confidence=confidence,
            )
        )


def _apply_geo(
    deps: Deps,
    rows: list[dict],
    virtual: dict,
    budget: int,
    hook: Callable[[httpx.Response, float], None],
) -> tuple[list[dict], int]:
    country = virtual.get("owner_country")
    threshold = virtual.get("min_geo_confidence")
    if country is None and threshold is None:
        return rows, 0
    owner_ids: list[int] = []
    for row in rows:
        if row["owner_id"] not in owner_ids:
            owner_ids.append(row["owner_id"])
    owners = _load_owners(deps.engine, owner_ids)
    used = 0
    for owner_id in owner_ids:
        owner = owners.get(owner_id)
        if owner is None:
            continue
        if owner["country_iso"] is not None or owner["geo_confidence"] == "unmatched":
            continue
        if owner["location_raw"] is None:
            if used >= budget:
                continue
            used += 1
            ok, location = _fetch_owner_location(deps, owner["login"], hook)
            if not ok:
                continue
            if location is None:
                _store_owner_geo(deps.engine, owner_id, location_raw=None, result=None)
                owner["geo_confidence"] = "unmatched"
                continue
            owner["location_raw"] = location
        result = resolve_owner(deps.engine, owner["location_raw"])
        _store_owner_geo(deps.engine, owner_id, location_raw=owner["location_raw"], result=result)
        owner["country_iso"] = result.country_iso
        owner["geo_confidence"] = result.confidence
    threshold_index = None
    if isinstance(threshold, str) and threshold in GEO_CONFIDENCE_ORDER:
        threshold_index = GEO_CONFIDENCE_ORDER.index(threshold)
    kept: list[dict] = []
    for row in rows:
        owner = owners.get(row["owner_id"])
        if owner is None:
            continue
        if country is not None and owner["country_iso"] != country:
            continue
        if threshold_index is not None:
            confidence = owner["geo_confidence"]
            if confidence not in GEO_CONFIDENCE_ORDER:
                continue
            if GEO_CONFIDENCE_ORDER.index(confidence) > threshold_index:
                continue
        row["country_iso"] = owner["country_iso"]
        row["geo_confidence"] = owner["geo_confidence"]
        kept.append(row)
    return kept, used


def _apply_dockerfile(
    deps: Deps,
    rows: list[dict],
    wanted: object,
    budget: int,
    hook: Callable[[httpx.Response, float], None],
) -> list[dict]:
    if not isinstance(wanted, bool):
        return rows
    kept: list[dict] = []
    remaining = budget
    for row in rows:
        if remaining <= 0:
            continue
        try:
            presence = fetch_tree(
                deps.client,
                row["full_name"],
                ref=row["default_branch"] or None,
                limiter=deps.limiter,
                token_id=deps.token_id,
                on_response=hook,
            )
        except (RequestFailed, ThrottledError, PartialResultsError):
            continue
        remaining -= 1
        has_dockerfile = presence.has(_DOCKERFILE_PATH)
        row["has_dockerfile"] = has_dockerfile
        if has_dockerfile == wanted:
            kept.append(row)
    return kept


def _payload_item(row: dict, virtual: dict) -> RunPayloadItem:
    badges = dict(virtual)
    if "has_dockerfile" in row:
        badges["has_dockerfile"] = row["has_dockerfile"]
    return RunPayloadItem(
        repo_id=row["id"],
        full_name=row["full_name"],
        stargazers=row["stargazers"],
        pushed_at=row["pushed_at"],
        archived=row["archived"],
        language=row["language"],
        license_spdx=row["license_spdx"],
        country_iso=row.get("country_iso"),
        geo_confidence=row.get("geo_confidence"),
        virtuals=badges,
    )


def _r44_warnings(virtual: dict) -> list[str]:
    return [
        f"`{name}` is recorded but unenforceable in this run; results are incomplete"
        for name in _R44_VIRTUALS
        if name in virtual
    ]


def run_filter(deps: Deps, spec: FilterSpec, *, config: RunnerConfig | None = None) -> RunPayload:
    cfg = config or RunnerConfig()
    warnings = _r44_warnings(dict(spec.virtual))
    query = spec_to_query(spec)
    total_count = pipeline.count_total(deps, query)
    stats = pipeline.run_search_discovery(
        deps,
        query,
        max_shards=cfg.max_shards,
        max_pages=spec.max_pages,
    )
    hook = _audit_hook(deps)
    candidates = _ordered_rows(deps.engine, list(stats.repo_ids))[: cfg.max_candidates]
    _hydrate(deps, candidates, cfg.max_hydrate, hook)
    rows = _ordered_rows(deps.engine, [row["id"] for row in candidates])
    rows = _apply_db_filters(rows, dict(spec.virtual))
    rows, used = _apply_geo(deps, rows, dict(spec.virtual), cfg.max_enrich, hook)
    rows = _apply_dockerfile(
        deps, rows, spec.virtual.get("has_dockerfile"), cfg.max_enrich - used, hook
    )
    items = [_payload_item(row, dict(spec.virtual)) for row in rows]
    return RunPayload(
        total_count=total_count,
        fetched=stats.fetched,
        incomplete=bool(warnings),
        warnings=warnings,
        items=items,
    )


def make_runner(deps: Deps, *, config: RunnerConfig | None = None) -> Runner:
    cfg = config or RunnerConfig()

    def runner(run_id: int, filter_spec: dict) -> RunPayload:
        return run_filter(deps, parse_filter_spec(filter_spec), config=cfg)

    return runner
