"""Restart safety and concurrency of the plan executor."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from homeassistant.util import dt as dt_util

from smart_battery_pilot.const import (
    ACTION_AUTO,
    ACTION_CHARGE,
    ACTION_IDLE,
    CONF_SCRIPT_AUTO,
    CONF_SCRIPT_CHARGE,
    CONF_SCRIPT_IDLE,
)
from smart_battery_pilot.coordinator import SBPData
from smart_battery_pilot.executor import PlanExecutor
from smart_battery_pilot.optimizer import Plan, PlanSlot


class _Services:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.delay = 0.0

    async def async_call(self, domain, service, data=None, blocking=False):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.calls.append(service)


class _Hass:
    def __init__(self) -> None:
        self.services = _Services()

    def async_create_task(self, coro):
        if asyncio.iscoroutine(coro):
            coro.close()
        return


class _Coordinator:
    """Stands in for SBPCoordinator, including the persisted last_applied."""

    def __init__(self, slots, *, valid=True, last_applied=None):
        self.data = SBPData(plan=Plan(slots=slots), valid=valid)
        self.enabled = True
        self.dry_run = False
        self.last_update_success = True
        self.last_applied = last_applied
        self.persist_calls = 0
        self.delayed_persist = 0
        self.immediate_persist = 0
        self._conf = {
            CONF_SCRIPT_CHARGE: "script.sbp_charge",
            CONF_SCRIPT_IDLE: "script.sbp_idle",
            CONF_SCRIPT_AUTO: "script.sbp_auto",
        }

    def conf(self, key, default=None):
        return self._conf.get(key, default)

    def async_add_listener(self, _listener):
        return lambda: None

    def async_update_listeners(self):
        pass

    def schedule_persist(self):
        self.persist_calls += 1
        self.delayed_persist += 1

    def note_conditions(self):
        pass

    async def async_persist(self):
        self.persist_calls += 1
        self.immediate_persist += 1


def _slot(action, *, hours=1.0):
    now = dt_util.now()
    return PlanSlot(
        start=now - timedelta(minutes=5),
        end=now + timedelta(hours=hours),
        action=action,
        price=0.10,
        net_demand_kwh=1.0,
        charge_power_w=4000.0 if action == ACTION_CHARGE else 0.0,
    )


def test_restart_with_stale_forced_mode_releases_the_battery():
    """HA restarted mid-charge and the price entity is not up yet."""
    hass = _Hass()
    coord = _Coordinator([], valid=False, last_applied=ACTION_CHARGE)
    executor = PlanExecutor(hass, coord)

    asyncio.run(executor.async_apply_current())

    assert hass.services.calls == ["sbp_auto"]
    assert coord.last_applied == ACTION_AUTO


def test_restart_with_nothing_applied_calls_no_script():
    """A fresh install must not poke the inverter just because it started."""
    hass = _Hass()
    coord = _Coordinator([], valid=False, last_applied=None)
    executor = PlanExecutor(hass, coord)

    asyncio.run(executor.async_apply_current())

    assert hass.services.calls == []


def test_applied_action_is_written_through_for_persistence():
    hass = _Hass()
    coord = _Coordinator([_slot(ACTION_IDLE)])
    executor = PlanExecutor(hass, coord)

    asyncio.run(executor.async_apply_current())

    assert coord.last_applied == ACTION_IDLE
    assert coord.persist_calls >= 1


def test_applied_action_is_persisted_immediately():
    """A crash within the 60 s delay must not forget that the inverter is forced."""
    hass = _Hass()
    coord = _Coordinator([_slot(ACTION_IDLE)])
    executor = PlanExecutor(hass, coord)

    asyncio.run(executor.async_apply_current())

    assert coord.last_applied == ACTION_IDLE
    assert coord.immediate_persist >= 1
    assert coord.delayed_persist == 0


def test_setup_failure_still_releases_a_forced_mode():
    """Price entity not up yet: setup aborts, but the battery is released."""
    hass = _Hass()
    coord = _Coordinator([], valid=False, last_applied=ACTION_CHARGE)
    executor = PlanExecutor(hass, coord)

    asyncio.run(executor.async_release_stale_mode())

    assert hass.services.calls == ["sbp_auto"]
    assert coord.last_applied == ACTION_AUTO


def test_setup_failure_is_a_noop_when_nothing_was_forced():
    hass = _Hass()
    coord = _Coordinator([], valid=False, last_applied=ACTION_AUTO)
    executor = PlanExecutor(hass, coord)

    asyncio.run(executor.async_release_stale_mode())

    assert hass.services.calls == []


def test_concurrent_applies_do_not_interleave():
    """Two triggers at once must not leave last_applied describing the loser."""
    hass = _Hass()
    hass.services.delay = 0.01
    coord = _Coordinator([_slot(ACTION_CHARGE)])
    executor = PlanExecutor(hass, coord)

    async def race():
        first = asyncio.create_task(executor.async_apply_current())
        await asyncio.sleep(0)  # let the first run reach its script call
        coord.data = SBPData(plan=Plan(slots=[_slot(ACTION_IDLE)]), valid=True)
        second = asyncio.create_task(executor.async_apply_current())
        await asyncio.gather(first, second)

    asyncio.run(race())

    # The second run is the later truth, so it must also be the final one.
    assert hass.services.calls == ["sbp_charge", "sbp_idle"]
    assert coord.last_applied == ACTION_IDLE


class _RunningHass(_Hass):
    """Runs the tasks the executor creates instead of discarding them."""

    def __init__(self) -> None:
        super().__init__()
        self.tasks: list[asyncio.Task] = []

    def async_create_task(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(task)
        return task

    async def drain(self) -> None:
        while self.tasks:
            pending, self.tasks = self.tasks, []
            await asyncio.gather(*pending)


class _ListeningCoordinator(_Coordinator):
    """Calls its listeners, the way DataUpdateCoordinator really does."""

    def __init__(self, slots, **kw) -> None:
        super().__init__(slots, **kw)
        self._listeners: list = []

    def async_add_listener(self, listener):
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def async_update_listeners(self):
        for listener in list(self._listeners):
            listener()


def test_slot_boundary_applies_once_not_twice():
    """The boundary refresh re-enters the coordinator listener synchronously.

    Without deduplication that queues one apply, and `_fire` queues a second;
    the lock serialises them but charge and export are exempt from the
    "same action, skip" short-circuit, so the script runs twice per boundary.
    """
    from homeassistant.helpers import event as event_stub

    event_stub.SCHEDULED_POINTS.clear()
    hass = _RunningHass()
    coord = _ListeningCoordinator([_slot(ACTION_CHARGE)])
    executor = PlanExecutor(hass, coord)

    async def scenario():
        await executor.async_start()
        await hass.drain()
        assert hass.services.calls == ["sbp_charge"]

        hass.services.calls.clear()
        event_stub.fire_scheduled_point()  # the slot boundary
        await hass.drain()

    asyncio.run(scenario())

    assert hass.services.calls == ["sbp_charge"]


def test_apply_queued_before_unload_does_not_revive_a_forced_mode():
    """A task created just before the unload is already scheduled.

    Dropping the listener does not cancel it; it waits on the lock that
    async_stop holds and would otherwise re-arm the timer and re-apply the
    forced mode behind an entry that no longer exists.
    """
    from homeassistant.helpers import event as event_stub

    event_stub.SCHEDULED_POINTS.clear()
    hass = _RunningHass()
    coord = _ListeningCoordinator([_slot(ACTION_CHARGE)])
    executor = PlanExecutor(hass, coord)

    async def scenario():
        await executor.async_start()
        await hass.drain()
        hass.services.calls.clear()

        executor._queue_apply()          # in flight when the unload starts
        await executor.async_stop(restore_auto=True)
        await hass.drain()

    asyncio.run(scenario())

    # Only the restore, and no timer left behind.
    assert hass.services.calls == ["sbp_auto"]
    assert event_stub.SCHEDULED_POINTS == []
