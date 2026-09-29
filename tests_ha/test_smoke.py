"""End-to-end smoke test against a real Home Assistant.

Narrow on purpose: it does not re-test the planner (the stub suite does that
thoroughly and fast). It answers the one question the stubs cannot - does this
integration still set up, create its entities and tear down again on the
Home Assistant version we claim to support?
"""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import event as event_helper
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.smart_battery_pilot.const import (
    CONF_CAPACITY_KWH,
    CONF_CONSUMPTION_ENTITY,
    CONF_EFFICIENCY,
    CONF_MAX_CHARGE_POWER_W,
    CONF_MAX_DISCHARGE_POWER_W,
    CONF_MAX_SOC,
    CONF_MIN_SOC,
    CONF_PRICE_ENTITY,
    CONF_SCRIPT_AUTO,
    CONF_SCRIPT_CHARGE,
    CONF_SCRIPT_IDLE,
    CONF_SOC_ENTITY,
    DOMAIN,
    SERVICE_REPLAN,
)

ENTRY_DATA = {
    CONF_PRICE_ENTITY: "sensor.electricity_price",
    CONF_SOC_ENTITY: "sensor.battery_soc",
    CONF_CAPACITY_KWH: 12.8,
    CONF_MAX_CHARGE_POWER_W: 5000,
    CONF_MAX_DISCHARGE_POWER_W: 5000,
    CONF_MIN_SOC: 10,
    CONF_MAX_SOC: 95,
    CONF_EFFICIENCY: 90,
    CONF_SCRIPT_CHARGE: "script.sbp_charge",
    CONF_SCRIPT_IDLE: "script.sbp_idle",
    CONF_SCRIPT_AUTO: "script.sbp_auto",
    CONF_CONSUMPTION_ENTITY: "sensor.house_load",
}


