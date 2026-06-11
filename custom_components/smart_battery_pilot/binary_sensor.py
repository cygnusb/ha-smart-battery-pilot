"""Plan validity binary sensor."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SBPConfigEntry
from .coordinator import SBPCoordinator
from .entity import SBPEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SBPConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([PlanValidSensor(entry.runtime_data.coordinator)])


class PlanValidSensor(SBPEntity, BinarySensorEntity):
    """On when a valid plan exists."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: SBPCoordinator) -> None:
        super().__init__(coordinator, "plan_problem")

    @property
    def is_on(self) -> bool:
        """Problem class: on = there IS a problem."""
        data = self.coordinator.data
        return not (data and data.valid and data.plan.slots)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        return {"error": data.error if data else "no_data"}
