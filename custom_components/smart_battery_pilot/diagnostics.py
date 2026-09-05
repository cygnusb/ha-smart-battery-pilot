"""Diagnostics dump for a config entry.

Everything an issue report needs: which price adapter matched, what the
forecast model is, and the head of the current plan. Entity ids are kept -
they are the whole point of a support dump and hold nothing private.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import SBPConfigEntry

# The plan can hold ~190 slots; the first day is enough to judge a plan.
MAX_DIAGNOSTIC_SLOTS = 96


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SBPConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data.coordinator
    executor = entry.runtime_data.executor
    data = coordinator.data

    diagnostics: dict[str, Any] = {
        "entry": {
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "runtime": {
            "enabled": coordinator.enabled,
            "dry_run": coordinator.dry_run,
            "last_applied": coordinator.last_applied,
            "last_update_success": coordinator.last_update_success,
        },
    }

    if data is None:
        diagnostics["state"] = "no data yet"
        return diagnostics

    current = executor.current_slot()
    diagnostics["state"] = {
        "valid": data.valid,
        "error": data.error,
        "price_adapter": data.adapter_name,
        "model_type": data.model_type,
        "training_samples": data.training_samples,
        "soc": data.soc,
        "updated_at": data.updated_at.isoformat() if data.updated_at else None,
        "consumption_forecast_24h_kwh": data.consumption_forecast_24h_kwh,
        "pv_forecast_24h_kwh": data.pv_forecast_24h_kwh,
        "current_action": current.action if current else None,
    }
    diagnostics["plan"] = {
        "total_slots": len(data.plan.slots),
        "grid_charge_kwh": round(data.plan.grid_charge_kwh, 3),
        "battery_discharge_kwh": round(data.plan.battery_discharge_kwh, 3),
        "estimated_savings_eur": data.plan.estimated_savings_eur,
        "warnings": list(data.plan.warnings),
        "slots": [
            {
                "start": slot.start.isoformat(),
                "end": slot.end.isoformat(),
                "action": slot.action,
                "price": round(slot.price, 4),
                "net_demand_kwh": round(slot.net_demand_kwh, 3),
                "pv_kwh": round(slot.pv_kwh, 3),
                "power_w": slot.power_w,
                "charge_power_w": slot.power_w,
                "discharge_kwh": slot.discharge_kwh,
                "soc_forecast": slot.soc_forecast,
            }
            for slot in data.plan.slots[:MAX_DIAGNOSTIC_SLOTS]
        ],
    }
    return diagnostics
