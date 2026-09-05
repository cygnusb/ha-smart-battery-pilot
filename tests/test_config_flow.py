"""Config and options flow: the whole user-facing setup path.

The options flow writes *every* field of a section - including keys the config
flow put in `entry.data` - into `entry.options`, which is why the integration
carries a unique-id repair on startup. That coupling is the part worth
pinning down.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from homeassistant.config_entries import AbortFlow, ConfigEntry
import pytest

from smart_battery_pilot import config_flow as cf
from smart_battery_pilot.const import (
    CONF_CAPACITY_KWH,
    CONF_CONSUMPTION_ENTITY,
    CONF_DISCHARGE_MODE,
    CONF_DRY_RUN,
    CONF_EFFICIENCY,
    CONF_FEED_IN_TARIFF,
    CONF_HAS_HEAT_PUMP,
    CONF_MAX_CHARGE_POWER_W,
    CONF_MAX_DISCHARGE_POWER_W,
    CONF_MAX_SOC,
    CONF_MIN_SOC,
    CONF_PRICE_ENTITY,
    CONF_PRICE_OFFSET,
    CONF_PV_FORECAST_TODAY,
    CONF_SCRIPT_AUTO,
    CONF_SCRIPT_CHARGE,
    CONF_SCRIPT_EXPORT,
    CONF_SCRIPT_IDLE,
    CONF_SOC_ENTITY,
    CONF_SPREAD_THRESHOLD,
    CONF_TEMPERATURE_ENTITY,
    CONF_TRAINING_DAYS,
    DEFAULT_TRAINING_DAYS,
    DISCHARGE_MODE_EXPORT,
    DISCHARGE_MODE_SELF_CONSUMPTION,
)

TZ = timezone(timedelta(hours=1))


class _States:
    def __init__(self) -> None:
        self._data: dict = {}

    def get(self, entity_id):
        return self._data.get(entity_id)

    def set(self, entity_id, attributes):
        self._data[entity_id] = SimpleNamespace(state="0.30", attributes=attributes)


class _ConfigEntries:
    def __init__(self, entries=None) -> None:
        self._entries = list(entries or [])

    def async_entries(self, domain):
        return list(self._entries)


class _FakeHass:
    def __init__(self, entries=None) -> None:
        self.states = _States()
        self.config_entries = _ConfigEntries(entries)


def _hourly_price_attrs() -> dict:
    """A price curve that is always in the future, whenever the test runs."""
    now = datetime.now(TZ)
    return {
        "prices": [
            {
                "start_time": (now + timedelta(hours=i)).isoformat(),
                "end_time": (now + timedelta(hours=i + 1)).isoformat(),
                "marketprice": 100.0 + i,
            }
            for i in range(24)
        ]
    }


def _flow(hass) -> cf.SBPConfigFlow:
    flow = cf.SBPConfigFlow()
    flow.hass = hass
    return flow


def _run(coro):
    return asyncio.run(coro)


BATTERY_INPUT = {
    CONF_SOC_ENTITY: "sensor.soc",
    CONF_CAPACITY_KWH: 12.8,
    CONF_MAX_CHARGE_POWER_W: 5000,
    CONF_MAX_DISCHARGE_POWER_W: 5000,
    CONF_MIN_SOC: 10,
    CONF_MAX_SOC: 95,
    CONF_EFFICIENCY: 90,
}
CONTROL_INPUT = {
    CONF_SCRIPT_CHARGE: "script.charge",
    CONF_SCRIPT_IDLE: "script.idle",
    CONF_SCRIPT_AUTO: "script.auto",
}


# --- price validation --------------------------------------------------------


def test_a_missing_price_entity_is_rejected():
    hass = _FakeHass()
    result = _run(_flow(hass).async_step_user({CONF_PRICE_ENTITY: "sensor.nope"}))
    assert result["type"] == "form"
    assert result["errors"] == {CONF_PRICE_ENTITY: "entity_not_found"}


def test_an_unparseable_price_entity_is_rejected():
    hass = _FakeHass()
    hass.states.set("sensor.price", {"totally": "unknown"})
    result = _run(_flow(hass).async_step_user({CONF_PRICE_ENTITY: "sensor.price"}))
    assert result["errors"] == {CONF_PRICE_ENTITY: "unsupported_price_format"}


def test_a_price_entity_with_only_past_slots_is_rejected():
    hass = _FakeHass()
    past = datetime.now(TZ) - timedelta(days=2)
    hass.states.set(
        "sensor.price",
        {
            "prices": [
                {
                    "start_time": (past + timedelta(hours=i)).isoformat(),
                    "end_time": (past + timedelta(hours=i + 1)).isoformat(),
                    "marketprice": 100.0,
                }
                for i in range(24)
            ]
        },
    )
    result = _run(_flow(hass).async_step_user({CONF_PRICE_ENTITY: "sensor.price"}))
    assert result["errors"] == {CONF_PRICE_ENTITY: "no_future_prices"}


# --- the happy path ----------------------------------------------------------


def test_the_full_setup_walks_every_step_and_starts_safe():
    hass = _FakeHass()
    hass.states.set("sensor.price", _hourly_price_attrs())
    flow = _flow(hass)

    assert (
        _run(
            flow.async_step_user(
                {
                    CONF_PRICE_ENTITY: "sensor.price",
                    CONF_PRICE_OFFSET: 0.15,
                    CONF_FEED_IN_TARIFF: 0.08,
                }
            )
        )["step_id"]
        == "battery"
    )
    assert _run(flow.async_step_battery(dict(BATTERY_INPUT)))["step_id"] == "control"
    assert _run(flow.async_step_control(dict(CONTROL_INPUT)))["step_id"] == "consumption"
    assert (
        _run(
            flow.async_step_consumption(
                {CONF_CONSUMPTION_ENTITY: "sensor.load", CONF_HAS_HEAT_PUMP: False}
            )
        )["step_id"]
        == "pv"
    )

    entry = _run(flow.async_step_pv({CONF_PV_FORECAST_TODAY: "sensor.pv"}))
    assert entry["type"] == "create_entry"
    assert entry["data"][CONF_PRICE_ENTITY] == "sensor.price"
    assert entry["data"][CONF_SOC_ENTITY] == "sensor.soc"
    assert entry["data"][CONF_PV_FORECAST_TODAY] == "sensor.pv"
    # Shipped safe: disabled is the switch default, dry-run is an option.
    assert entry["options"][CONF_DRY_RUN] is True
    assert entry["options"][CONF_DISCHARGE_MODE] == DISCHARGE_MODE_SELF_CONSUMPTION


def test_the_unique_id_tracks_the_steered_battery():
    hass = _FakeHass()
    hass.states.set("sensor.price", _hourly_price_attrs())
    flow = _flow(hass)
    _run(flow.async_step_user({CONF_PRICE_ENTITY: "sensor.price"}))
    _run(flow.async_step_battery(dict(BATTERY_INPUT)))
    assert flow.unique_id == "sensor.soc"


def test_a_second_entry_for_the_same_battery_aborts():
    """Two entries steering one inverter would fight slot by slot."""
    existing = ConfigEntry(data={}, entry_id="first", unique_id="sensor.soc")
    hass = _FakeHass(entries=[existing])
    hass.states.set("sensor.price", _hourly_price_attrs())
    flow = _flow(hass)
    _run(flow.async_step_user({CONF_PRICE_ENTITY: "sensor.price"}))

    with pytest.raises(AbortFlow) as err:
        _run(flow.async_step_battery(dict(BATTERY_INPUT)))
    assert err.value.reason == "already_configured"


def test_a_different_battery_does_not_abort():
    existing = ConfigEntry(data={}, entry_id="first", unique_id="sensor.other_soc")
    hass = _FakeHass(entries=[existing])
    hass.states.set("sensor.price", _hourly_price_attrs())
    flow = _flow(hass)
    _run(flow.async_step_user({CONF_PRICE_ENTITY: "sensor.price"}))
    assert _run(flow.async_step_battery(dict(BATTERY_INPUT)))["step_id"] == "control"


@pytest.mark.parametrize(("min_soc", "max_soc"), [(95, 10), (50, 50)])
def test_an_inverted_soc_window_is_rejected(min_soc, max_soc):
    hass = _FakeHass()
    hass.states.set("sensor.price", _hourly_price_attrs())
    flow = _flow(hass)
    _run(flow.async_step_user({CONF_PRICE_ENTITY: "sensor.price"}))

    result = _run(
        flow.async_step_battery({**BATTERY_INPUT, CONF_MIN_SOC: min_soc, CONF_MAX_SOC: max_soc})
    )
    assert result["errors"] == {"base": "soc_range_invalid"}


# --- options flow ------------------------------------------------------------


def _options_flow(hass, entry) -> cf.SBPOptionsFlow:
    flow = cf.SBPOptionsFlow()
    flow.hass = hass
    flow.config_entry = entry
    return flow


def _entry(**options) -> ConfigEntry:
    return ConfigEntry(
        data={
            CONF_PRICE_ENTITY: "sensor.price",
            CONF_SOC_ENTITY: "sensor.soc",
            CONF_FEED_IN_TARIFF: 0.08,
            **BATTERY_INPUT,
            **CONTROL_INPUT,
            CONF_SCRIPT_EXPORT: "script.export",
            CONF_CONSUMPTION_ENTITY: "sensor.load",
        },
        options={
            CONF_SPREAD_THRESHOLD: 0.20,
            CONF_DISCHARGE_MODE: DISCHARGE_MODE_SELF_CONSUMPTION,
            CONF_DRY_RUN: True,
            CONF_TRAINING_DAYS: DEFAULT_TRAINING_DAYS,
            **options,
        },
    )


def test_sections_only_persist_once_apply_is_chosen():
    """A reload per edited section would bounce the inverter repeatedly."""
    hass = _FakeHass()
    flow = _options_flow(hass, _entry())

    result = _run(
        flow.async_step_tuning(
            {
                CONF_SPREAD_THRESHOLD: 0.05,
                CONF_DISCHARGE_MODE: DISCHARGE_MODE_SELF_CONSUMPTION,
                CONF_TRAINING_DAYS: 30,
            }
        )
    )
    assert result["type"] == "menu"
    assert flow.config_entry.options[CONF_SPREAD_THRESHOLD] == 0.20

    applied = _run(flow.async_step_apply())
    assert applied["type"] == "create_entry"
    assert applied["data"][CONF_SPREAD_THRESHOLD] == 0.05
    assert applied["data"][CONF_TRAINING_DAYS] == 30


def test_apply_keeps_options_the_menu_never_offers():
    """dry_run has no form field; it must survive an options edit."""
    hass = _FakeHass()
    flow = _options_flow(hass, _entry())
    _run(
        flow.async_step_tuning(
            {
                CONF_SPREAD_THRESHOLD: 0.05,
                CONF_DISCHARGE_MODE: DISCHARGE_MODE_SELF_CONSUMPTION,
                CONF_TRAINING_DAYS: 30,
            }
        )
    )
    assert _run(flow.async_step_apply())["data"][CONF_DRY_RUN] is True


def test_clearing_an_optional_entity_really_clears_it():
    """An absent optional field means "removed", not "unchanged"."""
    hass = _FakeHass()
    entry = _entry()
    entry.data[CONF_TEMPERATURE_ENTITY] = "sensor.outside"
    flow = _options_flow(hass, entry)

    _run(
        flow.async_step_consumption(
            {CONF_CONSUMPTION_ENTITY: "sensor.load", CONF_HAS_HEAT_PUMP: False}
        )
    )
    assert _run(flow.async_step_apply())["data"][CONF_TEMPERATURE_ENTITY] is None


def test_a_changed_soc_entity_lands_in_options_not_data():
    """This is what makes the unique-id repair on startup necessary."""
    hass = _FakeHass()
    entry = _entry()
    flow = _options_flow(hass, entry)

    _run(flow.async_step_battery({**BATTERY_INPUT, CONF_SOC_ENTITY: "sensor.new_soc"}))
    applied = _run(flow.async_step_apply())

    assert applied["data"][CONF_SOC_ENTITY] == "sensor.new_soc"
    assert entry.data[CONF_SOC_ENTITY] == "sensor.soc"


def test_options_battery_step_validates_the_soc_window():
    hass = _FakeHass()
    flow = _options_flow(hass, _entry())
    result = _run(flow.async_step_battery({**BATTERY_INPUT, CONF_MIN_SOC: 90, CONF_MAX_SOC: 20}))
    assert result["errors"] == {"base": "soc_range_invalid"}


def test_options_price_step_revalidates_the_entity():
    hass = _FakeHass()
    flow = _options_flow(hass, _entry())
    result = _run(flow.async_step_prices({CONF_PRICE_ENTITY: "sensor.gone"}))
    assert result["errors"] == {CONF_PRICE_ENTITY: "entity_not_found"}


# --- export mode that could never fire ---------------------------------------


def test_export_mode_below_the_spread_is_refused():
    """Feed-in at or under the spread means no slot can ever qualify."""
    hass = _FakeHass()
    flow = _options_flow(hass, _entry())
    result = _run(
        flow.async_step_tuning(
            {
                CONF_SPREAD_THRESHOLD: 0.20,
                CONF_DISCHARGE_MODE: DISCHARGE_MODE_EXPORT,
                CONF_TRAINING_DAYS: 60,
            }
        )
    )
    assert result["errors"] == {"base": "export_spread_unreachable"}


def test_export_mode_above_the_spread_is_accepted():
    hass = _FakeHass()
    flow = _options_flow(hass, _entry())
    result = _run(
        flow.async_step_tuning(
            {
                CONF_SPREAD_THRESHOLD: 0.05,
                CONF_DISCHARGE_MODE: DISCHARGE_MODE_EXPORT,
                CONF_TRAINING_DAYS: 60,
            }
        )
    )
    assert result["type"] == "menu"


def test_market_price_export_is_never_refused():
    """Feed-in 0 means "sell at market price", which varies - cannot be judged."""
    hass = _FakeHass()
    flow = _options_flow(hass, _entry())
    flow._pending[CONF_FEED_IN_TARIFF] = 0.0
    result = _run(
        flow.async_step_tuning(
            {
                CONF_SPREAD_THRESHOLD: 0.30,
                CONF_DISCHARGE_MODE: DISCHARGE_MODE_EXPORT,
                CONF_TRAINING_DAYS: 60,
            }
        )
    )
    assert result["type"] == "menu"


def test_lowering_the_feed_in_under_an_active_export_mode_is_refused():
    """The other half of the pair: the tariff lives in the prices step."""
    hass = _FakeHass()
    hass.states.set("sensor.price", _hourly_price_attrs())
    flow = _options_flow(hass, _entry(**{CONF_DISCHARGE_MODE: DISCHARGE_MODE_EXPORT}))
    result = _run(
        flow.async_step_prices(
            {CONF_PRICE_ENTITY: "sensor.price", CONF_PRICE_OFFSET: 0.0, CONF_FEED_IN_TARIFF: 0.10}
        )
    )
    assert result["errors"] == {"base": "export_spread_unreachable"}


# --- export mode without the script that carries it --------------------------


def _export_entry(**options) -> ConfigEntry:
    """An entry in export mode whose export script was never configured."""
    entry = _entry(**{CONF_DISCHARGE_MODE: DISCHARGE_MODE_EXPORT, **options})
    del entry.data[CONF_SCRIPT_EXPORT]
    return entry


def test_export_mode_without_an_export_script_is_refused():
    """Export mode is the one action whose script is optional everywhere else.

    Without it the planner still schedules export slots, the executor logs
    "no export script configured" at every slot boundary and falls back to
    auto mode - so the setting looks active while doing nothing at all.
    """
    hass = _FakeHass()
    flow = _options_flow(hass, _export_entry())
    result = _run(
        flow.async_step_tuning(
            {
                CONF_SPREAD_THRESHOLD: 0.05,
                CONF_DISCHARGE_MODE: DISCHARGE_MODE_EXPORT,
                CONF_TRAINING_DAYS: 60,
            }
        )
    )
    assert result["errors"] == {"base": "export_script_missing"}


def test_an_export_script_staged_in_the_same_session_satisfies_the_check():
    """Sections are collected before they are applied; the check sees them."""
    hass = _FakeHass()
    flow = _options_flow(hass, _export_entry())
    _run(flow.async_step_control({**CONTROL_INPUT, CONF_SCRIPT_EXPORT: "script.export"}))
    result = _run(
        flow.async_step_tuning(
            {
                CONF_SPREAD_THRESHOLD: 0.05,
                CONF_DISCHARGE_MODE: DISCHARGE_MODE_EXPORT,
                CONF_TRAINING_DAYS: 60,
            }
        )
    )
    assert result["type"] == "menu"


def test_clearing_the_export_script_under_an_active_export_mode_is_refused():
    """The other half of the pair: the script lives in the control step."""
    hass = _FakeHass()
    flow = _options_flow(hass, _entry(**{CONF_DISCHARGE_MODE: DISCHARGE_MODE_EXPORT}))
    result = _run(flow.async_step_control(dict(CONTROL_INPUT)))
    assert result["errors"] == {"base": "export_script_missing"}


def test_the_export_script_stays_optional_in_self_consumption_mode():
    hass = _FakeHass()
    flow = _options_flow(hass, _entry())
    result = _run(flow.async_step_control(dict(CONTROL_INPUT)))
    assert result["type"] == "menu"
    assert _run(flow.async_step_apply())["data"][CONF_SCRIPT_EXPORT] is None
