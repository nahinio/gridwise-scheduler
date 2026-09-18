"""Fold validated directives into per-hour limits (Problem Statement 5.3). Pure."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from app.config import HOURS_PER_DAY
from app.schemas import (
    Directive,
    MaxGrid,
    MinReserve,
    NoCharge,
    NoDischarge,
    OptimizeRequest,
    SolarReduction,
)


@dataclass(frozen=True)
class HourlyConstraints:
    """Everything the optimizer and the validator need to know about one hour, per hour."""

    eff_solar: tuple[float, ...]
    min_reserve: tuple[float, ...]
    charge_cap: tuple[float, ...]
    discharge_cap: tuple[float, ...]
    grid_cap: tuple[float, ...]  # math.inf where no max_grid_window applies


def merge(request: OptimizeRequest, directives: Iterable[Directive]) -> HourlyConstraints:
    """Overlapping directives combine to the tightest limit.

    solar factors multiply, reserves take the max, grid caps take the min, and any
    no-charge / no-discharge window zeroes the corresponding rate for that hour.
    """
    battery = request.battery
    eff_solar = [entry.solar_kwh for entry in request.hours]
    min_reserve = [battery.minimum_energy_kwh] * HOURS_PER_DAY
    charge_cap = [battery.max_charge_kwh_per_hour] * HOURS_PER_DAY
    discharge_cap = [battery.max_discharge_kwh_per_hour] * HOURS_PER_DAY
    grid_cap = [math.inf] * HOURS_PER_DAY

    for directive in directives:
        if isinstance(directive, SolarReduction):
            for hour in directive.hours:
                eff_solar[hour] *= directive.factor
        elif isinstance(directive, MinReserve):
            for hour in directive.hours:
                min_reserve[hour] = max(min_reserve[hour], directive.minimum_energy_kwh)
        elif isinstance(directive, NoCharge):
            for hour in directive.hours:
                charge_cap[hour] = 0.0
        elif isinstance(directive, NoDischarge):
            for hour in directive.hours:
                discharge_cap[hour] = 0.0
        elif isinstance(directive, MaxGrid):
            for hour in directive.hours:
                grid_cap[hour] = min(grid_cap[hour], directive.max_grid_kwh)

    return HourlyConstraints(
        eff_solar=tuple(eff_solar),
        min_reserve=tuple(min_reserve),
        charge_cap=tuple(charge_cap),
        discharge_cap=tuple(discharge_cap),
        grid_cap=tuple(grid_cap),
    )
