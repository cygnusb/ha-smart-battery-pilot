"""Deriving expected EV sessions from evcc's state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from smart_battery_pilot.ev_sessions import (
    SOURCE_CHARGING,
    SOURCE_NOW,
    SOURCE_PLAN,
    SOURCE_SMART_COST,
    derive,
    limit_on_price_basis,
    parse_rates,
    per_slot,
)

NOW = datetime(2026, 3, 2, 18, 0, tzinfo=UTC)
TS = NOW.timestamp()
Q = 900  # one evcc slot, seconds


def _rates(values: list[float], start: float = TS) -> list[list[float]]:
    return [[start + i * Q, start + (i + 1) * Q, v] for i, v in enumerate(values)]


def _lp(**overrides) -> dict:
    lp = {
        "connected": True,
        "charging": False,
        "chargePower": 0,
        "mode": "smart",
        "vehicleName": "car",
        "vehicleSoc": 40,
        "effectiveLimitSoc": 80,
        "limitEnergy": 0,
        "chargedEnergy": 0,
        "effectivePlanTime": None,
        "plan": None,
        "smartCostLimit": None,
        "effectiveMaxCurrent": 16,
        "phasesActive": 3,
    }
    lp.update(overrides)
    return lp


def _state(*loadpoints, planner=None, discharge_control=True) -> dict:
    return {
        "loadpoints": list(loadpoints),
        "vehicles": {"car": {"capacity": 50}},
        "forecast": {"planner": planner or []},
        "batteryDischargeControl": discharge_control,
    }


# 40 % -> 80 % of 50 kWh at 90 % charge efficiency
NEED_KWH = 0.4 * 50 / 0.9


def test_both_rate_shapes_parse():
    triples = parse_rates([[TS, TS + Q, 0.2]])
    objects = parse_rates(
        [{"start": "2026-03-02T18:00:00Z", "end": "2026-03-02T18:15:00Z", "value": 0.2}]
    )
    assert triples == objects
    assert parse_rates([["junk"], None, {"start": "x"}]) == []


def test_plan_slots_are_used_as_they_are():
    plan = [
        {"start": "2026-03-02T23:00:00Z", "end": "2026-03-02T23:15:00Z", "value": 0.1},
        {"start": "2026-03-03T01:00:00Z", "end": "2026-03-03T01:10:00Z", "value": 0.1},
    ]
    lp = _lp(effectivePlanTime="2026-03-03T06:00:00Z", plan=plan)
    forecast = derive(_state(lp), {1: {"power": 11000.0}}, NOW)
    (session,) = forecast.sessions
    assert session.source == SOURCE_PLAN
    assert [w.kwh for w in session.windows] == pytest.approx([11 * 0.25, 11 * 10 / 60])


def test_a_plan_without_power_from_the_endpoint_uses_current_and_phases():
    plan = [{"start": "2026-03-02T23:00:00Z", "end": "2026-03-03T00:00:00Z", "value": 0.1}]
    lp = _lp(effectivePlanTime="2026-03-03T06:00:00Z", plan=plan)
    (session,) = derive(_state(lp), {}, NOW).sessions
    assert session.energy_kwh == pytest.approx(16 * 3 * 230 / 1000)


def test_now_mode_charges_until_the_car_is_full():
    (session,) = derive(_state(_lp(mode="now")), {}, NOW).sessions
    assert session.source == SOURCE_NOW
    assert session.energy_kwh == pytest.approx(NEED_KWH)
    assert session.windows[0].start == NOW


def test_an_energy_limit_bounds_a_car_without_soc():
    lp = _lp(mode="now", vehicleName="", vehicleSoc=0, limitEnergy=10, chargedEnergy=4000)
    (session,) = derive(_state(lp), {}, NOW).sessions
    assert session.energy_kwh == pytest.approx(6.0)


def test_smart_cost_takes_slots_at_or_below_the_limit_in_time_order():
    planner = _rates([0.30, 0.20, 0.25, 0.19, 0.18] + [0.10] * 20)
    lp = _lp(smartCostLimit=0.20)
    forecast = derive(_state(lp, planner=planner), {}, NOW)
    (session,) = forecast.sessions
    assert session.source == SOURCE_SMART_COST
    assert forecast.price_limit == 0.20
    # the equal-price slot counts: evcc compares with <=
    assert session.windows[0].start == NOW + timedelta(seconds=Q)
    assert session.energy_kwh == pytest.approx(NEED_KWH)


def test_no_limit_means_no_smart_cost_session():
    planner = _rates([0.10] * 8)
    assert derive(_state(_lp(), planner=planner), {}, NOW).sessions == []


def test_a_car_without_any_bound_counts_only_while_charging():
    lp = _lp(vehicleName="", vehicleSoc=0, smartCostLimit=0.5, charging=False)
    planner = _rates([0.10] * 8)
    assert derive(_state(lp, planner=planner), {}, NOW).sessions == []

    lp["charging"] = True
    lp["chargePower"] = 7400
    (session,) = derive(_state(lp, planner=planner), {}, NOW).sessions
    assert session.source == SOURCE_CHARGING
    assert not session.bounded
    assert session.energy_kwh == pytest.approx(7.4 * 0.25)


@pytest.mark.parametrize(
    "overrides",
    [{"connected": False}, {"mode": "off"}, {"vehicleSoc": 80}],
)
def test_nothing_is_expected_for_these_cars(overrides):
    lp = _lp(**{"mode": "now", **overrides})
    assert derive(_state(lp), {}, NOW).sessions == []


def test_past_windows_are_dropped_and_the_running_one_counts_from_now():
    plan = [
        {"start": "2026-03-02T16:00:00Z", "end": "2026-03-02T17:00:00Z", "value": 0.1},
        {"start": "2026-03-02T17:30:00Z", "end": "2026-03-02T18:30:00Z", "value": 0.1},
    ]
    lp = _lp(effectivePlanTime="2026-03-03T06:00:00Z", plan=plan, vehicleSoc=10)
    (session,) = derive(_state(lp), {1: {"power": 11000.0}}, NOW).sessions
    assert len(session.windows) == 1
    assert session.windows[0].start == NOW
    assert session.energy_kwh == pytest.approx(5.5)


def test_two_loadpoints_add_up_per_slot():
    a = _lp(mode="now", vehicleSoc=78)
    b = _lp(mode="now", vehicleSoc=78)
    forecast = derive(_state(a, b), {}, NOW)
    assert len(forecast.sessions) == 2
    (kwh, source), *_ = per_slot(forecast.windows, [(NOW, NOW + timedelta(hours=1))])
    assert kwh == pytest.approx(2 * 0.02 * 50 / 0.9)
    assert source == SOURCE_NOW


def test_quarter_hour_windows_fit_onto_hourly_slots():
    plan = [
        {"start": "2026-03-02T20:15:00Z", "end": "2026-03-02T20:30:00Z", "value": 0.1},
        {"start": "2026-03-02T20:45:00Z", "end": "2026-03-02T21:15:00Z", "value": 0.1},
    ]
    lp = _lp(effectivePlanTime="2026-03-03T06:00:00Z", plan=plan)
    forecast = derive(_state(lp), {1: {"power": 4000.0}}, NOW)
    hour = timedelta(hours=1)
    t20 = datetime(2026, 3, 2, 20, tzinfo=UTC)
    slots = per_slot(forecast.windows, [(t20, t20 + hour), (t20 + hour, t20 + 2 * hour)])
    assert [round(k, 3) for k, _ in slots] == [2.0, 1.0]


def test_malformed_state_yields_no_sessions():
    assert derive({"loadpoints": "nope"}, {}, NOW).sessions == []
    assert derive(None, {}, NOW).sessions == []
    assert derive({"loadpoints": [None, 3]}, {}, NOW).sessions == []


def _slots(prices: list[float]):
    q = timedelta(seconds=Q)
    return [(NOW + i * q, NOW + (i + 1) * q, p) for i, p in enumerate(prices)]


def test_the_limit_moves_onto_the_planner_price_basis():
    rates = parse_rates(_rates([0.10, 0.12, 0.30, 0.20]))
    # A fixed 0.19 EUR/kWh on top, with a cent of rounding noise.
    slots = _slots([0.29, 0.31, 0.49, 0.40])
    assert limit_on_price_basis(0.20, rates, slots) == pytest.approx(0.39)
    assert limit_on_price_basis(None, rates, slots) is None
    assert limit_on_price_basis(0.20, [], slots) is None


def test_equal_tariffs_keep_the_limit():
    rates = parse_rates(_rates([0.10, 0.12, 0.30, 0.20]))
    assert limit_on_price_basis(0.20, rates, _slots([0.10, 0.12, 0.30, 0.20])) == 0.20


def test_no_line_when_the_tariffs_are_not_a_constant_offset_apart():
    """A percentage VAT on one side: no single line separates evcc's slots."""
    rates = parse_rates(_rates([0.10, 0.20, 0.30, 0.40]))
    assert limit_on_price_basis(0.20, rates, _slots([0.12, 0.24, 0.36, 0.48])) is None


def test_no_line_on_too_little_overlap():
    rates = parse_rates(_rates([0.10, 0.12, 0.30]))
    assert limit_on_price_basis(0.20, rates, _slots([0.29, 0.31, 0.49])) is None


def test_the_plan_endpoint_supplies_the_slots_when_the_state_has_none():
    lp = _lp(effectivePlanTime="2026-03-03T06:00:00Z", plan=None)
    plans = {
        1: {
            "plan": [{"start": "2026-03-02T23:00:00Z", "end": "2026-03-03T00:00:00Z"}],
            "power": 7400.0,
        }
    }
    (session,) = derive(_state(lp), plans, NOW).sessions
    assert session.source == SOURCE_PLAN
    assert session.energy_kwh == pytest.approx(7.4)


def test_a_co2_limit_is_not_compared_with_prices():
    planner = _rates([0.10] * 8)
    state = _state(_lp(smartCostLimit=300), planner=planner)
    state["smartCostType"] = "co2"
    forecast = derive(state, {}, NOW)
    assert forecast.sessions == []
    assert forecast.price_limit is None
    state["smartCostType"] = "priceforecast"
    assert derive(state, {}, NOW).sessions
