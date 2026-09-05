"""Smart Battery Pilot: charge your home battery when electricity is cheap."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_SOC_ENTITY,
    DOMAIN,
    FRONTEND_SCRIPT_URL,
    SERVICE_REPLAN,
    STORAGE_KEY,
    STORAGE_VERSION,
)
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
        # Set just before a reload so unloading does not bounce the inverter
        # through auto mode on its way back to the very same action.
        self.reloading = False


async def _async_handle_replan(hass: HomeAssistant, _call: ServiceCall) -> None:
    """Refresh every loaded entry's coordinator (survives options reload)."""
    for loaded in hass.config_entries.async_entries(DOMAIN):
        runtime = getattr(loaded, "runtime_data", None)
        if runtime is None:
            continue
        await runtime.coordinator.async_request_refresh()


def _ensure_replan_service(hass: HomeAssistant) -> None:
    """Register replan; safe to call from setup and from each entry setup."""
    if hass.services.has_service(DOMAIN, SERVICE_REPLAN):
        return

    async def _handle(call: ServiceCall) -> None:
        await _async_handle_replan(hass, call)

    hass.services.async_register(DOMAIN, SERVICE_REPLAN, _handle)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the bundled Lovelace card resource and the replan service."""
    card_path = Path(__file__).parent / "frontend" / "smart-battery-pilot-card.js"
    if await hass.async_add_executor_job(card_path.exists):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(FRONTEND_SCRIPT_URL, str(card_path), cache_headers=False)]
        )
        add_extra_js_url(hass, FRONTEND_SCRIPT_URL)
    _ensure_replan_service(hass)
    return True


def _ensure_unique_id(hass: HomeAssistant, entry: SBPConfigEntry) -> None:
    """Keep the unique id on the battery the entry actually steers.

    The config flow's "one battery, one plan" guard matches on the unique id,
    so it has to track the SOC entity in use: entries from 0.5.x carry None,
    and the options flow writes a changed SOC entity to `options`, leaving the
    id pointing at the old inverter. Either way a second entry for the same
    battery would walk straight through the guard and both executors would
    fight over it.
    """
    soc_entity = entry.options.get(CONF_SOC_ENTITY) or entry.data.get(CONF_SOC_ENTITY)
    if not soc_entity or entry.unique_id == soc_entity:
        return
    if any(
        other.unique_id == soc_entity
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
    ):
        _LOGGER.warning(
            "Another Smart Battery Pilot entry already steers %s; leaving this "
            "entry's unique id unchanged",
            soc_entity,
        )
        return
    hass.config_entries.async_update_entry(entry, unique_id=soc_entity)


async def async_setup_entry(hass: HomeAssistant, entry: SBPConfigEntry) -> bool:
    # Options reload never re-runs async_setup, so the service has to be
    # (re)registered here as well.
    _ensure_replan_service(hass)
    _ensure_unique_id(hass, entry)
    coordinator = SBPCoordinator(hass, entry)
    await coordinator.async_setup()
    executor = PlanExecutor(hass, coordinator)

    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        # Retrying is fine, but not while the battery is still parked in a
        # forced mode from before the restart.
        await executor.async_release_stale_mode()
        raise

    entry.runtime_data = SBPRuntimeData(coordinator, executor)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await executor.async_start()

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: SBPConfigEntry) -> None:
    """Reload the entry when options change."""
    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None:
        runtime.reloading = True
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: SBPConfigEntry) -> bool:
    runtime: SBPRuntimeData = entry.runtime_data
    # A reload puts the same plan back in place moments later; only a real
    # unload hands the battery back to the inverter.
    await runtime.executor.async_stop(restore_auto=not runtime.reloading)
    await runtime.coordinator.async_shutdown()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not runtime.reloading:
        remaining = [
            loaded
            for loaded in hass.config_entries.async_entries(DOMAIN)
            if loaded.entry_id != entry.entry_id
            and getattr(loaded, "runtime_data", None) is not None
        ]
        if not remaining and hass.services.has_service(DOMAIN, SERVICE_REPLAN):
            hass.services.async_remove(DOMAIN, SERVICE_REPLAN)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the entry's stored model and savings totals."""
    await Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}").async_remove()
