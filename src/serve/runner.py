from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlparse

import httpx
from sqlalchemy import bindparam, select, update
from sqlalchemy.engine import Engine

from discover import pipeline
from discover.pipeline import Deps
from discover.search_shards import RequestFailed
from enrich.cost_planner import plan_enrichment
from enrich.geo_resolver import GeoCache, _cache_key, resolve_many
from enrich.segment_executor import execute_segments
from enrich.trees_first import fetch_tree
from hydrate.tail import refresh_repos
from lib import audit
from lib.batching import chunked
from lib.deadlines import Deadline, request_deadline_seconds
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
from serve.executor import Runner, RunPayload, RunPayloadItem
from serve.filter_spec import FilterSpec, parse_filter_spec, spec_to_query
from serve.virtual_params import GEO_CONFIDENCE_ORDER
from store.models import Owner, Repo

_INT_PARAMS = ("page", "per_page", "since")
_R44_VIRTUALS = ("min_commits", "min_loc")
_DOCKERFILE_PATH = "Dockerfile"
_ID_BATCH = 5000


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
        token_fp=token_fingerprint(token),
        audit_buffer=audit.AuditBuffer(engine),
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
            token_fp=deps.token_fp,
            latency_ms=latency_ms,
        )
        if deps.audit_buffer is not None:
            deps.audit_buffer.add(record)
        else:
            audit.record_audit(deps.engine, record)

    return hook


