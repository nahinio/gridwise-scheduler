"""Directive merging: overlaps always resolve to the tightest limit."""

from __future__ import annotations

import math

import pytest

from app.merge import merge
from app.schemas import MaxGrid, MinReserve, NoCharge, NoDischarge, NoOp, SolarReduction
from tests.conftest import make_request


def test_no_directives_leaves_base_limits() -> None:
    request = make_request()
    limits = merge(request, [NoOp()])
    assert limits.eff_solar == tuple(e.solar_kwh for e in request.hours)
    assert set(limits.min_reserve) == {20.0}
    assert set(limits.charge_cap) == {50.0}
    assert set(limits.discharge_cap) == {50.0}
    assert all(math.isinf(cap) for cap in limits.grid_cap)


def test_overlapping_solar_factors_multiply() -> None:
    limits = merge(
        make_request(),
        [SolarReduction(hours=(10, 11), factor=0.5), SolarReduction(hours=(11, 12), factor=0.5)],
    )
    assert limits.eff_solar[10] == pytest.approx(40.0)
    assert limits.eff_solar[11] == pytest.approx(20.0)
    assert limits.eff_solar[12] == pytest.approx(40.0)
    assert limits.eff_solar[13] == pytest.approx(80.0)


def test_reserves_take_the_max_and_never_drop_below_base() -> None:
    limits = merge(
        make_request(),
        [
            MinReserve(hours=(18, 19), minimum_energy_kwh=90),
            MinReserve(hours=(19, 20), minimum_energy_kwh=120),
            MinReserve(hours=(21,), minimum_energy_kwh=5),
        ],
    )
    assert limits.min_reserve[18:22] == (90, 120, 120, 20.0)


def test_grid_caps_take_the_min() -> None:
    limits = merge(
        make_request(),
        [MaxGrid(hours=(19, 20), max_grid_kwh=180), MaxGrid(hours=(20,), max_grid_kwh=150)],
    )
    assert limits.grid_cap[19] == 180
    assert limits.grid_cap[20] == 150
    assert math.isinf(limits.grid_cap[21])


def test_three_directive_types_on_the_same_hour() -> None:
    limits = merge(
        make_request(),
        [NoCharge(hours=(12,)), NoDischarge(hours=(12,)), SolarReduction(hours=(12,), factor=0.0)],
    )
    assert (limits.charge_cap[12], limits.discharge_cap[12], limits.eff_solar[12]) == (0, 0, 0)
    assert (limits.charge_cap[13], limits.discharge_cap[13]) == (50.0, 50.0)
