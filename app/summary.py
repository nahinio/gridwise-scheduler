"""Deterministic `plan_summary` text, built from the directives and the final plan. Pure."""

from __future__ import annotations

from collections.abc import Sequence

from app.optimizer import ScheduleOutcome
from app.schemas import Directive, MaxGrid, MinReserve, NoOp, SolarReduction


def _span(hours: Sequence[int]) -> str:
    """[18, 19, 20] -> '18:00-21:00'; non-contiguous hours are listed."""
    if list(hours) == list(range(hours[0], hours[-1] + 1)):
        return f"{hours[0]:02d}:00-{hours[-1] + 1:02d}:00"
    return "hours " + ", ".join(str(h) for h in hours)


def _describe(directive: Directive) -> str:
    if isinstance(directive, NoOp):
        return ""
    window = _span(directive.hours)
    if isinstance(directive, SolarReduction):
        return f"solar limited to {directive.factor:g} of forecast {window}"
    if isinstance(directive, MinReserve):
        return f"battery reserve of {directive.minimum_energy_kwh:g} kWh {window}"
    if isinstance(directive, MaxGrid):
        return f"grid import capped at {directive.max_grid_kwh:g} kWh {window}"
    action = "charging" if directive.directive_type == "no_charge_window" else "discharging"
    return f"no battery {action} {window}"


def build_summary(directives: Sequence[Directive], outcome: ScheduleOutcome) -> str:
    applied = [text for text in map(_describe, outcome.applied) if text]
    ignored = sum(isinstance(d, NoOp) for d in directives)
    result = outcome.result

    parts = [
        f"Applied {len(applied)} operator directive(s)"
        + (": " + "; ".join(applied) + "." if applied else ".")
    ]
    if ignored:
        parts.append(f"Ignored {ignored} note(s) unrelated to today's schedule.")
    if outcome.relaxed_note_indexes:
        indexes = ", ".join(str(i) for i in outcome.relaxed_note_indexes)
        parts.append(f"Note(s) {indexes} could not be satisfied together and were relaxed.")

    charged = sum(p.battery_kwh for p in result.plan if p.battery_action == "charge")
    if charged > 0:
        parts.append(
            f"The battery shifts {charged:g} kWh from low-tariff to high-tariff hours and "
            "returns to its initial energy by hour 23."
        )
    else:
        parts.append("The battery stays idle; solar is used first and the grid covers the rest.")
    parts.append(
        f"Minimum grid cost {result.total_cost_bdt:g} BDT for {result.total_grid_kwh:g} kWh "
        f"(peak {result.peak_grid_kwh:g} kWh)."
    )
    return " ".join(parts)
