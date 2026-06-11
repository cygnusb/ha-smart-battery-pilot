"""Sensors exposing the charge plan and forecasts."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import SBPConfigEntry
from .const import ACTION_AUTO, ATTR_SLOTS
from .coordinator import SBPCoordinator
from .entity import SBPEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SBPConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        [
            CurrentActionSensor(coordinator),
            NextActionSensor(coordinator),
            ChargePlanSensor(coordinator),
            SavingsSensor(coordinator),
            ConsumptionForecastSensor(coordinator),
        ]
    )


def _find_current_slot(coordinator: SBPCoordinator):
    data = coordinator.data
    if not data or not data.valid:
        return None
    now = dt_util.now()
    for slot in data.plan.slots:
        if slot.start <= now < slot.end:
            return slot
    return None


class CurrentActionSensor(SBPEntity, SensorEntity):
    """The action the plan prescribes right now."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["charge", "auto", "idle", "export", "unknown"]

    def __init__(self, coordinator: SBPCoordinator) -> None:
        super().__init__(coordinator, "current_action")

    @property
    def native_value(self) -> str:
        slot = _find_current_slot(self.coordinator)
        return slot.action if slot else "unknown"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        slot = _find_current_slot(self.coordinator)
        if not slot:
            return {}
        return {
            "slot_start": slot.start.isoformat(),
            "slot_end": slot.end.isoformat(),
            "price": slot.price,
            "charge_power_w": slot.charge_power_w,
            "enabled": self.coordinator.enabled,
            "dry_run": self.coordinator.dry_run,
        }


class NextActionSensor(SBPEntity, SensorEntity):
    """The next action that differs from the current one."""

    def __init__(self, coordinator: SBPCoordinator) -> None:
        super().__init__(coordinator, "next_action")

    def _next_change(self):
        data = self.coordinator.data
        if not data or not data.valid:
            return None
        current = _find_current_slot(self.coordinator)
        current_action = current.action if current else ACTION_AUTO
        now = dt_util.now()
        for slot in data.plan.slots:
            if slot.start > now and slot.action != current_action:
                return slot
        return None

    @property
    def native_value(self) -> str | None:
        slot = self._next_change()
        return slot.action if slot else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        slot = self._next_change()
        if not slot:
            return {}
        return {
            "start": slot.start.isoformat(),
            "price": slot.price,
            "charge_power_w": slot.charge_power_w,
        }


class ChargePlanSensor(SBPEntity, SensorEntity):
    """Full plan as attributes; state is the number of planned slots."""

    def __init__(self, coordinator: SBPCoordinator) -> None:
        super().__init__(coordinator, "charge_plan")

    @property
    def native_value(self) -> int:
        data = self.coordinator.data
        return len(data.plan.slots) if data and data.valid else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if not data:
            return {}
        return {
            ATTR_SLOTS: [
                {
                    "start": s.start.isoformat(),
                    "end": s.end.isoformat(),
                    "action": s.action,
                    "price": round(s.price, 4),
                    "net_demand_kwh": round(s.net_demand_kwh, 3),
                    "pv_kwh": round(s.pv_kwh, 3),
                    "charge_power_w": s.charge_power_w,
                    "discharge_kwh": s.discharge_kwh,
                    "soc_forecast": s.soc_forecast,
                }
                for s in data.plan.slots
            ],
            "grid_charge_kwh": round(data.plan.grid_charge_kwh, 2),
            "battery_discharge_kwh": round(data.plan.battery_discharge_kwh, 2),
            "price_adapter": data.adapter_name,
            "updated_at": data.updated_at.isoformat() if data.updated_at else None,
            "error": data.error,
        }


class SavingsSensor(SBPEntity, SensorEntity):
    """Estimated savings of the current plan horizon."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "EUR"
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: SBPCoordinator) -> None:
        super().__init__(coordinator, "estimated_savings")

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        return data.plan.estimated_savings_eur if data and data.valid else None


class ConsumptionForecastSensor(SBPEntity, SensorEntity):
    """Forecasted household consumption for the next 24h."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = "kWh"
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: SBPCoordinator) -> None:
        super().__init__(coordinator, "consumption_forecast")

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        return data.consumption_forecast_24h_kwh if data and data.valid else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if not data:
            return {}
        return {
            "model_type": data.model_type,
            "training_samples": data.training_samples,
            "pv_forecast_24h_kwh": data.pv_forecast_24h_kwh,
        }
