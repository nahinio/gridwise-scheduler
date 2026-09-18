"""Independent replay of a 24-hour plan against every rule of the specification. Pure.

The same function guards every API response, backs the test-suite, and is what
`tools/check.py` runs against a live deployment with the reference directives.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.config import HOURS_PER_DAY, TOLERANCE_KWH
from app.merge import merge
from app.schemas import Directive, HourPlan, OptimizeRequest


@dataclass(frozen=True)
class Violation:
    rule: str
    hour: int | None
    detail: str


def replay_and_check(
    request: OptimizeRequest,
    directives: Iterable[Directive],
    plan: Sequence[HourPlan],
    *,
    total_grid_kwh: float | None = None,
    total_cost_bdt: float | None = None,
    peak_grid_kwh: float | None = None,
    tol: float = TOLERANCE_KWH,
) -> list[Violation]:
    """Replay `plan` hour by hour under `directives`; an empty list means the plan is valid."""
    violations: list[Violation] = []

    def fail(rule: str, hour: int | None, detail: str) -> None:
        violations.append(Violation(rule, hour, detail))

    if len(plan) != HOURS_PER_DAY or [entry.hour for entry in plan] != list(range(HOURS_PER_DAY)):
        fail("plan_hours", None, "hourly_plan must list hours 0..23 exactly once, in order")
        return violations

    limits = merge(request, directives)
    battery = request.battery
    energy = battery.initial_energy_kwh

    for h, (entry, given) in enumerate(zip(plan, request.hours, strict=True)):
        numbers = {
            "grid_kwh": entry.grid_kwh,
            "solar_used_kwh": entry.solar_used_kwh,
            "battery_kwh": entry.battery_kwh,
            "battery_energy_after_kwh": entry.battery_energy_after_kwh,
        }
        bad = [name for name, value in numbers.items() if not math.isfinite(value) or value < 0]
        if bad:
            fail("non_negative_finite", h, f"{', '.join(bad)} must be finite and >= 0")
            continue

        charge = entry.battery_kwh if entry.battery_action == "charge" else 0.0
        discharge = entry.battery_kwh if entry.battery_action == "discharge" else 0.0

        if entry.battery_action == "idle" and entry.battery_kwh > tol:
            fail("idle_nonzero", h, "battery_kwh must be 0 when battery_action is idle")
        if charge > limits.charge_cap[h] + tol:
            rule = "no_charge_window" if limits.charge_cap[h] == 0 else "charge_rate"
            fail(rule, h, f"charge {charge} exceeds limit {limits.charge_cap[h]}")
        if discharge > limits.discharge_cap[h] + tol:
            rule = "no_discharge_window" if limits.discharge_cap[h] == 0 else "discharge_rate"
            fail(rule, h, f"discharge {discharge} exceeds limit {limits.discharge_cap[h]}")

        if entry.solar_used_kwh > limits.eff_solar[h] + tol:
            fail(
                "solar_overuse",
                h,
                f"solar_used {entry.solar_used_kwh} exceeds effective solar {limits.eff_solar[h]}",
            )

        supplied = entry.grid_kwh + entry.solar_used_kwh + discharge
        required = given.demand_kwh + charge
        if abs(supplied - required) > tol:
            fail("energy_balance", h, f"supplied {supplied} != demand + charge {required}")

        energy = energy + charge - discharge
        if abs(energy - entry.battery_energy_after_kwh) > tol:
            fail(
                "battery_transition",
                h,
                f"battery_energy_after {entry.battery_energy_after_kwh} != replayed {energy}",
            )
        if energy < limits.min_reserve[h] - tol:
            rule = (
                "minimum_battery_reserve"
                if limits.min_reserve[h] > battery.minimum_energy_kwh
                else "battery_minimum"
            )
            fail(rule, h, f"battery energy {energy} below required {limits.min_reserve[h]}")
        if energy > battery.capacity_kwh + tol:
            fail("battery_capacity", h, f"battery energy {energy} above capacity")

        if entry.grid_kwh > limits.grid_cap[h] + tol:
            fail("max_grid_window", h, f"grid {entry.grid_kwh} exceeds cap {limits.grid_cap[h]}")

    if abs(energy - battery.initial_energy_kwh) > tol:
        fail("end_of_day_neutrality", 23, f"final battery energy {energy} != initial")

    grid = [entry.grid_kwh for entry in plan]
    recomputed = {
        "total_grid_kwh": (total_grid_kwh, sum(grid)),
        "total_cost_bdt": (
            total_cost_bdt,
            sum(g * given.tariff_bdt_per_kwh for g, given in zip(grid, request.hours, strict=True)),
        ),
        "peak_grid_kwh": (peak_grid_kwh, max(grid)),
    }
    for name, (reported, actual) in recomputed.items():
        if reported is not None and not (math.isfinite(reported) and abs(reported - actual) <= tol):
            fail("totals_mismatch", None, f"{name} reported {reported}, recomputed {actual}")

    return violations
