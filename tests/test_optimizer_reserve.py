"""Backup reserve in the planner: a floor arbitrage never spends, refilled by a deadline."""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_optimizer import BATTERY, CONFIG, make_slots

from smart_battery_pilot.const import ACTION_CHARGE, ACTION_EXPORT, DISCHARGE_MODE_EXPORT
from smart_battery_pilot.optimizer import build_plan

PEAKY = [0.10] * 6 + [0.50] * 4 + [0.10] * 6 + [0.50] * 4


def _charges(plan):
    return [i for i, s in enumerate(plan.slots) if s.action == ACTION_CHARGE]


def test_an_inactive_reserve_is_todays_plan():
    slots = make_slots(PEAKY, demand_kwh=1.5)
    today = build_plan(slots, replace(BATTERY, soc=60.0), CONFIG)
    for reserve in (None, 0.0, BATTERY.min_soc):
        assert build_plan(slots, replace(BATTERY, soc=60.0, reserve_soc=reserve), CONFIG) == today


def test_arbitrage_never_plans_below_the_reserve():
    plan = build_plan(
        make_slots(PEAKY, demand_kwh=1.5), replace(BATTERY, soc=60.0, reserve_soc=40.0), CONFIG
    )
    assert min(s.soc_forecast for s in plan.slots) >= 40.0 - 0.05


def test_export_never_plans_below_the_reserve():
    config = replace(CONFIG, discharge_mode=DISCHARGE_MODE_EXPORT, feed_in_tariff=0.0)
    plan = build_plan(
        make_slots(PEAKY, demand_kwh=0.2), replace(BATTERY, soc=90.0, reserve_soc=50.0), config
    )
    assert any(s.action == ACTION_EXPORT for s in plan.slots)
    assert min(s.soc_forecast for s in plan.slots) >= 50.0 - 0.05


def test_a_deficit_is_refilled_in_the_cheapest_slots_before_the_deadline():
    prices = [0.30, 0.12, 0.25, 0.11, 0.40, 0.40] + [0.05] * 6  # cheapest slots after 6 h
    config = replace(CONFIG, reserve_refill_hours=6.0, spread_threshold=5.0)  # no arbitrage
    plan = build_plan(make_slots(prices, 0.0), replace(BATTERY, soc=20.0, reserve_soc=40.0), config)
    # 20 % of 12.8 kWh fits one slot: the cheapest before the deadline (slot 3 @ 0.11).
    assert _charges(plan) == [3]
    assert plan.slots[5].soc_forecast >= 40.0 - 0.05
    assert plan.reserve_refill_kwh > 0
    assert "reserve_refill_incomplete" not in plan.warnings


def test_the_soc_forecast_shows_the_real_soc_below_the_reserve():
    config = replace(CONFIG, reserve_refill_hours=6.0, spread_threshold=5.0)
    plan = build_plan(
        make_slots([0.30] * 3 + [0.10] * 9, 0.0),
        replace(BATTERY, soc=20.0, reserve_soc=40.0),
        config,
    )
    assert plan.slots[0].soc_forecast == 20.0


def test_pv_alone_refills_without_grid_charge():
    slots = make_slots([0.20] * 12, 0.0)
    slots = [replace(s, net_demand_kwh=-3.0, pv_kwh=3.0) for s in slots]
    plan = build_plan(
        slots, replace(BATTERY, soc=20.0, reserve_soc=40.0), replace(CONFIG, spread_threshold=5.0)
    )
    assert _charges(plan) == []
    assert plan.reserve_refill_kwh == 0.0


def test_a_too_short_deadline_refills_what_fits_and_warns():
    config = replace(CONFIG, reserve_refill_hours=1.0, spread_threshold=5.0)
    battery = replace(BATTERY, soc=10.0, reserve_soc=90.0)  # needs ~10 kWh, 1 h at 6 kW cannot
    plan = build_plan(make_slots([0.20] * 12, 0.0), battery, config)
    assert _charges(plan)[:1] == [0]
    assert "reserve_refill_incomplete" in plan.warnings


