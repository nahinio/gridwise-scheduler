"""Shared fixtures: the official public case pack, parsed into our own models."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.schemas import (
    Directive,
    HourPlan,
    OptimizeRequest,
    directive_from_adjustment,
)

PACK = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "official"
    / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
)


@dataclass(frozen=True)
class PublicCase:
    id: str
    raw_input: dict[str, Any]
    request: OptimizeRequest
    directives: list[Directive]
    reference_plan: list[HourPlan]
    expected: dict[str, Any]


def _load() -> list[PublicCase]:
    cases = []
    for case in json.loads(PACK.read_text(encoding="utf-8"))["cases"]:
        expected = case["expected_output"]
        cases.append(
            PublicCase(
                id=case["id"],
                raw_input=case["input"],
                request=OptimizeRequest.model_validate(copy.deepcopy(case["input"])),
                directives=[
                    directive_from_adjustment(e["directive_type"], e["structured_adjustment"])
                    for e in expected["directive_interpretation"]
                ],
                reference_plan=[HourPlan.model_validate(p) for p in expected["hourly_plan"]],
                expected=expected,
            )
        )
    return cases


PUBLIC_CASES = _load()


@pytest.fixture(params=PUBLIC_CASES, ids=[c.id for c in PUBLIC_CASES])
def public_case(request: pytest.FixtureRequest) -> PublicCase:
    return request.param  # type: ignore[no-any-return]


@pytest.fixture
def sample_input() -> dict[str, Any]:
    """A fresh, mutable copy of SAMPLE-06 (three notes: solar, no-charge, distractor)."""
    return copy.deepcopy(PUBLIC_CASES[5].raw_input)


def make_request(**battery_overrides: float) -> OptimizeRequest:
    """Small synthetic scenario for unit tests: flat demand, midday solar, evening peak."""
    battery = {
        "capacity_kwh": 200.0,
        "initial_energy_kwh": 100.0,
        "minimum_energy_kwh": 20.0,
        "max_charge_kwh_per_hour": 50.0,
        "max_discharge_kwh_per_hour": 50.0,
    } | battery_overrides
    hours = [
        {
            "hour": h,
            "demand_kwh": 100.0,
            "solar_kwh": 80.0 if 9 <= h <= 15 else 0.0,
            "tariff_bdt_per_kwh": 30.0 if 18 <= h <= 20 else 5.0 if h <= 5 else 12.0,
        }
        for h in range(24)
    ]
    return OptimizeRequest.model_validate(
        {"scenario_id": "UNIT", "operator_notes": ["n/a"], "hours": hours, "battery": battery}
    )
