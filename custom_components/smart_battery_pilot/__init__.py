"""Smart Battery Pilot: charge your home battery when electricity is cheap."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, FRONTEND_SCRIPT_URL, SERVICE_REPLAN
from .coordinator import SBPCoordinator
from .executor import PlanExecutor

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "switch", "binary_sensor"]

type SBPConfigEntry = ConfigEntry["SBPRuntimeData"]


class SBPRuntimeData:
    """Runtime objects stored on the config entry."""

    def __init__(self, coordinator: SBPCoordinator, executor: PlanExecutor) -> None:
        self.coordinator = coordinator
        self.executor = executor


async def _async_handle_replan(hass: HomeAssistant, _call: ServiceCall) -> None:
    """Refresh every loaded entry's coordinator (survives options reload)."""
    for loaded in hass.config_entries.async_entries(DOMAIN):
        runtime = getattr(loaded, "runtime_data", None)
        if runtime is None:
            continue
        await runtime.coordinator.async_request_refresh()


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the bundled Lovelace card resource and the replan service."""
    card_path = Path(__file__).parent / "frontend" / "smart-battery-pilot-card.js"
    if await hass.async_add_executor_job(card_path.exists):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(FRONTEND_SCRIPT_URL, str(card_path), cache_headers=False)]
        )
        add_extra_js_url(hass, FRONTEND_SCRIPT_URL)
    if not hass.services.has_service(DOMAIN, SERVICE_REPLAN):
        async def _handle(call: ServiceCall) -> None:
            await _async_handle_replan(hass, call)

        hass.services.async_register(DOMAIN, SERVICE_REPLAN, _handle)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SBPConfigEntry) -> bool:
    coordinator = SBPCoordinator(hass, entry)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()

    executor = PlanExecutor(hass, coordinator)
    entry.runtime_data = SBPRuntimeData(coordinator, executor)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await executor.async_start()

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: SBPConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: SBPConfigEntry) -> bool:
    runtime: SBPRuntimeData = entry.runtime_data
    await runtime.executor.async_stop(restore_auto=True)
    await runtime.coordinator.async_shutdown()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    remaining = [
        loaded
        for loaded in hass.config_entries.async_entries(DOMAIN)
        if loaded.entry_id != entry.entry_id and getattr(loaded, "runtime_data", None) is not None
    ]
    if not remaining and hass.services.has_service(DOMAIN, SERVICE_REPLAN):
        hass.services.async_remove(DOMAIN, SERVICE_REPLAN)
    return unloaded
