"""Battery benefit vs. pilot savings in the coordinator.

The battery benefit (net and gross) is what the battery is worth, pilot or
not. The pilot savings count only energy the pilot itself charged from the
grid or held back.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import math

import pytest
from test_coordinator import NOW, _coordinator, _FakeHass, _plan_slot

from smart_battery_pilot.const import ACTION_AUTO, ACTION_CHARGE, ACTION_IDLE
from smart_battery_pilot.coordinator import SBPData
from smart_battery_pilot.optimizer import Plan
from smart_battery_pilot.sensor import BatteryGrossSensor, PilotSavingsSensor

ETA = math.sqrt(0.9)  # default roundtrip efficiency 90 %
T0 = NOW.replace(hour=3, minute=0)


def _run(coro):
    return asyncio.run(coro)


def _meters(hass, charge, discharge, soc=90.0):
    hass.states.set("sensor.charge_energy", charge)
    hass.states.set("sensor.discharge_energy", discharge)
    hass.states.set("sensor.soc", soc)


def _plan(*slots):
    return Plan(slots=list(slots))


def _step(coord, plan, action, when):
    """A mode change or slot boundary: what the executor triggers."""
    coord._last_applied = action
    coord._note_conditions(plan, when)


def test_the_battery_benefit_counts_while_the_pilot_is_off():
    """It is the battery's worth, pilot or not; with the pilot off the inverter
    runs on its own, i.e. in auto mode (PV charge at the feed-in tariff)."""
    hass = _FakeHass()
    coord = _coordinator(hass, steering=False)
    plan = _plan(_plan_slot("auto", 0.40, T0, hours=6))
    for i in range(4):
        _meters(hass, 1.0 * i, 2.0 * i)
        coord._update_actual_savings(plan, T0 + timedelta(minutes=30 * i))

    assert coord._acc_savings_eur == pytest.approx(3 * (2 * 0.40 - 1 * 0.08))
    assert coord._acc_gross_eur == pytest.approx(3 * 2 * 0.40)
    assert coord.pilot_ledger.savings_eur == 0.0


def test_an_auto_only_pilot_saves_nothing():
    """The reported case: switched on, never anything but auto."""
    hass = _FakeHass()
    coord = _coordinator(hass)
    plan = _plan(_plan_slot("auto", 0.40, T0, hours=6))
    coord._last_applied = ACTION_AUTO
    for i in range(4):
        _meters(hass, 1.0 * i, 2.0 * i)
        coord._update_actual_savings(plan, T0 + timedelta(minutes=30 * i))
    assert coord.pilot_ledger.savings_eur == 0.0
    assert coord._acc_gross_eur > 0


def test_grid_charge_used_later_earns_the_spread():
    hass = _FakeHass()
    coord = _coordinator(hass)
    plan = _plan(_plan_slot("charge", 0.10, T0), _plan_slot("auto", 0.40, T0 + timedelta(hours=1)))
    _meters(hass, 0.0, 0.0)
    _step(coord, plan, ACTION_CHARGE, T0)
    _meters(hass, 2.0, 0.0)
    _step(coord, plan, ACTION_AUTO, T0 + timedelta(hours=1))
    _meters(hass, 2.0, 2.0)
    _step(coord, plan, ACTION_AUTO, T0 + timedelta(hours=2))

    assert coord.pilot_ledger.savings_eur == pytest.approx(2 * (0.40 * ETA - 0.10 / ETA))


def test_held_back_energy_earns_the_later_price():
    hass = _FakeHass()
    coord = _coordinator(hass)
    plan = _plan(
        _plan_slot("idle", 0.20, T0), _plan_slot("auto", 0.40, T0 + timedelta(hours=1))
    )  # the idle slot forecasts 1 kWh of demand
    _meters(hass, 0.0, 0.0)
    _step(coord, plan, ACTION_IDLE, T0)
    _step(coord, plan, ACTION_AUTO, T0 + timedelta(hours=1))
    _meters(hass, 0.0, 1.0 / ETA)
    _step(coord, plan, ACTION_AUTO, T0 + timedelta(hours=2))

    assert coord.pilot_ledger.savings_eur == pytest.approx(0.40 - 0.20)


def test_a_stale_charge_mode_books_nothing_while_the_pilot_is_off():
    hass = _FakeHass()
    coord = _coordinator(hass, steering=False)
    plan = _plan(_plan_slot("charge", 0.10, T0, hours=3))
    _meters(hass, 0.0, 0.0)
    _step(coord, plan, ACTION_CHARGE, T0)
    _meters(hass, 3.0, 0.0)
    _step(coord, plan, ACTION_CHARGE, T0 + timedelta(hours=1))
    assert coord.pilot_ledger.total_kwh == 0.0


def test_a_meter_reset_books_nothing():
    hass = _FakeHass()
    coord = _coordinator(hass)
    plan = _plan(_plan_slot("charge", 0.10, T0, hours=3))
    _meters(hass, 100.0, 0.0)
    _step(coord, plan, ACTION_CHARGE, T0)
    _meters(hass, 1.0, 0.0)
    _step(coord, plan, ACTION_CHARGE, T0 + timedelta(hours=1))
    assert coord.pilot_ledger.total_kwh == 0.0


def test_lots_never_exceed_what_the_battery_holds():
    hass = _FakeHass()
    coord = _coordinator(hass)
    plan = _plan(_plan_slot("charge", 0.10, T0, hours=3))
    _meters(hass, 0.0, 0.0, soc=20.0)  # 10 % above min SOC of a 10 kWh battery = 1 kWh
    _step(coord, plan, ACTION_CHARGE, T0)
    _meters(hass, 5.0, 0.0, soc=20.0)
    _step(coord, plan, ACTION_CHARGE, T0 + timedelta(hours=1))
    assert coord.pilot_ledger.total_kwh == pytest.approx(1.0)


def test_gross_and_pilot_savings_survive_a_restart():
    hass = _FakeHass()
    coord = _coordinator(hass)
    coord._acc_gross_eur = 12.5
    coord.pilot_ledger.book_charge(1.0, grid_price=0.10, eta_one_way=ETA)
    coord.pilot_ledger.savings_eur = 3.0
    _run(coord.async_persist())

    restarted = _coordinator(hass)
    restarted._store = coord._store
    _run(restarted.async_setup())
    assert restarted._acc_gross_eur == 12.5
    assert restarted.pilot_ledger.savings_eur == 3.0
    assert restarted.pilot_ledger.total_kwh == pytest.approx(1.0)


def _value(sensor_cls, coord):
    sensor = object.__new__(sensor_cls)
    sensor.coordinator = coord
    return sensor.native_value


def test_the_new_sensors_report_the_totals():
    hass = _FakeHass()
    coord = _coordinator(hass)
    coord._acc_gross_eur = 12.345
    coord.pilot_ledger.savings_eur = 1.2345
    coord.data = SBPData(plan=Plan(), valid=True, **coord._savings_fields())
    assert _value(BatteryGrossSensor, coord) == 12.345
    assert _value(PilotSavingsSensor, coord) == 1.234