def _load_rows(
    engine: Engine, repo_ids: list[int], *, batch_size: int = _ID_BATCH
) -> dict[int, dict]:
    if not repo_ids:
        return {}
    result: dict[int, dict] = {}
    with engine.connect() as connection:
        for batch in chunked(repo_ids, batch_size):
            rows = connection.execute(
                select(
                    Repo.id,
                    Repo.full_name,
                    Repo.owner_id,
                    Repo.stargazers,
                    Repo.forks_count,
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
                .where(Repo.id.in_(batch))
            ).mappings()
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
        token_id=deps.token_fp,
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
    try:
        response = request_with_retry(
            deps.client,
            "GET",
            f"{API_BASE}/users/{login}",
            limiter=deps.limiter,
            token_id=deps.token_fp,
            on_response=hook,
        )
    except PartialResultsError:
        return False, None
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


def _apply_geo(
    deps: Deps,
    rows: list[dict],
    virtual: dict,
    budget: int,
    hook: Callable[[httpx.Response, float], None],
) -> tuple[list[dict], int, int]:
    country = virtual.get("owner_country")
    threshold = virtual.get("min_geo_confidence")
    if country is None and threshold is None:
        return rows, 0, 0
    owner_ids = list(dict.fromkeys(row["owner_id"] for row in rows))
    owners = _load_owners(deps.engine, owner_ids)
    cache = GeoCache(deps.engine)
    used = 0
    pending: list[tuple[int, str | None]] = []
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
            owner["location_raw"] = location
        pending.append((owner_id, owner["location_raw"]))
    resolved = resolve_many(deps.engine, [raw for _, raw in pending], cache=cache)
    updates: list[dict] = []
    for owner_id, raw in pending:
        result = resolved.get(_cache_key(raw))
        confidence = result.confidence if result is not None else "unmatched"
        country_iso = result.country_iso if result is not None else None
        updates.append(
            {
                "owner_id": owner_id,
                "location_raw": raw,
                "country_iso": country_iso,
                "geo_confidence": confidence,
            }
        )
        owner = owners[owner_id]
        owner["country_iso"] = country_iso
        owner["geo_confidence"] = confidence
    if updates:
        statement = (
            update(Owner)
            .where(Owner.id == bindparam("owner_id"))
            .values(
                location_raw=bindparam("location_raw"),
                country_iso=bindparam("country_iso"),
                geo_confidence=bindparam("geo_confidence"),
            )
        )
        with deps.engine.begin() as connection:
            connection.execute(statement, updates)
    threshold_index = None
    if isinstance(threshold, str) and threshold in GEO_CONFIDENCE_ORDER:
        threshold_index = GEO_CONFIDENCE_ORDER.index(threshold)
    kept: list[dict] = []
    skipped = 0
    for row in rows:
        owner = owners.get(row["owner_id"])
        if owner is None:
            skipped += 1
            continue
        if country is not None and owner["country_iso"] is None:
            skipped += 1
            continue
        if country is not None and owner["country_iso"] != country:
            continue
        if threshold_index is not None:
            confidence = owner["geo_confidence"]
            if confidence not in GEO_CONFIDENCE_ORDER:
                skipped += 1
                continue
            if GEO_CONFIDENCE_ORDER.index(confidence) > threshold_index:
                continue
        row["country_iso"] = owner["country_iso"]
        row["geo_confidence"] = owner["geo_confidence"]
        kept.append(row)
    return kept, used, skipped


def _apply_dockerfile(
    deps: Deps,
    rows: list[dict],
    wanted: object,
    budget: int,
    hook: Callable[[httpx.Response, float], None],
) -> tuple[list[dict], int, int]:
    if not isinstance(wanted, bool):
        return rows, 0, 0
    kept: list[dict] = []
    skipped = 0
    used = 0
    remaining = budget
    for row in rows:
        if remaining <= 0:
            skipped += 1
            continue
        try:
            presence = fetch_tree(
                deps.client,
                row["full_name"],
                ref=row["default_branch"] or None,
                limiter=deps.limiter,
                token_id=deps.token_fp,
                on_response=hook,
            )
        except (RequestFailed, ThrottledError, PartialResultsError):
            skipped += 1
            continue
        remaining -= 1
        used += 1
        has_dockerfile = presence.has(_DOCKERFILE_PATH)
        row["has_dockerfile"] = has_dockerfile
        if has_dockerfile == wanted:
            kept.append(row)
    return kept, skipped, used


def _record_handler(rows_by_id: dict[int, dict], field: str, virtual: dict):
    value = virtual[field]

    def handler(ids):
        kept = _apply_db_filters([rows_by_id[repo_id] for repo_id in ids], {field: value})
        return [row["id"] for row in kept], 0

    return handler


def _geo_handler(deps, rows_by_id, virtual, budget, hook, skipped):
    def handler(ids):
        rows = [rows_by_id[repo_id] for repo_id in ids]
        kept, used, geo_skipped = _apply_geo(deps, rows, virtual, budget["remaining"], hook)
        budget["remaining"] = max(0, budget["remaining"] - used)
        skipped["geo"] += geo_skipped
        return [row["id"] for row in kept], used

    return handler


def _dockerfile_handler(deps, rows_by_id, wanted, budget, hook, skipped):
    def handler(ids):
        rows = [rows_by_id[repo_id] for repo_id in ids]
        kept, dockerfile_skipped, used = _apply_dockerfile(
            deps, rows, wanted, budget["remaining"], hook
        )
        budget["remaining"] = max(0, budget["remaining"] - used)
        skipped["dockerfile"] += dockerfile_skipped
        return [row["id"] for row in kept], used

    return handler


def _enrich_handlers(
    deps: Deps,
    rows: list[dict],
    virtual: dict,
    budget: dict,
    hook: Callable[[httpx.Response, float], None],
    skipped: dict,
    *,
    depth: str = "page",
) -> tuple[dict, list[str]]:
    rows_by_id = {row["id"]: row for row in rows}
    handlers: dict = {}
    unsupported: list[str] = []
    geo_claimed = False
    for step in plan_enrichment(list(virtual), depth=depth).steps:
        if step.field in ("min_stars", "team_topic"):
            handlers[step.field] = _record_handler(rows_by_id, step.field, virtual)
        elif step.field in ("owner_country", "min_geo_confidence"):
            if not geo_claimed:
                handlers[step.field] = _geo_handler(
                    deps, rows_by_id, virtual, budget, hook, skipped
                )
                geo_claimed = True
        elif step.field == "has_dockerfile":
            handlers[step.field] = _dockerfile_handler(
                deps, rows_by_id, virtual.get("has_dockerfile"), budget, hook, skipped
            )
        else:
            unsupported.append(step.field)
    return handlers, unsupported


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


def _as_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _sort_key(item: dict, sort: str | None) -> tuple:
    if sort == "forks":
        primary: object = _as_number(item.get("forks_count"))
    elif sort == "updated":
        primary = item.get("pushed_at") or ""
    else:
        primary = _as_number(item.get("stargazers"))
    return (primary, item.get("pushed_at") or "", _as_number(item.get("id")))


def apply_sort(items: list[dict], sort: str | None, order: str | None) -> list[dict]:
    if not items or sort == "help-wanted-issues":
        return items
    return sorted(items, key=lambda item: _sort_key(item, sort), reverse=order != "asc")


def run_filter(deps: Deps, spec: FilterSpec, *, config: RunnerConfig | None = None) -> RunPayload:
    if deps.limiter is not None:
        deps.limiter.bind_deadline(Deadline(request_deadline_seconds()))
    try:
        return _run_filter(deps, spec, config=config)
    finally:
        if deps.limiter is not None:
            deps.limiter.bind_deadline(None)
        if deps.audit_buffer is not None:
            deps.audit_buffer.flush()


def _run_filter(deps: Deps, spec: FilterSpec, *, config: RunnerConfig | None = None) -> RunPayload:
    cfg = config or RunnerConfig()
    virtual = dict(spec.virtual)
    warnings = _r44_warnings(virtual)
    query = spec_to_query(spec)
    total_count = pipeline.count_total(deps, query)
    stats = pipeline.run_search_discovery(
        deps,
        query,
        max_shards=cfg.max_shards,
        max_pages=spec.max_pages,
        total_count=total_count,
    )
    if stats.incomplete_shards > 0:
        warnings.append(
            f"{stats.incomplete_shards} discovery shard(s) incomplete; results are partial"
        )
    if stats.page_capped_shards > 0:
        warnings.append(
            f"{stats.page_capped_shards} discovery shard(s) hit the page cap "
            f"(max_pages={spec.max_pages}) with more pages available; results are incomplete"
        )
    if stats.plan_capped:
        warnings.append(
            f"discovery shard plan stopped at max_shards={cfg.max_shards}; results are incomplete"
        )
    hook = _audit_hook(deps)
    ordered = _ordered_rows(deps.engine, list(stats.repo_ids))
    dropped_candidates = max(0, len(ordered) - cfg.max_candidates)
    if dropped_candidates > 0:
        warnings.append(
            f"{dropped_candidates} candidate(s) dropped by max_candidates="
            f"{cfg.max_candidates}; results are incomplete"
        )
    candidates = ordered[: cfg.max_candidates]
    dropped_hydration = max(0, len(candidates) - cfg.max_hydrate)
    if dropped_hydration > 0:
        warnings.append(
            f"{dropped_hydration} repo(s) not hydrated due to max_hydrate="
            f"{cfg.max_hydrate}; results are incomplete"
        )
    _hydrate(deps, candidates, cfg.max_hydrate, hook)
    rows = _ordered_rows(deps.engine, [row["id"] for row in candidates])
    budget = {"remaining": cfg.max_enrich}
    skipped = {"geo": 0, "dockerfile": 0}
    handlers, unsupported = _enrich_handlers(deps, rows, virtual, budget, hook, skipped)
    for field in unsupported:
        warnings.append(
            f"`{field}` requires full-depth enrichment, which is not wired yet; "
            "results are incomplete"
        )
    survivors, segment_stats = execute_segments([row["id"] for row in rows], handlers, segments=1)
    surviving = set(survivors)
    rows = [row for row in rows if row["id"] in surviving]
    warnings.extend(segment_stats.warnings)
    if skipped["geo"] > 0:
        warnings.append(
            f"{skipped['geo']} repo(s) skipped because owner country could not be resolved; "
            "results are incomplete"
        )
    if skipped["dockerfile"] > 0:
        warnings.append(
            f"{skipped['dockerfile']} repo(s) skipped because Dockerfile presence could not be "
            "checked; results are incomplete"
        )
    if spec.sort == "help-wanted-issues":
        warnings.append(
            "sort=help-wanted-issues has no local data; survivor order kept and results are "
            "incomplete"
        )
    rows = apply_sort(rows, spec.sort, spec.order)
    items = [_payload_item(row, virtual) for row in rows]
    return RunPayload(
        total_count=total_count,
        fetched=stats.fetched,
        incomplete=bool(warnings) or bool(segment_stats.warnings) or stats.incomplete_shards > 0,
        warnings=warnings,
        items=items,
        field_stats=asdict(segment_stats),
        updated=stats.updated,
        unchanged=stats.unchanged,
        skipped=stats.skipped,
    )


def make_runner(deps: Deps, *, config: RunnerConfig | None = None) -> Runner:
    cfg = config or RunnerConfig()

    def runner(run_id: int, filter_spec: dict) -> RunPayload:
        return run_filter(deps, parse_filter_spec(filter_spec), config=cfg)

    return runner