def test_refill_cost_is_not_counted_against_the_savings():
    config = replace(CONFIG, reserve_refill_hours=6.0, spread_threshold=5.0)  # no arbitrage
    plan = build_plan(
        make_slots([0.30] * 12, 0.5), replace(BATTERY, soc=20.0, reserve_soc=40.0), config
    )
    assert plan.reserve_refill_cost_eur > 0
    assert plan.estimated_savings_eur >= 0.0
    assert "plan_worse_than_baseline" not in plan.warnings
    assert _charges(plan)  # the refill survived the baseline check


def test_refill_slots_obey_the_cold_derating_cap():
    config = replace(CONFIG, reserve_refill_hours=12.0, spread_threshold=5.0)
    battery = replace(BATTERY, soc=10.0, reserve_soc=50.0, charge_factor=0.25)
    plan = build_plan(make_slots([0.20] * 12, 0.0), battery, config)
    cap_kwh = BATTERY.max_charge_power_w / 1000.0 * 0.25 * (BATTERY.efficiency / 100.0) ** 0.5
    previous = battery.soc
    for slot in plan.slots:
        stored = (slot.soc_forecast - previous) / 100.0 * BATTERY.capacity_kwh
        assert stored <= cap_kwh + 0.013
        if slot.action == ACTION_CHARGE and stored >= cap_kwh - 0.013:
            assert slot.power_w == BATTERY.max_charge_power_w
        previous = slot.soc_forecast


def _below_reserve(plan, reserve):
    return [
        (i, s.action, s.soc_forecast)
        for i, s in enumerate(plan.slots)
        if (s.discharge_kwh > 0 or s.action == ACTION_EXPORT) and s.soc_forecast < reserve - 0.05
    ]


def test_no_discharge_below_the_reserve_while_it_is_still_being_refilled():
    """Starting under the reserve, a pairing of a cheap charge slot with a later
    discharge slot ran the battery back down to 10 % hours before the refill."""
    prices = [0.10, 0.50] + [0.30] * 9 + [0.01]
    battery = replace(BATTERY, soc=10.0, reserve_soc=50.0)
    plan = build_plan(make_slots(prices, demand_kwh=1.0), battery, CONFIG)
    assert _below_reserve(plan, 50.0) == []


def test_no_export_below_the_reserve_while_it_is_still_being_refilled():
    prices = [0.10, 0.50] + [0.30] * 9 + [0.01]
    config = replace(CONFIG, discharge_mode=DISCHARGE_MODE_EXPORT, feed_in_tariff=0.0)
    battery = replace(BATTERY, soc=10.0, reserve_soc=50.0)
    plan = build_plan(make_slots(prices, demand_kwh=1.0), battery, config)
    assert _below_reserve(plan, 50.0) == []


def test_the_all_auto_fallback_keeps_the_mandatory_refill(monkeypatch):
    """The refill is excluded from the baseline check so it cannot be thrown
    away as 'worse than doing nothing' - the fallback must not drop it either."""
    from smart_battery_pilot import optimizer

    real = optimizer._simulate_self_consumption

    def perfect_baseline(n, demand, *args):
        _, levels = real(n, demand, *args)
        return list(demand), levels  # a baseline no plan can beat

    monkeypatch.setattr(optimizer, "_simulate_self_consumption", perfect_baseline)
    config = replace(CONFIG, reserve_refill_hours=6.0)
    plan = build_plan(
        make_slots([0.30, 0.12, 0.25, 0.11, 0.40, 0.40] + [0.50] * 6, 1.0),
        replace(BATTERY, soc=20.0, reserve_soc=40.0),
        config,
    )
    assert "plan_worse_than_baseline" in plan.warnings
    assert _charges(plan) == [3]
    assert plan.slots[3].power_w > 0
    assert plan.reserve_refill_kwh > 0
    assert plan.grid_charge_kwh == pytest.approx(plan.reserve_refill_kwh, abs=1e-3)
