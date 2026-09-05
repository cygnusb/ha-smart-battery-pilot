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
        assert slot.power_w <= BATTERY.max_charge_power_w + 1


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


def test_negative_slots_hold_stored_energy_for_later_positive_hours():
    """Self-consumption must not dump the battery into a paid (negative) import."""
    battery = BatteryState(
        capacity_kwh=12.8,
        soc=80.0,
        min_soc=10.0,
        max_soc=95.0,
        max_charge_power_w=6000,
        max_discharge_power_w=6000,
        efficiency=90,
    )
    prices = [-0.10] * 12 + [0.30] * 12
    plan = build_plan(make_slots(prices, demand_kwh=1.0), battery, CONFIG)
    assert all(s.discharge_kwh == 0 for s in plan.slots[:12])
    assert any(s.discharge_kwh > 0 for s in plan.slots[12:])


def test_all_negative_day_does_not_discharge_stored_energy():
    battery = BatteryState(
        capacity_kwh=12.8,
        soc=80.0,
        min_soc=10.0,
        max_soc=95.0,
        max_charge_power_w=6000,
        max_discharge_power_w=6000,
        efficiency=90,
    )
    plan = build_plan(make_slots([-0.05] * 24, demand_kwh=1.0), battery, CONFIG)
    assert all(s.discharge_kwh == 0 for s in plan.slots)
    assert ACTION_IDLE in {s.action for s in plan.slots}


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


def test_export_slot_sets_discharge_power():
    config = OptimizerConfig(
        spread_threshold=0.10,
        discharge_mode=DISCHARGE_MODE_EXPORT,
        feed_in_tariff=0.0,
    )
    prices = [0.05] * 6 + [0.20] * 12 + [0.60] * 3 + [0.20] * 3
    plan = build_plan(make_slots(prices, demand_kwh=0.5), BATTERY, config)
    export_slots = [s for s in plan.slots if s.action == ACTION_EXPORT]
    assert export_slots
    for slot in export_slots:
        hours = (slot.end - slot.start).total_seconds() / 3600.0
        assert slot.power_w == pytest.approx(
            slot.discharge_kwh / hours * 1000.0, abs=1
        )
        assert 0 < slot.power_w <= BATTERY.max_discharge_power_w + 1


def test_export_unreachable_spread_sets_warning():
    """Stock feed-in 0.08 with spread 0.20 can never export — surface it."""
    config = OptimizerConfig(
        spread_threshold=0.20,
        discharge_mode=DISCHARGE_MODE_EXPORT,
        feed_in_tariff=0.08,
    )
    prices = [0.05] * 6 + [0.60] * 3 + [0.20] * 15
    plan = build_plan(make_slots(prices, demand_kwh=0.5), BATTERY, config)
    assert all(s.action != ACTION_EXPORT for s in plan.slots)
    assert "export_spread_unreachable" in plan.warnings


def test_export_does_not_treat_import_offset_as_revenue():
    """Import surcharge is what you pay, not what you earn when exporting."""
    offset = 0.20
    market_night = 0.05
    market_peak = 0.35
    # Coordinator still hands the optimizer import-total prices.
    prices = [market_night + offset] * 6 + [market_peak + offset] * 18
    config = OptimizerConfig(
        spread_threshold=0.20,
        discharge_mode=DISCHARGE_MODE_EXPORT,
        feed_in_tariff=0.0,
        price_offset=offset,
    )
    plan = build_plan(make_slots(prices, demand_kwh=0.2), BATTERY, config)
    assert all(s.action != ACTION_EXPORT for s in plan.slots)


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
        assert slot.power_w <= BATTERY.max_charge_power_w + 1


def test_no_slot_both_charges_and_discharges():
    """A force-charged slot cannot serve the house or the grid at the same time.

    The greedy pairing used to hand a cheap slot to a pricier one as a charge
    candidate after that same slot had already been credited with covering its
    own demand. The plan then asked the inverter to force-charge a full
    battery while the cost model booked a discharge that never happened.
    """
    prices = [0.05, 0.04, 0.03, 0.03, 0.04, 0.08, 0.18, 0.30, 0.34, 0.28, 0.20, 0.15,
              0.12, 0.14, 0.18, 0.24, 0.32, 0.42, 0.48, 0.44, 0.36, 0.28, 0.20, 0.12]
    slots = [
        InputSlot(
            price_slot=PriceSlot(
                start=T0 + timedelta(hours=i),
                end=T0 + timedelta(hours=i + 1),
                price=price,
            ),
            net_demand_kwh=0.6,
        )
        for i, price in enumerate(prices)
    ]
    battery = BatteryState(
        capacity_kwh=10.0,
        soc=50.0,
        min_soc=10.0,
        max_soc=95.0,
        max_charge_power_w=5000,
        max_discharge_power_w=5000,
        efficiency=90,
    )
    config = OptimizerConfig(
        spread_threshold=0.05,
        discharge_mode=DISCHARGE_MODE_EXPORT,
        feed_in_tariff=0.0,
    )

    plan = build_plan(slots, battery, config)

    conflicting = [
        s for s in plan.slots if s.action == ACTION_CHARGE and s.discharge_kwh > 1e-9
    ]
    assert conflicting == [], f"charge slots also crediting a discharge: {conflicting}"


def test_charge_slots_never_discharge_across_random_price_curves():
    """The pairing has two directions, so one guard is not enough."""
    random = __import__("random").Random(20260904)
    for _ in range(300):
        prices = [round(random.uniform(-0.1, 0.8), 3) for _ in range(24)]
        slots = [
            InputSlot(
                price_slot=PriceSlot(
                    start=T0 + timedelta(hours=i),
                    end=T0 + timedelta(hours=i + 1),
                    price=price,
                ),
                net_demand_kwh=round(random.uniform(-2, 3), 2),
            )
            for i, price in enumerate(prices)
        ]
        battery = BatteryState(
            capacity_kwh=random.choice([5.0, 10.0, 20.0]),
            soc=random.uniform(10, 95),
            min_soc=10.0,
            max_soc=95.0,
            max_charge_power_w=random.choice([2000, 5000, 10000]),
            max_discharge_power_w=random.choice([2000, 5000]),
            efficiency=random.choice([85, 90, 95]),
        )
        config = OptimizerConfig(
            spread_threshold=random.choice([0.0, 0.05, 0.2]),
            discharge_mode=random.choice(
                [DISCHARGE_MODE_SELF_CONSUMPTION, DISCHARGE_MODE_EXPORT]
            ),
            feed_in_tariff=random.choice([0.0, 0.08]),
            price_offset=random.choice([0.0, 0.15]),
        )

        plan = build_plan(slots, battery, config)

        assert not [
            s for s in plan.slots if s.action == ACTION_CHARGE and s.discharge_kwh > 1e-9
        ]