async def _setup(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    """Publish a 24 h EPEX-style curve and set the integration up on it."""
    start = dt_util.now().replace(minute=0, second=0, microsecond=0)
    hass.states.async_set(
        "sensor.electricity_price",
        "0.30",
        {
            "data": [
                {
                    "start_time": (start + timedelta(hours=i)).isoformat(),
                    "end_time": (start + timedelta(hours=i + 1)).isoformat(),
                    "price_eur_per_mwh": 300.0 - (100.0 if i in (2, 3, 4) else 0.0),
                }
                for i in range(24)
            ]
        },
    )
    hass.states.async_set("sensor.battery_soc", "55", {"unit_of_measurement": "%"})

    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=options or {}, unique_id="sensor.battery_soc"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_the_integration_sets_up_and_creates_its_entities(
    sbp_hass: HomeAssistant,
) -> None:
    entry = await _setup(sbp_hass)

    assert entry.state is entry.state.LOADED
    assert sbp_hass.services.has_service(DOMAIN, SERVICE_REPLAN)

    created = [eid for eid in sbp_hass.states.async_entity_ids() if "smart_battery_pilot" in eid]
    # Twelve sensors, two switches, one binary sensor.
    assert len(created) == 15, created


async def test_a_plan_is_computed_from_the_price_entity(
    sbp_hass: HomeAssistant,
) -> None:
    entry = await _setup(sbp_hass)
    coordinator = entry.runtime_data.coordinator

    assert coordinator.data.valid is True
    assert coordinator.data.adapter_name == "epex_spot"
    assert coordinator.data.plan.slots
    assert all(isinstance(slot.start, datetime) for slot in coordinator.data.plan.slots)


async def test_the_integration_ships_disabled_and_in_dry_run(
    sbp_hass: HomeAssistant,
) -> None:
    """Safety default: no script may be called before the user opts in."""
    entry = await _setup(sbp_hass)

    assert entry.runtime_data.coordinator.enabled is False
    assert entry.runtime_data.coordinator.dry_run is True
    assert entry.runtime_data.coordinator.last_applied is None


async def test_the_replan_service_refreshes_the_plan(sbp_hass: HomeAssistant) -> None:
    entry = await _setup(sbp_hass)
    before = entry.runtime_data.coordinator.data.updated_at

    await sbp_hass.services.async_call(DOMAIN, SERVICE_REPLAN, blocking=True)
    await sbp_hass.async_block_till_done()

    assert entry.runtime_data.coordinator.data.updated_at >= before


async def test_setup_with_cold_weather_charging_on(sbp_hass: HomeAssistant) -> None:
    """The new options section must not break setup on a real Home Assistant."""
    sbp_hass.states.async_set("sensor.battery_temp", "3.0", {"unit_of_measurement": "°C"})
    entry = await _setup(
        sbp_hass,
        options={
            "charge_derating": True,
            "battery_temperature_entity": "sensor.battery_temp",
        },
    )

    assert entry.state is entry.state.LOADED
    derating = entry.runtime_data.coordinator.data.inputs["charge_derating"]
    assert derating["source"] == "curve"
    assert derating["factor"] < 1.0


async def test_the_derating_options_step_renders_and_saves(sbp_hass: HomeAssistant) -> None:
    """The stub suite cannot tell whether real selectors accept the slider and
    entity configs, nor whether the frontend can serialise the form."""
    entry = await _setup(sbp_hass)
    options = sbp_hass.config_entries.options

    menu = await options.async_init(entry.entry_id)
    assert menu["type"] == "menu"
    assert "derating" in menu["menu_options"]

    form = await options.async_configure(menu["flow_id"], {"next_step_id": "derating"})
    assert form["type"] == "form"
    assert form["step_id"] == "derating"
    # What the frontend does per field; a selector config HA rejects raises here.
    for selector in form["data_schema"].schema.values():
        assert "selector" in cv.custom_serializer(selector)

    refused = await options.async_configure(
        form["flow_id"],
        {
            "charge_derating": True,
            "derating_0c": 10,
            "derating_5c": 20,
            "derating_10c": 50,
            "derating_15c": 80,
            "derating_20c": 100,
        },
    )
    assert refused["errors"] == {"base": "derating_needs_temperature"}

    saved = await options.async_configure(
        refused["flow_id"],
        {
            "charge_derating": True,
            "battery_temperature_entity": "sensor.battery_soc",
            "derating_0c": 10,
            "derating_5c": 30,
            "derating_10c": 50,
            "derating_15c": 80,
            "derating_20c": 100,
        },
    )
    assert saved["type"] == "menu"
    done = await options.async_configure(saved["flow_id"], {"next_step_id": "apply"})
    await sbp_hass.async_block_till_done()
    assert done["type"] == "create_entry"
    assert entry.options["derating_5c"] == 30
    assert entry.options["charge_derating"] is True


async def test_the_reserve_options_step_renders_and_saves(sbp_hass: HomeAssistant) -> None:
    entry = await _setup(sbp_hass)
    options = sbp_hass.config_entries.options

    menu = await options.async_init(entry.entry_id)
    assert "reserve" in menu["menu_options"]
    form = await options.async_configure(menu["flow_id"], {"next_step_id": "reserve"})
    assert form["step_id"] == "reserve"
    for selector in form["data_schema"].schema.values():
        assert "selector" in cv.custom_serializer(selector)

    saved = await options.async_configure(
        form["flow_id"],
        {
            "backup_reserve": 30,
            "reserve_refill_hours": 8,
            "reserve_block_discharge": True,
        },
    )
    assert saved["type"] == "menu"
    done = await options.async_configure(saved["flow_id"], {"next_step_id": "apply"})
    await sbp_hass.async_block_till_done()
    assert done["type"] == "create_entry"
    assert entry.options["backup_reserve"] == 30
    assert entry.runtime_data.coordinator.data.inputs["reserve"]["soc"] == 30.0


async def test_a_real_script_receives_reserve_soc(sbp_hass: HomeAssistant) -> None:
    """HA scripts accept variables they do not declare; the reserve must arrive."""
    assert await async_setup_component(
        sbp_hass,
        "script",
        {
            "script": {
                "sbp_auto": {
                    "sequence": [
                        {"event": "sbp_test", "event_data": {"reserve": "{{ reserve_soc }}"}}
                    ]
                }
            }
        },
    )
    events = async_capture_events(sbp_hass, "sbp_test")
    entry = await _setup(sbp_hass, options={"backup_reserve": 30})

    assert await entry.runtime_data.executor._call_script("auto", 0.0)
    await sbp_hass.async_block_till_done()
    assert [str(e.data["reserve"]) for e in events] == ["30"]


def _state_listeners(hass: HomeAssistant, entity_id: str) -> int:
    """How many state-change callbacks HA holds for one entity."""
    data = hass.data.get(event_helper._TRACK_STATE_CHANGE_DATA)
    return len(data.callbacks.get(entity_id, [])) if data else 0


async def test_a_setup_that_is_not_ready_leaves_no_listeners(sbp_hass: HomeAssistant) -> None:
    """Every setup retry built a coordinator that subscribed to the price and
    reserve entities and was never shut down - listeners piled up per retry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA,
        options={"backup_reserve_entity": "input_number.reserve"},
        unique_id="sensor.battery_soc",
    )
    entry.add_to_hass(sbp_hass)
    assert not await sbp_hass.config_entries.async_setup(entry.entry_id)  # no price entity
    await sbp_hass.async_block_till_done()

    assert entry.state is entry.state.SETUP_RETRY
    assert _state_listeners(sbp_hass, "sensor.electricity_price") == 0
    assert _state_listeners(sbp_hass, "input_number.reserve") == 0


async def test_the_entry_unloads_cleanly(sbp_hass: HomeAssistant) -> None:
    entry = await _setup(sbp_hass)

    assert await sbp_hass.config_entries.async_unload(entry.entry_id)
    await sbp_hass.async_block_till_done()

    assert entry.state is entry.state.NOT_LOADED
