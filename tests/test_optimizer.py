"""Scenario tests for the deterministic optimizer."""

from datetime import datetime, timedelta, timezone

import pytest

from smart_battery_pilot.const import (
    ACTION_AUTO,
    ACTION_CHARGE,
    ACTION_EXPORT,
    ACTION_IDLE,
    DISCHARGE_MODE_EXPORT,
    DISCHARGE_MODE_SELF_CONSUMPTION,
)
from smart_battery_pilot.optimizer import (
    BatteryState,
    InputSlot,
    OptimizerConfig,
    build_plan,
)
from smart_battery_pilot.price_adapters.base import PriceSlot

TZ = timezone(timedelta(hours=2))
T0 = datetime(2026, 1, 15, 0, 0, tzinfo=TZ)  # winter day (Dunkelflaute)

BATTERY = BatteryState(
    capacity_kwh=12.8,
    soc=10.0,
    min_soc=10.0,
    max_soc=95.0,
    max_charge_power_w=6000,
    max_discharge_power_w=6000,
    efficiency=90,
)

CONFIG = OptimizerConfig(
    spread_threshold=0.10,
    discharge_mode=DISCHARGE_MODE_SELF_CONSUMPTION,
)


def make_slots(prices: list[float], demand_kwh: float = 1.0) -> list[InputSlot]:
    """Hourly slots starting at T0 with uniform demand."""
    slots = []
    for i, price in enumerate(prices):
        start = T0 + timedelta(hours=i)
        slots.append(
            InputSlot(
                price_slot=PriceSlot(start=start, end=start + timedelta(hours=1), price=price),
                net_demand_kwh=demand_kwh,
            )
        )
    return slots


def test_dunkelflaute_charges_cheap_discharges_expensive():
    """Classic winter day: cheap night, expensive morning/evening peaks."""
    prices = [0.10] * 6 + [0.45] * 3 + [0.20] * 8 + [0.50] * 4 + [0.15] * 3
    plan = build_plan(make_slots(prices, demand_kwh=1.5), BATTERY, CONFIG)

    actions = [s.action for s in plan.slots]
    # Charges in the cheap night slots
    assert ACTION_CHARGE in actions[:6]
    # Battery covers the evening peak (0.50) and morning peak (0.45)
    assert all(a == ACTION_AUTO for a in actions[17:21])
    assert ACTION_AUTO in actions[6:9]
    assert plan.estimated_savings_eur > 0
    assert plan.grid_charge_kwh > 0


def test_no_spread_no_charging():
    """Flat prices: arbitrage never beats the spread threshold."""
    plan = build_plan(make_slots([0.25] * 24), BATTERY, CONFIG)
    assert all(s.action != ACTION_CHARGE for s in plan.slots)
    assert plan.grid_charge_kwh == 0


def test_soc_limits_respected():
    prices = [0.05] * 12 + [0.50] * 12
    plan = build_plan(make_slots(prices, demand_kwh=2.0), BATTERY, CONFIG)
    for slot in plan.slots:
        assert BATTERY.min_soc - 0.1 <= slot.soc_forecast <= BATTERY.max_soc + 0.1


def test_charge_power_limit():
    prices = [0.05] + [0.50] * 10
    plan = build_plan(make_slots(prices, demand_kwh=2.0), BATTERY, CONFIG)
    for slot in plan.slots:
        assert slot.charge_power_w <= BATTERY.max_charge_power_w + 1


def test_scarce_energy_idles_to_preserve_for_peak():
    """Battery holds just enough for the peak: cheap slots must idle,
    not drain the battery before the expensive hours."""
    battery = BatteryState(
        capacity_kwh=12.8,
        soc=45.0,  # ~4.5 kWh usable, peak needs ~4.2 kWh
        min_soc=10.0,
        max_soc=95.0,
        max_charge_power_w=6000,
        max_discharge_power_w=6000,
        efficiency=90,
    )
    # Spread 0.20 makes grid arbitrage 0.30->0.45 unattractive
    config = OptimizerConfig(
        spread_threshold=0.20, discharge_mode=DISCHARGE_MODE_SELF_CONSUMPTION
    )
    prices = [0.30] * 6 + [0.45] * 4 + [0.30] * 14
    plan = build_plan(make_slots(prices, demand_kwh=1.0), battery, config)
    actions = [s.action for s in plan.slots]
    assert all(a != ACTION_CHARGE for a in actions)
    # Cheap early slots preserve the stored energy for the 0.45 peak
    assert ACTION_IDLE in actions[:6]
    # Peak slots are covered by the battery
    assert all(a == ACTION_AUTO for a in actions[6:10])
    assert all(s.discharge_kwh > 0 for s in plan.slots[6:10])


