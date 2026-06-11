"""Base entity for Smart Battery Pilot."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SBPCoordinator


class SBPEntity(CoordinatorEntity[SBPCoordinator]):
    """Common device info and naming."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SBPCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry_id = coordinator.entry.entry_id
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Smart Battery Pilot",
            manufacturer="Smart Battery Pilot",
            model="Battery charge optimizer",
        )
