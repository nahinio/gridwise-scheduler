"""The judge clone: organizer reference plans pass, every broken rule is named."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.schemas import HourPlan
from app.validator import replay_and_check
from tests.conftest import PUBLIC_CASES, PublicCase


def _rules(case: PublicCase, plan: list[HourPlan], **totals: float) -> set[str]:
    return {v.rule for v in replay_and_check(case.request, case.directives, plan, **totals)}


def test_reference_plans_are_valid(public_case: PublicCase) -> None:
    expected = public_case.expected
    violations = replay_and_check(
        public_case.request,
        public_case.directives,
        public_case.reference_plan,
        total_grid_kwh=expected["total_grid_kwh"],
        total_cost_bdt=expected["total_cost_bdt"],
        peak_grid_kwh=expected["peak_grid_kwh"],
        tol=1e-6,
    )
    assert violations == []


def _edit(hour: int, **changes: Any) -> Callable[[list[HourPlan]], list[HourPlan]]:
    def apply(plan: list[HourPlan]) -> list[HourPlan]:
        plan[hour] = plan[hour].model_copy(update=changes)
        return plan

    return apply


CASE = {c.id: c for c in PUBLIC_CASES}

# (case, mutation of the reference plan, rule that must be reported)
MUTATIONS: list[tuple[str, Callable[[list[HourPlan]], list[HourPlan]], str]] = [
    ("SAMPLE-01", lambda p: p[:-1], "plan_hours"),
    ("SAMPLE-01", lambda p: [p[1], p[0], *p[2:]], "plan_hours"),
    ("SAMPLE-01", _edit(0, grid_kwh=-5.0), "non_negative_finite"),
    ("SAMPLE-01", _edit(0, grid_kwh=float("nan")), "non_negative_finite"),
    ("SAMPLE-01", _edit(0, grid_kwh=90.02), "energy_balance"),
    ("SAMPLE-01", _edit(0, battery_kwh=5.0), "idle_nonzero"),
    # reduced solar in hour 12 is 45 kWh; using the full 180 forecast is overuse
    ("SAMPLE-01", _edit(12, solar_used_kwh=135.0, grid_kwh=0.0), "solar_overuse"),
    ("SAMPLE-01", _edit(2, battery_kwh=60.0, grid_kwh=140.0), "charge_rate"),
    ("SAMPLE-01", _edit(1, battery_kwh=60.0, grid_kwh=25.0), "discharge_rate"),
    ("SAMPLE-01", _edit(5, battery_energy_after_kwh=200.0), "battery_transition"),
    ("SAMPLE-01", _edit(23, battery_kwh=40.0, grid_kwh=145.0), "end_of_day_neutrality"),
    (
        "SAMPLE-02",
        _edit(3, battery_action="charge", battery_kwh=10.0, grid_kwh=100.0),
        "no_charge_window",
    ),
    (
        "SAMPLE-04",
        _edit(18, battery_action="discharge", battery_kwh=10.0, grid_kwh=205.0),
        "no_discharge_window",
    ),
    (
        "SAMPLE-03",
        _edit(20, battery_action="discharge", battery_kwh=20.0, grid_kwh=185.0),
        "minimum_battery_reserve",
    ),
    ("SAMPLE-05", _edit(18, battery_kwh=40.0, grid_kwh=165.0), "max_grid_window"),
]


@pytest.mark.parametrize(("case_id", "mutate", "rule"), MUTATIONS)
def test_mutations_are_caught(
    case_id: str, mutate: Callable[[list[HourPlan]], list[HourPlan]], rule: str
) -> None:
    case = CASE[case_id]
    assert rule in _rules(case, mutate(list(case.reference_plan)))


def test_battery_bounds_are_enforced() -> None:
    case = CASE["SAMPLE-01"]
    # Hour 5 is idle at full capacity (220); charging 10 more overshoots.
    over = _edit(5, battery_action="charge", battery_kwh=10.0, grid_kwh=105.0)
    assert "battery_capacity" in _rules(case, over(list(case.reference_plan)))
    # Hour 21 is idle at the base minimum (40); discharging 10 more undershoots.
    under = _edit(21, battery_action="discharge", battery_kwh=10.0, grid_kwh=165.0)
    assert "battery_minimum" in _rules(case, under(list(case.reference_plan)))


@pytest.mark.parametrize("field", ["total_grid_kwh", "total_cost_bdt", "peak_grid_kwh"])
def test_reported_totals_must_match_the_plan(field: str) -> None:
    case = CASE["SAMPLE-07"]
    totals = {k: case.expected[k] for k in ("total_grid_kwh", "total_cost_bdt", "peak_grid_kwh")}
    totals[field] += 1.0
    assert _rules(case, case.reference_plan, **totals) == {"totals_mismatch"}


def test_directive_the_plan_ignores_invalidates_it() -> None:
    """The judge replays with ITS directives: a plan built without them must fail."""
    unconstrained, constrained = CASE["SAMPLE-03"], CASE["SAMPLE-05"]
    # SAMPLE-03 and SAMPLE-05 share demand/solar/tariff but not the battery or directive,
    # so SAMPLE-03's plan replayed under SAMPLE-05's scenario cannot be valid.
    assert replay_and_check(
        constrained.request, constrained.directives, unconstrained.reference_plan
    )
