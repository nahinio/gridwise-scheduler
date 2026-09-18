"""Request contract: what is accepted, what is a 400-class structural error, what is 422."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas import (
    DirectiveInterpretation,
    MaxGrid,
    MinReserve,
    NoCharge,
    NoOp,
    OptimizeRequest,
    SemanticError,
    SolarReduction,
    check_semantics,
)


def test_public_inputs_are_accepted(public_case: Any) -> None:
    assert public_case.request.scenario_id == public_case.id
    assert [entry.hour for entry in public_case.request.hours] == list(range(24))


def test_unknown_fields_are_ignored(sample_input: dict[str, Any]) -> None:
    sample_input["harness_metadata"] = {"run": 7}
    sample_input["hours"][0]["label"] = "midnight"
    sample_input["battery"]["vendor"] = "acme"
    assert OptimizeRequest.model_validate(sample_input).scenario_id == "SAMPLE-06"


def test_hours_are_sorted_when_sent_out_of_order(sample_input: dict[str, Any]) -> None:
    sample_input["hours"].reverse()
    request = OptimizeRequest.model_validate(sample_input)
    assert [entry.hour for entry in request.hours] == list(range(24))


def _drop_hour(data: dict[str, Any]) -> None:
    data["hours"].pop()


def _duplicate_hour(data: dict[str, Any]) -> None:
    data["hours"][5]["hour"] = 4


def _set(path: tuple[Any, ...], value: Any) -> Any:
    def mutate(data: dict[str, Any]) -> None:
        target = data
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


STRUCTURAL_MUTATIONS = {
    "23_hours": _drop_hour,
    "duplicate_hour": _duplicate_hour,
    "hour_out_of_range": _set(("hours", 0, "hour"), 24),
    "negative_demand": _set(("hours", 3, "demand_kwh"), -1),
    "nan_solar": _set(("hours", 3, "solar_kwh"), float("nan")),
    "infinite_tariff": _set(("hours", 3, "tariff_bdt_per_kwh"), float("inf")),
    "string_number": _set(("hours", 3, "demand_kwh"), "120"),
    "boolean_number": _set(("battery", "capacity_kwh"), True),
    "missing_battery_field": lambda d: d["battery"].pop("capacity_kwh"),
    "missing_scenario_id": lambda d: d.pop("scenario_id"),
    "numeric_scenario_id": _set(("scenario_id",), 12),
    "empty_scenario_id": _set(("scenario_id",), ""),
    "no_notes": _set(("operator_notes",), []),
    "four_notes": _set(("operator_notes",), ["a", "b", "c", "d"]),
    "blank_note": _set(("operator_notes",), ["   "]),
    "non_string_note": _set(("operator_notes",), [42]),
    "notes_not_a_list": _set(("operator_notes",), "just one note"),
    "hours_not_a_list": _set(("hours",), {}),
}


@pytest.mark.parametrize("name", STRUCTURAL_MUTATIONS)
def test_structural_errors_are_rejected(name: str, sample_input: dict[str, Any]) -> None:
    STRUCTURAL_MUTATIONS[name](sample_input)
    with pytest.raises(ValidationError):
        OptimizeRequest.model_validate(sample_input)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("initial_energy_kwh", 10),  # below minimum (35)
        ("initial_energy_kwh", 500),  # above capacity (220)
        ("minimum_energy_kwh", 300),  # above capacity
    ],
)
def test_inconsistent_battery_is_semantic(
    field: str, value: float, sample_input: dict[str, Any]
) -> None:
    sample_input["battery"][field] = value
    request = OptimizeRequest.model_validate(sample_input)
    with pytest.raises(SemanticError):
        check_semantics(request)


def test_interpretation_shapes_match_the_contract() -> None:
    built = [
        DirectiveInterpretation.build(0, SolarReduction(hours=(13, 14), factor=0.2), "x"),
        DirectiveInterpretation.build(1, MinReserve(hours=(18,), minimum_energy_kwh=120), "x"),
        DirectiveInterpretation.build(2, NoCharge(hours=(14, 15)), "x"),
        DirectiveInterpretation.build(3, MaxGrid(hours=(19,), max_grid_kwh=155), "x"),
        DirectiveInterpretation.build(4, NoOp(), "x"),
    ]
    assert [b.structured_adjustment for b in built] == [
        {"hours": [13, 14], "factor": 0.2},
        {"hours": [18], "minimum_energy_kwh": 120},
        {"hours": [14, 15]},
        {"hours": [19], "max_grid_kwh": 155},
        None,
    ]
    assert [b.applies for b in built] == [True, True, True, True, False]
