"""Executor side of the backup reserve: reserve_soc, re-send on change, block."""

from __future__ import annotations

import asyncio

from test_executor import _FakeCoordinator, _FakeHass, _slot

from smart_battery_pilot.const import ACTION_AUTO, ACTION_CHARGE, ACTION_IDLE
from smart_battery_pilot.executor import PlanExecutor


def _run(coro):
    return asyncio.run(coro)


def _calls(hass):
    return [(service, data) for _, service, data, _ in hass.services.calls]


def _live(action, **kwargs):
    hass = _FakeHass()
    coord = _FakeCoordinator([_slot(action)], dry_run=False, **kwargs)
    return hass, coord, PlanExecutor(hass, coord)


def test_every_script_gets_reserve_soc():
    hass, coord, executor = _live(ACTION_IDLE)
    _run(executor.async_apply_current())
    assert _calls(hass) == [("sbp_idle", {"reserve_soc": 10})]

    hass, coord, executor = _live(ACTION_CHARGE)
    _run(executor.async_apply_current())
    assert _calls(hass) == [("sbp_charge", {"power_w": 4000, "reserve_soc": 10})]


def test_a_successful_call_remembers_the_reserve_sent():
    hass, coord, executor = _live(ACTION_IDLE)
    _run(executor.async_apply_current())
    assert coord.last_reserve_sent == 10


def test_a_changed_reserve_is_re_sent_with_the_same_action():
    hass, coord, executor = _live(ACTION_IDLE)
    _run(executor.async_apply_current())
    coord.reserve = 30
    _run(executor.async_apply_current())
    assert _calls(hass)[-1] == ("sbp_idle", {"reserve_soc": 30})
    assert executor.decisions[-1]["outcome"] == "reserve_updated"
    assert coord.last_reserve_sent == 30


def test_an_unchanged_reserve_is_not_re_sent():
    hass, coord, executor = _live(ACTION_IDLE)
    _run(executor.async_apply_current())
    _run(executor.async_apply_current())
    assert len(hass.services.calls) == 1
    assert executor.decisions[-1]["outcome"] == "unchanged"


def test_after_a_restart_a_reserve_already_sent_is_not_re_sent():
    hass, coord, executor = _live(ACTION_IDLE)
    coord.last_applied = ACTION_IDLE
    coord.last_reserve_sent = 10
    _run(executor.async_apply_current())
    assert hass.services.calls == []


def test_dry_run_never_sends_a_reserve_update():
    hass = _FakeHass()
    coord = _FakeCoordinator([_slot(ACTION_IDLE)], dry_run=True)
    executor = PlanExecutor(hass, coord)
    coord.reserve = 30
    _run(executor.async_apply_current())
    assert hass.services.calls == []


def test_a_failed_call_does_not_remember_the_reserve():
    hass, coord, executor = _live(ACTION_IDLE)
    hass.services.fail = True
    _run(executor.async_apply_current())
    assert coord.last_reserve_sent is None


# --- discharge block -------------------------------------------------------------


def _blocking(soc):
    hass, coord, executor = _live(ACTION_AUTO)
    coord.block, coord.reserve, coord.soc = True, 30, soc
    return hass, coord, executor


def test_the_block_replaces_auto_with_idle_at_the_reserve():
    hass, coord, executor = _blocking(soc=30.0)
    _run(executor.async_apply_current())
    assert _calls(hass) == [("sbp_idle", {"reserve_soc": 30})]
    assert executor.decisions[-1]["outcome"] == "reserve_block"


def test_the_block_releases_only_two_points_above_the_reserve():
    hass, coord, executor = _blocking(soc=30.0)
    _run(executor.async_apply_current())
    coord.soc = 31.0
    _run(executor.async_apply_current())
    assert _calls(hass)[-1][0] == "sbp_idle"
    coord.soc = 32.0
    _run(executor.async_apply_current())
    assert _calls(hass)[-1] == ("sbp_auto", {"reserve_soc": 30})


def test_the_block_leaves_charge_slots_alone():
    hass, coord, executor = _live(ACTION_CHARGE)
    coord.block, coord.reserve, coord.soc = True, 30, 20.0
    _run(executor.async_apply_current())
    assert _calls(hass)[0][0] == "sbp_charge"


def test_without_the_option_auto_stays_auto_below_the_reserve():
    hass, coord, executor = _live(ACTION_AUTO)
    coord.reserve, coord.soc = 30, 20.0
    _run(executor.async_apply_current())
    assert _calls(hass)[0][0] == "sbp_auto"


def test_a_soc_change_queues_an_apply_only_when_the_block_flips():
    hass, coord, executor = _blocking(soc=40.0)
    queued = []
    executor._queue_apply = lambda: queued.append(True)
    executor._handle_soc_change(35.0)
    assert queued == []
    executor._handle_soc_change(30.0)
    assert queued == [True]
