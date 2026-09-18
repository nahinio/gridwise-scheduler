"""Optimizer: provably optimal on the public pack, always valid on anything feasible."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.config import STRICT_TOLERANCE
from app.merge import merge
from app.optimizer import idle_plan, plan_schedule, solve
from app.schemas import (
    Directive,
    MaxGrid,
    MinReserve,
    NoCharge,
    NoDischarge,
    NoOp,
    OptimizeRequest,
    SolarReduction,
)
from app.validator import replay_and_check
from tests.conftest import PublicCase, make_request


def _strictly_valid(request: OptimizeRequest, directives: list[Directive], outcome: Any) -> None:
    result = outcome.result
    assert (
        replay_and_check(
            request,
            directives,
            result.plan,
            total_grid_kwh=result.total_grid_kwh,
            total_cost_bdt=result.total_cost_bdt,
            peak_grid_kwh=result.peak_grid_kwh,
            tol=STRICT_TOLERANCE,
        )
        == []
    )


def test_public_cases_match_the_organizer_optimum(public_case: PublicCase) -> None:
    outcome = plan_schedule(public_case.request, public_case.directives)
    assert outcome.fallback is None
    _strictly_valid(public_case.request, public_case.directives, outcome)
    assert outcome.result.total_cost_bdt == pytest.approx(
        public_case.expected["total_cost_bdt"], abs=0.01
    )


def test_plan_never_charges_and_discharges_pointlessly(public_case: PublicCase) -> None:
    """Tie-breaking keeps battery throughput no higher than the organizer's reference."""
    ours = sum(
        p.battery_kwh
        for p in plan_schedule(public_case.request, public_case.directives).result.plan
    )
    reference = sum(p.battery_kwh for p in public_case.reference_plan)
    assert ours <= reference + 1e-6


def test_dead_battery_stays_idle() -> None:
    request = make_request(capacity_kwh=80, initial_energy_kwh=80, minimum_energy_kwh=80)
    outcome = plan_schedule(request, [NoOp()])
    assert {p.battery_action for p in outcome.result.plan} == {"idle"}
    _strictly_valid(request, [NoOp()], outcome)


def test_battery_locked_all_day_equals_idle_cost() -> None:
    request = make_request()
    locked: list[Directive] = [NoCharge(hours=tuple(range(24)))]
    outcome = plan_schedule(request, locked)
    _strictly_valid(request, locked, outcome)
    assert outcome.result.total_cost_bdt == pytest.approx(
        idle_plan(request, merge(request, locked)).total_cost_bdt
    )


def test_binding_grid_cap_forces_precharging() -> None:
    request = make_request()
    cap: list[Directive] = [MaxGrid(hours=(18, 19, 20), max_grid_kwh=60)]
    outcome = plan_schedule(request, cap)
    assert outcome.fallback is None
    _strictly_valid(request, cap, outcome)
    assert all(outcome.result.plan[h].grid_kwh <= 60 + 1e-9 for h in (18, 19, 20))


def test_reserve_above_initial_energy_is_reached_by_charging() -> None:
    request = make_request()  # initial 100, charge rate 50
    reserve: list[Directive] = [MinReserve(hours=(1, 2, 3), minimum_energy_kwh=150)]
    outcome = plan_schedule(request, reserve)
    assert outcome.fallback is None
    _strictly_valid(request, reserve, outcome)
    assert outcome.result.plan[1].battery_energy_after_kwh >= 150


def test_zero_tariff_zero_demand_and_surplus_solar() -> None:
    data = make_request().model_dump()
    for h in range(24):
        data["hours"][h]["tariff_bdt_per_kwh"] = 0.0 if h < 12 else 9.0
    data["hours"][3]["demand_kwh"] = 0.0
    data["hours"][12]["solar_kwh"] = 500.0  # far more than demand: must be curtailed
    request = OptimizeRequest.model_validate(data)
    outcome = plan_schedule(request, [NoOp()])
    assert outcome.fallback is None
    _strictly_valid(request, [NoOp()], outcome)
    assert outcome.result.plan[12].grid_kwh == 0


