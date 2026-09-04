"""Entry setup helpers that run before the coordinator exists."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry

from smart_battery_pilot import _ensure_unique_id
from smart_battery_pilot.const import CONF_SOC_ENTITY


class _ConfigEntries:
    def __init__(self) -> None:
        self.updates: list[tuple[str, str | None]] = []

    def async_update_entry(self, entry, **changes):
        entry.unique_id = changes.get("unique_id", entry.unique_id)
        self.updates.append((entry.entry_id, entry.unique_id))


class _Hass:
    def __init__(self) -> None:
        self.config_entries = _ConfigEntries()


def test_unique_id_is_backfilled_for_pre_0_6_entries():
    """Without this the duplicate-entry guard never matches an upgraded entry."""
    hass = _Hass()
    entry = ConfigEntry(data={CONF_SOC_ENTITY: "sensor.byd_soc"})

    _ensure_unique_id(hass, entry)

    assert entry.unique_id == "sensor.byd_soc"
    assert hass.config_entries.updates == [("test", "sensor.byd_soc")]


def test_existing_unique_id_is_left_alone():
    hass = _Hass()
    entry = ConfigEntry(
        data={CONF_SOC_ENTITY: "sensor.byd_soc"}, unique_id="sensor.other"
    )

    _ensure_unique_id(hass, entry)

    assert entry.unique_id == "sensor.other"
    assert hass.config_entries.updates == []


def test_entry_without_soc_entity_is_not_touched():
    hass = _Hass()
    entry = ConfigEntry(data={})

    _ensure_unique_id(hass, entry)

    assert entry.unique_id is None
    assert hass.config_entries.updates == []
