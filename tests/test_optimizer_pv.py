"""PV surplus charging scenarios (summer behavior)."""

from datetime import datetime, timedelta, timezone

from smart_battery_pilot.const import (
    ACTION_AUTO,
    ACTION_CHARGE,
    ACTION_IDLE,
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
T0 = datetime(2026, 6, 11, 0, 0, tzinfo=TZ)  # summer day

BATTERY = BatteryState(
    capacity_kwh=12.8,
    soc=40.0,
    min_soc=10.0,
    max_soc=95.0,
    max_charge_power_w=6000,
    max_discharge_power_w=6000,
    efficiency=90,
)
CONFIG = OptimizerConfig(
    spread_threshold=0.20, discharge_mode=DISCHARGE_MODE_SELF_CONSUMPTION
)


def summer_slots() -> list[InputSlot]:
    """Cheap-ish day with PV surplus 9-17h, evening peak 20-23h."""
    slots = []
    for h in range(24):
        start = T0 + timedelta(hours=h)
        price = 0.45 if 20 <= h <= 22 else 0.30
        consumption = 0.8
        pv = 4.0 if 9 <= h <= 16 else 0.0
        slots.append(
            InputSlot(
                price_slot=PriceSlot(start=start, end=start + timedelta(hours=1), price=price),
                net_demand_kwh=consumption - pv,
                pv_kwh=pv,
            )
        )
    return slots


def test_summer_pv_refill_no_idle_no_charge():
    plan = build_plan(summer_slots(), BATTERY, CONFIG)
    actions = [s.action for s in plan.slots]

    # PV will refill the battery: no grid charging anywhere
    assert ACTION_CHARGE not in actions
    # Surplus hours are auto (inverter charges from PV), never idle
    assert all(a == ACTION_AUTO for a in actions[9:17])
    # Evening peak is covered by the battery
    assert all(s.discharge_kwh > 0 for s in plan.slots[20:23])
    # Morning hours need not be locked: PV refills before the evening peak
    assert all(a == ACTION_AUTO for a in actions[0:9])


def test_soc_forecast_rises_with_pv():
    plan = build_plan(summer_slots(), BATTERY, CONFIG)
    soc_8h = plan.slots[8].soc_forecast
    soc_17h = plan.slots[16].soc_forecast
    assert soc_17h > soc_8h, "SOC forecast must rise during PV surplus hours"
    assert soc_17h <= BATTERY.max_soc + 0.1


def test_pv_kwh_passed_through():
    plan = build_plan(summer_slots(), BATTERY, CONFIG)
    assert plan.slots[12].pv_kwh == 4.0
    assert plan.slots[3].pv_kwh == 0.0


def test_winter_unchanged_idle_still_works():
    """No PV: scarce energy must still be reserved (idle) for the peak."""
    slots = []
    for h in range(24):
        start = T0 + timedelta(hours=h)
        price = 0.45 if 6 <= h <= 9 else 0.30
        slots.append(
            InputSlot(
                price_slot=PriceSlot(start=start, end=start + timedelta(hours=1), price=price),
                net_demand_kwh=1.0,
                pv_kwh=0.0,
            )
        )
    battery = BatteryState(
        capacity_kwh=12.8, soc=45.0, min_soc=10.0, max_soc=95.0,
        max_charge_power_w=6000, max_discharge_power_w=6000, efficiency=90,
    )
    plan = build_plan(slots, battery, CONFIG)
    actions = [s.action for s in plan.slots]
    assert ACTION_IDLE in actions[:6]
    assert all(s.discharge_kwh > 0 for s in plan.slots[6:10])
