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


# --- observations -----------------------------------------------------------------

from datetime import timedelta  # noqa: E402

from smart_battery_pilot.const import (  # noqa: E402
    CONF_BATTERY_CHARGE_ENERGY_ENTITY,
    CONF_MAX_CHARGE_POWER_W,
)

T0 = datetime(2026, 1, 15, 2, 0, tzinfo=UTC)
LEARNING = {**DERATING, CONF_MAX_CHARGE_POWER_W: 5000, "efficiency": 100}


def _observed(hass, coord, *, kwh_end, minutes=60, soc_end=40.0, requested=5000.0, unit="kWh"):
    hass.states.set(
        "sensor.charge_energy", 100.0 if unit == "kWh" else 100000.0, {"unit_of_measurement": unit}
    )
    coord.charge_observation(requested, now=T0)
    hass.states.set(
        "sensor.charge_energy",
        kwh_end if unit == "kWh" else kwh_end * 1000,
        {"unit_of_measurement": unit},
    )
    hass.states.set("sensor.soc", soc_end)
    coord.charge_observation(None, now=T0 + timedelta(minutes=minutes))
    return coord.charge_model.samples


def test_a_throttled_charge_slot_becomes_a_saturated_sample():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)
    [sample] = _observed(hass, coord, kwh_end=101.5)  # 1.5 kW of 5 kW asked
    assert sample.temperature == 3.0
    assert round(sample.ratio, 3) == 0.3
    assert sample.saturated is True


def test_a_full_power_slot_is_a_lower_bound():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)
    [sample] = _observed(hass, coord, kwh_end=104.8)
    assert sample.saturated is False


def test_a_wh_meter_gives_the_same_ratio():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)
    [sample] = _observed(hass, coord, kwh_end=101.5, unit="Wh")
    assert round(sample.ratio, 3) == 0.3


def test_the_ratio_is_measured_on_the_battery_side():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **{**LEARNING, "efficiency": 81})  # one way 0.9
    [sample] = _observed(hass, coord, kwh_end=104.5)
    assert round(sample.ratio, 3) == 1.0


def test_short_slots_are_discarded():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)
    assert _observed(hass, coord, kwh_end=100.1, minutes=5) == ()


def test_a_nearly_full_battery_is_discarded():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)  # max_soc default 95
    assert _observed(hass, coord, kwh_end=101.0, soc_end=91.0) == ()


def test_a_meter_reset_is_discarded():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)
    assert _observed(hass, coord, kwh_end=0.5) == ()


def test_a_small_request_is_not_observed():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)
    assert _observed(hass, coord, kwh_end=100.5, requested=500.0) == ()


def test_no_learning_without_a_charge_meter():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING, **{CONF_BATTERY_CHARGE_ENERGY_ENTITY: None})
    assert _observed(hass, coord, kwh_end=101.5) == ()


def test_no_learning_while_derating_is_off():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **{**LEARNING, CONF_CHARGE_DERATING: False})
    assert _observed(hass, coord, kwh_end=101.5) == ()


def _meter(hass, kwh):
    hass.states.set("sensor.charge_energy", kwh, {"unit_of_measurement": "kWh"})


def _durations(coord):
    return len(coord.charge_model.samples)


def test_a_refresh_inside_a_15_minute_slot_keeps_one_observation():
    """Splitting at the refresh left an 8 and a 7 minute half - both under the
    10-minute minimum, so the slot was lost entirely."""
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)
    _meter(hass, 100.0)
    coord.charge_observation(5000.0, now=T0)
    _meter(hass, 100.2)
    coord.charge_observation(5000.0, now=T0 + timedelta(minutes=8))  # refresh re-applies
    _meter(hass, 100.375)
    coord.charge_observation(None, now=T0 + timedelta(minutes=15))
    [sample] = coord.charge_model.samples
    assert round(sample.ratio, 3) == 0.3


