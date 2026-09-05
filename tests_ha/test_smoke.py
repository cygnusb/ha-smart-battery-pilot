"""End-to-end smoke test against a real Home Assistant.

Narrow on purpose: it does not re-test the planner (the stub suite does that
thoroughly and fast). It answers the one question the stubs cannot - does this
integration still set up, create its entities and tear down again on the
Home Assistant version we claim to support?
"""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

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


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
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

    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="sensor.battery_soc")
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
    # Ten sensors, two switches, one binary sensor.
    assert len(created) == 13, created


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


async def test_the_entry_unloads_cleanly(sbp_hass: HomeAssistant) -> None:
    entry = await _setup(sbp_hass)

    assert await sbp_hass.config_entries.async_unload(entry.entry_id)
    await sbp_hass.async_block_till_done()

    assert entry.state is entry.state.NOT_LOADED
