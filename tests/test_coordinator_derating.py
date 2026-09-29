"""Coordinator side of cold-weather charging: factor, warnings, persistence."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging

import pytest
from test_coordinator import _coordinator
from test_coordinator import _FakeHass as _CoordHass

from smart_battery_pilot.const import (
    CONF_BATTERY_TEMPERATURE_ENTITY,
    CONF_CHARGE_DERATING,
    CONF_DERATING_0C,
    CONF_DERATING_5C,
)
from smart_battery_pilot.forecast.charge_rate import ChargeSample

DERATING = {
    CONF_CHARGE_DERATING: True,
    CONF_BATTERY_TEMPERATURE_ENTITY: "sensor.battery_temp",
}


def _run(coro):
    return asyncio.run(coro)


def _hass_with_prices(temp=None, unit="°C"):
    hass = _CoordHass()
    hass.states.set("sensor.price", 0.30, {"today": [0.10] * 12 + [0.50] * 12})
    hass.states.set("sensor.soc", 20.0)
    if temp is not None:
        hass.states.set("sensor.battery_temp", temp, {"unit_of_measurement": unit})
    return hass


def test_derating_off_passes_factor_one():
    hass = _hass_with_prices(temp=0.0)
    coord = _coordinator(hass)
    result = _run(coord._async_update_data())
    assert result.inputs["battery"]["charge_factor"] == 1.0
    assert result.inputs["charge_derating"] == {
        "enabled": False,
        "temperature": None,
        "factor": 1.0,
        "source": "off",
    }


def test_a_cold_battery_uses_the_curve():
    hass = _hass_with_prices(temp=2.5)
    coord = _coordinator(hass, **DERATING)
    result = _run(coord._async_update_data())
    assert result.inputs["charge_derating"]["factor"] == 0.15
    assert result.inputs["charge_derating"]["source"] == "curve"
    assert result.inputs["battery"]["charge_factor"] == pytest.approx(0.15)


def test_fahrenheit_is_converted():
    hass = _hass_with_prices(temp=36.5, unit="°F")  # 2.5 °C
    coord = _coordinator(hass, **DERATING)
    factor, _, temperature = coord.charge_factor()
    assert round(temperature, 2) == 2.5
    assert round(factor, 3) == 0.15


def test_the_sliders_shape_the_curve():
    hass = _hass_with_prices(temp=0.0)
    coord = _coordinator(hass, **DERATING, **{CONF_DERATING_0C: 40, CONF_DERATING_5C: 60})
    assert coord.charge_factor()[0] == 0.4


def test_missing_temperature_falls_back_to_one_and_warns_once(caplog):
    hass = _hass_with_prices()
    coord = _coordinator(hass, **DERATING)
    caplog.set_level(logging.DEBUG, logger="smart_battery_pilot.coordinator")
    first = _run(coord._async_update_data())
    _run(coord._async_update_data())
    assert first.inputs["charge_derating"]["factor"] == 1.0
    assert first.inputs["charge_derating"]["source"] == "no_temperature"
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and r.name.endswith("coordinator")
    ]
    assert len(warnings) == 1
    assert "sensor.battery_temp" in warnings[0].getMessage()


def test_samples_survive_a_restart():
    hass = _hass_with_prices(temp=2.5)
    coord = _coordinator(hass, **DERATING)
    at = datetime(2026, 1, 15, tzinfo=UTC)
    for _ in range(6):
        coord.charge_model.add_sample(ChargeSample(3.0, 0.4, True, at))
    _run(coord.async_persist())

    restarted = _coordinator(hass, **DERATING)
    restarted._store = coord._store
    _run(restarted.async_setup())
    assert len(restarted.charge_model.samples) == 6
    assert restarted.charge_factor()[1] == "learned"


def test_moving_the_sliders_keeps_learned_samples():
    hass = _hass_with_prices(temp=12.0)
    coord = _coordinator(hass, **DERATING)
    at = datetime(2026, 1, 15, tzinfo=UTC)
    for _ in range(6):
        coord.charge_model.add_sample(ChargeSample(3.0, 0.4, True, at))
    _run(coord.async_persist())

    changed = _coordinator(hass, **DERATING, **{CONF_DERATING_0C: 0})
    changed._store = coord._store
    _run(changed.async_setup())
    assert len(changed.charge_model.samples) == 6
    assert changed.charge_model.curve[0] == (0.0, 0.0)


def test_a_store_from_v07_loads_an_empty_model(caplog):
    hass = _hass_with_prices(temp=2.5)
    coord = _coordinator(hass, **DERATING)
    coord._store._data = {"model": None, "last_applied": "auto"}
    caplog.set_level(logging.WARNING)
    _run(coord.async_setup())
    assert coord.charge_model.samples == ()
    assert caplog.records == []


# --- configuration sensor and diagnostics --------------------------------------

from smart_battery_pilot.sensor import ConfigSensor  # noqa: E402


def _config_attributes(coord):
    sensor = object.__new__(ConfigSensor)
    sensor.coordinator = coord
    return sensor.extra_state_attributes


def test_the_config_sensor_reports_the_charge_factor():
    hass = _hass_with_prices(temp=2.5)
    coord = _coordinator(hass, **DERATING)
    coord.data = _run(coord._async_update_data())
    attrs = _config_attributes(coord)
    assert attrs["charge_derating"] is True
    assert attrs["charge_factor"] == 0.15
    assert attrs["charge_factor_source"] == "curve"
    assert attrs["charge_rate_learning"] == "active"
    assert attrs["learned_bands"] == 0


def test_the_config_sensor_says_when_the_curve_stays_for_good():
    hass = _hass_with_prices(temp=2.5)
    coord = _coordinator(hass, **DERATING, battery_charge_energy_entity=None)
    assert _config_attributes(coord)["charge_rate_learning"] == "no_charge_meter"
    coord_off = _coordinator(hass)
    assert _config_attributes(coord_off)["charge_rate_learning"] == "off"
