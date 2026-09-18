"""Deterministic validation of one model-produced interpretation (Problem Statement 08). Pure.

Model output is untrusted data. It either becomes a typed `Directive` or a `GuardrailReject`
whose reason is fed back to the model for one corrective re-ask. Nothing here raises.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, get_args

from app.config import HOURS_PER_DAY
from app.schemas import (
    Directive,
    DirectiveType,
    MaxGrid,
    MinReserve,
    NoCharge,
    NoDischarge,
    NoOp,
    SolarReduction,
)

ALLOWED_TYPES: frozenset[str] = frozenset(get_args(DirectiveType))
MAX_EXPLANATION_CHARS = 300
DEFAULT_EXPLANATIONS = {
    "solar_reduction": "Usable solar is reduced during the stated window.",
    "minimum_battery_reserve": "A higher battery reserve must be held during the stated window.",
    "no_charge_window": "Battery charging is unavailable during the stated window.",
    "no_discharge_window": "Battery discharging is unavailable during the stated window.",
    "max_grid_window": "Grid import is capped during the stated window.",
    "no_op": "This note does not affect today's 24-hour energy schedule.",
}
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass(frozen=True)
class Accepted:
    directive: Directive
    explanation: str
    normalised: bool = False  # True when a harmless defect was repaired (e.g. unsorted hours)


@dataclass(frozen=True)
class GuardrailReject:
    reason: str  # machine-readable code, also used as a metric label
    fix: str  # one-sentence correction hint sent back to the model


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _clean_hours(value: Any) -> tuple[tuple[int, ...], bool]:
    """Keep integral hours in 0..23, de-duplicated and ascending. Returns (hours, repaired)."""
    if not isinstance(value, (list, tuple)):
        return (), True
    kept: set[int] = set()
    for item in value:
        number = _finite_number(item)
        if number is not None and number == int(number) and 0 <= number < HOURS_PER_DAY:
            kept.add(int(number))
    hours = tuple(sorted(kept))
    return hours, list(value) != list(hours)


def _clean_explanation(value: Any, directive_type: str) -> str:
    text = _CONTROL_CHARS.sub(" ", value).strip() if isinstance(value, str) else ""
    return text[:MAX_EXPLANATION_CHARS] or DEFAULT_EXPLANATIONS[directive_type]


def validate_interpretation(
    raw: Any, *, note_index: int, capacity_kwh: float
) -> Accepted | GuardrailReject:
    """Apply guardrails G1-G10 in order; the first failure wins."""
    if not isinstance(raw, dict):
        return GuardrailReject("not_an_object", "Return exactly one JSON object.")

    # G1 - note mapping is owned by this service, never by the model: each note gets its own
    # call and the response index is set by us, so every note appears exactly once, in order.
    # A model that echoes a different note_index is repaired, not trusted.
    index_repaired = raw.get("note_index") not in (None, note_index)

    # G2 - only the six published directive types exist.
    directive_type = raw.get("directive_type")
    if not isinstance(directive_type, str) or directive_type not in ALLOWED_TYPES:
        return GuardrailReject(
            "unknown_type", f"directive_type must be one of: {', '.join(sorted(ALLOWED_TYPES))}."
        )

    # G3/G4 - applies is true for every real directive and false only for no_op.
    applies = raw.get("applies")
    if isinstance(applies, bool) and applies != (directive_type != "no_op"):
        return GuardrailReject(
            "applies_type_conflict",
            "applies must be false only for no_op and true for every other directive_type. "
            "Decide whether the note limits solar, the battery, or grid import today.",
        )

    explanation = _clean_explanation(raw.get("explanation"), directive_type)
    if directive_type == "no_op":
        return Accepted(NoOp(), explanation, index_repaired)

    # G5 - hours are unique integers 0..23 in ascending order, and there is at least one.
    hours, hours_repaired = _clean_hours(raw.get("hours"))
    repaired = hours_repaired or index_repaired
    if not hours:
        return GuardrailReject(
            "bad_hours", "hours must list at least one whole hour between 0 and 23."
        )

    if directive_type == "solar_reduction":
        # G6 - factor is the usable fraction that remains.
        factor = _finite_number(raw.get("factor"))
        if factor is None:
            return GuardrailReject("factor_missing", "solar_reduction needs a numeric factor.")
        if 1 < factor <= 100:
            return GuardrailReject(
                "factor_looks_like_percent",
                "factor is a fraction between 0 and 1 (the share of solar that remains), "
                "not a percentage.",
            )
        if not 0 <= factor <= 1:
            return GuardrailReject("factor_out_of_range", "factor must be between 0 and 1.")
        return Accepted(SolarReduction(hours=hours, factor=round(factor, 6)), explanation, repaired)

    if directive_type == "minimum_battery_reserve":
        # G7 - reserve is finite, non-negative and physically storable.
        reserve = _finite_number(raw.get("minimum_energy_kwh"))
        if reserve is None:
            return GuardrailReject(
                "reserve_missing", "minimum_battery_reserve needs minimum_energy_kwh in kWh."
            )
        if not 0 <= reserve <= capacity_kwh:
            return GuardrailReject(
                "reserve_out_of_range",
                f"minimum_energy_kwh must be between 0 and the battery capacity "
                f"({capacity_kwh:g} kWh). Convert percentages of capacity to kWh.",
            )
        return Accepted(
            MinReserve(hours=hours, minimum_energy_kwh=round(reserve, 6)), explanation, repaired
        )

    if directive_type == "max_grid_window":
        # G8 - grid cap is finite and non-negative.
        cap = _finite_number(raw.get("max_grid_kwh"))
        if cap is None or cap < 0:
            return GuardrailReject(
                "grid_cap_invalid", "max_grid_window needs a non-negative max_grid_kwh."
            )
        return Accepted(MaxGrid(hours=hours, max_grid_kwh=round(cap, 6)), explanation, repaired)

    # G9 - window types carry hours only; stray numerics are ignored, never applied.
    if directive_type == "no_charge_window":
        return Accepted(NoCharge(hours=hours), explanation, repaired)
    return Accepted(NoDischarge(hours=hours), explanation, repaired)
