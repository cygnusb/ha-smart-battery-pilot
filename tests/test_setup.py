"""Entry setup helpers that run before the coordinator exists."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry

from smart_battery_pilot import _ensure_unique_id
from smart_battery_pilot.const import CONF_SOC_ENTITY


class _ConfigEntries:
    def __init__(self, entries: list[ConfigEntry] | None = None) -> None:
        self.updates: list[tuple[str, str | None]] = []
        self._entries = entries or []

    def async_entries(self, _domain):
        return list(self._entries)

    def async_update_entry(self, entry, **changes):
        entry.unique_id = changes.get("unique_id", entry.unique_id)
        self.updates.append((entry.entry_id, entry.unique_id))


class _Hass:
    def __init__(self, entries: list[ConfigEntry] | None = None) -> None:
        self.config_entries = _ConfigEntries(entries)


def test_unique_id_is_backfilled_for_pre_0_6_entries():
    """Without this the duplicate-entry guard never matches an upgraded entry."""
    entry = ConfigEntry(data={CONF_SOC_ENTITY: "sensor.byd_soc"})
    hass = _Hass([entry])

    _ensure_unique_id(hass, entry)

    assert entry.unique_id == "sensor.byd_soc"
    assert hass.config_entries.updates == [("test", "sensor.byd_soc")]


def test_matching_unique_id_is_not_rewritten():
    entry = ConfigEntry(
        data={CONF_SOC_ENTITY: "sensor.byd_soc"}, unique_id="sensor.byd_soc"
    )
    hass = _Hass([entry])

    _ensure_unique_id(hass, entry)

    assert hass.config_entries.updates == []


def test_unique_id_follows_a_soc_entity_changed_in_the_options():
    """The options flow writes soc_entity to `options`; the guard reads the id.

    Left behind, the id points at the old inverter and a second entry for the
    new one would sail past the "one battery, one plan" check.
    """
    entry = ConfigEntry(
        data={CONF_SOC_ENTITY: "sensor.old_soc"},
        options={CONF_SOC_ENTITY: "sensor.new_soc"},
        unique_id="sensor.old_soc",
    )
    hass = _Hass([entry])

    _ensure_unique_id(hass, entry)

    assert entry.unique_id == "sensor.new_soc"


def test_unique_id_claimed_by_another_entry_is_left_alone():
    """Two entries sharing one unique id is worse than one stale id."""
    other = ConfigEntry(
        data={CONF_SOC_ENTITY: "sensor.byd_soc"},
        entry_id="other",
        unique_id="sensor.byd_soc",
    )
    entry = ConfigEntry(
        data={CONF_SOC_ENTITY: "sensor.byd_soc"}, unique_id="sensor.stale"
    )
    hass = _Hass([other, entry])

    _ensure_unique_id(hass, entry)

    assert entry.unique_id == "sensor.stale"
    assert hass.config_entries.updates == []


def test_entry_without_soc_entity_is_not_touched():
    entry = ConfigEntry(data={})
    hass = _Hass([entry])

    _ensure_unique_id(hass, entry)

    assert entry.unique_id is None
    assert hass.config_entries.updates == []
