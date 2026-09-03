"""PlanExecutor: dry-run must not pretend scripts ran; fail-safe on stale plans."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from homeassistant.util import dt as dt_util

from smart_battery_pilot.const import (
    ACTION_AUTO,
    ACTION_CHARGE,
    ACTION_EXPORT,
    ACTION_IDLE,
    CONF_SCRIPT_AUTO,
    CONF_SCRIPT_CHARGE,
    CONF_SCRIPT_EXPORT,
    CONF_SCRIPT_IDLE,
)
from smart_battery_pilot.coordinator import SBPData
from smart_battery_pilot.executor import PlanExecutor
from smart_battery_pilot.optimizer import Plan, PlanSlot


class _FakeServices:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, bool]] = []
        self.fail = False

    async def async_call(self, domain, service, data=None, blocking=False):
        self.calls.append((domain, service, data or {}, blocking))
        if self.fail:
            raise RuntimeError("script failed")


class _FakeHass:
    def __init__(self) -> None:
        self.services = _FakeServices()

    def async_create_task(self, coro):
        if asyncio.iscoroutine(coro):
            coro.close()
        return


class _FakeCoordinator:
    def __init__(self, slots: list[PlanSlot], *, valid=True, enabled=True, dry_run=True):
        self.data = SBPData(plan=Plan(slots=slots), valid=valid)
        self.enabled = enabled
        self.dry_run = dry_run
        self.last_update_success = True
        self.last_applied = None
        self.persisted = 0
        self.listener_updates = 0
        self._conf = {
            CONF_SCRIPT_CHARGE: "script.sbp_charge",
            CONF_SCRIPT_IDLE: "script.sbp_idle",
            CONF_SCRIPT_AUTO: "script.sbp_auto",
            CONF_SCRIPT_EXPORT: "script.sbp_export",
        }

    def conf(self, key, default=None):
        return self._conf.get(key, default)

    def async_add_listener(self, _listener):
        return lambda: None

    def async_update_listeners(self):
        self.listener_updates += 1

    def schedule_persist(self):
        self.persisted += 1

    async def async_persist(self):
        self.persisted += 1


def _slot(action: str, *, hours: float = 1.0, power: float = 4000.0) -> PlanSlot:
    now = dt_util.now()
    return PlanSlot(
        start=now - timedelta(minutes=5),
        end=now + timedelta(hours=hours),
        action=action,
        price=0.10,
        net_demand_kwh=1.0,
        charge_power_w=power if action in (ACTION_CHARGE, ACTION_EXPORT) else 0.0,
    )


def _run(coro):
    return asyncio.run(coro)


def test_dry_run_does_not_call_scripts():
    hass = _FakeHass()
    coord = _FakeCoordinator([_slot(ACTION_CHARGE)], dry_run=True, enabled=True)
    executor = PlanExecutor(hass, coord)
    _run(executor.async_apply_current())
    assert hass.services.calls == []


def test_leaving_dry_run_applies_current_action():
    hass = _FakeHass()
    coord = _FakeCoordinator([_slot(ACTION_IDLE)], dry_run=True, enabled=True)
    executor = PlanExecutor(hass, coord)
    _run(executor.async_apply_current())
    assert hass.services.calls == []

    coord.dry_run = False
    _run(executor.async_apply_current())
    assert [(d, s) for d, s, _data, _b in hass.services.calls] == [("script", "sbp_idle")]


def test_entering_dry_run_after_live_charge_restores_auto():
    hass = _FakeHass()
    coord = _FakeCoordinator([_slot(ACTION_CHARGE)], dry_run=False, enabled=True)
    executor = PlanExecutor(hass, coord)
    _run(executor.async_apply_current())
    assert hass.services.calls[-1][1] == "sbp_charge"

    coord.dry_run = True
    _run(executor.async_apply_current())
    assert hass.services.calls[-1][1] == "sbp_auto"


def test_stop_during_dry_run_only_does_not_call_auto():
    hass = _FakeHass()
    coord = _FakeCoordinator([_slot(ACTION_IDLE)], dry_run=True, enabled=True)
    executor = PlanExecutor(hass, coord)
    _run(executor.async_apply_current())
    _run(executor.async_stop(restore_auto=True))
    assert hass.services.calls == []


def test_invalid_plan_restores_auto_when_live():
    hass = _FakeHass()
    coord = _FakeCoordinator([_slot(ACTION_CHARGE)], dry_run=False, enabled=True)
    executor = PlanExecutor(hass, coord)
    _run(executor.async_apply_current())

    coord.data = SBPData(plan=Plan(), valid=False, error="price_unavailable")
    _run(executor.async_apply_current())
    assert hass.services.calls[-1][1] == "sbp_auto"


def test_scripts_are_called_blocking():
    hass = _FakeHass()
    coord = _FakeCoordinator([_slot(ACTION_CHARGE)], dry_run=False, enabled=True)
    executor = PlanExecutor(hass, coord)
    _run(executor.async_apply_current())
    assert hass.services.calls[0][3] is True


def test_missing_export_script_does_not_stick_last_applied():
    hass = _FakeHass()
    coord = _FakeCoordinator([_slot(ACTION_EXPORT)], dry_run=False, enabled=True)
    coord._conf.pop(CONF_SCRIPT_EXPORT)
    executor = PlanExecutor(hass, coord)
    _run(executor.async_apply_current())
    assert executor._last_applied == ACTION_AUTO
    assert hass.services.calls[-1][1] == "sbp_auto"

    _run(executor.async_apply_current())
    assert [c[1] for c in hass.services.calls].count("sbp_auto") >= 1


def test_failed_script_is_retried_next_apply():
    hass = _FakeHass()
    hass.services.fail = True
    coord = _FakeCoordinator([_slot(ACTION_IDLE)], dry_run=False, enabled=True)
    executor = PlanExecutor(hass, coord)
    _run(executor.async_apply_current())
    assert executor._last_applied is None

    hass.services.fail = False
    _run(executor.async_apply_current())
    assert hass.services.calls[-1][1] == "sbp_idle"
    assert executor._last_applied == ACTION_IDLE


def test_export_passes_power_w():
    hass = _FakeHass()
    coord = _FakeCoordinator(
        [_slot(ACTION_EXPORT, power=2500.0)], dry_run=False, enabled=True
    )
    executor = PlanExecutor(hass, coord)
    _run(executor.async_apply_current())
    domain, service, data, blocking = hass.services.calls[0]
    assert (domain, service, blocking) == ("script", "sbp_export", True)
    assert data == {"power_w": 2500}


def test_consecutive_export_slots_reapply_new_power():
    hass = _FakeHass()
    coord = _FakeCoordinator(
        [_slot(ACTION_EXPORT, power=2500.0)], dry_run=False, enabled=True
    )
    executor = PlanExecutor(hass, coord)
    _run(executor.async_apply_current())
    coord.data = SBPData(plan=Plan(slots=[_slot(ACTION_EXPORT, power=1200.0)]), valid=True)
    _run(executor.async_apply_current())
    assert hass.services.calls[-1][2] == {"power_w": 1200}


def test_failed_refresh_with_stale_valid_plan_restores_auto():
    hass = _FakeHass()
    coord = _FakeCoordinator([_slot(ACTION_CHARGE)], dry_run=False, enabled=True)
    executor = PlanExecutor(hass, coord)
    _run(executor.async_apply_current())

    coord.last_update_success = False
    _run(executor.async_apply_current())
    assert hass.services.calls[-1][1] == "sbp_auto"
