"""Hourly PV forecasts from the integrations that feed the Energy dashboard.

A forecast integration (Forecast.Solar, Open-Meteo Solar Forecast, Solcast,
Helios, ...) offers its curve to the Energy dashboard through an `energy`
platform with `async_get_solar_forecast(hass, config_entry_id)`, returning
`{"wh_hours": {iso timestamp: Wh}}`. This module finds those config entries
and reads their curves the same way the dashboard does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .forecast.pv import HourlyPv, Interval, intervals_from_wh_hours

_LOGGER = logging.getLogger(__name__)

ENERGY_PLATFORM = "energy"
FORECAST_FUNCTION = "async_get_solar_forecast"

# Integrations whose `wh_hours` timestamps close the period they name instead
# of opening it (see intervals_from_wh_hours). Forecast.Solar passes its API's
# `watt_hours_period` through unchanged: first key at sunrise, last at sunset.
PERIOD_END_DOMAINS = frozenset({"forecast_solar"})


@dataclass
class HourlyFetch:
    """The outcome of reading the configured forecast entries once."""

    pv: HourlyPv | None = None
    failed: list[str] = field(default_factory=list)


async def _forecast_function(hass: HomeAssistant, domain: str) -> Any | None:
    """The domain's `async_get_solar_forecast`, or None when it has none."""
    try:
        integration = await async_get_integration(hass, domain)
        if not integration.platforms_exists((ENERGY_PLATFORM,)):
            return None
        platform = await integration.async_get_platform(ENERGY_PLATFORM)
    except Exception:  # a broken third-party platform must not break us
        _LOGGER.debug("No usable energy platform in %s", domain, exc_info=True)
        return None
    return getattr(platform, FORECAST_FUNCTION, None)


async def async_solar_forecast_entries(hass: HomeAssistant) -> dict[str, str]:
    """Config entries that can deliver an hourly solar forecast: id -> label."""
    entries: dict[str, str] = {}
    supported: dict[str, bool] = {}
    for entry in hass.config_entries.async_entries():
        if entry.domain == DOMAIN:
            continue
        if entry.domain not in supported:
            supported[entry.domain] = await _forecast_function(hass, entry.domain) is not None
        if supported[entry.domain]:
            entries[entry.entry_id] = f"{entry.title} ({entry.domain})"
    return entries


async def async_energy_dashboard_forecast_entries(hass: HomeAssistant) -> list[str]:
    """The forecast entries the Energy dashboard's solar sources use."""
    try:
        from homeassistant.components.energy.data import (
            async_get_manager,
        )

        manager = await async_get_manager(hass)
    except Exception:  # the dashboard is a convenience here, never required
        _LOGGER.debug("Energy dashboard preferences unavailable", exc_info=True)
        return []
    found: list[str] = []
    for source in (manager.data or {}).get("energy_sources", []):
        if source.get("type") != "solar":
            continue
        for entry_id in source.get("config_entry_solar_forecast") or []:
            if entry_id not in found:
                found.append(entry_id)
    return found


async def async_fetch_hourly_pv(hass: HomeAssistant, entry_ids: list[str]) -> HourlyFetch:
    """Read and sum the hourly forecasts of `entry_ids`.

    All or nothing: when one entry delivers no curve, `pv` is None and the
    caller plans with the daily sensors. Summing only the entries that did
    answer would quietly drop a roof from the plan.
    """
    forecasts: list[list[Interval]] = []
    failed: list[str] = []
    for entry_id in entry_ids:
        intervals = await _fetch_one(hass, entry_id)
        if intervals:
            forecasts.append(intervals)
        else:
            failed.append(entry_id)
    if failed or not forecasts:
        return HourlyFetch(pv=None, failed=failed)
    return HourlyFetch(pv=HourlyPv(forecasts))


async def _fetch_one(hass: HomeAssistant, entry_id: str) -> list[Interval]:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        return []
    function = await _forecast_function(hass, entry.domain)
    if function is None:
        return []
    try:
        result = await function(hass, entry_id)
    except Exception:  # see _forecast_function
        _LOGGER.debug("Solar forecast of %s failed", entry_id, exc_info=True)
        return []
    wh_hours = result.get("wh_hours") if isinstance(result, dict) else None
    if not isinstance(wh_hours, dict):
        return []
    return intervals_from_wh_hours(
        wh_hours,
        period_end=entry.domain in PERIOD_END_DOMAINS,
        default_tz=dt_util.now().tzinfo,
    )
