"""Planned EV sessions in the optimizer."""

from dataclasses import replace

import pytest
from test_optimizer import BATTERY, CONFIG, make_slots

from smart_battery_pilot.const import ACTION_AUTO, ACTION_CHARGE, ACTION_IDLE
from smart_battery_pilot.optimizer import build_plan

FULL = replace(BATTERY, soc=80.0)
# A cheap night, an expensive evening the battery is saved for.
PRICES = [0.10] * 6 + [0.30] * 12 + [0.50] * 6


def with_ev(slots, kwh: dict[int, float], blocked: bool):
    return [
        replace(s, ev_kwh=kwh.get(i, 0.0), battery_blocked=blocked) for i, s in enumerate(slots)
    ]


def test_no_ev_energy_leaves_the_plan_unchanged():
    slots = make_slots(PRICES, demand_kwh=1.0)
    plain = build_plan(slots, BATTERY, CONFIG)
    flagged = build_plan(with_ev(slots, {}, blocked=True), BATTERY, CONFIG)
    assert [s.action for s in flagged.slots] == [s.action for s in plain.slots]
    assert [s.soc_forecast for s in flagged.slots] == [s.soc_forecast for s in plain.slots]
    assert flagged.estimated_savings_eur == plain.estimated_savings_eur
    assert not any(s.battery_blocked for s in flagged.slots)


def test_allowed_sessions_are_demand_the_battery_may_serve():
    slots = make_slots([0.30] * 4, demand_kwh=0.0)
    plain = build_plan(slots, FULL, CONFIG)
    assert plain.battery_discharge_kwh == 0
    plan = build_plan(with_ev(slots, {1: 3.0}, blocked=False), FULL, CONFIG)
    assert plan.slots[1].discharge_kwh > 0
    assert plan.slots[1].ev_kwh == 3.0
    assert not plan.slots[1].battery_blocked


def test_a_blocked_session_neither_discharges_nor_turns_idle():
    slots = with_ev(make_slots([0.30] * 4, demand_kwh=1.0), {1: 7.0, 2: 7.0}, blocked=True)
    plan = build_plan(slots, FULL, CONFIG)
    for i in (1, 2):
        slot = plan.slots[i]
        assert slot.battery_blocked
        assert slot.action == ACTION_AUTO
        assert slot.discharge_kwh == 0
    # The house load waits for the slot after the session.
    assert plan.slots[1].soc_forecast == plan.slots[0].soc_forecast
    assert plan.slots[2].soc_forecast == plan.slots[1].soc_forecast
    assert plan.slots[3].discharge_kwh > 0


def test_blocked_car_energy_does_not_move_the_savings():
    slots = make_slots(PRICES, demand_kwh=1.0)
    plain = build_plan(slots, BATTERY, CONFIG)
    # A session at noon, where the plan neither charges nor discharges.
    plan = build_plan(with_ev(slots, {10: 11.0, 11: 11.0}, blocked=True), BATTERY, CONFIG)
    assert plan.estimated_savings_eur == pytest.approx(plain.estimated_savings_eur, abs=0.01)


def test_an_external_controller_leaves_no_room_for_grid_charging():
    slots = with_ev(make_slots(PRICES, demand_kwh=1.0), dict.fromkeys(range(6), 7.0), blocked=True)
    own = build_plan(slots, BATTERY, CONFIG)
    assert any(s.action == ACTION_CHARGE for s in own.slots[:6])
    external = build_plan(slots, BATTERY, replace(CONFIG, ev_no_grid_charge=True))
    assert all(s.action != ACTION_CHARGE for s in external.slots[:6])


def test_pv_still_charges_a_held_battery_but_the_car_takes_its_share_first():
    slots = make_slots([0.30] * 3, demand_kwh=-4.0)
    config = replace(CONFIG, ev_no_grid_charge=True)
    plain = build_plan(slots, BATTERY, config)
    plan = build_plan(with_ev(slots, {1: 3.0}, blocked=True), BATTERY, config)
    rise = [
        b.soc_forecast - a.soc_forecast for a, b in zip(plain.slots, plain.slots[1:], strict=False)
    ]
    rise_ev = [
        b.soc_forecast - a.soc_forecast for a, b in zip(plan.slots, plan.slots[1:], strict=False)
    ]
    # Slot 1 keeps 1 of 4 kWh surplus for the battery instead of all 4.
    assert rise_ev[0] == pytest.approx(rise[0] / 4, abs=0.2)
    # After the session the surplus charges the battery again.
    assert rise_ev[1] > 0


def test_an_unblocked_car_eats_pv_surplus_before_the_battery():
    slots = make_slots([0.30] * 2, demand_kwh=-2.0)
    plan = build_plan(with_ev(slots, {0: 5.0}, blocked=False), BATTERY, CONFIG)
    # 2 kWh PV go into the car, the other 3 kWh are grid demand at 10 % SOC.
    assert plan.slots[0].soc_forecast == BATTERY.soc
    assert plan.slots[0].action in (ACTION_AUTO, ACTION_IDLE)
