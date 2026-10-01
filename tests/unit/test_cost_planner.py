from __future__ import annotations

import pytest

from enrich.cost_planner import (
    FIELD_COSTS,
    FilterPlan,
    PlanStep,
    order_filters,
    plan_enrichment,
)

D12_TABLE = {
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
    "min_commits": 4,
}


def test_field_costs_match_the_d12_priority_table():
    assert FIELD_COSTS == D12_TABLE


def test_plan_orders_cheap_first_and_maps_sources():
    plan = plan_enrichment(["has_dockerfile", "min_stars", "owner_country", "team_topic"])

    assert isinstance(plan, FilterPlan)
    assert plan.depth == "page"
    assert plan.steps == (
        PlanStep(field="min_stars", priority=0, source="record"),
        PlanStep(field="team_topic", priority=0, source="record"),
        PlanStep(field="owner_country", priority=1, source="mirror"),
        PlanStep(field="has_dockerfile", priority=2, source="single"),
    )


def test_plan_is_stable_within_a_priority():
    plan = plan_enrichment(["min_geo_confidence", "owner_country", "team_topic", "min_stars"])

    assert [step.field for step in plan.steps] == [
        "team_topic",
        "min_stars",
        "min_geo_confidence",
        "owner_country",
    ]


def test_plan_collapses_duplicates_keeping_the_first_occurrence():
    plan = plan_enrichment(["has_dockerfile", "min_stars", "has_dockerfile", "min_stars"])

    assert [step.field for step in plan.steps] == ["min_stars", "has_dockerfile"]


def test_plan_rejects_unknown_fields():
    with pytest.raises(ValueError, match="min_tabs"):
        plan_enrichment(["min_tabs"])


def test_plan_rejects_unknown_depth():
    with pytest.raises(ValueError, match="deep"):
        plan_enrichment(["min_stars"], depth="deep")


def test_page_depth_stops_after_single_call_fields():
    requested = ["funding", "coverage", "has_dockerfile", "min_geo_confidence", "min_stars"]

    plan = plan_enrichment(requested, depth="page")

    assert plan.depth == "page"
    assert [step.field for step in plan.steps] == [
        "min_stars",
        "min_geo_confidence",
        "has_dockerfile",
    ]


def test_full_depth_keeps_batched_and_deep_fields():
    requested = ["coverage", "funding", "has_dockerfile", "min_stars"]

    plan = plan_enrichment(requested, depth="full")

    assert plan.depth == "full"
    assert [step.field for step in plan.steps] == [
        "min_stars",
        "has_dockerfile",
        "funding",
        "coverage",
    ]


def test_order_filters_returns_names_in_page_plan_order():
    assert order_filters(["coverage", "team_topic", "has_dockerfile"]) == [
        "team_topic",
        "has_dockerfile",
    ]


def test_empty_request_yields_an_empty_plan():
    plan = plan_enrichment([])

    assert plan.steps == ()
    assert plan.depth == "page"
    assert order_filters([]) == []