def test_three_directives_on_overlapping_hours() -> None:
    request = make_request()
    directives: list[Directive] = [
        SolarReduction(hours=(11, 12, 13), factor=0.25),
        NoDischarge(hours=(12, 13, 18)),
        MinReserve(hours=(18, 19), minimum_energy_kwh=120),
    ]
    outcome = plan_schedule(request, directives)
    assert outcome.fallback is None
    _strictly_valid(request, directives, outcome)


def test_infeasible_interpretation_is_relaxed_not_fatal() -> None:
    """A grid cap of 0 with no battery headroom cannot be met: drop it, keep the rest."""
    request = make_request()
    directives: list[Directive] = [
        NoCharge(hours=(2, 3)),
        MaxGrid(hours=tuple(range(24)), max_grid_kwh=0),
    ]
    assert solve(request, merge(request, directives)) is None
    outcome = plan_schedule(request, directives)
    assert outcome.fallback == "relaxed"
    assert outcome.relaxed_note_indexes == [1]
    _strictly_valid(request, [directives[0]], outcome)


def test_relaxation_prefers_dropping_grid_caps_over_reserves() -> None:
    request = make_request()
    directives: list[Directive] = [
        MinReserve(hours=(0,), minimum_energy_kwh=200),  # unreachable: 100 + 50 < 200
        MaxGrid(hours=tuple(range(24)), max_grid_kwh=0),  # unreachable on its own too
    ]
    outcome = plan_schedule(request, directives)
    assert outcome.fallback == "relaxed"
    assert sorted(outcome.relaxed_note_indexes) == [0, 1]


scenario = st.builds(
    lambda demand, solar, tariff, capacity, fractions, rates: {
        "scenario_id": "HYP",
        "operator_notes": ["n/a"],
        "hours": [
            {
                "hour": h,
                "demand_kwh": demand[h],
                "solar_kwh": solar[h],
                "tariff_bdt_per_kwh": tariff[h],
            }
            for h in range(24)
        ],
        "battery": {
            "capacity_kwh": capacity,
            "minimum_energy_kwh": round(capacity * min(fractions), 3),
            "initial_energy_kwh": round(capacity * max(fractions), 3),
            "max_charge_kwh_per_hour": rates[0],
            "max_discharge_kwh_per_hour": rates[1],
        },
    },
    st.lists(st.floats(0, 400).map(lambda v: round(v, 2)), min_size=24, max_size=24),
    st.lists(st.floats(0, 300).map(lambda v: round(v, 2)), min_size=24, max_size=24),
    st.lists(st.floats(0, 40).map(lambda v: round(v, 2)), min_size=24, max_size=24),
    st.floats(0, 600).map(lambda v: round(v, 1)),
    st.tuples(st.floats(0, 1), st.floats(0, 1)),
    st.tuples(
        st.floats(0, 120).map(lambda v: round(v, 1)), st.floats(0, 120).map(lambda v: round(v, 1))
    ),
)

window = st.lists(st.integers(0, 23), min_size=1, max_size=6, unique=True).map(
    lambda hs: tuple(sorted(hs))
)
always_feasible_directive = st.one_of(
    st.builds(SolarReduction, hours=window, factor=st.floats(0, 1).map(lambda v: round(v, 2))),
    st.builds(NoCharge, hours=window),
    st.builds(NoDischarge, hours=window),
    st.just(NoOp()),
)


@settings(max_examples=150, deadline=None)
@given(data=scenario, directives=st.lists(always_feasible_directive, min_size=1, max_size=3))
def test_any_feasible_scenario_yields_a_valid_plan_no_worse_than_idle(
    data: dict[str, Any], directives: list[Directive]
) -> None:
    request = OptimizeRequest.model_validate(data)
    outcome = plan_schedule(request, directives)
    assert outcome.fallback is None
    _strictly_valid(request, directives, outcome)
    baseline = idle_plan(request, merge(request, directives))
    assert outcome.result.total_cost_bdt <= baseline.total_cost_bdt + 0.01


def test_scheduling_stage_is_time_boxed() -> None:
    """With no time left the stage still answers at once, with the battery-idle plan."""
    request = make_request()
    outcome = plan_schedule(request, [NoOp()], budget_s=0.0)
    assert outcome.fallback == "idle"
    assert {p.battery_action for p in outcome.result.plan} == {"idle"}
    assert replay_and_check(request, [NoOp()], outcome.result.plan) == []
