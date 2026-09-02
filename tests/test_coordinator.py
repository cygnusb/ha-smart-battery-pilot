"""Coordinator: price fail-safe, actual-savings accounting, replan service."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import UpdateFailed

from smart_battery_pilot.const import (
    CONF_BATTERY_CHARGE_ENERGY_ENTITY,
    CONF_BATTERY_DISCHARGE_ENERGY_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_SOC_ENTITY,
)
from smart_battery_pilot.coordinator import SBPCoordinator, SBPData
from smart_battery_pilot.optimizer import Plan, PlanSlot


TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 1, 15, 3, 10, tzinfo=TZ)


class _States:
    def __init__(self) -> None:
        self._data: dict = {}

    def get(self, entity_id):
        return self._data.get(entity_id)

    def set(self, entity_id, state, attributes=None):
        self._data[entity_id] = SimpleNamespace(
            state=str(state) if state is not None else "unknown",
            attributes=attributes or {},
        )


class _Services:
    def __init__(self) -> None:
        self._handlers: dict = {}

    def has_service(self, domain, service):
        return (domain, service) in self._handlers

    def async_register(self, domain, service, handler):
        self._handlers[(domain, service)] = handler

    def async_remove(self, domain, service):
        self._handlers.pop((domain, service), None)

    async def async_call_handler(self, domain, service):
        await self._handlers[(domain, service)](None)


class _ConfigEntries:
    def __init__(self) -> None:
        self._entries: list = []

    def async_entries(self, domain):
        return list(self._entries)


class _FakeHass:
    def __init__(self) -> None:
        self.states = _States()
        self.services = _Services()
        self.config_entries = _ConfigEntries()
        self.http = SimpleNamespace(async_register_static_paths=self._noop_async)
        self.data: dict = {}

    async def _noop_async(self, *args, **kwargs):
        return None

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def async_create_task(self, coro):
        if asyncio.iscoroutine(coro):
            coro.close()
        return None


def _run(coro):
    return asyncio.run(coro)


def _plan_slot(action: str, price: float, start: datetime, hours: float = 1.0) -> PlanSlot:
    return PlanSlot(
        start=start,
        end=start + timedelta(hours=hours),
        action=action,
        price=price,
        net_demand_kwh=1.0,
    )


def _coordinator(hass, **data) -> SBPCoordinator:
    entry = ConfigEntry(
        data={
            CONF_PRICE_ENTITY: "sensor.price",
            CONF_SOC_ENTITY: "sensor.soc",
            CONF_BATTERY_CHARGE_ENERGY_ENTITY: "sensor.charge_energy",
            CONF_BATTERY_DISCHARGE_ENERGY_ENTITY: "sensor.discharge_energy",
            **data,
        }
    )
    return SBPCoordinator(hass, entry)


def test_unavailable_meter_does_not_reset_baseline():
    hass = _FakeHass()
    coord = _coordinator(hass)
    night = _plan_slot("charge", 0.10, NOW.replace(hour=3, minute=0))
    plan = Plan(slots=[night])

    hass.states.set("sensor.charge_energy", 1500.0)
    hass.states.set("sensor.discharge_energy", 800.0)
    coord._update_actual_savings(plan, NOW)

    hass.states.set("sensor.charge_energy", "unavailable")
    coord._update_actual_savings(plan, NOW)
    assert coord._acc_charge_kwh == 0.0
    assert coord._acc_savings_eur == 0.0

    hass.states.set("sensor.charge_energy", 1500.0)
    coord._update_actual_savings(plan, NOW)
    assert coord._acc_charge_kwh == 0.0
    assert coord._acc_savings_eur == 0.0


def test_actual_savings_uses_current_slot_price_not_horizon_mean():
    hass = _FakeHass()
    coord = _coordinator(hass)
    night = _plan_slot("charge", 0.10, NOW.replace(hour=3, minute=0))
    tomorrow_night = _plan_slot("charge", 0.50, NOW.replace(day=16, hour=3, minute=0))
    plan = Plan(slots=[night, tomorrow_night])

    hass.states.set("sensor.charge_energy", 10.0)
    hass.states.set("sensor.discharge_energy", 5.0)
    coord._update_actual_savings(plan, NOW)

    hass.states.set("sensor.charge_energy", 12.0)
    hass.states.set("sensor.discharge_energy", 5.0)
    coord._update_actual_savings(plan, NOW)

    # 2 kWh charged at the *current* 0.10 slot, not mean(0.10, 0.50)=0.30
    assert coord._acc_charge_kwh == pytest.approx(2.0)
    assert coord._acc_savings_eur == pytest.approx(-0.20)


def test_discharge_only_meter_does_not_count_full_value_as_profit():
    hass = _FakeHass()
    entry = ConfigEntry(
        data={
            CONF_BATTERY_DISCHARGE_ENERGY_ENTITY: "sensor.discharge_energy",
        }
    )
    coord = SBPCoordinator(hass, entry)
    slot = _plan_slot("auto", 0.40, NOW.replace(hour=3, minute=0))
    plan = Plan(slots=[slot])

    hass.states.set("sensor.discharge_energy", 5.0)
    coord._update_actual_savings(plan, NOW)
    hass.states.set("sensor.discharge_energy", 7.0)
    coord._update_actual_savings(plan, NOW)

    assert coord._acc_discharge_kwh == pytest.approx(2.0)
    assert coord._acc_savings_eur == 0.0


def test_price_unavailable_after_success_returns_invalid_plan():
    hass = _FakeHass()
    coord = _coordinator(hass)
    coord.data = SBPData(plan=Plan(slots=[_plan_slot("charge", 0.1, NOW)]), valid=True)
    hass.states.set("sensor.price", "unavailable")

    result = _run(coord._async_update_data())
    assert result.valid is False
    assert result.error == "price_unavailable"


def test_price_unavailable_on_first_refresh_still_raises():
    hass = _FakeHass()
    coord = _coordinator(hass)
    hass.states.set("sensor.price", "unavailable")

    try:
        _run(coord._async_update_data())
    except UpdateFailed:
        return
    raise AssertionError("first refresh with no prices must raise UpdateFailed")


class _RefreshCoordinator:
    def __init__(self) -> None:
        self.refreshes = 0

    async def async_request_refresh(self):
        self.refreshes += 1


def test_replan_service_follows_reloaded_entry():
    """Service must not keep the coordinator closed over at first setup."""
    from smart_battery_pilot import _async_handle_replan

    hass = _FakeHass()
    first = ConfigEntry(entry_id="old")
    first.runtime_data = SimpleNamespace(coordinator=_RefreshCoordinator())
    hass.config_entries._entries = [first]
    _run(_async_handle_replan(hass, None))
    assert first.runtime_data.coordinator.refreshes == 1

    reloaded = ConfigEntry(entry_id="old")
    reloaded.runtime_data = SimpleNamespace(coordinator=_RefreshCoordinator())
    hass.config_entries._entries = [reloaded]
    _run(_async_handle_replan(hass, None))
    assert first.runtime_data.coordinator.refreshes == 1
    assert reloaded.runtime_data.coordinator.refreshes == 1
