from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

FIELD_COSTS: Mapping[str, int] = {
    "min_stars": 0,
    "team_topic": 0,
    "owner_country": 1,
    "min_geo_confidence": 1,
    "has_dockerfile": 2,
    "funding": 3,
    "discussions": 3,
    "sponsors": 3,
    "coverage": 4,
    "ci": 4,
    "loc": 4,
    "min_loc": 4,
    "max_loc": 4,
    "min_commits": 2,
    "max_commits": 2,
}

_SOURCES = {
    0: "record",
    1: "mirror",
    2: "single",
    3: "batched",
    4: "deep",
}

_DEPTHS = ("page", "full")
_PAGE_MAX_PRIORITY = 2


@dataclass(frozen=True)
class PlanStep:
    field: str
    priority: int
    source: str


@dataclass(frozen=True)
class FilterPlan:
    steps: tuple[PlanStep, ...]
    depth: str


def plan_enrichment(requested: Sequence[str], *, depth: str = "page") -> FilterPlan:
    if depth not in _DEPTHS:
        raise ValueError(f"unknown plan depth `{depth}`; expected one of: {', '.join(_DEPTHS)}")
    steps: list[PlanStep] = []
    seen: set[str] = set()
    for field in requested:
        priority = FIELD_COSTS.get(field)
        if priority is None:
            raise ValueError(f"unknown enrichment field `{field}`")
        if field in seen:
            continue
        seen.add(field)
        if depth == "page" and priority > _PAGE_MAX_PRIORITY:
            continue
        steps.append(PlanStep(field=field, priority=priority, source=_SOURCES[priority]))
    steps.sort(key=lambda step: step.priority)
    return FilterPlan(steps=tuple(steps), depth=depth)


def order_filters(requested: Sequence[str]) -> list[str]:
    return [step.field for step in plan_enrichment(requested).steps]
