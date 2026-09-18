"""Request / response contract (Problem Statement sections 04, 07, 10).

Requests ignore unknown fields (a harness may attach metadata) but are strict about the
fields the contract defines. Responses are built only from these models, so the emitted
JSON shape is exactly the documented one.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from app.config import HOURS_PER_DAY

DirectiveType = Literal[
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]
BatteryAction = Literal["charge", "discharge", "idle"]


class SemanticError(Exception):
    """Well-formed request whose values contradict each other (HTTP 422)."""


def _number_only(value: Any) -> Any:
    """JSON numbers only: `true` and "12" are type errors, not coercible inputs."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("must be a JSON number")
    return value


def _string_only(value: Any) -> Any:
    if not isinstance(value, str):
        raise ValueError("must be a string")
    return value


NonNegFloat = Annotated[float, BeforeValidator(_number_only), Field(ge=0, allow_inf_nan=False)]
HourIndex = Annotated[int, BeforeValidator(_number_only), Field(ge=0, le=HOURS_PER_DAY - 1)]


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class HourEntry(_RequestModel):
    hour: HourIndex
    demand_kwh: NonNegFloat
    solar_kwh: NonNegFloat
    tariff_bdt_per_kwh: NonNegFloat


class Battery(_RequestModel):
    capacity_kwh: NonNegFloat
    initial_energy_kwh: NonNegFloat
    minimum_energy_kwh: NonNegFloat
    max_charge_kwh_per_hour: NonNegFloat
    max_discharge_kwh_per_hour: NonNegFloat


class OptimizeRequest(_RequestModel):
    scenario_id: Annotated[str, BeforeValidator(_string_only), Field(min_length=1, max_length=200)]
    operator_notes: Annotated[
        list[Annotated[str, BeforeValidator(_string_only)]], Field(min_length=1, max_length=3)
    ]
    hours: list[HourEntry]
    battery: Battery

    @model_validator(mode="after")
    def _check_structure(self) -> OptimizeRequest:
        if any(not note.strip() for note in self.operator_notes):
            raise ValueError("operator_notes entries must be non-empty strings")
        if len(self.hours) != HOURS_PER_DAY:
            raise ValueError(f"hours must contain exactly {HOURS_PER_DAY} entries")
        if sorted(entry.hour for entry in self.hours) != list(range(HOURS_PER_DAY)):
            raise ValueError("hours must cover each hour 0..23 exactly once")
        self.hours.sort(key=lambda entry: entry.hour)
        return self


def check_semantics(request: OptimizeRequest) -> None:
    """Battery values must be mutually consistent (the only 422 case)."""
    battery = request.battery
    if battery.minimum_energy_kwh > battery.capacity_kwh:
        raise SemanticError("battery.minimum_energy_kwh exceeds battery.capacity_kwh")
    if battery.initial_energy_kwh > battery.capacity_kwh:
        raise SemanticError("battery.initial_energy_kwh exceeds battery.capacity_kwh")
    if battery.initial_energy_kwh < battery.minimum_energy_kwh:
        raise SemanticError("battery.initial_energy_kwh is below battery.minimum_energy_kwh")


# --- Directives: the validated, typed form of one interpreted note -------------------------


class _Directive(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SolarReduction(_Directive):
    directive_type: Literal["solar_reduction"] = "solar_reduction"
    hours: tuple[int, ...]
    factor: float


class MinReserve(_Directive):
    directive_type: Literal["minimum_battery_reserve"] = "minimum_battery_reserve"
    hours: tuple[int, ...]
    minimum_energy_kwh: float


class NoCharge(_Directive):
    directive_type: Literal["no_charge_window"] = "no_charge_window"
    hours: tuple[int, ...]


class NoDischarge(_Directive):
    directive_type: Literal["no_discharge_window"] = "no_discharge_window"
    hours: tuple[int, ...]


class MaxGrid(_Directive):
    directive_type: Literal["max_grid_window"] = "max_grid_window"
    hours: tuple[int, ...]
    max_grid_kwh: float


class NoOp(_Directive):
    directive_type: Literal["no_op"] = "no_op"


Directive = SolarReduction | MinReserve | NoCharge | NoDischarge | MaxGrid | NoOp


def structured_adjustment(directive: Directive) -> dict[str, Any] | None:
    """The exact machine-checked object of Problem Statement 4.1 (`null` for no_op)."""
    if isinstance(directive, NoOp):
        return None
    adjustment: dict[str, Any] = {"hours": list(directive.hours)}
    if isinstance(directive, SolarReduction):
        adjustment["factor"] = directive.factor
    elif isinstance(directive, MinReserve):
        adjustment["minimum_energy_kwh"] = directive.minimum_energy_kwh
    elif isinstance(directive, MaxGrid):
        adjustment["max_grid_kwh"] = directive.max_grid_kwh
    return adjustment


def directive_from_adjustment(directive_type: str, adjustment: dict[str, Any] | None) -> Directive:
    """Inverse of `structured_adjustment` - used to load organizer ground truth in tools/tests."""
    fields = dict(adjustment or {})
    if "hours" in fields:
        fields["hours"] = tuple(fields["hours"])
    models: dict[str, type[_Directive]] = {
        "solar_reduction": SolarReduction,
        "minimum_battery_reserve": MinReserve,
        "no_charge_window": NoCharge,
        "no_discharge_window": NoDischarge,
        "max_grid_window": MaxGrid,
        "no_op": NoOp,
    }
    return models[directive_type](**fields)  # type: ignore[return-value]


# --- Response ------------------------------------------------------------------------------


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DirectiveInterpretation(_ResponseModel):
    note_index: int
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: dict[str, Any] | None
    explanation: str

    @classmethod
    def build(
        cls, note_index: int, directive: Directive, explanation: str
    ) -> DirectiveInterpretation:
        """`applies` is derived, never trusted: false if and only if the directive is no_op."""
        return cls(
            note_index=note_index,
            applies=not isinstance(directive, NoOp),
            directive_type=directive.directive_type,
            structured_adjustment=structured_adjustment(directive),
            explanation=explanation,
        )


class HourPlan(_ResponseModel):
    hour: int
    grid_kwh: float
    solar_used_kwh: float
    battery_action: BatteryAction
    battery_kwh: float
    battery_energy_after_kwh: float


class OptimizeResponse(_ResponseModel):
    scenario_id: str
    directive_interpretation: list[DirectiveInterpretation]
    hourly_plan: list[HourPlan]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str


class HealthResponse(_ResponseModel):
    status: Literal["ok"] = "ok"
