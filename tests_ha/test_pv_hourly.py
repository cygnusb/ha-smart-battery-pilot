"""Hourly PV forecasts read through a real Home Assistant's energy platforms.

The stub suite fakes the fetch. Only a real core can say whether the loader
finds an integration's `energy` platform, whether the selector serialises,
and whether a loaded entry's curve reaches the plan.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    mock_integration,
    mock_platform,
)
from test_smoke import _setup

from custom_components.smart_battery_pilot.pv_sources import (
    async_fetch_hourly_pv,
    async_solar_forecast_entries,
)

FAKE = "fake_solar"


def _curve() -> dict[str, float]:
    start = dt_util.now().replace(minute=0, second=0, microsecond=0)
    return {(start + timedelta(hours=h)).isoformat(): 1500.0 for h in range(48)}


def _forecast_integration(hass: HomeAssistant) -> MockConfigEntry:
    async def async_get_solar_forecast(hass, config_entry_id):
        return {"wh_hours": _curve()}

    mock_integration(hass, MockModule(FAKE))
    mock_platform(
        hass,
        f"{FAKE}.energy",
        SimpleNamespace(async_get_solar_forecast=async_get_solar_forecast),
    )
    entry = MockConfigEntry(domain=FAKE, title="South roof")
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry


async def test_a_forecast_integration_is_offered_and_read(sbp_hass: HomeAssistant) -> None:
    roof = _forecast_integration(sbp_hass)

    assert await async_solar_forecast_entries(sbp_hass) == {roof.entry_id: f"South roof ({FAKE})"}
    fetch = await async_fetch_hourly_pv(sbp_hass, [roof.entry_id])
    assert fetch.failed == []
    now = dt_util.now().replace(minute=0, second=0, microsecond=0)
    assert fetch.pv.kwh(now, now + timedelta(minutes=15)) == 0.375


async def test_an_unloaded_entry_is_reported_and_nothing_is_summed(
    sbp_hass: HomeAssistant,
) -> None:
    roof = _forecast_integration(sbp_hass)
    roof.mock_state(sbp_hass, ConfigEntryState.NOT_LOADED)

    fetch = await async_fetch_hourly_pv(sbp_hass, [roof.entry_id, "missing"])
    assert fetch.pv is None
    assert fetch.failed == [roof.entry_id, "missing"]


async def test_the_pv_options_step_picks_entries_and_the_plan_uses_them(
    sbp_hass: HomeAssistant,
) -> None:
    roof = _forecast_integration(sbp_hass)
    entry = await _setup(sbp_hass)
    coordinator = entry.runtime_data.coordinator
    assert coordinator.data.inputs["pv_hourly"]["slots"] == 0

    options = sbp_hass.config_entries.options
    menu = await options.async_init(entry.entry_id)
    form = await options.async_configure(menu["flow_id"], {"next_step_id": "pv"})
    assert form["step_id"] == "pv"
    for selector in form["data_schema"].schema.values():
        assert "selector" in cv.custom_serializer(selector)

    saved = await options.async_configure(form["flow_id"], {"pv_forecast_entries": [roof.entry_id]})
    assert saved["type"] == "menu"
    await options.async_configure(saved["flow_id"], {"next_step_id": "apply"})
    await sbp_hass.async_block_till_done()

    assert entry.options["pv_forecast_entries"] == [roof.entry_id]
    coordinator = entry.runtime_data.coordinator
    assert coordinator.data.inputs["pv_hourly"]["slots"] > 0
    assert coordinator.data.pv_forecast_24h_kwh > 0
