"""Coordinator: price fail-safe, actual-savings accounting, replan service."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest

from smart_battery_pilot.const import (
    ACTION_AUTO,
    ACTION_CHARGE,
    CONF_BATTERY_CHARGE_ENERGY_ENTITY,
    CONF_BATTERY_DISCHARGE_ENERGY_ENTITY,
    CONF_FEED_IN_TARIFF,
    CONF_PRICE_ENTITY,
    CONF_SOC_ENTITY,
    DOMAIN,
    SERVICE_REPLAN,
)
from smart_battery_pilot.coordinator import (
    MAX_CONDITION_SAMPLES,
    SBPCoordinator,
    SBPData,
)
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

    async def async_unload_platforms(self, entry, platforms):
        return True


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
        return


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
    coord.last_applied = ACTION_CHARGE
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


def test_single_meter_does_not_accumulate_anything():
    """One meter alone cannot produce a net figure, so nothing is counted.

    Accumulating anyway would mean that adding the second meter later starts
    from a one-sided total that never washes out.
    """
    hass = _FakeHass()
    coord = _coordinator(hass, **{CONF_BATTERY_CHARGE_ENERGY_ENTITY: None})
    plan = Plan(slots=[_plan_slot("auto", 0.30, NOW)])

    hass.states.set("sensor.discharge_energy", 5.0)
    coord._update_actual_savings(plan, NOW)
    hass.states.set("sensor.discharge_energy", 7.0)
    coord._update_actual_savings(plan, NOW)

    assert coord._acc_discharge_kwh == 0.0
    assert coord._acc_savings_eur == 0.0
    assert coord._has_energy_entities() is False


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


class _StopExecutor:
    async def async_stop(self, restore_auto=True):
        return None


class _ShutdownCoordinator:
    async def async_shutdown(self):
        return None


def _loaded_entry(entry_id: str = "only"):
    from smart_battery_pilot import SBPRuntimeData

    entry = ConfigEntry(entry_id=entry_id)
    entry.runtime_data = SBPRuntimeData(_ShutdownCoordinator(), _StopExecutor())
    return entry


def test_reload_of_only_entry_keeps_replan_service():
    from smart_battery_pilot import async_setup, async_unload_entry

    hass = _FakeHass()
    _run(async_setup(hass, {}))
    assert hass.services.has_service(DOMAIN, SERVICE_REPLAN)

    entry = _loaded_entry()
    entry.runtime_data.reloading = True
    hass.config_entries._entries = [entry]
    _run(async_unload_entry(hass, entry))
    assert hass.services.has_service(DOMAIN, SERVICE_REPLAN)


def test_real_unload_of_last_entry_removes_replan_service():
    from smart_battery_pilot import async_setup, async_unload_entry

    hass = _FakeHass()
    _run(async_setup(hass, {}))
    entry = _loaded_entry()
    hass.config_entries._entries = [entry]
    _run(async_unload_entry(hass, entry))
    assert not hass.services.has_service(DOMAIN, SERVICE_REPLAN)


def test_setup_reregisters_replan_after_it_was_removed():
    from smart_battery_pilot import _ensure_replan_service, async_setup

    hass = _FakeHass()
    _run(async_setup(hass, {}))
    hass.services.async_remove(DOMAIN, SERVICE_REPLAN)
    assert not hass.services.has_service(DOMAIN, SERVICE_REPLAN)
    _ensure_replan_service(hass)
    assert hass.services.has_service(DOMAIN, SERVICE_REPLAN)


def test_auto_mode_charge_is_priced_at_feed_in_not_import():
    hass = _FakeHass()
    coord = _coordinator(hass, **{CONF_FEED_IN_TARIFF: 0.08})
    coord.last_applied = ACTION_AUTO
    slot = _plan_slot("auto", 0.40, NOW.replace(hour=3, minute=0))
    plan = Plan(slots=[slot])

    hass.states.set("sensor.charge_energy", 10.0)
    hass.states.set("sensor.discharge_energy", 5.0)
    coord._update_actual_savings(plan, NOW)
    hass.states.set("sensor.charge_energy", 12.0)
    hass.states.set("sensor.discharge_energy", 5.0)
    coord._update_actual_savings(plan, NOW)

    assert coord._acc_charge_kwh == pytest.approx(2.0)
    assert coord._acc_savings_eur == pytest.approx(-0.16)


def test_wh_energy_meters_are_converted_to_kwh():
    hass = _FakeHass()
    coord = _coordinator(hass)
    coord.last_applied = ACTION_CHARGE
    slot = _plan_slot("charge", 0.10, NOW.replace(hour=3, minute=0))
    plan = Plan(slots=[slot])
    attributes = {"unit_of_measurement": "Wh"}

    hass.states.set("sensor.charge_energy", 1_500_000, attributes)
    hass.states.set("sensor.discharge_energy", 800_000, attributes)
    coord._update_actual_savings(plan, NOW)
    hass.states.set("sensor.charge_energy", 1_502_000, attributes)
    hass.states.set("sensor.discharge_energy", 800_000, attributes)
    coord._update_actual_savings(plan, NOW)

    assert coord._acc_charge_kwh == pytest.approx(2.0)
    assert coord._acc_savings_eur == pytest.approx(-0.20)


def test_actual_savings_averages_price_across_the_interval():
    hass = _FakeHass()
    coord = _coordinator(hass)
    coord.last_applied = ACTION_CHARGE
    t0 = NOW.replace(hour=3, minute=0)
    mid = NOW.replace(hour=3, minute=15)
    t1 = NOW.replace(hour=3, minute=30)
    plan = Plan(
        slots=[
            _plan_slot("charge", 0.10, t0, hours=0.25),
            _plan_slot("charge", 0.30, mid, hours=0.25),
        ]
    )

    hass.states.set("sensor.charge_energy", 10.0)
    hass.states.set("sensor.discharge_energy", 5.0)
    coord._update_actual_savings(plan, t0)
    # The executor samples at every slot boundary; without that the whole
    # interval would be billed at the price that happened to start it.
    coord._note_conditions(plan, mid)
    hass.states.set("sensor.charge_energy", 12.0)
    hass.states.set("sensor.discharge_energy", 5.0)
    coord._update_actual_savings(plan, t1)

    assert coord._acc_charge_kwh == pytest.approx(2.0)
    assert coord._acc_savings_eur == pytest.approx(-0.40)


def test_charge_is_priced_by_the_mode_in_force_not_the_mode_at_read_time():
    """The executor has moved on to the next slot by the time the meter is read.

    Pricing grid charging at the feed-in tariff because `last_applied` already
    says "auto" inflates the savings by the whole spread, every single day the
    plan grid-charges.
    """
    hass = _FakeHass()
    coord = _coordinator(hass)
    t0 = NOW.replace(hour=2, minute=0)
    t1 = NOW.replace(hour=3, minute=5)
    charge_slot = _plan_slot("charge", 0.32, t0, hours=1.0)
    plan = Plan(slots=[charge_slot])

    coord.last_applied = ACTION_CHARGE
    hass.states.set("sensor.charge_energy", 10.0)
    hass.states.set("sensor.discharge_energy", 5.0)
    coord._update_actual_savings(plan, t0)

    # 03:00: the slot ends, the executor releases to auto.
    coord._note_conditions(plan, NOW.replace(hour=3, minute=0))
    coord.last_applied = ACTION_AUTO

    hass.states.set("sensor.charge_energy", 14.0)
    hass.states.set("sensor.discharge_energy", 5.0)
    coord._update_actual_savings(plan, t1)

    # 60 of the 65 interval minutes fall in the charge slot, 5 after it, so
    # the weighted import price is 0.32 * 60/65. What matters is that the
    # grid charge is billed at the import price at all: the old code read the
    # momentary `last_applied` ("auto") and charged the 0.08 feed-in tariff,
    # which would have made this -0.32 and inflated savings by ~0.86 EUR.
    assert coord._acc_charge_kwh == pytest.approx(4.0)
    assert coord._acc_savings_eur == pytest.approx(-4 * 0.32 * 60 / 65)
    assert coord._acc_savings_eur < -1.0


def test_expired_slots_still_price_the_interval_they_covered():
    """`plan.slots` only ever holds future slots, so pricing cannot rely on it.

    The slot that covered the measured interval is gone from the plan by the
    time the delta is settled; the recorded samples have to carry it.
    """
    hass = _FakeHass()
    coord = _coordinator(hass)
    coord.last_applied = ACTION_CHARGE
    t0 = NOW.replace(hour=2, minute=0)
    t1 = NOW.replace(hour=3, minute=0)

    cheap = Plan(slots=[_plan_slot("charge", 0.08, t0, hours=1.0)])
    hass.states.set("sensor.charge_energy", 10.0)
    hass.states.set("sensor.discharge_energy", 5.0)
    coord._update_actual_savings(cheap, t0)

    # An hour later the cheap slot has expired out of the plan entirely.
    expensive = Plan(slots=[_plan_slot("charge", 0.32, t1, hours=1.0)])
    hass.states.set("sensor.charge_energy", 14.0)
    hass.states.set("sensor.discharge_energy", 5.0)
    coord._update_actual_savings(expensive, t1)

    assert coord._acc_savings_eur == pytest.approx(-0.32)  # 4 kWh at 0.08


def test_condition_samples_do_not_grow_without_meters():
    """Only the savings accounting trims them, and it never runs without meters."""
    hass = _FakeHass()
    coord = _coordinator(
        hass,
        **{
            CONF_BATTERY_CHARGE_ENERGY_ENTITY: None,
            CONF_BATTERY_DISCHARGE_ENERGY_ENTITY: None,
        },
    )
    for minute in range(120):
        moment = NOW.replace(hour=1, minute=0) + timedelta(minutes=minute)
        coord._note_conditions(Plan(slots=[_plan_slot("auto", minute / 100.0, moment)]), moment)

    assert coord._conditions == []


def test_condition_samples_are_capped_when_trimming_stalls():
    hass = _FakeHass()
    coord = _coordinator(hass)
    for minute in range(MAX_CONDITION_SAMPLES + 50):
        moment = NOW.replace(hour=1, minute=0) + timedelta(minutes=minute)
        coord._note_conditions(Plan(slots=[_plan_slot("auto", minute / 100.0, moment)]), moment)

    assert len(coord._conditions) == MAX_CONDITION_SAMPLES
