"""One script call per slot unless something changed, and a failed update
within a running slot must not cancel it."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

from test_executor import _FakeCoordinator, _FakeHass, _run, _slot

from smart_battery_pilot.const import ACTION_AUTO, ACTION_CHARGE, ACTION_EXPORT
from smart_battery_pilot.coordinator import SBPData
from smart_battery_pilot.executor import PlanExecutor
from smart_battery_pilot.optimizer import Plan


def _live(action=ACTION_CHARGE, power=2268.0):
    hass = _FakeHass()
    slot = _slot(action, power=power)
    coord = _FakeCoordinator([slot], dry_run=False, enabled=True)
    return hass, coord, PlanExecutor(hass, coord), slot


def _replan(coord, slot, **changes):
    """A refresh that plans the running slot again."""
    coord.data = SBPData(plan=Plan(slots=[replace(slot, **changes)]), valid=True)


def _next_slot(coord, slot):
    """The following slot, asking for the same. Still covers now for the fakes."""
    later = replace(slot, start=slot.start - timedelta(minutes=15))
    coord.data = SBPData(plan=Plan(slots=[later]), valid=True)


def _scripts(hass):
    return [call[1] for call in hass.services.calls]


def _fail_charge_only(hass):
    calls = hass.services.calls

    async def async_call(domain, service, data=None, blocking=False):
        calls.append((domain, service, data or {}, blocking))
        if service == "sbp_charge":
            raise RuntimeError("script failed")

    hass.services.async_call = async_call


def test_a_replan_without_change_does_not_call_the_charge_script_again():
    hass, coord, executor, slot = _live()
    _run(executor.async_apply_current())
    _replan(coord, slot)
    _run(executor.async_apply_current())
    assert _scripts(hass) == ["sbp_charge"]
    assert executor.decisions[-1]["outcome"] == "unchanged"


def test_a_replan_without_change_does_not_call_the_export_script_again():
    hass, coord, executor, slot = _live(ACTION_EXPORT, power=2500.0)
    _run(executor.async_apply_current())
    _replan(coord, slot)
    _run(executor.async_apply_current())
    assert _scripts(hass) == ["sbp_export"]


def test_a_change_of_less_than_half_a_watt_is_no_change():
    # The script receives whole watts, so it would get the same value.
    hass, coord, executor, slot = _live(power=2268.0)
    _run(executor.async_apply_current())
    _replan(coord, slot, power_w=2268.3)
    _run(executor.async_apply_current())
    assert _scripts(hass) == ["sbp_charge"]


def test_a_trigger_during_the_script_does_not_call_it_again():
    class SlowServices:
        def __init__(self):
            self.calls = []

        async def async_call(self, domain, service, data=None, blocking=False):
            self.calls.append((domain, service, data or {}, blocking))
            await asyncio.sleep(0.01)

    class TaskHass:
        def __init__(self):
            self.services = SlowServices()
            self.tasks = []

        def async_create_task(self, coro):
            task = asyncio.ensure_future(coro)
            self.tasks.append(task)
            return task

    async def scenario():
        hass = TaskHass()
        coord = _FakeCoordinator([_slot(ACTION_CHARGE)], dry_run=False, enabled=True)
        executor = PlanExecutor(hass, coord)
        executor._handle_coordinator_update()
        await asyncio.sleep(0)  # the first run is inside the script now
        assert len(hass.services.calls) == 1
        executor._handle_coordinator_update()
        await asyncio.gather(*hass.tasks)
        return hass

    assert _scripts(asyncio.run(scenario())) == ["sbp_charge"]


def test_a_new_power_within_the_slot_is_sent():
    hass, coord, executor, slot = _live(power=2268.0)
    _run(executor.async_apply_current())
    _replan(coord, slot, power_w=1800.0)
    _run(executor.async_apply_current())
    assert _scripts(hass) == ["sbp_charge", "sbp_charge"]
    assert hass.services.calls[-1][2]["power_w"] == 1800


def test_a_new_reserve_within_the_slot_is_sent():
    hass, coord, executor, slot = _live()
    _run(executor.async_apply_current())
    coord.reserve = 20
    _replan(coord, slot)
    _run(executor.async_apply_current())
    assert _scripts(hass) == ["sbp_charge", "sbp_charge"]
    assert hass.services.calls[-1][2]["reserve_soc"] == 20


def test_the_next_slot_is_sent_even_when_it_asks_for_the_same():
    # Inverters with a command timeout may rely on this refresh.
    hass, coord, executor, slot = _live()
    _run(executor.async_apply_current())
    _next_slot(coord, slot)
    _run(executor.async_apply_current())
    assert _scripts(hass) == ["sbp_charge", "sbp_charge"]


def test_a_failed_update_within_the_slot_keeps_the_charge_running():
    hass, coord, executor, slot = _live(power=2268.0)
    _run(executor.async_apply_current())
    hass.services.fail = True
    _replan(coord, slot, power_w=1800.0)
    _run(executor.async_apply_current())
    assert _scripts(hass) == ["sbp_charge", "sbp_charge"]
    assert coord.last_applied == ACTION_CHARGE
    assert executor.decisions[-1]["outcome"] == "failed_kept_running"
    assert coord.script_results[-1] == (ACTION_CHARGE, "script.sbp_charge", False)


def test_a_failed_update_is_retried_by_the_next_run():
    hass, coord, executor, slot = _live(power=2268.0)
    _run(executor.async_apply_current())
    hass.services.fail = True
    _replan(coord, slot, power_w=1800.0)
    _run(executor.async_apply_current())
    hass.services.fail = False
    _run(executor.async_apply_current())
    assert hass.services.calls[-1][2]["power_w"] == 1800
    assert executor.decisions[-1]["outcome"] == "applied"


def test_a_failed_update_is_retried_even_when_the_plan_returns_to_the_old_power():
    # The script may have stopped halfway, so the old values are not certain.
    hass, coord, executor, slot = _live(power=2268.0)
    _run(executor.async_apply_current())
    hass.services.fail = True
    _replan(coord, slot, power_w=1800.0)
    _run(executor.async_apply_current())
    hass.services.fail = False
    _replan(coord, slot, power_w=2268.0)
    _run(executor.async_apply_current())
    assert _scripts(hass) == ["sbp_charge"] * 3


def test_a_failed_first_call_of_a_slot_still_falls_back_to_auto():
    hass, coord, executor, _ = _live()
    _fail_charge_only(hass)
    _run(executor.async_apply_current())
    assert _scripts(hass) == ["sbp_charge", "sbp_auto"]
    assert coord.last_applied == ACTION_AUTO
    assert executor.decisions[-1]["outcome"] == "failed_fell_back_to_auto"


def test_a_failed_call_of_the_next_slot_still_falls_back_to_auto():
    hass, coord, executor, slot = _live()
    _run(executor.async_apply_current())
    _fail_charge_only(hass)
    _next_slot(coord, slot)
    _run(executor.async_apply_current())
    assert _scripts(hass) == ["sbp_charge", "sbp_charge", "sbp_auto"]
    assert coord.last_applied == ACTION_AUTO


def test_the_charge_is_sent_again_after_a_fallback_within_the_slot():
    hass, coord, executor, slot = _live()
    original = hass.services.async_call
    _fail_charge_only(hass)
    _run(executor.async_apply_current())
    assert coord.last_applied == ACTION_AUTO
    hass.services.async_call = original
    _replan(coord, slot)
    _run(executor.async_apply_current())
    assert _scripts(hass) == ["sbp_charge", "sbp_auto", "sbp_charge"]
    assert coord.last_applied == ACTION_CHARGE


def test_the_charge_is_sent_again_when_another_controller_let_go():
    hass, coord, executor, _ = _live()
    _run(executor.async_apply_current())
    coord.external = True
    _run(executor.async_apply_current())
    coord.external = False
    _run(executor.async_apply_current())
    assert _scripts(hass) == ["sbp_charge", "sbp_charge"]


def test_a_failed_send_after_another_controller_falls_back_to_auto():
    # It overwrote the inverter, so no charge is left to keep running.
    hass, coord, executor, _ = _live()
    _run(executor.async_apply_current())
    coord.external = True
    _run(executor.async_apply_current())
    coord.external = False
    _fail_charge_only(hass)
    _run(executor.async_apply_current())
    assert _scripts(hass) == ["sbp_charge", "sbp_charge", "sbp_auto"]
    assert coord.last_applied == ACTION_AUTO


def test_an_unchanged_charge_continues_the_observation():
    # Reporting None here would end the charge-rate sample seconds into the slot.
    _, coord, executor, slot = _live(power=4000.0)
    _run(executor.async_apply_current())
    _replan(coord, slot)
    _run(executor.async_apply_current())
    assert coord.charge_requests == [4000.0, 4000.0]


def test_a_kept_charge_is_observed_at_the_power_still_in_force():
    hass, coord, executor, slot = _live(power=4000.0)
    _run(executor.async_apply_current())
    hass.services.fail = True
    _replan(coord, slot, power_w=3000.0)
    _run(executor.async_apply_current())
    assert coord.charge_requests == [4000.0, 4000.0]
