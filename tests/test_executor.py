"""PlanExecutor: dry-run must not pretend scripts ran; fail-safe on stale plans."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from homeassistant.helpers.event import SCHEDULED_POINTS, fire_scheduled_point
from homeassistant.util import dt as dt_util

from smart_battery_pilot import executor as executor_module
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
        try:
            return asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return None


class _FakeCoordinator:
    def __init__(self, slots: list[PlanSlot], *, valid=True, enabled=True, dry_run=True, refresh=0):
        self.data = SBPData(plan=Plan(slots=slots), valid=valid)
        self.enabled = enabled
        self.dry_run = dry_run
        self.last_update_success = True
        self.last_applied = None
        self.persisted = 0
        self.listener_updates = 0
        self.charge_requests: list = []
        self.script_results: list = []
        self.closings: list = []
        self.reserve = 10
        self.block = False
        self.soc = None
        self.last_reserve_sent = None
        self.ev_policy_value = "allow"
        self.ev_entities: list[str] = []
        self.ev_power = None
        self.ev_threshold = 1000.0
        self.external = False
        self.refresh = refresh
        self._conf = {
            CONF_SCRIPT_CHARGE: "script.sbp_charge",
            CONF_SCRIPT_IDLE: "script.sbp_idle",
            CONF_SCRIPT_AUTO: "script.sbp_auto",
            CONF_SCRIPT_EXPORT: "script.sbp_export",
        }

    def conf(self, key, default=None):
        return self._conf.get(key, default)

    def script_refresh_interval(self):
        return self.refresh

    def async_add_listener(self, _listener):
        return lambda: None

    def async_update_listeners(self):
        self.listener_updates += 1

    def schedule_persist(self):
        self.persisted += 1

    def note_conditions(self):
        pass

    def charge_reading(self):
        return None

    def reserve_for_scripts(self):
        return self.reserve

    def reserve_block_enabled(self):
        return self.block

    def reserve_state(self):
        if self.reserve > 10:
            return float(self.reserve), "fixed", None
        return None, "off", None

    def live_soc(self):
        return self.soc

    def ev_policy(self):
        return self.ev_policy_value

    def ev_power_entities(self):
        return self.ev_entities

    def ev_power_w(self):
        return self.ev_power

    def ev_threshold_w(self):
        return self.ev_threshold

    def external_control_active(self):
        return self.external

    def note_script_result(self, action, entity_id, success):
        self.script_results.append((action, entity_id, success))

    def charge_observation(self, requested_w, now=None, closing=None):
        self.charge_requests.append(requested_w)
        self.closings.append(closing)

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
        power_w=power if action in (ACTION_CHARGE, ACTION_EXPORT) else 0.0,
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
    coord = _FakeCoordinator([_slot(ACTION_EXPORT, power=2500.0)], dry_run=False, enabled=True)
    executor = PlanExecutor(hass, coord)
    _run(executor.async_apply_current())
    domain, service, data, blocking = hass.services.calls[0]
    assert (domain, service, blocking) == ("script", "sbp_export", True)
    assert data == {"power_w": 2500, "reserve_soc": 10}


def test_consecutive_export_slots_reapply_new_power():
    hass = _FakeHass()
    coord = _FakeCoordinator([_slot(ACTION_EXPORT, power=2500.0)], dry_run=False, enabled=True)
    executor = PlanExecutor(hass, coord)
    _run(executor.async_apply_current())
    coord.data = SBPData(plan=Plan(slots=[_slot(ACTION_EXPORT, power=1200.0)]), valid=True)
    _run(executor.async_apply_current())
    assert hass.services.calls[-1][2] == {"power_w": 1200, "reserve_soc": 10}


def test_failed_refresh_with_stale_valid_plan_restores_auto():
    hass = _FakeHass()
    coord = _FakeCoordinator([_slot(ACTION_CHARGE)], dry_run=False, enabled=True)
    executor = PlanExecutor(hass, coord)
    _run(executor.async_apply_current())

    coord.last_update_success = False
    _run(executor.async_apply_current())
    assert hass.services.calls[-1][1] == "sbp_auto"


def test_a_hanging_script_does_not_block_forever(monkeypatch):
    """A control script that never returns must not wedge the executor.

    The call is awaited under the executor lock, and entry unload waits for
    that same lock - so without a bound, one stuck script hangs reloads and
    Home Assistant's shutdown.
    """
    monkeypatch.setattr(executor_module, "SCRIPT_CALL_TIMEOUT_SECONDS", 0.05)
    hass = _FakeHass()

    async def _never_returns(domain, service, data=None, blocking=False):
        hass.services.calls.append((domain, service, data or {}, blocking))
        if service == "sbp_charge":
            await asyncio.sleep(3600)

    hass.services.async_call = _never_returns
    coordinator = _FakeCoordinator([_slot(ACTION_CHARGE)], dry_run=False)
    executor = PlanExecutor(hass, coordinator)

    asyncio.run(asyncio.wait_for(executor.async_apply_current(), timeout=5))

    called = [service for _, service, _, _ in hass.services.calls]
    # Charge timed out, so the executor fell back to auto rather than claiming
    # a mode the inverter is not in.
    assert called == ["sbp_charge", "sbp_auto"]
    assert coordinator.last_applied == ACTION_AUTO


def test_a_hanging_script_does_not_block_unload(monkeypatch):
    monkeypatch.setattr(executor_module, "SCRIPT_CALL_TIMEOUT_SECONDS", 0.05)
    hass = _FakeHass()

    async def _never_returns(domain, service, data=None, blocking=False):
        await asyncio.sleep(3600)

    hass.services.async_call = _never_returns
    coordinator = _FakeCoordinator([_slot(ACTION_CHARGE)], dry_run=False)
    coordinator.last_applied = ACTION_CHARGE
    executor = PlanExecutor(hass, coordinator)

    # Would hang indefinitely without the timeout.
    asyncio.run(asyncio.wait_for(executor.async_stop(), timeout=5))


def test_every_script_call_reports_its_result():
    """A failed charge falls back to auto; the checks need to see both."""
    hass = _FakeHass()
    now = dt_util.now()
    slot = PlanSlot(
        start=now - timedelta(minutes=5),
        end=now + timedelta(minutes=55),
        action=ACTION_CHARGE,
        price=0.1,
        net_demand_kwh=0.5,
    )
    coord = _FakeCoordinator([slot], dry_run=False)
    hass.services.fail = True
    executor = PlanExecutor(hass, coord)
    asyncio.run(executor.async_apply_current())
    assert coord.script_results == [
        (ACTION_CHARGE, "script.sbp_charge", False),
        (ACTION_AUTO, "script.sbp_auto", False),
    ]


def test_refresh_timer_callbacks_repeat_only_refreshable_actions(monkeypatch):
    for action in (ACTION_IDLE, ACTION_CHARGE, ACTION_EXPORT):
        SCHEDULED_POINTS.clear()
        hass = _FakeHass()
        slot = _slot(action, hours=2)
        clock = {"now": slot.start + timedelta(minutes=5)}
        monkeypatch.setattr(executor_module.dt_util, "now", lambda clock=clock: clock["now"])
        coord = _FakeCoordinator([slot], dry_run=False, refresh=180)
        executor = PlanExecutor(hass, coord)

        async def scenario(executor=executor, hass=hass, clock=clock):
            await executor.async_apply_current()
            refresh_at = clock["now"] + timedelta(seconds=180)
            index = next(i for i, entry in enumerate(SCHEDULED_POINTS) if entry[2] == refresh_at)
            clock["now"] = refresh_at
            fire_scheduled_point(index)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        _run(scenario())
        assert [call[1] for call in hass.services.calls] == [f"sbp_{action}"] * 2
        assert executor.decisions[-1]["outcome"] == "applied"


def test_failed_refresh_is_not_success_and_does_not_loop(monkeypatch):
    SCHEDULED_POINTS.clear()
    hass = _FakeHass()
    slot = _slot(ACTION_CHARGE, hours=2)
    now = slot.start + timedelta(minutes=5)
    monkeypatch.setattr(executor_module.dt_util, "now", lambda: now)
    coord = _FakeCoordinator([slot], dry_run=False, refresh=180)
    executor = PlanExecutor(hass, coord)

    async def scenario():
        nonlocal now
        await executor.async_apply_current()
        confirmed_at = executor._refresh_success_at
        now += timedelta(seconds=180)
        hass.services.fail = True
        index = next(i for i, entry in enumerate(SCHEDULED_POINTS) if entry[2] == now)
        fire_scheduled_point(index)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert executor._refresh_success_at == confirmed_at

    _run(scenario())
    assert len(hass.services.calls) == 2
    assert executor.decisions[-1]["outcome"] == "failed_kept_running"
    assert any(entry[2] == now + timedelta(seconds=180) for entry in SCHEDULED_POINTS)


def test_stop_cancels_timed_action_refresh(monkeypatch):
    SCHEDULED_POINTS.clear()
    hass = _FakeHass()
    slot = _slot(ACTION_IDLE, hours=2)
    now = slot.start + timedelta(minutes=5)
    monkeypatch.setattr(executor_module.dt_util, "now", lambda: now)
    coord = _FakeCoordinator([slot], dry_run=False, refresh=180)
    executor = PlanExecutor(hass, coord)

    _run(executor.async_apply_current())
    assert executor._unsub_refresh_timer is not None
    _run(executor.async_stop(restore_auto=False))

    assert executor._unsub_refresh_timer is None
    assert executor._refresh_success_at is None


def test_zero_and_missing_refresh_interval_never_arm_refresh(monkeypatch):
    for refresh in (0, None):
        SCHEDULED_POINTS.clear()
        hass = _FakeHass()
        slot = _slot(ACTION_IDLE, hours=2)
        monkeypatch.setattr(
            executor_module.dt_util,
            "now",
            lambda slot=slot: slot.start + timedelta(minutes=5),
        )
        coord = _FakeCoordinator([slot], dry_run=False, refresh=refresh)
        executor = PlanExecutor(hass, coord)
        _run(executor.async_apply_current())
        assert executor._unsub_refresh_timer is None
        _run(executor.async_apply_current())
        assert len(hass.services.calls) == 1


def test_auto_is_never_periodically_refreshed():
    SCHEDULED_POINTS.clear()
    hass = _FakeHass()
    coord = _FakeCoordinator([_slot(ACTION_AUTO, hours=2)], dry_run=False, refresh=180)
    executor = PlanExecutor(hass, coord)
    _run(executor.async_apply_current())
    assert executor._unsub_refresh_timer is None


def test_delivered_refresh_callback_cannot_race_stop_into_a_script(monkeypatch):
    SCHEDULED_POINTS.clear()
    hass = _FakeHass()
    slot = _slot(ACTION_IDLE, hours=2)
    now = slot.start + timedelta(minutes=5)
    monkeypatch.setattr(executor_module.dt_util, "now", lambda: now)
    coord = _FakeCoordinator([slot], dry_run=False, refresh=180)
    executor = PlanExecutor(hass, coord)

    async def scenario():
        nonlocal now
        await executor.async_apply_current()
        now += timedelta(seconds=180)
        index = next(i for i, entry in enumerate(SCHEDULED_POINTS) if entry[2] == now)
        fire_scheduled_point(index)
        await executor.async_stop(restore_auto=False)
        await asyncio.sleep(0)

    _run(scenario())
    assert [call[1] for call in hass.services.calls] == ["sbp_idle"]
