"""Coordinator side of EV charging: clean training data, live wallbox and controller state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_coordinator import _coordinator, _FakeHass

from smart_battery_pilot.const import (
    CONF_CONSUMPTION_ENTITY,
    CONF_EV_BATTERY_POLICY,
    CONF_EV_POWER_ENTITIES,
    CONF_EXTERNAL_CONTROL_ENTITY,
    EV_POLICY_ALLOW,
    EV_POLICY_BLOCK,
)

HOUR = datetime(2026, 10, 10, 10, 0, tzinfo=UTC)


def _row(hour: int, *, mean=None, change=None) -> dict:
    return {"start": HOUR + timedelta(hours=hour), "mean": mean, "change": change}


def _setup(*, house_unit="W", ev_units=("kW",)):
    hass = _FakeHass()
    ev_entities = [f"sensor.wallbox_{i}" for i in range(len(ev_units))]
    hass.states.set("sensor.house", 500, {"unit_of_measurement": house_unit})
    for entity_id, unit in zip(ev_entities, ev_units, strict=True):
        hass.states.set(entity_id, 0, {"unit_of_measurement": unit})
    coord = _coordinator(
        hass,
        **{CONF_CONSUMPTION_ENTITY: "sensor.house", CONF_EV_POWER_ENTITIES: ev_entities},
    )
    return hass, coord, ev_entities


# --- training data -----------------------------------------------------------------


def test_wallbox_energy_is_taken_out_of_each_hour():
    _hass, coord, (wallbox,) = _setup()
    house = [_row(0, mean=600.0), _row(1, mean=11600.0), _row(2, mean=700.0)]
    ev = {wallbox: [_row(1, mean=11.0)]}
    samples = coord._build_samples(house, [], ev)
    assert [round(s.kwh, 3) for s in samples] == [0.6, 0.6, 0.7]


def test_the_house_never_goes_below_zero():
    """Averaging can leave the wallbox a little above the whole-house meter."""
    _hass, coord, (wallbox,) = _setup()
    samples = coord._build_samples([_row(0, mean=10900.0)], [], {wallbox: [_row(0, mean=11.04)]})
    assert samples[0].kwh == 0.0


def test_several_wallboxes_and_mixed_units_add_up():
    _hass, coord, (kw_box, w_box) = _setup(ev_units=("kW", "W"))
    house = [_row(0, mean=8000.0)]
    ev = {kw_box: [_row(0, mean=4.0)], w_box: [_row(0, mean=3500.0)]}
    samples = coord._build_samples(house, [], ev)
    assert samples[0].kwh == pytest.approx(0.5)


def test_an_energy_meter_wallbox_is_subtracted_by_its_hourly_change():
    _hass, coord, (wallbox,) = _setup(house_unit="kWh", ev_units=("kWh",))
    samples = coord._build_samples([_row(0, change=7.5)], [], {wallbox: [_row(0, change=7.0)]})
    assert samples[0].kwh == pytest.approx(0.5)


def test_without_wallbox_rows_the_samples_are_unchanged():
    _hass, coord, _ = _setup()
    house = [_row(0, mean=600.0), _row(1, mean=11600.0)]
    assert [s.kwh for s in coord._build_samples(house, [])] == [0.6, 11.6]


# --- live state --------------------------------------------------------------------


def test_live_wallbox_power_is_summed_in_watts():
    hass, coord, (kw_box, w_box) = _setup(ev_units=("kW", "W"))
    hass.states.set(kw_box, 3.7, {"unit_of_measurement": "kW"})
    hass.states.set(w_box, 1200, {"unit_of_measurement": "W"})
    assert coord.ev_power_w() == pytest.approx(4900.0)


def test_an_unavailable_wallbox_counts_as_no_reading():
    hass, coord, (wallbox,) = _setup()
    hass.states.set(wallbox, "unavailable", {"unit_of_measurement": "kW"})
    assert coord.ev_power_w() is None


def test_the_policy_defaults_to_allow():
    _hass, coord, _ = _setup()
    assert coord.ev_policy() == EV_POLICY_ALLOW
    coord.entry.options[CONF_EV_BATTERY_POLICY] = EV_POLICY_BLOCK
    assert coord.ev_policy() == EV_POLICY_BLOCK


@pytest.mark.parametrize(
    ("entity_id", "state", "active"),
    [
        ("sensor.evcc_battery_mode_value", "normal", False),
        ("sensor.evcc_battery_mode_value", "hold", True),
        ("sensor.evcc_battery_mode_value", "charge", True),
        ("sensor.evcc_battery_mode_value", "unknown", False),
        ("sensor.evcc_battery_mode_value", "unavailable", False),
        ("binary_sensor.wallbox_holds_battery", "on", True),
        ("binary_sensor.wallbox_holds_battery", "off", False),
        ("input_boolean.manual_battery", "on", True),
        ("switch.evcc_hold", "unavailable", False),
    ],
)
def test_external_control_reads_on_off_and_mode_entities(entity_id, state, active):
    hass = _FakeHass()
    hass.states.set(entity_id, state)
    coord = _coordinator(hass, **{CONF_EXTERNAL_CONTROL_ENTITY: entity_id})
    assert coord.external_control_active() is active


def test_no_external_entity_means_no_external_control():
    hass = _FakeHass()
    assert _coordinator(hass).external_control_active() is False
