from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Engine

from lib.graphql_batch import MAX_BATCH_SIZE
from store.models import AppSettings

SETTINGS_ENV: Mapping[str, str] = {
    "max_shards": "GITCRAWL_MAX_SHARDS",
    "max_candidates": "GITCRAWL_MAX_CANDIDATES",
    "max_hydrate": "GITCRAWL_MAX_HYDRATE",
    "max_enrich": "GITCRAWL_MAX_ENRICH",
    "request_deadline_seconds": "GITCRAWL_REQUEST_DEADLINE_SECONDS",
    "graphql_batch": "GITCRAWL_GRAPHQL_BATCH",
    "graphql_batch_size": "GITCRAWL_GRAPHQL_BATCH_SIZE",
    "limiter_max_concurrent": "GITCRAWL_MAX_CONCURRENT",
    "discovery_concurrency": "GITCRAWL_DISCOVERY_CONCURRENCY",
}

_BOOL_FIELDS = frozenset({"graphql_batch"})
_DISCOVERY_CONCURRENCY_MAX = 64


@dataclass(frozen=True)
class RunSettings:
    max_shards: int = 10
    max_candidates: int = 500
    max_hydrate: int = 200
    max_enrich: int = 100
    request_deadline_seconds: int = 3600
    graphql_batch: bool = True
    graphql_batch_size: int = 29
    limiter_max_concurrent: int = 10
    discovery_concurrency: int = 32

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _parse_env(field: str, raw: str) -> object | None:
    value = raw.strip()
    if not value:
        return None
    if field in _BOOL_FIELDS:
        lowered = value.lower()
        if lowered in {"1", "true", "on", "yes"}:
            return True
        if lowered in {"0", "false", "off", "no"}:
            return False
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _env_overrides() -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for field, variable in SETTINGS_ENV.items():
        raw = os.environ.get(variable)
        if raw is None:
            continue
        parsed = _parse_env(field, raw)
        if parsed is not None:
            overrides[field] = parsed
    return overrides


def env_pinned_fields() -> frozenset[str]:
    return frozenset(
        field for field, variable in SETTINGS_ENV.items() if os.environ.get(variable) is not None
    )


def load_run_settings(engine: Engine) -> RunSettings:
    with engine.connect() as connection:
        row = (
            connection.execute(select(AppSettings).where(AppSettings.id == 1))
            .mappings()
            .one_or_none()
        )
    values = dict(row) if row is not None else {}
    values.pop("id", None)
    values.pop("updated_at", None)
    fields = RunSettings.__dataclass_fields__
    valid = {field: values[field] for field in fields if field in values}
    settings = RunSettings(**valid)
    overrides = _env_overrides()
    resolved = RunSettings(**{**settings.as_dict(), **overrides}) if overrides else settings
    if resolved.graphql_batch_size > MAX_BATCH_SIZE:
        # Env overrides bypass form bounds; a batch size above the API cap would
        # make every GraphQL call raise. Clamp instead of failing every run.
        resolved = replace(resolved, graphql_batch_size=MAX_BATCH_SIZE)
    clamped = min(_DISCOVERY_CONCURRENCY_MAX, max(1, resolved.discovery_concurrency))
    if clamped != resolved.discovery_concurrency:
        resolved = replace(resolved, discovery_concurrency=clamped)
    return resolved


def update_run_settings(engine: Engine, values: Mapping[str, object]) -> RunSettings:
    unknown = set(values) - set(RunSettings.__dataclass_fields__)
    if unknown:
        raise KeyError(sorted(unknown)[0])
    with engine.begin() as connection:
        result = connection.execute(
            update(AppSettings).where(AppSettings.id == 1).values(**values, updated_at=func.now())
        )
        if not result.rowcount:
            connection.execute(insert(AppSettings).values(id=1, **values))
    return load_run_settings(engine)
