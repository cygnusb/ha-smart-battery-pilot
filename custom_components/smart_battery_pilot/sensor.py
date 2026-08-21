"""Sensors exposing the charge plan and forecasts."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import SBPConfigEntry
from .const import (
    ACTION_AUTO,
    ATTR_SLOTS,
    CONF_CAPACITY_KWH,
    CONF_DISCHARGE_MODE,
    CONF_EFFICIENCY,
    CONF_FEED_IN_TARIFF,
    CONF_MAX_CHARGE_POWER_W,
    CONF_MAX_DISCHARGE_POWER_W,
    CONF_MAX_SOC,
    CONF_MIN_SOC,
    CONF_PRICE_OFFSET,
    CONF_SPREAD_THRESHOLD,
    CONF_TRAINING_DAYS,
    DEFAULT_DISCHARGE_MODE,
    DEFAULT_EFFICIENCY,
    DEFAULT_FEED_IN_TARIFF,
    DEFAULT_MAX_SOC,
    DEFAULT_MIN_SOC,
    DEFAULT_PRICE_OFFSET,
    DEFAULT_SPREAD_THRESHOLD,
    DEFAULT_TRAINING_DAYS,
)
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
            CurrentPriceSensor(coordinator),
            ChargePlanSensor(coordinator),
            PlanStatusSensor(coordinator),
            SavingsSensor(coordinator),
            ActualSavingsEurSensor(coordinator),
            ActualSavingsKwhSensor(coordinator),
            ConsumptionForecastSensor(coordinator),
            ConfigSensor(coordinator),
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

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["charge", "auto", "idle", "export", "no_change"]

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
    def native_value(self) -> str:
        slot = self._next_change()
        return slot.action if slot else "no_change"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        slot = self._next_change()
        if not slot:
            return {}
        now = dt_util.now()
        delta = slot.start - now
        minutes = int(delta.total_seconds() / 60)
        return {
            "start": slot.start.isoformat(),
            "in_minutes": minutes,
            "price": slot.price,
            "charge_power_w": slot.charge_power_w,
        }


class CurrentPriceSensor(SBPEntity, SensorEntity):
    """Current electricity price from the active plan slot."""

    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_suggested_display_precision = 4
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: SBPCoordinator) -> None:
        super().__init__(coordinator, "current_price")

    @property
    def native_value(self) -> float | None:
        slot = _find_current_slot(self.coordinator)
        return round(slot.price, 4) if slot else None


class ChargePlanSensor(SBPEntity, SensorEntity):
    """Full plan as attributes; state is the number of actively planned (non-auto) slots."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: SBPCoordinator) -> None:
        super().__init__(coordinator, "charge_plan")

    @property
    def native_value(self) -> int:
        data = self.coordinator.data
        if not data or not data.valid:
            return 0
        return sum(1 for s in data.plan.slots if s.action != ACTION_AUTO)

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
            "total_slots": len(data.plan.slots),
            "grid_charge_kwh": round(data.plan.grid_charge_kwh, 2),
            "battery_discharge_kwh": round(data.plan.battery_discharge_kwh, 2),
            "price_adapter": data.adapter_name,
            "updated_at": data.updated_at.isoformat() if data.updated_at else None,
            "error": data.error,
        }


class PlanStatusSensor(SBPEntity, SensorEntity):
    """Plan validity status — diagnostic, not recorded."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["ok", "no_price_data", "no_soc", "no_price_adapter", "error"]
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SBPCoordinator) -> None:
        super().__init__(coordinator, "plan_status")

    @property
    def native_value(self) -> str:
        data = self.coordinator.data
        if not data:
            return "error"
        if not data.valid:
            error_map = {
                "no_price_data": "no_price_data",
                "soc_unavailable": "no_soc",
            }
            return error_map.get(data.error or "", "error")
        return "ok"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if not data:
            return {}
        return {
            "error_detail": data.error,
            "adapter": data.adapter_name,
            "updated_at": data.updated_at.isoformat() if data.updated_at else None,
            "model_type": data.model_type,
            "training_samples": data.training_samples,
        }


class SavingsSensor(SBPEntity, SensorEntity):
    """Estimated savings of the current plan horizon."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "EUR"
    _attr_suggested_display_precision = 2
    _attr_state_class = SensorStateClass.TOTAL

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


class ConfigSensor(SBPEntity, SensorEntity):
    """Active integration configuration — diagnostic, not intended for automations."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, coordinator: SBPCoordinator) -> None:
        super().__init__(coordinator, "konfiguration")

    @property
    def native_value(self) -> str:
        return "aktiv" if self.coordinator.enabled else "inaktiv"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self.coordinator.conf
        return {
            "kapazitaet_kwh": c(CONF_CAPACITY_KWH, 10.0),
            "min_soc_prozent": c(CONF_MIN_SOC, DEFAULT_MIN_SOC),
            "max_soc_prozent": c(CONF_MAX_SOC, DEFAULT_MAX_SOC),
            "wirkungsgrad_prozent": c(CONF_EFFICIENCY, DEFAULT_EFFICIENCY),
            "mindest_preisdifferenz_eur_kwh": c(CONF_SPREAD_THRESHOLD, DEFAULT_SPREAD_THRESHOLD),
            "entlade_modus": c(CONF_DISCHARGE_MODE, DEFAULT_DISCHARGE_MODE),
            "preisaufschlag_eur_kwh": c(CONF_PRICE_OFFSET, DEFAULT_PRICE_OFFSET),
            "einspeiseverguetung_eur_kwh": c(CONF_FEED_IN_TARIFF, DEFAULT_FEED_IN_TARIFF),
            "training_tage": c(CONF_TRAINING_DAYS, DEFAULT_TRAINING_DAYS),
            "dry_run": self.coordinator.dry_run,
        }


class ActualSavingsEurSensor(SBPEntity, SensorEntity):
    """Accumulated actual savings in EUR since integration start.

    Only available when battery charge/discharge energy entities are configured.
    Uses actual energy meter deltas correlated with plan prices.
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "EUR"
    _attr_suggested_display_precision = 2
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: SBPCoordinator) -> None:
        super().__init__(coordinator, "actual_savings_eur")

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        return data.actual_savings_eur if data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if not data:
            return {}
        return {
            "charge_kwh_total": data.actual_charge_kwh,
            "discharge_kwh_total": data.actual_discharge_kwh,
        }


class ActualSavingsKwhSensor(SBPEntity, SensorEntity):
    """Net kWh benefit: battery discharge minus grid charge since integration start.

    Only available when battery charge/discharge energy entities are configured.
    Positive = net energy benefit from arbitrage, negative = net loss.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = "kWh"
    _attr_suggested_display_precision = 2
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: SBPCoordinator) -> None:
        super().__init__(coordinator, "actual_savings_kwh")

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if data is None or data.actual_discharge_kwh is None or data.actual_charge_kwh is None:
            return None
        return round(data.actual_discharge_kwh - data.actual_charge_kwh, 2)
