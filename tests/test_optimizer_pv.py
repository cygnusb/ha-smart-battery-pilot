"""PV surplus charging scenarios (summer behavior)."""

from datetime import datetime, timedelta, timezone

from smart_battery_pilot.const import (
    ACTION_AUTO,
    ACTION_CHARGE,
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


def test_pv_surplus_and_grid_share_charge_power():
    """Grid charge plus modelled PV charge must not exceed inverter charge power."""
    import math

    battery = BatteryState(
        capacity_kwh=12.8,
        soc=10.0,
        min_soc=10.0,
        max_soc=95.0,
        max_charge_power_w=6000,
        max_discharge_power_w=6000,
        efficiency=90,
    )
    slots = []
    for h in range(24):
        start = T0 + timedelta(hours=h)
        if 11 <= h <= 13:
            # Small surplus so leftover inverter headroom remains for grid charging.
            price, consumption, pv = -0.05, 0.5, 1.5
        elif 20 <= h <= 22:
            price, consumption, pv = 0.80, 3.0, 0.0
        else:
            price, consumption, pv = 0.30, 0.8, 0.0
        slots.append(
            InputSlot(
                price_slot=PriceSlot(start=start, end=start + timedelta(hours=1), price=price),
                net_demand_kwh=consumption - pv,
                pv_kwh=pv,
            )
        )
    plan = build_plan(slots, battery, CONFIG)
    eta_one_way = math.sqrt(battery.efficiency / 100.0)
    max_stored = battery.max_charge_power_w / 1000.0 * eta_one_way
    prev_soc = battery.soc
    saw_midday_charge = False
    for i, slot in enumerate(plan.slots):
        stored_added = (slot.soc_forecast - prev_soc) / 100.0 * battery.capacity_kwh
        assert stored_added <= max_stored + 0.05, (
            f"{slot.start}: stored +{stored_added:.2f} kWh exceeds "
            f"charge power cap {max_stored:.2f} kWh"
        )
        if 11 <= i <= 13 and slot.action == ACTION_CHARGE:
            saw_midday_charge = True
            pv_charge_w = max(0.0, -slot.net_demand_kwh) * 1000.0
            assert slot.power_w + pv_charge_w <= battery.max_charge_power_w + 1
        prev_soc = slot.soc_forecast
    assert saw_midday_charge, "negative-price surplus slots should still grid-charge leftover headroom"


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


# Prices/net demand of the sunny summer day from issue #16, where holding
# energy back for the morning peak used to plan a *worse* day than doing
# nothing: the reservation filled capacity that midday PV then curtailed.
CURTAILMENT_PRICES = [
    0.24, 0.22, 0.21, 0.21, 0.22, 0.25, 0.30, 0.34, 0.31, 0.22, 0.14, 0.09,
    0.06, 0.05, 0.06, 0.10, 0.16, 0.24, 0.34, 0.42, 0.40, 0.33, 0.27, 0.25,
]
CURTAILMENT_NET = [
    0.40, 0.35, 0.35, 0.35, 0.40, 0.60, 1.60, 1.60, 0.50, -1.50, -2.60, -3.20,
    -3.30, -3.00, -2.30, -1.40, -0.40, 0.50, 1.60, 2.00, 1.80, 1.40, 0.90, 0.50,
]


def curtailment_slots() -> list[InputSlot]:
    """Sunny day: morning peak, PV bell 09-16h that overfills, evening peak."""
    slots = []
    for h in range(24):
        start = T0 + timedelta(hours=h)
        slots.append(
            InputSlot(
                price_slot=PriceSlot(
                    start=start, end=start + timedelta(hours=1), price=CURTAILMENT_PRICES[h]
                ),
                net_demand_kwh=CURTAILMENT_NET[h],
                pv_kwh=max(0.0, -CURTAILMENT_NET[h]),
            )
        )
    return slots


CURTAILMENT_BATTERY = BatteryState(
    capacity_kwh=10.0, soc=60.0, min_soc=10.0, max_soc=95.0,
    max_charge_power_w=5000, max_discharge_power_w=5000, efficiency=90,
)
CURTAILMENT_CONFIG = OptimizerConfig(
    spread_threshold=0.10,
    discharge_mode=DISCHARGE_MODE_SELF_CONSUMPTION,
    feed_in_tariff=0.08,
)


def test_reservation_released_before_curtailed_pv():
    """Energy a later PV surplus replaces for free must not be reserved."""
    plan = build_plan(curtailment_slots(), CURTAILMENT_BATTERY, CURTAILMENT_CONFIG)
    actions = [s.action for s in plan.slots]

    # The plan must beat doing nothing, not lose money against it.
    assert plan.estimated_savings_eur > 0, actions

    # It used to hold 1.02 kWh back over night for the morning peak, only for
    # the midday surplus to be curtailed instead - the battery ended the day
    # having delivered *less* to the house than an untouched inverter.
    baseline = _self_consumption_delivered(
        curtailment_slots(), CURTAILMENT_BATTERY
    )
    assert plan.battery_discharge_kwh >= baseline - 1e-6, (
        f"plan delivers {plan.battery_discharge_kwh:.2f} kWh, "
        f"doing nothing delivers {baseline:.2f} kWh"
    )

    # Hour 0 is the priciest of the pre-dawn block and the surplus refills what
    # it spends, so it discharges instead of idling. Hours 2-4 stay reserved:
    # the battery really does reach min SOC at 8h, before any PV arrives.
    assert actions[0] == ACTION_AUTO
    assert plan.slots[0].discharge_kwh > 0
    # 17h is still legitimately reserved, and the evening peak is still served.
    assert actions[17] == ACTION_IDLE
    assert all(s.discharge_kwh > 0 for s in plan.slots[18:23])


def _self_consumption_delivered(
    slots: list[InputSlot], battery: BatteryState
) -> float:
    """kWh the battery hands the load with no planning at all (hourly slots)."""
    import math

    eta_one_way = math.sqrt(battery.efficiency / 100.0)
    e_max = (battery.max_soc - battery.min_soc) / 100.0 * battery.capacity_kwh
    energy = min(e_max, (battery.soc - battery.min_soc) / 100.0 * battery.capacity_kwh)
    total = 0.0
    for s in slots:
        demand = max(0.0, s.net_demand_kwh)
        cap = battery.max_discharge_power_w / 1000.0 * s.price_slot.hours
        used = min(min(demand, cap) / eta_one_way, energy)
        total += used * eta_one_way
        surplus = min(
            max(0.0, -s.net_demand_kwh) * eta_one_way,
            battery.max_charge_power_w / 1000.0 * s.price_slot.hours * eta_one_way,
        )
        energy = min(energy - used + surplus, e_max)
    return total


def test_plan_never_worse_than_doing_nothing():
    """The all-auto baseline is always available, so savings cannot be negative."""
    import random

    rng = random.Random(20260905)
    for _ in range(2000):
        battery = BatteryState(
            capacity_kwh=rng.uniform(5.0, 20.0),
            soc=rng.uniform(10.0, 95.0),
            min_soc=10.0,
            max_soc=95.0,
            max_charge_power_w=rng.choice([3000, 5000, 8000]),
            max_discharge_power_w=rng.choice([3000, 5000, 8000]),
            efficiency=rng.choice([85, 90, 95]),
        )
        config = OptimizerConfig(
            spread_threshold=rng.choice([0.05, 0.10, 0.20]),
            discharge_mode=rng.choice(
                [DISCHARGE_MODE_SELF_CONSUMPTION, DISCHARGE_MODE_EXPORT]
            ),
            feed_in_tariff=rng.choice([0.0, 0.08]),
        )
        slots = []
        for h in range(24):
            start = T0 + timedelta(hours=h)
            pv = rng.uniform(0.0, 4.0) if 7 <= h <= 17 else 0.0
            slots.append(
                InputSlot(
                    price_slot=PriceSlot(
                        start=start,
                        end=start + timedelta(hours=1),
                        price=round(rng.uniform(-0.05, 0.50), 3),
                    ),
                    net_demand_kwh=round(rng.uniform(0.1, 2.5) - pv, 3),
                    pv_kwh=round(pv, 3),
                )
            )
        plan = build_plan(slots, battery, config)
        # The last-resort fallback must stay unused: the assignment itself is
        # what has to get this right, not the guard behind it.
        assert "plan_worse_than_baseline" not in plan.warnings
        assert plan.estimated_savings_eur >= 0.0, (
            f"negative savings {plan.estimated_savings_eur} for "
            f"{battery} {config} "
            f"prices={[s.price_slot.price for s in slots]} "
            f"net={[s.net_demand_kwh for s in slots]}"
        )


def test_losing_plan_falls_back_to_doing_nothing(monkeypatch):
    """The guard behind the assignment: never ship a plan that loses money."""
    from smart_battery_pilot import optimizer

    real = optimizer._simulate_self_consumption

    def inflated_baseline(*args, **kwargs):
        # Pretend the do-nothing battery is far better than it is, which drives
        # any plan's savings negative. Cheaper than hunting for a scenario the
        # assignment still gets wrong - and it exercises the same branch.
        delivered, levels = real(*args, **kwargs)
        return [d + 5.0 for d in delivered], levels

    monkeypatch.setattr(optimizer, "_simulate_self_consumption", inflated_baseline)

    plan = build_plan(summer_slots(), BATTERY, CONFIG)
    assert "plan_worse_than_baseline" in plan.warnings
    assert plan.estimated_savings_eur == 0.0
    assert all(s.action == ACTION_AUTO for s in plan.slots)
    assert plan.grid_charge_kwh == 0.0
    assert all(s.power_w == 0.0 for s in plan.slots)
    assert len(plan.slots) == 24