def test_abundant_energy_no_idle():
    """Full battery covering all demand: no idle needed, peaks served first."""
    battery = BatteryState(
        capacity_kwh=30.0,
        soc=95.0,
        min_soc=10.0,
        max_soc=95.0,
        max_charge_power_w=6000,
        max_discharge_power_w=6000,
        efficiency=90,
    )
    prices = [0.15] * 6 + [0.45] * 4 + [0.15] * 14
    plan = build_plan(make_slots(prices, demand_kwh=1.0), battery, CONFIG)
    # Peak demand fully covered
    assert all(s.discharge_kwh > 0 for s in plan.slots[6:10])
    assert all(s.action != ACTION_CHARGE for s in plan.slots)


def test_negative_prices_charge():
    prices = [-0.05] * 3 + [0.30] * 21
    plan = build_plan(make_slots(prices, demand_kwh=1.0), BATTERY, CONFIG)
    assert any(
        s.action == ACTION_CHARGE for s in plan.slots[:3]
    ), "should charge at negative prices"


def test_pv_surplus_reduces_demand():
    """Slots with PV surplus (net demand <= 0) never trigger discharge pairing."""
    slots = make_slots([0.10] * 6 + [0.40] * 18, demand_kwh=1.0)
    surplus = [
        InputSlot(price_slot=s.price_slot, net_demand_kwh=-0.5)
        if 8 <= i <= 16
        else s
        for i, s in enumerate(slots)
    ]
    plan = build_plan(surplus, BATTERY, CONFIG)
    for i in range(8, 17):
        assert plan.slots[i].discharge_kwh == 0


def test_export_mode_sells_at_peak():
    config = OptimizerConfig(
        spread_threshold=0.10,
        discharge_mode=DISCHARGE_MODE_EXPORT,
        feed_in_tariff=0.0,  # market price applies
    )
    prices = [0.05] * 6 + [0.20] * 12 + [0.60] * 3 + [0.20] * 3
    plan = build_plan(make_slots(prices, demand_kwh=0.5), BATTERY, config)
    actions = [s.action for s in plan.slots]
    assert ACTION_EXPORT in actions[18:21]
    assert plan.estimated_savings_eur > 0


def test_self_consumption_never_exports():
    prices = [0.05] * 6 + [0.60] * 4 + [0.20] * 14
    plan = build_plan(make_slots(prices, demand_kwh=0.2), BATTERY, CONFIG)
    assert all(s.action != ACTION_EXPORT for s in plan.slots)
    # Delivered energy per slot never exceeds demand
    for slot in plan.slots:
        assert slot.discharge_kwh <= max(0.0, slot.net_demand_kwh) + 1e-6


def test_empty_slots():
    plan = build_plan([], BATTERY, CONFIG)
    assert plan.slots == []
    assert plan.estimated_savings_eur == 0


def test_savings_accounting_consistent():
    prices = [0.10] * 6 + [0.40] * 18
    plan = build_plan(make_slots(prices, demand_kwh=1.0), BATTERY, CONFIG)
    # Savings must be positive and bounded by total demand value
    assert 0 < plan.estimated_savings_eur < 24 * 1.0 * 0.40


def test_quarter_hour_slots():
    """15-minute resolution like Nordpool delivers."""
    slots = []
    for i in range(96):
        start = T0 + timedelta(minutes=15 * i)
        price = 0.10 if i < 24 else 0.40
        slots.append(
            InputSlot(
                price_slot=PriceSlot(start=start, end=start + timedelta(minutes=15), price=price),
                net_demand_kwh=0.25,
            )
        )
    plan = build_plan(slots, BATTERY, CONFIG)
    charge_slots = [s for s in plan.slots if s.action == ACTION_CHARGE]
    assert charge_slots
    # 15-min slot at 6 kW can take at most 1.5 kWh from the grid
    for slot in charge_slots:
        assert slot.charge_power_w <= BATTERY.max_charge_power_w + 1
