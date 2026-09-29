"""Coordinator side of the backup reserve: effective value, inputs, persistence."""

from __future__ import annotations

import asyncio
import logging

from test_coordinator import _coordinator
from test_coordinator import _FakeHass as _CoordHass

from smart_battery_pilot.const import (
    CONF_BACKUP_RESERVE,
    CONF_BACKUP_RESERVE_ENTITY,
    CONF_RESERVE_BLOCK_DISCHARGE,
    CONF_RESERVE_REFILL_HOURS,
)
from smart_battery_pilot.sensor import ChargePlanSensor, ConfigSensor


def _run(coro):
    return asyncio.run(coro)


def _hass(soc=20.0):
    hass = _CoordHass()
    hass.states.set("sensor.price", 0.30, {"today": [0.10] * 12 + [0.50] * 12})
    hass.states.set("sensor.soc", soc)
    return hass


ENTITY = {CONF_BACKUP_RESERVE_ENTITY: "input_number.reserve"}


def test_a_fixed_reserve():
    coord = _coordinator(_hass(), **{CONF_BACKUP_RESERVE: 30})
    assert coord.reserve_state() == (30.0, "fixed", None)
    assert coord.reserve_for_scripts() == 30


def test_no_reserve_sends_min_soc():
    coord = _coordinator(_hass())
    assert coord.reserve_state() == (None, "off", None)
    assert coord.reserve_for_scripts() == 10


def test_the_entity_overrides_the_fixed_value():
    hass = _hass()
    hass.states.set("input_number.reserve", 50)
    coord = _coordinator(hass, **{CONF_BACKUP_RESERVE: 30, **ENTITY})
    assert coord.reserve_state() == (50.0, "entity", 50.0)
    assert coord.reserve_for_scripts() == 50


def test_an_entity_below_min_soc_switches_the_reserve_off():
    hass = _hass()
    hass.states.set("input_number.reserve", 5)
    coord = _coordinator(hass, **{CONF_BACKUP_RESERVE: 30, **ENTITY})
    assert coord.reserve_state() == (None, "off", 5.0)
    assert coord.reserve_for_scripts() == 10


def test_an_entity_above_max_soc_is_clamped():
    hass = _hass()
    hass.states.set("input_number.reserve", 120)
    coord = _coordinator(hass, **ENTITY)
    assert coord.reserve_state() == (95.0, "entity", 120.0)


def test_an_unreadable_entity_falls_back_and_warns_once(caplog):
    hass = _hass()
    hass.states.set("input_number.reserve", "unavailable")
    coord = _coordinator(hass, **{CONF_BACKUP_RESERVE: 30, **ENTITY})
    caplog.set_level(logging.DEBUG, logger="smart_battery_pilot.coordinator")
    assert coord.reserve_state() == (30.0, "fixed", None)
    assert coord.reserve_state() == (30.0, "fixed", None)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "input_number.reserve" in warnings[0].getMessage()


def test_the_planner_gets_the_reserve_and_the_inputs_record_it():
    coord = _coordinator(
        _hass(),
        **{
            CONF_BACKUP_RESERVE: 30,
            CONF_RESERVE_REFILL_HOURS: 8,
            CONF_RESERVE_BLOCK_DISCHARGE: True,
        },
    )
    result = _run(coord._async_update_data())
    assert result.inputs["battery"]["reserve_soc"] == 30.0
    assert result.inputs["config"]["reserve_refill_hours"] == 8.0
    assert result.inputs["reserve"] == {
        "soc": 30.0,
        "source": "fixed",
        "entity_value": None,
        "refill_hours": 8.0,
        "block": True,
    }
    assert result.plan.reserve_refill_kwh > 0  # SOC 20 % is below it


def test_an_inactive_reserve_reaches_the_planner_as_none():
    result = _run(_coordinator(_hass())._async_update_data())
    assert result.inputs["battery"]["reserve_soc"] is None


def test_the_last_sent_reserve_survives_a_restart():
    hass = _hass()
    coord = _coordinator(hass, **{CONF_BACKUP_RESERVE: 30})
    coord.last_reserve_sent = 30
    _run(coord.async_persist())

    restarted = _coordinator(hass, **{CONF_BACKUP_RESERVE: 30})
    restarted._store = coord._store
    _run(restarted.async_setup())
    assert restarted.last_reserve_sent == 30


def test_live_soc_reads_the_soc_entity():
    coord = _coordinator(_hass(soc=42.0))
    assert coord.live_soc() == 42.0


def _attrs(sensor_cls, coord):
    sensor = object.__new__(sensor_cls)
    sensor.coordinator = coord
    return sensor.extra_state_attributes


def test_the_sensors_show_the_reserve():
    coord = _coordinator(_hass(), **{CONF_BACKUP_RESERVE: 30, CONF_RESERVE_BLOCK_DISCHARGE: True})
    coord.data = _run(coord._async_update_data())
    config = _attrs(ConfigSensor, coord)
    assert config["reserve_soc"] == 30.0
    assert config["reserve_source"] == "fixed"
    assert config["reserve_block_discharge"] is True
    plan = _attrs(ChargePlanSensor, coord)
    assert plan["reserve_refill_kwh"] == coord.data.plan.reserve_refill_kwh
    assert plan["reserve_refill_cost_eur"] == coord.data.plan.reserve_refill_cost_eur


def test_a_nan_reserve_entity_is_unreadable_not_max_soc():
    """float('nan') parses, and clamping nan returned max_soc - the refill would
    have charged the battery full from the grid."""
    hass = _hass()
    hass.states.set("input_number.reserve", "nan")
    coord = _coordinator(hass, **{CONF_BACKUP_RESERVE: 30, **ENTITY})
    assert coord.reserve_state() == (30.0, "fixed", None)
