"""Exact minimum-cost 24-hour schedule as a linear program (HiGHS via SciPy). Pure.

Per hour h: grid g, solar used s, charge c, discharge d (all >= 0).

    min   sum(tariff_h * g_h)
    s.t.  g_h + s_h + d_h - c_h = demand_h                      energy balance
          s_h <= effective_solar_h,  c_h <= charge_cap_h,
          d_h <= discharge_cap_h,    g_h <= grid_cap_h          directive-adjusted limits
          reserve_h <= E0 + sum_{k<=h}(c_k - d_k) <= capacity   battery bounds
          sum(c) - sum(d) = 0                                   end-of-day neutrality
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import combinations

import numpy as np
from scipy.optimize import linprog

from app.config import HOURS_PER_DAY, ROUND_DECIMALS, STRICT_TOLERANCE, TOLERANCE_KWH
from app.merge import HourlyConstraints, merge
from app.schemas import (
    BatteryAction,
    Directive,
    HourPlan,
    MaxGrid,
    MinReserve,
    NoOp,
    OptimizeRequest,
    SolarReduction,
)
from app.validator import Violation, replay_and_check

H = HOURS_PER_DAY
_IDLE_EPS = 1e-9
# Total objective weight spent on discouraging battery throughput. It only breaks ties
# between equal-cost optima (no pointless cycling); the true cost moves by < 0.001 BDT.
_THROUGHPUT_BUDGET_BDT = 1e-3


@dataclass(frozen=True)
class PlanResult:
    plan: list[HourPlan]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float


@dataclass(frozen=True)
class ScheduleOutcome:
    result: PlanResult
    applied: list[Directive]
    relaxed_note_indexes: list[int] = field(default_factory=list)
    fallback: str | None = None  # None | "relaxed" | "best_effort" | "idle"
    violations: list[Violation] = field(default_factory=list)


def _solve_lp(request: OptimizeRequest, limits: HourlyConstraints) -> np.ndarray | None:
    """Return the LP optimum as an (4, 24) array [g, s, c, d], or None when infeasible."""
    battery = request.battery
    demand = np.array([entry.demand_kwh for entry in request.hours])
    tariff = np.array([entry.tariff_bdt_per_kwh for entry in request.hours])

    throughput_cap = sum(limits.charge_cap) + sum(limits.discharge_cap)
    penalty = _THROUGHPUT_BUDGET_BDT / max(1.0, throughput_cap)
    cost = np.concatenate([tariff, np.zeros(H), np.full(2 * H, penalty)])

    eye, zero = np.eye(H), np.zeros((H, H))
    balance = np.hstack([eye, eye, -eye, eye])  # g + s - c + d = demand
    neutrality = np.concatenate([np.zeros(2 * H), np.ones(H), -np.ones(H)])[None, :]
    a_eq = np.vstack([balance, neutrality])
    b_eq = np.concatenate([demand, [0.0]])

    cumulative = np.tril(np.ones((H, H)))
    soc = np.hstack([zero, zero, cumulative, -cumulative])  # E_h - E0
    a_ub = np.vstack([soc, -soc])
    b_ub = np.concatenate(
        [
            np.full(H, battery.capacity_kwh - battery.initial_energy_kwh),
            battery.initial_energy_kwh - np.array(limits.min_reserve),
        ]
    )

    bounds: list[tuple[float, float | None]] = (
        [(0.0, None if math.isinf(cap) else cap) for cap in limits.grid_cap]
        + [(0.0, cap) for cap in limits.eff_solar]
        + [(0.0, cap) for cap in limits.charge_cap]
        + [(0.0, cap) for cap in limits.discharge_cap]
    )

    try:
        solution = linprog(
            cost, A_ub=a_ub, b_ub=b_ub, A_eq=a_eq, b_eq=b_eq, bounds=bounds, method="highs"
        )
    except ValueError:
        return None
    if solution.status != 0 or solution.x is None:
        return None
    return np.asarray(solution.x, dtype=float).reshape(4, H)


def _build_plan(
    request: OptimizeRequest,
    limits: HourlyConstraints,
    net_charge: Sequence[float],
    solar: Sequence[float],
) -> PlanResult:
    """Turn raw LP values into the response plan.

    Rounding happens on the battery-energy trajectory (not on charge/discharge), so bounds,
    rate limits and end-of-day neutrality hold exactly; grid is the residual of the balance
    equation and totals are summed from the final plan only.
    """
    battery = request.battery
    plan: list[HourPlan] = []
    energy_before = battery.initial_energy_kwh
    trajectory = battery.initial_energy_kwh

    for h, given in enumerate(request.hours):
        trajectory += net_charge[h]
        if h == H - 1:
            energy = battery.initial_energy_kwh
        else:
            low = max(limits.min_reserve[h], energy_before - limits.discharge_cap[h])
            high = min(battery.capacity_kwh, energy_before + limits.charge_cap[h])
            energy = min(max(round(trajectory, ROUND_DECIMALS), low), high)

        delta = round(energy - energy_before, 6)
        action: BatteryAction
        if abs(delta) < _IDLE_EPS:
            action, delta, energy = "idle", 0.0, energy_before
        elif delta > 0:
            action = "charge"
        else:
            action = "discharge"

        solar_used = max(0.0, min(round(solar[h], ROUND_DECIMALS), limits.eff_solar[h]))
        grid = given.demand_kwh + delta - solar_used
        if grid > limits.grid_cap[h]:  # rounding pushed us over a cap: lean on spare solar
            solar_used = min(limits.eff_solar[h], solar_used + grid - limits.grid_cap[h])
            grid = given.demand_kwh + delta - solar_used
        if grid < 0:  # surplus after rounding: curtail solar instead of exporting
            solar_used = max(0.0, solar_used + grid)
            grid = 0.0

        plan.append(
            HourPlan(
                hour=h,
                grid_kwh=round(max(0.0, grid), 6),
                solar_used_kwh=round(solar_used, 6),
                battery_action=action,
                battery_kwh=abs(delta),
                battery_energy_after_kwh=round(energy, 6),
            )
        )
        energy_before = energy

    return _with_totals(request, plan)


def _with_totals(request: OptimizeRequest, plan: list[HourPlan]) -> PlanResult:
    grid = [entry.grid_kwh for entry in plan]
    cost = sum(g * given.tariff_bdt_per_kwh for g, given in zip(grid, request.hours, strict=True))
    return PlanResult(
        plan=plan,
        total_grid_kwh=round(sum(grid), 6),
        total_cost_bdt=round(cost, 6),
        peak_grid_kwh=max(grid),
    )


def solve(request: OptimizeRequest, limits: HourlyConstraints) -> PlanResult | None:
    """Cheapest valid plan under `limits`, or None if the constraints cannot be met."""
    raw = _solve_lp(request, limits)
    if raw is None:
        return None
    _, solar, charge, discharge = raw
    return _build_plan(request, limits, list(charge - discharge), list(solar))


def idle_plan(request: OptimizeRequest, limits: HourlyConstraints) -> PlanResult:
    """Battery untouched, solar first, grid covers the rest. Always satisfies the base rules."""
    return _build_plan(
        request,
        limits,
        [0.0] * H,
        [
            min(entry.demand_kwh, cap)
            for entry, cap in zip(request.hours, limits.eff_solar, strict=True)
        ],
    )


def _check(
    request: OptimizeRequest, directives: Sequence[Directive], result: PlanResult, tol: float
) -> list[Violation]:
    return replay_and_check(
        request,
        directives,
        result.plan,
        total_grid_kwh=result.total_grid_kwh,
        total_cost_bdt=result.total_cost_bdt,
        peak_grid_kwh=result.peak_grid_kwh,
        tol=tol,
    )


def _drop_rank(directive: Directive) -> int:
    """Relaxation preference: grid caps go first, solar reductions last."""
    if isinstance(directive, MaxGrid):
        return 0
    if isinstance(directive, MinReserve):
        return 1
    if isinstance(directive, SolarReduction):
        return 3
    return 2


def plan_schedule(request: OptimizeRequest, directives: Sequence[Directive]) -> ScheduleOutcome:
    """Optimize under all directives; degrade in a controlled, logged way if that fails.

    Organizer scenarios are feasible under the true directives, so infeasibility means one
    of *our* interpretations is wrong. We then keep the largest feasible subset of
    directives rather than returning an error or an arbitrary plan.
    """
    active = [i for i, d in enumerate(directives) if not isinstance(d, NoOp)]

    candidates: list[tuple[int, ...]] = []
    for dropped_count in range(len(active) + 1):
        drops = sorted(
            combinations(active, dropped_count),
            key=lambda drop: sum(_drop_rank(directives[i]) for i in drop),
        )
        candidates += drops

    all_directives = list(directives)
    for dropped in candidates:
        kept = [d for i, d in enumerate(directives) if i not in dropped]
        result = solve(request, merge(request, kept))
        if result is None:
            continue
        violations = _check(request, kept, result, STRICT_TOLERANCE)
        if violations:
            violations = _check(request, kept, result, TOLERANCE_KWH)
        if not violations:
            return ScheduleOutcome(
                result=result,
                applied=kept,
                relaxed_note_indexes=list(dropped),
                fallback="relaxed" if dropped else None,
            )
        fallback = idle_plan(request, merge(request, all_directives))
        fallback_violations = _check(request, all_directives, fallback, TOLERANCE_KWH)
        if len(fallback_violations) < len(violations):
            return ScheduleOutcome(
                fallback, all_directives, list(dropped), "idle", fallback_violations
            )
        return ScheduleOutcome(result, kept, list(dropped), "best_effort", violations)

    fallback = idle_plan(request, merge(request, all_directives))
    return ScheduleOutcome(
        fallback,
        all_directives,
        active,
        "idle",
        _check(request, all_directives, fallback, TOLERANCE_KWH),
    )
