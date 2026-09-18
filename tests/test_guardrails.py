"""Guardrails G1-G10: every model output becomes a typed directive or a named rejection."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.guardrails import DEFAULT_EXPLANATIONS, Accepted, GuardrailReject, validate_interpretation
from app.schemas import MaxGrid, MinReserve, NoCharge, NoDischarge, NoOp, SolarReduction


def check(raw: Any, note_index: int = 0, capacity: float = 200.0) -> Accepted | GuardrailReject:
    return validate_interpretation(raw, note_index=note_index, capacity_kwh=capacity)


def llm(directive_type: str, **fields: Any) -> dict[str, Any]:
    """Shape of the flat strict-schema object the model returns."""
    base = {
        "note_index": 0,
        "applies": directive_type != "no_op",
        "directive_type": directive_type,
        "hours": [],
        "factor": None,
        "minimum_energy_kwh": None,
        "max_grid_kwh": None,
        "explanation": "because",
    }
    return base | fields


ACCEPTED = [
    (
        llm("solar_reduction", hours=[13, 14], factor=0.2),
        SolarReduction(hours=(13, 14), factor=0.2),
    ),
    (llm("solar_reduction", hours=[9], factor=0), SolarReduction(hours=(9,), factor=0.0)),
    (llm("solar_reduction", hours=[9], factor=1), SolarReduction(hours=(9,), factor=1.0)),
    (
        llm("minimum_battery_reserve", hours=[18, 19], minimum_energy_kwh=120),
        MinReserve(hours=(18, 19), minimum_energy_kwh=120.0),
    ),
    (
        llm("minimum_battery_reserve", hours=[18], minimum_energy_kwh=200),
        MinReserve(hours=(18,), minimum_energy_kwh=200.0),
    ),
    (llm("no_charge_window", hours=[2, 3, 4]), NoCharge(hours=(2, 3, 4))),
    (llm("no_discharge_window", hours=[17, 18]), NoDischarge(hours=(17, 18))),
    (llm("max_grid_window", hours=[19], max_grid_kwh=155), MaxGrid(hours=(19,), max_grid_kwh=155)),
    (llm("max_grid_window", hours=[19], max_grid_kwh=0), MaxGrid(hours=(19,), max_grid_kwh=0.0)),
    (llm("no_op"), NoOp()),
    # no_op ignores any stray hours / numerics the model attached
    (llm("no_op", hours=[1, 2], factor=0.5), NoOp()),
    # stray numerics on a window type are ignored, never applied (G9)
    (llm("no_charge_window", hours=[5], factor=0.3, max_grid_kwh=10), NoCharge(hours=(5,))),
]


@pytest.mark.parametrize(("raw", "expected"), ACCEPTED)
def test_valid_outputs_become_typed_directives(raw: dict[str, Any], expected: Any) -> None:
    result = check(raw)
    assert isinstance(result, Accepted)
    assert result.directive == expected


REJECTED = [
    ("a string", "not_an_object"),
    (None, "not_an_object"),
    ([llm("no_op")], "not_an_object"),
    (llm("shed_load", hours=[1]), "unknown_type"),
    (llm("no_op") | {"directive_type": None}, "unknown_type"),
    (llm("no_op", applies=True), "applies_type_conflict"),
    (llm("no_charge_window", hours=[3], applies=False), "applies_type_conflict"),
    (llm("no_charge_window", hours=[]), "bad_hours"),
    (llm("no_charge_window", hours=None), "bad_hours"),
    (llm("no_charge_window", hours=[24, 25, -1]), "bad_hours"),
    (llm("no_charge_window", hours=["two", 3.5, True]), "bad_hours"),
    (llm("solar_reduction", hours=[13]), "factor_missing"),
    (llm("solar_reduction", hours=[13], factor="0.2"), "factor_missing"),
    (llm("solar_reduction", hours=[13], factor=20), "factor_looks_like_percent"),
    (llm("solar_reduction", hours=[13], factor=-0.1), "factor_out_of_range"),
    (llm("solar_reduction", hours=[13], factor=250), "factor_out_of_range"),
    (llm("solar_reduction", hours=[13], factor=float("nan")), "factor_missing"),
    (llm("minimum_battery_reserve", hours=[18]), "reserve_missing"),
    (llm("minimum_battery_reserve", hours=[18], minimum_energy_kwh=-5), "reserve_out_of_range"),
    (llm("minimum_battery_reserve", hours=[18], minimum_energy_kwh=201), "reserve_out_of_range"),
    (llm("max_grid_window", hours=[19]), "grid_cap_invalid"),
    (llm("max_grid_window", hours=[19], max_grid_kwh=-1), "grid_cap_invalid"),
    (llm("max_grid_window", hours=[19], max_grid_kwh=float("inf")), "grid_cap_invalid"),
]


@pytest.mark.parametrize(("raw", "reason"), REJECTED)
def test_invalid_outputs_are_rejected_with_a_reason(raw: Any, reason: str) -> None:
    result = check(raw)
    assert isinstance(result, GuardrailReject)
    assert result.reason == reason
    assert result.fix


def test_hours_are_deduplicated_sorted_and_clipped_to_the_day() -> None:
    result = check(llm("no_discharge_window", hours=[23, 22, 22, 24, 22.0]))
    assert isinstance(result, Accepted)
    assert result.directive == NoDischarge(hours=(22, 23))
    assert result.normalised


def test_note_mapping_is_owned_by_the_service_not_the_model() -> None:
    result = check(llm("no_charge_window", hours=[4], note_index=7), note_index=1)
    assert isinstance(result, Accepted)
    assert result.directive == NoCharge(hours=(4,)) and result.normalised


def test_missing_note_index_and_applies_are_tolerated() -> None:
    raw = {"directive_type": "no_charge_window", "hours": [4]}
    result = check(raw, note_index=2)
    assert isinstance(result, Accepted)
    assert result.explanation == DEFAULT_EXPLANATIONS["no_charge_window"]


def test_explanation_is_sanitised_and_bounded() -> None:
    result = check(llm("no_op", explanation="line one\n\tline two\x00" + "x" * 500))
    assert isinstance(result, Accepted)
    assert "\n" not in result.explanation and "\x00" not in result.explanation
    assert len(result.explanation) <= 300


json_like = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=12),
    lambda inner: (
        st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=4)
    ),
    max_leaves=12,
)
llm_like = st.fixed_dictionaries(
    {},
    optional={
        "note_index": json_like,
        "applies": json_like,
        "directive_type": st.sampled_from(sorted(DEFAULT_EXPLANATIONS)) | json_like,
        "hours": st.lists(st.integers(-5, 30) | json_like, max_size=30) | json_like,
        "factor": json_like,
        "minimum_energy_kwh": json_like,
        "max_grid_kwh": json_like,
        "explanation": json_like,
    },
)


@settings(max_examples=400, deadline=None)
@given(raw=json_like | llm_like)
def test_guardrails_never_raise_and_never_emit_an_invalid_directive(raw: Any) -> None:
    result = check(raw)
    assert isinstance(result, (Accepted, GuardrailReject))
    if isinstance(result, Accepted) and not isinstance(result.directive, NoOp):
        hours = result.directive.hours
        assert hours and list(hours) == sorted(set(hours))
        assert all(isinstance(h, int) and 0 <= h <= 23 for h in hours)
        if isinstance(result.directive, SolarReduction):
            assert 0 <= result.directive.factor <= 1
        if isinstance(result.directive, MinReserve):
            assert 0 <= result.directive.minimum_energy_kwh <= 200
        if isinstance(result.directive, MaxGrid):
            assert result.directive.max_grid_kwh >= 0