def test_one_slot_is_one_sample_however_often_it_is_re_applied():
    """Two samples from one slot would let a band count as learned after three
    real slots instead of six."""
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)
    _meter(hass, 100.0)
    coord.charge_observation(5000.0, now=T0)
    for minutes in (10, 20, 30, 40, 50):
        coord.charge_observation(5000.0, now=T0 + timedelta(minutes=minutes))
    _meter(hass, 101.5)
    coord.charge_observation(None, now=T0 + timedelta(minutes=60))
    assert [round(s.ratio, 3) for s in coord.charge_model.samples] == [0.3]


def test_a_changed_request_closes_the_observation():
    """A mixed window would compare what the battery took against an average
    of two different requests."""
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)
    _meter(hass, 100.0)
    coord.charge_observation(5000.0, now=T0)
    _meter(hass, 100.375)
    coord.charge_observation(1200.0, now=T0 + timedelta(minutes=15))
    _meter(hass, 100.675)
    coord.charge_observation(None, now=T0 + timedelta(minutes=30))
    samples = coord.charge_model.samples
    assert [s.saturated for s in samples] == [True, False]


def test_a_long_charge_is_cut_into_hour_long_samples():
    """The battery warms while it charges; one sample per hour keeps the
    temperature it is filed under close to the truth."""
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)
    _meter(hass, 100.0)
    coord.charge_observation(5000.0, now=T0)
    _meter(hass, 101.5)
    coord.charge_observation(5000.0, now=T0 + timedelta(minutes=60))
    _meter(hass, 101.875)
    coord.charge_observation(None, now=T0 + timedelta(minutes=75))
    assert _durations(coord) == 2


def test_a_new_sample_is_persisted():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)
    _observed(hass, coord, kwh_end=101.5)
    assert coord._store._data["charge_rate"]["samples"]


def test_saturation_compares_battery_side_with_battery_side():
    """At 72 % roundtrip efficiency a battery taking everything it was asked
    for stores 0.85 of the request on its side of the inverter. Comparing that
    with the grid-side request called every such slot limited."""
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **{**LEARNING, "efficiency": 72})
    one_way = 0.72**0.5
    [sample] = _observed(hass, coord, kwh_end=100.0 + 5.0 * one_way)
    assert sample.saturated is False


def test_the_close_reading_can_be_taken_before_the_next_script():
    """The auto script after a charge slot may take two minutes, with no charge
    flowing; timing the sample after it returned diluted the rate."""
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)
    _meter(hass, 100.0)
    coord.charge_observation(5000.0, now=T0)
    _meter(hass, 101.5)
    reading = coord.charge_reading(now=T0 + timedelta(minutes=60))
    coord.charge_observation(None, now=T0 + timedelta(minutes=62), closing=reading)
    [sample] = coord.charge_model.samples
    assert round(sample.ratio, 3) == 0.3


def test_a_short_sample_below_meter_resolution_is_discarded():
    """Many inverter meters count in 0.1 kWh steps; 0.1 kWh in 15 minutes can
    be anything from 0.0 to 0.2 kWh - a ratio off by a factor of two."""
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)
    assert _observed(hass, coord, kwh_end=100.1, minutes=15) == ()


def test_a_battery_taking_nothing_for_half_an_hour_is_a_real_sample():
    hass = _hass_with_prices(temp=-2.0)
    coord = _coordinator(hass, **LEARNING)
    [sample] = _observed(hass, coord, kwh_end=100.0, minutes=30)
    assert sample.ratio == 0.0
    assert sample.saturated is True


def test_a_mangled_charge_rate_store_entry_does_not_break_setup(caplog):
    hass = _hass_with_prices(temp=2.5)
    coord = _coordinator(hass, **DERATING)
    coord._store._data = {"charge_rate": ["not", "a", "dict"]}
    caplog.set_level(logging.WARNING)
    _run(coord.async_setup())
    assert coord.charge_model.samples == ()
    assert "charge rate" in caplog.text
