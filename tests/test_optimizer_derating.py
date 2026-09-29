"""A cold battery takes less: the planner spreads the charge, the script
still gets asked for full power."""

from __future__ import annotations

from dataclasses import replace
import math

from test_optimizer import BATTERY, CONFIG, make_slots

from smart_battery_pilot.const import ACTION_CHARGE
from smart_battery_pilot.optimizer import build_plan

PRICES = [0.10] * 6 + [0.50] * 4
ETA_ONE_WAY = math.sqrt(BATTERY.efficiency / 100.0)


def _charge_slots(plan):
    return [s for s in plan.slots if s.action == ACTION_CHARGE]


def test_factor_one_is_todays_plan():
    slots = make_slots(PRICES, demand_kwh=2.0)
    assert build_plan(slots, replace(BATTERY, charge_factor=1.0), CONFIG) == build_plan(
        slots, BATTERY, CONFIG
    )


def test_a_cold_battery_is_charged_over_more_slots():
    slots = make_slots(PRICES, demand_kwh=2.0)
    warm = build_plan(slots, BATTERY, CONFIG)
    cold = build_plan(slots, replace(BATTERY, charge_factor=0.25), CONFIG)
    assert len(_charge_slots(cold)) > len(_charge_slots(warm))


def test_the_soc_forecast_respects_the_derated_rate():
    factor = 0.25
    cold = build_plan(
        make_slots(PRICES, demand_kwh=2.0), replace(BATTERY, charge_factor=factor), CONFIG
    )
    cap_kwh = BATTERY.max_charge_power_w / 1000.0 * factor * ETA_ONE_WAY  # 1 h slots
    previous = BATTERY.soc
    for slot in cold.slots:
        if slot.action == ACTION_CHARGE:
            stored = (slot.soc_forecast - previous) / 100.0 * BATTERY.capacity_kwh
            # Two soc_forecast values rounded to 0.1 % of 12.8 kWh: <= 12.8 Wh.
            assert stored <= cap_kwh + 0.013
        previous = slot.soc_forecast


def test_the_script_is_asked_for_full_power_not_the_derated_one():
    cold = build_plan(
        make_slots(PRICES, demand_kwh=2.0), replace(BATTERY, charge_factor=0.25), CONFIG
    )
    powers = [s.power_w for s in _charge_slots(cold)]
    assert all(p <= BATTERY.max_charge_power_w + 0.5 for p in powers)
    # A slot charged up to the derated cap requests the nominal maximum.
    assert any(abs(p - BATTERY.max_charge_power_w) < 1.0 for p in powers)


def test_only_a_slot_filled_to_the_cold_limit_asks_for_full_power():
    """A cold BMS caps the current; it does not take a share of the request.

    Asking for more than planned in a partial slot would store up to 1/factor
    times the planned energy. Only a slot the planner fills to its derated cap
    asks for the maximum - which the BMS then limits to exactly that cap.
    """
    factor = 0.5
    cold = build_plan(
        make_slots(PRICES, demand_kwh=0.5), replace(BATTERY, charge_factor=factor), CONFIG
    )
    cap_kwh = BATTERY.max_charge_power_w / 1000.0 * factor * ETA_ONE_WAY
    previous = BATTERY.soc
    for slot in cold.slots:
        if slot.action == ACTION_CHARGE:
            stored = (slot.soc_forecast - previous) / 100.0 * BATTERY.capacity_kwh
            planned_grid_w = stored / ETA_ONE_WAY * 1000.0  # 1 h slots
            if stored >= cap_kwh - 0.013:
                assert slot.power_w == BATTERY.max_charge_power_w
            else:
                # soc_forecast is rounded to 0.1 %, i.e. ~13 Wh on 12.8 kWh.
                assert abs(slot.power_w - planned_grid_w) < 20.0
        previous = slot.soc_forecast


def test_a_top_up_to_max_soc_does_not_overshoot():
    """85 % -> 95 % needs 1.28 kWh; the cold cap per slot is larger. Asking for
    planned/factor would have the battery take its full cold limit and end
    above max SOC."""
    factor = 0.3
    battery = replace(BATTERY, soc=85.0, charge_factor=factor)
    plan = build_plan(make_slots(PRICES, demand_kwh=3.0), battery, CONFIG)
    [first] = _charge_slots(plan)[:1]
    cold_limit_w = BATTERY.max_charge_power_w * factor
    assert first.power_w < cold_limit_w


def test_factor_zero_plans_no_grid_charging():
    plan = build_plan(
        make_slots(PRICES, demand_kwh=2.0), replace(BATTERY, charge_factor=0.0), CONFIG
    )
    assert _charge_slots(plan) == []
